# 200 — `normalize_phone`: what is still untested, and the tests it needs

**Target:** `dedup.py:33-63`
**Callers:** `dedup.py:205` (dedup key), `dedup.py:228` (phone already captured?),
`main.py:194` (value written to all five CSVs).
**Existing tests:** `tests/test_lead_signal.py:102-123` — 4 methods, 6 assertions.
**Verified by:** importing `dedup` and calling `normalize_phone` directly. It imports only
`re` and `unicodedata` (`dedup.py:1-2`), so it is fully exercisable offline with no stub.

## Verdict

The four existing tests are correct and they lock in the fixes from
`docs/audits/001-dedup-phone-normalization.md` for junk handling and `00` stripping. They
are **presentation-only**: every one of them feeds a single well-formed string, so nothing
in the suite can notice that `normalize_phone` is a function of *how the number was
written*, not just of the number. Four of the twelve presentation variants of one real
Beirut landline and one real Riyadh mobile each produce **two different dedup keys**, which
splits a business across two rows of the CSVs; a 6-digit short code produces a key
**byte-identical to a real landline**, which merges two different businesses; and
non-ASCII digits survive `\D` untouched, so an Arabic-Indic number never matches its ASCII
twin. The tests this function still needs are therefore mostly *properties over generated
variants and corpora*, not more examples — an example table would encode today's guesses
instead of the invariants.

## Findings

### S1 — A trunk zero after the calling code survives: one business, two keys
- **Where:** `dedup.py:53-55` (the `0 + cc` branch rebuilds `cc + digits[len(cc)+1:]`, i.e.
  it keeps a trunk zero if one is present) and `dedup.py:57-60` (the known-code branch
  returns `digits` verbatim).
- **Breaks:** national and international presentations of the same number disagree.
  `dedup.py:58-60` returns as soon as the digits start with a known calling code, so it
  never strips the national trunk prefix that `dedup.py:63` strips on the other branch.
  `dedup()` therefore files the two presentations under different keys, and the output
  CSV gets two rows for one client. `main.py:194` re-normalizes but does not repair it:
  `had_plus` is still true, so the same wrong key is written.
- **Trigger (all verified by execution):**

  | input | country | actual | expected |
  |---|---|---|---|
  | `"01 234 567"` | `LB` | `+9611234567` | `+9611234567` |
  | `"+961 01 234 567"` | `LB` | `+96101234567` | `+9611234567` |
  | `"00961 01 234 567"` | `LB` | `+96101234567` | `+9611234567` |
  | `"03 123 4567"` / `"+961 03 123 4567"` | `LB` | `+96131234567` / `+961031234567` | `+96131234567` |
  | `"011 234 5678"` / `"+966 011 234 5678"` | `SA` | `+966112345678` / `+9660112345678` | `+966112345678` |
  | `"050 123 4567"` / `"+966 050 123 4567"` | `SA` | `+966501234567` / `+9660501234567` | `+966501234567` |

  Simulating `main.py:183-194` on the pair a master row and a fresh OSM row would produce:
  `+9611234567` and `+96101234567` in `all_businesses.csv` for the same cafe.
- **Fix:** after recognising a calling code, strip one leading `0` from the remaining
  national significant number before returning (same rule as `dedup.py:63`).
- **Pins:** `001-dedup-phone-normalization.md` S1 (its "line 15-19", now `dedup.py:57-60`),
  which was never implemented.

### S1 — `\D` keeps Arabic-Indic digits, so non-ASCII numbers never match and leak into the CSV
- **Where:** `dedup.py:44` — `re.sub(r"\D", "", raw)`. Python's `\d` is Unicode-aware, so
  `٠`–`٩` (U+0660–0669, category Nd) are digits and are *kept*.
- **Breaks:** two failures at once. (a) `lstrip("0")` at `dedup.py:63` and
  `startswith(known)` at `dedup.py:59` only understand ASCII, so the country code is not
  recognised from an Arabic-Indic number and the trunk zero is not stripped. (b) The
  returned key contains Arabic-Indic digits, which is an undialable string that lands in
  all five CSVs via `main.py:194`.
