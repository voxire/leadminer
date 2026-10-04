# 346 — Tests: `_merge` is not a semilattice, and nothing tests that it is

**Target:** `_merge` at `dedup.py:190` (with `_pick` at `dedup.py:159`, `rank` at `dedup.py:179`)
**Lens:** tests
**Baseline:** `python3 -m unittest discover -s tests` → **28 passed** (`tests/test_lead_signal.py`).
Every test below was executed against `dedup.py` as it stands; each result (PASS/FAIL/TypeError) is
the observed result, not an estimate.

---

## Verdict

Commit `99493b9` fixed the three `_merge` bugs it set out to fix, and the eight tests it added pass.
But it replaced one wrong rule (whole-record field count) with a rule that is **not a semilattice**, and
the tests it shipped cannot see that: `tests/test_lead_signal.py:292` asserts commutativity for
**one field of one pair**, not for the merged object, and nothing tests associativity at all.

Consequence, verified today: for **four plausible records of one business** — a fresh Google record, two
fresh OSM records, and yesterday's master row — `dedup()` emits **two different `website` values**
depending only on which scraper thread finished first. The winner is decided by `str()` ordering between
`"https://maps.google.com/?cid=1"` and `"http://aaa-real.example"`, not by merit. Because `website`
drives `with_websites.csv` (`main.py:199`), `enricher.check_websites` HTTP GET (`enricher.py:231`), and
therefore `website_live`, `lead_score` and `recommended_service`, **the pitch we send changes run to run
for the same business.**

The one-line root cause: after the first merge, the accumulator's `source` is the *union* of all sources
and its `scraped_at` is the *max* (`dedup.py:174`, `dedup.py:177`). `_trust_for` (`dedup.py:148`) and
`_recency` (`dedup.py:155`) then read **those group attributes** and apply them to **whichever value
happens to be in the accumulator**. A surviving value is permanently re-credited with the best trust and
the newest timestamp in the group — including trust it never had. Provenance is laundered.

---

## What the suite already covers, and why it isn't enough

`tests/test_lead_signal.py:267-327` (`TestMergeSurvivorship`) is a good-faith regression set. It pins:

| Test | Line | Pins |
|---|---|---|
| `test_trust_is_per_field_not_per_record` | 278 | 002 S1 #1, 024 S1 |
| `test_empty_string_does_not_beat_a_real_value` | 286 | 002 S1 #4 |
| `test_merge_is_order_independent` | 292 | 002 S1 #3 — **but only `_merge(a,b)["website"] == _merge(b,a)["website"]` for one hand-picked pair where both values are valid URLs** |
| `test_invalid_value_loses_to_valid_one` | 297 | validity > trust-tie |
| `test_volatile_field_prefers_the_fresher_observation` | 303 | `_VOLATILE` recency |
| `test_out_of_range_rating_is_rejected` | 311 | `_validity` rating range |
| `test_sources_are_unioned_and_ordered` | 316 | `dedup.py:174` |
| `test_no_field_is_lost` | 321 | union-of-keys shape |

The three gaps that matter:

1. **`test_merge_is_order_independent` (line 292) compares one field, not the object.** Both records use
   `source="osm"` and both values are valid URLs, so `trust`, `validity` and `recency` all tie and the
   `str(value)` tiebreak (`dedup.py:184`) is symmetric. It passes for a reason that has nothing to do with
   the real pipeline, where the two records come from **different** sources and `trust` does not tie.
   A one-line change to `assertEqual(_merge(a, b), _merge(b, a))` fails today.
2. **No associativity test anywhere.** `dedup()` folds — `phone_index[key] = _merge(phone_index[key], record)`
   at `dedup.py:208` and `name_index[key] = _merge(...)` at `dedup.py:216` — so the result of a run is a
   left fold over arrival order. A fold over a non-associative operator is not a function of its input set.
3. **No test uses two records for the same source + two different sources + a stale record.** Every
   existing fixture is either two records or one stale-vs-fresh pair of the *same* source. Real runs
   produce three or four records per business, which is exactly where it breaks.

---

## Findings

### S1 — The merged record's group identity is used as the provenance of its individual values

- **Where:** `dedup.py:190-194` (`_merge`), `dedup.py:179-185` (`rank`), `dedup.py:148-152`
  (`_trust_for`), `dedup.py:155-156` (`_recency`), `dedup.py:174` / `dedup.py:177`
  (unioned `source`, maxed `scraped_at`). Reached from `dedup.py:208`, `dedup.py:216`; fed
  nondeterministically by `main.py:162-166` (`as_completed`) and `main.py:179` (`raw_filtered + master`).
