# 200 — `normalize_name`: the missing test contract

## Verdict

`normalize_name` (`dedup.py:66-69`) is half of the name-based identity key
(`dedup.py:214`), and it has **zero tests** — `tests/test_lead_signal.py:65`
imports `_merge` and `normalize_phone` and deliberately skips it. Only
`normalize_phone` got the "garbage must not collapse into a shared key"
regression set (`tests/test_lead_signal.py:102-123`), because that bug was
found. `normalize_name` has the *same class of bug*, found by three separate
audits and never fixed or pinned:

1. the combining-mark strip on `dedup.py:68` is **script-agnostic**, so it deletes
   Tamil/Malayalam/Thai vowel signs that are letters, not accents;
2. `NFKD` on `dedup.py:67` folds Arabic hamza/madda letters away;
3. nothing guards the empty result, so every whitespace-only name in a city
   shares the key `("", city)` and distinct businesses are silently merged.

I reproduced 1 and 3 end-to-end through `dedup()`: two records in, **one row
out**, and the surviving row carries the *other* business's `address`, `lat`,
`lon`, and therefore the wrong `region` (`enricher.py:89-92`). Below is the
exact test set that function still needs — 7 Hypothesis properties (4 of which
fail today) and 11 examples — plus an explicit list of what cannot be tested
here and why.

---

## The function and its blast radius

```python
# dedup.py:66-69
def normalize_name(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", name).lower().strip()
```

Call sites — exactly one:

```python
# dedup.py:211-214
name = record.get("name") or ""
city = _extract_city(record.get("address"))
key = (normalize_name(name), city)
```

Two facts that set the severity of everything below:

- **The mangled output never reaches the CSV.** `name` on `dedup.py:212` is a
  local; `name_index[key] = dict(record)` (`dedup.py:218`) stores the record
  untouched. So `normalize_name` can only cause harm by making two *merge
  decisions* come out wrong. It cannot corrupt a stored name. That is why the
  two `test_lead_signal.py` merge tests (`test_lead_signal.py:321-326`) and not
  this function's output are the trust surface.
- **It only runs on the phone-less path.** `dedup.py:205-206` routes any record
  with a usable phone to `phone_index` and never touches `normalize_name`. So
  every bug below is confined to records with **no usable phone** — which is
  exactly the "weak digital presence" segment the product exists to sell
  (`docs/audits/058-english-arabic-name-matching.md:197-201` makes this point
  and it is correct).

Verified environment for every claim below: Python 3.14.8,
`unicodedata.unidata_version == "16.0.0"`. I re-checked each assertion with the
literal input on this interpreter.

---

## Findings

### S1 — the combining-mark strip deletes letters in Indic and Thai scripts → one business becomes two rows, permanently

- **Where:** `dedup.py:68`
  (`"".join(c for c in name if not unicodedata.combining(c))`)
- **Breaks:** the filter drops **every** code point with a nonzero combining
  class, in every script. That is correct for Latin/French (it removes accents)
  and for Hebrew/Greek (it removes niqqud/tonos — desirable). It is destructive
  for scripts where the mark *is* the vowel: Tamil, Malayalam, and Thai
  vowel signs carry `ccc != 0` and are deleted, so the key is no longer a
  transcription of the name. Two spellings of the same business then produce
  two different keys, two rows in `all_businesses.csv`, and — because
  `main.py:179-180` re-dedups the *cumulative master* every run — the duplicate
  is re-created on every subsequent run and every count downstream
  (`main.py:220-234`) is inflated.
- **Trigger** (all four verified, exact outputs):

  | literal input | `normalize_name` output | letters destroyed |
  |---|---|---|
  | `"கோனிப்பரி"` (Tamil, Konippari) | `"கோனிபபரி"` | `்` U+0B95 CHILLU SIGN, ccc=9 |
  | `"കോഴിക്കട"` (Malayalam) | `"കോഴികട"` | `്` U+0D4D, ccc=9 |
  | `"ร้านอาหาร"` (Thai, "restaurant") | `"รานอาหาร"` | `้` U+0E49, ccc=230 |
  | `"मैसूर मिठाई"` (Hindi) | unchanged | none — Devanagari matras are `ccc=0` |

  Devanagari being safe is the tell: this is not "accents are removed", it is
  "any `ccc != 0` code point is removed", and whether that is right is a
  property of the script, not of the character class. Note `"ร้านอาหาร"`
  → `"รานอาหาร"` loses the mark from the **first** word, changing `ร้าน`
  ("shop") into `ราน` — the key no longer contains the name at all.
