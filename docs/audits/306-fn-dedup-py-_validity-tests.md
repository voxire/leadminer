# 306 — `_validity` (`dedup.py:120`): the test specification it still needs

## Verdict

`_validity` has **no direct test at all**. The whole suite reaches it only through
`_merge`, and only along two single-example paths (`tests/test_lead_signal.py:297-301`
for `email`, `:311-314` for an out-of-range `rating`). Of its six branches — the
`-100` sentinel, `email`, `website`, `lat`/`lon`, `rating`, and the flat `return 1`
— **four have zero discriminating coverage**, and one of those four (`lat`/`lon`,
`dedup.py:128-133`) is not merely untested but *wrong*: it awards the maximum score
of 3 to `nan`, `inf`, `999` and `True`, so a garbage coordinate beats a real one.
Every defect class that sibling audits already documented for this function
(`300-…_is_missing-contract.md` S1, `343-…_pick-ops.md` S2, `350-…_merge-deps.md`
S1/S2) is currently unpinned, so all of them would ship green.

This report is therefore mostly a **paste-in test class**, plus one genuinely new
finding the sibling reports missed: `_merge` is **not commutative** when the same
value arrives in two different Python types, which is the ordinary OSM-vs-Google
coordinate merge.

**Severity convention for this report** (the brief's table describes bugs, not gaps):
severity is the severity of the *untested defect class*, i.e. how bad it is that
nothing in CI would notice.

---

## Baseline: what is pinned today

| Branch | Line | Pinned by | Discriminating? |
|---|---|---|---|
| `_is_missing` → `-100` | `dedup.py:122-123` | nothing | no — and **unreachable** from `_pick` |
| `email` | `dedup.py:124-125` | `test_lead_signal.py:297-301` | yes, 1 pair (`"not-an-email"` vs `"a@b.com"`) |
| `website` | `dedup.py:126-127` | nothing | no — the 3 website tests (`:278`, `:286`, `:292`) all resolve on trust or `_is_missing`, never on validity |
| `lat` / `lon` | `dedup.py:128-133` | nothing | no — no `lat`/`lon` appears in `TestMergeSurvivorship` at all |
| `rating` (range) | `dedup.py:139` | `test_lead_signal.py:311-314` | yes, 1 pair (`99` vs `4.0`) |
| `rating` (non-numeric) | `dedup.py:137-138` | nothing | no |
| flat `return 1` | `dedup.py:140` | nothing | no |

Everything below was executed against `dedup.py` at `99493b9` (stdlib only, no
network). Tally: **17 tests, 18 failing assertions** (subTest failures counted).
11 tests pass, 6 fail.

---

## Findings

### S1 — an invalid value is adopted over a blank peer for every special-cased field, and nothing tests it

- **Where:** `dedup.py:120-140` returns `0` for "present but implausible";
  `dedup.py:168-171` short-circuits on `_is_missing` **only**, so a `0` is never
  demoted.
- **Pins:** the S1 in `docs/audits/300-fn-dedup-py-_is_missing-contract.md:82`,
  which prescribes the fix but ships no test.
- **Breaks:** a `website` that scores `0` is still truthy, so `main.py:199` files
  the row in `with_websites.csv` and `main.py:200` **excludes it from
  `without_websites.csv`** — the new-site pitch list that is the product. It also
  costs one HTTP GET at `enricher.py:231`, which then fails with
  `MissingSchema` and is swallowed at `enricher.py:176`, so the row is never
  cleaned up; it pollutes the cumulative master forever.
- **Trigger (verified, fully in-repo):** `scrapers/osm.py:95-96` prefixes `https://`
  to any `website` tag that lacks a scheme, so the OSM tag `website=no` becomes
  the literal `"https://no"` — `_is_missing` → `False`, `_URL_OK` → no match → `0`.
  ```python
  _merge({"name":"Cafe","phone":"+96170123456","source":"osm","website":"https://no"},
         {"name":"Cafe","phone":"+96170123456","source":"osm"})
  # -> {'website': 'https://no'}   assertIsNone(...) FAILS
  ```
  Same shape for `website="none"` (an OSM anti-pattern), `email="N/A"`,
  `rating=99`, `lat="999"`, `lat=nan` — 6 failing sub-cases.
- **Test:** `test_p6_invalid_value_is_not_adopted_over_a_blank_peer`.
- **Fix (one line, for the implementer):** `_validity(field, v) <= 0` must be
  treated as missing, i.e. the guard at `dedup.py:168-171` becomes
  `if _is_missing(av) or _validity(field, av) <= 0: return bv` and symmetrically.

### S1 — the `lat`/`lon` branch has no test and returns 3 for values that cannot be coordinates

- **Where:** `dedup.py:128-133`. Compare `rating` at `dedup.py:139`, which *does*
  range-check.
- **Pins:** S2 in `docs/audits/343-fn-dedup-py-_pick-ops.md:130`.
- **Breaks:** `float(value)` succeeds for `nan`, `inf`, `999`, `-999` and `True`
  (bool is an `int` subclass), so all five get the maximum score. Validity then
  ties and the final tiebreak is `str(value)` at `dedup.py:184`, where
  `'999' > '33.8938'`, `str(nan) == 'nan' > '33.8938'`, `str(inf) > '33.8938'`,
  `'True' > '33.8938'` — so the garbage wins **deterministically in both argument
  orders** (verified).
- **Trigger — the asymmetry is visible in one CSV row** (verified end to end
  through `main.load_master`, whose `float()` casts at `main.py:90-95` accept
  `"nan"`, `"inf"` and `"1e999"`):
  ```
  name,lat,lon,rating,website
  Cafe,nan,1e999,9.9,https://no
  -> load_master: lat=nan  lon=inf  rating=9.9
  -> _validity:   lat=3    lon=3    rating=0     website=0
  ```
  `rating` is correctly rejected; `lat`/`lon` are crowned. NaN then round-trips
  through `write_csv` as the string `nan` and is re-cast on every subsequent run,
  so the corruption is permanent in the cumulative master. Downstream,
  `enricher.infer_region`'s box test at `enricher.py:92` never matches a NaN, so
  `region` stays `None`/`Unknown` forever.
- **Honest reachability:** no in-repo producer emits a bad coordinate — Overpass is
  bounded by `area["ISO3166-1"="LB"]` (`scrapers/osm.py:13`), Places returns
  validated floats, `_parse_point` uses `float()` (`scrapers/wikidata.py:60`). The
  entry point is a hand-edited master CSV — which is exactly where these files live
  (they are uploaded to Google Drive by `.github/workflows/scrape.yml`). That
  self-perpetuation is what keeps this at S1 for the *test gap* rather than S2.
- **Tests:** `test_coordinates_reject_implausible_values` (5 failing sub-cases),
  `test_coordinates_reject_non_numbers` (passes), `test_p7_implausible_coordinate_beats_a_real_one`
  (4 failing sub-cases).
- **Fix:** `math.isfinite()` plus a range test. The bounds already exist as data at
  `enricher.py:58-72` (`_LB_COORD_REGIONS`, `_KSA_COORD_REGIONS`); a coarse
  `-90 <= lat <= 90` / `-180 <= lon <= 180` is enough to close it.

### S2 — `_merge` is not commutative when the same value arrives in two types; the existing commutativity test cannot see it

- **Where:** `dedup.py:184` (`str(value),  # deterministic final tiebreak`) and
  `dedup.py:187` (`return av if rank(a, av) >= rank(b, bv) else bv`). `_validity`
  returns the same score for both representations, `str()` renders them identically,
  so the tuple ties **exactly** and `>=` resolves the tie in favour of `a`.
- **Breaks:** `_merge(a, b)["rating"] is 4.2` (float) while `_merge(b, a)["rating"] is "4.2"`
  (str) — verified. This is not a hypothetical: `scrapers/osm.py:68-69` takes
  `lat`/`lon` straight from Overpass JSON as **strings**, while Places
  (`scrapers/google_places.py:273-274`) and `load_master` (`main.py:90-95`) produce
  **floats**. The OSM-vs-Google coordinate merge — the exact case per-field
  survivorship exists to serve — is the non-reproducible one.
- **Why no test catches it:** `tests/test_lead_signal.py:292-295` compares two
  website values that are both `str`. Same type, no tie, no finding. Commit
  `99493b9` claims to have fixed order-dependence ("the merge is commutative", and
  `Tests: 28, including commutativity … properties`) — that claim is true only
  within a type.
- **Observable today?** Not in the CSV: `str(33.8938)` and `"33.8938"` are the
  same text. It is observable to every numeric consumer, and there is one in the
  path: `enricher.py:92` does `if lat_min <= lat <= lat_max`, which is a **string**
  comparison when `lat` survived as a str. Verified divergence: `'33.85'` vs the
  Beirut floor `'33.845'` → numerically inside (33.85 ≥ 33.845), lexicographically
  outside → `infer_region` returns `None` for a business that is in Beirut.
- **Tests:** `test_p5_merge_is_commutative` (full table property, fails) and
  `test_p5b_merge_is_commutative_across_value_types` (minimal repro, fails).
- **Fix:** make `rank`'s last element type-aware (numeric compare for
  `lat`/`lon`/`rating`/`review_count`, per `350-…_merge-deps.md:134`), and assert
  the *type* as well as the value.

