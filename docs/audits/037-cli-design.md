# 037 — CLI Design: Modular Subcommand Architecture for Pipeline Decoupling

## Verdict

`leadminer` currently relies on an unparameterized, monolithic `python main.py` script (`main.py:93-184`) that executes scraping, filtering, deduplication, HTTP enrichment, scoring, and multi-file CSV export in a single uninterrupted in-memory pass. A failure in downstream HTTP enrichment (or an interrupted run) destroys all upstream scraped data, while developers and CI operators have no way to isolate sources, target individual geographic regions, validate existing CSVs, re-score leads, or verify API keys without executing the entire multi-hour pipeline. Replacing `main.py` with a zero-dependency, UNIX-compliant `argparse` CLI providing eight purpose-built subcommands (`doctor`, `scrape`, `dedup`, `enrich`, `score`, `validate`, `export`, `stats`) and a composite `run` orchestrator eliminates intermediate data loss, cuts API costs during development, and provides deterministic exit codes for automated CI/CD pipelines.

---

## Findings

### S1 — Monolithic execution traps intermediate state in volatile memory, discarding expensive scrapes on downstream failures
- **Where:** `main.py:99-130`
- **Breaks:** Scraped records from Overpass, Wikidata, and Google Places are accumulated into a Python list in heap memory (`raw: list[dict] = []`, `main.py:99`). Deduplication and enrichment occur sequentially in memory (`combined = raw_filtered + master`, `records = dedup(combined)`, `records = enrich(records)` at lines 124–129). If `enricher.py` encounters an unhandled exception across its 40 concurrent HTTP threads (`enricher.py:193`), or if the runner times out or crashes before `main.py:149`, **all scraped records and consumed Google Places API credits are completely lost**. No checkpoint is saved to disk between scraping and enrichment.
- **Trigger:** Any uncaught HTTP socket timeout, DNS failure, thread safety crash, or SIGINT during `enrich()` after 45 minutes of scraping Google Places and Overpass.
- **Fix:** Decouple pipeline phases into discrete CLI subcommands with distinct on-disk intermediate stages (e.g., `leadminer scrape -o data/raw.csv` followed by `leadminer dedup` and `leadminer enrich`).

### S1 — Silent failure on missing credentials and unhandled network errors exits 0 in CI
- **Where:** `scrapers/google_places.py:158-160`; `main.py:107-114`; `.github/workflows/scrape.yml:31`
- **Breaks:** If `GOOGLE_PLACES_API_KEY` is missing or invalid, `GooglePlacesScraper.scrape()` prints `"[Google] GOOGLE_PLACES_API_KEY not set, skipping."` (`scrapers/google_places.py:159`) and returns an empty generator. `main.py` catches worker exceptions (`main.py:112-113`), prints to stderr, but proceeds to execute the rest of the pipeline and exits with status code `0`. In GitHub Actions (`.github/workflows/scrape.yml:31`), CI reports a green checkmark despite zero Google Places leads being retrieved. Furthermore, there is no diagnostic preflight command to test connectivity or credential validity before scheduling hours of compute.
- **Trigger:** Running `python main.py` in CI or locally when `GOOGLE_PLACES_API_KEY` is unset or revoked.
- **Fix:** Introduce `leadminer doctor` and make `scrape` fail with explicit, non-zero exit codes (e.g., exit code `3` for configuration/credential errors) unless an explicit `--allow-partial` flag is passed.

### S2 — Hardcoded geographic queries and sources prevent targeted extraction and incremental refreshes
- **Where:** `scrapers/osm.py:10-26`; `scrapers/wikidata.py:10-26`; `scrapers/google_places.py:45-124`; `main.py:100`
- **Breaks:** Sources and geographies are rigidly hardcoded. `OSMScraper` queries only Lebanon (`ISO3166-1="LB"` at `osm.py:12`). `WikidataScraper` queries only Lebanon (`wdt:P17 wd:Q822` at `wikidata.py:12`). `GooglePlacesScraper` couples 33 Lebanon queries with 34 KSA queries (`google_places.py:45-124`) in a single list. Operators cannot scrape only Saudi Arabia, cannot scrape only Beirut or Riyadh, and cannot run OSM without also triggering Google Places API calls.
- **Trigger:** An operator wants to refresh KSA real estate leads or test a new category in Riyadh without paying for or waiting on Lebanon Overpass queries.
- **Fix:** Add `--source` (`osm`, `wikidata`, `google_places`) and `--region` / `--country` (`LB`, `SA`, `all`, or regional bounding boxes) filters to `leadminer scrape`.

