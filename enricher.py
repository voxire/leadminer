import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

# ---------------------------------------------------------------------------
# Region normalization
# ---------------------------------------------------------------------------

REGION_KEYWORDS: list[tuple[str, list[str]]] = [
    ("Beirut", [
        "beirut", "بيروت", "hamra", "achrafieh", "verdun", "ras beirut",
        "sodeco", "badaro", "gemmayze", "mar mikhael", "corniche", "bliss",
        "sanayeh", "zkak el blat", "tallet el khayat", "raouche",
    ]),
    ("Mount Lebanon", [
        "jounieh", "jbeil", "byblos", "baabda", "aley", "chouf", "metn",
        "antelias", "jdeideh", "bikfaya", "broummana", "beit mery", "dbayeh",
        "naccache", "sin el fil", "dekwaneh", "hazmieh", "aramoun", "khalde",
        "damour", "jiyeh", "bchamoun", "bhamdoun", "aaley", "deir el qamar",
        "beit ed dine", "zahle el metn", "kaslik", "zouk", "ghazir",
        "keserwan", "fanar", "mansourieh", "mtayleb", "rabweh",
    ]),
    ("North Lebanon", [
        "tripoli", "طرابلس", "zgharta", "batroun", "bcharre", "koura",
        "amioun", "chekka", "enfeh", "qalamoun", "minyeh", "danniyeh",
        "bsharri", "ehden", "kousba", "zghorta",
    ]),
    ("Akkar", [
        "akkar", "عكار", "halba", "حلبا", "andqet", "bebnine", "kobayat",
        "qoubaiyat",
    ]),
    ("South Lebanon", [
        "sidon", "saida", "صيدا", "tyre", "sur", "صور", "jezzine",
        "nabatieh", "النبطية", "bint jbeil", "marjayoun", "khiam",
        "ibl el saqi", "hasbaya",
    ]),
    ("Nabatieh", [
        "nabatieh", "النبطية", "bint jbeil", "بنت جبيل", "hasbaya",
        "marjayoun", "merjeyoun",
    ]),
    ("Bekaa", [
        "zahle", "زحلة", "chtaura", "anjar", "rashaya", "west bekaa",
        "saghbine", "yohmor", "taanayel", "bar elias",
    ]),
    ("Baalbek-Hermel", [
        "baalbek", "بعلبك", "hermel", "الهرمل", "yammouneh", "ras baalbek",
        "qaa", "deir el ahmar",
    ]),
]

_REGION_MAP: list[tuple[str, re.Pattern]] = [
    (region, re.compile("|".join(re.escape(k) for k in keywords), re.IGNORECASE))
    for region, keywords in REGION_KEYWORDS
]


_LB_COORD_REGIONS: list[tuple[str, float, float, float, float]] = [
    # (region, lat_min, lat_max, lon_min, lon_max) — most-specific first
    ("Beirut",          33.845, 33.920, 35.462, 35.545),
    ("Akkar",           34.380, 34.720, 35.980, 36.650),
    ("Baalbek-Hermel",  34.000, 34.720, 36.100, 36.850),
    ("Bekaa",           33.380, 34.200, 35.750, 36.650),
    ("South Lebanon",   33.040, 33.580, 35.090, 35.750),
    ("Nabatieh",        33.240, 33.560, 35.330, 35.720),
    ("North Lebanon",   34.100, 34.680, 35.490, 36.300),
    ("Mount Lebanon",   33.540, 34.120, 35.370, 35.950),
]

_KSA_COORD_REGIONS: list[tuple[str, float, float, float, float]] = [
    # Smaller / more specific cities checked first to win against overlapping
    # larger metros (e.g. Khobar overlaps with Dammam's eastern edge).
    ("Khobar",   26.20, 26.40, 50.10, 50.30),
    ("Yanbu",    24.00, 24.20, 38.00, 38.20),
    ("Taif",     21.20, 21.40, 40.30, 40.50),
    ("Buraydah", 26.30, 26.45, 43.95, 44.10),
    ("Tabuk",    28.30, 28.55, 36.45, 36.70),
    ("Abha",     18.15, 18.30, 42.45, 42.65),
    ("Hail",     27.45, 27.60, 41.65, 41.80),
    ("Mecca",    21.30, 21.55, 39.75, 40.00),
    ("Medina",   24.30, 24.65, 39.45, 39.80),
    ("Dammam",   26.20, 26.65, 49.85, 50.10),  # eastern bound trimmed for Khobar
    ("Riyadh",   24.40, 25.20, 46.40, 47.20),
    ("Jeddah",   21.30, 21.80, 39.05, 39.45),
]


