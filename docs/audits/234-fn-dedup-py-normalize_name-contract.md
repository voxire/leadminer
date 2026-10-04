# 200 — `normalize_name` (`dedup.py:66`) type contract vs. practice

## Verdict

The annotation `def normalize_name(name: str) -> str` (`dedup.py:66`) is **honest in
both directions**, and I could not break it. Every current caller passes a `str`;
every reachable input returns exactly `str` (verified even from a `str` subclass,
because `re.sub` returns plain `str`); the function is idempotent; and it reads and
writes **zero** record fields, so there is nothing outside `BusinessRecord` to
report. On the narrow question "can a caller violate the contract silently on the
type axis" the answer is **no**.

The damage is entirely on the **value** axis, and it is the same damage the phone
path already learned to avoid. `normalize_name` is the only definition of business
identity in the pipeline, it can legitimately return `""`, and `dedup.py:214` uses
that `""` as a **live, shared, unguarded** dict key — while its sibling
`normalize_phone` explicitly documents `""` as a "must fall through" sentinel
(`dedup.py:34-38`) and *is* guarded by `if key:` at `dedup.py:206`. Second, the
"strip accents/diacritics" filter at `dedup.py:68` uses
`unicodedata.combining(c)`, whose character class is far narrower than the author
intended: it removes only non-zero canonical-combining-class marks, so the Arabic
format characters (ZWNJ/ZWJ/LRM/RLM) and the invisible U+00AD/U+FEFF **survive**
into the key and split one business into two.

## Contract answers (the four questions, answered directly)

### 1. Actual input type vs. declared `str`

Declared `str`. Actual: **`str`, always, at the only call site.** The declaration
holds.

`dedup.py:212` is `name = record.get("name") or ""`, and `dedup.py:214` is the sole
consumer of that local. Tracing every producer of `name`:

| Producer | Value type | Guard |
|---|---|---|
| `osm.py:63` `tags.get("name") or tags.get("name:en") or tags.get("name:ar")` | `str \| None` | `osm.py:64` `if not name: continue` |
| `wikidata.py:91` `row.get("itemLabel", {}).get("value")` | `str \| None` | `wikidata.py:92` `if not name or name.startswith("Q"): continue` |
| `google_places.py:268` `(place.get("displayName") or {}).get("text") or ""` | `str \| None` | `google_places.py:269` `if not name: continue` |
| `main.py:86-104` `load_master` via `csv.DictReader` | `str \| None` | `main.py:88-89` blank → `None` |

`main.py:43-44` casts only `lat/lon/rating` and
`review_count/completeness_score/lead_score`. **`name` is never cast**, so no
numeric coercion can arrive from the master CSV, and no JSON producer yields a
non-`str`. `or ""` converts the only falsy non-`str` case (`None`) into `""`.

**But `name: str | None` (`base.py:6`) means `None` is a legal record value, and the
declaration `name: str` silently narrows it at the boundary.** The narrowing is
legitimate — the caller's contract is "give me a `str` or I will fail" — but it is
documented nowhere, and `normalize_name` itself has **zero** tolerance (see S2).

### 2. Actual output type vs. declared `str`

Declared `str`. Actual: **always exactly `str`.** Verified:

```
normalize_name("")             -> ''        (type str)
normalize_name("   ")          -> ''        (type str)
normalize_name("\t\n ")        -> ''        (type str)
normalize_name("Café  de  Beyrouth") -> 'cafe de beyrouth'
class S(str): normalize_name(S("Café")) -> 'cafe'   type(r) is str -> True
```

`unicodedata.normalize` returns `str`, `str.join` returns `str`, `re.sub` on a `str`
returns `str`, `.lower()`/`.strip()` return `str`. **No `str` subclass leaks**, so
the output is not merely `str`-compatible, it is exactly `str`.

Idempotency also holds, which matters because `main.py:179` accumulates the master:
`normalize_name(normalize_name(x)) == normalize_name(x)` for all 11 samples tested
(`Café`, `café`, `CAFÉ`, `Ångström`, `ﬁne`, `Al‑Salam`,
`مطعم أبو شقير`, `Al  Salam`, `\ufeffCafe`, `Cafe\xa0Bar`, …). There is no
run-over-run key drift caused by non-idempotency.

