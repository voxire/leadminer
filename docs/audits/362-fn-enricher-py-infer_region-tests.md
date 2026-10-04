# 362 — `infer_region` test coverage: the exact cases still missing

## Verdict

`infer_region` has exactly **one** test in the entire repo (`tests/test_lead_signal.py:147-153`),
and that test only pins the *composition* `resolve_country -> infer_region`. Nothing pins the
function's own behaviour: not the keyword table, not the bounding boxes, not the `country`
contract, not the `enrich()` caller. As a result a whole governorate is provably unreachable
(`Nabatieh`'s coordinate box is 100% swallowed by `South Lebanon`, and 5 of its 7 keywords are
dead), 108 of 114 keyword entries match *inside* a longer word, and 6 of 19 real Lebanese cities
resolve to the wrong governorate — none of which any test would notice. Write the suite below
**before** applying the table fix in audit 094, because 094 rewrites all four tables and there is
currently nothing that says what "correct" means.

## What exists today, and the one distinction that matters

| Covered | Not covered |
|---|---|
| `infer_region("Hamra, Beirut, Lebanon", None, None, "LB") == "Beirut"` via `resolve_country` (`tests/test_lead_signal.py:147-153`) | `infer_region` with `country=None` / `""` / `"lebanon"` / `"LEB"` / `"sa"` |
| | the entire coordinate branch (`enricher.py:89-93`) |
| | the entire keyword table (`enricher.py:10-50`) except `beirut` |
| | `enrich()` as a caller (`enricher.py:330-335`) |
| | the `google_places.py:275` caller |

### Red vs green: which tests you write today

This is the trap. A naive suite for this function **locks in the bugs**. Sort every case:

- **Characterization (green now, documents current behaviour, must be deleted or inverted when
  the tables are fixed):** the 6 wrong-anchor cases, the `Nabatieh` cases, the substring-collision
  cases, the SA-address `None` cases, `country=None -> None`.
- **Bug-pin (red now, becomes green with the fix):** `test_hermel_is_baalbek_hermel`,
  `test_nabatieh_coordinate_is_nabatieh`, `test_bint_jbeil_is_nabatieh`,
  `test_baabda_is_mount_lebanon`, `test_byblos_is_mount_lebanon`,
  `test_saudi_address_without_coords_infers_region`.
- **Invariant (green now, must stay green forever):** purity/thread-safety, vocabulary parity,
  `None` only for out-of-country input, all strings are declared labels.

Everything I list below is tagged with its current result so you know what you are committing.

## How I verified every literal

Every `assertEqual` value below was produced by calling the real `enricher.infer_region` (no
source edits, no network, `python3 -B` so no `__pycache__` was written). Anything I could not
execute is marked **UNVERIFIED** and carries a caveat. `requests` is **not installed** in this
checkout, so importing `enricher` (`enricher.py:2`, `enricher.py:128`) requires the same
`_stub_requests()` shim `tests/test_lead_signal.py:23-63` already provides — copy it, and put it
in a shared `tests/conftest.py` rather than duplicating it.

Operational note: `pyproject.toml:75-78` sets `testpaths`/`addopts` but **no `pythonpath`**, so a
new test file needs the `sys.path.insert` shim from `tests/test_lead_signal.py:20`.

---

## Findings

### S1 — `Nabatieh` is unreachable from coordinates and reachable from only 2 of 7 keywords

- **Where:** `enricher.py:38-41` (keywords), `enricher.py:52-55` (first-match map),
  `enricher.py:64-65` (Nabatieh box), `enricher.py:66` (South Lebanon precedes it),
  `enricher.py:86-88` (address loop), `enricher.py:91-93` (coord loop).
- **Breaks:** Two independent dead-code paths.
  1. The `Nabatieh` box `33.240-33.560 × 35.330-35.720` (`enricher.py:65`) is *entirely
     contained* in the `South Lebanon` box `33.040-33.580 × 35.090-35.750` (`enricher.py:64`),
     which is checked first. I brute-forced the whole Nabatieh box at 0.005° resolution:
     **5,135 / 5,135 points return `"South Lebanon"`.** Over the whole Lebanese extent
     (32.5-35.0N × 34.5-37.5E at 0.02°) the coordinate path never once emits `"Nabatieh"`.
  2. Five of Nabatieh's seven keywords are shadowed by `South Lebanon`'s list, which contains
     all five (`enricher.py:36`): `nabatieh`, `النبطية`, `bint jbeil`, `marjayoun`, `hasbaya`.
     Only `merjeyoun` (a colloquial spelling) and `بنت جبيل` reach `"Nabatieh"`.
- **Trigger (verified):** `infer_region("Nabatieh", None, None, "LB")` → `"South Lebanon"`;
  `infer_region("Bint Jbeil", None, None, "LB")` → **`"Mount Lebanon"`** — not South Lebanon,
  because `"jbeil"` is a Mount Lebanon keyword (`enricher.py:17`) matching inside `"bint jbeil"`.
  So English `Bint Jbeil` is wrong in a *third* way. `infer_region(None, 33.3772, 35.4831, "LB")`
  → `"South Lebanon"`.
- **Pins:** audit 007:36-37 (S1 "Nabatieh unreachable"), audit 094:17-19 / 094:45-50 (S1 + S4).
- **Fix (test-visible):** the box must precede `South Lebanon`, or the two must be disjoint; the
  keyword must appear in exactly one set.

