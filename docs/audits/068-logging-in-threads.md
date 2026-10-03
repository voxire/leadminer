# 068 — Thread-Safe Logging Architecture for Concurrent Scraping & Enrichment Pools

## Verdict

The `leadminer` pipeline runs 40 concurrent HTTP enrichment threads and 5 concurrent Google Places query workers, yet relies entirely on 57 unsynchronized `print()` statements that interleave mid-string, drop execution context, and lack correlation identifiers (`run_id`). Crucially, because Python's `ThreadPoolExecutor` does not propagate `contextvars` to worker threads by default, naive context variable usage silently fails, leaving worker log lines detached from originating runs, scraper sources, and queries. Furthermore, because target records represent real Lebanese and Saudi small businesses (dental clinics, sole-proprietor law firms, boutiques, and personal consulting practices), logging raw contact dictionaries, exception payloads, or URLs leaks personal mobile numbers (`+961 70...`, `+966 50...`), personal emails, and direct WhatsApp links directly into public or persistent GitHub Actions CI logs in violation of the Saudi Personal Data Protection Law (PDPL) and international data privacy norms. Replacing this with a dedicated, context-propagating logging architecture—featuring a `ContextThreadPoolExecutor`, dual-mode formatting (structured NDJSON for CI/automation versus human-readable console output for local TTY), rigorous pipeline-stage level controls, and a zero-bypass PII redaction filter—is essential for production stability and legal compliance.

---

## Findings

### S1 — Standard `ThreadPoolExecutor` Drops `ContextVar` State Across Worker Threads Without Explicit Context Propagation
- **Where:** `enricher.py:229-232`; `scrapers/google_places.py:183-186`; `main.py:160-163`
- **Breaks:** In Python 3.7+, `contextvars` provide context-local storage. However, standard library `concurrent.futures.ThreadPoolExecutor.submit()` runs submitted callables in worker threads without copying or inheriting the caller's context. Any `run_id`, `stage`, `source`, or `worker_id` set in the orchestrating thread resolves to default/empty values inside all 40 enricher threads and 5 Places workers. Furthermore, if a worker thread sets a context variable internally during task execution, thread reuse across the pool causes context contamination: when worker thread 3 finishes processing Place query `"dentists in Lebanon"` and subsequently picks up query `"cafes in Riyadh"`, any unreset context variable retains the previous task's attributes, causing catastrophic log misattribution across threads.
- **Trigger:** Calling `run_id_var.set("run_20261003_01")` in `main.py`, submitting a fetch task in `enricher.py:230`, and reading `run_id_var.get()` inside `_fetch_website()`: the worker thread returns `""` (the default value), producing disconnected logs across all 40 worker threads.
- **Fix:** Subclass `ThreadPoolExecutor` into a context-propagating executor (`ContextThreadPoolExecutor`) that captures `contextvars.copy_context()` at task submission time and invokes worker callables via `ctx.run(fn, *args, **kwargs)`, ensuring isolated inheritance and preventing task-to-task context leakage.

---

### S1 — Unredacted Business-Contact PII (Personal Mobiles, Direct WhatsApp Links, and Personal Emails) Leaks into Persistent CI Logs
- **Where:** `enricher.py:191-213, 244-251`; `scrapers/google_places.py:218-220, 225`; `main.py:168`
- **Breaks:** Target businesses in Lebanon and Saudi Arabia are overwhelmingly micro-enterprises and professional practices (physicians, dentists, solo attorneys, local retail). Their public listings do not contain corporate call-center numbers; they contain personal mobile numbers (`+961 70/71...`, `+966 50/54/55...`), personal email addresses (`dr.hassan@gmail.com`), and direct WhatsApp click-to-chat links (`wa.me/966...`). Unhandled HTTP exceptions (`requests.RequestException`), debug dumps of record dictionaries (`r`), or raw query logging in CI/CD environments commit unmasked personal contact details to GitHub Actions runner logs, which are retained for 90 days. Under the Saudi Personal Data Protection Law (PDPL, Royal Decree No. M/19) and global data protection standards, persisting unencrypted, unredacted personal contact data in operational CI logs without purpose limitation constitutes a severe compliance violation.
- **Trigger:** An SSL or DNS connection failure while enriching a website containing personal contact parameters (e.g. `https://dr-hassan.com/contact?email=dr.hassan@gmail.com&phone=+966501234567`), or an unhandled exception printing `record` dictionary contents in `enricher.py:236` or `main.py:168`.
- **Fix:** Implement a centralized `PiiRedactionFilter` attached to the root logging handler that executes deterministic regex-based masking on phone numbers, email user parts, API keys (`AIza...`), and query parameters before log records reach formatters or emission streams.

---

### S2 — Unsynchronized Multi-Thread Stdout Writes Cause Buffer Interleaving Across 40 Enrichment and 5 Places Workers
- **Where:** `enricher.py:227, 254, 263-267`; `scrapers/google_places.py:166, 211-225`; `main.py:165, 168`
- **Breaks:** Python's built-in `print()` function is not thread-safe with respect to atomic stream writes when formatting strings or when stdout buffering is active. With 40 threads running concurrently in `enricher.py` and 5 threads in `google_places.py`, concurrent stdout writes interleave character buffers mid-line (e.g. `[Google] 'gyms in Leb[Enricher] 200/400 done...anon' page 1: 20 results`). Error traces written to `stderr` in `main.py:168` collide with standard output lines from `google_places.py:225`. Interleaved lines corrupt downstream log aggregators, break JSON parsing, and render terminal output illegible during debugging.
- **Trigger:** 40 threads completing website requests in `enricher.py` while Google Places queries simultaneously emit page completion notices.
- **Fix:** Route all console emissions through Python's standard `logging` library using a shared `StreamHandler` protected by `logging.Handler.createLock()` / `acquire()` re-entrant locks, ensuring that each formatted log record is emitted atomically with a single newline.

