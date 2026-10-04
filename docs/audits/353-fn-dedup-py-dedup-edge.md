# 353 — `dedup()` (dedup.py:197): edge & hostile-input audit

**Target:** `dedup.dedup` at `dedup.py:197-232`, plus the two helpers it calls to build
keys — `normalize_phone` (`dedup.py:33-63`) and `normalize_name` (`dedup.py:66-69`) +
`_extract_city` (`dedup.py:72-76`).
**Lens:** edge — boundary and hostile inputs.
**Method:** every value below was **executed** against the real functions on
Python 3.14.8, no network, no source modified. Probe scripts live outside the repo
(`/private/var/folders/.../T/opencode/edge/probe{,2,3,4,5,6}.py`); every printed
value is quoted verbatim. `enricher.infer_region` was inlined verbatim
(`enricher.py:58-94`) because `requests` is not installed and installing packages
is forbidden by `docs/audits/BRIEF.md`.
**Scope note:** `docs/audits/352-fn-dedup-py-dedup-correctness.md` already filed
the country-as-city key, the dead reconciliation at `dedup.py:225-230`, the
spliced `lat`/`lon`, and the all-zero-phone shared key. I do not re-file those.
This report is the hostile-input layer: what happens for `None`, `""`, whitespace,
`0`, negative, wrong types, Arabic/French script, emoji, absurd lengths, and
contract violations — plus **three gaps in 352's proposed fixes** that only an
edge pass finds.

---

## Verdict

`dedup()` is far more robust than its reputation on the **key** side and far more
fragile on the **value** side. Every hostile input I could construct is either
handled correctly or fails silently — **there is not one reachable traceback from
any input the declared contract (`base.py:6-28`, `str | None` / `float | None` /
`int | None`) permits**, and the function is provably `len(out) <= len(in)`,
non-mutating, and idempotent across three generations. But silent is not safe:
the **key** `("", "")` carries zero bits of identity, and every record that
reaches it is deleted except one; the closed country-code list at
`dedup.py:8-21` **manufactures a Lebanese `+961` number for every foreign number
it does not recognise**, and `osm.py:103` hardcodes `country="LB"` for every OSM
record so this is guaranteed, not hypothetical.

**Single most dangerous input:** `{"name": "Al Baik", "phone": None,
"address": "Riyadh, Saudi Arabia", "country": "SA", "source": "google_places"}`
— i.e. a Google-sourced record with **no phone** whose address is in Google's
documented `formattedAddress` shape (street…, **country** last). `_extract_city`
returns `parts[-1]` (`dedup.py:76`), so the key becomes
`("al baik", "saudi arabia")` — constant across the entire country. Verified:
three `Al Baik` branches in Riyadh / Jeddah / Dammam → **1 row**, whose `address`
says *Riyadh* and whose `lat/lon` say *Dammam*. It beats every rival candidate
because it is not hostile at all: it is the **normal, documented shape of the
primary KSA data source**, it requires no malformed data to trigger, it fires
silently with no exception and no log line, and the surviving row is
*plausible* — so nobody notices. And because `all_businesses.csv` is cumulative
(`main.py:150` load, `main.py:209` write), the two deleted branches are
unrecoverable from that point on.

---

## Boundary-input classification

`dedup([...])` unless noted. "Contract" = the `BusinessRecord` TypedDict at
`base.py:5-28`.

### A. `records` argument

| # | Input | Result | Class |
|---|---|---|---|
| A1 | `dedup([])` | `[]` | correctly handled |
| A2 | generator / any iterable of dicts | works (`dedup.py:201` is a plain `for`) | correctly handled (undocumented bonus) |
| A3 | `dedup(None)` | `TypeError: 'NoneType' object is not iterable` (`dedup.py:201`) | crashes — **contract violation** |
| A4 | `dedup("abc")` | `AttributeError: 'str' object has no attribute 'get'` (`dedup.py:202`) | crashes — contract violation |
| A5 | `dedup({"name": "X"})` (dict iterates keys) | `AttributeError: 'str' object has no attribute 'get'` | crashes — contract violation |
| A6 | `dedup([None])` | `AttributeError: 'NoneType' object has no attribute 'get'` | crashes — contract violation |
| A7 | `dedup([["a","b"]])` | `AttributeError: 'list' object has no attribute 'get'` | crashes — contract violation |

### B. `name`

| # | Input | Result | Class |
|---|---|---|---|
| B1 | key absent, `None`, `""` | `record.get("name") or ""` (`dedup.py:212`) → `normalize_name("")` → `""` | silently wrong — contributes to a **zero-information key** |
| B2 | `"   "` / `"\t"` / `" "` | truthy, survives every scraper guard, `normalize_name` strips → `""` | silently wrong — **reachable**, see S1-1 |
| B3 | `""`, `"AlBaik"` | `\s+` does not match U+200B; NFKD does not remove it → `'albaik'` ≠ `'albaik'` | silently wrong (missed dedup) |
| B4 | `"AlBaik­"`, `"Al­Baik"` (U+00AD soft hyphen) | `'\xa0'` is stripped, `'­'` is not → `'albaik­'` ≠ `'albaik'` | silently wrong (missed dedup) |
| B5 | `"الـبيرق"` vs `"البيرق"` (tatweel U+0640) | not a combining mark, NFKD keeps it → keys differ | silently wrong (missed dedup) |
| B6 | NFC `"Café"` vs NFD `"Café"` | both → `"cafe"` | correctly handled |
| B7 | `"\xa0Beirut\xa0"` (NBSP) | `\s+` matches `\xa0` → `"beirut"` | correctly handled |
| B8 | `"😀 Cafe"`, `"🏪 Cafe"` | `'😀 cafe'`, no crash, no `UnicodeEncodeError` | correctly handled |
| B9 | `12345` (int) | **`TypeError: normalize() argument 2 must be str, not int`** (`dedup.py:67`) | crashes — contract violation |
| B10 | `3.5` (float), `True` (bool) | same `TypeError` | crashes — contract violation |
| B11 | `"x" * 2_000_000` | 359 ms, no ReDoS, 2 M-char key | correctly handled (no crash, linear) |
| B12 | name differing only by case / inner runs of spaces | `"AL  BAIK"` → `"al baik"` | correctly handled |

