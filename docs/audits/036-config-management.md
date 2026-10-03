# 036 — Layered configuration management

## Verdict

Runtime policy is spread across module constants and function defaults, so changing markets, geography, lead scoring, category coverage, or concurrency currently requires a source edit. Add a single typed `pydantic-settings` model and resolve values in the requested order: code defaults → optional TOML file → environment → CLI. The defaults below should initially preserve the existing literals exactly; configuration should make them adjustable, not silently change the current product behavior.

## Findings

### S2 — Market coverage and query volume are compiled into source
- **Where:** `scrapers/google_places.py:45-124`; `scrapers/osm.py:10-26`; `scrapers/wikidata.py:10-26`
- **Breaks:** Operators cannot add/remove a search query, adjust query volume, or revise the country/area queried without editing and deploying Python. This makes market experiments and quota/cost controls unnecessarily risky. OSM and Wikidata are also Lebanon-only in their query definitions; do not imply that changing Google query lists alone enables those sources in KSA.
- **Trigger:** A run needs to pause an expensive Google query or add a city/industry. The only available control is changing the code. OSM's `OVERPASS_QUERY` and Wikidata's `SPARQL_QUERY`/`LIMIT 5000` are also embedded literals, so source coverage and the result cap are not operator-configurable.
- **Fix:** Add `google_places.lebanon_queries` and `google_places.ksa_queries` as `list[str]`, plus explicit `osm.overpass_query` and `wikidata.sparql_query` (or structured query parameters) defaults. Include the Wikidata limit as a separate integer setting if it is expected to be tuned. Preserve the present full query text/list as each field's default.

### S2 — Region inference policy is not configurable
- **Where:** `enricher.py:10-76`
- **Breaks:** Coordinate boundaries and address aliases determine region labels on records. Boundary/alias fixes or additional local spellings currently need code edits; geographic coverage also differs between Lebanon (keyword and box matching) and KSA (boxes only).
- **Trigger:** A new locality spelling is encountered, or a boundary needs a small correction to stop assigning a business to an adjacent region. There is no data-only override.
- **Fix:** Add typed `regions.lb_coordinate_boxes`, `regions.sa_coordinate_boxes`, and `regions.keywords` settings. Keep list order intact because inference returns the first matching box/keyword region.

### S2 — Scoring and category policy are fixed in code
- **Where:** `enricher.py:101-117,227-259`; `scrapers/whitelist.py:17-102,123-128`
- **Breaks:** Lead-score calibration and category inclusion affect qualification and sales ordering, but updates require changing implementation code. Whitelist changes can also alter which scraped records ever reach dedup/enrichment.
- **Trigger:** The sales team wants to give dead websites more points, or a newly relevant category should be retained. Both changes currently require editing Python and redeploying.
- **Fix:** Put score weights/thresholds and all category policy collections in validated settings. Keep the current rules and values as defaults. Reject or warn on configurations with invalid bounds, duplicate/overlapping category sets, or a score maximum inconsistent with the configured cap.

### S2 — Concurrency is partly hardcoded and partly implicit
- **Where:** `scrapers/google_places.py:138-142,183`; `enricher.py:180,188`; `main.py:100-106`
- **Breaks:** Request concurrency affects Google rate limits and website-enrichment load. The Google pool is fixed at five; website checks default to forty; scraper orchestration implicitly uses one worker per scraper (currently three). Operators cannot tune those separately from runtime configuration.
- **Trigger:** A constrained runner or API throttling requires lowering concurrency, or a larger runner should raise it. Changing `_WORKERS` or the function default requires a source change; orchestration has no named setting at all.
- **Fix:** Expose three independent positive integers—Google query workers (default 5), website workers (default 40), and orchestration workers (effective current default 3)—and pass the settings explicitly to each executor. Validate each as `>= 1`; do not couple Google and website worker counts.

### S3 — There is no settings/CLI layer or dependency yet
- **Where:** `requirements.txt:1-3`; `main.py:14-16,93-100,183-184`
- **Breaks:** The documented entry point takes no options, and `pydantic-settings` is not installed. A config model alone would not help unless the entry point loads it and passes it to consumers.
- **Trigger:** An operator wants a one-off override for a run without committing a config-file change; `python main.py` currently has no CLI parser.
- **Fix:** Add and pin `pydantic-settings`, initialize settings once at the entry point, and inject/pass the immutable settings object to scrapers, region inference, filtering, scoring, and enrichment. Keep secrets such as `GOOGLE_PLACES_API_KEY` environment-only.

## Configuration inventory — exact current defaults

Every collection default below means the **entire existing literal, unchanged**, at the cited source lines (including spelling, capitalization, order, and membership). This avoids introducing a second hand-maintained copy while making the complete existing behavior the code fallback. Use JSON arrays in environment variables/CLI for list-valued overrides.