---

### S2 — Lack of Scraper-Source & Query Provenance During Enrichment Obscures Root Causes of Failures
- **Where:** `enricher.py:223-230`; `main.py:186-187`
- **Breaks:** In `main.py:179-180`, records from OSM, Wikidata, and Google Places are merged and deduplicated before being passed to `enricher.py`. When `check_websites()` runs across 40 threads, target URLs experience connection timeouts, SSL certificate rejections, and HTTP 403 Cloudflare blocks. Currently, failure logging (or silent swallowing in `enricher.py:168-170`) discards the provenance of the record: operators cannot determine whether 80% of dead websites came from stale 2018 OSM nodes, deprecated Wikidata URLs, or active Google Places results. This blinds engineering to upstream scraper data quality issues.
- **Trigger:** 500 websites fail DNS resolution during enrichment; logs provide no mechanism to group failure rates by `source` (`osm`, `wikidata`, `google_places`).
- **Fix:** Bind `source` and `record_id` into worker thread contextvars during task dispatch in `enricher.py`, including `source` and origin domain in every structured log event.

---

### S3 — Rigid Formatting Mismatch Across Local Development and CI/CD Ingestion Pipelines
- **Where:** `main.py:152, 165, 173, 181, 219-240`; `.github/workflows/scrape.yml:44`
- **Breaks:** Monolithic logging formats force a false choice: emitting raw JSON blobs to an interactive developer terminal produces unreadable noise that degrades developer velocity; conversely, emitting multi-line formatted strings to GitHub Actions prevents automated log parsing, alerting on error rates, or filtering with `jq`. Furthermore, raw terminal formatting in CI lacks ISO-8601 timestamps and thread identifiers, making post-mortem debugging of CI timeout failures impossible.
- **Trigger:** Running `python main.py` in GitHub Actions and attempting to aggregate error rates using `jq`, or running locally and getting flooded with multi-megabyte JSON lines.
- **Fix:** Implement environment-aware format selection: detect interactive TTY sessions via `sys.stderr.isatty()` to render human-readable, colored/compact terminal lines; default to single-line structured NDJSON (JSON-Lines) when running in non-TTY, CI (`CI=true` / `GITHUB_ACTIONS=true`), or headless environments.

---

## Not a bug, but worth knowing

- **Thread Safety of Python's `logging` Module:** Python's standard library `logging` module is inherently thread-safe across handlers: each `Handler` instance manages an `RLock` that serializes writes to underlying I/O streams (`Handler.handle()` acquires the lock before `emit()`). Custom formatters and filters do not need their own locks provided they do not mutate shared global state.
- **High-Frequency Enrichment Log Flooding:** In `enricher.py`, 40 threads processing 5,000 domains at 8-second timeouts can emit 5,000 log events in under 90 seconds. Logging every individual HTTP request at `INFO` level creates excessive console noise. The correct architecture logs batch progress milestones (e.g. every 10% or 250 records) at `INFO`, while relegating individual URL request/response outcomes to `DEBUG`.
- **Worker Thread Naming:** Setting explicit thread names when creating thread pools (e.g., `thread_name_prefix="places-worker"` and `thread_name_prefix="enrich-worker"`) provides an immediate fallback identifier in `threading.current_thread().name` even before application-level `worker_id` contextvars are set.
- **ContextVar Lifetime with `copy_context().run`:** When executing a callable inside `ctx.run(fn, *args, **kwargs)`, mutations to context variables exist solely within that execution. When the function returns, the worker thread's base context remains clean. This prevents context "bleed" between sequential tasks executed by the same pooled thread.

---

## Recommended order of work

1. **Deploy Logging Architecture Module (`leadminer/telemetry/logging.py`)**: Implement `ContextThreadPoolExecutor`, `ContextVar` declarations, `PiiRedactionFilter`, `JsonFormatter`, and `HumanConsoleFormatter`.
2. **Configure Pipeline Entrypoint (`main.py`)**: Initialize logging configuration during bootstrap, generate the run correlation identifier (`run_id`), and bind pipeline execution stage contextvars (`orchestration`, `dedup`, `export`).
3. **Refactor Google Places Scraper (`scrapers/google_places.py`)**: Replace `ThreadPoolExecutor` with `ContextThreadPoolExecutor`, set `thread_name_prefix="places-worker"`, bind query and page contextvars, and replace bare `print()` calls with logger calls adhering to stage level rules.
4. **Refactor Website Enricher (`enricher.py`)**: Replace `ThreadPoolExecutor(max_workers=40)` with `ContextThreadPoolExecutor`, bind target domain and record ID contextvars, instrument HTTP request outcomes at `DEBUG`, and emit batched progress milestones at `INFO`.
5. **Verify PII Sanitization via Tests**: Validate that regex redaction strips telephone numbers, emails, and API keys from log messages and exception tracebacks before production deployment.

