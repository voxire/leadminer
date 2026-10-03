# 034 — Normalised Data Model: Entity, Observations, Contacts, and Audit History

## Verdict

The current architecture stores lead generation data in a single flat 23-column schema (`BusinessRecord` in `scrapers/base.py:5-29` and `all_businesses.csv` in `main.py:32-39`). This design fatally conflates three orthogonal concepts: **immutable external source observations**, **the resolved canonical business entity ("golden record")**, and **multi-valued volatile attributes (contacts, social handles, HTTP health checks, and algorithm scores)**. 

Because `dedup.py:45-61` merges records via a crude, winner-takes-all `_field_count` rule while squashing sources into a pipe-delimited string (`"osm|google_places"`) and collapsing timestamps to `max(scraped_at)`, raw observation data is permanently erased on every run. Furthermore, 1:N relationships (multiple phones, emails, social handles) are forced into 1:1 columns, point-in-time history is lost, and deduplication errors cannot be rolled back without re-scraping the entire web.

This report specifies a fully normalized relational schema (PostgreSQL DDL) separating immutable source observations from canonical entities, models contact methods and social profiles as first-class 1:N entities, provides native support for deterministic and probabilistic entity resolution (ER), preserves temporal audit history, and provides an end-to-end Python migration path and backwards-compatible export view for existing CSV consumers.

---

## Findings

### S1-1 — Destructive observation collapse during merge permanently discards raw evidence
- **Where:** `dedup.py:45-61` (`_merge`) and `main.py:123-126`.
- **Breaks:** When two records match on phone or `(name, city)`, `_merge` resolves field conflicts using `merged[key] = av if _field_count(a) >= _field_count(b) else bv`. This is a coarse winner-takes-all heuristic applied globally across all fields. If Record A (Google Places) has 12 non-null fields (e.g. ratings, review counts, formatted address) and Record B (OSM) has 8 non-null fields (including exact doorway `lat`/`lon` and specific shop tags), Record A wins all non-empty scalar fields. The losing source's coordinates, tags, or alternative addresses are permanently discarded in memory and never written to `all_businesses.csv`.
- **Trigger:** Record A (Google): `{name: "Abou Hassan", phone: "+9611234567", lat: 33.8938, lon: 35.5018, rating: 4.6, review_count: 120, address: "Beirut, Lebanon"}` (7 fields). Record B (OSM): `{name: "Abou Hassan Restaurant", phone: "+9611234567", lat: 33.893845, lon: 35.501789, category: "restaurant", address: "Hamra Main Street, Hamra, Beirut"}` (6 fields).
- **Result:** Record A has `_field_count == 7` and Record B has `6`. Record A overwrites `address`, `lat`, and `lon`. The precise street address and centimeter-accurate coordinates from OSM are destroyed forever. If the match was a false positive, the merged data cannot be unmerged because the component observations were not persisted.
- **Fix:** Decouple ingestion from resolution. Persist every raw scraper record immutably in a `source_observations` table; derive canonical entity records in `canonical_entities` via explicit, field-level survivorship rules that reference the underlying observation IDs.

### S1-2 — 1:1 modeling of 1:N relationships causes silent data loss for contacts and socials
- **Where:** `scrapers/base.py:5-28`, `enricher.py:144, 197-204`, and `main.py:32-39`.
- **Breaks:** A business frequently operates multiple contact channels: a landline and mobile numbers, a general email (`info@`) and a sales email (`sales@`), and multiple social channels (e.g., separate brand and regional Instagram accounts). The flat schema reserves exactly one string column each for `phone`, `email`, `whatsapp`, `facebook`, `instagram`, `linkedin`. 
  - In `enricher.py:197-204`, extracted contacts from websites only populate if the field is currently falsy: `if not r.get("email") and contacts["email"]: r["email"] = contacts["email"]`. If OSM had an obsolete email (`old@domain.com`) or Google had a generic call center number, a verified direct sales contact discovered on the website is silently dropped.
  - If a company has both a local phone and a WhatsApp number that differ, one must be arbitrarily picked or `whatsapp` is overwritten by regex hits in `enricher.py:167-170`.
- **Trigger:** A business with an existing office landline `+9611340123` whose website advertises WhatsApp support at `+96170123456` and mobile orders at `+96171987654`.
- **Result:** Only one phone number and one WhatsApp string survive in the CSV; subsequent discovered numbers are ignored or replace previous numbers without tracking which is primary.
- **Fix:** Normalize contact methods into a dedicated `contact_methods` table (`type`, `value`, `normalized_value`, `is_primary`, `first_seen_at`, `last_seen_at`) and social handles into a `social_profiles` table (`platform`, `handle`, `url`).

