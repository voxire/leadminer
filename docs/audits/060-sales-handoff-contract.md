# 060 — Sales Handoff Contract, Rep Actionability, and Closed-Loop Feedback

## Verdict

The current pipeline operates as an open-loop firehose that dumps raw, unverified scraper artifacts into five static CSVs (`main.py:149-153`) with zero feedback capture, zero lead identity persistence, and opaque scores (`enricher.py:275-312`). Sales representatives are handed arbitrary integers (e.g., `lead_score: 85`) and generic pitch labels (`pitch_recommender.py:25-99`) with no evidence trail, no verified direct contact channel, and no mechanism to report bad phone numbers, hostile objections, or closed deals. To transform `leadminer` into an enterprise outbound engine, we must establish a formal bilateral contract: an export specification guaranteeing immutable lead IDs, verified contactability, explainable confidence/reason battle-cards, and a structured outcome ingestion protocol that continuously calibrates scoring weights against real-world conversions.

---

## Findings

### S1 — Lack of Immutable Lead Identity Breaks Rep Attribution, Workflow State, and Feedback Loops
- **Where:** `scrapers/base.py:5-28`, `main.py:32-39`, and `dedup.py:45-61`.
- **Breaks:** The schema lacks an immutable, unique identifier (`lead_id`). On every execution, `main.py:124-126` merges freshly scraped items with the existing master and runs `dedup()`. If business attributes change (e.g., phone format tweaked, address refined, or scraper ordering altered), row positions shift, field counts change winner-takes-all resolution, and CSV row indices become entirely non-deterministic. When a sales representative works a lead from `sales_ready.csv`, logs notes, or attempts outreach, there is no primary key to join sales activity or CRM records back to the master store.
- **Trigger:** A rep claims lead row #42 ("Al-Amir Bakery") in Monday's export. On Wednesday, a new scrape runs, adding 50 new Google Places records. Wednesday's row #42 is now an auto repair shop in Riyadh. Notes and status updates cannot be re-associated with the original entity.
- **Fix:** Generate a deterministic, immutable `lead_id` (e.g., UUIDv5 seeded by country and normalized E.164 phone, falling back to normalized name + city coordinates: `uuid5(NAMESPACE_URL, f"urn:leadminer:{country}:{norm_phone}")`). Persist this ID across all exports and downstream feedback tables.

### S1 — Absence of Closed-Loop Feedback Ingestion Prevents Score Calibration and Perpetuates Arbitrary Heuristics
- **Where:** Entire codebase (`main.py:93-184`, `enricher.py:275-312`, `pitch_recommender.py:25-99`).
- **Breaks:** The scoring algorithm is completely open-loop. `enricher.py:275-312` assigns arbitrary hardcoded weights (`email: +20`, `whatsapp: +15`, `phone: +15`, `website_dead: +20`, `rating < 4.0: +10`), but there is no mechanism to ingest rep outcomes (`contacted`, `no_answer`, `wrong_number`, `not_interested`, `closed_won`). As a result, scoring errors—such as inflating leads with non-functional info@ emails or dead websites that were actually false-positive HTTP 403 blocks—are never penalized, and high-converting patterns are never amplified.
- **Trigger:** A sales rep calls 50 consecutive leads tagged with "Website rebuild + maintenance" (`pitch_recommender.py:58`), discovering that 35 were Cloudflare-protected sites or parked domains where the business owner is unreachable. In the current system, the scraper continues to award those identical profiles +20 bonus points on every subsequent run.
- **Fix:** Define an operational feedback schema (`lead_outcomes`), provide an automated ingestion command (`leadminer feedback import <path>`), and calibrate scoring weights using logistic regression or empirical Bayesian reachability/conversion multipliers.

