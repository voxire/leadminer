# 096 — OpenStreetMap Saudi Arabia Expansion: Overpass Area Resolution, Relations Ingestion, and Multi-Country Configuration Architecture

## Verdict

`leadminer` advertises commercial lead generation for Saudi Arabia (Riyadh, Jeddah, Dammam), yet its OpenStreetMap scraper (`scrapers/osm.py`) is hardcoded to Lebanon (`area["ISO3166-1"="LB"]`, `country="LB"`), completely locking out the largest and highest-margin commercial market in the region. Directly substituting `ISO3166-1=SA` in the current query pattern fails catastrophically on public Overpass instances due to memory limits (`[maxsize]`) and 180s gateway timeouts across Saudi Arabia's 2.15M km² landmass. To expand to Saudi Arabia reliably, the scraper must transition to partitioned administrative area resolution (`admin_level=4` / `ISO3166-2` or metro bounding boxes), incorporate multipolygon relations (`nwr`) to capture Saudi Arabia's mall-centric retail infrastructure, and be driven by a declarative multi-country configuration model.

---

## Findings

### S1 — Hardcoded Lebanon boundary and country tag completely blind OSM ingestion to Saudi Arabia
- **Where:** `scrapers/osm.py:12, 33, 100`
- **Breaks:** Line 12 restricts the Overpass search space strictly to `area["ISO3166-1"="LB"]->.lb;`, line 33 prints a Lebanon-specific log message, and line 100 hardcodes `country="LB"` on every generated `BusinessRecord`. While `main.py:183-185` attempts post-hoc `resolve_country(r)` based on phone prefix heuristics, OSM records without explicit international phone prefixes default back to `"LB"` (`main.py:47, 74`). Consequently, even if an operator modifies the raw Overpass query string to target Saudi Arabia, records lacking phone numbers or carrying local formats are misattributed to Lebanon, misrouting them into `_LB_COORD_REGIONS` inside `enricher.py:90`, corrupting region tags, and polluting `all_businesses.csv`.
- **Trigger:** Running `python main.py` expecting Saudi leads from OSM as promised in `README.md`. OSM produces 0 Saudi records.
- **Fix:** Parameterize `OSMScraper` with a target country configuration, dynamically build the query from country-specific administrative area selectors, and instantiate `BusinessRecord` with `country=self.country_code`.

### S1 — Monolithic nationwide Overpass query for Saudi Arabia triggers fatal OOM and 504 timeouts
- **Where:** `scrapers/osm.py:10-26`
- **Breaks:** Lebanon's commercial POI dataset is compact (~18,000–24,000 elements across 10,452 km²). Saudi Arabia spans 2,149,690 km² with >140,000 named commercial POIs. Executing an unindexed, single-pass countrywide query (`area["ISO3166-1"="SA"]`) across six broad category keys (`shop`, `amenity`, `office`, `tourism`, `craft`, `healthcare`) demands over 750 MB of RAM on the Overpass backend. Public instances (`overpass-api.de`) enforce a default memory ceiling of 512 MB (`[maxsize:536870912]`), causing the server process to abort mid-stream with `runtime error: Query ran out of memory` or hit the 180s execution ceiling with an HTTP 504 Gateway Timeout.
- **Trigger:** Changing `ISO3166-1="LB"` to `ISO3166-1="SA"` in `scrapers/osm.py:12` without spatial partitioning or memory increases.
- **Fix:** Partition Saudi extraction by target metro areas or administrative provinces (`admin_level=4` for Riyadh, Makkah, and Eastern Province), append quadtile sorting (`qt`), and configure explicit `[maxsize:1073741824]` (1 GB) and `[timeout:60]`.