```python
# tests/test_infer_region.py  — TestInferRegionNabatieh
LB = {"address": None, "lat": None, "lon": None, "country": "LB"}

class TestInferRegionNabatieh(unittest.TestCase):
    """Regression: every Nabatieh rule is shadowed by an earlier South Lebanon rule
    (enricher.py:38-41 vs 33-37; enricher.py:64-65 vs 64). Audit 007:36, audit 094:45."""

    # RED today -> "South Lebanon". Fix the keyword sets, then this is green.
    def test_nabatieh_english_address(self):
        self.assertEqual(infer_region("Nabatieh", None, None, "LB"), "Nabatieh")

    # RED today -> "South Lebanon".
    def test_nabatieh_arabic_address(self):
        self.assertEqual(infer_region("النبطية", None, None, "LB"), "Nabatieh")

    # RED today -> "Mount Lebanon" (via "jbeil", enricher.py:17). This one is a
    # substring bug, not an ordering bug; it needs BOTH the token-boundary fix
    # and the de-duplicated sets to go green.
    def test_bint_jbeil_english_address(self):
        self.assertEqual(infer_region("Bint Jbeil", None, None, "LB"), "Nabatieh")

    # GREEN today (only reachable Nabatieh spelling) — pins the narrow path so a
    # blanket "de-duplicate by dropping Nabatieh's list" refactor is caught.
    def test_nabatieh_colloquial_address_still_resolves(self):
        self.assertEqual(infer_region("Marjeyoun", None, None, "LB"), "Nabatieh")

    def test_nabatieh_arabic_district_address_still_resolves(self):
        self.assertEqual(infer_region("بنت جبيل", None, None, "LB"), "Nabatieh")

    # RED today -> "South Lebanon". Pinned for all three city anchors in
    # audit 094:82.
    def test_nabatieh_coordinates(self):
        for lat, lon, place in [(33.3772, 35.4831, "Nabatieh city"),
                                (33.36,   35.59,   "Marjayoun"),
                                (33.12,   35.43,   "Bint Jbeil")]:
            with self.subTest(place=place):
                self.assertEqual(infer_region(None, lat, lon, "LB"), "Nabatieh")

    # RED today -> "South Lebanon" for all 5,135 sampled points. Exhaustive
    # proof the box is dead, not just "Nabatieh city happens to miss".
    def test_whole_nabatieh_coordinate_box_resolves_to_nabatieh(self):
        lat = 33.240
        while lat <= 33.560 + 1e-9:
            lon = 35.330
            while lon <= 35.720 + 1e-9:
                self.assertEqual(infer_region(None, round(lat, 4), round(lon, 4), "LB"),
                                 "Nabatieh", f"({lat:.3f},{lon:.3f})")
                lon += 0.02
            lat += 0.02
```

### S1 — keyword matching is unbounded substring: 108 of 114 entries match inside a longer word

- **Where:** `enricher.py:52-55` (`"|".join(re.escape(k) ...)` with **no `\b`**),
  `enricher.py:87` (`pattern.search`, not a tokenised match).
- **Breaks:** I appended `"zzqx"` to each of the 114 keyword entries and re-ran inference.
  **108 of 114 still matched their declaring region** — i.e. matching is entirely substring-based.
  The 6 that do not are exactly the 6 shadowed Nabatieh entries from S1 above. The damaging
  cases are the keywords that are ordinary English fragments, and the highest-risk ones are the
  ones ≤4 characters: `aley`, `metn`, `qaa`, `sur`, `tyre`, `zouk` plus `حلبا`, `زحلة`, `صور`,
  `صيدا`, `عكار`. Verified mislabels:
  - `infer_region("Rashaya Surgical Center, West Bekaa", None, None, "LB")` → **`"South Lebanon"`**
    — `"sur"` inside `"Surgical"`, and South Lebanon precedes Bekaa in `_REGION_MAP`.
  - `infer_region("Surgical Clinic, Taanayel", None, None, "LB")` → **`"South Lebanon"`**.
  - `infer_region("Al Hamra Hotel, King Fahd Rd, Riyadh", None, None, "LB")` → **`"Beirut"`**.
    Reachable in production: `main.resolve_country` (`main.py:50-74`) defaults to `LB` for any
    record with a blank country and a local-format phone, then `main.py:184` → `enricher.py:85`
    runs the LB keyword map over a Saudi address. A Riyadh hotel becomes a Beirut lead.
  - `infer_region("Hamra District, Amman, Jordan", None, None, "LB")` → `"Beirut"`.
  - `infer_region("Bliss Corner, Adana, Turkey", None, None, "LB")` → `"Beirut"`.
  - `infer_region("Corniche Street, Ajman, UAE", None, None, "LB")` → `"Beirut"`.
  - `infer_region("Zahle El Metn", None, None, "LB")` → **`"Mount Lebanon"`**, while
    `infer_region("Zahle", None, None, "LB")` → `"Bekaa"`. Caused by the malformed token
    `"zahle el metn"` at `enricher.py:21` (flagged at audit 094:175-177). Crispest possible
    demonstration that *adding a neighbourhood name to an address changes the governorate*.
- **Important caveat for whoever fixes this:** adding `\b` alone **loses coverage**. Today
  `infer_region("الصيدا", None, None, "LB")` → `"South Lebanon"` only because the definite
  article `ال` is absorbed by substring. Verified: `"صيدا"` → `"South Lebanon"` and
  `"الصيدا"` → `"South Lebanon"`. With `\b...\b` the second stops matching. The fix must be
  Unicode normalisation (strip `U+064B-0652`, unify `أإآ→ا`, `ة→ه`, `ى→ي`, `ی→ي`, `ك→ک`)
  **plus** boundaries, in that order. Audit 094:293-300 says the same thing.
- **Pins:** audit 094:295-300 (the word-boundary caveat, stated but never tested).

