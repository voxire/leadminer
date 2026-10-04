# 200 — `_is_missing` (dedup.py:79): the "carries no information" contract holds for strings and fails for every non-string

## Verdict

`_is_missing` is **total** — I probed it with 23 hostile values (`b"\xff"`, a lone surrogate, a 5 MB string, `float("nan")`, `object()`) and it never raises, so there is no traceback to find in this function and no crash finding here. The real defect is the opposite: it is a **false-negative machine**. Its `else: return False` fallthrough at `dedup.py:90` means *any* non-`None`, non-`str` value is declared to "carry information" — including `float("nan")`, which `_validity("lat", nan)` then scores the **maximum** 3 for at `dedup.py:133`, making NaN the *preferred* candidate over a real GPS coordinate in `_pick`. A poisoned `lat` in `data/all_businesses.csv` therefore destroys the correct coordinate permanently and silently on every future run. Separately, `.strip()` at `dedup.py:89` covers Unicode `Zs`/`Zl`/`Zp` whitespace but **not** category `Cf` format characters, so `"\u200b"` (zero-width space) and friends are treated as real content.

---

## The contract, read literally

```python
# dedup.py:79-90
def _is_missing(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False          # <-- every non-str, non-None value: "carries information"
```

Docstring (`dedup.py:80-85`): *"True when a field carries no information."* Three of the 22 columns are not strings — `lat`/`lon` are `float | None`, `rating` is `float | None`, `review_count` is `int | None`, `website_live` is `bool | None` (`scrapers/base.py:9-10,16,19`). Those are exactly the fields the `else` branch waves through without inspection.

Callers, all of which inherit the verdict:
- `dedup.py:122` — `_validity` returns `-100` for anything `_is_missing` calls missing.
- `dedup.py:168` / `dedup.py:170` — `_pick` substitutes the other side's value when this side is "missing".
- Reached for **every key** in `set(a) | set(b)` at `dedup.py:192`, not just the 22 declared columns.

---

## Boundary and hostile-input enumeration

`D` = correctly handled · `W` = silently wrong value · `C` = crashes. Nothing in the list is `C`; see "Not a bug" for why that is structural rather than lucky.

| # | Input | `_is_missing` | Cls | Notes anchored to code |
|---|---|---|---|---|
| 1 | `None` | `True` | D | `dedup.py:86-87`, the canonical case |
| 2 | `""` | `True` | D | `dedup.py:88-89`; this is the bug the docstring says it fixed |
| 3 | `"   "`, `"\t\n\r "` | `True` | D | `dedup.py:89` |
| 4 | `"\u00a0"` NBSP only | `True` | D | `str.isspace()` covers all of `Zs`/`Zl`/`Zp` |
| 5 | `"\u3000"` ideographic space | `True` | D | same |
| 6 | `"\u2028"` / `"\u2029"` line / paragraph sep | `True` | D | same (`Zl`/`Zp`) |
| 7 | `0`, `0.0`, `-0.0` | `False` | D | **Correct** for `review_count=0`; wrong for `lat=0.0` (Null Island) but that is a range check, not a presence check |
| 8 | `False` | `False` | D | **Correct and load-bearing**: `website_live=False` means DEAD (`enricher.py:154-155`), which is +20 lead points (`enricher.py:301-304`) |
| 9 | `-1`, `-1.0` | `False` | **W** | Out-of-domain sentinel counted as present; see S2 |
| 10 | `float("nan")` | `False` | **W** | Amplified to maximum by `_validity`; see S1 |
| 11 | `float("inf")`, `float("-inf")` | `False` | **W** | same mechanism as #10 |
| 12 | `float("1e999")` | `False` | **W** | Overflows to `inf` at `main.py:93` before it ever reaches here |
| 13 | `b""` | `False` | **W** | Contract violation (`base.py` has no bytes); empty bytes has no information |
| 14 | `[]`, `{}`, `set()` | `False` | **W** | Same; see S3 |
| 15 | `"مطعم بيروت"` (Arabic) | `False` | D | Real content |
| 16 | `"مطعم\u200b"` (Arabic + ZWSP) | `False` | D | **Correct** — there *is* a name; only the wholly-invisible case is wrong |
| 17 | `"\u200b"` ZWSP only | `False` | **W** | Zero information. See S2 |
| 18 | `"\u200c"`, `"\u200d"` ZWNJ / ZWJ | `False` | **W** | Zero information |
| 19 | `"\u00ad"` soft hyphen | `False` | **W** | Zero information |
| 20 | `"\u2060"` word joiner | `False` | **W** | Zero information |
| 21 | `"\ufeff"` BOM | `False` | **W** | Zero information. `main.py:82-84` already got burned by this char in the *header* |
| 22 | `"N/A"`, `"-"`, `"0"`, `"none"`, `"nan"` | `False` | **W** (mild) | Placeholder text counted as data; visible, so cheaper to catch than #17-21 |
| 23 | `"\U0001f600"` emoji | `False` | D | Real content |
| 24 | `"\u200b\U0001f600"` | `False` | D | Real content |
| 25 | `"x" * 5_000_000` | `False` | D | Returns immediately; no memory/time cliff |
| 26 | `"\ud800"` lone surrogate | `False` | D | `.strip()` does not validate UTF-8; no `UnicodeError` |
| 27 | `1j`, `object()` | `False` | **W** | Contract violation, no crash |
| 28 | `"\u0000"` NUL | `False` | D | Not whitespace; treated as content |