def infer_region(
    address: str | None,
    lat: float | None,
    lon: float | None,
    country: str = "LB",
) -> str | None:
    if address and country == "LB":
        for region, pattern in _REGION_MAP:
            if pattern.search(address):
                return region
    if lat is not None and lon is not None:
        boxes = _KSA_COORD_REGIONS if country == "SA" else _LB_COORD_REGIONS
        for region, lat_min, lat_max, lon_min, lon_max in boxes:
            if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
                return region
    return None


# ---------------------------------------------------------------------------
# Completeness score
# ---------------------------------------------------------------------------

def completeness_score(record: dict) -> int:
    score = 0
    if record.get("phone"):
        score += 1
    if record.get("email"):
        score += 1
    if record.get("website"):
        score += 1
    if record.get("address"):
        score += 1
    if record.get("facebook") or record.get("instagram"):
        score += 1
    if record.get("whatsapp"):
        score += 1
    if record.get("linkedin"):
        score += 1
    return score


# ---------------------------------------------------------------------------
# Combined website liveness + contact extraction (single HTTP pass)
# ---------------------------------------------------------------------------

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "Mozilla/5.0 (compatible; leadminer/1.0)"

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_INSTAGRAM_RE = re.compile(r"(?:instagram\.com/|@)([a-zA-Z0-9_.]{2,30})/?", re.IGNORECASE)
_WHATSAPP_RE = re.compile(
    r"(?:wa\.me/|whatsapp\.com/send\?phone=|api\.whatsapp\.com/send\?phone=)(\d{7,15})",
    re.IGNORECASE,
)
_LINKEDIN_RE = re.compile(r"linkedin\.com/company/([a-zA-Z0-9\-_.]+)/?", re.IGNORECASE)

_EMAIL_BLACKLIST = {
    "example.com", "domain.com", "yourdomain.com", "email.com",
    "sentry.io", "wixpress.com", "squarespace.com", "shopify.com",
}
_IG_BLACKLIST = {"instagram", "p", "explore", "accounts", "stories", "reel", "reels", "tv"}

_EMAIL_VALID_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")


def _clean_email(addr: str) -> str | None:
    """Strip URL params, split on punctuation, validate format. None if junk."""
    if not addr:
        return None
    addr = addr.strip().lower()
    addr = addr.split("?")[0]                          # drop URL params (?subject=...)
    addr = re.split(r"[;,\s<>]", addr, maxsplit=1)[0]  # take first if multiple
    addr = addr.strip(".,;:'\"()[]{}")                 # strip stray punctuation
    if not _EMAIL_VALID_RE.match(addr):
        return None
    if addr.endswith((".png", ".jpg", ".gif", ".svg", ".webp")):
        return None
    return addr


def _fetch_website(url: str) -> tuple[bool, dict]:
    """Single GET — returns (is_live, contact_info)."""
    contacts: dict = {"email": None, "instagram": None, "whatsapp": None, "linkedin": None}
    try:
        r = _SESSION.get(url, timeout=8, allow_redirects=True, verify=False)
        live = r.status_code < 500
        if not live or r.status_code >= 400:
            return live, contacts
        html = r.text[:200_000]
    except Exception:
        return False, contacts

    mailto_hits = re.findall(r'href=["\']mailto:([^"\'>\s]+)', html, re.IGNORECASE)
    for addr in mailto_hits + _EMAIL_RE.findall(html):
        cleaned = _clean_email(addr)
        if not cleaned:
            continue
        domain = cleaned.split("@")[-1]
        if domain not in _EMAIL_BLACKLIST:
            contacts["email"] = cleaned
            break

    for m in _INSTAGRAM_RE.finditer(html):
        handle = m.group(1).strip("/").lower()
        if handle not in _IG_BLACKLIST and len(handle) >= 2:
            contacts["instagram"] = handle
            break

    m = _WHATSAPP_RE.search(html)
    if m:
        contacts["whatsapp"] = "+" + m.group(1).lstrip("+")

    m = _LINKEDIN_RE.search(html)
    if m:
        slug = m.group(1).strip("/").lower()
        if slug not in {"company", "in", "pub"}:
            contacts["linkedin"] = f"https://linkedin.com/company/{slug}"

    return True, contacts