```python
class TestInferRegionKeywordMatching(unittest.TestCase):
    """Regression: enricher.py:53 joins keywords with no \\b, so any keyword
    matches inside a longer word. Audit 094:295-300."""

    # RED today (all four). After normalisation+boundary fix all four are green.
    def test_keyword_does_not_match_inside_a_longer_word(self):
        cases = [
            ("Rashaya Surgical Center, West Bekaa", "Bekaa"),
            ("Surgical Clinic, Taanayel", "Bekaa"),
            ("Al Hamra Hotel, King Fahd Rd, Riyadh", None),   # must NOT be Beirut
            ("Bliss Corner, Adana, Turkey", None),            # must NOT be Beirut
        ]
        for address, expected in cases:
            with self.subTest(address=address):
                self.assertEqual(infer_region(address, None, None, "LB"), expected)

    # RED today -> "Mount Lebanon" (enricher.py:21). Pins audit 094:175-177.
    def test_zahle_with_a_metn_neighbourhood_stays_bekaa(self):
        self.assertEqual(infer_region("Zahle", None, None, "LB"), "Bekaa")
        self.assertEqual(infer_region("Zahle El Metn", None, None, "LB"), "Bekaa")

    # GREEN today — pins the fix direction: normalisation must PRESERVE the
    # definite-article forms that a naive \\b fix would break.
    def test_definite_article_forms_still_match(self):
        self.assertEqual(infer_region("الصيدا", None, None, "LB"), "South Lebanon")
        self.assertEqual(infer_region("صور", None, None, "LB"), "South Lebanon")

    # RED today -> None for the diacriticised and Persian-yeh spellings.
    # enricher.py:53 does no Unicode normalisation at all.
    def test_arabic_diacritics_and_non_arabic_yeh_normalise(self):
        self.assertEqual(infer_region("بَيْرُوت", None, None, "LB"), "Beirut")
        self.assertEqual(infer_region("طرابلُس", None, None, "LB"), "North Lebanon")
        self.assertEqual(infer_region("زحلَة", None, None, "LB"), "Bekaa")
        self.assertEqual(infer_region("بیروت", None, None, "LB"), "Beirut")  # Persian ی

    # GREEN today — documents that Latin case is handled (re.IGNORECASE, enricher.py:53)
    # so a future refactor does not silently drop it.
    def test_latin_case_is_still_insensitive(self):
        for address in ["HAMRA, BEIRUT", "hamra, beirut", "Hamra, Beirut"]:
            self.assertEqual(infer_region(address, None, None, "LB"), "Beirut")
```

### S2 — `country` is an unvalidated string; five spellings silently disable the address branch

- **Where:** `enricher.py:83` (the parameter), `enricher.py:85` (`country == "LB"`),
  `enricher.py:90` (`country == "SA"`), and `enricher.py:334` where `enrich()` passes
  `r.get("country", "LB")` — the `.get` default that never fires when the value is `None`.
- **Breaks:** every value that is not exactly `"LB"` or exactly `"SA"` silently takes the
  *wrong* branch or no branch. All verified:
  | call | result | should be |
  |---|---|---|
  | `infer_region("Hamra, Beirut", None, None, None)` | `None` | `"Beirut"` |
  | `infer_region("Hamra, Beirut", None, None, "")` | `None` | `"Beirut"` |
  | `infer_region("Hamra, Beirut", None, None, "lebanon")` | `None` | `"Beirut"` |
  | `infer_region("Hamra, Beirut", None, None, "LEB")` | `None` | `"Beirut"` |
  | `infer_region("Hamra, Beirut", None, None, "sa")` | `None` | `"Beirut"` |
  | `infer_region(None, 24.7136, 46.6753, "sa")` | `None` | `"Riyadh"` |
  `main.resolve_country` (`main.py:59-74`) is the only normaliser, and `main.py:184` applies it
  before `enrich()` — so the production path is safe *today*. The hazard is the public function:
  `google_places.py:275` calls `infer_region` directly with `country=country` derived from
  `_country_from_query` (`google_places.py:127-136`), which returns uppercase so it is safe, but
  nothing enforces that, and audit 084:152-163 already flagged `enricher.py:334` and the fix was
  never applied. Audit 020:138 and 020:246 flag the same unguarded surface.
- **Note on the existing test:** `tests/test_lead_signal.py:147-153` passes
  `resolve_country(r)` in explicitly, so it proves `resolve_country` works — it does **not**
  prove `infer_region` tolerates a bad `country`. That is the gap this closes.
- **Pins:** audit 084 S3, audit 020:138/246, audit 048:24-29.

```python
class TestInferRegionCountryContract(unittest.TestCase):
    """Regression: enricher.py:85 and :90 compare the raw string, so only the
    exact literals "LB"/"SA" work. main.resolve_country normalises, infer_region
    does not."""

    # RED today, all five. The test as written asserts the CURRENT fragile
    # behaviour; the fix (normalise inside infer_region) inverts every one.
    @pytest.mark.parametrize("country,expected", [
        ("LB", "Beirut"),
        (None, "Beirut"),      # today: None
        ("", "Beirut"),        # today: None
        ("lebanon", "Beirut"), # today: None
        ("LEB", "Beirut"),     # today: None
    ])
    def test_country_spelling_is_tolerated(self, country, expected):
        self.assertEqual(infer_region("Hamra, Beirut", None, None, country), expected)

    @pytest.mark.parametrize("country,lat,lon,expected", [
        ("SA", 24.7136, 46.6753, "Riyadh"),
        ("sa", 24.7136, 46.6753, "Riyadh"),   # today: None
        ("KSA", 24.7136, 46.6753, "Riyadh"),  # today: None
    ])
    def test_saudi_country_spelling_routes_to_ksa_boxes(self, country, lat, lon, expected):
        self.assertEqual(infer_region(None, lat, lon, country), expected)

    # RED today -> None. Pins audit 084:152-163: enricher.py:334 uses
    # r.get("country", "LB"), whose default never fires for a present None.
    def test_enrich_passes_a_none_country_through_without_losing_the_address(self):
        out = enrich([{"name": "X", "address": "Hamra, Beirut", "country": None}])
        self.assertEqual(out[0]["region"], "Beirut")

    # GREEN today — the fix for enricher.py:334 must not break the missing-key case.
    def test_enrich_missing_country_key_defaults_to_lb(self):
        out = enrich([{"name": "X", "address": "Hamra, Beirut"}])
        self.assertEqual(out[0]["region"], "Beirut")

    # GREEN today — enrich() is offline-safe only when no record has a website:
    # check_websites returns early at enricher.py:232-233. Keep it that way or
    # every enrich() test needs an HTTP mock.
    def test_enrich_does_no_network_when_no_record_has_a_website(self):
        self.assertEqual(enrich([{"name": "X", "address": "Hamra, Beirut"}])[0]["website"],
                         None)
```

