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


def _is_missing(value) -> bool:
    """True when a field carries no information.

    The previous check was `is None`, which meant an empty string counted as
    present: merging a rich record with a sparse record whose website was ""
    produced website="", silently discarding a real URL.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


# How much each source is trusted per field. Google knows ratings, review
# counts and coordinates because they are its own data; OSM carries contact
# tags that Google does not expose at all. Trust therefore has to be per-field,
# not per-record.
_SOURCE_TRUST: dict[str, dict[str, int]] = {
    "google_places": {
        "rating": 3, "review_count": 3, "lat": 3, "lon": 3,
        "name": 3, "address": 3, "category": 3, "website": 2, "phone": 2,
    },
    "osm": {
        "email": 3, "website": 3, "facebook": 3, "instagram": 3, "whatsapp": 3,
        "phone": 2, "name": 2, "address": 2, "category": 2,
    },
    "wikidata": {
        "email": 2, "website": 2, "phone": 2,
        "name": 2, "address": 2, "category": 1,
    },
}
_DEFAULT_TRUST = 1

# Fields whose value changes over time, so the fresher observation wins.
_VOLATILE = {"rating", "review_count", "website_live", "website"}

_EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_OK = re.compile(r"^https?://[^\s/]+\.[^\s]+", re.IGNORECASE)


def _validity(field: str, value) -> int:
    """0-3 bonus for whether a value is merely present or actually plausible."""
    if _is_missing(value):
        return -100
    if field == "email":
        return 3 if _EMAIL_OK.match(str(value).strip()) else 0
    if field == "website":
        return 3 if _URL_OK.match(str(value).strip()) else 0
    if field in ("lat", "lon"):
        try:
            float(value)
        except (TypeError, ValueError):
            return 0
        return 3
    if field == "rating":
        try:
            v = float(value)
        except (TypeError, ValueError):
            return 0
        return 3 if 0 <= v <= 5 else 0
    return 1


def _sources_of(record: dict) -> set[str]:
    raw = str(record.get("source") or "")
    return {s for s in raw.split("|") if s}


def _trust_for(field: str, record: dict) -> int:
    best = _DEFAULT_TRUST
    for src in _sources_of(record):
        best = max(best, _SOURCE_TRUST.get(src, {}).get(field, _DEFAULT_TRUST))
    return best


def _recency(record: dict) -> str:
    return str(record.get("scraped_at") or "")


def _pick(field: str, a: dict, b: dict) -> object:
    """Choose the surviving value for one field, deterministically.

    Replaces the previous rule, which picked every conflicting field from
    whichever whole record happened to have more populated fields. That made
    the merge order-dependent and let a low-quality value in a rich record
    beat a high-quality value in a sparse one.
    """
    av, bv = a.get(field), b.get(field)
    if _is_missing(av):
        return bv
    if _is_missing(bv):
        return av

    if field == "source":
        return "|".join(sorted(_sources_of(a) | _sources_of(b)))

    if field == "scraped_at":
        return max(av, bv)

    def rank(rec: dict, value) -> tuple:
        return (
            _trust_for(field, rec),
            _validity(field, value),
            _recency(rec) if field in _VOLATILE else "",
            str(value),  # deterministic final tiebreak
        )

    return av if rank(a, av) >= rank(b, bv) else bv


def _merge(a: dict, b: dict) -> dict:
    merged = {}
    for key in set(a) | set(b):
        merged[key] = _pick(key, a, b)
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