---

# Detailed Architectural Specification

```
leadminer/
├── telemetry/
│   ├── __init__.py           # Exports setup_logging, get_logger, ContextThreadPoolExecutor, bind_context
│   └── logging.py            # Complete logging subsystem: ContextVars, filters, formatters, executor
├── main.py                   # Bootstraps logging with run_id, orchestrates stages
├── scrapers/
│   └── google_places.py      # 5-worker Places pool using ContextThreadPoolExecutor + contextvars
└── enricher.py               # 40-thread enrichment pool using ContextThreadPoolExecutor + contextvars
```

---

## 1. Thread-Safe Concurrency & Context Binding

### The `ThreadPoolExecutor` Context Gap
In Python standard library `concurrent.futures`, `ThreadPoolExecutor` creates a pool of reusable worker threads. In Python 3.12, context variables (`contextvars.ContextVar`) are stored in thread-local storage (`PyThreadState`). When a task is queued via `pool.submit(fn, *args)`, the task is placed on an internal `queue.SimpleQueue`. When an idle worker thread pops the task, it executes in that worker thread's distinct context.

By default, **the parent thread's context is not transferred to the worker thread**.

To guarantee that `run_id`, `stage`, `source`, and execution metadata flow seamlessly from `main.py` into both the 5-worker Places pool and the 40-thread Enrichment pool, we provide `ContextThreadPoolExecutor`. This subclass captures `contextvars.copy_context()` at the instant of submission and executes the target function inside that context snapshot via `context.run()`:

```python
class ContextThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor that automatically propagates ContextVars to worker threads."""

    def submit(self, fn, /, *args, **kwargs):
        ctx = contextvars.copy_context()
        return super().submit(ctx.run, fn, *args, **kwargs)
```

### Context Variable Registry

The following context variables are registered globally and injected into every log record by a custom `logging.Filter`:

| ContextVar | Type | Scope | Example Value | Description |
|---|---|---|---|---|
| `run_id_var` | `str` | Process / Run | `run_20261003T143000Z_a7b9` | Canonical monotonic execution correlation ID. Generated at pipeline start. |
| `stage_var` | `str` | Stage | `scrape`, `enrichment`, `dedup` | Current pipeline execution stage. |
| `source_var` | `str` | Component | `google_places`, `enricher`, `osm` | Data source or subsystem emitting the event. |
| `worker_id_var` | `str` | Thread / Worker | `places-01` .. `05`, `enrich-01` .. `40` | Logical worker identifier inside the concurrent pool. |
| `item_id_var` | `str` | Task / Item | `rec_9f83a21e` (hashed ID) | Anonymous, deterministic record hash identifying the lead. |
| `target_domain_var`| `str` | Task / HTTP | `drkhoury-clinic.com` | Clean origin domain being enriched (no paths, no query strings). |
| `query_var` | `str` | Task / Scraper | `dental clinics in Lebanon` | Google Places search query currently being evaluated. |

---

## 2. JSON versus Human-Readable Formatting Decision per Environment

Emitting logs in a single hardcoded format fails operational requirements:
1. **Interactive Development (TTY):** Engineers need clean, scannable terminal output with visible severity colors, timestamps, worker tags, and clear messages without raw JSON noise.
2. **Continuous Integration (GitHub Actions) / Production:** Automated runners require structured, single-line NDJSON (JSON-Lines) so that failures can be ingested, indexed, aggregated, and queried using `jq` or cloud log aggregators without regex parsing.

### Environment Detection Matrix

| Environment | Trigger Condition | Formatter | Destination | Characteristics |
|---|---|---|---|---|
| **Local Interactive** | `sys.stderr.isatty() == True` and `CI != "true"` | `HumanConsoleFormatter` | `stderr` | ANSI colors, ISO-8601 time, `[LEVEL] [worker] [stage] message`, aligned columns, redacted PII. |
| **GitHub Actions / CI** | `os.getenv("CI") == "true"` or `GITHUB_ACTIONS` | `JsonFormatter` | `stderr` | Single-line JSON (NDJSON), RFC-3339 UTC timestamps, full contextvars, error stacks, redacted PII. |
| **Headless / Production** | Non-TTY (`isatty() == False`) or `LOG_FORMAT=json` | `JsonFormatter` | `stderr` / File | Machine-parseable, structured JSON-Lines. |
| **Forensic Run Artifact** | Always configured file sink: `logs/run_<run_id>.jsonl` | `JsonFormatter` | File | Complete debug-level NDJSON audit trail saved to disk and uploaded as a CI artifact. |

### Structured JSON Schema Specification (NDJSON)

Each line emitted by `JsonFormatter` conforms to the following schema:

```json
{
  "timestamp": "2026-10-03T14:32:10.145Z",
  "level": "INFO",
  "logger": "leadminer.enricher",
  "message": "Enrichment fetch completed",
  "run_id": "run_20261003T143000Z_a7b9",
  "stage": "enrichment",
  "source": "website_enricher",
  "worker_id": "enrich-14",
  "thread_name": "enrich-worker_13",
  "target_domain": "beirutdental.com",
  "item_id": "rec_8a7d6e4b",
  "status_code": 200,
  "outcome": "LIVE",
  "duration_ms": 342,
  "contacts_found": {
    "has_email": true,
    "has_instagram": false,
    "has_whatsapp": true,
    "has_linkedin": false
  }
}
```