### S2 — the boxes overlap, and 6 of 19 real Lebanese cities resolve to the wrong governorate

- **Where:** `enricher.py:58-68` (table), `enricher.py:59` (`most-specific first` comment — the
  comment is aspirational; 12 of the 28 box pairs still overlap), `enricher.py:91-93` (loop).
- **Breaks:** I computed every pairwise intersection. **12 of 28 pairs overlap**; 2 are
  fully contained (Beirut ⊂ Mount Lebanon, Nabatieh ⊂ South Lebanon). Every overlap is resolved
  by list position, not by geography. Anchors from audit 094:73-87, run through the current code
  (**6 of 19 wrong**, verified):

  | place | (lat, lon) | truth (094) | today | |
  |---|---|---|---|---|
  | Beirut | 33.89, 35.50 | Beirut | `Beirut` | ok |
  | Baabda | 33.85, 35.53 | Mount Lebanon | `Beirut` | **wrong** |
  | Jounieh | 33.98, 35.63 | Mount Lebanon | `Mount Lebanon` | ok |
  | Byblos / Jbeil | 34.12, 35.65 | Mount Lebanon | `North Lebanon` | **wrong** |
  | Sidon | 33.56, 35.37 | South Lebanon | `South Lebanon` | ok |
  | Tyre | 33.27, 35.19 | South Lebanon | `South Lebanon` | ok |
  | Jezzine | 33.54, 35.58 | South Lebanon | `South Lebanon` | ok |
  | Nabatieh | 33.38, 35.48 | Nabatieh | `South Lebanon` | **wrong** |
  | Marjayoun | 33.36, 35.59 | Nabatieh | `South Lebanon` | **wrong** |
  | Bint Jbeil | 33.12, 35.43 | Nabatieh | `South Lebanon` | **wrong** |
  | Zahle | 33.85, 35.90 | Bekaa | `Bekaa` | ok |
  | Chtaura | 33.82, 35.77 | Bekaa | `Bekaa` | ok |
  | Baalbek | 34.00, 36.20 | Baalbek-Hermel | `Baalbek-Hermel` | ok |
  | Hermel | 34.39, 36.38 | Baalbek-Hermel | `Akkar` | **wrong** |
  | Halba | 34.55, 36.08 | Akkar | `Akkar` | ok |
  | Kobayat | 34.55, 36.28 | Akkar | `Akkar` | ok |
  | Tripoli | 34.44, 35.85 | North Lebanon | `North Lebanon` | ok |
  | Batroun | 34.25, 35.66 | North Lebanon | `North Lebanon` | ok |
  | Bcharre | 34.25, 36.03 | North Lebanon | `North Lebanon` | ok |

  Severity context, stated honestly: `region` feeds **only** two reporting sites,
  `main.py:228-234` and `cli.py:123-124`. It does not touch `lead_score`
  (`enricher.py:283-319`) or `recommend_service` (verified: `pitch_recommender.py` never reads
  `region`). So a wrong region does not corrupt scoring — it corrupts the column a human uses to
  choose a sales territory, permanently, in the cumulative master (`main.py:209`).
- **Pins:** audit 007:14-30 (S1 overlap table), audit 094:15-26 (S1).

```python
class TestInferRegionAnchorCities(unittest.TestCase):
    """Table-driven ground truth. Provenance: audit 094:73-87. Store as
    tests/data/lb_anchor_cities.csv with a `source` column, NOT inline, so the
    expected values can be reviewed by a human and re-proven against a boundary
    dataset later."""

    ANCHORS = [
        # (lat, lon, expected, place)
        (33.89,  35.50, "Beirut",         "Beirut"),
        (33.85,  35.53, "Mount Lebanon",  "Baabda"),          # RED: Beirut
        (33.98,  35.63, "Mount Lebanon",  "Jounieh"),
        (34.12,  35.65, "Mount Lebanon",  "Byblos/Jbeil"),    # RED: North Lebanon
        (33.56,  35.37, "South Lebanon",  "Sidon"),
        (33.27,  35.19, "South Lebanon",  "Tyre"),
        (33.54,  35.58, "South Lebanon",  "Jezzine"),
        (33.38,  35.48, "Nabatieh",       "Nabatieh"),        # RED: South Lebanon
        (33.36,  35.59, "Nabatieh",       "Marjayoun"),       # RED: South Lebanon
        (33.12,  35.43, "Nabatieh",       "Bint Jbeil"),      # RED: South Lebanon
        (33.85,  35.90, "Bekaa",          "Zahle"),
        (33.82,  35.77, "Bekaa",          "Chtaura"),
        (34.00,  36.20, "Baalbek-Hermel", "Baalbek"),
        (34.39,  36.38, "Baalbek-Hermel", "Hermel"),          # RED: Akkar
        (34.55,  36.08, "Akkar",          "Halba"),
        (34.55,  36.28, "Akkar",          "Kobayat"),
        (34.44,  35.85, "North Lebanon",  "Tripoli"),
        (34.25,  35.66, "North Lebanon",  "Batroun"),
        (34.25,  36.03, "North Lebanon",  "Bcharre"),
    ]

    def test_anchor_cities(self):
        for lat, lon, expected, place in self.ANCHORS:
            with self.subTest(place=place):
                self.assertEqual(infer_region(None, lat, lon, "LB"), expected)

    # GREEN today, must stay green after any table edit. Proves the loop really is
    # a fallback: an address hit is not overridden by contradicting coordinates.
    def test_address_wins_over_contradicting_coordinates(self):
        # coords alone would say Mount Lebanon
        self.assertEqual(infer_region(None, 33.855, 35.533, "LB"), "Beirut")
        # ...but the address keyword wins
        self.assertEqual(infer_region("Hamra, Beirut, Lebanon", 33.855, 35.533, "LB"),
                         "Beirut")
```

