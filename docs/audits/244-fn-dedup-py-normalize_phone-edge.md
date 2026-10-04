# 200-FN — `normalize_phone` (`dedup.py:33`): hostile-input edge enumeration

## Verdict

`normalize_phone` raises **no** traceback for any realistic input. Its entire failure
surface is the other category: it returns a **truthy, wrong string** for junk that a
length-only guard (`dedup.py:45`, `_MIN_DIGITS` at `dedup.py:30`) waves through. Two
inputs are genuinely dangerous: an **all-zero digit string of length ≥ 7** returns the
bare string `"+961"` / `"+966"` — a direct regression of the exact bug the docstring at
`dedup.py:36-38` says it fixed — which **irreversibly merges unrelated businesses into one
row**; and a national **trunk zero after an explicit country code** (`+966 05 012 34567`)
returns `+9660501234567`, splitting one business into two dedup keys and publishing an
undialable phone into all five CSVs via the write-back at `main.py:194`.

## The contract, restated

What `normalize_phone` promises, in its own words:

| Promise | Anchor |
|---|---|
| "Normalize to E.164, or return `""` if the input is not a usable number" | `dedup.py:34` |
| "Returns `""` rather than a placeholder like `"+"` so that callers can test the result for truthiness" | `dedup.py:36-37` |
| "Junk used to normalize to `"+"`, which made unrelated businesses share one dedup key" | `dedup.py:37-38` |
| "Anything shorter than this is punctuation, a shortcode or an extension fragment, not a dialable number. **Such input must never become a dedup key.**" | `dedup.py:28-29` |
| Declared type: `phone: str, country: str = "LB"` | `dedup.py:33` |

The two callers turn truthiness into control flow, so "returns a truthy string" *is* the
entire safety contract:

- `dedup.py:205-206` — `if key:` decides phone-index vs. name-index.
- `main.py:193-194` — `if raw_phone:` then `r["phone"] = normalize_phone(...)` writes the
  returned string into the record, which becomes the `phone` column of all five CSVs
  (`main.py:209-213`) and the cumulative master.
- `pitch_recommender.py:38` — `has_phone = bool(record.get("phone"))` promotes a junk
  phone into a pitching signal.
- `main.py:70-73` — `resolve_country` reads the raw phone back, so anything already
  written is self-perpetuating.

**The guard is a digit-count test, not a validity test.** Everything below follows from
that one design decision.

## Exhaustive hostile-input classification

Every row was executed against the current `dedup.py`. Classification is
**CRASH** (traceback) / **WRONG** (silently wrong value) / **OK** (correctly handled).

### Category 1 — Absent, empty, whitespace, zero

| Input | `country` | Result | Class |
|---|---|---|---|
| `None` | `"LB"` | `""` | OK |
| `""` | `"LB"` | `""` | OK |
| `"   "` | `"LB"` | `""` | OK |
| `"\xa0\u200f\u200e٠"` (NBSP, RLM, LRM, Arabic-Indic zero) | `"LB"` | `""` | OK |
| `0` (int) | `"LB"` | `""` | OK |
| `"0"` | `"LB"` | `""` | OK (via `dedup.py:45`) |
| `True` (bool) | `"LB"` | `""` | OK |
| `float("nan")` / `float("inf")` / `Decimal("NaN")` | `"LB"` | `""` | OK |
| **`"0000000"`** | `"LB"` | **`"+961"`** | **WRONG** |
| **`"0000000000"`** | `"LB"` | **`"+961"`** | **WRONG** |
| **`"0000000000"`** | `"SA"` | **`"+966"`** | **WRONG** |
| **`"000 000 0000"`** | `"LB"` | **`"+961"`** | **WRONG** |
| **`"---000000000"`** | `"LB"` | **`"+961"`** | **WRONG** |

`None`/`""`/whitespace are caught by `dedup.py:40-41`. The all-zero family passes the
count gate and dies at `dedup.py:63`.

### Category 2 — Wrong type (violates the declared `str` contract at `dedup.py:33`)

