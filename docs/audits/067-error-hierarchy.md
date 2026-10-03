# 067 — Error Hierarchy and Failure Policy

## Verdict

The scraper layer currently turns several materially different outcomes—temporary network faults, exhausted retries, invalid credentials, malformed responses, and bad records—into a print plus an empty or partial result. `main()` then catches scraper exceptions and still writes the five CSVs, so a source can disappear without changing the apparent run outcome. Introduce a small typed error hierarchy, make retry and degradation decisions at the operation boundary, and persist every handled failure in a run manifest; keep website-check failures as an explicit `unknown` outcome rather than treating them as evidence that a business site is dead.

## Findings

### S1 — Source failures are converted into successful-looking partial runs

- **Where:** `scrapers/osm.py:38-53`; `scrapers/wikidata.py:40-56`; `scrapers/google_places.py:157-160,208-220`; `main.py:160-168,208-213`
- **Breaks:** OSM and Wikidata return no records after exhausting retries. Google returns empty or partial records for missing credentials, 401, and request exceptions. At the outer boundary, `main()` catches any escaping exception, prints it, and continues writing the normal CSV set. There is no source status or failure record to distinguish “source returned zero businesses” from “source never worked.” If all three fail, the existing master can still be rewritten and presented as a successful fresh run.
- **Trigger:** The Google key is revoked (401), Wikidata times out on all attempts, and Overpass is unavailable; `raw` remains empty, but `main()` proceeds through deduplication and emits the ordinary summary and CSVs.
- **Fix:** Have scraper/query boundaries raise typed errors and return explicit success/partial/failure outcomes; aggregate those in orchestration, write a manifest for every run, mark partial-source runs `partial`, and fail the run when no source completed or a required pipeline stage fails. Preserve existing outputs but do not promote a failed/partial run as a clean refresh.

### S1 — Google rate-limit handling has no stop condition

- **Where:** `scrapers/google_places.py:203-220`
- **Breaks:** A 429 sleeps for 30 seconds and continues the same `while True` loop without an attempt limit, `Retry-After` handling, or deadline. A permanently throttled request can occupy a worker indefinitely until the five-hour workflow timeout, while no typed rate-limit event is available to diagnose the run.
- **Trigger:** Google returns 429 on every page attempt for a query, including after the 30-second sleep.
- **Fix:** Raise `RateLimitError` for 429, retry only within a bounded per-request/query budget using `Retry-After` or capped exponential backoff with jitter, and record exhaustion as a failed query while allowing other queries to finish.

### S2 — Response parsing and record validation are not classified

- **Where:** `scrapers/osm.py:55-60`; `scrapers/wikidata.py:58-64`; `scrapers/google_places.py:222-247`
- **Breaks:** `resp.json()` and subsequent `.get()`/nested accesses assume valid JSON with the expected object/list shapes. Invalid JSON, an HTML proxy page with HTTP 200, or a structurally changed API response escapes as an untyped exception (or can fail partway through iteration). No boundary checks guarantee that emitted records satisfy even the minimal business-record contract. The parent may only see a generic future exception and lose any records yielded before the failure.
- **Trigger:** Overpass responds `200 text/html`, Wikidata returns JSON without `results.bindings`, or a Google `places` entry has a non-object `displayName`.
- **Fix:** Translate JSON decoding and envelope-shape failures to `ParseError`; validate each source item before constructing/yielding a `BusinessRecord`, count and skip invalid individual items, and treat an invalid response envelope as a failed query (not an empty successful result).

### S2 — Website enrichment deliberately degrades, but discards failure cause

- **Where:** `enricher.py:158-184,229-240`
- **Breaks:** `_fetch_website()` catches every exception around the request and body read and returns `UNKNOWN`; `check_websites()` catches another broad exception and also maps it to `UNKNOWN`. This correctly avoids falsely labelling an unreachable site as dead, but hides whether the run encountered a timeout, DNS/TLS failure, read error, or unexpected bug. It also makes the manifest unable to distinguish a normal blocked site from an implementation failure.
- **Trigger:** A business site times out, or a future bug in `_fetch_website()` raises `AttributeError`; both become the same `website_live=None` with no structured explanation.
- **Fix:** Catch and classify expected per-site transport/body errors, retain the `UNKNOWN` business outcome, and aggregate their typed error codes/counts; record unexpected exceptions as internal errors without including raw business data in logs.

## Proposed exception contract