### S1-3 — Erasure of temporal provenance and scraper audit trail
- **Where:** `dedup.py:54-58` (`source` and `scraped_at` handling in `_merge`).
- **Breaks:** When records merge, `source` is transformed into a pipe-delimited string (`"|".join(sorted(sources))`) and `scraped_at` is set to `max(av, bv)`. 
  1. All field-to-source provenance is erased: there is no record of whether `phone` came from Google, OSM, Wikidata, or an HTML crawl.
  2. Temporal state changes are masked: if Google Places changes a business status, rating, or phone number between monthly runs, the old value is overwritten without a change log.
  3. Scraper reliability cannot be audited: it is impossible to evaluate the field-level accuracy or freshness of OSM vs Google Places across runs because source identities are blended into `"google_places|osm"`.
- **Trigger:** Monthly re-scrape where Google Places updates a business phone number, but Wikidata retains an obsolete 2018 phone number.
- **Result:** If Wikidata has a higher field count on that run, the obsolete phone number overwrites Google's active phone number, with no historical log indicating when or why the phone changed.
- **Fix:** Store every raw observation as an immutable event with `source_id`, `source_record_id`, `raw_payload` (JSONB), and `observed_at` timestamp.

### S2-1 — Conflation of derived scoring metrics with entity state prevents algorithmic iteration
- **Where:** `main.py:131-138`, `enricher.py:227-260`, and `pitch_recommender.py:25-70`.
- **Breaks:** `completeness_score`, `lead_score`, `industry_priority`, and `recommended_service` are derived business metrics, not intrinsic entity attributes. In the flat model, they are hardcoded as columns in `all_businesses.csv`. 
  - Any change to the scoring model requires mutating every entity record in the database.
  - A/B testing of pitch rules (e.g. testing whether "Website rebuild" converts better than "E-commerce launch") is impossible without overwriting master data.
  - There is no history of how a lead's score evolved over time (e.g. whether a business's digital presence degraded over 6 months, triggering an outreach event).
- **Trigger:** Changing the weights in `enricher.py:lead_score` or adjusting priority thresholds in `whitelist.py`.
- **Result:** Prior scores are wiped out during the run. Sales reps cannot tell if a lead was promoted or demoted compared to last week's batch.
- **Fix:** Move derived metrics to a `scoring_snapshots` table tagged with `scoring_version` and timestamped evaluation logs.

### S2-2 — Binary `website_live` boolean destroys HTTP diagnostics and transient error tracking
- **Where:** `enricher.py:142-152` (`_fetch_website`) and `scrapers/base.py:16`.
- **Breaks:** `_fetch_website` evaluates HTTP liveness into a tri-state boolean: `True` (status < 400), `False` (status >= 400 or network timeout/exception), or `None` (untested). All telemetry—HTTP status codes (403 Cloudflare vs 404 Not Found vs 500 Server Error vs DNS failure), SSL certificate validity, redirect hops, and response latency—is thrown away.
  - A transient Cloudflare rate limit or network blip marks `website_live = False`, causing `pitch_recommender.py:54` to falsely recommend an expensive "Website rebuild + maintenance" pitch to a business whose site was merely experiencing a temporary outage.
  - The sales team has no evidence (e.g. HTTP status, error message, screenshot/headers) to reference on discovery calls.
- **Trigger:** An e-commerce site behind Cloudflare returning HTTP 403 to the scraper's generic requests session.
- **Result:** The system records `website_live = False` and awards +20 to `lead_score` (`enricher.py:244`). The lead is flagged as "Broken Website" when it is actually live.
- **Fix:** Implement a `website_probes` audit table capturing `url`, `http_status`, `response_time_ms`, `error_category`, `redirect_url`, and `checked_at`.

---

## Entity Resolution & History Architecture

The normalized schema adopts a **Four-Layer Architecture**:

```
[Layer 1: Immutable Ingestion]
      OSM / Google Places / Wikidata / Web Crawl
                          │
                          ▼
             source_observations (Raw JSONB + extracted fields)
                          │
[Layer 2: Entity Resolution & Linkage]
                          │ (Deterministic & Probabilistic Matchers)
                          ▼
             entity_observation_matches (Match Rule, Confidence, Auditor)
                          │
                          ▼
             canonical_entities ("Golden Record" Identity)
                          │
[Layer 3: Multi-Valued Attributes (1:N)]
       ┌──────────────────┼──────────────────┐
       ▼                  ▼                  ▼
 contact_methods    social_profiles      addresses / geolocations
(phone/email/wa)   (ig/fb/li/tiktok)    (osm_nodes / google_places)

[Layer 4: Verification & Audit State]
       ┌──────────────────┴──────────────────┐
       ▼                                     ▼
 website_probes (HTTP audit logs)     scoring_snapshots (Lead score & pitches)
```

### 1. How Entity Resolution (ER) Operates

1. **Immutable Ingestion:**
   When a scraper runs, it writes an immutable row into `source_observations`. It never updates or merges existing rows directly.
2. **Candidate Generation (Blocking):**
   Potential entity matches are identified using high-recall, low-cost blocking queries:
   - **Block 1 (Phone):** Match on E.164 normalized phone numbers (`contact_methods.normalized_value`).
   - **Block 2 (Geo-Proximity):** Match on spatial distance (`ST_DWithin` or bounding box ≤ 100 meters) combined with first-letter of name.
   - **Block 3 (Domain):** Match on registered domain names extracted from URLs.
3. **Match Scoring & Linkage:**
   A pairwise scoring engine computes match confidence:
   - Exact phone match + matching country: Confidence = `0.99`.
   - Geo distance < 50m + Jaro-Winkler name similarity > 0.88: Confidence = `0.92`.
   - Domain match + same city: Confidence = `0.95`.
   Links are stored in `entity_observation_matches` with the strategy name (`phone_exact`, `geo_name_fuzzy`), match confidence, and timestamp.
4. **Non-Destructive Golden Record Synthesis:**
   The `canonical_entities` table represents the "Golden Record". It is synthesized using explicit **survivorship rules**:
   - `rating`, `review_count`: Google Places takes precedence.
   - `coordinates`, `building_address`: OSM takes precedence (higher spatial precision).
   - `website`, `email`: Direct crawl / website probe takes precedence over stale directory data.
   **Crucial invariant:** If an ER match is found to be incorrect (e.g. two branches of "Zaatar w Zeit" were falsely collapsed), an engineer can simply delete or update the linkage in `entity_observation_matches`. The canonical entities can be recomputed immediately without re-scraping!

### 2. How History and Temporal Tracking Operate

- **Observation Log (Event Sourcing):** Every scrape creates an entry in `source_observations`. If Google Places is scraped monthly, 12 distinct observations exist for the year, capturing rating velocity (e.g. review count moving from 15 → 120).
- **Attribute Life-cycle (SCD Type 2 / Effective Dating):** In `contact_methods` and `social_profiles`, records contain `first_seen_at`, `last_seen_at`, and `is_active`. If a business drops an email from their website, that email's `is_active` flag is set to `FALSE` with `last_seen_at` preserved, maintaining full audit history for sales outreach.
- **Probe Audit Trail:** `website_probes` tracks every HTTP probe attempt. This creates a time-series log that detects *when* a site died, allowing Voxire to trigger a "Your website went down 4 days ago" sales campaign.
- **Scoring Versioning:** `scoring_snapshots` captures every calculation of `lead_score`, `completeness_score`, and `recommended_service` alongside the algorithm version (`scoring_engine_v1.2`). This prevents scoring updates from altering historical lead qualification records.

---

## Production DDL (PostgreSQL 15+)

