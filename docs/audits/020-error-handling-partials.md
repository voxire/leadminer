# 020 — Error Handling and Partial Failure Audit

## Verdict

The pipeline has an asymmetric, brittle error-handling architecture: while `main.py` isolates scraper-level crashes with a coarse `try/except Exception`, lower-level concurrency and processing stages contain multiple naked execution points that will crash the process and cause total data loss. Most critically, `enricher.check_websites` calls `future.result()` without a `try/except` block across 40 parallel threads, meaning a single unhandled network, parser, or thread-safety exception after hours of scraping destroys the entire run before CSV output is written. To achieve production resilience, `leadminer` must replace ad-hoc `try/except` statements with an explicit per-stage failure policy: fail-fast on master state corruption and schema violations, isolate and degrade-and-continue on external scraping and HTTP enrichment, and quarantine non-conforming leads into an auditable dead-letter sink.

---

## Enumeration of Unhandled Exceptions That Kill the Run

The following table catalogs every unhandled exception path in the execution flow that can terminate the process or cause catastrophic data loss before results are persisted:

| Stage | Location | Exception Type | Immediate Cause | Blast Radius |
|---|---|---|---|---|
| **Enrichment** | `enricher.py:193` | Any unhandled `Exception` from `_fetch_website` | Naked `future.result()` inside `as_completed(futures)` loop | **Total Run Death.** Crashes main thread; pending futures aborted; hours of OSM, Wikidata, and Google Places data lost before CSV write (`main.py:149`). |
| **Enrichment** | `enricher.py:154-177` | `AttributeError`, `IndexError`, `re.error` | Unprotected parsing block in `_fetch_website` outside `try/except` (e.g. malformed regex, unexpected string types) | Re-raised via `future.result()` at `enricher.py:193`, terminating the process. |
| **Enrichment** | `enricher.py:124, 146` | `RuntimeError`, `KeyError` | 40 threads sharing non-thread-safe `_SESSION.cookies` (`CookieJar` dict mutation during iteration) | Intermittent thread crash, propagated via `future.result()`, killing run. |
| **Scraping** | `scrapers/google_places.py:186` | Any unhandled `Exception` from `fetch_query` | Naked `f.result()` inside query worker pool | **Total Google Places Loss.** Drops all 68 queries even if 67 succeeded; `yield from all_records` is never reached. |
| **Scraping** | `scrapers/google_places.py:222` | `json.JSONDecodeError`, `ValueError` | `resp.json()` called outside `try/except requests.RequestException` block on non-JSON response (e.g., 200 with HTML proxy error) | Crashes `fetch_query`, re-raised at line 186, wiping all Google Places results. |
| **Scraping** | `scrapers/google_places.py:213-216` | Infinite Hang (Process Timeout) | Unbounded `while True` loop on HTTP 429 rate limit | Hangs query worker thread; GitHub Actions 300-min job timeout triggers hard `SIGKILL`. |
| **Scraping** | `scrapers/osm.py:55` | `json.JSONDecodeError`, `requests.exceptions.JSONDecodeError` | `resp.json()` called outside `try/except` on Overpass server-side timeout (Overpass returns HTTP 200 with HTML error) | Crashes `OSMScraper.scrape()`; all OSM records dropped for the run. |
| **Scraping** | `scrapers/wikidata.py:58` | `json.JSONDecodeError`, `requests.exceptions.JSONDecodeError` | `resp.json()` called outside `try/except` on SPARQL gateway timeout or HTML maintenance page | Crashes `WikidataScraper.scrape()`; all Wikidata records dropped for the run. |
| **Filtering** | `scrapers/whitelist.py:112` | `AttributeError` | `category.strip().lower()` called when `category` is non-string (int, float, bool, or dict) | Crashes list comprehension at `main.py:117`; run terminates immediately after scraping. |
| **Dedup** | `dedup.py:12` | `TypeError` | `re.sub(r"\D", "", phone)` called when `phone` is a numeric type (int, float) | Crashes `dedup(combined)` at `main.py:125`; run terminates. |
| **Dedup** | `dedup.py:55` | `AttributeError` | `av.split("|")` in `_merge()` called when `source` is non-string | Crashes `dedup(combined)` at `main.py:125`; run terminates. |
| **Dedup** | `dedup.py:30` | `TypeError` | `unicodedata.normalize("NFKD", name)` called when `name` is non-string | Crashes `dedup(combined)` at `main.py:125`; run terminates. |
| **Dedup** | `dedup.py:58` | `TypeError` | `max(av, bv)` in `_merge()` called when `scraped_at` has mixed types (e.g., str vs. int/float) | Crashes `dedup(combined)` at `main.py:125`; run terminates. |
| **Scoring** | `pitch_recommender.py:50` | `AttributeError` | `category.strip().lower()` called when `category` is non-string | Crashes record loop at `main.py:133`; run terminates before CSV write. |
| **Persistence** | `main.py:75-78` | `OSError`, `IOError`, `KeyboardInterrupt` | Non-atomic write (`open(path, "w")`); interrupted write leaves 0-byte or corrupted CSV | Destroys cumulative `all_businesses.csv`; subsequent run loads empty master. |