- **Why S1:** wrong results in the product CSV, non-recoverable across runs
  because the master is cumulative (`main.py:150`, `main.py:209`).
- **Reachability, honestly:** this needs the *name* to be in a Brahmic/Thai
  script. `restaurant` and `cafe` are tier-1 priority industries
  (`scrapers/whitelist.py:19`) and the region has a large South Asian
  workforce and business-owner population, so OSM `name` tags in Tamil or
  Malayalam (`scrapers/osm.py:63`) are credible. Google Places
  (`scrapers/google_places.py:268`) usually returns a Latin transliteration for
  the same shop, so the failure mode is specifically **native-script OSM record
  + Latin-script Google record for one business → no merge**, which is the
  cross-script problem of `docs/audits/058-english-arabic-name-matching.md:55`
  arriving by a different door.
- **Fix:** branch on script like `docs/audits/058-english-arabic-name-matching.md:105-118`
  proposes; strip `Mn` marks only in Latin/Greek/Hebrew, strip `Mc`/spacer
  marks in Brahmic, and drop Thai/Lao entirely. Add `unicodedata.script(ch)`
  (CPython 3.9+) as the branch predicate instead of a hand-rolled range check.

### S2 — whitespace-only names share the key `("", city)` → distinct businesses merge and both lose a row

- **Where:** `dedup.py:212-214`. No emptiness guard before building the key.
- **Breaks:** `normalize_name("   ")` is `""`, so every nameless record in the
  same `_extract_city` bucket collapses into one row. `_merge` then unions their
  fields, and `dedup.py:226-229` cannot separate them afterwards because they
  no longer exist as separate records.
- **Trigger** (verified, 2 records in → 1 row out):
  ```python
  dedup([
    {"name": "   ", "address": "Hamra, Beirut, Lebanon", "phone": "",
     "website": "https://a.example", "category": "cafe", "source": "osm"},
    {"name": "\t\n", "address": "Hamra, Beirut, Lebanon", "phone": None,
     "email": "b@b.example", "category": "cafe", "source": "osm"},
  ])
  # -> len == 1; the single row is
  #    {"name": "\t\n", "address": "Hamra, Beirut, Lebanon",
  #     "website": "https://a.example", "email": "b@b.example"}
  ```
  Note the surviving `name` is `"\t\n"` — `dedup.py:86-89` `_is_missing` treats
  whitespace as missing and returns `b`'s value, so the output row carries a
  name that is blank in the CSV and impossible to act on.
- **Reachability:** all three scraper guards are *falsy* checks, not *blank*
  checks — `scrapers/osm.py:64` (`if not name:`),
  `scrapers/wikidata.py:92`, `scrapers/google_places.py:269`. `bool("   ")` is
  `True`, so a whitespace-only tag passes every one of them. `main.py:172` then
  filters on **category only** — there is no name filter anywhere in the
  pipeline. And once a blank-name row exists in the cumulative master,
  `main.py:88-89` reads it back as `None`, which `dedup.py:212`'s `or ""`
  converts to `""` again, so the collision is re-formed every run forever.
- **Why S2 not S1:** only whitespace-only input can reach `""` (NFKD never
  empties a string — I checked U+00AD, U+200B, U+2060, U+FEFF, all preserved),
  so the triggering class is narrow. The consequence when it fires is total, so
  fix it, but do not treat it as the headline.
- **Fix:** `dedup.py:214` — skip the name index for a blank key, or give blank
  names a unique per-record key so they can never collide.

### S2 — `NFKD` + strip-combining folds Arabic hamza and madda letters away

