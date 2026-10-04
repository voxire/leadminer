# 360 — infer_region: correctness

**Target:** `infer_region` — `enricher.py:79-94`
**Lens:** correctness (wrong / surprising outputs per input)

## Verdict

`infer_region` is not "mostly right with rough edges" — it is wrong on **6 of 21
verified Lebanese places**, it can **never** emit one of Lebanon's eight governorates
(`Nabatieh`) from either branch, and its address branch is *actively harmful*: because the
keyword regexes have no word boundaries (`enricher.py:52-55`), a substring false positive
such as `sur` in "Insurance" **overrides coordinates that were already correct**, turning
a right answer into a wrong one. The table-driven overlaps are already documented in
`007` and `094`; what is new here is (a) the address branch out-damaging the coordinate
branch, (b) the two branches of the same function returning different governorates for the
same town, (c) the fact that every one of these wrong answers is **permanent**, because
`enricher.py:331` never re-infers a non-empty `region` and `main.py:150` reloads the
previous run's values out of the cumulative master. Fixing the tables without clearing
the `region` column in `all_businesses.csv` will change nothing.

### How this was verified

`requirements.txt` pins are not installed in this environment and I may not install
them, so I imported the real `enricher` module with two offline stub modules on
`sys.path` (`requests`, `urllib3` — neither performs I/O; `infer_region` uses neither).
Every result below is actual output from the unmodified `enricher.py` in this repo.

Reference coordinates were checked against the English Wikipedia REST summary API and
Nominatim (geocoding only). **No** call was made to Overpass, Wikidata, the Google Places
API, or any business website. Two of my initial coordinate assumptions were wrong and I
dropped them after checking: KAUU is at `21.4934, 39.2485` (inside the Jeddah box, not
outside it) and Kousba is at `34.3017, 35.8528` (correctly North Lebanon, not Akkar).

---

## Findings

### S1 — Address substring false positives override *correct* coordinates, making the address branch worse than no address at all

- **Where:** `enricher.py:52-55` (regex construction — `re.escape` + `"|".join` + `IGNORECASE`, **no `\b`**), `enricher.py:85-88` (address branch runs first and returns), `enricher.py:89-93` (coordinate branch is only reached if no keyword matched).
- **Breaks:** the address branch has no word boundaries, so short and generic tokens match
  inside unrelated words. Because it returns first (`enricher.py:88`), a false positive
  **pre-empts a coordinate answer that was already correct.** The same record is
  classified differently depending on whether the word "Insurance" appears in its address:

  | exact input | returned | truth |
  |---|---|---|
  | `infer_region("Al Qleiat Insurance, Qleiat, Lebanon", None, None)` | `'South Lebanon'` | Mount Lebanon (`sur` ⊂ `Insurance`) |
  | `infer_region("Al Qleiat Trading, Qleiat, Lebanon", 33.9728, 35.7143)` | `'Mount Lebanon'` | Mount Lebanon ✅ |
  | `infer_region("Al Qleiat Insurance, Qleiat, Lebanon", 33.9728, 35.7143)` | `'South Lebanon'` | **Mount Lebanon** — address destroyed a correct coordinate answer |

  Qleiat is a Keserwan (Mount Lebanon) town at `33.9728, 35.7143`. `Insurance`, `Treasury`,
  `Pleasure`, `Leisure`, `Pressure`, `Measure`, `Ensure` all contain `sur`, which is a
  South Lebanon keyword at `enricher.py:34`.

  Two more tokens in the *first* (Beirut) entry hijack the whole country:
  `corniche` (`enricher.py:13`) and `hamra` (`enricher.py:12`) are generic coastal-road /
  neighbourhood names, and Beirut is index 0 of `REGION_KEYWORDS`:

  | exact input | returned | truth |
  |---|---|---|
  | `infer_region("Byblos Corniche, Jbeil, Lebanon", 34.12361, 35.65194)` | `'Beirut'` | Mount Lebanon |
  | `infer_region("Corniche St, Tyre, Lebanon", 33.2704, 35.2038)` | `'Beirut'` | South Lebanon |
  | `infer_region("Al Hamra Restaurant, Jounieh, Lebanon", 33.9808, 35.6178)` | `'Beirut'` | Mount Lebanon |

  The Arabic keywords have no word boundaries either, which makes the bug **script-asymmetric** —
  the same name is mis-detected in Arabic and correctly ignored in Latin:

  | exact input | returned | truth |
  |---|---|---|
  | `infer_region("مقهى منصور، زحلة، لبنان", None, None)` | `'South Lebanon'` | Bekaa (`صور` ⊂ `منصور`) |
  | `infer_region("Cafe Mansour, Zahle, Lebanon", None, None)` | `'Bekaa'` ✅ | Bekaa |
  | `infer_region("منصور، لبنان", None, None)` | `'South Lebanon'` | — (`منصور` = Mansour, one of the most common Arabic given names) |

  This is provable with **no external geography at all**: the module's own table maps
  `زحلة` → `Bekaa` (`enricher.py:43`) and `صور` → `South Lebanon` (`enricher.py:34`), so
  for an address containing both, `Bekaa` is the only self-consistent answer and the
  function returns `South Lebanon`.