### S2 — the `website` validity branch has zero discriminating tests

- **Where:** `dedup.py:126-127`, regex at `dedup.py:117`.
- **Breaks:** the three website tests in the suite all decide the merge before
  validity is consulted — `:278-284` on `_SOURCE_TRUST` (osm website 3 vs google
  2), `:286-290` on `_is_missing("")`, `:292-295` on the `str()` tiebreak. Deleting
  the entire `website` branch from `_validity` would keep all 28 tests green.
- **Tests:** `test_website_table` (13 literals, passes today — this is a
  characterisation test) plus the 32 website sub-cases inside
  `test_p4_valid_beats_invalid_at_equal_trust`.
- **Boundaries worth pinning explicitly** (all verified, all currently passing, so
  they are guards in both directions):
  | literal | `_validity("website", …)` | note |
  |---|---|---|
  | `"https://a.example"`, `"http://a.example"`, `"HTTPS://A.EXAMPLE"` | `3` | `IGNORECASE` at `dedup.py:117` is load-bearing |
  | `"https://a.example/p?q=1#frag"` | `3` | regex is deliberately unanchored at the end |
  | `"https://x"`, `"https://.com"`, `"https://a .com"` | `0` | dot required, no spaces |
  | `"ftp://x.com"`, `"javascript:alert(1)"`, `"mailto:a@b.com"` | `0` | scheme allow-list |
  | `"www.cafe.com"`, `"cafe.com"` | `0` | **arguably wrong** — a real site scored invalid |
  | `"  https://a.example  "` | `3` | `.strip()` at `dedup.py:127` |

  The scheme-less row is the one to think about: `osm.py:95-96` and
  `wikidata.py:103-104` both prefix the scheme, so it should not arise from our own
  scrapers — but it means `_validity` treats "real URL, no scheme" as garbage, and
  anyone tightening or loosening `_URL_OK` should see the table go red.

