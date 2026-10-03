# 058 — Bilingual business-name matching (Arabic + Latin + French)

## Verdict

`normalize_name` (`dedup.py:66`) normalizes **every** string with `NFKD` and then
deletes every combining mark. That is correct for Latin/French (`café → cafe`) but
destructive for Arabic: it splits the hamza/madda letters `أ إ آ ؤ ئ` into a bare
letter plus a combining mark, then deletes the mark, so `أبو` becomes `ابو`. On top
of that, dedup compares names by **exact string equality** and has **no
transliteration**, so the same business recorded as `مطعم الأرز` (OSM) and `Al Arz
Restaurant` (Google Places) never merges. The fix is a per-script normalization
(NFKC for Arabic, NFKD+strip for Latin), an Arabic→Latin transliteration key, and a
fuzzy similarity metric scoped by city.

## Findings

### S1 — `NFKD` + strip-combining destroys Arabic letters

- **Where:** `dedup.py:67-68`
- **Breaks:** `أ إ آ` (alef with hamza above / hamza below / madda) all collapse to
  bare `ا`; `ؤ` collapses to `و`; `ئ` collapses to `ي`. Distinct names that differ
  only by a hamza or madda become the same dedup key, and the normalized form is no
  longer a faithful reading of the name.
- **Trigger:**
  ```python
  normalize_name("مطعم أبو شقير")   # -> "مطعم ابو شقير"  (hamza erased)
  normalize_name("آل راشد")         # -> "ال راشد"       (madda erased)
  ```
  `آل` (U+0622, "family of") becomes `ال`, the same string as the definite article
  `ال` ("the"), so `آل راشد` and `الراشد` converge on one key.
