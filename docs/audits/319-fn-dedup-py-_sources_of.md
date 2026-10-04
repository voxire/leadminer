# 319 — `_sources_of` is the pipeline's trust oracle and it has no failure modes because it cannot see any

## Verdict

`_sources_of` (`dedup.py:143-145`) is three lines of string splitting, and taken alone it is
essentially correct. But it is the sole source of provenance for the entire merge engine, and it
reads that provenance from a **record**, never from a **run**. That single design choice produces
two S1 bugs that only manifest after the pipeline has been running for weeks: the `source` column
is a **one-way ratchet** that can never retract a dead source, and the union it produces is
recorded downstream as "multi-source confirmation" (`enricher.py:316`) and paid out as **+5
`lead_score` in `sales_ready.csv`** — the actual sales product — for corroboration that never
happened. Neither is visible in any artifact: the workflow's only gate is a total row count
(`scrape.yml:80`), `leadminer validate` does not read the `source` column at all, and the run
summary in `main.py:219-242` has no per-source line. Detection time for both is *never*.

---

## The ops questions, answered directly

| Question | Answer | Evidence |
|---|---|---|
| **What does it print?** | Nothing. No `print`, no `log`, no counter, no return-code signal. | `dedup.py:143-145` |
| **What does it swallow?** | Unknown source names, casing, surrounding whitespace, wrong delimiters, embedded newlines, non-string values, empty/`None`/`0`/`False` sources, and any `AttributeError` from a non-dict argument. | Table in S2-2 |
| **Network drops halfway** | Indistinguishable from "nothing to scrape". `_sources_of` reads the record, not the run. OSM returns `[]` after 3 retries (`osm.py:47-56`) and prints a line to stdout; nothing in `dedup.py` or `main.py` reflects it downstream. | S2-3 |
| **Dependency returns garbage** | Impossible for `source` as written — all three scrapers hardcode it (`osm.py:119`, `wikidata.py:127`, `google_places.py:302`). But garbage enters through **the master CSV**, which is user-editable output. `"OSM"`, `"osm "`, `"osm,wikidata"`, `"osm\nwikidata"` all become unknown tokens → trust silently drops to `_DEFAULT_TRUST = 1`. | S2-2 |
| **Called with an empty list** | `_sources_of([])` raises `AttributeError: 'list' object has no attribute 'get'`. Its signature takes a `dict`, not a list; a caller that hands it an iterable of records gets a traceback from inside `_pick`, not at the boundary. | S3 |
| **Interrupted** | Safe, and worth saying plainly. `write_csv` is temp + `fsync` + `os.replace` under `except BaseException: tmp.unlink(); raise` (`main.py:123-133`), so a `KeyboardInterrupt` or the workflow's 300-minute SIGTERM cannot corrupt the master. `main.py` has no handler (traceback, exit 1); `cli.py:316` catches `KeyboardInterrupt` → 130. You lose the whole run; you do not lose history. | "Not a bug" below |

**Performance is a non-issue — do not optimise this.** Measured 0.52 µs per `_sources_of` call
(1,000,000 iterations). `dedup()` over a synthetic 500,000-row master + 40,000 fresh records
completes in **6.0 s at 1.06 GiB peak RSS**. That is ~13M `_sources_of` calls in the worst case,
≈ 7 seconds total. This function will never be why the job takes six hours.

---

## Findings

### S1 — The `source` union is a one-way ratchet: a source that died a year ago is still claimed forever

- **Where:** `dedup.py:143-145` (the set), consumed at `dedup.py:174`
  (`"|".join(sorted(_sources_of(a) | _sources_of(b)))`), persisted at `main.py:209`, re-read at
  `main.py:150`.
- **Breaks:** Set union is the *only* operation ever performed on provenance, and the merged
  result is written back into the **cumulative master**, which is the input to the next run. There
  is no retraction path and no per-source last-seen timestamp. Once `"google_places|osm"` is
  written, every subsequent run re-unions it with whatever the current scrape produced and
  writes the same string back. Provenance in `all_businesses.csv` is monotonically increasing and
  permanently false.
  The `scraped_at` column moves forward every run while `source` never admits that a contributor
  stopped existing. After six months of a dead Google key, 100% of rows Google ever touched still
  claim Google.