---

## Findings

### S1 — Naked `future.result()` in `enricher.check_websites` aborts the pipeline after hours of scraping

- **Where:** `enricher.py:191-195`
- **Breaks:** In `check_websites()`, worker tasks are submitted to a 40-worker `ThreadPoolExecutor`. Results are consumed using `for future in as_completed(futures): live, contacts = future.result()`. If any thread raises an uncaught exception, `future.result()` re-raises it immediately on the main thread. There is no `try/except` enclosing `future.result()` in `enricher.py`, nor is there any enclosing `enrich(records)` in `main.py:129`. The main thread aborts, unhandled tasks in the thread pool are discarded, and execution terminates prior to line 149 (`write_csv`). Hours of Overpass API, Wikidata SPARQL, and Google Places API calls (and associated API costs) are permanently lost without writing a single row to disk.
- **Trigger:** A single thread raises an exception during website fetching—for instance, if `_SESSION.cookies` raises `RuntimeError: dictionary changed size during iteration` under 40-thread contention, or if `_fetch_website` encounters an unexpected parsing issue in lines 154–177.
- **Fix:**
```python
try:
    live, contacts = future.result()
except Exception as exc:
    print(f"[Enricher] Worker error for index {idx}: {exc}", file=sys.stderr)
    live, contacts = False, {"email": None, "instagram": None, "whatsapp": None, "linkedin": None}
```

---

### S1 — Naked `f.result()` in `GooglePlacesScraper.scrape` causes total data loss across all 68 queries

- **Where:** `scrapers/google_places.py:183-187`
- **Breaks:** `GooglePlacesScraper.scrape()` spawns 5 workers across 68 city/industry queries and calls `f.result()` inside `for f in as_completed(futures):`. If even one query fails (e.g., unexpected JSON decoding error on page 2 of query 45), `f.result()` re-raises the exception. This immediately terminates the `as_completed` loop and exits the context manager before `yield from all_records` (line 188) is ever executed. In `main.py:112-113`, the exception is caught and logged, but `batch` is lost entirely: all successfully scraped queries from Google Places are discarded, and the run proceeds with 0 Google leads.
- **Trigger:** Google Places API returns an intermittent HTTP 502/503 HTML error page or truncated response on query #68: `resp.json()` at line 222 raises `json.JSONDecodeError`, which bubbles through `f.result()`.
- **Fix:**
```python
for f in as_completed(futures):
    try:
        f.result()
    except Exception as exc:
        print(f"[Google] Query worker failed: {exc}", file=sys.stderr)
```

---

### S1 — Unprotected `resp.json()` calls crash OSM and Wikidata scrapers on API gateway timeouts

