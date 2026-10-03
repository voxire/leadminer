# 001 — Phone normalization and dedup keys

## Verdict

`normalize_phone()` is a digit-shaping heuristic, not a phone-number normalizer: it does not validate a country’s numbering plan, remove a national trunk zero after an international country code, or separate extensions. Those errors silently create wrong E.164-looking dedup keys, split the same business across records, and can merge unrelated data. The country argument is also a critical input: anything other than the exact keys `LB` and `SA` silently defaults to Lebanon.

## Findings

### S1 — Country context and foreign country codes are not checked
- **Where:** `dedup.py:5-8, 11-13, 19-25, 71`; `main.py:134-136`
- **Breaks:** The normalizer only recognizes the selected country’s calling code. A number beginning with the *other* supported country code is treated as national digits and prefixed with the selected country code. Missing, `None`, lowercase (`"sa"`), or other country values default to `LB`; `record.get("country", "LB")` does not replace a present `None`. Since the result is the primary dedup key, this can corrupt keys and merge otherwise distinct businesses. Correctly labeled `LB` and `SA` national numbers remain distinct; the failure is wrong/missing country metadata or a foreign-format number in a record.
- **Trigger:** `normalize_phone("+966 50 123 4567", "LB")` returns `+961966501234567`, not `+966501234567`. The reverse, `normalize_phone("+961 3 123 456", "SA")`, returns `+9669613123456`. `normalize_phone("0501234567", None)` returns `+961501234567`, while with `"SA"` it returns `+966501234567`. `"sa"` behaves like `None`.
- **Fix:** Require a recognized country, detect and preserve either supported international calling code independently of the default country, and reject/report country-number conflicts rather than inventing a new key.

### S1 — International numbers retaining a national trunk zero get a second key
- **Where:** `dedup.py:15-20`
- **Breaks:** International numbers are returned as soon as they start with the selected country code, before removing a national trunk prefix following that code. Lebanon `+961 0...` and Saudi Arabia `+966 0...` therefore remain malformed and do not match their national-format counterparts. `00`-prefixed versions hit the same branch after `00` is removed.
- **Trigger:** Lebanon `"01 234 567"` (national) becomes `+9611234567`, but `"+961 01 234 567"` and `"00961 01 234 567"` both become `+96101234567`. Saudi `"011 234 5678"` becomes `+966112345678`, but `"+966 011 234 5678"` and `"00966 011 234 5678"` become `+9660112345678`.
- **Fix:** After parsing the country calling code, normalize the national significant number using that country’s plan (remove its trunk prefix only where appropriate) before formatting.

### S1 — Extension digits are appended to the subscriber number
- **Where:** `dedup.py:12`
- **Breaks:** Removing every non-digit also removes extension markers (`ext`, `x`, `#`) but keeps the extension digits. The result is not the base telephone number; it will not match a record without the extension and may be mistaken for a different number. Whitespace, dashes, and parentheses alone are harmless, but the same blanket stripping is unsafe for extension syntax.
- **Trigger:** `normalize_phone("03 123 456 ext. 9", "LB")` returns `+96131234569`, instead of the base `+9613123456` plus a separately stored extension. `"050-123-4567 x123", "SA"` returns `+966501234567123`.
- **Fix:** Parse and remove a recognized extension before normalization; store it separately and use only the base number as the dedup key.

### S2 — No validity boundary: short codes and junk become phone keys
- **Where:** `dedup.py:12, 19-25`; `dedup.py:69-75`
- **Breaks:** The only length test is `len(digits) >= 7`; shorter digit strings are still returned with `+`, and nonempty punctuation-only/junk input becomes `+`. `dedup()` treats any truthy raw value as a phone and indexes these outputs, so unrelated records with no usable phone can collide on `+`, while service short codes are represented as fake international numbers. Longer invalid lengths and unassigned prefixes are also accepted without warning.
- **Trigger:** Lebanon inputs `"112"` and `"140"` become `+112` and `+140`; `"---"`, `"+"`, and `"abc"` each become `+`. Two records with any of those punctuation/junk values can share the same phone key.
- **Fix:** Return an explicit invalid/short-code result (not an E.164 key), validate lengths and country-specific numbering ranges, and let dedup fall back to a non-phone strategy when parsing fails.

## Truth table

Expected values below assume the `country` value shown is authoritative. `OK` means the emitted key matches the expected canonical base number; `WRONG` means it must not be used as that number’s canonical dedup key. Extension and short-code expectations are intentionally not E.164 numbers: extensions must be separate, and short codes must not be fabricated into global numbers.