**The declared return type is the one part of this function that is entirely
accurate.** I am not going to manufacture a finding where there isn't one.

### 3. Can a caller violate the contract silently?

**On the type axis: no, and there is no reachable violation.** Every
truthy-non-`str` `name` raises loudly rather than silently:

```
dedup([{"name": 123,     ...}]) -> TypeError: normalize() argument 2 must be str, not int
dedup([{"name": 4.5,     ...}]) -> TypeError: ... not float
dedup([{"name": True,    ...}]) -> TypeError: ... not bool
dedup([{"name": ["Cafe"],...}]) -> TypeError: ... not list
dedup([{"name": {"n":1}, ...}]) -> TypeError: ... not dict
dedup([{"name": b"Cafe", ...}]) -> TypeError: ... not bytes
```

Falsy non-`str` values are silently coerced to `""` by `or ""` and land in the
degenerate bucket (S1) rather than raising:

```
dedup([{"name": 0    , ...}]) -> 1 row, key half ''
dedup([{"name": 0.0  , ...}]) -> 1 row, key half ''
dedup([{"name": False, ...}]) -> 1 row, key half ''
dedup([{"name": []   , ...}]) -> 1 row, key half ''
dedup([{"name": {}   , ...}]) -> 1 row, key half ''
```

**On the value axis: yes, and this is the whole problem.** `""` is a perfectly valid
`str`, so nothing can catch it — and it is reachable from every scraper, because
`bool("   ")` is `True`, so a whitespace-only name sails through all three
`if not name: continue` guards:

```
'   '    -> guard passes (bool=True)  ->  normalize_name -> ''   len=0
'\xa0'   -> guard passes (bool=True)  ->  normalize_name -> ''   len=0
'\u200b' -> guard passes (bool=True)  ->  normalize_name -> '\u200b' len=1
'\ufeff' -> guard passes (bool=True)  ->  normalize_name -> '\ufeff'  len=1
```

### 4. Fields this function reads or writes that are not in the `TypedDict`

**None. Zero. That is the finding.**

`normalize_name` has no `record` parameter. It is a pure `str -> str` function. It
touches no field, declared or undeclared, and it mutates nothing. Confirmed by
reading `dedup.py:66-69` in full — the body only rebinds its own local.

The actionable list is therefore the **inverse**, and it is long. `normalize_name`
ignores 21 of the 23 `BusinessRecord` keys, and the two that would make the dedup
key *correct* are among them:

| Ignored field | Declared | Why it matters for the key |
|---|---|---|
| `region` | `base.py:9` | **Already populated before `dedup` runs.** `google_places.py:275` calls `infer_region(None, lat, lon, country=country)` at scrape time and `google_places.py:284` stores the result. Verified against the KSA boxes at `enricher.py:71`: `lat=24.71, lon=46.68` → `region="Riyadh"`. That is the correct locality, sitting in the record, discarded in favour of `_extract_city`'s last-comma-segment. |
| `country` | `base.py:10` | The key at `dedup.py:214` has no country component at all, so `("starbucks", "")` is one bucket for Riyadh and Beirut. See S1. |
| `lat`, `lon` | `base.py:11-12` | The `_LB_COORD_REGIONS` / `_KSA_COORD_REGIONS` tables already exist at `enricher.py:58-76`. A 2-decimal geo-bin would disambiguate branches for free. |
| `phone` | `base.py:13` | Deliberately excluded — the phone path owns that. Correct decision. |

The structural problem is that the pipeline's **only** declared type boundary is a
function that sits *below* the data. The thing that actually needs a type — the
identity key `tuple[str, str]` at `dedup.py:214`, and the
`name_index: dict[tuple, dict]` at `dedup.py:199` — is annotated with bare
generics, so even a running checker would accept a heterogeneous key.

### 5. Places the return value is used in a way that breaks on an unexpected shape

**One production consumer, and it is the bug.** Confirmed by grep across the whole
repo (including `docs/`): `normalize_name` is called at `dedup.py:214` and nowhere
else in code. `main.py:29` imports only `dedup, normalize_phone`; `cli.py` never
imports it.

