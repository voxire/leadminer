# 339 — `_pick` (`dedup.py:159`) — concurrency review

## Verdict

**`_pick` is safe under this codebase's concurrency today, and that is not luck — it is
structural.** It is a pure function of `(field, a, b)`, it writes to nothing but the fresh
dict its caller owns, it touches no module-level mutable state, it takes no lock and needs
none, and it has **zero call sites under any thread pool**. All three pools are fully joined
before `dedup()` is entered. I stress-tested it (40 threads × 3 000 `_merge` calls against
shared input dicts) and it is clean.

The one thing that *is* wrong, and the reason this lens is not a one-line "fine": **`_pick`
is not associative across 3+ records, because `_recency` reads the *accumulator's*
`scraped_at` — which `_pick` itself just set to the running max.** The fold is therefore
"first-arrival wins", not "freshest wins". I proved it flips the surviving value. It is
currently *masked* by three separate coincidences in the pipeline (trust dominates
cross-source, all same-source rows in a run share one timestamp, and the master is always
folded last). That masking is fragile and none of it is asserted anywhere.

## Findings

### S2 — `_pick` is not associative: the merge is order-dependent, so a thread-completion order leaks into the output

- **Where:** `dedup.py:183` (`_recency(rec) if field in _VOLATILE else ""`), reading
  `_recency` at `dedup.py:155-156`, against the accumulator's timestamp that
  `dedup.py:177` (`return max(av, bv)`) has just inflated to the running maximum.
- **Breaks:** `_merge(acc, next)` at `dedup.py:193` is called with `acc` as `a`. Once a
  record with a fresh `scraped_at` has been folded in, `acc["scraped_at"]` is the max over
  every contributor so far. `rank(acc, v)` then compares that *aggregate* timestamp against
  each *individual* rival's timestamp. The accumulator always claims to be the freshest thing
  in the set, so every subsequent record is judged stale — **no matter what value it
  actually carries for the field**. For the four volatile fields (`dedup.py:114`:
  `rating`, `review_count`, `website`, `website_live`) the rule silently degrades from
  "newest observation wins" to "whatever arrived first wins". The surviving
  `website` then drives `check_websites` → `website_live` → `lead_score`
  (`enricher.py:301-304`, +20 for a server-confirmed dead site), so a stale URL turns
  directly into the wrong pitch.
- **Trigger (concrete, reproduced — same three records, four arrival orders):**

  ```python
  M  = {"name":"X","phone":"+96170000001","website":"https://old.example",
        "source":"google_places","scraped_at":"2026-05-01T00:00:00+00:00"}
  N1 = {"name":"X","phone":"+96170000001","website":"https://new.example",
        "source":"google_places","scraped_at":"2026-06-01T00:00:00+00:00"}
  N2 = {"name":"X","phone":"+96170000001","website":None,
        "source":"google_places","scraped_at":"2026-09-01T00:00:00+00:00"}
  ```

  ```
  [N1, N2, M] -> website = https://new.example   scraped_at = 2026-09-01   (correct)
  [N2, N1, M] -> website = https://new.example   scraped_at = 2026-09-01   (correct)
  [N1, M, N2] -> website = https://new.example   scraped_at = 2026-09-01   (correct)
  [M,  N2, N1]-> website = https://old.example   scraped_at = 2026-05-01   (WRONG, both fields)
  ```

  The last row is the whole finding: the newest observation (`2026-09`) is seen and then
  thrown away, the *oldest* website is exported, and `scraped_at` regresses three months.
  Adding one more record with a different website produces **3 distinct outcomes over the 6
  permutations** of the same set. Removing `"website"` from `_VOLATILE` makes the fold
  associative again — that is the mechanism, confirmed by bisecting the set membership.

  **Why it is not S1, stated honestly:** I could not build a *live* trigger in today's
  pipeline. `main.py:179` is `combined = raw_filtered + master`, so the master is always
  folded **last** and always with the oldest timestamp; cross-source volatile ties are broken
  one rank level earlier by trust (`osm` website 3 > `google_places` 2, `dedup.py:103-104`);
  and every record from a given source in a run carries one shared timestamp
  (`google_places.py:173`, `osm.py:32`, `wikidata.py:67`), so same-source ties land on the
  `str(value)` tiebreak at `dedup.py:184`, which *is* associative. It becomes live the moment
  any of those changes: chunking the master, feeding scraper output per-scraper instead of
  merged, parallelising `dedup`, or trusting records whose `source` string is unrecognised
  (both sides fall to `_DEFAULT_TRUST` at `dedup.py:111`, trust ties, and recency — which
  now differs by milliseconds between the three scrapers — decides).
- **Fix:** rank on the timestamp of the record that actually *carries* the candidate value,
  not on the accumulator's. Thread a per-field `(value, scraped_at)` provenance pair through
  `_merge`, or carry a `_max_seen` side-channel that `_recency` reads instead of
  `rec["scraped_at"]` — and add a test asserting `_merge(_merge(a,b),c) == _merge(a,_merge(b,c))`.

### S3 — row order in all five exported CSVs is a function of thread completion order