| Input | country | Actual output (`dedup.py`) | Expected | Result |
|---|---|---|---|---|
| `03 123 456` | `LB` | `+9613123456` | `+9613123456` | OK — Lebanon mobile, national trunk removed |
| `03-(123)-456` | `LB` | `+9613123456` | `+9613123456` | OK — separators only |
| `+961 3 123 456` | `LB` | `+9613123456` | `+9613123456` | OK — international form |
| `00961 3 123 456` | `LB` | `+9613123456` | `+9613123456` | OK — `00` plus selected calling code |
| `961 3 123 456` | `LB` | `+9613123456` | `+9613123456` | OK — calling code without `+` |
| `70 123 456` | `LB` | `+96170123456` | `+96170123456` | OK — local mobile without trunk zero |
| `71 123 456` | `LB` | `+96171123456` | `+96171123456` | OK — local mobile without trunk zero |
| `01 234 567` | `LB` | `+9611234567` | `+9611234567` | OK — Beirut landline in national form |
| `04 123 456` | `LB` | `+9614123456` | `+9614123456` | OK — landline area prefix in national form |
| `+961 01 234 567` | `LB` | `+96101234567` | `+9611234567` | **WRONG** — trunk zero retained after country code |
| `00961 01 234 567` | `LB` | `+96101234567` | `+9611234567` | **WRONG** — same after `00` removal |
| `+961 03 123 456` | `LB` | `+96103123456` | `+9613123456` | **WRONG** — national mobile trunk retained |
| `050 123 4567` | `SA` | `+966501234567` | `+966501234567` | OK — KSA mobile, national trunk removed |
| `50 123 4567` | `SA` | `+966501234567` | `+966501234567` | OK — KSA mobile without trunk zero |
| `+966 50 123 4567` | `SA` | `+966501234567` | `+966501234567` | OK — international form |
| `00966 50 123 4567` | `SA` | `+966501234567` | `+966501234567` | OK — `00` plus selected calling code |
| `966 50 123 4567` | `SA` | `+966501234567` | `+966501234567` | OK — calling code without `+` |
| `011 234 5678` | `SA` | `+966112345678` | `+966112345678` | OK — Riyadh landline in national form |
| `012 234 5678` | `SA` | `+966122345678` | `+966122345678` | OK — Jeddah-area landline in national form |
| `+966 011 234 5678` | `SA` | `+9660112345678` | `+966112345678` | **WRONG** — trunk zero retained after country code |
| `00966 011 234 5678` | `SA` | `+9660112345678` | `+966112345678` | **WRONG** — same after `00` removal |
| `+966 050 123 4567` | `SA` | `+9660501234567` | `+966501234567` | **WRONG** — mobile trunk retained after country code |
| `800 123 456` | `SA` | `+966800123456` | `+966800123456` | OK — toll-free shape; no service/type validation |
| `0800 123 456` | `SA` | `+966800123456` | `+966800123456` | OK — national trunk removed for toll-free shape |
| `+966 800 123 456` | `SA` | `+966800123456` | `+966800123456` | OK — international toll-free shape |
| `112` | `LB` | `+112` | short code, not an E.164 key | **WRONG** |
| `140` | `LB` | `+140` | short code, not an E.164 key | **WRONG** |
| `911` | `SA` | `+911` | short code, not an E.164 key | **WRONG** |
| `03 123 456 ext. 9` | `LB` | `+96131234569` | base `+9613123456`, extension `9` separate | **WRONG** |
| `050-123-4567 x123` | `SA` | `+966501234567123` | base `+966501234567`, extension `123` separate | **WRONG** |
| `+966 50 123 4567` | `LB` | `+961966501234567` | `+966501234567` or reject country conflict | **WRONG** |
| `+961 3 123 456` | `SA` | `+9669613123456` | `+9613123456` or reject country conflict | **WRONG** |
| `0501234567` | `None` | `+961501234567` | Cannot infer country; reject/flag | **WRONG** |
| `0501234567` | `sa` | `+961501234567` | Cannot silently default; reject/flag | **WRONG** |
| `---` | `LB` | `+` | invalid/no phone key | **WRONG** |
| `abc` | `LB` | `+` | invalid/no phone key | **WRONG** |

## Complete branch behavior

The implementation has no further country-specific rules beyond these digit-string branches (`cc` is `961` for `LB`, `966` for `SA`; unknown country values use `LB`):

| Digits after deleting all `\D` | Rule taken | Output behavior |
|---|---|---|
| Starts with `00` + selected `cc` | `dedup.py:15-16` | Drop `00`, retain `cc`, then continue |
| Else starts with `0` + selected `cc` | `dedup.py:17-18` | Drop one leading `0`, retain `cc`, then continue |
| Starts with selected `cc` | `dedup.py:19-20` | Return `+` + digits unchanged (including any trunk `0`) |
| Else starts with `0` | `dedup.py:21-22` | Drop one zero and prepend selected `+cc` |
| Else has at least 7 digits | `dedup.py:23-24` | Prepend selected `+cc`, regardless of whether digits are foreign, valid, or too long |
| Else (0–6 digits) | `dedup.py:25` | Return `+` + digits, even for short codes or empty digits |

Therefore spaces/dashes/parentheses are harmless only when they are formatting and the digits already represent one valid base number. Every other digit-bearing suffix—including extensions—is retained. A `00` prefix is only recognized when followed by the selected calling code; for example `00966...` under `LB` falls through and is not preserved as a Saudi international number.

## Not a bug, but worth knowing

- For properly labeled national inputs, the usual Lebanese mobile (`03`, `70`, `71`, etc.) and landline (`01`, `04`, etc.) trunk-zero forms, and Saudi mobile (`05...`) and landline (`011...`, `012...`) trunk-zero forms, happen to normalize as expected. Saudi toll-free `800...` is also mechanically prefixed correctly in the example above. This is not proof of validity: the function does not classify or validate mobile, landline, toll-free, or assigned prefixes.
- The supported country labels are exactly `LB` and `SA` (`dedup.py:5-8`). Any other representation must be mapped before this function is called; silently treating it as Lebanon is not safe.
- `+` and common punctuation are stripped/rebuilt rather than preserved. This is okay for canonical formatting when the number is otherwise parsed correctly.

## Recommended order of work

1. Parse country and international calling code explicitly; reject unknown country metadata and preserve a recognized foreign country code rather than prepending another one.
2. Normalize the national significant number with country-specific trunk-prefix/length rules, covering national and international presentations as the same key.
3. Parse extensions and service short codes separately; do not index invalid or non-geographic values as E.164 phones.
4. Add table-driven tests for every truth-table row above, plus malformed lengths, both country-conflict directions, `00` with the other calling code, and punctuation/junk-only values.
