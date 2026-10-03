# 064 — One Bounded Retry and Backoff Policy for Outbound HTTP

## Verdict

HTTP resilience is inconsistent: OSM and Wikidata retry every `RequestException` with fixed sleeps, Google Places retries 429 forever, and website enrichment never retries. Replace those paths with one synchronous, requests-based retry helper using capped exponential full jitter, `Retry-After`, operation-specific retry safety, and shared per-host/global retry budgets. In particular, exhausted website 5xx/429 responses must remain `UNKNOWN`, not become a false “dead site” sales signal.

## Findings

### S1 — Google Places can retry forever; other callers silently give up on transient failures

- **Where:** `scrapers/google_places.py:203-220`; `scrapers/osm.py:38-53`; `scrapers/wikidata.py:40-56`
- **Breaks:** Google Places handles 429 by sleeping 30 seconds and continuing an unbounded `while True` (`google_places.py:213-216`), including permanent quota exhaustion. The OSM and Wikidata loops allow three total attempts, but sleep fixed 30/15 seconds and retry every `RequestException`, including permanent client errors (`osm.py:38-53`, `wikidata.py:40-56`). Each uses different behavior and neither observes `Retry-After`. A temporary network failure on Google aborts that query immediately (`google_places.py:218-220`).
- **Trigger:** Google project quota is exhausted and every request returns 429: all five query workers can remain in the loop until the workflow's 300-minute timeout. Conversely, one short DNS/connectivity interruption aborts an entire Google query.
- **Fix:** Route all request attempts through one shared retry helper; bound retries and total wait, retry only transient failures, and return/raise the final failure to the existing caller.

### S2 — Website checks turn transient server failures into “dead” leads and never retry

- **Where:** `enricher.py:158-189`, especially `167-175`; fan-out is `enricher.py:217-240`
- **Breaks:** `_fetch_website()` makes one GET and catches any exception as `UNKNOWN` (`168-170`). A timeout/reset therefore loses a recoverable liveness/contact result. Conversely, a 429 or temporary 5xx is immediately classified as `DEAD` at `173-176`; `lead_score()` awards dead sites +20 at `enricher.py:289-296`, so rate limiting or a short outage can create a false rebuild lead. Forty workers can also retry/fail against shared hosts without a coordinated host limit.
- **Trigger:** A site returns 503 once and then 200, or returns 429 while the 40-worker sweep is active. The former is incorrectly recorded dead; the latter is both incorrectly dead and receives no recovery attempt.
- **Fix:** Retry safe GETs for transient statuses/transport errors within shared budgets. After exhaustion, map retryable statuses (429 and 5xx) to `UNKNOWN`; reserve `DEAD` for terminal site responses such as 404/410 and other explicitly chosen non-retryable 4xx.

### S2 — Retries need both a per-host politeness limit and a run-wide failure budget

- **Where:** `scrapers/google_places.py:138-143, 175-186`; `enricher.py:217-234`; `main.py:154-168`
- **Breaks:** The existing Google concurrency is five workers and enrichment is forty (`google_places.py:141`, `enricher.py:229-230`); OSM, Wikidata, and Google also run concurrently (`main.py:154-168`). A per-request cap alone still permits many workers to retry one host at once, while a host-only limit still permits a broad outage to add retries across thousands of website hosts. Retries consume API quota/billing and run time as well as load.
- **Trigger:** A shared provider/host starts returning 429/503 across parallel workers, or widespread network loss makes a large website sweep retry every record.
- **Fix:** Before every retry, require both a per-normalized-host retry token and a process-wide run token; if either is exhausted, stop retrying that request (do not queue an unbounded backlog).

### S3 — The retry-safe boundary must account for POST semantics and Google billing

