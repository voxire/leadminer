# 361 — `infer_region` edge & hostile-input review

**Target:** `infer_region` — `enricher.py:79-94`
**Lens:** edge — boundary, boundary-adjacent and hostile inputs

All results below were produced by loading the *actual* literals and the *actual*
`infer_region` body out of `enricher.py` via `ast` (no retyping, no importing
`enricher` because `requests` is absent, and **no network / no scraper run**), then
calling it. Every "→" value is observed output, not inference.

---

## Verdict

**`infer_region` cannot be made to crash from any input the pipeline can actually
produce — that is the good news, and it is worth saying plainly.** Every falsy value,
`None`, empty string, whitespace, `0`, negatives, `NaN`, `±inf`, swapped coordinates,
`bool`, a 5 MB string and every wrong `country` returns `None` and lets the run finish.
I tried hard to kill it and could not.

**The real damage is the opposite of a crash: it returns a confident, wrong region name
for real businesses, and the two lookup tables in the same file contradict each other.**
`enricher.py:38-41` (Nabatieh keywords) and `enricher.py:65` (Nabatieh box) are each fully
contained in the South Lebanon entries just above them, which are checked first at
`enricher.py:86-88` and `enricher.py:91-93`. **The string `"Nabatieh"` is unreachable —
the function cannot return it for any input.** Meanwhile `enricher.py:59` comments
`# (region, ...) — most-specific first`, and that is **false** for the list it describes.

Blast radius, stated honestly: `region` does **not** feed `lead_score` or
`recommend_service` (`enricher.py:283-319`, `pitch_recommender.py` — grep shows zero
reads of `region` there). It is a segmentation/reporting column only, consumed by
`main.py:228-234` and `cli.py:123-124`. So a wrong region never corrupts scoring or
routing — it corrupts the map a human uses to decide where to sell, and it corrupts a
shipped CSV. That is why the top finding is S1 and not "pipeline destroyed".

---

## Call sites (all of them — only two)

| Site | Call | Reaches the address branch? |
|---|---|---|
| `enricher.py:332` | `infer_region(r.get("address"), ...)` from `enrich()`, called at `main.py:187` | yes |
| `google_places.py:275` | `infer_region(None, lat, lon, country=country)` | **no — address is discarded** |

`google_places.py:275` is the amplifier; see S1.

---

## Findings

### S1 — The two region tables contradict each other; `infer_region` cannot return `"Nabatieh"`, and 42% of Mount Lebanon is sold as a neighbour

- **Where:** `enricher.py:59` (false comment), `enricher.py:33-41` (South Lebanon vs
  Nabatieh keywords), `enricher.py:63` vs `:67` (Bekaa vs Mount Lebanon boxes),
  resolved by the first-match-wins loops at `enricher.py:86-88` and `enricher.py:91-93`.

- **Breaks — part A, a dead region.** `Nabatieh`'s keyword list (`enricher.py:38-41`) is a
  strict subset of South Lebanon's (`enricher.py:33-37`), and South Lebanon is iterated
  first. All five Nabatieh keywords are therefore dead:
  `'nabatieh'`, `'النبطية'`, `'bint jbeil'`, `'hasbaya'`, `'marjayoun'`.
  Its coordinate box (`enricher.py:65`, lat 33.240–33.560 / lon 35.330–35.720) is
  *entirely contained* in South Lebanon's box (`enricher.py:64`, lat 33.040–33.580 /
  lon 35.090–35.750), which is checked first. Measured on a 0.001° grid: **100.00% of the
  Nabatieh box is unreachable.** Both paths, verified:
  - `infer_region("Marjayoun, Lebanon", None, None, "LB")` → `"South Lebanon"`
  - `infer_region(None, 33.3789, 35.5040, "LB")` → `"South Lebanon"` *(Nabatieh city)*

- **Breaks — part B, mislabelling that is silently frozen into the CSV.** Because
  `enricher.py:91-93` returns on the first hit, any point claimed by an earlier box can
  never be labelled by a later one. Union area (0.001° grid, no double counting):

  | box | unreachable share |
  |---|---|
  | `Nabatieh` (`enricher.py:65`) | **100.00%** |
  | `Mount Lebanon` (`enricher.py:67`) | **42.40%** |
  | `North Lebanon` (`enricher.py:66`) | 39.80% |
  | `Baalbek-Hermel` (`enricher.py:62`) | 34.63% |
  | `Bekaa` (`enricher.py:63`) | 14.91% |
  | Beirut / Akkar / South Lebanon | 0.00% |

  `Bekaa` alone steals **34.48%** of the Mount Lebanon box: lat 33.540–34.120 ×
  lon 35.750–35.950. That band is the Chouf / Bhamdoun / Kfarshima corridor.