Measured, not assumed — the Cf gap is exactly the Unicode **format** category:

```
U+00A0 NO-BREAK   isspace=True   _is_missing=True    cat=Zs
U+2028 LINE      isspace=True   _is_missing=True    cat=Zl
U+3000 IDEOGRAPH isspace=True   _is_missing=True    cat=Zs
U+200B ZERO-WIDTH-SPACE  isspace=False  _is_missing=False  cat=Cf   <-- gap
U+00AD SOFT HYPHEN       isspace=False  _is_missing=False  cat=Cf   <-- gap
U+2060 WORD JOINER       isspace=False  _is_missing=False  cat=Cf   <-- gap
U+FEFF ZERO WIDTH NBSP   isspace=False  _is_missing=False  cat=Cf   <-- gap
```

---

## Findings

### S1 — `float("nan")` scores maximum validity and permanently destroys real coordinates

- **Where:** `dedup.py:90` (`return False` fallthrough), `dedup.py:128-133` (`_validity` for `lat`/`lon`), `dedup.py:184` (the `str(value)` tiebreak), `dedup.py:187` (whoever wins the tuple), `main.py:93` (how nan enters the record), `main.py:150` + `main.py:209` (why it never heals), `httpclient.py:113` (a second entry route)
- **Breaks:** `_validity`'s coordinate branch is a bare "does `float()` accept this" test with **no finiteness check and no range check**:
  ```python
  # dedup.py:128-133
  if field in ("lat", "lon"):
      try:
          float(value)
      except (TypeError, ValueError):
          return 0
      return 3          # <-- nan, inf, -inf, 0.0 all score the MAXIMUM
  ```
  So when `_pick` ranks two coordinates at `dedup.py:180-185`, a NaN ties a real coordinate on trust and on validity (3 vs 3), `lat` is not in `_VOLATILE` (`dedup.py:114`) so recency is `""` on both sides, and the final tiebreak at `dedup.py:184` is `str(value)` — where `"nan" > "33.8938"` lexicographically. **The NaN wins.**
- **Trigger** — a real `lat`/`lon` discarded in favour of NaN, reproduced end-to-end:
  ```python
  master = {"name":"Cafe Hamra","phone":"+961 1 555 123","country":"LB",
            "lat":float("nan"),"lon":float("nan"),"rating":float("nan"),
            "address":"Hamra St, Beirut","website":None,"source":"google_places",
            "scraped_at":"2026-08-01T00:00:00Z","email":None}   # what load_master() yields
  fresh  = {"name":"Cafe Hamra","phone":"+9611555123","country":"LB",
            "lat":33.8945,"lon":35.5021,"rating":4.3,
            "address":"Hamra St, Beirut","website":"https://cafehamra.example",
            "source":"google_places","scraped_at":"2026-10-03T00:00:00Z","email":None}
  dedup.dedup([master, fresh])[0]
  ```
  Actual output:
  ```
  name='Cafe Hamra'  lat=nan  lon=nan  rating=4.3
  website='https://cafehamra.example'  source='google_places'  scraped_at='2026-10-03T00:00:00Z'
  ```
  `33.8945` / `35.5021` are gone. Two entry routes for the NaN, both verified:
  1. **CSV text.** `main.py:93` does `row[field] = float(row[field])`, and `float()` accepts `"nan"`, `"NaN"`, `"inf"`, `"-Infinity"`, `"1e999"`, and any digit string ≥ 309 chars (overflow). I confirmed all of these parse.
  2. **A JSON API.** `httpclient.py:113` is `_json.loads(...)` with default settings, and CPython's `json.loads` **accepts the non-standard bare tokens** — verified: `json.loads('{"lat": NaN}')` → `nan`, `json.loads('{"lat": Infinity}')` → `inf`. Python's own `json.dumps` emits those tokens by default (`allow_nan=True`), so any upstream that serialises a NaN coordinate poisons `osm.py:68`'s `el.get("lat")` directly.
