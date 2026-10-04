# 200 — `normalize_phone` (dedup.py:33) correctness

## Verdict

The function is **not correct**, and the two worst defects are silent: a zero-filled
placeholder phone (`"0000000"`) normalizes to the bare country code `"+961"`, so every
such record in the corpus collapses into **one** dedup row (I measured 6 unrelated shops
→ 1 row); and a national trunk zero written *after* an explicit country code
(`"+961 01 234 567"`) is never stripped, producing a key that both **fails to match the
same business written in national format** and is written verbatim into
`all_businesses.csv` as an undialable 9-digit Lebanese number.

The rewrite in commits `3b95250`/`99493b9` genuinely fixed the four bugs earlier audits
flagged (junk collapsing to `"+"`, `00` IDD handling, cross-border prefix corruption,
`None`/non-string input). Those fixes hold under execution. What remains is that the
function still does **digit shaping**, not number parsing: it has no notion of a
numbering plan, no extension/multi-number handling, no Unicode-digit folding, and one
`lstrip("0")` whose empty-result case is unguarded. All 28 existing tests pass
(`python3 -m unittest discover -s tests -q`), so nothing here is a regression — these are
uncovered gaps.

Every claim below was produced by importing `dedup` and executing the function, not by
reading it. `python3 -c` heredocs against the working tree, no network, no source edits.

---

## Callers (the full blast radius)

| Caller | What it does with the return value | Consequence of a bad key |
|---|---|---|
| `dedup.py:205` | `key = normalize_phone(raw_phone, record.get("country") or "LB")`; non-empty keys index `phone_index` | bad key → **merge** (unrelated businesses fused) or **split** (one business twice) |
| `dedup.py:228` | re-normalizes name-indexed records and drops them if the key was already captured | see S3-7 — this branch is unreachable |
| `main.py:194` | `r["phone"] = normalize_phone(raw_phone, r.get("country") or _DEFAULT_COUNTRY)` | bad key is written into **all five CSVs**, including `sales_ready.csv` |

Inputs arrive from three places with three different conventions:

- `scrapers/osm.py:89` — raw `phone` / `contact:phone` tag, **verbatim**. OSM's own wiki
  sanctions `;` between multiple numbers and four different extension syntaxes, and
  sanctions national-format toll-free numbers.
- `scrapers/wikidata.py:97` — P1329 with a `tel:` prefix stripped.
- `scrapers/google_places.py:289` — `nationalPhoneNumber`, i.e. the **national** form.

And `country` is not a trustworthy disambiguator: `scrapers/osm.py:103` and
`scrapers/wikidata.py:111` **hardcode `country="LB"`** for every row, and
`scrapers/google_places.py:127-136` derives it from the *query string*, not the result.

---

## Findings

### S1 — All-zero placeholder phones collapse every record onto the bare country code

- **Where:** `dedup.py:63` — `return "+" + cc + digits.lstrip("0")`
- **Breaks:** `"0000000"` passes the 7-digit gate at `dedup.py:45`, matches no entry in
  `_KNOWN_COUNTRY_CODES`, and `lstrip("0")` empties it. The result is `"+961"` — a
  **valid-looking, shared key**. Every zero-filled phone in the corpus therefore merges
  into a single business. This is the exact failure mode the docstring at `dedup.py:36-38`
  says it was written to prevent; it just moved from `"+"` to `"+961"`.
- **Trigger (executed, through `dedup()`, not just `normalize_phone`):**

  ```python
  recs = [rec(f"Shop {i}", "0000000") for i in range(6)]
  dedup(recs)   # -> 1 row, name='Shop 5'
  ```

  Observed: `input=6 output=1`. All six `source="osm"` records are destroyed into one.
  Mixed corpus:

  ```
  Zero One     phone='0000000'
  Zero Two     phone='00000000'
  Zero Three   phone='000 000 0000'
  Real Cafe    phone='70 123 456'   <- survived
  input=4 output=2
  ```

  Also affected, all confirmed: `"00000000"` → `"+961"`, `"000000000"` → `"+961"`,
  `"000 000 00"` → `"+961"`; with `country="SA"` each → `"+966"` (a second, independent
  collapse bucket).
