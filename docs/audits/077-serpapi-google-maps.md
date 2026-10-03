# 077 — SERP-Based Google Maps Providers Audit

## Verdict

Replacing Google Places API (New) with a dedicated SERP provider eliminates Google's punitive Enterprise SKU billing ($32.00/1k calls for website, phone, and rating fields) and avoids Google Maps Platform's strict contractual prohibitions against permanent storage and caching (Section 3.2.3). However, **a SERP provider does not materially increase reach if queried with the exact same 68 broad text queries** (e.g. `"restaurants in Lebanon"`): Google Maps web UI imposes its own hard display ceiling of ~100–120 results per viewport/query (a modest 1.7× to 2.0× increase over Google Places API's 60-result limit). 

To achieve a true order-of-magnitude coverage expansion (10×–50×), `leadminer` must shift to **geographic coordinate grid tiling** (`lat`, `lon`, `zoom`). Among the evaluated providers, **DataForSEO** is the overwhelming recommendation for `leadminer`: at **$0.0006 per 100-result SERP page** ($0.60/1,000 requests) via its Standard Queue with pay-as-you-go billing ($0 monthly commitment), a 500-tile dense grid across Lebanon and Saudi metro areas costs **$0.30 total**, yielding up to 50,000 raw listings with complete contact, category, and coordinate metadata. **Bright Data** is the premier enterprise alternative ($1.50/1k requests, backed by federal case law in *Meta v. Bright Data*), while **SerpAPI** is reliable but over-priced ($25–$150/mo, $10–$25/1k calls), **ScaleSERP** offers a budget clone at $66–$82/mo, and **ZenRows** is poorly suited because it is a generic headless unblocker requiring brittle CSS selectors and a 25× JavaScript/proxy credit penalty ($7.00/1k requests).

---

## Provider Comparison Matrix

| Provider | Entry Monthly Price | Per-Request Cost (Google Maps) | Cost for 68 Queries (3 pages / 204 calls) | Cost for 500 Geo-Grid Tiles | Max Results Per Single Query | Rate Limits / Concurrency | Pre-Parsed Schema vs CSS Extractor | Legal Shield / Court Precedent | Primary URL |
|---|---|---|---|---|---|---|---|---|---|
| **Google Places (Current)** | $0 min ($200 credit) | $0.0320 (Enterprise SKU) | $6.53 | $16.00 | 60 (hard cap) | 6,000 QPM default | Native Google API | Breaches ToS Sec 3.2.3 (caching/storage) | [Google Maps Pricing](https://developers.google.com/maps/billing-and-pricing/pricing) |
| **DataForSEO** | **$0** ($50 top-up) | **$0.0006** (Std Queue) / $0.002 (Live) | **$0.12** (Std) / $0.41 (Live) | **$0.30** (Std) / $1.00 (Live) | **100–700** (`depth` param) | 2,000 tasks/POST; high queue concurrency | Pre-parsed JSON (Maps SERP + Business Data) | Public data extraction; B2B standard | [DataForSEO Maps API](https://dataforseo.com/apis/serp-api/google-maps-api) |
| **Bright Data** | $0 (PAYG) or $499/mo | $0.0015 (PAYG) / $0.0013 (Sub) | $0.31 | $0.75 | 100 (SERP) / Full profile | Unlimited concurrency | Pre-parsed JSON (Fast SERP + Scraper API) | Federal summary judgment (*Meta v. Bright Data*) | [Bright Data Maps](https://brightdata.com/products/serp-api/google-search/maps) |
| **ScaleSERP** | $49/mo (5k) or $66/mo (10k/yr) | $0.0066 – $0.0098 | $1.35 – $2.00 | $3.30 – $4.90 | ~100–120 (web UI cap) | Up to 15,000 parallel searches | Pre-parsed JSON (`places_results`) | Standard commercial proxy terms | [ScaleSERP Places](https://docs.trajectdata.com/scaleserp/search-api/searches/google/places) |
| **SerpAPI** | $25/mo (1k) or $75/mo (5k) | $0.0150 – $0.0250 | $3.06 – $5.10 | $7.50 – $12.50 | ~100–120 (offset 100) | 200–1,000 searches/hr throughput cap | Pre-parsed JSON (`local_results`) | $2M US Legal Shield (Production tier+) | [SerpAPI Google Maps](https://serpapi.com/google-maps-api) |
| **ZenRows** | $19/mo (Build) or $69/mo (Launch) | ~$0.0069 – $0.0105 (25× multiplier) | $1.41 – $2.14 | $3.45 – $5.25 | ~20/page (manual scroll) | 20–50 concurrent requests | Unparsed HTML / Brittle CSS Selectors | Standard EU proxy terms | [ZenRows Maps Scraper](https://www.zenrows.com/scrape/google-maps) |

---

## Detailed Provider Analysis

### 1. DataForSEO

- **Target Endpoints:** 
  - `v3/serp/google/maps/task_post` + `task_get/advanced` (Standard Queue)
  - `v3/serp/google/maps/live/advanced` (Live Mode)
  - `v3/business_data/google/business_listings/search` (Pre-indexed Point-of-Interest database)
- **Field Coverage for `leadminer`:**
  - `name`: Supported (`title`)
  - `phone`: Supported (`phone`)
  - `website`: Supported (`url` / `domain`)
  - `rating`: Supported (`rating.value`)
  - `review_count`: Supported (`rating.votes_count`)
  - `address`: Supported (`address` string and structured `address_info` containing `borough`, `city`, `zip`, `region`, `country_code`)
  - `place_id`: Supported (`place_id`, plus `cid` and `feature_id`)
  - `categories`: Supported (`category`, `additional_categories`, and `category_ids`)
  - `coordinates`: Supported (`latitude`, `longitude`)
  - `opening_hours`: Supported (`work_hours.timetable` per weekday with `open`/`close` hours, and `work_hours.current_status`)
- **Pricing Model:**
  - **Monthly Base:** $0 monthly recurring commitment. Pure Pay-As-You-Go model with a $50 minimum wallet top-up. Credits do not expire.
  - **Per-Request Cost:**
    - Standard Queue (Normal priority, ~5 min turnaround): **$0.0006 per 1 SERP** (contains up to 100 results).
    - Standard Queue (High priority, ~1 min turnaround): **$0.0012 per 1 SERP**.
    - Live Mode (synchronous, ~6 sec turnaround): **$0.0020 per 1 SERP**.
    - *Depth Multiplier:* Default depth is 100 results ($0.0006). If depth is set to 200, billing is 2 × base ($0.0012). Max depth is 700.
    - *Effective cost per lead:* At $0.0006 for 100 listings, the cost is **$0.000006 per business listing**.
- **Rate Limits & Concurrency:**
  - Standard Queue allows batching up to **2,000 tasks per single POST request**.
  - No practical concurrency ceiling for async queue; tasks execute in worker pools and are polled via `task_get` or received via `postback_url`.
  - Live mode supports up to 20–100 concurrent requests depending on balance tier.
- **Legal Posture:**
  - Operates as an automated B2B SERP provider collecting public search engine listings. Does not sign Google Maps API terms; customers do not provide Google credentials.
- **Documentation & Sources:**
  - [DataForSEO Google Maps SERP API Overview](https://docs.dataforseo.com/v3/serp/google/maps/overview/)
  - [DataForSEO Google Maps SERP Pricing](https://dataforseo.com/pricing/google-serp/google-maps-serp-api)
  - [DataForSEO Maps Task Post Docs](https://docs.dataforseo.com/v3/serp-google-maps-task_post/)
  - [DataForSEO Business Listings API](https://dataforseo.com/pricing/business-data/business-listings-api)

---

### 2. Bright Data

- **Target Endpoints:**
  - `SERP API (Google Maps Fast SERP)`: Target `https://www.google.com/maps/search/...` via Bright Data Super Proxy with header `x-unblock-data-format: parsed_light` and parameter `brd_json=1`.
  - `Google Scraper API (Datasets / Web Scraper)`: Dataset `gd_m8ebnr0q2qlklc02fz` (Google Maps full information).
- **Field Coverage for `leadminer`:**
  - `name`: Supported (`title` in Fast SERP, `name` in Scraper API)
  - `phone`: Supported (`phone`)
  - `website`: Supported (`link` in Fast SERP, `website` in Scraper API)
  - `rating`: Supported (`rating`)
  - `review_count`: Supported (`reviews_cnt` / `reviews_count`)
  - `address`: Supported (`address` full string)
  - `place_id`: Supported (`map_id_encoded` is the standard Place ID; `map_id` is the hexadecimal feature ID)
  - `categories`: Supported (`category` array of objects with `id`, `title`, and `title_short`)
  - `coordinates`: Supported (`latitude`, `longitude`)
  - `opening_hours`: Supported (`open_hours` dictionary keyed by day, e.g., `"Monday": "9am–5pm"`)
- **Pricing Model:**
  - **Monthly Base:** $0 commitment on Pay-As-You-Go plan; subscription tiers start at $499/month (includes 380,000 requests, then $1.30/1k).
  - **Per-Request Cost:** 
    - Pay-As-You-Go: **$1.50 per 1,000 requests** ($0.0015 per request).
    - Failed requests cost $0 (pay only for successful responses `200 OK`).
- **Rate Limits & Concurrency:**
  - **Unlimited concurrency** on all plans. Built on Bright Data's global proxy network (72M+ residential IPs).
- **Legal Posture:**
  - **Industry-leading legal precedent:** Bright Data prevailed on summary judgment in *Meta Platforms, Inc. v. Bright Data Ltd.* (Case No. 3:23-cv-00077-EMC, N.D. Cal. Jan. 23, 2024, Judge Edward Chen), establishing that logged-off scraping of publicly available web data does not breach platform Terms of Service. In May 2024, Bright Data also secured dismissal of breach of contract claims in *X Corp. v. Bright Data Ltd.*
  - Operates strict KYC, IP hygiene, and ethical peer-routing verification.
- **Documentation & Sources:**
  - [Bright Data Google Maps SERP API](https://brightdata.com/products/serp-api/google-search/maps)
  - [Bright Data Fast SERP Maps Documentation](https://docs.brightdata.com/scraping-automation/serp-api/fast-serp/maps-search)
  - [Bright Data SERP API Pricing](https://brightdata.com/pricing/serp)
  - [Judge Chen Summary Judgment Ruling: Meta v. Bright Data](https://digitalcommons.law.scu.edu/cgi/viewcontent.cgi?article=3834&context=historical)

---

### 3. SerpAPI

- **Target Endpoints:**
  - `GET https://serpapi.com/search?engine=google_maps&q=...`
  - Geographic search: parameter `ll=@latitude,longitude,zoom` or `location=...`
- **Field Coverage for `leadminer`:**
  - `name`: Supported (`title`)
  - `phone`: Supported (`phone`)
  - `website`: Supported (`website` in `local_results`)
  - `rating`: Supported (`rating`)
  - `review_count`: Supported (`reviews`)
  - `address`: Supported (`address`)
  - `place_id`: Supported (`place_id`, `data_id`, `data_cid`)
  - `categories`: Supported (`type`, `types`, `type_id`, `type_ids`)
  - `coordinates`: Supported (`gps_coordinates.latitude`, `gps_coordinates.longitude`)
  - `opening_hours`: Supported (`operating_hours` dictionary keyed by weekday, plus `open_state` and `hours`)
- **Pricing Model:**
  - **Monthly Base:** $25/mo (Starter, 1,000 searches), $75/mo (Developer, 5,000 searches), $150/mo (Production, 15,000 searches), $275/mo (Big Data, 30,000 searches).
  - **Per-Request Cost:** 
    - Starter: $0.0250/search ($25/1k).
    - Developer: $0.0150/search ($15/1k).
    - Production: $0.0100/search ($10/1k).
    - Big Data: $0.0092/search ($9.17/1k).
    - Extra credits: Not sold per call; running out triggers early plan renewal.
- **Rate Limits & Concurrency:**
  - Rate limits are throttled by **Throughput per hour** quotas: Starter = 200 searches/hr; Developer = 1,000 searches/hr; Production = 3,000 searches/hr.
  - A multi-threaded scraper running 40 workers can easily exceed the 200/hr limit on Starter in less than 5 minutes.
- **Legal Posture:**
  - **U.S. Legal Shield:** Provides commercial indemnification up to **$2,000,000** for legal claims arising from scraping and parsing public search engine data (available on Production tier $150/mo and higher).
  - Offers "ZeroTrace Mode" (Enterprise only) to prevent query logging.
- **Documentation & Sources:**
  - [SerpAPI Google Maps API Documentation](https://serpapi.com/google-maps-api)
  - [SerpAPI Pricing & Legal Shield Details](https://serpapi.com/pricing)
  - [SerpAPI Google Maps Local Results](https://serpapi.com/maps-local-results)

---

### 4. ScaleSERP (Traject Data)

- **Target Endpoints:**
  - `GET https://api.scaleserp.com/search?search_type=places&q=...`
  - Maps mode triggered by `location=location=lat:x,lon:y,zoom:z`
- **Field Coverage for `leadminer`:**
  - `name`: Supported (`title`)
  - `phone`: Supported (`phone`)
  - `website`: Supported (`link` in `places_results`; full `website` in `place_details`)
  - `rating`: Supported (`rating`)
  - `review_count`: Supported (`reviews`)
  - `address`: Supported (`address`)
  - `place_id`: Supported (`place_id`, `data_id`, `data_cid`)
  - `categories`: Supported (`category`)
  - `coordinates`: Supported (`gps_coordinates.latitude`, `gps_coordinates.longitude`)
  - `opening_hours`: Supported (`opening_hours.current` and `opening_hours.per_day` list)
- **Pricing Model:**
  - **Monthly Base:** $49/mo (Starter, 5,000 searches) or Annual billing at $66/mo (10,000 credits/mo), $199/mo (50,000 credits/mo).
  - **Per-Request Cost:** 
    - Starter: $0.0098 per request.
    - Annual 10k: $0.0066 per request ($6.60/1k).
    - Overage: $0.0118 per additional credit.
- **Rate Limits & Concurrency:**
  - ScaleSERP supports up to **15,000 parallel searches** across its distributed proxy network. Batch API supports up to 10,000 batches.
- **Legal Posture:**
  - Standard commercial proxy infrastructure terms. No formal legal shield or indemnification.
- **Documentation & Sources:**
  - [ScaleSERP Homepage & Pricing](https://www.scaleserp.com/)
  - [ScaleSERP Google Places & Maps API Docs](https://docs.trajectdata.com/scaleserp/search-api/searches/google/places)
  - [ScaleSERP Places Results Schema](https://docs.trajectdata.com/scaleserp/search-api/results/google/places)

---

### 5. ZenRows

- **Target Endpoints:**
  - `GET https://api.zenrows.com/v1/?url=https://www.google.com/maps/search/...`
  - Requires parameters: `js_render=true`, `premium_proxy=true`, and `css_extractor={...}` or DOM scraping.
- **Field Coverage for `leadminer`:**
  - `name`: Brittle CSS selector (`h1.DUwDvf.lfPIob`)
  - `phone`: Brittle CSS selector (`.RcCsl:nth-child(5) .Io6YTe`)
  - `website`: Brittle CSS selector (`.RcCsl:nth-child(4) a.CsEnBe @href`)
  - `rating`: Brittle CSS selector (`.ceNzKf @aria-label`)
  - `review_count`: Brittle CSS selector (`.F7nice > span:nth-of-type(2)`)
  - `address`: Brittle CSS selector (`.RcCsl:nth-child(3) .Io6YTe`)
  - `place_id`: Not directly returned; requires regex extraction from URL or DOM attributes.
  - `categories`: Brittle CSS selector (`button.DkEaL`)
  - `coordinates`: Requires parsing `@lat,lon,zoom` from the canonical map URL.
  - `opening_hours`: Brittle CSS selector (`.t392fc table tr`)
- **Pricing Model:**
  - **Monthly Base:** Build plan from $19/mo (45,000 credits); Launch plan from $69/mo (250,000 credits); Growth plan from $199/mo (1.2M credits).
  - **Per-Request Multiplier Penalty:**
    - Standard HTML request = 1 credit.
    - JavaScript rendering (`js_render=true`) = 5 credits.
    - Premium residential proxy (`premium_proxy=true`) = 10 credits.
    - Protected target requiring **both JS rendering and premium proxies** (mandatory for Google Maps anti-bot) = **25 credits per request**.
    - On the $69/mo Launch plan (250k credits), 250,000 / 25 = **10,000 effective Google Maps requests**.
    - **Effective per-request cost: $0.0069 per page ($6.90 / 1,000 requests)**.
- **Rate Limits & Concurrency:**
  - Highly constrained concurrency on lower tiers: Free = 5; Build ($19) = 20; Launch ($69) = 50; Growth ($199) = 100 concurrent requests.
- **Legal Posture:**
  - Standard European proxy scraping terms. No indemnification or dedicated public SERP litigation history.
- **Why ZenRows is Unsuitable for this Use Case:**
  - ZenRows is a raw headless browser / anti-bot proxy provider, **not a SERP parser**.
  - Google frequently mutates internal Maps CSS classes (`.RcCsl`, `.Io6YTe`, `.DUwDvf`). Maintaining custom selectors in `leadminer` creates continuous engineering maintenance overhead.
  - Extracting 100 listings from Google Maps web requires automating scroll events inside ZenRows headless browser sessions, which burns browser session time (charged at 5 credits per minute + bandwidth).
- **Documentation & Sources:**
  - [ZenRows: How to Scrape Google Maps](https://www.zenrows.com/blog/google-maps-scraper)
  - [ZenRows Pricing & Request Multipliers](https://www.zenrows.com/blog/scraperapi-alternative-for-anti-bot-bypass)
  - [ZenRows Google Maps Overview](https://www.zenrows.com/scrape/google-maps)

---

## Coverage Analysis: Does a SERP Provider Materially Increase Reach?

### The 60-Result Places API Ceiling

In `scrapers/google_places.py:45-103`, `leadminer` executes 68 broad queries:
- 34 Lebanon queries: `"restaurants in Lebanon"`, `"cafes in Lebanon"`, `"hotels in Lebanon"`, etc.
- 34 KSA queries: `"fashion brands in Riyadh"`, `"real estate developers in Jeddah"`, etc.

Google Places API (New) `places:searchText` enforces an architectural ceiling:
1. Each response returns at most 20 results (`pageSize` default and max is 20).
2. The response supplies `nextPageToken` for page 2 and page 3.
3. **On page 3 (result 60), Google omits `nextPageToken`.** No further results can ever be retrieved for that query ([Google Places Text Search Documentation](https://developers.google.com/maps/documentation/places/web-service/text-search)).
4. Across 68 queries, `leadminer` has a theoretical upper bound of **4,080 raw records** before deduplication. In practice, because many queries overlap geographically and thematically, the yield is ~2,000–2,500 unique places.

### Does a SERP Provider Increase Reach for the Same 68 Text Queries?

**No, not materially.**
If `leadminer` merely swaps `requests.post(API_URL)` to call SerpAPI, ScaleSERP, or DataForSEO with the exact same text query `"restaurants in Lebanon"`:
1. **Google Maps Web Display Limit:** The Google Maps web desktop interface (`google.com/maps/search/...`) does not display an infinite list of businesses for nationwide or citywide broad text searches. It displays the top relevance/prominence results in the viewport, usually truncating after **100 to 120 results** (approx. 5 to 6 scroll increments or pages).
2. **SerpAPI's Official Guidance:** SerpAPI's documentation explicitly warns:
   > *"We recommend a maximum of 100 (page six) which is the same behavior as with the Google Maps web app. More than that, the result might be duplicated or irrelevant."* ([SerpAPI Google Maps API](https://serpapi.com/google-maps-api))
3. **Yield Comparison:**
   - Google Places API: 60 results / query × 68 queries = **4,080 raw places**.
   - SERP Provider (same queries): ~100–120 results / query × 68 queries = **6,800–8,160 raw places**.
   - This represents a **1.7× to 2.0× increase**. While not zero, doubling 4,000 records to 7,000 records does not solve the coverage problem for entire countries (Lebanon has over 30,000 registered commercial establishments; Riyadh alone has over 100,000 businesses).

### How a SERP Provider *Actually* Unlocks Material Coverage (10×–50×)

A SERP provider materially transforms coverage **only if paired with a spatial coordinate grid strategy (tiling)**:
1. **The Spatial Viewport Mechanism:** Google Maps web search ranks results based on viewport proximity (`ll=@latitude,longitude,zoom` or `location_coordinate="lat,lon,zoom"`).
2. **Dense Bounding Box Tiling:**
   - Instead of 1 query for `"restaurants in Lebanon"`, partition Greater Beirut into a grid of 20 coordinate centers at zoom `16z` (e.g., Hamra, Achrafieh, Mar Mikhael, Badaro, Verdun, Downtown).
   - Each tile returns 60–100 local establishments that would never rank in a nationwide query.
   - For Lebanon (Beirut, Mount Lebanon, Tripoli, Saida, Zahle) + KSA (Riyadh, Jeddah, Dammam), a grid of **500 coordinate points** across 10 major industry categories yields **50,000 to 100,000 unique business listings**.
3. **The Economic Feasibility of Geo-Tiling:**
   - **On Google Places API (New):** Requesting phone number, website, and rating classifies the call under the **Enterprise SKU** at **$32.00 per 1,000 requests** ([Google Maps Platform SKU Details](https://developers.google.com/maps/billing-and-pricing/sku-details)). Running 500 tiles × 3 pages = 1,500 requests costs **$48.00 per run** ($576/year on a monthly cadence).
   - **On DataForSEO (Standard Queue):** 500 tiles at $0.0006 per 100-result page costs **$0.30 per run** ($3.60/year).
   - **On Bright Data (Fast SERP):** 500 requests at $0.0015 costs **$0.75 per run** ($9.00/year).
   
**Conclusion:** A SERP provider materially increases coverage not through its pagination depth on text queries, but by **making dense geo-coordinate grid scraping economically viable** (reducing cost per tile from $0.096 to $0.0006, a 160× cost reduction).

---

## Legal & Compliance Posture

### 1. Google Maps Platform Terms of Service (The Current API Hazard)

Using the official Google Places API (`scrapers/google_places.py`) places `leadminer` under the **Google Maps Platform Terms of Service**:
- **Section 3.2.3(a) — No Caching:** Customers may not pre-fetch, cache, index, or store any Content outside the Service, except for temporary caching (up to 30 consecutive calendar days) solely to improve performance ([Google Maps Platform Terms of Service](https://cloud.google.com/maps-platform/terms)).
- **Section 3.2.3(b) — No Scraping / No Database Creation:** Customers may not export, extract, or scrape Content to create or augment an independent database or sales directory.
- **The Violation in `leadminer`:** `main.py:124` writes Google Places data to `all_businesses.csv` (a permanent master record) and uploads it to Google Drive. Storing place names, phone numbers, ratings, and addresses permanently in a lead-generation database directly breaches Google Maps Platform ToS Section 3.2.3. Continued use risks API key revocation and Google Cloud account termination.

### 2. Third-Party SERP Scrapers & Public Data Precedent

When using third-party SERP scrapers (DataForSEO, Bright Data, SerpAPI):
- `leadminer` enters into no contract with Google and supplies no Google API credentials. Requests are made to public web endpoints (`google.com/maps`) without authentication.
- **The CFAA & Public Data (*hiQ v. LinkedIn*):** The 9th U.S. Circuit Court of Appeals held in *hiQ Labs, Inc. v. LinkedIn Corp.* (31 F.4th 1180, 2022) that accessing publicly available data without logging into a password-protected account does not constitute "unauthorized access" under the Computer Fraud and Abuse Act (CFAA, 18 U.S.C. § 1030).
- **Terms of Service on Logged-Off Visitors (*Meta v. Bright Data*):** In *Meta Platforms, Inc. v. Bright Data Ltd.* (Case No. 3:23-cv-00077-EMC, N.D. Cal. Jan. 23, 2024), Judge Edward Chen granted summary judgment to Bright Data, ruling that a platform's Terms of Service do not bind automated scrapers collecting public data while logged out.
- **Copyright Preemption (*X Corp. v. Bright Data*):** In May 2024, the Northern District of California ruled that state-law breach of contract claims seeking to restrict copying of factual public content are preempted by the federal Copyright Act. Factual data points (business name, address, phone number, operating hours, coordinates) are uncopyrightable facts under *Feist Publications, Inc. v. Rural Telephone Service Co.* (499 U.S. 340, 1991).
- **Commercial Indemnification:** SerpAPI offers an explicit $2,000,000 legal shield on its Production tier ($150/mo), covering legal costs if sued by search engines.

### 3. Privacy Regulations (GDPR & Saudi PDPL)

- **B2B Contact Data:** Business phone numbers, public business addresses, and official business websites displayed publicly on Google Maps are generally classified as B2B commercial contact information rather than protected personal data.
- **Saudi Arabia Personal Data Protection Law (PDPL):** Enacted via Royal Decree No. M/19, PDPL regulates personal data belonging to individuals. Sole proprietorships where the business name is an individual's personal name, or personal mobile numbers used as business lines, require basic data protection hygiene (e.g., providing an opt-out mechanism upon outreach).

---

## Findings

### S1 — Google Places API Enterprise SKU causes massive cost inefficiency and ToS breach

- **Where:** `scrapers/google_places.py:31-42` and `main.py:124`.
- **Breaks:** Requesting `places.websiteUri`, `places.nationalPhoneNumber`, `places.rating`, and `places.userRatingCount` in `FIELD_MASK` elevates all 68 queries (up to 204 paginated calls) to Google's Enterprise Text Search SKU ($32.00/1k requests). Simultaneously, storing these records indefinitely in `all_businesses.csv` violates Google Maps Platform ToS Section 3.2.3 (30-day cache ceiling and prohibition against database compilation).
- **Trigger:** Any production execution of `main.py` with `GOOGLE_PLACES_API_KEY` set.
- **Fix:** Replace `scrapers/google_places.py` with a SERP client (DataForSEO or Bright Data). This shifts billing from $32.00/1k to $0.60–$1.50/1k (a 95%–98% cost reduction) and legally decouples `leadminer` from Google Maps Platform ToS.

### S2 — Text-only query strategy caps geographic reach regardless of provider

- **Where:** `scrapers/google_places.py:45-103`.
- **Breaks:** Broad queries like `"restaurants in Lebanon"` or `"fashion brands in Riyadh"` hit Google's ranking truncation. Places API caps at 60 results; Google Maps SERP caps at ~100–120. Over 90% of local establishments outside the city center are never retrieved.
- **Trigger:** Running any SERP provider with the existing 68 text queries expecting nationwide market coverage.
- **Fix:** Transition query architecture to a **grid-based spatial crawl** using lat/lon/zoom bounding boxes (`location_coordinate` in DataForSEO, `ll` in SerpAPI, or `location=lat:x,lon:y,zoom:z` in ScaleSERP).

### S3 — ZenRows is a false economy for structured Maps scraping

- **Where:** Third-party provider selection.
- **Breaks:** ZenRows applies a 25× credit multiplier for JS rendering + premium proxy on Google Maps, raising effective cost to $6.90/1k requests ($0.0069/call). It requires scraping raw HTML using brittle CSS selectors (`.DUwDvf`, `.Io6YTe`) that break when Google updates frontend obfuscation.
- **Trigger:** Attempting to implement ZenRows Fetch API as a drop-in replacement.
- **Fix:** Avoid ZenRows for Google Maps. Select a provider that returns pre-parsed JSON schemas (DataForSEO or Bright Data).

---

## Recommended Order of Work

1. **Adopt DataForSEO as Primary Google Maps Provider:**
   - Sign up for DataForSEO Pay-As-You-Go ($50 initial balance).
   - Implement an async client in `scrapers/dataforseo_maps.py` targeting `v3/serp/google/maps/task_post` and polling `task_get/advanced`.
   - Map response fields directly to `BusinessRecord`:
     - `name` $\leftarrow$ `item["title"]`
     - `category` $\leftarrow$ `item["category"]`
     - `address` $\leftarrow$ `item["address"]`
     - `lat` $\leftarrow$ `item["latitude"]`
     - `lon` $\leftarrow$ `item["longitude"]`
     - `phone` $\leftarrow$ `item["phone"]`
     - `website` $\leftarrow$ `item["url"]`
     - `rating` $\leftarrow$ `item["rating"]["value"]`
     - `review_count` $\leftarrow$ `item["rating"]["votes_count"]`
     - `source` $\leftarrow$ `"dataforseo_maps"`
2. **Implement Geo-Grid Coordinate Tiling:**
   - Define a bounding box grid for Lebanon (Beirut, Mount Lebanon, Tripoli, Sidon, Bekaa) and KSA (Riyadh, Jeddah, Dammam) with zoom `15z`–`16z`.
   - Post batch tasks (up to 2,000 per request) with `location_coordinate="lat,lon,15z"`.
3. **Deprecate `scrapers/google_places.py`:**
   - Remove dependency on `GOOGLE_PLACES_API_KEY` to eliminate Enterprise SKU billing and eliminate risk of Google Maps Platform ToS enforcement.
4. **Fallback / Redundancy Configuration:**
   - Keep **Bright Data Fast SERP** as an enterprise failover endpoint configured via environment variable `SERP_PROVIDER=dataforseo|brightdata`.
