# 351 — `_merge` (dedup.py:190) — Production / Ops lens

## Verdict

`_merge` is the only place in the pipeline where lead data is destroyed, and it is
the only stage with **zero instrumentation whatsoever** — `dedup.py:1-2` imports only
`re` and `unicodedata`, so there is no logger, no counter, no threshold, and no
diff against last week's master. It never raises, so the six-hour cron always exits
green; the sole number a human ever sees is the aggregate at `main.py:181`
(`After merge + dedup: N unique businesses`), which is indistinguishable between
"merged 4 branch records correctly" and "collapsed 4 branches into 1 and deleted 3
street addresses". Because the merged record's `source` string is a **monotonically
growing union** (`dedup.py:173-174`), each weekly run re-grants every previously-seen
source's trust to every value on the row (`dedup.py:148-152`), so provenance is
progressively laundered and stale values become unevictable. Ranked by time-to-notice,
the worst failure is not a crash — it is a slow, monotone, never-announced corruption
of the master that a human discovers only when a prospect says "you are calling the
wrong branch".

Empirically (Python 3.12, this repo, no network):

```
dedup(200,000 fresh records):  1.02 s
dedup(500,000 records):        OK, peak RSS 1005 MB
4 branch records sharing one hotline -> 1 row, 3 addresses + 3 coordinate pairs destroyed
```

---

## Answers to the six operational questions

**What does it print?** Nothing. `_merge` (`dedup.py:190-194`) and every helper it
calls (`_pick:159`, `_validity:120`, `_trust_for:148`, `_recency:155`,
`_sources_of:143`) contain no `print`, no `logging` call, and `dedup.py` does not
import `logging` at all. No merge count, no conflict count, no discarded-field count,
no warning when two records disagree. The only post-hoc trace is the single line at
`main.py:181`.

**What does it swallow?** It catches nothing, so nothing is swallowed by an
`except`. What it swallows is *semantics*: every disagreement between two records is
resolved silently by a 4-tuple comparator (`dedup.py:180-185`) whose last element is
`str(value)` — a lexicographic string comparison. Discarded values leave no trace
whatsoever.

**Network drops halfway?** `_merge` itself is unaffected (no I/O). The danger is
upstream: `osm.py:47-56` and `wikidata.py:79-85` `print` a FAILED banner and `return`
from a *generator*, which raises nothing, so `main.py:163-168`'s `except` never fires
and the run continues with a silently partial batch. `dedup` then folds that partial
batch into the full master, preserves all previously-merged values it has no new
evidence for (safe for `rating`/`review_count`, which only Google supplies), and
`main.py:209` rewrites the master. Exit code 0.

**A dependency returns garbage?** It is absorbed one stage earlier (`_validity:
129-139` does catch `ValueError`/`TypeError` on `float()`), but garbage that *parses*
is graded as valid. `float("nan")` and `float("inf")` both satisfy `float(value)` at
`dedup.py:130`, so a NaN coordinate receives the **maximum** validity score of 3
(details in S2-3).

**Called with an empty list?** `_merge({}, {})` returns `{}`; `_merge(a, {})` returns a
copy of `a`; neither mutates the inputs (verified). `dedup([])` returns `[]`, the five
CSVs are written header-only, and `scrape.yml:74`'s `[ ! -s … ]` **passes** (a header
line is non-empty) — it is caught only by `scrape.yml:78-83`, where
`ROWS = 1 - 1 = 0 < 100` → `exit 1`. Correct outcome, for the wrong reason.

**When it is interrupted?** `_merge` is pure: it allocates a fresh dict, mutates
neither `a` nor `b`, and touches no shared state, so a kill at any point loses only
CPU time and the master is untouched (nothing is written before `main.py:209`). The
real interruption exposure is *upstream of the write*: `timeout-minutes: 300`
(`scrape.yml:25`) is 5 hours, and the assignment's budget is 6. A kill during
`check_websites` means zero enrichment is persisted, yet `scrape.yml:91-98`'s
`if: always()` artifact step uploads `data/` — which at that point contains only the
*downloaded* master CSVs (`scrape.yml:56`). The artifact looks like a successful run's
output; the operator must read the job status bar to learn the run never finished.

---

## Findings — ranked by how long a human takes to notice

