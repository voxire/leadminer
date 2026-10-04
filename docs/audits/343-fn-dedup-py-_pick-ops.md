# 343 — `_pick`: the merge that erases leads without a sound

**Lens:** ops — six hours on a cron, from the operator's chair.
**Target:** `dedup.py:159` `_pick` (introduced by `99493b9 fix: per-field merge survivorship`).
**Scope:** what `_pick` prints, swallows, and returns when the network, the data, or the
clock goes wrong.

## Verdict

`_pick` prints nothing, logs nothing, counts nothing, and catches nothing. It is the only
function in the pipeline with the power to make a lead **permanently** disappear — and it
exercises that power every single run with no evidence left behind. Worse, the ranking tuple
it just gained puts `trust` above `recency`, while `_pick` itself makes trust permanently
sticky by unioning `source` on every merge, so once a record has ever touched OSM, no later
Google Places observation can ever update that field again — and the `scraped_at` column
concurrently stamps the stale value with the *new* run's date.

The good news, and it is real: `_pick` is fast (200k records in 1.49s), pure, and holds no
resources, so it will not be what eats your six hours or what dies at the 300-minute timeout.
`main.py:121-133` writes atomically, so an interrupt mid-merge loses nothing. **The risk is
entirely in correctness and silence, not in runtime or durability.**

## What `_pick` prints: nothing. Ever.

`dedup.py:159-187` contains no `print`, no `logging` call, no counter, no metric, no
`try`. Compare the rest of the codebase, which is conspicuously loud: `osm.py:47-56` shouts
on failure, `enricher.py:235,262` prints progress every 200 fetches, `httpclient.py` has a
whole error taxonomy. `_pick` is the quietest function in the repo, and it is the one holding
the master record.

The single line printed near it, `main.py:181 After merge + dedup: N unique businesses`, is
**invariant to merge damage**. Verified:

```
len with osm: 2000   len after total OSM outage: 2000   -> identical
```

If Overpass returns nothing, the master rows still flow through, so the count is unchanged.
`scrape.yml:74-83` then checks only `rows >= 100`. A fully dead source, a fully broken merge
rule, and a perfect run produce the same three numbers and the same green check.

## Findings

### S1 — Trust outranks recency, and `_pick` inflates trust itself, so a field can never be updated again

- **Where:** `dedup.py:179-187` (rank order), `dedup.py:173-174` (source union),
  `dedup.py:148-152` (`_trust_for` = max over the union of sources).
- **Breaks:** `website` is explicitly in `_VOLATILE` (`dedup.py:114`) precisely so that the
  fresher observation wins. But in the tuple returned by `rank`, recency is the **third**
  element and is only consulted when `trust` and `validity` tie. `google_places` has
  `website: 2` (`dedup.py:100`); `osm` has `website: 3` (`dedup.py:103`). So OSM outranks
  Google on trust alone, before recency is ever read.
  Then `_pick` line 174 unions `source` on every merge, and `_trust_for` takes
  `max()` over that union. **The accumulated master row inherits OSM's trust on every field
  forever, whether or not OSM ever supplied that field.**
- **Trigger** (executed):
  ```python
  a = {"source": "osm",           "website": "http://old.example.com",
       "scraped_at": "2026-09-01T00:00:00+00:00"}
  b = {"source": "google_places", "website": "https://new.example.com",
       "scraped_at": "2026-10-01T00:00:00+00:00"}
  _pick("website", a, b)
  # rank(a) = (3, 3, '', 'http://old.example.com')
  # rank(b) = (2, 3, '', 'https://new.example.com')   <- valid AND 5 weeks fresher
  # -> 'http://old.example.com'
  ```
  Persistence across runs, also executed:
  ```
  wk1 merge -> source = 'google_places|osm',  _trust_for('website') = 3
  wk2 merge -> website = 'http://old.example.com'   # 'https://BRAND-NEW.example.com' discarded
  ```
- **Compounding:** line 177 merges `scraped_at` as `max(av, bv)`, so the merged row carries
  `2026-10-01`. Verified. **The CSV now asserts the stale URL was scraped in October.** The
  one column an operator would use to spot staleness is laundered by the very merge that
  caused it. There is no per-field observation timestamp anywhere in the 22 columns
  (BRIEF 59-62), so nothing downstream can recover the truth.
- **Fix:** put `_recency` first in the tuple for `_VOLATILE` fields, or better, stop using a
  mutable union as a trust signal — carry per-field `source` provenance and trust the record
  that actually supplied the surviving value. Emit the freshness of the value, not the
  freshness of the merge.

