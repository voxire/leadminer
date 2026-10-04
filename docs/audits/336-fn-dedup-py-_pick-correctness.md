# 336 — `_pick` correctness (`dedup.py:159`)

## Verdict

`_pick` is *correct for a single pairwise merge of two true observations*: it is
order-independent, blanks lose to real values, and `_validity` correctly decides
ties inside a trust tier. It is **not correct as a fold**, which is how it is
actually used. `_merge` is non-associative, because a merged record is fed back
into `_pick` as if it were a fresh observation — it carries a union `source`
and a `max` `scraped_at` that *raise its own ranking* on every subsequent
comparison. The concrete damage: **once a row in `all_businesses.csv` has been
merged with Google even once, its `address`, `lat`, `lon`, `category` and `name`
are frozen forever** and every subsequent scrape for those fields is silently
discarded. Verified over three simulated consecutive runs below.

Callers: `_merge` at `dedup.py:193` is the only caller. `_merge` is called only
from `dedup` (`dedup.py:208`, `dedup.py:216`), which `main.py:180` invokes as
`dedup(raw_filtered + master)` (`main.py:179`) — i.e. the cumulative master CSV
is merged into fresh observations, which is what turns the defect below from
theoretical into permanent.

---

## Findings

### S1 — Trust ratchet: a row that ever saw Google is permanently frozen

- **Where:** `dedup.py:148-152` (`_trust_for` takes `max` over *all* sources of
  the record), combined with `dedup.py:173-174` (`source` is merged as a
  **union** that is written back to the master CSV).
- **Breaks:** `_pick`'s first ranking key is `_trust_for(field, rec)`
  (`dedup.py:181`). After a merge, `_merge` sets
  `merged["source"] = "google_places|osm"`. On the *next* run, that master row
  is compared against a fresh single-source row and `_trust_for` returns
  `max(3, 2) = 3` for `address`/`category`/`name` and `max(3, 1) = 3` for
  `lat`/`lon`. The master row therefore out-trusts **every** fresh OSM or
  Wikidata observation, forever. Trust is monotonic in the union and never
  decays, so after a few months essentially every master row has
  `max(all sources)` trust and `_SOURCE_TRUST` (`dedup.py:97-110`) is a no-op for
  every master-vs-fresh comparison — exactly the comparison the cumulative
  master exists to make.
- **Trigger (exact input, executed):**

  ```python
  # today's OSM record, and a master row written in January
  fresh_osm  = {"name": "Cafe Beirut", "phone": "+96170123456",
                "category": "coffee_shop", "address": "New St 5, Verdun 1003, Beirut",
                "region": None, "lat": 33.88512, "lon": 35.48311,
                "email": None, "website": None, "website_live": None,
                "facebook": None, "instagram": "cafe_beirut", "whatsapp": None,
                "linkedin": None, "rating": None, "review_count": None,
                "lead_score": 0, "completeness_score": 0,
                "industry_priority": None, "recommended_service": None,
                "source": "osm", "scraped_at": "2026-10-03T09:00:00+00:00"}
  master_row = {"name": "Cafe Beirut", "category": "restaurant", "region": "Beirut",
                "address": "Old Google Formatted, Hamra, Beirut, Lebanon",
                "lat": 33.89, "lon": 35.48, "phone": "+96170123456",
                "rating": 5.0, "review_count": 812, "lead_score": 85,
                "completeness_score": 4, "source": "google_places|osm",
                "scraped_at": "2026-01-01T00:00:00+00:00", ...}
  dedup([fresh_osm, master_row])
  ```

  Exact wrong output (all keys verified by execution):

  ```
  address  = 'Old Google Formatted, Hamra, Beirut, Lebanon'   # fresh said 'New St 5, Verdun 1003, Beirut'
  lat      = 33.89                                            # fresh said 33.88512
  lon      = 35.48                                            # fresh said 35.48311
  category = 'restaurant'                                     # fresh said 'coffee_shop'
  ```

  And it never recovers — folding three consecutive months of fresh OSM data:

  ```
  after run 2: fresh OSM said ('Feb St', 33.91) -> master now has ('Jan St', 33.89)
  after run 3: fresh OSM said ('Mar St', 33.92) -> master now has ('Jan St', 33.89)
  after run 4: fresh OSM said ('Apr St', 33.93) -> master now has ('Jan St', 33.89)
  ```

  Per-field ratchet, executed:

  ```
  field      master('google|osm|wd')  fresh single source   outcome
  address          3                       2 (osm)          MASTER (stale) wins
  category         3                       2 (osm)          MASTER (stale) wins
  name             3                       2 (osm)          MASTER (stale) wins
  lat              3                       1 (osm)          MASTER (stale) wins
  lon              3                       1 (osm)          MASTER (stale) wins
  email            3                       2 (wikidata)     MASTER (stale) wins
  ```

  Note `rating` and `review_count` ratchet from 1 → 3 too; they only escape harm
  today by the accident that Google is the only scraper that emits them.
  Downstream damage: `category` feeds `industry_priority` /
  `recommend_service` (`main.py:190-191`), `address` feeds
  `enricher.infer_region` (`enricher.py:85-88`, via `main.py:187`), and
  `lat`/`lon` feed the coordinate-box region fallback (`enricher.py:89-93`).
