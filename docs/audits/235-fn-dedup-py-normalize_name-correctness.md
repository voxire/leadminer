# 200 — `normalize_name` (dedup.py:66) correctness review

**Target:** `dedup.py:66-69`. **Callers:** exactly one — `dedup.py:214`, inside
`dedup()`. Verified by import-graph grep: only `main.py:29` (`dedup`,
`normalize_phone`) and `tests/test_lead_signal.py:65` (`_merge`,
`normalize_phone`) import from `dedup`; neither touches `normalize_name`.

**Method:** I imported the module and executed the function and `dedup()` itself.
Python 3.14.8, `unicodedata` 16.0.0. No scraper was run, no network call was made,
no package installed, and **no repo file other than this report was written or
modified** (`git status` shows only this path plus sibling agents' untracked
reports). Reproduction commands are in the appendix.

## Verdict

The fold itself is sound, and I can state the invariant that guarantees it:
**`normalize_name` is a deterministic, total, idempotent function on `str` that
maps any input to the NFC/NFD-normalised, whitespace-collapsed, lowercase,
diacritic-free canonical form — I verified idempotence across all 1,114,112
Unicode codepoints with zero counterexamples.** It is *not* wrong for the Latin
text it was written for.

It has exactly one S1 defect and it is not about Unicode at all: the function
**cannot distinguish "this record has no name" from "this record's name is
blank"**, and `dedup.py:214` uses its return value directly as half of a dict key.
So every unnamed record in a given city shares the key `("", city)`, `_merge`
folds them into one row, and the losing rows' websites and emails are deleted
from the cumulative master with no error and no log line. I demonstrated this
end-to-end through `dedup()`: 3 records in, 1 row out, one website gone.

The second real defect is market-specific and, I think, the most important
finding in this report for Lebanon: **NFKD canonical decomposition cannot conflate
Arabic letter-form variants**, because those are distinct *base* letters with
canonical combining class 0 and no decomposition. `كافه` and `کافه` (kaf vs
Persian keheh — the same cafe, one typed by an Iranian hand, one by a Lebanese
one) produce different keys and will never merge.

---

## Findings

### S1 — `normalize_name("") == ""` is used as a dedup key, so every unnamed record in a city is merged into one row and the losers are deleted

- **Where:** `dedup.py:66-69` (returns `""`), consumed at `dedup.py:212`, keyed at
  `dedup.py:214`, merged at `dedup.py:215-218`.
- **Breaks:** the name-keyed branch of `dedup()` has no guard for "no usable
  name". Two records with no phone and no name in the same city both produce the
  key `("", "beirut")`, so `_merge` combines them and `_pick` chooses **one**
  value per field. The other record's `website` / `email` / `address` is silently
  discarded — this is destructive, not just a merge. It also **compounds across
  runs**: the surviving row still has a blank `name`, `main.py:124` writes it as
  an empty cell, `main.py:88-89` reads it back as `None`, and the next run's new
  nameless rows are folded into the same row again.
- **Trigger — exact input, exact output.** Executed against `dedup()`:

  ```python
  recs = [
    {"name": None, "address": "Main St, Beirut", "website": "https://baklava.example", "source": "osm",      "scraped_at": "2026-10-01T00:00:00+00:00"},
    {"name": "  ",  "address": "Rue A, Beirut",  "website": "https://kibbeh.example",  "source": "wikidata", "scraped_at": "2026-10-01T00:00:00+00:00"},
    {"name": None, "address": "Rue Z, Beirut",  "email": "new@z.example",           "source": "osm",      "scraped_at": "2026-11-01T00:00:00+00:00"},
  ]
  dedup(recs)  ->  1 record:
      name=None  website='https://baklava.example'  email='new@z.example'  address='Rue Z, Beirut'
  ```

  `https://kibbeh.example` does not exist anywhere in the output. It was chosen
  away by the `str(value)` final tiebreak at `dedup.py:184` — deterministic,
  arbitrary, and invisible. Note also that the row's `website` and `address` now
  come from **different businesses**, which is a data-quality poison that survives
  into `without_websites.csv` and the sales pitch. A fourth record added in a
  later run takes the row from 3 to 4 inputs without increasing the output count.

  Which inputs reach `""`: `""`, `None` (via the `or ""` at `dedup.py:212`),
  any whitespace-only string (`"   "`, `"\t\n "`, `"\xa0"`, `"\u2028"`,
  `"\u3000"`), and any name made only of marks (`"\u0345"` Greek ypogegrammeni →
  `""`, `"\u0301"` → `""`). Note that `"\u200b"` does *not* collapse — ZWSP is not
  whitespace, so it stays in the key (see S3).
