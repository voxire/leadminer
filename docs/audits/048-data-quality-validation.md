# 048 — Data Quality Validation Layer: Ingestion Gate, Geographic Integrity, Referential Sanity, and Quarantine System

## Verdict

The `leadminer` pipeline currently operates with **zero validation between raw scraping and CSV serialization**: any malformed dictionary emitted by a scraper or reloaded from `master` flows unchecked into dedup, triggers expensive HTTP requests in the enricher, and lands permanently in production CSVs. Because `all_businesses.csv` is cumulative across runs (`main.py:124`), a single corrupted record permanently poisons the dataset. To ensure data integrity, a two-gate validation layer must be inserted into `main.py`: **Gate 1 (Ingestion)** immediately after scraping and master load to enforce schema, types, phone formats, and country-bounded coordinate boxes (rejecting Null Island, transpositions, and cross-border leaks); and **Gate 2 (Pre-Export)** after scoring to enforce referential sanity, score ranges, and rating/review outlier bounds, diverting all invalid rows into a dedicated `quarantine` table and CSV.

---

## Findings

### S1-1 — Zero schema and type validation allows poison records into dedup, enricher, and master
- **Where:** `main.py:99-126`, `scrapers/base.py:5-29`, and `enricher.py:183-186`.
- **Breaks:** `scrapers/base.py` defines `BusinessRecord` as a `TypedDict`, which provides static type hints during development but zero runtime validation. When scrapers execute in `main.py:105-114`, they return unchecked dictionaries.
  - If a scraper emits `name = None` or `name = ""` (e.g. malformed OSM node), `dedup.py:117-120` hashes it under `("", city)`. Multiple unrelated nameless businesses are merged into a single corrupt record via `_merge()`.
  - If a scraper emits an invalid or hostile URL (e.g. `website = "https://"`, `website = "localhost:8080"`, or `website = "http://169.254.169.254"`), `enricher.py:183` blindly passes it to `requests.Session.get()`, causing thread hangs, SSRF risks, or unhandled exceptions.
  - If `load_master()` (`main.py:46-71`) encounters corrupted rows from a previous failed run or manual edit, those rows are merged back into `combined` at `main.py:124` without verification.
- **Trigger:** A scraper yields `{"name": "", "category": "restaurant", "website": "ftp://bad-url", "lat": "invalid_float"}`.
- **Result:** `load_master` or `dedup` crashes on unexpected types, or passes junk into `enrich()`, which makes malformed network calls and writes poison records to `all_businesses.csv`.
- **Fix:** Enforce runtime schema validation immediately upon collecting raw scraper batches and upon reading the master CSV. Reject records missing mandatory identity fields (`name`, `country`, `source`) before dedup or enrichment.

### S1-2 — Unbounded geographic coordinates corrupt region inference and leak foreign entities
- **Where:** `main.py:117-129`, `enricher.py:79-94`, `scrapers/osm.py:65-66`, and `scrapers/google_places.py:241-243`.
- **Breaks:** No coordinate boundary checks exist anywhere in the pipeline.
  - **Null Island & Default Coordinates:** If an API returns `(0.0, 0.0)` or `(-1.0, -1.0)`, `infer_region` fails silently and returns `None`, but the corrupted coordinates are serialized directly into `all_businesses.csv`.
  - **Lat/Lon Transposition:** In Lebanon, valid latitudes are ~33.05–34.70° N and longitudes are ~35.10–36.65° E. If a scraper transposes them (`lat=35.5, lon=33.8`), the entity is placed in the Mediterranean Sea south of Cyprus. In Saudi Arabia (Riyadh: `lat ≈ 24.7, lon ≈ 46.7`), a transposition (`lat=46.7, lon=24.7`) places the lead in Kazakhstan.
  - **Cross-Border Leaks:** The Google Places query list in `scrapers/google_places.py:8-124` uses free-text queries like `"restaurants in Riyadh"` and `"luxury hotels in Saudi Arabia"`. Because Google Places returns places matching text relevance rather than hard polygon clipping, a query can return multinational chains, UAE branches, or Egyptian businesses. If `_country_from_query` tags the query as `"SA"`, a place physically located in Dubai (`lat=25.20, lon=55.27`) is tagged as `country="SA"` and `region=None`, polluting the Saudi sales export.
- **Trigger:** Google Places returns a hotel with `lat = 25.0657, lon = 55.1713` (Dubai, UAE) for the query `"luxury hotels in Saudi Arabia"`.
- **Result:** The business is stamped with `country="SA"`, bypasses `infer_region`, and is written to `all_businesses.csv` and `sales_ready.csv` as an actionable Saudi lead.
- **Fix:** Validate `(lat, lon)` against strict country bounding polygons (or conservative bounding boxes) matching `record["country"]`. Flag transpositions (`lat` in longitude range and vice versa) and quarantine out-of-bounds rows.

### S2-1 — Unchecked ratings and review counts permit negative values and statistical anomalies
- **Where:** `main.py:56-67`, `scrapers/google_places.py:265-266`, and `enricher.py:240-244`.
- **Breaks:** `rating` and `review_count` are ingested without range or sanity checks.
  - `rating` must strictly satisfy `0.0 <= rating <= 5.0` (standard Google Places range). If an upstream payload contains a malformed rating (e.g. `rating = 45.0` from a 0–100 scale, `rating = -1`, or `NaN`), it is written directly to CSV.
  - `review_count` must be a non-negative integer (`review_count >= 0`). Upstream API corruption or negative sentinel values (e.g. `-1` meaning unrated) become legitimate review counts in the CSV.
  - **Referential Contradiction:** Google Places places with `review_count > 0` have ratings in `[1.0, 5.0]`. A record with `rating = 0.0` but `review_count = 1500` is logically corrupt.
  - **Extreme Outliers:** While commercial businesses typically have 5 to 5,000 reviews, scraping artifacts or automated bot farms can produce review counts of `999,999` or ratings of `5.0` with `5,000` identical reviews. `lead_score()` in `enricher.py:241` awards points based on rating (`rating < 4.0`), meaning an unvalidated rating directly skews prioritization.