- **Where:** `scrapers/osm.py:55`; `scrapers/wikidata.py:58`
- **Breaks:** Both scrapers implement retry loops wrapping HTTP network calls, but place `resp.json()` *outside* the retry and `try/except` blocks. Overpass API and Wikidata Query Service (SPARQL) frequently encounter backend query timeouts under heavy load. When this happens, Overpass returns HTTP 200 with an HTML/text error body (`<strong style="color:#FF0000">Error</strong>: runtime error: Query timed out`), and Wikidata returns an HTML gateway timeout page. Calling `resp.json()` raises `requests.exceptions.JSONDecodeError` / `json.JSONDecodeError`, which is unhandled. The scraper crashes out of `scrape()`, resulting in complete data loss for that source in `main.py`.
- **Trigger:** Overpass API times out on the Lebanon monolithic union query, returning HTTP 200 with HTML error text. Line 55 executes `resp.json()` and raises `JSONDecodeError`.
- **Fix:**
```python
try:
    elements = resp.json().get("elements", [])
except Exception as e:
    print(f"[OSM] Failed to parse JSON response: {e}", file=sys.stderr)
    return
```

---

### S1 — Non-atomic CSV writes destroy cumulative master on process abortion or crash

- **Where:** `main.py:74-80, 149-153`
- **Breaks:** `write_csv()` directly opens the destination path in `"w"` mode: `with open(path, "w", newline="", encoding="utf-8") as f:`. If the process is terminated mid-write (SIGINT, GitHub Actions runner timeout, disk quota exhaustion, or unhandled exception during `writer.writerows()`), `all_businesses.csv` is left truncated at 0 bytes or corrupted. Because `.github/workflows/scrape.yml` downloads the master at the start of each run, a subsequent run will load the truncated CSV via `load_master()`, wiping out all historical leads accumulated across months of runs.
- **Trigger:** Disk full or workflow timeout during the writing of `data/all_businesses.csv` at `main.py:149`.
- **Fix:** Write to a temporary file in the same directory (`path.with_suffix(".tmp")`) and perform an atomic replace using `os.replace(temp_path, path)`.

---

### S1 — Infinite 429 retry loop in `GooglePlacesScraper` leads to GitHub Actions timeout exhaustion

- **Where:** `scrapers/google_places.py:213-216`
- **Breaks:** When Google Places API responds with HTTP 429 (Too Many Requests), the scraper logs a message, sleeps for 30 seconds, and executes `continue` within an unbounded `while True:` loop. If the project exceeds its daily quota or the API key is restricted, the loop never terminates. Because 5 threads are running, all 5 worker threads eventually get trapped in this infinite loop. The process hangs indefinitely until the 300-minute GitHub Actions job limit is reached, causing the runner to terminate the entire job with `SIGKILL` without persisting any collected records.
- **Trigger:** Google Places daily spending limit reached or query quota exhausted on a run.
- **Fix:** Enforce a maximum retry counter (e.g., `max_retries = 3`) per query before returning the partial records collected so far.

---

### S2 — Shared non-thread-safe `requests.Session` across 40 enricher workers causes intermittent crashes

- **Where:** `enricher.py:124-125, 146`
- **Breaks:** `_SESSION = requests.Session()` is instantiated at module scope and shared across 40 threads in `check_websites()`. In the `requests` library, `requests.Session` is explicitly not thread-safe: modifying `session.cookies` (`CookieJar`) concurrently causes race conditions where dictionary resizing during iteration raises `RuntimeError` or `KeyError`. While lines 145–152 catch generic `Exception` during `_SESSION.get()`, connection pool corruption or internal state collisions inside `urllib3.PoolManager` can cause worker threads to fail silently or crash, which then propagates to `future.result()`.
- **Trigger:** High-concurrency website scanning where multiple websites simultaneously set response cookies.
- **Fix:** Use a `threading.local()` instance to ensure each worker thread owns its own isolated `requests.Session`, or instantiate a fresh `requests.Session` per task.

---

### S2 — Unprotected regex extraction in `_fetch_website` outside `try/except`