- **Trigger:** `infer_region("Al Qleiat Insurance, Qleiat, Lebanon", 33.9728, 35.7143)` → `'South Lebanon'` (≈70 km from Mount Lebanon).
- **Fix:** match tokens on word boundaries (`rf"\b(?:{...})\b"` for the Latin keys) and score *all* matches, returning the most specific / most frequent rather than the first — or, minimally, run the coordinate branch first and let a *confident* address hit override it rather than any hit.

### S1 — First-match rectangles misassign verified Lebanese places, and `Nabatieh` is unreachable from **both** branches

- **Where:** `enricher.py:58-68` (`_LB_COORD_REGIONS`) with the loop at `enricher.py:89-93`; the address path at `enricher.py:38-41` vs `enricher.py:33-37`. (Overlaps previously documented in `007`/`094`; this is the measured error rate and the `Nabatieh` unreachability, which is worse than reported.)
- **Breaks:** executing the real function on 21 reference coordinates taken from Wikipedia:

  | place | (lat, lon) | returned | truth |
  |---|---|---|---|
  | Hazmieh | 33.85111, 35.54139 | `Beirut` | Mount Lebanon |
  | Byblos / Jbeil | 34.12361, 35.65194 | `North Lebanon` | Mount Lebanon |
  | Hermel | 34.39139, 36.39583 | `Akkar` | Baalbek-Hermel |
  | Nabatieh | 33.37722, 35.48361 | `South Lebanon` | Nabatieh |
  | Marjayoun | 33.36194, 35.58972 | `South Lebanon` | Nabatieh |
  | Bint Jbeil | 33.12083, 35.43361 | `South Lebanon` | Nabatieh |

  The other 15 (Beirut, Broummana, Jounieh, Jeita, Batroun, Kousba, Bsharri, Tripoli,
  Halba, Kobayat, Baalbek, Zahle, Chtaura, Sidon, Tyre) are correct. **6/21 ≈ 29% wrong.**

  `Nabatieh` cannot be produced at all:
  - keyword path — `nabatieh`, `النبطية`, `bint jbeil`, `marjayoun`, `hasbaya` all appear in
    the **South Lebanon** list (`enricher.py:35-36`) which precedes `Nabatieh`
    (`enricher.py:38-41`) in `_REGION_MAP` (`enricher.py:52-55`);
  - coordinate path — `Nabatieh`'s box `33.240–33.560, 35.330–35.720` is **fully contained**
    in `South Lebanon`'s `33.040–33.580, 35.090–35.750` (`enricher.py:64-65`), which is
    checked first, so the box is dead code.

  Verified exhaustively: feeding `infer_region(kw, None, None)` for every keyword in
  `REGION_KEYWORDS` yields only 7 distinct regions — `Nabatieh` never appears from the
  address path; a grid scan over Lebanon's bounding box never yields `Nabatieh` either.

  The keyword table and the coordinate table also **contradict each other** for the same
  town, so the function is internally inconsistent regardless of which geography you
  believe:

  | place | from `address=` | from `lat`/`lon` | truth |
  |---|---|---|---|
  | Hazmieh | `Mount Lebanon` | `Beirut` | Mount Lebanon |
  | Byblos / Jbeil | `Mount Lebanon` | `North Lebanon` | Mount Lebanon |
  | Hermel | `Baalbek-Hermel` | `Akkar` | Baalbek-Hermel |
  | Bint Jbeil | `Mount Lebanon` | `South Lebanon` | Nabatieh |

  Bint Jbeil is the worst case — **four** different governorates are asserted about one
  place by one module: `Nabatieh` list (`enricher.py:39`), `South Lebanon` list
  (`enricher.py:35`), `jbeil` ⊂ `bint jbeil` → `Mount Lebanon` (`enricher.py:17`), and the
  coordinate box → `South Lebanon`. `infer_region("Bint Jbeil, Lebanon")` returns
  `'Mount Lebanon'`, ~112 km from Bint Jbeil.