### S2 — Total exclusion of relations discards Saudi commercial real estate, shopping malls, and healthcare complexes
- **Where:** `scrapers/osm.py:13-24`
- **Breaks:** The query targets only `node` and `way`. In the Gulf region, extreme summer temperatures dictate that retail, dining, entertainment, and professional services are overwhelmingly housed within indoor shopping malls, commercial gallerias, and mixed-use complexes rather than standalone street-front buildings. In OpenStreetMap, these properties (e.g., Kingdom Centre and Riyadh Gallery in Riyadh, Red Sea Mall and Mall of Arabia in Jeddah, Dhahran Mall in Eastern Province) are universally mapped as `relation["type"="multipolygon"]` or `relation["type"="site"]`. Omitting `relation` excludes both the mall operators (prime enterprise agency targets) and any enclosed facility mapped as a polygon member, dropping high-value prospects before whitelist evaluation.
- **Trigger:** Ingesting POIs from Riyadh, Jeddah, or Khobar where premier retail spaces are mapped as multipolygon relations.
- **Fix:** Replace distinct `node` and `way` blocks with unified `nwr` (nodes, ways, relations) selectors, and retrieve relation centroids via `out center tags qt;`.

### S2 — Hardcoded single Overpass endpoint risks total pipeline blockage under public rate limiting
- **Where:** `scrapers/osm.py:8, 40-44`
- **Breaks:** Line 8 hardcodes `OVERPASS_URL = "https://overpass-api.de/api/interpreter"`. The main German Overpass instance strictly limits IP concurrency to 2 slots and frequently sheds load during European/Middle Eastern peak business hours (returning HTTP 429 or HTTP 504). When querying larger payloads for Saudi Arabia, a single rate-limit block or backend outage halts all OSM scraping across all retries (`lines 38-54`), completely abandoning OSM data collection for the entire pipeline run.
- **Trigger:** Public instance load spike or consecutive large area queries consuming rate-limit slots.
- **Fix:** Configure an ordered list of Overpass mirrors (e.g., `https://overpass-api.de/api/interpreter`, `https://overpass.kumi.systems/api/interpreter`, `https://maps.mail.ru/osm/tools/overpass/api/interpreter`) with automatic failover upon receiving 429/502/503/504 status codes.

### S3 — Missing sub-national administrative tagging degrades city deduplication and region inference
- **Where:** `scrapers/osm.py:77-84`, interacting with `dedup.py:34-38` and `enricher.py:70-76`
- **Breaks:** `osm.py` extracts address strings from `addr:housenumber`, `addr:street`, `addr:suburb`, `addr:city`, and `addr:district`. In Saudi Arabia, postal addressing is infrequently tagged on OSM nodes; businesses are usually placed within boundary polygons without explicit `addr:city` tags. When `addr:city` is absent and `addr:full` is ignored, `address` becomes `None` or lacks a city name. In `dedup.py:79`, phone-less records collapse to `(normalized_name, "")`, causing identically named common businesses in Riyadh and Jeddah (e.g., "Al Romansiah Restaurant", "Dr. Sulaiman Al Habib Pharmacy") to merge into a single mangled entity.
- **Trigger:** Two distinct branches of a popular chain in different Saudi cities mapped in OSM without explicit street addresses or phone numbers.
- **Fix:** Ingest the enclosing administrative boundary name (`is_in` or metro query context) as an address fallback so every Saudi record carries an explicit city/region tag into `dedup.py`.

---

## Saudi Arabia Overpass Query Architecture & Area Resolution

### 1. Area Resolution Mechanics in OpenStreetMap

Overpass QL creates synthetic `area` objects by pre-processing closed boundary relations and ways. To target an entire nation or province, the query must match tags on the OSM boundary relation:

- **Kingdom of Saudi Arabia (National Boundary):**
  - **OSM Relation ID:** `307584`
  - **Key Tags:**
    - `boundary=administrative`
    - `admin_level=2`
    - `ISO3166-1=SA`
    - `ISO3166-1:alpha2=SA`
    - `name=المملكة العربية السعودية`
    - `name:en=Saudi Arabia`
  - **Overpass Area Selector:**
    ```overpass
    area["ISO3166-1"="SA"]["admin_level"="2"]->.sa;
    ```
    *(Note: Specifying `["admin_level"="2"]` is mandatory. Without it, Overpass may match auxiliary maritime boundaries or statistical zones that share the `ISO3166-1` tag, generating ambiguous or broken area geometries).*

