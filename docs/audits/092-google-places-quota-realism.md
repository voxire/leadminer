# 092 — Google Places Query Ceiling and Market Reach

## Verdict

This is a discovery sampler, not a market-coverage crawler. The source contains **67 queries**, so the absolute Places API ceiling is **4,020 result slots per run** (not 4,080); category synonyms, overlapping nationwide/city searches, and sparse niche queries make a planning yield of roughly **1,500–2,500 unique Place IDs** more realistic. Against a deliberately broad estimate of **about 100,000–200,000 businesses in the queried sectors and markets**, that is around **1% reach** (roughly 0.8–2.5% across the estimate bounds), before accounting for results outside the intended Saudi cities.

## Findings

### S2 — The real hard cap is 4,020 slots, not an expandable result set

- **Where:** `scrapers/google_places.py:45-124, 163-167, 203-278`
- **Breaks:** Literal counting gives **33 Lebanon + 34 Saudi Arabia = 67 queries**. Places Text Search (New) returns at most 60 places across three 20-result pages per text query. Thus `67 × 60 = 4,020` is the hard maximum number of result appearances; the brief's 68-query arithmetic would be 4,080 but does not match the source. With no overlap, 4,020 could also be 4,020 unique IDs. The scraper's shared, locked `seen_ids` set suppresses repeat Place IDs across queries (`:170, :231-235`), so its emitted Google records can only be lower. The three-page ceiling is Google's service limit, not enforced locally; the loop itself has no `page >= 3` guard.
- **Breaks down by geography:** Lebanon has at most `33 × 60 = 1,980` slots; Saudi queries at most `34 × 60 = 2,040`. Only three Saudi queries explicitly target Dammam (`:120-124`), a maximum of **180 slots**. Most other KSA queries are limited to Riyadh/Jeddah or use Saudi Arabia-wide phrasing, which both leaves Dammam thinly covered and can return results outside the three target cities.
- **Fix:** Treat 4,020 as a one-run ceiling, correct the documented query count, and add explicit city/neighborhood or spatial partitioning; extra pagination cannot raise this cap.

### S2 — Query overlap consumes a material share of even the theoretical ceiling

- **Where:** `scrapers/google_places.py:45-124, 170-188, 227-235`
- **Breaks:** Queries are not disjoint partitions. Clear overlap clusters include `hotels in Lebanon` / `boutique hotels in Lebanon`; `schools` / `language schools`; `gyms` / `yoga studios`; `real estate offices` / `real estate developers`; and, in KSA, city-specific developer searches / Saudi-wide `property developers`, nationwide luxury-hotel and cafe-chain searches / city hospitality searches, plus `fintech startups` / `fintech companies` / `tech startups`. Some searches are subsets; Saudi-wide searches can also return the same prominent Riyadh/Jeddah places as city searches. `seen_ids` removes exact ID duplicates, but it does not make those repeated result slots discover new businesses.
- **How much:** No stored response fixtures or run-level per-query Place-ID logs are available to calculate observed overlap, and this audit did not execute the scraper. As a planning estimate, assume **35–50% overlap** if every query were full: 4,020 result appearances become **about 2,010–2,610 unique places**. In real runs, narrow queries such as fertility, insurtech, SaaS, and DTC often will not fill 60 slots; allowing for those unfilled slots yields a practical planning band of roughly **1,500–2,500 unique Places IDs per sweep** (about 2,000 at the midpoint). These are estimates, not measured API results. Actual overlap can only be settled by logging each query's returned IDs and computing the union/intersections.
- **Important distinction:** Place IDs are locations/listings, not necessarily distinct parent businesses. Multiple branches of one chain can each count as a place; conversely, the same listing returned by several queries counts once in this scraper.

### S2 — Current reach is approximately one percent of the addressable category stock

- **Where:** `scrapers/google_places.py:44-124`; market sources below
- **Breaks:** The endpoint returns a ranked top slice for each broad query, not an exhaustive inventory. A 60-result national search for restaurants or a 60-result city search for fashion brands cannot enumerate a market with thousands of candidates. Query count does not represent 67 separate 60-business territories: result sets repeat, many queries underfill, and nationwide KSA phrases are not limited to Riyadh/Jeddah/Dammam.
- **Addressable-market estimate:** There is no current, public, harmonized establishment census split by all these detailed categories *and* by the four target geographies. The following is therefore an order-of-magnitude planning estimate, not a claimed official count. It combines the project's existing 30–50k Lebanon all-business working estimate (`docs/audits/041-anomaly-detection.md:37-40`; also `072-run-failure-triage.md:338-340`), Saudi city/region business-stock signals, and a broad 20–45% allowance for the named sectors (retail/fashion, hospitality, health, property, professional services, education, and adjacent local services). Saudi ministry regional figures are **regions, not target cities**, and registered commercial registrations are not a clean count of unique active storefronts; they are useful scale checks, not exact denominators.

