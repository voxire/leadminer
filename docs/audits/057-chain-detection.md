# 057 — Chain and Brand Detection: Multi-Location Entity Resolution and Corporate Pitch Architecture

## Verdict

The current pipeline treats every scraped point-of-interest (POI) as an isolated, single-unit small business. This creates two catastrophic failures: (1) if branches share a central call-center number (e.g., in `dedup.py:102-124`), they are destructively merged into a single arbitrarily chosen branch, permanently erasing all other physical locations and geographic coordinates; (2) if branches have distinct local numbers, they are emitted as $N$ independent leads in `sales_ready.csv` (`main.py:167-170`). In both cases, sales reps reach out to branch managers pitching $500 SMB websites instead of targeting corporate headquarters with high-ticket enterprise services ($25k–$100k+ RTYLR multi-location commerce OS, centralized patient booking portals, or franchise digital governance).

This document designs an end-to-end Brand and Multi-Location Entity Resolution (ER) system. It leverages OpenStreetMap (OSM) `brand` and `operator` tags, Wikidata hierarchical relations, shared registrable web domains (eTLD+1), shared verified Instagram/social handles, and MENA-specific telephony topology (unified 9200 numbers, toll-free 800, 4-digit hotlines, and PBX/DID exchange blocks). We specify a constrained graph clustering algorithm that avoids transitive percolation, establishes parent-child (headquarters vs. branch) entity linkages, preserves every physical location, and separates enterprise corporate pitches from single-site SMB pitches.

---

## Findings

### S1-1 — Destructive dedup merges multi-branch chains sharing hotlines into a single POI
- **Where:** `dedup.py:83-124` (`_merge` and `dedup`)
- **Breaks:** Multi-location chains, hospital networks, and franchise systems frequently publish a single centralized hotline, call center, or WhatsApp number across all marketing touchpoints. When `dedup.py:107-115` encounters identical normalized phone numbers, it unconditionally routes them into `phone_index` and executes `_merge()`. The `_merge` function uses a winner-takes-all `_field_count` rule (`dedup.py:98`), overwriting `address`, `lat`, and `lon` with whichever record has more non-null fields. 
- **Trigger:** Scraped data for "Malak Al Tawouk" containing 15 branches across Lebanon, all listing delivery hotline `+9611514` or `+9611585858`. Or 5 branches of "Dr. Sulaiman Al Habib Hospital" in Riyadh listing Unified Access Number `+966920000000`.
- **Result:** 14 of the 15 restaurant branches (and 4 of the 5 hospital campuses) are permanently deleted from memory and omitted from `all_businesses.csv`. The agency loses accurate local coverage, geospatial density mapping, and regional market penetration data.
- **Fix:** Redefine deduplication identity. Phone matching must only merge records that are geodistributed within the same physical radius ($\le 100$ meters) or share matching branch-level addresses. Records sharing a phone across distinct geographic coordinates ($> 500$ meters) must trigger a **Brand/Chain Cluster Link**, not a record merge.

### S1-2 — Lead fragmentation emits $N$ disconnected records for chains, causing embarrassing pitch misrouting
- **Where:** `dedup.py:117-124`, `pitch_recommender.py:25-99`, and `main.py:167-170`
- **Breaks:** When branches have distinct local landlines or mobile numbers (e.g., Starbucks Hamra vs. Starbucks Kaslik vs. Starbucks Verdun), `normalize_phone` yields distinct keys. The fallback key `(normalize_name(name), city)` in `dedup.py:119` fails because `city` differs. As a result, $N$ independent records for the exact same brand enter the enrichment and scoring pipeline.
- **Trigger:** A cafe chain with 20 branches across Mount Lebanon, Beirut, and North Lebanon. Branch 1 has no website listed on OSM; Branch 2 has a dead campaign landing page; Branch 3 has an Instagram link.
- **Result:** `pitch_recommender.py:57-96` assigns wildly conflicting, low-level SMB pitches to individual stores:
  - Branch 1: `"Full digital launch (brand + website + social setup)"`
  - Branch 2: `"Website rebuild + maintenance"`
  - Branch 3: `"E-commerce launch + Instagram-to-store funnel"`
  Sales reps then cold-call branch baristas or store managers to pitch local website design. This completely burns agency credibility with the corporate C-suite / CMO at brand headquarters.
- **Fix:** Cluster branches under a unified `brand_id`. Aggregate branch signals into a corporate-level entity record, suppress outbound SMB pitches to branch-level contacts, and generate a single corporate pitch directed to brand headquarters.

