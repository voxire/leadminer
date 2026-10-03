# 043 — Transparent, Versioned, and Calibratable Lead Scoring Engine

## Verdict

The legacy `lead_score()` function is fatally flawed: 60% of its score is a duplicative tally of reachability fields already present in `completeness_score`, and its website opportunity logic suffers from a catastrophic ranking inversion. Because `enricher.py:151-153` catches all network exceptions and returns `False`, any business that blocks the crawler (Cloudflare WAF, bot challenges, rate limits, or regional network latency) is stamped `website_live = False` and awarded a **+20 point dead-site bonus** (`enricher.py:244`), while a working website receives only **+10 points** (`enricher.py:242`) and a business with *no website at all* receives **+0 points**. As a result, the highest-tech, best-protected regional enterprises outrank actual sales targets and receive embarrassing "Website rebuild" pitches. We must replace this 32-line procedural if-chain with an orthogonal, config-driven scoring engine ($Score = f(\text{Opportunity}, \text{Reachability}, \text{Confidence})$) backed by an explicit multi-state HTTP probe (`website_status`), strict semantic versioning (`score_version`), and an auditable sales override mechanism.

---

## Findings

### S1 — Crawler blocks and network timeouts invert lead ranking; dead sites outrank live ones and absent sites are zeroed
- **Where:** `enricher.py:151-153` (in `_fetch_website()`), consumed at `enricher.py:241-244` (in `lead_score()`).
- **Breaks:** In `_fetch_website()`, any request timeout (the 8s socket timeout at `:146`), TLS handshake failure, connection reset, or Cloudflare/Incapsula bot-block triggers `except Exception: return False, contacts`. This sets `record["website_live"] = False`. In `lead_score()`, lines 241–244 execute:
  ```python
  if record.get("website_live") is True:
      score += 10
  elif record.get("website") and record.get("website_live") is False:
      score += 20  # dead website = sales opportunity
  ```
  Consequently:
  1. A well-defended enterprise with Cloudflare bot mitigation throws a challenge or 403, triggers the exception handler, is marked `website_live = False`, and receives **+20 points**.
  2. A business whose site loads cleanly with 200 OK receives **+10 points**.
  3. A business with *no website at all*—the agency's primary greenfield target (`BRIEF.md:8`, `BRIEF.md:54`, `main.py:140`)—receives **0 points**.
  This inverts reality: well-funded companies that block automated scraping outrank functional SMBs, while genuine new-site prospects sit at the bottom of the lead list. Furthermore, `pitch_recommender.py:52-55` routes these blocked sites into Tier 1 *"Website rebuild + maintenance"*, prompting outbound sales reps to pitch a website rebuild to a company whose site is fully functional.
- **Trigger:** Probe `https://almarai.com` or any Cloudflare-protected regional business. An unhandled TLS challenge or bot-block yields `website_live = False`. With phone and category, it scores 70–80 points and gets tagged for a website rebuild. A restaurant down the street with no website scores 40 points and is buried.
- **Fix:** Replace binary `website_live: bool` with an explicit categorical `website_status` enum (`ABSENT`, `LIVE_VERIFIED`, `PARKED_OR_FORWARDED`, `CONFIRMED_DEAD`, `CRAWL_BLOCKED`, `INDETERMINATE`). Award opportunity points only for `CONFIRMED_DEAD` (persistent 404/410 or NXDOMAIN) and `ABSENT`; assign neutral or zero opportunity to `CRAWL_BLOCKED` and `INDETERMINATE`.

### S1 — Lead score is additive and conflates Reachability with Pitch Opportunity
- **Where:** `enricher.py:231-259`
- **Breaks:** `lead_score()` treats reachability and opportunity as additive terms in a single scalar sum:
  - Reachability: `email` (+20), `whatsapp` (+15), `phone` (+15), `instagram` (+10) = **60 points**.
  - Opportunity: dead website (+20) or live website (+10), rating < 4.0 (+10) = **20–30 points**.
  - Categorization: priority (+15), multi-source (+5) = **20 points**.
  Because 60% of the score measures reachability (identical to `completeness_score`, `enricher.py:101-117`), a business that is completely optimized (working website, 4.9★ rating, active Instagram, email, phone, WhatsApp) scores **75 to 85 out of 100**, while a business that is in desperate need of digital services (no website, 3.1★ rating) but only has a phone number scores **35 to 45 out of 100**. The additive model ranks leads you *can* talk to above leads that actually *need* what the agency sells.
- **Trigger:** Compare an established dental clinic (phone, email, WhatsApp, Instagram, live site, 4.8★) scoring **75**, against an un-digitized distributor (phone, no website, 3.2★) scoring **40**. The clinic is pitched nothing useful; the distributor is an ideal client but deprioritized.
- **Fix:** Decompose the score into two orthogonal dimensions: **Opportunity Score** ($S_{\text{opp}} \in [0, 100]$) and **Reachability Score** ($S_{\text{reach}} \in [0, 100]$), combined via a gated formula: a lead with zero commercial opportunity must never score in the top tier, regardless of how many contact channels it possesses.

