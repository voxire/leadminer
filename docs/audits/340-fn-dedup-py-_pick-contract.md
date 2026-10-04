# 340 — `_pick` (dedup.py:159) contract audit

## Verdict

`_pick` has no enforceable contract. The record type it merges is declared as bare `dict`
(`dedup.py:159-160`), so `BusinessRecord` (`scrapers/base.py:5-28`) never reaches this
function — nothing downstream of the scrapers imports it at all. The function's *only*
input-side guarantee is its dispatch table: `_SOURCE_TRUST` (`dedup.py:97-110`) declares
trust for 24 of the 69 possible (source, field) pairs, `_VOLATILE` (`dedup.py:114`) names
4 fields, and `_validity` (`dedup.py:120-140`) special-cases 5. For the remaining fields —
`country`, `region`, `address` has trust, but `country` and `region` do not — every comparison
axis ties at 1 and the decision falls to `str(value)` lexicographic max (`dedup.py:185`).
That is an arbitrary answer presented as a merge decision, and it is reachable today.

The sharpest instance: `normalize_phone` deliberately merges records whose `country` fields
disagree (`dedup.py:5-7` comment, `dedup.py:57-60` implementation). `country` has no trust
entry for any source, so the merged row's country is decided by whether `"SA" > "LB"`. A
Beirut business on a Gulf number is silently re-labelled `SA`, its Beirut address is replaced
by the Riyadh one, and its `region` becomes permanently blank.

## Findings

### S1 — `country` is arbitrated by alphabetical max, which silently relabels cross-border businesses and destroys their address

- **Where:** `dedup.py:185` (the `str(value)` final tiebreak), reached because
  `_SOURCE_TRUST` (`dedup.py:97-110`) has **no `country` entry for any source** (so both
  sides hit `_DEFAULT_TRUST = 1` at `dedup.py:111`) and `"country" not in _VOLATILE`
  (`dedup.py:114`). The cross-border merge itself is by design: `normalize_phone` returns
  `+966…` regardless of the `country` argument (`dedup.py:57-60`).