- **Why it never heals:** `main.py:150` re-reads `all_businesses.csv` every run and `main.py:209` writes it back. `csv.DictWriter` serialises `float("nan")` as the literal text `nan`, so the cell round-trips verbatim. I ran the accumulate loop five times:
  ```
  run 1: lat=nan lon=nan rating=4.3
  run 2: lat=nan lon=nan rating=4.3
  run 3: lat=nan lon=nan rating=4.3
  run 4: lat=nan lon=nan rating=4.3
  run 5: lat=nan lon=nan rating=4.3
  ```
  The fresh, correct coordinate is re-fetched from Google every single run and re-discarded every single run. Once written, the real value is **not in the master any more** — this is unrecoverable silent data loss requiring manual CSV surgery.
- **Blast radius:** `enricher.py:92-93`'s `lat_min <= lat <= lat_max` is `False` for NaN, so `infer_region` returns `None` for that row forever — the business silently drops out of the "By region" breakdown at `main.py:230-234`. The CSV cell reads `nan`, which an operator scanning the file will read as a value. `rating` escapes only by luck: `_validity("rating", nan)` is 0 because `dedup.py:139` does range-check `0 <= v <= 5` — the coordinate branch has no such check.
- **Fix:** two lines. Add `if not math.isfinite(v): return 0` to the `lat`/`lon` branch at `dedup.py:128-133`, and reject non-finite casts in `load_master` (`main.py:90-95`). Belt-and-braces: make `dedup.py:90` `return isinstance(value, (int, float)) and value != value` (NaN-only) or route all numeric fields through a `math.isfinite` guard.

### S2 — Zero-width / format characters defeat `.strip()` and remove leads from the pitch list

- **Where:** `dedup.py:88-89`
- **Breaks:** `str.strip()` removes characters for which `str.isspace()` is true — that is Unicode `Zs`, `Zl` and `Zp`. It does **not** remove category `Cf`. So `"\u200b"`, `"\u200c"`, `"\u200d"`, `"\u00ad"`, `"\u2060"`, `"\ufeff"` all survive `.strip()`, `not value.strip()` is `False`, and `_is_missing` reports *"carries information"* for a value that is 100% invisible.
- **Trigger:** `website = "\u200b"` (a paste artefact from an OSM volunteer tag or an HTML copy-paste). Then:
  - `_pick` treats it as a present value, so it competes with — and can win against — nothing, but at minimum it is never cleared out of the record.
  - `_validity("website", "\u200b")` returns `0` (`_URL_OK` at `dedup.py:117` does not match), **not** `-100`, because `_is_missing` said "present".
  - `main.py:199` `if r.get("website")` is a pure truthiness test → `True`. `main.py:200` `if not r.get("website")` → `False`.
  - Verified: the row lands in `with_websites.csv` and is **excluded from `without_websites.csv`** — which `BRIEF.md:54` identifies as the new-site pitch target list, i.e. the product.
  - `enricher.py:107` `if record.get("website"): score += 1` also awards a `completeness_score` point, pushing the row into `qualified_businesses.csv`.
- **Same mechanism, other fields:** `email = "\u200b"` → `enricher.py:105` awards a completeness point. `name = "\ufeffCafe"` → `_is_missing` correctly says "present" (it is), but `normalize_name` at `dedup.py:67-69` does not remove the BOM, so `normalize_name("\ufeffCafe Hamra")` → `'\ufeffcafe hamra'` and the name-key at `dedup.py:214` fails to match `"cafe hamra"`. I confirmed `dedup([a, b])` returns **2** records for what is one business. That is a duplicate-row generator, not a `_is_missing` bug, but it is the same invisible-character root cause and `main.py:82-84` already documents having been burned by this exact character in the CSV *header*.
- **Frequency:** low. I am not claiming a large volume here — I am claiming that when it occurs the failure is invisible and lands in the wrong sales list.
- **Fix:** strip category `Cf` before the whitespace check, e.g. `not "".join(c for c in value if not unicodedata.category(c).startswith("C")).strip()`, or a cheap targeted `re.sub(r"[\u200b-\u200f\u2060\ufeff\u00ad]", "", value)` at `dedup.py:88-89`. `unicodedata` is already imported at `dedup.py:2`.

### S2 — Out-of-domain numerics are "present", beat `None`, and earn the bad-rating bonus