### S2 — Hardcoded weights and absence of `score_version` prevent calibration and corrupt historical runs
- **Where:** `enricher.py:227-260`, `scrapers/base.py:25`
- **Breaks:** Weights are hardcoded literals inside `enricher.py`. There is no metadata indicating what scoring rubric generated a row's `lead_score`. When a developer alters a weight (e.g., changing WhatsApp from 15 to 10), subsequent merge runs (`main.py:124`) combine newly calculated scores with legacy scores stored in `all_businesses.csv`. Historical tracking, model backtesting, and conversion attribution become mathematically impossible because "80" in January means something different than "80" in March.
- **Trigger:** Merge a newly scraped batch into an existing `all_businesses.csv` master after adjusting scoring logic. Rows scored under different algorithms are sorted together in `sales_ready.csv`.
- **Fix:** Introduce an external configuration file (`scoring_config.yaml`), emit a mandatory `score_version` string (e.g., `v2.1.0`), and store sub-score components (`score_opportunity`, `score_reachability`, `score_confidence`) in output records.

### S2 — Lifecycle race: `lead_score()` is executed twice with inconsistent feature inputs
- **Where:** `enricher.py:290` vs `main.py:132-137`
- **Breaks:** `enricher.enrich()` calls `lead_score(r)` at line 290 before `industry_priority` has been evaluated. At that stage, `r.get("industry_priority")` is `None`, so line 246 evaluates to False and forfeits up to 15 points. `main.py:137` recalculates `lead_score` after assigning `industry_priority`, masking the bug in the main CLI run. However, any automated test, notebook analysis, or standalone service invoking `enrich()` gets defective, degraded scores.
- **Trigger:** Call `from enricher import enrich; enriched = enrich(sample_records)` and inspect `enriched[0]["lead_score"]`. The score is missing industry priority points.
- **Fix:** Remove `lead_score` invocation from `enricher.py`. Lead scoring is an analytical evaluation stage that must run strictly after all enrichment and classification stages are finalized.

### S3 — Zero sales override mechanism causes rep churn and manual data loss
- **Where:** `main.py:124-126`, `main.py:149-153`
- **Breaks:** There is no schema or workflow for sales reps or SDRs to override an erroneous score (e.g., verifying by hand that a site is indeed dead, or marking a lead as disqualified because the phone is disconnected). Because `main.py:124-126` deduplicates and overwrites records based on scraper runs, any manual edits made by sales reps to CSV exports are obliterated on the next pipeline execution.
- **Trigger:** An SDR manually edits `sales_ready.csv` to flag a lead as "dead number" or changes score from 85 to 10. The next scheduled pipeline run regenerates `sales_ready.csv` from source scrapers, wiping out the rep's work.
- **Fix:** Establish an explicit overrides ledger (`data/overrides.csv` or DB table) with schema `(business_id, score_override, override_reason, overridden_by, updated_at)` that is merged deterministically during pipeline execution.

---

## The Root Cause: Why Sites That Block the Crawler Outrank Live Sites

To understand how the inversion occurs, trace a single HTTP request through `enricher.py`:

```
                    _SESSION.get(url, timeout=8, verify=False)
                                       │
        ┌──────────────────────────────┴──────────────────────────────┐
        ▼                                                             ▼
   HTTP Response Received                                     Exception Raised
   (status code available)                       (Timeout, SSLError, ConnectionError,
        │                                         WAF TCP Reset, Cloudflare Drop)
        │                                                             │
  r.status_code < 500?                                                │
  ┌─────┴─────┐                                                       │
  ▼           ▼                                                       │
True        False (5xx)                                               │
  │           │                                                       │
r.status >= 400?                                                      │
┌─┴─┐         │                                                       │
▼   ▼         ▼                                                       ▼
T   F    live = False                                            live = False
│   │    contacts = empty                                        contacts = empty
│   │         │                                                       │
│   └─────────┼───────────────────────────────────────────────────────┘
│             │
│             ▼
│    `record["website_live"] = False`
│             │
│             ▼
│    In `lead_score()`:
│    `elif record.get("website") and record.get("website_live") is False:`
│             │
│             ▼
│    ★ AWARDS +20 POINTS (DEAD SITE BONUS) ★
│    ★ ROUTES TO "Website rebuild + maintenance" ★
│
▼
`live = True` (Includes 404, 403, 429!)
contacts = empty
      │
      ▼
In `lead_score()`:
`if record.get("website_live") is True:`
      │
      ▼
★ AWARDS +10 POINTS (LIVE SITE) ★
```

### The Three Pathologies
1. **The Crawler-Block Vulnerability:** Sophisticated companies run Web Application Firewalls (Cloudflare, AWS WAF, Imperva). When `leadminer` hits them with `User-Agent: Mozilla/5.0 (compatible; leadminer/1.0)` (`enricher.py:125`) over 40 parallel threads without browser TLS fingerprints, the WAF drops the connection or resets the TCP socket. In Python, this raises `requests.exceptions.ConnectionError` or `ProtocolError`. The code catches `Exception`, returns `live = False`, and awards **+20 points**.
2. **The 4xx Perversion:** Line 147 defines `live = r.status_code < 500`. A canonical dead page returning HTTP 404 Not Found has `status_code = 404 < 500`, so it is marked `live = True`, gets only **+10 points**, and misses the rebuild pitch!
3. **The Greenfield Exclusion:** A business that has never had a website (`record["website"] is None`) falls through both branches and gets **+0 points**, despite being the easiest sales motion in the agency portfolio ("Website launch").

---

## Architectural Redesign: The Multi-State Website Verification Model

Binary `website_live: bool` must be decommissioned. In its place, the enrichment pipeline must produce a structured `website_status` enum and a diagnostic `probe_http_status` code:

```
                  ┌────────────────────────────────────────────────────────┐
                  │                 Input Website URL                      │
                  └──────────────────────────┬─────────────────────────────┘
                                             │
                       ┌─────────────────────┴─────────────────────┐
                       ▼                                           ▼
                 URL is Empty                               URL is Provided
                       │                                           │
                       ▼                                           ▼
             `website_status = ABSENT`                    Standard HTTP Probe
                                                  (Follow max 5 redirects, TLS verify)
                                                                   │
         ┌───────────────────┬───────────────────┬─────────────────┴─────────────────┬───────────────────┐
         ▼                   ▼                   ▼                                   ▼                   ▼
    2xx Success       3xx/Social Hop       404/410 Dead                        403/429 Blocked       Network Fail
         │                   │                   │                                   │                   │
  Content Check       Target Host Check   DNS Retry Check                     Header/Body Check     Retry (Backoff)
         │                   │                   │                                   │                   │
    ┌────┴────┐              │              ┌────┴────┐                         ┌────┴────┐              │
    ▼         ▼              ▼              ▼         ▼                         ▼         ▼              ▼
  Valid    Parked/        Social /       Domain    Page Dead                 Challenge/  Blocked       Socket/DNS
 Content   Placeholder   Aggregator     NXDOMAIN                               WAF                     Timeout
    │         │              │              │         │                         │         │              │
    ▼         ▼              ▼              └────┬────┘                         └────┬────┘              │
  LIVE_    PARKED_        REDIRECT_              ▼                                   ▼                   ▼
 VERIFIED DOMAIN           SOCIAL         CONFIRMED_DEAD                       CRAWL_BLOCKED       INDETERMINATE
```

### Enumeration of Website States

| `website_status` | Operational Meaning | Diagnostic Criteria | Opportunity Treatment |
|---|---|---|---|
| `ABSENT` | Business has no registered website. | `website` field is null or empty. | **High (+25 pts)**: Greenfield website build target. |
| `CONFIRMED_DEAD` | Registered domain is completely down, expired, or removed. | HTTP 404/410, domain NXDOMAIN, or connection refused across 2 retry attempts. | **High (+25 pts)**: Rebuild + recovery pitch. |
| `PARKED_DOMAIN` | Domain is registered but dormant or parked for resale. | HTTP 200/302 returning known parking broker signatures (Sedo, GoDaddy, Dan, Namecheap). | **High (+25 pts)**: Domain activation / launch pitch. |
| `REDIRECT_SOCIAL` | Domain redirects directly to Instagram, WhatsApp, or Facebook. | Final URL domain matches `instagram.com`, `facebook.com`, `wa.me`. | **Medium-High (+20 pts)**: Social-to-web conversion funnel pitch. |
| `LIVE_VERIFIED` | Site is online, accessible, and serving active business content. | HTTP 200, valid SSL, non-parked HTML body > 500 bytes. | **Low for web dev (+0 pts)**; Opportunity derived from SEO/reputation deficits. |
| `CRAWL_BLOCKED` | Host rejected crawler or presented a challenge. | HTTP 403, 429, Cloudflare challenge, or CAPTCHA detected. | **Neutral (+0 pts)**: Quarantine. Never treat as dead. Flag for manual inspection. |
| `INDETERMINATE` | Transient network error, socket timeout, or routing failure. | Read timeout (>10s), TLS handshake error, or network unreachable. | **Neutral (+0 pts)**: Mark for retry. Zero score distortion. |

---

## Transparent Feature Set & Mathematical Scoring Model

The new model abandons the flat 100-point sum. Instead, it evaluates leads across **three orthogonal, transparent sub-scores**:

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                       FINAL LEAD SCORE                                           │
│                       Final Score = Round( (0.60 · Sopp + 0.40 · Sreach) · Sconf )               │
│                                           Range: 0 – 100                                         │
└─────────────────────────────────┬──────────────────────────────────────┬─────────────────────────┘
                                  │                                      │
            ┌─────────────────────┴──────────────────┐ ┌─────────────────┴───────────────────┐
            ▼                                        ▼ ▼                                     ▼
 ┌──────────────────────┐                 ┌──────────────────────┐                ┌──────────────────────┐
 │  Opportunity Score   │                 │  Reachability Score  │                │  Confidence Factor   │
 │        (Sopp)        │                 │       (Sreach)       │                │       (Sconf)        │
 │    Weight: 60%       │                 │     Weight: 40%      │                │   Multiplier: 0.7-1.0│
 ├──────────────────────┤                 ├──────────────────────┤                ├──────────────────────┤
 │• Website Deficit (35)│                 │• Phone Verified (30) │                │• Multi-source (1.0)  │
 │• Rating Pain (20)    │                 │• WhatsApp Active (30)│                │• Single Source (0.85)│
 │• Social Deficit (15) │                 │• Direct Email (25)   │                │• Geo-precise (1.0)   │
 │• Vertical Need (30)  │                 │• Social Inbox (15)   │                │• Inferred Region(0.9)│
 └──────────────────────┘                 └──────────────────────┘                └──────────────────────┘
