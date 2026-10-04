# 344 — `_merge` (dedup.py:190) correctness

## Verdict

`_merge` is **not** a function of its input *set* — it is a function of its input
*sequence*. `_pick` unions `source` (`dedup.py:173-174`) and then `_trust_for`
(`dedup.py:148-152`) reads that union back to grade a field value, so a value
inherits the trust of a source that never supplied it. Because the union is
accumulated left-to-right, a value's effective trust depends on what it has
already been merged with, and the fold stops being associative at three records.
`dedup()` feeds `_merge` in `as_completed` order (`main.py:160-166`) over
`raw_filtered + master` (`main.py:179-180`), so the same business can come out
of the same run with a different email on different days. Compounding this,
`country` has no trust entry and no validity rule, so it is decided purely by
`str(value)` — which for the only two codes this codebase emits always selects
`"SA"`. That wrong country then survives `resolve_country` (`main.py:59-67`)
and **rewrites the phone's country code** at `main.py:194`, drifting the row
out of the dedup bucket it was filed under.

Every finding below was executed against the real module, not reasoned about.

## Method

`requests`/`urllib3` are not installed in this environment and the brief forbids
installing them, so I injected import stubs for those two packages only and then
executed the real `dedup.py`, `enricher.py` and `main.py`. No network calls. The
probes live outside the repo; no repo file was modified.

The existing suite passes (`python3 -m unittest discover -s tests` → 28 tests,
OK), so everything below is an uncovered case, not a known-failing one.

---

## Findings

### S1 — `_merge` is order-dependent for 3+ records: `source`-union trust laundering

- **Where:** `dedup.py:190-194` (`_merge`), `dedup.py:173-174` (`source` union),
  `dedup.py:148-152` (`_trust_for` takes `max` over the union),
  `dedup.py:179-187` (`rank`).
- **Breaks:** `_pick` unions the two records' `source` strings
  (`dedup.py:174`). `_trust_for` then reads the *merged* record's union and
  returns the maximum trust over all sources in it (`dedup.py:151`). So a field
  value acquires the credibility of any source merged into the record, whether
  or not that source supplied the value. Since the union grows left-to-right,
  the fold is not associative and the output depends on record order.
- **Exact input** (three records for one business, same set, six permutations):

  ```python
  X = {"name":"X","phone":"+96170123456","email":"a@x.example","source":"google_places"}
  Y = {"name":"X","phone":"+96170123456","                            "source":"osm"}
  Z = {"name":"X","phone":"+96170123456","email":"z@z.example","source":"wikidata"}
  ```

  `Y` contributes nothing to `email`. Measured fold results:

  ```
  ('google_places','osm','wikidata')          -> a@x.example
  ('google_places','wikidata','osm')          -> z@z.example
  ('osm','google_places','wikidata')          -> a@x.example
  ('osm','wikidata','google_places')          -> z@z.example
  ('wikidata','google_places','osm')          -> z@z.example
  ('wikidata','osm','google_places')          -> z@z.example
  distinct outcomes: 2
  ```

  Why: Google's email trust alone is `_DEFAULT_TRUST` = 1 (no `email` key in
  the `google_places` block, `dedup.py:98-101`). Wikidata's is 2
  (`dedup.py:107`). OSM's is 3 (`dedup.py:103`). Fold `X,Y` first and the union
  is `google_places|osm`, so `_trust_for("email", merged)` returns 3 and X's
  email beats Z's 2. Fold `X,Z` first and the union is `google_places|wikidata`,
  trust stays 1, and Z's email wins. Measured directly:

  ```
  Google email trust alone : 1
  OSM email trust          : 3     (Y has email=None)
  after union with OSM     : 3     <- laundered
  Wikidata email trust     : 2
  ```

- **Trigger in production:** `dedup(raw_filtered + master)` at `main.py:179-180`.
  `raw_filtered` is built by extending `raw` in `as_completed` order
  (`main.py:160-166`), which is thread-scheduling dependent and therefore
  different on every run. Same three sources, different day, different email.
  The winner is then written to the cumulative master (`main.py:209`), so the
  flapping is permanent.
- **Test gap:** `tests/test_lead_signal.py:292-295`
  (`test_merge_is_order_independent`) only folds **two** records. Two-record
  symmetry and `_merge(m, m) == m` both genuinely hold (verified) — the defect
  appears only at three.
