# 031 — Pytest Suite Architecture and Regression Test Design

## Verdict

`leadminer` currently operates with 1,402 lines of Python, zero tests, and zero continuous verification. Because the codebase processes untrusted external data across concurrent threads with unhandled edge cases, unmocked network dependencies, and silent type coercion failures, any refactoring or scraping run risks total silent data corruption or mid-run catastrophic termination. A production-grade test suite must combine deterministic HTTP mocking (`responses` for unit tests, `vcrpy` for API record-replay), robust unit invariants across all 9 modules, property-based verification (`hypothesis`) for dedup and phone parsing, and strict schema validation on pipeline boundaries.

---

## HTTP Mocking Strategy Evaluation

The pipeline relies entirely on the synchronous `requests` library (`requests==2.32.3`). Choosing the right mocking layer is critical to ensure test speed, deterministic CI execution, and complete isolation from external rate limits and live third-party network flakiness.

| Framework | Mechanism | Applicability to `leadminer` | Verdict & Role |
|---|---|---|---|
| **`responses`** | Intercepts `requests.adapters.HTTPAdapter` at the transport layer using socket emulation. | **Primary Unit Mocking Tool.** Native fit for `requests.Session` and top-level `requests.get/post`. Provides strict call-count assertions, query/header/body matching, regex URLs, and simulated network timeouts/HTTP 429/500 faults without modifying code. | **Adopt for all unit tests** (`scrapers/osm.py`, `scrapers/wikidata.py`, `scrapers/google_places.py`, `enricher.py`). |
| **`requests-mock`** | Adapter-level mocking fixture for `requests`. | Strong alternative to `responses`. Slightly more natural when mounting adapters into custom sessions, but weaker call-history introspection and match syntax compared to `responses`. | **Secondary / Fallback.** Can be used for custom session mounts, but standardizing on `responses` minimizes test fixture cognitive load. |
| **`vcrpy` (`pytest-vcr`)** | Serializes real HTTP interactions to YAML cassettes and replays them deterministically. | **Integration & Golden Dataset Verification.** Essential for capturing massive Overpass (3,000+ elements) and Wikidata SPARQL (5,000 bindings) payloads. Unit tests should not hand-write 5,000 lines of JSON mock data; `vcrpy` allows snapshotting real API schemas while sanitizing `GOOGLE_PLACES_API_KEY`. | **Adopt for scraper integration tests** to verify that third-party schema mutations (e.g., OSM tag restructuring or Wikidata property deprecation) are caught immediately. |
| **`pytest-httpx`** | Custom transport mocker for the `httpx` HTTP library. | **Incompatible with current codebase.** `leadminer` does not import `httpx`. Attempting to mock `requests` with `pytest-httpx` has zero effect. | **Do not use today.** If the scraper engine is refactored from `requests` + `ThreadPoolExecutor` to `httpx` + `asyncio`, `pytest-httpx` will become the mandatory replacement for `responses`. |

### Mocking Tool Recommendations by Pipeline Layer
- **Scraper Unit Tests (`osm.py`, `wikidata.py`, `google_places.py`)**: `responses` for testing pagination, status code handling (401, 429, 500), network timeouts, and JSON decode errors.
- **Scraper Contract Tests**: `vcrpy` with sanitized credentials to validate schema parsing against real API response shapes.
- **Website Liveness & Enrichment (`enricher.py`)**: `responses` to mock HTML payloads with malformed regex targets, oversized pages (200,000-byte cap), SSL errors, and slow timeouts.
- **State & Pipeline Orchestration (`main.py`)**: `responses` combined with `tmp_path` filesystem fixtures to test end-to-end data flow without real network egress.

---

## Per-Module Test Specifications

Every module in `leadminer` requires targeted test coverage addressing its exact runtime behavior, type contracts, and failure modes.

### 1. `scrapers/base.py`
- **Mocking Approach:** None (pure type contracts and abstract base class enforcement).
- **Highest-Value Test Functions:**
  1. `test_business_record_typed_dict_keys`: Asserts that an instantiated `BusinessRecord` contains exactly the expected 23 keys (`name`, `category`, `address`, `region`, `country`, `lat`, `lon`, `phone`, `email`, `website`, `website_live`, `facebook`, `instagram`, `whatsapp`, `linkedin`, `rating`, `review_count`, `industry_priority`, `recommended_service`, `lead_score`, `source`, `scraped_at`, `completeness_score`).
  2. `test_business_record_type_annotations`: Asserts that `BusinessRecord.__annotations__` preserves nullable fields (`str | None`, `float | None`, `bool | None`) and mandatory primitive fields (`lead_score: int`, `completeness_score: int`).
  3. `test_base_scraper_abstract_instantiation`: Asserts that attempting to instantiate `BaseScraper` directly raises `TypeError: Can't instantiate abstract class BaseScraper with abstract method scrape`.
  4. `test_base_scraper_subclass_generator_contract`: Defines a dummy concrete scraper and asserts that `scrape()` returns an `Iterator[BusinessRecord]`, verifying generator semantics.

### 2. `scrapers/whitelist.py`
- **Mocking Approach:** None (pure functional deterministic domain logic).
- **Highest-Value Test Functions:**
  1. `test_is_business_category_hard_blocked_noise`: Asserts that all strings in `GEOGRAPHIC_NOISE` (e.g., `"village"`, `"city"`, `"ruins"`, `"watercourse"`, `"yes"`, `"organization"`) evaluate to `False` (`scrapers/whitelist.py:85-102, 115-116`).
  2. `test_is_business_category_priority_and_adjacent`: Asserts that items in `PRIORITY_INDUSTRIES` (e.g., `"clinic"`, `"restaurant"`, `"dentist"`) and `ADJACENT_BUSINESSES` (e.g., `"supermarket"`, `"gym"`, `"bakery"`) evaluate to `True` (`scrapers/whitelist.py:119-120`).
  3. `test_is_business_category_keyword_soft_allow`: Asserts that compound categories containing business keywords (`"agency"`, `"salon"`, `"boutique"`, `"center"`, `"firm"`) evaluate to `True` even if not in explicit sets (`scrapers/whitelist.py:123-128`).
  4. `test_is_business_category_none_and_empty`: Asserts that `None`, `""`, and whitespace-only strings evaluate to `False` without raising `AttributeError` (`scrapers/whitelist.py:110-112`).
  5. `test_is_business_category_non_string_type_safety`: Asserts that passing integer, float, boolean, or list inputs does not crash with `AttributeError` on `.strip().lower()` (catches S2 bug at `scrapers/whitelist.py:112`).
  6. `test_industry_priority_tier_mapping`: Asserts that `industry_priority()` strictly maps priority industries to `"high"`, adjacent industries to `"medium"`, and unlisted/None/empty categories to `"low"` (`scrapers/whitelist.py:133-145`).