- **Breaks:** `all_businesses.csv` gets a self-contradictory row. Concretely, for an OSM
  node in Hamra with `phone=+966501234567` merging with the matching Google place in
  Riyadh:
  - `country` → `"SA"` (alphabetical, not evidence-based)
  - `address` → `"Al Olaya, Riyadh"` (Google trust 3 at `dedup.py:100` beats OSM trust 2 at
    `dedup.py:104`), so **the Beirut address is gone from the cumulative master**
    (`main.py:209` rewrites the file wholesale; the only recovery is the dated Drive
    archive at `.github/workflows/scrape.yml:88`)
  - `lat`/`lon` stay `33.89, 35.50` from OSM (Google's were absent), because
    `_is_missing(None)` makes OSM's win by default (`dedup.py:168-169`)
  - `region` → `None`. `enricher.py:85` (`if address and country == "LB"`) is skipped, and
    `enricher.py:90` selects `_KSA_COORD_REGIONS` for `country == "SA"`, where Beirut
    coordinates match no box.
  - The flip is **sticky**: `resolve_country` (`main.py:59-63`) returns on the first branch
    the moment `country in ("LB","SA")`, so it never reaches the phone-based fallback at
    `main.py:68-73` that would have noticed the contradiction.
  - `leadminer validate` cannot catch it: `in_box` (`cli.py:159-173`) accepts the row via
    the Lebanon box because the coordinates were kept.
- **Trigger** (verified, no network):
  ```python
  lb = {"name":"Beirut Dental","country":"LB","address":"Hamra, Beirut, Lebanon",
        "phone":"+966501234567","lat":33.89,"lon":35.50,"source":"osm",
        "scraped_at":"2026-10-01T00:00:00+00:00"}
  sa = {"name":"Beirut Dental","country":"SA","address":"Al Olaya, Riyadh",
        "phone":"+966501234567","source":"google_places",
        "scraped_at":"2026-10-02T00:00:00+00:00"}
  _merge(lb, sa)
  # -> {'country': 'SA', 'address': 'Al Olaya, Riyadh', 'lat': 33.89, 'region': None}
  _merge(sa, lb)["country"]   # -> 'SA'   (order-independent, so not a bug in itself)
  dedup([lb, sa])            # -> 1 row, country 'SA'
  infer_region("Hamra, Beirut, Lebanon", 33.89, 35.50, country="SA")  # -> None
  infer_region("Hamra, Beirut, Lebanon", 33.89, 35.50, country="LB")  # -> 'Beirut'
  ```
  This is the *intended* market case, not a contrived one: a KSA-owned business operating in
  Beirut is a core target segment for this pipeline.
- **Same root cause, narrower blast radius:** `region` also has no trust entry, so two
  Google records for the same `+961` number land in different regions and the tiebreak
  picks alphabetically. Verified: `_pick("region", {"region":"Beirut","source":"osm"},
  {"region":"North Lebanon","source":"google_places"})` → `"North Lebanon"`.
- **Fix:** `_pick` must never reach `str(value)` for identity fields. Special-case
  `country` (resolve jointly with `address`, taking both from the record whose `country`
  matches the phone-derived code) and give `region` a real rule, or add
  `_NEVER_TIEBREAK = {"country", "region"}` and return `None` on conflict so the row is
  visibly incomplete instead of silently wrong.

### S2 — `website_live`'s declared tri-state is collapsed: a stale `False` outranks a fresh `None`, permanently

- **Where:** `dedup.py:167-171` + `_is_missing` (`dedup.py:86-90`). Declared
  `website_live: bool | None` at `base.py:16`.
- **Breaks:** `_is_missing(False)` is `False` (not `None`, not a `str`), so `False` counts
  as present; `_is_missing(None)` is `True`, so line 170-171 returns the stale `False` and
  discards the fresh `None`. `_validity` gives both 1 and `dedup.py:140` is unreachable, so
  the early return fires before ranking. `None` here means *"we never reached the host"* —
  not *"no value"* — and the merge treats it as the latter. `False` is the single
  highest-value signal in the product (`+20` lead score at `enricher.py:303-304`, Tier-1
  pitch `"Website rebuild + maintenance"` at `pitch_recommender.py:57-58`).
- **Trigger** (verified):
  ```python
  old_dead  = {"website":"https://x.example","website_live":False,
               "source":"google_places","scraped_at":"2026-09-01T00:00:00+00:00"}
  new_unk   = {"website":"https://x.example","website_live":None,
               "source":"google_places","scraped_at":"2026-10-01T00:00:00+00:00"}
  _pick("website_live", old_dead, new_unk)   # -> False  (fresh "unknown" discarded)
  ```
  Note `"website_live"` *is* in `_VOLATILE` (`dedup.py:114`) and would have picked the
  fresher record — the `_is_missing` early return pre-empts it.
- **Reachability, honestly:** on the `main.py` path this is currently masked, because
  `enricher.check_websites` unconditionally overwrites `r["website_live"]` for every record
  that has a website (`enricher.py:231`, `enricher.py:250`), and any record carrying
  `website_live=False` necessarily also carries a website. It is live in three other ways:
  1. `leadminer score` (`cli.py:247-277`) re-scores an existing CSV with **no refetch**, so
     a stale `False` there keeps `+20` and the rebuild pitch forever.
  2. `leadminer validate` (`cli.py:135-201`) reads the same CSV, so the tri-state it audits
     is the tri-state `dedup` produced.
  3. Any future `dedup`-without-`enrich` path — the obvious next CLI subcommand, a unit test
     of `dedup`, a re-keying migration — hits it on line one.
- **Fix:** give `website_live` a `_pick` special case alongside `source`/`scraped_at` with
  the semantically correct union: `True` if any side is `True`, else `False` if any side is
  `False`, else `None`. Positive evidence of a dead site should not be undone by a network
  timeout, and `_is_missing` should not be extended to handle tri-states.

### S2 — The union `source` string re-enters `_trust_for`, ratcheting every value's trust up to the best of any source that ever touched the record

- **Where:** `dedup.py:173-174` writes `"|".join(sorted(...))` into `source`, declared as a
  plain `str` at `base.py:26`. That widened domain is then re-parsed by `_sources_of`
  (`dedup.py:144-145`) and aggregated with `max()` by `_trust_for` (`dedup.py:148-152`).
- **Breaks:** the contract comment at `dedup.py:93-96` — *"Trust therefore has to be
  per-field, not per-record"* — becomes false after the first merge. `_trust_for(field, rec)`
  returns the best score of **any** source in the union for **any** field, so once a record
  has been touched by more than one source, values inherit trust they were never awarded.
  It only ever ratchets **up**, and never resets.
- **Trigger** (verified): a wikidata-sourced email (trust 2, `dedup.py:107`) merges with an
  OSM record that contributes only a website (trust 3, `dedup.py:103`):
  ```python
  wd  = {"email":"stale@wd.example","source":"wikidata",
         "scraped_at":"2026-01-01T00:00:00+00:00","website":None}
  osm = {"email":None,"website":"https://real.example","source":"osm",
         "scraped_at":"2026-01-01T00:00:00+00:00"}
  m = _merge(wd, osm)                      # -> source 'osm|wikidata', email 'stale@wd.example'
  _trust_for("email", m)   # -> 3   (was 2 when it was pure wikidata)
  _pick("email", m, {"email":"fresh@wd.example","source":"wikidata",
                     "scraped_at":"2026-10-01T00:00:00+00:00"})
  # -> 'stale@wd.example'   (fresh wikidata email, trust 2, loses)
  # same pair without the union -> 'zzz@other.example'
  ```
  The wikidata value is now permanently graded as OSM-grade.
- **At scale:** `all_businesses.csv` is cumulative and re-read every run
  (`main.py:150`, downloaded at `.github/workflows/scrape.yml:56`), and the cron runs
  weekly (`scrape.yml:9`). Every record the pipeline has ever re-merged carries a union, so
  after N runs `_trust_for` saturates at 3 across the board, stops discriminating, and
  `_pick` degenerates to the `str(value)` tiebreak for the entire row — including S1.
- **Fix:** carry provenance as a real set (`sources: list[str]` or `frozenset[str]`)
  instead of an undocumented delimiter, and make `_trust_for` score the value against the
  source that *produced that value*, not against the record's accumulated provenance.
  Minimum viable: store `_pick`'s per-field winner source in a parallel `field_source`
  map.

### S2 — `_validity` accepts any parseable coordinate and `_SOURCE_TRUST` gives Google lat/lon 3 while OSM lat/lon is undeclared, so `lat=0.0` beats a real one

- **Where:** `dedup.py:128-133` — `float(value)` succeeding is the *entire* plausibility
  check for `lat`/`lon`. `dedup.py:102-105` — `_SOURCE_TRUST["osm"]` has **no `lat`/`lon`
  entry**, so OSM coordinates fall to `_DEFAULT_TRUST = 1` (`dedup.py:111`) while Google is
  awarded 3 (`dedup.py:99`).
- **Breaks:** verified — `_pick("lat", {"source":"google_places","lat":0.0},
  {"source":"osm","lat":33.89})` → **`0.0`**. Trust 3 beats 1; `_validity` scores `0.0` a
  full 3 because `float(0.0)` parses. Null Island permanently displaces the
  contributor-verified OSM coordinate. `enricher.infer_region` (`enricher.py:89-93`) then
  box-matches `(0.0, 0.0)`, finds nothing, and returns `None` — blank region. The row
  ships with `0,0` and `cmd_validate` counts it (`cli.py:164-173`), but the CI gate never
  runs the validator: `.github/workflows/scrape.yml:71-83` only checks the file is non-empty
  and has >100 rows.
- **Note the two definitions of "plausible":** `cli.py:159-173` already knows the LB/KSA
  bounding boxes. `_validity` runs in-process, earlier, and does not — so the cheap in-memory
  gate is laxer than the expensive out-of-band one. One bounds table, used by both.
- **Fix:** move `in_box` into a shared module and call it from `_validity`; add
  `"lat": 3, "lon": 3` to `_SOURCE_TRUST["osm"]` — an OSM node's coordinates are
  contributor-verified and deserve at least parity with Google's.

### S2 — `max(av, bv)` on `scraped_at` is the one comparison in `_pick` that is not `str()`-coerced

- **Where:** `dedup.py:176-177`. Every other comparison in `_pick`/`rank` coerces:
  `_recency` returns `str(...)` (`dedup.py:156`) and the tiebreak is `str(value)`
  (`dedup.py:185`). `max()` is the exception.
- **Breaks (loud):** verified — `_pick("scraped_at", {"scraped_at": 20260101.0, ...},
  {"scraped_at": "2026-09-01T00:00:00+00:00", ...})` raises
  `TypeError: '>' not supported between instances of 'str' and 'float'`. Nothing catches it:
  `dedup(combined)` at `main.py:180` is unguarded and `cli.main` only catches
  `KeyboardInterrupt` (`cli.py:316`). The run dies *after* scraping and *before* any CSV is
  written (`main.py:209-213`), so an entire scheduled run — a 300-minute budget at
  `scrape.yml:25` — is lost to one cell.
- **Breaks (silent, and this is the worse one):** ISO-8601 with a non-UTC offset sorts
  backwards. Verified:
  ```python
  _pick("scraped_at", {"scraped_at":"2026-09-01T12:00:00+03:00","source":"osm", ...},
                   {"scraped_at":"2026-09-01T10:00:00+00:00","source":"osm", ...})
  # -> '2026-09-01T12:00:00+03:00'  == 09:00 UTC — one hour OLDER, chosen as "newer"
  ```
  `_recency` (`dedup.py:156`) then feeds that inverted value into the freshness comparison
  for all four `_VOLATILE` fields. Verified follow-on: with the above pair carrying
  `website`, `_pick("website", old, new)` returns the **older** website. Wrong data, no
  error, and `scraped_at` is the only provenance the merge has.
- **Reachability:** not reachable from the three scrapers today — all use
  `httpclient.utc_now_iso()` (`osm.py:32`, `wikidata.py:67`, `google_places.py:173` →
  `httpclient.py:220-229`), which is always `+00:00`. It **is** reachable from the master
  CSV: `data/all_businesses.csv` is a Drive-hosted, downloaded artifact
  (`scrape.yml:50-56`) that operators open in Sheets, and `leadminer score` consumes it
  directly (`cli.py:254-259`). And any fourth source that timestamps in local time — the
  obvious next scraper — trips the silent variant immediately. Also note the field is
  declared non-optional `str` at `base.py:27` but `_pick` can return `None` for it
  (`dedup.py:168-169`).
- **Fix:** `return max(str(av), str(bv))` to close the crash, and compare parsed
  `datetime` objects rather than strings to close the silent inversion.

### S3 — `-> object` and `dict` annotations make the contract unenforceable, and mypy is configured but never run

- **Where:** `dedup.py:159` (`a: dict, b: dict`, `-> object`), `dedup.py:190-194`
  (`merged: dict` built from `set(a) | set(b)`), `dedup.py:197` (`list[dict]`).
  `BusinessRecord` is imported by exactly three modules — `osm.py:5`, `wikidata.py:5`,
  `google_places.py:28`. `dedup.py`, `enricher.py`, `main.py` and `cli.py` never import it.
- **Breaks:** `-> object` erases the value type for every caller, `_merge` returns `dict`,
  and `dedup` returns `list[dict]` — so the entire post-scrape pipeline is untyped even
  though `main.py:180` is the only place the record crosses from typed to untyped. And
  `TypedDict` is `total=True` (`base.py:5-28`), which `_merge` makes irrelevant by
  construction, since it unions whatever keys it finds.
- **The check that would catch all of the above is not wired up:** `pyproject.toml:64-68`
  sets `strict = true` and `mypy>=1.11` is in the dev extras (`pyproject.toml:26`), but CI
  runs only `python main.py` (`.github/workflows/scrape.yml:65-69`). No `mypy`, no
  `pytest`, no `ruff`. `tests/test_lead_signal.py` exists and passes locally but never runs
  in CI.
- **Fix:** move `BusinessRecord` to a top-level `contracts.py` (keeps the `scrapers → dedup`
  import direction acyclic), annotate
  `dedup(records: list[BusinessRecord]) -> list[BusinessRecord]`, and add a
  `lint` job running `mypy` + `pytest` + `ruff` to the workflow. Without the last part the
  annotations are decorative.

### S3 — `_merge` is key-agnostic: the `field` argument comes from CSV headers, not from the contract

- **Where:** `dedup.py:192` — `for key in set(a) | set(b): merged[key] = _pick(key, a, b)`.
- **Breaks:** verified — `_merge({"name":"A","notes":"keep me","osm_id":"node/1"},
  {"name":"A","notes":None,"google_id":"ChIJ"})` returns keys
  `['google_id','name','notes','osm_id']`. Unknown columns round-trip through `dedup`
  silently and are dropped only by `extrasaction="ignore"` at `main.py:125`.
- **Correction to prior audit:** `docs/audits/020-type-contract-import-cycle.md:210-223`
  flagged that unknown keys could *steer* merge priority via `_field_count`. That half is
  fixed by commit `99493b9` — per-field trust replaced the whole-record count. What remains
  is propagation plus a bad ranking rule: an unknown key gets no trust (default 1,
  `dedup.py:111`), no validity check (`dedup.py:140`), is not volatile, so it is resolved
  by `str(value)` lexicographic max — the crudest rule in the file, applied silently to
  data nobody has looked at.
- **Forward risk:** `docs/audits/043-scoring-upgrade.md:393` proposes adding
  `score_breakdown` (a structured value) to `BusinessRecord`. `_is_missing({...})` is
  `False`, `_validity` returns 1, and `rank` compares `str({...})` — dicts merged by their
  `repr`. No crash; silently meaningless.
- **Fix:** iterate `FIELDS` (or `BusinessRecord.__annotations__`) instead of
  `set(a) | set(b)`, and log the dropped keys once per run so drift is visible on the run
  that introduces it. This also converges with the S3 above and with
  `docs/audits/020` S3 (`FIELDS` and the TypedDict are declared twice with nothing linking
  them).

## Not a bug, but worth knowing

- **The lexicographic `str(value)` tiebreak (`dedup.py:185`) is safe for the current value
  ranges — I checked.** `str()` comparison of floats inverts only across a power of ten
  (`"100.0" < "99.9"`), and LB/KSA coordinates, `rating ∈ [0,5]` and `review_count` never
  cross one. It is *not* a general numeric comparison, so it is a trap for the next field
  with a wide range — put S1's `_NEVER_TIEBREAK` set in place before adding one.
- **`name`, `category`, `address`, `website`, `phone` are all safe**: the trust axis
  (`dedup.py:99-108`) discriminates before the tiebreak is reached, and `website`/`phone`/
  `name` are the dedup keys themselves (`dedup.py:205`, `dedup.py:214`).
- **`_is_missing` treats `0`, `0.0` and `False` as present** (verified). That is correct in
  isolation for `review_count=0` and is precisely what causes S2/`website_live`.
- **The `source` union is idempotent and order-independent** (verified): `_sources_of`
  splits on `|`, so `"a|b" ∪ "b"` → `"a|b"`, and `_merge(a,b)["source"] ==
  _merge(b,a)["source"]`. `tests/test_lead_signal.py:316-319` already asserts this. The
  union itself is not the bug — the trust aggregation it feeds (S2) is.
- **`lead_score`, `completeness_score`, `industry_priority`, `recommended_service`** are
  carried through `_pick` and then unconditionally recomputed (`main.py:190-197`,
  `enricher.py:349-350`), so a stale value there is harmless. `country` is the exception —
  `resolve_country` (`main.py:59-63`) *trusts* the merged value, which is why S1 sticks.
- **`dict(record)` at `dedup.py:210`/`dedup.py:218` is a shallow copy and `_pick` returns
  values by reference** (`dedup.py:169`, `dedup.py:171`, `dedup.py:187`). Harmless while
  every field is a scalar; becomes aliasing across output rows the moment a mutable field
  lands — which 043's `score_breakdown` would do.
- **Doc nit:** `BRIEF.md:18` says "22 keys" and `BRIEF.md:57-62` says "The 22 columns",
  but `BusinessRecord` (`base.py:5-28`) and `main.FIELDS` (`main.py:33-40`) both declare
  **23**. Already flagged at `docs/audits/042-exports-design.md:5` and
  `docs/audits/031-test-suite-design.md:35`; still uncorrected.

## Recommended order of work

1. **S1 `country`** — one rule plus one regression test. It is the only finding that
   destroys data today.
2. **S3 annotations + `mypy`/`pytest`/`ruff` in CI** — the cheapest way to stop the next
   three findings from being silent. Do this before the refactors so they are verified.
3. **S2 `website_live` tri-state** — small, self-contained, and it is a live landmine under
   `leadminer score`.
4. **S2 `_SOURCE_TRUST` completeness + geographic `_validity`** — one shared `in_box`
   table closes both the `lat=0.0` hole and S1's residue.
5. **S2 `scraped_at`** — coerce, then parse. Two-line fix for a run-killer plus a silent
   inversion.
6. **S2 union-`source` trust ratchet** — largest refactor; schedule it before 043's
   `score_breakdown` lands, not after.
7. **S3 restrict `_merge` to `FIELDS`** — and derive `FIELDS` from the TypedDict so the two
   declarations stop drifting.