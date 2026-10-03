# 014 — Google Places Quota, Cost Model, and 429 Retry Loop Vulnerability

## Verdict

The Google Places scraper (`scrapers/google_places.py`) executes 67 targeted queries (cited as 68 in the brief) against the Google Places API (New) `places:searchText` endpoint with up to 3 pages per query (capped server-side at 60 results per query), generating **a minimum of 67 and a maximum of 201 billable requests per run** (or 68 to 204 requests under the brief's count). Because the field mask requests `websiteUri`, `nationalPhoneNumber`, `rating`, and `userRatingCount`, every request triggers the **Text Search Enterprise** SKU ($35.00 / 1,000 requests); at the intended monthly or weekly cadences (201 to 804 requests/month), the scraper remains completely within Google's 1,000-request free tier (**$0.00/month**), rising to **$176.05/month** only if run on a daily cadence.

However, the 429 handling at `google_places.py:213-216` contains a critical operational flaw: upon receiving an HTTP 429, the code executes `time.sleep(30)` followed by `continue` inside an unbounded `while True:` loop without retry counting, backoff, or max-attempt termination. In the event of account-level daily quota exhaustion or billing budget cap enforcement (which return HTTP 429 `RESOURCE_EXHAUSTED`), all 5 worker threads enter an infinite retry loop, preserving `page_token` across `continue` and hammering Google every 30 seconds until the GitHub Actions workflow hits its hard 300-minute ceiling (`timeout-minutes: 300`), wasting up to 5 hours of billable runner time per run.

---

## Findings

### S1 — Unbounded 429 retry loop causes infinite execution and 5-hour CI hangs on quota exhaustion

- **Where:** `scrapers/google_places.py:203-217`
- **Breaks:** When Google Places API returns HTTP 429, lines 213–216 execute:
  ```python
  if resp.status_code == 429:
      print(f"[Google] Rate limited on '{query}', waiting 30s...")
      time.sleep(30)
      continue
  ```
  There is no retry counter, no max-retries limit, and no exponential backoff. In Google Maps Platform, HTTP 429 is returned not only for transient QPS spikes, but also for **daily quota exhaustion** and **billing cap exhaustion** (`RESOURCE_EXHAUSTED`). When a billing cap or daily quota is reached, every subsequent request returns 429 permanently. Because all 5 worker threads (`_WORKERS = 5`, line 141) share the same API key and quota, all 5 workers enter this infinite 30-second sleep loop simultaneously. In `scrape()` (lines 183–186), `as_completed(futures)` never yields, freezing the entire Python process.
- **Trigger:** The Google Cloud billing account reaches its daily budget cap or project quota limit during a run.
- **Impact:** The GitHub Actions workflow (`.github/workflows/scrape.yml:15`) has `timeout-minutes: 300`. The job hangs for 5 full hours (burning 300 GitHub Actions runner minutes) before being forcibly killed by GitHub's runner supervisor, producing no data and masking the underlying quota failure. During those 5 hours, the 5 worker threads emit ~3,000 useless HTTP requests (5 threads × 2 req/min × 300 min).
- **Fix:** Replace the infinite retry with a bounded retry counter and exponential backoff:
  ```python
  MAX_RETRIES = 3
  retries = 0
  ...
  if resp.status_code == 429:
      retries += 1
      if retries > MAX_RETRIES:
          print(f"[Google] Quota/rate limit exceeded for '{query}' after {MAX_RETRIES} retries. Aborting query.")
          return records
      sleep_time = min(30, 2 ** retries * 5)
      time.sleep(sleep_time)
      continue
  ```

---

### S2 — `page_token` state preservation across 429 `continue` risks token expiration and silent loss of up to 40 records

- **Where:** `scrapers/google_places.py:200, 204-207, 213-217, 275`
- **Breaks:** Variable `page_token` is scoped outside the `while True:` loop (`page_token = None` at line 200) and is only updated at the very end of a successful page iteration (`page_token = data.get("nextPageToken")` at line 275). When a paginated request (page 2 or page 3) hits HTTP 429:
  1. Lines 218–278 are skipped due to `continue`.
  2. `page_token` remains assigned to the `nextPageToken` string returned by the *previous* successful page.
  3. The next loop iteration constructs `body = {"textQuery": query, "pageToken": page_token}` and retries the exact same page.
  
  While retrying with the same token is correct for an immediate retry, Google Places API (New) page tokens are ephemeral. If multiple 429 retries occur (e.g. 2 or 3 retries = 60 to 90 seconds of cumulative sleep) or if Google expires the token, Google returns **HTTP 400 Bad Request** (`INVALID_ARGUMENT: The provided page token is invalid or has expired`).
  
  Because HTTP 400 is neither 401 nor 429, execution falls through to line 217 (`resp.raise_for_status()`), which raises `requests.HTTPError`. Line 218 (`except requests.RequestException as e:`) catches the error, logs a message, and executes `return records`. The query aborts immediately, **silently discarding all subsequent pages** (up to 40 valid business records lost for that query) with zero recovery.
- **Trigger:** A transient 429 on page 2 or 3 that persists for >60 seconds, or an ephemeral token invalidated server-side by Google during rate-limit cooldown.
- **Fix:** If a paginated call fails with an invalid/expired token error (HTTP 400), log specifically that pagination was broken by token expiry, or fall back to an unpaginated query with narrower geographic bounds.

---

### S3 — Severe error-handling asymmetry: infinite retries for 429 vs. zero retries for transient network drops

- **Where:** `scrapers/google_places.py:209-220`
- **Breaks:** `session.post(API_URL, json=body, timeout=30)` handles errors asymmetrically:
  - HTTP 429 (rate limit / quota): Retries **infinitely** with 30-second sleeps (lines 213–216).
  - Transient network drops, connection resets, DNS blips, or read timeouts (`requests.Timeout`, `requests.ConnectionError`): Caught by line 218 (`except requests.RequestException:`) and **immediately aborts the entire query** (`return records`), performing 0 retries.
  A transient 100ms packet drop on a GitHub Actions runner permanently aborts a query, while a hard quota block hangs the thread forever.
- **Trigger:** A brief network glitch or TCP reset during any query page request.
- **Fix:** Mount a `requests.adapters.HTTPAdapter` with `urllib3.util.Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])` on the session in `_make_session()`, establishing a uniform, bounded retry policy for all transient failures.

---

### S4 — Unbounded `while True` loop lacks defensive client-side pagination ceiling

- **Where:** `scrapers/google_places.py:203, 275-277`
- **Breaks:** The loop at line 203 relies entirely on the upstream API returning a falsy `nextPageToken` (`if not page_token: return records` at line 276). There is no safety ceiling on `page` (line 224 increments `page` for logging, but never checks it). Google Places Text Search (New) currently returns a maximum of 60 results across 3 pages (20 per page). However, if Google modifies the pagination behavior, if an API regression causes `nextPageToken` to loop, or if a custom `pageSize` is introduced in the future, the client has no defensive guard and will iterate indefinitely, exhausting API quotas and incurring unexpected Enterprise billing charges.
- **Trigger:** Upstream API regression or configuration returning non-terminating page tokens.
- **Fix:** Add an explicit guard: `if page >= 3 or not page_token: return records`.

---

## Detailed Quota, Concurrency, and Cost Analysis

### 1. Query Count: 67 in Code vs. 68 in Brief

A literal count of the query arrays in `scrapers/google_places.py`:
- `LEBANON_QUERIES` (`google_places.py:45-79`): Exactly **33 queries**.
- `KSA_QUERIES` (`google_places.py:81-124`): Exactly **34 queries** (7 DTC/e-comm, 6 hospitality, 4 real estate, 5 fintech/tech, 4 healthcare, 5 adjacent, 3 Dammam).
- **Total defined queries in code:** $33 + 34 =$ **67 queries**.
- Note: `README.md:12` explicitly documents *"67 targeted queries"*. `BRIEF.md:21` and the audit lens cite *"68 queries"*, which is an off-by-one discrepancy in the project brief. Calculations below present both the actual code figure (67) and the brief figure (68).

### 2. Request Bounds per Full Sweep

Google Places API (New) `places:searchText` caps results at **60 places per text search query**, returned in pages of 20 results:
- **Page 1:** Initial POST (no `pageToken`). Returns up to 20 places + `nextPageToken`.
- **Page 2:** POST with `pageToken`. Returns up to 20 places + `nextPageToken`.
- **Page 3:** POST with `pageToken`. Returns up to 20 places (total 60). No `nextPageToken` returned.

| Metric | Actual Code (67 queries) | Brief Count (68 queries) |
|---|---|---|
| **Minimum billable requests** (all queries return $\le 20$ results or 0 results) | 67 | 68 |
| **Maximum billable requests** (all queries return $\ge 60$ results across 3 pages) | **201** | **204** |
| **Maximum raw business records** ($Q \times 60$) | 4,020 | 4,080 |
| **Realistic requests per sweep** (broad queries hit 3 pages; niche hit 1–2 pages) | ~160 – 190 | ~165 – 195 |

### 3. Google Maps SKU and Pricing Classification

Billing is evaluated at the highest SKU tier triggered by the requested fields in `X-Goog-FieldMask` (`google_places.py:31-42`):

```
places.id, places.displayName, places.formattedAddress, places.location,
places.websiteUri, places.nationalPhoneNumber, places.types,
places.rating, places.userRatingCount, nextPageToken
```

Under Google Maps Platform pricing (Places API New):
- `places.id`, `nextPageToken`: **Essentials** (Included / free)
- `places.displayName`, `places.formattedAddress`, `places.location`, `places.types`: **Text Search Pro** ($32.00 / 1,000)
- `places.websiteUri`, `places.nationalPhoneNumber`: **Text Search Enterprise** ($35.00 / 1,000)
- `places.rating`, `places.userRatingCount`: **Text Search Enterprise** ($35.00 / 1,000)

Because `websiteUri`, `nationalPhoneNumber`, `rating`, and `userRatingCount` are present, **100% of all requests are billed as Text Search Enterprise at $35.00 per 1,000 requests** ($0.035 per request).

*(Note: Even if `rating` and `userRatingCount` were removed, the presence of `websiteUri` and `nationalPhoneNumber` keeps the request at the Enterprise tier. Reaching the Pro tier would require dropping phone numbers and websites from the initial search, which defeats the purpose of lead generation unless done as a two-stage Details lookup).*

### 4. Monthly Cost Model Across Cadences

Google Maps Platform accounts receive a monthly free tier (1,000 free Enterprise calls per month under current Places API New SKU pricing, or a $200/month recurring billing credit under legacy billing):

| Cadence | Runs / Mo | Total Requests (Max) | Free Tier Allowance | Net Billable Requests | Net Monthly Cost |
|---|---|---|---|---|---|
| **Manual Dispatch / Monthly Cron** (`README.md`) | 1 | 201 (or 204) | 1,000 | 0 | **$0.00** |
| **Bi-weekly** | 2 | 402 (or 408) | 1,000 | 0 | **$0.00** |
| **Weekly** | 4 | 804 (or 816) | 1,000 | 0 | **$0.00** |
| **Daily** | 30 | 6,030 (or 6,120) | 1,000 | 5,030 | **$176.05** (or $179.20) |

- **Headline Cost:** Under the intended monthly schedule (or even weekly execution), the Google Places scraper costs **$0.00/month**.
- **Gross list price per sweep:** $201 \times \$0.035 =$ **$7.04 per run** (before applying free tier credits).

### 5. Concurrency, Rate Limiting, and Inter-Page Sleep

The scraper runs with `_WORKERS = 5` (`google_places.py:141`).
- **Sequential sleep per query:** `time.sleep(2)` at line 278 is executed after fetching each page that has a `nextPageToken`.
- **Query lifecycle:** For a query with 3 pages:
  - Page 1: POST (~0.4s) $\rightarrow$ sleep 2.0s
  - Page 2: POST (~0.4s) $\rightarrow$ sleep 2.0s
  - Page 3: POST (~0.4s) $\rightarrow$ finishes query
  - Total time per 3-page query $\approx 5.2$ seconds.
- **Aggregate throughput across 5 workers:**
  - Effective request rate per worker $\approx 1 \text{ request} / 2.4 \text{s} = 0.42 \text{ req/s}$.
  - Total pool throughput $\approx 5 \times 0.42 \approx \mathbf{2.1 \text{ requests/second}}$ ($\approx 126 \text{ QPM}$).
- **Comparison to Google Quotas:** Google Places API default quota is **600 QPM (10 QPS)**. The scraper operates at ~21% of default quota. Under steady-state conditions, 5 workers will not trigger rate-limiting 429s.
- **The Cargo-Cult 2-Second Sleep:** In the legacy Google Places API (`/maps/api/place/textsearch/json`), a 2-second delay was mandatory because `next_page_token` was generated asynchronously and returned `INVALID_REQUEST` if called immediately. In Places API (New) (`v1/places:searchText`), `nextPageToken` is valid **immediately**. The 2-second sleep is a legacy carryover. Across 67 queries with 2 inter-page pauses each, it forces **268 worker-seconds of idle sleep** per run.

---

## Detailed Trace of the 429 Path and `page_token` State Machine

Below is the line-by-line state trace of variables `page`, `page_token`, `body`, and the HTTP request flow during 429 rate-limiting events.

### Trace 1: Page 0 (Initial Query) Hits 429

```
Initial State: query = "restaurants in Lebanon", page_token = None, page = 0

1. Line 203: while True:
2. Line 204:   body = {"textQuery": "restaurants in Lebanon"}
3. Line 205:   if page_token:  --> False (page_token is None)
4. Line 209:   resp = session.post(API_URL, json={"textQuery": "restaurants in Lebanon"}, timeout=30)
5.             --> Server returns HTTP 429 (Too Many Requests / RESOURCE_EXHAUSTED)
6. Line 213:   if resp.status_code == 429:  --> True
7. Line 214:     print("[Google] Rate limited on 'restaurants in Lebanon', waiting 30s...")
8. Line 215:     time.sleep(30)
9. Line 216:     continue  -------------------------------------------------------------+
                                                                                         |
[Loop restarts at Line 203]                                                              |
State across continue: page_token = None (UNTOUCHED), page = 0 (UNTOUCHED) <-------------+
10. Line 204:  body = {"textQuery": "restaurants in Lebanon"}
11. Line 205:  if page_token:  --> False
12. Line 209:  session.post(API_URL, json={"textQuery": "restaurants in Lebanon"})
```
- **Outcome:** If transient, the request succeeds and page 1 is retrieved. If permanent (quota exhaustion), it repeats steps 1–12 forever.

### Trace 2: Page 2 (Paginated Call) Hits 429

```
State after Page 1 succeeds:
  page = 1, records = [20 items], page_token = "tok_ABC123"
  time.sleep(2) completes.

1. Line 203: while True:
2. Line 204:   body = {"textQuery": "restaurants in Lebanon"}
3. Line 205:   if page_token:  --> True ("tok_ABC123")
4. Line 206:     body["pageToken"] = "tok_ABC123"
5. Line 209:   resp = session.post(API_URL, json=body, timeout=30)
6.             --> Server returns HTTP 429
7. Line 213:   if resp.status_code == 429:  --> True
8. Line 214:     print("[Google] Rate limited on 'restaurants in Lebanon', waiting 30s...")
9. Line 215:     time.sleep(30)
10. Line 216:    continue  -------------------------------------------------------------+
                                                                                         |
[Loop restarts at Line 203]                                                              |
State across continue:                                                                   |
  - page_token = "tok_ABC123" (STILL IDENTICAL; Line 275 was never executed) <----------+
  - page = 1 (NOT incremented; Line 224 was never executed)
  - records = [20 items] (unaltered)
11. Line 204:  body = {"textQuery": "restaurants in Lebanon"}
12. Line 205:  if page_token:  --> True ("tok_ABC123")
13. Line 206:    body["pageToken"] = "tok_ABC123"
14. Line 209:  session.post(API_URL, json={"textQuery": ..., "pageToken": "tok_ABC123"})
```

### Trace 3: Ephemeral Token Expiry after 429 Retry (The Silent Data Loss Path)

```
Following Trace 2 above, after 30s sleep:
15. Line 209:  session.post(...) with "pageToken": "tok_ABC123"
16.            --> Google server: token "tok_ABC123" has expired or is invalid
17.            --> Server returns HTTP 400 Bad Request:
                   {"error": {"code": 400, "message": "The provided page token is invalid or has expired."}}
18. Line 210:  if resp.status_code == 401:  --> False
19. Line 213:  if resp.status_code == 429:  --> False (Status is 400, NOT 429)
20. Line 217:  resp.raise_for_status()      --> RAISES requests.exceptions.HTTPError (400 Client Error)
21. Line 218:  except requests.RequestException as e:
22. Line 219:    print("[Google] Request error for 'restaurants in Lebanon': 400 Client Error...")
23. Line 220:    return records  --> ABORTS QUERY EARLY
```
- **Consequence:** The scraper yields only the 20 records from Page 1. Pages 2 and 3 (up to 40 records) are completely lost without any indication to downstream orchestrators.

---

## Not a bug, but worth knowing

- **Ceiling on lead generation scale:** Because Text Search (New) hard-caps results at 60 per query string, 67 queries can **never produce more than 4,020 raw records** per run. After category filtering (`whitelist.py`) and deduplication (`dedup.py`), the net yield is ~2,000–2,500 unique businesses. Scaling to 10k or 100k leads cannot be done by paginating further; it requires generating hundreds of sub-locality/neighborhood queries or moving to Nearby Search with geographic grid tiling.
- **In-memory deduplication only:** `seen_ids` (`google_places.py:170`) is an in-memory set that is re-initialized on every execution. The scraper has no persistence of previously scraped Google Place IDs. A daily run would repeatedly spend $7.04/day fetching the exact same places over and over.
- **Rating fields trigger Enterprise tier for minimal value:** `places.rating` and `places.userRatingCount` are only used downstream in `enricher.py:253` to grant a +1 bonus to `lead_score` if `review_count < 10`. However, dropping them would not reduce the API cost because `places.websiteUri` and `places.nationalPhoneNumber` also independently trigger the Enterprise tier.
- **Immediate validity of page tokens:** The 2-second sleep at line 278 can safely be reduced to 0.2s or removed entirely in Places API (New), saving ~50 seconds of wall-clock run time without triggering rate limits under 5 workers.

---

## Recommended order of work

1. **Add bounded retries and exponential backoff to `_scrape_query`** (`scrapers/google_places.py:213-216`): Cap retries at 3 attempts with progressive backoff (e.g. 5s, 10s, 20s). Abort the query with a clean error log if quota is exhausted to prevent 5-hour CI workflow hangs.
2. **Mount a standard `HTTPAdapter` with `urllib3.util.Retry`** (`scrapers/google_places.py:148-155`): Provide symmetrical retry behavior across 429, 500, 502, 503, 504, and network read/connection timeouts.
3. **Enforce defensive client-side pagination limit** (`scrapers/google_places.py:203`): Guard the loop with `if page >= 3 or not page_token: return records`.
4. **Reduce or remove the cargo-cult 2-second sleep** (`scrapers/google_places.py:278`): Reduce `time.sleep(2)` to `time.sleep(0.2)` to shave 45–50 seconds off total scrape duration.
5. **Add cross-run Place ID persistence** if execution frequency is ever increased beyond monthly, avoiding redundant API charges for unchanged business records.