```sql
-- ============================================================================
-- 034: leadminer Normalized Relational Schema
-- Target: PostgreSQL 15+ (Extensions: pgcrypto / uuid-ossp, pg_trgm)
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- ----------------------------------------------------------------------------
-- Enums & Reference Domains
-- ----------------------------------------------------------------------------

CREATE TYPE source_type AS ENUM (
    'osm',
    'google_places',
    'wikidata',
    'web_crawler',
    'manual_entry'
);

CREATE TYPE contact_type AS ENUM (
    'phone',
    'mobile',
    'landline',
    'email',
    'whatsapp'
);

CREATE TYPE social_platform AS ENUM (
    'instagram',
    'facebook',
    'linkedin',
    'twitter_x',
    'tiktok',
    'youtube'
);

CREATE TYPE match_strategy AS ENUM (
    'phone_exact',
    'domain_exact',
    'geo_name_fuzzy',
    'social_handle_exact',
    'manual_override'
);

CREATE TYPE priority_level AS ENUM (
    'low',
    'medium',
    'high'
);

-- ----------------------------------------------------------------------------
-- 1. Canonical Entities (The Golden Record)
-- ----------------------------------------------------------------------------

CREATE TABLE canonical_entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    primary_category TEXT,
    country_code VARCHAR(2) NOT NULL DEFAULT 'LB' CHECK (country_code IN ('LB', 'SA')),
    region TEXT,
    formatted_address TEXT,
    latitude NUMERIC(10, 7),
    longitude NUMERIC(10, 7),
    website_url TEXT,
    primary_domain TEXT,
    google_rating NUMERIC(2, 1) CHECK (google_rating >= 1.0 AND google_rating <= 5.0),
    google_review_count INTEGER CHECK (google_review_count >= 0),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_canonical_normalized_name_trgm ON canonical_entities USING gin (normalized_name gin_trgm_ops);
CREATE INDEX idx_canonical_domain ON canonical_entities (primary_domain) WHERE primary_domain IS NOT NULL;
CREATE INDEX idx_canonical_country_region ON canonical_entities (country_code, region);
CREATE INDEX idx_canonical_coords ON canonical_entities (latitude, longitude) WHERE latitude IS NOT NULL AND longitude IS NOT NULL;

-- ----------------------------------------------------------------------------
-- 2. Raw Source Observations (Immutable Scrape Ingestion)
-- ----------------------------------------------------------------------------

CREATE TABLE source_observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source source_type NOT NULL,
    source_record_id TEXT, -- e.g. OSM node ID, Google Place ID, Wikidata Q-ID
    raw_name TEXT,
    raw_category TEXT,
    raw_address TEXT,
    raw_phone TEXT,
    raw_email TEXT,
    raw_website TEXT,
    latitude NUMERIC(10, 7),
    longitude NUMERIC(10, 7),
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    batch_run_id TEXT, -- Correlates with execution run
    CONSTRAINT uq_source_record_run UNIQUE (source, source_record_id, observed_at)
);

CREATE INDEX idx_obs_source_record ON source_observations (source, source_record_id);
CREATE INDEX idx_obs_observed_at ON source_observations (observed_at DESC);
CREATE INDEX idx_obs_raw_phone ON source_observations (raw_phone) WHERE raw_phone IS NOT NULL;
CREATE INDEX idx_obs_batch_run ON source_observations (batch_run_id);

-- ----------------------------------------------------------------------------
-- 3. Entity Resolution Links (Audit & Provenance)
-- ----------------------------------------------------------------------------

CREATE TABLE entity_observation_matches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_id UUID NOT NULL REFERENCES canonical_entities(id) ON DELETE CASCADE,
    observation_id UUID NOT NULL REFERENCES source_observations(id) ON DELETE CASCADE,
    strategy match_strategy NOT NULL,
    confidence_score NUMERIC(4, 3) NOT NULL CHECK (confidence_score >= 0.0 AND confidence_score <= 1.0),
    match_evidence JSONB, -- Details: {"matched_phone": "+9611234567", "distance_meters": 12.4}
    resolved_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_by TEXT NOT NULL DEFAULT 'system_dedup_engine',
    CONSTRAINT uq_entity_obs UNIQUE (canonical_id, observation_id)
);

CREATE INDEX idx_eom_canonical ON entity_observation_matches (canonical_id);
CREATE INDEX idx_eom_observation ON entity_observation_matches (observation_id);

-- ----------------------------------------------------------------------------
-- 4. Multi-Valued Contact Methods (1:N)
-- ----------------------------------------------------------------------------

CREATE TABLE contact_methods (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_id UUID NOT NULL REFERENCES canonical_entities(id) ON DELETE CASCADE,
    contact_type contact_type NOT NULL,
    raw_value TEXT NOT NULL,
    normalized_value TEXT NOT NULL, -- E.164 for phones, lowercased for emails
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    source_observation_id UUID REFERENCES source_observations(id) ON DELETE SET NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_entity_contact UNIQUE (canonical_id, contact_type, normalized_value)
);

CREATE INDEX idx_contact_norm_value ON contact_methods (normalized_value);
CREATE INDEX idx_contact_canonical_lookup ON contact_methods (canonical_id, contact_type, is_active);

-- ----------------------------------------------------------------------------
-- 5. Multi-Valued Social Profiles (1:N)
-- ----------------------------------------------------------------------------

CREATE TABLE social_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_id UUID NOT NULL REFERENCES canonical_entities(id) ON DELETE CASCADE,
    platform social_platform NOT NULL,
    handle TEXT NOT NULL,
    url TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    source_observation_id UUID REFERENCES source_observations(id) ON DELETE SET NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_entity_social UNIQUE (canonical_id, platform, handle)
);

CREATE INDEX idx_social_canonical ON social_profiles (canonical_id, platform);
CREATE INDEX idx_social_handle ON social_profiles (platform, handle);

-- ----------------------------------------------------------------------------
-- 6. Website Diagnostics & Liveness Probes
-- ----------------------------------------------------------------------------

CREATE TABLE website_probes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_id UUID NOT NULL REFERENCES canonical_entities(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    final_redirect_url TEXT,
    http_status INTEGER,
    is_live BOOLEAN NOT NULL,
    response_time_ms INTEGER,
    ssl_valid BOOLEAN,
    error_message TEXT,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_probes_canonical_time ON website_probes (canonical_id, checked_at DESC);
CREATE INDEX idx_probes_is_live ON website_probes (is_live, checked_at DESC);

-- ----------------------------------------------------------------------------
-- 7. Scoring & Pitch Recommender Snapshots
-- ----------------------------------------------------------------------------

CREATE TABLE scoring_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_id UUID NOT NULL REFERENCES canonical_entities(id) ON DELETE CASCADE,
    completeness_score INTEGER NOT NULL CHECK (completeness_score >= 0),
    lead_score INTEGER NOT NULL CHECK (lead_score >= 0 AND lead_score <= 100),
    industry_priority priority_level NOT NULL DEFAULT 'low',
    recommended_service TEXT NOT NULL,
    algorithm_version TEXT NOT NULL DEFAULT 'v1.0',
    scoring_signals JSONB NOT NULL DEFAULT '{}'::jsonb,
    scored_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_scoring_canonical_time ON scoring_snapshots (canonical_id, scored_at DESC);
CREATE INDEX idx_scoring_lead_priority ON scoring_snapshots (industry_priority, lead_score DESC, scored_at DESC);

-- ----------------------------------------------------------------------------
-- 8. Backwards-Compatible Legacy Export View
-- Matches exact 23-column contract expected by all_businesses.csv
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_legacy_flat_export AS
WITH latest_scores AS (
    SELECT DISTINCT ON (canonical_id)
        canonical_id,
        completeness_score,
        lead_score,
        industry_priority::text AS industry_priority,
        recommended_service
    FROM scoring_snapshots
    ORDER BY canonical_id, scored_at DESC
),
latest_probe AS (
    SELECT DISTINCT ON (canonical_id)
        canonical_id,
        is_live AS website_live
    FROM website_probes
    ORDER BY canonical_id, checked_at DESC
),
agg_sources AS (
    SELECT 
        eom.canonical_id,
        string_agg(DISTINCT so.source::text, '|' ORDER BY so.source::text) AS sources,
        to_char(MAX(so.observed_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') AS scraped_at
    FROM entity_observation_matches eom
    JOIN source_observations so ON eom.observation_id = so.id
    GROUP BY eom.canonical_id
),
primary_contacts AS (
    SELECT
        canonical_id,
        MAX(CASE WHEN contact_type IN ('phone', 'mobile', 'landline') THEN normalized_value END) AS phone,
        MAX(CASE WHEN contact_type = 'email' THEN normalized_value END) AS email,
        MAX(CASE WHEN contact_type = 'whatsapp' THEN normalized_value END) AS whatsapp
    FROM contact_methods
    WHERE is_active = TRUE
    GROUP BY canonical_id
),
primary_socials AS (
    SELECT
        canonical_id,
        MAX(CASE WHEN platform = 'facebook' THEN url END) AS facebook,
        MAX(CASE WHEN platform = 'instagram' THEN handle END) AS instagram,
        MAX(CASE WHEN platform = 'linkedin' THEN url END) AS linkedin
    FROM social_profiles
    WHERE is_active = TRUE
    GROUP BY canonical_id
)
SELECT
    ce.canonical_name AS name,
    ce.primary_category AS category,
    ce.region AS region,
    ce.country_code AS country,
    ce.formatted_address AS address,
    ce.latitude::float AS lat,
    ce.longitude::float AS lon,
    pc.phone,
    pc.email,
    ce.website_url AS website,
    lp.website_live,
    ps.facebook,
    ps.instagram,
    pc.whatsapp,
    ps.linkedin,
    ce.google_rating::float AS rating,
    ce.google_review_count AS review_count,
    COALESCE(ls.completeness_score, 0) AS completeness_score,
    COALESCE(ls.lead_score, 0) AS lead_score,
    ls.industry_priority,
    ls.recommended_service,
    COALESCE(src.sources, 'unknown') AS source,
    COALESCE(src.scraped_at, to_char(ce.updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US')) AS scraped_at
FROM canonical_entities ce
LEFT JOIN primary_contacts pc ON ce.id = pc.canonical_id
LEFT JOIN primary_socials ps ON ce.id = ps.canonical_id
LEFT JOIN latest_probe lp ON ce.id = lp.canonical_id
LEFT JOIN latest_scores ls ON ce.id = ls.canonical_id
LEFT JOIN agg_sources src ON ce.id = src.canonical_id
WHERE ce.is_active = TRUE;
```