- **Trigger:** `infer_region(None, 34.39139, 36.39583)` → `'Akkar'` (Hermel, Baalbek-Hermel governorate); `infer_region("Bint Jbeil, Lebanon")` → `'Mount Lebanon'`.
- **Fix:** replace the four LB lists with one non-overlapping table keyed by district/municipality name *and* a disjoint-or-explicitly-ordered box set, and assert in a test that each of the 8 governorates is reachable from both branches and that the two branches agree for every keyword that has coordinates.

### S1 — Wrong regions are permanent: `enricher.py:331` never re-infers, so the master CSV accumulates errors forever

- **Where:** `enricher.py:330-335` (`if not r.get("region"):`), `main.py:150` (`load_master(DATA_DIR / "all_businesses.csv")`), `main.py:209` (master rewritten every run), `main.py:187` (`enrich` called after the master is merged).
- **Breaks:** `all_businesses.csv` is cumulative and `load_master` reads `region` back as a
  plain string (`main.py:86-104` — `region` is in neither `_FLOAT_FIELDS` nor `_INT_FIELDS`).
  Because `enrich` only fills a *falsy* region, every row that has ever been mislabelled
  keeps its mislabelling on every subsequent run. Measured:

  ```
  region pre-set to 'Akkar'          -> 'Akkar'            (truth: Baalbek-Hermel)
  region pre-set to 'Baalbek-Hermel' -> 'Baalbek-Hermel'
  region pre-set to None             -> 'Baalbek-Hermel'
  ```

  `scrapers/google_places.py:275` compounds this: it sets `region` at scrape time from
  coordinates alone (`address=None`), so those rows are already non-empty when `enrich`
  sees them and are never re-examined either. Consequence for the fix in `094`: applying
  corrected tables changes **only** rows whose `region` is currently blank. To actually
  repair history you must blank the `region` column first — and, per the next point, you
  must do it **without** `094`'s relabelling of the Eastern Province, or you will churn
  every existing `Dammam` row into `Khobar`/`Dhahran`/`Qatif`.

  Related, in `dedup.py`: `region` has no entry in `_SOURCE_TRUST` (`dedup.py:96-110`) and
  is not in `_VOLATILE` (`dedup.py:114`), so when two sources disagree `_pick`
  (`dedup.py:187`) breaks the tie on `str(value)` — the surviving region is whichever
  string sorts higher, not whichever is right.

- **Trigger:** run the pipeline twice with Hermel in the master. Row stays `Akkar` forever, even after `_LB_COORD_REGIONS` is corrected.
- **Fix:** make region inference idempotent and authoritative — always recompute `region` from `country`/`address`/`lat`/`lon` when coordinates are present, and only honour a pre-existing value that is provably not a lookup result (or store the value in a separate `region_source` column and overwrite `lookup` values unconditionally).

### S2 — Saudi Arabia is five rectangles, and the address branch is disabled for Saudi entirely