- **Breaks:** `_pick` is not a function of `(field, value, the record that produced that value)`. It is a
  function of `(field, value, the accumulator)`. After the first merge the accumulator claims the union of
  all sources and the newest timestamp in the group, so:
  - a value from a *low*-trust source inherits a *high*-trust source's weight, and
  - a value from a *stale* record inherits the group's newest timestamp.
  Neither survives a later merge, so the fold's result depends on the order the scrapers happened to
  finish in. `as_completed` is explicitly documented as yielding "in arbitrary order".
- **Trigger (verified):** the fixture below, four records for one business, master appended last exactly
  as `main.py:179` does. Six permutations of the three fresh records produce two distinct `website` values:

  ```
  ['google_places', 'osm', 'osm']  ->  website='https://maps.google.com/?cid=1'
  ['google_places', 'osm', 'osm']  ->  website='http://aaa-real.example'      # same sources, other thread order
  ['osm', 'google_places', 'osm']  ->  website='https://maps.google.com/?cid=1'
  ['osm', 'osm', 'google_places']  ->  website='http://aaa-real.example'
  ['osm', 'google_places', 'osm']  ->  website='http://aaa-real.example'
  ['osm', 'osm', 'google_places']  ->  website='http://aaa-real.example'
  ```

  The single deciding question is whether the OSM record carrying the real URL arrived **before or after**
  the Google record. No master row is required for the divergence — the fresh-only case diverges
  identically.
- **Why it bites the product:** `website` is in `_VOLATILE` (`dedup.py:114`) so it also decides
  `website_live` (`enricher.py:250`), which decides `lead_score` (`enricher.py:303`) and
  `recommended_service` (`pitch_recommender.py`). A Maps CID redirect that survives the merge is then
  HTTP-GET by `enricher.check_websites` (`enricher.py:231`) — the request goes to the wrong host and the
  scraped email/Instagram come from the wrong page.
- **Fix:** carry per-field provenance through the accumulator. Either (a) `_merge` returns
  `(value, source, scraped_at)` per field and `rank` scores the *value's own* source/recency, or
  (b) `_pick` must be made a function of `(value, field, owning_source, owning_scraped_at)` stored
  alongside each value. Until then, at minimum make the fold order-independent by sorting records by a
  total key before folding in `dedup()`.

### S1 — `_merge` launders trust backwards: an invalid value from a trusted source beats a valid one

- **Where:** `dedup.py:180-185`. `rank` returns `(trust, validity, recency, str(value))` — **trust is
  the outer tuple element, so it dominates validity.**
- **Breaks:** `_validity` exists precisely to reject implausible values (`dedup.py:120-140`), but it can
  only ever break ties *within* a trust level. A high-trust source with garbage therefore wins outright.
- **Trigger (verified):** `_merge({"website": "not a url", "phone": "1", "source": "google_places"},
  {"website": "https://good.example", "phone": "1", "source": "mysite"})["website"]` → `"not a url"`.
  Also `_merge({"email": "n/a", "source": "google_places"}, {"email": "real@x.com",
  "source": "wikidata"})` is fine only because `google_places` has no `email` trust entry (`dedup.py:98-101`).
- **Reachability today:** **low**. `scrapers/osm.py:95-96` and `scrapers/wikidata.py:103-104` both prepend
  `https://`, so no scraper emits a scheme-less `website`; and the scrapers only emit the three known
  `source` labels. This becomes reachable the moment (a) a master CSV is hand-edited, (b) an unknown
  `source` label appears, or (c) `resolve_country`/`industry_priority` are moved before dedup as
  `docs/audits/084-SYNTH-adversarial-fix-review.md:188-189` recommends. Severity is S2 today.
- **Fix:** reorder to `(validity, trust, recency, str(value))`, or clamp — treat a validity of 0 as
  disqualifying rather than merely low-scoring.

### S2 — A value `_is_missing` calls missing is nevertheless written into the merged record

- **Where:** `dedup.py:168-171`. `_is_missing` correctly classifies `""`, `" "`, `"\t\n"`, `"\xa0"`,
  `"\u3000"` as missing (verified), but the short-circuit returns the *raw* other-side value, so a blank
  string crosses into the output. `_is_missing` is used for *selection* but never for *normalisation*.
- **Breaks:** `load_master` maps only the exact string `""` to `None` (`main.py:88`), so a cell containing
  a single space survives the CSV round trip untouched. Verified end to end:

  ```python
  # master row written with website="  ", fresh OSM row with no website key
  load_master(...)   -> website == '   '
  dedup([...])[0]    -> website == '   '
  r.get("website")   -> '   '  (truthy)
  ```

  A truthy website puts the record in `with_websites.csv` (`main.py:199`), excludes it from
  `without_websites.csv` (`main.py:200`), makes it an HTTP GET target (`enricher.py:231`), and awards it
  a `completeness_score` point (`enricher.py:107`) — a lead that has no website is scored as having one,
  so it is filtered out of the *new-site pitch* sheet, which is the product.
- **Trigger (verified):** `_merge({"name":"X","phone":"1","source":"osm"},
  {"name":"X","phone":"1","source":"osm","website":"   "})["website"]` → `'   '`.
