# 327 — `_trust_for` (dedup.py:148) under a six-hour cron: silent trust collapse, permanent field freezes, and zero operator surface

## Verdict

`_trust_for` is six lines, cannot raise on any realistic input, is not a performance risk, and is
deterministic. It is nonetheless the least observable decision in the pipeline: `dedup.py` contains
**zero** `print`, `log`, `logging` or `warnings` references, so every one of its ~42 calls per
`_merge` is invisible, and its failure mode is not an exception — it is a silent collapse of the
whole field-ranking system to alphabetical order (`_DEFAULT_TRUST = 1` for anything it does not
recognise, `dedup.py:111,151`) combined with a permanent one-way ratchet, because `_pick` persists
the *union* of source names (`dedup.py:173-174`) which `_trust_for` then reads back as if every
source had supplied every field (`dedup.py:150-151`). Two consequences dominate: a merged record's
`website`/`rating` can never again be updated by a lower-trust source (a stale dead URL is pitched
as "Website rebuild" forever), and the merge is not idempotent across runs, so the same business
merges differently in week 1 and week 2 purely because of what a previous run wrote to the CSV.

---

## Silent-failure ledger, ranked by how long a human would take to notice

This is the operational core of the report. Every row was reproduced locally with no network
access (see "Reproductions").

| # | Failure mode | What the operator sees | Time to notice |
|---|---|---|---|
| 1 | Source-union inflation: `_trust_for` scores a value by the trust of *every* source that ever touched the record (`dedup.py:150-151`), so a Google-supplied email is scored at OSM's trust 3 and can never be replaced by a fresh Wikidata value | Nothing. Same 23 columns, plausible counts, plausible values. Only symptom: `website` is frozen at a URL the business abandoned, `website_live=False`, `lead_score +20` (`enricher.py:303-304`) → the row is pitched "Website rebuild" to a company that already rebuilt | Days → **never**. Only a prospect saying "we rebuilt in March" surfaces it |
| 2 | Unknown / renamed / miscased `source` → `.get(src, {}).get(field, _DEFAULT_TRUST)` returns 1 (`dedup.py:151`) | Nothing. No exception, no log, no counter. Ranking degenerates to `_validity` → `_recency` → `max()` of the raw strings, so surviving URLs/emails are simply the alphabetically largest | Weeks → **never**. Indistinguishable from correct data |
| 3 | `_VOLATILE` recency is unreachable: `rank` compares `trust` first (`dedup.py:180-185`), so a 1-year-old Google rating beats today's OSM rating | Nothing. Ratings and review counts freeze at the first observation while `scraped_at` shows this week's date — the CSV actively asserts freshness it does not have | Months, and only if someone spot-checks a rating against Google |
| 4 | Ten of the 23 exported columns have no trust entry at all, including `phone` and `linkedin` → every conflict resolves to `max()` of the strings (`dedup.py:184`) | Nothing. `phone` is the primary dedup key *and* the primary sales channel, and its winner is decided by ASCII: `"05 123 4567"` beats `"+961 5 123 4567"`; `"…/company/zzz-old"` beats `"…/company/aaa-real"` | Weeks, and only when someone tries to dial from the CSV |
| 5 | `_DEFAULT_TRUST = 1` is a floor, not a distrust value: garbage provenance scores identically to a deliberately low trust | Nothing, and the function *cannot* express "untrusted", so no record can ever be routed to review | **Never** |
| 6 | `_sources_of` does not strip (`dedup.py:143-145`): one stray space voids part of the cell | Nothing. Partial collapse — some fields keep their trust, others silently drop to 1 | **Never** |
| 7 | Empty run: `dedup([])` returns `[]`, `_trust_for` executes zero times, and `main.py:209` writes a header-only cumulative master | Loud in CI only — `scrape.yml:74-83` catches `<100` rows. `leadminer run` exits **0** (`cli.py:33-37`, `main.py:245`) | Minutes in Actions; **never** on any other scheduler |

---

## Findings

### S1 — Source-union inflation freezes fields permanently and breaks run-to-run idempotence