- **Where:** `enricher.py:154-177`
- **Breaks:** `_fetch_website` only encloses `_SESSION.get()` and `html = r.text[:200_000]` within `try/except Exception`. Lines 154 through 177 execute regex extraction for emails, Instagram, WhatsApp, and LinkedIn slugs directly on the raw HTML string without exception protection. If an unhandled exception occurs (such as regex engine backtracking limits, unicode decode edge cases, or string operation bugs), it escapes `_fetch_website` and is thrown into the thread's future, causing `future.result()` in `check_websites` to crash the entire application.
- **Trigger:** A website returning malformed binary/text data that triggers an edge-case parser exception during regex execution.
- **Fix:** Move the entire body of `_fetch_website`, including all regex matching and string formatting, inside the `try/except Exception` block.

---

### S2 — Type-unsafe field operations in `whitelist.py`, `dedup.py`, and `pitch_recommender.py`

- **Where:** `scrapers/whitelist.py:112`; `dedup.py:12, 55, 58`; `pitch_recommender.py:50`
- **Breaks:** 
  1. `whitelist.py:112`: `cat = category.strip().lower()` assumes `category` is a `str`. If `category` is parsed as an int, bool, or float (e.g. from upstream raw JSON or corrupted CSV), an uncaught `AttributeError` terminates the run at `main.py:117`.
  2. `dedup.py:12`: `re.sub(r"\D", "", phone)` assumes `phone` is a string. A numeric phone value raises `TypeError: expected string or bytes-like object`.
  3. `dedup.py:55`: `sources = set(av.split("|")) | set(bv.split("|"))` assumes `source` is a string. If either record has a non-string `source`, `split()` raises `AttributeError`.
  4. `dedup.py:58`: `max(av, bv)` on `scraped_at` fails with `TypeError` if string and numeric/null types are compared.
  5. `pitch_recommender.py:50`: `category = (record.get("category") or "").strip().lower()` raises `AttributeError` if `category` is an int or bool.
- **Trigger:** Upstream OSM tag `shop=1` or Google Places category list returning numeric or non-string values.
- **Fix:** Coerce all input values to strings defensively before invoking string methods (e.g., `cat = str(category).strip().lower() if category is not None else ""`).

---

### S3 — `load_master` lacks schema validation and fails on CSV null-byte corruption

- **Where:** `main.py:46-71`
- **Breaks:** `load_master()` reads `all_businesses.csv` without verifying that required header columns exist. If a corrupted CSV containing NUL bytes (`\x00`) is loaded (e.g., from an aborted write or disk glitch), `csv.DictReader` raises `_csv.Error: line contains NUL`, which is unhandled and halts `main.py` at line 95. Furthermore, if the CSV structure is missing fields, records are loaded with missing keys, causing silent downstream type and KeyError failures.
- **Trigger:** A previously corrupted or zero-padded `all_businesses.csv` file in the `data/` directory.
- **Fix:** Wrap `load_master` file reading in a `try/except (csv.Error, OSError)` block; validate required headers against `FIELDS`.

---

## Not a Bug, but Worth Knowing

1. **`main.py` Swallows Scraper Crashes Without Threshold Alarms:** `main.py:108-114` catches any top-level scraper exception and prints to `sys.stderr`. While this prevents a single scraper crash from immediately halting the process, it fails open: if all three scrapers fail (or return 0 records due to API bans), the pipeline silently continues, runs dedup and enrichment on an empty or master-only set, and uploads the result without signaling a failure to CI.
2. **Missing Timeouts on Future Resolution:** In both `main.py` and `enricher.py`, `as_completed()` and `future.result()` calls specify no timeout. If a worker thread deadlocks or hangs (e.g., inside an un-timeoutable socket read in C-extensions), the master process will wait indefinitely.

---

## Per-Stage Failure Policy Design

To prevent cascading pipeline aborts and eliminate silent data corruption, every pipeline stage must adhere to an explicit, standardized failure mode: **Fail-Fast**, **Degrade-and-Continue**, or **Quarantine**.