### S2 — the `-100` sentinel is unreachable *and* untested, and the fix its sibling report prescribes will make it live

- **Where:** `dedup.py:122-123`. `_pick` calls `_validity` only from `rank`
  (`dedup.py:182`), which is only reached after `dedup.py:168-171` has already
  returned for either blank side. **No `_pick` input can reach it** (verified:
  `300-…_is_missing-contract.md:198-200` and `:243-248` both say so).
- **Why it still needs a test now:** `300-…_is_missing-contract.md:82` prescribes
  `if _is_missing(av) or _validity(field, av) <= 0: return bv`. That change routes
  *blank* values into `_validity`, which turns `-100` from dead code into the value
  that decides the branch. If `-100` were changed to `-1`, `0`, or deleted at the
  same time, **nothing in the suite would fail** — the fix would ship with a
  sentinel that no longer dominates.
- **Tests:** `test_p1_minus_100_iff_is_missing` (property over 23 fields × 11
  literals, passes), `test_p2_return_alphabet` (property: every return is in
  `{-100, 0, 1, 3}`, passes), `test_p3_sentinel_is_strict_minimum` (passes).
  Write these **before** the `_pick` change lands, not after.
- **Doc bug this exposes:** `dedup.py:121` says *"0-3 bonus"* and the function
  returns `-100`. `test_p2_return_alphabet` is the executable version of the
  contract the docstring should state.

### S3 — eleven fields have no validity case; junk is scored exactly like a real value