### S2 — Pitch recommendation and lead scoring cannot be recalculated without re-running network enrichment
- **Where:** `main.py:128-138`; `pitch_recommender.py:25-97`; `enricher.py:227-260`
- **Breaks:** Scoring and service recommendations are locked inside `main.py` directly following `enrich(records)`. If sales leadership updates the pitch logic (e.g. tuning `_RTYLR_TARGETS` in `pitch_recommender.py:109` or updating weights in `lead_score`), the entire dataset must be re-scraped and re-probed over HTTP (`main.py:128`).
- **Trigger:** Updating pitch rules in `pitch_recommender.py` and needing to re-tag `all_businesses.csv`.
- **Fix:** Provide a standalone `leadminer score` subcommand that operates deterministically on existing CSV files without making any network requests.

### S3 — Hardcoded paths, lack of structured stdout, and missing telemetry prevent pipeline composability
- **Where:** `main.py:40, 148-154, 158-181`
- **Breaks:** `DATA_DIR = pathlib.Path("data")` is hardcoded. All logs and summaries are printed directly to stdout via ad-hoc `print()` calls (`main.py:158-181`), mixing diagnostic messages with summary statistics. Output cannot be piped into `jq`, exported to JSON for dashboards, or redirected without capturing log noise.
- **Trigger:** Attempting to run `leadminer` in a cron script that expects JSON telemetry or custom destination directories.
- **Fix:** Standardize logs to stderr, reserve stdout for machine-readable data streams, support `--data-dir` and `--format` (json/text/csv), and implement `leadminer stats` and `leadminer export`.

---

## Architecture & Framework Selection

### 1. Framework: Standard Library `argparse`
To adhere strictly to the project constraints (Python 3.12, zero unnecessary third-party dependencies, minimal packaging friction), the CLI must be built entirely with Python's built-in `argparse` module using subparsers.

**Why `argparse` over `click` or `typer`:**
- **Zero dependencies:** Does not require modifying `requirements.txt` or introducing wheel build steps in restricted environments.
- **Native Python 3.12 features:** Subparser dest handling, custom help formatting, strongly-typed argument converters, and `required=True` subparsers are fully supported out of the box.
- **Instant startup:** Sub-millisecond execution overhead for CLI invocation (critical for pre-commit hooks and quick health checks).

### 2. Entry Point Structure
The CLI should be structured as a modular package under `leadminer/`:
```
leadminer/
├── __init__.py
├── __main__.py          # Invoked via `python -m leadminer`
├── cli.py               # Root parser, global flags, and dispatcher
├── commands/
│   ├── __init__.py
│   ├── doctor.py        # `leadminer doctor`
│   ├── scrape.py        # `leadminer scrape`
│   ├── dedup.py         # `leadminer dedup`
│   ├── enrich.py        # `leadminer enrich`
│   ├── score.py         # `leadminer score`
│   ├── validate.py      # `leadminer validate`
│   ├── export.py        # `leadminer export`
│   ├── stats.py         # `leadminer stats`
│   └── run.py           # `leadminer run` (orchestrator)
```
For backward compatibility during the migration phase, the root `main.py` is preserved as a lightweight wrapper delegating directly to `leadminer.cli.main()`.

---

## Global CLI Specification

### Invocation Syntax
```bash
leadminer [GLOBAL_FLAGS] <subcommand> [SUBCOMMAND_FLAGS] [ARGS...]
python -m leadminer [GLOBAL_FLAGS] <subcommand> [SUBCOMMAND_FLAGS] [ARGS...]
```

### Global Flags
These flags apply to all subcommands and must be processed before dispatching to the subparser:

| Flag | Short | Type | Default | Description |
|---|---|---|---|---|
| `--data-dir` | `-d` | `PATH` | `./data` | Base directory for reading/writing input and output artifacts. |
| `--verbose` | `-v` | `COUNT` | `0` | Increase log verbosity (`-v` = INFO, `-vv` = DEBUG). Sent to `stderr`. |
| `--quiet` | `-q` | `FLAG` | `False` | Suppress non-essential informational messages; output errors only. |
| `--log-file` | | `PATH` | `None` | Optional path to write structured execution logs. |
| `--format` | | `CHOICE` | `text` | Global formatting style for command summaries: `text`, `json`. |
| `--version` | `-V` | `FLAG` | | Display application version and exit `0`. |
| `--help` | `-h` | `FLAG` | | Display root or subcommand usage information and exit `0`. |

---

## Subcommand Specifications