| Input | Result | Class |
|---|---|---|
| `9611234567.0` (float) | `"+96112345670"` | **WRONG** — spurious trailing `0` |
| `501234567.0`, `"SA"` | `"+9665012345670"` | **WRONG** — spurious trailing `0` |
| `501234567` (int), `"SA"` | `"+966501234567"` | OK (int repr has no `.0`) |
| `-9611234567` (negative int) | `"+9611234567"` | WRONG — a negative int is accepted as a dialable number |
| `b"+96170123456"` (bytes) | `"+96170123456"` | OK (lucky: `str(bytes)` keeps the digits) |
| `"٩٦١".encode()` (arabic bytes) | `""` | OK by accident |
| `["+96170123456"]` (list) | `"+96170123456"` | **WRONG** — a list becomes a phone key |
| `{"phone": "70123456"}` (dict) | `"+96170123456"` | **WRONG** — a dict becomes a phone key |
| `[["96170123456"]]` | `"+96170123456"` | **WRONG** |
| `(c for c in "70123456")` (generator) | `"+961109190"` / `"+96110516190"` / `"+961100402190"` across three processes | **WRONG — key is a function of heap address, not of the phone** |
| `"70\x0012\x0034\x0056"` (NUL bytes) | `"+96170123456"` | OK |
| `"+96170123456\ud800"` (unpaired surrogate) | `"+96170123456"` | OK |
| `"9" * 2_000_000` | 1,000,003-char truthy key, 47 ms | **WRONG** (see Category 6) |

The generator case is the sharpest: `str(phone)` at `dedup.py:42` stringifies the
*object's repr*, and `<generator object <genexpr> at 0x109ade190>` contributes the digits
`109190`. Three separate processes produced three different keys. The same record would key
differently in `dedup.py:205` and `dedup.py:228` within one run, and `main.py:194` would
publish a value that changes on every run — breaking the cumulative master.

### Category 3 — Non-Latin digits (Arabic, Persian, Devanagari, Fullwidth)

`\D` at `dedup.py:44` is Unicode-aware, so `\D` *preserves* every Unicode decimal digit.

| Input | Result | Class |
|---|---|---|
| `"٩٦١١٢٣٤٥٦٧"` (U+0669 … Arabic-Indic, `country="LB"`) | `"+961٩٦١١٢٣٤٥٦٧"` | **WRONG** — mixed-script key |
| `"٩٦١١٢٣٤٥٦٧"` prefixed with a real `+961` | `"+961٩٦١١٢٣٤٥٦٧"` | **WRONG** |
| `"٠٩٦١١٢٣٤٥٦"` (Arabic-Indic national) | `"+961٠٩٦١١٢٣٤٥٦"` | **WRONG** |
| `"９６１１２３４５６"` (U+FF19 … Fullwidth) | `"+961９６１１２３４５６"` | **WRONG** |
| `"۹"` U+06F9 (Extended Arabic-Indic), `"०"` U+0966 (Devanagari), `"१"` U+0969 | survive `\D` | **WRONG** — same class |
| `"٧٠١٢٣٤٥٦"` — Arabic-Indic valid Lebanese mobile | `"+961٧٠١٢٣٤٥٦"` | **WRONG** |

`startswith("961")` at `dedup.py:59` compares against ASCII, so it can never match
`٩٦١`. The loop falls through to `dedup.py:63` and prepends `+961` to a body that already
is a Lebanese number.

### Category 4 — Unicode that is genuinely not a digit (correctly rejected)

| Input | Result | Class |
|---|---|---|
| `"①②③④⑤⑥⑦"` (U+2460, category `No`) | `""` | OK |
| `"⁹²³⁰⁵¹⁸"` (superscripts, category `No`) | `""` | OK |
| `"📞📱"`, `"📞|📞|"` (U+1F4DE, U+1F4F1) | `""` | OK |
| `"📞 +96170123456"` | `"+96170123456"` | OK |
| `"+961\u202f70\u202f123\u202f456"` (French narrow no-break space U+202F) | `"+96170123456"` | **OK — French typography handled** |
| `"+961 70 123 456"`, `"+961-70-123-456"`, `"+961 (70) 123-456"` | `"+96170123456"` | OK |
| `"1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣"` (7 keycap emoji, `U+0031 U+FE0F U+20E3` each) | **`"+9611234567"`** | **WRONG** — 7 code-point sequences that *are* `\d` |

The keycap case proves the guard is counting code points that merely look like digits:
a string of 7 emoji becomes a plausible-looking Lebanese number.

### Category 5 — Zero, negative, and country-code edge values

