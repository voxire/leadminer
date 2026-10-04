# 200 — `_extract_city` (dedup.py:72) hostile-input & boundary audit

## Verdict

`_extract_city` cannot crash on any `str` — it is crash-proof but not
value-proof. Its single failure mode is returning a plausible-looking wrong
string, and the most common input shape in this dataset triggers it: any
address whose last comma-separated segment is the country collapses to the
token `"lebanon"` (or `"saudi arabia"`, `"liban"`), which is then used as half
of the phone-less dedup key (`dedup.py:213-214`). Two genuinely different
Lebanese businesses 450 km apart with the same name are silently merged into one
row whose address, phone, email and website belong to different real companies.
That is the single most dangerous input in this function, and it is not an edge
case — it is the shape of the data.

**Scope note:** the headline defect overlaps audit `003-dedup-identity-model.md`
S1 ("returns the country, not the city"). I confirm that finding and do not
restate its argument. What is new here is the full input/return matrix below,
the reachability proof that the type-crash class is currently unreachable (so
it is hardening, not an S1), and the discovery that the delimiter itself
(`","`) fails on **provably in-contract** inputs produced by this repo's own
scrapers at `scrapers/osm.py:87` and `scrapers/wikidata.py:99`.

---

## The function and its contract

```python
# dedup.py:72-76
def _extract_city(address: str | None) -> str:
    if not address:
        return ""
    parts = [p.strip().lower() for p in address.split(",")]
    return parts[-1] if parts else ""
```

Declared contract: input `str | None`, output `str`. Sibling
`normalize_name` (`dedup.py:66-69`) applies `NFKD` normalisation, strips
combining marks, collapses internal whitespace with `re.sub(r"\s+", " ", …)`
and lowercases. `_extract_city` applies **none** of that — only `.strip()` on
the edges and `.lower()`.

The only caller is `dedup.py:213`, feeding the identity key at
`dedup.py:214`: `key = (normalize_name(name), city)`. A key collision means
`_merge` (`dedup.py:190-194`) blends two records field-by-field. So every wrong
string this function returns is a silent, irreversible field-level data loss —
not a cosmetic defect.

---

## Complete input matrix (all rows executed against `dedup.py:72`)

### A. `None` / empty / whitespace — **correctly handled**

| Input | Return | Verdict |
|---|---|---|
| `None` | `''` | correct — `dedup.py:73-74` |
| `''` | `''` | correct |
| `'   '` | `''` | correct (`' '.split(',') == [' ']`, `.strip()` → `''`) |
| `'\n\t'` | `''` | correct — Python's `str.strip()` treats `\n\t` as whitespace |
| `b''` | `''` | correct (falsy, caught by line 73 before `.split`) |

The module documents this truthiness convention explicitly at `dedup.py:36-38`
("callers can test the result for truthiness"), and `_extract_city` honours it.
Return type is `str` in every one of these cases; verified
`all(type(f(s)) is str for s in ['', 'a', ',', '  ']) == True`.

### B. Zero / negative / wrong type — **split verdict: silent `''` vs. traceback**

| Input | Return | Verdict |
|---|---|---|
| `0` (int) | `''` | **silently wrong** — falsy, treated as "no address" |
| `0.0` | `''` | silently wrong |
| `False` | `''` | silently wrong |
| `[]` | `''` | silently wrong |
| `{}` | `''` | silently wrong |
| `-5` | **raises** `AttributeError: 'int' object has no attribute 'split'` | crashes with traceback |
| `12345` | **raises** `AttributeError: 'int' object has no attribute 'split'` | crashes with traceback |
| `3.14` | **raises** `AttributeError: 'float' object has no attribute 'split'` | crashes with traceback |
| `True` | **raises** `AttributeError: 'bool' object has no attribute 'split'` | crashes with traceback |
| `['Beirut']` | **raises** `AttributeError: 'list' object has no attribute 'split'` | crashes with traceback |
| `{'a': 1}` / `('Beirut',)` / `{'Beirut'}` / `object()` | **raises** `AttributeError` | crashes with traceback |
| `b'Beirut'` | **raises** `TypeError: a bytes-like object is required, not 'str'` | crashes with traceback |

The `0 / 0.0 / False / [] / {}` rows are the sharpest edge in the function: a
genuinely wrong-typed value is indistinguishable from `address=None`, so a
data-shape bug upstream converts into *silent dedup keys* instead of a loud
failure. `True` and `12345` crash, `False` and `0` do not — the same logical
mistake yields opposite outcomes depending on the value.