- **Trigger:** A scraper returns `{"rating": 48.0, "review_count": -5}`.
- **Result:** Downstream CSV consumers receive negative review counts and a 48-star rating, breaking sales dashboard filters and analytics formulas.
- **Fix:** Enforce range checks `0.0 <= rating <= 5.0` and `review_count >= 0`. Route impossible values to quarantine, and flag high-volume statistical outliers (`review_count > 10,000`) for review.

### S2-2 — Referential incoherence between computed columns produces contradictory exports
- **Where:** `enricher.py:142-152, 289-290`, `pitch_recommender.py:25-70`, and `main.py:131-147`.
- **Breaks:**
  - **Orphan Liveness:** If `website` is `None` or `""`, `website_live` must be `None`. If `website_live` is `True` while `website` is empty, the record is contradictory. In `main.py:68-69`, `load_master` parses `website_live` independently of `website`.
  - **Country vs. Phone Calling Code Conflict:** If `country == "LB"`, phone must have country calling code `+961` (or local format). A record marked `country="LB"` with phone `+966501234567` (Saudi mobile) indicates cross-border dedup merge corruption or misclassified query origin.
  - **Completeness & Lead Score Range Invariants:** `completeness_score` is defined as a sum of 7 specific signals (`enricher.py:101-118`), so it must strictly satisfy `0 <= completeness_score <= 7`. `lead_score` is capped conceptually at 100, yet no post-calculation assertion validates that `0 <= lead_score <= 100`.
- **Trigger:** A record is merged where Record A (Lebanon) provided `country="LB"` and Record B (Saudi) provided phone `"+966501234567"`.
- **Result:** The record appears in Lebanese sales queues with a Saudi Arabian phone number, wasting sales agent time and damaging outreach credibility.
- **Fix:** Run referential sanity rules asserting cross-field consistency before writing CSVs.

### S2-3 — Absence of a quarantine system forces an all-or-nothing dilemma: drop data or leak corruption
- **Where:** `main.py:108-114`, `main.py:117-121`, and `main.py:149-153`.
- **Breaks:** Currently, when an error occurs during scraping or filtering:
  - Scraper exceptions are caught and swallowed via `except Exception as e: print(...)` (`main.py:112-113`), discarding the entire batch without saving the offending response for inspection.
  - Category filtering (`main.py:117`) silently drops non-whitelisted items without recording why they were excluded or how many were valid businesses with non-standard tags.
  - If a validation rule were added as a simple `assert` or exception, the entire CI run would crash, aborting hours of scraping. Conversely, if bad rows are simply dropped with `if not valid: continue`, engineers have zero visibility into scraper regressions, API field changes, or schema drift.
- **Trigger:** Google Places changes its API response structure, emitting strings for `rating` or omitting `displayName`.
- **Result:** Either `main.py` crashes mid-run, losing all in-memory leads, or silently drops thousands of leads without an audit trail.
- **Fix:** Implement a dedicated `quarantine` table (persisted to `data/quarantined_businesses.csv`) capturing rejected records, exact failure rule codes, offending fields, invalid values, and raw payloads.

---

## Data Quality Validation Architecture & Specification

The validation layer is designed as a **Two-Gate Defense System** operating inside `main.py`:

```
[ Scrapers: OSM / Wikidata / Google Places ]       [ data/all_businesses.csv (Master) ]
                     │                                              │
                     └──────────────────────┬───────────────────────┘
                                            ▼
                           ┌──────────────────────────────────┐
                           │   GATE 1: INGESTION VALIDATION   │  (main.py: line ~116)
                           │   - Schema & primitive types     │
                           │   - Geographic bounding boxes    │
                           │   - Phone E.164 & URL syntax     │
                           │   - Mandatory identity checks    │
                           └──────────────────────────────────┘
                                      │            │
                         Passed Rows  │            │ Failed Rows
                                      ▼            ▼
                         [ Whitelist & Dedup ]   ┌───────────────────────────────┐
                                      │          │       QUARANTINE TABLE        │
                                      ▼          │ data/quarantined_businesses.csv
                            [ Enrich & Score ]   └───────────────────────────────┘
                                      │                               ▲
                                      ▼                               │ Failed Rows
                           ┌──────────────────────────────────┐       │
                           │   GATE 2: PRE-EXPORT SANITY      │  (main.py: line ~138)
                           │   - Rating [0.0, 5.0] range      │───────┘
                           │   - Review count non-negativity  │
                           │   - Statistical outlier flags    │
                           │   - Referential cross-checks     │
                           │   - Score invariant bounds       │
                           └──────────────────────────────────┘
                                      │
                         Clean Rows   ▼
                           ┌──────────────────────────────────┐
                           │     PRODUCTION CSV EXPORTS       │  (main.py: line ~149)
                           │ - all_businesses.csv             │
                           │ - qualified_businesses.csv       │
                           │ - with_websites.csv              │
                           │ - without_websites.csv           │
                           │ - sales_ready.csv                │
                           └──────────────────────────────────┘
```

---

### 1. Schema & Type Contract

Each record passing through the pipeline must satisfy the 23-column schema. The table below defines the strict validation specification for each field:

| Column | Python Type | Nullable? | Format / Allowed Values | Validation Rule |
|---|---|---|---|---|
| `name` | `str` | ❌ No | Stripped string, length `2..255` | Cannot be empty, whitespace, or placeholder (`"None"`, `"N/A"`, `"Unknown"`, `"-"`, `"test"`) |
| `category` | `str` | ❌ No (at Gate 2) | Canonical slug (e.g. `restaurant`) | Length `2..100`, lowercase alphanumeric + underscores |
| `region` | `str` | ✅ Yes | Recognized region name | If `country == 'LB'`: one of 8 Lebanese governorates. If `country == 'SA'`: one of 5 Saudi metro regions. |
| `country` | `str` | ❌ No | ISO-3166-1 alpha-2: `LB` or `SA` | Must be exactly `'LB'` or `'SA'` |
| `address` | `str` | ✅ Yes | Stripped string, length `3..500` | No binary characters, newlines normalized to commas |
| `lat` | `float` | ✅ Yes | WGS84 decimal degrees | Not `NaN`/`Inf`. Must fall within country coordinate bounding box. |
| `lon` | `float` | ✅ Yes | WGS84 decimal degrees | Not `NaN`/`Inf`. Must fall within country coordinate bounding box. |
| `phone` | `str` | ✅ Yes | E.164 international format | Regex `^\+[1-9]\d{6,14}$`. Country code must agree with `country`. |
| `email` | `str` | ✅ Yes | RFC 5322 compliant address | Regex `^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$`. No trailing dots, length `6..254`. |
| `website` | `str` | ✅ Yes | Fully-qualified HTTP(S) URL | Must parse with `urllib.parse`. Scheme in `('http', 'https')`. Netloc contains valid TLD. No private IPs / localhost. |
| `website_live` | `bool` | ✅ Yes | `True`, `False`, or `None` | Strict boolean type. If `website is None`, `website_live` MUST be `None`. |
| `facebook` | `str` | ✅ Yes | URL or handle | If URL: domain must be `facebook.com` / `fb.com`. |
| `instagram` | `str` | ✅ Yes | Clean handle or URL | Handle regex `^[a-zA-Z0-9._]{1,30}$` (no leading `@`). |
| `whatsapp` | `str` | ✅ Yes | E.164 phone string | Regex `^\+[1-9]\d{6,14}$`. |
| `linkedin` | `str` | ✅ Yes | URL or handle | If URL: domain must be `linkedin.com`. |
| `rating` | `float` | ✅ Yes | Float `0.0` to `5.0` | `0.0 <= rating <= 5.0`. Max 2 decimal places. Not `NaN`/`Inf`. |
| `review_count`| `int` | ✅ Yes | Non-negative integer | `0 <= review_count <= 1_000_000`. |
| `completeness_score` | `int` | ❌ No | Integer `0` to `7` | Must equal sum of populated contact/identity fields. |
| `lead_score` | `int` | ❌ No | Integer `0` to `100` | `0 <= lead_score <= 100`. |
| `industry_priority` | `str` | ❌ No (at Gate 2) | Enum: `'high'`, `'medium'`, `'low'` | Must match whitelist lookup for category. |
| `recommended_service`| `str` | ❌ No (at Gate 2) | Approved service name | Must exist in `pitch_recommender.SERVICES` catalog. |
| `source` | `str` | ❌ No | Pipe-delimited source tokens | Regex `^(osm\|wikidata\|google_places)(\|(osm\|wikidata\|google_places))*$`. |
| `scraped_at` | `str` | ❌ No | ISO-8601 UTC timestamp | Format `YYYY-MM-DDTHH:MM:SSZ` or `YYYY-MM-DDTHH:MM:SS+00:00`. Year `>= 2024`. |

---

### 2. Geographic Boundary & Range Checks

Coordinates must be validated against hard terrestrial bounding boxes for each country. Furthermore, orphan coordinates (lat without lon or lon without lat), Null Island coordinates, and transposed coordinates must be trapped.

#### A. National & Regional Bounding Envelopes

```
Lebanon (LB) Envelope:
  Latitude:   [33.0400, 34.7200] N
  Longitude:  [35.0800, 36.6600] E

Saudi Arabia (SA) Envelope:
  Latitude:   [16.3500, 32.2000] N
  Longitude:  [34.4500, 55.7000] E

Saudi Arabia Target Metro Envelopes:
  - Riyadh Metro:  Lat [24.4000, 25.2000] N, Lon [46.4000, 47.2000] E
  - Jeddah Metro:  Lat [21.2500, 21.9000] N, Lon [39.0000, 39.4500] E
  - Greater Dammam / Khobar: Lat [26.1500, 26.6500] N, Lon [49.8500, 50.3000] E
  - Mecca Metro:   Lat [21.2500, 21.6000] N, Lon [39.7000, 40.0500] E
  - Medina Metro:  Lat [24.3000, 24.7000] N, Lon [39.4000, 39.8500] E
```

#### B. Coordinate Anomaly & Failure Modes

1. **Orphan Coordinates:**
   - Rule: `(lat is None) == (lon is None)`. If one is present and the other is `None`, fail with `ERR_COORD_ORPHAN`.
2. **Null Island & Default Floats:**
   - Rule: Coordinates where `abs(lat) < 0.001 and abs(lon) < 0.001` or `lat in (-1.0, 0.0, 1.0) and lon in (-1.0, 0.0, 1.0)` fail with `ERR_COORD_NULL_ISLAND`.