- **Fix:** normalise on the way out — `_pick` should return `None`, not the raw value, whenever
  `_is_missing(value)` is true. One-line change at `dedup.py:169` and `dedup.py:171`.

### S2 — Eight of the 23 exported columns are arbitrated by `str(value)`

- **Where:** `dedup.py:97-110` (`_SOURCE_TRUST`), `dedup.py:184` (`str(value)`).
- **Breaks:** a field absent from every `_SOURCE_TRUST` row, and not in `_VOLATILE`, is decided purely
  by lexicographic comparison of its string form. Verified:

  | Field | `_SOURCE_TRUST` | Reaches the CSV? |
  |---|---|---|
  | `region` | absent from all three sources | **yes** — `enricher.py:331` only re-infers when falsy |
  | `country` | absent from all three sources | **yes** — written at `main.py:209`, `main.py:184` runs after dedup |
  | `linkedin` | absent from all three sources | **yes** |
  | `phone` | present but uniform (2/2/2) | **yes** in effect — `main.py:194` re-normalises whichever raw string won |
  | `lead_score` | absent | no — recomputed `main.py:197` |
  | `completeness_score` | absent | no — recomputed `enricher.py:349` |
  | `industry_priority` | absent | no — recomputed `main.py:190` |
  | `recommended_service` | absent | no — recomputed `main.py:191` |

  Consequences that are observable today: `country` `"LB"` vs `"SA"` → **`"SA"` always wins**, in both
  argument orders (verified) — a cross-border duplicate is relabelled Saudi purely because `S > L`.
  `linkedin` `"N/A"` vs `"Real"` → `"N/A"` survives as a value because `_validity` returns 1 for
  unknown fields (`dedup.py:140`). `region` `"Beirut"` vs `"Mount Lebanon"` → `"Mount Lebanon"`.
  `phone` `"+961 1 572121"` vs `"01 572 121"` → the punctuated one wins because `"+"` (0x2B) sorts after
  `"0"` (0x30)... in fact `"0" < "+"`, so `"+96…"` > `"01…"` and the international form wins by accident
  of ASCII, not by design.
- **Fix:** add explicit entries (or an explicit `"|" not handled` allow-list with a comment) for
  `region`, `country`, `linkedin`, `phone`. Add the meta-test G5 below so a future field cannot be added
  to `FIELDS` without a rule.

### S2 — `scraped_at` is compared with `max()` on raw values: `TypeError` kills the run

- **Where:** `dedup.py:176-177`.
- **Breaks:** `_is_missing(0)` is `False` (`dedup.py:86-90`), so a numeric or boolean `scraped_at` reaches
  `max(av, bv)` and raises. Verified: `_merge({"scraped_at": 1750000000, ...}, {"scraped_at":
  "2026-10-01T00:00:00+00:00", ...})` → `TypeError: '>' not supported between instances of 'str' and
  'int'`; the same with `True` → `... 'str' and 'bool'`.
- **Reachability today:** low — `httpclient.utc_now_iso()` (`httpclient.py:220-229`) always returns a
  string, and `csv.DictReader` always yields strings. But `dedup()` is a library function and this is
  the exact test `docs/audits/031-test-suite-design.md:236-241` specified as #10; it is still open and
  still untested. `_merge` crashing means `main.py:180` crashes, which means a 5-hour run produces
  nothing.
- **Fix:** `max(str(av), str(bv))`, or validate/coerce on entry.

### S2 — `_validity` accepts out-of-range and non-finite coordinates as maximally valid

- **Where:** `dedup.py:128-133`. `float(value)` succeeds for `200.0`, `999.0`, `"-33.9"`, `"nan"` and
  `"inf"`; there is no range check, unlike `rating` which does have one at `dedup.py:139`.
- **Breaks:** a corrupt coordinate from the *trusted* source (`google_places` has the only `lat`/`lon`
  trust entries, `dedup.py:99`) scores validity 3 and beats a real coordinate from any other source.
  Verified: `_merge({"lat": 200.0, "lon": 500.0, "source": "google_places"}, {"lat": 33.895,
  "lon": 35.481, "source": "osm"})` → `lat=200.0, lon=500.0`.
- **Impact:** `lat=200` fails every bounding box in `enricher.infer_region`, so `region` degrades to
  `"Unknown"`; a NaN coordinate silently poisons any future geo filter and serialises to CSV as `nan`.
  `cli.py` `cmd_validate` catches out-of-range `rating` — it has no coordinate check to catch.
- **Fix:** `_validity("lat"|"lon")` → `3 if -90 <= v <= 90` (or `-180 <= v <= 180`) and
  `v == v and v not in (inf, -inf)`.

### S3 — `_merge` is not idempotent on a non-canonical `source`