- **Second-order damage — it is also the only non-idempotent input.** `main.py:194`
  writes `phone="+961"` into the cumulative master. On the *next* run `load_master`
  reads it back and `normalize_phone("+961", "LB")` returns `""` (3 digits < gate), so
  the record silently moves from the **phone key** to the **`(name, city)` key**
  (`dedup.py:211-218`) and can now merge with a completely different business.

  ```
  NOT IDEMPOTENT '0000000' LB: '+961' -> ''
  NOT IDEMPOTENT '00000000' SA: '+966' -> ''
  ```

  Every other input I probed is idempotent, including all the broken ones below — so
  this one bug *changes the merge topology between runs*.
- **Fix:** `stripped = digits.lstrip("0");  return "" if not stripped else "+" + cc + stripped` — one line at `dedup.py:63`.

---

### S1 — National trunk zero is never stripped after an explicit country code, so one business gets two keys and a broken CSV cell

- **Where:** `dedup.py:57-60` (the `_KNOWN_COUNTRY_CODES` early `return "+" + digits`)
  combined with `dedup.py:53` (`elif not had_plus and ...`). The trunk-zero rewrite is
  gated on `not had_plus`; the early return at `dedup.py:60` fires **before** the
  `lstrip("0")` fallback at `dedup.py:63` is ever reached.
- **Breaks:** neither Lebanon nor Saudi Arabia uses a trunk prefix in international
  format (unlike Italy/UK). `+961 01 234 567` and `+961 1 234 567` are the same number;
  the function keys them as two different businesses. It is the *only* asymmetry in the
  function: the identical digit string with the `+` removed normalizes correctly.
- **Trigger (executed):**

  | input | country | output |
  |---|---|---|
  | `"01 234 567"` | `LB` | `"+9611234567"` ✅ |
  | `"+961 01 234 567"` | `LB` | `"+96101234567"` ❌ |
  | `"+9611 234 567"` | `LB` | `"+9611234567"` ✅ |
  | `"0501234567"` | `SA` | `"+966501234567"` ✅ |
  | `"+966 05 0123 4567"` | `SA` | `"+9660501234567"` ❌ |
  | `"011 234 5678"` | `SA` | `"+966112345678"` ✅ |
  | `"+966 011 234 5678"` | `SA` | `"+9660112345678"` ❌ |

  End-to-end through `dedup()` — one cafe, two rows, output row is the OSM one only, so
  the Google email/social data is **discarded, not merged**:

  ```
  input=2 output=2
  Cafe Hamra phone='01 234 567'      source='osm'
  Cafe Hamra phone='+961 01 234 567'  source='google_places'
  ```

  Saudi equivalent: `Riyadh Auto` → `input=2 output=2`. This is the failure the
  existing test at `tests/test_lead_signal.py:117-118` (`"0501234567","SA"`,
  `"70123456","LB"`) does **not** cover — it only tests the `+`-less form.
- **Second-order:** `main.py:194` writes the broken value into the deliverable.
  `+96101234567` is 9 national digits with a trunk zero; nobody can dial it. It then
  round-trips through `data/all_businesses.csv` and **stays** broken (idempotent), so
  the poison is permanent in the cumulative master.
- **Fix:** before the `_KNOWN_COUNTRY_CODES` loop, `digits = digits[len(known):].lstrip("0")` when the matched prefix is followed by a trunk zero — i.e. drop the `not had_plus` gate at `dedup.py:53` and apply `lstrip("0")` to the number after any matched country code.

---

### S2 — Multiple numbers in one field are glued into a single super-long "phone"