- **Where:** `dedup.py:67-68`
- **Breaks:** `NFKD` decomposes the precomposed Arabic letters, then line 68
  deletes the mark that was just split off — but hamza/madda is part of the
  letter, not an optional vowel sign. Verified on this interpreter:

  | in | code points | after `normalize_name` |
  |---|---|---|
  | `أ` U+0623 | `0627 0654` | `ا` |
  | `إ` U+0625 | `0627 0655` | `ا` |
  | `آ` U+0622 | `0627 0653` | `ا` |
  | `ؤ` U+0624 | `0648 0654` | `و` |
  | `ئ` U+0626 | `064A 0654` | `ي` |

  So `normalize_name("مطعم أبو شقير") == "مطعم ابو شقير"` and
  `normalize_name("آل راشد") == "الراشد"` (verified: the keys are equal — `آل`
  "family of" and `ال` "the" become indistinguishable).
- **Why I rate this below `docs/audits/058-english-arabic-name-matching.md:17`'s
  S1:** in the *common* case the fold is in the direction you want — `أبو` and
  `ابو` are the same word spelled two ways, and merging them is correct
  dedup. The demonstrated damage is over-collapse onto the wrong word
  (`آل`/`الراشد`, `ؤ`→`و`, `ئ`→`ي`), not a systematic under-merge. That makes
  it a quality bug with a bounded blast radius, not the identity-model failure
  058 describes.
- **Fix:** same as 058 — `NFKC` for Arabic (recomposes `أ إ آ ؤ ئ`), strip only
  `U+064B-U+0652` and `U+0670`.
- **Correction to audit 058's premise, checked on this platform:** `U+0640
  ARABIC TATWEEL` has `unicodedata.combining() == 0` and category `Lm`, so the
  current strip **does not** remove it — `normalize_name("مـــطعم") !=
  normalize_name("مطعم")` (verified). 058's suggested `.replace("\u0640","")`
  is therefore load-bearing, not belt-and-braces.

### S2 — punctuation, `&`, apostrophes and legal suffixes are not folded → same business, two rows

- **Where:** `dedup.py:69` — only `\s+` is rewritten; no punctuation rule.
- **Breaks:** verified unequal pairs, each a realistic pair of spellings for
  one shop in this market:

  | A | B | keys |
  |---|---|---|
  | `"ABC Real Estate S.A.L."` | `"ABC Real Estate"` | `"abc real estate s.a.l."` / `"abc real estate"` |
  | `"Sari & Sons"` | `"Sari and Sons"` | `"sari & sons"` / `"sari and sons"` |
  | `"Café de l'Orient"` | `"Cafe de lorient"` | `"cafe de l'orient"` / `"cafe de lorient"` |
  | `"Al-Ra's"` | `"Al Rasa"` | `"al-ra's"` / `"al rasa"` |
  | `"Cafe (Hamra)"` | `"Cafe Hamra"` | `"cafe (hamra)"` / `"cafe hamra"` |
  | `"Zaatar w Zeit"` | `"Zaatar and Zeit"` | `"zaatar w zeit"` / `"zaatar and zeit"` |

  `S.A.L.` / `S.A.R.L.` / `SARL` are the standard Lebanese corporate forms, so
  the first row alone is a systematic under-merge on a whole tier-1 category
  (`scrapers/whitelist.py:34`, `real_estate`).
- **Fix:** fold `&`→`and`, strip trailing `.`/`,`/`'`/`-`, drop a trailing legal
  token from a fixed set, and drop a parenthesised branch qualifier.
- **Caveat to state in the test:** collapsing punctuation is *not* free. `Cafe
  (Hamra)` vs `Cafe Hamra` and `Al-Ra's` vs `Al Rasa` are not automatically the
  same business. Any test that asserts these merge is asserting a **product
  decision**, not a fact. See "What cannot be tested".

### S2 — no transliteration: an Arabic and a Latin record of one business never merge

- **Where:** `dedup.py:66-69` (no Latin projection), `dedup.py:214` (exact
  tuple lookup)