| Suggested setting | Exact default to preserve | Source |
|---|---|---|
| `google_places.lebanon_queries: list[str]` | The complete 33-string `LEBANON_QUERIES` list, verbatim | `scrapers/google_places.py:45-79` |
| `google_places.ksa_queries: list[str]` | The complete 35-string `KSA_QUERIES` list, verbatim | `scrapers/google_places.py:82-124` |
| `osm.overpass_query: str` | The complete current multiline query, verbatim | `scrapers/osm.py:10-26` |
| `wikidata.sparql_query: str` | The complete current multiline query, including `LIMIT 5000`, verbatim | `scrapers/wikidata.py:10-26` |
| `regions.keywords: list[RegionKeywords]` | Every region and keyword, in current order, verbatim: Beirut, Mount Lebanon, North Lebanon, Akkar, South Lebanon, Nabatieh, Bekaa, Baalbek-Hermel | `enricher.py:10-50` |
| `regions.lb_coordinate_boxes: list[RegionBox]` | `(region, lat_min, lat_max, lon_min, lon_max)`: Beirut `(33.845,33.920,35.462,35.545)`; Akkar `(34.380,34.720,35.980,36.650)`; Baalbek-Hermel `(34.000,34.720,36.100,36.850)`; Bekaa `(33.380,34.200,35.750,36.650)`; South Lebanon `(33.040,33.580,35.090,35.750)`; Nabatieh `(33.240,33.560,35.330,35.720)`; North Lebanon `(34.100,34.680,35.490,36.300)`; Mount Lebanon `(33.540,34.120,35.370,35.950)` | `enricher.py:58-68` |
| `regions.sa_coordinate_boxes: list[RegionBox]` | Riyadh `(24.40,25.20,46.40,47.20)`; Jeddah `(21.30,21.80,39.05,39.45)`; Dammam `(26.20,26.65,49.85,50.30)`; Mecca `(21.30,21.55,39.75,40.00)`; Medina `(24.30,24.65,39.45,39.80)` | `enricher.py:70-76` |
| `scoring.completeness_weights` | `phone=1, email=1, website=1, address=1, social=(facebook OR instagram)=1, whatsapp=1, linkedin=1` | `enricher.py:101-117` |
| `scoring.lead_weights` | `email=20, whatsapp=15, phone=15, instagram=10, live_website=10, dead_website=20, high_priority=15, medium_priority=8, rating_below_threshold=10, multi_source=5` | `enricher.py:227-259` |
| `scoring.phone_min_digits` / `scoring.low_rating_threshold` / `scoring.max_score` | `7` / `4.0` (the existing condition is strictly `< 4.0`) / `100` | `enricher.py:229,236,253-254,259` |
| `whitelist.priority_industries: set[str]` | The complete `PRIORITY_INDUSTRIES` set, verbatim | `scrapers/whitelist.py:16-42` |
| `whitelist.adjacent_businesses: set[str]` | The complete `ADJACENT_BUSINESSES` set, verbatim | `scrapers/whitelist.py:44-82` |
| `whitelist.geographic_noise: set[str]` | The complete `GEOGRAPHIC_NOISE` set, verbatim | `scrapers/whitelist.py:84-102` |
| `whitelist.business_keywords: list[str]` | `shop, store, agency, firm, company, service, studio, office, salon, centre, center, boutique` | `scrapers/whitelist.py:122-128` |
| `google_places.workers: int` | `5` | `scrapers/google_places.py:138-142,183` |
| `enrichment.website_workers: int` | `40` | `enricher.py:180,188,277` |
| `scraping.orchestration_workers: int` | `3` effective workers (`len([OSMScraper(), WikidataScraper(), GooglePlacesScraper()])`) | `main.py:100-106` |

The score-related thresholds that are not additive weights are included because they change the score result just as directly as a weight. Likewise, `business_keywords` is part of the effective allow policy even though it is a tuple rather than a named set. `main.py:144-146` also hardcodes the downstream sales-ready priority labels and completeness cutoff (`>= 1`); expose these only if operators are expected to tune output segmentation, not as substitutes for the requested whitelist/scoring settings.

## Suggested settings shape and precedence

Use one `BaseSettings` model with nested sections and constrained types (positive worker counts, finite numeric weights, valid ordered coordinate bounds). Suggested sections/fields are those in the inventory. Model region boxes as ordered records with `name`, `lat_min`, `lat_max`, `lon_min`, and `lon_max`; model keyword regions as ordered records with `name` and `keywords: list[str]`. Keep the collections mutable only during parsing, then treat the resolved settings as read-only for a run.

Use a non-secret `config.toml` as the optional file layer, `LEADMINER_` environment variables with a nested delimiter such as `__`, and pydantic-settings CLI parsing. Explicitly configure source precedence **CLI > environment > TOML file > field defaults** (highest-priority source first in pydantic-settings source customization). For example, `LEADMINER_GOOGLE_PLACES__WORKERS=3` overrides the TOML worker value, and `--google-places.workers 2` overrides both for one run. Use JSON-encoded arrays/objects for structured environment and CLI overrides. Resolve settings once in `main()` and pass them explicitly; avoid module-import-time settings construction so library imports remain predictable.

## Recommended order of work

1. Add the pinned `pydantic-settings` dependency and typed config model, with defaults sourced from the existing constants and no behavior changes.
2. Thread settings through orchestration and scraper/enricher/whitelist/scoring call sites; remove module-level duplicate policy only after all consumers use the model.
3. Add TOML loading, environment nesting, and CLI parsing in the stated precedence; keep API credentials out of TOML.
4. Add validation for malformed boxes/empty query lists/worker counts and a startup summary of effective non-secret settings. Verify source precedence with small settings-only tests before enabling overrides operationally.