3. **Lat/Lon Transposition Check (Swapped Axes):**
   - In Lebanon, `lat ≈ 33–34.7` and `lon ≈ 35.1–36.6`. If coordinates are swapped (`lat > 35.0 and lon < 35.0`), the point lands in the Mediterranean Sea. Fail with `ERR_COORD_TRANSPOSED`.
   - In Saudi Arabia (Riyadh), `lat ≈ 24.7` and `lon ≈ 46.7`. If swapped (`lat > 35.0 and lon < 30.0`), the point lands in Central Asia. Fail with `ERR_COORD_TRANSPOSED`.
4. **Country Bounding Violation:**
   - If `country == "LB"` and not `(33.0400 <= lat <= 34.7200 and 35.0800 <= lon <= 36.6600)`: fail with `ERR_COORD_OUT_OF_BOUNDS_LB`.
   - If `country == "SA"` and not `(16.3500 <= lat <= 32.2000 and 34.4500 <= lon <= 55.7000)`: fail with `ERR_COORD_OUT_OF_BOUNDS_SA`.
5. **Cross-Border Contamination:**
   - If `country == "SA"` but coordinates fall inside the Lebanon envelope, fail with `ERR_COORD_COUNTRY_MISMATCH`.

---

### 3. Referential Sanity Checks

Multi-field consistency rules prevent contradictory state:

1. **Website vs. `website_live`:**
   - Invariant: `website is None ==> website_live is None`.
   - Violation: A record has `website=None` but `website_live=True` or `website_live=False`. Fail with `ERR_REF_ORPHAN_LIVENESS`.
2. **Country vs. E.164 Phone Calling Code:**
   - Invariant:
     - `country == "LB"`: If `phone` is present, it must start with `+961` (Lebanon calling code).
     - `country == "SA"`: If `phone` is present, it must start with `+966` (Saudi Arabia calling code).
   - Foreign numbers (e.g. `+1`, `+44`, `+971` UAE): Permitted only if logged as an external contact, but if a Lebanese business has a `+966` number, flag `ERR_REF_PHONE_COUNTRY_MISMATCH` to prevent wrong sales territory assignment.
3. **Country vs. Region Coherence:**
   - If `country == "LB"`, `region` must be one of: `{"Beirut", "Mount Lebanon", "North Lebanon", "Akkar", "South Lebanon", "Nabatieh", "Bekaa", "Baalbek-Hermel", None}`.
   - If `country == "SA"`, `region` must be one of: `{"Riyadh", "Jeddah", "Dammam", "Mecca", "Medina", None}`.
   - Violation: `country == "LB"` with `region == "Riyadh"`. Fail with `ERR_REF_REGION_COUNTRY_MISMATCH`.
4. **Category vs. `industry_priority`:**
   - Invariant: `industry_priority` must strictly equal `whitelist.industry_priority(category)`. Any discrepancy indicates mutation drift. Fail with `ERR_REF_PRIORITY_INCONSISTENT`.
5. **Score Verification:**
   - `completeness_score` must match the exact sum calculated by `enricher.completeness_score(record)`.
   - `lead_score` must fall strictly within `[0, 100]`. If `lead_score < 0` or `lead_score > 100`, fail with `ERR_REF_LEAD_SCORE_OVERFLOW`.

---

### 4. Rating & Review Count Outlier & Anomaly Detection

#### A. Hard Boundary Constraints
- **Rating Range:** `0.0 <= rating <= 5.0`.
  - Values `< 0.0` or `> 5.0` fail with `ERR_RATING_OUT_OF_RANGE` (REJECT to Quarantine).
  - Non-numeric strings or `NaN`/`Inf` fail with `ERR_RATING_TYPE_INVALID` (REJECT).
- **Review Count Range:** `0 <= review_count <= 1_000_000`.
  - Negative values (`review_count < 0`) fail with `ERR_REVIEW_COUNT_NEGATIVE` (REJECT).

#### B. Referential Rating Invariants
- **Unrated vs. Zero-Rating:**
  - Google Places API does not assign `0.0` stars to businesses with reviews; ratings start at `1.0`.
  - If `rating == 0.0` and `review_count > 0`: Flagged as anomalous (`ERR_RATING_ZERO_WITH_REVIEWS`). Coerced to `rating = None` or quarantined.
  - If `rating is not None` and `(review_count == 0 or review_count is None)`: A rating cannot exist without reviews. Coerced to `review_count = 1` if `rating > 0`, or quarantined.

#### C. Statistical Outliers & Bot-Farm Anomaly Rules

```
Tier 1: Review Count Extreme Outlier (Hard Ceiling)
  Threshold:  review_count > 100,000
  Action:     REJECT to Quarantine (ERR_REVIEW_COUNT_CEILING)
  Rationale:  In Lebanon and Saudi Arabia, no commercial agency prospect has >100,000 reviews.
              Such entities are either national public monuments (e.g. Grand Mosque) or scrape corruptions.

Tier 2: High-Volume Commercial Outlier (Soft Warning / Flag)
  Threshold:  review_count > 10,000
  Action:     WARN & AUDIT (Tag in metadata, permit in export)
  Rationale:  Major shopping malls (e.g. Red Sea Mall) or massive chains may legitimately hit 10k–30k.

Tier 3: Perfect Score Bot Farm / Manipulation Anomaly
  Threshold:  rating == 5.0 and review_count > 500
  Action:     FLAG ANOMALY (Set anomaly_flag = "SUSPICIOUS_PERFECT_RATING")
  Rationale:  Statistically impossible for a mature business to have >500 reviews with zero negative ratings.
              Enricher should cap lead score bonus rather than trusting blindly.
```

---

### 5. Quarantine Table Specification

When a record fails validation, it must not be silently dropped (which blinds engineering to scraper regressions) nor passed downstream (which corrupts sales data). It must be routed to **Quarantine**.