### S2-1 — Scrapers discard native brand and corporate operator taxonomies at ingestion
- **Where:** `scrapers/osm.py:59-91` and `scrapers/wikidata.py:10-26`
- **Breaks:** Both OSM and Wikidata already contain explicit, human-curated corporate hierarchy and brand membership tags. In `osm.py:59-91`, the Overpass response contains all tags via `out center tags;`, but the parser only extracts `name`, `shop`/`amenity`, address, phone, email, website, and social links. It deliberately ignores:
  - `brand`, `brand:wikidata`, `brand:wikipedia`
  - `operator`, `operator:wikidata`, `operator:type`
  - `network`, `branch`
  Similarly, `scrapers/wikidata.py:10-26` only fetches `?item wdt:P17 wd:Q822`, completely omitting properties `wdt:P1716` (brand), `wdt:P127` (owned by), `wdt:P749` (parent organization), `wdt:P137` (operator), and `wdt:P355` (subsidiary).
- **Trigger:** OSM node representing "Spinneys Achrafieh": `tags={"name": "Spinneys", "brand": "Spinneys", "brand:wikidata": "Q7577598", "operator": "Gray Mackenzie Retail Lebanon"}`.
- **Result:** The system discards the `Q7577598` identifier, forcing downstream stages to guess entity relationships using noisy string heuristics.
- **Fix:** Update `BusinessRecord` in `scrapers/base.py` and the scrapers to extract and preserve `brand`, `brand_wikidata`, `operator`, `operator_wikidata`, `branch_name`, and `network`.

### S2-2 — Enricher treats URLs and social handles as isolated strings without domain normalization
- **Where:** `enricher.py:158-214` (`_fetch_website`)
- **Breaks:** Branches of a chain often point to specific branch subpages on the same corporate root domain (e.g., `https://locations.starbucks.com/lb/beirut/hamra-street` vs. `https://locations.starbucks.com/lb/jounieh/kaslik-square`) or have slightly varying tracking parameters (`?utm_source=osm`). `enricher.py` treats `website` as a literal string. It does not extract the registrable domain (eTLD+1) and does not index records by shared domain.
- **Trigger:** 10 hospital locations under Clemenceau Medicine International listing `https://cmc.com.lb/beirut`, `https://cmc.com.lb/riyadh`, `https://cmc.com.lb/dubai`, or 8 clinic branches listing the same Instagram `@clemenceaumedicalcenter`.
- **Result:** The enricher runs redundant HTTP health checks against the same server 10 times, risking rate-limits or bans, and fails to recognize that all 10 locations share an existing corporate domain and corporate marketing team.
- **Fix:** Implement strict eTLD+1 domain extraction and social handle normalization. Index records by root domain and canonical social handle as deterministic edges for brand clustering.

### S3-1 — Lead scoring penalizes enterprise multi-unit brands as low-value SMBs
- **Where:** `enricher.py:275-312` (`lead_score`)
- **Breaks:** The scoring algorithm awards points for missing or broken assets typical of small mom-and-pop shops (+20 for dead website, +15 for missing digital channels). A 15-branch franchise group with a functional corporate website and 4.2 rating receives a low SMB lead score ($30–40/100$), ranking near the bottom of `qualified_businesses.csv`.
- **Trigger:** A regional restaurant group with 25 branches, generating $15M/year in revenue, with an outdated legacy POS and no online ordering engine.
- **Result:** The business is deprioritized or hidden from sales reps because it does not fit the "broken SMB website" heuristic.
- **Fix:** Introduce `corporate_lead_score` that scales with branch footprint (`locations_count`), multi-location review volume, and franchise vertical priority.

---

## Multi-Signal Evidence Model