def check_websites(records: list[dict], workers: int = 40) -> list[dict]:
    """Single-pass: check liveness and extract contacts in one GET per site."""
    targets = [(i, r["website"]) for i, r in enumerate(records) if r.get("website")]
    if not targets:
        return records

    print(f"[Enricher] Fetching {len(targets)} websites — liveness + contacts ({workers} workers)...")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_website, url): idx for idx, url in targets}
        done = 0
        for future in as_completed(futures):
            idx = futures[future]
            live, contacts = future.result()
            r = records[idx]
            r["website_live"] = live
            if live:
                if not r.get("email") and contacts["email"]:
                    r["email"] = contacts["email"]
                if not r.get("instagram") and contacts["instagram"]:
                    r["instagram"] = contacts["instagram"]
                if not r.get("whatsapp") and contacts["whatsapp"]:
                    r["whatsapp"] = contacts["whatsapp"]
                if not r.get("linkedin") and contacts["linkedin"]:
                    r["linkedin"] = contacts["linkedin"]
            done += 1
            if done % 200 == 0:
                print(f"[Enricher] {done}/{len(targets)} done...")

    live_count = sum(1 for r in records if r.get("website_live"))
    dead_count = sum(1 for r in records if r.get("website_live") is False)
    found_email = sum(1 for r in records if r.get("email"))
    found_ig = sum(1 for r in records if r.get("instagram"))
    found_wa = sum(1 for r in records if r.get("whatsapp"))
    found_li = sum(1 for r in records if r.get("linkedin"))
    print(f"[Enricher] {live_count} live / {dead_count} dead")
    print(
        f"[Enricher] Contacts — email:{found_email} instagram:{found_ig} "
        f"whatsapp:{found_wa} linkedin:{found_li}"
    )
    return records


# ---------------------------------------------------------------------------
# Synthetic WhatsApp from phone (covers Lebanon/KSA/UAE/Qatar SMBs whose
# websites don't include a wa.me link but who do answer WhatsApp on their
# main phone — which is most of MENA).
# ---------------------------------------------------------------------------

_WHATSAPP_PHONE_RE = re.compile(r"^\+(961|966|971|974|973|968|965|962|970)\d{7,9}$")


def synthesize_whatsapp_from_phone(records: list[dict]) -> list[dict]:
    """
    For records that have a phone but no whatsapp, synthesize the wa.me
    equivalent. A real wa.me link extracted from the website always wins —
    this only fills gaps.
    """
    filled = 0
    for r in records:
        if r.get("whatsapp"):
            continue
        phone = (r.get("phone") or "").strip()
        if not phone:
            continue
        if _WHATSAPP_PHONE_RE.match(phone):
            r["whatsapp"] = phone
            filled += 1
    if filled:
        print(f"[Enricher] Synthesized {filled} WhatsApp numbers from phone field")
    return records


# ---------------------------------------------------------------------------
# Lead quality score (0–100)
# ---------------------------------------------------------------------------

def lead_score(record: dict) -> int:
    score = 0
    phone = re.sub(r"\D", "", str(record.get("phone") or ""))

    # Contact signals
    if record.get("email"):
        score += 20
    if record.get("whatsapp"):
        score += 15
    if len(phone) >= 7:
        score += 15
    if record.get("instagram"):
        score += 10
    # Website signals
    if record.get("website_live") is True:
        score += 10
    elif record.get("website") and record.get("website_live") is False:
        score += 20  # dead website = sales opportunity
    # Industry priority
    priority = record.get("industry_priority")
    if priority == "high":
        score += 15
    elif priority == "medium":
        score += 8
    # Low rating = pain point worth pitching
    rating = record.get("rating")
    try:
        if rating is not None and float(rating) < 4.0:
            score += 10
    except (TypeError, ValueError):
        pass
    # Review count = signal of real customer base. Sorts the premium tier.
    try:
        rc = int(record.get("review_count") or 0)
    except (TypeError, ValueError):
        rc = 0
    if rc >= 100:
        score += 10
    elif rc >= 20:
        score += 5
    elif rc >= 5:
        score += 2
    # Multi-source confirmation
    if "|" in str(record.get("source") or ""):
        score += 5

    return min(score, 100)


# ---------------------------------------------------------------------------
# Main enrichment pipeline
# ---------------------------------------------------------------------------

def enrich(records: list[dict]) -> list[dict]:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    for r in records:
        if not r.get("region"):
            r["region"] = infer_region(
                r.get("address"), r.get("lat"), r.get("lon"),
                country=r.get("country", "LB"),
            )

    records = check_websites(records)
    # Synthesize whatsapp AFTER website scrape so any real wa.me link wins
    records = synthesize_whatsapp_from_phone(records)

    for r in records:
        r.setdefault("lat", None)
        r.setdefault("lon", None)
        r.setdefault("facebook", None)
        r.setdefault("instagram", None)
        r.setdefault("whatsapp", None)
        r.setdefault("linkedin", None)
        r.setdefault("website_live", None)
        r.setdefault("rating", None)
        r.setdefault("review_count", None)
        r["completeness_score"] = completeness_score(r)
        r["lead_score"] = lead_score(r)

    return records