```
                              ┌──────────────────────────────────────────────┐
                              │              leadminer doctor                │
                              │ (Validate environment, keys & upstream APIs) │
                              └──────────────────────┬───────────────────────┘
                                                     │ exit 0
                                                     ▼
                              ┌──────────────────────────────────────────────┐
                              │              leadminer scrape                │
                              │   --source osm,wikidata,google_places        │
                              │   --region LB,SA,Beirut,Riyadh...            │
                              └──────────────────────┬───────────────────────┘
                                                     │ raw records (CSV/JSONL)
                                                     ▼
                              ┌──────────────────────────────────────────────┐
                              │              leadminer dedup                 │
                              │ (Normalize phones, merge with master state)  │
                              └──────────────────────┬───────────────────────┘
                                                     │ unique entities
                                                     ▼
                              ┌──────────────────────────────────────────────┐
                              │              leadminer enrich                │
                              │ (Infer regions, test liveness, find socials) │
                              └──────────────────────┬───────────────────────┘
                                                     │ enriched entities
                                                     ▼
                              ┌──────────────────────────────────────────────┐
                              │              leadminer score                 │
                              │ (Calculate completeness, lead_score & pitch) │
                              └──────────────────────┬───────────────────────┘
                                                     │ scored records
                                                     ▼
                              ┌──────────────────────────────────────────────┐
                              │             leadminer validate               │
                              │ (Check schema, coordinate bounds & contracts)│
                              └──────────────────────┬───────────────────────┘
                                                     │ valid records
                                      ┌──────────────┴──────────────┐
                                      ▼                             ▼
                        ┌───────────────────────────┐ ┌───────────────────────────┐
                        │     leadminer export      │ │      leadminer stats      │
                        │ (Slice into sales_ready,  │ │ (Tabulate counts, contact │
                        │  qualified, websites CSVs)│ │  ratios & pitch breakdowns│
                        └───────────────────────────┘ └───────────────────────────┘
```

---

### 1. `leadminer doctor`
Performs comprehensive pre-flight verification of runtime environment, credentials, filesystem permissions, and network connectivity to upstream APIs. Prevents starting multi-hour pipeline runs when keys are missing or endpoints are down.

#### Syntax
```bash
leadminer doctor [FLAGS]
```

#### Flags
| Flag | Short | Type | Default | Description |
|---|---|---|---|---|
| `--check-network` | | `BOOL` | `True` | Probe external API endpoints (`--no-check-network` to skip). |
| `--strict` | | `FLAG` | `False` | Exit with code `3` if any optional check (e.g. `SCRAPER_EMAIL`) fails. |
| `--format` | `-f` | `CHOICE` | `text` | Output format: `text` (human-readable checklist) or `json`. |

#### Checks Performed
1. **Python Environment:** Asserts Python `>= 3.12`.
2. **Filesystem Permissions:** Validates that `--data-dir` exists (or can be created) and has read/write permissions.
3. **Environment Variables:**
   - `GOOGLE_PLACES_API_KEY`: Verifies presence and string structure.
   - `SCRAPER_EMAIL`: Verifies presence (used in User-Agent for OSM and Wikidata; defaults with warning if unset).
4. **Upstream Connectivity & Authentication:**
   - **Overpass API:** Sends HTTP `GET https://overpass-api.de/api/status` (asserts HTTP 200 and available query slots).
   - **Wikidata SPARQL:** Sends lightweight test query `ASK { ?item wdt:P17 wd:Q822 }` to `https://query.wikidata.org/sparql` (asserts HTTP 200).
   - **Google Places API:** Sends an authenticated test query with `pageSize: 1` to `https://places.googleapis.com/v1/places:searchText` (asserts HTTP 200; catches 400 invalid field mask or 401 unauthorized).
5. **Local Master Integrity:** Checks `data/all_businesses.csv` (if present) for header schema validity and UTF-8 encoding.

#### Exit Codes
- `0`: All checks passed.
- `3`: Missing or invalid API key or environment variable.
- `4`: Network error or upstream API endpoint down.

---

### 2. `leadminer scrape`
Executes data collection across one or more scrapers, filtered by geographic country/region and business category. Emits raw records directly to disk.

#### Syntax
```bash
leadminer scrape [FLAGS]
```

#### Flags
| Flag | Short | Type | Default | Description |
|---|---|---|---|---|
| `--source` | `-s` | `CHOICE` | `all` | Target scraper(s): `osm`, `wikidata`, `google_places`, `all` (repeatable or comma-separated). |
| `--region` | `-r` | `STR` | `all` | Target region/city: `LB`, `SA`, `Beirut`, `Mount Lebanon`, `Riyadh`, `Jeddah`, `Dammam`, `all`. |
| `--output` | `-o` | `PATH` | `data/raw_scraped.csv` | Output file for scraped records. Use `-` for stdout. |
| `--category` | `-c` | `STR` | `None` | Filter queries or results by specific industry vertical (e.g. `restaurant`, `clinic`). |
| `--limit` | `-n` | `INT` | `None` | Maximum records to fetch per scraper (useful for smoke testing). |
| `--workers` | `-w` | `INT` | `5` | Concurrency worker count for multi-threaded scrapers (`google_places`). |
| `--no-filter` | | `FLAG` | `False` | Skip initial `whitelist.py` category filtering; keep all raw hits. |
| `--allow-partial`| | `FLAG` | `False` | Exit `0` even if one source encounters an error, provided >= 1 source succeeded. |