- **Fix:** rank by the source that *produced the value*, not by the record's
  accumulated source union — carry a per-field provenance sidecar
  (`{field: source}`) and pass it to `_trust_for`, or at minimum evaluate
  `_trust_for` against the record's **primary** source rather than the max over
  the union.

### S2 — `_merge` is not associative; `dedup()` output depends on input order

- **Where:** `dedup.py:176-177` (`scraped_at` = `max`, applied to the
  accumulator) fed back into `dedup.py:183` (`_recency(rec)`), plus the
  `source` union at `dedup.py:174` fed back into `dedup.py:181`
  (`_trust_for`). The docstring at `dedup.py:160-166` claims `_pick` is
  "deterministic" and exists specifically to remove order-dependence.
- **Breaks:** `scraped_at` is the recency key for `rating`, `review_count`,
  `website_live`, `website` (`dedup.py:114`). `_merge` stamps the accumulator
  with `max(scraped_at)` — **including the timestamp of records that supplied
  no value for the volatile field**. A record with `rating=None` but the newest
  `scraped_at` therefore lends its timestamp to an older, worse rating, and that
  value then out-competes a genuinely fresher observation in the next comparison.
  Same shape for the source union, per S1.
- **Trigger (exact input, executed):** three records with the same normalized
  phone `"+96170123456"`:

  ```python
  A  = {"name":"Cafe","phone":"+96170123456","rating":5.0,  "review_count":200,
        "scraped_at":"2026-01-01T00:00:00+00:00","source":"google_places"}
  B  = {"name":"Cafe","phone":"+96170123456","rating":None, "review_count":None,
        "scraped_at":"2026-09-01T00:00:00+00:00","source":"osm"}          # no rating, newest
  Cc = {"name":"Cafe","phone":"+96170123456","rating":4.0,  "review_count":90,
        "scraped_at":"2026-06-01T00:00:00+00:00","source":"google_places"}
  ```

  Exact wrong / divergent outputs (executed):

  ```
  _merge(_merge(A,B),Cc)["rating"] -> 5.0    review_count -> 200
  _merge(A,_merge(B,Cc))["rating"] -> 4.0    review_count -> 90
  associative? False
  ```

  Through `dedup()`, all six input orders:

  ```
  [google, osm,      google] -> rating=5.0 / review_count=200
  [osm,    google,   google] -> rating=5.0 / review_count=200
  [google, google,   osm]    -> rating=4.0 / review_count=90
  [google, osm,      google] -> rating=4.0 / review_count=90
  [osm,    google,   google] -> rating=4.0 / review_count=90
  [google, google,   osm]    -> rating=4.0 / review_count=90
  ```

  The correct answer under the stated rule ("fresher observation wins") is
  `rating=4.0` / `review_count=90`: the June observation beats the January one,
  and the September record has no rating to offer. The output is also labelled
  `"2026-09-01"` in every case, so the CSV claims the surviving January rating
  was observed in September.
- **Reachability note (why S2 and not S1):** `main.py:179` currently folds
  `raw_filtered + master`, so master rows always land **last** and the
  accumulator always inherits the current run's timestamp. That ordering masks
  this specific volatile-field instance today — I could not construct a
  reachable case under it. It becomes live run-to-run nondeterminism in
  `all_businesses.csv` the moment `combined` becomes `master + raw_filtered`, a
  CLI/merge subcommand is added, or master rows are interleaved. It is already
  live at the `dedup()` level, and `dedup` is a public entry point
  (`main.py:29`, `tests/test_lead_signal.py:65`).
