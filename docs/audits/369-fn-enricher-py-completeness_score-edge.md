# 369 — `completeness_score` edge/hostile-input audit (`enricher.py:101`)

## Verdict

`completeness_score` cannot crash, cannot return a non-`int`, cannot leave its 0–7
range, and has no length or unicode sensitivity — I verified the range invariant
exhaustively over 16,777,216 value combinations. Its one and only defect is that
all seven tests (`enricher.py:103,105,107,109,111,113,115`) are bare truthiness
checks, so **any non-empty string is a "complete" field**: `" "`, `"\u00a0"`,
`"\u200b"`, `"."`, `"0"`, `"N/A"`, `"\ufeff"`. Because the only consumer gates on
`completeness_score >= 1` (`main.py:206`), a single whitespace character promotes
a zero-data record into the `qualified_businesses.csv` sales product. That is the
most dangerous input, and it is reachable today from one anonymous OSM tag edit
(`osm.py:87`).

---

## Method

`requests` is not installed locally, so I extracted the function body verbatim from
`enricher.py:101-117` and `exec`'d it in isolation. No repo file was modified, no
network call was made, nothing was installed. Every classification below is
observed output, not inference.

---

## Classification of boundary and hostile inputs

`FIELDS = phone, email, website, address, facebook, instagram, whatsapp, linkedin`
(8 keys, `enricher.py:103-115`). Observed:

| # | Input | Result | Class |
|---|---|---|---|
| 1 | `record = {}` | `0` | correctly handled |
| 2 | all 8 fields `None` | `0` | correctly handled |
| 3 | all 8 fields `""` | `0` | correctly handled (empty string is falsy) |
| 4 | `phone=None`, key **absent** | `0` | correctly handled — `.get` never raises, so no `KeyError` is reachable |
| 5 | `phone=0`, `0.0`, `False`, `Decimal("0")`, `whatsapp=0` | no credit | correctly handled (falsy zero) |
| 6 | `phone=-0.0` | no credit | correctly handled |
| 7 | `scraped_at=date(...)` | `0` | correctly handled (not read) |
| 8 | `record={"completeness_score": "7"}` | `0` | correctly handled — no read/write recursion, no self-reference |
| 9 | **`website=" "`** (one space) | **`1`** | **silently wrong** |
| 10 | `address="\t\n "` | `1` | silently wrong |
| 11 | `email="\u00a0"` (NBSP) | `1` | silently wrong |
| 12 | `address="\u200b"` (ZWSP) | `1` | silently wrong — and survives `.strip()` **and** `dedup._is_missing` |
| 13 | `phone="\ufeff"` (BOM), `address="\u200e"`, `"\u2060"` | `1` | silently wrong — same, survives `.strip()` |
| 14 | `address="\u0301"` (bare combining acute) | `1` | silently wrong |
| 15 | `address="🏢"` (emoji only) | `1` | silently wrong |
| 16 | `phone="0"`, `"123"`, `"N/A"`, `"-1"` | `1` | silently wrong — not a phone, credited as one |
| 17 | `phone=-9611234567` (negative int) | `1` | silently wrong |
| 18 | `phone=float("nan")`, `Decimal("NaN")` | `1` | silently wrong — NaN is truthy |
| 19 | `phone=["+961 1 234 567"]`, `{"v": ...}`, `{...}`, `True`, `b"..."`, `bytearray(...)`, `{"ig"}` | `1` | silently wrong — any truthy wrong type earns the point |
| 20 | `phone={"v": None}` (nested empty) | `1` | silently wrong |
| 21 | `email="unknown"`, `"a@b"`, `website="."`, `"http://"`, `"javascript:void(0)"` | `1` | silently wrong — fails every validity regex the repo already owns |
| 22 | `address="شارع الحمرا، بيروت"` (Arabic) | `1` | **correctly handled** |
| 23 | `email="مصلى"` (Arabic RTL) | `1` | correctly handled |
| 24 | `address="Café du Coin"` (NFD, combining acute inside) | `1` | **correctly handled** — no mojibake, no normalization damage |
| 25 | `address="x" * 10_000_000` (20 MB str) | `1`, **0.0141 s for 100,000 tests** | correctly handled — `bool(str)` is O(1); **no** memory or CPU cliff |
| 26 | all 7 signals populated | `7` | correctly handled |
| 27 | exhaustive sweep of `{None,""," ","x",0,1,True,-1}` over all 8 fields | distinct outputs exactly `{0..7}`, always `int` | correctly handled — **range invariant provably holds** |
| 28 | `record = None` | `AttributeError: 'NoneType' object has no attribute 'get'` | crashes with traceback — but **unreachable**: `enrich` only ever passes dicts produced by `dedup` (`enricher.py:337,349`) |
| 29 | `record = []`, `()`, `"abc"`, `5`, generator | `AttributeError` | crashes with traceback — same, unreachable |
| 30 | value whose `__bool__` raises (`RuntimeError("boom")`) | `RuntimeError: boom` | crashes with traceback — no defensive `try`; only reachable via a hypothetical pydantic/attrs model |