```
[Stage 0: Master Ingestion] ----(Fail-Fast on Corrupt Master)
            │
[Stage 1: Scrapers (OSM, WD, GP)] -(Degrade-and-Continue; Circuit Breaker >90% err)
            │
[Stage 2: Whitelist & Validation] --(Quarantine Malformed Schema to dead_letter.jsonl)
            │
[Stage 3: Dedup & Merge] ----------(Degrade-and-Continue with Defensive String Coercion)
            │
[Stage 4: Region Inference] -------(Degrade-and-Continue; Tag 'Unknown' on Unmatched)
            │
[Stage 5: Website Enrichment] -----(Degrade-and-Continue per Lead; Circuit Breaker on Bulk 403/Ban)
            │
[Stage 6: Scoring & Pitching] -----(Degrade-and-Continue; Fallback to Default Tiers)
            │
[Stage 7: Atomic CSV Export] ------(Fail-Fast on Write / Master Shrink Guard)
```

---

### Stage 0: Master State Acquisition & Ingestion
- **Policy:** **FAIL-FAST**
- **Rationale:** The cumulative master CSV (`all_businesses.csv`) represents historical business intelligence collected over multiple runs. Operating on corrupted or accidentally missing master state leads to irrecoverable data loss when outputs are synced to Google Drive.
- **Operational Rules:**
  1. *Acquisition Validation:* If `.github/workflows/scrape.yml` attempts to pull the master from Google Drive and `rclone` fails with an authentication, network, or path error, the workflow must fail immediately. Suppressing failure with `|| true` is strictly prohibited.
  2. *Integrity Check:* `load_master()` must verify that the file exists, is non-empty, contains valid CSV headers matching `FIELDS`, and parses without `_csv.Error`.
  3. *Bootstrap Exception:* An empty master is permitted *only* if an explicit environment variable `ALLOW_EMPTY_MASTER=true` is passed during initial repository provisioning.
  4. *Failure Action:* Raise `RuntimeError("Master state invalid or inaccessible. Aborting run to protect cumulative data.")` and terminate the process with exit code `1`.

---

### Stage 1: Raw Scraper Ingestion (OSM, Wikidata, Google Places)
- **Policy:** **DEGRADE-AND-CONTINUE with Source-Level Isolation & Global Floor**
- **Rationale:** External APIs suffer from rate limits, network blips, and upstream maintenance windows. A failure in one external provider should not invalidate leads harvested from others.
- **Operational Rules:**
  1. *Sub-Task Isolation:* In `GooglePlacesScraper`, each query must be individually isolated in a `try/except` block. If 5 queries fail out of 68, the 63 successful queries must be yielded, not dropped.
  2. *Bounded Retries:* Network requests must use bounded exponential backoff (e.g. max 3 retries, delays: 2s, 8s, 30s). Infinite retry loops on HTTP 429 are forbidden; after 3 retries, the specific query must log an error and yield empty.
  3. *JSON Parse Safety:* All `resp.json()` calls must be enclosed in `try/except (ValueError, requests.exceptions.JSONDecodeError)`. If JSON parsing fails, log the raw response snippet to `sys.stderr` and abort only that scraper attempt.
  4. *Global Ingestion Floor (Circuit Breaker):* At the completion of the scraping stage, calculate total raw records. If `len(raw) == 0` AND `len(master) == 0`, trigger **FAIL-FAST**: abort the run with an exit code `2` to prevent writing and uploading empty CSV files. If `len(raw) == 0` but `len(master) > 0`, log a critical alert and abort without overwriting the master.

---