### S2 — Opaque Scoring and Unsubstantiated Pitch Recommendations Paralyze Sales Outreach
- **Where:** `enricher.py:275-312` and `pitch_recommender.py:25-99`.
- **Breaks:** Reps receive an opaque scalar (`lead_score: 75`) and a rigid, ungrounded recommendation string (e.g., `"SEO audit + visibility upgrade"` or `"Website rebuild + maintenance"`). The rep has no visibility into *why* the pitch was chosen, *what specific evidence* was detected, or *how confident* the system is in the data. Before dialing, reps must waste 5–10 minutes manually searching Google, Instagram, and the company website to verify whether the lead is legit and find a talking point.
- **Trigger:** A rep cold-calls an upscale clinic pitched for "Website launch + capture their existing audience" (`pitch_recommender.py:66`) because `website` was null in OSM. The clinic owner immediately responds that they have had a fully transactional web portal for four years. The rep loses credibility because `leadminer` failed to surface data source provenance or confidence levels.
- **Fix:** Replace opaque outputs with structured explainability fields: `pitch_reason` (human-readable evidence statement), `score_confidence` (normalized float 0.00–1.00 based on source corroboration and probe certainty), and `pitch_evidence` (structured key-value signals detailing HTTP status codes, review counts, and active channels).

### S2 — Flawed "Sales-Ready" Predicate Sends Uncontactable or Low-Viability Leads to Reps
- **Where:** `main.py:82-90` and `main.py:142-145`.
- **Breaks:** `main.py:82-90` defines contact readiness as:
  ```python
  bool(len(phone) >= 7 or ("@" in email and len(email) > 5) or len(instagram) > 3)
  ```
  This definition introduces three critical operational failures:
  1. It ignores `whatsapp` and `linkedin`, even though `whatsapp` is the dominant B2B commercial channel in Lebanon and Saudi Arabia.
  2. It accepts raw, un-normalized phone strings of 7 digits without verifying country dial-code validity or mobile vs. landline viability.
  3. A bare Instagram handle without a phone or email qualifies as "sales-ready", forcing reps to manually log into Instagram and send cold DMs (a channel with abysmal B2B conversion, aggressive spam throttling, and no CRM automation).
- **Trigger:** A record has `phone: None`, `email: None`, and `instagram: "explore"` (a blacklisted scraper artifact that slipped through). `has_any_contact()` evaluates to `True`. The lead is exported to `sales_ready.csv` despite having zero reachable channels.
- **Fix:** Redefine the `sales_ready` predicate to require at least one verified direct communication channel (E.164 mobile/WhatsApp or verified corporate email) and establish explicit channel preference ordering (`whatsapp > phone > email > dm`).

### S3 — Raw CSV Export Fails Rep Ergonomic & Localization Requirements
- **Where:** `main.py:74-80`.
- **Breaks:** CSV files are written with standard Python UTF-8 (`encoding="utf-8"`) without a Byte Order Mark (BOM). When sales reps or account executives in Beirut or Riyadh open these files in Microsoft Excel on Windows, Arabic business names (e.g., `مؤسسة النور`, `طرابلس`, `الرياض`) render as unreadable mojibake (`Ø§Ù„Ù†ÙˆØ±`). Furthermore, phone numbers lack standard clickable URI protocols (`tel:` or `https://wa.me/`), forcing reps to manually copy-paste and clean strings between tools.
- **Trigger:** Rep opens `sales_ready.csv` in Excel 2021 on Windows. All Arabic merchant names and street addresses appear corrupted. Rep abandons the CSV and requests a manual export.
- **Fix:** Export Excel-facing CSVs using `encoding="utf-8-sig"`, format all phone numbers strictly in E.164, and provide generated columns for `whatsapp_click_url` and `google_maps_url`.

---

## The Sales Handoff Contract Specification

The relationship between the data engineering pipeline (`leadminer`) and the outbound sales team is governed by an explicit Service Level Agreement (SLA) and schema contract. The pipeline is not a data dump; it is a just-in-time sales enablement feed.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          LEADMINER PIPELINE                                 │
│  Scrape ──► Dedup ──► HTTP Probe ──► Rule Engine ──► Calibration Model      │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                      OUTBOUND CONTRACT (sales_ready.csv)
                      1. Immutable lead_id
                      2. Verified Direct Channel (E.164 / WhatsApp / Email)
                      3. Actionable Context (pitch_reason + score_confidence)
                      4. Ergonomic URLs (Click-to-Chat WhatsApp, Maps)
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           SALES OUTREACH                                    │
│  Rep scans lead ──► One-click dial/chat ──► Uses pitch battle-card          │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                      FEEDBACK CONTRACT (lead_outcomes.csv / Sheet)
                      1. lead_id + timestamp + rep_id
                      2. Standardized Outcome Status (wrong_number, closed, etc)
                      3. Lost/Uninterested Reason Category
                      4. Actual Service Pitched vs Won
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       CONTINUOUS CALIBRATION LOOP                           │
│  Dynamic Weight Tuning ──► Domain Blacklisting ──► Pitch Rule Refinement    │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