- **Reachability — needs a blank name in the master, and only the master can
  supply one.** All three scrapers explicitly skip empty names
  (`scrapers/osm.py:63-65`, `scrapers/wikidata.py:91-93`,
  `scrapers/google_places.py:268-270`). But the whitelist filter at
  `main.py:172` is applied to `raw` only — `master` is concatenated afterwards at
  `main.py:179` — and `load_master` turns every blank cell into `None`
  (`main.py:88-89`). So one blank-name row in `data/all_businesses.csv` (hand-edited,
  or written by a pre-fix run, or pasted in from a supplier list — and
  `cli.py:151-152` treats "every row has a blank name" as a live failure mode
  worth a dedicated check) turns every subsequent nameless record in every city
  into an append-to-one-row operation.
- **Also worth noting:** this is the exact failure mode `normalize_phone` was
  hardened against 35 lines above, with the invariant written down in a comment
  at `dedup.py:28-30` — *"Such input must never become a dedup key"* — and a
  regression test at `tests/test_lead_signal.py:103-108`. `normalize_name` never
  got the same treatment, and `normalize_phone` returns `""` which the caller
  checks (`dedup.py:206`), whereas `normalize_name`'s `""` is used unchecked.
- **Fix (one line):** guard the name-keyed branch so a blank name never becomes
  a key — `dedup.py:211-218`: `if name_key: name_index[...] = ... else:
  singletons.append(dict(record))`, and return `singletons` alongside
  `phone_records + name_records`.

### S2 — NFKD cannot conflate Arabic letter-form variants; the most common variant pairs in this market never merge

- **Where:** `dedup.py:67-68`. The fold is `NFKD` + *remove chars whose canonical
  combining class is non-zero*. That removes diacritics correctly (verified:
  `مَطْعَم أَبُو شَقِير` and `مطعم أبو شقير` both → `مطعم ابو شقير`; `أحمد` and
  `احمد` both → `احمد`) but it does nothing about **base-letter** variants,
  because those have `ccc == 0` and no canonical decomposition.
- **Breaks:** the same shop, entered by an Arabic speaker and by a Persian/Urdu or
  non-native hand, gets two rows, two enrichment runs, two pitches, two rows in
  the master — permanently. Google Places returns whatever the owner typed;
  OSM `name`/`name:ar` tags (`scrapers/osm.py:63`) and Wikidata's `en,ar` label
  service (`scrapers/wikidata.py:40`) return yet other spellings.
- **Trigger — exact input, exact wrong output.** All rows executed:

  | input A | input B (same shop) | `normalize_name(A)` | `normalize_name(B)` | differing codepoint |
  |---|---|---|---|---|
  | `كافه` | `کافه` | `كافه` | `کافه` | U+0643 ARABIC KAF vs U+06A9 KEHEH |
  | `مصطفى` | `مصطفی` | `مصطفى` | `مصطفی` | U+0649 ALEF MAKSURA vs U+06CC FARSI YEH |
  | `مكتبة` | `مكتبه` | `مكتبة` | `مكتبه` | U+0629 TEH MARBUTA vs U+0647 HEH |
  | `حديقة` | `حديقه` | `حديقة` | `حديقه` | U+0629 vs U+0647 |
  | `يمن` | `یمن` | `يمن` | `یمن` | U+064A ARABIC YEH vs U+06CC FARSI YEH |

  Through `dedup()` with all five pairs at the same address (`"Rue A, Beirut"`,
  no phones, so all ten records take the name-keyed branch): **10 records → 10
  rows.** Zero merges.
- **Fix (one line):** fold the equivalence classes explicitly after
  `dedup.py:67` — `name = name.translate(_ARABIC_FOLD)` with a module-level table
  mapping `ك→ك`, `ی→ي`, `ی→ي`, `ة→ه`, `ۀ→ه`, and dropping U+0640 TATWEEL (see S3).

### S3 — the mark filter keys on `unicodedata.combining()`, so spacing marks and invisible formatting characters survive into the key

- **Where:** `dedup.py:68` (`unicodedata.combining(c)`) and `dedup.py:69`
  (`re.sub(r"\s+", " ", ...)`).
- **Breaks:** `combining()` returns the *canonical combining class*, which is `0`
  for **1,113 codepoints whose category is `Mn`** — all the spacing vowel signs.
  And `\s` does not match format characters (`Cf`). Neither class is removed, so
  two spellings of the same name keep different keys.