| Market | Public scale signal / limitation | Estimated businesses in the query-set's target categories |
|---|---|---:|
| **Lebanon** | The project's 30–50k all-business figure is a working assumption, not a recent CAS category census. The 33 queries cover a broad set of local sectors, but no governorate-specific result quotas exist. | **20k–35k** |
| **Riyadh** | Monsha'at reported over 570k SMEs in the **Riyadh Region** in Q4 2023 (43.7% of Saudi SMEs); region includes more than Riyadh city. A third-party city-directory listing indexed ~198.7k Riyadh companies, which is a noisy upper-scale cross-check, not official census data. | **40k–90k** |
| **Jeddah** | Jeddah is inside Makkah Region. A third-party directory indexed ~107.2k Jeddah companies; Monsha'at's regional share cannot be read as a Jeddah-city count. | **25k–50k** |
| **Dammam** | Dammam is inside Eastern Region. Monsha'at reported 16% of 1.6m Q4 2024 Saudi commercial registrations in the Eastern Region, but that includes many cities and all sectors; city/category breakout is not published in the cited summary. | **10k–25k** |
| **Total target category stock** | Ranges are approximate and not additive to registry counts without the stated sector/geography assumptions. | **95k–200k** |

- **Reach calculation:** Practical unique Places yield of **1,500–2,500 / 95,000–200,000** implies a broad sensitivity of **0.75–2.6%**. Midpoint illustration: **2,000 / 145,000 ≈ 1.4%**. Given denominator and overlap uncertainty, the plain-language result is **about 1% (order of 1–2%) of the addressable market per run**, not tens of percent. This is the Google query set's location-level discovery reach; it is not a measure of good-fit leads, and some nationwide Saudi results may be outside the requested cities.
- **Sources:** Google's [Text Search (New) documentation](https://developers.google.com/maps/documentation/places/web-service/text-search) documents the 60-result maximum. Saudi scale checks: [Monsha'at Q4 2023 SME Monitor, via SPA](https://www.spa.gov.sa/en/N2056534) (1.3m SMEs; >570k / 43.7% in Riyadh Region) and [Monsha'at Q4 2024 SME Monitor, via SPA](https://www.spa.gov.sa/en/N2273553) (1.6m commercial registrations; regional shares include Riyadh 39%, Makkah 17%, Eastern 16%). Saudi official [DataSaudi](https://datasaudi.sa/en/sector/accommodation-and-food-service-activities) reports 102,333 active accommodation/food-service enterprises nationally in 2024, illustrating that just one queried sector is larger than this scraper's total one-run ceiling. City-directory cross-checks: [Riyadh](https://companydata.com/companies/riyadh/) and [Jeddah](https://companydata.com/companies/jeddah/) (third-party datasets; counts should not be treated as official establishment statistics). Lebanon's cited 30–50k baseline is an internal working estimate, not a verified current national register.

## Not a bug, but worth knowing

- The 67 queries are a **query strategy**, not a guaranteed 67 × 60 output. A request may return fewer than 20, no result, or stop before page three; only the API's upper bound is deterministic.
- Lebanon is materially better represented in query breadth (33 different general business categories) than any one Saudi city. On the Saudi side, only three strings explicitly mention Dammam, and country-wide queries are not geographically constrained to the requested cities.
- The market fraction is necessarily approximate until the project records actual per-query counts/IDs and chooses an authoritative category-level establishment denominator. Even the optimistic all-pages/no-overlap ceiling of 4,020 is only **2.0–4.2%** of the estimated 95k–200k addressable stock, before query overlap and under-filled searches.

## Recommended order of work

1. Emit per-query `result_count`, page count, distinct Place IDs, and IDs shared with earlier queries; use these to replace the overlap assumption with measured run data.
2. Replace country-wide/large-city text searches with a documented city × category (or spatial-tile × category) matrix, prioritizing the 60-result truncation zones.
3. Obtain current active-establishment counts by industry and municipality from Lebanon CAS/registry and Saudi GASTAT/Ministry of Commerce; separate registered entities, branches/establishments, and Places listings before publishing a precise coverage percentage.
