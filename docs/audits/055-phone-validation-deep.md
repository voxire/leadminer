# 055 — Replace string-surgery phone handling with libphonenumber end to end

## Verdict

`normalize_phone()` (`dedup.py:33`) is a digit-shaping heuristic, not a phone
validator: it has no knowledge of any country's numbering plan, so it emits
E.164-shaped keys for numbers that do not exist, and — critically — it never
classifies a number as *mobile* vs *landline*. That classification is the
gate for WhatsApp eligibility, and right now the pipeline cannot compute it.
Replace the heuristic with Google's `phonenumbers` library (`phonenumbers==9.0.30`,
pure-Python, metadata bundled, no runtime network) end to end: one parse, one
`is_valid_number` gate, E.164 formatting, and `number_type()` for the
mobile/landline split. The recent fix that returns `""` for junk and trusts a
recognised country code is preserved automatically by `parse()`'s country-code
precedence, so nothing that regression-tests already cover is lost.

## Findings

### S1 — `normalize_phone` accepts numbers that are not in the numbering plan

- **Where:** `dedup.py:33-63` (only length gate is `_MIN_DIGITS = 7` at `dedup.py:30,45`)
- **Breaks:** There is no length or prefix validation per country. Any 7+ digit
  string gets `+961` prepended and becomes a dedup key. A made-up
  `99999999` under `LB` returns `+96199999999` even though no such Lebanese
  range exists; an impossible 13-digit national number is accepted as-is. These
  phantom keys pollute the dedup index and, downstream, `has_any_contact`
  (`main.py:108`) and `lead_score` (`enricher.py:284`) both count them as real
  contact channels because they only test `len(phone) >= 7` on stripped digits.
- **Trigger:** `normalize_phone("99999999", "LB")` → `+96199999999`
  (not a real Lebanese number). `normalize_phone("1234567890123", "LB")` →
  `+9611234567890123` (13-digit NSN, impossible).
- **Fix:** Gate on `phonenumbers.is_valid_number(num)` after parsing. This
  validates both the leading-prefix ranges and the exact length against the
  country metadata.

### S1 — No mobile/landline distinction, so WhatsApp eligibility is unknowable

- **Where:** `dedup.py:33-63`; consumed as `has_phone = bool(record.get("phone"))`
  in `pitch_recommender.py:38` and `whatsapp` in `enricher.py:204-206`
- **Breaks:** WhatsApp only registers **mobile** numbers (plus the
  `FIXED_LINE_OR_MOBILE` ranges). A Lebanese landline (`01`, `04`, …) or a
  Saudi landline (`011`, `012`, …) is a valid phone and gets an E.164 key, but
  it is **not** WhatsApp-eligible. Today the `whatsapp` column is populated only
  when a `wa.me/...` link is scraped off a website (`enricher.py:129-132,204-206`);
  it is never derived from the business's own `phone`, and `recommend_service`
  has no way to distinguish "reachable on WhatsApp" from "call-only landline".
  Every pitch that leans on WhatsApp capture (the entire `_LEAD_GEN_VERTICALS`
  branch, `pitch_recommender.py:83-86`) is therefore blind to whether the number
  can even receive a WhatsApp message.
- **Trigger:** A restaurant with phone `01 234 567` (Beirut landline) is treated
  identically to a clinic with `03 123 456` (mobile) — both are just
  `has_phone = True`; neither can be told apart for WhatsApp outreach.
- **Fix:** After validation, classify with
  `phonenumbers.number_type(num)` and treat only
  `PhoneNumberType.MOBILE` and `PhoneNumberType.FIXED_LINE_OR_MOBILE` as
  WhatsApp-eligible. Persist the distinction (see "Recommended implementation").

### S2 — WhatsApp numbers scraped from `wa.me` links are unvalidated string surgery

- **Where:** `enricher.py:129-132` (`_WHATSAPP_RE`) and `enricher.py:204-206`
  (`contacts["whatsapp"] = "+" + m.group(1).lstrip("+")`)