### S1-1 — Zero instrumentation on a lossy fold: `_merge` destroys rows and reports nothing

- **Where:** `dedup.py:190-194` (no logging), `dedup.py:1-2` (`dedup.py` imports only
  `re`, `unicodedata`), aggregate-only reporting at `main.py:181`
- **Breaks:** `_merge` is a lossy fold over every record sharing a dedup key. Whenever
  two records describe *different physical places* under one key, `_pick` resolves
  each conflicting field independently and one location's `address`, `lat`, `lon`,
  `website` and `phone` are discarded with no record of the fact. Because there is no
  counter and no comparison to the previous master, the discarded branch leaves the
  CSV identically healthy.
- **Trigger:** four branches of one pharmacy chain, each carrying the chain's central
  hotline (exactly what Google returns for `nationalPhoneNumber` on multi-location
  brands):

  ```python
  br = [
    {'name': f'Nahdi #{i}', 'address': f'{i} Road',
     'phone': '+961 1 999 000',              # same hotline, as Google returns it
     'lat': 33.8 + i*0.1, 'lon': 35.4,
     'category': 'pharmacy', 'source': 'google_places',
     'scraped_at': '2026-10-03T00:00:00Z'}
    for i in range(4)
  ]
  dedup(br)
  ```
  Measured: **4 in → 1 out.** Survivor is `Nahdi #3` / `3 Road` / `34.099999999999994`.
  Addresses `'0 Road'`, `'1 Road'`, `'2 Road'` and three coordinate pairs are gone.
  `main.py:181` prints `After merge + dedup: N unique businesses` and the run is green.
- **Time to notice:** the worst in this report. An SDR calling the surviving branch
  for a lead that belongs to a different branch is the only detection path — weeks to
  months. Note `dedup.py:205` keys on `normalize_phone`, and `normalize_phone:60`
  returns the number verbatim once it starts with a known code, so a chain hotline is
  a *perfect* collapse key by construction.
- **Fix:** make the fold countable and fail-closed — return `(merged, stats)` from
  `dedup`, count `fields_discarded` and `records_with_conflicting_latlon` in
  `_merge`, `logger.warning` any key whose merged records disagree on `lat`/`lon` by
  >1 km, and add a `main.py` invariant that post-merge row count is within a
  configurable band (e.g. 85–100 %) of last week's.

### S1-2 — Provenance laundering: the union `source` string re-grants trust to values the source never supplied

- **Where:** `dedup.py:148-152` (`_trust_for` takes `max` over `_sources_of(record)`),
  `dedup.py:143-145` (`_sources_of` splits on `|`), `dedup.py:173-174` (`_pick` for
  `source` unions the sets, sorted), combined with `main.py:179`
  (`combined = raw_filtered + master`) making the master a permanent merge input
- **Breaks:** `_trust_for(field, record)` scores the *record*, never the *value's
  provenance*. Once a record has ever been merged with an OSM record, its `source`
  permanently contains `"osm"`, and every field on that row thereafter inherits
  `_SOURCE_TRUST["osm"]` — including fields whose surviving value was observed by a
  different source entirely. Because the master is re-merged every weekly run
  (`main.py:179`) and `source` only ever grows, this is a one-way ratchet: after N
  weeks a row has absorbed the maximum trust of every source it has ever seen, and
  the merge becomes insensitive to which source is actually right.
- **Trigger** (three consecutive weekly runs, measured):

  ```python
  wk1 = {'name':'Acme','phone':'+9611234567','website':'https://osmsite.com',
         'email':'info@acme.com','source':'osm',
         'scraped_at':'2026-09-01T00:00:00Z'}
  wk2 = {'name':'Acme','phone':'+9611234567','website':'https://googlesite.com',
         'rating':4.4,'review_count':88,'source':'google_places',
         'scraped_at':'2026-10-03T00:00:00Z'}
  m  = _merge(wk1, wk2)
  # m['source']  == 'google_places|osm'   (osm's union won — sorted)
  # m['website'] == 'https://osmsite.com'
  # _trust_for('website', m) == 3  <- inherited from osm, which never saw this value

  wk3 = {'name':'Acme','phone':'+9611234567','website':'https://newest.example',
         'source':'wikidata','scraped_at':'2026-10-10T00:00:00Z'}
  _merge(m, wk3)['website'] == 'https://osmsite.com'   # 6-week-old OSM value survives
  ```