### Human-Readable Console Format Specification

When running interactively in a terminal, `HumanConsoleFormatter` renders:

```text
14:32:10.145 [INFO ] [enrich-14  ] [enrichment   ] beirutdental.com -> LIVE (200 OK, 342ms) [email=yes wa=yes]
14:32:10.198 [WARN ] [enrich-32  ] [enrichment   ] badhost-example.lb -> DEAD (502 Bad Gateway, 1205ms)
14:32:10.210 [DEBUG] [places-02  ] [google_places] Query 'dental clinics in Lebanon' page 2: 20 places (415ms)
```

---

## 3. Pipeline Stage Logging Matrix & Levels

To prevent 40 concurrent threads from flooding stdout while ensuring critical events are captured, strict logging level boundaries are defined per stage:

| Pipeline Stage | Worker Pool | Level | Allowed Events & Content | Forbidden Events & Content |
|---|---|---|---|---|
| **Bootstrap & Config** | Main Thread | `INFO` | Run ID, environment, target markets, enabled scrapers, worker pool limits. | Raw API keys, raw file system credentials, unmasked environment variables. |
| **Google Places Scrape** | 5 Workers | `INFO` | Scraper start/finish, total queries queued, query rate-limit pauses (429 backoff duration), summary yields. | Full Places API response bodies, unmasked street addresses. |
| | | `DEBUG` | Page fetch execution, query name, page number, place count, request latency (ms), place_id deduplication. | Raw place JSON, personal phone numbers in `nationalPhoneNumber`. |
| | | `WARNING` | HTTP 429 rate limit backoff triggered, HTTP 5xx retry attempts, empty results on known active categories. | Logging the API key header (`X-Goog-Api-Key`). |
| | | `ERROR` | HTTP 401 Unauthorized (invalid key), HTTP 403 (quota exhausted), network connection aborts. | Logging unhandled exception traceback containing HTTP request headers. |
| **OSM & Wikidata Scrape**| 2 Workers | `INFO` | Query submission, HTTP response received, payload size, total raw records extracted. | Raw SPARQL/Overpass XML/JSON response dumps. |
| | | `WARNING` | Endpoint latency exceeding 30s, Overpass gateway queuing delays, retryable 504 Gateway Timeouts. | Plaintext business owner contact fields in raw OSM tags. |
| **Whitelist Filter** | Main Thread | `INFO` | Total records evaluated, retained count, dropped count by broad category. | Dumping full dropped business records. |
| **Deduplication** | Main Thread | `INFO` | Input count, unique businesses output, phone collision merges, name+city collision merges. | Plaintext phone numbers or names used as merge keys. |
| | | `DEBUG` | Exact merge decisions showing field preference resolution using hashed record IDs. | Plaintext phone normalization inputs/outputs. |
| **Enrichment** | 40 Threads | `INFO` | Pool initialization (worker count, target URLs), batch progress updates (every 250 records or 10%), final outcomes. | Logging every single URL fetch at `INFO` (produces 5,000+ lines in 60s). |
| | | `DEBUG` | Individual fetch outcome: domain, status code, outcome (`LIVE`, `DEAD`, `UNKNOWN`), latency (ms), contact booleans. | Full URL paths containing query tokens, scraped HTML body text, raw email/phone values. |
| | | `WARNING` | DNS failures, TLS certificate errors, read timeouts (>8s), Cloudflare 403 challenge blocks. | Dumping raw response bytes or connection error objects containing URLs with sensitive query parameters. |
| | | `ERROR` | Unexpected worker thread exceptions, thread pool executor crashes. | Logging unhandled exceptions with locals (`locals()`) containing raw business records. |
| **Scoring & Pitching** | Main Thread | `INFO` | Distribution of lead scores (0-100), sales-ready count, breakdown by recommended service. | Individual lead record dumps. |
| **CSV Export** | Main Thread | `INFO` | Atomic file write success, destination paths, row counts, execution duration. | Logging full CSV content rows. |

---

## 4. Data Privacy & What Must NEVER Be Logged

### The Threat Model: Real Business Contacts in Lebanon and KSA
In the Lebanese and Saudi B2B landscape, commercial registries and listings for clinics, boutiques, restaurants, auto repair shops, and professional services routinely list:
- The owner's personal mobile phone (e.g., Lebanese Alfa/MTC numbers `+961 3...`, `+961 70...`, `+961 71...`; Saudi STC/Mobily numbers `+966 50...`, `+966 54...`, `+966 55...`).
- Direct personal WhatsApp links (`https://wa.me/966501234567` or `api.whatsapp.com/send?phone=...`).
- Personal email addresses (`dr.fadi.gear@gmail.com`, `boutique.reem@hotmail.com`).
- Personal residential/home-office addresses for sole practitioners.

Once written to `stdout`/`stderr` in GitHub Actions, logs are persisted for 90 days across GitHub servers. They cannot be partially edited; removing leaked PII requires deleting the entire workflow execution history.

### Strict Redaction Invariants