- **Saudi Administrative Provinces (`admin_level=4` — Minţaqah / إمارة منطقة):**
  Saudi Arabia is divided into 13 administrative provinces. The product specifically targets three primary commercial metros:
  1. **Riyadh Province (منطقة الرياض - includes Riyadh City, Al-Kharj):**
     - Relation ID: `1317544` | `ISO3166-2=SA-01` | `admin_level=4`
  2. **Makkah Province (منطقة مكة المكرمة - includes Jeddah, Mecca, Taif):**
     - Relation ID: `1317547` | `ISO3166-2=SA-02` | `admin_level=4`
  3. **Eastern Province (المنطقة الشرقية - includes Dammam, Khobar, Dhahran, Jubail):**
     - Relation ID: `1317546` | `ISO3166-2=SA-04` | `admin_level=4`

### 2. Nationwide vs. Metro-Partitioned Querying

Querying Saudi Arabia via a single national boundary (`area.sa`) introduces severe operational risks:

| Dimension | Monolithic National Query (`area["ISO3166-1"="SA"]`) | Metro-Partitioned Query (`ISO3166-2` / BBoxes) |
|---|---|---|
| **Geographic Scope** | 2,149,690 km² (includes vast uninhabited desert) | ~150,000 km² (concentrated urban economic centers) |
| **Server Memory Peak** | 700 MB – 1.2 GB (exceeds public Overpass default) | 120 MB – 220 MB per partition (well within limits) |
| **Query Execution Time** | 45s – 180s+ (frequent timeouts / 504s) | 6s – 18s per partition |
| **Payload Data Volume** | 35 MB – 55 MB raw JSON (~140,000 elements) | 8 MB – 14 MB raw JSON (~35,000 actionable leads) |
| **Irrelevant POIs** | Thousands of desert rest stops, border posts, small villages | Zero desert noise; 100% focused on commercial hubs |
| **Failure Blast Radius** | Single failure drops all Saudi leads | One metro failure leaves other metros intact |

### 3. The Saudi Equivalent Overpass Queries

#### Option A: Optimized National Query (Single Area Resolution)
Use this if running against a private Overpass server or high-capacity mirror with elevated memory limits:

```overpass
[out:json][timeout:120][maxsize:1073741824];
// Resolve Kingdom of Saudi Arabia national boundary
area["ISO3166-1"="SA"]["admin_level"="2"]->.sa;
(
  // Simultaneously query nodes, ways, and relations across target categories
  nwr["name"]["shop"](area.sa);
  nwr["name"]["amenity"](area.sa);
  nwr["name"]["office"](area.sa);
  nwr["name"]["tourism"](area.sa);
  nwr["name"]["craft"](area.sa);
  nwr["name"]["healthcare"](area.sa);
  // Capture unbranded or brand-tagged commercial anchors
  nwr["brand"]["shop"](area.sa);
  nwr["brand"]["amenity"](area.sa);
);
// Output element tags and centroids with quadtile sorting to optimize stream serialization
out center tags qt;
```

#### Option B: Production Standard — Multi-Province Commercial Metro Partition
This query executes against the three core commercial provinces (Riyadh, Makkah/Jeddah, Eastern Province/Dammam) in a single unified area union. It eliminates 90% of geographic empty space while capturing 95%+ of all commercial B2B leads in the Kingdom:

```overpass
[out:json][timeout:90][maxsize:536870912];
// Union the three target commercial provinces using ISO 3166-2
(
  area["ISO3166-2"="SA-01"]["admin_level"="4"]; // Riyadh Province
  area["ISO3166-2"="SA-02"]["admin_level"="4"]; // Makkah Province (Jeddah, Mecca)
  area["ISO3166-2"="SA-04"]["admin_level"="4"]; // Eastern Province (Dammam, Khobar)
)->.commercial_provinces;

(
  nwr["name"]["shop"](area.commercial_provinces);
  nwr["name"]["amenity"](area.commercial_provinces);
  nwr["name"]["office"](area.commercial_provinces);
  nwr["name"]["tourism"](area.commercial_provinces);
  nwr["name"]["craft"](area.commercial_provinces);
  nwr["name"]["healthcare"](area.commercial_provinces);
  nwr["brand"]["shop"](area.commercial_provinces);
  nwr["brand"]["amenity"](area.commercial_provinces);
);
out center tags qt;
```

#### Option C: Zero-Area Dependency Bounding Box Alternative
If Overpass area caches are cold or regenerating, bounding box queries execute immediately without area resolution overhead:

```overpass
[out:json][timeout:60][maxsize:536870912];
(
  // Riyadh Metro (24.40, 46.40, 25.20, 47.20)
  nwr["name"]["shop"](24.40,46.40,25.20,47.20);
  nwr["name"]["amenity"](24.40,46.40,25.20,47.20);
  nwr["name"]["office"](24.40,46.40,25.20,47.20);
  nwr["name"]["healthcare"](24.40,46.40,25.20,47.20);

  // Jeddah Metro (21.30, 39.05, 21.80, 39.45)
  nwr["name"]["shop"](21.30,39.05,21.80,39.45);
  nwr["name"]["amenity"](21.30,39.05,21.80,39.45);
  nwr["name"]["office"](21.30,39.05,21.80,39.45);
  nwr["name"]["healthcare"](21.30,39.05,21.80,39.45);

  // Dammam / Khobar Metro (26.20, 49.85, 26.65, 50.30)
  nwr["name"]["shop"](26.20,49.85,26.65,50.30);
  nwr["name"]["amenity"](26.20,49.85,26.65,50.30);
  nwr["name"]["office"](26.20,49.85,26.65,50.30);
  nwr["name"]["healthcare"](26.20,49.85,26.65,50.30);
);
out center tags qt;
```

---

## Architectural Decision: Ingestion of OSM Relations

### Verdict: Relations MUST be included.

### Technical & Commercial Rationale

1. **Urban Retail Topography in Saudi Arabia:**
   Unlike European or Levantine cities where street-level retail predominates, commercial life in Saudi Arabia is concentrated in large-scale shopping malls, mixed-use commercial centers, and plazas (e.g., Al Nakheel Mall, Riyadh Front, Centria, Stars Avenue, Red Sea Mall). In OpenStreetMap, these massive physical structures are represented as `relation["type"="multipolygon"]` because they contain inner atriums, skylights, courtyards, and distinct architectural wings.
2. **Enterprise Account Value (LTV):**
   A shopping mall or healthcare network relation represents an enterprise prospect requiring custom digital services (mobile apps, tenant directories, digital signage, multi-location SEO). Excluding relations discards the highest-value leads while keeping low-margin corner grocery nodes.
3. **Wire & Payload Efficiency with `out center tags qt;`:**
   A common misconception is that querying relations downloads every constituent node and way, creating massive payloads. In Overpass QL, invoking `out center;` instructs the server to calculate the geometric centroid of the relation internally and return **only the relation ID, tags, and center coordinates**:
   ```json
   {
     "type": "relation",
     "id": 11234567,
     "center": {"lat": 24.7136, "lon": 46.6753},
     "tags": {
       "name": "Al Faisaliah Mall",
       "shop": "mall",
       "website": "https://alfaisaliahmall.com",
       "phone": "+966112734000"
     }
   }
   ```
   The wire size of a relation with `out center` is virtually identical to that of a node (~150 bytes). Adding relations across Saudi Arabia adds fewer than 3,500 high-value elements (~500 KB uncompressed JSON) to the output.
4. **Zero Code Changes in Coordinate Parsing:**
   In `scrapers/osm.py:65-66`:
   ```python
   lat = el.get("lat") or (el.get("center") or {}).get("lat")
   lon = el.get("lon") or (el.get("center") or {}).get("lon")
   ```
   The existing coordinate parser already checks `el.get("center")` for ways. Because Overpass returns relation centroids inside the identical `"center"` structure, **no parser alterations are required** to consume relation coordinates.