- **Where:** `scrapers/osm.py:40-45`; `scrapers/google_places.py:203-219`; `scrapers/wikidata.py:42-47`; `enricher.py:167`
- **Breaks:** HTTP verb alone is not enough to decide retry safety. OSM sends a read-only Overpass query in a POST, and Google sends read-only search requests in POST, so replaying does not mutate lead data; however, a repeated Places request can consume quota / incur billing again. Treating every future POST as retryable would be unsafe if write endpoints are added.
- **Trigger:** A response is lost after Google processed a search request. The caller cannot know whether the server processed it, and a retry can bill a second request even though the returned data is deduplicated later.
- **Fix:** Make retry safety explicit at each call site (`retry_safe=True` only for these read-only queries/GETs); keep attempts especially tight for billed Places calls, and never retry a future side-effecting POST without an idempotency key or API-specific guarantee.

## Concrete implementation

Add a small shared module, for example `http_retry.py`, using only stdlib plus the already-pinned `requests`. Use an explicit helper rather than mounting `urllib3.Retry`: hidden adapter retries make total attempts, budgets, and `Retry-After` behavior hard to audit. Suggested contract:

```python
def request_with_retry(
    session, method, url, *, retry_safe: bool, policy, budget, **kwargs
):
    # max_attempts counts the first request (not retries).
    deadline = time.monotonic() + policy.max_wait_seconds
    last_error = None
    for attempt in range(policy.max_attempts):
        response = None
        try:
            response = session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
            if not retry_safe or attempt + 1 == policy.max_attempts:
                raise
            retry_after = None
        else:
            if response.status_code not in policy.retry_statuses:
                return response
            if not retry_safe or attempt + 1 == policy.max_attempts:
                return response  # let caller classify/raise the final response
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            response.close()  # release the connection before sleeping

        # Full jitter: U(0, min(cap, base * 2**attempt)).
        delay = random.uniform(0, min(policy.cap_seconds,
                                      policy.base_seconds * 2**attempt))
        if retry_after is not None:
            delay = max(delay, retry_after)  # never retry earlier than requested
        if (time.monotonic() + delay > deadline) or not budget.take(url):
            if response is not None:
                return response  # final retryable response; caller handles it
            raise last_error  # preserve the transport failure
        time.sleep(delay)
```

Implementation details the helper must preserve:

1. Retry **only** `requests.RequestException` transport failures and HTTP **408, 429, 500, 502, 503, 504**. Do not retry other 4xx, 501/505, or a successful response whose JSON/data is malformed. Do not retry at all when `retry_safe=False`.
2. `parse_retry_after` accepts both non-negative delta-seconds and HTTP-date (using `email.utils.parsedate_to_datetime`); convert dates to a delay against UTC now, and treat invalid/past values as absent/zero. Use a monotonic deadline for elapsed retry time. If a valid `Retry-After` exceeds `max_wait_seconds`, stop and surface the 429/503 response rather than retrying earlier than requested or sleeping for an unbounded period.
3. Use `max_attempts=4` (initial + at most three retries) for OSM, Wikidata and website GETs; use `max_attempts=3` (initial + at most two retries) for Google Places because requests are metered. Shared defaults: `base_seconds=1`, exponential cap `30` seconds, and `max_wait_seconds=120` per call. The delay before retry *n* is uniform from zero to `min(30, 1 * 2**n)`; apply `max(jitter, Retry-After)` when the header exists.
4. Use a thread-safe `RetryBudget` shared by the process. Key per-host buckets by normalized URL hostname (lowercase/IDNA, trailing dot removed; include port when non-default): capacity **4 retries**, refill **one token per 15 seconds**. Add a process-wide bucket with capacity **100 retries per run**, replenishing **one token per 10 successful initial requests** up to 100. A retry needs a token from both buckets; if either is empty, stop this request's retries. This bounds repeated pressure on one shared host and prevents a widespread outage from multiplying the whole run's traffic. Keep request concurrency limits as separate controls; tokens limit retries, not initial requests.
5. Make budgets/policies injectable for unit tests and log a structured, non-secret event for each retry: host, status/exception class, attempt number, chosen delay, and stop reason. Never log full URLs with query strings or Google API credentials.

`Retry-After` parsing sketch:

```python
def parse_retry_after(value):
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            date = email.utils.parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
```

### Retry-safety matrix