#### Behavior & Output
- Runs scrapers in parallel or sequence based on `--workers`.
- Automatically maps `--region` to corresponding query sets:
  - If `--region LB`: Runs OSM (Lebanon), Wikidata (Lebanon), and Google Places `LEBANON_QUERIES`.
  - If `--region SA`: Skips OSM and Wikidata (currently LB-only); runs Google Places `KSA_QUERIES`.
  - If `--region Riyadh`: Runs Google Places queries matching `"Riyadh"`.
- Appends or writes records with the canonical 22 fields initialized (`source`, `scraped_at` set).

#### Exit Codes
- `0`: Scraping completed successfully for all requested sources.
- `1`: General scraper execution crash.
- `2`: Invalid argument combination (e.g. `--source osm --region Riyadh`).
- `3`: Missing required credentials (`GOOGLE_PLACES_API_KEY` required when `--source google_places`).
- `4`: Network failure / upstream API rate limit exhausted.
- `6`: Partial failure (one source failed, but records were extracted from another and `--allow-partial` was specified).

---

### 3. `leadminer dedup`
Merges multiple raw scrape batches with each other and/or an existing cumulative master file, applying telephone normalization and deduplication logic.

#### Syntax
```bash
leadminer dedup [FLAGS] [INPUT_FILES...]
```

#### Flags
| Flag | Short | Type | Default | Description |
|---|---|---|---|---|
| `INPUT_FILES` | | `PATH...` | `[data/raw_scraped.csv]` | One or more input CSV files containing raw scraped businesses. |
| `--master` | `-m` | `PATH` | `data/all_businesses.csv`| Existing cumulative master CSV to merge with. If absent, treats as cold-start. |
| `--output` | `-o` | `PATH` | `data/deduped.csv` | Output destination for deduplicated records. |
| `--default-country`| | `CHOICE`| `LB` | Fallback country code for phone normalization: `LB`, `SA`. |
| `--strategy` | | `CHOICE` | `phone-first` | Deduplication strategy: `phone-first` (current standard) or `strict`. |
| `--atomic` | | `BOOL` | `True` | Write output to a `.tmp` file and perform atomic replacement (`--no-atomic` to disable). |

#### Behavior & Output
- Normalizes phone numbers with country-specific rules (`dedup.py:11-26`).
- Resolves conflicts between master records and fresh scrapes using field completeness and `scraped_at` timestamp comparison (`dedup.py:45-61`).
- Preserves combined provenance in `source` column (e.g., `osm|google_places`).
- Prints merge telemetry (raw count, master count, duplicate count, merged total) to `stderr`.

#### Exit Codes
- `0`: Deduplication successful.
- `1`: File I/O error or malformed input record.
- `2`: Missing input files.

---

### 4. `leadminer enrich`
Performs external data enrichment on deduplicated records, including coordinate-based region inference and concurrent HTTP probes to check website liveness and extract contact channels.

#### Syntax
```bash
leadminer enrich [FLAGS]
```

#### Flags
| Flag | Short | Type | Default | Description |
|---|---|---|---|---|
| `--input` | `-i` | `PATH` | `data/deduped.csv` | Input CSV of records requiring enrichment. |
| `--output` | `-o` | `PATH` | `data/enriched.csv`| Output destination for enriched records. |
| `--workers` | `-w` | `INT` | `40` | Concurrent worker threads for HTTP website fetching (`enricher.py:180`). |
| `--timeout` | `-t` | `FLOAT` | `8.0` | Socket timeout in seconds per website HTTP GET. |
| `--max-bytes` | | `INT` | `200000` | Maximum HTML response bytes scanned for contact patterns (`enricher.py:150`). |
| `--skip-http` | | `FLAG` | `False` | Run offline enrichment only (region inference from coordinates/address); skip web fetching. |
| `--force` | | `FLAG` | `False` | Re-fetch websites even if `website_live` is already populated in the input. |
| `--user-agent` | | `STR` | `Mozilla/5.0...` | Custom User-Agent header for website requests. |