- **Breaks:** The regex grabs 7–15 digits from any `wa.me/<digits>` /
  `whatsapp.com/send?phone=<digits>` link and prepends `+` with no validation.
  A website with a malformed or placeholder link (e.g. `wa.me/00000000000` or a
  truncated ID) writes a fake E.164 number into `whatsapp`, which then bumps
  `completeness_score` (`enricher.py:113`) and `lead_score` (`enricher.py:282`)
  as a real contact channel. This is the same class of bug as the junk→`+`
  dedup collapse, relocated to the enricher.
- **Trigger:** HTML containing `https://wa.me/999999` (6 digits, below `\d{7,15}`
  so actually skipped) vs `https://wa.me/00000000000` (11 digits, accepted) →
  `whatsapp = "+00000000000"`, counted as a contact.
- **Fix:** Run the extracted digits through the same parse+validate path
  (`phonenumbers.parse("+00000000000", None)` → `is_valid_number` is `False` →
  drop). Only write E.164 for valid numbers.

### S2 — Region inference silently defaults any unknown country to Lebanon

- **Where:** `dedup.py:23-26` (`_COUNTRY_CODES`), `dedup.py:48`
  (`cc = _COUNTRY_CODES.get(country, "961")`); call sites `dedup.py:110,133`
  and `main.py:157` use `record.get("country") or "LB"`
- **Breaks:** The region map only knows `LB` and `SA`. A `country` value of
  `None`, `"sa"` (lowercase), `"KSA"`, `"Lebanon"`, or anything else silently
  resolves to Lebanon (`961`). `record.get("country") or "LB"` also turns a
  present-but-`None` into `LB`. So a Saudi national number (`0501234567`) in a
  record whose `country` came through as `"sa"` or `None` becomes
  `+961501234567` — relabelled as Lebanese and keyed under the wrong country.
  `phonenumbers.parse()` needs a **region code** (ISO-3166-1 alpha-2), so the
  same normalization gap must be closed before the hint is passed in.
- **Trigger:** `normalize_phone("0501234567", "sa")` → `+961501234567` (wrong
  country). `normalize_phone("0501234567", None)` → `+961501234567`.