- **Where:** `main.py:162` `for future in as_completed(futures)` → `raw.extend(batch)` at
  `main.py:166` (completion order, not submission order) → `combined` at `main.py:179` →
  `dedup` preserves insertion order at `dedup.py:210`/`218`/`222-232`.
- **Breaks:** the *content* of each row is order-insensitive (verified: 24 permutations of a
  4-record set → 1 distinct row content), but the *row sequence* is not: the same set
  produces **6 distinct output orderings**. `data/all_businesses.csv` is the cumulative
  master and is re-read and re-fed at `main.py:150`/179, so every run reshuffles the file,
  and the reshuffle is inherited by the next run's fold order. You cannot diff two runs of
  the master to answer "what changed", which is the question a cumulative lead list exists
  to answer.
- **Trigger:** run the pipeline twice with Overpass slow in one run and fast in the other.
  `[GooglePlacesScraper(), OSMScraper(), WikidataScraper()]` (`main.py:155`) complete in a
  different order, the three batches concatenate differently, and the export is ordered
  differently.
- **Fix:** sort `records` by a stable key (e.g. `(normalize_phone(phone) or normalize_name(name), city)`)
  immediately before `main.py:209`, or replace `as_completed` with a submission-ordered
  collection at `main.py:161-166`.
- **Not escalated** because nothing in the repo currently consumes row order: I checked
  `cli.py` and there is no `[:N]` truncation or sampling anywhere, so this is diff noise, not
  a wrong answer. It becomes S2 the moment anyone adds an incremental upload or a "changed
  since last run" report.

### S3 — `_pick` returns borrowed references; scalar-only field values are a convention, not an enforced invariant

- **Where:** `dedup.py:187` (`return av if … else bv` returns the *same object* held by `a`
  or `b`), the shallow `dict(record)` copies at `dedup.py:210`/`218`, and the unvalidated
  key union at `dedup.py:192` (`for key in set(a) | set(b)`).
- **Breaks:** verified by identity, not by value:
  `m["rating"] is A["rating"]` → `True`, `m["website"] is B["website"]` → `True`. Every
  merged row aliases its inputs' field objects. Today that is harmless because all 22
  `BusinessRecord` fields are `str | float | int | bool | None` (`scrapers/base.py:6-28`)
  and `load_master` (`main.py:86-104`) casts only the float/int columns, leaving everything
  else a string — so every alias is to an immutable. Add one list- or dict-valued field (a
  `tags: list[str]` is the obvious candidate) and `main.py:183-197` and
  `enricher.py:330-350`, which mutate rows **in place**, would be writing through to two
  rows at once; and `_merge` would happily carry an extra key through unvalidated, since
  `set(a) | set(b)` at `dedup.py:192` accepts any key either side happens to have.
- **Trigger:** `{"name":"X","phone":"1","source":"osm","tags":["a"]}` merged with
  `{"name":"X","phone":"1","source":"osm"}` → the merged row's `tags` **is** the first
  record's list object, not a copy.
- **Fix:** `copy.deepcopy(value)` on the borrowed branch of `_pick` (cheap — these are all
  scalars today), or a `assert isinstance(v, (str,int,float,bool,type(None)))` in
  `_merge` so the invariant fails loudly instead of silently aliasing.

## Not a bug, but worth knowing

- **`_pick` is safe to parallelise, and that is worth stating before anyone tries.** I ran
  10 trials of 40 threads each calling `dedup()` on disjoint slices of a 400-record input;
  every trial produced byte-identical content to one sequential `dedup()` over all 400 rows.
  `_pick`/`_merge` need no lock — only the two index dicts at `dedup.py:198-199` would need
  partitioning (shard by the normalized phone / name key, then concatenate in key order).
- **`_merge` never mutates its arguments.** Verified: `A != deepcopy(A)` is `False` after
  the call, and 40 threads × 3 000 `_merge` calls against shared input dicts produced zero
  exceptions and left the inputs unchanged. `merged = {}` at `dedup.py:191` is the only
  object written.
- **No dedup global is ever rebound.** Every module-level name is assigned exactly once, at
  its definition: `_KNOWN_COUNTRY_CODES` (`dedup.py:8`), `_COUNTRY_CODES` (23), `_MIN_DIGITS`
  (30), `_SOURCE_TRUST` (97), `_DEFAULT_TRUST` (111), `_VOLATILE` (114), `_EMAIL_OK` (116),
  `_URL_OK` (117). There is no cache, no memo, no lazily-built structure that a second
  thread could race to initialise. This is the single biggest reason `_pick` is
  concurrency-safe here, and it is worth preserving deliberately.
- **Two-record folds are always order-independent** (verified over the permutations), and
  `rank` at `dedup.py:180-185` is homogeneously typed `(int, int, str, str)`, so the `>=`
  at `dedup.py:187` cannot raise `TypeError` even on mistyped CSV data —
  `{"rating": 99.0}` vs `{"rating": "4.0"}` returns `"4.0"` cleanly. Contrast `max(av, bv)`
  at `dedup.py:177`, which *would* raise on a mixed-type `scraped_at`; it is safe today only
  because `scraped_at` is in neither `_FLOAT_FIELDS` nor `_INT_FIELDS`
  (`main.py:43-44`) and the scrapers always emit `utc_now_iso()`.