#### Behavior & Output
- Always executes coordinate and address-based region tagging (`enricher.py:79-94`).
- Unless `--skip-http` is set, schedules HTTP GET requests across `--workers` threads with isolated `requests.Session` instances to prevent thread-safety races.
- Extracts `email`, `instagram`, `whatsapp`, and `linkedin` via regex while filtering common false positives and blacklisted domains (`enricher.py:135-140`).
- Updates `website_live` boolean flag.

#### Exit Codes
- `0`: Enrichment completed.
- `1`: Runtime error or unrecoverable thread pool failure.
- `2`: Input file missing or unreadable.

---

### 5. `leadminer score`
Evaluates record completeness, assigns industry priority, generates recommended pitch hooks, and calculates numerical lead quality scores. Operates 100% offline.

#### Syntax
```bash
leadminer score [FLAGS]
```

#### Flags
| Flag | Short | Type | Default | Description |
|---|---|---|---|---|
| `--input` | `-i` | `PATH` | `data/enriched.csv` | Input CSV containing enriched business records. |
| `--output` | `-o` | `PATH` | `data/scored.csv` | Output destination for scored records. |
| `--min-lead-score`| | `INT` | `0` | Optional filter threshold: drop records with `lead_score < N`. |
| `--min-completeness`| | `INT`| `0` | Optional filter threshold: drop records with `completeness_score < N`. |

#### Behavior & Execution Order
Strictly resolves the order-of-operations defect identified in audit `012`:
1. Evaluates and assigns `completeness_score` (`enricher.py:101-117`).
2. Evaluates and assigns `industry_priority` from category (`scrapers/whitelist.py:133-145`).
3. Evaluates and assigns `recommended_service` based on contact channels, website status, rating, and category (`pitch_recommender.py:25-97`).
4. Evaluates and assigns `lead_score` (0–100), ensuring `industry_priority` is already present so priority weight (+15/+8) is correctly scored (`enricher.py:227-260`).

#### Exit Codes
- `0`: Scoring completed successfully.
- `1`: Scoring evaluation failure or malformed numeric fields.
- `2`: Missing input file.

---

### 6. `leadminer validate`
Enforces schema compliance, data type constraints, coordinate boundaries, and formatting rules across records. Serves as a pipeline quality gate before committing or uploading datasets.

#### Syntax
```bash
leadminer validate [FLAGS] [FILE]
```

#### Flags
| Flag | Short | Type | Default | Description |
|---|---|---|---|---|
| `FILE` | | `PATH` | `data/all_businesses.csv`| CSV file to validate. |
| `--schema` | | `CHOICE` | `canonical` | Schema specification to enforce (`canonical` = 22 fields). |
| `--check-geo` | | `FLAG` | `False` | Validate that coordinates fall within official LB or SA bounding boxes. |
| `--check-phones`| | `FLAG` | `False` | Verify phone numbers conform to E.164-compatible normalized format. |
| `--max-errors` | | `INT` | `0` | Maximum allowable validation errors before exiting with failure (`0` = zero tolerance). |
| `--quarantine` | | `PATH` | `None` | Optional path to write invalid/corrupted records instead of discarding them. |
| `--format` | `-f` | `CHOICE` | `text` | Error reporting format: `text` (human summary with row numbers) or `json`. |

#### Validation Rules Checked
1. **Schema Integrity:** Exact 22-column header presence (`main.py:32-39`).
2. **Type Validity:**
   - `lat`, `lon`, `rating` must be parseable as `float` or `None`.
   - `review_count`, `completeness_score`, `lead_score` must be parseable as `int` or `None`.
   - `website_live` must be boolean or `None`.
3. **Geo-bounds (when `--check-geo`):**
   - Lebanon: Lat `[33.0, 34.8]`, Lon `[35.0, 36.7]`.
   - Saudi Arabia: Lat `[16.0, 32.5]`, Lon `[34.5, 55.7]`.
4. **Mandatory Identity:** Record must have non-empty `name` and non-empty `country`.

#### Exit Codes
- `0`: File is 100% valid.
- `2`: Missing target file.
- `5`: Validation failure (schema violation, invalid datatypes, or errors exceeding `--max-errors`).

---

### 7. `leadminer export`
Generates the standard partitioned product views from a scored or master dataset. Supports slicing by qualification, website presence, sales readiness, or geographic region.

#### Syntax
```bash
leadminer export [FLAGS]
```

