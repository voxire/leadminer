# 355 — `dedup()` under concurrency: safe today, but one scheduling race and one permanent-write race

## Verdict

`dedup()` (`dedup.py:197`) is **thread-safe today, and not by accident of internal design
but by accident of call-site ordering**: it is invoked exactly once, from the main thread,
at `main.py:180`, which is provably after every scraper pool has joined (`main.py:160-168`)
and provably before the 40-thread `check_websites` pool is ever created (`enricher.py:237`,
reached via `main.py:187`). It holds no module-level mutable state (`dedup.py:198-199` are
function-local) and it never mutates its input (`dedup.py:210,218` copy; `_merge` builds a
fresh dict at `dedup.py:191-194`), so nothing about it can corrupt under the 40-thread /
5-worker / 3-scraper topology.

The problem is one level down: **the 3 scraper threads each sample their own `utc_now_iso()`
at different instants** (`osm.py:32`, `wikidata.py:67`, `google_places.py:173`), and
`dedup` uses that timestamp as the third rank element for volatile fields (`dedup.py:183`).
For `website`, Google Places and Wikidata tie on trust *and* validity, so the recency
tiebreak decides — and recency is thread-scheduling noise. Then `_trust_for` takes
`max()` over the *union* of sources (`dedup.py:148-152`), so whichever random value won is
re-stamped with every source's trust and becomes **permanently unwinnable** in the
cumulative master. The race is not just run-to-run noise; it is a one-shot write of a
coin-flip into a file that only ever grows.

---

## Findings

### S1 — `_trust_for` maxes over the *union* of sources, so a merged row claims trust it never earned and its fields become permanently unwinnable

- **Where:** `dedup.py:148-152` (`best = max(best, _SOURCE_TRUST.get(src, {}).get(field, _DEFAULT_TRUST))`)
  over `_sources_of(record)` at `dedup.py:143-145`, combined with the source union at
  `dedup.py:173-174` (`"|".join(sorted(_sources_of(a) | _sources_of(b)))`).
  Persisted by `main.py:209` → reloaded by `main.py:77-105`.
- **Breaks:** `_pick` calls `rank(rec, value)` where `rec` is the *merged* record
  (`dedup.py:179-185`), and `_trust_for` walks every source that contributed **to the
  group**, not to **this field**. So a `google_places` website that landed in a row merely
  because OSM had none is immediately re-graded as OSM-grade (`website` trust 3,
  `dedup.py:103`) instead of Google-grade (2, `dedup.py:100`). From that point on the row
  beats *every future* `google_places` website forever — including a correct, freshly
  scraped replacement. `load_master` turns the CSV's empty cells back into `None`
  (`main.py:88-89`), and `_is_missing(None)` is `True` (`dedup.py:86-87`), so the inflated
  grade survives every subsequent run. There is no path by which it decays.
- **Trigger** (verified, no network):
  ```python
  osm  = {"name":"Cafe XL","source":"osm",         "website":None,
          "email":"real@osm.example","phone":"01123456"}
  goog = {"name":"Cafe XL","source":"google_places","website":"https://GOOGLE-listing.example","email":None}
  m = _merge(osm, goog)
  m["website"]                      # -> 'https://GOOGLE-listing.example'  (came from Google)
  _trust_for("website", m)          # -> 3   <-- inflated to OSM's grade; OSM had no website
  _merge(m, dict(goog, scraped_at="2027-01-01T00:00:00+00:00",
                     website="https://RENEWED-google.example"))["website"]
                                    # -> 'https://GOOGLE-listing.example'  (2027 value discarded)
  ```
  This is **order-independent** (verified over both merge orders) — that is what makes it
  dangerous rather than merely flaky: it is a silent, monotonic, self-reinforcing
  corruption of `data/all_businesses.csv`.
- **Why this is the S1 and not the race below:** the race (S2) picks a wrong value
  *nondeterministically*; this turns that wrong value *permanent*. It also silently
  discards a field that a later scrape deliberately corrected — data loss, invisible,
  with no log line and no diff.
- **Concurrency relevance:** this is the mechanism by which a one-microsecond scheduling
  difference becomes a write-once decision in an append-only master. Fixing only the S2
  race leaves the freeze in place for every row already poisoned.
