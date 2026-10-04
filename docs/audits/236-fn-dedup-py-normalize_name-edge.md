# 200 — `normalize_name` boundary & hostile-input audit

**Target:** `dedup.py:66-69` — `normalize_name`
**Method:** read the function + its contract, then executed ~90 hostile inputs
against it (Python 3.14.8, `unicodedata` 16.0.0; every structural result below —
`combining() == 0`, `\s` membership, `.strip()` behaviour — is Unicode-version
independent and holds identically on the project's Python 3.12 / UCD 15.1).
Probe scripts live outside the repo; **no repo file other than this one was touched.**

## Verdict

`normalize_name` is **not** crash-prone and **not** slow — it is linear, has no
ReDoS surface, and handles the French/accent and NBSP cases correctly. Its defect
is that it has **two silent modes**: it can *shrink a name to the empty string*,
which turns every such business into one shared dedup key and silently deletes
businesses from the master CSV, and it **fails to remove the invisible characters**
it appears to remove, because the filter at `dedup.py:68` keys on canonical
combining class, which is `0` for 368 BMP `Mn` characters. The single most
dangerous input is `""` — reachable through `dedup.py:212`, destructive,
irreversible, and a direct violation of the invariant `normalize_phone` states
out loud three lines above it at `dedup.py:29-30`.

## The contract, as actually written

| Layer | Statement | Anchor |
|---|---|---|
| Function signature | `normalize_name(name: str) -> str` | `dedup.py:66` |
| Docstring | *none* — the function has no docstring at all | `dedup.py:66-69` |
| Data-layer type | `BusinessRecord.name: str \| None` | `scrapers/base.py:6` |
| Master-CSV reality | `""` → `None` for every column | `main.py:88-89` |
| Only caller | `name = record.get("name") or ""` then `key = (normalize_name(name), city)` | `dedup.py:212`, `dedup.py:214` |
| Normalization contract | NFKD + strip-combining + collapse-whitespace + lower + strip, i.e. a **canonical-ish ASCII fold for Latin script** | `dedup.py:67-69` |

Two contract facts follow immediately and drive everything below:

1. **`str` is a lie.** The declared parameter type contradicts the declared type of
   its only data source (`base.py:6` says `str | None`). The annotation describes
   a stricter world than the scrapers or the CSV reader produce.
2. **`normalize_name` is the only value reader in `dedup.py` that does not coerce.**
   Compare `dedup.py:42` (`raw = str(phone)`), `dedup.py:125` / `dedup.py:127`
   (`str(value).strip()`), `dedup.py:144` (`str(record.get("source") or "")`),
   `dedup.py:156` (`str(record.get("scraped_at") or "")`). Every sibling defends
   against non-`str`; `dedup.py:67` hands the value straight to
   `unicodedata.normalize`, which raises.

## Hostile-input matrix

Classification legend: **CRASH** = raises `TypeError`; **SILENT-WRONG** = returns
without error but the value is wrong or the dedup consequence is wrong;
**OK** = correctly handled.

### A. Types — the annotation is the contract, and it is violated

| Input | Result | Class | Note |
|---|---|---|---|
| `None` | `TypeError: normalize() argument 2 must be str, not None` | CRASH | contract-legal per `base.py:6` |
| `""` | `""` | **SILENT-WRONG** | see S1 |
| `0`, `0.0`, `False`, `[]`, `{}`, `()` | `TypeError` | CRASH | **but** `or ""` at `dedup.py:212` absorbs all six before the call |
| `42`, `3.14`, `True` | `TypeError` | CRASH | truthy → **not** absorbed by `or ""` |
| `["a"]`, `{"a": 1}`, `b"Cafe"` | `TypeError` | CRASH | truthy → **not** absorbed |
| `""` returned, `"  "` returned, `"\xa0"` returned | `""` | **SILENT-WRONG** | all three collapse to the shared key |

Verified end-to-end, not just in isolation:

```
normalize_name(None)  -> TypeError: normalize() argument 2 must be str, not None
dedup([{"name": ["Cafe"], "phone": None, "address": "Beirut", ...}])
                      -> TypeError: normalize() argument 2 must be str, not list
```

The `or ""` guard at `dedup.py:212` is an **asymmetric** shield: it hides falsy
non-strings and does nothing for truthy ones. That is the worst of both worlds —
it looks defended while leaving the actual crash path open.

### B. Whitespace and invisible characters

`re.sub(r"\s+", " ", name).lower().strip()` at `dedup.py:69`:

| Input | Result | Class |
|---|---|---|
| `"   "` | `""` | **SILENT-WRONG** (joins the `""` bucket) |
| `"\t\n\r\f\v"` | `""` | **SILENT-WRONG** |
| NBSP `"\u00a0"` | `""` | **SILENT-WRONG** |
| `"Cafe\u00a0Bar"` | `"cafe bar"` | **OK** — matches `normalize_name("Cafe Bar")` |
| NNBSP `"\u202f"`, ideographic `"\u3000"`, Ogham `"\u1680"`, file-sep `"\u001c"` | `""` | as above |
| ZWSP `"\u200b"` | `"\u200b"` | **SILENT-WRONG** — survives |
| `"Cafe\u200bBar"` | `"cafe\u200bbar"` | **SILENT-WRONG** — ≠ `"cafebar"` |
| ZWNJ `"\u200c"`, ZWJ `"\u200d"`, WJ `"\u2060"`, SHY `"\u00ad"`, BOM `"\ufeff"` inside a word | survive verbatim | **SILENT-WRONG** |
| CGJ `"\u034f"` inside a word | survives | **SILENT-WRONG** — see S2 |
| LRM `"\u200e"` / RLM `"\u200f"` | survive | **SILENT-WRONG** — bidi controls leak into the CSV |
| Variation selector `"\ufe0f"` | survives | **SILENT-WRONG** |

Verified directly: `re.match(r"\s", "\u200b")` is `False`,
`len("Cafe\u200bBar".strip()) == 8`. Neither the `\s+` collapse nor `.strip()`
can see these characters, and `unicodedata.combining()` returns `0` for all of
them, so `dedup.py:68` cannot see them either.

### C. Arabic

| Input | Result | Class |
|---|---|---|
| `"مطعم أبو شقير"` | `"مطعم ابو شقير"` | **SILENT-WRONG** — hamza deleted (already filed as 058 S1) |
| `"آل راشد"` vs `"الراشد"` | `"ال راشد"` vs `"الراشد"` | still distinct (058 overstated this one) |
| `"مُطَعِم"` vs `"مطعم"` | both `"مطعم"` | **OK** — harakat are genuinely optional |
| `"مـــطعم"` (tatweel U+0640) | `"مـــم"` — **tatweel survives** | **SILENT-WRONG** — same word, never merges |
| `"كافه‌ی"` (ZWNJ, Persian) | ZWNJ survives | **SILENT-WRONG** |
| Arabic-Indic digit `"٥"` | unchanged | OK (no digit folding; not relevant here) |

### D. Latin / French

| Input | Result | Class |
|---|---|---|
| `"Café de Beyrouth"` vs `"CAFÉ DE BEYROUTH"` | both `"cafe de beyrouth"` | **OK** |
| `"Nîmes"` | `"nimes"` | **OK** |
| fullwidth `"Ｃａｆｅ"` | `"cafe"` | **OK** — NFKD compat folding earns its keep here |
| `"İstanbul"` vs `"istanbul"` | both `"istanbul"` | **OK** |
| `"Œuvre"` vs `"Oeuvre"` | `"œuvre"` vs `"oeuvre"` | **SILENT-WRONG** |
| `"Ørsted"` vs `"Orsted"` | `"ørsted"` vs `"orsted"` | **SILENT-WRONG** |
| `"Łódź"` vs `"Lodz"` | `"łodz"` vs `"lodz"` | **SILENT-WRONG** |
| `"Straße"` vs `"STRASSE"` | `"straße"` vs `"strasse"` | **SILENT-WRONG** (`.lower()` ≠ `.casefold()`) |

Correction to a neighbouring audit: **Æ/æ/Œ/œ/Ø/Ł/ß/ẞ have no NFKD
decomposition.** Verified — `unicodedata.normalize("NFKD", "Æ") == "Æ"` for all
eight. The claim "Æ -> AE" at `058-english-arabic-name-matching.md:114` is
incorrect; `Æ` and `æ` are atomic letters in the UCD, not compatibility
ligatures (only the `FB00` typographic ligature block decomposes). `å` and `İ`
*do* decompose and *do* fold correctly, so the Latin path is genuinely partial,
not broken.

### E. Symbols, emoji, control characters

| Input | Result | Class |
|---|---|---|
| `"Cafe ☕"` vs `"Cafe"` | `"cafe ☕"` vs `"cafe"` | **SILENT-WRONG** — emoji never stripped |
| `"☕\ufe0f"` (emoji + VS16) | survives as 2 code points | **SILENT-WRONG** |
| `"🇱🇨"` flag | survives as 2 code points (NFKD does **not** fold regional indicators to letters) | OK |
| `"Cafe\x00Bar"` | `"cafe\x00bar"` — **NUL survives** | **SILENT-WRONG** |
| `"Cafe\x07Bar"`, `"Cafe\x1bBar"` | C0 controls survive | **SILENT-WRONG** |
| `"\u0301"*1_000_000` (only combining marks) | `""` | **SILENT-WRONG** (joins the `""` bucket) |
| `"\u064e\u064f\u0651"` (only Arabic marks) | `""` | **SILENT-WRONG** |

Verified: NUL round-trips through `csv.DictWriter` into a cell
(`'cafe\x00bar,,,,,...'`) and is valid UTF-8, so `write_csv` (`main.py:124`) and
the rclone/Drive round-trip (`scrape.yml:50`, `scrape.yml:78-79`) neither reject
nor sanitise it.

### F. Length — no problem here

| Input | Time | Result |
|---|---|---|
| 912 chars | 0.1 ms | OK |
| 92 304 chars | 13.5 ms | OK |
| 923 076 chars | 258.8 ms | OK, linear |
| 1 000 000 combining marks | 84.1 ms | collapses to `""` |

`\s+` is a single greedy character-class match with no ambiguous backtracking
structure, so there is **no ReDoS surface** — confirmed empirically at 1 M chars.
The real long-input hazard is upstream: `csv.field_size_limit()` is the default
`131072`, and `main.py:86` uses `csv.DictReader` without raising it, so a >128 KB
name cell raises `_csv.Error` inside `load_master` — *not* in `normalize_name`.

## Findings

### S1 — a name that normalizes to `""` becomes a shared dedup key and silently deletes businesses

- **Where:** `dedup.py:69` (`re.sub(r"\s+", " ", name).lower().strip()` produces
  `""`), `dedup.py:212` (`record.get("name") or ""` manufactures `""` from `None`),
  `dedup.py:73-74` (`_extract_city` returns `""` for a blank address),
  `dedup.py:214` (`key = (normalize_name(name), city)`),
  `dedup.py:216` (`_merge` folds them), persisted by `main.py:209`.
- **Breaks:** every name that normalizes to empty — `""`, `"   "`, `"\t"`,
  `"\u00a0"`, `"\u202f"`, `"\u3000"`, or a name consisting only of combining
  marks — produces the *same* key `("", city)`. All of them are merged into one
  record. With a blank address the key is `("", "")`: **one global bucket**.
  `_pick` (`dedup.py:159-187`) then chooses each field *independently*, so the
  survivor is a composite of two different businesses whose coordinates,
  category, website and country may belong to different real-world entities.
- **Trigger** (executed against `dedup()`):

  ```python
  dedup([
    {"name": None, "phone": None, "address": None, "category": "cafe",
     "lat": 33.89, "lon": 35.50, "source": "osm",        "country": "LB"},
    {"name": "",   "phone": None, "address": None, "category": "hotel",
     "lat": 24.71, "lon": 46.67, "source": "wikidata",   "country": "LB"},
    {"name": "\t\n","phone": None, "address": None, "category": "pharmacy",
     "lat": 21.48, "lon": 39.79, "source": "google_places","country": "SA"},
  ])
  # 3 distinct businesses  ->  1 row
  # {'name': '\t\n', 'category': 'pharmacy', 'lat': 21.48, 'lon': 39.79,
  #  'source': 'google_places|osm|wikidata'}
  ```

  A Beirut café, a Riyadh hotel and a Jeddah pharmacy are now one row with Jeddah
  coordinates. In the same-city variant (3 Beirut businesses, blank names,
  junk phones) 3 rows become 2.
- **Why S1:** silent data loss, and it is **irreversible**. `main.py:209`
  overwrites `all_businesses.csv` with the deduped set; next Monday's run reads
  that already-truncated file at `main.py:150`. The two lost businesses are gone
  from the only store there is. Nothing flags it: `cli.py:151` only errors when
  *every* row is blank, `cli.py:154` only checks for a *leading* BOM,
  `cli.py:190` counts raw `(name, phone)` pairs so it sees one row, not a merge,
  and the workflow gate only rejects a total row count under 100
  (`scrape.yml:73-79`).
- **Asymmetry that makes this a defect, not a design choice:**
  `normalize_phone` documents the invariant verbatim — *"Anything shorter than
  this is punctuation, a shortcode or an extension fragment… Such input must never
  become a dedup key"* (`dedup.py:28-30`), implements it at `dedup.py:45-46`, and
  the caller comments on it again at `dedup.py:203-204`. `normalize_name` has
  **no equivalent guard, no docstring, and no comment**. Same file, same hazard,
  opposite treatment.
- **Fix:** make `normalize_name` return `""`-means-nothing explicit and route it
  the same way `normalize_phone` does — e.g. have `normalize_name` return `None`
  for an empty result and at `dedup.py:212-218` put those records in a
  `degenerate` list that is emitted un-merged (or keyed on rounded
  `(lat, lon)`), so they can never share a key.

### S2 — `dedup.py:68` filters on the wrong property: 354 invisible characters pass straight through

- **Where:** `dedup.py:68` — `"".join(c for c in name if not unicodedata.combining(c))`
- **Breaks:** the line reads as "remove combining marks", but
  `unicodedata.combining()` returns the **canonical combining class**, which is
  `0` for 368 `Mn` (nonspacing mark) code points in the BMP — **354 of which
  survive into the dedup key**, verified by execution. A second, larger class
  survives because they are `Cf` (format) with `ccc=0` and `\s` does not match
  them. Census of 17 adjacent candidates: **all 17 survived, zero stripped.**

  | Blocked from the key | `cat` | `ccc` | `\s`? | concrete result |
  |---|---|---|---|---|
  | U+034F COMBINING GRAPHEME JOINER | **Mn** | 0 | no | `normalize_name("Cafe\u034fBar") == "cafe\u034fbar"` |
  | U+FE00–FE0F VARIATION SELECTOR-n | **Mn** | 0 | no | `"☕\ufe0f"` survives whole |
  | U+200B ZERO WIDTH SPACE | Cf | 0 | no | `normalize_name("Cafe\u200bBar") == "cafe\u200bbar"` |
  | U+200C / U+200D ZWNJ / ZWJ | Cf | 0 | no | survive |
  | U+200E / U+200F LRM / RLM | Cf | 0 | no | survive (bidi controls leak into the CSV) |
  | U+2060–2064 WORD JOINER, FUNCTION APPLICATION, INVISIBLE ×, SEPARATOR, + | Cf | 0 | no | survive |
  | U+00AD SOFT HYPHEN | Cf | 0 | no | survive |
  | U+FEFF ZERO WIDTH NO-BREAK SPACE | Cf | 0 | no | survive |
  | U+180E MONGOLIAN VOWEL SEPARATOR | Cf | 0 | no | survive |

  U+034F is the sharpest case: Unicode defines it *specifically to block
  normalization and reordering* — i.e. it is the one character designed to defeat
  line 67/68, and it wins. Two spellings of one business then never merge:
  `normalize_name("Cafe\u034fBar") != normalize_name("CafeBar")`.
- **Trigger:** `"Cafe\u200bBeirut"` written to `all_businesses.csv`, downloaded
  from Drive, opened in Google Sheets and re-saved. Verified the whole string
  survives `write_csv` (`main.py:124`, `utf-8-sig`) → `load_master`
  (`main.py:85`, `utf-8-sig`) unchanged, so the poisoned key is **permanent** in
  the master file. `normalize_name` is deterministic, so every subsequent run
  keeps forking on it.
- **Reachability is the whole argument here.** The CSVs are explicitly built for
  human round-trips: `main.py:118-119` says `utf-8-sig` is used *"so Excel and
  Google Sheets detect UTF-8"*, and `scrape.yml:50` / `scrape.yml:78-79` shuttle
  them through `gdrive:leads/` in both directions every week. Pasting between
  Sheets/Excel/LibreOffice, PDF copy-paste, and LLM-generated names are all
  routine sources of ZWSP/SHY/ZWJ/bidi controls.
- **Why S2 not S1:** the harm is **under-merge** — duplicate rows in the master
  and a double-pitched prospect. That inflates counts and degrades trust, but it
  does not delete data, and it is not silent-corruption-of-a-single-row the way S1
  is.
- **Fix:** filter on **general category**, not combining class, and delete rather
  than keep — `if unicodedata.category(c) in ("Mn", "Me", "Cf")`, plus
  explicitly drop `U+00AD`, `U+200B`-`U+200F`, `U+2060`-`U+2064`, `U+FE00`-`U+FE0F`
  and the `U+034F` family. Also drop U+0640 ARABIC TATWEEL (`cat=Lm`, `ccc=0`) —
  see below.

### S2 — `str` is the declared type but the crash set is truthy-only, and one bad cell kills the run

- **Where:** `dedup.py:66` (`name: str`) vs `scrapers/base.py:6`
  (`name: str | None`); shield at `dedup.py:212` (`or ""`); unguarded call at
  `dedup.py:67`; propagation at `main.py:180` (`records = dedup(combined)` — **no
  `try`/`except`**, unlike the per-scraper block at `main.py:167-168`).
- **Breaks:** `normalize_name` is the only value reader in `dedup.py` that does
  not wrap its input in `str()` — compare `dedup.py:42`, `dedup.py:125`,
  `dedup.py:127`, `dedup.py:144`, `dedup.py:156`, all of which do. Falsy non-strings
  (`None`, `0`, `0.0`, `False`, `[]`, `{}`, `()`) are silently absorbed into `""`
  by `or ""`; **truthy** non-strings (`42`, `3.14`, `True`, `["a"]`, `{"a": 1}`,
  `b"Cafe"`) pass through and raise
  `TypeError: normalize() argument 2 must be str, not list`. Nothing catches it:
  `dedup` → `main.py:180` → the process dies before `main.py:209`, so **no CSV is
  written at all** for that week.
- **Trigger:** `dedup([{"name": ["Cafe"], "phone": None, "address": "Beirut",
  "country": "LB"}])` → `TypeError`.
- **Why S2 and not S1:** `csv.DictReader` (`main.py:86`) only ever yields `str` or
  `None`, and all three scrapers skip falsy names (`scrapers/osm.py:64-65`,
  `scrapers/wikidata.py:92-93`, `scrapers/google_places.py:269-270`). So today's
  pipeline cannot produce a truthy non-string name. This is a **latent** contract
  violation, not a live one — it becomes S1 the moment any caller, importer or
  new scraper bypasses `or ""`. This is exactly what test 08 of
  `031-test-suite-design.md:222-224` is designed to catch, and it should be
  written before any refactor moves that `or ""`.
- **Fix:** `name = str(name or "")` on line 67 — one token, matches the style of
  every sibling in the file, and makes the S1 empty-string path the only
  remaining question.

### S3 — Arabic tatweel is cosmetic but survives; `.lower()` is not `.casefold()`

- **Where:** `dedup.py:68-69`
- **Breaks:** U+0640 ARABIC TATWEEL is `cat=Lm`, `ccc=0` — pure typographic
  elongation, semantically a no-op. Verified:
  `normalize_name("مـــطعم") == "مــم"` ≠ `normalize_name("مطعم") == "مطعم"`.
  Separately, `.lower()` leaves `ß` alone, so `Straße`/`STRASSE` do not match;
  and `Œ`, `Ø`, `Ł`, `æ` have no NFKD decomposition (see §D), so
  `Œuvre`/`Oeuvre` and `Ørsted`/`Orsted` never merge.
- **Why S3:** low frequency for LB/SA leads, and each case is one lost merge
  rather than corruption. Listed so it is not mistaken for "the Latin path works".
- **Fix:** delete `"\u0640"` explicitly; switch `.lower()` → `.casefold()` (free,
  and a strict superset of `.lower()`).

## Not a bug, but worth knowing

- **The function is fast and has no ReDoS surface.** `\s+` is a single greedy
  character class with no ambiguous backtracking structure. 923 KB in 259 ms,
  1 M combining marks to `""` in 84 ms. If you add fuzzy matching later, *that*
  is where the blow-up risk moves to — not here.
- **NBSP is handled, and that is a real win.** `Cafe\u00a0Bar` → `"cafe bar"`,
  matching `Cafe Bar`. This is the single most likely artifact a human Google
  Sheets edit leaves behind, and it is the one invisible character the function
  gets right. The failures in S2 are the *other* invisibles, which are strictly
  harder to notice because `cat.isprintable()` is `False` for all of them but so
  is U+00A0 — the difference is `ccc`/`\s`, not "visible vs invisible".
- **Emoji regional indicators are safe.** NFKD does **not** decompose
  `🇱🇨` into letter code points, so a flag can never collide with the text `LB`.
  Emoji as *suffixes* still break merges (`"Cafe ☕"` ≠ `"Cafe"`), which is an
  under-merge, not a corruption.
- **The 1 % row-count gate will not catch S1.** `scrape.yml:73-79` only rejects
  `ROWS < 100`. Losing three rows out of 50 000 to the `("", "")` bucket is
  invisible to it. Any fix for S1 should come with a
  `dedup`-level counter (e.g. `degenerate_names: N`) printed in `main.py:181` and
  asserted non-zero-growth, so the next occurrence is loud.
- **`cli.py` validate will not catch S1 or S2 either.** `cli.py:151` is
  all-or-nothing; `cli.py:154` checks only a *leading* `\ufeff`, not U+FEFF or
  U+200B anywhere in the cell. A one-line `any(unicodedata.category(c) in ("Cf","Mn") ...)`
  check over the `name` column would close both.

## The single most dangerous input

**`""` — and the class of names that normalize to it, of which the most likely
real-world producer is the non-breaking space `"\u00a0"`.**

Not `None`, and not a 1 MB string. The reasoning:

1. **It is the only input that silently destroys data.** `None` raises a loud
   `TypeError` and fails closed; it is also unreachable, because `dedup.py:212`
   converts it to `""` and `csv.DictReader` (`main.py:86`) only emits `str`/`None`.
   A huge string is merely slow, and linear. `""` returns successfully and
   deletes real businesses.
2. **It is reachable, and the reachability is structural, not hypothetical.**
   `main.py:88-89` turns every blank master-CSV cell into `None`, and
   `dedup.py:212` turns that `None` into `""`. The master CSV is downloaded from
   Drive every week (`scrape.yml:50`) and was deliberately written with a BOM
   *"so Excel and Google Sheets"* render it (`main.py:118-119`) — i.e. the file is
   designed to be opened, edited and pasted into by humans, who routinely leave
   `" "` or `"\u00a0"` in a cell. All three live scrapers guard against blank
   names, so the master file is the *only* place this can enter — which also means
   a human editing the master is the whole attack surface.
3. **The damage is bounded by nothing and permanent.** The `('', '')` worst case
   collapses every address-less, phone-less, name-less record on Earth into one
   row. `main.py:209` then overwrites `all_businesses.csv` with the loss, and the
   next run reads the lossy file at `main.py:150`. There is no backup and no
   upstream to re-derive from.
4. **The output is plausible enough to ship.** The surviving row has a category,
   in-market coordinates (`cli.py:159-162`), no BOM (`cli.py:154`), a unique
   `(name, phone)` pair (`cli.py:190`) and a plausible source list. It will be
   scored by `lead_score`, tagged by `recommend_service`, and appear in
   `sales_ready.csv` — pitched as a Jeddah pharmacy that does not exist.
5. **It violates an invariant the same file already states.** `dedup.py:29-30`:
   *"Such input must never become a dedup key."* `normalize_phone` obeys;
   `normalize_name`, three functions below it, does not. A rule written down and
   applied to one of two sibling key-builders is a defect, not a convention.

Fix it before the fuzzy matcher from `058` lands — a similarity metric scored
against `""`/`"   "`/`"\u00a0"` as a *legitimate* key will make the S1 blast
radius larger, not smaller, because blank-vs-blank will start matching across
cities.

## Recommended order of work

1. `dedup.py:212-218` — stop manufacturing a shared key. Have `normalize_name`
   return `None` for an empty result; route those records to a `degenerate` list
   emitted un-merged (or keyed on `(round(lat, 2), round(lon, 2))`). Print the
   count at `main.py:181`. *(Kills S1.)*
2. `dedup.py:68` — filter on `unicodedata.category(c) in ("Mn", "Me", "Cf")`
   instead of `unicodedata.combining(c)`, and explicitly delete `U+00AD`,
   `U+0640`, `U+200B`-`U+200F`, `U+2060`-`U+2064`, `U+FE00`-`U+FE0F`. *(Kills S2
   before it is amplified by fuzzy matching.)*
3. `dedup.py:67` — `str(name or "")`, and fix the annotation to
   `name: str | None` to match `scrapers/base.py:6`. *(Kills the latent S2
   crash.)*
4. Write the tests this report implies, before any refactor moves the `or ""`:
   empty/whitespace/invisible fuzz cases asserting `normalize_name` output
   directly, plus two regression tests for `("", city)` and `("", "")` in
   `dedup()`. This is test 08 of `031-test-suite-design.md:222-224` plus the
   cases it does not list.
5. `dedup.py:69` — `.lower()` → `.casefold()`, and add a small explicit folding
   table for `Œ œ æ Ø Ł Đ ı` and the Arabic tatweel. *(S3, and S3 of 058.)*
6. Coordinate with `058`: its fix must **not** move to `NFKC` for Latin (that
   would stop folding `Å`, `å`, `İ`, fullwidth forms — all verified working today)
   and must not repeat the incorrect "Æ -> AE" claim at `058:114`.

## Reproduction

Three probe scripts were written **outside** the repo (no repo file was modified
except this report) and executed against `dedup.py` as-is:

```
normalize_name("")                          -> ""
normalize_name("Cafe\u034fBar")             -> 'cafe\u034fbar'      # != "cafebar"
normalize_name("Cafe\u200bBar")             -> 'cafe\u200bbar'      # != "cafebar"
normalize_name("Cafe\u00a0Bar")             -> 'cafe bar'           # OK
normalize_name("مـــطعم")                    -> 'مــم'                # tatweel kept
normalize_name("Œuvre") / ("Oeuvre")        -> 'œuvre' / 'oeuvre'   # DIFF
normalize_name("Café de Beyrouth")          -> 'cafe de beyrouth'   # OK
normalize_name(None)                        -> TypeError
normalize_name(42)                          -> TypeError
dedup([3 nameless+address-less records])    -> 1 row
```