#### Flags
| Flag | Short | Type | Default | Description |
|---|---|---|---|---|
| `--input` | `-i` | `PATH` | `data/all_businesses.csv`| Source CSV containing scored master records. |
| `--out-dir` | `-d` | `PATH` | `data/` | Target directory for exported files. |
| `--view` | | `CHOICE` | `all` | Specific view to export: `all`, `sales_ready`, `qualified`, `with_websites`, `without_websites`, `by_region`. |
| `--format` | `-f` | `CHOICE` | `csv` | Output file format: `csv`, `json`, `jsonl`. |
| `--atomic` | | `BOOL` | `True` | Use atomic writes via temporary files to avoid partial corruption. |

#### Product Views Exported (when `--view all`)
- `all_businesses.csv`: Complete cumulative master dataset (`main.py:149`).
- `qualified_businesses.csv`: Records where `completeness_score >= 1` (`main.py:150`).
- `with_websites.csv`: Records having a non-empty `website` (`main.py:151`).
- `without_websites.csv`: Records lacking a website (targets for web design pitch) (`main.py:152`).
- `sales_ready.csv`: Records meeting `has_any_contact()` AND `industry_priority in ("high", "medium")` (`main.py:153`).

#### Exit Codes
- `0`: Export completed.
- `1`: I/O or filesystem permission error.
- `2`: Missing input dataset.

---

### 8. `leadminer stats`
Calculates and displays distribution metrics, contact channel fill rates, category rankings, and sales pipeline readiness without altering any data.

#### Syntax
```bash
leadminer stats [FLAGS] [FILE]
```

#### Flags
| Flag | Short | Type | Default | Description |
|---|---|---|---|---|
| `FILE` | | `PATH` | `data/all_businesses.csv`| CSV file to analyze. |
| `--group-by` | `-g` | `CHOICE` | `region` | Primary breakdown grouping: `region`, `country`, `category`, `service`, `source`. |
| `--format` | `-f` | `CHOICE` | `text` | Presentation format: `text` (terminal table), `json` (for dashboards), `markdown`. |
| `--top` | `-n` | `INT` | `10` | Limit breakdown display to top N categories/services. |

#### Metrics Emitted
- Total unique businesses.
- Lead qualification breakdown (`completeness_score >= 1`).
- Website presence & liveness counts (live vs dead).
- Contact channel densities: phone, email, Instagram, WhatsApp, LinkedIn fill rates.
- Actionable Sales-Ready count.
- Frequency tables for `--group-by` dimension.

#### Exit Codes
- `0`: Statistics calculated and displayed successfully.
- `2`: File not found or unreadable.

---

### 9. `leadminer run` (Composite Pipeline Runner)
Orchestrates the entire sequence (`doctor` → `scrape` → `dedup` → `enrich` → `score` → `validate` → `export` → `stats`) with proper checkpointing, error recovery, and failure isolation. This is the direct drop-in replacement for `python main.py` in scheduled environments.

#### Syntax
```bash
leadminer run [FLAGS]
```

#### Flags
| Flag | Short | Type | Default | Description |
|---|---|---|---|---|
| `--source` | `-s` | `CHOICE` | `all` | Upstream scraper sources to execute (`osm,wikidata,google_places,all`). |
| `--region` | `-r` | `STR` | `all` | Geographic scope (`LB`, `SA`, `all`). |
| `--skip-doctor` | | `FLAG` | `False` | Skip pre-flight doctor checks. |
| `--skip-enrich` | | `FLAG` | `False` | Skip HTTP website liveness/contact checking. |
| `--skip-scrape` | | `FLAG` | `False` | Re-run dedup/enrich/score/export using existing raw or master data. |
| `--allow-partial`| | `FLAG` | `False` | Proceed through pipeline even if one non-critical scraper fails. |
| `--clean` | | `FLAG` | `False` | Wipe intermediate temporary files before starting. |

#### Behavior
1. Runs `doctor` pre-flight checks. Aborts immediately on exit code `3` or `4` before any network queries are initiated.
2. Checks for `data/all_businesses.csv`. If absent, warns and proceeds in cold-start mode.
3. Scrapes to `data/.tmp/raw_scraped.csv`. If scraping fails, preserves any existing master state.
4. Merges and deduplicates against `data/all_businesses.csv`, writing to `data/.tmp/deduped.csv`.
5. Enriches to `data/.tmp/enriched.csv`.
6. Scores to `data/.tmp/scored.csv`.
7. Validates `data/.tmp/scored.csv`. Fails fast if schema or data corruption is detected.
8. Atomically exports all 5 production CSVs into `data/`.
9. Prints run summary statistics to `stdout`.

#### Exit Codes
- `0`: Full pipeline run succeeded.
- `1`: Unhandled pipeline exception.
- `3`: Missing API credentials during preflight.
- `4`: Network failure on critical upstream source.
- `5`: Validation failure on final dataset.

---

## Exit Code Specification Matrix