- **Where:** `dedup.py:90`, amplified at `enricher.py:312-314`
- **Breaks:** `_is_missing(-1)` is `False`, so in `_pick` at `dedup.py:168-171` a `-1` **replaces** a `None`, because `None` is "missing" and `-1` is not. Verified: `_pick("rating", {"rating": -1.0, ...}, {"rating": None, ...})` → `-1.0`.
- **Trigger / impact:** `rating=-1.0` then hits `enricher.py:312-313`:
  ```python
  rating = record.get("rating")
  if rating is not None and rating < 4.0:
      score += 10          # "Low rating = pain point worth pitching"
  ```
  `-1.0 < 4.0` is `True`, so a business is handed a **+10 pain-point penalty** for a garbage value, outranking a business that simply has no rating data (which gets 0). Measured: `lead_score` rating bonus is `nan=0, -1=10, 0.0=10, 4.8=0, None=0`. The rating is also written into `all_businesses.csv` as `-1` and re-read as `-1` forever.
- **Not reachable from the three scrapers** (Google/Overpass/Wikidata will not return `-1`), but the master CSV is operator-editable and cumulative, and `_validity` already scores it `0` (`dedup.py:139` rejects it) — meaning the function *knows* the value is implausible and then treats it as present anyway.
- **Fix:** make the presence decision field-scoped, not type-scoped: reject `rating` outside `[0, 5]` and `lat`/`lon` outside their region's bounding box as *missing* rather than merely implausible.

### S3 — No type gate: containers and bytes sail through as "present"

- **Where:** `dedup.py:79` (no annotation on `value`), `dedup.py:90`, `dedup.py:184`
- **Breaks:** `def _is_missing(value) -> bool:` has no annotation on its parameter, so nothing rejects a value outside the `BusinessRecord` contract (`scrapers/base.py:5-28`). Verified: `_pick("website", {"website": []}, {"website": None})` → `[]`; same for `{}`, `set()`, `b""`. All are declared "present", win over `None`, and reach `write_csv` (`main.py:125`, `extrasaction="ignore"` only drops unknown *columns*, not unknown values), where `csv` renders them as `"[]"`, `"{}"`, `"b''"`.
- **Reachability:** not reachable today. `_merge` iterates `set(a) | set(b)` at `dedup.py:192`, but `load_master` only ever produces keys from the CSV header and the three scrapers set exactly the 22 declared columns — so `_pick` only ever sees `str`, `float`, `int`, `bool` or `None`.
- **Fix:** annotate `_is_missing(value: object) -> bool` and add an explicit allow-list, or add a `validate_record()` at the scraper boundary. This is cheap insurance for the ~400-agent refactor, not a live bug.

---

## Not a bug, but worth knowing

- **`_is_missing` cannot raise. At all.** The body is an identity test, an `isinstance` test, and `.strip()` reachable only on `str`. No attribute access, no arithmetic, no regex, no I/O. I probed 23 hostile values — including `b"\xff"` (invalid UTF-8), `"\ud800"` (lone surrogate), `"x" * 5_000_000`, `object()`, `1j` and `float("nan")` — and all 23 returned normally. Every traceback risk in this merge path lives in `_validity` (`dedup.py:125`, `127` `str(value).strip()`; `dedup.py:130`, `137` `float(value)`, though those are `try`-guarded) or downstream in `enricher.py`. Do not spend audit time looking for a crash here.
- **`_is_missing(0)` and `_is_missing(False)` returning `False` is correct, and a blanket falsy "fix" would be a regression.** `website_live=False` is DEAD — real, valuable information (`enricher.py:154-155`) worth +20 lead points as a confirmed rebuild pitch (`enricher.py:301-304`). `review_count=0` genuinely means zero reviews. Any fix must be **field-scoped**, never `if not value: return True`.
- **Stale-value tiebreaks on the computed columns are harmless.** `_pick`'s `str(value)` tiebreak (`dedup.py:184`) does let a stale `completeness_score=5` beat a fresh `0` (I confirmed `"5" > "0"`), but all four computed columns are overwritten after dedup — `enricher.py:349-350`, then `main.py:190-197`. Do not spend time here.
- **`0.0` for `lat`/`lon` is Null Island, not a real location**, and `_is_missing` calls it present with full validity. `infer_region` returns `None` for it. But this is a range check, not a presence check, and it belongs with the S1 `isfinite` fix, not with `_is_missing`.
- **`scraped_at` is the one field where `_is_missing`'s permissiveness could reach a traceback** — `_pick` at `dedup.py:177` does `max(av, bv)` on strings, and `_is_missing(2026.0)` is `False`, so a float survives the gate. Verified: `_pick("scraped_at", {"scraped_at": "2026-10-03"}, {"scraped_at": 2026.0})` raises `TypeError: '>' not supported between instances of 'float' and 'str'`. **Not reachable today** — `main.py:87-104` never casts `scraped_at` and all three scrapers emit `str` — but it is the one place where loosening the contract later would crash the run rather than corrupt a value.
- **`dedup.py:212` asks the same question inconsistently.** The name key is built with `record.get("name") or ""` and `normalize_name("")` → `""`, so *every* record with neither a name nor a city collapses into the single key `("", "")` — a mass merge. `_is_missing` is not consulted on that path at all. Out of this lens, but it is the same "is this field empty?" decision made two different ways in one file.
- **NBSP is handled correctly.** If you read `main.py:82-84`'s BOM anecdote as "this codebase can't handle unicode", that is too broad: `"\u00a0"` → `_is_missing` `True` (verified). The gap is specifically the `Cf` format characters, which is a much narrower and much more precisely fixable target than "unicode".