- **Fix:** make `scraped_at` per-field rather than per-record — store
  `{field: scraped_at_of_the_observation_that_supplied_this_field}` and have
  `_pick` recurse on the *observations*, not on the merged accumulator.

### S2 — `rank` orders `trust` above `validity`, so an invalid value beats a valid one

- **Where:** `dedup.py:179-185`; `_trust_for` at `dedup.py:181` outranks
  `_validity` at `dedup.py:182`.
- **Breaks:** `_validity` is not a validity *check*, it is a tiebreak inside a
  trust tier. Whenever trust differs it is discarded entirely. `_validity`
  scoring an email `0` and a website `0` (`dedup.py:125`, `dedup.py:127`) buys
  nothing, and an unusable value from the trusted source overwrites a usable one
  from the less-trusted source. Note `google_places.py:290` emits
  `email=None`, so Google's `email` trust is the `_DEFAULT_TRUST` of `1`
  (`dedup.py:111`) — but every master row that ever touched OSM carries `email`
  trust `3`.
- **Trigger (exact input, executed):**

  ```python
  master_bad = {"name":"Cafe","phone":"+96170123456",
                "email":"info@cafe.com.lb, sales@cafe.com.lb",   # _EMAIL_OK rejects it
                "source":"google_places|osm","scraped_at":"2026-01-01T00:00:00+00:00"}
  fresh_wd   = {"name":"Cafe","phone":"+96170123456",
                "email":"hello@cafe.com.lb",                      # _EMAIL_OK accepts it
                "source":"wikidata","scraped_at":"2026-10-03T00:00:00+00:00"}
  _merge(fresh_wd, master_bad)["email"]
  ```

  Exact wrong output: `'info@cafe.com.lb, sales@cafe.com.lb'`
  (`_validity(email, survivor) == 0`). Comma-separated `contact:email` tags are
  common in OSM, and `osm.py:90` passes them through untouched. The row still
  counts as a contact channel in `main.has_any_contact` (`main.py:142-143`,
  `'"@" in email and len(email) > 5'` → `True`) and in
  `enricher.lead_score` (`enricher.py:288-289`, +20), so an undialable address
  is scored and exported as a lead.

  Same shape on `website`, executed: OSM `cafebistro.com.lb` (validity `0`,
  trust `3`) beats Google `https://maps.example/cafe` (validity `3`, trust
  `2`) in **both** argument orders. (Weakly reachable today — `osm.py:95-96`
  and `wikidata.py:103-104` prepend a scheme — but live for any master row
  written by an older revision, or for values like `"http://"` / `"http://localhost"`
  that survive the `startswith("http")` check and fail `_URL_OK`.)
- **Fix:** swap the tuple to `(validity, trust)` so an unusable value can never
  outrank a usable one, or hard-reject: if the winner's `_validity` is `0` and
  the loser's is `> 0`, take the loser.

### S3 — Fields with no trust entry are decided by lexicographic order

- **Where:** `dedup.py:184` (`str(value)` final tiebreak), `dedup.py:97-110`
  (no `region` key), `dedup.py:114` (`region` not in `_VOLATILE`).
- **Breaks:** `region` has no trust entry (so trust `1` for every source), is
  not volatile (so recency is `""`), and `_validity` returns the flat `1`
  (`dedup.py:140`). The winner is therefore the alphabetically-larger string.
  Since `enricher.infer_region` runs *after* dedup (`main.py:187`) and skips
  inference when `region` is already set (`enricher.py:331-335`), a stale region
  survives permanently.
- **Trigger (exact input, executed):** fresh Google record `region="Akkar"`
  (`scraped_at` `2026-10-03`) vs master record `region="Beirut"`
  (`scraped_at` `2026-01-01`). Exact wrong output: `'Beirut'`, in both argument
  orders — the January value wins because `"Beirut" > "Akkar"`.
- **Fix:** add `region` to `_VOLATILE` (or give it a trust entry), so
  `_recency` is consulted before the string tiebreak.

---

## Not a bug, but worth knowing

- **`_pick` *is* order-independent for a two-record merge, and here is the exact
  invariant.** `rank()` (`dedup.py:179-185`) is a 4-tuple
  `(trust, validity, recency, str(value))`. Tuple comparison is a total order,
  so `av if rank(a,av) >= rank(b,bv) else bv` (`dedup.py:187`) is symmetric
  **iff** `str(av) != str(bv)` or `av == bv`. Confirmed: `_merge(a,b)["address"]
  == _merge(b,a)["address"] == "B St"` for two same-tier OSM rows. The `>=`
  (rather than `>`) only matters when the `str` tiebreak is itself tied, i.e.
  two different objects with the same `repr` — in practice only `float("nan")`.
  So `>=` is not a bug; the order-dependence in S2 comes entirely from the
  non-associativity of the fold, not from the comparison operator.