- **Breaks (verified):**
  ```python
  dedup([
    {"name": "مطعم الأرز",         "address": "Hamra, Beirut, Lebanon",
     "lat": 33.89, "lon": 35.50, "phone": "", "source": "osm"},
    {"name": "Al Arz Restaurant",  "address": "Hamra, Beirut, Lebanon",
     "lat": 33.89, "lon": 35.51, "phone": "",
     "email": "info@alarz.example", "source": "google_places"},
  ])  # -> len == 2
  ```
  Two rows for one business, same coordinates, one street apart. Neither can be
  scored or pitched twice correctly, and the master grows by one every run.
  This is `docs/audits/058-english-arabic-name-matching.md:55` S1, unchanged.
- **Why S2 here and not S1:** `058` covers the design. From *this* function's
  point of view the defect is "the function has no second key", which is a
  missing feature on the phone-less path only. It does not affect any record
  that has a phone (`dedup.py:205`).
- **Fix:** see `058`'s `_AR_TRANS` table + Damerau-Levenshtein block by city.
  Not a one-line fix; see the flip-test in the test set below so the change is
  visible in the diff when it lands.

### S3 — `.lower()` is not `.casefold()` → the key is not case-invariant

- **Where:** `dedup.py:69`
- **Breaks:** verified property violation on 20 000 fuzzed strings:
  `normalize_name(s) == normalize_name(s.upper())` fails for **8 855** of them.
  Minimal examples: `"Straße"` → `"straße"` but `"STRASSE"` → `"strasse"`;
  `"ı"` (U+0131) stays `ı` while `"I"` → `"i"`.
- **Why S3:** LB/SA business names in Latin script rarely contain `ß` or
  dotless `ı`, so this is a correctness-and-hygiene issue. `058:194-196` flagged
  it as "not a bug"; I agree with the severity and disagree with dismissing the
  *property*, which is cheap to assert and cheap to fix.
- **Fix:** `.casefold()` on `dedup.py:69` instead of `.lower()`.

### S3 — invisible format characters pass through untouched → two visually identical rows that did not merge

- **Where:** `dedup.py:68` (only `ccc != 0` is dropped) and `dedup.py:69`
  (only `\s` is rewritten).
- **Breaks:** these code points have `combining() == 0` and are not `\s`, so
  they survive verbatim. Verified survivors: `U+200B` ZWSP,
  `U+200C` ZWNJ, `U+200D` ZWJ, `U+200E`/`U+200F` LRM/RLM, `U+FEFF` BOM,
  `U+2060` WORD JOINER, `U+00AD` SOFT HYPHEN, `U+0640` TATWEEL.

  ```python
  normalize_name("Cafe\u200bX") == "cafe\u200bx"   # != "cafex"
  normalize_name("مـــطعم")  == "مـــطعم"     # != normalize_name("مطعم")
  ```
  The nastiest property of this class: the stored `name` is untouched
  (`dedup.py:218`), so `all_businesses.csv` shows two rows whose names look
  byte-identical on screen while `dedup` never considered them equal. That is
  expensive to debug by eye and invisible in a diff.
- **Note on what NFKD *does* fix:** `U+00A0` NBSP, `U+3000` ideographic space
  and `U+202F` narrow NBSP all have compatibility decompositions to `U+0020`
  and are handled correctly — `normalize_name("Cafe\u00a0\u00a0Beyrouth") ==
  "cafe beyrouth"` (verified). So the whitespace half of this finding is fine;
  only `Cf` (format) characters leak.
- **Fix:** `dedup.py:68` — also drop `unicodedata.category(c) == "Cf"`, and drop
  `U+0640` explicitly.

### S3 — non-`str` input raises `TypeError` instead of degrading

- **Where:** `dedup.py:67` (`unicodedata.normalize` requires `str`)
- **Breaks:** verified — `normalize_name` raises for `None`, `int`, `float`,
  `bytes`, `list`, `bool`:
  `TypeError: normalize() argument 2 must be str, not int`