- **Trigger:** Four consecutive runs of one business, where `google_places` returns zero rows
  from run 2 onward. Verified through the **real** `write_csv` → `load_master` → `dedup` path, not
  just in memory:

  ```
  run 1 in-memory source : google_places|osm
  run 1 CSV source       : google_places|osm
  loaded master source   : google_places|osm (type str)
  run 2 source           : google_places|osm   <- google_places contributed NOTHING
  run 3 source           : google_places|osm
  run 4 source           : google_places|osm
  ```

  Note `load_master` (`main.py:77-105`) preserves the string faithfully — the round trip is not the
  bug. The bug is that the merge has no operation that can produce a smaller set than its inputs.
- **Also note the ratchet has a hole in the other direction:** `_pick` returns early at
  `dedup.py:168-171` when either value is `_is_missing`, so the `field == "source"` branch at
  `dedup.py:173-174` is only reached when **both** sides are non-empty. A master row with a blank
  `source` is therefore *replaced wholesale* by the fresh record's source rather than unioned.
  Wrong provenance is immortal; absent provenance dies. Both directions need fixing.
- **Why S1:** This is silent data corruption of the master column that can never self-heal.
  Recovery requires manual CSV surgery on the Drive copy, and the very next run re-corrupts it.
- **Detection time:** Effectively never. Nothing in the artifact looks wrong — `source` is
  populated and plausible. It becomes visible only when someone cross-references Google Places
  console usage against CSV contents.
- **Fix:** Store provenance as `{source: last_seen_run_id}` (or add a `source_seen_at` map column)
  and union *last-seen* rather than *names*; drop any source not refreshed by the current run.

---

### S1 — The union fabricates "multi-source confirmation", and `lead_score` pays +5 for it in `sales_ready.csv`

- **Where:** produced at `dedup.py:174`; consumed at `enricher.py:315-317`
  (`if "|" in str(record.get("source") or ""): score += 5`).
- **Breaks:** `_sources_of` is a **whole-record bag of source names**. It cannot express *which
  source supplied which field*. So the moment any two records share a dedup key, the merged
  record's `source` contains a pipe and `lead_score` awards a "Multi-source confirmation" bonus —
  regardless of whether the two sources ever agreed on anything, and regardless of whether they
  described the same business.
- **Trigger:** Two genuinely different businesses that collide on the normalized phone key:

  ```
  a = {"name": "Acme Trading Co", "phone": "+9611234567", "rating": 4.4, "source": "google_places"}
  b = {"name": "Acme",             "phone": "1234567",     "website": "http://other.example", "source": "osm"}
  _merge(a, b) -> name "Acme Trading Co", website "http://other.example", source "google_places|osm"
  "|" in source: True   ->  lead_score += 5
  ```

  Google's rating and OSM's website are two unrelated observations of two unrelated names, fused
  into one row that now advertises itself as cross-source-confirmed. Worse, because of S1-1 the
  fabricated `|` is written to the master and re-earned on every subsequent run, so the bonus
  becomes self-sustaining.
- **Blast radius:** `sales_ready.csv` is defined at `main.py:202-205` as records with a contact
  channel **and** priority in `(high, medium)`. These are the rows the sales team works. Every one
  of them that arrived via this path carries an unearned +5, which is enough to reorder a sorted
  pipeline and to inflate any conversion-rate reporting built on `lead_score`.
- **Why S1:** Wrong results in the revenue artifact, compounding weekly, undetectable from the data.
- **Detection time:** Weeks to never. It requires auditing individual leads to notice that the
  "confirmed by Google Places" claim has no corroborating field behind it.
- **Fix:** Emit provenance **per field** (see S2-1) and change `enricher.py:316` to require two
  sources on the *same field*, not anywhere in the record.

---

### S2 — Trust is attributed by co-membership, not by contribution

- **Where:** `dedup.py:148-152` — `for src in _sources_of(record): best = max(best, _SOURCE_TRUST.get(src, {}).get(field, _DEFAULT_TRUST))`.
- **Breaks:** `_trust_for` takes the `max` trust over **every** source in the record's bag,
  including sources that contributed nothing to the field being ranked. A merged record's trust
  for a field is therefore the best trust of *any* source that has *ever* touched the row, not the
  trust of the source that actually supplied the value.