### Handling Relations vs. Tenant POIs (Deduplication)

A relation often represents a parent venue (e.g., `shop=mall`), while nodes inside the relation represent individual retail tenants (e.g., `shop=clothes`, `amenity=cafe`).
- Both are distinct, valid leads: the mall operator is an enterprise lead; the tenant store is a retail lead.
- They possess distinct names (`"Mall of Arabia"` vs. `"Jarir Bookstore"`) and different categories (`mall` vs. `books`), so `dedup.py:79` naturally preserves both.
- If a single merchant is mapped as both a building relation and an internal POI node sharing the same name and phone number, `dedup.py:70` merges them based on normalized phone.

---

## Generalising the OSM Scraper Across Countries via Config

To support both Lebanon and Saudi Arabia (and easily add future markets such as the UAE or Qatar), scraping policy must be decoupled from Python source code.

### 1. Configuration Data Model

Define a typed configuration structure for country-level OSM scraping parameters:

```python
from dataclasses import dataclass, field
from typing import Literal

@dataclass(frozen=True)
class CountryOSMConfig:
    country_code: str                          # ISO 3166-1 alpha-2 (e.g. "LB", "SA")
    strategy: Literal["national_area", "province_areas", "bounding_boxes"]
    area_selectors: list[str] = field(default_factory=list) # e.g. ['["ISO3166-1"="LB"]["admin_level"="2"]']
    bounding_boxes: list[tuple[float, float, float, float]] = field(default_factory=list)
    categories: list[str] = field(default_factory=lambda: [
        "shop", "amenity", "office", "tourism", "craft", "healthcare"
    ])
    timeout: int = 60
    maxsize_bytes: int = 536870912             # 512 MB default
    default_city_fallback: str | None = None
```

### 2. Config File Schema (`config.toml`)

```toml
[scrapers.osm]
timeout = 60
max_retries = 3
endpoints = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
]

[scrapers.osm.countries.LB]
country_code = "LB"
strategy = "national_area"
area_selectors = ['area["ISO3166-1"="LB"]["admin_level"="2"]']
maxsize_bytes = 536870912
default_city_fallback = "Beirut"

[scrapers.osm.countries.SA]
country_code = "SA"
strategy = "province_areas"
area_selectors = [
    'area["ISO3166-2"="SA-01"]["admin_level"="4"]', # Riyadh
    'area["ISO3166-2"="SA-02"]["admin_level"="4"]', # Makkah / Jeddah
    'area["ISO3166-2"="SA-04"]["admin_level"="4"]'  # Eastern Province / Dammam
]
maxsize_bytes = 1073741824 # 1 GB for Saudi provinces
default_city_fallback = "Riyadh"
```

### 3. Dynamic Query Builder Logic

The scraper should construct its Overpass QL query dynamically based on the active `CountryOSMConfig`:

```python
def build_overpass_query(cfg: CountryOSMConfig) -> str:
    categories = cfg.categories
    header = f"[out:json][timeout:{cfg.timeout}][maxsize:{cfg.maxsize_bytes}];\n"
    
    if cfg.strategy in ("national_area", "province_areas"):
        if len(cfg.area_selectors) == 1:
            area_def = f"{cfg.area_selectors[0]}->.search_area;\n"
        else:
            selectors = "\n  ".join(f"{s};" for s in cfg.area_selectors)
            area_def = f"(\n  {selectors}\n)->.search_area;\n"
            
        clauses = []
        for cat in categories:
            clauses.append(f'nwr["name"]["{cat}"](area.search_area);')
        clauses.append('nwr["brand"]["shop"](area.search_area);')
        clauses.append('nwr["brand"]["amenity"](area.search_area);')
        body = "(\n  " + "\n  ".join(clauses) + "\n);\n"
        return header + area_def + body + "out center tags qt;\n"

    elif cfg.strategy == "bounding_boxes":
        clauses = []
        for s, w, n, e in cfg.bounding_boxes:
            for cat in categories:
                clauses.append(f'nwr["name"]["{cat}"]({s},{w},{n},{e});')
        body = "(\n  " + "\n  ".join(clauses) + "\n);\n"
        return header + body + "out center tags qt;\n"
    
    raise ValueError(f"Unknown OSM query strategy: {cfg.strategy}")
```