```

### 1. Opportunity Score ($S_{\text{opp}} \in [0, 100]$)
Measures the commercial value of pitching digital services to this business:

$$S_{\text{opp}} = \min(100, W_{\text{web}} + W_{\text{rating}} + W_{\text{social}} + W_{\text{vertical}})$$

- **Website Deficit ($W_{\text{web}}$, Max 35 pts):**
  - `ABSENT`: **+35 pts** (Cleanest greenfield web dev pitch).
  - `CONFIRMED_DEAD`: **+35 pts** (High urgency rebuild pitch).
  - `PARKED_DOMAIN`: **+30 pts** (Ready domain, needs site).
  - `REDIRECT_SOCIAL`: **+25 pts** (Needs owned digital funnel).
  - `LIVE_VERIFIED`: **+0 pts** (Has site; web dev pitch invalid).
  - `CRAWL_BLOCKED` / `INDETERMINATE`: **+0 pts** (Neutral; cannot verify deficit).
- **Reputation & Visibility Deficit ($W_{\text{rating}}$, Max 20 pts):**
  - Low rating ($1.0 \le \text{rating} < 3.8$ with $\ge 5$ reviews): **+20 pts** (Reputation management / review engine pitch).
  - Zero review footprint (`review_count` == 0 or null, but business exists): **+15 pts** (Local SEO & Google Maps optimization pitch).
  - Moderate rating ($3.8 \le \text{rating} < 4.3$): **+8 pts**.
  - Stellar rating ($\ge 4.5$ with $\ge 50$ reviews): **+0 pts** (Reputation already optimal).
- **Social Media Presence Deficit ($W_{\text{social}}$, Max 15 pts):**
  - Has phone/business but zero social accounts: **+15 pts** (Social setup & brand kit pitch).
  - Has single social account (Instagram or Facebook): **+5 pts**.
  - Multi-channel social presence: **+0 pts**.
- **Vertical Strategic Priority ($W_{\text{vertical}}$, Max 30 pts):**
  - High-ticket verticals (Medical/Dental Clinics, High-End Hospitality, Real Estate, Law, Architecture): **+30 pts**.
  - Medium-ticket verticals (Retail, Contractors, Specialized Salons, Wholesalers): **+18 pts**.
  - Low-ticket / commodity verticals (Small grocery, kiosk, gas station): **+5 pts**.

### 2. Reachability Score ($S_{\text{reach}} \in [0, 100]$)
Measures the SDR's ability to initiate a real-time outbound conversation:

$$S_{\text{reach}} = \min(100, R_{\text{phone}} + R_{\text{wa}} + R_{\text{email}} + R_{\text{social}})$$

- **Direct Voice / Mobile ($R_{\text{phone}}$, Max 30 pts):**
  - Validated national mobile number (e.g., Lebanese `+961 70/71/76/81` or Saudi `+966 5x`): **+30 pts**.
  - Validated landline / standard phone: **+20 pts**.
  - Unformatted or invalid string: **+0 pts**.
- **Direct Messaging ($R_{\text{wa}}$, Max 30 pts):**
  - Verified active WhatsApp / WA Business link: **+30 pts** (Primary closing channel in LB/KSA).
- **Decision Maker Direct Email ($R_{\text{email}}$, Max 25 pts):**
  - Company domain email (`info@business.com`, `owner@business.com`): **+25 pts**.
  - Generic email (`business@gmail.com`): **+15 pts**.
  - Scraped registrar/host email (GoDaddy, cPanel, Wix): **+0 pts** (Strictly blacklisted).
- **Social Direct Message Channel ($R_{\text{social}}$, Max 15 pts):**
  - Active Instagram handle or LinkedIn page: **+15 pts**.

### 3. Confidence Multiplier ($S_{\text{conf}} \in [0.70, 1.00]$)
Discounts records with unverified or conflicting data:

$$S_{\text{conf}} = C_{\text{sources}} \times C_{\text{geo}} \times C_{\text{web\_probe}}$$

- **Multi-Source Cross-Validation ($C_{\text{sources}}$):**
  - Multiple sources (e.g. `google_places|osm`): **1.00**.
  - Single source (`google_places`): **0.90**.
  - Single unconfirmed source (`wikidata` or `osm` node without phone): **0.80**.
- **Geographic Precision ($C_{\text{geo}}$):**
  - Verified coordinates (`lat`, `lon` within target country bounding box): **1.00**.
  - Regex-inferred region from address string: **0.95**.
  - Region unknown: **0.85**.
- **Web Probe Integrity ($C_{\text{web\_probe}}$):**
  - `LIVE_VERIFIED`, `ABSENT`, `CONFIRMED_DEAD`: **1.00** (State is known with certainty).
  - `PARKED_DOMAIN`, `REDIRECT_SOCIAL`: **0.95**.
  - `CRAWL_BLOCKED`: **0.85** (State uncertain; penalized confidence).
  - `INDETERMINATE`: **0.75** (Transient probe failure).

### The Composite Formulation

$$\text{Final Lead Score} = \text{Round}\left( \left( 0.60 \cdot S_{\text{opp}} + 0.40 \cdot S_{\text{reach}} \right) \times S_{\text{conf}} \right)$$

#### Concrete Validation Cases

| Scenario | $S_{\text{opp}}$ | $S_{\text{reach}}$ | $S_{\text{conf}}$ | Legacy Score | **New Score** | Behavioral Outcome |
|---|---|---|---|---|---|---|
| **Case A: Greenfield Restaurant** (No website, mobile phone, Instagram, high priority) | 35 (no site) + 15 (no email) + 30 (priority) = **80** | 30 (phone) + 15 (IG) = **45** | 1.00 | 40 | **66 (P2 Warm)** | Properly surfaced as an active prospect. |
| **Case B: Confirmed Dead Clinic** (404 page, phone, WhatsApp, high priority) | 35 (dead) + 30 (priority) = **65** | 30 (phone) + 30 (WA) = **60** | 1.00 | 65 | **63 (P2 Warm)** | High priority rebuild lead. |
| **Case C: Cloudflare-Protected Enterprise** (WAF blocks crawler, full contacts, live site) | 0 (blocked) + 30 (priority) = **30** | 30 (phone) + 30 (WA) + 25 (email) + 15 (IG) = **100** | 0.85 (probe blocked) | **85 (False Hot)** | **49 (P3 Quarantine)** | **Inversion fixed: does not outrank target SMBs.** |
| **Case D: Fully Optimized Clinic** (Live site, 4.9★, all contacts) | 0 (live site) + 0 (rating) + 30 (priority) = **30** | 100 (all contacts) = **100** | 1.00 | **85 (False Hot)** | **58 (P3 Retainer)** | Prevented from clogging new-build pipeline. |
| **Case E: Dead Web + Full Contacts + WhatsApp** (Confirmed dead site, mobile, WhatsApp, email) | 35 (dead) + 30 (priority) = **65** | 30 (phone) + 30 (WA) + 25 (email) = **85** | 1.00 | 70 | **73 (P1 Hot)** | Top of queue for urgent rebuild pitch. |

---

## Calibrated Scoring Bands & SLA Routing

Instead of dumping arbitrary integer scores into a CSV, scores map into actionable **B2B Sales Bands** with strict operational SLAs:

```
[ 0 ──────────────────────── 40 ──────────────────────── 65 ──────────────────────── 80 ─────────────────────── 100 ]
  P4: DISQUALIFIED / JUNK         P3: NURTURE / COLD             P2: WARM TARGET               P1: HOT / SALES-READY
  • No reachable contact         • Fully built digital pres.    • High need + 1 channel       • High need + multi-channel
  • Commodity retail             • Blocked crawler probe        • Greenfield with mobile      • Confirmed dead site + WA
  • Action: Archive / Drop       • Action: Retainer / Marketing • Action: Standard Outbound   • Action: SDR Call within 4h