- **Where:** `dedup.py:173-174`.
- **Breaks:** `_sources_of` sorts and re-joins, so `_merge({"source": "b|a"}, {"source": "b|a"})`
  returns `{"source": "a|b"}` (verified). Harmless in isolation, but it means the merge is not a
  projection and `dedup()` is not idempotent on its own output.
- **Fix:** normalise `source` on entry to `dedup()`; test G4.

### S3 — `website_live`: a stale "live" beats a confirmed "dead" on a timestamp tie

- **Where:** `dedup.py:114` (`website_live` in `_VOLATILE`), `dedup.py:184` (`str(True) > str(False)`).
- **Breaks:** verified — two records with identical `scraped_at`, one `website_live=False` and one
  `True`, merge to `True` in **both** argument orders. `_validity` returns 1 for both (`dedup.py:140`),
  so nothing distinguishes "we confirmed it is dead today" from "it was alive a week ago".
- **Reachability today:** low in the main flow, because `enrich()` runs *after* dedup (`main.py:187`) so
  fresh records always carry `website_live=None`. It bites on master-vs-master merges of two prior rows
  that share a `scraped_at` to the second, and it is the exact lead-signal-inversion class that
  `tests/test_lead_signal.py:71-99` exists to prevent.
- **Fix:** give `website_live` an explicit ordering — `False` (confirmed dead) must outrank `True`
  when trust ties, because it is the harder-won observation.

---

## The test cases `_merge` still needs

Style note: `tests/test_lead_signal.py:1-11` mandates stdlib-only `unittest` and
`python3 -m unittest discover -s tests`. `hypothesis>=6.100` is already a declared dev dependency
(`pyproject.toml:23`) but bare `@given` functions are **not** collected by `unittest.discover`, so the
property tests below belong in a separate `tests/test_merge_properties.py` run under pytest, and each
carries a deterministic seed corpus in its `@example(...)` list so the invariants are also covered by the
stdlib runner. Proposed new file: `tests/test_dedup_merge.py`.

### G1 — Semilattice invariants (the headline gap)

```python
class TestMergeAlgebra(unittest.TestCase):
    """_merge must be a join-semilattice for dedup()'s left fold to be a
    function of its input SET rather than its input ORDER."""

    CORPUS = [
        # google fresh, websiteUri is a Maps CID redirect (the shape
        # scrapers/google_places.py:291 actually returns)
        {"name": "Acme", "phone": "+96170123456", "category": "clinic",
         "address": "Hamra St, Beirut, Lebanon", "country": "LB",
         "website": "https://maps.google.com/?cid=1", "email": None,
         "rating": 4.6, "review_count": 1350, "website_live": None,
         "lead_score": 0, "completeness_score": 0,
         "source": "google_places", "scraped_at": "2026-10-01T00:00:00+00:00"},
        # osm fresh, no website
        {"name": "Acme", "phone": "+96170123456", "category": "clinic",
         "address": "Hamra", "country": "LB", "website": None,
         "email": "real@acme.example", "rating": None, "review_count": None,
         "website_live": None, "lead_score": 0, "completeness_score": 0,
         "source": "osm", "scraped_at": "2026-10-01T00:00:00+00:00"},
        # osm fresh, real website
        {"name": "Acme", "phone": "+96170123456", "category": "clinic",
         "address": "Hamra", "country": "LB", "website": "http://aaa-real.example",
         "email": None, "rating": None, "review_count": None,
         "website_live": None, "lead_score": 0, "completeness_score": 0,
         "source": "osm", "scraped_at": "2026-10-01T00:00:00+00:00"},
        # yesterday's master row: unioned source, maxed timestamp
        {"name": "Acme", "phone": "+96170123456", "category": "clinic",
         "address": "Hamra, Beirut", "country": "LB",
         "website": "http://zzz-old.example", "email": None,
         "rating": 4.9, "review_count": 1200, "website_live": True,
         "lead_score": 75, "completeness_score": 4, "industry_priority": "high",
         "source": "google_places|osm", "scraped_at": "2026-09-01T00:00:00+00:00"},
    ]

    def test_merge_is_commutative_on_the_whole_object(self):
        """Currently FAILs: '' survives from one order, None from the other."""
        for a, b in itertools.combinations(self.CORPUS, 2):
            self.assertEqual(_merge(a, b), _merge(b, a),
                             f"order changed the result for sources "
                             f"{a['source']} / {b['source']}")
        blank = {"name": "X", "phone": "1", "source": "osm"}
        self.assertEqual(_merge(blank, dict(blank, website="")),
                         _merge(dict(blank, website=""), blank),
                         "empty string vs absent key is order-dependent")

    def test_merge_is_associative(self):
        """Currently FAILs: (A.B).C -> 3.0 but A.(B.C) -> 4.9."""
        A = {"name": "X", "source": "osm", "rating": 3.0,
             "scraped_at": "2026-10-01T00:00:00+00:00"}
        B = {"name": "X", "source": "google_places", "rating": None,
             "scraped_at": "2026-10-01T00:00:00+00:00"}
        C = {"name": "X", "source": "wikidata", "rating": 4.9,
             "scraped_at": "2026-10-01T00:00:00+00:00"}
        self.assertEqual(_merge(_merge(A, B), C), _merge(A, _merge(B, C)),
                         "trust is re-credited to the surviving value, so a "
                         "two-step fold disagrees with a different two-step fold")

    def test_dedup_is_permutation_invariant(self):
        """Currently FAILs: 2 distinct outputs from the same 4 records.
        Enumerates the consequence of the as_completed() race in
        main.py:162-166 deterministically, with master appended last
        exactly as main.py:179 does."""
        master = self.CORPUS[3]
        fresh = self.CORPUS[:3]
        outputs = {
            tuple(sorted((k, repr(v)) for k, v in dedup(list(order) + [master])[0].items()))
            for order in itertools.permutations(fresh)
        }
        self.assertEqual(len(outputs), 1,
                         f"{len(outputs)} different rows exported for one business; "
                         f"the winner is decided by str() ordering")

    def test_surviving_value_keeps_its_own_provenance(self):
        """Currently FAILs. The S1 test: the real URL must win regardless of
        whether the OSM thread finished before or after the Google thread."""
        g, o_none, o_site = self.CORPUS[0], self.CORPUS[1], self.CORPUS[2]
        master = self.CORPUS[3]
        first = dedup([g, o_none, o_site, master])[0]
        second = dedup([g, o_site, o_none, master])[0]
        self.assertEqual(first["website"], "http://aaa-real.example")
        self.assertEqual(second["website"], "http://aaa-real.example")

    def test_merge_is_idempotent(self):
        """Currently FAILs on a non-canonical source: 'b|a' -> 'a|b'."""
        a = {"name": "X", "source": "b|a"}
        self.assertEqual(_merge(a, a), a)

    def test_merge_key_set_is_the_union(self):
        """PASSES today. Cheap shape invariant; pins audit 024 S2."""
        self.assertEqual(set(_merge({"name": "A"}, {"rating": None})),
                         {"name", "rating"})

    def test_merge_does_not_mutate_its_arguments(self):
        """PASSES today. dedup() hands real dicts into _merge (dedup.py:208)."""
        a = {"name": "X", "phone": "1", "source": "osm"}
        b = {"name": "Y", "phone": "1", "source": "osm"}
        sa, sb = dict(a), dict(b)
        _merge(a, b)
        self.assertEqual(a, sa)
        self.assertEqual(b, sb)
```