- **Trigger — exact input, exact wrong output (each verified):**

  | character | codepoint | survives? | `repr(normalize_name("A" + ch + "B"))` (escaped so the invisible chars are visible) |
  |---|---|---|---|
  | ARABIC TATWEEL | U+0640 | yes | `'a\u0640b'` — should be `'a b'` |
  | ZERO WIDTH SPACE | U+200B | yes | `'a\u200bb'` |
  | ZERO WIDTH NON-JOINER (Persian `می‌رود`) | U+200C | yes | `'a\u200cb'` |
  | ZERO WIDTH JOINER | U+200D | yes | `'a\u200db'` |
  | WORD JOINER | U+2060 | yes | `'a\u2060b'` |
  | BYTE ORDER MARK | U+FEFF | yes | `'a\ufeffb'` |
  | LEFT-TO-RIGHT / RIGHT-TO-LEFT MARK | U+200E / U+200F | yes | `'a\u200eb'` / `'a\u200fb'` |
  | SOFT HYPHEN | U+00AD | yes | `'a\xadb'` |
  | COMBINING GRAPHEME JOINER (Mn, ccc=0) | U+034F | yes | `'a\u034fb'` |
  | MONETARY VIETNAMESE DONG | U+180E | yes | `'a\u180eb'` |
  | NBSP / NNBSP / NEL / IDEOGRAPHIC SPACE | U+00A0 / U+202F / U+0085 / U+3000 | **no, correctly collapsed** | `'a b'` in all four cases |

  The tatweel case is the one that will actually bite in Lebanon: kashida is
  ordinary Arabic typography (`مـــطعم` vs `مطعم`), and the two spellings produce
  different keys. The ZWNJ case bites on Iranian/Pakistani-owned businesses, and
  U+FEFF matters because this project has *already* been bitten by a BOM once
  (`main.py:82-84` documents the header-BOM incident; a BOM inside a name would be
  silently part of the key and `cli.py:154-156` only checks *leading* BOMs).
- **Counter-intuitive corollary, verified precisely:** the 1,113 spacing `Mn`
  characters are **0 in the Arabic block (U+0600-06FF) and 0 in Hebrew
  (U+0590-05FF)** — every Arabic harakat, hamza and maddah has `ccc != 0` and is
  stripped correctly. The failure is confined to Indic scripts, which matter for
  the Saudi market (large Indian and Pakistani populations in Riyadh): Devanagari
  vowel signs are `ccc = 0`, so the diacritic filter is a **no-op** there and
  only the nukta (`ccc = 7`) is removed. Measured: `normalize_name("कॉफ़ी")` →
  `कॉफी` (good), but `normalize_name("हिंदी")` = `हिंदी` ≠
  `normalize_name("हिन्दी")` = `हिन्दी` — the two standard Hindi spellings of the
  same word never merge.
- **Fix (one line):** switch the filter at `dedup.py:68` from
  `unicodedata.combining(c)` to `unicodedata.category(c)[0] != "M"` and add the
  invisible set (U+0640, U+200B-200F, U+2060, U+FEFF, U+00AD) to the same table.

### S3 — the signature says `str`, the data model says `str | None`, and the function raises `TypeError` on every non-`str`

- **Where:** `dedup.py:66` (`def normalize_name(name: str) -> str:`) against
  `scrapers/base.py:6` (`name: str | None`) and `main.py:88-89` (blank → `None`).
- **Breaks:** the declared parameter type is stricter than the type of its only
  input. `normalize_name` is also the only value reader in the module that does
  not coerce — compare `raw = str(phone)` at `dedup.py:42` and the
  `str(value)` casts at `dedup.py:125` and `dedup.py:127`. The type checker
  therefore approves a call the runtime rejects, and the mask is one `or ""`
  (`dedup.py:212`) that no future caller will know to add.
- **Trigger — exact inputs, exact wrong outputs:**
  ```
  normalize_name(None)     -> TypeError: normalize() argument 2 must be str, not None
  normalize_name(123)      -> TypeError: normalize() argument 2 must be str, not int
  normalize_name(1.5)      -> TypeError: normalize() argument 2 must be str, not float
  normalize_name(b"Cafe")  -> TypeError: normalize() argument 2 must be str, not bytes
  normalize_name(["Cafe"]) -> TypeError: normalize() argument 2 must be str, not list
  ```