### Stage 2: Schema Validation & Category Whitelisting
- **Policy:** **QUARANTINE**
- **Rationale:** Records entering the pipeline from different scrapers may contain malformed data types, missing required attributes, or dirty categories. Discarding them silently conceals scraper bugs; letting them crash the pipeline halts operations.
- **Operational Rules:**
  1. *Type Normalization:* Coerce all incoming fields to primitive expected types (`str`, `float`, `int`, `None`). Ensure `category` is cast via `str(val).strip().lower() if val else None`.
  2. *Schema Conformance:* Any record missing a valid `name` or containing unparseable geometric/coordinate data must be routed to a dead-letter sink: `data/quarantine_invalid_schema.jsonl`.
  3. *Category Filtering:* Records failing `is_business_category(category)` that are explicitly classified under `GEOGRAPHIC_NOISE` are dropped silently. Records with unknown or non-whitelisted categories are logged to `data/quarantine_unmatched_categories.jsonl` for weekly taxonomy review.

---

### Stage 3: Deduplication & Identity Merging
- **Policy:** **DEGRADE-AND-CONTINUE with Defensive Normalization**
- **Rationale:** Deduplication joins newly scraped data with the cumulative master. Unhandled type mismatches between historical string fields and freshly scraped types must never abort the merge.
- **Operational Rules:**
  1. *Defensive Phone Normalization:* In `normalize_phone`, coerce `str(phone)` before stripping non-digits. If stripping results in `< 7` digits, treat phone as absent and fall back to `(name, city)` indexing.
  2. *Safe Conflict Resolution in `_merge`:* 
     - `source`: Coerce both operands to `str`, split on `|`, take set union, and re-join with `|`.
     - `scraped_at`: Ensure ISO-8601 string normalization; parse to UTC datetimes prior to computing `max()` to prevent string precision sorting bugs.
     - General scalar conflicts: Pick the value from the record with higher non-null field completeness.
  3. *Unmatched Identity:* Records lacking both a valid normalized phone number and a valid `(normalized_name, city)` tuple must be quarantined to `data/quarantine_unidentifiable.jsonl`.

---

### Stage 4: Geocoding & Region Inference
- **Policy:** **DEGRADE-AND-CONTINUE**
- **Rationale:** Region classification is an analytical enrichment feature. A missing or unmappable address/coordinate should not block sales readiness for an otherwise valid business.
- **Operational Rules:**
  1. *Bounding Box Fallback:* If coordinate lookup fails or coordinates fall outside known Lebanon/KSA boxes, fall back to keyword address regex.
  2. *Default Classification:* If both coordinate and keyword matching yield no match, assign `r["region"] = "Unknown"`. Do not raise exceptions or drop records.

---

### Stage 5: Concurrent Website Liveness & Contact Extraction
- **Policy:** **DEGRADE-AND-CONTINUE with Per-Lead Isolation & Global Circuit Breaker**
- **Rationale:** Probing external third-party websites across 40 threads encounters every failure mode of the internet: DNS resolution failures, connection resets, SSL handshake rejections, WAF blocks, and honeypots. No single website failure may ever crash a thread or the parent process.
- **Operational Rules:**
  1. *Worker Isolation:* Wrap the worker loop inside `check_websites` with a complete `try/except Exception`:
     ```python
     for future in as_completed(futures):
         idx = futures[future]
         try:
             live, contacts = future.result()
         except Exception as exc:
             print(f"[Enricher] Worker exception for index {idx}: {exc}", file=sys.stderr)
             live, contacts = False, {"email": None, "instagram": None, "whatsapp": None, "linkedin": None}
     ```
  2. *Thread-Isolated Sessions:* Replace the global `_SESSION` with a `threading.local()` session factory to eliminate concurrent `CookieJar` mutations and connection pool corruption.
  3. *Full Extraction Guard:* Enclose regex matching and contact parsing in `_fetch_website` within the error handler. If parsing fails, mark `live = True` (if HTTP status was valid) and return empty contacts.
  4. *Global Enrichment Circuit Breaker:* Track the consecutive failure rate across the worker pool. If >90% of requests fail over a window of 100 consecutive requests (indicating local outbound network failure, DNS outage, or widespread IP ban), trip the circuit breaker: abort the remaining enrichment futures, preserve existing scraped records with `website_live = None`, and continue pipeline execution to scoring and export.

---