| Input | `country` | Result | Class |
|---|---|---|---|
| `"+966501234567"` | `"LB"` | `"+966501234567"` | OK — `dedup.py:57-60` override is intentional |
| `"+962 51 234 5678"` | `"LB"` | `"+962512345678"` | OK |
| `"+201012345678"` (Egypt) | `"SA"` | `"+201012345678"` | OK |
| `"0501234567"` (KSA national) | `"SA"` | `"+966501234567"` | OK |
| `"0501234567"` | `"LB"` / `None` / `"sa"` / `"KSA"` / `"Saudi Arabia"` / `""` / `0` | `"+961501234567"` for all 7 | **WRONG** — `dedup.py:48` |
| `"+1 212 555 1234"` (US) | `"LB"` | `"+96112125551234"` | **WRONG** — 14-digit non-existent Lebanese number |
| `"+33142685300"` (France) | `"LB"` | `"+96133142685300"` | **WRONG** |
| `"+902123456789"` (Turkey) | `"LB"` | `"+961902123456789"` | **WRONG** |
| `"+8613800138000"` (China) | `"LB"` | `"+9618613800138000"` | **WRONG** — 16 digits, undialable |
| `"05012345670000"` | `"SA"` | `"+9665012345670000"` | **WRONG** — 16 digits |
| `"00 0 961 1 234 567"` | `"LB"` | `"+9619611234567"` | **WRONG** — cc duplicated by `dedup.py:55` |
| `"0009612345"` | `"LB"` | `"+9619612345"` | **WRONG** — cc duplicated |
| `"961+70 123 456"` | `"LB"` | `"+96170123456"` | OK, but see S3-1 on `had_plus` |
| `"not+a+number 70123456"` | `"LB"` | `"+96170123456"` | OK, but see S3-1 |

### Category 6 — Strings far longer than any real record

| Input | Result | Class |
|---|---|---|
| `"9" * 1000` | 1003-char truthy key | **WRONG** |
| `"9" * 1024 * 1024` (1 MiB) | 1 MiB truthy key, returned in full | **WRONG** |
| `"9617012345" * 1_000_000` (10 MiB) | no crash, full string returned | **WRONG** |

No crash, no ReDoS (`re.sub(r"\D", …)` is a single character class: 2,000,001 chars in
47 ms, linear). But there is **no ceiling**: E.164 caps at 15 digits total including the
country code, so anything over a 12-digit national body is illegal. `dedup.py:45` checks
only a floor.

### Category 7 — Trunk zero (the highest-likelihood wrong answer)

| Spelling of one Saudi number, `country="SA"` | Result |
|---|---|
| `"0501234567"` | `"+966501234567"` |
| `"05 012 34567"` | `"+966501234567"` |
| `"00966501234567"` | `"+966501234567"` |
| `"+9660501234567"` | **`"+9660501234567"`** |
| `"+966 05 012 34567"` | **`"+9660501234567"`** |
| `"009660501234567"` | **`"+9660501234567"`** |

Two keys for one number. Symmetrically for Beirut, `country="LB"`:
`"01 234 567"` → `"+9611234567"` and `"+9611234567"` → `"+9611234567"`, but
`"+961 01 234 567"` and `"0096101234567"` → **`"+96101234567"`**.

`country="LB"` mobile `"70123456"` is the one family that is fully consistent across all
four spellings — because a Lebanese mobile has no trunk zero.

## Findings

### S1 — All-zero digit strings return the bare country code, irreversibly merging businesses

- **Where:** `dedup.py:45` (the floor-only guard), `dedup.py:63`
  (`return "+" + cc + digits.lstrip("0")`), consumed by `dedup.py:206`.
- **Breaks:** `_MIN_DIGITS` counts digits; it never checks that any digit is nonzero. A
  string of ≥ 7 zeros survives `dedup.py:45`, and `lstrip("0")` at `dedup.py:63` removes
  **every** character, so the function returns `"+961"` — a truthy string that is nothing
  but the placeholder `"+"` the docstring at `dedup.py:37` claims to have eliminated. This
  is the same defect as `docs/audits/001-dedup-phone-normalization.md` S2
  ("`"---"`, `"+"`, and `"abc"` each become `+`"), which the rewrite fixed *only for
  sub-7-digit input*. It now wears a three-character costume.
  It therefore violates two invariants written four and eight lines above it:
  `dedup.py:29` ("Such input must never become a dedup key") and `dedup.py:36-38`.
  Every record whose phone normalizes to `"+961"` lands on `phone_index["+961"]` and is
  merged with every other one. `_merge` keeps only the fields the loser does not already
  supply, and `_pick` breaks a `name` tie on `str(value)` at `dedup.py:184`, so the
  alphabetically-later name survives and the other business **ceases to exist**. Since
  `main.py:209` writes this to `all_businesses.csv`, described in `main.py:8` as the
  "cumulative master (all time)", the loss is permanent and unrecoverable without
  re-scraping.