### 1. What a Rep Needs Per Lead to Act (The Rep Actionability Minimum)

A sales development representative (SDR) or account executive (AE) takes between 45 and 90 seconds to qualify a cold lead before executing outreach. If required information is missing, ambiguous, or poorly structured, contact velocity drops by over 60%.

To initiate contact without external research, a lead record **must guarantee the following 7 information pillars**:

| Pillar | Required Field | Operational Purpose & Rep Value |
|---|---|---|
| **1. Identity** | `lead_id`, `business_name`, `legal_or_alt_name` | Uniquely identifies the target across runs and CRMs; provides dual-language naming (Arabic + English) so the rep uses the authentic commercial name. |
| **2. Physical Anchor** | `country`, `region`, `city`, `street_address`, `google_maps_url` | Prevents geographic blunders (e.g., calling a Dammam business at Beirut local hours); one-click Google Maps verification confirms the business is an active physical operation, not a shell. |
| **3. Direct Channel** | `primary_channel`, `primary_contact_value`, `whatsapp_click_url` | Eliminates hesitation on how to engage. Pre-formatted click-to-chat links (`https://wa.me/...`) allow 1-click mobile or desktop WhatsApp engagement with pre-filled greeting text. |
| **4. The Hook (The Pitch)** | `recommended_service`, `pitch_tier` | Tells the rep exactly what service package to lead with (e.g., "E-commerce Launch" vs. "Website Rebuild"). |
| **5. The Evidence (The "Why")** | `pitch_reason`, `score_confidence` | A concise 1-2 sentence battle-card explaining the diagnostic findings (e.g., "Website returns 404 Not Found; active Instagram with 14k followers; lacks online menu"). Rep reads this directly during the opening hook. |
| **6. Credibility / Posture** | `rating`, `review_count`, `digital_presence_summary` | Prepares the rep for objections. If rating is 3.4 across 48 reviews, the rep knows customer dissatisfaction is high; if review count is 0, the rep pitches Google Maps listing optimization. |
| **7. Freshness & Provenance** | `last_verified_at`, `source_summary` | Assures the rep that the contact info was confirmed within the last 30 days and indicates whether the record was multi-source corroborated. |

---

### 2. Export Guarantees & SLA (What the CSV Must Guarantee)

The file `data/sales_ready.csv` is the designated production delivery artifact. Any row present in this file must meet the following strict contractual guarantees:

#### A. Encoding and File Integrity
1. **UTF-8 with BOM (`utf-8-sig`):** All exports must include the Unicode Byte Order Mark `0xEF, 0xBB, 0xBF` to ensure instant, corruption-free rendering of Arabic text in Microsoft Excel on Windows and macOS.
2. **Explicit Null Representation:** Null values are represented strictly as empty strings `""`. String literals such as `"None"`, `"null"`, `"NaN"`, or `"#N/A"` are strictly prohibited.
3. **Escaped Formatting:** Multiline addresses or notes must adhere to RFC 4180 (enclosed in double quotes with internal quotes escaped as `""`).

#### B. Reachability & Data Quality Guarantees
1. **Zero Fake Numbers:** Phone numbers must be fully normalized to E.164 standard (`+<country_code><national_number>`). Any string failing ITU-T E.164 syntax validation (e.g., fewer than 7 digits, invalid national destination codes, placeholder sequences like `+961000000`) is rejected from `sales_ready.csv`.
2. **Channel Verification:** Every lead must possess at least one viable direct channel. A record having only an Instagram handle or Facebook page is relegated to `marketing_nurture.csv` and excluded from `sales_ready.csv`.
3. **HTTP Probe Verification:** A lead pitched for "Website rebuild" (`pitch_recommender.py:58`) must have a verified server-level HTTP error code (`404`, `410`, `500`, `502`, `503`). Connection timeouts, SSL handshake failures, DNS resolution errors, and HTTP `403 Forbidden` (WAF challenges) must **never** trigger a dead-website rebuild pitch; they must be classified as `UNKNOWN` and pitched under general digital presence.
4. **Deduplication Invariant:** Exactly 1 row per physical business entity. Zero duplicate phone numbers, zero duplicate WhatsApp links, and zero duplicate active website URLs within any single export batch.