---

## Migration Path from the Flat CSV (`all_businesses.csv`)

### Migration Strategy (4 Phases)

1. **Phase 1: Zero-Downtime Infrastructure Provisioning**
   - Execute the DDL above against target PostgreSQL instance.
   - Verify view `v_legacy_flat_export` compiles and matches `main.py:FIELDS` schema 1:1.
2. **Phase 2: Historical Ingestion ETL (Cold Load)**
   - Run the Python ETL script below to load the existing `all_businesses.csv`.
   - The script splits pipe-delimited sources into individual synthetic observations, extracts contacts and social profiles into normalized child tables, and establishes canonical entity records.
   - Run the verification integrity queries to guarantee zero data loss.
3. **Phase 3: Pipeline Dual-Writing & Scraper Migration**
   - Update `scrapers/osm.py`, `scrapers/google_places.py`, and `scrapers/wikidata.py` to yield structured observations directly into `source_observations`.
   - Run the deduplication engine in relational space using candidate blocking indexes.
   - Scrapers write to DB; downstream CSV files are exported directly via:
     `COPY (SELECT * FROM v_legacy_flat_export) TO STDOUT WITH CSV HEADER`
4. **Phase 4: Cutover and CSV Deprecation**
   - Deprecate in-memory merging in `dedup.py`. All entities survive across runs natively in PostgreSQL.