```
dedup.py:212  name  = record.get("name") or ""
dedup.py:213  city  = _extract_city(record.get("address"))
dedup.py:214  key   = (normalize_name(name), city)      <-- NO `if key:` guard
dedup.py:215  if key in name_index:
dedup.py:216      name_index[key] = _merge(name_index[key], record)
dedup.py:218      name_index[key] = dict(record)
```

Compare the phone branch immediately above it:

```
dedup.py:203-204  # comment: normalize_phone returns "" for junk, which must fall
                  #  through to the name key rather than becoming its own
                  #  (shared, empty) phone key.
dedup.py:205      key = normalize_phone(raw_phone, ...) if raw_phone else ""
dedup.py:206      if key:                                 <-- GUARD EXISTS
dedup.py:211      else:
```

The sentinel discipline exists on one side of the `if/else` and is absent on the
other. That is the whole S1.

Breakage modes of the return value, ranked:

1. **`""` → live shared key.** A crash-free, silent, high-blast-radius corruption.
   S1 below.
2. **Non-`str` → `TypeError` at `dedup.py:214`.** Unreachable today; a latent
   *crash* if the boundary moves. S2 below. Note `main.py:162-168`'s `try/except`
   wraps **only** the scraper futures — `dedup` at `main.py:180` is unguarded, so
   one such record aborts a 300-minute GitHub Actions run after the scrapes
   completed.
3. **`str` subclass →** no breakage; verified the return is exactly `str`.
4. **A long name → no breakage.** No length cap anywhere; the key is unbounded, so
   `name_index` holds one entry per distinct spelling (see S2 on punctuation).

## Findings

### S1 — `normalize_name` can return `""`, and `dedup.py:214` uses it as a live dedup key with no sentinel guard

- **Where:** `dedup.py:66-69` (the `""` return), `dedup.py:212-218` (the unguarded
  consumer), contrast `dedup.py:203-206` (the guarded phone branch). Data deletion
  happens in `_merge` at `dedup.py:190-194`; the fabricated multi-source credit is
  read by `enricher.py:316-317`.
- **Breaks:** A record whose `name` normalizes to `""` and whose `address` yields
  `""` keys to the literal tuple `("", "")`. **Every such record in the entire run
  — all countries, all categories, all sources — collapses into one row.** The
  losing records' `website`, `email`, `instagram`, `whatsapp`, `linkedin` are
  **deleted**, not unioned: `_pick` (`dedup.py:167-187`) returns exactly one value
  per field. And the survivor's `source` becomes the pipe-joined union of every
  record it swallowed (`dedup.py:173-174`), which `enricher.py:316-317` reads as
  multi-source confirmation and awards a **+5 lead score the record never earned**.
  No warning, no log line, no counter. `main.py:181` prints one integer.
- **Trigger** (executed, no network):
  ```python
  junk  = {"name": None, "category": "restaurant", "phone": "12345",
           "address": None, "source": "osm"}
  real1 = {"name": "Restaurant Al Baik", "category": "restaurant", "phone": None,
           "address": None, "source": "osm"}
  real2 = {"name": "Cafe Mocha", "category": "cafe", "phone": None,
           "address": None, "source": "wikidata"}
  dedup([junk, junk, junk, real1, real2])
  # -> 3 rows, but the 3 junk records are now ONE row
  #    survivor: name=None | source=osm   (Al Baik and Mocha survive intact)
  ```
  The junk phone does not save it: `normalize_phone("12345")` returns `""`
  (`dedup.py:45-46`, below `_MIN_DIGITS = 7` at `dedup.py:30`), so `key` is falsy
  at `dedup.py:206` and the record **falls through into the name branch** — the
  exact scenario the phone-branch comment at `dedup.py:203-204` describes, landing
  in an unguarded key instead.