- **Where:** `dedup.py:140` (`return 1`). No case for `phone`, `facebook`,
  `instagram`, `whatsapp`, `linkedin`, `review_count`, `region`, `name`,
  `category`, `address`, `country`.
- **Breaks:** `_validity("phone", "---") == _validity("phone", "+96170123456") == 1`,
  and the tiebreak is then lexicographic, where `'+'` (0x2B) sorts below every
  letter — so `"not a phone"` beats `+96170123456` in a direct `_merge` call
  (verified).
- **Reachability: not through `dedup()` today, and that is worth stating plainly.**
  `normalize_phone` returns `""` for junk (`dedup.py:45-46`), so `dedup.py:205-206`
  routes a junk phone to the *name* index while a real phone goes to the *phone*
  index; the two records never meet. The gap becomes live the moment the
  cross-index merge lands — explicitly deferred as backlog #44 in commit `99493b9`
  ("the phone-keyed and name-keyed indexes do not cross-match"). This is also
  `200-fn-dedup-py-normalize_phone-ops.md:285`'s finding; I confirm it and add the
  test.
- **Test:** `test_unmodelled_fields_score_one` (55 assertions, passes today,
  characterises the gap). Do **not** write a `_merge`-level version yet — it would
  be testing a path that does not exist.

### S3 — the `except` clause is one exception type short, and a raised error here kills the run

- **Where:** `dedup.py:131` and `dedup.py:137`, both `except (TypeError, ValueError)`.
- **Breaks:** `float(10 ** 400)` raises `OverflowError`, which escapes `_validity`
  → `_merge` → `dedup()` → `main.py:180`, where the only `try` block wraps the
  *scrapers* (`main.py:164-168`). One enormous integer in a record aborts the whole
  run before any CSV is written. Verified.
- **Reachability:** unreachable from `json` / `csv.DictReader` / Overpass / Places
  (all produce `float` or `int` within range), so this is a guard for future
  callers, not a regression test.
- **Test:** `test_oversized_int_escapes_the_except_clause` — **passes today**
  because it asserts the crash. Flip it to "does not raise" when the fix lands.

### S3 — `bool` passes as a coordinate and a rating; `bytes` passes as an email

- **Where:** `dedup.py:129-133`, `dedup.py:135-139`, `dedup.py:125`.
- **Verified:** `_validity("rating", True) == 3` (→ 1.0, inside 0-5),
  `_validity("lat", True) == 3`, `_validity("email", b"a@b.com") == 3` (via
  `str(value)` at `dedup.py:125`).
- **Why it is still worth pinning:** `load_master` (`main.py:87-89`) turns blank
  cells into `None` but never coerces `"True"`/`"False"` outside `website_live`
  (`main.py:103`), and any future dict-shaped input (SQLite row, JSON cache — see
  `033-storage-sqlite-migration.md`) can hand this function a bool.
- **Test:** folded into `test_coordinates_reject_implausible_values` (`True`) and
  `test_nasty_values_do_not_raise` (`b"a@b.com"`).

---

## The exact test cases

Paste into `tests/test_lead_signal.py`. Requires changing the import at
`tests/test_lead_signal.py:65` to
`from dedup import _is_missing, _merge, _validity, normalize_phone` and adding
`import math` at the top. Stdlib only — no pytest, no `hypothesis`
(`requirements.txt` has 3 pins), so the properties below are hand-rolled as
cartesian loops over literal tables.