### G2 — Volatile-field semantics must be a decision, not an accident

```python
    def test_validity_outranks_recency(self):
        """PASSES today. Pins the tuple order in dedup.py:180-185: a fresher
        junk value must not displace an older real URL."""
        old = {"name": "X", "phone": "1", "website": "https://real.example",
               "source": "osm", "scraped_at": "2026-09-01T00:00:00+00:00"}
        new = {"name": "X", "phone": "1", "website": "n/a",
               "source": "osm", "scraped_at": "2026-10-01T00:00:00+00:00"}
        self.assertEqual(_merge(old, new)["website"], "https://real.example")

    def test_trust_outranks_recency(self):
        """PASSES today, and pins the opposite of the property above. If this
        ever flips, one of the two tests above must change with it — that is
        the point."""
        stale_google = {"name": "X", "phone": "1", "website": "http://old.example",
                        "source": "google_places",
                        "scraped_at": "2026-09-01T00:00:00+00:00"}
        fresh_google = {"name": "X", "phone": "1", "website": "https://new.example",
                        "source": "google_places",
                        "scraped_at": "2026-10-01T00:00:00+00:00"}
        self.assertEqual(_merge(stale_google, fresh_google)["website"],
                         "https://new.example")

    def test_trust_outranks_validity(self):
        """Currently FAILs. Google's trust for website is 2 (dedup.py:100);
        an unknown source is _DEFAULT_TRUST 1 (dedup.py:111). Asserting the
        current ordering so a change is deliberate."""
        trusted_garbage = {"name": "X", "phone": "1", "website": "not a url",
                           "source": "google_places"}
        untrusted_valid = {"name": "X", "phone": "1",
                           "website": "https://good.example", "source": "mysite"}
        self.assertEqual(_merge(trusted_garbage, untrusted_valid)["website"],
                         "not a url")

    def test_dead_website_is_not_overwritten_by_stale_live(self):
        """Currently FAILs: 'True' > 'False' lexicographically, so a stale
        live verdict beats today's confirmed dead one. This is the lead-signal
        inversion class tests/test_lead_signal.py:71-99 exists to prevent."""
        dead = {"name": "X", "phone": "1", "website_live": False,
                "source": "osm", "scraped_at": "2026-10-01T00:00:00+00:00"}
        live = dict(dead, website_live=True)
        self.assertIs(_merge(dead, live)["website_live"], False)
        self.assertIs(_merge(live, dead)["website_live"], False)

    def test_newer_smaller_review_count_wins_by_design(self):
        """PASSES today: review_count is in _VOLATILE (dedup.py:114) so a
        fresher count of 12 displaces a stored 900. Google review counts are
        monotonically non-decreasing, so this is almost certainly wrong —
        the test exists to force an explicit decision."""
        old = {"name": "X", "phone": "1", "review_count": 900,
               "source": "google_places", "scraped_at": "2026-09-01T00:00:00+00:00"}
        new = {"name": "X", "phone": "1", "review_count": 12,
               "source": "google_places", "scraped_at": "2026-10-01T00:00:00+00:00"}
        self.assertEqual(_merge(old, new)["review_count"], 12)
```