```

### Band Specifications

| Band | Score Range | Criteria Summary | Target Services | Sales SLA / Routing |
|---|---|---|---|---|
| **P1 — HOT** | **80 – 100** | Verified Opportunity ($S_{\text{opp}} \ge 60$) **AND** Multi-Channel Reachability ($S_{\text{reach}} \ge 60$) with high confidence ($S_{\text{conf}} \ge 0.95$). | Website Rebuild, Greenfield Website Launch, E-Commerce Launch. | **Immediate Outbound (< 4 hours).** Auto-routed to senior SDR. WhatsApp audio message + direct phone outreach. |
| **P2 — WARM** | **65 – 79** | High Opportunity with Single Contact Channel (e.g. mobile only), OR Moderate Opportunity with Complete Contacts. | Greenfield Launch, Local SEO Turnaround, Lead-Gen Overhaul. | **Next-Day Outbound (< 24 hours).** Placed in SDR daily power-dialer queue. |
| **P3 — NURTURE** | **40 – 64** | Low Opportunity (fully digitized businesses) OR Unverified Opportunity (crawl blocked, indeterminate probe). | Digital Marketing Retainer, Reputation Management, or Re-enrichment. | **Automated Cadence / Cold Email.** Assigned to automated email drip; quarantined crawl-blocked sites scheduled for browser-based re-check. |
| **P4 — JUNK** | **0 – 39** | No viable contact channel ($S_{\text{reach}} < 20$) OR zero opportunity (commodity kiosks, non-commercial entities). | None. | **Dropped from Sales Exports.** Written to master archive `all_businesses.csv`, excluded from `sales_ready.csv`. |

---

## Config-Driven Weights Engine

All scoring logic, feature thresholds, and band limits are decoupled from code and defined in a structured YAML configuration file (`config/scoring_rules.v2.yaml`).

### The Configuration Specification (`config/scoring_rules.v2.yaml`)

```yaml
version: "2.1.0"
description: "Production scoring engine with multi-state HTTP probe and gated reachability"

weights:
  opportunity_weight: 0.60
  reachability_weight: 0.40

bands:
  p1_hot:
    min_score: 80
    label: "P1_HOT"
    sla_hours: 4
  p2_warm:
    min_score: 65
    label: "P2_WARM"
    sla_hours: 24
  p3_nurture:
    min_score: 40
    label: "P3_NURTURE"
    sla_hours: 72
  p4_disqualified:
    min_score: 0
    label: "P4_DISQUALIFIED"
    sla_hours: null

features:
  opportunity:
    website_status:
      ABSENT: 35
      CONFIRMED_DEAD: 35
      PARKED_DOMAIN: 30
      REDIRECT_SOCIAL: 25
      CRAWL_BLOCKED: 0
      INDETERMINATE: 0
      LIVE_VERIFIED: 0
    rating_pain:
      critical_unhappy: # < 3.8 stars with at least 5 reviews
        max_rating: 3.8
        min_reviews: 5
        points: 20
      invisible_unreviewed: # 0 reviews or unlisted
        max_reviews: 0
        points: 15
      mediocre: # 3.8 <= rating < 4.3
        max_rating: 4.3
        points: 8
      optimal: # >= 4.3
        points: 0
    social_deficit:
      zero_social: 15
      single_social: 5
      mature_social: 0
    vertical_priority:
      high: 30
      medium: 18
      low: 5

  reachability:
    phone:
      verified_mobile: 30
      landline_or_generic: 20
      missing: 0
    whatsapp:
      active: 30
      missing: 0
    email:
      domain_corporate: 25
      generic_free: 15
      missing: 0
    social_inbox:
      available: 15
      missing: 0

  confidence:
    sources:
      multi_source: 1.00
      single_google_places: 0.90
      single_scraped: 0.80
    geographic:
      verified_coords: 1.00
      inferred_region: 0.95
      unknown: 0.85
    web_probe:
      verified_or_absent: 1.00
      parked_or_redirect: 0.95
      crawl_blocked: 0.85
      indeterminate: 0.75