- **Fix:** Normalize the country to a canonical region code up front (map
  `"sa"→"SA"`, `"KSA"→"SA"`, `"Lebanon"→"LB"`; anything un-mapped that has no
  `+`/country code in the number → reject, don't default). For numbers that
  carry their own `+`/IDD country code, let `parse()` infer the region from the
  number itself.

## The exact replacement

Add `phonenumbers==9.0.30` to `requirements.txt`. It is a pure-Python port of
Google's libphonenumber with the metadata bundled inside the wheel — no C
extension, no runtime network calls, no extra data files. (If wheel size ever
matters, `phonenumberslite` drops geocoder/carrier/timezone; do **not** use it
here because the geocoder is useful for region inference, below.)

Replace the body of `normalize_phone` (and the ad-hoc digit checks) with a single
module-level helper pair. This preserves the current contract — return `""` for
anything unusable so callers can test truthiness (`dedup.py:36-38`) — while
adding validation and type classification:

```python
import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat, PhoneNumberType

_REGION_ALIASES = {
    "LB": "LB", "LEBANON": "LB", "SA": "SA", "KSA": "SA", "SAUDI": "SA",
    "SAUDI ARABIA": "SA", "SAUDIARABIA": "SA",
}

def _region(country: str | None) -> str | None:
    """Canonical ISO region code, or None if we cannot trust the hint."""
    if not country:
        return None
    return _REGION_ALIASES.get(str(country).strip().upper())

def parse_phone(raw: str | None, country: str | None = "LB"):
    """Return a validated PhoneNumber, or None for anything unusable.

    This is the one choke point every phone path should call.
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None

    # A leading '+' or an IDD prefix (00...) carries its own country code and
    # parse() prefers it over the region hint, so a +966 number in an LB record
    # is trusted as Saudi — exactly the behaviour the recent fix encodes.
    region = _region(country)

    try:
        num = phonenumbers.parse(s, region)
    except NumberParseException:
        return None  # see "handling rejects" below

    if not phonenumbers.is_valid_number(num):
        return None

    # Optional: only accept numbers inside our target footprint.
    # Uncomment to drop e.g. +1 (US) numbers that don't belong in a MENA set.
    # if phonenumbers.region_code_for_number(num) not in {"LB", "SA"}:
    #     return None
    return num

def normalize_phone(raw: str | None, country: str | None = "LB") -> str:
    num = parse_phone(raw, country)
    return phonenumbers.format_number(num, PhoneNumberFormat.E164) if num else ""

def is_mobile(num) -> bool:
    return phonenumbers.number_type(num) in (
        PhoneNumberType.MOBILE,
        PhoneNumberType.FIXED_LINE_OR_MOBILE,
    )
```

### How to handle numbers libphonenumber rejects

`parse()` fails in two different ways, and both must be caught:

1. **It raises `NumberParseException`** when the string cannot be uniquely
   parsed. Inspect `e.error_type`:
   - `NumberParseException.INVALID_COUNTRY_CODE` — no region hint and no
     leading `+`/country code (`parse("0501234567", None)`).
   - `NumberParseException.NOT_A_NUMBER` — no digits (`parse("gibberish", "LB")`).
   - `NumberParseException.TOO_SHORT_AFTER_IDD` — `"00"` (or `+`) with nothing
     after it.
   - `NumberParseException.TOO_SHORT_NSN` / `TOO_LONG_NSN` — returned as an
     *object* (not raised) in current versions, but treat them identically: the
     `is_valid_number()` gate rejects them.

2. **It returns a `PhoneNumber` object that is still not valid.** `parse()` is
   lenient by design (README: "quite lenient and looks for a number in the
   input text"). `+120012301` parses but `is_valid_number()` is `False`.
   Therefore the `is_valid_number` gate after the `try/except` is **mandatory**,
   not optional — it is what converts "parsed" into "real".

Net rule: `try: parse → except NumberParseException: drop → if not
is_valid_number: drop → else: keep`. Everything that fails lands on `""`, which
makes `dedup()` fall through to the name key (`dedup.py:116-123`) exactly as the
recent fix intends.

### Validation and E.164 formatting

- `phonenumbers.parse("03 123 456", "LB")` → `Country Code: 961 National Number: 3123456`.
- `phonenumbers.is_valid_number(num)` → `True` only if the prefix is assigned
  **and** the length is exactly right for that country.
- `phonenumbers.format_number(num, PhoneNumberFormat.E164)` → `"+9613123456"`.
  Store this in the `phone` column and use it as the dedup key. E.164 is
  globally unique and what WhatsApp itself expects for `wa.me/<E164>`.
- `PhoneNumberFormat.NATIONAL` / `PhoneNumberFormat.INTERNATIONAL` are
  available if the CSV ever needs a human-facing column.

### Mobile vs landline (the WhatsApp gate)

```python
t = phonenumbers.number_type(num)
whatsapp_eligible = t in (PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE)
```

| Type | WhatsApp-eligible? | Lebanon examples | Saudi examples |
|---|---|---|---|
| `MOBILE` | yes | `03`, `70`, `71`, `76`, `78`, `79`, `81` | `050`, `053`–`059` |
| `FIXED_LINE_OR_MOBILE` | yes | — | some shared ranges |
| `FIXED_LINE` | **no** | `01`, `04`, `05`, `06`, `07`, `08`, `09` | `011`, `012`, `013`, `014`, `016`, `017` |
| `TOLL_FREE` | no | `80`? (validated per metadata) | `800` |
| `VOIP` / `PREMIUM_RATE` / `SHARED_COST` | no | — | — |

Landlines are real, valid numbers and must keep their E.164 key for dedup and
call outreach — they just must not be offered a WhatsApp pitch. Persist the flag
(e.g. a derived `is_mobile` boolean used by `recommend_service`, or compute it
inline) rather than dropping landlines.

### Region inference when country is missing

Three layers, in order:

1. **The number carries its own country code.** If the raw string starts with
   `+` or an IDD prefix (`00`), call `parse(s, None)` (or just pass the region;
   `parse` ignores it when the number is unambiguous). This is the "trust a
   recognised country code" behaviour: `parse("+966 50 123 4567", "LB")` →
   Saudi, regardless of the record's `country`.
2. **The record has a country but not a trusted code.** Normalise it through
   `_REGION_ALIASES`; if unmapped, pass `None` and let the number's own code
   (if any) decide. If there is still no region and no `+`, `parse` raises
   `INVALID_COUNTRY_CODE` → drop to `""` (fall through to name dedup).
3. **Bonus — infer the *region* (city) of the record from the number** to
   supplement `enricher.infer_region()` (`enricher.py:79`), which today only
   reads address text and lat/lon:
   ```python
   from phonenumbers import geocoder, carrier
   city = geocoder.description_for_number(num, "en")   # e.g. "Beirut", "Riyadh"
   carrier_name = carrier.name_for_number(num, "en")   # e.g. "Alfa", "STC"
   ```
   Note geocoder/carrier data is lazy-loaded; `import phonenumbers.geocoder`
   (and `.carrier`) at startup to force-load the ~2 MB metadata and avoid a
   mid-run pause.

## Truth table (current vs libphonenumber)

| Input | country | Current `dedup.py` | libphonenumber (`parse` + `is_valid_number` + E.164) |
|---|---|---|---|
| `03 123 456` | `LB` | `+9613123456` | `+9613123456` — `MOBILE` ✓ |
| `01 234 567` | `LB` | `+9611234567` | `+9611234567` — `FIXED_LINE` (not WhatsApp) |
| `+961 01 234 567` | `LB` | `+96101234567` **WRONG** (trunk zero) | `+9611234567` ✓ (trunk removed by plan) |
| `00966 50 123 4567` | `LB` | `+966501234567` | `+966501234567` ✓ |
| `+966 50 123 4567` | `LB` | `+966501234567` | `+966501234567` ✓ (code trusted over `LB`) |
| `0501234567` | `SA` | `+966501234567` | `+966501234567` ✓ |
| `0501234567` | `None`/`"sa"` | `+961501234567` **WRONG** | `""` (no trusted region) — or `+966…` after alias fix |
| `99999999` | `LB` | `+96199999999` **WRONG** | `""` (not valid) |
| `03 123 456 ext 9` | `LB` | `+96131234569` **WRONG** (ext merged) | `+9613123456` + `num.extension == "9"` |
| `112` | `LB` | `+112` **WRONG** | `""` (short code, not E.164) |
| `---` / `abc` | `LB` | `""` (fixed) | `""` ✓ (same) |

## Not a bug, but worth knowing

- The recent fix's two guarantees are both preserved by the library: junk →
  `""` via `is_valid_number`/`parse` rejection, and "trust a recognised country
  code" via `parse()`'s built-in precedence of an explicit `+`/IDD code over the
  region hint. The hand-maintained `_KNOWN_COUNTRY_CODES` tuple
  (`dedup.py:8-21`) and the `00`/`0+cc` branches (`dedup.py:50-55`) become dead
  code and can be deleted.
- `phonenumbers` ships with a `PhoneNumberMatcher` (`phonenumbers.phonenumbermatcher`)
  that can pull candidates out of free text; it could eventually replace the
  hand-written `_WHATSAPP_RE`/`_EMAIL_RE` regexes, but it is heavier and out of
  scope for the minimal change.
- The `_MIN_DIGITS = 7` heuristic (`dedup.py:30`) is subsumed by
  `is_valid_number`; do not keep both, or you will reject a valid 6-digit number
  the metadata accepts and accept a 7-digit one it rejects. Delete it.

## Recommended order of work

1. Add `phonenumbers==9.0.30` to `requirements.txt`.
2. Implement `parse_phone`/`normalize_phone`/`is_mobile` as above in `dedup.py`,
   deleting `_KNOWN_COUNTRY_CODES`, `_COUNTRY_CODES`, `_MIN_DIGITS`, and the
   digit-string branches.
3. Replace the two ad-hoc `re.sub(r"\D", ...)` + `len >= 7` checks —
   `main.py:104` (`has_any_contact`) and `enricher.py:277,284` (`lead_score`) —
   with `parse_phone(...) is not None` (or the stored E.164 field).
4. Route the `wa.me` extraction (`enricher.py:204-206`) through `parse_phone` so
   junk links cannot write a fake `whatsapp`.
5. Add the mobile/landline split and feed it to `recommend_service` for the
   WhatsApp-dependent pitches.
6. Extend `tests/test_lead_signal.py` with the truth-table rows above, including
   `None`/lowercase country, trunk-zero-after-country-code, extension, short
   code, impossible length, and both mobile and landline classification.