- **Trigger:**
  ```python
  from dedup import dedup
  rec = lambda n, p, c, city: {"name": n, "phone": p, "country": c,
                               "address": f"{n} St, {city}", "source": "osm",
                               "scraped_at": "2026-01-01", "category": "restaurant"}
  dedup([rec("Cafe A",    "0000000",      "LB", "Beirut"),
         rec("Pharmacy B", "0000000000",  "LB", "Tripoli"),
         rec("Salon C",   "0-0-0-0-0-0-0", "SA", "Riyadh")])
  # -> 2 rows. Cafe A and Pharmacy B collapsed into one; 'Cafe A' is gone.
  ```
  `osm.py:89` (`phone = tags.get("phone") or tags.get("contact:phone")`) applies no
  validation whatsoever, so any placeholder, bulk-entered, or masked OSM tag reaches this.
- **Fix:** reject a digit body with no nonzero digit before formatting, and bound the
  result — `if not digits.strip("0") or len(digits) < 7 or len(cc + digits) > 15: return ""`.

### S1 — A national trunk zero after an explicit country code gives one business two keys

- **Where:** `dedup.py:43` (`had_plus = "+" in raw`) gating `dedup.py:53-55`, and the
  early return at `dedup.py:58-60`.
- **Breaks:** the trunk prefix is only stripped in the `elif not had_plus` branch at
  `dedup.py:53`, i.e. **only when the string contains no `+` anywhere**. Once a country
  code is recognised at `dedup.py:59`, the function returns `+digits` immediately and
  never removes the national trunk zero that follows it. `+966 05 012 34567` and
  `"0501234567"` are the same number; they produce different keys, so `dedup` emits two
  rows. The second row's phone — `"+9660501234567"`, published by `main.py:194` — is not
  a dialable Saudi number, and `resolve_country` at `main.py:70-73` can never repair it
  because the string contains `"+966"` and looks canonical.
  This is the same defect as `docs/audits/001-dedup-phone-normalization.md` S1
  ("International numbers retaining a national trunk zero get a second key"), cited there
  against a prior revision of this file (`dedup.py:15-20` in that report). **The rewrite
  carried it forward.** It survives the codebase's own passing assertion at
  `tests/test_lead_signal.py:117`, which pins only the national form `"0501234567"` and
  never the complementary `+966 05 …` form.
- **Trigger:**
  ```python
  from dedup import dedup, normalize_phone
  normalize_phone("+966 05 012 34567", "SA")   # '+9660501234567'
  normalize_phone("0501234567",        "SA")   # '+966501234567'
  dedup([rec("Nader Kebab", "0501234567",     "SA"),
         rec("Nader Kebab", "+9660501234567", "SA")])   # -> 2 rows
  ```
  Reachability: OSM `phone` tags are human-entered and non-compliant forms are common
  (`osm.py:89`); and the cumulative master `main.py:150` loads whatever spelling previous
  runs stored, including any run predating the `main.py:194` write-back.
- **Fix:** after matching a known code at `dedup.py:59`, strip that country's national
  trunk prefix (`"0"` for LB/SA) before returning — or replace the string surgery with
  `libphonenumber` as proposed in `docs/audits/055-phone-validation-deep.md`.

### S2 — `\D` preserves non-ASCII decimal digits, so Arabic-Indic numbers never match and leak into the CSV

- **Where:** `dedup.py:44` (`re.sub(r"\D", "", raw)`), interacting with
  `dedup.py:59` (`startswith(known)`) and `dedup.py:63`.