### 3. `scrapers/osm.py`
- **Mocking Approach:** `responses` (to mock `POST https://overpass-api.de/api/interpreter`).
- **Highest-Value Test Functions:**
  1. `test_osm_scrape_success_node_and_way_parsing`: Uses `responses` (HTTP 200) to return a minimal Overpass payload with one `node` (direct `lat`/`lon`) and one `way` (nested `center.lat`/`center.lon`); asserts coordinates, name resolution (`name:en` fallback), address concatenation, and `BusinessRecord` field values (`scrapers/osm.py:58-119`).
  2. `test_osm_scrape_retry_backoff_and_recovery`: Uses `responses` to return two consecutive HTTP 504 Gateway Timeouts followed by HTTP 200; mocks `time.sleep` and asserts all 3 attempts were triggered and valid records were yielded (`scrapers/osm.py:38-54`).
  3. `test_osm_scrape_retry_exhaustion`: Uses `responses` to return three HTTP 500 errors; asserts graceful iteration termination with 0 records yielded and no unhandled exception crash (`scrapers/osm.py:51-53`).
  4. `test_osm_scrape_unhandled_json_decode_error`: Uses `responses` to return HTTP 200 with HTML timeout body (`<html>Query timed out</html>`); asserts behavior when `resp.json()` raises `JSONDecodeError` (catches S1 bug at `scrapers/osm.py:55`).
  5. `test_osm_scrape_website_protocol_prepended`: Asserts that raw website tags lacking `http` (e.g., `"example.com"`) are properly transformed to `"https://example.com"` (`scrapers/osm.py:92-93`).
  6. `test_osm_scrape_user_agent_header`: Uses `responses` callback to verify that the request includes `User-Agent: leadminer/1.0 (<email>)` matching `SCRAPER_EMAIL` (`scrapers/osm.py:35-36`).

### 4. `scrapers/wikidata.py`
- **Mocking Approach:** `responses` (to mock `GET https://query.wikidata.org/sparql`).
- **Highest-Value Test Functions:**
  1. `test_wikidata_scrape_success_binding_extraction`: Uses `responses` (HTTP 200) with SPARQL JSON bindings; asserts correct mapping of `itemLabel`, `websiteLabel`, `phoneLabel`, `emailLabel`, and `categoryLabel` into `BusinessRecord` (`scrapers/wikidata.py:58-99`).
  2. `test_wikidata_scrape_filters_q_id_fallbacks`: Uses `responses` with entities whose `itemLabel` is an unresolved Wikidata Q-ID (e.g. `"Q1082348"` or missing); asserts that such entities are skipped (`scrapers/wikidata.py:63-64`).
  3. `test_wikidata_scrape_retry_and_timeout`: Uses `responses` to simulate SPARQL HTTP 429 rate limit / 503 gateway error with retry loop; asserts retry behavior and final return on failure (`scrapers/wikidata.py:40-56`).
  4. `test_wikidata_scrape_html_gateway_timeout_json_error`: Uses `responses` (HTTP 200 with HTML error text); asserts handling of `resp.json()` failure (catches S1 bug at `scrapers/wikidata.py:58`).
  5. `test_wikidata_scrape_website_https_normalization`: Asserts raw domain strings are prefixed with `"https://"` (`scrapers/wikidata.py:72-73`).

### 5. `scrapers/google_places.py`
- **Mocking Approach:** `responses` (to mock `POST https://places.googleapis.com/v1/places:searchText`).
- **Highest-Value Test Functions:**
  1. `test_google_places_missing_api_key_exits_cleanly`: Unsets `GOOGLE_PLACES_API_KEY` in `monkeypatch`; asserts `scrape()` prints message and immediately returns empty iterator without network calls (`scrapers/google_places.py:158-160`).
  2. `test_google_places_country_from_query_mapping`: Asserts `_country_from_query` maps Lebanese queries to `"LB"` and Saudi queries (`"riyadh"`, `"jeddah"`, `"dammam"`, `"mecca"`, `"medina"`) to `"SA"` (`scrapers/google_places.py:126-136`).
  3. `test_google_places_pagination_token_flow`: Uses `responses` to return page 1 with `nextPageToken: "token_123"` and page 2 without `nextPageToken`; asserts 2 HTTP calls made with matching `pageToken` request bodies and combined records yielded (`scrapers/google_places.py:203-278`).
  4. `test_google_places_401_unauthorized_aborts_query`: Uses `responses` (HTTP 401); asserts that query terminates without looping and returns collected records (`scrapers/google_places.py:210-212`).
  5. `test_google_places_429_rate_limit_retry`: Uses `responses` to return HTTP 429 then HTTP 200; mocks `time.sleep` and asserts rate limit handling (`scrapers/google_places.py:213-216`).
  6. `test_google_places_worker_pool_exception_handling`: Mocks a failure in one query future; asserts whether `f.result()` in `as_completed` kills all queries or isolates failure (catches S1 bug at `scrapers/google_places.py:186`).
  7. `test_google_places_place_id_deduplication`: Returns identical `places.id` across distinct pages; asserts thread-safe deduplication via `seen_ids` (`scrapers/google_places.py:231-235`).
  8. `test_google_places_pick_category_filters_generic_types`: Asserts `_pick_category(["establishment", "point_of_interest", "dentist"])` resolves to `"dentist"` (`scrapers/google_places.py:292-296`).