- **Where:** `dedup.py:44` — `digits = re.sub(r"\D", "", raw)`
- **Breaks:** OSM explicitly sanctions `phone=number;number` ("In case of multiple
  phone numbers, use `phone=number;number`"; "The character `;` can be used to separate
  multiple phone numbers" — <https://wiki.openstreetmap.org/wiki/Key:phone>).
  `scrapers/osm.py:89` passes the tag through untouched. `re.sub(r"\D", ...)` deletes
  the separator and concatenates. The result is not a phone number, and it passes the
  length gate purely because it is long.
- **Trigger (executed):**

  | input | output | length |
  |---|---|---|
  | `"+9611234567;+96137654321"` | `"+961123456796137654321"` | 21 chars |
  | `"+961 1 234567;+961 3 765432"` | `"+96112345679613765432"` | 21 |
  | `"+966 50 1234567;011 234 5678"` | `"+9665012345670112345678"` | 23 |
  | `"+961 1 234567 / +961 3 765432"` | `"+96112345679613765432"` | 21 |
  | `"01 234567, 03 765432"` | `"+961123456703765432"` | 18 |
  | `"961-1-234567/961-3-765432"` | `"+96112345679613765432"` | 21 |

  With a three-number tag: `normalize_phone("+961 1 234567 / +961 3 765432 / +961 70 123456","LB")`
  → `'+9611234567961376543296170123456'` (**32 chars**). That string goes straight into
  `sales_ready.csv` as the call-to-action number.
- **Aggravating:** the row also **fails to merge** with its own single-number sibling:

  ```python
  dedup([rec("Multi","+9611234567;+96137654321"), rec("Single","+9611234567")])
  # input=2 output=2
  ```
- **Fix:** split on `[;/|,] or \s+or\b` before normalizing, return the first E.164 candidate, and keep the rest in a `phone_alt` list — or at minimum `re.search(r"\+?\d[\d\s().-]{6,}", raw)` then take the first match.

---

### S2 — Extensions are concatenated into the subscriber number, and the existing junk test gives false assurance

- **Where:** `dedup.py:44`
- **Breaks:** OSM documents four extension syntaxes (`x`, ` ext. N`, ` ext N`, `-N` DIN
  5008). All become extra subscriber digits, producing a number that is wrong to dial
  and that keys differently from the base number it belongs to.
- **Trigger (executed):**

  | input | output | should be |
  |---|---|---|
  | `"03 123 456 ext. 9"` | `"+96131234569"` | `+9613123456` + ext 9 |
  | `"+961 70 123 456 ext 9"` | `"+961701234569"` | `+96170123456` + ext 9 |
  | `"70 123 456 x9"` | `"+961701234569"` | same |
  | `"+961 1 234567 ext. 4309"` | `"+96112345674309"` | `+9611234567` + ext 4309 |
  | `"+961 1 234567-1234"` | `"+96112345671234"` | `+9611234567` + ext 1234 |
  | `"+961 3 123 456 #45"` | `"+961312345645"` | `+9613123456` + ext 45 |
  | `"050-123-4567 x123"`, `SA` | `"+966501234567123"` | `+966501234567` + ext 123 |

  The last one is also a **split**: `normalize_phone("050-123-4567","SA")` and
  `normalize_phone("050-123-4567 x123","SA")` are different keys for one business.
- **The test is misleading.** `tests/test_lead_signal.py:106` asserts that
  `normalize_phone("ext 4", "LB")` is falsy — it passes only because `"ext 4"` has one
  digit and trips the gate at `dedup.py:45`. An extension *on a real number* sails
  through. This test gives the impression extensions are handled.
- **Fix:** strip `(?:\s*(?:ext\.?|x|#)\s*\d+\s*)$` from `raw` before `dedup.py:44`, and store the captured extension separately.

---

### S2 — Arabic-Indic digits are not folded to ASCII, so the same business splits and non-ASCII reaches the CSV

- **Where:** `dedup.py:44`. Python's `\D` is Unicode-aware, so `٠١٢٣٤٥٦` (Arabic-Indic,
  U+0660-0669) and `۱۲۳۴۵۶۷` (Extended Arabic-Indic, U+06F0-06F9) are *kept as digits*.
- **Breaks:** they survive into the dedup key and into the CSV. Two records for one
  business, one keyed in Latin digits and one in Arabic-Indic, never match.
- **Trigger (executed):**

  ```python
  normalize_phone("٧٠١٢٣٤٥٦", "LB")            # '+961٧٠١٢٣٤٥٦'
  normalize_phone("٧٠١٢٣٤٥٦", "LB")            # '+961٧٠١٢٣٤٥٦'
  normalize_phone("70123456",  "LB")           # '+96170123456'   <- different key
  ```

  Through `dedup()`:

  ```
  input=2 output=2
  Bakery phone='70 123 456'  source='google_places'
  Bakery phone='٧٠١٢٣٤٥٦'     source='osm'
  ```

  Mixed-script input also survives: `"١۰۱٢٣٤٥٦٧"` → `"+961١۰۱٢٣٤٥٦٧"`. Neither
  `main.py:138` (`has_any_contact`) nor `enricher.py:285` folds digits either, so these
  pass as "has phone" downstream. Lebanon is a realistic source for this — `phone`/`name`
  in OSM are frequently entered in Arabic-Indic numerals.
