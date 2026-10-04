# 200 — `_extract_city` (dedup.py:72) has zero tests and three silent-data-loss bugs

## Verdict

`_extract_city` is a 5-line function that produces the `city` half of the `(name, city)`
identity key at `dedup.py:214` — the identity model for every record that has no usable
phone — and it has **no tests at all**: none of the 28 tests in
`tests/test_lead_signal.py` touch it (I ran the suite: `Ran 28 tests ... OK`), even though
that file already imports the private `_merge` (`tests/test_lead_signal.py:65`) and audits
003/002/049/067/083/095 all blame this exact function. Three independent input classes
produce wrong keys, and the worst one turns the city discriminator into a **constant per
country**: 8 pharmacies named "City Pharmacy" in 8 different Lebanese cities collapse to
**1 row**. The 15 tests below are what it needs; three of them fail on the current code.

## What the function actually does

```python
def _extract_city(address: str | None) -> str:   # dedup.py:72-76
    if not address:
        return ""
    parts = [p.strip().lower() for p in address.split(",")]
    return parts[-1] if parts else ""
```

Verified behaviour (`python3` against the real function; `dedup.py` imports only `re` and
`unicodedata`, so this runs offline with no dependencies):

| Input | Output | Note |
|---|---|---|
| `"Beirut, Lebanon"` | `"lebanon"` | country, not city |
| `"Hamra Street, Beirut, Lebanon"` | `"lebanon"` | same bucket as every other LB address |
| `"Riyadh, Saudi Arabia"` | `"saudi arabia"` | |
| `"12, Hamra St, Achrafieh, Beirut, Badaro"` | `"badaro"` | district, not city |
| `"12, Hamra St, Beirut"` | `"beirut"` | **right answer, different bucket** |
| `"Tripoli"` | `"tripoli"` | Wikidata's bare label — the only shape that works |
| `"Hamra Main Street"` | `"hamra main street"` | no comma → whole string is the "city" |
| `"Beirut,"` / `","` / `"   "` | `""` | empty city is a valid, shared key |
| `"شارع الحمرا, بيروت, لبنان"` | `"لبنان"` | Arabic ≠ Latin bucket for the same city |
| `"Rue Verdun\nBeirut\nLebanon"` | `"rue verdun\nbeirut\nlebanon"` | newline survives |
| `None` | `""` | |

The last segment is the **country** for Google, the **district** for OSM, and the **city**
only for Wikidata — three producers, three different meanings, one positional guess.

## Findings

### S1-A — The city discriminator is constant per country, so same-named businesses in different cities merge

- **Where:** `dedup.py:72-76` (the `parts[-1]`) consumed at `dedup.py:213-214`
- **Pins:** audit **003** S1 (verbatim trigger, `docs/audits/003-dedup-identity-model.md:19-42`),
  audit **083** item 45 (`docs/audits/083-SYNTH-s1-backlog.md:99`), audit **049** `:127-132`
- **Breaks:** `google_places.py:286` sets `address=place.get("formattedAddress")`, whose
  last comma segment is the country. Every phone-less Google record in Lebanon therefore
  keys `(normalized_name, "lebanon")`. Two genuinely different businesses merge, and
  `_merge` (`dedup.py:190-194`) silently discards the losing record's address, lat and lon.
- **Trigger (verified):** `dedup([{"name":"City Pharmacy","address":"Beirut, Lebanon",
  "phone":None}, ... 7 more cities ...])` → **8 input rows → 1 output row**.
  Two-row version: `{"name":"Zaatar w Zeit","address":"Beirut, Lebanon"}` +
  `{"name":"Zaatar w Zeit","address":"Jounieh, Lebanon"}` → 1 row, Beirut branch lost.
- **Scale argument:** `google_places.py` issues 68 city queries, so same-named chains
  ("City Pharmacy", "Sultan Centre", "Al Baik") are seeded in every one of them.
- **Fix:** stop parsing; carry structured `city` from the producer (`addr:city` for OSM,
  a component off `formattedAddress` for Google) — see S1-C note on why no positional
  rule works.

### S1-B — OSM's last address segment is the *district*, so one business splits into two rows and never converges

- **Where:** `dedup.py:76` + `scrapers/osm.py:80-86`, which orders the join
  `housenumber, street, suburb, city, district`