- **Trigger, high-volume variant (this is the one that will actually fire).**
  `osm.py:87` builds `address` from five optional `addr:*` tags and yields `None`
  when none are present — so **the majority of OSM records have `address=None`**.
  Distinct branches of a chain therefore all key to `(name, "")`:
  ```python
  hamra  = {"name":"Starbucks","category":"cafe","phone":None,"address":None,
            "country":"LB","lat":33.8880,"lon":35.4870,
            "website":"https://starbucks.example/hamra",
            "email":"hamra@sb.example","instagram":"starbucks.hamra","source":"osm"}
  kaslik = {"name":"Starbucks","category":"cafe","phone":None,"address":None,
            "country":"LB","lat":33.9040,"lon":35.5010,
            "website":"https://starbucks.example/kaslik",
            "email":"kaslik@sb.example","instagram":"starbucks.kaslik",
            "source":"google_places"}
  dedup([hamra, kaslik])
  # -> 1 row.  lat/lon 33.904/35.501 (Kaslik)
  #     LOST website https://starbucks.example/kaslik
  #     LOST email    kaslik@sb.example
  #     LOST insta    starbucks.kaslik
  #     fabricated source 'google_places|osm' -> +5 lead_score
  ```
  `sales_ready.csv` (`main.py:202-213`) now lists one Hamra address and no Kaslik
  branch. Give both records phones and the collision vanishes — 2 rows — which is
  precisely why this is invisible in normal spot-checks.
- **Agreement / what is new here.** The `(name, "")` mega-bucket itself is already
  documented by the sibling `_extract_city` report
  (`200-fn-dedup-py-_extract_city-contract.md:137`, `:191-212`), audit 003 S1,
  audit 083 line 99, and audit 096 line 37. I am **not** re-reporting it. Three
  things are new:
  1. **The *name* half can independently be `""`**, which the sibling's framing
     never reaches (it treats the empty half as the *city*). Whitespace-only names
     pass all three scraper guards because `bool("   ") is True`.
  2. **The guard asymmetry.** The phone path has `if key:` at `dedup.py:206`; the
     name path has nothing. Same sentinel, opposite discipline, in adjacent lines.
  3. **The data-deletion consequence** (losing branch's contacts destroyed by
     `_merge`) plus the **fabricated +5 lead score** via the `source` union. Every
     prior report I read frames this as "fewer rows"; nobody priced what is lost
     *inside* the surviving row.
- **Fix:** make `normalize_name` mirror `normalize_phone`'s documented contract —
  return `""` for unusable input, and guard the consumer:
  ```python
  # dedup.py, replacing 212-218
  name = record.get("name") or ""
  city = _extract_city(record.get("address"))
  key = (normalize_name(name), city)
  if not key[0]:                    # no usable name -> never a shared bucket
      name_index[("__unkeyed__", str(record_index))] = dict(record)
      continue
  ```
  Separately, make `_merge`/`_pick` non-destructive for contact fields (union or
  keep-both) so a collision can never *delete* a real website or email.

### S2 — `unicodedata.combining()` at `dedup.py:68` has a character class far narrower than "strip accents": Arabic format characters and invisible codepoints survive into the key

- **Where:** `dedup.py:68`
  `name = "".join(c for c in name if not unicodedata.combining(c))`.
- **Breaks:** `unicodedata.combining(c)` returns the **canonical combining class**,
  which is non-zero only for combining diacritical marks. It is `0` for every
  format (`Cf`) and invisible character. So the filter removes `U+0301 COMBINING
  ACUTE` and leaves ZWNJ, ZWJ, LRM, RLM, soft hyphen, non-breaking hyphen, ZWSP and
  BOM in the key. For a Lebanon/Saudi corpus this is not academic: **ZWNJ
  (`U+200C`) and ZWJ (`U+200D`) are pervasive in real Arabic and Persian-script
  business names**, and LRM/RLM are pasted in wholesale from web copy-paste. Two
  OSM mappers spelling the same shop produce two dedup keys, and because the
  collision goes the *under-merge* direction it never shows up as a suspicious row
  count — it just shows up as a business that is in the master twice.