- **Trigger (verified):** `normalize_phone("٠٧٩٠١٢٣٤", "LB")` → `'+961٠٧٩٠١٢٣٤'`;
  `normalize_phone("٠٧ ٩٠١ ٢٣٤", "LB")` → the same key;
  `normalize_phone("+٩٦١ ٧ ٩٠١٢٣٤", "LB")` → `'+961٩٦١٧٩٠١٢٣٤'`.
  `normalize_phone("07901234", "LB")` → `'+9617901234'` ≠ the above: **the same number in
  two scripts gets two keys.** This is a Lebanon-only defect and OSM/wikidata entries in
  Lebanon are exactly where Arabic-Indic digits appear.
- **Fix:** normalise digits first — `unicodedata.normalize("NFKC", raw)` (already imported
  at `dedup.py:2`, used at `dedup.py:67`) maps Arabic-Indic to ASCII — then `\D`.
- **Pins:** nothing in `001` or in the existing suite; new.

### S1 — `_MIN_DIGITS` is checked before the `00` is stripped, so short codes become real keys
- **Where:** `dedup.py:45` runs on the pre-strip digit string; `dedup.py:50-52` then removes
  two digits that were never accounted for. This directly contradicts the contract written
  in the comment at `dedup.py:28-30` ("must never become a dedup key").
- **Breaks:** a short code or a truncated number passes the 7-digit gate *because of the
  prefix it is about to lose*, then returns a key of the same shape as a real number.
- **Trigger (verified):**
  - `normalize_phone("001 234 567", "LB")` → `'+9611234567'`, which is
    **byte-identical** to `normalize_phone("01 234 567", "LB")` → `'+9611234567'`, a real
    Beirut landline. `dedup()` merges them: `[{"name":"Beirut Electrical Co","phone":"01 234 567"},
    {"name":"Shortcut vendor blurb","phone":"001 234 567"}]` → 1 row out, name
    `"Shortcut vendor blurb"`. Silent loss of a real lead.
  - `normalize_phone("00 12 34 56", "LB")` → `'+961123456'` — a 6-digit national number,
    below the module's own stated floor.
  - `normalize_phone("0000001", "LB")` → `'+9611'`; with `"SA"` → `'+9661'`. A 7-character
    junk string becomes a plausible-looking phone in the output CSV. (The one existing junk
    test, `test_lead_signal.py:106`, misses this because its corpus is punctuation-only.)
- **Fix:** re-apply the length check to the national significant number *after* prefix
  handling, and make the threshold plan-aware rather than a single global 7.
- **Pins:** `001-dedup-phone-normalization.md` S2 ("line 12, 19-25"), partially fixed.

### S1 — Foreign numbers in national-trunk form are re-labelled Lebanon and split by country label
- **Where:** `dedup.py:53-55` only recognises `0 + cc` for the *requested* country;
  `dedup.py:63` then prefixes `cc` unconditionally. The header comment at `dedup.py:5-7`
  promises exactly the opposite behaviour for cross-border leaks.
- **Breaks:** (a) a foreign national number inherits the record's country and its key
  collides with a real number in that country; (b) the same foreign number keys differently
  depending on the record's `country` value, so the leak defeats dedup instead of helping.
- **Trigger (verified):**
  - `normalize_phone("0790 1234", "LB")` → `'+9617901234'` — identical to the real Lebanese
    `+961 79 012 34`. `dedup([{"name":"Beirut Electrical Co","phone":"+961 79 012 34"},
    {"name":"Amman Auto Spares","phone":"0790 1234"}])` → **1 row out**, survivor
    `"Amman Auto Spares"`. Two companies in two countries merged into one lead.
  - `normalize_phone("0962 7 9012345", "LB")` → `'+96196279012345'`;
    the same string with `"SA"` → `'+96696279012345'`. Note the contrast: the `00` form
    `"00962 7 9012345"` is correct under both (`'+96279012345'`), so the defect is exactly
    the missing `0 + <any known cc>` case, not the concept.
- **Fix:** apply the `0 + code` transformation for every code in `_KNOWN_COUNTRY_CODES`
  before falling back to the bare-national path.