### C. `address`

| # | Input | Result | Class |
|---|---|---|---|
| C1 | `None`, `""`, `"   "`, `" , "` | `""` (`dedup.py:73-74`, `:76`) | silently wrong — **zero-information city** |
| C2 | `"Riyadh, Saudi Arabia"` | `"saudi arabia"` — **the country** | silently wrong — **THE most dangerous** |
| C3 | `"King Fahd Rd, Al Olaya, Riyadh, Saudi Arabia"` | `"saudi arabia"` | silently wrong |
| C4 | `"3450 Sacramento St #204, San Francisco, CA 94118, USA"` | `"usa"` | silently wrong (Google's own doc example) |
| C5 | `"367 Pitt St, Sydney NSW 2000, Australia"` | `"australia"` | silently wrong (Google's own doc example) |
| C6 | `"Beirut, Lebanon"` / `"Tripoli, Lebanon"` | `"lebanon"` for both | silently wrong — Beirut and Tripoli fuse |
| C7 | `"Riyadh,"` / `"Riyadh, Saudi Arabia,"` (one trailing comma) | `""` — the discriminator is **destroyed by one character** | silently wrong |
| C8 | `","`, `",,,,"` | `""` | silently wrong |
| C9 | `"شارع الحمرا، بيروت، لبنان"` (U+060C Arabic comma) | the **entire address** — `str.split(",")` does not split U+060C | silently wrong (dedup **never** matches) |
| C10 | OSM shape `[housenumber, street, suburb, city, district]` (`osm.py:80-87`) | `parts[-1]` = **`addr:district`**, not `addr:city` | silently wrong (per-source vocabulary) |
| C11 | OSM bare node, no `addr:*` tags → `address=None` (`osm.py:87 … or None`) | `""` | silently wrong |
| C12 | `33.8` (float) | **`AttributeError: 'float' object has no attribute 'split'`** (`dedup.py:75`) | crashes — contract violation |
| C13 | `"Riyadh , Saudi Arabia"` (space before comma) | `"saudi arabia"` | silently wrong (already covered by C2) |

### D. `phone`

| # | Input | Result | Class |
|---|---|---|---|
| D1 | `None`, `""`, `"   "`, `"0"`, `0`, `0.0`, `False`, `-1`, `"abc"`, `"---"`, `"nan"`, `"NaN"`, `"+961 1 2"` | all `""` → falls through to the name key (`dedup.py:205-206`) | **correctly handled** |
| D2 | `9611234567` (int) | `+9611234567` via `str()` at `dedup.py:42` | correctly handled |
| D3 | `9611234567.0` (float) | `+96112345670` — spurious trailing digit from `"9611234567.0"` | silently wrong (unreachable: `load_master` casts no `phone`) |
| D4 | `["+9611234567"]` (list) | `+9611234567` via `str(list)` | accidentally correct |
| D5 | `"٩٦١١٢٣٤٥٦٧"` (Arabic-Indic) | **`+961٩٦١١٢٣٤٥٦٧`** — `\D` is Unicode-aware so the digits survive `dedup.py:44`, then fail every `startswith` at `dedup.py:59`, then get `+961` glued on | silently wrong — **and the fix in 352 does not close it** |
| D6 | `"٠٠٠٠٠٠٠٠"` (7 Arabic-Indic zeros) | **`+961٠٠٠٠٠٠٠٠`** — `len >= 7` passes `_MIN_DIGITS`, and `lstrip("0")` cannot strip U+0660 | silently wrong — **a second shared all-zero key** |
| D7 | `"+33 6 12 34 56 78"` (French) | **`+96133612345678`** | silently wrong — fabricated LB number |
| D8 | `"+44 20 7123 4567"` / `"+90 212 555 1234"` / `"+1 800 555 0199"` / `"+91 98765 43210"` / `"+49 30 901820"` | `+961442071234567` / `+961902125551234` / `+96118005550199` / `+961919876543210` / `+9614930901820` | silently wrong — fabricated LB numbers |
| D9 | `"+974 3333 4444"`, `"+20 100 123 4567"` | `+97433334444`, `+201001234567` | correctly handled (both codes listed at `dedup.py:8-21`) |
| D10 | `"Tel: +961 1 234 567 / +961 3 456 789"` | **`+96112345679613456789`** — two real numbers concatenated into one 20-digit key | silently wrong |
| D11 | `"+9611234567 or 0701234567"`, `"009611234567/009613456789"` | `+96112345670701234567` (20), `+9611234567009613456789` (22) | silently wrong |
| D12 | `"9" * 5_000` | `+961` + 5 000 nines, accepted as a valid key (5 004 chars) — no length cap anywhere | silently wrong, no crash |
| D13 | `float("nan")` | truthy at `dedup.py:205` → `normalize_phone` → digits `""` → `""` → name key | correctly handled |
| D14 | `["a","b"]`, `{"x":1}` | `str()` of the container; digits extracted from it | silently wrong (no crash) |

### E. `country`

| # | Input | Result | Class |
|---|---|---|---|
| E1 | `None`, `""`, `0`, `"xx"`, `"FR"` | `record.get("country") or "LB"` (`dedup.py:205`) → `"LB"`, then `_COUNTRY_CODES.get(...,"961")` (`dedup.py:48`) → `"961"` | correctly handled |
| E2 | `"sa"` (lowercase) | `"961"` at `dedup.py:48` — wrong cc, but the phone's own `+966` still wins at `dedup.py:58-60` | correctly handled by accident |
| E3 | `False` | `or "LB"` → `"LB"` | correctly handled |
| E4 | `country="LB"` on a `+33` phone | `+96133612345678` (D7) — the fallback cc is **asserted, not validated** | silently wrong |

### F. Other fields / composite

| # | Input | Result | Class |
|---|---|---|---|
| F1 | `lat = float("nan")` | passes `_is_missing` (`:86-90`) and `_validity` (`float(nan)` does not raise, `:128-133`) → emitted; `write_csv` writes the text `nan`; `infer_region` boxes all compare False → `region=None`; `load_master` re-reads it as `nan` (`main.py:90-95`) → permanent | silently wrong |
| F2 | `phone = 0` and `phone = ""` on the same name key | both key on `(name, city)`; `_pick` sees `""` as missing and returns the other side → merged `phone = 0` | silently wrong |
| F3 | `phone = False` and `phone = None` | merged `phone = False` — `_is_missing(False)` is `False`, so `False` is "present" | silently wrong (unreachable: `load_master` casts only `website_live` to bool, `main.py:102-103`) |
| F4 | two records, same key, **different cities** | `(address, lat, lon)` torn apart — see S1-2 | silently wrong |
| F5 | 40 000 records on one key | no quadratic blowup | correctly handled |

---

## Findings

### S1 — `("", "")` is a valid dedup key: it carries zero identity and silently deletes every record that reaches it

- **Where:** `dedup.py:212-214` (`name = record.get("name") or ""` → `normalize_name`;
  `city = _extract_city(record.get("address"))`), `dedup.py:75-76`
  (`if not address: return ""` and `return parts[-1] if parts else ""`),
  `dedup.py:69` (`normalize_name(" ") == ""`), consumed at `dedup.py:218`.
- **Breaks:** there is no guard that a key is *informative*. A record with no
  name and no address normalizes to the constant `("", "")`, and **every such
  record in the entire corpus lands on the same dict slot** and is merged into
  one. Same for a record with a name but a blank address — key `("<name>", "")`,
  which fuses every same-named phone-less business on Earth.
- **Trigger (verified, exact output):**
  ```python
  dedup([
    {"name":"Nahdi","phone":None,"address":None, "country":"LB","source":"osm","lat":34.4,"lon":35.8},
    {"name":"Nahdi","phone":None,"address":"   ","country":"LB","source":"osm","lat":33.9,"lon":35.5},
    {"name":"   ", "phone":None,"address":None, "country":"LB","source":"osm","lat":33.9,"lon":35.5},
    {"name":None, "phone":None,"address":None, "country":"LB","source":"osm","lat":33.9,"lon":35.5},
  ])
  ```
  Keys are `('nahdi','')`, `('nahdi','')`, `('','')`, `('','')` — 4 records in
  2 towns → **2 output rows**, survivor `lat=34.4`. Over 50 such pairs:
  100 records in → 50 rows out.
- **Why this is reachable despite every scraper requiring a name:** all three
  name guards are `if not name` (`osm.py:64-65`, `google_places.py:269-270`,
  `wikidata.py:92`), and **`" "` is truthy** — verified. So a whitespace-only OSM
  `name` tag is emitted, and `normalize_name` strips it to `""` (`dedup.py:69`).
  Two such nodes → 1 row, `lat` from whichever sorted later. Separately,
  `load_master` converts a blank CSV cell to `None` (`main.py:88-89`), so any
  master row that ever lands with an empty `name` or `address` column
  contributes to this bucket **on every subsequent run, forever**.
- **Why a blank city is the common case, not an exotic one:** Google documents
  that "businesses without a physical service address don't include the
  `formattedAddress` field" (Places Text Search (New), *Places data fields*), so
  `place.get("formattedAddress")` at `google_places.py:286` returns `None` for
  exactly the categories this scraper queries hardest — `wedding venues`
  (`google_places.py:117`), `photography studios` (`:118`), `events venues`
  (`:122`), `law firms` (`:92`), `marketing agencies` (`:95`). And a single
  trailing comma turns a discriminating key into `""` (`"Riyadh," → ""`,
  `"Riyadh, Saudi Arabia," → ""`), so one malformed character is enough.
- **Fix:** reject non-informative keys before indexing, at `dedup.py:213-214`:
  ```python
  city = _extract_city(record.get("address"))
  nm = normalize_name(name)
  if not nm or not city:
      # no usable identity: keep the record out of the mergeable pool entirely
      name_records_unkeyed.append(dict(record)); continue
  ```
  An unkeyable record must be **emitted as its own row**, never merged. Pair it
  with a locality extractor that also splits on U+060C and on `osm.py:84`'s
  `addr:city` (which fixes C2/C9/C10 too — see 352's fix note below).

---

### S1 — `_extract_city` fabricates the **country** as the city, fusing phone-less same-named businesses nationwide; `_merge` then tears `address` away from `lat`/`lon`

- **Where:** `dedup.py:76` (`return parts[-1] if parts else ""`), key at
  `dedup.py:213-214`; the tear is `dedup.py:190-194` (`for key in set(a) | set(b)`,
  one `_pick` per key) with the tiebreak at `dedup.py:184`.
- **Breaks, part 1 — over-merging:** Google's documented `formattedAddress`
  places the **country last**, so `parts[-1]` is the country for every place:
  ```
  "3450 Sacramento St #204, San Francisco, CA 94118, USA" -> 'usa'
  "367 Pitt St, Sydney NSW 2000, Australia"               -> 'australia'
  "Riyadh, Saudi Arabia"                                 -> 'saudi arabia'
  "Beirut, Lebanon"                                      -> 'lebanon'
  ```
  (first two quoted verbatim from the Places Text Search (New) *Places data
  fields* examples). The `city` half of the key is a **constant per country**.
- **Breaks, part 2 — incoherent survivors:** when a fusion does happen between
  two records from the **same** source, `address` and `lat`/`lon` are decided by
  two independent `str()` comparisons (`dedup.py:184`). Because
  `addr:district` sorts after `Riyadh…` but `24.7136` sorts after `21.4858`, the
  merged row takes the **Jeddah address with the Riyadh coordinates**.
- **Trigger (verified, both halves):**
  ```python
  dedup([
    {"name":"Al Baik","phone":None,"address":"King Fahd Rd, Riyadh, Saudi Arabia",
     "country":"SA","source":"google_places","scraped_at":"2026-01-01","lat":24.7136,"lon":46.6753},
    {"name":"Al Baik","phone":None,"address":"Prince Abdulaziz St, Jeddah, Saudi Arabia",
     "country":"SA","source":"google_places","scraped_at":"2026-01-01","lat":21.4858,"lon":39.1925},
    {"name":"Al Baik","phone":None,"address":"Dhahran St, Dammam, Saudi Arabia",
     "country":"SA","source":"google_places","scraped_at":"2026-01-01","lat":26.4207,"lon":50.0888},
  ])
  ```
  → **1 record**, `address='Riyadh Main St, Riyadh, Saudi Arabia'`-shaped
  string with `lat=26.4207, lon=50.0888`. `enricher.infer_region`
  (`enricher.py:89-94`) labels it from the coordinates. Verified downstream
  incoherence: `address` city `Jeddah` vs geo region `Riyadh`; and the LB case
  `address='Nabatieh St, Tripoli, Lebanon'` vs geo region `North Lebanon`.
- **Amplification, verified:** run 1 fuses Riyadh+Jeddah → 1 row
  (`address='Prince Abdulaziz St, Jeddah, Saudi Arabia'`, `lat=24.7136`). That
  row is written to `all_businesses.csv` (`main.py:209`) and reloaded next run
  (`main.py:150`, `:180`). Run 2 re-adds the Jeddah branch and merges into the
  survivor again → still 1 row, `scraped_at` bumped. **The Riyadh branch is gone
  from every CSV permanently.** `main.py:181` prints
  `After merge + dedup: 1 unique businesses`, which reads as healthy.
- **Fix:** (a) make the locality extractor strip a trailing country token and
  validate the result against a city gazetteer — the vocabulary already exists at
  `enricher.py:52-54` (`_REGION_MAP`) and the coordinate boxes at
  `enricher.py:58-76`; (b) resolve `address`, `lat`, `lon` as an **indivisible
  bundle** in `_merge` — pick the winning record once, then copy all three from
  it, instead of calling `_pick` three times (this also closes the `lat`/`lon`
  splice filed independently as 352-S2-4 and 345-S1-1).

> **Correction to 352's fix (`:114-116`).** "strip a country token, then take
> `parts[-1]`" closes the country case but **not** the empty case: when
> `address is None` or blank, `parts == []` and `dedup.py:76` returns `""`
> regardless. And the `(",")`-only / `"Riyadh,"` trailing-comma inputs still
> yield `""`. The uninformative-key guard in S1-1 is required in addition to it.

---

### S1 — `_merge` fabricates a row whose address and coordinates describe different physical places, for the same business

- **Where:** `dedup.py:190-194`, specifically `merged[key] = _pick(key, a, b)`
  at `dedup.py:193` with the `str(value)` tiebreak at `dedup.py:184`. `lat`/`lon`
  have no `_SOURCE_TRUST` entry (`dedup.py:97-110`) and are not in `_VOLATILE`
  (`dedup.py:114`), so the recency slot is `""` (`dedup.py:183`) and both fall
  to the tiebreak.
- **Breaks:** `(address, lat, lon)` is a **composite identity triple**, but
  `_merge` resolves it field-by-field with three independent decisions. When two
  records agree on source and have equally valid floats, `address` is chosen by
  lexicographic order on the address string and `lat` by lexicographic order on
  the coordinate string. Those two orders are **uncorrelated**, so the survivor
  is a chimera. This is not limited to the empty-city case: it fires on the
  **phone key** too, via a shared switchboard or a relocated listing.
- **Trigger (verified on the phone key, so no `_extract_city` involvement):**
  ```python
  dedup([
    {"name":"Al Baik","phone":"+966112345678","country":"SA","source":"osm","scraped_at":"2026-01-01",
     "address":"King Fahd Rd, Riyadh, Saudi Arabia","lat":24.7136,"lon":46.6753},
    {"name":"Al Baik","phone":"+966 11 234 5678","country":"SA","source":"osm","scraped_at":"2026-01-01",
     "address":"Prince Abdulaziz St, Jeddah, Saudi Arabia","lat":21.4858,"lon":39.1925},
  ])
  ```
  → **1 record**, `address='Prince Abdulaziz St, Jeddah, Saudi Arabia'`,
  `lat=24.7136, lon=46.6753`. `infer_region` → `"Riyadh"`. A salesperson
  reading the row is sent to Jeddah; the map pin and the `region` column say
  Riyadh. Nothing in the pipeline can detect this: `main.py:228-234` reports
  `region` counts, so Riyadh gains a Jeddah branch and Jeddah silently loses one.
  Also verified LB: `address='Nabatieh St, Tripoli, Lebanon'` with
  `lat=34.4367` → region `"North Lebanon"`.
- **Why it is S1 and not cosmetic:** the master is cumulative
  (`main.py:150` → `main.py:209`), so the chimera is re-merged every run and
  becomes the next run's input. One bad listing poisons the record permanently,
  and `website_live`/`check_websites` will spend an HTTP GET on whatever website
  survived alongside those coordinates.
- **Fix:** in `_merge`, special-case the triple — choose the winning record from
  `("address", "lat", "lon")` with one `rank` evaluation, then assign all three
  from it. Roughly:
  ```python
  GEO_BUNDLE = ("address", "lat", "lon")
  if any(k in GEO_BUNDLE for k in merged):
      winner = a if _record_rank(a) >= _record_rank(b) else b
      for k in GEO_BUNDLE:
          if not _is_missing(winner.get(k)):
              merged[k] = winner[k]
  ```

---

### S2 — `_KNOWN_COUNTRY_CODES` is a closed list: every unrecognised country code gets a **fabricated `+961`/`+966` number**, and `osm.py:103` guarantees it happens

- **Where:** `dedup.py:8-21` (the 12-entry tuple), the scan at `dedup.py:58-60`,
  and the fabrication at `dedup.py:63`
  (`return "+" + cc + digits.lstrip("0")`). `cc` comes from
  `_COUNTRY_CODES.get(country, "961")` (`dedup.py:48`), which has exactly two
  entries (`dedup.py:23-26`).
- **Breaks:** when a number's own prefix is not in the closed list, the function
  does not say "I don't know this country" — it **asserts the record's country**
  and prepends that code. Verified:
  ```
  '+33 6 12 34 56 78'   -> '+96133612345678'      (France)
  '+44 20 7123 4567'    -> '+961442071234567'     (UK)
  '+90 212 555 1234'    -> '+961902125551234'     (Turkey)
  '+1 800 555 0199'     -> '+96118005550199'      (US)
  '+91 98765 43210'     -> '+961919876543210'     (India)
  '+49 30 901820'       -> '+9614930901820'       (Germany)
  '+974 3333 4444'      -> '+97433334444'         (Qatar  - correct, IS listed)
  '+20 100 123 4567'    -> '+201001234567'        (Egypt  - correct, IS listed)
  ```
  The header comment at `dedup.py:5-7` says borders leak constantly, which is
  exactly why Morocco/Tunisia/Libya/Egypt were added — but the design has no
  fallback for a country the author did not think of.
- **Reachability, and why it is guaranteed rather than hypothetical:** `osm.py:103`
  hardcodes `country="LB"` for **every** OSM record (as does
  `wikidata.py:111`), and `main.py:69-74` `resolve_country` defaults to `LB` for
  anything that is not `+966…`/`+961…`. So an OSM node anywhere in the
  `ISO3166-1=LB` area (`osm.py:13`) whose `phone` tag (`osm.py:89`) is a
  non-Lebanese number is keyed and normalised as Lebanese, guaranteed.
- **Verified end-to-end damage — the same business stored twice, once fictional:**
  ```python
  dedup([
    {"name":"Cairo Coffee","phone":"01012345678",   "address":None,"country":"LB","source":"osm"},
    {"name":"Cairo Coffee","phone":"+20 10 1234 5678","address":None,"country":"LB","source":"google_places"},
  ])
  ```
  → **2 records** (should be 1), and `main.py:194` then writes
  `+9611012345678` and `+201012345567`. The first is a **fabricated Lebanese
  number that a sales rep can dial** and that will reach an unrelated Lebanese
  business. Both rows persist in `all_businesses.csv` and re-merge as two
  businesses forever.
- **Fix:** when no listed code matches at `dedup.py:58-60` and the number does
  not look like a bare national number for `cc`, **return `""`** rather than
  inventing a code. Cheap heuristic: if the digit count after the scan is
  longer than the longest plausible national number for `cc` (LB 7–9, SA 8–9
  national significant digits), the number carries a country code this module
  does not know — refuse to key it. Store the raw value on the record untouched.

---

### S2 — `\D` is Unicode-aware, so Arabic-Indic digits survive `dedup.py:44`, bypass every `startswith` test, and get a country code glued on — **and 352's one-line fix does not close it**

- **Where:** `dedup.py:44` (`re.sub(r"\D", "", raw)`), `dedup.py:58-60`
  (`digits.startswith(known)`), `dedup.py:63`.
- **Breaks:** in Python 3, `\d`/`\D` in a `str` pattern match by Unicode general
  category `Nd`, so U+0660–U+0669 (ARABIC-INDIC DIGIT) are **digits**, not
  non-digits, and `re.sub(r"\D", "", …)` **keeps them**. They then fail every
  ASCII `startswith` at `dedup.py:59`, fall through to `dedup.py:63`, and get
  `cc` prepended to a number that already contained its own country code.
- **Trigger (all verified):**
  ```
  re.sub(r'\D','', '٩٦١١٢٣٤٥٦٧')      == '٩٦١١٢٣٤٥٦٧'      # NOT stripped
  '٩٦١١٢٣٤٥٦٧'.startswith('961')       == False             # fails dedup.py:59
  normalize_phone('٩٦١١٢٣٤٥٦٧','LB')    == '+961٩٦١١٢٣٤٥٦٧'  # cc glued on
  normalize_phone('٠٧٦١٢٣٤٥٧٨','LB')    == '+961٠٧٦١٢٣٤٥٧٨'
  normalize_phone('+٩٦٦٥٠٠١٢٣٤٥٦٧','LB') == '+961٩٦٦٥٠٠١٢٣٤٥٦٧'
  ```
  One Lebanese number, two scripts, one business:
  ```python
  dedup([
    {"name":"Nahdi","phone":"٩٦١١٢٣٤٥٦٧","address":"Beirut","country":"LB","source":"osm"},
    {"name":"Nahdi","phone":"9611234567","address":"Beirut","country":"LB","source":"google_places"},
  ])
  ```
  → **2 records** (should be 1), and the Arabic-Indic row carries the raw
  non-E.164 string `'٩٦١١٢٣٤٥٦٧'` into the `phone` column of
  `all_businesses.csv`. This is the mixed-script case the whole pipeline is built
  for — the brief's two markets are Arabic-speaking, and OSM/Wikidata carry
  Arabic-script values.
- **A second shared junk key, and a hole in 352's fix:** `dedup.py:63`'s
  `lstrip("0")` strips ASCII `U+0030` only. `'٠'.lstrip("0")` is unchanged.
  Verified:
  ```
  input '0000000'   digits '0000000'    current(dedup.py:63) '+961'           352-fix ''
  input '٠٠٠٠٠٠٠٠'  digits '٠٠٠٠٠٠٠٠'   current '+961٠٠٠٠٠٠٠٠'               352-fix '+961٠٠٠٠٠٠٠'   <-- still shared
  ```
  `_MIN_DIGITS = 7` (`dedup.py:30`) counts characters, and 7 Arabic-Indic zeros
  is 7 characters, so the `len(digits) < _MIN_DIGITS` guard at `dedup.py:45`
  does not fire either. So 352's proposed one-liner
  (`national = digits.lstrip("0"); … if national else ""`) closes the ASCII
  all-zero key and **leaves a second corpus-wide shared key open**. Both keys are
  reachable from an OSM `phone` tag.
- **Fix:** transliterate to ASCII before any digit logic, and cap the length.
  ```python
  digits = re.sub(r"\D", "", unicodedata.normalize("NFKC", str(phone)))
  digits = digits.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
  ```
  `NFKC` alone is **not** enough — it does not fold Arabic-Indic digits to ASCII;
  an explicit translate table is required. Then reject `len(digits) > 15`
  (E.164's hard limit) and require at least one ASCII digit in `1-9`.

---

### S2 — a multi-number `phone` tag becomes one concatenated, undialable 20-digit key that still counts as a contact

- **Where:** `dedup.py:44` strips every non-digit including the separator, so
  `"Tel: +961 1 234 567 / +961 3 456 789"` becomes one digit run; `dedup.py:50-63`
  then treats it as a single number. Downstream, `main.py:138-145`
  `has_any_contact` strips non-digits itself and requires only
  `len(phone) >= 7`.
- **Breaks:** verified —
  ```
  'Tel: +961 1 234 567 / +961 3 456 789' -> '+96112345679613456789'  (20 digits)
  '+961 1 234 567; +961 3 456 789'       -> '+96112345679613456789'
  '+9611234567 or 0701234567'            -> '+96112345670701234567'  (20)
  '009611234567/009613456789'            -> '+9611234567009613456789' (22)
  ```
  E.164 caps a number at 15 digits, so none of these is dialable. Each also
  becomes a **unique, stable** key, so the two real numbers behind it never dedup
  against each other or against their standalone listings. And
  `has_any_contact` returns `True` for all four (20–22 raw digits), so the row is
  admitted to `sales_ready.csv` (`main.py:202-205`) — the CSV whose entire purpose
  is "call these businesses".
- **Reachability:** `osm.py:89` reads the raw `phone` / `contact:phone` tag with
  no parsing, and multi-number and `Tel:`/`Fax:` forms are common OSM conventions.
- **Fix:** in `normalize_phone`, split on `/`, `;`, ` or `, `,` and newlines
  **before** extracting digits, then take the first segment that normalises
  cleanly — and keep the rest as `alt_phones`. Separately, add
  `len(re.sub(r"\D", "", phone)) <= 15` to `has_any_contact` so an
  undialable string cannot reach the sales list.

---

### S2 — Arabic comma U+060C is not a split point, so name-dedup never fires for Arabic-script addresses

- **Where:** `dedup.py:75` (`address.split(",")`), used at `dedup.py:213-214`.
- **Breaks:** `str.split(",")` splits only on U+002C. Arabic text uses U+060C
  ARABIC COMMA, so `parts` has **length 1** and `parts[-1]` is the entire
  address. The `city` key half becomes a full unique street string, i.e. it is
  unique per record, i.e. **the name key never matches anything**.
- **Trigger (verified):**
  ```
  'شارع الحمرا، بيروت، لبنان'
     split(',')    -> ['شارع الحمرا، بيروت، لبنان']       # length 1
     _extract_city -> 'شارع الحمرا، بيروت، لبنان'          # the whole address
  ```
  ```python
  dedup([
    {"name":"مطعم البيرق","phone":None,"address":"شارع الحمرا، بيروت، لبنان","country":"LB","source":"osm"},
    {"name":"مطعم البيرق","phone":None,"address":"شارع الجلاء، بيروت، لبنان","country":"LB","source":"osm"},
  ])
  ```
  → **2 records** for one shop in two Beirut districts. Add an
  ASCII-comma transliteration of the same shop from Google and it is still 2.
  This is the **mirror image** of the `saudi arabia` case: instead of fusing
  everything, it fuses nothing — for the primary language of both target
  markets. It also compounds 352-S2-1 (per-source key vocabularies), because the
  Arabic-script half of the corpus uses a *different separator* from the
  Latin-script half.
- **Fix:** `re.split(r"[,،、，]", address)` (ASCII comma, U+060C, U+3001 ideographic
  comma, U+FF0C fullwidth comma) before taking the last non-empty segment.

---

### S3 — `normalize_name` crashes on a non-`str` name while `normalize_phone` coerces one — an asymmetry in the same module

- **Where:** `dedup.py:67` (`unicodedata.normalize("NFKD", name)` — no coercion)
  versus `dedup.py:42` (`raw = str(phone)`).
- **Breaks:** `dedup([{"name": 12345, "phone": None, "address": None, …}])`
  raises **`TypeError: normalize() argument 2 must be str, not int`** out of
  `dedup.py:67`. The sibling normalizer in the same file deliberately accepts
  `int` and `list`; this one does not, and `dedup.py:212`'s `or ""` guard only
  covers falsy values, not wrong types.
- **Why S3 not S1:** unreachable from every current path. `BusinessRecord.name`
  is `str | None` (`base.py:6`), all three scrapers build it from a `str`
  (`osm.py:63`, `google_places.py:268`, `wikidata.py:91`), and `load_master`
  reads `name` straight from `csv.DictReader` (`main.py:86`) with no cast.
  **But** `records = dedup(combined)` at `main.py:180` is **unguarded** — the
  scraper futures are wrapped in `try/except` at `main.py:162-168` while this
  call is not — so any exception here escapes `main()` and **none of the five
  CSVs are written** (writes begin at `main.py:208`). The 300-minute budget is
  spent and the run produces nothing.
- **Fix:** `name = str(name or "")` at `dedup.py:67`, matching `dedup.py:42`.
  Do the same for `address` at `dedup.py:75`, where `address=33.8` raises
  **`AttributeError: 'float' object has no attribute 'split'`**.

---

### S3 — `dedup()` has no guard on its own argument or its elements; every malformed call dies with a confusing traceback

- **Where:** `dedup.py:197` (no validation), `dedup.py:201` (`for record in records`),
  `dedup.py:202` (`record.get("phone")`).
- **Breaks:** verified —
  ```
  dedup(None)          -> TypeError: 'NoneType' object is not iterable
  dedup("abc")         -> AttributeError: 'str' object has no attribute 'get'
  dedup({"name": "X"}) -> AttributeError: 'str' object has no attribute 'get'
  dedup([None])        -> AttributeError: 'NoneType' object has no attribute 'get'
  dedup([["a", "b"]])  -> AttributeError: 'list' object has no attribute 'get'
  ```
  `dedup({"name": "X"})` is the nastiest: iterating a dict yields its **keys**,
  so the error names a string rather than the dict the caller actually passed.
- **Why S3:** all five are contract violations against `records: list[dict]`
  (`dedup.py:197`) and all five are unreachable from `main.py:180`, where
  `combined = raw_filtered + master` (`main.py:179`) is always a list of dicts.
  The severity argument is the same as the S3 above: the call site is unguarded
  and the CSVs are written only after it (`main.py:208-213`), so a future caller
  mistake costs the entire run.
- **Fix:** two lines at `dedup.py:198` —
  `if not isinstance(records, (list, tuple)): raise TypeError(f"dedup() expects a list of dicts, got {type(records).__name__}")`
  and inside the loop
  `if not isinstance(record, dict): raise TypeError(f"dedup(): record {i} is {type(record).__name__}, expected dict")`.
  A precise message beats `AttributeError: 'str' object has no attribute 'get'`.

---

### S3 — `_is_missing` and `dedup.py:205` disagree about what "empty" means, so `phone=0`/`False`/`[]` can be merged as if present

- **Where:** `dedup.py:86-90` (`_is_missing` returns `False` for every non-`str`
  that is not `None`) versus `dedup.py:205` (`if raw_phone else ""`, a pure
  truthiness test).
- **Breaks, verified side by side:**
  ```
  value=None    _is_missing=True   dedup.py:205 "is there a phone?"=False
  value=''      _is_missing=True   dedup.py:205                          =False
  value='  '    _is_missing=True   dedup.py:205                          =True   <-- disagree
  value=0       _is_missing=False  dedup.py:205                          =False  <-- disagree
  value=0.0     _is_missing=False  dedup.py:205                          =False  <-- disagree
  value=False   _is_missing=False  dedup.py:205                          =False  <-- disagree
  value=[]      _is_missing=False  dedup.py:205                          =False  <-- disagree
  value=nan     _is_missing=False  dedup.py:205                          =True   <-- disagree
  ```
  So `phone=0` keys on `(name, city)` (treated as absent) while `_pick` treats
  `0` as a **present, comparable** value. Verified:
  `{"phone": ""}` merged with `{"phone": 0}` on one name key returns
  **`phone = 0`** (because `_pick` at `dedup.py:168-171` sees `""` as missing
  and hands back the `0`), and `{"phone": False}` + `{"phone": None}` returns
  **`phone = False`**. Both then reach the `phone` column of
  `all_businesses.csv` as `0` / `False`.
- **Reachability:** weak and honestly so — `load_master` casts only
  `_FLOAT_FIELDS`/`_INT_FIELDS` and `website_live` (`main.py:43-44`, `:102-103`),
  so `phone` stays a string out of the CSV, and `False`/`0` cannot come from a
  CSV cell. No scraper produces them either. This is a **latent** trap for
  whoever adds a numeric or boolean field.
- **The one part that is not merely latent:** `_is_missing("  ") is True` but
  `dedup.py:205` sees `"  "` as truthy, so a whitespace-only phone *is* passed
  to `normalize_phone`. That is harmless today (`normalize_phone("  ")` → `""`,
  verified) and the code comment at `dedup.py:203-204` shows it was intended.
- **Fix:** one helper used by both sites — `_coerce_str(value) or ""` at
  `dedup.py:205`, so keying and merging share one definition of empty; and make
  `_is_missing` return `True` for falsy non-`str` scalars (`0`, `0.0`, `False`),
  which is harmless for `website_live` because that field is compared, not
  coalesced.

---

### S3 — `nan` in a numeric field passes every validity gate and is written to the master

- **Where:** `dedup.py:128-133` (`_validity` for `lat`/`lon` — a bare
  `float(value)` in `try/except`, no range or finiteness check),
  `dedup.py:86-90` (`_is_missing(nan)` is `False`).
- **Breaks:** `dedup([{"name":"X","phone":None,"address":"Beirut","country":"LB",
  "source":"osm","lat":nan,"lon":nan}])` returns the record with `lat=nan`
  intact. `write_csv` (`main.py:124-127`) emits the literal text `nan`; Excel
  and Google Sheets show it as a string. `enricher.infer_region`'s boxes
  (`enricher.py:91-93`) all evaluate `False` against NaN, so `region` becomes
  `None` on a record with a perfectly good Beirut address. `load_master` re-reads
  `"nan"` as `float("nan")` (`main.py:90-95`), so it is **permanent** in the
  cumulative master.
- **Why S3:** no current path produces NaN — Google returns `location.latitude`
  as a JSON number (`google_places.py:273-274`), and OSM returns a parsed float.
  The exposure is the CSV text path plus any future source that emits `"nan"`.
- **Fix:** `math.isfinite(v)` plus bounds in `_validity` for `lat`/`lon`
  (`-90 <= v <= 90`, `-180 <= v <= 180`) — this is the same one-line change
  345-S1-1 recommends, and it should be done once, in `_validity`, not twice.

---

## Not a bug, but worth knowing

- **The phone-key side is genuinely bulletproof for junk.** Every one of
  `None`, `""`, `"   "`, `"0"`, `0`, `0.0`, `False`, `-1`, `"abc"`, `"---"`,
  `"nan"`, `"NaN"`, `"+961 1 2"`, `[]`, `{}`, `3.14`, `"😀"`, and
  `"ext 12"` normalises to `""` and correctly falls through to the name key
  (`dedup.py:205-206`). `int` and `list` are accepted via `str()` at
  `dedup.py:42`. **Do not weaken this** — it is what stops the junk inputs from
  becoming shared keys, and it is the direct fix for the class of bug in
  `docs/audits/001-dedup-phone-normalization.md`.
- **`_MIN_DIGITS = 7` (`dedup.py:30`) is well chosen.** It rejects every
  shortcode, extension fragment and punctuation-only value I tried while
  accepting every real LB/SA national number. Its one gap is that it counts
  *characters*, so 7 Arabic-Indic zeros pass (see S2-4).
- **The reconciliation at `dedup.py:226-229` really is dead — independently
  confirmed.** 352 proved it by argument; I also ran a **40 000-trial randomised
  search** over the cross-product of 19 junk phones, 7 valid phones, 8 names,
  9 addresses, and 5 country values, recomputing both indexes by hand to isolate
  the drop path: **0 hits**. The invariant that makes it dead is *fragile and
  undocumented* — it holds only because `dedup.py:206` (`if key:`) refuses to
  insert `""` into `phone_index`. Anyone who "helpfully" lets `""` through, or
  who makes `normalize_phone` non-deterministic, turns this loop into a silent
  record-dropper. Either delete it or add a comment stating the dependency.
- **`len(out) <= len(in)` always.** Verified for `n = 0..5` and in every probe
  above. Each input either creates a key (net +1) or merges into an existing one
  (net 0). There is no path that duplicates a record and no path that emits a
  record that was not in the input.
- **`dedup()` never mutates its input.** Verified: mutating an output record's
  keys leaves the caller's dict untouched, because `dedup.py:210` and `:218` both
  copy with `dict(record)`. `main.py:183-184` (`for r in records: r["country"] =
  resolve_country(r)`) depends on this — if it were ever removed, dedup would
  corrupt `combined` in place.
- **Idempotent across three generations for every hostile set tested.** Verified
  for the country-collapse, Arabic-comma, Arabic-Indic-digit and bare-OSM-node
  sets: `dedup(dedup(x))`, `dedup(dedup(dedup(x)))` all hold the row count. The
  master CSV does not oscillate between runs, which is what you want from a
  cumulative store even while the keys are wrong.
- **No ReDoS and no quadratic blow-up.** `re.sub(r"\D", …)` (`dedup.py:44`),
  `unicodedata.normalize("NFKD", …)` (`dedup.py:67`) and `re.sub(r"\s+", " ")` are
  all linear with no nesting. Measured on this machine: `normalize_phone` 2 MB →
  29 ms, `normalize_name` 2 MB → 359 ms, a whole `dedup()` call with one 2 MB
  name → 398 ms. The only consequence of absurd lengths is a long dict key and
  the wrong winner on the `str(value)` tiebreak.
- **Emoji, NFC/NFD, NBSP and case are all handled correctly.** `"😀 Cafe"` keys
  without error, `"Café"` and `"Café"` both → `"cafe"`, `"\xa0Beirut\xa0"` →
  `"beirut"`, `"AL  BAIK"` → `"al baik"`. The Unicode gaps are narrow and
  specific: U+200B, U+00AD and Arabic tatweel U+0640 (S-classified above), all
  of which fix with one `unicodedata.category(c) == "Cf"` strip in
  `normalize_name`.
- **`dict` key order in the output is nondeterministic.** `dedup.py:192`
  iterates `set(a) | set(b)`, so merged key order varies with
  `PYTHONHASHSEED` (verified: two seeds gave
  `['phone','source','country','address','name']` and
  `['name','source','phone','address','country']`). **Harmless**:
  `write_csv` uses explicit `fieldnames=FIELDS, extrasaction="ignore"`
  (`main.py:125`), so CSV column order is fixed. It only shows up in `repr()`
  and in tests that compare dicts by `==` (which is order-insensitive) rather
  than by `list(d)`.
- **`dedup()` output order is `phone_records + name_records` (`dedup.py:232`).**
  Deterministic and stable, but it means CSV row order is not input order, so a
  diff between two runs shows spurious reordering when the phone/name split
  changes. Cosmetic.
- **`_pick("source")` (`dedup.py:173-174`) is the most robust branch in the
  module.** `_sources_of` (`dedup.py:143-145`) coerces with `str(... or "")` and
  filters empties, and `sorted()` makes the union stable across re-merges, so
  the `"google_places|osm"` composite that `lead_score`'s multi-source bonus
  depends on is reliable.

---

## Recommended order of work

1. **Refuse to key on an uninformative tuple** (S1-1). One `if not nm or not
   city:`
   emit-the-record-alone branch at `dedup.py:213-214`. Smallest diff in this
   report, and it is the only fix that makes `dedup()` provably lossless for
   every input it cannot identify. **Note this is required *in addition to*
   352's country-token fix, which does not cover `address=None`/`""`/`"Riyadh,"`.**
2. **Make `(address, lat, lon)` atomic in `_merge`** (S1-3). One `rank`
   evaluation picks the winning record; copy all three fields from it. Also
   closes the `lat`/`lon` splice from 345-S1-1 and 352-S2-4 in the same change.
3. **Fix the locality extractor** (S1-2, S2-5): split on
   `[,،、，]`, drop trailing country tokens, prefer OSM `addr:city`
   (`osm.py:84`) over `addr:district` (`osm.py:85`), and validate against the
   gazetteer already present at `enricher.py:52-76`. Fixes over-merging,
   under-merging and the Arabic-script case together.
4. **Transliterate digits to ASCII before any phone logic** (S2-4). `NFKC` plus an
   explicit `str.maketrans` table for U+0660–U+0669 and U+06F0–U+06F9, then
   reject `len > 15` and require an ASCII `1-9` digit. **Do this before or with
   352's `lstrip` fix** — `lstrip("0")` alone leaves the Arabic-Indic all-zero
   key open.
5. **Stop fabricating country codes** (S2-3). At `dedup.py:58-63`, when no
   listed code matches and the digit count exceeds the plausible national length
   for `cc`, return `""` instead of prepending `cc`. Closes the `+961`-from-`+33`
   fabrication that `osm.py:103` currently guarantees.
6. **Split multi-number `phone` tags on `/`, `;`, `,`, ` or ` before extracting
   digits** (S2-6), and add a `<= 15` digit guard to `has_any_contact`
   (`main.py:142`) so an undialable string cannot reach `sales_ready.csv`.
7. **Harden the boundary** (S3 ×3, S3 ×4, S3 ×5): `str()` coercion in
   `normalize_name` and `_extract_city`, `isinstance` guards on `dedup()`'s
   argument and elements, `math.isfinite` + bounds in `_validity` for
   `lat`/`lon`, and one shared "is empty" helper for `dedup.py:205` and
   `_is_missing`.
8. **Write the edge tests** — there are none. One table-driven test over §B–§F
   above, plus three properties worth asserting forever:
   `len(dedup(x)) <= len(x)`; `dedup` does not mutate its input; and **no output
   record's `address`, `lat` and `lon` come from different input records** —
   that last one is the machine-checkable form of S1-3 and would have caught it.