#### 1. NEVER LOG (Forbidden Under Any Circumstance):
- **Raw Phone Numbers:** Neither normalized (`+96170123456`, `+966501234567`) nor raw local formats (`03 123 456`, `050 123 4567`).
- **Raw Email Addresses:** Any address matching `user@domain.tld`.
- **WhatsApp Phone Numbers or Direct Links:** Any URL or string containing `wa.me/<number>` or `whatsapp.com/send`.
- **Sole Proprietor Names Coupled with Contact Data:** (e.g., `"Dr. Joseph Khoury - +9613123456"`).
- **Exact Street Addresses:** Residential villa/building numbers, street details, floor/apartment details.
- **Scraped HTML Response Bodies:** Scraped web pages contain embedded contact forms, personal biographies, hidden input tokens, and tracking scripts. HTML content must never be logged.
- **Full URLs with Query Parameters:** URLs often contain session IDs, authentication tokens, or email addresses (e.g., `?contact=info@...` or `?token=abc`). Only scheme and host domain (`https://example.com`) may be logged.
- **Secret Credentials & API Keys:** Google Places API Key (`GOOGLE_PLACES_API_KEY`), GitHub tokens, and rclone configuration credentials.
- **Raw Exception Dumps with Locals:** `traceback.print_exc()` or `logger.exception()` without automatic payload sanitization, which dumps local variables containing `BusinessRecord` dictionaries.

#### 2. PERMITTED AND ENCOURAGED TO LOG:
- **Hashed Record Identifiers:** Deterministic, non-reversible IDs such as `rec_` followed by an 8-character SHA-256 hex digest of `(name + normalized_phone)`.
- **Sanitized Target Domains:** Registered domain names (e.g., `lebanonhealth.com`, `riyadhboutique.sa`).
- **Contact Presence Boolean Flags:** `{"has_phone": true, "has_email": true, "has_whatsapp": false, "has_instagram": true}`.
- **Masked Contact Previews (when debugging contact parsers):** Masked representations retaining only non-identifying structural digits:
  - Phone: `+966 50 *** **67`
  - Email: `d***n@gmail.com`
- **Aggregated Counts & Metrics:** Number of live sites, count of extracted contacts, request latencies in milliseconds, HTTP status codes (`200`, `404`, `502`).
- **Outcome Taxonomy Enums:** `LIVE`, `DEAD`, `UNKNOWN`.

---

## 5. Complete Configuration Code

The following implementation is self-contained, requires zero third-party dependencies beyond the Python 3.12 standard library, and is designed for direct placement into `leadminer/telemetry/logging.py`.