- **Breaks:** Python's `\d`/`\D` are Unicode-aware by default, so Arabic-Indic (U+0660-0669),
  Extended Arabic-Indic (U+06F0-06F9), Devanagari (U+0966-096F) and Fullwidth (U+FF10-FF19)
  digits all survive the strip. `startswith("961")` then compares Arabic-Indic codepoints
  against ASCII literals and can never match, so the loop at `dedup.py:58-60` is bypassed
  and `dedup.py:63` prepends `+961` to a body that already *is* a Lebanese number. The
  result is a mixed-script key that matches nothing else, so the business is split across
  rows — and `main.py:194` publishes the mixed-script string into all five CSVs. This is
  not a theoretical locale: Arabic-Indic numerals are the native notation in both target
  markets, and `osm.py:89` reads contributor-typed tags with no normalization.
- **Trigger:** `normalize_phone("٧٠١٢٣٤٥٦", "LB")` → `"+961٧٠١٢٣٤٥٦"`;
  `dedup([rec("Cafe Y", "+96170123456", "LB"), rec("Cafe Y", "+961٧٠١٢٣٤٥٦", "LB")])`
  → 2 rows.
- **Fix:** `re.sub(r"[^0-9]", "", raw)`, or NFKC-normalize the string first (NFKC maps
  Fullwidth but **not** Arabic-Indic, so transliteration is still required). The identical
  `\D` expression is duplicated at `main.py:138` and `enricher.py:285` and needs the same
  change; extract one `_ascii_digits()` helper.

### S2 — `country` is silently coerced to Lebanon for every unrecognised value

- **Where:** `dedup.py:48` (`cc = _COUNTRY_CODES.get(country, "961")`).
- **Breaks:** `_COUNTRY_CODES` (`dedup.py:23-26`) has exactly two keys and `.get` has a
  silent `"961"` default. `normalize_phone` performs none of the normalisation that
  `main.py:59-67` does (`strip()`, `upper()`, `LEB`/`KSA` aliases), and `dedup` runs at
  `main.py:180` **before** `resolve_country` is applied at `main.py:183-184`. So `"sa"`,
  `"KSA"`, `"Saudi Arabia"`, `"lebanon"`, `""`, `None`, `0` all select Lebanon. A Saudi
  national number then becomes `+961501234567`, a 12-digit number that does not exist,
  which `main.py:194` writes to the master and which `resolve_country` at `main.py:72`
  can never flag again because it reads back `"+961"` and concludes Lebanon. **Verified:
  `resolve_country({"country": None, "phone": "+961501234567"})` → `"LB"`,** and likewise
  for `"+96112125551234"`, `"+96133142685300"`, `"+9618613800138000"`. The mislabel is
  permanent. Any repair must therefore include a migration pass over the existing
  `data/all_businesses.csv`, not just a code fix.
- **Trigger:** `normalize_phone("0501234567", "sa")` → `"+961501234567"` while
  `normalize_phone("0501234567", "SA")` → `"+966501234567"`.
- **Fix:** normalise `country` through `resolve_country`'s alias table before use, and
  raise (or return `""`) when the result is not in `_COUNTRY_CODES` instead of defaulting.

### S2 — `str(phone)` converts type violations into different numbers instead of rejecting them

- **Where:** `dedup.py:42` (`raw = str(phone)`), guarding a parameter declared `str` at
  `dedup.py:33`.
- **Breaks:** three distinct outcomes. (a) A **float** gains a spurious digit —
  `str(9611234567.0)` is `"9611234567.0"`, `\D` keeps the `0`, giving
  `"+96112345670"`, which does not match the string spelling `"+96170123456"`, so one
  business splits into two rows. (b) A **negative int** is accepted as dialable:
  `-9611234567` → `"+9611234567"`. (c) **Container and generator** types are stringified
  into a truthy key; a generator yields its heap address in the key
  (`"+961109190"` in one process, `"+96110516190"` in the next), making the key a
  function of allocation state rather than of the phone. `load_master`
  (`main.py:90-101`) casts only `_FLOAT_FIELDS` and `_INT_FIELDS`, which do not include
  `phone`, so a JSON number from `google_places.py:289` reaches here uncast.
- **Trigger:** `normalize_phone(9611234567.0, "LB")` → `"+96112345670"`;
  `dedup([rec("Cafe X", "+96170123456", "LB"), rec("Cafe X", 9611234567.0, "LB")])` → 2 rows.
- **Fix:** `if not isinstance(phone, str): return ""` (or `raise TypeError`) and delete the
  `str()` coercion; let the type error surface at the scraper boundary where it can be
  attributed.

### S2 — Numbers outside the 12-code list are force-labelled as national for the requested country