```python
# --- shared literal tables -------------------------------------------------

FIELDS = [  # the 22 columns of main.py:33-40, plus nothing
    "name", "category", "region", "country", "address", "lat", "lon",
    "phone", "email", "website", "website_live", "facebook", "instagram",
    "whatsapp", "linkedin", "rating", "review_count", "completeness_score",
    "lead_score", "industry_priority", "recommended_service",
    "source", "scraped_at",
]

MISSING = (None, "", "   ", "\t\n")
PRESENT = ("Cafe", "info@x.com", "https://x.com", 0, 0.0, False, True,
           33.8938, "33.8938", 4.2, 120, "n/a")

UNMODELLED = ["name", "category", "region", "address", "phone", "facebook",
              "instagram", "whatsapp", "linkedin", "review_count", "country"]

VALID_EMAIL = ("a@b.com", "info@cafe.com.eg", "first.last+tag@sub.cafe.co.uk")
INVALID_EMAIL = ("not-an-email", "a@b", "@b.com", "a@.com", "https://x.com",
                 "a b@c.com", "N/A", "info@a.com;info2@a.com")

VALID_URL = ("https://a.example", "http://a.example", "HTTPS://A.EXAMPLE",
             "https://a.example/p?q=1#frag")
UNPARSEABLE_URL = ("none", "n/a", "https://x", "ftp://x.com",
                   "javascript:alert(1)", "mailto:a@b.com", "https://a .com",
                   "https://.com")
SCHEMELESS_URL = ("www.cafe.com", "cafe.com", "https://cafe_beirut")

VALID_COORD = ("33.8938", 33.8938, " 33.89 ", 35.5, "35.5018", 0.0)
IMPLAUSIBLE_COORD = ("n/a", "33.89 4", "999", "-999", float("nan"),
                     float("inf"), True, {1, 2}, object())

VALID_RATING = (4.2, "4.2", " 4.2 ", 5, 5.0, 0, "0", 0.0)
INVALID_RATING = ("n/a", 99, 5.000001, -1, float("nan"), float("inf"))

NASTY = (b"a@b.com", b"1.5", [1], {"a": 1}, complex(1, 2), 10 ** 400)


class TestValidityUnit(unittest.TestCase):
    """Direct unit tests for dedup._validity (dedup.py:120). None of these exist
    today: the suite only reaches _validity indirectly, via _merge, and only for
    two of its five special-cased fields."""

    # P1 - the -100 sentinel fires on exactly the missing set
    def test_p1_minus_100_iff_is_missing(self):
        for field in FIELDS:
            for value in MISSING:
                self.assertEqual(_validity(field, value), -100, f"{field}={value!r}")
            for value in PRESENT:
                self.assertNotEqual(_validity(field, value), -100, f"{field}={value!r}")

    # P2 - the alphabet is exactly {-100, 0, 1, 3}; the docstring says "0-3"
    def test_p2_return_alphabet(self):
        for field in FIELDS:
            values = (MISSING + PRESENT + IMPLAUSIBLE_COORD[:6] + VALID_COORD
                      + VALID_EMAIL + VALID_RATING + UNPARSEABLE_URL)
            for value in values:
                self.assertIn(_validity(field, value), (-100, 0, 1, 3),
                              f"{field}={value!r}")

    # P3 - the sentinel is a strict minimum, which is what makes it safe
    def test_p3_sentinel_is_strict_minimum(self):
        for field in FIELDS:
            lowest_present = min(_validity(field, v) for v in PRESENT)
            self.assertLess(_validity(field, None), lowest_present, field)

    def test_email_table(self):
        for good in VALID_EMAIL:
            self.assertEqual(_validity("email", good), 3, good)
        for bad in INVALID_EMAIL:
            self.assertEqual(_validity("email", bad), 0, bad)
        self.assertEqual(_validity("email", "  a@b.com  "), 3, "must strip")
        self.assertEqual(_validity("email", "mailto:a@b.com"), 3,
                         "current boundary: the mailto: prefix is accepted")

    def test_website_table(self):
        for good in VALID_URL:
            self.assertEqual(_validity("website", good), 3, good)
        for bad in UNPARSEABLE_URL:
            self.assertEqual(_validity("website", bad), 0, bad)
        for url in SCHEMELESS_URL:
            self.assertEqual(_validity("website", url), 0, url)

    # THE FAILING TEST - pins 343-fn-dedup-py-_pick-ops.md S2
    def test_coordinates_reject_implausible_values(self):
        for bad in ("999", "-999", float("nan"), float("inf"), True):
            with self.subTest(value=bad):
                self.assertEqual(_validity("lat", bad), 0)
                self.assertEqual(_validity("lon", bad), 0)

    def test_coordinates_reject_non_numbers(self):
        for bad in ("n/a", "33.89 4"):
            self.assertEqual(_validity("lat", bad), 0)
            self.assertEqual(_validity("lon", bad), 0)

    def test_rating_table(self):
        for good in VALID_RATING:
            self.assertEqual(_validity("rating", good), 3, repr(good))
        for bad in INVALID_RATING:
            self.assertEqual(_validity("rating", bad), 0, repr(bad))
        self.assertEqual(_validity("rating", " 4.2 "), 3,
                         "float() tolerates surrounding whitespace")

    def test_unmodelled_fields_score_one(self):
        for field in UNMODELLED:
            for junk in ("junk", -5, 999, float("nan"), 0):
                self.assertEqual(_validity(field, junk), 1,
                                 f"{field}={junk!r} has no validity case")

    def test_nasty_values_do_not_raise(self):
        for field in FIELDS:
            for value in NASTY:
                if isinstance(value, int) and not isinstance(value, bool):
                    continue  # see the OverflowError test below
                try:
                    _validity(field, value)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"_validity({field!r}, {value!r}) raised {exc!r}")

    def test_oversized_int_escapes_the_except_clause(self):
        """PASSES today - it pins the crash. Flip to "does not raise" on fix.

        dedup.py:131/137 catch (TypeError, ValueError); float(10**400) raises
        OverflowError, which escapes _merge -> dedup() -> main.py:180."""
        with self.assertRaises(OverflowError):
            _validity("lat", 10 ** 400)


class TestValidityViaMerge(unittest.TestCase):
    """Reach _validity the way production does: through _pick's rank tuple
    (dedup.py:179-187). Both records carry the same `source` so _trust_for ties
    and validity is the deciding term."""

    BASE = {"name": "Cafe", "phone": "+96170123456", "source": "osm"}

    def _pair(self, field, a, b):
        return dict(self.BASE, **{field: a}), dict(self.BASE, **{field: b})

    # P4 - valid beats invalid at equal trust, for all three modelled fields
    def test_p4_valid_beats_invalid_at_equal_trust(self):
        cases  = [("email", bad, good) for bad in INVALID_EMAIL for good in VALID_EMAIL]
        cases += [("website", bad, good) for bad in UNPARSEABLE_URL for good in VALID_URL]
        cases += [("rating", bad, good) for bad in INVALID_RATING for good in VALID_RATING]
        for field, bad, good in cases:
            a, b = self._pair(field, bad, good)
            with self.subTest(field=field, bad=bad, good=good):
                self.assertEqual(_merge(a, b)[field], good)
                self.assertEqual(_merge(b, a)[field], good)

    # P5 - the commutativity property, over the whole table.
    # Needs a NaN-aware comparison: `nan != nan` makes a plain assertEqual fail.
    def test_p5_merge_is_commutative(self):
        def same(u, v):
            return u == v or (isinstance(u, float) and isinstance(v, float)
                              and math.isnan(u) and math.isnan(v))
        for field, values in (("email", INVALID_EMAIL + VALID_EMAIL),
                              ("website", UNPARSEABLE_URL + VALID_URL),
                              ("rating", INVALID_RATING + VALID_RATING),
                              ("lat", IMPLAUSIBLE_COORD + VALID_COORD)):
            for x in values:
                for y in values:
                    a, b = self._pair(field, x, y)
                    self.assertTrue(same(_merge(a, b)[field], _merge(b, a)[field]),
                                    (field, x, y))

    # P5b - the minimal repro. OSM gives lat/lon as str (osm.py:68-69), Places
    # and load_master give float (main.py:90-95): str(value) ties at dedup.py:184
    # and `>=` at dedup.py:187 hands the tie to `a`, so the surviving TYPE depends
    # on input order. Invisible in the CSV, visible to enricher.py:92.
    def test_p5b_merge_is_commutative_across_value_types(self):
        a = dict(self.BASE, source="osm", lat="33.8938", lon="35.5018", rating="4.2")
        b = dict(self.BASE, source="osm", lat=33.8938, lon=35.5018, rating=4.2)
        for field, want in (("lat", 33.8938), ("lon", 35.5018), ("rating", 4.2)):
            self.assertEqual(_merge(a, b)[field], want)
            self.assertEqual(_merge(b, a)[field], want)

    # THE FAILING TEST - pins 300-fn-dedup-py-_is_missing-contract.md S1
    def test_p6_invalid_value_is_not_adopted_over_a_blank_peer(self):
        blank = {"name": "Cafe", "phone": "+96170123456", "source": "osm"}
        for field, bad in (("website", "https://no"), ("website", "none"),
                           ("email", "N/A"), ("rating", 99),
                           ("lat", float("nan")), ("lat", "999")):
            with self.subTest(field=field, bad=bad):
                self.assertIsNone(_merge(dict(blank, **{field: bad}), blank)[field])

    # THE FAILING TEST - the merge-level consequence of the coordinate branch
    def test_p7_implausible_coordinate_beats_a_real_one(self):
        for bad in ("999", float("nan"), float("inf"), True):
            a = dict(self.BASE, source="wikidata", lat=bad, lon=bad)
            b = dict(self.BASE, source="wikidata", lat="33.8938", lon="35.5018")
            with self.subTest(bad=bad):
                self.assertEqual(_merge(a, b)["lat"], "33.8938")
                self.assertEqual(_merge(a, b)["lon"], "35.5018")

    # THE FAILING TEST - pins 350-fn-dedup-py-_merge-deps.md S2. Write this
    # BEFORE giving website_live a validity case, so the fix cannot be made to
    # prefer True over False.
    def test_server_confirmed_dead_outranks_live(self):
        t = "2026-09-01T00:00:00+00:00"
        dead = dict(self.BASE, website_live=False, scraped_at=t)
        live = dict(self.BASE, website_live=True, scraped_at=t)
        self.assertEqual(_merge(dead, live)["website_live"], False)
        self.assertEqual(_merge(live, dead)["website_live"], False)
```