- **Trigger:**

  ```
  a = {"name":"A", "website":"https://google.example", "source":"google_places"}  # google trusts website 2
  b = {"name":"A", "email":"a@b.com",                     "source":"osm"}        # osm has NO website, trusts website 3
  merged.source = "google_places|osm"
  _trust_for("website", merged) = 3   # inflated from the honest 2
  ```

  From then on, that merged row carries trust 3 for a field it got from the source that rated it
  2, and it competes against fresh records with that inflated number on the *first* rank key —
  ahead of `_validity` and ahead of the `_VOLATILE` recency check at `dedup.py:183`.
- **Why S2:** Materially degrades merge quality at scale. It does not always produce a wrong row
  (the inflated trust is still bounded by `_validity` in the common case), but it systematically
  biases every conflict resolution in the master's direction.
- **Detection time:** Only by reading `_pick`'s ranking tuple and reasoning backwards about which
  source won which field. No artifact shows it.
- **Fix:** Carry `{field: source}` alongside the value; look up trust for *that* source, not the
  max over the record.

---

### S2 — An unrecognised source token silently drops that record's trust to 1, and can make a junk URL beat a real one

- **Where:** `dedup.py:143-145` returns whatever it split, with no membership check against
  `_SOURCE_TRUST` (`dedup.py:97-110`).
- **Breaks:** `_SOURCE_TRUST.get(src, {}).get(field, _DEFAULT_TRUST)` (`dedup.py:151`) means an
  unknown token is not an error — it is a silent downgrade to `_DEFAULT_TRUST = 1`
  (`dedup.py:111`). No log, no warning, no counter. Measured behaviour of `_sources_of` on shapes
  that actually occur in a hand-edited CSV:

  | `source` cell | `_sources_of` returns | `_trust_for("website", …)` |
  |---|---|---|
  | `"google_places"` | `{'google_places'}` | 2 |
  | `"google_places\|osm"` | `{'google_places', 'osm'}` | 3 |
  | `None` (master blank, `main.py:87-89`) | `set()` | 1 |
  | `"osm "` (trailing space, Excel) | `{'osm '}` | **1** |
  | `" osm"` | `{' osm'}` | **1** |
  | `"OSM"` | `{'OSM'}` | **1** |
  | `"osm,wikidata"` | `{'osm,wikidata'}` | **1** |
  | `"osm\nwikidata"` (pasted cell) | `{'osm\nwikidata'}` | **1** |
  | `"   "` | `{'   '}` | **1** |
  | `1` | `{'1'}` | **1** |
  | `["osm"]` | `{"['osm']"}` | **1** |

  Because trust is the **first** element of the rank tuple at `dedup.py:180-185`, a 1-vs-3 trust
  gap short-circuits the `_validity` check entirely.
- **Trigger:** One space in one cell.

  ```
  a.website = "https://a.example"        source = "OSM"   (capitalised by a human)
  b.website = "http://b.example.broken"  source = "osm"
  _merge(a, b)["website"] = "http://b.example.broken"
  ```

  The malformed URL wins, because `"OSM"` scored trust 1 and `"osm"` scored trust 3. The malformed
  URL then flows into `with_websites.csv` and becomes a live pitch. This CSVs are the sales
  product; they get opened in Excel and Sheets and saved back. A `rclone copy` at `scrape.yml:56`
  pulls that edited file back in as the next run's master.
- **No gate catches it.** `leadminer validate` (`cli.py:135-201`) checks name, BOM, coordinates,
  rating and duplicates. It does **not** read `source`:

  ```
  leadminer validate on a row whose source is "OSM  garbage":
    checked 1 rows in …/bad.csv
      all checks passed  ->  exit 0
  ```

- **Why S2:** Reachable by routine human interaction with the product, corrupts merge quality,
  and is invisible to every check that exists.
- **Detection time:** Never, unless someone happens to read `_SOURCE_TRUST` and diff it against
  the distinct values in the column.
- **Fix:** Normalise inside `_sources_of` (`.strip().lower()`), split on `[|,;\s]+`, and `raise` or
  `log.warning` on any token not in `_SOURCE_TRUST`.

---

### S2 — A source returning zero rows is reported as a successful run; nothing in `dedup` or `main` can see it

- **Where:** `dedup.py:143-145` reads `record["source"]`; there is no run-level provenance
  anywhere in `dedup.py` or `main.py`.