- **Time to notice:** weeks. Requires reading the master and noticing a website is
  months stale, or noticing the `source` column has drifted to three pipes on most
  rows. Nobody looks at either.
- **Fix:** track provenance per field, not per record — store
  `field -> {source: value, observed_at}` (a per-field evidence map) and have
  `_trust_for` score *that field's* observed source; stop letting `_pick`'s
  `"source"` special case grant trust to unrelated keys.

### S1-3 — `website` is in `_VOLATILE`, but trust outranks recency, so a stale domain wins and then inverts the lead signal

- **Where:** `dedup.py:114` (`_VOLATILE = {…, "website"}`, commented
  *"Fields whose value changes over time, so the fresher observation wins"*) versus
  `dedup.py:180-185`, where `_VOLATILE` is tuple element **2** and `_trust_for` is
  element **0**
- **Breaks:** the tuple comparison is lexicographic and trust-first, so recency is only
  consulted when trust is *already equal*. `_SOURCE_TRUST` gives OSM `website: 3` and
  Google `website: 2` (`dedup.py:100`, `dedup.py:105`) — unequal — so `website` is the
  one field in `_VOLATILE` where the volatile branch is unreachable in the common case.
  The documented intent is not implemented. A months-old OSM-registered domain
  permanently outranks Google's freshly-observed one.
- **Trigger** (measured):

  ```python
  old = {'source':'osm','website':'https://old-osm.example.com',
         'scraped_at':'2026-01-01T00:00:00Z'}       # rank = (3, 3, '2026-01-01…')
  new = {'source':'google_places','website':'https://new-google.example.com',
         'scraped_at':'2026-10-03T00:00:00Z'}       # rank = (2, 3, '2026-10-03…')
  _merge(old, new)['website']  ->  'https://old-osm.example.com'
  ```
  The 9-month-old value wins.
- **Consequence (this is the severe part):** `enricher.py:231` fetches
  `r["website"]` — the *stale* one. If that domain is parked, expired or squatted,
  `_fetch_website` returns `DEAD` (`enricher.py:182-184`), `website_live` is set
  `False` (`enricher.py:250`), and `lead_score:303-304` awards **+20 for a
  server-confirmed dead site** and `recommend_service` pitches a rebuild — to a
  business that has a perfectly good live website which the pipeline itself observed
  and then discarded. `with_websites.csv`/`without_websites.csv` and `sales_ready.csv`
  are all built on that wrong value.
- **Time to notice:** weeks, and it is *invisible in aggregate* — counts look normal.
  A single bad lead is caught only if an SDR opens the domain and finds it parked.
- **Fix:** make recency dominate for `_VOLATILE` fields — reorder to
  `(recency, trust, validity, str(value))` when `field in _VOLATILE`, or drop
  `"website"` from `_VOLATILE` and fix the comment so it stops claiming an ordering the
  code does not implement.

### S1-4 — The only anti-catastrophe gate counts physical lines, and `_merge` preserves the newlines that inflate it

- **Where:** `.github/workflows/scrape.yml:78-83`
  (`ROWS=$(($(wc -l < data/all_businesses.csv) - 1))`, `if [ "$ROWS" -lt 100 ]`),
  `main.py:108-134` (`write_csv`, no newline normalisation), `dedup.py:190-194`
  (`_merge` copies every value verbatim, including embedded `\n`)
- **Breaks:** the gate is an *absolute floor*, not a ratio against the master it just
  downloaded (`scrape.yml:56`), so it cannot detect a 50,000 → 3,000-row collapse.
  Worse, `wc -l` counts newlines, and nothing between `_merge` and `write_csv` escapes
  or strips them. Google returns `formattedAddress` (`google_places.py:286`) with
  newline separators for multi-line addresses, and `_merge` is what carries them into
  the master — so the row counter over-reports by exactly the number of embedded
  newlines.