### 6. `dedup.py`
- **Mocking Approach:** None (pure algorithmic string manipulation and data structure merging).
- **Highest-Value Test Functions:**
  1. `test_normalize_phone_lebanon_formats`: Asserts canonical `+961` formatting for local mobile (`"03 123 456"` -> `"+9613123456"`), local 7-digit (`"70 123 456"` -> `"+96170123456"`), Beirut landline (`"01 234 567"` -> `"+9611234567"`), international prefix (`"+961 3 123 456"` -> `"+9613123456"`), and double-zero (`"00961 3 123 456"` -> `"+9613123456"`).
  2. `test_normalize_phone_saudi_formats`: Asserts canonical `+966` formatting for mobile (`"050 123 4567"` -> `"+966501234567"`), Riyadh landline (`"011 234 5678"` -> `"+966112345678"`), and international form (`"+966 50 123 4567"` -> `"+966501234567"`).
  3. `test_normalize_phone_trunk_zero_regression`: Asserts whether `normalize_phone("+961 01 234 567", "LB")` strips the redundant national trunk zero or mistakenly produces `+96101234567` (`dedup.py:15-20`).
  4. `test_normalize_phone_foreign_country_cross_corruption`: Asserts whether passing an explicit Saudi number `+966 50 123 4567` with country `"LB"` produces `+961966501234567` (catches S1 bug at `dedup.py:11-25`).
  5. `test_normalize_phone_short_codes_and_junk`: Asserts handling of short codes (`"112"`, `"911"`) and punctuation-only inputs (`"---"`, `"+"`), verifying whether dedup keys collide on `+` (`dedup.py:23-25`).
  6. `test_normalize_name_unicode_and_case`: Asserts NFKD normalization, stripping of accents/diacritics (`"Café de Beyrouth"` -> `"cafe de beyrouth"`), and collapsing of whitespace (`dedup.py:28-31`).
  7. `test_extract_city_comma_separated`: Asserts extraction of last comma-delimited token (`"Hamra, Beirut"` -> `"beirut"`, `None` -> `""`) (`dedup.py:34-38`).
  8. `test_merge_source_union_and_scraped_at_max`: Asserts that merging two records unions `source` strings (`"osm"` + `"google_places"` -> `"google_places|osm"`) and takes `max(scraped_at)` (`dedup.py:54-58`).
  9. `test_merge_field_count_preference`: Asserts that non-null fields from the record with higher total non-null attributes take precedence over the sparser record (`dedup.py:45-61`).
  10. `test_dedup_two_pass_phone_and_name_city`: Asserts that records with matching normalized phones merge into `phone_records`, and records lacking phones merge on `(normalized_name, city)` without dropping leads (`dedup.py:64-97`).

### 7. `enricher.py`
- **Mocking Approach:** `responses` (for HTTP website fetching) + `monkeypatch` (for thread pool sizing).
- **Highest-Value Test Functions:**
  1. `test_infer_region_lebanon_address_keywords`: Asserts matching of address substrings to Lebanese governorates (`"Hamra, Beirut"` -> `"Beirut"`, `"Jounieh"` -> `"Mount Lebanon"`, `"Tripoli"` -> `"North Lebanon"`) (`enricher.py:10-55, 79-88`).
  2. `test_infer_region_coordinate_bounding_boxes`: Asserts coordinate fallbacks for Lebanon (`lat=33.88, lon=35.51` -> `"Beirut"`) and Saudi Arabia (`country="SA", lat=24.71, lon=46.67` -> `"Riyadh"`) (`enricher.py:58-76, 89-94`).
  3. `test_completeness_score_calculation`: Asserts that each present contact channel (`phone`, `email`, `website`, `address`, `facebook`/`instagram`, `whatsapp`, `linkedin`) adds exactly 1 point up to a maximum of 7 (`enricher.py:101-117`).
  4. `test_fetch_website_contact_extraction_regex`: Uses `responses` (HTTP 200) with HTML containing `mailto:`, raw email, Instagram profile link, WhatsApp wa.me link, and LinkedIn company slug; asserts extraction into `contacts` dictionary (`enricher.py:142-177`).
  5. `test_fetch_website_blacklisted_domains_and_handles`: Uses `responses` with HTML containing `sentry.io`, `example.com`, and `instagram.com/p/post_id`; asserts that blacklisted values are discarded (`enricher.py:135-139, 156-164`).
  6. `test_fetch_website_status_code_boundaries`: Uses `responses` across status codes (200 -> `live=True`, 404 -> `live=True, contacts=empty`, 500/503 -> `live=False`, Timeout -> `live=False`) (`enricher.py:147-152`).
  7. `test_check_websites_threadpool_future_exception_resilience`: Uses `responses` to throw a `requests.exceptions.ConnectionError` on one worker; asserts whether naked `future.result()` kills `check_websites` or completes the remaining records (catches S1 bug at `enricher.py:193`).
  8. `test_lead_score_weighting_matrix`: Asserts exact scoring math for email (+20), whatsapp (+15), phone (+15), dead website (+20), high priority (+15), rating < 4.0 (+10), multi-source (+5), capped at 100 (`enricher.py:227-260`).

### 8. `pitch_recommender.py`
- **Mocking Approach:** None (pure decision-table logic).
- **Highest-Value Test Functions:**
  1. `test_recommend_service_dead_website_tier1`: Asserts that `has_website=True` and `website_live=False` always triggers `"Website rebuild + maintenance"` regardless of industry category or rating (`pitch_recommender.py:52-55`).
  2. `test_recommend_service_ecom_friendly_no_website`: Asserts that a business in `_ECOM_FRIENDLY` with social media but no website triggers `"E-commerce launch + Instagram-to-store funnel"` (`pitch_recommender.py:57-60`).
  3. `test_recommend_service_low_reviews_seo`: Asserts that a business with a live website but `review_count < 20` triggers `"SEO audit + visibility upgrade"` (`pitch_recommender.py:67-68`).
  4. `test_recommend_service_low_rating_reputation`: Asserts that a live website with `rating < 4.0` and `review_count >= 20` triggers `"Reputation + SEO turnaround"` (`pitch_recommender.py:70-71`).
  5. `test_recommend_service_rtylr_hospitality_fnb`: Asserts that restaurants and cafes with phone or social trigger `"RTYLR commerce OS (POS, online ordering, CRM)"` (`pitch_recommender.py:73-76`).
  6. `test_recommend_service_lead_gen_verticals`: Asserts that clinics and law firms trigger `"Lead-gen website + Google Ads launch"` when without website, or `"Lead-gen overhaul (landing pages + Google Ads + WhatsApp capture)"` when with website (`pitch_recommender.py:78-83`).
  7. `test_recommend_service_full_digital_launch`: Asserts that leads with no website and no social trigger `"Full digital launch (brand + website + social setup)"` (`pitch_recommender.py:85-88`).
  8. `test_is_low_rating_type_safety`: Asserts that `_is_low_rating` handles string floats (`"3.8"`), `None`, and invalid strings (`"N/A"`) without throwing `ValueError` or `TypeError` (`pitch_recommender.py:132-137`).