---

## The single most dangerous input

**`float("nan")` in `lat` (or `lon`).**

It is the only input in the entire enumeration that is wrong in *three* compounding ways at once, and every one of them pushes in the same direction:

1. **It is the maximal false negative.** The docstring contract is "carries no information" (`dedup.py:80`). `nan` carries *literally* none, yet `dedup.py:90` returns `False` — it is declared to carry information. Contrast with `"\u200b"` (S2), which also carries none but scores `_validity` 0 and therefore can only win against a field that is genuinely empty. NaN is *actively preferred*.
2. **The scoring function rewards it maximally.** `_validity("lat", nan)` returns **3** — the best score available (`dedup.py:133`), because the coordinate branch is `try: float(value) except: return 0; return 3` with no `isfinite` and no range check. `rating` gets lucky and is range-checked at `dedup.py:139`; `lat`/`lon` got no such check.
3. **It also wins the deterministic tiebreak.** `dedup.py:184`'s `str(value)` compares `"nan" > "33.8938"` → `True`.

So a NaN does not merely fail to be cleaned up — it **outranks correct data**, overwrites it in the cumulative master (`main.py:209`), is re-read verbatim as the literal CSV text `nan` on the next run (`main.py:150`, and `csv.DictWriter` serialises `nan` as `"nan"` — verified), re-wins the merge, and does so indefinitely. I ran the accumulate loop five times and the correct `33.8945` / `35.5021` was discarded every single time. The good data is **destroyed on disk**, not merely shadowed.

The consequence is also the quietest available: no exception, no warning, no log line. `infer_region` just returns `None` (`enricher.py:92-93`), the row vanishes from the regional breakdown (`main.py:230-234`), and the CSV cell reads `nan` — indistinguishable from data to anyone not specifically looking for it. Against a product whose entire purpose is a trustworthy lead list, a merge function that can permanently destroy a verified coordinate and never say so is the failure mode worth fixing first.

Two entry routes make this reachable without anyone hand-editing a file: `main.py:93`'s `float()` accepts the CSV text `"nan"` / `"NaN"` / `"inf"` / `"1e999"` / any 309+-digit number, and `httpclient.py:113`'s default `json.loads` accepts the non-standard bare tokens `NaN` / `Infinity` / `-Infinity` from any upstream JSON response — which is exactly what a Python service emitting `json.dumps` of a NaN coordinate produces.

---

## Recommended order of work

1. **S1** — `math.isfinite` guard in `_validity`'s `lat`/`lon` branch (`dedup.py:128-133`), plus the same guard on the `float()` casts in `load_master` (`main.py:90-95`). Then add a field-scoped range check so `0.0` (Null Island) is also rejected.
2. **S2 (invisible chars)** — strip Unicode `Cf` before the whitespace test at `dedup.py:88-89`. `unicodedata` is already imported at `dedup.py:2`. Fix `normalize_name` (`dedup.py:67-69`) in the same change to strip `Cf` from both sides of the dedup key, which also fixes the BOM-in-a-value duplicate-row bug demonstrated above.
3. **S2 (out-of-domain numerics)** — make the "is this plausible" boundary in `_validity` also drive "is this missing" in `_is_missing`, field by field. Do **not** introduce a blanket falsy check; that would destroy `website_live=False` and the dead-site pitch signal.
4. **S3** — annotate `_is_missing(value: object)` and add a record validator at the scraper boundary, so the `[]` / `{}` / `b""` class is caught before it reaches `_merge`.
5. **Add a regression test** pinning the table above. This function is 12 lines with a rich edge surface and, per `BRIEF.md:11`, the project has zero tests. A 20-line parametrized table over these 28 inputs would lock in both the correct cases (1-8, 15-16, 23-28) and the fixed ones (9-14, 17-22).