### S2 — the boxes are not a coverage guarantee, and the KSA table covers 0.46% of the country

- **Where:** `enricher.py:58-68`, `enricher.py:70-76`, `enricher.py:89-93`.
- **Breaks:** a grid sweep at 0.05° over Lebanon's own hull (33.0-34.75N × 35.05-36.70E) finds
  **805 of 1,224 cells covered (65.8%) — 419 holes**, of which 248 sit inside
  33.1-34.6N × 35.1-36.6E (the landmass). Saudi is far worse: **7 of 1,530 cells (0.46%)**
  over 16-32.5N × 34-56E. Verified `None`s: `infer_region(None, 21.270, 40.420, "SA")` (Taif)
  and `infer_region(None, 18.216, 42.505, "SA")` (Abha). And because `enricher.py:85` gates the
  address branch on `country == "LB"`, the address cannot rescue them:
  `infer_region("Taif, Al Hada Hotel", 21.270, 40.420, "SA")` → `None`.
  Box areas: LB 8 boxes = 2.80 deg² (≈34,500 km²) vs hull 2.96 deg². KSA 5 boxes = 1.23 deg²
  (≈15,100 km²) vs hull 60.19 deg² (≈742,000 km²).
- **Pins:** audit 007:40-44 (S2), audit 094:38-43 (S3), audit 087:268-290 (Test 3.5/3.6).

```python
class TestInferRegionCoverage(unittest.TestCase):
    """Coverage is a table property, not a per-point property. Assert it over a
    grid so a future box edit cannot shrink coverage silently."""

    LB_HULL = (33.0, 34.75, 35.05, 36.70)
    SA_HULL = (16.0, 32.5, 34.0, 56.0)

    def test_no_unexpected_holes_inside_the_lebanon_hull(self):
        holes = [p for p in self._grid(self.LB_HULL, 0.05, "LB")
                 if infer_region(None, p[0], p[1], "LB") is None]
        # Today: 419 holes over the full hull. After the table fix the count must
        # fall; keep an explicit allowlist of sea boxes rather than a bare number.
        self.assertEqual(holes, [], f"{len(holes)} uncovered 0.05 deg cells")

    def test_no_unexpected_holes_inside_the_ksa_hull(self):
        holes = [p for p in self._grid(self.SA_HULL, 0.5, "SA")
                 if infer_region(None, p[0], p[1], "SA") is None]
        # Today: 7 of 1,530 covered (0.46%). RED until the KSA table grows.
        self.assertEqual(holes, [])

    @staticmethod
    def _grid(hull, step, _country):
        la0, la1, lo0, lo1 = hull
        la, out = la0, []
        while la <= la1 + 1e-9:
            lo = lo0
            while lo <= lo1 + 1e-9:
                out.append((round(la, 4), round(lo, 4)))
                lo += step
            la += step
        return out
```

### S2 — geographically impossible coordinates are silently accepted

- **Where:** `enricher.py:89-93`. There is no plausibility guard anywhere in the pipeline.
- **Breaks:** every impossible coordinate returns `None`, and — this is the part that matters —
  the impossible `lat`/`lon` is still written verbatim to `all_businesses.csv`
  (`main.py:209`, `main.py:34-39`), so the corrupt row is indistinguishable from a real
  in-country row with a missing region. Verified: `infer_region(None, 0.0, 0.0, "LB")` → `None`
  (Null Island); `infer_region(None, 35.5, 33.8, "LB")` → `None` (lat/lon transposed, placing
  the row in the Mediterranean); `infer_region(None, 25.0657, 55.1713, "SA")` → `None` (a Dubai
  hotel returned by the free-text query `"luxury hotels in Saudi Arabia"`,
  `scrapers/google_places.py:275`, stamped `country="SA"`).
- **Note:** `float("nan")` and `float("inf")` also return `None` cleanly (no crash), which is
  lucky rather than designed — NaN fails every `<=` comparison.
- **Pins:** audit 048:24-29 (S1-2).

```python
class TestInferRegionImpossibleCoordinates(unittest.TestCase):
    """The function currently returns None for all of these, which is
    indistinguishable from "in-country but unmapped". Pin that, then invert."""

    def test_null_island_is_not_treated_as_a_lebanese_lead(self):
        self.assertIsNone(infer_region(None, 0.0, 0.0, "LB"))

    def test_transposed_coordinates_are_not_treated_as_a_lebanese_lead(self):
        self.assertIsNone(infer_region(None, 35.5, 33.8, "LB"))

    def test_out_of_country_coordinates_are_rejected(self):
        # Dubai hotel returned by the free-text query "luxury hotels in Saudi Arabia"
        self.assertIsNone(infer_region(None, 25.0657, 55.1713, "SA"))

    # GREEN today — documents the degenerate contract so a "helpful" refactor
    # doesn't start guessing when half the evidence is missing.
    def test_partial_or_missing_evidence_returns_none(self):
        self.assertIsNone(infer_region(None, None, None, "LB"))
        self.assertIsNone(infer_region("", None, None, "LB"))
        self.assertIsNone(infer_region(None, 33.89, None, "LB"))
        self.assertIsNone(infer_region(None, None, 35.50, "LB"))
        self.assertIsNone(infer_region(None, float("nan"), 35.50, "LB"))
        self.assertIsNone(infer_region(None, 33.89, float("inf"), "LB"))
```

### S3 — one non-numeric coordinate kills the whole enrichment stage