- **Not currently reachable in production** — I checked: the only caller passes
  `record.get("name") or ""`, all three scrapers yield `str`, and `load_master`
  never casts away from `str` for `name`. That is why this is S3 and not S1. It
  becomes a run-killer the moment anything calls it directly, and three audit
  reports already propose exactly such callers:
  `docs/audits/035-incremental-scraping.md:98`
  (`normalize_name(rec.get("name") or "")` — correct) vs `:128`
  (`normalize_name(rec.get("name", ""))` — raises `TypeError` the moment any
  master row has `name: None`, i.e. after the first `load_master` on a CSV with a
  blank name cell). `docs/audits/031-test-suite-design.md:145` already filed this
  as S1.
- **Fix (one line):** `if not name: return ""` as the first line of the function.

---

## Not a bug, but worth knowing

- **NFKD rewrites letters, and every instance I found is benign or helpful.**
  Measured: `ﷲ` (U+FDF2, Allah ligature) → `الله`; `﴿محمد﴾` no; `ﬀ` (U+FB00) →
  `ff`; `Å` (U+212B) → `a`; `Å` (U+00C5) → `a`; `¹` (U+00B9) → `1`;
  `™` (U+2122) → `tm`; `№` (U+2116) → `no`; `℡` → `tel`; `ᾳ` (U+1FB3) → `α`;
  `㈱`→`(주)`; `Ｃａｆｅ` (fullwidth) → `cafe`. So `"Area 51"` and `"Area 5¹"`
  collide, `"Shop 1"` and `"Shop ①"` collide — in both cases that is what you
  want. The one inconsistency: `½` (U+00BD) → `'1⁄2'` containing U+2044 FRACTION
  SLASH, which is introduced *by* the decomposition and therefore not ASCII-folded,
  so `"1/2 Cup"` → `1/2 cup` and `"½ Cup"` → `1⁄2 cup` do **not** match. Cosmetic
  only; worth one line in a docstring so the next person does not "fix" it.
- **No ReDoS surface and no pathological cost.** `re.sub(r"\s+", ...)` is a
  fixed literal pattern (no backtracking ambiguity), and the scan is linear.
  Measured 8.88 µs/call on a mixed Arabic/Latin corpus (150k calls in 1.33 s on
  this machine), and it is only paid for records that fail the phone branch
  (`dedup.py:205-211`), so a 1M-record run spends single-digit seconds here
  against a 300-minute workflow budget. I benchmarked the obvious optimisation
  (a 934-entry cached `str.translate` table, 0.28 s to build) and it is only
  1.3× faster — **not worth doing** for the pipeline's runtime; only worth doing
  if you ever call this per-request in a service.
- **The fold is invisible in the output, which is convenient but undocumented.**
  `write_csv` writes `record["name"]` verbatim (`main.py:125-127`) and `_pick`
  never normalises, so the exported `name` is always the raw source string —
  unlike `phone`, which *is* normalised before export at `main.py:194`. Good news:
  changing the fold cannot split historical rows, because the key is recomputed
  from raw values on every run. Bad news: nothing in the data records which fold
  produced a given merge, so a fold change is invisible in the output and can
  only be detected by diffing row counts (see `docs/audits/100-run-manifest-design.md:265`).

## Invariants I verified — what `normalize_name` actually guarantees

These are the precise statements that make the function safe to use as a key, all
executed rather than argued:

1. **Total on `str`:** for every one of the 1,114,112 Unicode codepoints
   (surrogates excluded), the function returns without raising — including
   U+10FFFF and lone surrogates, which pass through untouched.
2. **Idempotent:** `normalize_name(normalize_name(c)) == normalize_name(c)` for
   every codepoint, **0 counterexamples**. A key can therefore be recomputed from
   a stored value without drift, which is what makes the "key is derived from a
   field `_merge` can rewrite" pattern tolerable.
3. **NFC/NFD invariant:** `normalize_name` of the NFC and NFD forms of the same
   string are identical — verified for `Café`, `أحمد`, `Ångström`. The three
   sources really do disagree here (Google returns NFC, OSM tags are often NFD,
   Wikidata returns whatever its label service has), and this invariant is what
   keeps that from fragmenting the index.
4. **Unicode-aware whitespace collapse:** U+0020, U+00A0, U+202F, U+0085, U+2028,
   U+3000 all become a single space, and leading/trailing whitespace-only strings
   become `""` — see S1 for why that last part is dangerous.
5. **Deterministic and locale-independent:** no `locale`, no `casefold`, no
   environment dependency; the same input always yields the same key in any
   process, which is what lets `dedup()`'s output be reproducible.