To cluster disparate POIs into canonical corporate brands without human intervention, we define five independent signal layers. Each signal provides an evidence edge with an associated confidence score $w \in [0.0, 1.0]$.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        EVIDENCE SIGNAL LAYERS                         │
├────────────────────────────────────────────────────────────────────────┤
│ Layer 1: Deterministic Knowledge Graph IDs (OSM & Wikidata)             │
│   • brand:wikidata (e.g. Q37158)        → Weight: 1.00 (Ground Truth) │
│   • operator:wikidata (e.g. Q4751410)   → Weight: 1.00 (Ground Truth) │
├────────────────────────────────────────────────────────────────────────┤
│ Layer 2: Corporate Digital Identity (Web & Social)                    │
│   • Registrable Domain (eTLD+1)         → Weight: 0.95                │
│   • Corporate Instagram Handle          → Weight: 0.90                │
│   • Corporate LinkedIn Company Slug     → Weight: 0.90                │
├────────────────────────────────────────────────────────────────────────┤
│ Layer 3: Centralized & Telephony Topology (MENA Specific)              │
│   • Unified Access Numbers (KSA 9200)   → Weight: 0.95                │
│   • National Toll-Free (800) / Hotline  → Weight: 0.90                │
│   • Shared PBX / DID Prefix Exchange    → Weight: 0.75                │
├────────────────────────────────────────────────────────────────────────┤
│ Layer 4: Brand Token & Morphological Name Matching                     │
│   • Stripped Core Brand Name Identity   → Weight: 0.85                │
│   • Branch/Geographic Token Isolation   → Weight: 0.80                │
├────────────────────────────────────────────────────────────────────────┤
│ Layer 5: Category & Geospatial Co-Occurrence (Validation Guards)       │
│   • Category Compatibility Matrix       → Hard Constraint (0 or 1)    │
│   • Multi-Site Spatial Separation       → Validates Chain (Dist > 500m)│
└────────────────────────────────────────────────────────────────────────┘
```

### 1. Deterministic Taxonomy Signals (Layer 1)
- **OSM Brand & Operator Tags:**
  - `brand` (e.g., `"Starbucks"`, `"Dunkin'"`, `"Malak Al Tawouk"`, `"Hôpital Hôtel-Dieu de France"`)
  - `brand:wikidata` / `operator:wikidata` (e.g., `Q37158` for Starbucks; `Q1151624` for Carrefour)
  - `operator` (e.g., `"M.H. Alshaya Co."`, `"Azadea Group"`, `"Dr. Sulaiman Al Habib Medical Services Group"`)
  - `network` (used for healthcare networks, e.g. `"Assistance Publique - Hôpitaux"`)
- **Wikidata Ingestion:**
  - Properties `wdt:P1716` (brand), `wdt:P127` (owned by), `wdt:P749` (parent organization), `wdt:P137` (operator).
- **Rule:** If two POIs share an identical `brand:wikidata` or `operator:wikidata`, they connect with certainty $w = 1.00$.

### 2. Corporate Digital Identity Signals (Layer 2)
- **Registrable Domain (eTLD+1):**
  - Parsing URLs using Mozilla's Public Suffix List (e.g., `locations.starbucks.com.lb` $\to$ `starbucks.com.lb`; `dhabib.com.sa/branch/olaya` $\to$ `dhabib.com.sa`).
  - **Aggregator Blacklist:** Shared web builder/hosting/aggregator domains must be blocked from creating links:
    `facebook.com`, `instagram.com`, `tiktok.com`, `linktr.ee`, `bio.link`, `zomato.com`, `totersapp.com`, `jahez.net`, `hungerstation.com`, `talabat.com`, `google.com`, `wixsite.com`, `myshopify.com`, `wordpress.com`, `site123.me`.
- **Social Graph Consistency:**
  - Shared Instagram handle (e.g., `@albaik`, `@zaatarwzeit`, `@barnscafe`).
  - Shared LinkedIn company URL (e.g., `linkedin.com/company/al-habib-medical-group`).
- **Rule:** If two POIs share an un-blacklisted eTLD+1 or verified corporate Instagram handle, $w = 0.90 - 0.95$.

### 3. Telephony & Telecommunications Topology (Layer 3)
MENA telecommunications infrastructure features specific numbering architectures that reveal corporate relationships:
- **Unified Access Numbers (UAN) / Shared Hotlines:**
  - **Saudi Arabia (KSA):**
    - `9200 XXXXX` (Unified corporate number, 9 digits). E.g., `+966 9200 00000`. Used exclusively by multi-branch corporations, government entities, and large franchises.
    - `800 XXXXXX` (National Toll-Free, 7 or 10 digits).
  - **Lebanon (LB):**
    - 4-digit commercial hotlines (e.g., `1514`, `1530`, `1240`). In E.164 normalization, these are often erroneously padded or captured with Beirut area codes (`+961 1 514`).
    - Standard commercial call centers: e.g., `+961 1 585858` or `+961 1 372888`.
- **Shared PBX / DID (Direct Inward Dialing) Prefix Blocks:**
  - Large corporate campuses and hospital groups purchase continuous blocks of 50 to 500 phone numbers from telecom operators (e.g., Ogero in Lebanon, STC/Mobily in KSA).
  - *Lebanon Landlines:* `+961 {area_code} {exchange:4 digits} {extension:2 digits}`. E.g., AUB Medical Center uses the entire block `+961 1 350000` through `+961 1 354999`. Clemenceau Medical Center uses `+961 1 3728xx`.
  - *Saudi Landlines:* Riyadh `+966 11 {exchange:4 digits} {extension:3 digits}`. E.g., `+966 11 462 22xx`.
- **Rule:** 
  - Exact match on national UAN (`9200`) or Toll-Free (`800`) $\to w = 0.95$.
  - Shared PBX exchange block (first 6–8 digits identical) + high name similarity ($\ge 0.75$) $\to w = 0.85$.
  - Shared PBX exchange block alone without name similarity $\to w = 0.0$ (prevents co-tenants in the same commercial tower from clustering).

### 4. Brand Token Extraction & Morphological Normalization (Layer 4)
Business names in directories include physical branch descriptors, malls, and neighborhood suffixes. We apply regex-based morphological token stripping:

```python
# Suffixes and affixes stripped during brand token extraction
BRANCH_AFFIXES_EN = [
    r"\bbranch\b", r"\bexpress\b", r"\bclinic\b", r"\bhospital\b",
    r"\bdrive[\s-]?thru\b", r"\boutlet\b", r"\bcenter\b", r"\bgroup\b",
    r"\bcity\b", r"\bmall\b", r"\bairport\b", r"\bhighway\b",
    r"\bmain\s+street\b", r"\blocation\b", r"\bkiosk\b"
]