```python
"""
Thread-safe, context-aware, PII-redacting logging subsystem for leadminer.

Provides:
- ContextThreadPoolExecutor: Propagates contextvars across thread pools.
- ContextVar registry: run_id, stage, source, worker_id, item_id, domain, query.
- PiiRedactionFilter: Automatically redacts phone numbers, emails, and API keys.
- JsonFormatter: Structured NDJSON emission for CI and headless environments.
- HumanConsoleFormatter: Clean, colored, columnar console output for interactive terminals.
- setup_logging: Automatic environment detection and handler configuration.
"""

from __future__ import annotations

import contextvars
import datetime
import hashlib
import json
import logging
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Context Variable Registry
# ---------------------------------------------------------------------------

run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="")
stage_var: contextvars.ContextVar[str] = contextvars.ContextVar("stage", default="init")
source_var: contextvars.ContextVar[str] = contextvars.ContextVar("source", default="core")
worker_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("worker_id", default="main")
item_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("item_id", default="")
target_domain_var: contextvars.ContextVar[str] = contextvars.ContextVar("target_domain", default="")
query_var: contextvars.ContextVar[str] = contextvars.ContextVar("query", default="")


class bind_context:
    """Context manager to bind contextvars within a lexical scope and restore on exit."""

    def __init__(self, **kwargs: str):
        self._kwargs = kwargs
        self._tokens: list[tuple[contextvars.ContextVar, contextvars.Token]] = []

    def __enter__(self) -> None:
        mapping = {
            "run_id": run_id_var,
            "stage": stage_var,
            "source": source_var,
            "worker_id": worker_id_var,
            "item_id": item_id_var,
            "target_domain": target_domain_var,
            "query": query_var,
        }
        for key, value in self._kwargs.items():
            if key in mapping and value is not None:
                cvar = mapping[key]
                token = cvar.set(str(value))
                self._tokens.append((cvar, token))

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        for cvar, token in reversed(self._tokens):
            cvar.reset(token)


# ---------------------------------------------------------------------------
# Context-Propagating ThreadPoolExecutor
# ---------------------------------------------------------------------------

class ContextThreadPoolExecutor(ThreadPoolExecutor):
    """
    Subclass of ThreadPoolExecutor that captures the current ContextVars
    snapshot at submit() time and runs the task inside that context.

    Essential for Python 3.7+ multi-threaded logging: guarantees that worker
    threads in the 40-thread enricher pool and 5-worker Places pool inherit
    run_id, stage, and source without polluting worker thread state.
    """

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any):
        ctx = contextvars.copy_context()
        return super().submit(ctx.run, fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# PII Redaction Engine & Filter
# ---------------------------------------------------------------------------

class PiiRedactionFilter(logging.Filter):
    """
    Zero-bypass redaction filter. Strips personal phone numbers, emails,
    Google API keys, and sensitive URL query strings from log records
    and error messages before formatting and output.
    """

    # Matches Lebanese (+961/03/70/71/...) and Saudi (+966/05...) numbers
    _PHONE_RE = re.compile(
        r"(?:\+?96[16][\s.-]?0?[1-9]\d{1,2}[\s.-]?\d{3}[\s.-]?\d{3,4}|"
        r"\b0\d{1,2}[\s.-]?\d{3}[\s.-]?\d{3,4}\b|"
        r"\b\d{3}[\s.-]\d{3}[\s.-]\d{4}\b)"
    )

    # Standard email pattern
    _EMAIL_RE = re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    )

    # Google Cloud Places API keys (AIzaSy...)
    _API_KEY_RE = re.compile(r"AIza[0-9A-Za-z-_]{35}")

    # WhatsApp API click-to-chat links containing numbers
    _WHATSAPP_LINK_RE = re.compile(
        r"(?:https?://)?(?:wa\.me|api\.whatsapp\.com/send\?phone=)\d+"
    )

    # Strip query parameters from URLs in log strings
    _URL_QUERY_RE = re.compile(r"(https?://[^\s?#]+)\?[^\s#]*")

    @classmethod
    def redact_text(cls, text: str) -> str:
        if not text:
            return text
        text = cls._API_KEY_RE.sub("[REDACTED_API_KEY]", text)
        text = cls._WHATSAPP_LINK_RE.sub("[REDACTED_WHATSAPP_LINK]", text)
        text = cls._EMAIL_RE.sub(lambda m: cls._mask_email(m.group(0)), text)
        text = cls._PHONE_RE.sub(lambda m: cls._mask_phone(m.group(0)), text)
        text = cls._URL_QUERY_RE.sub(r"\1", text)
        return text

    @staticmethod
    def _mask_email(email: str) -> str:
        parts = email.split("@", 1)
        if len(parts) != 2:
            return "[REDACTED_EMAIL]"
        user, domain = parts
        if len(user) <= 2:
            masked_user = "*dr*"
        else:
            masked_user = f"{user[0]}***{user[-1]}"
        return f"{masked_user}@{domain}"

    @staticmethod
    def _mask_phone(phone: str) -> str:
        digits = re.sub(r"\D", "", phone)
        if len(digits) < 6:
            return "[REDACTED_PHONE]"
        # Preserve country prefix if available, mask central subscriber digits
        return f"{digits[:3]}****{digits[-2:]}"

    def filter(self, record: logging.LogRecord) -> bool:
        # Sanitize main message
        if isinstance(record.msg, str):
            record.msg = self.redact_text(record.msg)

        # Sanitize positional arguments if present
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._sanitize_arg(v) for k, v in record.args.items()}
            elif isinstance(record.args, (list, tuple)):
                record.args = tuple(self._sanitize_arg(arg) for arg in record.args)

        # Invalidate cached formatted message so sanitization applies
        record.message = ""
        return True

    def _sanitize_arg(self, arg: Any) -> Any:
        if isinstance(arg, str):
            return self.redact_text(arg)
        if isinstance(arg, dict):
            clean = {}
            for k, v in arg.items():
                if k.lower() in {"phone", "email", "whatsapp", "address", "raw"}:
                    clean[k] = "[REDACTED]"
                else:
                    clean[k] = self._sanitize_arg(v)
            return clean
        return arg


# ---------------------------------------------------------------------------
# Context Injection Filter
# ---------------------------------------------------------------------------

class ContextInjectionFilter(logging.Filter):
    """Injects current ContextVars values into the LogRecord attributes."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get()
        record.stage = stage_var.get()
        record.source = source_var.get()
        record.worker_id = worker_id_var.get()
        record.item_id = item_id_var.get()
        record.target_domain = target_domain_var.get()
        record.query = query_var.get()
        record.thread_name = threading.current_thread().name
        return True


# ---------------------------------------------------------------------------
# Formatters: JSON (NDJSON) and Human-Readable Console
# ---------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    """
    Renders structured NDJSON log records for CI, automated pipelines,
    and log files. Produces one JSON object per newline.
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc).isoformat()

        payload: dict[str, Any] = {
            "timestamp": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "run_id": getattr(record, "run_id", ""),
            "stage": getattr(record, "stage", ""),
            "source": getattr(record, "source", ""),
            "worker_id": getattr(record, "worker_id", ""),
            "thread_name": getattr(record, "thread_name", ""),
        }

        # Include optional contextual metadata if set
        if getattr(record, "target_domain", ""):
            payload["target_domain"] = record.target_domain
        if getattr(record, "query", ""):
            payload["query"] = record.query
        if getattr(record, "item_id", ""):
            payload["item_id"] = record.item_id

        # Merge extra attributes passed in extra={}
        for key, value in record.__dict__.items():
            if key not in {
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "id", "levelname", "levelno", "lineno", "module",
                "msecs", "message", "msg", "name", "pathname", "process",
                "processName", "relativeCreated", "stack_info", "thread",
                "threadName", "run_id", "stage", "source", "worker_id",
                "item_id", "target_domain", "query", "thread_name"
            }:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = PiiRedactionFilter.redact_text(self.formatException(record.exc_info))

        return json.dumps(payload, ensure_ascii=False)


class HumanConsoleFormatter(logging.Formatter):
    """
    Renders human-readable, columnar, clean console output with ANSI color
    highlights for interactive terminal sessions.
    """

    _COLORS = {
        "DEBUG": "\033[36m",     # Cyan
        "INFO": "\033[32m",      # Green
        "WARNING": "\033[33m",   # Yellow
        "ERROR": "\033[31m",     # Red
        "CRITICAL": "\033[35m",  # Magenta
    }
    _RESET = "\033[0m"

    def __init__(self, use_color: bool = True):
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        time_str = datetime.datetime.fromtimestamp(
            record.created, datetime.timezone.utc
        ).strftime("%H:%M:%S.%f")[:-3]

        level = record.levelname
        if self.use_color and level in self._COLORS:
            level_str = f"{self._COLORS[level]}{level:<5}{self._RESET}"
        else:
            level_str = f"{level:<5}"

        worker = getattr(record, "worker_id", "main") or "main"
        stage = getattr(record, "stage", "core") or "core"
        msg = record.getMessage()

        # Format tag header: TIME [LEVEL] [worker] [stage] MSG
        output = f"{time_str} [{level_str}] [{worker:<10}] [{stage:<11}] {msg}"

        if record.exc_info:
            exc_text = PiiRedactionFilter.redact_text(self.formatException(record.exc_info))
            output += f"\n{exc_text}"

        return output


# ---------------------------------------------------------------------------
# Setup & Helper Functions
# ---------------------------------------------------------------------------

def generate_record_id(name: str, phone_or_domain: str | None) -> str:
    """Generate an anonymous, deterministic 10-char hash identifier for a lead."""
    raw = f"{name.strip().lower()}|{(phone_or_domain or '').strip().lower()}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"rec_{digest}"


def setup_logging(
    run_id: str,
    level: str | int = logging.INFO,
    log_format: str | None = None,
    log_file: str | None = None,
) -> logging.Logger:
    """
    Configure root logging for leadminer with context propagation,
    PII sanitization, and dual format capabilities.
    """
    run_id_var.set(run_id)

    # Environment detection
    if log_format is None:
        is_ci = os.getenv("CI", "").lower() in ("true", "1") or "GITHUB_ACTIONS" in os.environ
        is_tty = sys.stderr.isatty()
        env_format = os.getenv("LOG_FORMAT", "").lower()

        if env_format in ("json", "ndjson"):
            use_json = True
        elif env_format in ("human", "console"):
            use_json = False
        else:
            use_json = is_ci or not is_tty
    else:
        use_json = log_format.lower() in ("json", "ndjson")

    # Clear existing handlers
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    # Core filters
    pii_filter = PiiRedactionFilter()
    ctx_filter = ContextInjectionFilter()

    # 1. Console / Stderr Handler
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level)
    console_handler.addFilter(pii_filter)
    console_handler.addFilter(ctx_filter)

    if use_json:
        console_handler.setFormatter(JsonFormatter())
    else:
        console_handler.setFormatter(HumanConsoleFormatter(use_color=sys.stderr.isatty()))

    root.addHandler(console_handler)

    # 2. File Handler (Optional or forensic artifact)
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)  # Capture full debug trail on disk
        file_handler.addFilter(pii_filter)
        file_handler.addFilter(ctx_filter)
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)

    # Silence verbose third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    logger = logging.getLogger("leadminer")
    logger.info(
        "Logging initialized (run_id=%s, format=%s, level=%s)",
        run_id,
        "json" if use_json else "human",
        logging.getLevelName(level),
    )
    return logger
```