6. **Namespace-disjoint from the phone index:** the return value only ever keys
   `name_index` (`dedup.py:214`), while phone keys live in `phone_index`
   (`dedup.py:198`), and `dedup()` keeps the two dicts separate. A name can
   therefore never be mistaken for a phone, and the two failure modes stay
   separable — unlike the pre-`99493b9` version, where junk normalised to `"+"`
   and merged unrelated businesses through the phone key
   (`tests/test_lead_signal.py:103-108`).
7. **Correct for its actual remit, Latin/Greek text:** accents, case, NBSP
   padding and inner whitespace all fold as intended —
   `"Café Al Azaam"`, `"Cafe Al Azaam"`, `"CAFE AL AZAAM"`,
   `"  Café   Al  Azaam "`, `"Cafe\u00a0Al Azaam"` and `"Café Al Azaam"`
   all → `cafe al azaam`.

**Where it stops being correct:** at anything that is not a Latin diacritic or a
Unicode space — Arabic base-letter variants (S2), spacing marks and invisible
format characters (S3), and the blank-name case (S1).

## Recommended order of work

1. **S1 first, alone, before anything else.** It is the only one of these findings
   that destroys data, and it is a three-line guard at `dedup.py:211-218`.
2. **S2 + the `combining()` → `category()` change in S3 in one edit** — they are the
   same three lines of Arabic/Indic folding, and together they are the difference
   between merging and not merging the majority of this dataset's records.
3. **S3 (`str | None`) together with the above**, because the fix belongs in the
   same function and the signature change is what stops the next caller from
   crashing.
4. **Tests, in the existing stdlib style** (`tests/test_lead_signal.py` runs with
   `python3 -m unittest discover -s tests`; 28 tests currently pass). Add a
   `TestNormalizeName` class next to `TestNormalizePhone`
   (`tests/test_lead_signal.py:102`) with, at minimum:
   `test_blank_name_is_not_a_dedup_key` (the S1 regression, asserting
   `len(dedup(recs)) == len(recs)` for two unnamed Beirut records),
   `test_arabic_letter_forms_match` (the S2 table),
   `test_tatweel_and_zwnj_are_removed`,
   `test_none_and_non_string_names_do_not_raise` (S3), and
   `test_fold_is_idempotent` (invariant 2, so a future "optimisation" cannot break it).

---

## Appendix — exact reproduction

Run from the repo root. No writes, no network, no installs.

```bash
# S1 — blank-name collapse, data loss, compounding
PYTHONDONTWRITEBYTECODE=1 python3 - <<'EOF'
from dedup import dedup
recs = [
 {"name": None, "address": "Main St, Beirut", "website": "https://baklava.example", "source": "osm",      "scraped_at": "2026-10-01T00:00:00+00:00"},
 {"name": "  ",  "address": "Rue A, Beirut",  "website": "https://kibbeh.example",  "source": "wikidata", "scraped_at": "2026-10-01T00:00:00+00:00"},
 {"name": None, "address": "Rue Z, Beirut",  "email": "new@z.example",           "source": "osm",      "scraped_at": "2026-11-01T00:00:00+00:00"},
]
print(len(dedup(recs)), dedup(recs))   # 1 record; kibbeh.example is gone
EOF

# S2 — Arabic letter-form variants
PYTHONDONTWRITEBYTECODE=1 python3 -c 'from dedup import normalize_name as N; print(N("كافه"), N("کافه"), N("مصطفى"), N("مصطفی"), N("مكتبة"), N("مكتبه"))'

# S3 — surviving invisible characters vs collapsed whitespace
PYTHONDONTWRITEBYTECODE=1 python3 -c '
from dedup import normalize_name as N
for cp in (0x0640,0x200B,0x200C,0x200D,0x2060,0xFEFF,0x200E,0x200F,0x00AD,0x034F,0x00A0,0x202F,0x0085,0x3000):
    print(hex(cp), repr(N("A"+chr(cp)+"B")))'

# S3 — non-str inputs
PYTHONDONTWRITEBYTECODE=1 python3 -c '
from dedup import normalize_name as N
for v in (None, 123, 1.5, b"Cafe", ["Cafe"]):
    try: N(v)
    except Exception as e: print(repr(v), type(e).__name__, e)'

# Invariants 1-2 — totality and idempotence over all of Unicode
PYTHONDONTWRITEBYTECODE=1 python3 -c '
from dedup import normalize_name as N
bad = [cp for cp in range(0x110000) if not (0xD800 <= cp <= 0xDFFF) and N(N(chr(cp))) != N(chr(cp))]
print("non-idempotent codepoints:", len(bad))'
```