**Summary: 0 reachable crashes inside the function, 0 out-of-range returns, 0
length/encoding failures, 13 distinct classes of silently wrong value.**

---

## Findings

### S1 — `int(float(cell))` at `main.py:99` raises an **uncaught `OverflowError`** and kills the run before `completeness_score` is ever called

- **Where:** `main.py:44` puts `completeness_score` in `_INT_FIELDS`; `main.py:96-101`
  is the coercion loop; the throwing line is `main.py:99`
  (`row[field] = int(float(row[field]))`) and the handler at `main.py:100` catches
  only `(ValueError, TypeError)`.
- **Breaks:** `float()` accepts `inf`/`-inf`/`1e400`/`1e309`/`Infinity` and returns
  `±inf`; `int(±inf)` then raises `OverflowError`, which the `except` clause does not
  name. `load_master` is called at `main.py:150`, i.e. **before** `dedup`
  (`main.py:180`) and `enrich` (`main.py:187`), so the exception escapes `main()` and
  terminates the run. Because `all_businesses.csv` is the cumulative master that is
  re-read as *input* on every subsequent run (`main.py:150`), one poisoned cell is a
  permanent wedge: every future run dies at the same line until the file is edited by
  hand. This is the ingest half of `completeness_score`'s contract, and it is the only
  crash-class defect I found in the whole contract.
- **Trigger** (verified, exact cells, column `completeness_score` in
  `data/all_businesses.csv`):

  | cell value | `float()` | `int()` result |
  |---|---|---|
  | `inf` | `inf` | **UNCAUGHT `OverflowError`** |
  | `-inf` | `-inf` | **UNCAUGHT `OverflowError`** |
  | `Infinity` | `inf` | **UNCAUGHT `OverflowError`** |
  | `1e400` | `inf` | **UNCAUGHT `OverflowError`** |
  | `1e309` | `inf` | **UNCAUGHT `OverflowError`** |
  | `nan` | `nan` | caught (`ValueError`) → `None` |
  | `""` | — | caught (`ValueError`) → `None` |
  | `0x10` | — | caught (`ValueError`) → `None` |

  Traceback shape: `File "main.py", line 99, in load_master -> OverflowError:
  cannot convert float infinity to integer`.
- **Reachability (honest caveat):** the pipeline writes this file itself with `int`
  values, so a self-produced master never contains these cells. The trigger requires
  external modification of the master CSV — and that file is one of five human-facing
  products uploaded to Google Drive (BRIEF lines 47-56) *and* re-ingested as input.
  So a single hand-edit, a Sheets round-trip, or any external tool that rewrites one
  numeric cell bricks every subsequent run. I rate this S1 on the "a run that dies"
  definition, not on likelihood.
- **Fix (one line):** `main.py:100` → `except (ValueError, TypeError, OverflowError):`
  (better: reject non-finite values explicitly with `math.isfinite` before `int()`).
  Same fix protects `review_count` and `lead_score`, which share `_INT_FIELDS`.

### S2 — Bare truthiness scores blank and degenerate strings as complete data