- **No session is shared across threads.** `httpclient.get_session()` is `threading.local()`
  (`httpclient.py:58`, `61-71`); `GooglePlacesScraper` builds a fresh
  `requests.Session()` per query thread (`google_places.py:159-166`, called at `189`);
  `osm.py:35` and `wikidata.py:73` go through `get_session()`. The 40-worker pool therefore
  gets 40 distinct sessions and the 5-worker Google pool gets 5.
- **The 40-thread pool never mutates a record from a worker.** `enricher.py:238` submits
  `_fetch_website(url)` — a bare string, not the record. Every write to `records[idx]`
  (`enricher.py:250-259`) happens in the `as_completed` consumer loop **on the main thread**.
  So `check_websites` contributes zero cross-thread dict mutation, which is why `dedup`'s
  output can be freely mutated in place afterwards without a lock.
- **Resource lifetime: no `session.close()` exists anywhere in the repo.** The only `.close()`
  in the tree is `r.close()` at `enricher.py:195`, which closes the *response*, not the
  session. Each `get_session()` session is stored in a `threading.local()`
  (`httpclient.py:70`) and becomes unreachable when its pool thread exits; CPython
  refcounting reclaims it and its sockets, so this is not an unbounded leak, but it is
  non-deterministic. With `requests`' default adapter (`pool_connections=10,
  pool_maxsize=10`) that is up to ~400 sockets released in a burst at
  `enricher.py:262`. Cheap fix: make the pools call `session.close()` in a `finally`.
- **Out of lens, adjacent, same failure family** (not `_pick`'s bug, do not double-count):
  `google_places.py:196-197` — `for f in as_completed(futures): f.result()` re-raises
  *inside* the `with ThreadPoolExecutor` block. One failing query thread discards
  `all_records` for all 68 queries; the exception surfaces at `main.py:167` as a
  per-scraper `ERROR:` line and the whole Google batch vanishes from a run that otherwise
  reports success. The pool's shared state dies with the first failing member.
- **Out of lens, adjacent:** `dedup.py:228-229` is dead code. A record only reaches
  `name_index` when `normalize_phone` returned `""` (`dedup.py:205-206`), and `_merge`
  cannot promote a falsy phone to a truthy one (verified: `_merge({"phone": ""},
  {"phone": "n/a"})["phone"] == "n/a"`, and a real phone would have routed the record into
  `phone_index` instead). So `normalize_phone(raw, …)` at line 228 is always `""`, and `""`
  is never a key in `captured_phones` (only truthy keys are inserted at
  `dedup.py:206-210`). The `continue` can never execute.

## Why the current call graph is safe — the specific facts that make it safe

1. `dedup()` has exactly **one** call site: `main.py:180`, in `main.main()`, on the main
   thread. (`cli.py:37` `cmd_run` is a thin delegate to the same function.) No worker
   anywhere calls `dedup`, `_merge`, or `_pick`.
2. The 3-worker pool at `main.py:160-168` is inside a `with` block, so
   `ThreadPoolExecutor.__exit__` → `shutdown(wait=True)` joins all three threads at
   `main.py:169`, before `dedup` at line 180.
3. The 5-worker Google pool (`google_places.py:194-197`) is closed by its own `with` exit
   *before* the `yield from all_records` at `google_places.py:199`, and `main.py:158`
   (`list(scraper.scrape())`) drains that generator to completion — so by the time `run()`
   returns, all 68 query workers are joined too.
4. The 40-worker pool does not exist yet at `dedup` time: it is created at
   `enricher.py:237`, reached from `enrich()` at `main.py:187`, seven lines *after*
   `dedup` has already returned.
5. `dedup`'s own index dicts (`dedup.py:198-199`) are function-local, so they are
   unreachable from any other thread by construction.
6. `dedup` shallow-copies on insert (`dict(record)` at `dedup.py:210`/`218`), so the dicts it
   hands back to `main.py` are not the scrapers' objects and not the loaded master's objects.
   `main.py:183-197` then mutates them in place with no aliasing hazard.

Nothing here is load-bearing on a comment or a convention; it is all `with`-block scoping
plus function-local state. The one convention that *is* load-bearing is the scalar-only
field invariant in the S3 above.

## Recommended order of work

1. **S2 non-associativity** — fix `_recency` to read a per-value provenance timestamp
   instead of the accumulator's `scraped_at`, and land the associativity test alongside it.
   This is the only finding that can put a wrong value in the export.
2. **S3 row order** — sort before `write_csv`, or collect scraper batches in submission
   order. One line, and it makes the cumulative master diffable.
3. **S3 aliasing guard** — `deepcopy` the borrowed branch in `_pick`, or assert scalar field
   types in `_merge`, before anyone adds a list-valued column.
4. **Hygiene, not in the findings above:** `session.close()` in a `finally` for the
   thread-local sessions; delete the dead branch at `dedup.py:228-229`; make
   `google_places.py:196-197` collect per-query failures instead of discarding the batch.