- **Where:** `dedup.py:148-152` (reads *all* sources in the union), `dedup.py:173-174`
  (`_pick` writes the union into `source`), `main.py:179-180` (the cumulative master is fed back
  into `dedup` every run), `scrape.yml:88-89` (that master is re-uploaded weekly, so the union is
  durable).
- **Breaks:** `_trust_for` returns `max(...)` over every source in `record["source"]`, but
  `record["source"]` is the union of all sources that ever *touched* the record, not the source
  that supplied *this field*. A Google email inherits OSM's email trust of 3 purely because an OSM
  record with no email merged in on its phone number. Because the union is written back to the CSV
  and read back on the next run, this ratchets: once a record's union contains a trust-3 source for
  a field, **no source with lower trust can ever change that field again**, on any future run.
  Demonstrated end to end (three sources, three rows, one phone number):

  ```
  RUN 1 -> {'email': 'info@google.example',      'address': 'A, Beirut',
            'source': 'google_places|osm'}   _trust_for(email) = 3
  RUN 2 -> {'email': 'info@google.example',      'address': 'A, Beirut',
            'source': 'google_places|osm|wikidata'}      <- fresh wikidata email LOST
  CTRL  -> {'email': 'contact@fresh-wikidata.example', 'address': 'A, Beirut',
            'source': 'google_places|wikidata'}           <- same inputs, union never persisted
  ```

  The control row is the point: with identical inputs, whether the fresh value wins depends purely
  on whether a previous run wrote the union to disk.
- **Trigger:** any business present in two sources. A record whose master row carries
  `source="google_places|osm"` and whose `website` came from OSM can never accept a newer
  `websiteUri` from Google (trust 2 < 3) — verified:

  ```
  master   : source='google_places|osm'  website='http://old-site.example'
  this week: source='google_places'      website='https://new-site.example'
  merged   : 'http://old-site.example'   (trust 3 beats trust 2)
  ```

  The business rebuilt; the export keeps pitching a rebuild, forever, at +20 lead score.
- **Fix:** carry per-field provenance — store `_provenance: dict[field, set[source]]` alongside the
  value and score the value by *its own* sources. Minimum viable change: make `_trust_for` take the
  contributing source set as an argument instead of reading the record's union, and never let a
  union raise trust above what the winning value's own source scored.

### S1 — An unrecognised source name silently reduces the whole merge to alphabetical ordering

- **Where:** `dedup.py:151` — `_SOURCE_TRUST.get(src, {}).get(field, _DEFAULT_TRUST)`.
- **Breaks:** two chained `.get()` defaults convert any provenance defect into "trust 1" with no
  diagnostic of any kind. `dedup.py` has no logger and no prints (verified: the strings `print`,
  `log`, `logging`, `warnings` do not appear in the file), so there is no place a human could ever
  observe this. Verified inputs that all collapse to 1 for `email`:
  `"OSM"`, `"osm "`, `"google"`, `"google-places"`, `"openstreetmap"`, `""`, `None`, a missing key,
  `['osm']` (a list), `5` (an int). Observed effect:

  ```
  sources spelled right  -> website: http://zzz-osm.example      (trust decides)
  osm mislabelled        -> website: https://aaa-real.example    (alphabet decides; no warning)
  ```

  The output is still 23 well-formed columns with plausible values and a plausible row count, so
  neither the CI gate (`scrape.yml:74-83`) nor `cmd_validate` (`cli.py:135-201`) can see it. The
  only forensic trace is that surviving URLs become alphabetically large.
- **Trigger:** rename `source="osm"` at `osm.py:119` to `source="openstreetmap"`, or `"google_places"`
  at `google_places.py:302` to `"google_places_v1"`, or introduce whitespace in the union string.
  One character; no test fails; the next six-hour run publishes degraded data.
- **Fix:** validate at record construction — `source` must be a member of a
  `_KNOWN_SOURCES = frozenset(_SOURCE_TRUST)`; raise or count-and-log anything else at startup, and
  emit the unknown-name histogram once per run.

### S1 — `_VOLATILE` is dead code for every field where trust differs: ratings and websites freeze