BRANCH_AFFIXES_AR = [
    r"\bفرع\b", r"\bمستشفى\b", r"\bمجمع\b", r"\bمركز\b", r"\bمجموعة\b",
    r"\bمول\b", r"\bاكسبرس\b", r"\bطريق\b", r"\bشارع\b", r"\bمطار\b"
]

GEOGRAPHIC_TOKENS = [
    # Lebanon
    "beirut", "hamra", "achrafieh", "verdun", "kaslik", "jounieh", "tripoli",
    "byblos", "jbeil", "saida", "sidon", "tyre", "sour", "zahle", "dhour choueir",
    # Saudi Arabia
    "riyadh", "jeddah", "dammam", "khobar", "olaya", "tahlia", "malaz",
    "rawdah", "nakheel", "yasmin", "corniche", "sulaimaniyah"
]
```

When stripping tokens from `"Malak Al Tawouk - Hamra Branch"` and `"Malak Al Tawouk Jounieh Highway"`, both normalize to canonical brand token: `"malak al tawouk"`.

---

## Graph Clustering Algorithm Specification

Entity resolution across multi-location chains is modeled as a **Constrained Weighted Graph Clustering** problem. 

### 1. Mathematical Formulation
Let $V = \{v_1, v_2, \dots, v_N\}$ be the set of all scraped POI records.
We construct an undirected graph $G = (V, E, W)$, where an edge $e_{ij} = (v_i, v_j) \in E$ exists if a candidate generator links $v_i$ and $v_j$. The edge weight $w_{ij} \in [0, 1]$ represents the composite confidence that $v_i$ and $v_j$ belong to the same parent corporate entity.

### 2. Candidate Generation (Blocking Phase)
To avoid all-pairs comparison ($O(N^2)$), records are indexed into candidate blocks. A candidate pair $(v_i, v_j)$ is evaluated if and only if they share at least one blocking key:
- **Block Key 1:** `eTLD+1` (Registrable domain, if not blacklisted)
- **Block Key 2:** `instagram_handle` (Normalized lowercase handle)
- **Block Key 3:** `brand_wikidata` or `operator_wikidata`
- **Block Key 4:** `phone_e164` (Normalized phone number)
- **Block Key 5:** `uan_or_pbx_prefix` (KSA 9200 number or exchange prefix)
- **Block Key 6:** `normalized_brand_token` (Exact match on stripped brand name token)

### 3. Edge Weight Computation Function
For any candidate pair $(v_i, v_j)$, the composite edge weight $W(v_i, v_j)$ is computed as follows:

$$W(v_i, v_j) = 1.0 - \prod_{k \in \text{Signals}} (1.0 - S_k(v_i, v_j))$$

Where $S_k(v_i, v_j)$ represents the independent score of signal $k$:
- $S_{\text{wikidata}} = 1.00$ if matching `brand:wikidata` or `operator:wikidata`
- $S_{\text{domain}} = 0.95$ if matching un-blacklisted `eTLD+1`
- $S_{\text{instagram}} = 0.90$ if matching valid `instagram_handle`
- $S_{\text{uan}} = 0.92$ if matching identical KSA `9200` or toll-free `800`
- $S_{\text{phone}} = 0.85$ if matching phone AND spatial distance $\text{dist}(v_i, v_j) > 500\text{m}$ (indicating different branches)
- $S_{\text{pbx}} = 0.70$ if matching PBX exchange prefix AND name similarity $\ge 0.75$
- $S_{\text{name}} = 0.85 \times \text{JaroWinkler}(\text{clean\_name}_i, \text{clean\_name}_j)$ if category matches

### 4. Hard Negative Constraints (Anti-Percolation Guards)
A fatal danger in naive graph clustering (e.g., standard Connected Components / Union-Find) is **transitive percolation** (a single bad edge merges two massive unrelated clusters into a giant "hairball"). 
We enforce three strict negative constraints that immediately zero out $W(v_i, v_j)$:
1. **Category Incompatibility:** If Category A and Category B have zero operational overlap (e.g., `healthcare/hospital` vs. `automotive/car_dealer`, or `beauty_salon` vs. `law_firm`), $W(v_i, v_j) = 0$.
2. **Explicit Brand Contradiction:** If $v_i$ has `brand:wikidata = Q37158` (Starbucks) and $v_j$ has `brand:wikidata = Q1151624` (Carrefour), $W(v_i, v_j) = 0$, regardless of whether they share a mall phone number or mall domain.
3. **Co-location / Shopping Mall Filter:** If $\text{dist}(v_i, v_j) < 30\text{ meters}$ and names have low similarity ($< 0.70$), they are co-located tenants in a shopping mall (e.g., ABC Mall Achrafieh, Riyadh Park Mall). They must never cluster together.

### 5. Clustering Algorithm: Constrained Agglomerative Clustering with Edge Pruning
We execute **Constrained Hierarchical Agglomerative Clustering (HAC)** with complete-linkage / minimax distance, or an edge-thresholded Connected Components traversal with consistency validation:

```python
def cluster_brands(nodes: list[dict], candidate_pairs: list[tuple[int, int, float]]) -> list[set[int]]:
    """
    Groups POI nodes into brand clusters.
    Threshold theta_high = 0.80.
    """
    adj = {i: set() for i in range(len(nodes))}
    
    # 1. Add edges meeting high-confidence threshold
    for u, v, weight in candidate_pairs:
        if weight >= 0.80:
            if not violates_negative_constraints(nodes[u], nodes[v]):
                adj[u].add(v)
                adj[v].add(u)
                
    # 2. Extract connected components
    visited = set()
    clusters = []
    for node in adj:
        if node not in visited:
            component = set()
            queue = [node]
            visited.add(node)
            while queue:
                curr = queue.pop()
                component.add(curr)
                for neighbor in adj[curr]:
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)
                        
            # 3. Post-cluster verification (Graph Density & Cohesion Check)
            # If a component is sparse (density < 0.40) and size > 3, prune weak cut edges
            cohesive_clusters = verify_and_split_component(component, nodes, adj)
            clusters.extend(cohesive_clusters)
            
    return clusters