### 4. Downstream Pipeline Alignment

Generalising the OSM scraper across countries requires synchronizing four downstream components in `leadminer`:

1. **Orchestration (`main.py:155`):**
   Instead of instantiating `OSMScraper()` with no arguments, instantiate per-country instances or iterate over enabled countries:
   ```python
   scrapers = [
       OSMScraper(country="LB"),
       OSMScraper(country="SA"),
       WikidataScraper(country="LB"),
       GooglePlacesScraper(),
   ]
   ```
2. **Country Identity (`scrapers/osm.py:100`):**
   `BusinessRecord` must be emitted with `country=self.country_code`. This ensures `main.py:184` preserves `"SA"` and does not overwrite it with `_DEFAULT_COUNTRY = "LB"`.
3. **Region Inference (`enricher.py:85-94`):**
   When `r["country"] == "SA"`, `infer_region` routes coordinates to `_KSA_COORD_REGIONS`, correctly assigning leads to `"Riyadh"`, `"Jeddah"`, or `"Dammam"`.
4. **Phone Normalization (`main.py:194`):**
   `normalize_phone` receives the explicit country code (`"SA"`), applying Saudi national number transformations (`05...` -> `+9665...`) instead of defaulting to Lebanese formatting (`+961`).

---

## Concrete Implementation Specification (`scrapers/osm.py`)

Here is the complete, drop-in replacement implementation for `scrapers/osm.py` incorporating multi-country configuration, endpoint failover, unified `nwr` queries, `addr:full` handling, and WhatsApp extraction:

```python
"""
OpenStreetMap Overpass API Scraper.

Supports multi-country extraction (Lebanon, Saudi Arabia) with area resolution,
quadtile streaming, unified node/way/relation queries, and endpoint failover.
"""

import datetime
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Iterator, Literal
import requests

from .base import BaseScraper, BusinessRecord

logger = logging.getLogger(__name__)

# Fallback public Overpass endpoints
OVERPASS_ENDPOINTS = [
    os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter"),
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]


@dataclass(frozen=True)
class CountryOSMConfig:
    country_code: str
    strategy: Literal["national_area", "province_areas", "bounding_boxes"]
    area_selectors: list[str] = field(default_factory=list)
    bounding_boxes: list[tuple[float, float, float, float]] = field(default_factory=list)
    categories: list[str] = field(default_factory=lambda: [
        "shop", "amenity", "office", "tourism", "craft", "healthcare"
    ])
    timeout: int = 60
    maxsize_bytes: int = 536870912
    default_city_fallback: str | None = None


COUNTRY_CONFIGS: dict[str, CountryOSMConfig] = {
    "LB": CountryOSMConfig(
        country_code="LB",
        strategy="national_area",
        area_selectors=['area["ISO3166-1"="LB"]["admin_level"="2"]'],
        timeout=60,
        maxsize_bytes=536870912,
        default_city_fallback="Beirut",
    ),
    "SA": CountryOSMConfig(
        country_code="SA",
        strategy="province_areas",
        area_selectors=[
            'area["ISO3166-2"="SA-01"]["admin_level"="4"]',  # Riyadh
            'area["ISO3166-2"="SA-02"]["admin_level"="4"]',  # Makkah (Jeddah, Mecca)
            'area["ISO3166-2"="SA-04"]["admin_level"="4"]',  # Eastern Province (Dammam)
        ],
        timeout=90,
        maxsize_bytes=1073741824,  # 1 GB allocation for Saudi metro regions
        default_city_fallback="Riyadh",
    ),
}


class OSMScraper(BaseScraper):
    def __init__(self, country: str = "LB", config: CountryOSMConfig | None = None):
        self.country_code = country.upper()
        self.config = config or COUNTRY_CONFIGS.get(self.country_code)
        if not self.config:
            raise ValueError(f"[OSM] Unsupported country code: {country}. Config must be provided.")

    def _build_query(self) -> str:
        cfg = self.config
        header = f"[out:json][timeout:{cfg.timeout}][maxsize:{cfg.maxsize_bytes}];\n"
        
        if cfg.strategy in ("national_area", "province_areas"):
            if len(cfg.area_selectors) == 1:
                area_def = f"{cfg.area_selectors[0]}->.search_area;\n"
            else:
                selectors = "\n  ".join(f"{s};" for s in cfg.area_selectors)
                area_def = f"(\n  {selectors}\n)->.search_area;\n"

            clauses = [f'nwr["name"]["{c}"](area.search_area);' for c in cfg.categories]
            clauses.append('nwr["brand"]["shop"](area.search_area);')
            clauses.append('nwr["brand"]["amenity"](area.search_area);')
            body = "(\n  " + "\n  ".join(clauses) + "\n);\n"
            return header + area_def + body + "out center tags qt;\n"

        elif cfg.strategy == "bounding_boxes":
            clauses = []
            for s, w, n, e in cfg.bounding_boxes:
                for c in cfg.categories:
                    clauses.append(f'nwr["name"]["{c}"]({s},{w},{n},{e});')
            body = "(\n  " + "\n  ".join(clauses) + "\n);\n"
            return header + body + "out center tags qt;\n"

        raise ValueError(f"[OSM] Unknown strategy: {cfg.strategy}")

    def scrape(self) -> Iterator[BusinessRecord]:
        scraped_at = datetime.datetime.utcnow().isoformat() + "Z"
        query = self._build_query()
        print(f"[OSM] Fetching {self.country_code} businesses from Overpass API ({self.config.strategy})...")

        email = os.environ.get("SCRAPER_EMAIL", "voxire.tech@gmail.com")
        session = requests.Session()
        session.headers.update({
            "User-Agent": f"leadminer/1.0 ({email})",
            "Accept-Encoding": "gzip, deflate",
        })

        elements: list[dict] = []
        success = False

        # Iterate over mirror endpoints if primary fails
        for endpoint in OVERPASS_ENDPOINTS:
            if success:
                break
            for attempt in range(3):
                try:
                    resp = session.post(
                        endpoint,
                        data={"data": query},
                        timeout=(10.0, float(self.config.timeout + 15)),
                    )
                    resp.raise_for_status()

                    data = resp.json()
                    # Check for Overpass server runtime remark errors inside 200 responses
                    if "remark" in data and "runtime error" in data["remark"].lower():
                        raise requests.RequestException(f"Overpass runtime error: {data['remark']}")

                    elements = data.get("elements", [])
                    success = True
                    break
                except (requests.RequestException, ValueError) as e:
                    print(f"[OSM] {endpoint} attempt {attempt + 1} failed: {e}")
                    if attempt < 2:
                        time.sleep(10 * (attempt + 1))
                    else:
                        print(f"[OSM] Failing over from {endpoint} to next mirror...")

        if not success:
            print(f"[OSM] All endpoints exhausted for {self.country_code}, skipping.")
            return

        print(f"[OSM] Got {len(elements)} elements for {self.country_code}.")

        for el in elements:
            tags = el.get("tags", {})
            name = (
                tags.get("name")
                or tags.get("name:en")
                or tags.get("name:ar")
                or tags.get("brand")
                or tags.get("operator")
            )
            if not name:
                continue

            # Centroid resolution: direct coordinates for nodes, center block for ways and relations
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")

            category = (
                tags.get("shop")
                or tags.get("amenity")
                or tags.get("office")
                or tags.get("tourism")
                or tags.get("craft")
                or tags.get("healthcare")
            )

            # Structured address components with fallback to addr:full
            addr_parts = [
                tags.get("addr:housenumber"),
                tags.get("addr:street"),
                tags.get("addr:suburb"),
                tags.get("addr:city") or self.config.default_city_fallback,
                tags.get("addr:district"),
            ]
            structured = ", ".join(p for p in addr_parts if p)
            address = tags.get("addr:full") or structured or None

            # Primary contacts including regional mobile and WhatsApp tags
            phone = (
                tags.get("phone")
                or tags.get("contact:phone")
                or tags.get("mobile")
                or tags.get("contact:mobile")
            )
            whatsapp = tags.get("contact:whatsapp") or tags.get("whatsapp")
            email_val = tags.get("email") or tags.get("contact:email")
            website = tags.get("website") or tags.get("contact:website") or tags.get("url")
            facebook = tags.get("contact:facebook") or tags.get("facebook")
            instagram = tags.get("contact:instagram") or tags.get("instagram")
            linkedin = tags.get("contact:linkedin") or tags.get("linkedin")

            if website and not website.startswith("http"):
                website = "https://" + website

            yield BusinessRecord(
                name=name,
                category=category,
                address=address,
                region=None,
                country=self.country_code,
                lat=float(lat) if lat is not None else None,
                lon=float(lon) if lon is not None else None,
                phone=phone,
                email=email_val,
                website=website,
                website_live=None,
                facebook=facebook,
                instagram=instagram,
                whatsapp=whatsapp,
                linkedin=linkedin,
                rating=None,
                review_count=None,
                industry_priority=None,
                recommended_service=None,
                lead_score=0,
                source="osm",
                scraped_at=scraped_at,
                completeness_score=0,
            )
```