- **Where:** `enricher.py:70-76` (`_KSA_COORD_REGIONS`), `enricher.py:85` (`if address and country == "LB":`).
- **Breaks:** measured against real coordinates — every one of these returns `None`:

  `Jubail (26.95, 49.66)`, `Al-Ahsa/Hofuf (25.36, 49.59)`, `Al-Kharj (24.155, 47.334)`,
  `Buraidah (26.359, 43.981)`, `Taif (21.27, 40.42)`, `Abha (18.216, 42.505)`,
  `Tabuk (28.384, 36.555)`, `Al Bahah (20.013, 41.47)`, `Jizan (16.889, 42.551)`,
  `Najran (17.49, 44.13)`, `Hail (27.512, 41.72)`, `Yanbu (24.089, 38.061)`.

  A 0.1° grid over the two-country window that `cli.py:159-162` itself validates as
  in-scope produces a non-`None` Saudi region for **122 of ~34,900 cells (0.35%)**.
  `Khobar`, `Dhahran` and `Qatif` all land inside `Dammam`'s box and are reported as
  `Dammam` (defensible as a metro label, but it means the Eastern Province cannot be
  reported at city granularity at all).

  Compounding: `country == "SA"` skips the address branch entirely, so a Saudi record with
  an address and no coordinates gets `region = None` no matter how explicit the address is:
  `infer_region("King Fahd Rd, Riyadh, Saudi Arabia", None, None, country="SA")` → `None`.
  `scrapers/google_places.py:275` always passes `address=None`, so Google Saudi rows depend
  entirely on the five boxes.

- **Trigger:** `infer_region("King Fahd Road, Riyadh, Saudi Arabia", None, None, "SA")` → `None`; `infer_region(None, 26.95, 49.66, "SA")` → `None`.
- **Fix:** add a `_KSA_REGION_MAP` keyed on the Saudi city names and run the address branch for both countries (`enricher.py:85`), then widen `_KSA_COORD_REGIONS` to at least the Eastern Province cluster and the eight `KSA_QUERIES` metro areas.

### S2 — `country` is compared to exact literals `"LB"` / `"SA"`; every other spelling silently degrades the result

- **Where:** `enricher.py:85` and `enricher.py:90`; `enricher.py:334` passes `country=r.get("country", "LB")`.
- **Breaks:** the default `"LB"` never fires when the key exists with a non-matching value
  (the `dict.get(key, default)` trap), and there is no normalisation. Measured, with the
  address `"Hamra, Beirut"` and Riyadh coordinates `(24.69, 46.686)`:

  | `country` | address path | coordinate path |
  |---|---|---|
  | `"LB"` | `Beirut` | `None` (correct — Riyadh is not in the LB boxes) |
  | `"lb"`, `"LEB"`, `"LBN"`, `"LEBANON"`, `""`, `None` | `None` ❌ | `None` |
  | `"SA"`, `"sa"`, `"SAU"`, `"KSA"`, `None`, `""` | n/a | `None` ❌ (Riyadh) |

  So `country="SAU"` makes a Riyadh business unresolvable, and `country="lb"` silently
  disables address inference. In the shipped `leadminer run` path this is masked because
  `main.py:184` runs `resolve_country` first, which normalises `LEB`/`LBN`/`KSA`/`SAU`/
  `SAU-AR` and falls back to the phone prefix — but `enricher.enrich()` and
  `infer_region()` are the module's public surface (and `scrapers/google_places.py:285`
  writes `country` straight from `_country_from_query`), so nothing at the function
  boundary is safe.

- **Trigger:** `infer_region("Hamra, Beirut", None, None, country="lb")` → `None`.
- **Fix:** normalise at the top of `infer_region` (`code = str(country or "").strip().upper()` then map `LEB/LBN → LB`, `SAU/KSA → SA`) instead of relying on every caller.

### S3 — Unguarded comparisons: non-numeric coordinates and non-`str` addresses raise `TypeError`