- **Trigger — the same place gets two different answers.** Of the 12 places named
  *verbatim* in the Mount Lebanon keyword list (`enricher.py:16-23`), **5 return a
  different region from their coordinates than from their own name**:

  | place (keyword says Mount Lebanon) | via coords | via keyword |
  |---|---|---|
  | `damour` (33.5436, 35.5362) | **South Lebanon** | Mount Lebanon |
  | `jiyeh` (33.4013, 35.5810) | **South Lebanon** | Mount Lebanon |
  | `bhamdoun` (33.8369, 35.8322) | **Bekaa** | Mount Lebanon |
  | `sin el fil` (33.8900, 35.5400) | **Beirut** | Mount Lebanon |
  | `majdal el bakhmout` (33.8600, 35.5100) | **Beirut** | *(not in list)* `None` |

  Airtight single-call demonstration:
  ```
  infer_region(None,                    33.8369, 35.8322, "LB")  ->  "Bekaa"
  infer_region("Bhamdoun, Chouf, Lebanon", 33.8369, 35.8322, "LB")  ->  "Mount Lebanon"
  ```
  Same coordinates, same country, same business. The file contradicts itself: `enricher.py:18`
  lists `"chouf"`, `:20` lists `"bhamdoun"` and `"damour"`/`"jiyeh"`, `:21` lists
  `"deir el qamar"` — all Mount Lebanon.

- **Why it persists into the export.** `google_places.py:275` calls
  `infer_region(None, lat, lon, country=country)` — **the address is deliberately passed as
  `None`** — and stores the result in `region` at `google_places.py:284`, while
  `place.get("formattedAddress")` sits unread in the very same record
  (`google_places.py:286`). Then `enricher.py:331` does `if not r.get("region")`, which is
  already truthy, so the correct keyword verdict is never computed. Traced end-to-end:
  ```
  after google_places.py:275      -> region='Bekaa'
  after enricher.py:331 guard     -> region='Bekaa'   (not re-derived)
  truth from the record's address -> 'Mount Lebanon'
  ```

- **Fix:** make one table authoritative. Delete the five duplicated entries from
  `enricher.py:33-37` (or delete the Nabatieh rows entirely), reorder `_LB_COORD_REGIONS`
  so no box is contained in a later one, **delete the false comment at `enricher.py:59`**,
  and pass `place.get("formattedAddress")` at `google_places.py:275` instead of `None`.
  Add a regression test that asserts, for every place named in `REGION_KEYWORDS`, that
  the keyword path and the coordinate path agree.

---

### S2 — `country == "LB"` is an exact, case-sensitive gate: any other value silently voids all address inference

- **Where:** `enricher.py:85` (`if address and country == "LB":`), `enricher.py:90`.

- **Breaks:** the entire keyword branch is skipped for any `country` that is not the exact
  literal `"LB"` — including `None`, `""`, `"lb"`, `"LB "`, `"Lebanon"`, `"LEB"`,
  `b"LB"`, and `0`. The annotation at `enricher.py:80-84` is
  `country: str = "LB"` with no documented enum, so nothing warns the caller.

- **Trigger:** address-only record, no coordinates, unambiguous place name:
  ```
  infer_region("Zahle, Lebanon",  None, None, country="LB")   ->  "Bekaa"
  infer_region("Zahle, Lebanon",  None, None, country=None)   ->  None
  infer_region("Zahle, Lebanon",  None, None, country="lb")   ->  None
  infer_region("Zahle, Lebanon",  None, None, country="LEB")  ->  None
  infer_region("Zahle, Lebanon",  None, None, country="SA")   ->  None
  ```
  Same for `"Byblos"`, `"Jounieh"`, `"Baalbek"`, `"Akkar"` — all lose their region.

- **Reachability, stated precisely:** `main.py:183-184` runs `resolve_country()` first,
  which normalises to `"LB"`/`"SA"`, so **`main()` is safe today**, and
  `tests/test_lead_signal.py:147` pins that. The sharp edge is still live in the callee:
  `enrich()` is public, `enricher.py:334` passes `country=r.get("country", "LB")` — the
  exact idiom `main.py:52-57` documents as the original bug, still default-guarded rather
  than value-guarded — and `google_places.py:275` calls `infer_region` directly with the
  output of `_country_from_query` (`google_places.py:127-136`), a query-string heuristic
  that defaults to `"LB"` for anything unrecognised. Any new caller reintroduces the bug.