- **Trigger** (executed, codepoints shown):
  ```
  U+0645 U+0643 U+062A U+0628 U+0629      (مكتبة, no joiner)
      -> 'مكتبة'          len=5
  U+0645 U+0643 U+200C U+062A U+0628 U+0629 (same + ZWNJ)
      -> 'مك\u200cتبة'    len=6          keys DIFFER
  U+0645 U+0643 U+200D U+062A U+0628 U+0629 (same + ZWJ)
      -> 'مك\u200dتبة'    len=6          keys DIFFER
  U+200E U+0645 U+0643 U+062A U+0628 U+0629 (same + LRM)
      -> '\u200eمكتبة'    len=6          keys DIFFER
  U+200F ... (same + RLM) -> '\u200fمكتبة' len=6         keys DIFFER
  ```
  ```
  U+200C ZWNJ          combining()=0    kept_by_line_68=True
  U+200D ZWJ           combining()=0    kept_by_line_68=True
  U+200E LRM           combining()=0    kept_by_line_68=True
  U+200F RLM           combining()=0    kept_by_line_68=True
  U+00AD SOFT HYPHEN   combining()=0    kept_by_line_68=True
  U+2011 NB HYPHEN     combining()=0    kept_by_line_68=True
  U+FEFF BOM           combining()=0    kept_by_line_68=True
  U+0301 COMB ACUTE    combining()=230  kept_by_line_68=False   <-- the only class removed
  ```
  Latin: `"Al-Salam"` → `'al-salam'`, `"Al‑Salam"` (U+2011) → `'al‐salam'`,
  `"Al­Salam"` (U+00AD) → `'al\xadsalam'`. **3 spellings → 3 distinct keys.**
- **Note on `U+00A0 NBSP` and `U+200B ZWSP`:** both survive `dedup.py:68`, but they
  are cleaned up by `dedup.py:69` (`re.sub(r"\s+", " ", …)` plus `.strip()`)
  because Python's `\s` and `str.strip()` are Unicode-aware. Verified:
  `"Cafe Aura"` / `"Cafe\xa0Aura"` / `"Cafe Aura"` / `"Cafe　Aura"` all →
  `'cafe aura'`. So line 69 does the invisible-character work line 68 was meant to.
- **Fix:** strip the format/invisible class explicitly, not just `ccc != 0`:
  ```python
  _INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u00ad\ufeff"))
  def normalize_name(name: str) -> str:
      name = unicodedata.normalize("NFKD", name.translate(_INVISIBLE))
      name = "".join(c for c in name if not unicodedata.combining(c))
      return re.sub(r"\s+", " ", name).lower().strip()
  ```
  Audit 058 (`058-english-arabic-name-matching.md`) covers the *Arabic-vs-Latin
  transliteration* half of this problem; it does not cover the invisible-character
  class, which is orthogonal and cheaper to fix.

### S2 — `normalize_name` strips no punctuation, so ordinary typography variants fragment one business into many keys

- **Where:** `dedup.py:66-69`. Only case, whitespace, and `ccc != 0` marks are
  touched. Every punctuation and symbol character is preserved verbatim.
- **Breaks:** Ten plausible spellings of one shop name produce **eight** distinct
  dedup keys. The pipeline accumulates the master (`main.py:150`, `main.py:179`),
  so every re-scrape that picks up a new punctuation variant appends a new row for
  a business that is already in `all_businesses.csv` — permanently, because the
  cumulative master is never rebuilt from scratch.
- **Trigger** (executed):
  ```
  'Cafe-Aura'   -> 'cafe-aura'
  'Cafe Aura'   -> 'cafe aura'
  'Cafe.Aura'   -> 'cafe.aura'
  'Cafe&Aura'   -> 'cafe&aura'
  'Cafe, Aura'  -> 'cafe, aura'
  'Cafe (Aura)' -> 'cafe (aura)'
  'Cafe/Aura'   -> 'cafe/aura'
  "Cafe 'Aura'" -> "cafe 'aura'"
  'CAFE AURA'   -> 'cafe aura'      <- only case is handled
  'Cafe  Aura'  -> 'cafe aura'
  10 spellings -> 8 distinct dedup keys
  ```
  Real-world producers make this routine: `osm.py:95-96` prefixes bare domains with
  `https://`, and business names legitimately carry `&` (`Marks & Spencer`),
  `-` (`Al-Rawabi`), `.` (`Dr. Sleiman's Clinic`), and parentheses
  (`Cafe (Hazmey)`).
- **Fix:** collapse all non-alphanumeric runs to a single space as part of the same
  pass, using `re.sub(r"[^\w\s]", " ", name, flags=re.UNICODE)` after the NFKD step,
  then re-run the whitespace collapse. Coordinate with audit 058, which proposes
  branching on script — Arabic must **not** have its punctuation stripped
  (abbreviations and `\u060c` carry meaning), so gate the collapse on
  `name.isascii()`.