- **Fix:** grade each value by the source that actually supplied it (carry a
  per-field provenance sidecar through the merge) instead of re-reading the
  unioned `source`; or, as a stopgap, sort `records` by a total key before the
  fold at `dedup.py:201` and add a 3-record permutation test.

### S1 — `country` is decided alphabetically, always yielding `"SA"`, and that rewrites the phone's country code

- **Where:** `dedup.py:97-110` (no `country` entry in any `_SOURCE_TRUST`
  block), `dedup.py:120-140` (`_validity` falls through to `return 1` at
  `dedup.py:140` for `country`), `dedup.py:184` (`str(value)` final tiebreak),
  `dedup.py:187`.
- **Breaks:** both candidates score `(1, 1, "", <str>)`, so the winner is the
  lexicographically larger code string. For the only two values this codebase
  ever produces (`osm.py:103`, `wikidata.py:111` hardcode `"LB"`;
  `google_places.py:127-136` emits `"LB"` or `"SA"`), that is **always
  `"SA"`**. This is a systematic bias, not noise. Measured:

  ```
  rank tuples: (1, 1, 'LB')  (1, 1, 'SA')
  _merge(a,b)['country'] = 'SA'
  _merge(b,a)['country'] = 'SA'        # deterministic, and always wrong-way
  trust for 'country': {osm: 1, google_places: 1, wikidata: 1}
  ```