- **Fix:** normalise inside the function, not at the call sites —
  `c = (country or "LB").strip().upper()` and accept `"LB"/"LEB"/"LBN"/"LEBANON"` →
  LB, `"SA"/"SAU"/"KSA"/"SAUDI"` → SA. Then `enricher.py:334` becomes
  `country=r.get("country") or "LB"`.

---

### S2 — No Unicode normalisation: valid French- and Arabic-spelled addresses return `None`

- **Where:** `enricher.py:53` (`re.escape(k)` joined with `|`, `re.IGNORECASE` only).

- **Breaks:** `re.IGNORECASE` folds case but does **not** equate `e` with `é` or strip
  combining marks, so a single diacritic flips a confident correct answer to `None`.
  This matters because `dedup.normalize_name` (`dedup.py:67-69`) *does* NFKD-normalise, so
  the project already knows the technique — it just isn't applied here.

- **Trigger — French orthography (Lebanon's second administrative language):**
  ```
  "Zahle"          -> "Bekaa"          "Zahlé"          -> None
  "Aley"           -> "Mount Lebanon"  "Alé"            -> None
  "Aaley"          -> "Mount Lebanon"  "Aaléy"          -> None
  "Jbeil"          -> "Mount Lebanon"  "Jbéil"          -> None
  "Deir el Qamar"  -> "Mount Lebanon"  "Déir el Qamar"  -> None
  "Zgharta"        -> "North Lebanon"  "Zghartá"        -> None
  "Bte Jbéil"      -> None
  ```
  Every one of these is a place already in the table — only the accent differs.

- **Trigger — Arabic orthography.** Clean Arabic works (`"بيروت، لبنان"` → `"Beirut"`,
  `"طرابلس، لبنان"` → `"North Lebanon"`). But bidi control characters, which are routine
  in real Arabic text copied from spreadsheets, Word and mixed-direction strings — and
  this project explicitly targets Arabic consumers (`main.py:118-119` writes `utf-8-sig`
  "so … Arabic business names" render in Excel/Sheets) — break the literal match:
  ```
  "بيروت"                       -> "Beirut"
  "بي\u200fر\u200fوت"  (RLM)     -> None
  "بي\u200cرو\u200cت"    (ZWNJ)    -> None
  "بَيْرُوْت"            (harakat)  -> None
  ```

- **Fix:** normalise both sides before matching — `unicodedata.normalize("NFKC", s)`,
  `unicodedata.category(c) == "Cf"` stripping for U+200E/U+200F/U+200C/U+200D/U+0640, then
  `casefold()`. Build the pattern from the normalised keywords.

---

### S2 — Keyword patterns have no word boundaries; `"sur"` matches inside ordinary words

- **Where:** `enricher.py:53`. Patterns are bare escaped literals joined by `|` with **no
  `\b`** and no anchoring. The worst offender is `"sur"` at `enricher.py:34` (South
  Lebanon) — a 3-letter English/French word fragment.

- **Breaks:** any address containing `sur` anywhere is labelled **South Lebanon**,
  silently, with no keyword of its own present.

- **Trigger:**
  ```
  "Assurance Co"       -> "South Lebanon"     (contains "sur")
  "Pressure Wash"      -> "South Lebanon"
  "Measure Clinic"     -> "South Lebanon"
  "insure" / "treasure"-> "South Lebanon"
  "Sursock"            -> "South Lebanon"     (a real Beirut neighbourhood)
  "Ras Sursock"        -> "South Lebanon"
  "Ain es-Sursock"     -> "South Lebanon"
  "Sour el Jezzine"    -> "South Lebanon"   (correct here, by luck)
  ```
  Reachability is data-dependent rather than certain: `address` is built from free-form,
  human-entered OSM `addr:*` tags (`osm.py:80-87`: `addr:street`, `addr:suburb`,
  `addr:city`, `addr:district`) and from Google `formattedAddress`
  (`google_places.py:286`). Note the country gate at `enricher.py:85` limits this to
  LB records — Saudi records never reach the keyword branch.

- **Fix:** wrap every keyword in `\b(?:...)\b` (`re.compile(r"\b(?:" + "|".join(...) + r")\b", ...)`)
  and drop `"sur"`/`"qaa"` in favour of whole-word forms (`"sour"`, `"al qaa"`).

---

### S3 — `enricher.py:334` still uses the `r.get("country", "LB")` idiom that `main.py:52-57` documents as the bug

- **Where:** `enricher.py:334` — `country=r.get("country", "LB")`.
- **Breaks:** a `None` or `""` country silently becomes a wrong keyword branch, per S2.
  The default only fires for a *missing* key, never for a present-but-empty one — which is
  precisely the failure `main.py:52-57` documents having shipped. The guard was added in
  the caller, never in the callee, so the defect is one refactor away from returning.
- **Trigger:** `enrich([{"address": "Zahle, Lebanon", "lat": None, "lon": None, "country": None}])`
  → `region=None` where `"LB"` would give `"Bekaa"`.
- **Fix:** `country=r.get("country") or "LB"` at minimum; see S2 for the real fix.

---

### S3 — `country == "SA"` skips the keyword branch entirely (latent trap)

- **Where:** `enricher.py:85`.
- **Breaks:** the address branch requires `country == "LB"`, so Saudi records are never
  keyword-matched. This is currently harmless — `REGION_KEYWORDS` (`enricher.py:10-50`)
  contains no Saudi entries at all. It is a trap, not a crash: the obvious next change
  ("let's add Saudi neighbourhood keywords") would silently never fire for `SA`, and
  `infer_region("Olaya, Riyadh", None, None, "SA")` would keep returning `None`.
- **Trigger:** `infer_region("Riyadh, Saudi Arabia", None, None, "SA")` → `None`.
- **Fix:** gate on "is this an LB keyword list?" rather than on the literal `"LB"` —
  iterate the keyword table for whichever country it belongs to.

---

### S3 — No length cap on `address`: linear, not catastrophic, but with a punishing constant

- **Where:** `enricher.py:87` runs 8 full-string scans per record, in the
  single-threaded loop at `enricher.py:330-335`.

- **Ruling out ReDoS first:** all 114 keywords are `re.escape`d literals with no
  backreferences, nested quantifiers or alternation overlap, so matching is **linear** —
  there is no catastrophic backtracking here. Measured, no-match strings:
  10 KB → 13 ms, 100 KB → 167 ms, 1 MB → 2.06 s, 5 MB → 13.7 s.
  A *matching* string short-circuits immediately (5 MB → 0.01 ms), so only the
  no-match path is slow.

- **Not reachable today:** Google `formattedAddress` is ~200 chars and OSM tag values are
  bounded, so realistic cost is 0.4 µs/record — 0.19 s for 500k records, negligible.

- **Fix:** skip the keyword pass when `len(address) > 2000` and fall through to
  coordinates. Cheap insurance for a future source that can emit a large blob.

---

### S3 — Contract-violating types raise an unhelpful `TypeError` (no reachable path today)

- **Where:** `enricher.py:87` (`pattern.search`) and `enricher.py:92` (`lat_min <= lat`).

- **Trigger, all observed:**
  ```
  address=123         -> TypeError: expected string or bytes-like object, got 'int'
  address=1.5         -> TypeError: expected string or bytes-like object, got 'float'
  address=b"Beirut"   -> TypeError: cannot use a string pattern on a bytes-like object
  address={"a":1}     -> TypeError: expected string or bytes-like object, got 'dict'
  address=["beirut"]  -> TypeError: expected string or bytes-like object, got 'list'
  lat="33.89"         -> TypeError: '<=' not supported between instances of 'float' and 'str'
  lat="NaN"           -> TypeError: '<=' not supported between instances of 'float' and 'str'
  ```

- **Reachability: none, verified.** `address` is built by `", ".join(...)` from OSM tag
  strings (`osm.py:80-87`), read from JSON as a string (`google_places.py:286`,
  `wikidata.py:99`), or read from CSV as a string (`main.py:86`). `lat`/`lon` are floats
  from JSON (`osm.py:68-69`, `google_places.py:273-274`), floats from `_parse_point`
  (`wikidata.py:54-62`), or floats via `load_master`'s guarded cast (`main.py:90-95`).
  **Not reachable — S3, not S1.** It is listed because the function is public API and the
  annotation is the only thing preventing the crash.

- **Fix:** `if isinstance(address, str) and address and ...` and
  `if not isinstance(lat, (int, float)) ...` — three lines, removes the whole class.

---

## The single most dangerous input

> **`infer_region(None, 33.8369, 35.8322, "LB")` — a valid Lebanese coordinate pair with
> no address — which returns `"Bekaa"` for Bhamdoun, a town the same file explicitly
> files under Mount Lebanon at `enricher.py:20`.**

It is the most dangerous because of *what makes it dangerous*, not the wrongness itself:

1. **Both inputs are individually perfect.** A real coordinate, a valid ISO country code,
   no malformed data. Nothing in a data-quality check flags it.
2. **It is the *only* form the highest-volume source ever uses.** `google_places.py:275`
   passes `address=None` for every single record it fetches, so this is not one bad input —
   it is the normal shape of the main data path.
3. **It self-conceals.** The answer is a real region name in a plausible-looking
   vocabulary. There is no `None`, no exception, nothing to grep for.
4. **It is frozen before it can be corrected.** `enricher.py:331` skips any record that
   already has a region, so the same record's own `formattedAddress`
   (`google_places.py:286`) never gets a second opinion. Verified above: `Bekaa` survives
   into the export while the address would have yielded `Mount Lebanon`.
5. **It generalises to 42% of Mount Lebanon** and **100% of Nabatieh** — not one bad row,
   a systematic mislabel of a whole corridor and the disappearance of an entire governorate.

Every crash-class input I could construct is *less* dangerous than this one, because each
of those either cannot occur or produces a visible `None`.

---

## Not a bug, but worth knowing

- **Zero reachable crashes.** Every falsy/boundary input is handled: `None` → `None`;
  `""`, `"   \t\n "`, `0` → `None`; `lat=lon=0.0` and `lat=lon=0` → `None` (the
  `is not None` guards at `enricher.py:89` correctly distinguish zero from absent);
  negative `(-33.88, -35.50)` → `None`; `lat` present with `lon=None` → `None`; `True`
  as a coordinate → `None`. Genuinely well-behaved.
- **`NaN` and `±inf` are safe**, and this is a real trap that *did not* fire. NaN makes
  every `<=` comparison at `enricher.py:92` `False`, so the whole `and` chain fails and
  the function returns `None`. Both survive `load_master`'s `float()` cast
  (`main.py:90-95`) and so can genuinely arrive from a master CSV — handled correctly.
- **Cross-country coordinates fail safe.** A Riyadh coordinate with `country="LB"` →
  `None`; a Beirut coordinate with `country="SA"` → `None`. The `"SA"`-vs-everything-else
  fallback at `enricher.py:90` never produces a *confidently wrong* answer, only `None`.
  This is good behaviour and should be preserved by any rewrite.
- **Swapped lat/lon fails safe.** `infer_region(None, 35.5018, 33.8938, "LB")` → `None`,
  not a mislabel. Relevant because `_parse_point` (`wikidata.py:54-62`) parses
  `"Point(35.50 33.89)"` by fixed slicing.
- **The KSA box table is correct.** All five `_KSA_COORD_REGIONS` (`enricher.py:70-76`)
  measure **0.00%** unreachable — no overlaps at all (Jeddah's lon max 39.45 is below
  Mecca's lon min 39.75). The defect is confined to the LB table. A regression test can
  lock KSA in as-is and force only the LB list to change.
- **Box edges are inclusive** (`<=` at `enricher.py:92`), and the discontinuity at a box
  border is sharp: `33.920` → `"Beirut"`, `33.9200001` → `"Mount Lebanon"`. Inherent to
  rectangle geofencing; worth knowing when a lead flips region between two runs because
  the provider nudged a coordinate by a metre.
- **Blank master cells become `None`, not `""`** (`main.py:87-89`), so the `if address`
  falsiness check at `enricher.py:85` behaves as intended for reloaded records.
- **Adjacent, not mine:** `dedup._pick` has no entry for `country` in `_SOURCE_TRUST`
  (`dedup.py:97-110`), so `_validity` returns `1` for both sides and the final
  `str(value)` tiebreak at `dedup.py:184-187` makes `"SA"` beat `"LB"` purely because
  `"S" > "L"`. A merged cross-border record can therefore reach `infer_region` with a
  `country` that is wrong for its address — compounding S2. Worth a separate ticket.

---

## Recommended order of work

1. **S1** — reconcile the two LB tables: drop the duplicated Nabatieh keywords from
   `enricher.py:33-37`, reorder `_LB_COORD_REGIONS` so no box is contained in a later one,
   delete the false `# most-specific first` comment at `enricher.py:59`, and pass
   `place.get("formattedAddress")` at `google_places.py:275`. Add the
   keyword-vs-coordinate agreement test — it fails today on `damour`, `jiyeh` and
   `bhamdoun`.
2. **S2** — normalise `country` inside `infer_region` (case/alias-insensitive), so the
   `enricher.py:85` gate stops being a silent cliff; then change `enricher.py:334` to
   `r.get("country") or "LB"`.
3. **S2** — Unicode-normalise both keywords and address (NFKC + strip bidi/zero-width +
   `casefold`), and add `\b` word boundaries to `_REGION_MAP`. This kills S2-unicode and
   S2-`sur` together in one pass.
4. **S3** — the type guards, the address length cap, and the `country == "SA"` trap.
5. Leave the KSA box table and the `NaN`/fail-safe behaviour exactly as they are.