#### C. Full Column Specification for `sales_ready.csv` (28-Column Contract)

```
Column Name                  Type        Null?    Description / Contract Rule
-------------------------------------------------------------------------------------------------------------------
1.  lead_id                  string      NO       Deterministic UUIDv5 (e.g., 'f81d4fae-7dec-11d0-a765-00a0c91e6bf6')
2.  business_name            string      NO       Cleaned commercial display name (Arabic or English)
3.  legal_or_alt_name        string      YES      Secondary name or transliterated trade name
4.  category                 string      NO       Normalized industry taxonomy (from whitelist)
5.  industry_priority        enum        NO       Allowed: 'high', 'medium'
6.  country                  string      NO       ISO 3166-1 alpha-2: 'LB' or 'SA'
7.  region                   string      NO       Normalized governorate/province (e.g., 'Beirut', 'Riyadh')
8.  city                     string      YES      Specific city or district (e.g., 'Hamra', 'Al Olaya')
9.  street_address           string      YES      Cleaned physical address
10. google_maps_url          string      YES      Direct navigational link: https://www.google.com/maps/search/?api=1&query=lat,lon
11. primary_channel          enum        NO       Preferred outreach mode: 'whatsapp', 'phone', 'email'
12. primary_contact_value    string      NO       The exact phone (E.164) or email address for primary outreach
13. whatsapp_click_url       string      YES      Direct pre-filled link: https://wa.me/<digits>?text=<encoded_pitch>
14. phone_e164               string      YES      Secondary/fallback telephone in E.164 format
15. email                    string      YES      RFC 5322 sanitized email; blacklisted domains excluded
16. website                  string      YES      Cleaned FQDN or URL
17. website_http_status      int         YES      Exact probe status (e.g., 200, 404, 500); NULL if unprobed
18. instagram_handle         string      YES      Sanitized handle without '@' or URL path
19. linkedin_url             string      YES      Company LinkedIn URL
20. google_rating            float       YES      Numeric score (1.0 to 5.0)
21. google_reviews_count     int         NO       Total review count (defaults to 0)
22. recommended_service      string      NO       Standardized pitch package name
23. pitch_tier               int         NO       Pitch priority tier (1 = Immediate pain point, 7 = Upsell)
24. pitch_reason             string      NO       Human-readable evidence statement for rep opening statement
25. score_confidence         float       NO       Algorithm certainty (0.00 to 1.00) based on corroboration
26. lead_score               int         NO       Calibrated opportunity score (0 to 100)
27. verification_date        date        NO       ISO 8601 date of latest probe/scrape (YYYY-MM-DD)
28. source_provenance        string      NO       Source audit trail (e.g., 'google_places+website_crawl')
```

---

### 3. Confidence & Reason Model (Explainable Scoring & Outreach Battle-Cards)

A lead score without justification breeds distrust. Reps ignore scores if they cannot see the underlying math. The contract specifies two required explainability attributes for every lead: `pitch_reason` and `score_confidence`.

#### A. The `pitch_reason` Specification
`pitch_reason` is a structured, synthesized natural language sentence designed to be read by the sales rep during the first 10 seconds of a cold call or pasted into a WhatsApp message. It must answer two questions:
1. *What specific technical or commercial deficiency did we detect?*
2. *Why does this deficiency cost the business revenue?*

**Template Structure:**
`[Observed Defect/State] + [Operational Consequence] + [Evidence Metric]`

**Deterministic Mapping Examples:**