### Expected result on today's `dedup.py` (`99493b9`)

```
Ran 17 tests — FAILED (failures=18)

PASS (11): p1, p2, p3, email_table, website_table, rating_table,
           coordinates_reject_non_numbers, unmodelled_fields_score_one,
           nasty_values_do_not_raise, oversized_int_escapes_the_except_clause,
           p4  (104 sub-cases, all green)

FAIL (6):  coordinates_reject_implausible_values   (5 sub-cases: 999, -999, nan, inf, True)
           p5_merge_is_commutative                 ('rating', 4.2, '4.2')
           p5b_merge_is_commutative_across_types
           p6_invalid_not_adopted_over_blank       (6 sub-cases)
           p7_implausible_coordinate_beats_real    (4 sub-cases)
           server_confirmed_dead_outranks_live     (True != False)
```

**Fifteen of the 18 failures are the two S1 findings** (`test_p6` × 6,
`test_coordinates_reject_implausible_values` × 5, `test_p7` × 4). The remaining
three are the type-dependent commutativity break (× 2) and the `website_live`
tri-state tie (× 1). That ratio is the argument for doing this before any other
dedup work: today, `_validity`'s two documented failure modes are both live and
both invisible.

---

## Not a bug, but worth knowing

- **The `website_live` finding is currently inert.** `350-…_merge-deps.md:173-196`
  is right that `_validity` scores `True` and `False` identically (both `1`) and
  that `"True" > "False"` wins the tie. But the damage never reaches an output:
  `enricher.check_websites` re-fetches the *merged* URL and overwrites
  `website_live` unconditionally at `enricher.py:250`, and a row with no website
  cannot earn the dead-site credit anyway (`enricher.py:303` requires
  `record.get("website")`). `main.py:187` runs `enrich` after `main.py:180` runs
  `dedup`, so the mask always applies. Write the test as a guard against the day
  someone reorders or memoises that pass; do not schedule a fix on its own.