- **Where:** `enricher.py:92` (unguarded `float <= str`), `enricher.py:330-335` (no guard, no
  try/except), `enricher.py:232-233` (nothing above it catches).
- **Breaks:** `enrich([{"name": "X", "country": "LB", "lat": "33.89", "lon": "35.50"}])` raises
  `TypeError: '<=' not supported between instances of 'float' and 'str'` at `enricher.py:92` and
  propagates out of `enrich()` to `main.py:187` — the entire multi-hour run dies on one row.
  `infer_region(123, None, None, "LB")` likewise raises `TypeError` at `enricher.py:87`.
- **Honest reachability:** not reachable from `main()` *today*, because
  `main.load_master` casts `lat`/`lon` to `float` (`main.py:43`, `main.py:90-95`) and all three
  scrapers emit real numbers (`osm.py:68-69` from JSON, `google_places.py:273-274` from JSON,
  `wikidata.py:60` via `float()`). It becomes reachable the moment any source yields a numeric
  string, and `dedup._validity` (`dedup.py:128-133`) explicitly *blesses* numeric strings for
  `lat`/`lon` (`float(value)` succeeds → trust 3), so `_merge` will happily propagate one into
  the enriched set. That is a contract already half-loosened three modules away.
- **Pins:** audit 020:246, audit 048:17.

```python
class TestInferRegionInputHardening(unittest.TestCase):
    def test_numeric_string_coordinates_do_not_raise(self):
        self.assertEqual(infer_region(None, "33.89", "35.50", "LB"), "Beirut")

    def test_one_bad_coordinate_does_not_kill_enrich(self):
        records = [
            {"name": "good", "country": "LB", "address": "Hamra, Beirut"},
            {"name": "bad",  "country": "LB", "lat": "33.89", "lon": "35.50"},
        ]
        out = enrich(records)          # must not raise
        self.assertEqual(out[0]["region"], "Beirut")

    def test_non_string_address_does_not_raise(self):
        self.assertIsNone(infer_region(123, None, None, "LB"))
```

### S3 — the box edges are inclusive, and one ULP flips the governorate