- **Breaks:** The failure mode this brief explicitly asks about — *network drops halfway* — is
  completely invisible to `_sources_of`, because provenance is inferred from records that *were*
  returned rather than from a record of what each source *should* have returned. Every degradation
  path lands in the same place:
  - `osm.py:47-56` — 3 retries exhausted, `print`s a line, `return`s an empty generator.
  - `google_places.py:228-233` — 401 invalid key, prints `FATAL`, returns whatever it has (empty
    on run 1).
  - `google_places.py:234-247` — 429 storm, abandons each query after `_MAX_429_RETRIES`.
  - `google_places.py:216-220` — per-query time budget exceeded, returns **partial** results.
    This is the nastiest one: a source that returns 40 of 68 queries produces records, so every
    "did we get data" signal downstream says yes.
- **And nothing alerts:** `scrape.yml:80` fails the run only when *total* rows drop below 100.
  `google_places` is one of three sources; OSM + Wikidata alone comfortably exceed 100. A total
  Google Places outage therefore **publishes a green run with exit code 0**. `main.py:219-242`
  prints totals, qualified, website coverage, region and service breakdowns — **no per-source
  line**. `cli.py:120` counters whole `source` strings, so `"google_places|osm"` becomes its own
  bucket and per-source contribution cannot be derived even from `leadminer stats`.
- **Why S2:** Materially degrades operability at scale. It is not S1 because the run does not die
  and no history is lost; the master still accumulates and the missing source will reappear when
  it recovers. But for weeks-to-months stretches the product is quietly poorer than it looks.
- **Detection time:** Weeks to months. A human notices when a salesperson reports there are no
  Saudi ratings, or when a client says their business is missing from the list.
- **Fix:** Emit a per-source record count from each scraper, assert it in `main.py` and in the
  workflow against a per-source threshold, and add the per-source line to the run summary.

---

### S2 — No logging is configured anywhere in the pipeline, so `_sources_of` — and every other failure surface — is unobservable

- **Where:** `httpclient.py:45`, `osm.py:7`, `wikidata.py:7` define loggers; **no
  `logging.basicConfig` call exists in any module.** Every observable in the codebase is a
  `print()`.
- **Breaks:** The `log.debug` diagnostics that were carefully written — retry and transport-error
  detail at `httpclient.py:178-179` and `httpclient.py:204-205`, the exact information needed to
  tell "the server said 500" from "we never reached the server" — are discarded by the
  last-resort `WARNING` handler. Verified: `logging.getLogger().handlers == []`.
  The consequence for this lens is direct. When `_sources_of` returns a wrong set, there is no
  counter, no warning, and no trace. The only artefact is `source` in the CSV, which looks
  plausible by construction. And CI evidence has a 14-day shelf: `scrape.yml:98` sets
  `retention-days: 14`, the artifact holds only `data/`, and the next `rclone copy`
  (`scrape.yml:56`) overwrites the master. **Six hours of run evidence is unrecoverable after two
  weeks.**
- **Why S2:** This is the finding that makes every other finding in this report undetectable. It
  materially degrades operability of a 300-minute-budget scheduled job.
- **Detection time:** The absence is not detectable; it is felt the first time an operator needs
  to answer "was Google rate-limited in August?" and finds nothing.
- **Fix:** One `logging.basicConfig(level=INFO, stream=sys.stderr)` in `main()`, then replace the
  `print` calls. Add counters for `_sources_of` returning an empty or unknown-token set.

---

### S3 — `str()` coercion hides type errors; non-dict input raises from inside `_pick`

- **Where:** `dedup.py:144` — `raw = str(record.get("source") or "")`.
- **Breaks:** Two things it should not do, and one thing it should do loudly:
  - `{"source": ["osm"]}` becomes the single token `"['osm']"` — an unknown source, trust 1. A
    list-valued source is almost certainly a caller bug, silently reinterpreted as a garbage
    string.
  - `0`, `False`, `""`, `None` and a missing key all collapse to `set()` and therefore to
    `_DEFAULT_TRUST = 1`. Five distinct states, one indistinguishable outcome.
  - `_sources_of([])`, `_sources_of("osm")` and `_sources_of(None)` all raise
    `AttributeError: 'list'/'str'/'NoneType' object has no attribute 'get'` — from deep inside
    `_pick` during a merge, not at the call boundary. The traceback points at `_pick`, not at the
    caller that passed the wrong type.