Keep this in a small shared module (for example `errors.py`; a later packaging change may place it under the application package). All domain exceptions should carry `source`, `operation`, stable `code`, `retryable`, and `scope` (`request`, `query`, `source`, `record`, or `run`); chain the original exception with `raise ... from exc`. Messages must be sanitized and must not contain API keys, full response bodies, or business contact data. Do not infer classification by matching exception text.

```text
PipelineError
├── TransportError       # DNS, connect/read timeout, reset, truncated response, retryable
├── RateLimitError       # HTTP 429 / explicit quota throttle, retryable within a budget
├── AuthenticationError  # confirmed missing/invalid credentials or permission, not retryable
├── ParseError           # response cannot be decoded / expected envelope is malformed
└── ValidationError      # decoded value violates source or BusinessRecord invariants
```

`PipelineError` is the common serializable base, not a blanket “retry everything” signal. Include `status_code` and `attempt` when known, and a stable source/operation identifier. For record-level `ValidationError`, include a safe field path and reason, not the record payload. A small `RunFatalError` may wrap a fatal stage failure, but should preserve the underlying typed cause rather than replace it.

### Retry and disposition policy

| Failure | Retry? | Scope and disposition |
|---|---|---|
| `TransportError` on an idempotent source read | Yes, bounded (e.g. 3 total attempts, capped exponential backoff + jitter) | Retry the request. OSM POST is a read-only Overpass query; Google Places POST is a read-only search, so both are safe to retry. After exhaustion, fail that query/source and continue independent work. |
| `RateLimitError` | Yes, bounded; honor valid `Retry-After`, otherwise capped backoff | Stop retrying at the query deadline/budget. Mark the query failed; never spin indefinitely. Rate limits should not be counted as authentication failures. |
| `AuthenticationError` | No | Fatal for that source (e.g. Google API key rejected). Cancel/avoid redundant in-flight queries when practical, record one source-level error, and let other sources run. The overall run is `partial` if another source completed; it is `failed` if none did. |
| `ParseError` | No by default | Fail the affected response/query and continue other queries/sources. A malformed HTTP 200 response is not a successful empty result. A later explicit policy may permit one retry for a known transient upstream corruption, but keep it bounded and visible. |
| `ValidationError` on one result item | No | Skip that item, increment rejected-record counts, and continue the response. If the top-level envelope or a required batch invariant is invalid, fail the query instead of accepting zero rows. |
| Website transport/body-read failure | Optional bounded retry; then degrade | Preserve website outcome as `unknown` (`website_live=None`), do not reduce the lead score as if the site were confirmed dead, and record the error in the run manifest. A valid HTTP 4xx/5xx remains a successful check with `website_live=False`, not a transport exception. |
| Master CSV read, output write, or other required-stage failure | No automatic retry except narrowly safe filesystem retry | Fatal run failure; do not claim success or replace canonical outputs. Still attempt an atomic failure manifest. |

HTTP mapping belongs immediately after receiving a response and before generic `raise_for_status()` handling: 429 → `RateLimitError`; confirmed invalid key/credentials (Google 401 and API-specific auth/permission 403 reasons) → `AuthenticationError`; transient 5xx and request transport exceptions → `TransportError`; other 4xx are non-retryable request/source errors and should be recorded as such rather than mislabelled auth. A generic 403 is not automatically an auth error: distinguish API credential reasons from upstream policy/WAF denial. For OSM/Wikidata, ordinary 4xx query rejection is a failed query, not a reason to loop.

### Raise/catch boundaries

1. **HTTP adapters in `scrapers/osm.py:38-55`, `wikidata.py:40-59`, and `google_places.py:203-222`:** translate `requests` connect/read exceptions and retryable 5xx responses to `TransportError`; map 429 and confirmed credential failures as above. Retry in this layer, so retry count and delay have one owner. Do not catch the typed error and return an empty iterator.
2. **Response decoders in those same scrapers:** wrap JSON decoding errors as `ParseError`; explicitly assert root and expected collection shapes (`elements`, `results.bindings`, `places`). Validate individual elements/places before record construction. Missing optional business attributes remain `None`; missing/blank required `name` is a rejected record, not a fabricated record. Expected filtering (for example Wikidata entity-ID labels currently skipped at `wikidata.py:62-64`) should be counted as a filter, not mislabeled an exception.
3. **Concurrent Google query workers in `google_places.py:175-188`:** collect a result/error per query rather than allowing `f.result()` to abort the whole source and discard completed query results. A confirmed source-wide auth failure should be aggregated once; malformed or transient query failures should not suppress valid sibling query results.
4. **Scraper orchestration in `main.py:157-168`:** catch `PipelineError` per scraper, attach the source and stage, and record its disposition. Catch unexpected `Exception` only at this outer safety boundary as `internal.unexpected`/run failure with traceback in protected diagnostics; never silently turn it into a successful empty batch. After all sources settle, continue transformations only if policy permits and set the run status accurately.
5. **Website checks in `enricher.py:158-189,229-240`:** convert expected request/read problems to a typed per-target error and `UNKNOWN` result at the worker boundary; aggregate counts by code. The run should continue. A response status (including 4xx/5xx) is a liveness result, not a transport failure. A body-read failure after a valid status can preserve known liveness while omitting contact extraction; record the extraction degradation separately.
6. **Pipeline/data boundaries in `main.py:77-105,108-133,208-213`:** invalid existing master CSV/schema and failed atomic CSV writes are run-fatal data-integrity errors. Ensure the manifest writer runs from a `finally`/top-level failure path and does not mask the original failure.