- **Fix:** carry per-field provenance through `_merge` and grade `_trust_for` against the
  source that actually supplied the value, not against the union. Concretely, have `_pick`
  return `(value, winning_source)`, accumulate `_src_by_field` in `_merge`
  (`dedup.py:190-194`), and have `_trust_for` (`dedup.py:148-152`) prefer
  `record["_src_by_field"][field]` when present. The private key costs nothing downstream:
  `write_csv` already drops unknown columns via `extrasaction="ignore"`
  (`main.py:125`), and `_merge` iterates `set(a) | set(b)` (`dedup.py:192`) so it will not
  re-pick the provenance dict itself as long as `_pick` special-cases it the way it already
  special-cases `source` (`dedup.py:173`).

---

### S2 — `website` selection is decided by which scraper thread called `utc_now_iso()` first

- **Where:** `dedup.py:183` (`_recency(rec) if field in _VOLATILE else ""`), with
  `website ∈ _VOLATILE` (`dedup.py:114`). The timestamps come from `osm.py:32`,
  `wikidata.py:67`, `google_places.py:173` — all sampled as the first statement of
  `scrape()`, i.e. **inside** the three threads spawned at `main.py:161`, from
  `utc_now_iso()` at `httpclient.py:220-229` (microsecond resolution).
- **Breaks:** `website` is the only field where two *real* scrapers tie on trust *and* on
  validity. Enumerating the full trust matrix from `dedup.py:97-110`:

  | volatile field | tied sources at equal trust | winner |
  |---|---|---|
  | `website` | `google_places`(2) vs `wikidata`(2) | **recency → thread scheduling** |
  | `rating` | `osm`(1) vs `wikidata`(1) | str tiebreak (neither scraper emits a rating — `osm.py:114`, `wikidata.py:123` — so unreachable today) |
  | `review_count` | `osm`(1) vs `wikidata`(1) | unreachable today |
  | `website_live` | all three at 1 | unreachable: all three scrapers emit `None` (`osm.py:112`, `wikidata.py:120`, `google_places.py:292`), and `_is_missing` short-circuits at `dedup.py:168-169` |

  So `website` is the one live exposure. `_validity("website", v)` returns 3 for any
  parseable URL (`dedup.py:126-127`), and Google/Wikidata URLs both parse, so validity
  never separates them either. What is left is the recency element.
- **Trigger** (verified; identical scraper payloads, identical merge order, only the
  thread-start order differs):
  ```python
  G = {"name":"Cafe XL","source":"google_places","website":"https://GOOGLE-listing.example",
       "scraped_at":"2026-10-03T10:00:00.000100+00:00"}
  W = {"name":"Cafe XL","source":"wikidata",     "website":"https://WIKIDATA-site.example",
       "scraped_at":"2026-10-03T10:00:00.000900+00:00"}
  dedup([G, W])[0]["website"]   # -> 'https://WIKIDATA-site.example'

  G2, W2 = dict(G, scraped_at=W["scraped_at"]), dict(W, scraped_at=G["scraped_at"])
  dedup([G2, W2])[0]["website"] # -> 'https://GOOGLE-listing.example'   <-- flipped
  ```
  `G2, W2` is the same two API responses arriving in the same run; only the order in
  which `ThreadPoolExecutor` happened to start the three threads differs. On a 2-vCPU
  GitHub runner (`main.py:160` spins up 3 threads at once) start order is not stable.
- **Downstream blast radius:** the flipped field drives `with_websites.csv` /
  `without_websites.csv` membership (`main.py:199-200`), `completeness_score`
  (`enricher.py:107-108`), 10–20 points of `lead_score` (`enricher.py:301-304`), and
  `recommend_service` (`main.py:191`). A single flaky ordering can therefore change the
  pitch a salesperson is handed. Combined with S1 the flip is then frozen forever.
- **Fix:** sample one run timestamp on the main thread before the pool and pass it to all
  three scrapers — `run_ts = utc_now_iso()` at `main.py:154`, replacing the per-scraper
  `scraped_at = utc_now_iso()` at `osm.py:32` / `wikidata.py:67` / `google_places.py:173`.
  Within a run all three recency elements become equal, the recency element degenerates
  to a no-op, and `str(value)` (`dedup.py:184`) decides deterministically. This also makes
  `scraped_at` mean "run" rather than "whichever thread woke up first", and it neutralises
  the whole `rating`/`review_count`/`website_live` column of the table above in one move.