### G3 — Falsy and sentinel values (regression guards for the old S1s)

```python
class TestFalsyAndSentinelValues(unittest.TestCase):
    def test_sentinel_zero_never_overwrites_a_computed_score(self):
        """PASSES today — but only because dedup.py:184 falls back to
        str(value) and '0' is lexicographically below every str(n), n>=1.
        Property, not example: a 20-line mechanism this accidental deserves a
        range, not one sample. Pins audit 002 S1 #2 (fresh_scrape lead_score=0
        wiping master lead_score=85)."""
        for n in range(0, 101):
            for field in ("lead_score", "completeness_score"):
                fresh = {"name": "X", "phone": "1", field: 0, "source": "osm"}
                master = {"name": "X", "phone": "1", field: n, "source": "osm"}
                self.assertEqual(_merge(fresh, master)[field], n, f"{field}={n}")
                self.assertEqual(_merge(master, fresh)[field], n, f"{field}={n}")

    def test_falsy_values_are_not_treated_as_missing(self):
        """PASSES today. Guards against a future 'fix' that rewrites
        _is_missing to use truthiness, which would delete every confirmed-dead
        website (website_live=False), every 5-star-0-review business, and
        every business on the equator."""
        real = {"name": "X", "phone": "1", "source": "osm"}
        for field, value in (("website_live", False), ("rating", 0),
                             ("review_count", 0), ("lat", 0.0), ("lon", 0.0)):
            merged = _merge(dict(real, **{field: None}), dict(real, **{field: value}))
            self.assertEqual(merged[field], value, f"{field}={value!r} was eaten")

    def test_no_missing_value_reaches_the_output(self):
        """Currently FAILs: '   ' survives (dedup.py:169/171 return the raw
        value). Reaches main.py:199-200 (with_websites.csv),
        enricher.py:231 (an HTTP GET for '   ') and enricher.py:107."""
        base = {"name": "X", "phone": "1", "source": "osm"}
        for blank in ("", " ", "\t\n", "\xa0", "\u3000"):
            merged = _merge(base, dict(base, website=blank))
            self.assertIsNone(merged["website"], f"{blank!r} reached the output")
        # and the round trip that produces the input
        # write_csv({'website': '  '}) -> load_master -> '  '  (main.py:88
        # maps only exact '' to None), so this is reachable from a real CSV.
```

### G4 — Type and shape robustness

```python
class TestMergeRobustness(unittest.TestCase):
    def test_non_string_scraped_at_does_not_crash(self):
        """Currently TypeError. This is audit 031 test #10, still open."""
        a = {"name": "X", "scraped_at": 1750000000, "source": "osm"}
        b = {"name": "X", "scraped_at": "2026-10-01T00:00:00+00:00", "source": "osm"}
        merged = _merge(a, b)
        self.assertIsInstance(merged["scraped_at"], str)

    def test_non_string_source_does_not_corrupt_provenance(self):
        """Currently FAILS: yields '123|osm' and "['osm']|google_places",
        which then hits _SOURCE_TRUST.get(...) -> _DEFAULT_TRUST for every
        field, silently discarding all trust ordering. Audit 031 test #9."""
        a = {"name": "X", "source": ["osm"]}
        b = {"name": "X", "source": "google_places"}
        self.assertEqual(_merge(a, b)["source"], "google_places|osm")

    def test_mixed_scalar_types_for_one_field_do_not_make_the_result_type_order_dependent(self):
        """Currently FAILS: _merge({'rating': '4.0'}, {'rating': 4.0}) gives
        '4.0'; the reverse gives 4.0. str(value) at dedup.py:184 ties, so the
        >= at dedup.py:187 hands back whichever side was passed first."""
        s = {"name": "X", "phone": "1", "source": "osm", "rating": "4.0"}
        f = {"name": "X", "phone": "1", "source": "osm", "rating": 4.0}
        self.assertEqual(type(_merge(s, f)["rating"]), type(_merge(f, s)["rating"]))

    def test_non_source_values_are_not_coerced_into_the_key(self):
        """Currently FAILS: lat=True beats lat=1 (str(True) > str(1))."""
        self.assertEqual(_merge({"lat": True, "phone": "1", "source": "osm"},
                                {"lat": 1, "phone": "1", "source": "osm"})["lat"], 1)
```