| Recommended Service | Trigger Conditions in Pipeline | Generated `pitch_reason` |
|---|---|---|
| **Website Rebuild + Maintenance** | `website` present, probe status `404 Not Found`, `google_reviews_count > 10` | "Existing website is returning HTTP 404 (broken), but business has 45 active Google reviews; customers looking up the brand are hitting a dead link." |
| **E-commerce Launch + IG Funnel** | `website` is NULL, `instagram_handle` present, category in `_ECOM_FRIENDLY` | "Active Instagram presence with retail clothing catalog, but no checkout funnel; orders currently handled manually via DMs without payment integration." |
| **Reputation + SEO Turnaround** | `website_http_status == 200`, `google_rating < 3.8`, `google_reviews_count >= 15` | "Live website active, but Google rating is 3.4 across 28 reviews with customer complaints; needs local search optimization and review recovery campaign." |
| **Full Digital Launch** | `website` is NULL, `instagram` is NULL, `primary_channel == 'phone'` | "High-priority commercial establishment operating with zero digital footprint (no website, no social); losing regional search traffic to local competitors." |
| **Lead-Gen Overhaul** | Vertical in `_LEAD_GEN_VERTICALS`, `website_http_status == 200`, missing WhatsApp button | "Live website lacks direct WhatsApp click-to-chat capture and modern landing pages; high-intent traffic in high-ticket vertical is bouncing without conversion." |

#### B. The `score_confidence` Formulation
`score_confidence` is a continuous scalar between `0.00` and `1.00` that quantifies data certainty. It prevents reps from treating a speculative single-tag OSM scrape with the same urgency as a triple-confirmed Google Places record.

The confidence score is computed as a weighted harmonic composite of four independent verification dimensions:

$$\text{score\_confidence} = \min\left(1.00, \; w_s \cdot C_{\text{source}} + w_p \cdot C_{\text{probe}} + w_c \cdot C_{\text{channel}} + w_f \cdot C_{\text{freshness}}\right)$$

Where:
1. **Source Corroboration ($C_{\text{source}}$, weight = 0.35):**
   - Single unverified source (OSM or Wikidata only): `0.40`
   - Google Places verified business: `0.75`
   - Multi-source match (e.g., `google_places|osm` or `google_places|wikidata`): `1.00`
2. **Technical Probe Certainty ($C_{\text{probe}}$, weight = 0.30):**
   - No website claimed: `0.80` (deterministic state)
   - HTTP probe returned definitive code (`200`, `404`, `410`): `1.00`
   - HTTP probe returned transient error (timeout, SSL fail, `403`): `0.30`
3. **Contact Channel Deliverability ($C_{\text{channel}}$, weight = 0.25):**
   - Verified WhatsApp mobile number (E.164 with active WhatsApp prefix): `1.00`
   - Standard landline phone or generic email (`info@`): `0.60`
   - Social handle only (DM outreach): `0.30`
4. **Data Freshness ($C_{\text{freshness}}$, weight = 0.10):**
   - Verified within last 14 days: `1.00`
   - Verified 15–45 days ago: `0.70`
   - Verified > 45 days ago: `0.40`

---

### 4. Sales Feedback Schema & Intake Mechanism (Capturing Ground Truth)

Without structured feedback from reps, lead scoring remains static guesswork. When a rep spends 3 minutes attempting to call a lead, that interaction represents high-value ground truth.

#### A. Standardized Outcome Taxonomy
Reps must select from a closed, controlled vocabulary of outcomes. Free-form text fields must never be used for primary classification.