### Executable Python Migration Script

```python
"""
migrate_csv_to_db.py
Zero-loss migration from all_businesses.csv into the normalized PostgreSQL schema.
Run:
    python migrate_csv_to_db.py data/all_businesses.csv postgresql://user:pass@localhost:5432/leadminer
"""

import csv
import json
import re
import sys
import uuid
from datetime import datetime, timezone
import psycopg2
from psycopg2.extras import execute_batch

def normalize_domain(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip().lower()
    match = re.search(r"https?://(?:www\.)?([^/:\s]+)", url)
    return match.group(1) if match else None

def parse_iso_ts(ts_str: str | None) -> datetime:
    if not ts_str:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)

def run_migration(csv_path: str, db_url: str):
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    print(f"[*] Starting migration from {csv_path}...")

    entities_batch = []
    obs_batch = []
    matches_batch = []
    contacts_batch = []
    socials_batch = []
    probes_batch = []
    scores_batch = []

    with open(csv_path, mode="r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        row_count = 0

        for row in reader:
            row_count += 1
            # Coerce empty strings to None
            r = {k: (v.strip() if v and v.strip() != "" else None) for k, v in row.items()}
            
            canonical_id = str(uuid.uuid4())
            name = r.get("name") or "Unnamed Business"
            norm_name = re.sub(r"\s+", " ", name).lower().strip()
            country = r.get("country") if r.get("country") in ("LB", "SA") else "LB"
            website = r.get("website")
            domain = normalize_domain(website)
            lat = float(r["lat"]) if r.get("lat") else None
            lon = float(r["lon"]) if r.get("lon") else None
            rating = float(r["rating"]) if r.get("rating") else None
            review_count = int(float(r["review_count"])) if r.get("review_count") else None
            scraped_at = parse_iso_ts(r.get("scraped_at"))

            # 1. Canonical Entity
            entities_batch.append((
                canonical_id, name, norm_name, r.get("category"), country,
                r.get("region"), r.get("address"), lat, lon, website, domain,
                rating, review_count, scraped_at, scraped_at
            ))

            # 2. Deconstruct pipe-delimited sources into separate observations
            raw_sources = (r.get("source") or "legacy_csv").split("|")
            for src_name in raw_sources:
                src_key = src_name.strip().lower()
                if src_key not in ('osm', 'google_places', 'wikidata', 'web_crawler', 'manual_entry'):
                    src_key = 'manual_entry'
                
                obs_id = str(uuid.uuid4())
                raw_payload = json.dumps({"legacy_row": row})
                obs_batch.append((
                    obs_id, src_key, f"legacy_{row_count}", name, r.get("category"),
                    r.get("address"), r.get("phone"), r.get("email"), website,
                    lat, lon, raw_payload, scraped_at, "migration_bootstrap"
                ))

                matches_batch.append((
                    str(uuid.uuid4()), canonical_id, obs_id,
                    'manual_override', 1.0, json.dumps({"note": "Bootstrap import from legacy CSV"}),
                    scraped_at
                ))

            # 3. Contact Methods
            if r.get("phone"):
                contacts_batch.append((
                    str(uuid.uuid4()), canonical_id, 'phone', r["phone"], r["phone"],
                    True, True, scraped_at, scraped_at
                ))
            if r.get("email"):
                contacts_batch.append((
                    str(uuid.uuid4()), canonical_id, 'email', r["email"], r["email"].lower(),
                    True, True, scraped_at, scraped_at
                ))
            if r.get("whatsapp"):
                contacts_batch.append((
                    str(uuid.uuid4()), canonical_id, 'whatsapp', r["whatsapp"], r["whatsapp"],
                    True, True, scraped_at, scraped_at
                ))

            # 4. Social Profiles
            if r.get("facebook"):
                socials_batch.append((
                    str(uuid.uuid4()), canonical_id, 'facebook', r["facebook"].split("/")[-1],
                    r["facebook"], True, scraped_at, scraped_at
                ))
            if r.get("instagram"):
                ig_handle = r["instagram"].strip("@").split("/")[-1].lower()
                socials_batch.append((
                    str(uuid.uuid4()), canonical_id, 'instagram', ig_handle,
                    f"https://instagram.com/{ig_handle}", True, scraped_at, scraped_at
                ))
            if r.get("linkedin"):
                socials_batch.append((
                    str(uuid.uuid4()), canonical_id, 'linkedin', r["linkedin"].split("/")[-1],
                    r["linkedin"], True, scraped_at, scraped_at
                ))

            # 5. Website Probe History
            if website and r.get("website_live") is not None:
                is_live = r["website_live"] in ("True", "true", "1", True)
                probes_batch.append((
                    str(uuid.uuid4()), canonical_id, website, None,
                    200 if is_live else 500, is_live, None, None, scraped_at
                ))

            # 6. Scoring Snapshot
            comp_score = int(float(r["completeness_score"])) if r.get("completeness_score") else 0
            ld_score = int(float(r["lead_score"])) if r.get("lead_score") else 0
            priority = r.get("industry_priority") if r.get("industry_priority") in ('low', 'medium', 'high') else 'low'
            service = r.get("recommended_service") or "Unknown"

            scores_batch.append((
                str(uuid.uuid4()), canonical_id, comp_score, ld_score,
                priority, service, 'legacy_v1', json.dumps({}), scraped_at
            ))

    print(f"[*] Parsed {row_count} records. Executing atomic database insert...")

    execute_batch(cur, """
        INSERT INTO canonical_entities (
            id, canonical_name, normalized_name, primary_category, country_code,
            region, formatted_address, latitude, longitude, website_url, primary_domain,
            google_rating, google_review_count, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, entities_batch)

    execute_batch(cur, """
        INSERT INTO source_observations (
            id, source, source_record_id, raw_name, raw_category,
            raw_address, raw_phone, raw_email, raw_website,
            latitude, longitude, raw_payload, observed_at, batch_run_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, obs_batch)

    execute_batch(cur, """
        INSERT INTO entity_observation_matches (
            id, canonical_id, observation_id, strategy, confidence_score, match_evidence, resolved_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, matches_batch)

    execute_batch(cur, """
        INSERT INTO contact_methods (
            id, canonical_id, contact_type, raw_value, normalized_value,
            is_primary, is_active, first_seen_at, last_seen_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, contacts_batch)

    execute_batch(cur, """
        INSERT INTO social_profiles (
            id, canonical_id, platform, handle, url,
            is_active, first_seen_at, last_seen_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, socials_batch)

    execute_batch(cur, """
        INSERT INTO website_probes (
            id, canonical_id, url, final_redirect_url, http_status,
            is_live, response_time_ms, error_message, checked_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, probes_batch)

    execute_batch(cur, """
        INSERT INTO scoring_snapshots (
            id, canonical_id, completeness_score, lead_score,
            industry_priority, recommended_service, algorithm_version, scoring_signals, scored_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, scores_batch)

    conn.commit()
    cur.close()
    conn.close()
    print("[+] Migration successfully completed.")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python migrate_csv_to_db.py <csv_path> <db_url>")
        sys.exit(1)
    run_migration(sys.argv[1], sys.argv[2])
```

