# 090 — Risk Register: Commercial Lead Generation Operations (Lebanon & Saudi Arabia)

## Verdict

Deploying `leadminer` in its current state as a commercial B2B lead source presents an unsustainable risk profile dominated by two existential vulnerabilities: **unbounded regulatory liability under Saudi Arabia's Personal Data Protection Law (PDPL)** (fines up to SAR 5,000,000 / $1.33M USD) and **immediate, unrecoverable vendor bans** from Meta WhatsApp and Google Maps Platform. The pipeline operates as an open-loop firehose without consent tracking, suppression lists, runtime schema validation, or E.164 phone classification, which guarantees high outreach failure rates (>50% landline dial bounce on WhatsApp pitches), rapid sales rep abandonment, and permanent dataset poisoning. Operating commercially requires immediate architectural segregation between data harvesting and outreach dispatch, replacement of the Google Places scraper with compliant data sources, implementation of two-gate validation and tombstone suppression, and strict human-in-the-loop sales workflows.

---

## Risk Ranking Methodology & Scoring Framework

To rank operating risks objectively, this register adapts the standard quantitative Risk Priority Number (RPN) and Annualized Loss Expectancy (ALE) models:

$$\text{Expected Loss Rank} = \text{Likelihood Score} \times \text{Impact Score}$$

### Likelihood Scale (1–5)
- **5 (Certain / Imminent):** Occurs on >80% of pipeline runs or within the first 100 outbound outreach attempts without changes.
- **4 (High):** 50%–80% probability per operational quarter under continuous scraping and outreach.
- **3 (Medium):** 20%–50% probability annually; triggered by routine external changes (API shifts, vendor quota limits).
- **2 (Low):** 5%–20% probability; requires specific adverse conditions or secondary regulatory triggers.
- **1 (Rare):** <5% probability; edge cases or dormant regulatory enforcement actions.

### Impact Scale (1–5)
- **5 (Catastrophic):** Total business shutdown, statutory fines >$100,000, criminal liability, or permanent revocation of core vendor infrastructure (Meta WABA / Google Cloud Project).
- **4 (Critical):** $25,000–$100,000 financial loss, complete commercial sales rep desertion, total loss of a target market, or severe cloud billing blowouts.
- **3 (Moderate):** $5,000–$25,000 financial or payroll loss, corrupted master dataset requiring manual purge/re-scrape, or 5-hour CI pipeline freezes.
- **2 (Minor):** $1,000–$5,000 operational friction, individual query drops, or localized data formatting mojibake.
- **1 (Negligible):** <$1,000 loss, cosmetic data issues, or easily automated re-runs.

---

## Master Risk Register: Ranked by Expected Loss

| Rank | Risk ID | Category | Risk Description | Likelihood (1-5) | Impact (1-5) | Expected Loss (Score / Severity) | Primary Location |
|:---:|:---:|:---:|:---|:---:|:---:|:---:|:---|
| **1** | **REG-01** | Legal / Reg | Saudi PDPL Non-Compliance & Direct Marketing Fines | 4 (High) | 5 (Catastrophic) | **20 / Catastrophic** | `scrapers/google_places.py:82-124`, `main.py:174-178` |
| **2** | **VEN-01** | Legal / Vendor | WhatsApp Cloud API Banning & Meta Account Termination | 5 (Certain) | 4 (Critical) | **20 / Catastrophic** | `enricher.py:204-206`, `pitch_recommender.py:83-96` |
| **3** | **BIZ-01** | Business | Sales Rep Abandonment ("The Rep Desertion Loop") | 5 (Certain) | 4 (Critical) | **20 / Catastrophic** | `main.py:82-90, 149-153`, `pitch_recommender.py:25-99` |
| **4** | **VEN-02** | Legal / Vendor | Google Maps Platform ToS Breach & API Key Termination | 4 (High) | 4 (Critical) | **16 / Critical** | `scrapers/google_places.py:31-42, 249-273` |
| **5** | **DATA-01** | Data Quality | Landline Leakage & WhatsApp Delivery Failure | 5 (Certain) | 3 (Moderate) | **15 / Critical** | `dedup.py:33-63`, `enricher.py:129-132` |
| **6** | **DATA-02** | Data Quality | False-Positive Website Liveness & Inverted Pitching | 5 (Certain) | 3 (Moderate) | **15 / Critical** | `enricher.py:147-152`, `pitch_recommender.py:54-55` |
| **7** | **TECH-01** | Technical | Google Places 429 Unbounded Infinite Hang in CI | 4 (High) | 3 (Moderate) | **12 / Moderate** | `scrapers/google_places.py:213-216`, `.github/workflows/scrape.yml:15` |
| **8** | **TECH-02** | Technical / Sec | Unchecked SSRF & CI Runner Credential Exfiltration | 3 (Medium) | 4 (Critical) | **12 / Moderate** | `enricher.py:124, 146`, `.github/workflows/scrape.yml:46-50` |
| **9** | **REG-02** | Legal / Reg | Erasure Impossibility & Re-Ingestion of Opted-Out Leads | 4 (High) | 3 (Moderate) | **12 / Moderate** | `main.py:93-97`, `.github/workflows/scrape.yml:35-50` |
| **10** | **BIZ-02** | Business | Open-Loop Feedback Starvation & Negative Drift | 4 (High) | 3 (Moderate) | **12 / Moderate** | `main.py:93-184`, `enricher.py:275-312` |
| **11** | **DATA-03** | Data Quality | Cumulative Master Dataset Poisoning (Zero Validation) | 4 (High) | 3 (Moderate) | **12 / Moderate** | `main.py:46-71, 99-126`, `scrapers/base.py:5-29` |
| **12** | **TECH-03** | Technical | Overpass Unhandled JSONDecodeError Thread Crash | 4 (High) | 3 (Moderate) | **12 / Moderate** | `scrapers/osm.py:38-56`, `main.py:112-113` |
| **13** | **DATA-04** | Data Quality | Dedup Identity Collapse & Data Loss on Collisions | 4 (High) | 2 (Minor) | **8 / Moderate** | `dedup.py:34-38, 70-85, 100-116` |
| **14** | **BIZ-03** | Business | Lead Staleness & Stagnant Master Accumulation | 4 (High) | 2 (Minor) | **8 / Moderate** | `main.py:46-71, 123-153` |
| **15** | **FIN-01** | Financial | Google Places Text Search Enterprise SKU Cost Trap | 3 (Medium) | 2 (Minor) | **6 / Minor** | `scrapers/google_places.py:31-42` |
| **16** | **REG-03** | Legal / Reg | OSM ODbL Share-Alike Contamination of Commercial CSVs | 3 (Medium) | 2 (Minor) | **6 / Minor** | `scrapers/osm.py:10-26`, `main.py:174-178` |