- **Where:** `enricher.py:92` (`<=` on both ends).
- **Breaks:** a 100 m difference in a geocoder's output silently changes the governorate. All
  verified:
  | point | today | 1 ULP outside |
  |---|---|---|
  | `(33.845, 35.462)` | `Beirut` | `(33.845, 35.4619)` → `Mount Lebanon` |
  | `(33.920, 35.545)` | `Beirut` | `(33.9201, 35.50)` → `Mount Lebanon` |
  | `(33.054, 35.09)` | `South Lebanon` | `(33.0399, 35.09)` → `None` |
  | `(24.40, 46.40)` | `Riyadh` | `(24.3999, 46.40)` → `None` |
  | `(21.30, 39.05)` | `Jeddah` | `(21.30, 39.0499)` → `None` |
  This is the mechanism behind audit 007:29 ("small coordinate differences can cause silent
  governorate changes") and it is currently unasserted anywhere.
- **Also worth pinning:** the `Beirut` box (`enricher.py:60`) is 0.0062 deg² ≈ 77 km², roughly
  2× the real Beirut governorate (~42 km²). That surplus is precisely what swallows Baabda.

```python
class TestInferRegionBoxEdges(unittest.TestCase):
    """GREEN today — characterisation. Invert or delete when the boxes are fixed."""

    def test_edges_are_inclusive_and_one_ulp_moves_you(self):
        self.assertEqual(infer_region(None, 33.845,  35.462,  "LB"), "Beirut")
        self.assertEqual(infer_region(None, 33.845,  35.4619, "LB"), "Mount Lebanon")
        self.assertEqual(infer_region(None, 33.920,  35.545,  "LB"), "Beirut")
        self.assertEqual(infer_region(None, 33.9201, 35.50,   "LB"), "Mount Lebanon")
        self.assertIsNone(infer_region(None, 33.0399, 35.09, "LB"))
        self.assertEqual(infer_region(None, 24.40,   46.40,   "SA"), "Riyadh")
        self.assertIsNone(infer_region(None, 24.3999, 46.40,   "SA"))
        self.assertEqual(infer_region(None, 21.30,   39.05,   "SA"), "Jeddah")
        self.assertIsNone(infer_region(None, 21.30,   39.0499, "SA"))
```

### S3 — a pre-populated `region` is never re-derived, so Google records can never use the address branch

- **Where:** `enricher.py:331` (`if not r.get("region")`), and the caller
  `scrapers/google_places.py:275` which calls `infer_region(None, lat, lon, ...)` with the
  address **hardcoded to `None`**.
- **Breaks:** every Google record gets a coordinate-derived `region` at ingest. If that is
  non-`None`, `enricher.py:331` skips it forever, so the address text — the only evidence that
  could have corrected it — is never consulted. Google records only reach the address branch when
  their coordinates miss every box (and for `country == "SA"` that means they get `None`, S2
  above). Verified: `enrich([{"name": "X", "address": "Main St, Nabatieh", "country": "LB",
  "region": "South Lebanon"}])[0]["region"]` → `"South Lebanon"`; an empty-string `region`
  is also treated as falsey and gets overwritten to `"South Lebanon"`.
- **This is a design decision, not a typo**, so the test's job is to make the decision explicit
  and reversible rather than implicit. Audit 007:50 flagged the same behaviour. Audit 045:27-30
  argues for deleting the `google_places.py:26` import entirely, which would also delete this
  trap.
- **Pins:** audit 007:50, audit 045:24-30.

```python
class TestInferRegionPrecedence(unittest.TestCase):
    """GREEN today. This pins the precedence ORDER, which is currently
    undocumented and unasserted: existing region > address > coordinates."""

    def test_existing_region_is_preserved_even_when_the_address_contradicts_it(self):
        out = enrich([{"name": "X", "address": "Main St, Nabatieh",
                       "country": "LB", "region": "South Lebanon"}])
        self.assertEqual(out[0]["region"], "South Lebanon")

    def test_empty_region_is_re_derived(self):
        out = enrich([{"name": "X", "address": "Main St, Nabatieh",
                       "country": "LB", "region": ""}])
        self.assertEqual(out[0]["region"], "South Lebanon")  # coords absent -> address

    def test_coordinates_only_win_when_the_address_says_nothing(self):
        out = enrich([{"name": "X", "address": "Riyadh", "country": "SA",
                       "lat": 24.7136, "lon": 46.6753}])
        self.assertEqual(out[0]["region"], "Riyadh")
```

## The property suite (preferred over examples)

Examples go stale the moment a table changes; properties describe what must stay true. These
are generative and run over the tables themselves, so they survive the audit-094 rewrite. Add
`hypothesis` (already in `pyproject.toml:22-28` dev deps) or generate from the tables directly.

```python
import itertools, re, threading
from concurrent.futures import ThreadPoolExecutor

import enricher as E


class TestInferRegionTableInvariants(unittest.TestCase):
    """Properties over the tables, not over hand-picked points."""

    # PROPERTY 1 — keyword ownership. Today: 6 violations, all in the Nabatieh
    # shadow (audit 094:45-50). Turns a data-entry error into a test failure.
    def test_every_keyword_resolves_to_its_own_declaring_region(self):
        wrong = [(r, k, infer_region(k, None, None, "LB"))
                 for r, kws in E.REGION_KEYWORDS for k in kws
                 if infer_region(k, None, None, "LB") != r]
        self.assertEqual(wrong, [], f"{len(wrong)} shadowed keyword entries")

    # PROPERTY 2 — keyword sets are pairwise disjoint. Today: 5 keywords appear
    # in two region sets (nabatieh, النبطية, bint jbeil, hasbaya, marjayoun).
    def test_no_keyword_is_declared_by_two_regions(self):
        owner = {}
        clashes = []
        for region, kws in E.REGION_KEYWORDS:
            for k in kws:
                if k in owner:
                    clashes.append((k, owner[k], region))
                owner[k] = region
        self.assertEqual(clashes, [])

    # PROPERTY 3 — no keyword matches inside a longer word. Today: 108 of 114.
    # Requires Unicode normalisation FIRST, then \\b (audit 094:295-300).
    def test_keywords_are_token_bounded(self):
        offenders = [(r, k) for r, kws in E.REGION_KEYWORDS for k in kws
                     if infer_region(k + "zzqx", None, None, "LB") == r]
        self.assertEqual(offenders, [], f"{len(offenders)} keywords match substrings")

    # PROPERTY 4 — boxes are disjoint, or every overlap is declared. Today: 12
    # of 28 LB pairs overlap and none is declared anywhere.
    def test_lb_boxes_do_not_overlap_unless_precedence_is_declared(self):
        declared = getattr(E, "_LB_PRECEDENCE", set())
        overlaps = []
        for a, b in itertools.combinations(E._LB_COORD_REGIONS, 2)
        ilat = (max(a[1], b[1]), min(a[2], b[2]))
        ilon = (max(a[3], b[3]), min(a[4], b[4]))
        if ilat[0] <= ilat[1] and ilon[0] <= ilon[1]:
            overlaps.append((a[0], b[0]))
            self.assertIn(tuple(sorted((a[0], b[0]))), declared)
        self.assertEqual(overlaps, [])   # after the table is fixed

    # PROPERTY 5 — ordering stability: shuffling the box table may change the
    # answer ONLY inside a declared overlap. Forces the precedence table to exist.
    def test_reordering_the_box_table_only_affects_declared_overlaps(self):
        original = list(E._LB_COORD_REGIONS)
        baseline = {p: infer_region(None, p[0], p[1], "LB")
                    for p in self._grid((33.0, 34.75, 35.05, 36.70), 0.05)}
        for shuffled in itertools.islice(itertools.permutations(original), 50):
            E._LB_COORD_REGIONS[:] = list(shuffled)
            try:
                for p, expected in baseline.items():
                    got = infer_region(None, p[0], p[1], "LB")
                    if got != expected:
                        self.fail(f"reordering changed {p}: {expected} -> {got}")
            finally:
                E._LB_COORD_REGIONS[:] = original
```

```python
class TestInferRegionPurity(unittest.TestCase):
    # PROPERTY 6 — purity and thread safety. google_places.py:190 runs 5 worker
    # threads and each calls infer_region at :275; the module-level compiled
    # patterns (enricher.py:52-55) are shared. Audit 015:102 noted the call site.
    def test_infer_region_is_pure_and_thread_safe(self):
        samples = [("Hamra, Beirut", None, None, "LB"),
                   (None, 34.394, 36.384, "LB"),
                   ("Main Square, Nabatieh", None, None, "LB"),
                   ("King Fahd Road, Riyadh", 24.7136, 46.6753, "SA")]
        expected = [infer_region(*a, country=c) for a, _, _, c in samples]
        with ThreadPoolExecutor(max_workers=8) as pool:
            got = list(pool.map(lambda s: infer_region(s[0], s[1], s[2], country=s[3]),
                                samples * 200))
        self.assertEqual(got, expected * 200)

    # PROPERTY 7 — vocabulary parity. GREEN today for LB (verified: the 8 keyword
    # labels equal the 8 box labels). This is the test that stops audit 094 from
    # silently splitting the vocabulary: 094:69 renames the box label to "North"
    # while 094:144 keeps "North Lebanon" in the keyword table. main.py:228-234
    # and cli.py:123-124 would then report one governorate under two names.
    def test_keyword_and_box_vocabularies_are_identical(self):
        self.assertEqual({r for r, _ in E.REGION_KEYWORDS},
                         {r for r, *_ in E._LB_COORD_REGIONS})

    # PROPERTY 8 — no invented labels. Guards against a typo like "Mount
    # Lebannon" reaching the CSV.
    def test_every_returned_label_is_a_declared_one(self):
        declared = ({r for r, _ in E.REGION_KEYWORDS}
                    | {r for r, *_ in E._LB_COORD_REGIONS}
                    | {r for r, *_ in E._KSA_COORD_REGIONS})
        for _ in range(200):
            self.assertIn(infer_region("Beirut", 33.89, 35.50, "LB"), declared)
            self.assertIn(infer_region(None, 24.7136, 46.6753, "SA"), declared)

    @staticmethod
    def _grid(hull, step):
        la0, la1, lo0, lo1 = hull
        la, out = la0, []
        while la <= la1 + 1e-9:
            lo = lo0
            while lo <= lo1 + 1e-9:
                out.append((round(la, 4), round(lo, 4)))
                lo += step
            la += step
        return out
```

## Not a bug, but worth knowing

- **The KSA boxes do not overlap at all** (verified: zero non-zero pairwise intersections among
  the 5 boxes at `enricher.py:70-76`). Every defect on the Saudi side is *coverage*, not
  ordering. So `test_reordering_the_box_table_only_affects_declared_overlaps` is expected to
  pass trivially for SA — do not treat that as evidence the KSA table is correct.
- **`enricher.py:89` requires *both* lat and lon.** A record with only one is `None`
  (verified). Untested anywhere, and it is a plausible real shape given OSM ways always have a
  centre (`osm.py:68-69`) but a malformed element would not.
- **The address branch is `search`, not `match`.** Any keyword anywhere in a ~200-character
  `formattedAddress` (`google_places.py:286`) wins. That is why substring collisions are so
  damaging here specifically: Google addresses are long and free-form.
- **The function cannot distinguish "unmapped" from "out of country".** Both are `None`
  (`enricher.py:94`). Audit 048:24-29 wants an explicit quarantine; until then, any test for
  the second has to assert `None` and a test for the first has to assert the opposite. Do not
  write both without deciding which one you are implementing.
- **`region` is report-only.** Verified: `enricher.py:283-319` (`lead_score`) never reads it and
  `pitch_recommender.py` never reads it. Keep that in mind when triaging: fixing region
  improves segmentation and territory assignment, not lead prioritisation.

## What genuinely cannot be tested here, and why

1. **Whether a bounding box is geographically *correct*.** This is the important one. Every
   expected value in this report is an audit's opinion (mine, citing audit 094:73-87). A unit
   test can only assert *internal consistency* — property 1/4 above prove the table is coherent,
   not that it matches Lebanon. Answering the real question needs an authoritative polygon
   dataset (GeoJSON of Lebanese and Saudi governorate boundaries), a `shapely` point-in-polygon
   implementation (audit 094:302-312), and a slow/opt-in test that sweeps points and compares
   against the polygons. Do not pretend the anchor table is ground truth — **give the fixture a
   `source` and `verified_by` column** so a reviewer can challenge it, and mark it
   `@pytest.mark.slow` so it is not the thing that blocks CI.
2. **Whether a `None` is the *right* answer for an in-country unmapped point.** That is a
   product decision (blank region vs. `"Unknown"` vs. quarantine), not a fact. It cannot be
   tested until it is decided; writing a test now bakes in an accident.
3. **Real-world address string diversity.** The substring-collision set is unbounded —
   `"Surgical Clinic, Taanayel"` is one instance of an open-ended class. You cannot enumerate
   the corpus offline. What you *can* test is the mechanism (property 3: append `"zzqx"` to
   every keyword), which generalises to the whole class. A true corpus test needs a frozen
   sample of real `formattedAddress` strings from a cassette/fixture (`docs/audits/032` §fixtures)
   — that data does not exist in this repo yet.
4. **Why a geocoder produced a given coordinate.** Google returning `33.855, 35.533` for a
   Baabda shop is upstream of this function. Testable only by replaying a recorded Places
   response, not by unit-testing `infer_region`.
5. **Whether the geocoder was right about the governorate at all.** The 6-of-19 anchor table is
   a claim about the world; I inherited its coordinates from audit 094 and did not re-derive
   them. Every one of those 6 assertions inherits that uncertainty.
6. **End-to-end region correctness through `main()`.** Needs a frozen master CSV plus stubbed
   scrapers and HTTP (audit 032:665-678). `enrich()` alone is offline-safe only because
   `check_websites` early-returns at `enricher.py:232-233` when no record has a `website` —
   keep that property (there is a test for it above) or every `enrich()` test needs a network mock.

## Recommended order of work

1. Land the **characterization** tests only (box edges, degenerate inputs, precedence order,
   purity/thread-safety, vocabulary parity). All green today, ~15 min, zero risk. This is the
   safety net for everything below.
2. Land the **invariant** property suite (P1, P2, P3, P4, P7). These go red immediately —
   6 shadowed keywords, 5 cross-set duplicates, 108 unbounded keywords, 12 box overlaps, and
   the vocabulary split that audit 094's proposed fix would introduce. Land them *with*
   `xfail(strict=True)` so the suite is honest about what is broken.
3. Fix the **country contract** (`country=None` / casing / aliases) — smallest change, biggest
   correctness win per line, and the only S-class finding in this report whose fix is
   self-contained inside `infer_region`. Then invert the S2 tests.
4. Fix **Nabatieh** (box ordering + de-duplicated keyword sets) and **word boundaries**
   (normalise, *then* `\b` — order matters, see the S1 caveat) and invert those tests.
5. Replace the **anchor and coverage tables** with the audit-094 tables, then invert the
   remaining red tests. Only do this after steps 1-2, otherwise the fix is unreviewable.
6. Move the anchor table out of the test body into `tests/data/lb_anchor_cities.csv` with a
   provenance column, and add the `shapely` polygon test as a separate, opt-in PR.