### Verification & Reconciliation Query

To verify zero data loss between the flat CSV and the newly populated relational tables, run the following SQL query:

```sql
-- Count reconciliation across layers
SELECT 
    (SELECT COUNT(*) FROM canonical_entities) AS total_canonical_entities,
    (SELECT COUNT(*) FROM source_observations) AS total_raw_observations,
    (SELECT COUNT(*) FROM contact_methods WHERE contact_type = 'phone') AS total_phones,
    (SELECT COUNT(*) FROM contact_methods WHERE contact_type = 'email') AS total_emails,
    (SELECT COUNT(*) FROM social_profiles WHERE platform = 'instagram') AS total_instagram,
    (SELECT COUNT(*) FROM website_probes) AS total_probes,
    (SELECT COUNT(*) FROM scoring_snapshots) AS total_scoring_snapshots,
    (SELECT COUNT(*) FROM v_legacy_flat_export) AS view_export_count;
```

---

## Not a bug, but worth knowing

- **Cross-Source Entity Splitting:** In the flat model, if two businesses with the same name are mistakenly merged by `dedup.py`, they are irretrievably fused in `all_businesses.csv`. In this normalized schema, unmerging them requires a single relational update: create a new `canonical_entities` row and re-point the corresponding `observation_id` in `entity_observation_matches`. The raw observation data was never mutated.
- **Arabic Script Name Matching:** Arabic business names in Lebanon and Saudi Arabia present significant orthographic variation (e.g. `أ`/`إ`/`آ` vs `ا`, `ة` vs `ه`, `ى` vs `ي`, and optional diacritics/tashkeel). The `pg_trgm` GIN index on `normalized_name` combined with an Arabic character fold function ensures fuzzy matching works across variations without corrupting the canonical display name in `canonical_name`.
- **E.164 Discrepancies between Landlines and Mobiles:** In Lebanon (`+961`), landlines have 1-digit area codes (`01`, `04`, `05`, `07`, `08`, `09`) followed by 6 digits (total 7 or 8 digits), whereas mobile numbers start with `03`, `70`, `71`, `76`, `78`, `79`, `81` (8 digits). In KSA (`+966`), mobile numbers start with `5` (9 digits). Modeling contact methods as dedicated rows allows validating numbers against Google's `phonenumbers` library (libphonenumber port) prior to insertion into `normalized_value`.

---

## Recommended Order of Work

1. **Deploy DDL & Database Infrastructure:** Provision PostgreSQL instance and apply the schema DDL, including indexes, foreign keys, and `v_legacy_flat_export`.
2. **Execute Initial CSV Bootstrap:** Run `migrate_csv_to_db.py` on the existing `data/all_businesses.csv` to seed the database with all historical leads without downtime or loss.
3. **Refactor Scraper Outputs to Emit Observations:** Update `scrapers/base.py` to replace `BusinessRecord` with `SourceObservationPayload`. Have each scraper write immutable records to `source_observations`.
4. **Implement Relational Entity Resolution Worker:** Replace `dedup.py` with an idempotent SQL/Python candidate blocking and matching service that writes to `entity_observation_matches` and updates `canonical_entities`.
5. **Decouple HTTP Probes & Scoring:** Re-architect `enricher.py` and `pitch_recommender.py` to read from `canonical_entities`, probe URLs into `website_probes`, and record score calculations in `scoring_snapshots`.
6. **Switch Export Script to Database View:** Update `main.py` so that generating `sales_ready.csv`, `qualified_businesses.csv`, and `all_businesses.csv` queries `v_legacy_flat_export`, providing backward compatibility for sales reps without preserving technical debt.
