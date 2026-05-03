import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

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


_COORD_REGIONS: list[tuple[str, float, float, float, float]] = [
    # (region, lat_min, lat_max, lon_min, lon_max) — ordered most-specific first
    ("Beirut",          33.845, 33.920, 35.462, 35.545),
    ("Akkar",           34.380, 34.720, 35.980, 36.650),
    ("Baalbek-Hermel",  34.000, 34.720, 36.100, 36.850),
    ("Bekaa",           33.380, 34.200, 35.750, 36.650),
    ("South Lebanon",   33.040, 33.580, 35.090, 35.750),
    ("Nabatieh",        33.240, 33.560, 35.330, 35.720),
    ("North Lebanon",   34.100, 34.680, 35.490, 36.300),
    ("Mount Lebanon",   33.540, 34.120, 35.370, 35.950),
]


def infer_region(address: str | None, lat: float | None, lon: float | None) -> str | None:
    if address:
        for region, pattern in _REGION_MAP:
            if pattern.search(address):
                return region
    if lat is not None and lon is not None:
        for region, lat_min, lat_max, lon_min, lon_max in _COORD_REGIONS:
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
    return score


# ---------------------------------------------------------------------------
# Website liveness check
# ---------------------------------------------------------------------------

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "Mozilla/5.0 (compatible; leadminer/1.0)"


def _check_url(url: str) -> bool:
    try:
        r = _SESSION.head(url, timeout=8, allow_redirects=True, verify=False)
        if r.status_code == 405:
            r = _SESSION.get(url, timeout=8, allow_redirects=True, verify=False, stream=True)
        return r.status_code < 500
    except Exception:
        return False


def check_websites(records: list[dict], workers: int = 50) -> list[dict]:
    urls = [(i, r["website"]) for i, r in enumerate(records) if r.get("website")]
    if not urls:
        return records

    print(f"[Enricher] Checking liveness of {len(urls)} websites ({workers} workers)...")
    results: dict[int, bool] = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_check_url, url): idx for idx, url in urls}
        done = 0
        for future in as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()
            done += 1
            if done % 100 == 0:
                print(f"[Enricher] {done}/{len(urls)} checked...")

    for idx, live in results.items():
        records[idx]["website_live"] = live

    live_count = sum(1 for v in results.values() if v)
    print(f"[Enricher] {live_count} live / {len(urls) - live_count} dead websites")
    return records


# ---------------------------------------------------------------------------
# Main enrichment pipeline
# ---------------------------------------------------------------------------

def enrich(records: list[dict]) -> list[dict]:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    for r in records:
        # Only infer region if the scraper hasn't already set one
        # (Google Places sets KSA regions that this Lebanon-only inferer would clobber)
        if not r.get("region"):
            r["region"] = infer_region(r.get("address"), r.get("lat"), r.get("lon"))
        r["completeness_score"] = completeness_score(r)

    records = check_websites(records)

    # ensure all new fields exist on every record (Wikidata records have None)
    for r in records:
        r.setdefault("lat", None)
        r.setdefault("lon", None)
        r.setdefault("facebook", None)
        r.setdefault("instagram", None)
        r.setdefault("website_live", None)
        r.setdefault("rating", None)
        r.setdefault("review_count", None)

    return records