### S2 — `normalize_name` has zero input tolerance; the only guard is `or ""` at one call site, and the repo's own documented recipe omits it

- **Where:** `dedup.py:66` (`name: str`, no coercion, no guard), protected only by
  `dedup.py:212` (`record.get("name") or ""`). Consumed unguarded at
  `dedup.py:214`, which sits inside `dedup()` called unguarded at `main.py:180`.
- **Breaks:** `normalize_name(None)` raises
  `TypeError: normalize() argument 2 must be str, not None`. A single non-`str`
  truthy `name` kills the entire run — `main.py:162-168`'s `try/except` wraps only
  the three scraper futures, so by the time `dedup` runs, three successful scrapes
  and possibly 45 minutes of enrichment budget are already spent and the process
  dies with no partial output (`write_csv` at `main.py:108-134` is atomic, so
  `data/` still holds the *previous* run's files and nothing signals the loss
  except the absent print). On the GitHub Actions side that is `timeout-minutes:
  300` burned for nothing.
- **Trigger:** the usage pattern already written into this repo's design docs —
  `docs/audits/035-incremental-scraping.md:98` and `:128` both write
  `normalize_name(rec.get("name", ""))`. `.get(key, default)` returns the default
  **only when the key is absent**; `load_master` turns a blank cell into a
  **present** `None` (`main.py:88-89`), so the default never fires:
  ```python
  normalize_name(record.get("name", ""))   # name IS present, value is None
  # TypeError: normalize() argument 2 must be str, not None
  ```
  `dedup.py:212` uses the correct `or ""` form; the documented recipe does not.
  Audit 031 already specifies a regression test for this
  (`031-test-suite-design.md:145` → `test_normalize_name_non_string_type_error`,
  and `:222-224`), and no such test exists.
- **Fix:** make the function total on its own — `def normalize_name(name: object)
  -> str:` with `s = "" if name is None else str(name)` as its first line. That
  makes the `""` sentinel the *only* way to signal "unusable", which is exactly the
  contract `dedup.py:214` still needs to honour (S1). Also wrap
  `main.py:180-213` in the same try/except discipline as `main.py:162-168`, or at
  minimum log the offending record before re-raising.

### S3 — `cli.py`'s duplicate gate uses the raw `name`, not `normalize_name(name)`: the quality gate and the pipeline disagree about identity

- **Where:** `cli.py:190`
  `dupes = total - len({(r.get("name"), r.get("phone")) for r in rows})`, versus
  the pipeline key at `dedup.py:214`.
- **Breaks:** `leadminer validate` — the only no-network data-quality gate the
  project has (`cli.py:135-201`) — computes duplicate identity with a *different
  normalizer* than the one that actually decides identity. It will report
  duplicates the pipeline merged (false positive) and miss duplicates the pipeline
  failed to merge (false negative). Both directions mislead.
- **Trigger** (executed):
  ```python
  rows = [{"name":"Café Aura","phone":""}, {"name":"Cafe Aura","phone":""}]
  # cli.py:190   -> 2 distinct pairs -> dupes = 0   ("no duplicates", exit 0)
  # dedup.py:214 -> [('cafe aura','beirut'), ('cafe aura','beirut')] -> 1 row
  ```
  The gate says the file is clean; the pipeline would have merged the two rows into
  one. Conversely, after an S1 collision deletes a branch, `cmd_validate` sees one
  row and reports nothing — there is no record *left* to be a duplicate.
- **Fix:** import `normalize_name` from `dedup` in `cli.py` and use
  `{(normalize_name(r.get("name") or ""), r.get("phone")) for r in rows}`. While
  there, add an inverse check — count rows whose key is the degenerate `("", "")`
  and exit non-zero, which is the S1 detector `leadminer validate` is missing.

### S3 — `BusinessRecord`'s non-optional keys go `None` through `load_master`, and `total=True` is declared but never enforced