### 9. `main.py`
- **Mocking Approach:** `responses` (for scrapers/enricher) + `tmp_path` fixture (for CSV filesystem I/O).
- **Highest-Value Test Functions:**
  1. `test_load_master_type_casting`: Writes a test CSV with mixed types (`""`, `"True"`, `"False"`, `"4.5"`, `"12"`); asserts `load_master()` casts empty strings to `None`, floats to `float`, ints to `int`, and `"True"`/`"False"` to boolean `True`/`False` (`main.py:46-71`).
  2. `test_write_csv_atomic_headers_and_row_count`: Writes records with extra unknown keys; asserts output CSV contains exactly `FIELDS` header in exact order with `extrasaction="ignore"` (`main.py:74-80`).
  3. `test_has_any_contact_logic`: Asserts true when phone digits >= 7, valid email length, or instagram handle length > 3; asserts false for empty/whitespace contacts (`main.py:82-90`).
  4. `test_main_orchestration_flow_e2e`: Mocks all 3 scrapers, provides a mock master CSV, mocks HTTP website liveness with `responses`; executes `main()` and asserts creation of all 5 CSVs (`all_businesses.csv`, `qualified_businesses.csv`, `with_websites.csv`, `without_websites.csv`, `sales_ready.csv`) with correct row filtering (`main.py:93-181`).
  5. `test_sales_ready_subset_criteria`: Asserts that `sales_ready.csv` includes only records with `has_any_contact(r) == True` and `industry_priority in ("high", "medium")` (`main.py:142-145`).

---

## Top 30 High-Value Regression Tests (Prioritized Catalog)

These 30 tests are selected and ranked specifically to catch real bugs, silent data corruption, thread-safety crashes, and pipeline termination risks identified across the codebase.

```
+----+---------------------------------------------------------------+-----------------------------+----------+
| #  | Test Function Name                                            | Target Module               | Severity |
+----+---------------------------------------------------------------+-----------------------------+----------+
| 01 | test_check_websites_thread_exception_does_not_abort_run       | enricher.py:191-195         | S1       |
| 02 | test_google_places_worker_pool_exception_does_not_kill_batch  | scrapers/google_places:186  | S1       |
| 03 | test_osm_scrape_json_decode_error_on_html_timeout             | scrapers/osm.py:55          | S1       |
| 04 | test_wikidata_scrape_json_decode_error_on_gateway_timeout     | scrapers/wikidata.py:58     | S1       |
| 05 | test_normalize_phone_cross_country_prefix_corruption         | dedup.py:11-25              | S1       |
| 06 | test_normalize_phone_retains_trunk_zero_duplicating_keys      | dedup.py:15-20              | S1       |
| 07 | test_normalize_phone_non_string_type_error                    | dedup.py:12                 | S1       |
| 08 | test_normalize_name_non_string_type_error                     | dedup.py:30                 | S1       |
| 09 | test_merge_source_non_string_attribute_error                  | dedup.py:55                 | S1       |
| 10 | test_merge_scraped_at_mixed_type_comparison                   | dedup.py:58                 | S1       |
| 11 | test_is_business_category_non_string_attribute_error          | scrapers/whitelist.py:112   | S1       |
| 12 | test_recommend_service_non_string_category_attribute_error   | pitch_recommender.py:50     | S1       |
| 13 | test_write_csv_atomic_write_failure_preserves_master          | main.py:75-78               | S1       |
| 14 | test_load_master_missing_file_returns_empty_list              | main.py:48-49               | S1       |
| 15 | test_load_master_boolean_website_live_roundtrip               | main.py:68-69               | S1       |
| 16 | test_dedup_phone_matching_merges_records_without_loss         | dedup.py:68-76              | S2       |
| 17 | test_dedup_name_city_fallback_when_phone_missing              | dedup.py:76-84              | S2       |
| 18 | test_dedup_captured_phones_second_pass_filtering              | dedup.py:90-95              | S2       |
| 19 | test_google_places_seen_ids_thread_safe_deduplication        | scrapers/google_places:231  | S2       |
| 20 | test_google_places_429_rate_limit_backoff_and_exit            | scrapers/google_places:213  | S2       |
| 21 | test_google_places_pagination_exhausts_next_page_token        | scrapers/google_places:275  | S2       |
| 22 | test_osm_scrape_way_center_fallback_coordinates               | scrapers/osm.py:65-66       | S2       |
| 23 | test_wikidata_scrape_filters_unresolved_q_ids                 | scrapers/wikidata.py:63-64  | S2       |
| 24 | test_enricher_fetch_website_buffer_cap_prevents_memory_dos    | enricher.py:150             | S2       |
| 25 | test_enricher_email_blacklist_filtering                       | enricher.py:135-138, 156    | S2       |
| 26 | test_enricher_instagram_blacklist_filtering                   | enricher.py:139, 161-165    | S2       |
| 27 | test_lead_score_dead_website_bonus_over_live_website          | enricher.py:241-244         | S2       |
| 28 | test_pitch_recommender_dead_website_supercedes_all_rules      | pitch_recommender.py:52-55  | S2       |
| 29 | test_pitch_recommender_rtylr_priority_over_lead_gen           | pitch_recommender.py:75-76  | S2       |
| 30 | test_sales_ready_requires_contact_and_priority_filter         | main.py:142-145             | S2       |
+----+---------------------------------------------------------------+-----------------------------+----------+
```

### Detailed Specifications for the Top 30 Regressions

#### 1. `test_check_websites_thread_exception_does_not_abort_run`
- **Where:** `enricher.py:191-195`
- **What it Asserts:** Asserts that when one website in a 40-thread batch raises an unhandled exception (e.g. `RuntimeError` on cookie contention or connection abort), `future.result()` does not bubble out to crash the main thread; remaining records are processed and enriched.
- **Trigger Caught:** Naked `live, contacts = future.result()` re-raises and aborts the entire run after hours of scraping before CSV write.
- **Mocking Approach:** `responses` throwing `requests.exceptions.ConnectionError` on `http://failing-site.com` and returning HTTP 200 on `http://healthy-site.com`.
- **Severity:** S1

#### 2. `test_google_places_worker_pool_exception_does_not_kill_batch`
- **Where:** `scrapers/google_places.py:183-187`
- **What it Asserts:** Asserts that an unhandled error in 1 of the 68 query threads does not terminate the `as_completed(futures)` loop before `yield from all_records`.
- **Trigger Caught:** `f.result()` re-raises on query #68, discarding all accumulated records from the first 67 successful queries.
- **Mocking Approach:** `responses` mocking 67 successful Google Places queries and 1 query returning invalid JSON.
- **Severity:** S1