- **`_validity("phone", junk) == 1` is a trap, not a bug, today.** See S3 above;
  `200-fn-dedup-py-normalize_phone-ops.md:285` has the same observation. The test
  to write is the direct unit assertion, and it should be re-checked when backlog
  #44 (cross-index merge) is picked up.
- **P1 is nearly tautological** — `_is_missing` is the first statement at
  `dedup.py:122`. It is still worth having, because it is the assertion that
  breaks if someone reorders the branches to make `_validity` field-first (which
  is the shape `300-…:82` is steering toward).
- **`_validity` is not, and cannot be, the place to fix the `str()` tiebreak.**
  That is `dedup.py:184` and it is `_pick`'s; see the S2 finding for why the
  existing commutativity test is insufficient.

## What genuinely cannot be tested here, and why

1. **The `-100` branch cannot be tested through any public path.** `_pick`
   short-circuits at `dedup.py:168-171`, so there is no input to `dedup()` or
   `_merge()` that reaches `dedup.py:123`. It has exactly one tester: a direct
   `_validity(field, None)` call. Corollary for the implementer: no integration
   test can ever protect that branch, so it must be protected by the unit test or
   not at all.
2. **Anything requiring the network is out of reach.** `enricher.check_websites`
   (`enricher.py:225-276`) opens real sockets, and the suite already stubs
   `requests` at `tests/test_lead_signal.py:23-60`. So "a junk website costs one
   wasted GET" can be asserted structurally (build the `targets` list, check its
   length) but **not** by running the enricher. The S1 consequence for the CSVs
   can be asserted by calling `main.py:199-200`'s predicate logic on a merged
   record, not by running `main()`.