- **Fix:** `raw = unicodedata.normalize("NFKC", str(phone))` before `dedup.py:44` (NFKC maps U+0660-0669 and U+06F0-06F9 to 0-9); `unicodedata` is already imported at `dedup.py:2`.

---

### S2 — An unrecognised or malformed `country` silently becomes Lebanon, rewriting foreign numbers as Lebanese

- **Where:** `dedup.py:48` — `cc = _COUNTRY_CODES.get(country, "961")`
- **Breaks:** every key other than the two exact strings `"LB"` / `"SA"` falls back to
  Lebanon with no signal. A Saudi mobile becomes a 12-digit "Lebanese" number.
- **Trigger (executed), all identical bad output `"+961501234567"`:**

  | `country` | output |
  |---|---|
  | `"SA "` (trailing space) | `"+961501234567"` |
  | `"sa"` | `"+961501234567"` |
  | `"SAU"` / `"KSA"` / `"Saudi Arabia"` | `"+961501234567"` |
  | `"Lebanon"` (the actual word) | `"+961501234567"` |
  | `None` / `"XX"` | `"+961501234567"` |

  `main.resolve_country` (`main.py:50-74`) *does* handle `"sa"`, `"LEB"`, `"KSA"`,
  `"SAU"`, `"LB"`, `"SA"` — but it runs at `main.py:184`, **after** `dedup()` at
  `main.py:180`. Both `normalize_phone` callers inside `dedup` (`dedup.py:205`,
  `dedup.py:228`) use the raw `record.get("country")` with no alias handling, so the
  normalization that builds the dedup key is the *un-normalized* one.
- **Reachability today:** low but nonzero. All three scrapers emit `"LB"`/`"SA"`
  (`scrapers/osm.py:103`, `scrapers/wikidata.py:111`, `scrapers/google_places.py:285`),
  but `load_master` reads `country` straight from the CSV (`main.py:86-104`) with **no
  validation**, so any hand-edited or externally-sourced master row flows straight in.
  And `main.py:194` will then write the mangled `"+961501234567"` into the deliverable.
- **Fix:** `cc = _COUNTRY_CODES.get(str(country or "").strip().upper(), "")` and fall back to the number's own prefix or return `""` instead of assuming Lebanon.

---

### S3 — `_KNOWN_COUNTRY_CODES` matches bare prefixes with no `+` requirement and no length plan check

- **Where:** `dedup.py:58-60`, list at `dedup.py:8-21`
- **Breaks:** `digits.startswith(known)` matches a **national-format** number whose first
  two digits happen to equal a country code. The shortest entry, `"20"` (Egypt,
  `dedup.py:20`), is the most collision-prone.
- **Trigger (executed):** `normalize_phone("2012345", "LB")` → `"+2012345"` — a Lebanese
  record relabelled Egypt. `normalize_phone("201234567","LB")` → `"+201234567"`.
  Because the branch ignores `country`, `normalize_phone("201234567","LB")` and
  `normalize_phone("201234567","SA")` return the **same key**, so two records claiming
  different countries merge.
- **Honest caveat:** neither the Lebanese nor the Saudi numbering plan uses a `20`
  prefix (LB: fixed line `1…`, mobile `70-99`; SA: mobile `5…`, landline `1…`), so this
  **cannot fire on the current corpus**. It is latent, and it becomes live the moment a
  third country is scraped. The related live failure is the missing `+972` (Israel),
  extremely common for Lebanese businesses:
  `normalize_phone("+972 50 123 4567", "LB")` → `"+961972501234567"`.
- **Fix:** require `had_plus or digits.startswith(known) and remaining length matches that country's NSN length` — match the prefix only when `had_plus` is true or the remaining digits are too long to be a bare LB/SA national number.

---

### S3 — The `_MIN_DIGITS` gate runs before the `00` strip, so 5-digit "numbers" survive it

- **Where:** `dedup.py:45-46` (gate) vs `dedup.py:50-52` (`00` strip). The gate counts
  the IDD prefix as if it were subscriber digits.