- **Why S3, defending against `docs/audits/031-test-suite-design.md:145` which
  rates it S1:** I could not construct a reachable path. `dedup.py:212`'s
  `or ""` absorbs `None`/`""`/`0`; all three scrapers read `name` out of JSON
  tags and guard with `if not name` (`scrapers/osm.py:64`,
  `scrapers/wikidata.py:92`, `scrapers/google_places.py:269`), so the value is
  always `str`; and `main.py:85-104` reads the master with `csv.DictReader`,
  which yields `str` for `name`. If a future source ever emits
  `name: 123`, `dedup(combined)` at `main.py:180` is **not** inside a `try` and
  the run dies after up to 300 minutes of scraping (`timeout-minutes: 300`,
  `.github/workflows/scrape.yml`). The exposure is "any future schema change",
  not "today".
- **Fix:** `dedup.py:67` — `name = unicodedata.normalize("NFKD", str(name or ""))`.
  This is the cheapest fix in the report and the cheapest test.

---

## The tests `normalize_name` still needs

Where to put them: a **new** `tests/test_normalize_name.py`. `dedup.py:1-2`
imports only `re` and `unicodedata`, so the file needs no stubs — unlike
`tests/test_lead_signal.py:23-63`, which has to hand-roll a fake `requests`.
`pyproject.toml:75-78` already sets `testpaths = ["tests"]`, and `hypothesis` is
a declared dev dependency (`pyproject.toml:24`), so properties are available.
Because `tests/test_lead_signal.py:4-6` documents `python3 -m unittest discover
-s tests` as the supported invocation, wrap the property tests in
`unittest.skipUnless(importlib.util.find_spec("hypothesis"), ...)` so the
stdlib-only path keeps working.

Legend: **[green]** passes against `dedup.py` today — a regression guard.
**[red]** fails today — it pins a live bug; land it as a known failure
(`@unittest.expectedFailure`) or let it go red to document the gap.

### Properties (use these where they exist)

```python
class TestNormalizeNameProperties(unittest.TestCase):
    # P1 [green] — verified: 0 violations over 20k fuzzed strings.
    # Pins: the master CSV is re-deduped every run (main.py:179-180), so a
    # non-idempotent key would merge differently on run 1 and run 2 for the
    # same bytes. Also pins: normalize_phone("+9611...") idempotence is
    # already relied on the same way.
    def test_is_idempotent(self):
        assert normalize_name(normalize_name(s)) == normalize_name(s)

    # P2 [green] — verified: 0 violations over 20k fuzzed strings.
    # Pins: dedup.py:69's \s+ collapse. Literal seed cases in T1-T3 below.
    def test_output_is_trimmed_and_single_spaced(self):
        assert "  " not in out and out == out.strip()
        assert not any(ch.isspace() and ch != " " for ch in out)

    # P3 [green] — verified: 0 violations over 20k fuzzed strings.
    # This is the *intent* of dedup.py:68 and it is genuinely true today.
    # Do not delete it when the per-script fix lands: it is the property that
    # must keep holding for Latin.
    def test_latin_accents_are_removed(self):
        assert not any(unicodedata.combining(c) for c in out)   # Latin only

    # P4 [red] — verified: 8855 / 20000 failures. Minimal: "Straße" vs "STRASSE",
    # "ı" (U+0131) vs "I". Pins: .lower() on dedup.py:69.
    # Fix is .casefold().
    def test_is_case_invariant(self):
        assert normalize_name(s) == normalize_name(s.upper())

    # P5 [red] — verified: U+200B/U+200C/U+200D/U+200E/U+200F/U+FEFF/U+2060/
    # U+00AD/U+0640 all survive. Note Zs chars (U+00A0, U+3000, U+202F) do
    # NOT survive (NFKD maps them to U+0020) — keep them out of this alphabet.
    # Pins: dedup.py:68 only drops ccc!=0, so Cf characters pass through and
    # produce two visually identical CSV rows that never merged.
    def test_output_contains_no_format_characters(self):
        assert not any(unicodedata.category(c) == "Cf" for c in out)
        assert "\u0640" not in out

    # P6 [red] — the S1. Verified literals:
    #   "கோனிப்பரி" (Tamil)  -> "கோனிபபரி"   U+0B95 CHILLU SIGN, ccc=9, deleted
    #   "കോഴിക്കട" (Malayalam) -> "കോഴികട" U+0D4D, ccc=9, deleted
    #   "ร้านอาหาร" (Thai)  -> "รานอาหาร"    U+0E49, ccc=230, deleted
    #   "मैसूर मिठाई" (Hindi) unchanged  <- control: Devanagari matras are ccc=0
    # Pins: dedup.py:68 treating "ccc != 0" as "accent".
    def test_non_latin_scripts_lose_no_letters(self):
        # script in {Tamil, Malayalam, Thai, Bengali, Gujarati, Kannada,
        #            Telugu, Sinhala}
        assert out == unicodedata.normalize("NFKC", s)   # modulo the fix's own folds

    # P7 [green] — characterization guard. Verified: NFKD never empties a
    # string (U+00AD/U+200B/U+2060/U+FEFF are all preserved), so "" today comes
    # only from whitespace-only input. This test exists so a future switch to
    # NFKC + a strip-list cannot silently start returning "" for real names.
    def test_empty_output_only_for_whitespace_input(self):
        assert (normalize_name(s) == "") == (s.strip() == "")

    # P8 [green] — pins the Unicode version dependency.
    # Verified: on Python 3.14.8 / unidata 16.0.0, NFKD does NOT decompose
    # U+0649 ALEF MAKSURA or U+0629 TEH MARBUTA, but it DOES decompose
    # U+0623/U+0625/U+0622/U+0624/U+0626. Older/newer unidata releases have
    # changed Arabic decomposition entries. The dedup key therefore depends on
    # the interpreter's Unicode tables, and the key is NOT persisted — it is
    # recomputed from the master CSV on every run (main.py:179-180). A Unicode
    # bump on the CI runner can therefore change merge decisions for a master
    # built under a different one. Asserting the version makes the dependency
    # visible instead of silent.
    def test_key_is_pinned_to_a_known_unicode_version(self):
        self.assertEqual(unicodedata.unidata_version, "16.0.0")
```