```

### 6. Headquarters (Parent) vs. Branch (Child) Designation Logic
For every detected cluster $C = \{v_1, v_2, \dots, v_k\}$ where $|C| \ge 2$:
1. **Explicit Designation:** If any record has `tags.get("headquarters") == "yes"` or OSM tag `office=company|corporate`, it is designated Headquarters.
2. **Corporate Name Heuristic:** A record containing keywords like `"Head Office"`, `"Corporate"`, `"Administration"`, `"المكتب الرئيسي"`, or `"الإدارة العامة"` is prioritized.
3. **Domain / Contact Root:** The record with the clean root domain (e.g., `hospital.com` vs. `hospital.com/branches/branch1`) and corporate email (`info@`, `contact@`) is selected.
4. **Centroid / Review Maximization:** If no explicit marker exists, the branch with the highest `review_count` or the oldest `scraped_at` is designated the primary canonical branch (`is_hq = True`).

---

## Data Model & Output Representation: Chain vs. Single Site

To preserve every physical location while exposing high-level corporate intelligence, the system must separate the **Brand Entity** from the **Location Entity**.

### 1. Relational Schema (PostgreSQL DDL)

```sql
-- Corporate Brand / Chain Entity (Parent)
CREATE TABLE brands (
    brand_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name VARCHAR(255) NOT NULL,
    brand_slug VARCHAR(255) NOT NULL UNIQUE,
    category VARCHAR(100) NOT NULL,
    brand_type VARCHAR(50) NOT NULL, -- 'single_site', 'regional_chain', 'national_chain', 'hospital_group', 'franchise'
    locations_count INT NOT NULL DEFAULT 1,
    corporate_website VARCHAR(500),
    corporate_phone VARCHAR(50),
    corporate_email VARCHAR(255),
    corporate_instagram VARCHAR(100),
    corporate_linkedin VARCHAR(255),
    osm_brand_wikidata VARCHAR(50),
    osm_operator_wikidata VARCHAR(50),
    corporate_lead_score INT NOT NULL DEFAULT 0,
    corporate_recommended_service VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Physical Location / Point of Presence (Child)
CREATE TABLE locations (
    location_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES brands(brand_id) ON DELETE RESTRICT,
    branch_name VARCHAR(255) NOT NULL,
    is_headquarters BOOLEAN NOT NULL DEFAULT FALSE,
    address TEXT,
    region VARCHAR(100),
    country VARCHAR(2) NOT NULL, -- 'LB' or 'SA'
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    local_phone VARCHAR(50),
    local_email VARCHAR(255),
    branch_website_url VARCHAR(500),
    google_rating NUMERIC(2, 1),
    google_review_count INT,
    source VARCHAR(100) NOT NULL, -- 'osm', 'google_places', 'wikidata'
    source_id VARCHAR(255),
    completeness_score INT NOT NULL DEFAULT 0,
    scraped_at TIMESTAMPTZ NOT NULL
);

-- Clustering Evidence Audit Log
CREATE TABLE brand_evidence_edges (
    edge_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES brands(brand_id) ON DELETE CASCADE,
    location_a UUID NOT NULL REFERENCES locations(location_id) ON DELETE CASCADE,
    location_b UUID NOT NULL REFERENCES locations(location_id) ON DELETE CASCADE,
    signal_type VARCHAR(50) NOT NULL, -- 'osm_brand_tag', 'shared_domain', 'shared_uan', 'shared_instagram'
    confidence_score NUMERIC(3, 2) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_locations_brand_id ON locations(brand_id);
CREATE INDEX idx_brands_type ON brands(brand_type);
CREATE INDEX idx_locations_geo ON locations(country, region);
```

### 2. In-Memory Pipeline Contract (`scrapers/base.py`)
To integrate cleanly with the existing Python pipeline without breaking consumers, we extend `BusinessRecord` with 6 explicit chain-tracking keys:

```python
class BusinessRecord(TypedDict):
    # --- Existing 23 fields ---
    name: str | None
    category: str | None
    address: str | None
    region: str | None
    country: str | None
    lat: float | None
    lon: float | None
    phone: str | None
    email: str | None
    website: str | None
    website_live: bool | None
    facebook: str | None
    instagram: str | None
    whatsapp: str | None
    linkedin: str | None
    rating: float | None
    review_count: int | None
    industry_priority: str | None
    recommended_service: str | None
    lead_score: int
    source: str
    scraped_at: str
    completeness_score: int

    # --- Extended Multi-Location & Chain Fields ---
    brand_id: str | None            # e.g., "brand_starbucks_lb" or UUID
    brand_name: str | None          # e.g., "Starbucks"
    entity_type: str                # "single_site", "chain_branch", "chain_headquarters"
    locations_count: int            # Total number of physical branches detected (>= 1)
    is_headquarters: bool           # True if this record is the corporate node
    corporate_pitch: str | None     # Enterprise pitch tailored to corporate decision makers
```

### 3. Concrete Behavior Across Output CSVs

| Output File | How Chains & Single Sites Are Represented | Why This Prevents Data Loss & Sales Spam |
|---|---|---|
| `all_businesses.csv` | **Retains 100% of physical locations.** Every branch is preserved with exact coordinates, address, and local phone. Enriched with `brand_id`, `brand_name`, `entity_type` (`chain_branch` or `chain_headquarters`), and `locations_count`. | Complete market intelligence and mapping fidelity. Deduplication no longer deletes branches that share a hotline. |
| `qualified_businesses.csv` | All locations meeting `completeness_score >= 1`. Multi-location branches inherit brand-level website and social presence, preventing false disqualification of branches. | Ensures qualified enterprise locations are not dropped due to sparse local branch tags. |
| `with_websites.csv` & `without_websites.csv` | Categorized based on whether the entity has an active website. For chains, if the corporate domain exists, all branches reflect `has_website = True`. | Prevents pitching "Website build" to a McDonald's or Dr. Sulaiman Al Habib branch that lacked an OSM URL tag. |
| `sales_ready.csv` | **Deduplicated to 1 row per Brand/Chain.** For single sites, emits the single site. For multi-location chains, emits **only the Corporate Headquarters node** (or primary flagship branch), with `locations_count`, corporate hotline, and corporate pitch. | **Eliminates sales spam.** Reps call headquarters once with an enterprise pitch instead of contacting 25 individual store managers. |
| `chains_master.csv` *(New)* | Dedicated executive export listing all detected multi-location entities ($|C| \ge 2$), sorted by `locations_count DESC`. Columns: `brand_name, category, locations_count, regions_covered, corporate_phone, corporate_email, corporate_pitch, corporate_lead_score`. | Immediate, high-ticket prospecting list for agency account executives. |

---

## Differentiated Pitch Strategy

A single independent cafe with one location has completely different business pain points than a 20-location franchise or an 8-hospital medical consortium. Pitching the wrong solution burns deals instantly.

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                          SALES PITCH MATRIX                                      │
├──────────────────────┬────────────────────────────┬──────────────────────────────┤
│ Business Type        │ Single Site (1 Location)   │ Multi-Location Chain (≥ 2)   │
├──────────────────────┼────────────────────────────┼──────────────────────────────┤
│ Cafe / Bakery        │ "Website launch + Meta ads"│ "RTYLR Commerce OS: Unified  │
│                      │                            │  ordering, POS, multi-branch │
│                      │                            │  loyalty & kitchen display"  │
├──────────────────────┼────────────────────────────┼──────────────────────────────┤
│ Healthcare / Clinic  │ "Local SEO + Google Maps + │ "Enterprise Multi-Facility   │
│ / Hospital Group     │  WhatsApp appointment bot" │  Patient Portal + Telehealth │
│                      │                            │  & Unified Doctor Directory" │
├──────────────────────┼────────────────────────────┼──────────────────────────────┤
│ Fast Food / F&B      │ "Instagram-to-store funnel │ "Multi-Branch Local SEO +    │
│ Franchise            │  + Google Business Setup"  │  Direct Ordering Engine      │
│                      │                            │  (Bypass aggregator fees)"   │
├──────────────────────┼────────────────────────────┼──────────────────────────────┤
│ Broken / Dead Site   │ "Website rebuild + hosting"│ "Enterprise Re-Platforming:  │
│                      │                            │  Headless CMS + Branch Sub-  │
│                      │                            │  locations & High-Volume SLA"│
└──────────────────────┴────────────────────────────┴──────────────────────────────┘
```

### Concrete Service Mapping Implementation (`pitch_recommender.py`)

```python
def recommend_corporate_service(record: Mapping) -> str:
    """
    Computes enterprise-tier corporate pitch for multi-location brands (locations_count >= 2).
    """
    locations_count = int(record.get("locations_count") or 1)
    category = (record.get("category") or "").strip().lower()
    has_website = bool(record.get("website"))
    website_live = record.get("website_live") is True
    website_dead = record.get("website_live") is False

    # Tier 1: Broken / dead enterprise infrastructure
    if has_website and website_dead:
        return (
            f"Enterprise digital re-platforming ({locations_count} branches): "
            "High-availability headless web infrastructure + centralized branch CMS"
        )

    # Tier 2: F&B / Cafe Chains / Quick-Service Restaurant Franchises
    if any(k in category for k in ["cafe", "coffee", "restaurant", "fast_food", "bakery"]):
        if locations_count >= 5:
            return (
                f"RTYLR Enterprise Commerce OS ({locations_count} locations): "
                "Unified cloud POS, direct multi-branch online ordering (kill aggregator commissions), "
                "centralized kitchen display system (KDS), and cross-branch loyalty CRM"
            )
        else:
            return (
                f"Multi-unit online ordering & delivery engine ({locations_count} branches) "
                "+ centralized Google Business Profile network management"
            )

    # Tier 3: Hospital Groups, Medical Networks, Polyclinics
    if any(k in category for k in ["hospital", "clinic", "healthcare", "medical"]):
        return (
            f"Centralized Healthcare Patient Portal ({locations_count} facilities): "
            "Unified multi-branch physician scheduling, EHR integration, "
            "automated WhatsApp appointment reminders, and multi-location medical SEO"
        )

    # Tier 4: Retail & Fashion Chains
    if any(k in category for k in ["boutique", "fashion", "retail", "supermarket", "jewelry"]):
        return (
            f"Omnichannel Retail Infrastructure ({locations_count} stores): "
            "Unified ERP/inventory sync, click-and-collect store locator, "
            "and centralized Meta/Google catalog ads"
        )

    # Tier 5: General Multi-Location Enterprise Default
    return (
        f"Multi-Location Brand Governance ({locations_count} branches): "
        "Centralized GBP network sync, multi-unit local SEO, "
        "and consolidated corporate reputation management"
    )
```

---

## Not a bug, but worth knowing

1. **Commercial Towers and Shopping Malls Share Phone Numbers and Landlines:**
   - In Beirut (e.g., ABC Mall Achrafieh, Beirut Souks) and Riyadh (e.g., Kingdom Centre, Riyadh Park), dozens of unrelated retail shops, dental offices, and law practices operate within the same building.
   - Telecom operators frequently assign a single PBX block or front-desk receptionist number to the entire tower.
   - *Architecture Defense:* The clustering engine strictly enforces **Category Compatibility** and **Brand Name Similarity**. Two tenants sharing a landline in Kingdom Centre will NEVER be clustered together unless their normalized brand tokens match.
2. **Master Franchisees Operating Multiple Unrelated Brands:**
   - In MENA, large retail conglomerates own the franchise rights to multiple distinct international brands (e.g., **Alshaya Group** operates Starbucks, Shake Shack, H&M, Boots, and P.F. Chang's; **Azadea Group** operates Zara, Massimo Dutti, Paul, and Virgin Megastore).
   - In OSM, these POIs often share the identical `operator` or `operator:wikidata` tag (e.g., `operator="Alshaya"`).
   - *Architecture Defense:* Clustering must distinguish **Brand Clusters** from **Operator/Conglomerate Clusters**. In the schema, `brands` represents consumer-facing brand identity (Starbucks vs. Shake Shack), while an optional `operator_id` tracks parent holding companies. Outreach for retail operations pitches Starbucks, not Alshaya corporate IT, unless pursuing a holding-company group deal.
3. **Arabic and Latin Script Bilingual Variance:**
   - Businesses in Lebanon and KSA frequently register under Latin script, Arabic script, or a bilingual mix (e.g., "د. سليمان الحبيب" vs. "Dr. Sulaiman Al Habib"; "ملك الطاووق" vs. "Malak Al Tawouk").
   - Relying solely on string distance across languages will fail.
   - *Architecture Defense:* Scrapers must preserve both `name:en` and `name:ar` from OSM. Name normalization should cross-reference dual-language Wikidata labels (`rdfs:label` in `en` and `ar`) to maintain bilingual brand equivalence dictionaries.

---

## Recommended Order of Work

### Phase 1: Scraper Ingestion & Type Contract Hardening
1. **Extend `BusinessRecord` (`scrapers/base.py:5-29`):**
   Add `brand_id`, `brand_name`, `entity_type`, `locations_count`, `is_headquarters`, and `corporate_pitch`.
2. **Update OSM Ingestion (`scrapers/osm.py:59-91`):**
   Extract `brand`, `brand:wikidata`, `operator`, `operator:wikidata`, `branch`, and `network` from element tags.
3. **Update Wikidata SPARQL Query (`scrapers/wikidata.py:10-26`):**
   Add optional bindings for `wdt:P1716` (brand), `wdt:P127` (owned by), and `wdt:P749` (parent org).

### Phase 2: Domain, Social, and Telephony Signal Extraction
4. **Implement eTLD+1 & Social Normalizer (`enricher.py:158-214`):**
   Add `urllib.parse` / Public Suffix extraction for clean registered domains. Build blacklists for aggregators, social networks, and free web hosts.
5. **Implement MENA Telephony Classifier (`dedup.py:1-65`):**
   Add recognition for Saudi `9200` UANs, national `800` toll-free lines, Lebanese 4-digit commercial hotlines, and PBX landline exchange blocks.

### Phase 3: Brand Clustering Engine & Safe Deduplication
6. **Decouple Physical Dedup from Brand Linking (`dedup.py:102-124`):**
   Refactor `dedup.py` so that matching phone numbers only merge records if spatial distance $\le 100$ meters. If distance $> 500$ meters, emit a candidate edge for brand clustering instead of overwriting records.
7. **Implement Constrained Graph Clustering Module (`brand_clusterer.py`):**
   Build the candidate blocking, edge weighting, negative constraint filters, and HAC community detection.
8. **Designate Headquarters & Flagship Nodes:**
   Implement rule-based selection for `is_headquarters` across each detected cluster.

### Phase 4: Scoring, Corporate Pitches & Export Contract
9. **Upgrade Pitch Recommender (`pitch_recommender.py:25-99`):**
   Implement `recommend_corporate_service()` to assign enterprise commerce OS, patient portal, and multi-location governance pitches to brands where `locations_count >= 2`.
10. **Refactor Output Generation (`main.py:164-185`):**
    - Populate all physical locations into `all_businesses.csv` with chain metadata.
    - Roll up `sales_ready.csv` so that multi-location brands output only 1 corporate row directed to headquarters.
    - Generate `data/chains_master.csv` summarizing all detected enterprise groups.