#### 3. `test_osm_scrape_json_decode_error_on_html_timeout`
- **Where:** `scrapers/osm.py:55`
- **What it Asserts:** Asserts that when Overpass times out and returns HTTP 200 with an HTML error body, `OSMScraper` catches `requests.exceptions.JSONDecodeError` / `json.JSONDecodeError` and exits gracefully without crashing the pipeline.
- **Trigger Caught:** `resp.json()` is placed outside the `try/except requests.RequestException` block, crashing `OSMScraper.scrape()`.
- **Mocking Approach:** `responses.post(OVERPASS_URL, body="<html>runtime error: Query timed out</html>", status=200)`.
- **Severity:** S1

#### 4. `test_wikidata_scrape_json_decode_error_on_gateway_timeout`
- **Where:** `scrapers/wikidata.py:58`
- **What it Asserts:** Asserts that when Wikidata returns HTTP 200/504 with an HTML maintenance page, `WikidataScraper` does not terminate the process with an uncaught `JSONDecodeError`.
- **Trigger Caught:** `resp.json()` is placed outside `try/except`, crashing the scraper.
- **Mocking Approach:** `responses.get(SPARQL_ENDPOINT, body="<title>Gateway Timeout</title>", status=200)`.
- **Severity:** S1

#### 5. `test_normalize_phone_cross_country_prefix_corruption`
- **Where:** `dedup.py:11-25`
- **What it Asserts:** Asserts that a Saudi number (`"+966 50 123 4567"`) processed with default country `"LB"` is NOT prefixed into an invalid Lebanese number (`"+961966501234567"`).
- **Trigger Caught:** `normalize_phone` blindly prepends the country calling code when digits do not start with that specific country's calling code, corrupting dedup keys across multi-country runs.
- **Mocking Approach:** None (pure unit assertion).
- **Severity:** S1

#### 6. `test_normalize_phone_retains_trunk_zero_duplicating_keys`
- **Where:** `dedup.py:15-20`
- **What it Asserts:** Asserts that `normalize_phone("+961 01 234 567", "LB")` and `normalize_phone("01 234 567", "LB")` produce the exact same canonical string `"+9611234567"`.
- **Trigger Caught:** International format with trunk zero (`+961 0...`) is returned immediately as `+96101234567`, while national format (`01...`) returns `+9611234567`, generating two distinct keys for the same business.
- **Mocking Approach:** None.
- **Severity:** S1

#### 7. `test_normalize_phone_non_string_type_error`
- **Where:** `dedup.py:12`
- **What it Asserts:** Asserts that passing an integer or float phone number (e.g. from an unquoted CSV cell or raw JSON payload) does not crash with `TypeError: expected string or bytes-like object, got 'int'`.
- **Trigger Caught:** `re.sub(r"\D", "", phone)` crashes when `phone` is not a string.
- **Mocking Approach:** None.
- **Severity:** S1

#### 8. `test_normalize_name_non_string_type_error`
- **Where:** `dedup.py:29-30`
- **What it Asserts:** Asserts that passing a non-string or numeric `name` to `normalize_name()` does not crash with `TypeError: normalize() argument 2 must be str, not int`.
- **Trigger Caught:** Scrapers or CSVs yielding integer names (e.g., business named `"1001"`) crashing unicodedata normalization.
- **Mocking Approach:** None.
- **Severity:** S1

#### 9. `test_merge_source_non_string_attribute_error`
- **Where:** `dedup.py:55`
- **What it Asserts:** Asserts that when merging two records where `source` is missing, `None`, or non-string, `_merge` does not crash with `AttributeError: 'NoneType' object has no attribute 'split'`.
- **Trigger Caught:** `av.split("|")` assumes string type without type guards.
- **Mocking Approach:** None.
- **Severity:** S1

#### 10. `test_merge_scraped_at_mixed_type_comparison`
- **Where:** `dedup.py:58`
- **What it Asserts:** Asserts that merging records with disparate `scraped_at` formats (e.g., ISO string vs integer timestamp vs `None`) does not raise `TypeError: '>' not supported between instances`.
- **Trigger Caught:** `max(av, bv)` raises `TypeError` when comparing incompatible types.
- **Mocking Approach:** None.
- **Severity:** S1

#### 11. `test_is_business_category_non_string_attribute_error`
- **Where:** `scrapers/whitelist.py:112`
- **What it Asserts:** Asserts that `is_business_category(123)` returns `False` instead of crashing with `AttributeError: 'int' object has no attribute 'strip'`.
- **Trigger Caught:** Numeric or malformed category tag crashing `raw_filtered` list comprehension at `main.py:117`.
- **Mocking Approach:** None.
- **Severity:** S1

#### 12. `test_recommend_service_non_string_category_attribute_error`
- **Where:** `pitch_recommender.py:50`
- **What it Asserts:** Asserts that `recommend_service({"category": 1234})` safely resolves without crashing.
- **Trigger Caught:** `(record.get("category") or "").strip().lower()` raises `AttributeError` when `category` is an integer.
- **Mocking Approach:** None.
- **Severity:** S1

#### 13. `test_write_csv_atomic_write_failure_preserves_master`
- **Where:** `main.py:75-78`
- **What it Asserts:** Asserts that an unhandled error or interruption during CSV writing does not wipe the cumulative master file into a 0-byte truncated file.
- **Trigger Caught:** `open(path, "w")` truncates existing files immediately before writing completes.
- **Mocking Approach:** Mocking `csv.DictWriter.writerows` to raise `IOError` and asserting original file content remains intact on disk via `tmp_path`.
- **Severity:** S1

#### 14. `test_load_master_missing_file_returns_empty_list`
- **Where:** `main.py:48-49`
- **What it Asserts:** Asserts that calling `load_master()` on a non-existent path cleanly returns `[]` without raising `FileNotFoundError`.
- **Trigger Caught:** Pipeline crash on initial clean run when `data/all_businesses.csv` does not exist.
- **Mocking Approach:** `tmp_path / "non_existent.csv"`.
- **Severity:** S1

#### 15. `test_load_master_boolean_website_live_roundtrip`
- **Where:** `main.py:68-69`
- **What it Asserts:** Asserts that CSV string values `"True"`, `"False"`, and `""` are faithfully deserialized into Python `True`, `False`, and `None` respectively, preserving website liveness states across runs.
- **Trigger Caught:** String `"False"` evaluating to truthy boolean `True` if parsed naively with `bool(val)`.
- **Mocking Approach:** `tmp_path` fixture writing and reading CSV.
- **Severity:** S1

