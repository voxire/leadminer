import re
import unicodedata


# Calling codes we recognise anywhere in the region. Data leaks across
# borders constantly, so we must recognise a number's real country even when
# the record claims another one, otherwise dedup keys it under the wrong one.
_KNOWN_COUNTRY_CODES = (
    "961",  # Lebanon
    "966",  # Saudi Arabia
    "962",  # Jordan
    "971",  # UAE
    "965",  # Kuwait
    "973",  # Bahrain
    "974",  # Qatar
    "968",  # Oman
    "212",  # Morocco
    "216",  # Tunisia
    "218",  # Libya
    "20",   # Egypt
)

_COUNTRY_CODES = {
    "LB": "961",
    "SA": "966",
}

# Anything shorter than this is punctuation, a shortcode or an extension
# fragment, not a dialable number. Such input must never become a dedup key.
_MIN_DIGITS = 7


def normalize_phone(phone: str, country: str = "LB") -> str:
    """Normalize to E.164, or return "" if the input is not a usable number.

    Returns "" rather than a placeholder like "+" so that callers can test the
    result for truthiness. Junk used to normalize to "+", which made unrelated
    businesses share one dedup key.
    """
    if not phone:
        return ""
    raw = str(phone)
    had_plus = "+" in raw
    digits = re.sub(r"\D", "", raw)
    if len(digits) < _MIN_DIGITS:
        return ""

    cc = _COUNTRY_CODES.get(country, "961")

    if digits.startswith("00"):
        # International dialling prefix: drop it, the real code follows.
        digits = digits[2:]
    elif not had_plus and digits.startswith("0" + cc):
        # National format written with a leading country code, e.g. 0961...
        digits = cc + digits[len(cc) + 1:]

    # The number already states its own country. Trust it over `country`.
    for known in _KNOWN_COUNTRY_CODES:
        if digits.startswith(known):
            return "+" + digits

    # Otherwise it is a bare national number for the requested country.
    return "+" + cc + digits.lstrip("0")


def normalize_name(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", name).lower().strip()


def _extract_city(address: str | None) -> str:
    if not address:
        return ""
    parts = [p.strip().lower() for p in address.split(",")]
    return parts[-1] if parts else ""


def _field_count(record: dict) -> int:
    return sum(1 for v in record.values() if v is not None)


def _merge(a: dict, b: dict) -> dict:
    all_keys = set(a) | set(b)
    merged = {}
    for key in all_keys:
        av, bv = a.get(key), b.get(key)
        if av is None:
            merged[key] = bv
        elif bv is None:
            merged[key] = av
        elif key == "source":
            sources = set(av.split("|")) | set(bv.split("|"))
            merged[key] = "|".join(sorted(sources))
        elif key == "scraped_at":
            merged[key] = max(av, bv)
        else:
            merged[key] = av if _field_count(a) >= _field_count(b) else bv
    return merged


def dedup(records: list[dict]) -> list[dict]:
    phone_index: dict[str, dict] = {}
    name_index: dict[tuple, dict] = {}

    for record in records:
        raw_phone = record.get("phone")
        # normalize_phone returns "" for junk, which must fall through to the
        # name key rather than becoming its own (shared, empty) phone key.
        key = normalize_phone(raw_phone, record.get("country") or "LB") if raw_phone else ""
        if key:
            if key in phone_index:
                phone_index[key] = _merge(phone_index[key], record)
            else:
                phone_index[key] = dict(record)
        else:
            name = record.get("name") or ""
            city = _extract_city(record.get("address"))
            key = (normalize_name(name), city)
            if key in name_index:
                name_index[key] = _merge(name_index[key], record)
            else:
                name_index[key] = dict(record)

    # collect phone-keyed records first, then name-keyed ones that
    # don't share a phone with an already-captured record
    phone_records = list(phone_index.values())
    captured_phones = set(phone_index.keys())

    name_records = []
    for record in name_index.values():
        raw = record.get("phone")
        if raw and normalize_phone(raw, record.get("country") or "LB") in captured_phones:
            continue
        name_records.append(record)

    return phone_records + name_records