```

---

## Schema Changes & Data Contracts

The existing 23-column `BusinessRecord` (`scrapers/base.py:5-29`) must be expanded to preserve diagnostic context, scoring lineage, and override state:

### Column Additions to `BusinessRecord`

```python
class BusinessRecordV2(TypedDict):
    # --- Existing 23 Core Columns ---
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
    website_live: bool | None       # Deprecated: kept for backward compatibility (maps LIVE_VERIFIED)
    facebook: str | None
    instagram: str | None
    whatsapp: str | None
    linkedin: str | None
    rating: float | None
    review_count: int | None
    industry_priority: str | None
    recommended_service: str | None
    lead_score: int                 # Computed or overridden score (0-100)
    source: str
    scraped_at: str
    completeness_score: int

    # --- New Diagnostic & Scoring Columns ---
    website_status: str             # ABSENT | LIVE_VERIFIED | PARKED_DOMAIN | REDIRECT_SOCIAL | CONFIRMED_DEAD | CRAWL_BLOCKED | INDETERMINATE
    probe_http_status: int | None   # Raw HTTP code (200, 404, 403, 500, etc.)
    score_version: str              # Semver of scoring engine, e.g. "2.1.0"
    score_band: str                 # P1_HOT | P2_WARM | P3_NURTURE | P4_DISQUALIFIED
    score_breakdown: str            # Serialized metrics: "opp:65;reach:85;conf:0.95;raw:71"

    # --- New Sales Override Columns ---
    is_overridden: bool             # True if human SDR adjusted score
    original_score: int | None      # Pre-override calculated score
    override_reason: str | None     # Controlled reason code
    overridden_by: str | None       # Email or ID of sales rep
    overridden_at: str | None       # ISO-8601 timestamp
```

---

## Sales Team Override & Calibration Feedback Loop

A major operational problem in `leadminer` is that sales reps in Beirut or Riyadh have ground truth that web scrapers cannot deduce (e.g., they call a number and find it disconnected, or they open a URL on a local mobile carrier and find the business has rebranded).

```
  ┌────────────────────────────────────────────────────────────────────────┐
  │                           Data Lake / Master                           │
  │                     `data/master_businesses.parquet`                   │
  └───────────────────────────────────┬────────────────────────────────────┘
                                      │
                                      ▼
                      ┌───────────────────────────────┐
                      │    Pipeline Run (main.py)     │
                      │  Scrape -> Deduplicate ->     │
                      │  Enrich -> Score (v2.1.0)     │
                      └───────────────┬───────────────┘
                                      │
                                      ▼
                      ┌───────────────────────────────┐
                      │    Reconciliation Engine      │◄────────────────┐
                      │ Merges automated records with │                 │
                      │     active sales overrides    │                 │
                      └───────────────┬───────────────┘                 │
                                      │                                 │
                 ┌────────────────────┴────────────────────┐            │
                 ▼                                         ▼            │
     ┌───────────────────────┐                 ┌──────────────────────┐ │
     │  `sales_ready.csv`    │                 │ `data/overrides.csv` │─┘
     │  (P1 & P2 Leads with  │                 │  (Sales Rep Ground   │
     │   Override Badges)    │                 │   Truth Ledger)      │
     └───────────┬───────────┘                 └──────────▲───────────┘
                 │                                        │
                 ▼                                        │
     ┌───────────────────────┐                            │
     │ Outbound Sales Action │                            │
     │ (WhatsApp / Calling)  │                            │
     └───────────┬───────────┘                            │
                 │                                        │
                 └────── Rep Discovers Ground Truth ──────┘
                         • Phone disconnected
                         • Site is actually alive
                         • Rebranded / Closed