- **Why:** `NFKD` is *canonical decomposition with no recomposition*. It decomposes
  the precomposed hamza letters into base + combining mark (UAX #15, Table 2; e.g.
  `أ` U+0623 → `U+0627 U+0654`). The next line,
  `"".join(c for c in name if not unicodedata.combining(c))`, deletes any code point
  whose combining class is nonzero — and `HAMZA ABOVE` U+0654 has ccc=230, `MADDAH
  ABOVE` U+0653 has ccc=230, `HAMZA BELOW` U+0655 has ccc=220
  (DerivedCombiningClass.txt, UAX #44). The hamza/madda is not an optional vowel
  mark; it is part of the letter. Verified behavior (Python 3.12):

  | input | code point | `NFKD` | after strip-combining |
  |---|---|---|---|
  | `أ` | U+0623 | `0627 0654` | `ا` |
  | `إ` | U+0625 | `0627 0655` | `ا` |
  | `آ` | U+0622 | `0627 0653` | `ا` |
  | `ؤ` | U+0624 | `0648 0654` | `و` |
  | `ئ` | U+0626 | `064A 0654` | `ي` |

- **Fix:** use `NFKC` (or `NFC`) for Arabic, not `NFKD`. `NFKC` = compatibility
  decomposition **plus** canonical recomposition, so it folds Arabic Presentation
  Forms-A/B (`U+FB50–U+FEFF`, the isolated/final shapes) back to base letters *and*
  recomposes `أ إ آ ؤ ئ` — verified: `NFKC("أبو محمد") == "أبو محمد"`. Strip only
  the optional short-vowel/harakat marks (`U+064B–U+0652` and superscript alef
  `U+0670`), never the hamza/madda.

### S1 — No transliteration: Arabic and Latin/French names never dedup

- **Where:** `dedup.py:66-70`, `dedup.py:119`
- **Breaks:** The same business appears as `قهوة بيروت` from OSM, `Cafe Beirut` from
  Google Places, and `Café Beyrouth` from Wikidata. None of these strings are equal
  after normalization, so `dedup()` emits three rows into `all_businesses.csv` for
  one business. Lead counts inflate, and the same prospect is scored and pitched
  three times. This defeats the single stated purpose of `dedup.py`.
- **Trigger:** OSM in Lebanon returns `name` in Arabic or French (`scrapers/osm.py:60`
  prefers `name`, then `name:en`, then `name:ar`); Google Places returns Latin
  `displayName` (`scrapers/google_places.py:236`). `normalize_name` keeps them in
  their original script, and `key = (normalize_name(name), city)` compares them as
  raw strings.
- **Fix:** add an Arabic→Latin transliteration key (see Design spec) and compare
  keys, not raw strings. After transliteration both sides are ASCII, so the
  similarity metric runs in one alphabet.

### S2 — Exact-equality key: no similarity metric, so near-misses never merge

- **Where:** `dedup.py:119`
- **Breaks:** Even within one script, `Al Salam`, `Al-Salam`, `Alsalam`, `Al Salam
  Restaurant` and `Al Salam Restaurant Co.` all produce different keys. The
  word-order variation `Cafe Beirut` vs `Beirut Cafe` and any typo also split a
  single business into multiple rows. Exact `dict` lookup has zero tolerance.
- **Trigger:** `normalize_name("Al Salam") != normalize_name("Alsalam")`, so two
  scrapes of the same shop never collide.
- **Fix:** replace the exact tuple key with a blocking key (city) plus a
  normalized-similarity threshold (Damerau-Levenshtein, see Design spec).

## Design spec — per-script normalization, transliteration, similarity

### 1. Script detection

```python
def _is_arabic(s: str) -> bool:
    return any(
        "\u0600" <= c <= "\u06ff"      # Arabic block
        or "\u0750" <= c <= "\u077f"   # Arabic Supplement
        or "\ufb50" <= c <= "\ufdff"   # Presentation Forms-A
        or "\ufe70" <= c <= "\ufeff"   # Presentation Forms-B
        for c in s
    )
```

### 2. Per-script normalization

```python
_HARAKAT = range(0x064B, 0x0653)          # fathatan..sukun (ccc 27-34)
_HARAKAT_EXTRA = {0x0670}                 # superscript alef

def normalize_name(name: str) -> str:
    if _is_arabic(name):
        # NFKC folds Presentation Forms to base letters AND recomposes أ إ آ ؤ ئ.
        # NFKD is wrong here: it decomposes the hamza letters and never recomposes.
        name = unicodedata.normalize("NFKC", name)
        name = name.replace("\u0640", "")  # tatweel/kashida
        name = "".join(c for c in name
                       if ord(c) not in _HARAKAT and ord(c) not in _HARAKAT_EXTRA)
    else:
        # Latin/French: NFKD + strip-combining is correct (café -> cafe, Æ -> AE).
        name = unicodedata.normalize("NFKD", name)
        name = "".join(c for c in name if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", name).casefold().strip()
```

Notes on the Unicode rules being relied on (UAX #15, "Unicode Normalization Forms"):

- **NFKD vs NFKC** differ only by the *canonical composition* step (UAX #15 §1.2).
  Both apply compatibility decomposition — which is what folds the Arabic
  Presentation Forms-A/B compatibility characters (`U+FB50–U+FEFF`) to base letters —
  but only the C forms recompose. For Arabic you must use the C form so that
  `أ إ آ ؤ ئ` survive as single letters.
- **Do not** use the compatibility mappings to "simplify" Arabic letters. `ة`
  (ta marbuta) and `ى` (alef maksura) have *no* compatibility decomposition in the
  UCD, so NFKC leaves them intact — that is fine; do not bolt on extra folding unless
  you mean to. The only deliberate letter-folding belongs in the transliteration key
  below, where the lossiness is what makes cross-script comparison possible.
- **Harakat** (`U+064B–U+0652`) are genuine optional marks (ccc 27–34); stripping
  them is safe and desirable for matching vocalized vs unvocalized text. The
  hamza/madda marks (`U+0653/0654/0655`, ccc 220–230) must be preserved — after NFKC
  they are already inside precomposed letters, so this harakat-only strip never
  touches them.

### 3. Transliteration key (Arabic → ASCII)

Transliterate with a fixed, dependency-free table. Fold the emphatic/back
consonants into their plain Latin spellings, because a Latin source name cannot
express them anyway — that folding is exactly what lets `سلام` and `صلاح`'s "s" be
compared to a Latin "s".

```python
_AR_TRANS = {
    "\u0621": "",  "\u0627": "a",  "\u0628": "b",  "\u062a": "t",
    "\u062b": "th", "\u062c": "j",  "\u062d": "h",  "\u062e": "kh",
    "\u062f": "d",  "\u0630": "dh", "\u0631": "r",  "\u0632": "z",
    "\u0633": "s",  "\u0634": "sh", "\u0635": "s",  "\u0636": "d",
    "\u0637": "t",  "\u0638": "z",  "\u0639": "'",  "\u063a": "gh",
    "\u0641": "f",  "\u0642": "q",  "\u0643": "k",  "\u0644": "l",
    "\u0645": "m",  "\u0646": "n",  "\u0647": "h",  "\u0648": "w",
    "\u064a": "y",  "\u0629": "h",  "\u0623": "a",  "\u0625": "a",
    "\u0622": "a",  "\u0624": "w",  "\u0626": "y",
}

def transliterate_key(name: str) -> str:
    if _is_arabic(name):
        return "".join(_AR_TRANS.get(c, c) for c in name)
    return name  # already normalized to ASCII in normalize_name
```

Use ALA-LC Romanization as the reference for the mapping choices
(Library of Congress, "ALA-LC Romanization Tables: Arabic") if you need to defend
the table; the table above is the *folded* matching form, not display transliteration.

### 4. Similarity metric across scripts

1. **Block** candidates by city (already available in `dedup.py:118` via
   `_extract_city`) so the comparison stays near-linear, not O(n²) over the full run.
2. Within a block, compare **transliterated ASCII keys** with a normalized
   Damerau-Levenshtein distance (it counts adjacent transpositions, which dominate
   in both transliteration and typing). Normalize by max length:
   `sim = 1 - dist(a, b) / max(len(a), len(b))`.
3. **Match** when `sim >= 0.85`, or `sim >= 0.80` for short names (≤ 6 chars). Use
   `rapidfuzz` (fast C extension) or `jellyfish`, or a ~20-line pure-Python
   Damerau-Levenshtein to avoid a dependency.
4. **Tiebreak / second pass** with a consonant skeleton: drop `a e i o u y '` from
   the key and compare again at a lower threshold. This catches vowel-spelling
   variants (`matam` vs `mataam` vs `mt3m` → `mtm`) that edit distance alone misses.

Keep the current `(name, city)` exact key as a fast-path only; the fuzzy path is the
real matcher.

## Not a bug, but worth knowing

- **`scrapers/osm.py:60` discards alternate-script names.** `tags.get("name") or
  tags.get("name:en") or tags.get("name:ar")` keeps exactly one name form and throws
  away the rest. For bilingual matching you ideally want *all* of `name`, `name:en`,
  `name:fr`, `name:ar` so the transliterator has both scripts to compare against.
  This is a data-capture gap that makes the cross-script fix above harder, not a bug
  in `dedup.py` itself.
- **`.lower()` vs `.casefold()`.** `dedup.py:69` uses `.lower()`. For French/Latin
  names the difference is negligible here (no `ß`, no Turkic dotless-i), but
  `.casefold()` is the correct Unicode case operation and costs nothing to switch.
- **Phones already cross the script gap.** `normalize_phone` (`dedup.py:33`) works in
  digits, so two records of the same business *with* a phone number dedup correctly
  regardless of script. The Arabic/Latin name problem only bites records that lack a
  phone — which is precisely the "weak digital presence" segment this product
  targets, so the bug disproportionately affects the highest-value rows.

## Recommended order of work

1. Fix `normalize_name` to branch on script: NFKC + harakat-only strip for Arabic,
   keep NFKD + strip-combining for Latin (one function, ~6 lines). This alone stops
   the silent letter erasure.
2. Add the transliteration key and switch `dedup()` from an exact `(name, city)`
   tuple to city-blocked Damerau-Levenshtein matching at the thresholds above.
3. (Follow-up, scrapers team) capture `name:en` / `name:fr` / `name:ar` alongside
   `name` in `scrapers/osm.py` so both scripts are available to the matcher.