- **`_is_missing` correctly treats `0` and `False` as present** (`dedup.py:86-90`).
  This is right for `website_live=False` (a confirmed dead site is high-value
  per `enricher.py:303-304`) and for the `0` sentinels that scrapers emit
  (`osm.py:118,121`, `google_places.py:301,304`,
  `wikidata.py:126,129`). The `0` sentinels only survive the merge because
  `"0"` happens to sort below every other digit string —
  `_pick("lead_score", 0, 100) == 100`. That is luck, not design.
- **`str(value)` tiebreak is lexicographic, not numeric.** Confirmed:
  `_pick("review_count", 9, 100) == 9`. Not currently harmful — `review_count`
  and `lead_score` come only from Google — but it makes any future multi-source
  numeric field wrong by construction. Likewise `_pick("lat", 9.5, 12.0) ==
  9.5`; harmless in this region because all Lebanon/Saudi latitudes and
  longitudes are two-digit, but it will break the moment a one-digit
  coordinate enters.
- **`_validity` accepts `nan`/`inf` for `lat`/`lon`** (`dedup.py:129-133`):
  `float("nan")` does not raise, so it scores a full `3`. Only reachable via
  non-standard JSON (`json.loads` accepts bare `NaN`); not worth fixing ahead
  of S1/S2.
- **`_pick("scraped_at", ...)` at `dedup.py:177` calls `max()` with no type
  guard.** Confirmed: `_pick("scraped_at", "2026-01-01", 20260101)` raises
  `TypeError: '>' not supported between instances of 'int' and 'str'`, which
  would kill the run at `main.py:180`. **Not reachable today** —
  `load_master` (`main.py:77-105`) leaves `scraped_at` as `str`/`None` and all
  three scrapers emit `utc_now_iso()` (`httpclient.py:220-229`), so the strings
  are always same-format and lexicographically ordered. It becomes reachable the
  moment `_merge` is exposed to hand-edited or externally-produced CSV. Cheap
  guard: `return str(max(str(av), str(bv)))`.
- **`_validity`'s `-100` sentinel is unreachable from `_pick`.** The
  `_is_missing` early returns at `dedup.py:168-171` fire first, so `rank()` is
  only ever called on two present values. Dead code, harmless.
- **`_pick` materialises keys.** `_merge` iterates `set(a) | set(b)`
  (`dedup.py:192`), and `_pick` returns `bv` (`None`) when `a` holds `None` and
  `b` lacks the key. `_merge({"name":"A","phone":"+96170123456","email":None},
  {"name":"A","phone":"+96170123456"})` yields `email: None`. `csv.DictWriter`
  writes that as `""` and `load_master` reads it back as `None`, so the round
  trip is stable — no finding.
- **The `source` union is load-bearing beyond trust.** `enricher.lead_score`
  awards +5 for `"|" in source` (`enricher.py:316-317`), so the union cannot
  simply be dropped as part of the S1 fix — it must be replaced, not removed.

---

## Recommended order of work

1. **S1** — make trust a property of the *provenance of the value*, not of the
   accumulated record's source union. Per-field provenance is the prerequisite
   for everything else here; without it, no ordering of the rank tuple is
   defensible.
2. **S2 (associativity)** — carry a per-field `scraped_at` alongside each value
   so the recency key describes the observation that actually supplied it, and
   so `_merge` becomes associative.
3. **S2 (trust above validity)** — reorder `rank` to `(validity, trust, ...)`.
   One line, and it converts a whole class of silent data corruption into a
   non-issue.
4. **S3 (`region`)** — add it to `_VOLATILE` or `_SOURCE_TRUST`.
5. Add the missing regression tests to `tests/test_lead_signal.py::TestMergeSurvivorship`:
   a multi-record (3+) associativity test, a master-vs-fresh trust-ratchet test,
   and a cross-trust-tier invalid-value test. The current suite
   (`tests/test_lead_signal.py:278-326`) only ever merges **two** records of
   **equal** trust, which is precisely the region where `_pick` is correct — so
   none of the three findings above can fail it today.