### Examples (each pins a specific past bug; no property captures them)

| # | test method | literal input | asserted output | pins |
|---|---|---|---|---|
| T1 | `test_latin_accents_and_case_collapse` | `"Café de Beyrouth"`, `"CAFÉ DE BEYROUTH"` | both `"cafe de beyrouth"` | the *reason* `dedup.py:67-68` exists — must survive the per-script rewrite in `058` |
| T2 | `test_unicode_whitespace_variants_collapse` | `"Cafe\u00a0\u00a0Beyrouth"`, `"Cafe\u3000Beyrouth"`, `"  Café   de\nBeyrouth\t"` | all `"cafe beyrouth"` | NFKD's compat mapping of NBSP/U+3000 plus `dedup.py:69`'s `\s+` + `strip()` |
| T3 | `test_arabic_hamza_is_not_erased` **[red]** | `"مطعم أبو شقير"` | `"مطعم أبو شقير"` (unchanged) — currently returns `"مطعم ابو شقير"` | `058:17`; asserts `NFKC`, not `NFKD`, for Arabic |
| T4 | `test_alef_madda_is_distinct_from_alef_lam` **[red]** | `"آل راشد"` | `!= normalize_name("الراشد")` — currently **equal** | the concrete false-merge in `058:27-30` |
| T5 | `test_hamza_youseh_and_waw_are_distinct` **[red]** | `"مؤ"` / `"ئ"` | output retains `ؤ` / `ئ` — currently `"و"` / `"ي"` | `058:45-46` |
| T6 | `test_tatweel_is_stripped` **[red]** | `"مـــطعم"` | `== normalize_name("مطعم")` — currently `False` | verified: `U+0640` is `ccc=0`/`Lm`, so `dedup.py:68` misses it; `058:110`'s `.replace` is load-bearing |
| T7 | `test_blank_name_does_not_share_a_dedup_key` **[red]** | `dedup([{"name": "   ", "address": "Hamra, Beirut, Lebanon", "phone": "", "website": "https://a.example", ...}, {"name": "\t\n", "address": "Hamra, Beirut, Lebanon", "phone": None, "email": "b@b.example", ...}])` | `len(result) == 2` — currently `1`, and the merged row's `name` is `"\t\n"` | the S2 above; guards the falsy-vs-blank gap at `scrapers/osm.py:64` / `scrapers/wikidata.py:92` / `scrapers/google_places.py:269` |
| T8 | `test_false_merge_does_not_move_coordinates` **[red]** | `dedup` over `{"name": "مطعم أبو شقير", "address": "Hamra, Beirut, Lebanon", "lat": 33.8938, "lon": 35.5018, "phone": "", "source": "osm"}` and `{"name": "مطعم ابو شقير", "address": "Tripoli, Lebanon", "lat": 34.4367, "lon": 35.8497, "phone": "", "source": "osm"}` | `len(result) == 2`; and for T3/T6-fixed code, `result[0]["lat"] == 33.8938` and `infer_region("Hamra, Beirut, Lebanon", 33.8938, 35.5018, "LB") == "Beirut"` | end-to-end proof that a name-key collision moves `address`/`lat`/`lon`/`region` between real businesses. **Verified today: `len(result) == 1`, the surviving row reads `address="Tripoli, Lebanon"`, `lat=34.4367`, `region="North Lebanon"`** — the Beirut shop is now filed in North Lebanon |
| T9 | `test_legal_suffix_is_folded` **[red]** | `"ABC Real Estate S.A.L."` | `== normalize_name("ABC Real Estate")` — currently `"abc real estate s.a.l."` vs `"abc real estate"` | `003:111-113`; `S.A.L.`/`SARL` are standard Lebanese forms for tier-1 `real_estate` (`scrapers/whitelist.py:34`) |
| T10 | `test_punctuation_variants_fold` **[red]** | `"Sari & Sons"` / `"Al-Ra's"` / `"Café de l'Orient"` / `"Cafe (Hamra)"` / `"Zaatar w Zeit"` | each pair's keys are equal — currently all unequal | `003:114-115`; see the product-decision caveat below |
| T11 | `test_arabic_and_latin_records_do_not_merge_yet` **[flip]** | `dedup` over `{"name": "مطعم الأرز", "lat": 33.89, "lon": 35.50, "phone": ""}` and `{"name": "Al Arz Restaurant", "lat": 33.89, "lon": 35.51, "phone": "", "email": "info@alarz.example"}` | `len(result) == 2` — **verified true today** | `058:55`. Write it asserting `2` so the gap is visible in `git log`; when transliteration lands, this test flips red and *is* the change-detector. Do not write `assertEqual(len(result), 1)` now — that is a failing test with no implementation behind it |
| T12 | `test_non_string_input_does_not_raise` **[red]** | `None`, `123`, `4.2`, `b"Cafe"`, `True` | each returns `""` (or a `str`) — currently `TypeError: normalize() argument 2 must be str, not int` | `031:222-224`; one-line `str(name or "")` at `dedup.py:67` |