| Call | Safe to replay? | Policy / terminal behavior |
|---|---|---|
| Overpass query POST (`osm.py:40-45`) | Yes, this POST is a read-only query; it has no data mutation. | Four attempts total; on exhausted retryable HTTP response, log and skip the OSM batch as today. Do not retry parsing/schema errors. |
| Wikidata SPARQL GET (`wikidata.py:42-47`) | Yes; GET is read-only. | Four attempts total; on exhaustion, log and skip batch as today. |
| Google Places `searchText` POST, including same `pageToken` (`google_places.py:203-219`) | Yes for result-state semantics (read-only search), **not exactly-once for quota/billing**. | Three attempts total. Retry only transient errors/statuses; 401 and other terminal 4xx abort that query immediately. On exhausted 429/5xx/transport error, stop that query and retain its already-collected records. Keep the existing two-second pagination pacing separate from retries unless separately validated. |
| Business website GET (`enricher.py:167`) | Yes under GET semantics; it only reads the page. | Four attempts total. 404/410 and selected other terminal 4xx remain `DEAD`; exhausted 408/429/5xx and transport/body failures are `UNKNOWN`, never “dead” solely because the retry budget ended. |

## Existing call sites to change

1. **`scrapers/osm.py:1-4, 38-53`:** remove the local `time` import and three-attempt loop; call the shared helper for `session` + Overpass POST with the OSM policy and `retry_safe=True`. Keep the existing `raise_for_status`/JSON-to-record flow for the returned successful response; preserve “skip batch” behavior after exhausted errors.
2. **`scrapers/wikidata.py:1-4, 40-56`:** remove its local sleep loop and `time` import; use the same helper for SPARQL GET with its policy and `retry_safe=True`; preserve the current skip-on-exhaustion behavior.
3. **`scrapers/google_places.py:17-24, 203-220`:** replace the inline 429 sleep/`continue` and request exception handling with the helper around each `session.post`. This removes the unbounded 429 loop and adds bounded transport/5xx retry. Keep 401 handling as an immediate terminal error and preserve partial `records` when a page finally fails. The `time.sleep(2)` at line 278 is pagination pacing, not a retry loop; leave it outside this change.
4. **`enricher.py:2-4, 124-125, 158-189`:** wrap `_SESSION.get` (or the selected per-thread session) with the same helper, `retry_safe=True`, and the website policy. Apply the response classification only after helper exhaustion: successful 2xx -> parse; terminal dead-site status -> `DEAD`; exhausted transient status/transport/body-read failure -> `UNKNOWN`. The helper must close discarded streamed responses before retry. Do not retry just the body-read step by issuing a second independent, unbounded read; if body download errors are to be retried, make that a bounded full GET retry under the same policy.
5. **`main.py:154-168`:** no call-site rewrite is required. Its existing parallel scraper orchestration is why the budget object must be process-shared and thread-safe; initialize one run budget before starting the scraper/enrichment work and pass it through the HTTP call paths (or expose one shared default created at import/run setup).

## Not a bug, but worth knowing

- A retry policy only governs retries, not the existing five-worker Places limit, forty-worker enrichment pool, redirect safety, TLS verification, or total run deadline; those remain independent controls.
- A repeated Places search can return the same result data but still consume a second billable request. Result idempotency is not billing idempotency.
- A 2-second pause between Places pages (`google_places.py:278`) is not a retry and should not be conflated with backoff; only retain/change it under a separate pagination/quota decision.

## Recommended order of work

1. Implement and test `Retry-After` parsing, full-jitter delay bounds, status/exception classification, attempt caps, and concurrent budget consumption in the shared helper using mocked responses (no live API tests).
2. Integrate OSM and Wikidata first, then Places; verify 429 and transport-failure paths terminate within attempt/wait bounds and preserve partial query output.
3. Integrate enrichment and explicitly test that retryable 5xx/429 exhaustion is `UNKNOWN`, whereas terminal 404/410 is `DEAD` and a recovered 503->200 is `LIVE`.
4. Run the same mocked concurrency tests across multiple workers to prove one host bucket and the global run bucket are shared, and record retry/stop metrics without logging credentials or full URLs.