- **Where:** `base.py:5` (`class BusinessRecord(TypedDict)` — no `total=False`,
  so all 23 keys are required), `base.py:25-28`
  (`lead_score: int`, `recommended_service: str | None`, `source: str`,
  `scraped_at: str`, `completeness_score: int`), violated by `main.py:88-89`
  (blank → `None`) and `main.py:96-101` (unparseable int → `None`), consumed as
  untyped `dict` at `dedup.py:197`.
- **Breaks:** `lead_score` and `completeness_score` are declared as **plain `int`,
  not `int | None`** — unlike their 18 nullable siblings. But a master CSV row with
  a blank or unparseable `lead_score` cell becomes `None`, which is not an `int`.
  The declared type is wrong, not merely unenforced. The same applies to
  `source: str` and `scraped_at: str`.
- **Trigger** (executed, replicating `load_master` logic on a hand-edited or
  externally-produced CSV):
  ```
  input : name,phone,lead_score,completeness_score,source,scraped_at
          Cafe,+9611234567,,,osm,
  output: {'name':'Cafe','phone':'+9611234567','lead_score':None,
           'completeness_score':None,'source':'osm','scraped_at':None}
  base.py:25  declares lead_score: int          <- actual None
  base.py:28  declares completeness_score: int  <- actual None
  base.py:27  declares scraped_at: str          <- actual None
  ```
  In the reverse direction, `total=True` guarantees `website_live` is present — yet
  `enricher.py:340-348` carries an eight-line `r.setdefault(...)` block for exactly
  those keys. That block is the codebase admitting the TypedDict is not total in
  practice.
- **Fix:** change `base.py:25` and `base.py:28` to `int | None` (and `:26-27` to
  `str | None`) so the declaration matches `load_master`, then add
  `dedup(records: Sequence[BusinessRecord]) -> list[BusinessRecord]` so a checker
  can see the mismatch at all. See S3 below for why that checker does not exist.

### S3 — `mypy strict = true` is configured and never run: every annotation above is documentation

- **Where:** `pyproject.toml:64-68` (`[tool.mypy] strict = true`) versus
  `.github/workflows/`, which contains **only** `scrape.yml` (98 lines,
  `workflow_dispatch` only) with **no** `mypy`, `ruff`, or `pytest` invocation.
- **Breaks:** This is the root enabler behind every finding above. `dedup.py:197`
  annotates `list[dict]` (bare generics → `disallow_any_generics` violations under
  `strict`); `dedup.py:143`, `:148`, `:159`, `:190` all annotate bare `dict`;
  `dedup.py:199` annotates `dict[tuple, dict]`. And because
  `scrapers/base.py` imports nothing but `abc` and `typing`, `dedup.py` — which
  imports only `re` and `unicodedata` (`dedup.py:1-2`) — *could* import
  `BusinessRecord` without an import cycle (audit 020's concern does not apply to
  this direction), yet does not. So the type contract this lens was asked to check
  has never been mechanically verified even once.
- **Trigger:** any of S2's crashes. A single CI job running
  `mypy --strict` over `dedup.py` today would flag `dedup.py:199`, `:212` and
  `:214` before the code ever reached a production run.
- **Fix:** add a `lint` workflow (`mypy` + `ruff check` + `pytest`) gated on `push`
  to `main`, then narrow `dedup`'s signatures to `BusinessRecord`. Audit 065 already
  lists the same `dedup.py:199` annotation as S3; what it does not say is that
  nothing is currently executing the checker.

## Not a bug, but worth knowing

- **The declared types are accurate and I could not break either direction.** Input
  is `str` at every reachable call site; output is exactly `str` (not a subclass)
  for every reachable input; `normalize_name` is idempotent on all 11 samples
  tested; and it reads/writes **no** record fields, so the "field not in the
  TypedDict" question has the answer *none*. If you only fix the semantics from
  S1/S2, the annotation needs no change.
- **`normalize_name` has exactly one production consumer.** `dedup.py:214`. It is
  not imported by `main.py` (which imports only `dedup, normalize_phone` at
  `main.py:29`) or by `cli.py`. It is public by naming convention only, which is
  why the broken usage in `035-incremental-scraping.md` is plausible.