- **Pins:** `001` S1 #1 — still incomplete; the `00` half was fixed (`dedup.py:50`), the
  trunk-zero half was not.

### S2 — `;`-separated multi-numbers and extensions are concatenated into the key
- **Where:** `dedup.py:44`. OSM's own convention is `phone=number;number`
  (<https://wiki.openstreetmap.org/wiki/Key:phone>, <https://wiki.openstreetmap.org/wiki/Semi-colon_value_separator>)
  and `osm.py:89` reads `phone`/`contact:phone` with no splitting.
- **Breaks:** an undialable key of arbitrary length, and a split from the master row that
  holds one of the numbers. No false merge, so S2 not S1.
- **Trigger (verified):** `normalize_phone("+961 3 123 456; +961 1 234 567", "LB")` →
  `'+96131234569611234567'` (20 digits); `normalize_phone("961-3-123456;961-1-234567","LB")`
  → the same; `normalize_phone("+966 01 234 5678 x123","SA")` → `'+966012345678123'`
  (wrong twice: the trunk zero survives *and* the extension is folded in);
  `normalize_phone("+961 3 123 456 ext. 12","LB")` → `'+961312345612'` (the base number
  `+9613123456` is unrecoverable from it).
- **Fix:** split on `;` first and keep the first parseable number (or store the list and
  key on all members); strip `ext`/`x`/`#` suffixes before `\D` and keep the extension in a
  separate field — `BusinessRecord` (`scrapers/base.py:1-22`) has no extension column, so
  this needs one, or the suffix must simply be dropped.
- **Pins:** `001` S1 (its "line 12"), unfixed.

### S2 — `country` is a silent guess, and `dedup()` runs before `resolve_country()`
- **Where:** `dedup.py:48` `_COUNTRY_CODES.get(country, "961")`, `dedup.py:33` default
  `country: str = "LB"`, `dedup.py:205`/`dedup.py:228` `record.get("country") or "LB"`.
- **Breaks:** every value that is not literally `LB` or `SA` becomes 961 with no signal,
  and the pipeline's alias resolver never runs on it: `main.py:180` calls `dedup(combined)`
  *before* `main.py:184` calls `resolve_country(r)`. `resolve_country`
  (`main.py:60-67`) knows `KSA`, `SAU`, `LEB`; `normalize_phone` knows none of them.
- **Trigger (verified):** `"0501234567"` → `'+966501234567'` under `"SA"` but
  `'+961501234567'` under every one of `"sa"`, `"KSA"`, `"Saudi Arabia"`, `"XX"`, `""`,
  `None`, `"  "`, `"Lebanon"`, `"LEB"`. And `resolve_country` does **not** rescue
  `"Saudi Arabia"`: `main.py:60-67` has no mapping for it, `main.py:68-73` only sniffs
  `+966`/`00966`, so a master row with `country="Saudi Arabia", phone="050 123 4567"`
  leaves the pipeline as `country=LB, phone=+961501234567` — a 9-digit national number in a
  7–8-digit country, undialable, in the client-facing CSV. `dedup()` also returns 2 rows for
  that business plus its correctly-labelled twin.
- **Fix:** make the country argument validated, not defaulted — return `""` (fall through to
  the name key) for an unrecognised code, and have `dedup()` route through
  `resolve_country` so aliases are handled once.
- **Pins:** `001` S1 #1's "anything other than `LB`/`SA` silently defaults to Lebanon" and
  `test_lead_signal.py:126-153` (which fixed `resolve_country` but not its bypass).

### S3 — `_KNOWN_COUNTRY_CODES` order is an undeclared invariant
- **Where:** `dedup.py:58-60`. The loop returns on the first prefix match; correctness
  depends on no code being a prefix of another.
- **Breaks:** if someone adds a code that prefixes an existing one (a 1-digit code, or
  `961x` for a future plan change), the shorter code wins and silently swallows longer
  numbers.
- **Trigger:** today the invariant holds — verified: no ordered pair in
  `dedup.py:8-21` has one code as a prefix of another; lengths are `{2, 3}`.
- **Fix:** longest-match-first (`sorted(..., key=len, reverse=True)`) plus a test.
- **Pins:** nothing; new guard.

### S3 — `normalize_phone` is called twice per record in `dedup()`
- **Where:** `dedup.py:205` and again at `dedup.py:228` for every name-keyed record.
- **Breaks:** nothing incorrect; it is ~2x the regex work over the master on every run.
  Worth noting only because a per-record memo keyed on `(raw_phone, country)` makes the
  property tests in the next section cheap enough to run over a real corpus.
- **Pins:** n/a.

## The tests this function still needs

Style: same file, same stdlib-only `unittest` runner as
`tests/test_lead_signal.py:1-20` (`python3 -m unittest discover -s tests -v`). No `requests`
stub is needed for this class — only `enricher`/`main` imports need it
(`test_lead_signal.py:23-63`).

**Keep the 4 existing methods unchanged** (`test_lead_signal.py:103-123`). They are correct
and their fixtures are realistic: `+96170123456` is an 8-digit Lebanese mobile NSN and
`+962512345678` is a 9-digit Jordanian mobile NSN.

### Properties (preferred — these fail today and each one is a whole bug class)

| # | test method | property | literal input | expected | actual today | pins |
|---|---|---|---|---|---|---|
| P1 | `test_result_is_ascii_e164_or_empty` | every non-empty result matches `^\+[1-9][0-9]{6,14}$` | `"٠٧٩٠١٢٣٤"` / `LB` | raises AssertionError today; after fix input yields `+9617901234` | `+961٠٧٩٠١٢٣٤` | S1 non-ASCII |
| P2 | `test_all_presentations_of_one_number_agree` | for one number, all 12 generated presentations normalize identically | `"+961 01 234 567"` vs `"01 234 567"` / `LB` | both `+9611234567` | `+96101234567` vs `+9611234567` | S1 trunk zero; 001 S1 |
| P3 | `test_national_significant_number_matches_the_plan` | NSN is 7–8 digits for `+961`, exactly 9 for `+966`, never starts `0` | `"00 12 34 56"` / `LB` | `""` | `+961123456` (NSN 6) | S1 min-digits; 001 S2 |
| P4 | `test_unusable_input_never_yields_a_key` | junk, short codes and truncated numbers all yield `""` | `"001 234 567"` / `LB` | `""` | `+9611234567` | S1 min-digits |
| P5 | `test_foreign_trunk_zero_number_is_country_label_independent` | `np(x,"LB")==np(x,"SA")` for any x, not just international x | `"0962 7 9012345"` | `+96279012345` under both | `+96196279012345` / `+96696279012345` | S1 foreign leak; 001 S1 |
| P6 | `test_known_real_numbers_never_share_a_key` | distinct real numbers → distinct keys; a key is never shared by a real number and junk or a foreign number | `"01 234 567"` vs `"001 234 567"`; `"+961 79 012 34"` vs `"0790 1234"` | 4 distinct keys | 2 collisions | S1 ×2 |
| P7 | `test_extension_is_not_folded_into_the_key` | base number recovered; extension not in the key | `"050 123 4567 x123"` / `SA` | `+966501234567` | `+966501234567123` | S2 ext; 001 S1 |
| P8 | `test_multi_number_values_are_split_not_concatenated` | a `;` list yields the first number only | `"+961 3 123 456; +961 1 234 567"` / `LB` | `+9613123456` | `+96131234569611234567` | S2 OSM `;` |
| P9 | `test_country_label_never_changes_an_international_key` | for every label in a corpus, `np(x,c)` is constant | `"+966501234567"` over `("LB","SA","sa","KSA","XX","",None,"  ","Lebanon")` | all `+966501234567` | already passes | locks 001 S1 #1 fix |
| P10 | `test_normalize_is_idempotent` | `np(np(x,c),c)==np(x,c)` | `"+961 01 234 567"` / `LB` | passes | passes | guard only — **do not ship P10 without P2** |
| P11 | `test_known_country_codes_are_prefix_free` | no code in `_KNOWN_COUNTRY_CODES` is a prefix of another | `dedup.py:8-21` | already passes | passes | S3 ordering invariant |
| P12 | `test_dedup_output_has_unique_phone_keys` | after `main.py:183-194`, no two rows of `all_businesses.csv` share a normalized phone | master `{"phone":"+9611234567","country":"LB"}` + fresh `{"phone":"+961 01 234 567","country":"LB"}` | 1 row | 2 rows, keys `+9611234567` and `+96101234567` | S1 end-to-end |

P1–P8 are red today. P9–P11 are green and should be committed **before** the fixes, as
characterization of the behaviour that must not regress. P10 is a trap worth calling out:
`+96101234567` is idempotent, so an idempotence-only suite passes on the bug in S1 — it is
strictly weaker than P2.

Property generators worth adding, both cheap:

- **variant generator** over `(national, cc, country)`: `national trunk0`, `national bare`,
  `+cc+body`, `+cc+national` *(the failing one)*, `00cc+body`, `00cc+national`, `cc+body`,
  `0cc+body`, spaced, dashed, dashed E.164, parenthesised. P2 over
  `("01234567","961","LB")`, `("031234567","961","LB")`, `("0501234567","966","SA")`,
  `("0112345678","966","SA")` currently yields **2 distinct keys each** — verified.
- **junk corpus**: extend the tuple at `test_lead_signal.py:106`
  (`"---","...","  ","n/a","ext 4"`) with `"001 234 567"`, `"00 12 34 56"`, `"0000001"`,
  `"112"`, `"140"`, `"911"`, `"961"`, `"٠٧٩٠١٢٣٤"`, `"+٩٦١ ٧ ٩٠١٢٣٤"`,
  `"961-3-123456;961-1-234567"`, `"+961 3 123 456 ext. 12"`. P1/P3/P4 over it.

Also worth one **negative control** at the `dedup()` level, because the unit tests alone
cannot see a merge:

```python
def test_two_different_businesses_are_not_merged(self):
    """A real Lebanese landline and a 6-digit short code must stay two rows."""
    out = dedup([
        {"name": "Beirut Electrical Co", "country": "LB", "phone": "01 234 567", "source": "osm"},
        {"name": "Shortcut vendor blurb", "country": "LB", "phone": "001 234 567", "source": "wikidata"},
    ])
    self.assertEqual(len(out), 2)
    # and the cross-border case
    out = dedup([
        {"name": "Beirut Electrical Co", "country": "LB", "phone": "+961 79 012 34", "source": "osm"},
        {"name": "Amman Auto Spares", "country": "LB", "phone": "0790 1234", "source": "google_places"},
    ])
    self.assertEqual(len(out), 2)
```
Both fail today (verified: 1 row each).

### What the existing suite structurally cannot catch

The 4 methods at `test_lead_signal.py:103-123` pass exactly 6 strings, all single
representations. There is no input in them where the same number appears twice in two
formats, no input where the `country` argument should not matter, and no junk with digits.
Each of the five S1s above lives in exactly one of those three holes. Adding more
hand-written pairs without the generators will keep finding the bugs you already know
about.

## What genuinely cannot be tested here, and why

1. **"Every returned key is actually dialable."** That needs numbering-plan metadata
   (ITU/libphonenumber). `requirements.txt` has 3 pins and none of them is a phone library,
   and rule 3 forbids installing one. So P3 can only assert a hard-coded shape (LB NSN
   7–8, SA NSN 9) — **my digit counts are from secondary sources
   (<https://en.wikipedia.org/wiki/E.164>, and LB area-code/trunk-prefix conventions), not
   from the repo.** Writing them into the suite bakes an unverified assumption into CI; mark
   them as a data table next to the test and cite the source, or gate them behind
   `phonenumbers` once a dependency policy exists.
2. **How often each raw format actually occurs.** That needs a sample of
   `data/all_businesses.csv` or a live query against OSM/Wikidata/Google; rule 2 forbids
   the network and `data/` is gitignored. So every exposure statement above is reasoned from
   tag conventions (`osm.py:89`, `wikidata.py:97`, `google_places.py:289`) and the code,
   **not measured.** Do not encode today's frequency guess as examples. Once real output
   exists, run P1–P8 over `data/all_businesses.csv` as a corpus and let the data set the
   fixtures.
3. **Whether the Arabic-Indic case is actually frequent.** Same reason. The bug is
   unconditional, so P1 is safe to commit regardless, but its *priority* depends on data I
   cannot collect.
4. **`normalize_phone` in isolation cannot see the CSV contract.** "No duplicate phone in
   `all_businesses.csv`" is a property of `main.py:180-213` plus this function. P12 as
   written simulates the two relevant lines; a real end-to-end test needs a pipeline run,
   which needs network. Do not fake it with `unittest.mock` around the scrapers — that
   tests the mock.
5. **Extension round-tripping.** The brief fixes the column list at 22 keys
   (`main.py:34-40`); there is no extension field, so "the extension is preserved
   separately" (P7) can only be asserted as "the extension is absent from the key". Adding
   the column is a schema change, out of scope for a test-only fix.
6. **Ordering of `_KNOWN_COUNTRY_CODES`** is currently correct, so P11 cannot be a
   regression test for a bug that does not exist yet. It is a tripwire for the next person
   who edits `dedup.py:8-21`, nothing more.

## Not a bug, but worth knowing

- `"+050 123 4567"` → `+966501234567` and `"call 70123456"` → `+96170123456`: `had_plus`
  (`dedup.py:43`) only gates the `0+cc` branch, and `lstrip("0")` at `dedup.py:63` saves
  both by accident. Do not read those two as evidence the function handles a plus sign.
- `normalize_phone("20 123 45678", "LB")` → `+2012345678`: a leaked Egyptian number keeps
  its own calling code via `dedup.py:58-60`, so the header comment at `dedup.py:5-7` is
  honoured for `00`/`+` forms and violated for trunk-zero national forms (S1). `_KNOWN_COUNTRY_CODES`
  carries 12 countries but `_COUNTRY_CODES` carries 2 (`dedup.py:23-26`); the asymmetry is
  the whole bug.
- `"tel:+961-3-123456"` → `+9613123456` is correct, because `wikidata.py:97` strips `tel:`
  first and RFC3966 `+961-3-123456` carries no trunk zero.
- Adjacent, not my lens: `_extract_city` takes the **last** comma-separated part
  (`dedup.py:75`), which is `"lebanon"` for Google's `"1 St, Beirut, Lebanon"` and is
  `addr:district` for OSM's 5-part concatenation (`osm.py:80-87`). So the name-keyed branch
  at `dedup.py:211-218` groups by country/district, not by city, and two same-named
  businesses in Beirut and Tripoli share a key. `_merge`'d `_pick` is trust-ranked, so
  there is no merge-order data loss, but this is a real over-merge and it belongs to
  whoever owns dedup keys. `("", "")` is also a live key: a record with no name and no
  address merges with every other such record.

## Recommended order of work

1. P1 + P9 + P11 (ASCII-only output, country-independence for international input, code-list
   invariant) — commit green, lock in the fixes that already landed.
2. Fix S1 non-ASCII digits: one `unicodedata.normalize("NFKC", raw)` at `dedup.py:42`.
   Smallest diff, largest single win for a Lebanon scraper.
3. Fix S1 min-digits: re-check length after prefix handling (`dedup.py:45`), so P3/P4 go
   green and the short-code merge with a real landline is closed.
4. Fix S1 trunk zero (`dedup.py:53-60`) so P2/P6/P12 go green; this is the duplicate-lead bug.
5. Fix S1 foreign trunk-zero (generalise the `0+cc` branch to every known code) so P5 goes
   green, then add the `dedup()` negative control above.
6. S2: split `;` lists, drop extension suffixes; add P7/P8.
7. S2: stop guessing the country — validate instead of default, and make `dedup()` call
   `resolve_country` so the alias table at `main.py:60-67` is the single source of truth.
8. Only then run P1–P8 as a corpus over `data/all_businesses.csv` and fix whatever the
   real distribution exposes.