- **Where:** `enricher.py:87` (`pattern.search(address)`), `enricher.py:92` (`lat_min <= lat <= lat_max`).
- **Breaks:** `lat`/`lon` are typed `float | None` but never coerced. Measured:

  ```
  infer_region(None, '33.891', '35.501') -> TypeError: '<=' not supported between instances of 'float' and 'str'
  infer_region(None, 33.891, '35.501')  -> TypeError: '<=' not supported between instances of 'float' and 'str'
  infer_region(12345, 33.891, 35.501)   -> TypeError: expected string or bytes-like object, got 'int'
  ```

  **This is latent, not currently firing.** I traced all four call sites: `main.load_master`
  casts `lat`/`lon` to `float` (`main.py:90-95`), `osm.py:68` reads JSON floats,
  `wikidata.py:60-61` calls `float()`, and `google_places.py:273-274` reads JSON floats. The
  address is likewise always a `str` or `None` (`osm.py:87`, `wikidata.py:99`,
  `google_places.py:286`). It is a landmine for any third-party caller and for `cmd_validate`,
  which *does* float-cast raw CSV cells (`cli.py:167`) — so the day someone reuses that
  pattern in `enrich`, the whole run dies rather than degrading.
- **Trigger:** `infer_region(None, "33.891", "35.501")` → `TypeError`.
- **Fix:** `try: lat = float(lat); lon = float(lon) except (TypeError, ValueError): return None` and `if not isinstance(address, str): address = None` at the top of the function.

### S3 — Keyword coverage gaps and one malformed token

- **Where:** `enricher.py:10-50`.
- **Breaks:** address-only records for real places resolve to `None`, which then shows up
  as a blank `region` cell in all five CSVs. Measured misses: `Zahlé` (the accented spelling
  — no Arabic-folding or accent-folding anywhere), `Maaloula`, `Rachana`, `Kfaraabida`,
  `Brummana` (the keyword is spelled `broummana` at `enricher.py:18`, and both spellings are
  in common use), `Bnashmoun`, `Qornayel`, `Haret Hreik`, `Mouallem`, `Faraya`, `Jeita`,
  `Deir al Ahmar` (the keyword at `enricher.py:48` is `deir el ahmar`; the `al` spelling is
  not matched).

  `enricher.py:21` contains `"zahle el metn"` — a malformed token that puts Zahle (a Bekaa
  city) under Mount Lebanon if it ever appears in an address: `infer_region("Zahle El Metn, Lebanon")` → `'Mount Lebanon'`.

  Arabic coverage is very uneven — measured keywords per region:
  `Mount Lebanon` 0 of 35, `Beirut` 1 of 16, `North Lebanon` 1 of 16, `Bekaa` 1 of 10,
  `Akkar` 2 of 8, `Baalbek-Hermel` 2 of 8, `Nabatieh` 2 of 7, `South Lebanon` 3 of 14.
  Mount Lebanon has **no** Arabic keywords at all, so an Arabic-only Mount Lebanon address
  resolves by coordinates alone or not at all.
- **Trigger:** `infer_region("Zahlé, Lebanon", None, None)` → `None`; `infer_region("Maaloula, Lebanon", None, None)` → `None`.
- **Fix:** normalise the address before matching (NFKD + strip combining marks, Arabic `أإآ→ا`, `ة→ه`, `ى→ي`) and replace `enricher.py:21`'s `zahle el metn` with `zalka`.

### S3 — Region inference is split across two layers, so a fix in one does not reach the other's records

- **Where:** `scrapers/google_places.py:26` (an upstream scraper importing a downstream enrichment function), `scrapers/google_places.py:275`; vs `enricher.py:332`.
- **Breaks:** Google rows are decided at scrape time with `address=None` — coordinates only —
  and are therefore invisible to any fix to `REGION_KEYWORDS`. Conversely OSM and Wikidata
  rows emit `region=None` (`osm.py:102`, `wikidata.py:110`) and are decided at enrich time
  by both branches. Two identical businesses from two sources can land in different
  governorates, and the master CSV will keep both.