#### 16. `test_dedup_phone_matching_merges_records_without_loss`
- **Where:** `dedup.py:68-76`
- **What it Asserts:** Asserts that two records with the same normalized phone number merge into a single record whose `source` is the union of both sources and whose attributes are preserved.
- **Trigger Caught:** Duplicate records bloating master database and causing redundant outbound sales outreach.
- **Mocking Approach:** None.
- **Severity:** S2

#### 17. `test_dedup_name_city_fallback_when_phone_missing`
- **Where:** `dedup.py:76-84`
- **What it Asserts:** Asserts that two records lacking phone numbers but having identical normalized names and cities merge correctly on the `(name, city)` composite key.
- **Trigger Caught:** Unphoned records from OSM and Wikidata creating duplicate entries for the same physical storefront.
- **Mocking Approach:** None.
- **Severity:** S2

#### 18. `test_dedup_captured_phones_second_pass_filtering`
- **Where:** `dedup.py:90-95`
- **What it Asserts:** Asserts that records merged in the name index do not leak into the final output if their phone number was already captured in the phone index.
- **Trigger Caught:** Cross-index duplicate leakage between phone and name-city passes.
- **Mocking Approach:** None.
- **Severity:** S2

#### 19. `test_google_places_seen_ids_thread_safe_deduplication`
- **Where:** `scrapers/google_places.py:231-235`
- **What it Asserts:** Asserts that multiple concurrent worker threads encountering the same Google `place_id` across overlapping queries (e.g., "cafes in Lebanon" vs "restaurants in Lebanon") only yield the record once.
- **Trigger Caught:** Thread race condition where duplicate `place_id` bypasses `seen_ids`.
- **Mocking Approach:** `responses` mocking overlapping query responses.
- **Severity:** S2

#### 20. `test_google_places_429_rate_limit_backoff_and_exit`
- **Where:** `scrapers/google_places.py:213-216`
- **What it Asserts:** Asserts that when Google returns HTTP 429, the scraper sleeps for 30s and retries; also asserts bounded retry count so worker does not hang infinitely.
- **Trigger Caught:** Unbounded `while True` loop on persistent 429 causing GitHub Actions job timeout (300 min).
- **Mocking Approach:** `responses` returning 429 followed by 200, mocking `time.sleep`.
- **Severity:** S2

#### 21. `test_google_places_pagination_exhausts_next_page_token`
- **Where:** `scrapers/google_places.py:203-278`
- **What it Asserts:** Asserts that pagination requests chain `nextPageToken` into `body["pageToken"]` until `nextPageToken` is omitted.
- **Trigger Caught:** Premature pagination termination or dropped pages.
- **Mocking Approach:** `responses` sequential POST mocks.
- **Severity:** S2

#### 22. `test_osm_scrape_way_center_fallback_coordinates`
- **Where:** `scrapers/osm.py:65-66`
- **What it Asserts:** Asserts that OSM `way` elements (which do not have top-level `lat`/`lon`) successfully extract coordinates from `el["center"]["lat"]` and `el["center"]["lon"]`.
- **Trigger Caught:** All OSM building footprints (`way`) silently receiving `None` for latitude and longitude.
- **Mocking Approach:** `responses.post(OVERPASS_URL)`.
- **Severity:** S2

#### 23. `test_wikidata_scrape_filters_unresolved_q_ids`
- **Where:** `scrapers/wikidata.py:63-64`
- **What it Asserts:** Asserts that entities where SPARQL returned raw item IDs (e.g., `"Q421543"`) instead of human labels are dropped.
- **Trigger Caught:** Raw entity identifiers polluting business name columns in production CSVs.
- **Mocking Approach:** `responses.get(SPARQL_ENDPOINT)`.
- **Severity:** S2

#### 24. `test_enricher_fetch_website_buffer_cap_prevents_memory_dos`
- **Where:** `enricher.py:150`
- **What it Asserts:** Asserts that `_fetch_website` slices HTML content at 200,000 characters (`r.text[:200_000]`), preventing memory exhaustion when scraping multi-megabyte bloated landing pages.
- **Trigger Caught:** Memory bloat / OOM crashes during 40-thread parallel website parsing.
- **Mocking Approach:** `responses.get("http://bloated.com", body="A" * 5_000_000, status=200)`.
- **Severity:** S2

#### 25. `test_enricher_email_blacklist_filtering`
- **Where:** `enricher.py:135-138, 156`
- **What it Asserts:** Asserts that generic platform emails (e.g. `support@sentry.io`, `info@wixpress.com`, `user@example.com`) and `.png` image strings are filtered out, preserving only real business email addresses.
- **Trigger Caught:** Platform infrastructure addresses overwriting business contact emails.
- **Mocking Approach:** `responses.get("http://test.com", body='<a href="mailto:dev@sentry.io"></a><a href="mailto:ceo@mybiz.com"></a>', status=200)`.
- **Severity:** S2

#### 26. `test_enricher_instagram_blacklist_filtering`
- **Where:** `enricher.py:139, 161-165`
- **What it Asserts:** Asserts that common Instagram URL segments (`/explore`, `/reels`, `/stories`, `/p/`) are not erroneously extracted as Instagram account handles.
- **Trigger Caught:** False positive Instagram handles stored as `"explore"` or `"reels"`.
- **Mocking Approach:** `responses.get("http://test.com", body='<a href="https://instagram.com/reels"></a><a href="https://instagram.com/realhandle"></a>', status=200)`.
- **Severity:** S2

#### 27. `test_lead_score_dead_website_bonus_over_live_website`
- **Where:** `enricher.py:241-244`
- **What it Asserts:** Asserts that a business with a dead website (`website` present and `website_live=False`) receives +20 points, whereas a live website receives +10 points.
- **Trigger Caught:** Incorrect lead prioritization where prime website rebuild opportunities are scored lower than businesses with functioning websites.
- **Mocking Approach:** None.
- **Severity:** S2

#### 28. `test_pitch_recommender_dead_website_supercedes_all_rules`
- **Where:** `pitch_recommender.py:52-55`
- **What it Asserts:** Asserts that any record with a dead website (`website` present and `website_live=False`) always receives `"Website rebuild + maintenance"`, even if it belongs to RTYLR, e-commerce, or clinic categories.
- **Trigger Caught:** Rule order inversion causing broken websites to receive SEO or Google Ads pitches instead of website rebuilds.
- **Mocking Approach:** None.
- **Severity:** S2

#### 29. `test_pitch_recommender_rtylr_priority_over_lead_gen`
- **Where:** `pitch_recommender.py:75-84`
- **What it Asserts:** Asserts that a restaurant with a phone number and live website receives `"RTYLR commerce OS (POS, online ordering, CRM)"` rather than a generic digital marketing retainer.
- **Trigger Caught:** Rule evaluation misordering misclassifying core POS/ERP prospects.
- **Mocking Approach:** None.
- **Severity:** S2