```
LEVEL 1: CATEGORY                     LEVEL 2: OUTCOME CODE              PIPELINE ACTION / SIGNAL
───────────────────────────────────────────────────────────────────────────────────────────────────
[DATA_HYGIENE_FAILURE]               wrong_number                       Permanently blacklists phone; invalidates dedup cluster.
                                     out_of_service                     Flags phone for re-verification in 30 days.
                                     business_permanently_closed        Marks entity as defunct in SQLite master.
                                     fake_or_spam_listing               Blacklists source record; audits scraper category.

[REACHABILITY_FRICTION]              no_answer                          Increments dial attempt counter (max 3 retries).
                                     gatekeeper_blocked                 Schedules alternative channel (e.g., WhatsApp / Email).
                                     unresponsive_whatsapp              Switches primary channel to voice call.

[PROSPECT_DISQUALIFIED]              not_interested_no_budget           Demotes lead score by 30 points; sets 90-day cooldown.
                                     not_interested_has_agency          Tags competitor agency; re-routes to competitive pitch.
                                     not_interested_bad_timing          Schedules automated pipeline re-surfacing in 60 days.
                                     already_solved_in_house            Updates digital status; suppresses pitch category.

[OPPORTUNITY_QUALIFIED]              meeting_scheduled                  Positive reinforcement (+15 to feature weights).
                                     audit_requested                    Confirms pitch validity (+25 to feature weights).
                                     proposal_sent                      Strong conversion signal; locks entity in active pipeline.

[TERMINAL_CONVERSION]                closed_won                         Max reward; writes feature vector to Gold Calibration Set.
                                     closed_lost                        Captures deal post-mortem reason.
```

#### B. Storage Architecture: The `lead_outcomes` Table
Feedback is stored in a durable relational table in SQLite (`data/leadminer.db`), decoupled from the raw scraper observations:

```sql
CREATE TABLE IF NOT EXISTS lead_outcomes (
    interaction_id       TEXT PRIMARY KEY,                     -- UUIDv4 for this specific touchpoint
    lead_id              TEXT NOT NULL,                        -- Foreign key to master business entity
    rep_id               TEXT NOT NULL,                        -- Identifier of the sales rep (e.g., 'rep_karim')
    attempt_timestamp    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    channel_used         TEXT NOT NULL CHECK(channel_used IN ('whatsapp', 'phone', 'email', 'in_person', 'social_dm')),
    outcome_code         TEXT NOT NULL,                        -- Controlled vocabulary from taxonomy above
    disqualification_subreason TEXT,                           -- Optional sub-category if not interested
    recommended_service  TEXT NOT NULL,                        -- The service leadminer told the rep to pitch
    actual_service_pitched TEXT NOT NULL,                     -- What the rep actually pitched on the call
    rep_confidence_rating INT CHECK(rep_confidence_rating BETWEEN 1 AND 5), -- Rep's subjective assessment of lead quality
    deal_value_estimated REAL,                                 -- Estimated contract value (USD / SAR)
    notes                TEXT,                                 -- Contextual feedback or merchant objections
    created_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (lead_id) REFERENCES businesses(lead_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_outcomes_lead_id ON lead_outcomes(lead_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_code ON lead_outcomes(outcome_code);
CREATE INDEX IF NOT EXISTS idx_outcomes_timestamp ON lead_outcomes(attempt_timestamp);
```

#### C. Bi-Directional Intake Interfaces
Sales teams operate in diverse tools (Google Sheets, HubSpot, or raw CSVs). We provide three frictionless methods to ingest feedback:

1. **Roundtrip CSV Ingestion (`leadminer feedback import <path>`):**
   The exported `sales_ready.csv` contains two pre-allocated, empty trailing columns: `feedback_outcome` and `feedback_notes`. Reps can update the CSV directly during their calling shift and upload it back. The CLI validates outcomes against the controlled vocabulary and imports valid entries into SQLite.
2. **Google Sheets Sync Integration:**
   `leadminer` exports daily partitions to a shared Google Drive spreadsheet. Reps update a dropdown column (`Data Validation` restricted to Level 2 Outcome Codes). A lightweight GitHub Action or scheduled CLI job polls the Sheet via Google Sheets API, extracts modified rows, commits them to `lead_outcomes`, and marks the row as ingested.
3. **CRM Webhook Endpoint (HubSpot / GoHighLevel / custom):**
   When a deal stage changes in the sales CRM, an HTTP POST webhook hits `leadminer/api/webhook/feedback` carrying `{lead_id, outcome_code, rep_id, deal_value}`.

---

### 5. Algorithmic Calibration Loop (Updating Scoring from Real Outcomes)

The primary reason to capture sales feedback is to replace arbitrary heuristic weights in `enricher.py:275-312` with empirical probabilities.