- **Trigger:** a Hermel business found by Google (`google_places.py:275`) is `Akkar`; the same business found by OSM and re-scored after a keyword-only fix is still `Akkar` (coords wrong) — but a Nabatieh business is `South Lebanon` from Google and can only ever be `South Lebanon`.
- **Fix:** delete the import and the scrape-time call; emit `region=None` and let `enricher.enrich` decide once, for every source, with the address available.

---

## Not a bug, but worth knowing

- **For Lebanon, address always beats coordinates by design** (`enricher.py:85-88` returns
  before the coordinate loop). This is defensible — a street name is more specific than a
  rectangle — but combined with S1 it means a bad address actively destroys a good
  coordinate answer, and a good address can never repair a bad coordinate answer.
- **NaN and infinities degrade safely:** `infer_region(None, float("nan"), 35.5)` and
  `inf` both return `None` (every `x <= nan` comparison is false). No guard needed.
- **Null Island and swapped coordinates degrade safely:** `(0.0, 0.0)` → `None`;
  `lat=35.5, lon=33.5` → `None`. `cli.py:159-162` is the thing that would flag them.
- **The label taxonomy is right.** All eight LB labels (`enricher.py:11-49`) are exactly
  Lebanon's eight governorates, and `_LB_COORD_REGIONS` covers the same eight — the
  *taxonomy* is correct, only the *assignment* is wrong. That is why the fix is a table
  repair rather than a redesign.
- **Jeddah's box is not the problem.** I checked: `lon_max = 39.45` (`enricher.py:72`) sits
  *outside* the city's real eastern extent (`39.33`), so nothing real is clipped there.
  Don't "fix" it.
- **`None` is not distinguishable from "no data".** `main.py:230` renders it as `"Unknown"`
  in the by-region summary and `main.py:209` writes an empty cell, so "we could not place
  this business" and "this business is in a place we never modelled" are the same string.
  Given S2, the second case dominates the Saudi rows.
- **Prior audits overlap with this one.** `007` and `094` already report the rectangle
  overlaps, the dead `Nabatieh` box, the LB-only address gate, and the KSA coverage gap.
  Two things in `094`'s proposed patch are worth flagging before anyone applies it:
  (a) its §C replacement **keeps** `"jbeil"` in the Mount Lebanon list, so
  `infer_region("Bint Jbeil", ...)` still returns `Mount Lebanon`;
  (b) its §D replacement inserts `Dhahran`/`Qatif`/`Khobar` **before** `Dammam`, which will
  relabel every existing `Dammam` row — and per the stickiness finding, those rows will
  never be updated unless `region` is blanked first.

## Recommended order of work

1. **Fix the harm that is active right now** (S1, first bullet): add word boundaries to the
   Latin keyword regex and normalise/deduplicate the Arabic keywords so a false positive
   cannot outrank a correct coordinate answer. Cheapest change in the file, largest effect
   on accuracy, and it is a pure improvement over the current behaviour.
2. **Make region recomputation idempotent** (S1, third bullet) *before* touching the
   tables — otherwise no table fix will ever reach historical rows. Blank `region` for
   coordinate-bearing records on each run, and keep `094`'s `Dammam` ordering unchanged so
   the backfill is a no-op for Eastern Province rows.
3. **Repair the LB tables as one unit** (S1, second bullet): one non-overlapping box list,
   `Nabatieh` pulled out of `South Lebanon` in both the keyword and box tables, `Bint Jbeil`
   removed from the South list, and a test asserting that (a) each of the 8 governorates is
   reachable from both branches, (b) the two branches agree for every keyword that has
   coordinates, and (c) the 21 reference rows above are correct.
4. **Normalise `country` inside `infer_region`** and add a `_KSA_REGION_MAP` for the address
   branch (S2 pair) — one-line guard plus one new table.
5. **Harden the types** (S3, first bullet) and close the keyword-coverage gaps (S3, second
   bullet); delete the scrape-time `infer_region` call in `google_places.py` (S3, third
   bullet) so there is exactly one place where region is decided.