- **Trigger** (measured, using only values `_merge` round-trips unchanged):

  ```
  12 real rows, each address 18 lines long  ->  wc -l = 217  ->  CI ROWS = 216
  12 real rows                                                          -> PASSES the
  5 real rows  (91 lines) -> CI ROWS = 90                               -> fails
  ```
  A run that genuinely produced **12 businesses** is published to Google Drive as a
  216-row healthy run, because the only guard is bypassed by its own output.
- **Time to notice:** days — a sales team opening a 216-line "leads" export that
  contains 12 businesses. But the failure is *self-concealing by design*: the alert
  that would have caught it is the alert this bug disarms.
- **Fix:** validate in Python where the real record count lives — have `main.py`
  print and assert `len(records)` against `len(combined)` and against the prior
  master's row count, and have the workflow read that number from the log
  (`${{ steps.run.outputs.rows }}`) rather than `wc -l`.

### S1-5 — `_merge` is never invoked for the OSM ∩ Google overlap, so the same business ships to SDRs twice

- **Where:** `dedup.py:72-76` (`_extract_city` returns `parts[-1]`),
  `scrapers/osm.py:80-87` (address is built
  `housenumber, street, suburb, city, district`), `scrapers/google_places.py:286`
  (single `formattedAddress` string), `dedup.py:211-218` (the only path to `_merge`
  for phone-less records)
- **Breaks:** `_extract_city` takes the **last** comma-separated component. For an
  OSM node that is `addr:district`; for a Google place that is the country. The two
  keys can therefore never be equal, so the phone-less overlap never reaches
  `_merge`, and `_merge` — the one component with any chance of reconciling them —
  is never given the opportunity.
- **Trigger** (measured):

  ```python
  osm = {'name':'Nahdi Pharmacy','phone':None,'source':'osm',
         'address':'12, Rue Verdun, Verdun, Beirut, Achrafieh'}
  goog= {'name':'Nahdi Pharmacy','phone':None,'source':'google_places',
         'address':'12 Rue Verdun, Achrafieh, Beirut, Lebanon'}
  # _extract_city(osm['address'])  == 'achrafieh'
  # _extract_city(goog['address']) == 'lebanon'
  dedup([osm, goog])  ->  2 rows   # one business, two rows, each half-populated
  ```
  Row 1 carries OSM's `address`/`lat`/`lon` but no rating; row 2 carries Google's
  rating and review count but OSM's coordinate precision is gone. Both land in
  `sales_ready.csv`.
- **Time to notice:** hours-to-days, and it is the *most* visible symptom in this
  report because duplicates are countable by a human with a sort — but nothing in
  the pipeline or the workflow counts them, so nobody looks.
- **Fix:** one line at `dedup.py:76` — match on any known locality token rather than
  `parts[-1]` (or normalise both sides through a shared city gazetteer), and add a
  dedup-stage assertion that reports `records_in - records_out` so the near-miss count
  is always visible.

### S2-1 — The final tiebreak is `str(value)`, so string order silently picks which contact reaches the SDR

- **Where:** `dedup.py:184` (`str(value),  # deterministic final tiebreak`) inside the
  `rank` closure at `dedup.py:179-185`
- **Breaks:** for any field where the two records have equal trust and equal validity,
  the surviving value is the **lexicographically larger string**. `phone` is exactly
  that case — `_SOURCE_TRUST` gives `google_places`, `osm` and `wikidata` all
  `phone: 2` (`dedup.py:100,105,107`) and `_validity` returns a flat `1` for `phone`
  (`dedup.py:140`). No recency term, because `phone` is not in `_VOLATILE`
  (`dedup.py:114`).
- **Trigger** (measured): a business whose email was found by crawling its own website
  (`enricher.py:252-253`) in the master, meeting this week's OSM record with a
  different, staler tag:

  ```python
  master = {'source':'google_places','email':'info@acme.com',
            'scraped_at':'2026-10-01T00:00:00Z'}   # crawled from the live site
  osm    = {'source':'osm','email':'zz-legacy@old-host.net',
            'scraped_at':'2026-10-03T00:00:00Z'}
  _merge(master, osm)['email'] -> 'zz-legacy@old-host.net'   # loses

  osm['email'] = 'aa-legacy@old-host.net'
  _merge(master, osm)['email'] -> 'aa-legacy@old-host.net'   # wins
  ```
  Two runs apart, the same business is emailed at two different addresses depending on
  alphabetical order of an unrelated mailbox. Both are "correct" per the comparator.