---

## Detailed Risk Assessments

---

### 1. Legal and Compliance Risks (Lebanon & Saudi Arabia)

#### Risk REG-01 — Saudi PDPL Non-Compliance & Electronic Marketing Fines
- **Where:** `scrapers/google_places.py:82-124`, `main.py:174-178`, `.github/workflows/scrape.yml:46-50`.
- **Failure Scenario & Root Cause:** `leadminer` collects contact numbers and names of commercial establishments across Riyadh, Jeddah, and Dammam. Under the Saudi Personal Data Protection Law (PDPL, Royal Decree M/19, fully enforced September 14, 2024), contact details of sole proprietors, partners, and named employees are legally classified as **Personal Data**. Crucially, the PDPL does **not** exempt business-to-business (B2B) communications from direct marketing consent requirements (ICLG Saudi Arabia §10.2). Cold electronic outreach (email, WhatsApp, SMS) without prior documented affirmative opt-in violates Article 26. Furthermore, pushing cumulative CSVs to an unencrypted, out-of-kingdom Google Drive folder (`.github/workflows/scrape.yml:46-50`) constitutes an unauthorized cross-border personal data transfer without an adequacy assessment or standard contractual clauses (SDAIA SCCs).
- **Likelihood:** **4 (High)** — Outreach to Saudi numbers without prior consent inevitably prompts recipient complaints to the Saudi Data & AI Authority (SDAIA) or the Communications, Space and Technology Commission (CST).
- **Impact:** **5 (Catastrophic)** — Administrative fines up to **SAR 5,000,000** (~$1,333,000 USD) under Article 35, doubling for repeat violations. Unauthorized transfer of sensitive records carries potential criminal penalties.
- **Expected Loss:** **20 (Catastrophic)**.
- **Early Warning Signal:** Recipient complaints referencing SDAIA/CITC regulations; inquiries from Saudi telecommunications providers; sudden bounce/rejection spikes from Saudi telecom gateways (`@moe.gov.sa`, `@saudi.net.sa`).
- **Mitigation:**
  1. Segregate Saudi data: mark all `country == 'SA'` records as personal data by default.
  2. Implement a strict consent-gating layer: cold automated outreach to Saudi numbers must be prohibited. Cold engagement must be limited to human B2B sales calls via verified landlines or inbound warming campaigns.
  3. Relocate data storage for Saudi entities to an in-kingdom storage bucket (e.g., Oracle Cloud Riyadh or AWS Bahrain/KSA) or execute formal Standard Contractual Clauses (SCCs).

---