```

### 1. Overrides Ledger (`data/overrides.csv`)
Overrides are kept in a separate, version-controlled ledger so pipeline re-runs **never** erase sales rep knowledge.

Schema:
```csv
record_id,business_name,phone,override_score,override_band,override_reason,notes,overridden_by,overridden_at
lb_9611234567,Cedar Dental Clinic,+9611234567,10,P4_DISQUALIFIED,DISCONNECTED_PHONE,"Number out of service",karim@voxire.com,2026-10-02T10:15:00Z
ksa_966512345,Riyadh Roasters,+966512345678,95,P1_HOT,WEBSITE_CONFIRMED_DEAD,"Site domain expired yesterday, owner wants rebuild",sara@voxire.com,2026-10-02T11:42:00Z
lb_9617011223,Beirut Fashion Hub,+96170112233,45,P3_NURTURE,FALSE_ALARM_CRAWL_BLOCKED,"Site is fully live on Shopify; Cloudflare blocked us",karim@voxire.com,2026-10-02T14:20:00Z
```

### 2. Standardized Reason Codes (Controlled Vocabulary)
To prevent unparseable freeform text from corrupting analytics, reps select from strict categories:
1. `FALSE_ALARM_CRAWL_BLOCKED`: The scraper marked the website dead/blocked, but it is fully active.
2. `WEBSITE_CONFIRMED_DEAD`: Manual check confirmed the website domain is lapsed, broken, or hijacked.
3. `DISCONNECTED_PHONE`: Phone number is invalid, wrong number, or out of service.
4. `UNRESPONSIVE_CHANNELS`: Reached out 3+ times across WA and Phone with no pickup.
5. `ALREADY_SERVICED`: Business already works with an active agency retainer.
6. `OUT_OF_BUSINESS`: Entity is permanently closed or dissolved.
7. `HIGH_PRIORITY_OPPORTUNITY`: Verbal interest confirmed; prospect requested proposal.

### 3. Model Calibration & Weight Tuning via Brier Score
Overrides are not just operational overrides; they are the gold-standard training data for scoring calibration.
Every week, a tuning script computes the model's calibration error:

1. **False Positive Rate of Dead Sites:**
   $$\text{FPR}_{\text{dead}} = \frac{\sum \text{Overrides with } \texttt{FALSE\_ALARM\_CRAWL\_BLOCKED}}{\text{Total Probed Sites flagged Dead}}$$
   If $\text{FPR}_{\text{dead}} > 5\%$, crawler user-agents, timeout thresholds, and WAF evasion strategies are recalibrated.
2. **Precision at $K$ ($P@K$) in P1_HOT:**
   Calculates the percentage of leads in P1_HOT that convert to a qualified sales discovery call.
3. **Automated Regression Testing:**
   Before releasing `scoring_rules.v2.2.yaml`, the new rules run against the historical overrides database. Any configuration change that would promote a previously disqualified lead into P1_HOT fails CI.

---

## Drop-in Implementation Blueprint

Here is the production implementation of `ScoringEngineV2`, fully typed and self-contained, designed to replace `lead_score()` in `enricher.py`:

```python
"""
leadminer/scoring_engine.py
Production lead scoring engine (Version 2.1.0).
Provides transparent, multi-state, config-driven lead evaluation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class WebsiteStatus(StrEnum):
    ABSENT = "ABSENT"
    LIVE_VERIFIED = "LIVE_VERIFIED"
    PARKED_DOMAIN = "PARKED_DOMAIN"
    REDIRECT_SOCIAL = "REDIRECT_SOCIAL"
    CONFIRMED_DEAD = "CONFIRMED_DEAD"
    CRAWL_BLOCKED = "CRAWL_BLOCKED"
    INDETERMINATE = "INDETERMINATE"


class ScoreBand(StrEnum):
    P1_HOT = "P1_HOT"
    P2_WARM = "P2_WARM"
    P3_NURTURE = "P3_NURTURE"
    P4_DISQUALIFIED = "P4_DISQUALIFIED"


@dataclass(frozen=True)
class ScoringResult:
    score: int
    band: ScoreBand
    version: str
    opportunity_score: int
    reachability_score: int
    confidence_factor: float
    breakdown: str


class LeadScorerV2:
    VERSION = "2.1.0"

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = config or self._default_config()

    @staticmethod
    def _default_config() -> dict[str, Any]:
        return {
            "weights": {"opportunity": 0.60, "reachability": 0.40},
            "opportunity": {
                "website": {
                    WebsiteStatus.ABSENT: 35,
                    WebsiteStatus.CONFIRMED_DEAD: 35,
                    WebsiteStatus.PARKED_DOMAIN: 30,
                    WebsiteStatus.REDIRECT_SOCIAL: 25,
                    WebsiteStatus.LIVE_VERIFIED: 0,
                    WebsiteStatus.CRAWL_BLOCKED: 0,
                    WebsiteStatus.INDETERMINATE: 0,
                },
                "rating": {"low_pain": 20, "unreviewed": 15, "average": 8},
                "social": {"no_social": 15, "single_channel": 5},
                "vertical": {"high": 30, "medium": 18, "low": 5},
            },
            "reachability": {
                "phone_mobile": 30,
                "phone_landline": 20,
                "whatsapp": 30,
                "email_corporate": 25,
                "email_generic": 15,
                "social_inbox": 15,
            },
            "confidence": {
                "multi_source": 1.00,
                "single_source": 0.90,
                "inferred_geo": 0.95,
                "blocked_probe": 0.85,
            },
        }

    def evaluate(
        self,
        record: Mapping[str, Any],
        website_status: WebsiteStatus | str,
    ) -> ScoringResult:
        status = WebsiteStatus(website_status)

        # 1. Calculate Opportunity Score (0 - 100)
        opp_pts = 0
        opp_cfg = self.config["opportunity"]

        # Website deficit
        opp_pts += opp_cfg["website"].get(status, 0)

        # Rating deficit
        rating = record.get("rating")
        reviews = record.get("review_count") or 0
        try:
            rating_f = float(rating) if rating is not None else None
            reviews_i = int(reviews)
        except (ValueError, TypeError):
            rating_f, reviews_i = None, 0

        if rating_f is not None and rating_f < 3.8 and reviews_i >= 5:
            opp_pts += opp_cfg["rating"]["low_pain"]
        elif reviews_i == 0 and not record.get("website"):
            opp_pts += opp_cfg["rating"]["unreviewed"]
        elif rating_f is not None and rating_f < 4.3:
            opp_pts += opp_cfg["rating"]["average"]

        # Social deficit
        has_fb = bool(record.get("facebook"))
        has_ig = bool(record.get("instagram"))
        if not has_fb and not has_ig:
            opp_pts += opp_cfg["social"]["no_social"]
        elif not (has_fb and has_ig):
            opp_pts += opp_cfg["social"]["single_channel"]

        # Vertical priority
        priority = str(record.get("industry_priority") or "").lower()
        if priority == "high":
            opp_pts += opp_cfg["vertical"]["high"]
        elif priority == "medium":
            opp_pts += opp_cfg["vertical"]["medium"]
        else:
            opp_pts += opp_cfg["vertical"]["low"]

        opp_score = min(100, opp_pts)

        # 2. Calculate Reachability Score (0 - 100)
        reach_pts = 0
        reach_cfg = self.config["reachability"]

        # Phone
        raw_phone = re.sub(r"\D", "", str(record.get("phone") or ""))
        if len(raw_phone) >= 8:
            # Check for Lebanon or KSA mobile prefixes
            if (
                raw_phone.startswith(("96170", "96171", "96176", "96181", "70", "71", "76", "81"))
                or raw_phone.startswith(("9665", "05"))
            ):
                reach_pts += reach_cfg["phone_mobile"]
            else:
                reach_pts += reach_cfg["phone_landline"]

        # WhatsApp
        if record.get("whatsapp"):
            reach_pts += reach_cfg["whatsapp"]

        # Email
        email = str(record.get("email") or "").lower()
        if email:
            if any(email.endswith(f"@{p}") for p in ["gmail.com", "yahoo.com", "hotmail.com"]):
                reach_pts += reach_cfg["email_generic"]
            else:
                reach_pts += reach_cfg["email_corporate"]

        # Social Inbox
        if has_ig or record.get("linkedin"):
            reach_pts += reach_cfg["social_inbox"]

        reach_score = min(100, reach_pts)

        # 3. Calculate Confidence Multiplier (0.70 - 1.00)
        conf_cfg = self.config["confidence"]
        conf = 1.0

        if "|" not in str(record.get("source") or ""):
            conf *= conf_cfg["single_source"]

        if not record.get("lat") or not record.get("lon"):
            conf *= conf_cfg["inferred_geo"]

        if status in (WebsiteStatus.CRAWL_BLOCKED, WebsiteStatus.INDETERMINATE):
            conf *= conf_cfg["blocked_probe"]

        conf_factor = max(0.70, round(conf, 2))

        # 4. Composite Gated Score
        w_opp = self.config["weights"]["opportunity"]
        w_reach = self.config["weights"]["reachability"]
        raw_composite = (w_opp * opp_score + w_reach * reach_score) * conf_factor
        final_score = int(round(raw_composite))
        final_score = max(0, min(100, final_score))

        # 5. Band Assignment
        if final_score >= 80:
            band = ScoreBand.P1_HOT
        elif final_score >= 65:
            band = ScoreBand.P2_WARM
        elif final_score >= 40:
            band = ScoreBand.P3_NURTURE
        else:
            band = ScoreBand.P4_DISQUALIFIED

        breakdown = (
            f"opp:{opp_score}|reach:{reach_score}|conf:{conf_factor:.2f}|"
            f"web_status:{status.value}"
        )

        return ScoringResult(
            score=final_score,
            band=band,
            version=self.VERSION,
            opportunity_score=opp_score,
            reachability_score=reach_score,
            confidence_factor=conf_factor,
            breakdown=breakdown,
        )
```

---

## Recommended Order of Work

### Phase 1: Fix Network Inversion & Multi-State HTTP Probe (Days 1–2)
1. **Refactor `_fetch_website()` in `enricher.py:142-177`:**
   - Eliminate blanket `status_code < 500` liveness condition.
   - Detect Cloudflare challenges, 403s, and 429 rate limits explicitly; tag them as `website_status = CRAWL_BLOCKED`.
   - Distinguish DNS NXDOMAIN and 404/410 errors as `website_status = CONFIRMED_DEAD`.
   - Identify domain parking signatures and 301/302 redirects to social profiles.
2. **Update Type Contracts in `scrapers/base.py`:**
   - Add `website_status`, `score_version`, `score_band`, `score_breakdown`, `is_overridden`, and `probe_http_status` to `BusinessRecord`.

### Phase 2: Deploy Config-Driven Scoring Engine (Days 3–4)
3. **Create `config/scoring_rules.v2.yaml`:**
   - Commit versioned weights, feature thresholds, and band definitions.
4. **Implement `LeadScorerV2` in `scoring_engine.py`:**
   - Implement the decomposed $S_{\text{opp}}$, $S_{\text{reach}}$, and $S_{\text{conf}}$ formulation.
   - Remove redundant `lead_score()` call from `enricher.py:290`.
   - Update `main.py:137` to score records only after all enrichment, categorization, and phone normalization stages have executed.

### Phase 3: Sales Override & Calibration Ledger (Days 5–6)
5. **Implement Overrides Ingestion in `dedup.py` / `main.py`:**
   - Add `load_overrides(path: Path) -> dict[str, dict]` to load `data/overrides.csv`.
   - Merge overrides after scoring: if a record matches an override entry by phone or normalized name/city, apply `override_score`, set `is_overridden = True`, and preserve the rep's note.
6. **Filter CSV Exports by Band:**
   - Update `sales_ready.csv` (`main.py:142-145`) to filter on `score_band in ("P1_HOT", "P2_WARM")` instead of the weak legacy predicate.

### Phase 4: Calibration & Verification (Day 7)
7. **Run Offline Backtest Against Master Data:**
   - Compare `lead_score` (legacy) vs `lead_score_v2` on the existing business catalog.
   - Verify that 0% of `CRAWL_BLOCKED` enterprise domains outrank verified greenfield or dead-site leads.
   - Verify that `without_websites.csv` prospects now populate the upper quartiles of `sales_ready.csv`.