- **Note:** the `[]` case in the brief is a genuine type error today, so a caller that treats
  `_sources_of` as taking an iterable of records gets a crash rather than an empty set. Given the
  `_is_missing`/empty-set convention used everywhere else in this module, returning `set()` for a
  non-mapping would be the consistent behaviour.
- **Why S3:** Real, minor, and cheap to fix. It costs correctness-of-signal, not data.
- **Fix:** `if not isinstance(record, dict): return set()`; validate `isinstance(value, str)`
  before coercing and log once per distinct offending value.

---

## Not a bug, but worth knowing

- **`_sources_of` is deterministic and merge-order independent — keep this property.** Line 174's
  `"|".join(sorted(...))` means `_merge(a, b)` and `_merge(b, a)` agree on `source`, covered by
  `tests/test_lead_signal.py:316-319`. Given that the rest of the pipeline is order-dependent
  (scraper completion order via `as_completed` at `main.py:162`) and non-deterministic (Google
  pagination, jittered backoff), this is the one merge path you can reason about statically. Any
  fix for S1-1 must preserve it.
- **Performance needs no work.** 0.52 µs/call; `dedup()` over 540,000 records = 6.0 s, 1.06 GiB
  peak RSS. Do not spend a cycle on it.
- **Interruption is handled correctly.** `write_csv` (`main.py:123-133`) writes to
  `path.with_suffix(".tmp")`, `flush()` + `fsync()`, then `os.replace()`, all inside
  `try/except BaseException: tmp.unlink(missing_ok=True); raise`. A `KeyboardInterrupt` or the
  workflow's 300-minute SIGTERM (`scrape.yml:25`) landing anywhere in the run leaves the Drive
  master either fully old or fully new. You lose the run's work; you do not lose history. The
  `concurrency` block at `scrape.yml:15-17` (`cancel-in-progress: false`) correctly prevents two
  runs racing on `data/`.
- **The one non-atomic step in the whole pipeline is the Drive upload, not `_sources_of`.**
  `scrape.yml:88-89` is two separate `rclone copy` calls to two destinations. A SIGTERM between
  them leaves `gdrive:leads/data/<TIMESTAMP>/` complete while `gdrive:leads/` is partial, and
  because the next run downloads from `gdrive:leads/` (`scrape.yml:56`), the master the pipeline
  reads is the one that may be half-written. Worth a follow-up audit; out of scope here.
- **`main.py:167` catches per-scraper exceptions and continues**, so a scraper that raises is
  downgraded to a stderr line and the run proceeds with two sources. Same net effect as S2-3 but
  without even the stdout warning.
- **`load_master` round-trips `source` faithfully**, so the S1-1 ratchet is purely a merge-semantics
  problem, not a serialisation bug. That narrows the fix.

---

## Recommended order of work

1. **Add logging.** One `basicConfig` in `main.py`, replace `print`. Everything below is
   undetectable until this lands. (S2, prerequisite for all)
2. **Stop the ratchet.** Give provenance a per-source `last_seen` and union that instead of names;
   handle the `dedup.py:168-171` short-circuit so blank provenance survives too. (S1-1)
3. **Make corroboration mean something.** Replace the whole-record bag with per-field provenance;
   change `enricher.py:316` to require two sources on the *same* field. This also fixes the trust
   inflation in S2-1 as a side effect. (S1-2, S2-1)
4. **Validate source tokens.** `.strip().lower()`, split on `[|,;\s]+`, and fail loudly on any
   token outside `_SOURCE_TRUST`. Add a `source`-column check to `cli.py cmd_validate` so the
   existing validation step would have caught the Excel edit. (S2-2)
5. **Per-source run accounting.** Have each scraper report its record count, assert it in
   `main.py` and in `scrape.yml` per-source (not just on the 100-row total), and add a per-source
   line to the run summary. (S2-3)
6. **Tighten the type contract.** `isinstance` guards on `_sources_of`'s argument and on the
   `source` value. (S3)

---

### Reproducing

Every measurement above came from local pure-Python probes against the repo (no network, no
`pip install` — `requests` was shimmed in-process purely to satisfy `main.py`'s import). The
load-bearing reproductions are the four-run CSV round trip through
`write_csv` → `load_master` → `dedup` (S1-1), the two-business phone collision (S1-2), the
co-membership trust inflation (S2-1), the 16-row `_sources_of` input table (S2-2), and
`cmd_validate` returning exit 0 on `source="OSM  garbage"` (S2-2).