3. **Whether the junk inputs actually occur cannot be tested at all.** That is a
   property of Overpass/Places/Wikidata data, and `data/` is gitignored, so there
   is no committed corpus to assert against. My `website=no` trigger is derived
   from `scrapers/osm.py:95-96`'s prefixing rule, which is in-repo and
   verifiable; the *frequency* of such tags is not. `032-fixtures-offline-replay.md`
   is the report that has to be fixed before this becomes testable.
4. **Property-based testing proper is not available.** `requirements.txt` pins
   three packages and neither `pytest` nor `hypothesis` is among them; the brief
   forbids installs. Every "property" above is therefore a nested `for` over a
   literal table, which means it tests the cases I thought of. That is a real
   limitation of this report: the tables were chosen by hand from the regexes at
   `dedup.py:116-117` and the producers at `scrapers/*`, not generated.
5. **The relative weight of `trust` vs `validity` inside `rank` cannot be isolated.**
   They are one tuple at `dedup.py:180-185` with no knob and `rank` is a closure
   with no seam. The only honest test is a composed `_merge` assertion, which is
   what `test_p4` does. Trying to assert the tuple's lexicity directly would mean
   reimplementing `rank` in the test — a tautology that passes no matter what the
   code does.
6. **"Newer" vs "lexically larger" cannot be distinguished for the `rating` and
   `website` recency term** (`dedup.py:183`). `_recency` (`dedup.py:155-156`)
   returns the raw string and `scraped_at` is ISO-8601, so a tz-aware parser would
   be needed to tell "later instant" from "larger text". That is
   `332-fn-dedup-py-_recency-contract.md` territory, not `_validity`'s — but it
   means `test_lead_signal.py:303-309` pins ordering, not chronology.
7. **Reachability of the S3 `OverflowError` and bool/bytes cases cannot be
   established from this repo.** `json`, `csv.DictReader` and both APIs produce
   neither. The tests are guards against the storage migration
   (`033-storage-sqlite-migration.md`) and a hand-edited master CSV, not
   regressions, and should be labelled as such when they land.

## Recommended order of work

1. **Paste the whole block above into `tests/test_lead_signal.py`** and land the
   11 passing tests now. They are characterisation tests: they cost nothing,
   they lock in `_validity`'s current contract, and they make every later change
   to `dedup.py:120-140` visible. Update the import at
   `tests/test_lead_signal.py:65` and add `import math`.
2. **Fix `dedup.py:128-133` so `test_coordinates_reject_implausible_values` and
   `test_p7` go green.** Add `math.isfinite()` and a range test; reuse the bounds
   already written down at `enricher.py:58-72` if country-aware precision is
   wanted. Smallest correct change; clears 9 of the 18 failures.
3. **Fix `dedup.py:168-171` so `test_p6` goes green** — treat
   `_validity(...) <= 0` as missing. This is the one-line fix from
   `300-…_is_missing-contract.md:82`, and it is the change that makes `-100` live,
   so P1/P2/P3 must already be in place (step 1).
4. **Make `rank`'s tiebreak type-aware so `test_p5` and `test_p5b` go green**
   (`dedup.py:184`). Do this before the `lat`/`lon` fix is relied on for
   reproducibility, and re-run `test_lead_signal.py:292-295` unchanged — it should
   stay green.
5. **Decide `website_live` deliberately.** Land
   `test_server_confirmed_dead_outranks_live` first, then either implement 350's
   S2 or record a comment explaining that the enricher masks it. Do not leave the
   test red "temporarily".
6. **Revisit S3 when backlog #44 (cross-index merge) is scheduled**, not before.
   Until then `phone` has no validity case by design of the keying, and the honest
   test is the unit assertion, not a merge assertion.