### Run-manifest recording

Write one versioned, atomic JSON manifest per invocation, e.g. `data/run_manifest_<run_id>.json`; keep it distinct from the five business CSVs. It should exist for success, partial, and failed runs, and the CI upload should retain it even when canonical CSV promotion is blocked. Minimum useful schema:

```json
{
  "schema_version": 1,
  "run_id": "<unique id>",
  "started_at": "<UTC ISO-8601>",
  "finished_at": "<UTC ISO-8601>",
  "status": "success | partial | failed",
  "sources": {
    "osm": {"status": "success | partial | failed | skipped", "attempted": 1,
      "succeeded": 1, "queries_failed": 0, "records_yielded": 0,
      "records_rejected": 0, "errors": []},
    "wikidata": {},
    "google_places": {"status": "partial", "queries_total": 67,
      "queries_succeeded": 66, "queries_failed": 1, "records_yielded": 0,
      "records_rejected": 0, "errors": []}
  },
  "degradations": {"website_checks_unknown": 0, "contact_extraction_failed": 0},
  "errors": [{"stage": "scrape", "source": "google_places",
    "operation": "query", "code": "rate_limit.exhausted",
    "exception": "RateLimitError", "retryable": true,
    "attempts": 3, "disposition": "query_failed_continue",
    "status_code": 429, "message": "Rate limit retry budget exhausted"}]
}
```

Each manifest error records classification and final decision, not just exception text: timestamp, stage, source, operation/query ID, exception class, stable code, retryable, attempt count, HTTP status if any, and disposition (`retried`, `query_failed_continue`, `source_failed_continue`, `record_skipped`, `degraded_unknown`, or `run_failed`). Store counters as well as errors so repetitive website failures can be summarized by code rather than creating an unbounded entry per target. Cap sampled error details and omit API secrets, full response bodies, full website URLs, and business PII. Record query names only where safe; otherwise use a stable query index/hash.

Recommended status rules: `success` means all configured sources completed without source/query errors (website `unknown` outcomes may still be successful checks but appear under degradations); `partial` means at least one source produced a valid completion and another source/query failed, was unavailable, or degraded; `failed` means no source completed or a required master/transform/write stage failed. A legitimately empty source result is not an error by itself—record zero yielded rows and successful completion. This makes source health observable without turning every empty search or blocked business website into a fatal run.

## Not a bug, but worth knowing

- `enricher.py:145-150,172-176` already encodes the important product rule that only a server-confirmed 4xx/5xx is `DEAD`; DNS/TLS/timeouts are `UNKNOWN`, not rebuild leads. Preserve this distinction while adding error reporting.
- A `403` can mean invalid API permissions, exhausted project quota, or upstream blocking depending on the provider response. Keep provider-specific response-code parsing at the API boundary and avoid one global HTTP-status-to-exception table.
- A zero-row successful response is valid; fail based on source/query health and contract validation, not a hard-coded positive yield requirement.

## Recommended order of work

1. Add shared `PipelineError`, `TransportError`, `RateLimitError`, `AuthenticationError`, `ParseError`, and `ValidationError` types with safe structured fields and source-specific HTTP mapping.
2. Replace unbounded/implicit retry loops and silent returns in the three scrapers with bounded retries plus typed exhaustion; make Google query workers preserve successful sibling results.
3. Add payload/record boundary validation and typed per-item rejection; preserve website `UNKNOWN` behavior while recording expected transport failures and unexpected worker errors distinctly.
4. Update `main()` to aggregate source results/errors into success/partial/failed status and implement explicit no-source/required-stage failure policy.
5. Atomically emit the versioned per-run manifest on every exit path; have the workflow archive it and gate canonical CSV upload/promotion on run status.