### S1 — `str(value)` tiebreak silently deletes a real, distinct business from the cumulative master

- **Where:** `dedup.py:184` and `:187`.
- **Breaks:** when `trust` and `validity` tie and the field is not volatile, the winner is
  whichever `str(value)` is **lexicographically greater**. Verified:
  ```python
  _pick("name", {"source": "google_places", "name": "Al Huria Trading"},
                 {"source": "google_places", "name": "Beirut Auto Parts"})
  # -> 'Beirut Auto Parts'      # 'Al Huria Trading' is simply gone
  ```
  (I initially predicted the *first* alphabetically would win and was wrong; `>=` on the
  tuple means the *largest* string wins. The rule is still arbitrary — it is sort order
  standing in for business identity — but state it correctly if you write the test.)
  `dedup.py:205-210` funnels every record with a normalizable phone into `phone_index` by
  phone key alone. Shared reception lines, franchise numbers, mall switchboards, and junk
  OSM/Wikidata values with ≥7 digits all collapse to one key. This is not exotic in
  Lebanon/Riyadh.
- **Ops consequence:** `all_businesses.csv` is cumulative (BRIEF 51) and is re-uploaded
  wholesale every week (`scrape.yml:88-89`). A row collapsed in week 3 is unrecoverable:
  `main.py:209-213` persists only post-merge output, the pre-merge records are never
  archived, and there is no tombstone. **The single property a lead-gen product must have —
  never lose a lead — is the one property `_pick` breaks.**
- **Time to notice:** a sales rep on the phone in month two, saying "you had this in March."
- **Fix:** a phone-key collision between two records with different `normalize_name` values
  is not a merge, it is a flag. Emit the conflict and keep both, or add a `_survivors` side
  channel that `_merge` can never collapse.

### S1 — Zero telemetry on the only function that can delete data; a total source outage is indistinguishable from a clean run

- **Where:** `dedup.py:159-187` (no output of any kind); `dedup.py:190-194` (`_merge`);
  `main.py:167-168` (scraper exceptions downgraded to one stderr line);
  `scrape.yml:74-83` (the only automated gate).
- **Breaks:** there is no way to answer, after the fact, any of the three questions an
  operator actually asks: *how many records merged this week, how many lost a value, and
  which field lost it.* `_merge` returns a single synthesized dict; the losing values are
  never captured, counted, or written anywhere. Combined with a source that fails closed
  (`osm.py:47-56`, `wikidata.py:79-85`) and returns zero records, the pipeline cannot tell
  "Overpass is down" from "Overpass legitimately had nothing new."
- **Trigger:** kill the Overpass endpoint. Log gains one line. `main.py:170-181` prints
  identical counts. `scrape.yml` passes validation. Drive receives a new master. Repeat for
  three weekly cycles and nobody has a reason to look.
- **Time to notice: never**, absent an external anomaly check. (`041-anomaly-detection.md`
  and `040-observability.md` address the platform-level answer; this is the specific gap:
  the counters `_pick` itself must own.)
- **Fix:** return a stats dict from `dedup` — merges, per-field conflicts, per-field
  value-dropped-without-replacement — and print it at `main.py:181` next to the row count
  that currently proves nothing.

### S2 — `_validity` accepts `nan`, `inf` and out-of-range coordinates, and the CSV round-trip re-legitimises them every week

- **Where:** `dedup.py:128-133`.
- **Breaks:** `lat`/`lon` get a bare `float(value)` and an unconditional `return 3`. Unlike
  `rating`, which range-checks `0 <= v <= 5` at `dedup.py:139`, coordinates are never
  bounded. Verified:
  ```
  lat='nan' validity=3    lat='inf' validity=3
  lat=999.0 validity=3    lat=-999.0 validity=3
  ```
  A `NaN` therefore outranks a real coordinate (both score 3, but `str()` ordering then
  picks, and `float('nan')` round-trips through `main.py:92` `float(row[field])` without
  error, re-earning validity 3 on every future run). The poison is permanent in a
  cumulative master.
- **Downstream, silently:** `enricher.py:92` `lat_min <= lat <= lat_max` is `False` for NaN,
  so region inference is skipped, `region` stays `None`, and the row moves between
  `qualified_businesses.csv` and `without_websites.csv` for reasons no error reports.
