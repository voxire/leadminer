import re
import unicodedata


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("00961"):
        digits = "961" + digits[5:]
    elif digits.startswith("0961"):
        digits = "961" + digits[4:]
    if digits.startswith("961"):
        return "+" + digits
    if digits.startswith("0"):
        return "+961" + digits[1:]
    if len(digits) >= 7:
        return "+961" + digits
    return "+" + digits


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
    merged = {}
    for key in a:
        av, bv = a[key], b[key]
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
        if raw_phone:
            key = normalize_phone(raw_phone)
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
        if raw and normalize_phone(raw) in captured_phones:
            continue
        name_records.append(record)

    return phone_records + name_records