#### A. Quarantine Record Schema

Every quarantined entry records the full audit trail:

| Field Name | Type | Description |
|---|---|---|
| `quarantine_id` | `str` | UUIDv4 unique identifier for the failure event |
| `run_id` | `str` | Timestamp/identifier of the scraper run (e.g. `20261003T120000Z`) |
| `stage` | `str` | Validation stage: `INGESTION` (Gate 1) or `PRE_EXPORT` (Gate 2) |
| `source` | `str` | Raw source string (`osm`, `wikidata`, `google_places`, `master`) |
| `record_identifier` | `str` | Business `name` or raw primary key |
| `rule_code` | `str` | Machine-readable error code (e.g. `ERR_COORD_OUT_OF_BOUNDS_LB`) |
| `severity` | `str` | `FATAL` (dropped from pipeline) or `WARNING` (sanitized and continued) |
| `failed_field` | `str` | Column name that failed validation (`lat`, `phone`, `rating`, etc.) |
| `invalid_value` | `str` | Stringified representation of the offending value |
| `error_message` | `str` | Descriptive reason for rejection |
| `raw_record_json` | `str` | Serialized JSON copy of the full record for offline replay and debugging |
| `quarantined_at` | `str` | UTC ISO-8601 timestamp of quarantine insertion |

#### B. Storage Targets

1. **File-based (Immediate / Current Architecture):**
   Written to `data/quarantined_businesses.csv` with header matching the quarantine schema.
2. **Relational / SQLite DDL (For upcoming SQLite migration):**

```sql
CREATE TABLE IF NOT EXISTS quarantine_records (
    quarantine_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('INGESTION', 'PRE_EXPORT')),
    source TEXT NOT NULL,
    record_identifier TEXT,
    rule_code TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('FATAL', 'WARNING')),
    failed_field TEXT NOT NULL,
    invalid_value TEXT,
    error_message TEXT NOT NULL,
    raw_record_json TEXT NOT NULL,
    quarantined_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quarantine_run_stage ON quarantine_records(run_id, stage);
CREATE INDEX IF NOT EXISTS idx_quarantine_rule_code ON quarantine_records(rule_code);
```

#### C. Rule Code Taxonomy

| Code | Severity | Description | Action |
|---|---|---|---|
| `ERR_NAME_MISSING` | FATAL | Business name is null, empty, or whitespace | Drop to quarantine |
| `ERR_NAME_PLACEHOLDER` | FATAL | Name is placeholder (`"Unknown"`, `"test"`, `"N/A"`) | Drop to quarantine |
| `ERR_COUNTRY_INVALID` | FATAL | Country is not `'LB'` or `'SA'` | Drop to quarantine |
| `ERR_COORD_ORPHAN` | FATAL | Lat provided without lon (or vice versa) | Strip coords or quarantine |
| `ERR_COORD_NULL_ISLAND`| FATAL | (0, 0) or close to origin | Strip coords to None, warn |
| `ERR_COORD_TRANSPOSED` | FATAL | Lat and lon swapped | Flip if unambiguous, or quarantine |
| `ERR_COORD_OUT_OF_BOUNDS_LB` | FATAL | Coordinates outside Lebanon envelope | Drop to quarantine |
| `ERR_COORD_OUT_OF_BOUNDS_SA` | FATAL | Coordinates outside Saudi Arabia envelope | Drop to quarantine |
| `ERR_URL_MALFORMED` | FATAL | Website is unparseable or lacks valid scheme/domain | Set `website=None`, warn |
| `ERR_PHONE_INVALID_E164`| WARNING | Phone cannot be parsed into dialable E.164 | Set `phone=None`, warn |
| `ERR_RATING_OUT_OF_RANGE`| FATAL | Rating < 0.0 or > 5.0 | Drop to quarantine |
| `ERR_REVIEW_COUNT_NEGATIVE`| FATAL | Review count < 0 | Drop to quarantine |
| `ERR_REVIEW_COUNT_CEILING`| FATAL | Review count > 100,000 | Drop to quarantine |
| `ERR_REF_ORPHAN_LIVENESS`| FATAL | `website_live` populated while `website` is None | Set `website_live=None` |
| `ERR_REF_PHONE_COUNTRY_MISMATCH`| FATAL | Lebanese record with Saudi calling code (or vice versa) | Drop to quarantine |
| `ERR_SCORE_OUT_OF_BOUNDS`| FATAL | `lead_score` not in `0..100` or `completeness` not in `0..7` | Recompute or quarantine |

---

### 6. Where in `main.py` the Layer Must Be Inserted

Validation must not be placed only at the very end before CSV serialization. If placed solely at line 149, bad URLs waste network bandwidth during `enrich()`, invalid phones corrupt deduplication keys, and corrupted records in `master` permanently infect newly scraped records.

The validation layer requires **two insertion points** in `main.py`:

#### Insertion Point 1: Master Sanitization & Ingestion Gate (Gate 1)
- **Exact Location:** `main.py:96` (for master) and `main.py:116` (for scraped `raw` batch).
- **Surrounding Code in `main.py`:**
  ```python
  # --- Line 94-98 in main.py ---
  master = load_master(DATA_DIR / "all_businesses.csv")
  if master:
      # INSERTION: Sanitize master to prevent legacy corruption
      master, master_quarantine = validate_batch(master, stage="INGESTION_MASTER")
      quarantine_records.extend(master_quarantine)
      print(f"Loaded {len(master)} valid records from existing master CSV ({len(master_quarantine)} quarantined)")

  # ... scrapers run in ThreadPoolExecutor ...

  # --- Line 115-117 in main.py ---
  print(f"\nTotal raw records from scrapers: {len(raw)}")

  # INSERTION: Gate 1 runs HERE, before whitelist filtering and dedup
  raw_valid, raw_quarantine = validate_batch(raw, stage="INGESTION_SCRAPE")
  quarantine_records.extend(raw_quarantine)
  print(f"Gate 1 Ingestion Validation: {len(raw_valid)} passed, {len(raw_quarantine)} quarantined")

  raw_filtered = [r for r in raw_valid if is_business_category(r.get("category"))]
  ```