- **Time to notice:** never, unless a human opens the CSV and sees `nan`.
- **Fix:** `return 3 if -90 <= v <= 90 else 0` for lat, `-180 <= v <= 180` for lon, plus an
  explicit `math.isfinite(v)` guard.

### S2 — Field-independent merging produces chimera rows that no source ever described

- **Where:** `dedup.py:190-194`; `_trust_for` `dedup.py:148-152`.
- **Breaks:** `_merge` decides every field independently, so the output is a combination
  that no input record contained. Verified:
  ```python
  a = {"source":"osm",          "region":"Beirut", "country":"LB", "address":None}
  b = {"source":"google_places","region":None,   "country":"SA",
       "address":"Riyadh, Saudi Arabia"}
  _merge(a, b)  # -> region='Beirut', country='SA', address='Riyadh, Saudi Arabia'
  ```
  A Beirut region with a Riyadh address is assigned to Saudi territory. Note that neither
  `country` nor `region` appears anywhere in `_SOURCE_TRUST` (`dedup.py:97-110`), so both
  fall to `_DEFAULT_TRUST = 1` for every source and are decided by the `str(value)`
  tiebreak alone — **alphabetical order is silently deciding which country a lead belongs
  to.** `"SA" > "LB"`, so Saudi wins every cross-border conflict, regardless of the phone.
- **Poison feedback loop:** the chimera is written to the master, re-read next week, and fed
  back into `_extract_city` (`dedup.py:72-76`), which returns `parts[-1]`. For Google
  `formattedAddress` that is the **country** (`"Riyadh, Saudi Arabia"` → `"saudi arabia"`),
  so the name key is effectively `(name, country)` and can never separate two same-named
  businesses in different cities.
- **Fix:** treat `region` and `country` as derived, recompute them after the merge from the
  surviving address/coords (as `main.py:183-184` already does for country), and let `_pick`
  handle only genuinely independent fields.

### S2 — `max()` on `scraped_at` raises `TypeError` on mixed types and kills the run

- **Where:** `dedup.py:176-177`. Contrast `_recency` at `dedup.py:155-156`, which wraps in
  `str()`. The `max()` at line 177 does not.
- **Breaks:** verified —
  ```python
  _pick("scraped_at", {"scraped_at": "2026-10-01"}, {"scraped_at": 20261001})
  # TypeError: '>' not supported between instances of 'int' and 'str'
  ```
  Uncaught, so it propagates through `_merge` → `dedup` → `main.py:180` and aborts the run.
  Reachability from a CSV is low (`csv.DictReader` always yields `str`), which is why this is
  S2 not S1 — but `load_master` (`main.py:86-104`) casts *some* fields and not others, so
  any future writer that emits a numeric timestamp poisons the master **permanently** and then
  kills every subsequent run, after the scrapers have already spent the run's Google quota
  and against the 300-minute budget (`scrape.yml:25`).
- **Fix:** `return max(str(av), str(bv))`.
- **Note:** this is the *only* failure mode in `_pick` that is loud. A run dying at 4 hours is
  arguably the correct outcome.

### S3 — The `-100` sentinel is dead code, and the one field with no plausibility check is the dedup key

- **Where:** `dedup.py:123`, `dedup.py:140`.
- `_validity` returns `-100` for missing values, but `_pick` short-circuits at
  `dedup.py:168-171` before `rank` is ever built, so `-100` is unreachable from this call
  path. Verified: `_validity("email", None) == -100`, and no `_pick` input reaches it. The
  docstring at `dedup.py:121` ("0-3 bonus") is also wrong about its own return domain.
  Meanwhile `phone` — the field that *decides whether two records are the same business* —
  has no branch at all and falls through to the constant `1` at line 140. Verified:
  `"abc"`, `"0000000"` and `"+9611234567"` all score 1.
- **Fix:** delete the `-100` branch, add a `phone` branch that runs `normalize_phone` and
  requires a non-empty result.

## Silent-failure inventory, ranked by time-to-detect

This is the lens question answered directly. All eight verified by execution.