- **Where:** `dedup.py:62-63` (the `else` arm).
- **Breaks:** any number whose digits do not begin with one of `_KNOWN_COUNTRY_CODES`
  (`dedup.py:8-21`) is assumed to be a bare national number for `cc`, with no check that it
  is plausible for that plan and no length ceiling. A US number becomes
  `"+96112125551234"`, a French number `"+96133142685300"`, a Chinese number
  `"+9618613800138000"` (16 digits). `+20` is present in the list as a bare two-digit
  prefix (`dedup.py:20`) matched by `startswith` (`dedup.py:59`), so any digit string
  beginning `20` is labelled Egypt. Multinational hotels, embassies, consulates and
  diaspora businesses are exactly the accounts a B2B agency wants, and Wikidata
  `wdt:P1329` values are RFC3966 strings for *any* country (`wikidata.py:97`) while the
  record is hardcoded `country="LB"` (`wikidata.py:111`). The result is not only a wrong
  label: 14-16 digit strings are not dialable, so `main.py:194` publishes a phone the
  sales team cannot call, and `pitch_recommender.py:38` counts it as a contact.
- **Trigger:** `normalize_phone("+8613800138000", "LB")` → `"+9618613800138000"` (16
  digits, E.164 max is 15).
- **Fix:** validate the digit count against the plan (`7 ≤ len(digits) ≤ 12` for LB/SA)
  and return `""` — falling back to the name key at `dedup.py:212-214` — when it fails.

### S3 — `had_plus` is a substring test over the entire raw string

- **Where:** `dedup.py:43`.
- **Breaks:** `"+" in raw` is true for `"961+70 123 456"` and even for
  `"not+a+number 70123456"`, which therefore skips the trunk-prefix branch at
  `dedup.py:53` and takes the international path. Today both land on the same answer by
  luck; the test is semantically wrong and one edit away from mattering.
- **Fix:** test `raw.lstrip().startswith(("+", "00"))`.

### S3 — The `0 + cc` rebuild at `dedup.py:55` can duplicate the country code

- **Where:** `dedup.py:53-55`.
- **Breaks:** `digits = cc + digits[len(cc) + 1:]` prepends `cc` unconditionally. Junk
  that has already been stripped once re-enters with a leading `0`: `"00 0 961 1 234 567"`
  → `"+9619611234567"`, `"0009612345"` → `"+9619612345"`. Junk-in-junk-out, but it means a
  13-14 digit key is reachable from a plausible typo.
- **Fix:** after `digits[2:]` at `dedup.py:52`, re-run the `00` strip rather than assuming
  one prefix level.

### S3 — `_KNOWN_COUNTRY_CODES` order is an undeclared invariant

- **Where:** `dedup.py:8-21`, consumed by the `for known in …` loop at `dedup.py:58-60`.
- **Breaks:** the loop returns on first match, so `"212"`, `"216"`, `"218"` must precede
  `"20"` (they do, `dedup.py:17-20`). Nothing documents or tests this; re-sorting the
  tuple alphabetically would silently relabel Moroccan and Tunisian numbers as Egyptian.
- **Fix:** order the tuple longest-first and add a test asserting
  `normalize_phone("+212612345678", "LB")` starts with `+212`.

### S3 — `_MIN_DIGITS` is checked before the `00` strip, so it counts prefix digits

- **Where:** `dedup.py:45` precedes `dedup.py:50-52`.
- **Breaks:** `"00"` counts toward the 7-digit floor, so a 5-digit extension fragment
  dressed as `"00961 12345"` passes a gate intended to reject it. Same class as the
  all-zero defect: the gate measures the wrong thing.
- **Fix:** move the floor check after prefix removal.

### S3 — No upper bound on the digit body

- **Where:** `dedup.py:45` (floor only), `dedup.py:63`.
- **Breaks:** `"9" * 1_000_000` returns a 1,000,003-character truthy key, which is
  simultaneously a `phone_index` key, a `main.py:194` CSV cell, and a
  `pitch_recommender.py:38` "has phone" signal. No crash and no ReDoS (linear character
  class, 2,000,001 chars in 47 ms), so this is hygiene, not availability.
- **Fix:** cap at 15 total digits (see the S2 ceiling fix).

## Not a bug, but worth knowing