- **Where:** `dedup.py:113-114` (`_VOLATILE = {"rating", "review_count", "website_live", "website"}`,
  commented "the fresher observation wins"), consumed at `dedup.py:180-185` as the *third* element
  of the rank tuple — behind `_trust_for` and `_validity`.
- **Breaks:** recency is only consulted when trust **and** validity tie. Verified:

  ```
  rating in _VOLATILE: True
  fresh OSM (2026-10-10) vs old Google (2026-01-01) -> 4.2   (trust 3 beats trust 1; recency never read)
  fresh Google (2026-10-10) vs old Google (2026-01-01) -> 4.9   (recency fires only on a trust tie)
  ```

  So the tie-break only ever works between two records from the *same* source. Combined with S1-1
  (the union raises the record's trust to the max ever seen), the first Google observation of a
  rating is the last one the CSV will ever hold, while `scraped_at` advances every week.
  `tests/test_lead_signal.py:303-309` (`test_volatile_field_prefers_the_fresher_observation`) uses
  two `google_places` records, so it ties on trust, exercises the recency branch, and passes — giving
  false confidence that volatility is handled. That is the most misleading artefact I found in the
  repo.
- **Trigger:** `{"source": "google_places", "rating": 4.2, "scraped_at": "2026-01-01…"}` vs
  `{"source": "osm", "rating": 4.8, "scraped_at": "2026-10-10…"}`.
- **Fix:** put recency ahead of trust for volatile fields — `rank` should return
  `(trust, validity, …)` for stable fields but `(recency, validity, trust, …)` for `_VOLATILE` ones;
  and add a test where the two records have *different* sources.

### S2 — Ten of the 23 exported columns can never be discriminated; `phone` is decided by ASCII

- **Where:** `dedup.py:97-110` (the table), `dedup.py:192` (`_merge` iterates every key in
  `set(a) | set(b)`, not just `FIELDS`), `dedup.py:140` (`_validity` returns a flat `1` for anything
  that is not email/website/lat/lon/rating), `dedup.py:184` (`str(value)` lexicographic max).
- **Breaks:** measured discrimination per column:

  | discriminates | columns |
  |---|---|
  | yes | `name`, `category`, `address`, `lat`, `lon`, `rating`, `review_count`, `email`, `website`, `facebook`, `instagram`, `whatsapp` |
  | **no (all trust = 1)** | `region`, `country`, `website_live`, `linkedin`, `completeness_score`, `lead_score`, `industry_priority`, `recommended_service`, `source`, `scraped_at` |
  | **no (all trust equal, non-1)** | `phone` — 2 for all three sources, so `_validity` ties at 1 and the winner is `max()` of the raw strings |

  Verified ties:

  ```
  phone    '05 123 4567'         vs '+961 5 123 4567'          -> '05 123 4567'
  phone    '96170123456'         vs '+96170123456'             -> '96170123456'
  linkedin '.../company/zzz-old' vs '.../company/aaa-real'     -> '.../company/zzz-old'
  ```

  `phone` is the column `dedup` keys on (`dedup.py:205`) *and* the one `has_any_contact` and
  `lead_score` read (`main.py:137-145`, `enricher.py:292`). Whether a national-format or an
  E.164 number reaches the sales team is decided by `0x2B '+' < 0x30 '0'`. Note `normalize_phone` is
  only applied *after* dedup (`main.py:192-194`), so this tie-break runs on raw, unnormalised input.
  The same path applies to any column a human adds to the master CSV: `_pick` keeps the
  lexicographic max, then `write_csv`'s `extrasaction="ignore"` (`main.py:125`) drops the column
  entirely — verified `_pick("notes", "bbb", "aaa") -> "bbb"`, and the column never reaches the file.
- **Trigger:** two records on the same phone number, one with `"96170123456"` and one with
  `"+96170123456"`; or any hand-added CSV column.
- **Fix:** give `phone` an explicit rule (prefer the value `normalize_phone` maps to a non-empty
  E.164; prefer the longer/national-format one when both normalise equal), and change the final
  tie-break for fields with no trust entry from `str(value)` to "first non-missing", so unknown
  columns stop being silently rewritten.

### S2 — `_DEFAULT_TRUST = 1` is a floor, so "unknown provenance" is indistinguishable from "weak provenance"

- **Where:** `dedup.py:111`, `dedup.py:149-152`.
- **Breaks:** `best = _DEFAULT_TRUST` means no record can ever score below 1, and there is no
  sentinel for "we do not know where this came from". Verified:
  `_trust_for("category", {"source": "wikidata"}) == 1` and
  `_trust_for("category", {"source": "sentry.io-ingest-payload"}) == 1`. A record whose provenance
  is garbage, blank, a list, or an int is scored exactly like the lowest entry in the table, so
  `dedup` has no way to quarantine it and no way to count it.
- **Trigger:** `{"source": ["osm"]}` or `{"source": 5}` — both return 1 with no error (verified).
- **Fix:** reserve `0` for "provenance missing or unrecognised", have `dedup` count zero-trust
  records and print the count with the source histogram, and exclude them from automatic merging
  (or emit them to a `needs_review.csv`).

### S2 — `run` exits 0 after total data loss; a mid-run source outage is one stderr line

- **Where:** `main.py:245-246` (`main()` called without `SystemExit`), `cli.py:33-37`
  (`return int(pipeline.main() or 0)` → `0`), `main.py:167-168` (scraper exception swallowed),
  `osm.py:47-56` / `wikidata.py:79-85` / `google_places.py:169-171` (each source returns an empty
  batch on failure after a single print).
- **Breaks:** this is the "called with an empty list" case. `dedup([])` returns `[]`
  (`dedup.py:197,232`), `_trust_for` executes zero times — the entire trust table goes unexercised,
  which is precisely why the failure modes above have never been caught — and `main.py:209` writes
  a header-only `all_businesses.csv` over the cumulative master. The write is atomic
  (`main.py:121-133`), so the *previous* master is not corrupted; it is simply replaced by nothing.
  Only `scrape.yml:74-83` catches it. `leadminer run` on any other scheduler exits 0 having
  destroyed history. Likewise, if OSM or Wikidata dies mid-run, the run continues and publishes a
  complete-looking export whose merges are simply missing the trust-3 contributor — and the CSV
  records nothing about which sources were empty.
- **Trigger:** unset `GOOGLE_PLACES_API_KEY` (`google_places.py:169-171`) on a first run with an
  existing master, or `rclone copy` succeeding while a source 401s.
- **Fix:** `main()` returns an int; exit non-zero when `len(records) == 0`, when any source
  contributed 0, or when `len(records)` drops more than X% versus the loaded master. Print the
  per-source contribution count as a first-class run statistic next to the existing summary
  (`main.py:219-242`), because that is the only number that reveals a silent outage.

### S3 — `_sources_of` does not strip, so one stray space voids part of the `source` cell

- **Where:** `dedup.py:143-145` — `{s for s in raw.split("|") if s}`.
- **Breaks:** verified: `"osm || google_places"` → `{"osm", " google_places"}` →
  `_trust_for("email", …) == 1`. The OSM entry that would have scored 3 is discarded because a
  space precedes the second name, while the first name still parses — so the record keeps *partial*
  trust and the collapse is invisible even to someone reading the cell. Same for `"osm "`.
- **Trigger:** any hand-edited CSV, or a future scraper name containing a space.
- **Fix:** `{s.strip() for s in raw.split("|") if s.strip()}`.

### S3 — `dedup` of records with no keys yields one phantom row that `_trust_for` never gets to veto

- **Where:** `dedup.py:201-218`; the guard is `_is_missing` at `dedup.py:168-171`, which short-circuits
  before `rank`/`_trust_for` is ever reached.
- **Breaks:** verified `dedup([{}, {}, {}]) == [{}]` — three empty records collapse into the shared
  key `("", "")` and one row of empty strings is written to all five CSVs. Not `_trust_for`'s
  fault, but it is the reason the trust machinery has no opportunity to object to a valueless row.
- **Fix:** drop records with no `name` before indexing, and count them.

---

## Not a bug, but worth knowing

- **Performance is a non-issue; do not spend time here.** Measured on this machine, no network:
  `dedup(20k, 5% key collisions) = 0.14s`, `dedup(100k, 5%) = 1.32s`,
  `dedup(200k, 100% collisions) = 26.85s`. `_trust_for` costs ~2.0µs and is called exactly **42
  times** per `_merge` of two full-width records (21 non-special fields × 2 sides; `source` and
  `scraped_at` return early at `dedup.py:173-177`), i.e. ~84µs of the 134µs merge. The six hours
  are Google Places pagination and the 40-thread enrichment pass, not the merge.
- **Hash-seed determinism holds.** `_sources_of` returns a `set`, but `max()` over ints is
  order-independent, so `PYTHONHASHSEED` cannot change a merge decision. Verified identical output
  for seeds 0, 1, 42, 12345. Merges are reproducible for a fixed input set.
- **`_trust_for` is not a crash site.** Garbage of any *shape* is absorbed silently —
  `{"source": ["osm"]}` and `{"source": 5}` both return 1. The only exception it can raise is
  `AttributeError` on a non-dict record (`dedup.py:144`), but `dedup` dereferences `.get` at
  `dedup.py:202` first, so that failure surfaces earlier and louder. Do not add defensive code here.
- **`_is_missing` treats `0` as present**, so the scrapers' `lead_score=0` / `completeness_score=0`
  placeholders (`osm.py:118,121`, `wikidata.py:126,130`, `google_places.py:300,304`) compete as if
  they were real observations. Currently masked because `enricher.py:349-350` and `main.py:197`
  recompute both after dedup. The only thing preventing a fleet of zeroed scores today is that
  `"0"` sorts below every two-digit string in the `str(value)` tie-break — do not rely on that if
  scoring is ever moved before dedup.
- **Provenance is destroyed by a blank `source` and cannot be recovered.**
  `_pick("source", {"source": "osm"}, {"source": ""})` returns `"osm"` (verified), and a
  write_csv → load_master round trip turns `source=""` into `None` (`main.py:87-89`), after which
  `_trust_for` returns 1 for every field of that row forever (verified).
- **`main.py:216-217` counts `website_live is None` as "unreachable"** for the run summary, but
  `_pick` cannot carry tri-state values — a `website_live=False` on the master versus a fresh
  `None` is resolved by trust (both 1) then recency, so tri-state is decided by the same
  string-ordering path that decides `phone`. Worth keeping in mind when reading the summary; it is
  a separate finding from `_trust_for`.

---

## Direct answers to the operational questions

**What does it print?** Nothing. `dedup.py` contains no `print`, no `logging` import, no logger and
no `warnings` (verified by scanning the file). It is a pure function called 42× per `_merge`, and
it emits nothing on any input, correct or not. Nothing in the run summary (`main.py:219-242`) or in
`cli.py` reports how many records were scored by trust 1 because their provenance was
unrecognised — that statistic does not exist.

**What does it swallow?** Every provenance defect: unknown source name, wrong case, trailing or
leading whitespace, empty string, `None`, missing key, list, int. All become `_DEFAULT_TRUST`.

**Network drops halfway?** `_trust_for` performs no I/O and dedup is entirely network-immune, so
nothing fails *at* it. The network reaches it only as changed inputs: `osm.py:47-56` and
`wikidata.py:79-85` return an empty batch after one print, `google_places.py:169-171` returns
immediately when the key is absent, and `main.py:167-168` swallows the scraper exception. So a
mid-run outage removes the high-trust contributors (OSM's website/email at trust 3) without any
signal in the export: fewer trust-3 merges, more rows carrying the master's stale website, and no
indication anywhere in the CSVs that OSM contributed zero. A human reading a GitHub Actions log has
to find one line among several thousand to learn that.

**A dependency returns garbage?** Two distinct cases. Garbage *in `source`* → silently trust 1
(S1 above). Garbage that looks like a *field value* → only caught for `email`, `website`, `lat`,
`lon`, `rating` (`dedup.py:120-140`); every other field has **no** validity check at all, so
`linkedin="n/a"`, `region="Unknown"` or a duplicated `address` string propagates unchallenged
because `_validity` returns a flat `1` (`dedup.py:140`) and `str(value)` then picks the larger.

**Called with an empty list?** `_trust_for` takes a record, not a list. The nearest case is
`dedup([])`, which returns `[]` (`dedup.py:197,232`) with zero `_trust_for` evaluations — the trust
table is never exercised — and `main.py:209` overwrites the cumulative master with a header-only
file. `scrape.yml:74-83` catches this; nothing else does.

**When interrupted?** Safe but expensive. `_trust_for` holds no state and `_merge` builds a new dict
(`dedup.py:191-194`), so a merge interrupted by SIGINT is discarded whole — no partial record, no
corruption, and the `KeyboardInterrupt` propagates out of `main()` to `cli.py:316-318` (exit 130).
There is no checkpoint, however: `main` writes only after enrichment (`main.py:208-213`), so an
interruption at hour 5.5 of 6 loses the whole run and every `_trust_for` decision it made. On
SIGKILL at the 300-minute `timeout-minutes` (`scrape.yml:25`) the run produces nothing at all and
the `if: always()` artifact step (`scrape.yml:91-98`) captures only whatever CSV the run happened to
finish. The function is interruption-safe; the pipeline's six-hour all-or-nothing shape is what costs
the operator the work.

---

## Reproductions

All of the above was verified locally with no network access, using synthetic records only (no
scraper was executed). Read-only imports of `dedup`, plus `main.load_master`/`main.write_csv` with
`requests`/`urllib3` stubbed the same way `tests/test_lead_signal.py:23-63` stubs them:

```
python3 -c "import sys; sys.path.insert(0,'.')
from dedup import _trust_for, _pick, _merge, _sources_of, dedup, _SOURCE_TRUST
print(_trust_for('email', {'source': 'OSM'}))
print(_pick('rating', {'source':'google_places','rating':4.2,'scraped_at':'2026-01-01T03:00:00+00:00'},
                     {'source':'osm','rating':4.8,'scraped_at':'2026-10-10T03:00:00+00:00'}))
print(_merge({'source':'google_places|osm','website':'http://old-site.example'},
             {'source':'google_places','website':'https://new-site.example'}))
print(dedup([{}, {}, {}]))"
```

The two-run idempotence transcript in S1 was produced by writing `dedup(...)` through
`main.write_csv`, reading it back with `main.load_master`, and re-running `dedup` on
`[new_record] + master` — the exact sequence `main.py:150,179-180` performs on the weekly cron.

---

## Recommended order of work

1. **Per-field provenance.** Stop `_trust_for` from reading the source *union*; score each value by
   the sources that actually supplied it. This is the single change that fixes S1-1 and removes the
   root cause of the S1-3 freeze. Requires a `_provenance` sidecar dict that survives the CSV
   round-trip (add it as a column, or move to the SQLite store proposed in
   `docs/audits/033-storage-sqlite-migration.md`).
2. **Validate `source` at construction.** A `frozenset(_SOURCE_TRUST)` membership check in
   `BaseScraper` plus a per-run histogram of unrecognised names printed next to the summary. Turns
   S1-2 from a six-month mystery into a startup assertion.
3. **Reorder `rank` for `_VOLATILE` fields** so recency outranks trust, and add a regression test
   where the two records have *different* sources — the existing test at
   `tests/test_lead_signal.py:303-309` ties on trust and therefore never exercises the branch.
4. **Give `phone` a real rule** and replace the `str(value)` tie-break with "first non-missing" for
   fields that have no trust entry, so `phone`/`linkedin`/hand-added columns stop being decided by
   ASCII ordering.
5. **Make the run observable and failable.** `main()` returns an int; `run` exits non-zero on an
   empty or sharply shrunken export; print per-source contribution counts; add
   `dedup(dedup(x)) == dedup(x)` as an idempotence test.
6. **Hygiene:** `.strip()` in `_sources_of` (`dedup.py:143-145`); drop valueless records before
   indexing (`dedup.py:201-218`); assert that `_SOURCE_TRUST` covers every column in `main.FIELDS`
   or that undocumented columns are intentionally excluded from merging.