---

### S2 — CSV row order is a function of `as_completed()` completion order, so every run reshuffles `all_businesses.csv`

- **Where:** `dedup.py:222` (`phone_records = list(phone_index.values())`) and
  `dedup.py:232` (`return phone_records + name_records`). Dict insertion order is the
  order records were first seen at `dedup.py:210` / `dedup.py:218`. That order is the order
  of `combined` (`main.py:179`), whose new-record prefix is built by
  `raw.extend(batch)` inside `as_completed()` (`main.py:162-166`).
- **Breaks:** `all_businesses.csv` is cumulative and is the file `load_master` reads back
  (`main.py:150`, `main.py:77-105`), so it is the project's only state. Its row order
  carries no information but is not reproducible, which defeats any diff-based review, any
  content-addressed cache (`docs/audits/053-caching-layer.md`), and any incremental
  append/checkpoint scheme (`docs/audits/035-incremental-scraping.md`). Worse: because
  `combined = raw_filtered + master` (`main.py:179`), a row present in *both* the new
  scrape and the master takes its position from the new scrape (`dedup.py:210` inserts on
  first sight), so even long-stable master rows migrate around the file on every run.
- **Trigger** (verified): 5 mutually-distinct records `B0..B4`, same input set, two
  different arrival orders → `['B0','B1','B2','B3','B4']` vs
  `['B4','B0','B1','B2','B3']`. Same records, same merged content, different file.
- **Fix:** sort before returning, e.g. at `main.py:181`
  `records = sorted(records, key=lambda r: (str(r.get("name") or "").casefold(), str(r.get("address") or "")))`
  — or, better, sort inside `dedup` on the merge key itself so the invariant is owned by
  the module that establishes the keys.

---

### S3 — `dict(record)` is a shallow copy: one mutable field value would alias across every row and become visible to the 40-thread mutator

- **Where:** `dedup.py:210` and `dedup.py:218` (`dict(record)`), and the returned records
  are then mutated in place by `check_websites` (`enricher.py:249-259`).
- **Breaks:** today every `BusinessRecord` value is a scalar (`base.py:5-28`) and every
  `load_master` value is `str | float | int | bool | None` (`main.py:88-103`), so the
  shallow copy is sufficient and nothing aliases. The moment a scraper puts a `list` or
  `dict` in a field — and `google_places.py:278` already builds `types = place.get("types")
  or []` — that single container is shared by the input record, the index copy, and every
  `_merge` output (`dedup.py:191-194` copies the reference, not the object). A worker-side
  or later-stage mutation of it would then be visible on multiple output rows at once, and
  because `check_websites` writes records in place from the main thread
  (`enricher.py:249-259`) there would be no error, only wrong cells in the CSV.
- **Trigger:** add `types=types` to the `BusinessRecord(...)` at `google_places.py:281-305`;
  two Google rows sharing a place id via different queries then share one list object.
- **Fix:** document the scalar-only precondition at `dedup.py:210` (e.g.
  `# BusinessRecord values must be immutable: this copy is shallow — see base.py:5-28`),
  so the next person to add a field knows the constraint. Enforce it with a
  `dict(record)` → `{k: (list(v) if isinstance(v, (list, dict)) else v) ...}` only if a
  mutable field is ever actually added; until then the comment is enough.

---

### S3 — The concurrency contract of `dedup()` is undocumented and has zero test coverage

- **Where:** `dedup.py:197` (no docstring at all — unlike every other function in the
  file, which all carry one); `tests/test_lead_signal.py:65` imports only
  `_merge` and `normalize_phone`, never `dedup`.
- **Breaks:** the suite's concurrency-relevant assertion is
  `test_merge_is_order_independent` (`tests/test_lead_signal.py:292-295`), which is
  *pairwise on `_merge`*. Nothing in `tests/` imports `threading` or exercises
  `dedup()` at all. So the three properties that make `dedup` safe here — no shared
  mutable state, no input mutation, per-field order independence across a whole group —
  are protected by nothing. The natural refactor (parallelise the group-by, the obvious
  "this is a big loop, let's shard it" change) would break the input-mutation contract
  only if someone removed the `dict(record)` copies, and would break nothing at all today,
  so a broken parallel `dedup` would be caught by nothing.