### Stage 6: Scoring, Classification & Pitch Recommendation
- **Policy:** **DEGRADE-AND-CONTINUE**
- **Rationale:** Lead scoring and service pitch recommendation are deterministic, pure-Python heuristic transformations without network I/O.
- **Operational Rules:**
  1. *Safe Field Access:* Use `.get()` with explicit defaults for all score calculations.
  2. *Numeric Coercion:* Wrap rating, review count, and completeness evaluations in `try/except (ValueError, TypeError)` with default fallbacks (`rating = 0.0`, `review_count = 0`).
  3. *Default Fallback Pitch:* If rule matching encounters an unexpected category or missing attribute state, assign the default baseline pitch: `"Discovery call - scope the right service"`.

---

### Stage 7: Persistence, Output Export & Sanity Guards
- **Policy:** **ATOMIC WRITE + PRE-PUBLISH VALIDATION (FAIL-FAST)**
- **Rationale:** Exporting corrupted, partial, or accidentally truncated data files to disk or remote storage irreversibly damages downstream sales workflows and cumulative historical state.
- **Operational Rules:**
  1. *Atomic File Writes:* `write_csv()` must write to a temporary file on the same filesystem (`.tmp`) and replace the target path via `os.replace()` only upon successful completion of all writes:
     ```python
     temp_path = path.with_suffix(".tmp")
     with open(temp_path, "w", newline="", encoding="utf-8") as f:
         writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
         writer.writeheader()
         writer.writerows(records)
         f.flush()
         os.fsync(f.fileno())
     os.replace(temp_path, path)
     ```
  2. *Master Shrink Guard (Sanity Validation):* Before exiting `main.py`, verify that `len(records) >= len(master)`. If `len(records) < len(master) * 0.8` (indicating an anomalous >20% loss of historical records), halt the pipeline immediately with an error exit code and do not allow the run to report success.
  3. *Automated GitHub Actions Guard:* In `.github/workflows/scrape.yml`, only execute the `rclone copy` step if `main.py` exited with status code `0` and `data/all_businesses.csv` is non-empty.

---

## Recommended Order of Work

1. **Fix `enricher.py:191-195` (S1):** Wrap `future.result()` in `check_websites()` with `try/except Exception` to immediately eliminate the vulnerability that terminates runs after hours of work.
2. **Isolate query workers in `scrapers/google_places.py:183-187` (S1):** Add `try/except` around `f.result()` in `GooglePlacesScraper.scrape()` so that a failure in one query does not drop all 68 queries.
3. **Protect `resp.json()` in all scrapers (S1):** Enclose `resp.json()` in `scrapers/osm.py:55`, `scrapers/wikidata.py:58`, and `scrapers/google_places.py:222` within `try/except (ValueError, requests.exceptions.JSONDecodeError)` blocks.
4. **Implement atomic file writes in `main.py:74-80` (S1):** Update `write_csv()` to write to `.tmp` files and swap atomically via `os.replace()`, preventing cumulative master corruption on process abort.
5. **Bound 429 retries in `scrapers/google_places.py:213-216` (S1):** Add a retry counter to prevent infinite 30-second sleep loops from consuming the GitHub Actions 300-minute allocation.
6. **Eliminate thread-unsafe session in `enricher.py:124, 146` (S2):** Convert `_SESSION` to a `threading.local()` session or instantiate worker-local sessions to prevent `CookieJar` mutation race conditions.
7. **Protect full body of `_fetch_website` (S2):** Wrap regex parsing and string extractions in `enricher.py:154-177` inside the function's main `try/except` block.
8. **Enforce defensive type coercion (S2):** Guard `whitelist.py:112`, `dedup.py:12, 55, 58`, and `pitch_recommender.py:50` against non-string and mismatched types.
9. **Add Master Shrink Guard & CI workflow failure enforcement (S1/S3):** Fail the GitHub Actions job if `all_businesses.csv` is unexpectedly shrunken, and remove `|| true` from master synchronization steps.