**Reachability (why the crashes are S3, not S1):** every producer of
`BusinessRecord` types `address: str | None` (`scrapers/base.py:8`) and honours
it — `scrapers/osm.py:87` joins with `", "`, `scrapers/google_places.py:286`
assigns `place.get("formattedAddress")`, `scrapers/wikidata.py:99` assigns
`row.get("address", {}).get("value")`. The one non-scraper input path,
`main.load_master` (`main.py:77-104`), blanks `""` → `None` (`main.py:87-89`) and
type-casts only `_FLOAT_FIELDS` / `_INT_FIELDS` (`main.py:43-44`), which do not
include `address`. No in-repo path can hand `_extract_city` a non-`str`. The
crash is therefore latent, reachable only from future code or external data.
`_pick`/`_merge` propagate arbitrary types unchanged, so the moment any producer
relaxes its annotation the exception surfaces at `dedup.py:213`, mid-run.

### C. Strings that violate the documented contract — **all silently wrong, zero crashes**

| Input | Return | Verdict |
|---|---|---|
| `'Beyrouth, Liban'` | `'liban'` | silently wrong — country, not city |
| `'12 Rue de la Paix, Beyrouth, Liban'` | `'liban'` | silently wrong |
| `'Olaya, Riyadh, 12211, Saudi Arabia'` | `'saudi arabia'` | silently wrong — also swallows the postcode |
| `'Hamra St, Beirut, Lebanon 1107 2020'` | `'lebanon 1107 2020'` | silently wrong — country **plus** postcode |
| `'Delta Coffee, Beirut'` (name inside address) | `'beirut'` | accidentally right, for the wrong reason |