### G5 — A meta-test so `str()` can never become an arbiter again

```python
class TestTrustTableCoverage(unittest.TestCase):
    ALLOWED_UNRANKED = {"source", "scraped_at"}   # handled explicitly at 173-177

    def test_every_exported_column_has_a_real_ranking_rule(self):
        """Currently FAILS with 8 names. Stops a 24th column being added to
        main.FIELDS with no rule, which would silently make str() its
        resolver. The four that reach the CSV today: region, country,
        linkedin, phone. The other four are recomputed after dedup
        (main.py:190,191,197; enricher.py:349) so their merge behaviour is
        inert — but they should still be ruled, not left to chance."""
        unruled = []
        for field in main.FIELDS:
            if field in self.ALLOWED_UNRANKED or field in _VOLATILE:
                continue
            weights = {src: table.get(field) for src, table in _SOURCE_TRUST.items()}
            if all(w is None for w in weights.values()):
                unruled.append(f"{field}: absent from _SOURCE_TRUST")
            elif len(set(weights.values())) == 1:
                unruled.append(f"{field}: uniform trust {set(weights.values())}")
        self.assertEqual(unruled, [])

    def test_country_conflict_is_not_resolved_alphabetically(self):
        """Currently FAILS: 'SA' >= 'LB', so Saudi wins every tie, in both
        argument orders. Two sources disagreeing on country is exactly the
        cross-border case the project exists for."""
        lb = {"name": "X", "phone": "1", "country": "LB", "source": "osm"}
        sa = {"name": "X", "phone": "1", "country": "SA", "source": "wikidata"}
        self.assertEqual(_merge(lb, sa)["country"], "LB")
```

### G6 — Validity checks that `_validity` is missing

```python
class TestValidity(unittest.TestCase):
    def test_out_of_range_coordinates_are_rejected(self):
        """Currently FAILS for every case. _validity checks rating's range
        (dedup.py:139) but not lat/lon's (dedup.py:128-133), and
        float('nan') / float('inf') both succeed."""
        for bad in (200.0, 999.0, -33.9, "nan", "inf", "-inf"):
            self.assertEqual(_validity("lat", bad), 0, f"lat={bad!r} scored 3")
            self.assertEqual(_validity("lon", bad), 0, f"lon={bad!r} scored 3")

    def test_google_coordinates_outrank_osm_coordinates_only_when_plausible(self):
        """Currently FAILS. google_places holds the only lat/lon trust entry
        (dedup.py:99), so a corrupt Google coordinate beats a real OSM one."""
        bad = {"name": "X", "phone": "1", "lat": 200.0, "lon": 500.0,
               "source": "google_places"}
        good = {"name": "X", "phone": "1", "lat": 33.895, "lon": 35.481,
                "source": "osm"}
        self.assertEqual(_merge(bad, good)["lat"], 33.895)

    def test_missing_scores_still_beat_present_junk(self):
        """PASSES today. Keeps the -100 sentinel (dedup.py:122) honest."""
        self.assertEqual(_validity("email", "not-an-email"), 0)
        self.assertEqual(_validity("email", None), -100)
        self.assertEqual(_validity("website", "http://localhost:8080"), 0)
        self.assertEqual(_validity("rating", 99), 0)
```

### G7 — Property-based (`tests/test_merge_properties.py`, pytest only)

```python
@given(st.lists(record_strategy(), min_size=0, max_size=8))
@settings(max_examples=300, deadline=None)
def test_merge_is_commutative(records):
    for a, b in itertools.combinations(records, 2):
        assert _merge(a, b) == _merge(b, a)

@given(st.lists(record_strategy(), min_size=0, max_size=8))
@settings(max_examples=200, deadline=None)
def test_merge_is_associative(records):
    for a, b, c in itertools.permutations(records, 3):
        assert _merge(_merge(a, b), c) == _merge(a, _merge(b, c))

@given(st.lists(record_strategy(), min_size=0, max_size=8))
@settings(max_examples=200, deadline=None)
def test_dedup_never_loses_a_key(record_list):
    """Every key present in any input is present in some output record, and
    no non-missing value is silently dropped."""
```

`record_strategy()` must generate per-source field sets matching what the scrapers actually emit —
`osm.py:98-121` (`rating`/`review_count`/`whatsapp`/`linkedin` hardcoded `None`), `google_places.py:280-306`
(`email` hardcoded `None`, `website` from `websiteUri`), `wikidata.py:106-129` (only `name`, `category`,
`address`, `phone`, `email`, `website`, `lat`, `lon`). A uniform all-fields strategy, as in
`docs/audits/031-test-suite-design.md:421-455`, will **not** find the S1: it needs at least one record
per source with that source's real field set.