| # | Failure | Signal a human gets | Time to notice |
|---|---|---|---|
| 1 | Source silently dies (Overpass/SPARQL 4xx, or Google key loses quota) | none — counts invariant, `scrape.yml` passes | never, without an anomaly check |
| 2 | `_pick` keeps a stale field forever because trust outranks recency (S1 #1) | none — `scraped_at` is `max()`, so it looks *fresh* | months |
| 3 | Distinct businesses collapsed on a shared phone key (S1 #2) | none — row count looks right | months, via a lost deal |
| 4 | `NaN`/out-of-range coords outrank real ones (S2) | none | never |
| 5 | `region`/`country` assigned by alphabetical tiebreak (S2) | none | months, via wrong-territory outreach |
| 6 | Value dropped with no replacement; no count of how many | none | never |
| 7 | Pre-merge inputs never persisted, so `_pick`'s decisions cannot be replayed or re-derived | none | never — **re-architectural** |
| 8 | Mixed-type `scraped_at` → `TypeError` | full stack trace, exit 1 | minutes |

Row 7 deserves its own line. **The merge is not replayable.** `main.py:209-213` writes only
the post-merge CSVs; the pre-merge scraper output is never persisted. Changing any rule inside
`_pick` therefore requires re-running the entire scrape — hours of Google Places quota and
Overpass load — just to observe the effect of a one-line change to a `rank` tuple. Every
future improvement to the merge rule inherits this cost. That, more than any individual bug
above, is what will keep this function from being fixed properly.

## Not a bug, but worth knowing

- **`_pick` will not be your performance problem.** 200k unique-key records dedup in
  **1.49s**; 50k in 0.44s. The pathological case — 50k records collapsed onto one shared
  phone key — takes 4.49s and produces exactly **1 row**, which is a data bug, not a speed
  bug. Do not spend the six hours optimising here.
- **Interruption is handled, by someone else, correctly.** `_pick` is pure: no I/O, no locks,
  no shared state, no partial writes. `main.py:121-133` writes to a temp file, `fsync`s, and
  `os.replace`s. Killing the job mid-merge loses the in-memory dicts and nothing on disk.
  This is genuinely fine and should not be changed.
- **Empty input is clean.** `dedup([])` returns `[]` without touching `_pick`; `main.py:206`
  guards with `.get("completeness_score", 0)`. No crash. The *related* risk belongs to
  `main.py`, not this function: a local `python main.py` with all three scrapers failing
  writes a header-only cumulative master, and only the workflow-side gate at
  `scrape.yml:74-83` stops the upload.
- **A network drop halfway is the same code path as "nothing new".** `_pick` never touches
  the network, so it cannot detect the drop. But `dedup.py:170-171` (`if _is_missing(bv):
  return av`) means "the source that would have contradicted me was down" and "the source
  had nothing new to say" are indistinguishable at the call site. Recovery requires the
  source to come back; the merge has no memory of what it never saw.
- **`lead_score`'s multi-source bonus is earned on `_pick`'s union, not on agreement.**
  `enricher.py:316-317` awards +5 for a `"|"` in `source`, which `_pick` line 174 creates
  from record-level union alone. Two records that agreed on nothing except a name can both
  score the bonus. Defensible as a "we saw it twice" signal; do not let anyone read it as
  corroboration.
- **Ranks 002/024 covered the *previous* `_pick`** (whole-record richness, arrival-order
  non-determinism). `99493b9` genuinely fixed those. The findings above are new properties
  of the replacement, and the sticky-trust regression is a direct consequence of the fix:
  per-field trust plus record-level source union is an inconsistent pair.

## Recommended order of work

1. **Make the merge observable before changing it.** Add a stats return to `dedup` and print
   merges / conflicts / values-dropped at `main.py:181`. Every other fix below is unverifiable
   until this exists, and it is the only thing that converts failures 1-6 from "never" into
   "within one run".
2. **Reorder `rank` for `_VOLATILE` fields** — recency before trust — and stop deriving trust
   from a union that `_pick` inflates (S1, `dedup.py:179-187`, `:173-174`). Ship with a
   regression test asserting a fresher valid Google website beats a stale OSM one.
3. **Stop `_pick` from choosing between businesses.** A `normalize_name` mismatch inside a
   shared phone key must be reported, not resolved by `str()` (S1, `dedup.py:184`).
4. **Persist pre-merge scraper output** (parquet/jsonl, one run per timestamp) so merge
   behaviour can be replayed offline. Unblocks every remaining fix and makes the cumulative
   master recoverable.
5. **Range-check `lat`/`lon`**, `str()`-wrap the `scraped_at` `max()` (S2, `dedup.py:128-133`,
   `:176-177`).
6. **Recompute `country`/`region` after the merge** instead of letting the tiebreak assign
   them; treat them as derived, not merged (S2).
7. **Add a `phone` validity branch** and delete the unreachable `-100` (S3, `dedup.py:123`,
   `:140`).