#### Risk VEN-01 — WhatsApp Cloud API Banning & Meta Account Termination
- **Where:** `enricher.py:129-132, 204-206`, `main.py:82-90`, `pitch_recommender.py:83-96`.
- **Failure Scenario & Root Cause:** `leadminer` scrapes WhatsApp links (`wa.me/(\d+)`) and phone numbers, promoting them to `sales_ready.csv` while recommending "WhatsApp lead capture funnels" (`pitch_recommender.py:86`). If downstream growth engineers connect an automated WhatsApp Cloud API dispatch bot to blast these scraped numbers, Meta's automated enforcement kicks in immediately. Meta's [WhatsApp Business Messaging Policy](https://www.whatsapp.com/legal/business-messaging-policy/) requires prior affirmative opt-in explicitly consenting to receive messages from the specific business. Scraped numbers possess zero opt-in. Recipients use the native "Block & Report Spam" button.
- **Likelihood:** **5 (Certain)** — Inevitable when sending cold marketing templates to >100 scraped numbers.
- **Impact:** **4 (Critical)** — Meta downgrades phone number quality rating (Green $\to$ Yellow $\to$ Red) within hours. Messaging tier drops from 1,000/day to 250/day. The WhatsApp Business Account (WABA) and associated Meta Business Portfolio are permanently restricted, burning business phone numbers, forfeiting API deposits, and destroying direct messaging as a company acquisition channel.
- **Expected Loss:** **20 (Catastrophic)**.
- **Early Warning Signal:** Meta Webhook events reporting `quality_score: RED` or `status: FLAGGED`; high block rate (>3% of delivered templates); delivery failure code `131049` (Message failed to send due to spam rate limiting).
- **Mitigation:**
  1. Code-level architectural firewall: scraped records must **never** enter an automated Cloud API message queue.
  2. Cold outreach via WhatsApp must be restricted to **manual, human-initiated 1-on-1 clicks** using `https://wa.me/<number>` URLs with pre-filled context (`080-whatsapp-outreach-compliance.md`).
  3. Maintain an automated suppression ledger that immediately blacklists any number that responds "STOP" or triggers an opt-out.

---

#### Risk VEN-02 — Google Maps Platform Terms of Service Breach & API Key Revocation
- **Where:** `scrapers/google_places.py:31-42, 249-273`, `main.py:174-178`, `.github/workflows/scrape.yml:46-50`.
- **Failure Scenario & Root Cause:** Google Maps Platform Terms of Service §3.2.3(a) explicitly prohibits scraping, bulk extraction, or permanent caching of Google Maps Content outside the service (*"Customer will not ... copy and save business names, addresses, or user reviews"*). §3.2.3(d)(iii) explicitly forbids using places data to *"create or augment an advertising product"* or directory. `leadminer` requests `places.displayName`, `places.nationalPhoneNumber`, `places.websiteUri`, `places.rating`, and `places.userRatingCount` via Text Search, serializes them to `all_businesses.csv`, and ships them to Google Drive via rclone. Furthermore, `enricher.py:79-94` performs point-in-polygon coordinate analysis on Google Places lat/long coordinates, violating §3.2.3(c)(iv).
- **Likelihood:** **4 (High)** — Google actively monitors API request signatures, billing accounts, and pattern bursts. Commercial disputes or downstream distribution expose the source.
- **Impact:** **4 (Critical)** — Instant, unilateral termination of the Google Cloud Project and API keys with zero refund; loss of access to Google Cloud infrastructure; potential commercial indemnification claims.
- **Expected Loss:** **16 (Critical)**.
- **Early Warning Signal:** Sudden HTTP 403 `PERMISSION_DENIED` or `PROJECT_SUSPENDED` errors; warnings from Google Cloud Trust & Safety; billing account auditing notifications.
- **Mitigation:**
  1. Decouple long-term persistence: store only ephemeral Google Place IDs (`places.id`), re-fetching details just-in-time via authorized client SDKs if required.
  2. Replace `GooglePlacesScraper` for bulk directory creation with licensed B2B providers whose ToS explicitly allow data resale/lead generation (e.g., Foursquare Places API, SerpApi/BrightData with compliance warranties, or commercial business registries).

---

#### Risk REG-02 — Erasure Impossibility & Re-Ingestion of Opted-Out Leads
- **Where:** `main.py:93-97`, `.github/workflows/scrape.yml:35-50`.
- **Failure Scenario & Root Cause:** Under Saudi PDPL Art. 28 and Lebanese Law 81/2018, data subjects have the right to request erasure and object to direct marketing. In `leadminer`, when a lead requests removal and is manually deleted from `sales_ready.csv` or `all_businesses.csv`, the next pipeline run re-downloads historical records or re-scrapes the entity from OSM/Google Places. Because there is no persistent suppression tombstone table, `dedup.py` merges the re-scraped business right back into the master dataset. Furthermore, `.github/workflows/scrape.yml:46-50` archives immutable timestamped folders (`gdrive:leads/data/$TIMESTAMP/`), meaning personal data persists indefinitely in historical snapshots across Drive.
- **Likelihood:** **4 (High)** — Inevitable as outbound outreach scales to thousands of businesses.
- **Impact:** **3 (Moderate)** — Regulatory complaints for re-contacting opted-out entities; hostile interactions with business owners; legal non-compliance notices.
- **Expected Loss:** **12 (Moderate)**.
- **Early Warning Signal:** Duplicate opt-out requests from the same company; complaints from business owners stating "I already told you to remove me last month."
- **Mitigation:**
  1. Implement a persistent, encrypted **Suppression Store** (containing cryptographically hashed phone numbers, emails, and domains) that sits outside the scraping pipeline.
  2. Execute a mandatory suppression filter at the export boundary (`main.py:148`) ensuring no suppressed token can ever appear in any output CSV.
  3. Automate a tombstone propagation script that sweeps historical Google Drive archives to delete matching entries upon verified erasure requests (`062-right-to-erasure.md`).

---

#### Risk REG-03 — OSM ODbL Share-Alike Contamination of Commercial Datasets
- **Where:** `scrapers/osm.py:10-26`, `main.py:174-178`.
- **Failure Scenario & Root Cause:** OpenStreetMap data is licensed under the Open Database License (ODbL 1.0). Under ODbL §4.4, any "Derivative Database" created by merging OSM data with other proprietary data must be made publicly available under the ODbL upon distribution. `leadminer` merges OSM entities with proprietary Google Places and Wikidata records into a unified `all_businesses.csv` and distributes it to sales teams and clients. Neither the CSV headers nor the output metadata provide required ODbL attribution (*"© OpenStreetMap contributors"*). This exposes the company to copyright infringement claims and community license-enforcement actions.
- **Likelihood:** **3 (Medium)** — OSM Legal Working Group actively investigates commercial data vendors who bundle OSM data into closed commercial lists without attribution or share-alike compliance.
- **Impact:** **2 (Minor)** — Legal demands to publish the full commercial database under ODbL or purge all OSM-derived records from internal databases.
- **Expected Loss:** **6 (Minor)**.
- **Early Warning Signal:** Direct inquiries from the OpenStreetMap Foundation (OSMF) or community mappers regarding data provenance.
- **Mitigation:**
  1. Add mandatory attribution (`© OpenStreetMap contributors, ODbL 1.0`) to all pipeline outputs, documentation, and client exports.
  2. Implement architectural separation: maintain OSM records as a "Collective Database" rather than a merged derivative, keeping source provenance isolated per row.

---

### 2. Technical and Infrastructure Risks

#### Risk TECH-01 — Google Places 429 Unbounded Infinite Hang in CI
- **Where:** `scrapers/google_places.py:203-217`, `.github/workflows/scrape.yml:15`.
- **Failure Scenario & Root Cause:** In `google_places.py:213-216`, when the Google Places API returns HTTP 429, the scraper executes `time.sleep(30)` followed by `continue` inside an unbounded `while True:` loop. There is no retry limit, no exponential backoff, and no circuit breaker. In Google Maps Platform, HTTP 429 is returned not only for transient queries-per-second (QPS) spikes, but also for **daily quota exhaustion** and **billing cap exhaustion** (`RESOURCE_EXHAUSTED`). When a billing cap is reached, all 5 worker threads (`_WORKERS = 5`) enter this 30-second loop simultaneously and permanently. The GitHub Actions job sits frozen until it hits the hard runner ceiling at `timeout-minutes: 300` (`.github/workflows/scrape.yml:15`).
- **Likelihood:** **4 (High)** — Occurs whenever monthly budget caps, daily quotas, or network throttling are triggered.
- **Impact:** **3 (Moderate)** — Burns 300 GitHub Actions runner minutes per hung run (costing billable CI credits); completely blocks daily/weekly lead generation; masks the underlying quota exhaustion failure.
- **Expected Loss:** **12 (Moderate)**.
- **Early Warning Signal:** CI runs exceeding 45 minutes; GitHub Actions workflow cancellation alerts at exactly 300 minutes; repeated `[Google] Rate limited on '...'` lines in CI logs.
- **Mitigation:**
  1. Replace the infinite retry with a bounded retry policy: `MAX_RETRIES = 3` with exponential backoff (`min(30, 2**retries * 5)`).
  2. Abort query immediately on non-transient quota exhaustion and raise an alert.
  3. Mount a standardized `requests.adapters.HTTPAdapter` with `urllib3.util.Retry` across all scraper sessions (`014-google-places-quota.md`).

---

#### Risk TECH-02 — Unchecked SSRF & CI Runner Credential Exfiltration
- **Where:** `enricher.py:124, 146`, `.github/workflows/scrape.yml:46-50`.
- **Failure Scenario & Root Cause:** The website enricher uses a global `requests.Session` with `verify=False`, `timeout=8` (inactivity only), and unconstrained redirects (`allow_redirects=True`) across 40 concurrent threads (`enricher.py:146, 180`). Scraped business websites are unvalidated user-controlled inputs. A malicious actor could inject internal loopback URLs (`http://127.0.0.1:8080`, `http://localhost`) or cloud metadata endpoints (`http://169.254.169.254/latest/meta-data/`). In GitHub Actions or containerized cloud runners, this enables Server-Side Request Forgery (SSRF) to query runner metadata, dump instance identity tokens, or access local network proxies. Furthermore, slow loris connections or infinite gzip streams cause thread starvation across the 40 worker threads.
- **Likelihood:** **3 (Medium)** — Public crowd-sourced directories (OSM, Wikidata) frequently contain malicious, defaced, or adversarial URLs.
- **Impact:** **4 (Critical)** — Exfiltration of repository secrets, Google Drive rclone credentials (`RCLONE_CONFIG_GDRIVE_TOKEN`), or Google Maps API keys stored in runner environment variables.
- **Expected Loss:** **12 (Moderate)**.
- **Early Warning Signal:** Unusually long enrichment phases (>60 minutes); DNS resolution warnings for private IP blocks (`10.0.0.0/8`, `192.168.0.0/16`, `169.254.0.0/16`); outbound security alerts from cloud hosting providers.
- **Mitigation:**
  1. Enforce strict URL pre-validation: validate that scheme is strictly `http` or `https` and resolve DNS before dispatch.
  2. Reject all private, loopback, multicast, and link-local IP addresses (`ipaddress.is_private`, `is_loopback`, `is_link_local`).
  3. Set a strict socket-level streaming byte cap (`stream=True`, reading maximum 200 KB) and enforce `verify=True` (`008-enricher-ssrf-security.md`, `054-politeness-and-ssrf.md`).

---

#### Risk TECH-03 — Overpass Unhandled JSONDecodeError Thread Crash
- **Where:** `scrapers/osm.py:38-56`, `main.py:112-113`.
- **Failure Scenario & Root Cause:** `scrapers/osm.py:38-53` wraps `session.post` in a 3-attempt retry block catching `requests.RequestException`. However, `resp.json()` is executed on line 55 **outside the retry block**. When the public Overpass server experiences heavy load or times out internally (180s timeout requested at line 11), it often returns an HTTP 200 OK header followed by an incomplete, truncated JSON payload or an XML error block (`runtime error: Query timed out`). Calling `resp.json()` on this response raises `requests.exceptions.JSONDecodeError`. Because it is outside the `try/except`, the unhandled exception crashes the thread and bubbles up to `main.py:113`, aborting the entire OpenStreetMap extraction for Lebanon.
- **Likelihood:** **4 (High)** — Public Overpass instances routinely experience load spikes, slot congestion, and payload truncation during peak European/Levant daytime hours.
- **Impact:** **3 (Moderate)** — Complete loss of all OpenStreetMap leads for the entire run (dropping thousands of Lebanese SMB records); pipeline outputs contain only Wikidata and Google results.
- **Expected Loss:** **12 (Moderate)**.
- **Early Warning Signal:** Log output showing `JSONDecodeError: Expecting value: line 1 column 1` originating from `scrapers/osm.py:55`; sudden drop of 10,000+ records in `all_businesses.csv`.
- **Mitigation:**
  1. Move `resp.json()` inside the retry block.
  2. Catch `JSONDecodeError` alongside `requests.RequestException`.
  3. Validate that the parsed JSON contains an `"elements"` key and no `"remark"` error message.
  4. Migrate bulk country extraction from the live public Overpass instance to pre-compiled Geofabrik daily OSM extracts (`017-osm-overpass-query.md`, `062-bulk-osm-alternatives.md`).

---

### 3. Data-Quality and Pipeline Integrity Risks

#### Risk DATA-01 — Heuristic Phone Normalization & WhatsApp Reachability Collapse
- **Where:** `dedup.py:33-63`, `enricher.py:129-132, 204-206`, `main.py:82-90`.
- **Failure Scenario & Root Cause:** `dedup.py:45` checks only `len(digits) < 7`, prepending calling codes (`+961` or `+966`) without any numbering plan validation or mobile vs. landline classification. In Lebanon, geographic fixed lines start with prefixes `01`, `04`, `05`, `07`, `08`, `09` (e.g., Beirut `01 234 567` $\to$ `+9611234567`). In Saudi Arabia, geographic landlines use `011` (Riyadh), `012` (Jeddah), `013` (Dammam) (e.g., `011 456 7890` $\to$ `+966114567890`). Neither fixed landlines nor toll-free lines can receive WhatsApp messages. `enricher.py` and `main.py` assign `completeness_score` points and qualify these records as `sales_ready.csv`, routing them to reps with pitches for "WhatsApp lead capture funnels" (`pitch_recommender.py:86`). Over 50% of these numbers are physical desk landlines.
- **Likelihood:** **5 (Certain)** — Landlines represent 40%–60% of all public listings in traditional Middle Eastern retail, clinics, and professional services.
- **Impact:** **3 (Moderate)** — Sales reps clicking WhatsApp links encounter `Phone number is not on WhatsApp` errors on over half their queue, destroying rep trust, cratering dial efficiency, and wasting SDR payroll.
- **Expected Loss:** **15 (Critical)**.
- **Early Warning Signal:** Rep complaints regarding WhatsApp link failures; delivery error receipts on WhatsApp Web; SDR activity logs showing zero WhatsApp conversation openings.
- **Mitigation:**
  1. Replace regex surgery in `dedup.py` with Google's `phonenumbers` library (`phonenumbers.parse`, `phonenumbers.is_valid_number`, `phonenumbers.number_type`).
  2. Tag each number with its explicit telecommunications type (`MOBILE`, `FIXED_LINE`, `VOIP`, `TOLL_FREE`).
  3. Strictly enforce that a lead can only receive a WhatsApp pitch or be flagged as WhatsApp-ready if its number type is verified as `MOBILE` (`001-dedup-phone-normalization.md`, `055-phone-validation-deep.md`).

---

#### Risk DATA-02 — False-Positive Website Liveness & Inverted Pitching
- **Where:** `enricher.py:147-152`, `pitch_recommender.py:54-55`, `main.py:149-156`.
- **Failure Scenario & Root Cause:** In `enricher.py:147`, liveness is defined as `live = r.status_code < 500`. Consequently, HTTP status codes **404 Not Found**, **403 Forbidden**, **410 Gone**, and **429 Too Many Requests** are evaluated as `live = True`. Furthermore, parked domains (GoDaddy, Sedo) and Cloudflare anti-bot verification challenges serve HTTP 200 with generic placeholder text. This creates two catastrophic inversions:
  1. A truly broken website returning HTTP 404 is marked `website_live = True`, denied the +20 "dead website" lead score bonus, and pitched an "SEO audit" rather than a "Website rebuild".
  2. A legitimate, highly active enterprise site protected by Cloudflare WAF that returns 403 or 429 is marked `website_live = True` but skips contact parsing (`enricher.py:148`), stripping it of emails and social links and dropping it from `sales_ready.csv`.
- **Likelihood:** **5 (Certain)** — 15%–25% of SMB websites in Lebanon and KSA are expired, parked, or fronted by Cloudflare.
- **Impact:** **3 (Moderate)** — Reps cold-call business owners offering to "upgrade their active website" when the URL is a blank 404 page, or pitching a "complete website rebuild" to an enterprise with an active Cloudflare-protected e-commerce portal, destroying agency credibility.
- **Expected Loss:** **15 (Critical)**.
- **Early Warning Signal:** Sales reps reporting that "live" leads have broken domains; anomalies in `sales_ready.csv` where `website_live == True` but all contact fields are `None`.
- **Mitigation:**
  1. Define liveness strictly: `live = (200 <= r.status_code < 300)`.
  2. Implement soft-404 and parked domain detection by matching title and body text against signatures (`"domain parked"`, `"buy this domain"`, `"cPanel default page"`, `"Apache2 Ubuntu"`).
  3. Handle HTTP 403/429 as `INCONCLUSIVE_WAF` rather than `live = True` (`005-enricher-http-robustness.md`).

---

#### Risk DATA-03 — Cumulative Master Dataset Poisoning (Zero Ingestion Validation)
- **Where:** `main.py:46-71, 99-126`, `scrapers/base.py:5-29`.
- **Failure Scenario & Root Cause:** `BusinessRecord` is a `TypedDict`, which performs zero runtime type checking. When scrapers execute, unvalidated dictionaries flow directly into dedup, enrichment, and the master CSV (`all_businesses.csv`). Because `load_master()` re-loads historical CSV data on every run (`main.py:124`), a single corrupted record permanently poisons the master dataset. Corruptions include:
  - Empty strings for `name` collapsing multiple unrelated businesses under `("", city)` in `dedup.py:79`.
  - Malformed lat/lon coordinates (e.g., Null Island `(0, 0)`, negative coordinates, or lat/lon transpositions placing Lebanese businesses in the Mediterranean Sea).
  - Cross-border query pollution (e.g., Google Places query `"luxury hotels in Saudi Arabia"` returning a hotel in Dubai, UAE, which is stamped as `country="SA"` and `region=None`).
- **Likelihood:** **4 (High)** — Unvalidated scraping across diverse third-party APIs inevitably encounters edge cases and schema drift.
- **Impact:** **3 (Moderate)** — Dataset corruption compounds over time; invalid entries skew regional analytics, break geographic filtering, and waste enrichment HTTP requests on invalid targets.
- **Expected Loss:** **12 (Moderate)**.
- **Early Warning Signal:** Increase in `region == None` counts in `all_businesses.csv`; coordinate outliers outside national bounding boxes; duplicate business names with mismatched industries.
- **Mitigation:**
  1. Implement **Gate 1 Ingestion Validation** immediately following scraping: enforce Pydantic/dataclass schema parsing.
  2. Bounding-box validation: assert `33.05 <= lat <= 34.70` and `35.10 <= lon <= 36.65` for Lebanon; assert Saudi national bounds for KSA.
  3. Divert all failing records to an isolated `data/quarantined_businesses.csv` with explicit rejection codes (`048-data-quality-validation.md`).

---

#### Risk DATA-04 — Dedup Identity Collapse & Data Loss on Collisions
- **Where:** `dedup.py:34-38, 70-85, 100-116`.
- **Failure Scenario & Root Cause:** When a record lacks a phone number, `dedup.py:79` hashes identity using `(normalized_name, city)`. However, `osm.py:77-84` discards informal addresses (`addr:full`), leaving `address = None` and `city = ""`. Unrelated businesses sharing generic commercial names (e.g., "Al-Arz Bakery", "Central Pharmacy", "Star Dental Clinic") across different towns collapse into a single merged entity. Furthermore, `_merge()` (`dedup.py:100-116`) resolves conflicting fields using an arbitrary "winner-takes-all" rule where the first-processed record wins non-empty values. If an unverified, incomplete record is processed before an enriched record, valid phone numbers, emails, or review counts are permanently dropped.
- **Likelihood:** **4 (High)** — High collision rate for common Arabic and French business names across Lebanon and Saudi Arabia.
- **Impact:** **2 (Minor)** — Merges distinct businesses into "Frankenstein" records; silent loss of valid contact information.
- **Expected Loss:** **8 (Moderate)**.
- **Early Warning Signal:** Disproportionately low unique business counts in dense categories (bakeries, pharmacies); records where address says "Tripoli" but region says "Beirut".
- **Mitigation:**
  1. Parse `tags.get("addr:full")` in `osm.py` to prevent blank city values.
  2. Do not merge phone-less records based solely on `(name, city)` unless geographic coordinates match within $\le 50$ meters.
  3. Upgrade `_merge()` to use field-level confidence weighting (e.g., Google Places overrides OSM for phone/rating; OSM overrides Google Places for category) (`002-dedup-merge-data-loss.md`, `003-dedup-identity-model.md`).

---

### 4. Business, Commercial and Operational Risks

#### Risk BIZ-01 — Sales Rep Abandonment ("The Rep Desertion Loop")
- **Where:** `main.py:82-90, 149-153`, `pitch_recommender.py:25-99`, `enricher.py:275-312`.
- **Failure Scenario & Root Cause:** `sales_ready.csv` is dumped as a raw, non-deterministic spreadsheet. Sales development reps (SDRs) encounter an operational breakdown across five distinct friction points:
  1. **Mojibake:** CSVs written with standard `utf-8` without BOM (`main.py:74`) render Arabic business names (`مؤسسة النور`) as unreadable gibberish (`Ø§Ù„Ù†ÙˆØ±`) in Microsoft Excel on Windows.
  2. **Phantom Contacts:** A record with `phone=None`, `email=None`, and `instagram="explore"` is marked sales-ready (`main.py:82-90`).
  3. **Opaque Scoring:** Reps receive an arbitrary integer (`lead_score: 85`) with no explanation of why the business is viable or what diagnostic evidence triggered it.
  4. **Embarrassing Pitches:** Rigid pitch strings (e.g., "Website rebuild") applied to businesses with modern active sites or 404 domains.
  5. **No Immutable ID:** Lack of `lead_id` means reps cannot track activity or sync with CRM.
  After experiencing 5–10 bad leads consecutively, reps abandon the dataset entirely, reverting to manual Google/LinkedIn prospecting.
- **Likelihood:** **5 (Certain)** — Inevitable when handing raw scraper dumps to commission-driven outbound sales teams.
- **Impact:** **4 (Critical)** — Complete commercial failure: SDR payroll is wasted, sales velocity stalls, and the entire lead generation pipeline fails to deliver revenue.
- **Expected Loss:** **20 (Catastrophic)**.
- **Early Warning Signal:** Reps refusing to work from `sales_ready.csv`; SDR dial volume dropping by >50%; Excel formatting help tickets; CRM pipeline remaining unpopulated.
- **Mitigation:**
  1. Add UTF-8-BOM (`encoding="utf-8-sig"`) to all CSV exports.
  2. Generate deterministic, immutable `lead_id` (UUIDv5) to enable CRM synchronization.
  3. Implement **Sales Handoff Contract**: require at least one verified direct channel (E.164 mobile or verified email), pre-generate clickable `whatsapp_click_url` links, and provide human-readable `pitch_reason` battle-cards explaining the diagnosis (`060-sales-handoff-contract.md`, `063-output-contract-for-humans.md`).

---

#### Risk BIZ-02 — Open-Loop Feedback Starvation & Negative Drift
- **Where:** Entire codebase (`main.py:93-184`, `enricher.py:275-312`).
- **Failure Scenario & Root Cause:** The pipeline is entirely open-loop. There is no feedback ingestion mechanism to record sales outreach outcomes (`contacted`, `wrong_number`, `gatekeeper_block`, `closed_won`, `closed_lost`). Consequently:
  - Leads with disconnected phones or hostile owners remain in the master and are repeatedly re-exported month after month.
  - Hardcoded heuristic weights (`enricher.py:275-312`: `email: +20`, `whatsapp: +15`, `rating < 4: +10`) are never calibrated against actual conversion data.
  - False-positive dead websites (e.g., Cloudflare blocks) continue receiving +20 bonuses on every subsequent run.
- **Likelihood:** **4 (High)** — Inevitable when operating without a bidirectional CRM integration.
- **Impact:** **3 (Moderate)** — Continuous erosion of sales efficiency; reps waste time re-dialing previously disqualified prospects; system prioritizes low-converting leads.
- **Expected Loss:** **12 (Moderate)**.
- **Early Warning Signal:** Reps complaining that they are calling the same dead numbers they reported weeks ago; stagnant conversion rates despite algorithmic scoring tweaks.
- **Mitigation:**
  1. Create a standardized outcome feedback schema (`lead_outcomes.csv` / CRM webhook).
  2. Implement an automated CLI command (`leadminer feedback import <path>`) that updates record disposition status.
  3. Dynamically down-weight or suppress records tagged with `wrong_number`, `permanently_closed`, or `not_interested` (`060-sales-handoff-contract.md`).

---

#### Risk BIZ-03 — Lead Staleness & Stagnant Master Accumulation
- **Where:** `main.py:46-71, 123-153`.
- **Failure Scenario & Root Cause:** `all_businesses.csv` accumulates records indefinitely without a time-to-live (TTL), expiration window, or verification timestamp. The schema records `scraped_at` but lacks `last_verified_at`. In the volatile SMB markets of Beirut and Riyadh, restaurants, retail shops, and clinics frequently close, rebrand, or change phone numbers (annual churn exceeds 20% in Levant SMBs). Over 12–24 months, the master dataset fills with zombie businesses that continue to pass completeness filters and enter sales queues.
- **Likelihood:** **4 (High)** — Certain to degrade dataset accuracy over operational quarters.
- **Impact:** **2 (Minor)** — Reps waste 15%–30% of calls attempting to reach closed businesses; inflated pipeline metrics mislead executive leadership.
- **Expected Loss:** **8 (Moderate)**.
- **Early Warning Signal:** SDR reports of "this number is disconnected" or "business permanently closed"; Google Maps listings showing "Permanently Closed" on manual review.
- **Mitigation:**
  1. Add `last_verified_at` and `status` (`ACTIVE`, `STALE`, `CHURNED`) to `BusinessRecord`.
  2. Enforce a 90-day lead aging policy: leads not re-verified within 90 days are excluded from `sales_ready.csv` until refreshed.
  3. Archive records inactive for >180 days to cold storage (`035-incremental-scraping.md`, `062-right-to-erasure.md`).

---

#### Risk FIN-01 — Google Places Text Search Enterprise SKU Cost Trap
- **Where:** `scrapers/google_places.py:31-42`, `docs/audits/073-cost-model.md`.
- **Failure Scenario & Root Cause:** Google Maps Platform charges Text Search (New) requests based on requested field masks. Including `places.rating` and `places.userRatingCount` in `google_places.py:35-36` automatically escalates every call to the **Text Search Enterprise SKU ($35.00 / 1,000 requests)** rather than Pro ($32/1k) or Basic. Currently, with 67 queries executed monthly (yielding $\le 201$ billable requests), the pipeline stays within Google's $200 monthly free credit tier ($0.00 actual cost). However, attempting to scale lead volume to 50,000 or 100,000 records requires hundreds of queries and permutations. At that scale, Google Places billing spikes to **$3,500 to $17,500 per month**, destroying customer acquisition economics.
- **Likelihood:** **3 (Medium)** — Triggered immediately upon attempting geographic scaling across the Kingdom of Saudi Arabia.
- **Impact:** **2 (Minor)** — Financial loss from unexpected cloud bills; forced throttling of lead acquisition.
- **Expected Loss:** **6 (Minor)**.
- **Early Warning Signal:** Google Cloud billing alerts crossing $100 threshold; high volume of API calls logged in Google Cloud Console.
- **Mitigation:**
  1. Remove `places.rating` and `userRatingCount` from the initial discovery field mask if not strictly required, dropping costs or relying on Basic/Pro SKUs.
  2. For large-scale footprint discovery, use bulk open datasets (Geofabrik OSM) or low-cost SERP aggregators (SerpApi / BrightData), reserving Google Places API strictly for high-priority targeted enrichment (`014-google-places-quota.md`, `073-cost-model.md`).

---

## Not a Bug, But Worth Knowing

1. **Sole Proprietor PII Ambiguity in Lebanon & KSA:** While corporate directory data (`info@company.com`, general corporate landlines) is not personal data, SMB listings in Lebanon and Saudi Arabia routinely use personal mobile numbers (`+966 5...`, `+961 3...`) and personal email addresses (`dr.ahmad@gmail.com`). Regulators in both jurisdictions treat these as personal data subject to full privacy protections, regardless of being publicly visible on storefronts or websites.
2. **ODbL Viral Share-Alike vs. CRM Enclosure:** If an agency enriches OSM data and redistributes the resulting dataset to a third party (e.g., selling a lead list or uploading to a shared multi-tenant CRM), ODbL §4.4 technically mandates that the derivative database be published under ODbL. Keeping the leads strictly for internal marketing by the controller avoids the public distribution trigger.
3. **GitHub Actions Infrastructure Terms of Service:** Running high-concurrency web scrapers (40 threads making thousands of external HTTP GETs) on GitHub-hosted Actions runners borders on violating GitHub's Acceptable Use Policies regarding automated network scanning and high-bandwidth scraping. Moving execution to a dedicated low-cost VPS (Hetzner, DigitalOcean) prevents repository suspension.
4. **LLM Enrichment Hallucination Risks:** If future teams introduce LLM agents to summarize websites or craft personalized pitches (`059-llm-enrichment.md`), prompt injection via adversarial website content (e.g., invisible text on SMB sites designed to hijack prompt instructions) can compromise automated pitch generation.

---

## Recommended Order of Work (De-risking Roadmap)

### Phase 1: Operational Kill Switches & Regulatory Gating (Immediate / Pre-Deployment)
1. **Quarantine the Google Places Persistence:** Remove Google Places from cumulative CSV storage or replace with licensed data sources to eliminate ToS termination risks (`VEN-02`).
2. **Establish the WhatsApp Outbound Boundary:** Hardcode an architectural prohibition against routing scraped numbers into automated WhatsApp Cloud API dispatch queues; enforce human-in-the-loop `wa.me` links only (`VEN-01`).
3. **Stand Up Saudi PDPL Data Isolation:** Segregate Saudi leads into dedicated storage with strict B2B calling protocols and a complete prohibition on cold automated messaging (`REG-01`).
4. **Deploy Persistent Suppression Store:** Build an independent suppression table that gates all CSV exports and prevents opted-out leads from being re-ingested (`REG-02`).

### Phase 2: Pipeline Hardening & Quality Defense (Sprint 1)
1. **E.164 & Number-Type Classification:** Integrate `phonenumbers` to validate all phone strings and restrict WhatsApp pitch qualification strictly to verified `MOBILE` lines (`DATA-01`).
2. **Two-Gate Validation Layer:** Implement Gate 1 (Ingestion) schema/coordinate bounds checks and Gate 2 (Pre-Export) referential sanity rules; divert failures to `quarantined_businesses.csv` (`DATA-03`).
3. **Liveness & Soft-404 Remediation:** Overhaul `enricher.py` to evaluate liveness strictly on `200 <= status < 300`, detect parked domain signatures, and treat WAF blocks as inconclusive (`DATA-02`).
4. **Bounded Retry & Network Robustness:** Eliminate infinite 429 loops in `google_places.py`, add SSRF IP checks in `enricher.py`, and move `resp.json()` inside the retry block in `osm.py` (`TECH-01`, `TECH-02`, `TECH-03`).

### Phase 3: Sales Enablement & Ergonomics (Sprint 2)
1. **Deliver the Sales Handoff Contract:** Add UTF-8-BOM (`utf-8-sig`) export encoding, deterministic `lead_id` (UUIDv5), clickable `wa.me` URLs, and plain-language `pitch_reason` battle-cards (`BIZ-01`).
2. **Establish Closed-Loop Feedback Ingestion:** Implement `leadminer feedback import` to ingest SDR call dispositions and update lead scoring weights based on conversion outcomes (`BIZ-02`).
3. **Lead Lifecycle & Aging Expiry:** Implement `last_verified_at` timestamps and a 90-day lead aging exclusion policy to purge stale businesses from active sales exports (`BIZ-03`).