- **How the two codes meet in one bucket:** `normalize_phone` deliberately keys
  on the number's own country code and ignores the `country` field
  (`dedup.py:57-61`, whose rationale is stated at `dedup.py:5-7`: "Data leaks
  across borders constantly... otherwise dedup keys it under the wrong one").
  Google assigns `country` from the **query text**, never from the result
  (`_country_from_query`, `google_places.py:127-136`), while OSM and Wikidata
  hardcode `"LB"`. A Lebanese group with a Riyadh branch listing gets
  `country="SA"` from the KSA query and commonly carries its `+961` HQ number
  in `nationalPhoneNumber`. Both records normalise to the same key and merge —
  which is exactly the cross-border case `_merge` then mishandles.
- **Breaks, concretely, three ways:**

  1. **Region inference is destroyed.** Measured with `address="Hamra, Beirut"`,
     `lat=33.8938`, `lon=35.5018`:

     ```
     merged country    = 'SA'          (phone is +961 = Lebanon)
     resolve_country   -> 'SA'         (main.py:59-67 short-circuits on any
                                        valid code, so the phone fallback at
                                        main.py:68-74 never runs)
     infer_region      -> None          (enricher.py:85 skips the address
                                        because country != 'LB';
                                        enricher.py:90 then applies the KSA
                                        coordinate boxes to Lebanese coords)
     ```

     A Beirut business loses its region and lands in `Unknown` in the
     by-region report (`main.py:228-234`).

  2. **The dedup key drifts.** Because `_pick` treats `country` and `phone` as
     two independent scalars, the merged pair can be internally inconsistent,
     and `main.py:194` then re-normalises the phone under the wrong country:

     ```
     A = {"country":"LB","phone":"1234567",        "source":"osm"}
     B = {"country":"SA","phone":"+9611234567",    "source":"google_places"}

     A key = normalize_phone('1234567', 'LB')   = +9611234567
     B key = normalize_phone('+9611234567', 'SA')= +9611234567   # same bucket

     merged country = 'SA'   merged phone = '1234567'
     main.py:194 rewrites the phone to: +9661234567
     KEY DRIFT: +9611234567 -> +9661234567
     ```

  3. **Next run, two different businesses become one.** The row now carries
     `+9661234567` and is filed in the cumulative master. If a genuine Riyadh
     business holds that number:

     ```
     dedup([lebanon_row_as_written, saudi_shop]) -> 1 row
     merged name = 'Shop'   merged country = 'SA'
     => a Beirut shop absorbed a Riyadh shop's phone number
     ```

     The Beirut business's real contact channel is gone, and `has_any_contact`
     (`main.py:141`) plus the whole `sales_ready` slice now point at the wrong
     company.
- **Fix:** in `_pick`, special-case `country` to return the code implied by the
  surviving `phone` (`normalize_phone(phone, country)`), and add an assertion in
  `_merge` that `normalize_phone(merged["phone"], merged["country"])` equals
  the bucket key the record was filed under.

### S2 — `_is_missing` treats `0`, `0.0` and `False` as present, so numeric sentinels win merges at maximum validity

- **Where:** `dedup.py:86-90` (falls through to `return False` for any
  non-`None`, non-`str`), `dedup.py:128-133` (`lat`/`lon` validity),
  `dedup.py:134-139` (`rating` validity).
- **Breaks:** the missing-value predicate only recognises `None` and blank
  strings. Measured:

  ```
  _is_missing(0)    = False    _validity('lat', 0)    = 3
  _is_missing(0.0)  = False    _validity('lat', 0.0)  = 3
  _is_missing(False)= False    _validity('lat', False)= 3
  _is_missing('')   = True     _validity('lat', '')   = -100
  _is_missing(None) = True     _validity('lat', None) = -100
  ```

  So `0.0` gets the **maximum possible validity score of 3** — the same score a
  real Beirut coordinate earns. `float(0.0)` passes the `lat`/`lon` branch and
  `0 <= 0.0 <= 5` passes the `rating` branch.
- **Trigger A — coordinates.** A `(0.0, 0.0)` row (`null island`, the canonical
  "no coordinates" sentinel) sourced from Google carries trust 3
  (`dedup.py:99`) against OSM's 1 (`dedup.py:104`):

  ```
  trust google lat: 3   trust osm lat: 1
  google (0.0, 0.0) + osm (33.8938, 35.5018)  ->  merged lat/lon = (0.0, 0.0)
  infer_region -> None
  cli.cmd_validate in_box(0.0, 0.0) -> False
  ```

  Note the last line: `_merge` actively **selects** a value that the project's
  own validator (`cli.py:159-173`) classifies as a bad coordinate, and then
  `write_csv` persists it to the master (`main.py:209`).
- **Trigger B — rating.** `load_master` casts `"0"` to `0.0`
  (`main.py:90-96`), so a zero-rating row already in the cumulative master
  re-enters the fold. Being `_VOLATILE` (`dedup.py:114`), it wins on recency:

  ```
  master row  rating=0.0  scraped_at=2026-09-02 (newer)
  fresh  row  rating=4.2  scraped_at=2026-09-01
  merged rating = 0.0        (the real 4.2 is lost)
  _validity('rating', 0.0) = 3   _validity('rating', 4.2) = 3
  lead_score(merged) = 40   <- +10 "pain point" from the bogus 0.0
                              (enricher.py:313)
  ```

  The row is then scored as a high-pitch lead on the strength of a missing-value
  sentinel.
- **Fix:** in `_is_missing`, treat falsy numbers as missing —
  `if isinstance(value, (int, float)) and not isinstance(value, bool): return not value`.

### S2 — `lat` and `lon` are picked independently, so `_merge` can fabricate a coordinate no observation contained

- **Where:** `dedup.py:192-193` (each key resolved independently),
  `dedup.py:179-187`.
- **Breaks:** the fold iterates the key union and resolves each scalar on its
  own trust/validity/recency. Nothing ties the two halves of a coordinate pair
  to a common source, so the merged pair can be one record's latitude and the
  other's longitude.
- **Trigger:**

  ```python
  n = {"name":"Cafe","phone":"+96170123456","lat":33.8938,"lon":None,  "source":"google_places"}
  w = {"name":"Cafe","phone":"+96170123456","lat":None,   "lon":35.5018,"source":"osm"}
  ```

  Measured:

  ```
  google saw (33.8938, None);  osm saw (None, 35.5018)
  merged            -> (33.8938, 35.5018)      # neither source had this pair
  infer_region      -> Beirut                    # a region asserted from a
                                                   # coordinate that never existed
  cli.cmd_validate in_box(33.8938, 35.5018) -> True   # validator accepts it too
  ```

  The fabricated pair is now indistinguishable from a real one, `write_csv`
  writes it to the cumulative master (`main.py:209`), and `load_master`
  (`main.py:90-96`) will preserve it on every subsequent run. `infer_region`
  then confidently names a Beirut district from a synthetic location.
- **Honest reachability:** no current scraper emits a partial pair — `osm.py:68-69`
  and `wikidata.py:54-63` and `google_places.py:272-274` are all
  both-or-neither. So this is a **latent** hole today. It opens the moment a
  partial pair enters the master, which `load_master` is explicitly written to
  tolerate (`main.py:90-96` casts each float field independently, so a row with
  a latitude and a blank longitude survives load untouched). `_merge` can never
  detect or repair such a row once it exists.
- **Fix:** treat `lat`/`lon` (and `name`/`address`) as a co-selected pair —
  resolve the pair from one record by rank, then fall back to the other — rather
  than resolving each scalar independently at `dedup.py:193`.

### S2 — the `source` union fabricates the "multi-source confirmation" bonus

- **Where:** `dedup.py:173-174`; consumed at `enricher.py:316-317`.
- **Breaks:** `lead_score` pays +5 for multi-source confirmation, detected as
  `"|" in record["source"]`. `_merge` inserts the `|` as soon as **any** two
  records fold together — including a fold where the second source supplied
  nothing at all. This is the name-keyed path (`dedup.py:212-218`): two records
  with the same normalised name and no usable phone, one of which is a bare
  name/address shell.
- **Trigger:**

  ```python
  g = {"name":"Cafe X","address":"Main St, Beirut","phone":None,"source":"google_places","rating":4.5}
  o = {"name":"Cafe X","address":"Main St, Beirut","phone":None,"source":"osm"}
  ```

  Measured — the OSM record supplies only `name`, `category`, `address` (all
  values the Google record already had) and contributes no corroboration:

  ```
  fields OSM actually supplied: {'name','category','country','address','source','scraped_at'}
  merged source = google_places|osm
  lead_score(google alone) = 15  |  lead_score(merged) = 20
  ```

  The same 5 points are also awarded by the S1 laundering path — a record whose
  OSM half contains nothing gets both the confirmation bonus and inflated trust
  for `email`, `website`, `facebook`, `instagram` and `whatsapp`
  (`dedup.py:102-104`).
- **Fix:** only add a source to `source` when it actually supplied the winning
  value for some field (the same per-field provenance sidecar as S1), or drop
  the `"|" in source` bonus at `enricher.py:316-317`.

### S3 — `scraped_at` is `max`'d as a raw value: a non-string timestamp kills the run

- **Where:** `dedup.py:176-177` (`return max(av, bv)`); reached from `dedup.py:208`
  and `dedup.py:216`.
- **Trigger:** `_is_missing` does not guard this field, and `max` compares the
  raw objects.

  ```python
  a = {"name":"C","phone":"+9611","scraped_at": datetime.datetime(2026,9,1), "source":"osm"}
  b = {"name":"C","phone":"+9611","scraped_at": "2026-09-01T00:00:00+00:00", "source":"osm"}
  dedup.dedup([a, b])
  ```

  Measured traceback:

  ```
  File "dedup.py", line 193, in _merge      merged[key] = _pick(key, a, b)
  File "dedup.py", line 177, in _pick       return max(av, bv)
  TypeError: '>' not supported between instances of 'str' and 'datetime.datetime'
  ```

  Uncaught, and it propagates out of `dedup()` in `main()` at `main.py:180` —
  the run dies **after** the three scrapers have already burned the API quota
  and hours of the 300-minute Actions budget (`scrape.yml`).
- **Reachability:** all three scrapers emit `utc_now_iso()`
  (`osm.py:32`, `wikidata.py:67`, `google_places.py:173`), so a fresh-only run is
  safe. But `load_master` (`main.py:85-105`) casts `lat`, `lon`, `rating`,
  `review_count` and the three scores — and **never** `scraped_at`. A master CSV
  with a non-ISO timestamp (hand-edited, or written by any tool other than this
  one) reaches `_merge` verbatim on the very next run.
- **Fix:** `return max(str(av), str(bv))` at `dedup.py:177`.

### S3 — `scraped_at` and `_recency` compare timestamps as strings, so legacy naive stamps outrank aware ones

- **Where:** `dedup.py:176-177` and `dedup.py:183` (fed by `_recency`,
  `dedup.py:155-156`).
- **Breaks:** nothing parses the ISO 8601 string; it is compared
  lexicographically. `httpclient.py:220-229` states this exact hazard as the
  reason `utc_now_iso()` was made tz-aware — it fixed the *producers*, but
  `_merge` still does not *parse*.
- **Trigger A (intra-year, naive vs aware):** a naive stamp sorts above any
  aware stamp with the same date prefix, because the `+00:00` suffix starts
  with `+` (0x2B) while the shorter naive string terminates:

  ```
  "2026-09-01T00:00:00" > "2026-01-01T00:00:00+00:00"  ->  True

  Jan(aware) rating 4.9  vs  Sep(naive) rating 1.0  ->  merged rating = 1.0
  ```

  The master CSV is cumulative across runs (`main.py:150`), so any naive stamp
  written before commit `607e729` sits in it permanently and, within its own
  calendar year, outranks **every** subsequent observation for every `_VOLATILE`
  field — `rating`, `review_count`, `website`, `website_live` (`dedup.py:114`).
  (Across calendar years the `YYYY-` prefix dominates, so the damage is bounded
  to the affected year — measured.)
- **Trigger B (mixed offsets):** lexicographic `max` disagrees with chronological
  `max`:

  ```
  X = "2026-09-01T09:00:00+09:00"   # UTC 00:00
  Y = "2026-09-01T01:00:00+00:00"   # UTC 01:00, genuinely 1h LATER
  max(X, Y)             -> X        # string max
  truly later instant    -> Y
  ```

  Not reachable from the current scrapers (all emit `+00:00`), but reachable
  from any master row written by a differently-configured producer.
- **Fix:** parse both operands to aware datetimes before comparing, falling back
  to the raw string if either fails to parse.

---

## Not a bug, but worth knowing

- **Two-record symmetry and self-merge idempotency genuinely hold.**
  `_merge(a,b) == _merge(b,a)` and `_merge(m,m) == m` both verified. That is
  exactly the surface `tests/test_lead_signal.py:279-326` covers, which is why
  the suite is green while S1 is live. The regression needs a three-record
  permutation assertion.
- **Derived fields are stale-tolerant, so `_merge` carrying them forward is not a
  live bug.** `completeness_score` and `lead_score` are recomputed
  unconditionally at `enricher.py:349-350`; `industry_priority` and
  `recommended_service` at `main.py:190-191`. Even though all four of these have
  no `_SOURCE_TRUST` entry (see below) and are picked lexicographically during
  the fold, nothing downstream reads the folded values.
- **`website_live` cross-field pairing does not materialise.** `check_websites`
  recomputes it for every record that has a website (`enricher.py:231`,
  `enricher.py:250`), so a stale verdict cannot survive alongside a new URL.
- **Eight fields have no `_SOURCE_TRUST` entry in any source block**, so for
  them `_pick` is decided by `_validity` (always 1) and then `str(value)` —
  i.e. pure lexicographic:

  ```
  ['region', 'country', 'website_live', 'linkedin', 'industry_priority',
   'recommended_service', 'lead_score', 'completeness_score']
  ```

  `country` is the load-bearing one (S1); the rest are recomputed or unused.
- **`dedup.py:226-230` is dead code.** `name_index` only ever holds records
  whose phone normalises to `""` (`dedup.py:205-211`), so
  `normalize_phone(raw, country) in captured_phones` can never be true —
  `captured_phones` contains only non-empty keys. Verified: a `phone="123"`
  record merges into a phone-less name-keyed record and survives to the output.
  It reads like a safety net that does not exist.
- **`_merge` inherits unknown keys.** `dedup.py:192` folds `set(a) | set(b)`, so
  a typo'd key rides along into the merged dict. Harmless only because
  `write_csv` uses `extrasaction="ignore"` (`main.py:125`).

## Recommended order of work

1. Carry **per-field provenance** through the merge and read trust from it in
   `_trust_for`. This one change fixes S1 (order dependence), S2 (fabricated
   confirmation bonus) and most of the trust-laundering surface at once. Assert
   permutation invariance over 3+ records in `tests/`.
2. Special-case `country` in `_pick` so it always agrees with
   `normalize_phone(merged["phone"], merged["country"])`, and add the
   bucket-key invariant assertion in `_merge`. This fixes both the
   mislabelling and the key drift that fuses two businesses across a border.
3. Tighten `_is_missing` to treat falsy numerics as missing. One line; removes
   the `(0,0)` and `rating=0` sentinels that currently outrank real values at
   maximum validity.
4. Select `lat`/`lon` as a pair rather than two independent scalars.
5. Parse `scraped_at` (and `_recency`) into aware datetimes, with a
   `str()`-coerced `max` at `dedup.py:177` so a malformed master can never take
   the run down.