- **Where:** `enricher.py:103,105,107,109,111,113,115` — seven `if record.get(...)`
  checks with no normalization, versus `dedup.py:79-90` `_is_missing()`, which exists
  in this same repo, *already documents this exact bug class* ("The previous check was
  `is None`, which meant an empty string counted as present"), and handles it. Two
  stages of the same pipeline disagree about what "present" means.
- **Breaks:** every non-empty string is a point. `qualified_businesses.csv` gates on
  `>= 1` (`main.py:206`) — the lowest possible bar — so **one whitespace character is
  enough to ship a record with zero real data into the qualified-leads product**, and
  it is reported as a KPI at `main.py:221` ("Qualified (score >= 1)"). Junk values are
  not hypothetical for `address`: `osm.py:87` builds the address with
  `", ".join(p for p in addr_parts if p) or None`, and `if p` is again bare truthiness,
  so a whitespace-only tag survives the join.
- **Trigger:** an OSM element tagged `name=Cafe X` + `addr:housenumber=" "` →
  `osm.py:87` yields `address=" "` → `enricher.py:109` returns `1` → the record lands
  in `qualified_businesses.csv` with a blank address. OSM tags are contributor-editable
  by anonymous users, so this is attacker-influenceable data, not a hypothetical.
  Verified: `osm_address({"name":"Cafe X","addr:housenumber":" "}) == " "`.
- **Fix:** route all seven checks through one shared helper that is stronger than
  truthiness, and reuse it in `osm.py:87` too:
  `def has_value(v): return bool(v) and any(not unicodedata.category(c) in ("Cf","Cc","Zs","Zl","Zp") for c in str(v))`

### S2 — No validity check: values this repo already rejects elsewhere still earn points

- **Where:** `enricher.py:103-116` vs. the repo's own existing validators —
  `dedup.py:30` (`_MIN_DIGITS = 7`), `dedup.py:45-46`, `dedup.py:116-117`
  (`_EMAIL_OK`, `_URL_OK`), `dedup.py:120-140` (`_validity`), `enricher.py:142`
  (`_IG_BLACKLIST`), `enricher.py:292` and `main.py:142` (both require ≥ 7 phone
  digits), `enricher.py:208` (`len(handle) >= 2`).
- **Breaks:** the codebase knows `"0"`, `"123"`, `"unknown"`, `"a@b"`, `"."`,
  `"http://"` and single-character handles are not real values — it has a validator
  for each. `completeness_score` is the only place that ignores all of them, so it
  credits a point for data that `lead_score` refuses to credit, and for emails the
  merge stage scored as invalid (`dedup.py:124-125` returns `0` for those). Result:
  `completeness_score` and `lead_score` disagree about the same record, and nothing
  surfaces the disagreement.
- **Trigger:** `{"phone": "0"}` → `completeness_score == 1`, while
  `dedup.normalize_phone("0")` returns `""` (`dedup.py:45-46`) and
  `main.has_any_contact` returns `False` (`main.py:141-145`). Verified.
- **Fix:** `phone` → require `len(re.sub(r"\D", "", v)) >= 7`; `email` → `_EMAIL_OK`;
  `website` → `_URL_OK`; `instagram`/`linkedin` → the same `>= 2` / blacklist checks
  used at `enricher.py:208,219`. Import them from `dedup` (already-imported module,
  no new dependency) rather than duplicating the regexes.

### S3 — `.strip()` is **not** a sufficient repair, and neither is `dedup._is_missing`

- **Where:** `enricher.py:103-115` (the fix site for S2 above) and `dedup.py:88-89`.
- **Breaks:** if the S2 fix is written as `record.get("phone", "").strip()`, it still
  credits these, because CPython's `str.strip()` uses `Py_UNICODE_ISSPACE`, and the
  Unicode format characters are *not* whitespace:

  | char | `isspace()` | `bool(s)` | `s.strip()` | `bool(s.strip())` | category |
  |---|---|---|---|---|---|
  | `U+200B` ZWSP | `False` | `True` | `'\u200b'` | **`True`** | `Cf` |
  | `U+200E` LRM | `False` | `True` | `'\u200e'` | **`True`** | `Cf` |
  | `U+2060` WJ | `False` | `True` | `'\u2060'` | **`True`** | `Cf` |
  | `U+FEFF` BOM | `False` | `True` | `'\ufeff'` | **`True`** | `Cf` |
  | `U+00A0` NBSP | `True` | `True` | `''` | `False` | `Zs` |
  | `U+0009` TAB | `True` | `True` | `''` | `False` | `Cc` |

  This is not academic: `load_master` deliberately writes UTF-8 **with BOM**
  (`main.py:124`, `encoding="utf-8-sig"`) and strips it only on the *header* row
  (`main.py:85`). A BOM landing inside a data cell therefore produces exactly this
  shape, and `dedup._is_missing("\ufeff")` returns `False`, so it is not scrubbed even
  when the record *is* merged with a duplicate — it survives `dedup.py:207-208`.
- **Fix:** normalize with `unicodedata.category()` filtering (`Cf`, `Cc`, `Zs`, `Zl`,
  `Zp`), not `.strip()`. Ship it once and call it from `enricher.py:103-115`,
  `dedup.py:88-89` and `osm.py:87` so all three stages finally agree.

---

## The single most dangerous input

**`{"website": " "}` — a one-character whitespace-only `website`.**

Not the `inf` cell (that one is the most dangerous *overall*, but it lives one hop
away at `main.py:99` and is loud). The junk `website` is the most dangerous input to
`completeness_score` because a single space propagates into **three** independent
downstream decisions, and one of them is destructive. Verified by trace:

| site | code | effect of `website=" "` |
|---|---|---|
| score inflation | `enricher.py:107` | `+1` → record reaches `qualified_businesses.csv` (`main.py:206`) |
| **partition flip** | `main.py:199-200` | lands in `with_websites.csv`, **and is excluded from `without_websites.csv`** |
| wasted work | `enricher.py:231` → `175-178` | selected as an HTTP target; `requests` raises `MissingSchema` for a scheme-less URL, swallowed to `UNKNOWN` |
| corrupted metric | `enricher.py:266`, `main.py:217` | counted in the "unreachable" bucket as though it were a real dead site |

The partition flip is the killer. `without_websites.csv` is not a side export — the
BRIEF names it the *new-site pitch targets* file, i.e. the single highest-value
artifact the business produces. One space of junk takes a lead out of it while
reporting the lead as having a website, and `completeness_score >= 1` simultaneously
certifies the record as fully qualified. The failure is silent at every step, lands
in two CSVs, and is invisible in the console summary because the "unreachable" count
at `main.py:217` looks like ordinary network flakiness.

Compare with the alternatives I tested: a junk `address` is *less* dangerous — it
inflates one number and corrupts no partition. A junk `phone` is *less* dangerous —
it is caught by the ≥7-digit rule everywhere it matters except here. Only `website`
is load-bearing in five places, and three of them are wrong in the same direction.

---

## Not a bug, but worth knowing

- **No reachable crash inside the function.** `record` is only ever a `dict` from
  `dedup` (`enricher.py:337,349`), and `.get` makes a missing key safe, so no
  `KeyError` is possible. Passing a non-dict raises `AttributeError` — correct
  behaviour for a contract violation (`enricher.py:101` annotates `record: dict`).
  The annotation is unenforced at runtime, consistent with `BusinessRecord` being an
  inert `TypedDict` (`scrapers/base.py:5-29`) — see audit 020. Do not "harden" this
  function with a `try/except`; fix it at the model layer instead.
- **The range invariant `0 <= completeness_score <= 7` is structurally guaranteed**
  and needs no runtime assertion (audit 048 asks for one at line 47). I swept all
  16,777,216 combinations of `{None,""," ","x",0,1,True,-1}` across the 8 fields:
  the output set is exactly `{0,1,2,3,4,5,6,7}`. Seven fixed `+1`s, no subtraction,
  no `sum()`, no user-controlled magnitude — it cannot be inflated past 7 by data.
  `lead_score` is the function that genuinely needs a clamp (it has one, at
  `enricher.py:319`).
- **No length or encoding sensitivity at all.** `bool()` on a `str` is a length
  check: 100,000 truthiness tests against a 20 MB string took 0.0141 s. A 10 MB cell
  is a `write_csv` problem (`main.py:127`), not a scoring problem. Arabic, French and
  emoji inputs are all handled correctly — do not add `str.encode`/normalization here.
- **`facebook` is dead for 2 of 3 sources.** `google_places.py:293` and
  `wikidata.py:118` hardcode `facebook=None`; only OSM sets it
  (`osm.py:92`, from the raw `contact:facebook` tag, unvalidated). So `enricher.py:111`
  is effectively instagram-only for the two highest-volume sources, and for OSM it
  credits whatever string an anonymous editor typed — the blacklist at
  `enricher.py:142` is applied only to website-scraped handles (`enricher.py:208`),
  never to OSM-sourced ones. Previously noted at 025 line 279; the edge-specific
  consequence is that this point is the cheapest in the function to inflate for free.
- **The `or` at `enricher.py:111` is a single shared point** — facebook + instagram
  together still score 1, so 6 of the 8 fields map to 7 points and any single social
  field suffices. Not a bug, but it means one junk OSM tag can carry a record over
  the `>= 1` line on its own. Worth knowing when you recalibrate (audit 043).

---

## Recommended order of work

1. **`main.py:100` — add `OverflowError` to the except tuple.** One line, unblocks
   the run permanently, protects three integer columns. Do this first; it is the only
   crash in the contract.
2. **Add `enricher.has_value()` (category-aware, not `.strip()`) and use it at
   `enricher.py:103-115`, `dedup.py:88-89`, `osm.py:87`.** This is the real fix for
   score inflation and makes three stages agree on what "present" means.
3. **Filter `check_websites` targets (`enricher.py:231`) and the partition
   (`main.py:199-200`) through `_URL_OK`.** Until this lands, step 2 protects the score
   but the record is still dropped from `without_websites.csv`, the pitch-target file.
   Fix 2 and 3 together or not at all.
4. **Tighten the three weakest signals** (`phone` ≥7 digits, `email` via `_EMAIL_OK`,
   `instagram`/`linkedin` ≥2 chars + blacklist) by reusing the regexes that already
   exist in `dedup.py:116-117` and `enricher.py:142,208,219`.
5. **Add the table from this report as a `unittest` parametrize block** over
   `test_enricher_pure.py` (the file named at 032 line 84). These are 30 pure
   assertions, no network, no fixtures — the cheapest tests in the project, and the
   only thing standing between the next scoring change and silent drift.