- **Time to notice:** a bounce or a "wrong mailbox" reply. Days-to-weeks, per lead.
- **Fix:** drop `str(value)` from the rank and replace it with an explicit
  last-resort rule (`_recency(rec)` for every field, then a deterministic
  `id()`-free tiebreak only for exact duplicates) so that recency — not alphabet —
  decides; and give `phone` a `_validity` branch that checks the national-number
  length for the record's country.

### S2-2 — `google_places` has no trust entries for `email`/social, so enricher-crawled contacts are outranked by raw OSM tags

- **Where:** `dedup.py:98-101` (google trust table lists only `rating`, `review_count`,
  `lat`, `lon`, `name`, `address`, `category`, `website`, `phone`),
  `dedup.py:102-105` (OSM gets `3` for `email`, `facebook`, `instagram`, `whatsapp`,
  `linkedin`), `dedup.py:111` (`_DEFAULT_TRUST = 1`), `enricher.py:251-259` (the
  crawler *finds* these contacts on the business's own website)
- **Breaks:** contacts harvested from the business's own live website — the highest
  provenance available — are written into the master row carrying that row's `source`
  string (`main.py:209`). On the next run, `_trust_for('email', master_row)` returns
  `1` for a Google-only row, while a fresh OSM tag email scores `3`. Verified:

  ```
  _trust_for('email', {'source':'google_places'}) == 1
  _trust_for('email', {'source':'osm'})         == 3
  ```

  So the crawled email deterministically loses to any OSM tag, and when both are OSM
  or the row has accumulated both sources, the contest is decided by S2-1's
  `str()`.
- **Time to notice:** weeks; surfaces as "the email bounced" or as a lead that was
  never actually contacted.
- **Fix:** add a pseudo-source `"website_crawl"` to `_SOURCE_TRUST` with `3` for
  `email`/`facebook`/`instagram`/`whatsapp`/`linkedin`, and have `enricher.py` stamp it
  into `record["source"]` on the fields it fills.

### S2-3 — NaN and inf coordinates are graded maximally valid and can outrank real coordinates

- **Where:** `dedup.py:128-133` (`lat`/`lon` validity is `try: float(value) except
  …: return 0; return 3`), `main.py:90-95` (`load_master` casts via
  `float(row[field])` with only `ValueError`/`TypeError` caught)
- **Breaks:** the `lat`/`lon` branch checks *parseability*, not *finiteness*.
  `float("nan")` and `float("inf")` both parse, so they receive validity `3` — the
  maximum. `main.py:93` will happily produce `float('nan')` from a master cell
  containing `nan`/`inf`/`-inf`, and `csv` writes `float('nan')` back out as `nan`,
  so the corruption is self-sustaining across runs. When trust ties (Wikidata vs OSM
  both score `lat: 1`, `_trust_for` verified), the comparison falls through to
  `str(value)`: `'33.8934' < 'nan'`, so NaN wins.
- **Trigger** (measured):

  ```python
  real   = {'source':'wikidata','lat':33.8934,'lon':35.4902,'scraped_at':'2026-10-03T00:00:00Z'}
  nanrow = {'source':'wikidata','lat':float('nan'),'lon':float('inf'),
            'scraped_at':'2026-09-01T00:00:00Z'}
  m = _merge(real, nanrow)
  # m['lat'] -> nan   (NaN won)   m['lon'] -> inf
  ```
  It only fails to win when the *other* record has higher trust (a Google row at
  `lat: 3` beats it), which is why this is intermittent and therefore hard to catch.
- **Time to notice:** days, and only if someone looks at the map view — it renders as
  a pin at `(0, 0)` or a blank pin. It also silently poisons `enricher.py:332-335`'s
  region inference (`lat_min <= nan <= lat_max` is always `False`), so affected rows
  lose their `region` and drop out of every regional sales view.
- **Fix:** at `dedup.py:128-133` use `math.isfinite(float(value))` (and reject
  `lat == 0.0 and lon == 0.0` as Null Island); at `main.py:90-95` reject non-finite
  casts to `None` before they reach `_merge`.

### S2-4 — A changed phone forks a business into two permanent rows that `_merge` can never reconcile

- **Where:** `dedup.py:205-211` (the record is filed under
  `normalize_phone(phone, country)`; a *different* number is a different key), so
  `_merge` at `dedup.py:190` is never called between the old and new rows
- **Breaks:** `_pick` can happily update the `phone` field *within* a merged record
  (`dedup.py:193`), but it can only do so for records that already share a key. The
  moment a source publishes a new number, the business forks. The old row keeps its
  stale number forever because nothing ever merges into it again.
- **Trigger** (measured): `{'name':'Nahdi','phone':'+961 1 999 000'}` in the master vs
  `{'name':'Nahdi','phone':'+961 9 111 222'}` from this week's scrape →
  `dedup` returns **2 rows**, both named `Nahdi`, both at `'A Rd'`. `sales_ready.csv`
  ships both. `_merge` is never invoked, so it cannot warn.
- **Time to notice:** days — an SDR dials a disconnected number. Fixing it requires a
  name/geo-similarity pass that does not exist.
- **Fix:** before keying on phone, match on `(normalize_name, city)` and, when a
  name+city match carries two distinct phones, emit a `phone_conflict` record for
  review instead of shipping both rows; treat the phone as a *weak* key (see
  `docs/audits/057-chain-detection.md`) rather than a sufficient one.

### S2-5 — The entire master is re-materialised in memory every run, and memory grows without bound

- **Where:** `dedup.py:190-194` (a fresh dict per merge, old dict immediately
  garbage), `main.py:179` (`combined = raw_filtered + master`), `main.py:150`
  (`load_master` materialises the whole master as a list of dicts)
- **Measured:** `dedup()` over 500,000 23-field records peaks at **1005 MB RSS**.
  Linear in master size, and the master grows every week.
- **Why it matters now:** it is fine on the current `ubuntu-latest` runner, but it is
  an unbounded, monotonically increasing term with no ceiling check and no warning. At
  ~2 M rows it approaches 4 GB and the run starts swapping or OOMing — and the OOM
  would surface as a `timeout-minutes: 300` kill attributed to `check_websites`, not
  to dedup.
- **Fix:** stream the master from SQLite rather than `load_master`-ing a list
  (see `docs/audits/033-storage-sqlite-migration.md`), or at minimum log RSS at the
  dedup boundary so the growth is observable before it bites.

### S3-1 — `_merge` is not commutative when both values are missing; output type depends on argument order

- **Where:** `dedup.py:167-171` — the `_is_missing` guards run **before** the
  `source`/`scraped_at` special cases, so when *both* sides are missing the function
  returns `bv`
- **Breaks** (measured):

  ```python
  _merge({'email': None}, {'email': ''})['email']  ->  ''      # str
  _merge({'email': ''}, {'email': None})['email']  ->  None    # NoneType
  ```

  Same data, two orderings, two different Python types for one field. The contract in
  `scrapers/base.py:5-28` types `email` as `str | None`.
- **Time to notice:** never in practice — `main.py:87-89` turns `""` back into `None`
  on the next `load_master`, so it self-heals. Worth fixing for type stability, not
  urgency.
- **Fix:** normalise both sides before the early returns, e.g.
  `return None if _is_missing(bv) else bv`.

### S3-2 — `set(a) | set(b)` propagates unknown columns into the master, with no schema pinning

- **Where:** `dedup.py:192` — `_merge` is key-agnostic; any column present in either
  dict is carried into the result and into `phone_index`/`name_index`
- **Breaks:** a stray column in `all_businesses.csv` (hand-edited on Drive, or left by
  a future migration) is merged forward by `_merge`, merged *against* for trust
  purposes via `_trust_for`, and then silently dropped by
  `main.py:125`'s `extrasaction="ignore"` at write time — so the master *appears* to
  carry data that was never persisted. `dedup.py:192` should intersect with a declared
  field list.
- **Fix:** `FIELDS` (or `BusinessRecord.__annotations__`) should be the authoritative
  key set: `for key in (set(a) | set(b)) & ALLOWED_FIELDS:`, plus a
  `logger.warning` naming any dropped key.

### S3-3 — No guard or documentation for the degenerate call

- **Where:** `dedup.py:190-194`
- **Breaks:** `_merge({}, {})` → `{}` and `_merge(a, {})` → `dict(a)`, both silent and
  both untested. There is no assertion that `a` and `b` are dicts, so a non-dict
  raises a bare `AttributeError` from `a.get` with no context about which key was
  being merged.
- **Fix:** `if not isinstance(a, dict) or not isinstance(b, dict): raise TypeError(...)`
  naming the caller, and add the two degenerate cases to the test suite.

---

## Not a bug, but worth knowing

- **`_merge` is genuinely pure.** No I/O, no locks, no shared mutable state, and it
  mutates neither argument (verified: `_merge({'x':1},{'x':2})` leaves both inputs
  unchanged and returns `{'x': 2}`). This is the right design and it means the function
  is completely immune to the two failure modes a 6-hour networked run normally fears —
  a mid-flight network drop and a double-scheduling race (`scrape.yml:15-17`, which
  correctly blocks concurrent runs). The corruption here is *logical*, not concurrent.
- **CPU cost is a non-issue.** 1.02 s for 200,000 records, 0.92 s for 200,000
  master-shaped records. The merge stage is well under 0.1 % of a 300-minute budget.
  Do not spend optimisation effort here.
- **The chain-hotline collapse and the `_extract_city` mismatch are one bug, not two.**
  `normalize_phone:60` treats any number with a known country prefix as fully
  qualified, which makes a chain's central hotline an ideal collapse key; and
  `_extract_city:76` makes the phone-less fallback key incomparable between OSM and
  Google. Fixing either alone leaves the other half of the duplicate/collapse
  problem open. See `docs/audits/057-chain-detection.md` and
  `docs/audits/095-dedup-strategy-v2.md`.
- **`_merge` has zero test coverage.** `docs/audits/031-test-suite-design.md` and
  `docs/audits/087-SYNTH-test-gap.md` both flag merge tests, but their proposed cases
  still target the old `_field_count` implementation (`dedup.py` was rewritten in
  commit `99493b9`). Every finding above is a two-line deterministic unit test — the
  function is pure, so there is no excuse for its absence.
- **A partial run does not corrupt Google-trusted fields.** Because
  `_SOURCE_TRUST["google_places"]` is `3` for `rating`/`review_count` and no other
  source supplies them at all, a run where Google returns zero pages still preserves
  the last known rating via `_pick`'s missing-value path (`dedup.py:168-171`). The
  staleness is invisible but bounded — worth stating because the natural fear
  ("partial run zeroes everything") is unfounded.

---

## Recommended order of work

1. **Add instrumentation to `_merge`/`dedup` before changing any logic** (S1-1,
   S2-5). A single `stats` dict — records in, records out, fields discarded,
   conflicts on `lat`/`lon`, conflicts on `phone` — plus a `main.py` ratio assertion
   against the downloaded master. Every other finding here is invisible without it,
   and it is roughly 20 lines. Do this first so the fixes below can be *verified*
   rather than argued about.
2. **Fix the row-count gate** (S1-4). Count records in Python; stop letting `wc -l`
   measure the output. This is the only thing standing between a broken run and Drive
   and it is currently defeatable by the data itself.
3. **Reorder `rank` so recency dominates for `_VOLATILE` fields** (S1-3), and add
   `math.isfinite` to lat/lon validity (S2-3). Both are one-line changes to
   `dedup.py:180-185` and `dedup.py:128-133` with immediate, measurable effect on
   `website_live` and `region` accuracy.
4. **Replace the cumulative `source` union with per-field provenance** (S1-2,
   S2-2). This is the structural fix and the largest one: add a
   `website_crawl` pseudo-source, and stop letting `_trust_for` score a record rather
   than the observation that produced the value. Until this lands, every other merge
   fix is partially self-defeating.
5. **Remove `str(value)` from the rank** (S2-1) and give `phone` a real validity
   check. A string comparison should never decide which phone number a salesperson
   dials.
6. **Treat the phone as a weak identity key** (S1-5, S2-4): fix `_extract_city:76`,
   and detect phone conflicts on the same `(name, city)` before filing two rows.
7. **Land the test file for `_merge`** — pure function, ~10 cases covering each finding
   above. Cheapest risk reduction available in the whole dedup module.