- **The key is never persisted, so a normalization regression is undetectable on a
  later run.** `FIELDS` (`main.py:33-40`) has 23 columns and none of them is an
  identity or provenance column; `csv.DictWriter(..., extrasaction="ignore")`
  (`main.py:125`) would silently drop one if added carelessly. `name_index` is read
  back only via `.values()` (`dedup.py:226-230`), never by key. The only observable
  signal of any of this report is the integer printed at `main.py:181`. Audit 072
  (`072-run-failure-triage.md:300-311`) already asks for
  `merged_by_phone`/`merged_by_name` counters — that is the cheapest possible
  instrumentation for verifying every S1 fix here.
- **The merged row can be internally incoherent: `name` from one record, `address`
  from another.** When two records share a key, `_pick` chooses each field
  independently and breaks remaining ties on `str(value)` (`dedup.py:184-187`), so
  the winner is the alphabetically *larger* value. Verified:
  ```python
  a = {"name":"Cafe Aura",  "address":"Hamra St, Beirut",  ... "source":"osm"}
  b = {"name":"Café Aura", "address":"Verdun St, Beirut", ... "source":"wikidata"}
  dedup([a, b]) -> 1 row
  #   name='Café Aura'    ('c'<'e' at the accent position)
  #   address='Verdun St, Beirut'   ('v' > 'h')
  # -> the row asserts Café Aura is on Verdun St. Neither source said that.
  ```
  This is a `_merge`/`_pick` defect rather than a `normalize_name` one, but it is
  only reachable *because* `normalize_name` put both records in the same bucket.
- **Whitespace handling on `dedup.py:69` is genuinely good.** `\s+` collapse plus
  trailing `.strip()` correctly folds `U+00A0`, `U+2009` and `U+3000` into plain
  spaces (`"Cafe Aura"` / `"Cafe\xa0Aura"` / `"Cafe　Aura"` → `'cafe aura'`), so
  NBSP never fragments a key. Only the non-whitespace invisibles (S2) survive.
- **Line-number drift in older audits.** Reports written against the previous
  `dedup.py` cite `dedup.py:28-31` for `normalize_name`, `dedup.py:34-38` for
  `_extract_city`, and `dedup.py:77-79` for the key. The current locations are
  **`dedup.py:66-69`**, **`dedup.py:72-76`** and **`dedup.py:212-218`**. Fix the
  anchors in 003, 028, 035, 057, 058 and 065 before acting on them — a five-line
  function has been rewritten and the citations no longer point at it.

## Recommended order of work

1. **Guard the name key** (S1) — add `if not key[0]:` before `dedup.py:215` so no
   record can ever enter the shared `("", …)` buckets. This is a two-line change
   and it stops active data loss. Do it first, before any normalization work,
   because otherwise you cannot measure whether the later fixes helped.
2. **Instrument before you refactor** — return
   `(records, merged_by_phone, merged_by_name)` from `dedup()` and log both
   counters at `main.py:181` (audit 072). Right now "fewer rows" is
   indistinguishable from a good week, and every S1 here is *fewer rows*.
3. **Make `_merge` non-destructive** (S1) — union `website`/`email`/`instagram`/
   `whatsapp`/`linkedin` or retain the loser in a secondary field. Until a
   collision can no longer *delete* a real contact, the over-merge blast radius is
   unbounded and fixing only the key is not enough.
4. **Widen the character filter** (S2) — strip the `Cf`/invisible class explicitly
   at `dedup.py:68`, then add punctuation collapsing gated on `name.isascii()`.
   Coordinate with audit 058, which owns the Arabic-script branch.
5. **Make `normalize_name` total** (S2) — `name: object` + `str(name)` coercion,
   so the documented `.get("name", "")` recipe cannot crash a run. Add the four
   regression tests audit 031 already specifies (`:222-224`) plus one asserting the
   key is never `("", "")`.
6. **Align the quality gate** (S3) — `cli.py:190` must use `normalize_name`, and
   should additionally fail on any row keyed `("", "")`.
7. **Turn on the checker** (S3) — a CI job running `mypy --strict` + `ruff` +
   `pytest`, then narrow `dedup` to `BusinessRecord` in and out and
   `dict[tuple[str, str], BusinessRecord]` for `name_index` (`dedup.py:199`).
   Without this, fixes 1–6 are all conventions rather than enforced contracts.