- **Pins:** audit **095** `:12` ("OSM builds address components in house/street/suburb/city/district
  order ... the last comma component is not a consistently defined city") and audit **017** `:417`
- **Breaks:** adding `addr:district` to a tag **moves the dedup key**. `Badaro` is a Beirut
  neighbourhood, so `"12, Hamra St, Achrafieh, Beirut, Badaro"` keys `("cafe hamra","badaro")`
  while the identical business tagged without a district keys `("cafe hamra","beirut")`.
- **Trigger (verified):** `dedup([{"name":"Cafe Hamra","address":"12, Hamra St, Beirut, Badaro"},
  {"name":"Cafe Hamra","address":"12, Hamra St, Beirut"}])` → **2 rows**.
- **And it is permanent (verified):** because `main.py:150` reloads the master and
  `main.py:179-180` re-dedups `raw + master`, and the split is baked into the stored
  `address`, feeding the output back gives 2 rows, then 2 rows, forever. Re-tagging OSM
  never repairs the master.
- **Downstream:** `main.py` writes 5 CSVs, so a duplicated row is a duplicated sales target.
- **Fix:** `addr:city` must be captured as its own field at `osm.py:80-86` and used as the
  key component; district belongs in `region`, not in the identity key.

### S1-C — `""` is a valid dedup key shared by every blank, whitespace-only and separator-only address — including across countries

- **Where:** `dedup.py:73-74` (`if not address: return ""`) and `dedup.py:214`
  (`key = (normalize_name(name), city)` — an empty `city` is a perfectly good tuple member)
- **Breaks:** `osm.py:87` yields `address=None` for **every** node with no address tags at
  all (`... or None`), and `wikidata.py:99` yields `None` whenever `wdt:P6375` is unbound.
  All of them share the bucket `("name", "")`. A trailing or stray comma produces the same
  bucket, because `parts[-1]` is then `""`.
- **Trigger (verified, cross-country):** `dedup([{"name":"Al Noor Trading",
  "address":"Riyadh,","country":"SA"}, {"name":"Al Noor Trading","address":None,
  "country":"LB"}])` → **1 row**, the Saudi record absorbed, the country discriminator
  destroyed. This is not a dedup near-miss; it is a business in the wrong country in the
  pitch list.
- **Trigger (verified, 180 km apart):** two blank-address `"City Pharmacy"` records at
  `(33.8938, 35.5018)` Beirut and `(34.4367, 35.8497)` Tripoli → **1 row**, Beirut
  coordinates overwritten by Tripoli's.
- **Fix:** if no city can be resolved, do not key on a name tuple at all — fall back to
  `("name", lat, lon)` rounded, or leave the record un-merged and flag it.

### S2-A — No whitespace, newline or Unicode normalization, unlike `normalize_name`

- **Where:** `dedup.py:75` uses `.strip()` only. Compare `dedup.py:66-69`, which does
  NFKD + combining-mark removal + `re.sub(r"\s+", " ", ...)` + `lower()`.
- **Pins:** audit **049** `:126` ("Google is a full multi-line string (which may contain
  newlines and commas)"); audit **058** on script splitting.
- **Trigger (verified):** `_extract_city("Rue Verdun\nBeirut\nLebanon")` →
  `"rue verdun\nbeirut\nlebanon"` — a newline inside a dedup key. And an Arabic address
  keys `"لبنان"` while its Latin twin keys `"lebanon"`, so
  `{"name":"مطعم النور","address":"شارع الحمرا, بيروت, لبنان"}` +
  `{"name":"Al Nour Restaurant","address":"Hamra, Beirut, Lebanon"}` → 2 rows (verified),
  duplicating one business that audit **067** `:155-167` calls the competitive moat.
- **Fix:** reuse `normalize_name`'s pipeline on the city, plus Arabic alef/hamza unification.

### S2-B — No gazetteer or transliteration check, so `beirut` / `beyrouth` / `بیروت` are three different keys

- **Where:** `dedup.py:76`
- **Breaks:** "Beyrouth" is a common French/official rendering and "Beirut" the English one;
  both appear in `addr:city` in the wild. `_extract_city("Beyrouth")` → `"beyrouth"` and
  `_extract_city("Beirut")` → `"beirut"` — no merge, and no way to tell a typo from a real
  second city.
- **Note:** this one is **not testable as a correctness test until a gazetteer exists**.
  See "Cannot be tested here".

### S3-A — `if parts else ""` is dead code

- **Where:** `dedup.py:76`. `str.split(",")` always returns at least one element —
  verified: `",".split(",") == ['', '']`. The guard can never fire and hides the fact that
  the empty-string return path is reachable by two *different* routes (`dedup.py:74` and
  `dedup.py:76`). Delete it.

### S3-B — The `str | None` annotation is not enforced; a non-`str` dies the run

- **Where:** `dedup.py:72` vs the call at `dedup.py:213`, which is `record.get("address")`
  and therefore `Any`, not `str | None`.
- **Trigger (verified):** `_extract_city(123)` → `AttributeError: 'int' object has no
  attribute 'split'`.
- **Currently unreachable:** `scrapers/base.py:8` types it `str | None`, and
  `main.py:86-104` never casts `address` (it only casts `_FLOAT_FIELDS`/`_INT_FIELDS` at
  `main.py:43-44`), so CSV rows stay `str | None`. The `AttributeError` risk is entirely
  hypothetical today. Worth one `assertRaises`-free type-guard test at most.

## The tests this function still needs

Add to `tests/test_lead_signal.py`. Importing the private name is consistent with the
existing `from dedup import _merge, normalize_phone` at `tests/test_lead_signal.py:65`.
No new dependencies: `dedup.py` imports only `re` and `unicodedata`, and the suite is
stdlib-`unittest` by design (`tests/test_lead_signal.py:4-6`).

Marked **FAILS NOW** = red against `dedup.py:72-76` as written. They are written against the
**contract**, not against `parts[-1]`, so they constrain a fix rather than re-encoding the bug.

### Properties (preferred — these generalize, and one of them is 100% red)

| # | Method | Literal input | Asserted | Pins |
|---|---|---|---|---|
| P1 | `test_extract_city_is_idempotent` | every address in a shared `ADDRESSES` table | `_extract_city(_extract_city(a)) == _extract_city(a)` | guards against a future fix that re-emits a multi-segment value |
| P2 | `test_extract_city_ignores_case_and_outer_whitespace` | `a`, `a.upper()`, `"  "+a+"  "`, `" "+a+"\t"` for each `a` | all four equal `_extract_city(a)` | locks in the `.strip().lower()` half of `dedup.py:75` |
| P3 | `test_extract_city_recovers_the_city_for_every_known_lebanon_city` | `f"Cafe, {city}, Lebanon"` for each `city` in `enricher.REGION_KEYWORDS` (`enricher.py:10-50`) | result == `city.lower()`, **not** `"lebanon"` | **FAILS NOW: 114 of 114.** audit **003** `:19-42`, **049** `:127-132` |
| P4 | `test_extract_city_output_has_no_newline_or_run_of_spaces` | each `a` in `ADDRESSES`, incl. `"Rue Verdun\nBeirut\nLebanon"` | `"\n" not in r` and `"  " not in r` | **FAILS NOW** for the multi-line address. audit **049** `:126` |
| P5 | `test_extract_city_is_empty_exactly_when_the_address_is` | `None`, `""`, `"   "` → `""`; `"Beirut,"`, `"Beirut, "`, `","`, `",,,"` → `"beirut"` | — | **FAILS NOW** on all four separator cases. S1-C |
| P6 | `test_blank_address_records_in_different_countries_do_not_merge` | `dedup([{...,"name":"Al Noor Trading","address":"Riyadh,","country":"SA"}, {...,"name":"Al Noor Trading","address":None,"country":"LB"}])` | `len(out) == 2` | **FAILS NOW** (returns 1). S1-C, audit **003** `:57-74` |
| P7 | `test_same_name_in_different_cities_never_shares_a_key` | `dedup([{...,"name":"City Pharmacy","phone":None,"address":f"{c}, Lebanon"} for c in 8 cities])` | `len(out) == 8` | **FAILS NOW** (returns 1). S1-A |
| P8 | `test_one_business_keyed_with_and_without_a_district_stays_one_row` | `dedup([{"name":"Cafe Hamra","address":"12, Hamra St, Beirut, Badaro"}, {"name":"Cafe Hamra","address":"12, Hamra St, Beirut"}])` | `len(out) == 1` | **FAILS NOW** (returns 2). S1-B, audit **095** `:12` |
| P9 | `test_district_tag_appears_or_disappears_across_runs_does_not_duplicate` | `dedup(run1 + [the district-tagged record])` where `run1` is the P8 output | `len(out) == 1` | **FAILS NOW**; pins master convergence, `main.py:150`+`179-180` |
| P10 | `test_blank_address_records_keep_their_own_coordinates` | two blank-address records at `(33.8938,35.5018)` and `(34.4367,35.8497)` | `len(out) == 2` | **FAILS NOW** (returns 1). S1-C |
| P11 | `test_no_non_ascii_key_collides_with_its_latin_twin` | `dedup` of `("مطعم النور","شارع الحمرا, بيروت, لبنان")` + `("Al Nour Restaurant","Hamra, Beirut, Lebanon")` | **documented as `len(out) == 2` today**, with a `# TODO` | pins the *current* under-merge so the Arabic gap stays visible after S1-A/B are fixed. audit **067** `:155-167` |

Concrete `ADDRESSES` table for P1/P2/P4 — one entry per producer shape, taken from the
source rather than invented:

```python
ADDRESSES = [
    "Hamra Street, Beirut, Lebanon",          # google_places.py:286 formattedAddress
    "King Fahd Road, Al Andalus, Jeddah, Saudi Arabia",
    "12, Hamra St, Achrafieh, Beirut, Badaro",  # osm.py:87, addr:district present
    "12, Hamra St, Beirut",                     # osm.py:87, no addr:district
    "12, Hamra Street",                        # osm.py:87, street-only
    "Tripoli",                                 # wikidata.py:99 bare wdt:P6375 label
    "شارع الحمرا, بيروت, لبنان",
    "Rue Verdun\nBeirut\nLebanon",
]
```

P3 needs `enricher` imported; `tests/test_lead_signal.py:23-63` already stubs `requests`
and `urllib3`, so `from enricher import REGION_KEYWORDS` is free. Note P3 must skip
multi-word keywords (`"ras beirut"`, `"mar mikhael"`) — with a naive last-segment rule
those come back as `"beirut"`/`"mar mikhael"` inconsistently, which is itself worth an
explicit assertion rather than a skip.

### Examples (no property exists — these are shape-specific)

| # | Method | Literal input | Asserted output | Pins |
|---|---|---|---|---|
| E1 | `test_extract_city_trailing_comma_does_not_erase_the_city` | `"Beirut,"` | `"beirut"` (today `""`) | S1-C; `dict.get`-style falsy trap, same family as `dedup.py:82-89` |
| E2 | `test_extract_city_prefers_city_over_district_for_osm` | `"12, Hamra St, Achrafieh, Beirut, Badaro"` | `"beirut"` (today `"badaro"`) | S1-B, audit **095** `:12` |
| E3 | `test_extract_city_multiline_google_address_yields_one_city` | `"Rue Verdun\nBeirut\nLebanon"` | `"beirut"` (today the whole string) | audit **049** `:126` |
| E4 | `test_extract_city_arabic_country_segment_does_not_become_the_city` | `"شارع الحمرا, بيروت, لبنان"` | `"بيروت"` (today `"لبنان"`) | S1-A applied to Arabic; audit **067** `:163` |
| E5 | `test_extract_city_preserves_the_only_working_shape` | `"Tripoli"` | `"tripoli"` | **passes today** — pins the Wikidata case so a fix does not regress it |
| E6 | `test_extract_city_separatorless_address_is_not_empty` | `"Hamra Main Street"` | non-empty, no commas (today `"hamra main street"`) | audit **017** `:417` calls this behaviour "strictly superior to returning `""`" — pin it as intentional, then tighten |
| E7 | `test_extract_city_rejects_a_postcode_as_the_last_segment` | `"Hamra, Beirut, Lebanon 1107"` | `"beirut"` (today `"lebanon 1107"`) | **lower confidence** — Google's usual ordering puts the postcode *before* the country (`"…, Beirut, 1107, Lebanon"` → `"lebanon"`, unchanged). Keep only if a recorded fixture shows a trailing postcode. |
| E8 | `test_extract_city_ignores_non_string_address` | `123`, `0` | `""` / no exception | S3-B, defensive only |

## Cannot be tested here, and why

1. **That Google really returns the country last.** The S1-A finding rests on
   `formattedAddress` conventions, and the brief forbids calling the Places API. Fix: commit
   one recorded API response as a fixture (`tests/fixtures/google_place_sample.json`) and
   assert `parts[-1]` of *that* payload; then P3's `f"Cafe, {city}, Lebanon"` string becomes
   a real input rather than a hand-written one. Until then S1-A is confirmed for OSM/Wikidata
   and argued for Google.
2. **Whether two same-named businesses are actually distinct.** Requires ground-truth POI
   data per city. P7 asserts the *invariant* (distinct city ⇒ distinct key), not the
   *business truth*, and that is the most this repo can honestly assert.
3. **S2-B transliteration/tatweel/hamza equivalence.** There is no city gazetteer to test
   against. Writing `assert _extract_city("Beyrouth") == "beirut"` would encode a guess.
   Build the gazetteer first (audit **095** `:57`; `enricher.REGION_KEYWORDS` at
   `enricher.py:10-50` is a usable seed — it already carries both Arabic and Latin forms),
   then P3 becomes a real conformance test and transliteration cases can join it.
4. **Real-world frequency of the malformed inputs** (trailing commas, postcode-last,
   multiline). Needs the accumulated `all_businesses.csv`, which is gitignored
   (`main.py:41` writes into `data/`). Only P1/P2/P4's `ADDRESSES` table is assertable now;
   the rest should be gated on a distribution check over one real master.
5. **Region-level, not city-level, correctness.** `enricher.infer_region` is tested
   (`tests/test_lead_signal.py:147`) and could be the oracle for a *region*-keyed dedup
   test — but that requires changing the key from city to region first, i.e. it is not a
   test for the current code.

## Not a bug, but worth knowing

- **No positional rule can fix this, and audit 003's proposed fix is wrong.** `003:43-45`
  and `083:99` both suggest "take the second-to-last segment". Verified against all five
  real producer shapes:

  | Shape | `parts[-1]` (now) | `parts[-2]` (003's fix) | correct |
  |---|---|---|---|
  | `"Hamra Street, Beirut, Lebanon"` | `lebanon` | `beirut` ✅ | `beirut` |
  | `"12, Hamra St, Achrafieh, Beirut, Badaro"` | `badaro` | `beirut` ✅ | `beirut` |
  | `"12, Hamra St, Beirut"` | `beirut` ✅ | `hamra st` ❌ | `beirut` |
  | `"12, Hamra Street"` | `hamra street` | `12` ❌ | `beirut` |
  | `"Tripoli"` | `tripoli` ✅ | `""` ❌ | `tripoli` |

  `parts[-2]` fixes Google and district-tagged OSM while breaking the two other OSM shapes
  and Wikidata. Write the tests against the contract (P3/P8) so any fix has to satisfy all
  three producers; do not test `parts[-2]`.
- **`dedup.py` is the only module with no import-time dependency**, which is why it can be
  tested without the `requests` stub dance at `tests/test_lead_signal.py:23-63`. Put these
  tests in a new `tests/test_dedup.py` rather than growing `test_lead_signal.py`, which is
  scoped to two named past bugs by its own docstring (`tests/test_lead_signal.py:1-10`).
- **Line numbers in the audit corpus are stale.** `003:21`, `067:157`, `049:128` and
  `031:90` all cite `dedup.py:34-38` for this function; it is now `dedup.py:72-76`.
  `017:417` and `067:163` still cite `34-38` inline in prose. Not worth a code change —
  worth knowing before someone "fixes line 38".

## Recommended order of work

1. **Write P3, P7, P8 first.** They are the three red properties, each is 3 lines, and they
   fail on the current code for the documented reason — so they are unambiguous proof the
   bug is live, not a test that was written to pass.
2. **Then P5, P6, P10** (the empty-key family) — one root cause, and S1-C is the only
   finding that can merge a Saudi record into a Lebanese row.
3. **Then P4/P9** — real but lower-blast.
4. **Then E1-E8**, dropping E7 unless a fixture justifies it.
5. **Do not** attempt to make E5 or S2-B pass until a gazetteer exists.
6. Fix order in the source, once the red tests are committed: capture structured `city`
   per producer (`osm.py:80-86` needs `addr:city` hoisted out of the join) →
   make `""` an invalid key → `normalize_name` the city component. The red tests are the
   acceptance criteria.