Measured on a hand-built 3-record corpus with scraper-faithful field sets, `_merge` is **non-associative
in 679/4000 and non-commutative in 694/4000** random pairs; `dedup()` diverges across permutations in
**559/1500**. With the master row appended last, exactly as `main.py:179` does, **1306/1330** realistic
permutations produce divergent output. These are the rates a property test would report on day one.

---

## What genuinely cannot be tested here, and why

1. **Whether the `_SOURCE_TRUST` weights are *correct*.** `dedup.py:97-110` encodes the prior
   "Google is authoritative for rating/coordinates, OSM for contact tags". No unit test can validate a
   business prior; it can only pin it. Validating needs a labelled gold set where a human has adjudicated
   the right value per field — see `docs/audits/032-fixtures-offline-replay.md`. G5 keeps the table
   *honest*, not *right*.
2. **The real distribution of junk.** My G/O/W fixtures are hand-built. In particular I assumed Google's
   `websiteUri` commonly returns a Maps CID redirect (`https://maps.google.com/?cid=…`) — that is from
   knowledge of Places v1, **not** from a captured payload; I am forbidden from making network calls, and
   there is no VCR cassette in the repo. The S1 fixture is structurally realistic (three real `source`
   labels, real trust/recency values, a Maps-shaped URL) but it is not a recorded sample. Before merging a
   fix, capture 3–5 real payloads and re-run G1 against them.
3. **Whether two records are the same business.** `_merge` is called only after `dedup` has already
   decided identity (`dedup.py:205-218`). The identity model — including the known gap that the
   phone-keyed and name-keyed indexes never cross-match (`dedup.py:226-230`, deliberately left alone in
   `99493b9` as backlog #44) — is a `dedup()` test, not a `_merge` test, and a different report's job.
4. **The thread race itself.** `main.py:162-166` collects via `as_completed`, which offers no seam to
   inject a scheduler without refactoring `main()`. The permutation tests above are the correct
   instrument: they enumerate the *consequence* deterministically. Do not try to "test the race" with
   mocks — you would be testing `as_completed`, not `dedup`.
5. **Whether the winning URL is actually the better one to pitch.** Deciding that needs a live fetch,
   a redirect chain, and domain-age data we do not collect. `_merge` can only be held to *determinism* and
   *documented precedence*, and that is what these tests do.
6. **CSV-cell weirdness beyond `" "`.** `load_master` maps only exact `""` to `None` (`main.py:88`). I
   verified `"\xa0"` and `"\u3000"` are caught by `_is_missing`, but I cannot enumerate which spreadsheet
   exports produce other near-empty cells. G3's round-trip assertion is the right shape; the corpus needs
   a real master CSV, which requires a real run.
7. **Cost.** `_trust_for` re-splits the `source` string and `_sources_of` re-allocates a set once **per
   field** (`dedup.py:148-152` called from `dedup.py:181`). `_pick` also builds a fresh `rank` closure and
   two tuples per field. That is a benchmark, not a correctness test — measure before optimising; the
   function is not currently the bottleneck next to 40-thread HTTP in `enricher.py`.

---

## Recommended order of work

1. **Write G1 first, as a failing test, before touching `dedup.py`.** `test_dedup_is_permutation_invariant`
   and `test_surviving_value_keeps_its_own_provenance` are the two that must be red. Everything else is
   hygiene next to them.
2. **Fix the S1 by carrying per-field provenance.** The smallest correct change: have `_merge` record,
   per field, the `source` and `scraped_at` of the record the surviving value came from, and score with
   those instead of the accumulator's unioned `source` and maxed `scraped_at`. A cheaper stopgap — sort
   `records` by a total key (`normalize_phone`, `normalize_name`, `source`, `scraped_at`) before folding
   in `dedup()` — makes the run reproducible without changing which value wins. Do the stopgap now, the
   real fix next; the stopgap alone still leaves the wrong winner, just a *consistent* one.
3. **One-line normalisations, each with a test in G3/G4:** return `None` instead of the raw blank at
   `dedup.py:169` and `dedup.py:171`; `max(str(av), str(bv))` at `dedup.py:177`; range-check
   `lat`/`lon` at `dedup.py:128-133`; reject non-str `source` at `dedup.py:144`.
4. **Then the decisions, not the accidents:** `rank` ordering (validity before trust, or trust before
   validity — pick one and say so in the docstring), `website_live` tie direction, `review_count`
   monotonicity, `country` conflict resolution. Each is one test in G2/G5.
5. **Add G5 to CI before anyone adds a column.** It is the only test in this list that prevents the class
   of bug from reappearing silently rather than loudly.
6. **Re-run G1 against captured payloads** (item 2 above) before calling the S1 closed.