A production CLI must provide unambiguous, standardized exit codes to allow orchestration platforms (GitHub Actions, Airflow, bash scripts) to react intelligently to different failure modes:

| Code | Constant | Meaning | Typical Trigger | Recommended Caller Action |
|---|---|---|---|---|
| `0` | `EX_OK` | Success | Normal execution completed. | Proceed to next stage / upload artifacts. |
| `1` | `EX_SOFTWARE` | Internal Error | Unhandled Python exception, logic bug. | Inspect traceback in stderr; alert engineers. |
| `2` | `EX_USAGE` | Usage Error | Bad CLI arguments, invalid choices, missing required flags. | Check command syntax; do not retry automatically. |
| `3` | `EX_CONFIG` | Config / Credential Error | `GOOGLE_PLACES_API_KEY` missing/invalid; bad env var. | Check GitHub Secrets / `.env` file; do not retry. |
| `4` | `EX_NOHOST` | Network / Upstream Failure | Overpass 504 gateway timeout; DNS failure; Google Places 429 quota block. | Retry with exponential backoff or alert ops. |
| `5` | `EX_DATAERR` | Data Validation Error | CSV schema mismatch; corrupted master; bounding box violation. | Inspect quarantine file; halt upload to prevent data corruption. |
| `6` | `EX_PARTIAL` | Partial Degradation | One source failed but others succeeded with `--allow-partial`. | Record warning; continue pipeline. |

---

## Usage Scenarios & Command Examples

### Scenario 1: Local Development & Smoke Testing
A developer wants to test a modification to `scrapers/google_places.py` for cafes in Beirut without scraping OSM, without hitting Wikidata, and without waiting for 40 HTTP threads:
```bash
# 1. Verify API key and connectivity
leadminer doctor

# 2. Scrape max 10 places in Beirut
leadminer scrape --source google_places --region Beirut --limit 10 -o data/test_raw.csv

# 3. Deduplicate
leadminer dedup data/test_raw.csv -o data/test_dedup.csv

# 4. Enrich offline (tag regions, skip slow HTTP liveness checks)
leadminer enrich -i data/test_dedup.csv -o data/test_enriched.csv --skip-http

# 5. Score and inspect results
leadminer score -i data/test_enriched.csv -o data/test_scored.csv
leadminer stats data/test_scored.csv --group-by service --format text
```

### Scenario 2: Offline Rule Tuning
Sales leadership adjusts service recommendations in `pitch_recommender.py`. Re-score all leads in place without making a single network request:
```bash
leadminer score -i data/all_businesses.csv -o data/all_businesses.csv
leadminer export -i data/all_businesses.csv --out-dir data/
leadminer stats data/all_businesses.csv --group-by service
```

### Scenario 3: Automated Quality Gate in CI
Before rclone uploads outputs to Google Drive, validate that the output contains the full 22-column contract and no corrupted geographic coordinates:
```bash
leadminer validate data/all_businesses.csv --schema canonical --check-geo --strict
```

### Scenario 4: Targeted Regional Sweep for Saudi Arabia
Scrape only Saudi Arabian targets (e-commerce, hospitality, fintech in Riyadh, Jeddah, Dammam), using 10 workers for Google Places:
```bash
leadminer scrape --source google_places --region SA --workers 10 -o data/sa_raw.csv
leadminer dedup data/sa_raw.csv -m data/all_businesses.csv -o data/sa_merged.csv --default-country SA
leadminer enrich -i data/sa_merged.csv -o data/sa_enriched.csv --workers 30
leadminer score -i data/sa_enriched.csv -o data/all_businesses.csv
leadminer export -i data/all_businesses.csv --out-dir data/
```

---

## CI/CD Workflow Architecture

The current GitHub Actions workflow (`.github/workflows/scrape.yml`) runs `python main.py` in a single opaque step:
```yaml
# Current .github/workflows/scrape.yml:31
- name: Run leadminer
  env:
    GOOGLE_PLACES_API_KEY: ${{ secrets.GOOGLE_PLACES_API_KEY }}
  run: python main.py
```

With the CLI, the workflow is refactored into distinct, observable steps with clear failure boundaries:

```yaml
# Proposed .github/workflows/scrape.yml replacement
name: Monthly Scrape & Enrich

on:
  workflow_dispatch:
  schedule:
    - cron: '0 3 1 * *'  # 1st of every month at 03:00 UTC

jobs:
  pipeline:
    runs-on: ubuntu-latest
    timeout-minutes: 180

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python 3.12
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: 'pip'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Setup rclone
        uses: PallTron/rclone@v1
        with:
          args: version

      - name: Pull cumulative master from Google Drive
        env:
          RCLONE_CONFIG_GDRIVE_TYPE: drive
          RCLONE_CONFIG_GDRIVE_SCOPE: drive
          RCLONE_CONFIG_GDRIVE_TOKEN: ${{ secrets.RCLONE_GDRIVE_TOKEN }}
        run: |
          mkdir -p data
          # Do not swallow failure with || true
          rclone copy gdrive:leadminer/all_businesses.csv data/ || echo "Cold start: No existing master found."

      - name: CLI Preflight Doctor
        env:
          GOOGLE_PLACES_API_KEY: ${{ secrets.GOOGLE_PLACES_API_KEY }}
          SCRAPER_EMAIL: "ops@voxire.com"
        run: |
          python -m leadminer doctor --strict

      - name: Execute Full Pipeline
        env:
          GOOGLE_PLACES_API_KEY: ${{ secrets.GOOGLE_PLACES_API_KEY }}
          SCRAPER_EMAIL: "ops@voxire.com"
        run: |
          python -m leadminer run --allow-partial

      - name: Validate Output Integrity
        run: |
          python -m leadminer validate data/all_businesses.csv --strict
          python -m leadminer validate data/sales_ready.csv --strict

      - name: Emit Pipeline Telemetry
        run: |
          python -m leadminer stats data/all_businesses.csv --format markdown >> $GITHUB_STEP_SUMMARY

      - name: Sync Master to Google Drive
        if: success()
        env:
          RCLONE_CONFIG_GDRIVE_TOKEN: ${{ secrets.RCLONE_GDRIVE_TOKEN }}
        run: |
          rclone copy data/ gdrive:leadminer/ --include "*.csv"
```

---

## Backward Compatibility & Migration Strategy

To prevent breaking existing scripts, documentation, and external hooks during the transition, `main.py` should be converted into a proxy that preserves legacy execution while issuing a deprecation notice:

```python
# main.py (Backward-compatibility wrapper)
import sys
import warnings

def main() -> None:
    warnings.warn(
        "Invoking 'python main.py' is deprecated and will be removed in v2.0. "
        "Use 'python -m leadminer run' or the 'leadminer' CLI instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    from leadminer.cli import main as cli_main
    # Translate bare invocation to `leadminer run`
    sys.exit(cli_main(["run"] + sys.argv[1:]))

if __name__ == "__main__":
    main()
```

---

## Not a bug, but worth knowing

1. **Subprocess stdout vs. stderr separation:** In `main.py:79, 110, 119`, progress strings are printed directly to `stdout`. In the new CLI design, all operational logs, progress counters (`[Enricher] 200/1000 done...`), and diagnostics must be written to `sys.stderr`. `sys.stdout` must be reserved strictly for command outputs (such as CSV records when piping, or JSON payloads when `--format json` is requested).
2. **Atomic writes on POSIX and Windows:** When subcommands write CSV outputs (`dedup`, `enrich`, `score`, `export`), they should write to a sibling file with a `.tmp` suffix and execute `os.replace(temp_path, final_path)`. On POSIX systems, `os.replace` is atomic; on Windows, it atomically overwrites existing destination files, preventing truncated 0-byte CSVs if a job is killed mid-write.
3. **Packaging with `pyproject.toml`:** Adding a minimal `pyproject.toml` with `[project.scripts] leadminer = "leadminer.cli:main"` enables the `leadminer` command directly in the shell when installed in editable mode (`pip install -e .`), while `python -m leadminer` works immediately without packaging changes.

---

## Recommended Order of Work

1. **Create Package Structure:**
   - Create `leadminer/` package root, move CLI parser logic into `leadminer/cli.py`, and register `__main__.py`.
2. **Implement `leadminer doctor`:**
   - Build health checks for environment variables (`GOOGLE_PLACES_API_KEY`, `SCRAPER_EMAIL`) and network pings to Overpass, Wikidata, and Google Places.
3. **Decouple Pipeline Stages into Subcommands:**
   - Extract `scrape` command with `--source` and `--region` parameterization.
   - Extract `dedup` command with master merge logic.
   - Extract `enrich` command with `--workers` and `--skip-http` flags.
   - Extract `score` command, ensuring `industry_priority` → `recommend_service` → `lead_score` evaluation order.
   - Extract `export`, `validate`, and `stats` commands.
4. **Implement Composite `run` Command:**
   - Chain individual stages with checkpointed intermediate `.tmp` artifacts.
5. **Update Legacy Wrapper & CI:**
   - Wrap `main.py` with deprecation proxy delegating to `cli.main(["run"])`.
   - Update `.github/workflows/scrape.yml` to run `doctor`, `run`, `validate`, and `stats`.