The postcode row matters operationally: `dedup.py:75` strips only whitespace, so
`"Lebanon 1107 2020"` and `"Lebanon 1213 2038"` are two different city keys for
the same city. Google's `formattedAddress` is documented as the concatenation of
address components and *ends with the country*
([Place Details](https://developers.google.com/maps/documentation/places/web-service/place-details),
sample `"1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA"`), so the
last segment is the country for the dominant address shape in this dataset.

### D. Non-Latin, French, emoji, control characters — **no crash, value quality varies**

| Input | Return | Verdict |
|---|---|---|
| `'شارع الحمراء, بيروت, لبنان'` (Arabic, ASCII commas) | `'لبنان'` | silently wrong — country in Arabic, no cross-script match with `'lebanon'` |
| `'شارع الحمراء، بيروت'` (Arabic, **U+060C** `،`) | `'شارع الحمراء، بيروت'` | silently wrong — **entire address becomes the "city"**, newlines and all |
| `'😀, Beirut, Lebanon'` | `'lebanon'` | correctly handled (emoji irrelevant, country still wins) |
| `'😀'` | `'😀'` | correctly handled (no exception) |
| `'‏‎'` (U+200F/U+200E RTL marks only) | `'‏‎'` | silently wrong — invisible control chars survive into the key |
| `'12 Rue de la Paix, Beyrouth, Liban'` | `'liban'` | accents handled (they lower fine), wrong segment |
| `'Beyrouth, Liban'` | `'liban'` | vs `'Beyrouth, Lebanon'` → `'lebanon'`: two keys, one city |

Arabic text never crashes: `str.lower()` is a no-op on Arabic and `unicodedata`
is not called here. The failure is purely semantic — `'بيروت'` (from
`wikidata.py` / OSM `addr:city` in Arabic) and `'lebanon'` (from Google) never
match, so the same business survives as two master rows forever.

### E. Delimiter / structural edge cases — **all silently wrong**

| Input | Return | Verdict |
|---|---|---|
| `'12, Hamra Street'` (OSM, no `addr:city`) | `'hamra street'` | silently wrong — a street used as a city |
| `'Hamra Street, Ras Beirut'` | `'ras beirut'` | silently wrong — a suburb used as a city |
| `'12, Hamra Street, Ras Beirut, Beirut, Achrafieh'` | `'achrafieh'` | silently wrong — a district used as a city (OSM puts `addr:district` last, `osm.py:85`) |
| `'12 Hamra St\nBeirut\nLebanon'` (newline-joined, no comma) | `'12 hamra st\nbeirut\nlebanon'` | silently wrong — **the whole address is the city key** |
| `'Beyrouth\nLebanon'` | `'beyrouth\nlebanon'` | same |
| `'Beirut; Lebanon'` | `'beirut; lebanon'` | silently wrong — semicolon not a delimiter |
| `'Beirut / Lebanon'` | `'beirut / lebanon'` | silently wrong — slash not a delimiter |
| `'Rue X. Beirut. Lebanon'` | `'rue x. beirut. lebanon'` | silently wrong — full stops not delimiters |
| `'Beirut，Lebanon'` (fullwidth U+FF0C comma) | `'beirut，lebanon'` | silently wrong — delimiter mismatch |
| `'Located in Beirut, near the port, Lebanon'` | `'lebanon'` | silently wrong |
| `'N/A'` / `'None'` / `'---'` | `'n/a'` / `'none'` / `'---'` | silently wrong — placeholder text becomes a key |
| `',,,,'` | `''` | silently wrong — see S2 |
| `'Hamra St, Beirut,'` (trailing comma) | `''` | silently wrong — see S2 |
| `', Beirut, Lebanon'` (leading comma) | `'lebanon'` | silently wrong (country wins anyway) |
| `'A,,Beirut'` (double comma) | `'beirut'` | correctly handled — `''` interior part is skipped by `[-1]` |

The fullwidth-comma row is a genuinely hostile case worth naming: `'،'`
(Arabic comma) and `'，'` (CJK fullwidth comma) are visually identical to ASCII
`,` to a human reviewer but are not `","` to `str.split`.

### F. Very large inputs — **correctly handled, no perf or memory concern**

| Input | Timing | Verdict |
|---|---|---|
| 10.2 MB, 250 001 comma-separated 40-char parts | 0.046 s | correct, linear |
| 14 MB, no comma at all (`'Beirut ' * 2_000_000`) | 0.021 s | correct, linear |
| 1 M bare commas (`',' * 1_000_000`) | 0.050 s | correct, linear |

There is no ReDoS here: `dedup.py:75` uses `str.split` (no backtracking) and no
regex at all. All the regex cost in this module lives in `normalize_phone`
(`dedup.py:44`). A pathological address from a hostile or broken upstream cannot
stall the run — worth stating so nobody spends time here.

---

## Findings

### S1 — A country-ending address makes the whole country one "city", silently merging unrelated businesses

- **Where:** `dedup.py:76` (`return parts[-1] if parts else ""`), consumed at `dedup.py:213-214`, damage amplified by `_merge` at `dedup.py:190-194`.
- **Breaks:** `address.split(",")` takes the **last** segment. Google documents `formattedAddress` as ending with the country, and both OSM (`scrapers/osm.py:87`, `", ".join`) and Wikidata (`scrapers/wikidata.py:99`, free text) produce country-ending strings routinely. Every phone-less Lebanese record therefore shares `city == "lebanon"`, so the key at `dedup.py:214` degenerates from `(name, city)` to just `(name)`. Two different companies named "Delta Coffee" — one in Beirut, one in Tripoli — are merged, and `_pick` blends their `address`, `phone`, `email` and `website` field-by-field into one output row. The result ships in `all_businesses.csv` and, because `_pick` only ranks by source trust / validity / recency (`dedup.py:179-187`), the blend looks entirely plausible to a human reviewer. There is no traceback and no warning.
- **Trigger (verified end-to-end through `dedup()`):**
  ```python
  a = {"name": "Delta Coffee", "address": "Beirut,  Lebanon", "phone": None, "source": "google_places"}
  b = {"name": "Delta Coffee", "address": "Tripoli, Lebanon", "phone": None, "source": "google_places"}
  _extract_city(a["address"])  # 'lebanon'
  _extract_city(b["address"])  # 'lebanon'
  len(dedup([a, b]))           # -> 1
  ```
  Two real businesses, 450 km apart, collapse to one. Same for
  `'12 Rue de la Paix, Beyrouth, Liban'` → `'liban'` and
  `'Olaya, Riyadh, 12211, Saudi Arabia'` → `'saudi arabia'`.
- **Why S1 and not S2:** silent, irreversible loss of real businesses' contact
  data, with no signal in the run log, the row counts, or the CSV. It also
  compounds monthly: `all_businesses.csv` is cumulative and re-scraped
  (`main.py:150`), so a bad merge is re-merged on every subsequent run and the
  two originals can never be recovered from the output.
- **Fix:** stop deriving the city from free text. Populate `lat`/`lon` (already
  collected by all three scrapers) and match on a geographic cell, or carry a
  real `city`/`locality` field from OSM `addr:city` and Google
  `addressComponents[].locality` and key on that. At minimum, strip trailing
  country tokens (`if parts[-1] in _COUNTRY_NAMES: parts.pop()`) and strip a
  trailing postcode — see audits `003`, `049`, `095`.

### S1 — The hard-coded `","` delimiter breaks on in-contract addresses from this repo's own scrapers

- **Where:** `dedup.py:75` (`address.split(",")`).
- **Breaks:** a comma-free address is not a contract violation — it is the
  normal output of `scrapers/osm.py:87`, which joins with `", "` only the
  `addr:*` tags that happen to be present. An element tagged with
  `addr:housenumber` + `addr:street` but no `addr:city` / `addr:district` (no
  code path forces them; `osm.py:80-86` filters with `if p`) yields
  `"12, Hamra Street"` → city `'hamra street'`, a street name as a city.
  More damagingly, `wikidata.py:99` takes `wdt:P6375` verbatim, and P6375 is
  free text that is frequently a bare locality (`"Beyrouth"` → `'beyrout'`, which
  happens to be fine) or a newline-joined multi-line value
  (`"Beyrouth\nLebanon"` → `'beyrouth\nlebanon'`). In all of these the returned
  key is effectively unique per record, so **the name-merge never fires** and
  every scrape re-appends the same business to the master CSV.
- **Trigger (verified):**
  ```python
  goog = {"name": "Starbucks", "address": "12 Hamra St\nBeirut\nLebanon",  "phone": None, "source": "google_places"}
  osm  = {"name": "Starbucks", "address": "12 Hamra Street, Beirut, Lebanon", "phone": None, "source": "osm"}
  _extract_city(goog["address"])  # '12 hamra st\nbeirut\nlebanon'
  _extract_city(osm["address"])   # 'lebanon'
  len(dedup([goog, osm]))         # -> 2   (one cafe, two rows, forever)
  ```
  Google's own docs warn against exactly this: *"Do not parse the formatted
  address programmatically. Instead, use the individual address components"*
  ([Places Library](https://developers.google.com/maps/documentation/javascript/reference/place)).
- **Why S1:** wrong results shipped in the cumulative master CSV. I rank it equal
  to the S1 above because both produce unverifiable bad data, though the blast
  radius differs: this one over-reports (visible duplicate counts) whereas the
  first under-reports and destroys rows (invisible).
- **Fix:** request `addressComponents` from Places (structured `locality` /
  `administrative_area_level_2` / `country`) instead of `formattedAddress`
  alone; for OSM and Wikidata, split on `[,\n;]` **and** return `""` when no
  segment matches a known-city list, so an unparseable address falls through to a
  distinct key rather than a fabricated one.

### S2 — Empty and junk addresses all collapse to `""`, merging same-named phone-less records globally

- **Where:** `dedup.py:74` (`return ""`) and `dedup.py:75-76`.
- **Breaks:** `'', '   ', '\n\t', None, ',,,,', 'Hamra St, Beirut,'` (trailing
  comma), `0`, `False`, `[]` all return `""`. Because `''` is indistinguishable
  from "address unknown", the key at `dedup.py:214` becomes
  `(normalize_name(name), "")` — and any two same-named, phone-less records with
  *any* unusable address anywhere in Lebanon or Saudi Arabia merge into one. That
  is the same over-merge as S1 but reached through the empty-string path instead
  of the country path, and it silently widens the blast radius to every record
  whose address merely ends in a trailing comma.
- **Trigger (verified):**
  ```python
  r1 = {"name": "Al Baraka", "address": "Beirut,", "phone": None}
  r2 = {"name": "Al Baraka", "address": None,     "phone": None}
  r3 = {"name": "Al Baraka", "address": "   ",     "phone": None}
  len(dedup([r1, r2, r3]))  # -> 1
  ```
- **Fix:** sentinel the "no usable city" case distinctly from the "no address"
  case, and — more importantly — exclude records whose city is unresolvable from
  name-merging entirely (index them under a unique key) rather than merging them
  on `("")`.

### S2 — The city half of the identity key gets no Unicode normalisation while the name half does

- **Where:** `dedup.py:75` vs. `dedup.py:67-68`.
- **Breaks:** `normalize_name` applies `NFKD` and strips combining marks; the
  city half of the *same tuple* applies only `.lower()`. The two halves of one
  key are therefore normalised by different rules, which guarantees divergence
  whenever the upstream writes the same city two ways. Verified:
  ```python
  osm_bey   = {"name": "Starbucks", "address": "Rue Verdun, Beyrouth",        "phone": None}
  goog_beir = {"name": "Starbucks", "address": "Verdun St, Beirut, Lebanon",  "phone": None}
  normalize_name("Starbucks") == normalize_name("Starbucks")   # 'starbucks'
  _extract_city(osm_bey["address"])   # 'beyrouth'
  _extract_city(goog_beir["address"]) # 'lebanon'
  len(dedup([osm_bey, goog_beir]))    # -> 2   (one cafe, two spellings)
  ```
  `'Beyrouth'` (OSM `addr:city`, French spelling — ubiquitous in Lebanon) and
  `'Beirut'` (Google, English spelling) are the same city and never match. Same
  class: `'بيروت'` vs `'beirut'`, NFC vs NFD, `'İ'` vs `'i'` (`.lower()` is used,
  not `.casefold()`).
- **Why S2 not S1:** it over-reports (duplicate rows), not destroys. But it is
  the mechanism that makes `all_businesses.csv` accumulate duplicates month over
  month, which is exactly the failure audit `028-csv-roundtrip-fidelity.md:145`
  describes.
- **Fix:** run the city through the *same* pipeline as `normalize_name`
  (`NFKD` → strip combining → collapse `\s+` → `.lower()`), then apply a
  spelling map (`beyrouth`→`beirut`, `beyrouth`/`bayrouth`→`beirut`) and, for
  Arabic, a transliteration step. Extend `tests/test_lead_signal.py` — the suite
  currently has no `_extract_city` coverage at all (see audit `031`:90, which
  only describes the happy path).

### S3 — Truthy non-`str` input crashes with an unhandled `AttributeError`

- **Where:** `dedup.py:75`.
- **Breaks:** `12345`, `3.14`, `True`, `['Beirut']`, `('Beirut',)`, `{'a': 1}`,
  `object()` raise `AttributeError: '<type>' object has no attribute 'split'`;
  `b'Beirut'` raises `TypeError: a bytes-like object is required, not 'str'`. The
  traceback would surface at `dedup.py:213` inside `dedup()`, aborting the whole
  run with no partial-write path (`main.py:180`). Meanwhile `0`, `False` and `[]`
  from the same bug return `""` and are swallowed — so one upstream type mistake
  produces either a loud crash or invisible over-merging depending only on the
  value's truthiness.
- **Why S3:** unreachable from every current producer
  (`scrapers/base.py:8`, `scrapers/osm.py:87`, `scrapers/google_places.py:286`,
  `scrapers/wikidata.py:99`, `main.load_master` at `main.py:87-89` which blanks
  cells and casts only float/int fields per `main.py:43-44`). Pure hardening.
- **Fix:** one line at the top of the function —
  `if not isinstance(address, str): return ""` (or `return str(address).strip()`)
  — which collapses both crash classes and the falsy-wrong-type class into a
  single safe behaviour, and makes the `str | None` annotation enforceable at
  runtime instead of aspirational.

### S3 — `dedup.py:76` contains an unreachable branch

- **Where:** `dedup.py:76` — `return parts[-1] if parts else ""`.
- **Breaks:** nothing at runtime; it misleads a reader into thinking a
  comma-less address can produce `""`. `str.split` never returns an empty list —
  verified for every edge: `''.split(',') == ['']`, `','.split(',') == ['', '']`,
  `' '.split(',') == [' ']`. So `parts` is always truthy and the `else ""` can
  never fire. (It is also the wrong guard if it ever did: it would return `""`
  rather than handling the empty-segment case.)
- **Fix:** delete the dead branch — `return parts[-1]`.

---

## Not a bug, but worth knowing

- **No ReDoS, no perf cliff.** 10.2 MB address → 0.046 s; 1 M commas → 0.050 s;
  14 MB with no comma → 0.021 s. All linear, no backtracking, no regex in this
  function. If an agent is auditing for catastrophic backtracking in `dedup.py`,
  the regex to worry about is `re.sub(r"\D", "", raw)` at `dedup.py:44`, not here.
- **The `str` return contract is honoured.** For any `str` input the function
  returns `str`, never `None` and never a partial list — verified. This matters
  because `dedup.py:214` puts the result straight into a dict key where `None`
  would be perfectly legal but would then behave differently from `''` at
  `dedup.py:74`. The current code is at least internally consistent.
- **`None`, `''` and whitespace are all handled correctly** and return `''`,
  matching the truthiness convention the module documents at `dedup.py:36-38`.
  That part of the design is right.
- **`.lower()` instead of `.casefold()`** (`dedup.py:75`). Measured on this
  input class there is no observable difference (`'İZMİR'.lower()` and
  `.casefold()` agree; `'STRASSE'` agrees). It is a latent correctness gap, not
  a live bug, for Arabic (no case) and the Latin city names in scope.
- **Postcodes are never stripped.** `'Lebanon 1107 2020'` →
  `'lebanon 1107 2020'`, so a single postal-code change re-keys a city. Affects
  every Google record whose `formattedAddress` carries a postcode.
- **Invisible control characters survive into the key.** `'‏‎'` (RTL
  marks only) → `'‏‎'`. Two records identical except for an invisible mark
  produce two master rows, and no reviewer will ever spot the difference in the
  CSV. Strip C0/C1 and bidi controls before keying.
- **Placeholder strings become keys.** `'N/A'` → `'n/a'`, `'None'` → `'none'`,
  `'---'` → `'---'`. Worth a sentinel list, especially given
  `wikidata.py:91-93` already had to guard `name.startswith("Q")`.
- **The function has no docstring** while its four neighbours do
  (`normalize_phone` at `dedup.py:34`, `normalize_name`, `_is_missing` at
  `dedup.py:80`, `_validity` at `dedup.py:121`), and each of those documents the
  historical bug it fixed. The absence is why the "last segment" assumption
  looks intentional rather than accidental.

---

## Single most dangerous input

**`address = "Beyrouth, Lebanon"` — any string whose last comma-separated segment
is the country.**

It is the most dangerous because of the conjunction of four properties, each
verified above:

1. **It is the dominant shape, not an edge case.** Google's `formattedAddress`
   is documented to end with the country, and `scrapers/osm.py:87` /
   `scrapers/wikidata.py:99` produce country-ending strings routinely. Every
   phone-less Lebanese record with a normal address hits this branch.
2. **It fails silently with no traceback.** `dedup.py:76` returns `'lebanon'`
   cleanly; nothing in the run log, the row counts, or the CSVs indicates a
   problem.
3. **The damage is destructive, not additive.** Because the key at
   `dedup.py:214` degrades to `(name, "lebanon")`, two unrelated businesses merge
   and `_merge`/`_pick` (`dedup.py:190-194`) blend their `address`, `phone`,
   `email` and `website` into a single row that looks entirely credible. A
   duplicate (the S1 newline case) at least inflates a number a human can see;
   this one destroys rows and contacts and is undetectable downstream.
4. **It is unrecoverable from the output.** `all_businesses.csv` is cumulative
   and re-loaded on every run (`main.py:150`), so once two companies are merged
   their contact data is gone from the product and cannot be reconstructed
   without re-scraping from scratch.

Contrast with the alternatives I also tested and rejected as *most* dangerous:
`12345` crashes loudly and is unreachable from any current producer;
`'😀'` is handled correctly; the 10 MB string is handled correctly in 0.046 s;
`None`/`''`/whitespace return the right answer. The type crashes are loud and
latent. This one is quiet and load-bearing.

**Recommended order of work**

1. Stop deriving the city from `address.split(",")[-1]` (`dedup.py:75-76`).
   Carry a real locality field from OSM `addr:city` and Google
   `addressComponents[].locality` / `administrative_area_level_2`, and fall back
   to a lat/lon geocell (`dedup.py:213-214`). This closes S1 #1 and S1 #2
   together and is the only fix that makes the name-key trustworthy again.
2. Until (1) ships, add the two cheap guards that stop the worst silent damage
   on the current key: pop a trailing country token and a trailing postcode
   before indexing (`dedup.py:75`).
3. Route the city half through the same normalisation as `normalize_name`
   (`dedup.py:67-68`) plus a Beyrouth/Beirut spelling map — S2.
4. Give unresolvable cities a distinct key instead of `""`, and exclude
   `city == ""` records from name-merging — S2.
5. Add `isinstance` guard (`dedup.py:73`) and delete the dead `else ""`
   (`dedup.py:76`) — S3, trivial.
6. Add `_extract_city` and `dedup` key tests. `tests/test_lead_signal.py` has
   **zero** coverage of either today; this function is the only place a
   cross-source identity decision is made, and it is currently unguarded.