- **Fix:** three cheap tests, all stdlib and offline: (1) `dedup` over all permutations of
  a 5-record fixture returns an identical canonical form — I ran this, it passes over all
  120 permutations; (2) `dedup` leaves its input list deep-equal and returns zero input
  dict objects; (3) 40 threads calling `dedup` on one shared list produce identical output
  with zero exceptions — I ran this 200×, it passes. Add a docstring at `dedup.py:197`
  stating: *pure — does not mutate `records`; returns fresh dicts; all module state is
  read-only.*

---

## Not a bug, but worth knowing

**`dedup()` itself is currently safe.** Stating this precisely, since it is the actual
answer to the question asked:

1. **One call site, main thread, after the join.** `dedup` is referenced exactly once in
   the codebase (`main.py:29` imports it, `main.py:180` calls it; `grep` over `*.py` finds
   no other caller, and `cli.py` does not call it at all). The scraper pool at
   `main.py:160-168` is consumed by an `as_completed` loop *inside* the `with` block, and
   `ThreadPoolExecutor.__exit__` performs `shutdown(wait=True)`, so by `main.py:170` all
   three scraper threads have terminated. Nothing is running when `dedup` executes.
2. **The 40-thread pool is created *after* dedup and never sees a record.** `enrich()` →
   `check_websites()` (`enricher.py:337`) happens at `main.py:187`, i.e. after
   `main.py:180`. Inside the pool, workers execute only `_fetch_website(url)`
   (`enricher.py:238`) — submitted as a **URL string**, not a record; `enricher.py:231`
   snapshots `(index, url)` pairs. Results are applied to `records[idx]` by the *calling*
   thread in the `as_completed` loop (`enricher.py:240-259`). There is no
   worker↔main-thread race over record dicts. (Cross-ref: `docs/audits/004-enricher-thread-safety.md:26`
   reaches the same conclusion from the enricher side.)
3. **No shared mutable state.** `phone_index` / `name_index` are function-local
   (`dedup.py:198-199`). Every module global is read-only: `_KNOWN_COUNTRY_CODES` is a
   tuple iterated at `dedup.py:58`, `_COUNTRY_CODES` via `.get` at `dedup.py:48`,
   `_SOURCE_TRUST` via `.get` at `dedup.py:151`, `_VOLATILE` via `in` at `dedup.py:183`,
   and `_EMAIL_OK`/`_URL_OK` (`dedup.py:116-117`) are compiled `re.Pattern` objects, which
   are safe to share across threads. `unicodedata.normalize` (`dedup.py:67`) is re-entrant.
   `_merge` never mutates `a` or `b`; it builds a new dict (`dedup.py:191-194`).
4. **It does not mutate its input, verified.** I asserted `dedup(input) == deepcopy(input)`,
   that no returned dict is any input dict by identity, and that mutating all returned dicts
   leaves the input untouched. All pass. This is what makes step 2 safe: `check_websites`
   mutating the output in place cannot reach back into `master` or `raw_filtered`.
5. **`_pick` is associative and commutative, so nondeterministic input order is absorbed.**
   Each field case is a per-field reduction that is order-free: missing-value short-circuit
   = "max over non-missing" (`dedup.py:167-171`), `source` = set union
   (`dedup.py:173-174`), `scraped_at` = `max` (`dedup.py:176-177`), otherwise a max over the
   rank tuple (`dedup.py:179-187`) with a total-order tiebreak `str(value)`
   (`dedup.py:184`). Verified over all 120 permutations of a 5-record fixture spanning
   three sources: identical output every time. **The only residual order sensitivity is
   cosmetic** — when two values tie on all three rank elements they must have equal
   `str()`, so they differ at most in Python type (`3` vs `"3"`), which renders identically
   through `csv.DictWriter` (`main.py:125`) and converts identically in
   `lead_score`'s `float(value)` (`enricher.py:312-314`). So `as_completed` ordering does
   **not** corrupt dedup's content — which is precisely why the S2 recency tiebreak, not the
   input ordering, is the real concurrency bug here.
6. **`dedup()` has never been called concurrently even by accident.** 40 threads × 200
   trials calling `dedup` on one shared list: identical canonical output, zero exceptions.
   This is not a licence to parallelise it (see S3 on why it is untested), but it does mean
   the function has no latent crash-or-corrupt path.