Run them with:

```bash
python3 -m unittest discover -s tests -v          # stdlib only
python3 -m pytest tests/test_normalize_name.py   # includes the properties
```

---

## What genuinely cannot be tested here, and why

1. **Whether a given merge is *correct*.** `normalize_name` is a key function.
   Unit tests can only pin the string transform; deciding that
   `"Cafe (Hamra)"` and `"Cafe Hamra"` are one business (T10) or that
   `"Straße"` and `"STRASSE"` are one business (P4) is a **product decision**,
   not a fact about the code. Any test asserting those merges bakes in a
   judgement call. Write the decision down in a comment next to the test or
   someone will "fix" it later without knowing the assumption.
2. **Fuzzy / similarity matching.** There is none (`dedup.py:214` is an exact
   tuple). T11 can only pin the current non-merge; the threshold behaviour
   `058:172-181` specifies (`sim >= 0.85`) is untestable until the
   implementation exists, and even then it needs a labelled corpus to have a
   meaningful expected value.
3. **How often each input class occurs in real data.** Requires a real scrape —
   Overpass, Wikidata, Google Places, or one HTTP GET per website in
   `enricher.py` — which the brief forbids (rules 2 and 3). So I cannot tell
   you how many of the 20 000-row masters carry Tamil names, whitespace-only
   names, or ZWSPs, and I have not pretended to. The offline substitute is a
   one-off report script over `data/all_businesses.csv` (gitignored, present
   only on a machine that has run the pipeline) counting: names whose
   `normalize_name` is empty; names containing `unicodedata.category(c) == "Cf"`;
   names whose script is not Latin/Arabic/Greek/Hebrew; and the ratio of
   `N(s) != N(s.upper())`. That is a script, not a test — do not check it into
   `tests/`.