---

## 6. Implementation & Integration Walkthrough

Below are the exact integration patterns showing how `scrapers/google_places.py` and `enricher.py` adopt `ContextThreadPoolExecutor` and the contextvars architecture.

### Integration in `scrapers/google_places.py` (5-Worker Pool)

```python
import logging
import time
from leadminer.telemetry.logging import (
    ContextThreadPoolExecutor,
    bind_context,
    worker_id_var,
    query_var,
)

logger = logging.getLogger("leadminer.google_places")
_WORKERS = 5

def scrape(self) -> Iterator[BusinessRecord]:
    # ... setup code ...
    all_queries = ... # 68 queries

    logger.info(
        "Beginning Google Places scrape: %d queries across Lebanon and KSA (workers=%d)",
        len(all_queries), _WORKERS
    )

    def fetch_query(worker_idx: int, query: str) -> None:
        # Explicit worker_id assignment per worker task
        worker_tag = f"places-{worker_idx:02d}"
        with bind_context(worker_id=worker_tag, query=query, source="google_places", stage="scrape"):
            country = _country_from_query(query)
            session = self._make_session()
            results = list(self._scrape_query(session, query, seen_ids, seen_lock, scraped_at, country))
            with records_lock:
                all_records.extend(results)

    # Use ContextThreadPoolExecutor instead of ThreadPoolExecutor
    with ContextThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="places-worker") as pool:
        futures = [
            pool.submit(fetch_query, i % _WORKERS + 1, q)
            for i, q in enumerate(all_queries)
        ]
        for f in as_completed(futures):
            f.result()

    yield from all_records

def _scrape_query(self, session, query, seen_ids, seen_lock, scraped_at, country):
    records = []
    page = 0
    page_token = None

    while True:
        body = {"textQuery": query}
        if page_token:
            body["pageToken"] = page_token

        start_time = time.perf_counter()
        try:
            resp = session.post(API_URL, json=body, timeout=30)
            latency_ms = int((time.perf_counter() - start_time) * 1000)

            if resp.status_code == 401:
                logger.error("Authentication failed: Invalid Google Places API key (status=401)")
                return records

            if resp.status_code == 429:
                logger.warning(
                    "Rate limit exceeded (HTTP 429) for query '%s'. Backing off for 30s...",
                    query, extra={"latency_ms": latency_ms, "status_code": 429}
                )
                time.sleep(30)
                continue

            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("Network failure executing Places query '%s': %s", query, e)
            return records

        data = resp.json()
        places = data.get("places", [])
        page += 1

        logger.debug(
            "Fetched page %d for query '%s' (%d places, %d ms)",
            page, query, len(places), latency_ms,
            extra={"page": page, "count": len(places), "latency_ms": latency_ms}
        )

        for place in places:
            # Process place and build BusinessRecord...
            pass

        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(2)  # Google nextPageToken activation delay

    return records
```