- **Breaks:** the exact protection `_MIN_DIGITS` was added for (`dedup.py:28-29`) is
  bypassed by prepending `00`.
- **Trigger (executed):** `normalize_phone("0091234", "LB")` → `"+96191234"` (5 national
  digits). `normalize_phone("0012345","LB")` → `"+96112345"`. `normalize_phone("00123456","LB")`
  → `"+961123456"`. Meanwhile `normalize_phone("123456","LB")` → `""` (correctly
  rejected). The same input class is thus accepted or rejected based on a two-character
  prefix.
- **Fix:** move the `len(digits) < _MIN_DIGITS` check to after the `00`/`0`+cc normalisation.

---

### S3 — No E.164 length ceiling

- **Where:** `dedup.py:63` and `dedup.py:60`
- **Trigger (executed):** `normalize_phone("+961 70 123 4567 8901 2345","LB")` →
  `"+96170123456789012345"` (20 digits). `normalize_phone("100000000000000000000","LB")`
  → `"+961100000000000000000000"` (24 digits). E.164 caps at 15; nothing here enforces
  it, so the glued-multi-number case (S2) has no natural backstop.
- **Fix:** `if not 7 <= len(stripped) <= 15: return ""` after normalization.

---

### S3 — `str()` coercion turns numeric-looking non-strings into different numbers

- **Where:** `dedup.py:42` — `raw = str(phone)`
- **Breaks:** the coercion (a good defensive fix for the `None`/int crash raised in
  audit 020) stringifies *any* type, including floats, whose repr injects a digit.
- **Trigger (executed):** `normalize_phone(7012345.0, "LB")` → `"+96170123450"`;
  `normalize_phone(7012345.5, "LB")` → `"+96170123455"`. A float phone silently becomes
  a **different, dialable-looking** number. `normalize_phone(True,"LB")` → `""` (fine).
- **Reachability:** low — no scraper emits a float phone. Worth a one-line guard rather
  than a redesign.
- **Fix:** `if not isinstance(phone, str): return ""` before `dedup.py:42`.

---

### S3 — Two branches are unreachable dead code

- **`dedup.py:53-55` (`elif not had_plus and digits.startswith("0" + cc)`)** is
  effectively dead for the only two codes in `_COUNTRY_CODES`. It fires only when a
  number starts `0` + its *own* country code — i.e. `0961…` for LB or `0966…` for SA.
  Neither country has such a national prefix. It does handle the human typo correctly
  (`normalize_phone("0961 1 234567","LB")` → `"+9611234567"` ✅), so keep it, but note
  that the *harmful* case it was written for is the same case the `+`-form already
  mishandles (S1-2): `normalize_phone("0961 1 234567","SA")` → `"+9669611234567"`,
  a Lebanese number relabelled Saudi.
- **`dedup.py:228`** can never fire. Proof: a record enters `name_index` only when
  `key` at `dedup.py:205` is falsy, i.e. `raw` is falsy **or** `normalize_phone(raw,…)`
  returned `""`. Line 228 requires `raw` truthy **and** `normalize_phone(raw,…)` in
  `captured_phones`. Every key in `captured_phones` is non-empty (guarded by `if key:`
  at `dedup.py:206`), and the only value `normalize_phone` could return here is the `""`
  it already returned at line 205. So `"" in captured_phones` is always `False`.
  Confirmed: `dedup([rec("Cafe","70 123 456"), rec("Cafe","0000000")])` → 2 rows; the
  guard never suppresses the second. Harmless today, but it reads as a safety net that
  does not exist.
- **Fix:** delete `dedup.py:226-230`'s guard, or replace the whole name-index filter with an explicit two-phase pass once `normalize_phone` stops being non-deterministic.

---

## Not a bug, but worth knowing

**What the function gets right (verified, and it is load-bearing):**

1. **Junk never yields a shared key.** `"---"`, `"..."`, `"  "`, `"n/a"`, `"+"`,
   `"ext 4"`, `"0"`, `"000"` all → `""` (the gate at `dedup.py:45`). This is the
   invariant that makes `dedup.py:211-218`'s fallthrough-to-name-key safe. Confirmed.