#### 30. `test_sales_ready_requires_contact_and_priority_filter`
- **Where:** `main.py:142-145`
- **What it Asserts:** Asserts that records with `industry_priority == "low"` or records lacking all contact channels (`phone`, `email`, `instagram`) are excluded from `data/sales_ready.csv`.
- **Trigger Caught:** Uncontactable leads or low-margin noise flooding sales outbound sheets.
- **Mocking Approach:** None.
- **Severity:** S2

---

## Hypothesis Property-Based Testing Suite

Property-based testing using `hypothesis` tests invariants across thousands of randomized, synthetically generated inputs. Below are 6 fully executable property test designs targeting the core normalization, identity resolution, and merge algorithms.

```python
"""
tests/test_properties.py - Property-based invariant tests for leadminer.
Run with: pytest tests/test_properties.py
"""

import copy
import random
import re
from hypothesis import given, settings, assume, strategies as st
from dedup import normalize_phone, normalize_name, _merge, dedup


# ---------------------------------------------------------------------------
# Strategy Generators
# ---------------------------------------------------------------------------

country_strat = st.sampled_from(["LB", "SA"])
digits_strat = st.from_regex(r"[0-9]{1,15}", fullmatch=True)
noise_strat = st.text(alphabet=" -()./[]#_abcxyzABCXYZ", min_size=0, max_size=5)

@st.composite
def raw_phone_strat(draw):
    """Generates realistic dirty phone numbers with mixed country calling codes and noise."""
    cc = draw(st.sampled_from(["", "00", "+", "00961", "+961", "961", "00966", "+966", "966"]))
    body = draw(st.from_regex(r"[0-9]{6,10}", fullmatch=True))
    prefix = draw(noise_strat)
    suffix = draw(noise_strat)
    return f"{prefix}{cc}{body}{suffix}"


@st.composite
def business_record_strat(draw):
    """Generates well-formed BusinessRecord dictionaries."""
    name = draw(st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -'", min_size=1, max_size=40))
    country = draw(country_strat)
    has_phone = draw(st.booleans())
    phone = draw(raw_phone_strat()) if has_phone else None
    city = draw(st.sampled_from(["Beirut", "Tripoli", "Riyadh", "Jeddah", "Dammam", "Sidon", "Zahle"]))
    address = f"Street {draw(st.integers(1, 100))}, {city}"
    sources = draw(st.sampled_from(["osm", "wikidata", "google_places"]))

    return {
        "name": name,
        "category": draw(st.sampled_from(["restaurant", "clinic", "supermarket", "law_firm"])),
        "address": address,
        "region": None,
        "country": country,
        "lat": draw(st.floats(min_value=20.0, max_value=36.0)),
        "lon": draw(st.floats(min_value=35.0, max_value=51.0)),
        "phone": phone,
        "email": draw(st.one_of(st.none(), st.emails())),
        "website": draw(st.one_of(st.none(), st.from_regex(r"https?://[a-z0-9]+\.com", fullmatch=True))),
        "website_live": draw(st.one_of(st.none(), st.booleans())),
        "facebook": None,
        "instagram": None,
        "whatsapp": None,
        "linkedin": None,
        "rating": draw(st.one_of(st.none(), st.floats(1.0, 5.0))),
        "review_count": draw(st.one_of(st.none(), st.integers(0, 500))),
        "industry_priority": None,
        "recommended_service": None,
        "lead_score": 0,
        "source": sources,
        "scraped_at": "2026-10-03T00:00:00Z",
        "completeness_score": 0,
    }


# ---------------------------------------------------------------------------
# Property 1: Phone Normalization Idempotency
# ---------------------------------------------------------------------------

@given(raw_phone=raw_phone_strat(), country=country_strat)
@settings(max_examples=500)
def test_hypothesis_phone_normalization_idempotent(raw_phone: str, country: str):
    """
    INVARIANT: Normalizing an already normalized phone number MUST be idempotent.
    normalize_phone(normalize_phone(p, c), c) == normalize_phone(p, c)
    """
    first_pass = normalize_phone(raw_phone, country)
    second_pass = normalize_phone(first_pass, country)
    assert first_pass == second_pass, (
        f"Idempotency violated: normalize_phone('{raw_phone}', '{country}') -> "
        f"'{first_pass}' -> '{second_pass}'"
    )


# ---------------------------------------------------------------------------
# Property 2: Canonical E.164 Country Calling Code Invariant
# ---------------------------------------------------------------------------

@given(digits=st.from_regex(r"[0-9]{7,10}", fullmatch=True), country=country_strat)
@settings(max_examples=300)
def test_hypothesis_phone_has_country_prefix_for_valid_lengths(digits: str, country: str):
    """
    INVARIANT: Any subscriber number with >= 7 digits normalized under country C
    MUST begin with '+' followed by the calling code of C.
    """
    normalized = normalize_phone(digits, country)
    expected_cc = "+961" if country == "LB" else "+966"
    assert normalized.startswith(expected_cc), (
        f"Prefix violated for country '{country}': '{digits}' normalized to '{normalized}'"
    )


# ---------------------------------------------------------------------------
# Property 3: Dedup Size Monotonicity (Non-Expansion Invariant)
# ---------------------------------------------------------------------------

@given(records=st.lists(business_record_strat(), min_size=0, max_size=30))
@settings(max_examples=250)
def test_hypothesis_dedup_never_expands_record_count(records: list[dict]):
    """
    INVARIANT: Deduplication must never increase the total record count.
    len(dedup(records)) <= len(records)
    """
    deduped = dedup(records)
    assert len(deduped) <= len(records), (
        f"Dedup expanded records! Input size: {len(records)}, output size: {len(deduped)}"
    )


# ---------------------------------------------------------------------------
# Property 4: Dedup Permutation Independence (Order Commutativity)
# ---------------------------------------------------------------------------

@given(records=st.lists(business_record_strat(), min_size=2, max_size=15))
@settings(max_examples=150)
def test_hypothesis_dedup_permutation_invariance(records: list[dict]):
    """
    INVARIANT: Shuffling the input order of records must not alter the total count
    of unique deduplicated entities resolved by the pipeline.
    """
    shuffled_records = copy.deepcopy(records)
    random.shuffle(shuffled_records)

    out1 = dedup(records)
    out2 = dedup(shuffled_records)

    assert len(out1) == len(out2), (
        f"Dedup result count is order-dependent! Original count: {len(out1)}, "
        f"shuffled count: {len(out2)}"
    )


# ---------------------------------------------------------------------------
# Property 5: Source Conservation Under Merge
# ---------------------------------------------------------------------------

@given(record_a=business_record_strat(), record_b=business_record_strat())
@settings(max_examples=200)
def test_hypothesis_merge_source_provenance_conserved(record_a: dict, record_b: dict):
    """
    INVARIANT: When two records are merged, the merged record's 'source' field
    must contain the source of both record_a and record_b. No provenance is lost.
    """
    merged = _merge(record_a, record_b)
    sources_a = set(record_a["source"].split("|"))
    sources_b = set(record_b["source"].split("|"))
    expected_sources = sources_a | sources_b
    actual_sources = set(merged["source"].split("|"))

    assert expected_sources.issubset(actual_sources), (
        f"Source provenance lost during merge! Expected {expected_sources}, got {actual_sources}"
    )


# ---------------------------------------------------------------------------
# Property 6: Deduplication Phone Uniqueness Guarantee
# ---------------------------------------------------------------------------

@given(records=st.lists(business_record_strat(), min_size=1, max_size=30))
@settings(max_examples=200)
def test_hypothesis_dedup_emits_unique_phones(records: list[dict]):
    """
    INVARIANT: In the output of dedup(), no two records may share the exact same
    normalized phone number.
    """
    deduped = dedup(records)
    seen_phones = set()
    for r in deduped:
        raw_phone = r.get("phone")
        if raw_phone:
            norm = normalize_phone(raw_phone, r.get("country", "LB"))
            assert norm not in seen_phones, (
                f"Duplicate normalized phone emitted by dedup: '{norm}' in record '{r.get('name')}'"
            )
            seen_phones.add(norm)
```