---

### Integration in `enricher.py` (40-Thread Pool)

```python
import logging
import time
from urllib.parse import urlparse
from leadminer.telemetry.logging import (
    ContextThreadPoolExecutor,
    bind_context,
    generate_record_id,
    worker_id_var,
    target_domain_var,
    item_id_var,
)

logger = logging.getLogger("leadminer.enricher")

def check_websites(records: list[dict], workers: int = 40) -> list[dict]:
    targets = [(i, r["website"], r.get("name", "Unknown"), r.get("source", "unknown"))
               for i, r in enumerate(records) if r.get("website")]
    if not targets:
        return records

    total = len(targets)
    logger.info(
        "Starting website enrichment: %d targets using %d concurrent workers",
        total, workers
    )

    def worker_task(worker_idx: int, idx: int, url: str, name: str, source: str) -> tuple[int, str, dict]:
        worker_tag = f"enrich-{worker_idx:02d}"
        domain = urlparse(url).netloc.lower() or "invalid-url"
        rec_id = generate_record_id(name, domain)

        with bind_context(
            worker_id=worker_tag,
            stage="enrichment",
            source=source,
            target_domain=domain,
            item_id=rec_id,
        ):
            start = time.perf_counter()
            outcome, contacts = _fetch_website(url)
            duration_ms = int((time.perf_counter() - start) * 1000)

            # Debug-level emission per target keeps CI stdout clean
            logger.debug(
                "Enrichment completed for domain '%s' -> %s (%d ms)",
                domain, outcome.upper(), duration_ms,
                extra={
                    "outcome": outcome,
                    "duration_ms": duration_ms,
                    "has_email": bool(contacts.get("email")),
                    "has_whatsapp": bool(contacts.get("whatsapp")),
                    "has_instagram": bool(contacts.get("instagram")),
                    "has_linkedin": bool(contacts.get("linkedin")),
                }
            )
            return idx, outcome, contacts

    done = 0
    with ContextThreadPoolExecutor(max_workers=workers, thread_name_prefix="enrich-worker") as pool:
        futures = {
            pool.submit(worker_task, (i % workers) + 1, idx, url, name, src): idx
            for i, (idx, url, name, src) in enumerate(targets)
        }

        for future in as_completed(futures):
            idx = futures[future]
            try:
                _, outcome, contacts = future.result()
            except Exception as e:
                logger.error("Unhandled worker exception during website fetch: %s", e)
                outcome, contacts = UNKNOWN, {"email": None, "instagram": None, "whatsapp": None, "linkedin": None}

            r = records[idx]
            r["website_live"] = True if outcome == LIVE else (False if outcome == DEAD else None)
            if outcome == LIVE:
                for channel in ("email", "instagram", "whatsapp", "linkedin"):
                    if not r.get(channel) and contacts.get(channel):
                        r[channel] = contacts[channel]

            done += 1
            # Milestone progress emitted at INFO level
            if done % 250 == 0 or done == total:
                logger.info(
                    "Enrichment progress: %d/%d targets processed (%.1f%%)",
                    done, total, (done / total) * 100
                )

    live_count = sum(1 for r in records if r.get("website_live") is True)
    dead_count = sum(1 for r in records if r.get("website_live") is False)
    unknown_count = sum(1 for r in records if r.get("website") and r.get("website_live") is None)

    logger.info(
        "Enrichment completed: %d live, %d dead, %d unreachable",
        live_count, dead_count, unknown_count
    )
    return records
```

---

### Pipeline Orchestration & Run ID Lifecycle (`main.py`)

```python
import datetime
import uuid
import logging
from leadminer.telemetry.logging import setup_logging, bind_context

def main() -> None:
    # Generate canonical run identifier
    run_timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_uuid = uuid.uuid4().hex[:8]
    run_id = f"run_{run_timestamp}_{run_uuid}"

    # Initialize logging subsystem
    log_file_path = f"logs/{run_id}.jsonl"
    logger = setup_logging(run_id=run_id, log_file=log_file_path)

    with bind_context(run_id=run_id, stage="orchestration", source="main", worker_id="main"):
        logger.info("Starting leadminer pipeline execution (run_id=%s)", run_id)

        # 1. Scrape Stage
        with bind_context(stage="scrape"):
            # Execute OSM, Wikidata, Google Places
            pass

        # 2. Filter & Dedup Stages
        with bind_context(stage="dedup"):
            pass

        # 3. Enrichment Stage (spawns 40 threads)
        records = check_websites(records, workers=40)

        # 4. Export Stage
        with bind_context(stage="export"):
            pass

        logger.info("Pipeline execution completed successfully (run_id=%s)", run_id)
```