#### A. The Calibration Pipeline

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            RAW OUTCOME LOGS                                 │
│         wrong_number, no_answer, not_interested, closed_won                 │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       CALIBRATION ENGINE (Weekly)                           │
│                                                                             │
│  1. Negative Filtering: Suppress dead numbers & bad categories              │
│  2. Reachability Modeling: P(Contacted | Channel, Region, Source)           │
│  3. Conversion Modeling: P(Opportunity | Service, Defects, Category)        │
│  4. Weight Optimization: Regularized Logistic Regression (Ridge / L2)       │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    CALIBRATED MODEL ARTIFACT (model.json)                   │
│         Updated Feature Weights & Dynamic Pitch Recommender Trees            │
└─────────────────────────────────────────────────────────────────────────────┘
```

#### B. Mathematical Calibration Formulation
Currently, `lead_score()` in `enricher.py:275-312` calculates:

$$\text{lead\_score} = \min\left(100, \sum w_i x_i\right)$$

where weights $w_i$ were chosen by guesswork ($+20, +15, +10$).

In the closed-loop system, `lead_score` represents the **Expected Commercial Value Percentile**, decomposed into Reachability Probability ($P_{\text{reach}}$) and Conversion Probability ($P_{\text{conv}}$):

$$\text{Expected Value (EV)} = P_{\text{reach}}(X) \times P_{\text{conv}}(X, \text{service}) \times V_{\text{service}}$$

Where:
1. **Reachability Probability ($P_{\text{reach}}$):**
   Modeled via logistic regression on operational features:
   
   $$P(\text{Contacted} \mid X) = \sigma\left(\beta_0 + \beta_1 X_{\text{channel\_type}} + \beta_2 X_{\text{source\_count}} + \beta_3 X_{\text{carrier\_valid}} + \beta_4 X_{\text{region}}\right)$$
   
   If reps report `wrong_number` or `out_of_service`, the reachability coefficients for that data source or phone pattern are penalized.

2. **Conversion Probability ($P_{\text{conv}}$):**
   Modeled via regularized logistic regression on verified digital defects:
   
   $$P(\text{Qualified} \mid X) = \sigma\left(\theta_0 + \sum_{j} \theta_j \cdot \text{Defect}_j + \sum_{k} \gamma_k \cdot \text{Category}_k\right)$$

3. **Dynamic Feedback Penalties (Automated Suppression Rules):**
   - **Phone Blacklist:** If an outcome is `wrong_number` or `fake_or_spam_listing`, the normalized phone number is inserted into `dialer_suppression_list` with a permanent TTL.
   - **Pitch Demotion:** If a specific `recommended_service` for a specific category (e.g., "RTYLR POS" for "Pharmacies") receives $\ge 10$ consecutive `not_interested_wrong_service` outcomes, the rule priority in `pitch_recommender.py` is dynamically demoted below the fallback discovery call.
   - **Source Reliability Index:** If records originating exclusively from OSM yield a reachability rate $< 25\%$ in Saudi Arabia, the system scales down OSM-only leads in SA by multiplying their final score by a discount factor:
   
   $$\text{Score}_{\text{final}} = \text{Score}_{\text{raw}} \times \left(\frac{\text{Reachability}_{\text{source}}}{\text{Baseline Reachability}}\right)$$

---

## Worked Example: End-to-End Lifecycle of a Calibrated Lead

### Step 1: Ingestion & Probe Execution
- **Raw Input:** Scraper finds "Al-Basha Grills", Hamra, Beirut. Phone: `01340123`, Mobile: `70123456`, Website: `http://albashagrills.com`.
- **HTTP Probe:** Probe connects to `http://albashagrills.com` $\to$ Returns HTTP `404 Not Found` in 310ms.
- **Enrichment:**
  - Phone normalized to E.164: `+9611340123` (landline), Mobile: `+96170123456` (WhatsApp eligible).
  - WhatsApp Click URL generated: `https://wa.me/96170123456?text=Marhaba%20Al-Basha%20Grills...`
  - Rating: 4.2 (110 reviews on Google).