- **The correct-handling surface is genuinely good and should not be "fixed".** `None`,
  `""`, whitespace-only, `0`, `True`, `NaN`/`inf`, NUL bytes, unpaired surrogates, and all
  emoji, circled digits and superscripts correctly return `""` via the length gate at
  `dedup.py:45`. French narrow no-break space (U+202F), Arabic RTL marks, parentheses,
  dashes and slashes are all stripped correctly by `\D` — `"+961\u202f70\u202f123\u202f456"`
  → `"+96170123456"`. Cross-border recognition at `dedup.py:57-60` works as its comment at
  `dedup.py:5-7` intends: `"+966501234567"` under `country="LB"` correctly stays `+966`.
- **The existing regression test for exactly this bug class cannot fail.**
  `tests/test_lead_signal.py:103-108` asserts `normalize_phone(junk, "LB")` is falsy for
  `("---", "...", "  ", "n/a", "ext 4")` — every one of which is already rejected by the
  `len(digits) < 7` floor. It never tests a junk value that *passes* the floor. The full
  suite is green (28 tests, `python3 -m unittest tests.test_lead_signal`, all pass) while
  `normalize_phone("0000000", "LB") == "+961"` and
  `normalize_phone("٧٠١٢٣٤٥٦", "LB") == "+961٧٠١٢٣٤٥٦"` are both live. This is the
  successor of the gap `docs/audits/001-dedup-phone-normalization.md` S2 identified; the
  fix shipped with a test that exercises only the already-fixed branch.
- **The `+961` collision is a single-run event, but its damage is permanent.**
  `main.py:194` writes `"+961"` to the master; on the next run `normalize_phone("+961")`
  returns `""` (only 3 digits), so the collision does not propagate. The row that was
  merged away, however, is already gone from the cumulative master and cannot be recovered.
- **`dedup.py:227-229` is effectively dead code.** A record only reaches `name_index` when
  `normalize_phone` returned `""` at `dedup.py:205-206`, so re-normalizing it at
  `dedup.py:228` returns `""` again, and `""` is never a key in `phone_index` (guarded by
  `if key:`). The `continue` is unreachable. Not harmful, but it reads as a safety net
  that does not exist.
- **No crash surface exists inside the declared contract.** Exhaustive probing raised
  nothing. The only `TypeError` path is an **unhashable `country`** —
  `_COUNTRY_CODES.get(["SA"])` at `dedup.py:48` raises
  `TypeError: cannot use 'list' as a dict key` — which is outside the `country: str`
  contract but is a two-line guard (`isinstance(country, str)`) away from being a hard
  `TypeError` in `dedup()`. Same for `{"c": "SA"}` and `{"SA"}`.

## The single most dangerous input

**`"0000000000"` — ten zeros — under any `country`. Returns `"+961"`.**

Not because it is the most *malicious* input, but because it is the only one that
**destroys records rather than merely corrupting one**:

1. **It causes irreversible silent data loss.** Verified: three distinct businesses
   (`"Cafe A"` in Beirut, `"Pharmacy B"` in Tripoli, `"Salon C"` in Riyadh) collapse to two
   rows; `Cafe A` ceases to exist, and `main.py:209` has written that into
   `all_businesses.csv`, which `main.py:8` defines as the all-time cumulative master. Every
   other input in this report costs at most one row.
2. **It is a regression of the precise bug the function documents as fixed.** `dedup.py:36-38`
   states that junk "used to normalize to `+`, which made unrelated businesses share one
   dedup key." `"+961"` is `"+"` with two characters appended. Two invariants asserted in
   the file — `dedup.py:29` and `dedup.py:36-38` — are false.
3. **It defeats every downstream guard by construction.** The truthiness checks at
   `dedup.py:206` and `main.py:193` are the only defences, and `"+961"` is truthy. The
   length gate that was added to fix `001`'s S2 is a *floor*, so the value walks straight
   through it and then gets emptied by `lstrip("0")` at `dedup.py:63`, which is the only
   place in the function that can produce an output with no digits at all.
4. **The test that was written to prevent it tests the wrong inputs**
   (`tests/test_lead_signal.py:106`), which is why a green suite coexists with a live bug.