---

## Test Harness Infrastructure & Configuration

To execute this test suite cleanly without environment side-effects, the following configuration structure must be established.

### 1. Proposed Directory Layout
```
leadminer/
├── tests/
│   ├── __init__.py
│   ├── conftest.py               # Shared fixtures, responses mock harness, sample payloads
│   ├── test_properties.py        # Hypothesis property-based tests
│   ├── test_base.py              # Schema and BaseScraper tests
│   ├── test_whitelist.py         # Category filtering and priority tests
│   ├── test_osm.py               # OSM Overpass parser & retry tests
│   ├── test_wikidata.py          # Wikidata SPARQL parser tests
│   ├── test_google_places.py     # Places v1 API, pagination, seen_ids tests
│   ├── test_dedup.py             # Phone normalization and dedup merge tests
│   ├── test_enricher.py          # Website liveness, regex extraction, lead score tests
│   ├── test_pitch_recommender.py # Pitch recommendation decision-table tests
│   └── test_main.py              # Pipeline orchestration and CSV persistence tests
├── pytest.ini                    # Pytest configuration, markers, and warning filters
└── requirements-dev.txt          # Test dependencies
```

### 2. `pytest.ini` Configuration
```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_functions = test_*
addopts = 
    -v
    --strict-markers
    --tb=short
    --durations=10
markers =
    unit: Isolated fast unit tests with zero I/O or pure mocked I/O.
    integration: Pipeline integration tests reading mock CSVs or cassettes.
    hypothesis: Generative property-based tests.
    network: Live network tests (disabled by default in CI).
filterwarnings =
    error
    ignore::urllib3.exceptions.InsecureRequestWarning
```

### 3. Core Test Fixtures (`tests/conftest.py`)
```python
"""
tests/conftest.py - Pytest fixtures and mock factories.
"""

import pathlib
import pytest
import responses
from typing import Generator


@pytest.fixture
def mocked_responses() -> Generator[responses.RequestsMock, None, None]:
    """Activates responses mocking for all HTTP requests made via `requests`."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


@pytest.fixture
def mock_csv_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Provides an isolated temporary data/ directory for CSV read/write tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


@pytest.fixture
def sample_business_record() -> dict:
    """Canonical test record matching BusinessRecord TypedDict schema."""
    return {
        "name": "Cedar Bistro",
        "category": "restaurant",
        "address": "Hamra Main Street, Beirut",
        "region": "Beirut",
        "country": "LB",
        "lat": 33.895,
        "lon": 35.481,
        "phone": "+9611340123",
        "email": "contact@cedarbistro.com",
        "website": "https://cedarbistro.com",
        "website_live": True,
        "facebook": "cedarbistro",
        "instagram": "cedarbistro",
        "whatsapp": "+96170123456",
        "linkedin": None,
        "rating": 4.5,
        "review_count": 85,
        "industry_priority": "high",
        "recommended_service": "RTYLR commerce OS (POS, online ordering, CRM)",
        "lead_score": 75,
        "source": "osm",
        "scraped_at": "2026-10-03T12:00:00Z",
        "completeness_score": 6,
    }
```

### 4. Continuous Integration Execution Plan
The test suite should be integrated into GitHub Actions as a mandatory pull-request gate:
```yaml
# Add to .github/workflows/test.yml
name: Test Suite
on: [push, pull_request]
jobs:
  pytest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python 3.12
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install pytest pytest-cov responses hypothesis vcrpy
      - name: Run Pytest Suite with Coverage
        run: |
          pytest --cov=. --cov-report=term-missing --cov-report=xml
```

---

## Recommended Order of Work

1. **Setup Test Harness Infrastructure:** Create `tests/conftest.py`, `pytest.ini`, and add `pytest`, `responses`, and `hypothesis` to development dependencies.
2. **Implement S1 Scraper & Enricher Crash Regression Tests (Tests #1–#4):** Pin down the naked `future.result()` in `enricher.py` and `GooglePlacesScraper`, as well as unprotected `resp.json()` calls in `osm.py` and `wikidata.py`.
3. **Implement Hypothesis Property Tests (Properties #1–#6):** Establish invariants for `normalize_phone()` and `dedup()` to safeguard data integrity before refactoring deduplication heuristics.
4. **Implement S1 Type Safety Regression Tests (Tests #7–#12):** Guard against `AttributeError` and `TypeError` when `category`, `phone`, `name`, or `source` are integers, floats, or `None`.
5. **Implement Persistence & Roundtrip Tests (Tests #13–#15):** Verify atomic writes and CSV deserialization fidelity in `main.py`.
6. **Implement Business Logic Unit Tests (Tests #16–#30):** Lock in pitch recommendation priority chains, lead scoring weights, whitelist filtering, and coordinate bounding box heuristics.