7. **The other shared-mutable-state sites in the pipeline are already correctly guarded**,
   which is why dedup receives a quiescent, consistent list:
   - `GooglePlacesScraper`: `seen_ids` under `seen_lock` (`google_places.py:263-266`), and
     the per-query `records` list is only merged into `all_records` under `records_lock`
     (`google_places.py:191-192`) — note this is the one place where the naive version would
     have lost records, and it is correct.
   - Sessions: `google_places.py:189` creates one `requests.Session` **per worker**
     (inside `fetch_query`), and `httpclient.get_session()` is `threading.local`
     (`httpclient.py:58,61-71`) so the 40 enrichment threads each get their own. Both
     correctly avoid the documented-non-thread-safe shared-`Session` bug. `osm.py:35` and
     `wikidata.py:73` each run on their own outer worker thread and therefore also get
     distinct sessions.
   - `raw` is only ever extended by the main thread (`main.py:166`), and each scraper's
     batch is fully materialised by `list(scraper.scrape())` inside the worker
     (`main.py:158`) before it crosses the `Future`, so no scraper can still be appending
     to a list dedup might read.
8. **The drop at `dedup.py:228-229` is unreachable dead code.** A record only enters
   `name_index` when `normalize_phone` returned `""` (`dedup.py:206-218`), which happens
   iff the raw phone is falsy or has <7 digits (`dedup.py:45-46`). Merging never
   concatenates — `_pick` returns one of the two original values — so a name-index record's
   merged phone always still normalizes to `""`, and `""` is never a `phone_index` key
   (keys are only inserted under `if key:`, `dedup.py:206`). I brute-forced 2,197
   `country`/phone-value combinations and instrumented the predicate directly: zero
   records dropped. Harmless today, but it is a second, untested invariant guarding
   **silent row deletion** — if it ever fires it deletes a row without a log line. Worth
   an assertion rather than a comment.
9. **Resource lifetime, minor:** the 40 enrichment threads each create a `Session` that is
   never closed. `enricher.py:194` closes the *response* (returning the socket to that
   thread's pool), but the pool itself is never cleared, so up to ~10 idle connections per
   host per thread are retained for the process lifetime. Process is short-lived, so this is
   S3 at most and belongs to the enricher lens rather than this one.
10. **Adjacent, flagged for whichever lens owns it:** `_KNOWN_COUNTRY_CODES` contains
    `"20"` (Egypt) at `dedup.py:20`, and the loop at `dedup.py:58-60` returns on the first
    `startswith` match. So a legitimate 7-digit Lebanese number beginning `20` (e.g. a
    Beirut `20xxxxx`) is graded as Egyptian `+20...`. Deterministic, order-independent,
    and not a concurrency issue — but it does mean the *phone index* buckets some real
    Beirut businesses under a foreign code, which interacts with the S1 trust inflation.

---

## Recommended order of work

1. **S1 — per-field provenance in `_merge`/`_trust_for`** (`dedup.py:148-152`,
   `dedup.py:190-194`). Highest value: it is silent, monotonic, and it is the reason the
   S2 race has teeth. Do it before any re-scrape, or the poisoned rows are already
   permanent. Requires a one-time repair pass over the existing
   `data/all_businesses.csv` regardless of the code fix.
2. **S2 — one run timestamp sampled before the pool** (new `run_ts` at `main.py:154`,
   threaded into `osm.py:32` / `wikidata.py:67` / `google_places.py:173`). One line of
   intent, removes the whole volatile-field race, and makes `scraped_at` mean "run".
3. **S2 — sort `dedup`'s output** before `main.py:181` (or inside `dedup`). Unblocks the
   incremental-scraping and caching plans (`035`, `053`) that are otherwise building on a
   file whose order is random.
4. **S3 — docstring + three offline tests for `dedup`** (`dedup.py:197`,
   `tests/test_lead_signal.py:65`). All three proposed assertions were run manually during
   this audit and pass today; they exist to keep passing. Add an explicit assertion at
   `dedup.py:227` that no name-index record is ever dropped, so the dead branch cannot
   start deleting rows silently.
5. **S3 — scalar-only comment** on the shallow copies at `dedup.py:210` / `dedup.py:218`.