**Runner-up, and the one I would fix first on expected value: `"+966 05 012 34567"`.**
The all-zero string needs a pathological tag to appear; the trunk-zero form is an ordinary
human formatting habit, it is the only defect here the codebase's own test suite asserts
*around* without covering (`tests/test_lead_signal.py:117` pins `"0501234567"` but not its
`+966 05 …` twin), it was already reported against a prior revision of this file
(`docs/audits/001-dedup-phone-normalization.md` S1) and survived the rewrite, and unlike
the all-zero case it does not stop after one run — every future run of `main.py:180`
re-splits the pair. Fix both; fix the trunk zero first.

## Recommended order of work

1. **`dedup.py:45` + `dedup.py:63`** — reject an all-zero digit body and enforce the E.164
   ceiling (`7 ≤ len(cc + digits) ≤ 15`); return `""` so `dedup.py:212-214` falls back to
   the name key. Closes S1-1, S2-4 and S3-5 in three lines. Add
   `("0000000", "0000000000", "000 000 0000")` to the junk list at
   `tests/test_lead_signal.py:106`.
2. **`dedup.py:58-60`** — after a known code matches, strip that country's national trunk
   zero before returning. Add
   `assertEqual(normalize_phone("+966 05 012 34567", "SA"), "+966501234567")` next to
   `tests/test_lead_signal.py:117`.
3. **`dedup.py:44`** — `re.sub(r"[^0-9]", "", raw)` plus a transliteration step for
   Arabic-Indic; apply the same fix to the duplicated expressions at `main.py:138` and
   `enricher.py:285`, behind one shared `_ascii_digits()` helper.
4. **`dedup.py:48`** — route `country` through `resolve_country`'s alias table
   (`main.py:59-67`) and refuse to default to Lebanon. Ship a migration pass over
   `data/all_businesses.csv` for rows already poisoned with a `+961`-prefixed foreign
   number, since `resolve_country` cannot distinguish them afterwards.
5. **`dedup.py:42`** — replace `str(phone)` with an `isinstance(phone, str)` check that
   returns `""`, so floats, ints and containers cannot invent numbers.
6. **Reconsider string surgery entirely.** Every finding above is a symptom of one root
   cause: deriving a country code by `startswith` against a bare list of 12 prefixes, with
   no numbering plan, no `+` requirement and no length bound. `libphonenumber` provides
   validation, trunk-prefix removal, `possible_length` and mobile/landline typing for
   free, and `docs/audits/055-phone-validation-deep.md` already contains a truth table
   mapping current behaviour to the library's.

## Reproduce

Read-only; no network, no installs. Verified on Python 3.12 against the working tree.

```python
from dedup import normalize_phone, dedup

# S1-1  all zeros -> bare country code -> irreversible merge
normalize_phone("0000000000", "LB")            # '+961'
normalize_phone("0000000000", "SA")            # '+966'
normalize_phone("0000000000", "LB") == normalize_phone("0000000", "LB")   # True: shared key

# S1-2  trunk zero survives an explicit country code
normalize_phone("+966 05 012 34567", "SA")     # '+9660501234567'
normalize_phone("0501234567",         "SA")     # '+966501234567'
normalize_phone("+961 01 234 567",   "LB")     # '+96101234567'
normalize_phone("01 234 567",        "LB")     # '+9611234567'

# S2-1  Arabic-Indic digits survive \D and never match the ASCII spelling
normalize_phone("٧٠١٢٣٤٥٦", "LB")             # '+961٧٠١٢٣٤٥٦'
normalize_phone("９６１１２３４５６", "LB")     # '+961９６１１２３４５６'

# S2-2  country silently becomes Lebanon
for c in ("LB", None, "sa", "KSA", "Saudi Arabia", "", 0):
    normalize_phone("0501234567", c)           # all '+961501234567'

# S2-3  str() invents numbers
normalize_phone(9611234567.0, "LB")            # '+96112345670'
normalize_phone(-9611234567,   "LB")           # '+9611234567'
normalize_phone(["+96170123456"], "LB")        # '+96170123456'

# S2-4  foreign numbers relabelled national; no length ceiling
normalize_phone("+8613800138000", "LB")        # '+9618613800138000'  (16 digits)

# S3-1  had_plus is a substring test
normalize_phone("not+a+number 70123456", "LB") # '+96170123456'

# keycap emoji count as digits
normalize_phone("1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣", "LB")  # '+9611234567'

# the only crash in the whole surface, and it is out of contract
normalize_phone("70123456", ["SA"])
# TypeError: cannot use 'list' as a dict key (unhashable type: 'list')
```