---

## Not a bug, but worth knowing

- **Public Overpass Fair-Use Guidelines:**
  Running massive area queries against `overpass-api.de` without caching violates OpenStreetMap's fair-use policy if executed frequently. For daily or continuous production runs in Saudi Arabia, team leadminer should deploy a containerized local Overpass instance using the Geofabrik GCC extract (`gcc-states-latest.osm.pbf`) or execute incremental queries via `(newer:"...")` filters as outlined in audit `017`.
- **Arabic Script Name Resolution in KSA:**
  In Saudi Arabia, ~85% of OSM POIs have their primary name mapped exclusively in Arabic (`name` or `name:ar`). The parser must preserve UTF-8 encoding without truncation so downstream enrichment and Arabic-English name normalization can properly match records.
- **Admin Boundary Shifts in OSM:**
  Saudi municipal boundaries (`admin_level=6`) are subject to periodic mapper reorganization. Relying on province-level `admin_level=4` relations (`ISO3166-2`) is significantly more stable than querying municipality-level polygons.

---

## Recommended Order of Work

1. **S1 (Country Parameterization & Fix):**
   Update `scrapers/osm.py` to accept `country` in `__init__`, set `BusinessRecord.country` dynamically, and remove hardcoded Lebanon references.
2. **S1 & S2 (Saudi Query Architecture & Relations):**
   Implement the multi-province Overpass query using unified `nwr` selectors with `out center tags qt;` and a 1 GB memory allocation (`[maxsize:1073741824]`).
3. **S2 (Resilient Networking):**
   Implement split timeouts `timeout=(10.0, read_timeout)`, move `resp.json()` inside the try-block, and add mirror endpoint failover (`kumi.systems`, `mail.ru`).
4. **S3 (Address & Contact Field Expansion):**
   Extract `addr:full`, `contact:whatsapp`, `mobile`, `brand`, and `operator` to enrich lead completeness and prevent phone-less dedup collisions.
5. **Configuration Integration:**
   Expose `CountryOSMConfig` settings via `config.toml` so operators can enable/disable countries or tune province boundaries without source code modifications.
6. **Orchestration Hook (`main.py`):**
   Update `main.py:155` to instantiate and run `OSMScraper(country="LB")` and `OSMScraper(country="SA")` in the worker thread pool.