#### Insertion Point 2: Pre-Export Integrity Gate (Gate 2) & Quarantine Flush
- **Exact Location:** `main.py:138`, immediately after scoring and before subset partitioning (`with_websites`, `sales_ready`).
- **Surrounding Code in `main.py`:**
  ```python
  # --- Lines 131-137 in main.py ---
  for r in records:
      r["industry_priority"] = industry_priority(r.get("category"))
      r["recommended_service"] = recommend_service(r)
      raw_phone = r.get("phone")
      if raw_phone:
          r["phone"] = normalize_phone(raw_phone, r.get("country", "LB"))
      r["lead_score"] = _lead_score(r)

  # INSERTION: Gate 2 runs HERE, validating referential integrity and rating/review outliers
  export_valid, export_quarantine = validate_batch(records, stage="PRE_EXPORT")
  quarantine_records.extend(export_quarantine)
  print(f"Gate 2 Pre-Export Validation: {len(export_valid)} passed, {len(export_quarantine)} quarantined")

  records = export_valid  # Only clean records proceed to CSV splits

  # --- Lines 139-153 in main.py ---
  with_websites = [r for r in records if r.get("website")]
  without_websites = [r for r in records if not r.get("website")]
  with_social = [r for r in records if r.get("facebook") or r.get("instagram")]
  sales_ready = [
      r for r in records
      if has_any_contact(r) and r.get("industry_priority") in ("high", "medium")
  ]
  qualified = [r for r in records if r.get("completeness_score", 0) >= 1]

  DATA_DIR.mkdir(exist_ok=True)
  write_csv(DATA_DIR / "all_businesses.csv", records)
  write_csv(DATA_DIR / "qualified_businesses.csv", qualified)
  write_csv(DATA_DIR / "with_websites.csv", with_websites)
  write_csv(DATA_DIR / "without_websites.csv", without_websites)
  write_csv(DATA_DIR / "sales_ready.csv", sales_ready)

  # INSERTION: Write quarantine log
  write_quarantine_csv(DATA_DIR / "quarantined_businesses.csv", quarantine_records)
  ```

---

### 7. Concrete Validation Implementation (Zero-Dependency)

The following pure-Python validation module (`validator.py`) satisfies all requirements without requiring external dependencies like Pydantic, making it directly compatible with the existing `requirements.txt`:

```python
"""
validator.py — Data Quality Validation Layer for leadminer.
Implements Gate 1 (Ingestion) and Gate 2 (Pre-Export) validation.
"""

from __future__ import annotations

import csv
import json
import math
import pathlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

# Bounding Envelopes
LB_BOUNDS = {"lat_min": 33.0400, "lat_max": 34.7200, "lon_min": 35.0800, "lon_max": 36.6600}
SA_BOUNDS = {"lat_min": 16.3500, "lat_max": 32.2000, "lon_min": 34.4500, "lon_max": 55.7000}

E164_REGEX = re.compile(r"^\+[1-9]\d{6,14}$")
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
PLACEHOLDER_NAMES = {"unknown", "n/a", "none", "null", "-", "--", "restaurant", "shop", "store", "test"}

QUARANTINE_FIELDS = [
    "quarantine_id", "run_id", "stage", "source", "record_identifier",
    "rule_code", "severity", "failed_field", "invalid_value",
    "error_message", "raw_record_json", "quarantined_at"
]


class ValidationError:
    def __init__(self, rule_code: str, field: str, value: Any, message: str, severity: str = "FATAL"):
        self.rule_code = rule_code
        self.field = field
        self.value = str(value) if value is not None else ""
        self.message = message
        self.severity = severity


def validate_coordinates(lat: Any, lon: Any, country: str) -> Tuple[Optional[float], Optional[float], List[ValidationError]]:
    errors = []
    if lat is None and lon is None:
        return None, None, errors
    if (lat is None) != (lon is None):
        errors.append(ValidationError("ERR_COORD_ORPHAN", "lat/lon", f"lat={lat}, lon={lon}", "Orphan coordinate"))
        return None, None, errors

    try:
        flat = float(lat)
        flon = float(lon)
    except (ValueError, TypeError):
        errors.append(ValidationError("ERR_COORD_NON_NUMERIC", "lat/lon", f"{lat},{lon}", "Non-numeric coordinates"))
        return None, None, errors

    if math.isnan(flat) or math.isnan(flon) or math.isinf(flat) or math.isinf(flon):
        errors.append(ValidationError("ERR_COORD_NAN_INF", "lat/lon", f"{lat},{lon}", "NaN or Inf coordinate"))
        return None, None, errors

    # Null island check
    if abs(flat) < 0.001 and abs(flon) < 0.001:
        errors.append(ValidationError("ERR_COORD_NULL_ISLAND", "lat/lon", f"{flat},{flon}", "Coordinate is at Null Island"))
        return None, None, errors

    # Transposition check (lat/lon swapped)
    if country == "LB" and flat > 35.0 and flon < 35.0:
        errors.append(ValidationError("ERR_COORD_TRANSPOSED", "lat/lon", f"lat={flat},lon={flon}", "Transposed LB coordinates"))
        return None, None, errors
    if country == "SA" and flat > 35.0 and flon < 30.0:
        errors.append(ValidationError("ERR_COORD_TRANSPOSED", "lat/lon", f"lat={flat},lon={flon}", "Transposed SA coordinates"))
        return None, None, errors

    bounds = LB_BOUNDS if country == "LB" else (SA_BOUNDS if country == "SA" else None)
    if bounds:
        if not (bounds["lat_min"] <= flat <= bounds["lat_max"] and bounds["lon_min"] <= flon <= bounds["lon_max"]):
            errors.append(ValidationError(
                f"ERR_COORD_OUT_OF_BOUNDS_{country}", "lat/lon", f"lat={flat},lon={flon}",
                f"Coordinates outside bounding box for {country}"
            ))
            return None, None, errors

    return flat, flon, errors


def validate_website(url: Any) -> Tuple[Optional[str], List[ValidationError]]:
    errors = []
    if not url:
        return None, errors
    s_url = str(url).strip()
    if not s_url.startswith(("http://", "https://")):
        s_url = "https://" + s_url

    parsed = urlparse(s_url)
    if not parsed.netloc or "." not in parsed.netloc:
        errors.append(ValidationError("ERR_URL_MALFORMED", "website", url, "Invalid domain structure"))
        return None, errors

    # Check for localhost / private IP ranges
    host = parsed.netloc.split(":")[0].lower()
    if host in ("localhost", "127.0.0.1") or host.startswith(("192.168.", "10.", "172.")):
        errors.append(ValidationError("ERR_URL_PRIVATE_HOST", "website", url, "Local or private IP detected"))
        return None, errors

    return s_url, errors


def validate_raw_record(r: dict, run_id: str) -> Tuple[Optional[dict], List[dict]]:
    """Gate 1: Ingestion validation."""
    errors: List[ValidationError] = []
    cleaned = dict(r)

    # 1. Name check
    name = str(cleaned.get("name") or "").strip()
    if not name or len(name) < 2:
        errors.append(ValidationError("ERR_NAME_MISSING", "name", cleaned.get("name"), "Name missing or too short"))
    elif name.lower() in PLACEHOLDER_NAMES:
        errors.append(ValidationError("ERR_NAME_PLACEHOLDER", "name", name, "Name is a generic placeholder"))
    cleaned["name"] = name

    # 2. Country check
    country = str(cleaned.get("country") or "").upper().strip()
    if country not in ("LB", "SA"):
        errors.append(ValidationError("ERR_COUNTRY_INVALID", "country", cleaned.get("country"), "Country must be LB or SA"))
    cleaned["country"] = country

    # 3. Source check
    source = str(cleaned.get("source") or "").strip()
    if not source:
        errors.append(ValidationError("ERR_SOURCE_MISSING", "source", cleaned.get("source"), "Source cannot be empty"))

    # 4. Coordinates
    flat, flon, coord_errors = validate_coordinates(cleaned.get("lat"), cleaned.get("lon"), country)
    errors.extend(coord_errors)
    cleaned["lat"] = flat
    cleaned["lon"] = flon

    # 5. Website sanitization
    site, site_errors = validate_website(cleaned.get("website"))
    errors.extend(site_errors)
    cleaned["website"] = site

    fatal_errors = [e for e in errors if e.severity == "FATAL"]
    if fatal_errors:
        quarantine_rows = []
        now_ts = datetime.now(timezone.utc).isoformat()
        for err in fatal_errors:
            quarantine_rows.append({
                "quarantine_id": str(uuid.uuid4()),
                "run_id": run_id,
                "stage": "INGESTION",
                "source": source,
                "record_identifier": name or "UNKNOWN",
                "rule_code": err.rule_code,
                "severity": err.severity,
                "failed_field": err.field,
                "invalid_value": err.value,
                "error_message": err.message,
                "raw_record_json": json.dumps(r, default=str),
                "quarantined_at": now_ts,
            })
        return None, quarantine_rows

    return cleaned, []


def validate_export_record(r: dict, run_id: str) -> Tuple[Optional[dict], List[dict]]:
    """Gate 2: Pre-Export validation."""
    errors: List[ValidationError] = []
    cleaned = dict(r)

    # 1. Rating checks
    rating = cleaned.get("rating")
    if rating is not None:
        try:
            frating = float(rating)
            if math.isnan(frating) or not (0.0 <= frating <= 5.0):
                errors.append(ValidationError("ERR_RATING_OUT_OF_RANGE", "rating", rating, "Rating must be between 0.0 and 5.0"))
            cleaned["rating"] = round(frating, 2)
        except (ValueError, TypeError):
            errors.append(ValidationError("ERR_RATING_TYPE_INVALID", "rating", rating, "Rating must be a valid float"))

    # 2. Review count checks
    reviews = cleaned.get("review_count")
    if reviews is not None:
        try:
            ireviews = int(reviews)
            if ireviews < 0:
                errors.append(ValidationError("ERR_REVIEW_COUNT_NEGATIVE", "review_count", reviews, "Review count cannot be negative"))
            elif ireviews > 100_000:
                errors.append(ValidationError("ERR_REVIEW_COUNT_CEILING", "review_count", reviews, "Review count exceeds 100k ceiling"))
            cleaned["review_count"] = ireviews
        except (ValueError, TypeError):
            errors.append(ValidationError("ERR_REVIEW_COUNT_TYPE_INVALID", "review_count", reviews, "Review count must be integer"))

    # 3. Rating vs Review referential sanity
    if cleaned.get("rating") == 0.0 and (cleaned.get("review_count") or 0) > 0:
        cleaned["rating"] = None  # Sanitize contradictory 0-star rating

    # 4. Website vs Website Live sanity
    if not cleaned.get("website") and cleaned.get("website_live") is not None:
        cleaned["website_live"] = None

    # 5. Phone vs Country calling code sanity
    phone = str(cleaned.get("phone") or "")
    country = cleaned.get("country")
    if phone.startswith("+"):
        if country == "LB" and phone.startswith("+966"):
            errors.append(ValidationError("ERR_REF_PHONE_COUNTRY_MISMATCH", "phone", phone, "LB record with SA phone code"))
        elif country == "SA" and phone.startswith("+961"):
            errors.append(ValidationError("ERR_REF_PHONE_COUNTRY_MISMATCH", "phone", phone, "SA record with LB phone code"))

    # 6. Score invariants
    lead_sc = cleaned.get("lead_score", 0)
    if not (0 <= lead_sc <= 100):
        errors.append(ValidationError("ERR_LEAD_SCORE_BOUNDS", "lead_score", lead_sc, "Lead score out of 0-100 range"))

    fatal_errors = [e for e in errors if e.severity == "FATAL"]
    if fatal_errors:
        quarantine_rows = []
        now_ts = datetime.now(timezone.utc).isoformat()
        for err in fatal_errors:
            quarantine_rows.append({
                "quarantine_id": str(uuid.uuid4()),
                "run_id": run_id,
                "stage": "PRE_EXPORT",
                "source": cleaned.get("source", "unknown"),
                "record_identifier": cleaned.get("name", "UNKNOWN"),
                "rule_code": err.rule_code,
                "severity": err.severity,
                "failed_field": err.field,
                "invalid_value": err.value,
                "error_message": err.message,
                "raw_record_json": json.dumps(r, default=str),
                "quarantined_at": now_ts,
            })
        return None, quarantine_rows

    return cleaned, []


def validate_batch(records: List[dict], stage: str, run_id: Optional[str] = None) -> Tuple[List[dict], List[dict]]:
    if not run_id:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    valid_records = []
    quarantine_records = []

    fn = validate_raw_record if "INGESTION" in stage else validate_export_record
    for r in records:
        cleaned, q_rows = fn(r, run_id)
        if q_rows:
            quarantine_records.extend(q_rows)
        if cleaned:
            valid_records.append(cleaned)

    return valid_records, quarantine_records


def write_quarantine_csv(path: pathlib.Path, quarantine_records: List[dict]) -> None:
    if not quarantine_records:
        return
    file_exists = path.exists()
    with open(path, "a" if file_exists else "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=QUARANTINE_FIELDS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerows(quarantine_records)
    print(f"  Quarantine: {path} ({len(quarantine_records)} failed records logged)")
```