4. **Cross-source agreement.** "Do OSM and Google Places agree on this shop's
   name?" needs both sources (`058:63-67`). Cannot be tested offline at all.
5. **Key stability across a CSV round-trip.** `write_csv`/`load_master` rewrite
   `name` through `utf-8-sig` (`main.py:85`, `main.py:124`), which is relevant:
   a leading BOM is stripped on read but a BOM *inside* a name is not. That is
   a `main.py` concern and belongs to a different lens; the `Cf` assertion in
   P5 already covers the `normalize_name` half.
6. **The `_extract_city` half of the key.** `dedup.py:214` builds a *tuple*.
   `normalize_name` can be perfect and the key still be near-useless, because
   `_extract_city` returns the country for every address the scrapers produce
   (`003:19-42`, verified: `"Hamra, Beirut, Lebanon"` and `"Tripoli, Lebanon"`
   both yield `"lebanon"`). Nothing tested against `normalize_name` alone will
   catch the over-merge that follows. **A single `dedup()`-level test that
   asserts two same-named phone-less businesses in Beirut and Tripoli stay as
   two rows is worth more than every `normalize_name` test in this document
   combined** — but it belongs to the identity-model lens, not this one.

---

## Not a bug, but worth knowing

- **Stale line references in the audit corpus.** `docs/audits/003:104` and
  `docs/audits/031:89` cite `normalize_name` at `dedup.py:28-31`; it is at
  `dedup.py:66-69` today. `003`'s S2 claim that "Arabic alef variants have no
  canonical decomposition and are left distinct" (`003:108-110`) is **wrong**
  and the opposite happens — they collapse. Do not implement against that line.
- **The three `Whitespace` behaviours that already work.** NFKD maps NBSP,
  ideographic space and narrow NBSP to `U+0020`; `dedup.py:69`'s `\s+` collapses
  runs; `.strip()` removes the edges. Verified, 0 violations in 20 000 fuzzed
  strings. T2 exists to stop anyone "simplifying" that away.
- **Idempotence already holds** (P1, 0 violations in 20 000 fuzzed strings).
  That is worth knowing because `main.py:179-180` re-normalizes the entire
  cumulative master on every run and every downstream count depends on the same
  bytes producing the same partition twice.
- **No CI runs the tests.** `.github/workflows/` contains only `scrape.yml`,
  which has no test step. Adding twelve tests that nobody runs buys nothing;
  that is a one-line `workflow_dispatch` addition and is out of this lens's
  scope.
- **`normalize_name` never writes to the CSV.** Because `dedup.py:212`'s local
  `name` is discarded and `dedup.py:218` stores `dict(record)`, no bug in this
  function can put a mangled name in `all_businesses.csv`. Every symptom is a
  wrong *merge decision*, and every fix should be evaluated on "does this
  change which rows collapse", not on the string output alone.

## Recommended order of work

1. **T12 + P4 + P5 + P6** — cheapest fixes with the widest coverage. `str(name
   or "")`, `casefold()`, drop `Cf` + `U+0640`, script-branch the strip. Land
   the per-script version of `058:105-118` and keep T1/T2 green.
2. **T7** — the `("", city)` guard in `dedup()`. One line, and it stops a class
   of silent row loss that recurs every run.
3. **T3/T4/T5/T6** — the Arabic branch. Only safe *after* step 1, because a
   naive `NFKC` switch without the `Cf`/tatweel handling regresses T6.
4. **T9/T10** — punctuation and legal-suffix folding, but only after someone
   writes down which folds are product decisions (see "cannot be tested" #1).
5. **T11 stays at `2`** until transliteration and fuzzy matching exist. Its value
   is as a change-detector, not as an assertion of desired behaviour.