2. **Cross-border numbers are trusted over the `country` argument.** All of
   `("+966501234567","LB")`, `("966501234567","LB")`, `("00966501234567","LB")` →
   `"+966501234567"`; and `("+9613123456","SA")`, `("9613123456","SA")`,
   `("00961370123456","SA")` → `"+9613123456"`. This closes the audit-084 bug and is
   exactly what the module comment at `dedup.py:5-7` promises.
3. **`_MIN_DIGITS = 7` is correctly calibrated for LB and SA.** A Beirut landline's
   shortest national form `1234567` → `"+9611234567"` (passes); a 6-digit `123456` →
   `""` (correctly rejected); Saudi `501234567` → `"+966501234567"`; Saudi landline
   `11234567` → `"+96611234567"`. The gate is right; only its **position** is wrong
   (S3-3).
4. **Idempotent for every input except the all-zero class.** I probed 14 representative
   inputs including all of the broken ones above; the only violations were
   `'0000000'`, `'00000000'`, `'00 00 00 00'` (S1-1). So the double normalization at
   `main.py:194` is redundant but harmless outside that one case — and
   `dedup.py:205`/`dedup.py:228`/`main.py:194` agreeing is the only reason key
   construction and the written column currently stay in sync.
5. **Saudi national formats normalize correctly** because the `lstrip("0")` fallback at
   `dedup.py:63` handles the trunk zero when no country code is present:
   `"0501234567"` → `"+966501234567"`, `"50 123 4567"` → `"+966501234567"`,
   `"0112345678"` → `"+966112345678"`.
6. **`country` is reliably `"LB"`/`"SA"` from the scrapers**, so the S2-5 silent-Lebanon
   fallback is not on the hot path *today*. It becomes live the moment a master CSV is
   hand-edited or a third country is scraped.
7. **A same-business collision across countries does *not* happen** for the common
   case: `normalize_phone("0123456","LB")` → `"+961123456"` vs
   `normalize_phone("0123456","SA")` → `"+966123456"` — `dedup()` correctly keeps them
   as 2 rows. The country code is genuinely part of the key, which is correct.

**Test gaps that let all of the above through** (28 tests, all passing):

- `tests/test_lead_signal.py:116-118` only asserts the `+`-less national forms, so the
  `+`-with-trunk-zero S1 is invisible.
- `tests/test_lead_signal.py:120-123` asserts `+962…` round-trips, but the assertion is
  written as `normalize_phone(...).replace("2","2")` — a **no-op** that makes the test
  pass for any string of the right length. It currently cannot fail.
- No test feeds `"0000000"`, a `;`-joined pair, an extension on a real number, or
  Arabic-Indic digits.

---

## Recommended order of work

1. **`dedup.py:63`** — guard `digits.lstrip("0")` being empty. One line, kills the only
   non-idempotent input and the only mass-merge. Do this first.
2. **`dedup.py:57-60`** — strip the national trunk zero after a matched country code.
   Removes the `+`/no-`+` asymmetry that splits one business into two rows and poisons
   the CSV.
3. **`dedup.py:42`** — `unicodedata.normalize("NFKC", str(phone))`, plus an
   `isinstance(phone, str)` guard. Two lines, kills S2-3 and S3-7.
4. **`dedup.py:44`** — strip extensions, then split on `[;/|]` and normalize each part;
   keep the extras in a `phone_alt` field. Kills S2-2 and S2-3 and stops 32-character
   strings reaching `sales_ready.csv`.
5. **`dedup.py:45`** — move the length gate after prefix stripping, and add a 15-digit
   E.164 ceiling. Kills S3-3 and S3-4.
6. **`dedup.py:48`** — stop silently defaulting `country` to Lebanon; return `""` for an
   unknown code so the caller falls back to the name key instead of writing a
   mislabelled number.
7. **`dedup.py:205` / `dedup.py:228`** — call `resolve_country` (or a shared
   country-code normalizer) before keying, or delete the unreachable guard at
   `dedup.py:228`.
8. **Tests** — add a table-driven `normalize_phone` matrix covering: all-zero, `+`-with-
   trunk-zero for both countries, `;`/`/`/` or ` multi-number, all four OSM extension
   syntaxes, Arabic-Indic digits, national-format toll-free, and an idempotency
   property (`normalize_phone(normalize_phone(x)) == normalize_phone(x)`) over the
   whole matrix. Fix the no-op `.replace("2","2")` at `tests/test_lead_signal.py:122`
   while you are there.