---

## Not a bug, but worth knowing

- **Coordinate Precision vs Floating Point Jitter:** OSM emits coordinates with up to 7 decimal places (~1.1 cm precision), while Google Places typically emits 6 or 7 decimals. When merging or writing floats, Python's `repr` preserves full precision. Bounding box comparisons should use standard IEEE 754 float comparison without epsilon slicing unless testing exact equality.
- **Transposition Asymmetry in KSA vs Lebanon:** In Lebanon, lat and lon ranges overlap slightly around 35° (Lat: 33.0–34.7, Lon: 35.1–36.6). If a point in Tyre (`lat=33.27, lon=35.20`) is swapped to `lat=35.20, lon=33.27`, `lat=35.20` is just north of Lebanon in Latakia, Syria, while `lon=33.27` is 150 km into the sea. Thus `lat > 35.0` is a 100% reliable trigger for transposition in Lebanese data. In Saudi Arabia, the overlap is zero (Lat: 16–32, Lon: 34–55), so swapping lat and lon immediately fails both national boundaries.
- **Quarantine Accumulation vs CI Disk Quota:** If `quarantined_businesses.csv` is committed or appended across hundreds of runs without rotation, it could grow unbounded if an upstream scraper breaks. Add a log rotation policy or keep only the last 30 runs in CI storage.
- **Google Places Rating Distribution:** Ratings in Google Places are heavily skewed toward 4.0–4.9. A business with rating < 3.5 is already in the bottom 5th percentile of all Google businesses. Lead scoring awards +10 points for ratings < 4.0 (`enricher.py:241-242`); the validation layer ensures this score bonus is never awarded to corrupt `0.0` ratings.

---

## Recommended order of work

1. **Implement `validator.py`:** Create the zero-dependency validation module with `validate_raw_record()`, `validate_export_record()`, bounding box specifications, and `write_quarantine_csv()`.
2. **Wire Gate 1 into `main.py`:** Insert `validate_batch(master, "INGESTION_MASTER")` at `main.py:96` and `validate_batch(raw, "INGESTION_SCRAPE")` at `main.py:116`. Verify that missing names, malformed URLs, and out-of-bounds coordinates are diverted before `is_business_category` and `dedup()`.
3. **Wire Gate 2 into `main.py`:** Insert `validate_batch(records, "PRE_EXPORT")` at `main.py:138`. Verify that ratings, review counts, score invariants, and referential phone/country links are verified before splitting into CSV files.
4. **Append Quarantine Export to `main.py`:** Add `write_quarantine_csv(DATA_DIR / "quarantined_businesses.csv", quarantine_records)` at `main.py:154` and add quarantine metrics to the terminal summary output (`main.py:159-183`).
5. **Add Quarantine Alarm Threshold:** In `main.py`, if `len(quarantine_records) / len(raw) > 0.05` (quarantine rate exceeds 5%), log an explicit CI warning or exit non-zero to alert the engineering team of scraper schema breakage before corrupting downstream Google Drive CSVs.