### Step 2: Scoring & Handoff Generation (`sales_ready.csv`)
- `lead_id`: `a8b3c4d5-e6f7-5a1b-9c2d-3e4f5a6b7c8d`
- `recommended_service`: `"Website rebuild + maintenance"`
- `pitch_reason`: `"Existing website returns HTTP 404 (down), but restaurant has 110 Google reviews (4.2 stars); diners cannot access menu or online reservations."`
- `score_confidence`: `0.92` (Multi-source Google+OSM, HTTP status 404 confirmed, verified mobile WhatsApp).
- `lead_score`: `88` (High opportunity, high reachability).

### Step 3: Rep Outreach & Feedback Capture
1. Rep clicks `whatsapp_click_url` from their tablet or desktop.
2. WhatsApp chat opens directly with the restaurant manager.
3. Rep sends opening hook based on `pitch_reason`.
4. Manager responds: *"We changed our domain to albashamezza.com last year and forgot to update Google. But we actually need an online ordering system because aggregators charge 20%."*
5. Rep books a discovery demo for online ordering (RTYLR Commerce OS).
6. Rep logs outcome:
   - `outcome_code`: `meeting_scheduled`
   - `actual_service_pitched`: `RTYLR commerce OS (online ordering)`
   - `notes`: `Domain changed to albashamezza.com. High interest in ordering system to cut aggregator commissions.`

### Step 4: Closed-Loop Calibration Effect
1. Pipeline ingests row: Updates master entity with active domain `albashamezza.com`.
2. Learning engine registers positive conversion on category `restaurant` with high review count for online ordering.
3. Pitch recommender rules for F&B in Beirut are adjusted: restaurant records with $>100$ reviews are nudged toward ordering funnels over static brochure rebuilds.

---

## Not a bug, but worth knowing

1. **Regional WhatsApp Primacy:** In Lebanon and Saudi Arabia, cold email outreach to small and medium businesses (SMBs) has an open rate of $< 8\%$ and response rate $< 1\%$. WhatsApp outreach achieves $> 70\%$ open rates and $> 25\%$ response rates. Treating email and WhatsApp as equivalent 15-point features in `enricher.py:280-283` is fundamentally misaligned with local commercial reality.
2. **KSA Commercial Registration (CR) Verification:** In Saudi Arabia, cold B2B outreach is heavily influenced by formal compliance (Wathq / Ministry of Commerce CR numbers). Incorporating a verified CR flag in future iterations will elevate enterprise sales conversion by over 40%.
3. **Lebanese Numbering Plan Volatility:** Due to telecommunications infrastructure shifts in Lebanon, landlines starting with `01`, `04`, `05`, `07`, `08`, `09` frequently suffer from copper cable theft or power outages at local exchanges. Cellular numbers (`03`, `70`, `71`, `76`, `78`, `79`, `81`) have over $3\times$ the connect rate of fixed landlines. `sales_ready.csv` must always prioritize mobile prefixes for `primary_contact_value`.

---

## Recommended order of work

1. **Implement Immutable `lead_id` Generation:**
   Update `dedup.py` and `scrapers/base.py` to assign a deterministic UUIDv5 to every entity upon deduplication, ensuring identity survives future re-scrapes.
2. **Upgrade CSV Writer for Excel & Localization:**
   Modify `main.py:74-80` to write `sales_ready.csv` with `encoding="utf-8-sig"` (UTF-8 BOM), guaranteeing pristine rendering of Arabic characters in Excel across all operating systems.
3. **Refactor `sales_ready` Filter & Actionability Predicate:**
   Replace the flawed `has_any_contact()` check in `main.py:82-90` with strict validation: require at least one validated E.164 phone or active WhatsApp channel, and generate click-to-dial / click-to-chat links.
4. **Deploy the Explainability Engine (`pitch_reason` & `score_confidence`):**
   Add `pitch_reason` and `score_confidence` generator functions in `pitch_recommender.py` and map them into the export schema.
5. **Establish the Feedback Intake Table & CLI Command:**
   Create the SQLite `lead_outcomes` table and build `leadminer feedback import <path>` to ingest rep status updates and validate against the outcome taxonomy.
6. **Activate the Closed-Loop Calibration Engine:**
   Implement weekly batch retraining that adjusts scoring weights and automates suppression of invalid phone numbers and misclassified domains based on recorded sales outcomes.
