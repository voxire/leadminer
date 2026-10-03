# 099 — Target Pipeline Stage Design

## Verdict

The current 1,402-line `leadminer` script runs as an untyped, imperative monolith (`main.py:148-244`) that mutates raw dictionaries in place, silently swallows scraper crashes (`main.py:167-169`), computes scores out of order (`enricher.py:342` vs `main.py:197`), and performs uncheckpointed network requests. This architecture cannot be safely paused, resumed, or tested in isolation. Refactoring the system into a 10-stage sequential pipeline (`Collect`, `Normalise`, `Filter`, `Resolve`, `Enrich`, `Validate`, `Score`, `Pitch`, `Export`, `Report`) governed by an explicit `RunContext`, monotonic clock instrumentation, content-addressed artifact checkpoints, and an immutable `run_manifest.json` decouples I/O from domain business logic and provides the deterministic foundation required for production operations.

---

## Findings

### S1 — Monolithic `main()` couples I/O, domain logic, and state mutation without stage boundaries
- **Where:** `main.py:148-244`
- **Breaks:** The entire run lifecycle—master CSV loading, multi-threaded scraping, category filtering, deduplication, HTTP enrichment, rule assignment, CSV writing, and console summary printing—is executed inside a single 96-line procedure. If a network timeout or crash occurs during website enrichment (`main.py:187`), all scraped Google Places, OSM, and Wikidata records are permanently lost in memory. The operator must re-run the entire pipeline from scratch, incurring redundant third-party API costs and triggering rate limits.
- **Trigger:** Any uncaught exception (OOM, SIGTERM, Google Places quota exhaustion, unhandled redirect exception) at line 187 or 209 aborts execution after hours of scraping with zero persisted intermediate results.
- **Fix:** Decompose `main()` into 10 discrete, composable `Stage[InT, OutT]` instances managed by a `PipelineRunner`. Each stage takes an immutable input dataclass and a `RunContext`, returning a typed output dataclass persisted to a deterministic checkpoint on disk before the next stage begins.

### S1 — Silent scraper error swallowing causes partial data runs to masquerade as full runs
- **Where:** `main.py:167-169`
- **Breaks:** Scraper thread failures are caught with a bare `except Exception as e:` and logged to `sys.stderr`, leaving `raw` partially populated. The pipeline then proceeds directly to filter, dedup, and export. Because there is no run manifest or completion assertion, downstream consumers (and Google Drive uploads via `scrape.yml:50`) receive an incomplete data generation without any alert that OSM or Google Places failed entirely.
- **Trigger:** An Overpass 504 Gateway Timeout or Google Places `RESOURCE_EXHAUSTED` error causes `futures[future].result()` to raise. `main()` prints an unparsed line to `stderr` and proceeds to export a degraded dataset as the new master CSV.
- **Fix:** Replace untyped thread loops with a formal `Collect` stage that returns a strongly typed `CollectionResult` recording explicit `SourceOutcome` records (status, duration, error classification, records yielded) and enforces a configurable failure policy (e.g., abort if primary sources fail, or proceed under strict degraded-flagging recorded in `run_manifest.json`).

### S1 — Premature scoring and circular ordering dependencies corrupt record scoring
- **Where:** `enricher.py:341-343` vs `main.py:190-198`
- **Breaks:** `enrich()` computes `lead_score()` at `enricher.py:342`. However, `lead_score()` depends on `industry_priority` (`enricher.py:298-302`), which is only assigned downstream in `main.py:190`. As a result, fresh scrape rows have `industry_priority=None` during enrichment, causing `lead_score()` to award 0 priority points. While `main.py:197` patches this by recomputing `lead_score`, any intermediate stage or independent consumer inspecting records post-enrichment sees an invalid score. Furthermore, `dedup()` runs before `resolve_country` (`main.py:184`), yet `dedup()` depends on `country` for phone normalization (`dedup.py:110`).
- **Trigger:** Calling `enrich()` in isolation (or inspecting pipeline state between enrichment and main scoring) yields inconsistent scores where high-priority verticals (e.g., dental clinics, restaurants) are scored as zero-priority leads.
- **Fix:** Establish strict topological stage ordering: `Normalise` (canonicalize phone and resolve country) $\rightarrow$ `Filter` $\rightarrow$ `Resolve` (dedup) $\rightarrow$ `Enrich` (infer region, probe HTTP liveness) $\rightarrow$ `Validate` $\rightarrow$ `Score` (compute completeness and lead score once all inputs are final) $\rightarrow$ `Pitch` (assign `industry_priority` and `recommended_service`).

### S2 — Monolithic `BusinessRecord` TypedDict enforces false invariant guarantees
- **Where:** `scrapers/base.py:5-29`
- **Breaks:** `BusinessRecord` is declared with `total=True` and contains 23 fields including `lead_score: int`, `completeness_score: int`, `recommended_service: str | None`, and `website_live: bool | None`. Scrapers cannot know these fields at collection time, forcing OSM, Wikidata, and Google Places scrapers to fabricate dummy default values (`lead_score=0`, `completeness_score=0`, `recommended_service=None`, `scrapers/osm.py:113-118`). This pollutes static type checking (`mypy` / `pyright`) because downstream stages cannot distinguish between "scraped with 0 score" and "unscored".
- **Trigger:** A developer or downstream filter queries `r["lead_score"] == 0` on an un-scored record and treats it as a low-quality lead instead of an unprocessed lead.
- **Fix:** Define stage-specific typed record schemas across the pipeline lifecycle: `RawRecord` $\rightarrow$ `NormalisedRecord` $\rightarrow$ `ResolvedRecord` $\rightarrow$ `EnrichedRecord` $\rightarrow$ `ValidatedRecord` $\rightarrow$ `ScoredRecord` $\rightarrow$ `SalesRecord`.

### S2 — Inability to test or re-run individual pipeline stages offline
- **Where:** `main.py:148-214`
- **Breaks:** Because stages are not exposed as discrete callable units with mockable ports, testing the website enrichment logic requires either running scrapers or manually constructing raw dictionary fixtures and mocking module globals (`enricher._SESSION`). Furthermore, if a single regex in `pitch_recommender.py` is updated, the operator must re-run the entire multi-hour scraping and website checking process to regenerate `sales_ready.csv`.
- **Trigger:** Changing the categorization or scoring weight rules in `pitch_recommender.py` requires spending API quotas and waiting hours for external HTTP requests.
- **Fix:** Implement isolated stage entry points and disk-backed artifact caching. Re-running `Pitch` or `Export` reads the serialized checkpoint of `Score` or `Validate` directly without executing network operations.

### S3 — Ad-hoc timing and lack of structured run telemetry
- **Where:** `main.py:165,173-176,181,219-243`
- **Breaks:** Progress is tracked exclusively via unstructured `print()` calls. When a GitHub Actions workflow times out or stalls, there is no structured log or JSON manifest to determine whether the bottleneck was Overpass API latency, Google Places paging, website socket timeouts, or CSV serialization.
- **Trigger:** A run takes 4 hours instead of 45 minutes; the operator has no per-stage duration breakdown, retry counters, or throughput metrics (records/sec).
- **Fix:** Instrument every stage execution via a nanosecond monotonic clock in the pipeline runner, logging start time, end time, elapsed milliseconds, input/output counts, error tallies, and memory deltas to a standardized `run_manifest.json`.

---

## Target Architecture

### Architecture Overview

```
                            ┌─────────────────────────────────────────┐
                            │               RunContext                │
                            │  - RunConfig (paths, concurrency, flags)│
                            │  - RunPorts  (adapters, scrapers, http) │
                            │  - Clock     (injected monotonic clock) │
                            │  - Manifest  (telemetry & audit ledger) │
                            │  - Checkpoints (stage caching & storage)│
                            └────────────────────┬────────────────────┘
                                                 │
                                                 ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                         PipelineRunner                                           │
│                                                                                                  │
│  [1. Collect]  ──► [2. Normalise] ──► [3. Filter]  ──► [4. Resolve] ──► [5. Enrich]              │
│        │                 │                │                 │               │                    │
│   (Checkpoint)      (Checkpoint)     (Checkpoint)      (Checkpoint)    (Checkpoint)              │
│        ▼                 ▼                ▼                 ▼               ▼                    │
│  [6. Validate] ──► [7. Score]     ──► [8. Pitch]   ──► [9. Export]  ──► [10. Report]             │
│        │                 │                │                 │               │                    │
│   (Checkpoint)      (Checkpoint)     (Checkpoint)      (Artifacts)     (Manifest)                │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

The pipeline follows the **Ports & Adapters (Hexagonal)** architecture and the **Pipes & Filters** pattern:
1. **Separation of Concerns:** Each stage is a self-contained, typed class implementing the `Stage[InT, OutT]` protocol.
2. **Immutability:** Stages receive frozen or cleanly copied input dataclasses and return new typed output containers. In-place mutation of input batches is prohibited.
3. **Execution Context:** All external configuration, filesystem paths, clock providers, loggers, and I/O adapters are injected via `RunContext`.
4. **Resumability & Checkpointing:** The `PipelineRunner` checkpoints the output of every stage to disk (compressed JSONL or Parquet with SHA-256 integrity hashes). Any stage can be re-run from its predecessor's checkpoint without repeating upstream work.
5. **Monotonic Telemetry:** Per-stage wall-clock and monotonic durations, record throughput, and failure tallies are captured in real time and committed to `run_manifest.json`.

---

## Core Interfaces & Context Object

### The Stage Protocol

```python
from typing import Protocol, TypeVar
from dataclasses import dataclass
import pathlib

InT = TypeVar("InT", contravariant=True)
OutT = TypeVar("OutT", covariant=True)

class Stage(Protocol[InT, OutT]):
    @property
    def name(self) -> str:
        """Unique identifier for the stage (e.g., 'Collect', 'Resolve')."""
        ...

    def run(self, data: InT, ctx: "StageContext") -> OutT:
        """Execute the stage transformation. Pure or port-mediated."""
        ...
```

### The Context Object (`RunContext` & `StageContext`)

To prevent state pollution and enforce strict boundaries, the pipeline distinguishes between the global `RunContext` (managed by the `PipelineRunner`) and the scoped `StageContext` provided to each stage execution.

```python
from dataclasses import dataclass, field
from typing import Protocol, Any, Callable
import logging

class Clock(Protocol):
    def now_utc(self) -> str: ...
    def monotonic_ns(self) -> int: ...

@dataclass(frozen=True)
class RunConfig:
    run_id: str
    data_dir: pathlib.Path
    checkpoint_dir: pathlib.Path
    export_dir: pathlib.Path
    http_workers: int = 40
    scraper_workers: int = 3
    request_timeout_seconds: float = 8.0
    dry_run: bool = False
    resume_from_stage: str | None = None
    force_rerun_stage: str | None = None

@dataclass(frozen=True)
class RunPorts:
    """Injected I/O adapters (mockable in unit tests)."""
    master_store: Any           # Protocol for master CSV / DB read/write
    scrapers: list[Any]         # List of BaseScraper instances
    website_prober: Any         # HTTP probe adapter
    filesystem: Any             # Filesystem read/write/fsync adapter
    notifier: Any               # Slack / Discord / GitHub Actions summary port

@dataclass
class StageTiming:
    stage_name: str
    started_at: str
    finished_at: str
    elapsed_ms: float
    status: str                 # "SUCCESS", "FAILED", "SKIPPED", "DEGRADED"

@dataclass
class RunManifest:
    manifest_version: int = 1
    run_id: str = ""
    status: str = "PENDING"     # "PENDING", "RUNNING", "COMPLETED", "FAILED"
    started_at: str = ""
    finished_at: str | None = None
    git_sha: str = ""
    config_hash: str = ""
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    timings: list[StageTiming] = field(default_factory=list)
    system_metrics: dict[str, Any] = field(default_factory=dict)

@dataclass
class RunContext:
    config: RunConfig
    ports: RunPorts
    clock: Clock
    logger: logging.Logger
    manifest: RunManifest
    checkpoints: "CheckpointManager"

@dataclass(frozen=True)
class StageContext:
    """Narrow view provided to an executing stage."""
    run_id: str
    stage_name: str
    config: RunConfig
    ports: RunPorts
    clock: Clock
    logger: logging.Logger
    record_metric: Callable[[str, Any], None]
```

---

## Detailed Specifications for the 10 Target Stages

### Stage 1: Collect

```
                ┌──────────────────────────────┐
                │        Input: None           │
                └──────────────┬───────────────┘
                               │
               ┌───────────────┼───────────────┐
               ▼               ▼               ▼
        OSMScraper      WikidataScraper   GooglePlaces
               │               │               │
               └───────────────┬───────────────┘
                               │
                ┌──────────────▼───────────────┐
                │   Cumulative Master Store    │
                └──────────────┬───────────────┘
                               │
                ┌──────────────▼───────────────┐
                │   Output: RawCollectionBatch │
                └──────────────────────────────┘
```

#### Responsibility & Contract
- Fan out concurrent acquisition across external scraper ports (OSM, Wikidata, Google Places) and read historical cumulative master data from storage.
- Isolate scraper worker exceptions so that a failure in one provider does not abort others.
- Capture raw provider payload records without performing domain filtering, normalization, or phone mutation.
- Stamp each incoming record with source metadata, raw payload hash, and ingestion timestamp.

#### Input Type
```python
@dataclass(frozen=True)
class NoneInput:
    """Collect is the source stage of the pipeline."""
    pass
```

#### Output Type
```python
@dataclass(frozen=True)
class RawRecord:
    raw_id: str                         # UUID or SHA-256(source + source_native_id)
    source: str                         # "osm", "wikidata", "google_places", "master"
    source_native_id: str | None
    name: str | None
    category: str | None
    address: str | None
    city: str | None
    country: str | None
    lat: float | None
    lon: float | None
    phone: str | None
    email: str | None
    website: str | None
    facebook: str | None
    instagram: str | None
    whatsapp: str | None
    linkedin: str | None
    rating: float | None
    review_count: int | None
    collected_at: str                   # ISO-8601 UTC
    raw_payload: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class SourceOutcome:
    source_name: str
    status: str                         # "SUCCESS", "PARTIAL", "FAILED"
    records_collected: int
    duration_ms: float
    error_message: str | None = None
    http_requests_made: int = 0
    quota_units_used: int = 0

@dataclass(frozen=True)
class RawCollectionBatch:
    master_records: list[RawRecord]
    scraped_records: list[RawRecord]
    source_outcomes: dict[str, SourceOutcome]
    total_raw_count: int
```

#### Failure Policy
- **Master Store Failure:** FATAL. If the historical master dataset cannot be retrieved or parsed, abort pipeline immediately to prevent creating an empty master.
- **Scraper Failure:** DEGRADED / CONTINUE. If a scraper fails (e.g. Overpass 504, Google Places quota reached):
  1. Record `status="FAILED"`, log stack trace, and capture error in `SourceOutcome`.
  2. If at least one primary scraper or the master store succeeds, proceed with partial dataset.
  3. Mark run as `DEGRADED` in the manifest.
  4. If all external scrapers fail AND master store is empty: FATAL abort.
- **Retry Strategy:** Scraper port adapters must implement 3 exponential backoff retries with jitter (1s, 2s, 4s) for transient network/5xx codes before bubbling a failure up to `SourceOutcome`.

#### Run Manifest Record
```json
{
  "Collect": {
    "status": "COMPLETED",
    "elapsed_ms": 14250.4,
    "metrics": {
      "master_records_loaded": 4120,
      "scraped_records_collected": 2840,
      "total_records": 6960,
      "sources": {
        "master": {"status": "SUCCESS", "count": 4120, "elapsed_ms": 120.2},
        "osm": {"status": "SUCCESS", "count": 1450, "elapsed_ms": 8410.5, "requests": 1},
        "wikidata": {"status": "SUCCESS", "count": 890, "elapsed_ms": 3120.1, "requests": 1},
        "google_places": {"status": "PARTIAL", "count": 500, "elapsed_ms": 14100.2, "requests": 68, "quota_used": 68, "error": "Query limit capped"}
      }
    },
    "checkpoint": {
      "file": "data/checkpoints/run-20261003-01/01_collect.jsonl.zst",
      "sha256": "3a8b...12ef",
      "record_count": 6960
    }
  }
}
```

---

### Stage 2: Normalise

#### Responsibility & Contract
- Transform raw, messy field inputs into strictly typed, canonical representations across all records (scraped and master).
- Resolve country codes using ISO-3166-1 alpha-2 standards (`LB`, `SA`) via phone calling codes (`+961`, `+966`) or address tokens (`main.py:50-74`).
- Normalize phone numbers to E.164 standard (`+9611234567`) based on resolved country, transforming junk/shortcodes to `None` (`dedup.py:33-64`).
- Normalize business names via NFKD Unicode normalization, accent stripping, lowercase transformation, and whitespace collapsing (`dedup.py:66-69`).
- Canonicalize URLs (lowercase domain, strip tracking query params, prepend `https://` if protocol missing).
- Parse float coordinates, clamping lat/lon to valid geographic bounds ($\text{lat} \in [-90, 90]$, $\text{lon} \in [-180, 180]$).

#### Input Type
- `RawCollectionBatch` (from Stage 1)

#### Output Type
```python
@dataclass(frozen=True)
class NormalisedRecord:
    record_id: str
    source: str
    source_native_id: str | None
    name_raw: str | None
    name_canonical: str                 # NFKD, lowercased, punctuation stripped
    category_raw: str | None
    category_canonical: str             # lowercased, trimmed
    country_code: str                   # "LB" or "SA" (guaranteed ISO-3166-1)
    phone_raw: str | None
    phone_e164: str | None              # "+9611234567" or None if invalid
    email: str | None                   # lowercased, validated RFC 5322 syntax
    website_url: str | None             # canonicalized URL
    address_raw: str | None
    city_canonical: str | None
    lat: float | None
    lon: float | None
    facebook_url: str | None
    instagram_handle: str | None
    whatsapp_e164: str | None
    linkedin_url: str | None
    rating: float | None
    review_count: int | None
    collected_at: str
    is_master: bool

@dataclass(frozen=True)
class NormalisedBatch:
    records: list[NormalisedRecord]
    normalisation_drop_count: int
    phone_normalisation_failures: int
    country_resolution_breakdown: dict[str, int]
```

#### Failure Policy
- **Record-Level Isolation:** Non-fatal on malformed individual fields. If an individual field fails parsing (e.g. unparseable phone or garbled latitude), set that field to `None`, log an audit warning, and continue.
- **Stage Failure:** FATAL only if catastrophic internal error (e.g., Unicode decoding table corruption, out of memory).
- **Quality Gate:** If $>50\%$ of records fail country resolution, flag stage as `DEGRADED` in run manifest.

#### Run Manifest Record
```json
{
  "Normalise": {
    "status": "COMPLETED",
    "elapsed_ms": 340.2,
    "metrics": {
      "input_records": 6960,
      "output_records": 6960,
      "phones_normalised": 4820,
      "phones_rejected_junk": 210,
      "country_breakdown": {
        "LB": 5900,
        "SA": 1060
      },
      "urls_canonicalised": 2310
    },
    "checkpoint": {
      "file": "data/checkpoints/run-20261003-01/02_normalise.jsonl.zst",
      "sha256": "9f21...c81a",
      "record_count": 6960
    }
  }
}
```

---

### Stage 3: Filter

#### Responsibility & Contract
- Filter out non-commercial and out-of-scope entities (geographic features, government bodies, religious structures, watercourses) leaking from OSM/Wikidata.
- Apply the two-tier category allow-list (`PRIORITY_INDUSTRIES` and `ADJACENT_BUSINESSES` from `scrapers/whitelist.py:17-64`).
- Filter boundary policy: Apply whitelist filtering to freshly scraped records; pass existing master records through unless explicitly marked for re-validation.
- Maintain a quarantine log of rejected records with explicit rejection reasons (e.g., `DISALLOWED_CATEGORY`, `MISSING_CORE_ATTRIBUTES`, `OUT_OF_BOUNDS_COORDINATES`).

#### Input Type
- `NormalisedBatch` (from Stage 2)

#### Output Type
```python
@dataclass(frozen=True)
class RejectedRecord:
    record_id: str
    source: str
    name: str | None
    category: str | None
    reason: str                         # e.g., "NON_COMMERCIAL_CATEGORY", "GEO_NOISE"
    details: str

@dataclass(frozen=True)
class FilteredBatch:
    surviving_records: list[NormalisedRecord]
    rejected_records: list[RejectedRecord]
    filter_counts_by_reason: dict[str, int]
    filter_counts_by_category: dict[str, int]
```

#### Failure Policy
- **Pure Stage Invariant:** This stage is pure computation (zero external I/O). It cannot fail due to network or third-party outages.
- **Sanity Threshold:** If the filter rejects $>95\%$ of scraped records, fail safe: pause pipeline execution and alert on potential category taxonomy drift or whitelist corruption.

#### Run Manifest Record
```json
{
  "Filter": {
    "status": "COMPLETED",
    "elapsed_ms": 85.6,
    "metrics": {
      "input_records": 6960,
      "surviving_records": 5120,
      "rejected_records": 1840,
      "rejection_reasons": {
        "NON_COMMERCIAL_CATEGORY": 1620,
        "GEO_NOISE": 190,
        "MISSING_NAME": 30
      },
      "top_rejections": {
        "watercourse": 420,
        "mountain_peak": 310,
        "village": 280
      }
    },
    "checkpoint": {
      "file": "data/checkpoints/run-20261003-01/03_filter.jsonl.zst",
      "sha256": "4b6c...990d",
      "record_count": 5120
    }
  }
}
```

---

### Stage 4: Resolve

#### Responsibility & Contract
- Deduplicate and merge entity records across all sources (OSM, Wikidata, Google Places, master store).
- Primary identity match: Valid `phone_e164` match within country code (`dedup.py:103-115`).
- Secondary identity match: Exact `(name_canonical, city_canonical)` composite key where phone is absent or shared across chains (`dedup.py:116-124`).
- Field-level conflict resolution:
  - `source`: Set union of source tags sorted and pipe-delimited (`osm|google_places`, `dedup.py:92-94`).
  - `collected_at`: Maximum timestamp (most recent scrape).
  - Categorical & numerical fields: Prioritize record with the highest populated field count (`_field_count(a) >= _field_count(b)`, `dedup.py:83-99`), with explicit source authority hierarchy: `google_places` > `master` > `osm` > `wikidata`.
- Emits resolved entity clusters with full lineage tracking (which raw IDs were merged into which resolved entity).

#### Input Type
- `FilteredBatch` (from Stage 3)

#### Output Type
```python
@dataclass(frozen=True)
class EntityLineage:
    canonical_id: str
    merged_raw_ids: list[str]
    sources_merged: list[str]
    match_strategy: str                 # "PHONE_E164" or "NAME_CITY"

@dataclass(frozen=True)
class ResolvedRecord:
    canonical_id: str
    sources: list[str]                  # e.g., ["google_places", "osm"]
    name: str
    name_canonical: str
    category: str
    country: str                        # "LB" or "SA"
    region: str | None                  # to be enriched in Stage 5
    address: str | None
    city: str | None
    lat: float | None
    lon: float | None
    phone: str | None                   # E.164
    email: str | None
    website: str | None
    facebook: str | None
    instagram: str | None
    whatsapp: str | None
    linkedin: str | None
    rating: float | None
    review_count: int | None
    scraped_at: str
    lineage: EntityLineage

@dataclass(frozen=True)
class ResolvedBatch:
    records: list[ResolvedRecord]
    unique_entities_count: int
    merge_events_count: int
    phone_merges_count: int
    name_city_merges_count: int
```

#### Failure Policy
- **Pure Computation Stage:** Zero network calls. No partial network failure possible.
- **Integrity Validation:** If output record count is 0 when input $>0$, fail pipeline immediately.
- **Cycle / Cluster Collision Detection:** If an entity cluster collapses $>50$ distinct raw records under a single key (indicative of generic default phone numbers like "+9611111111"), isolate the collision into an alert quarantine and prevent over-merging.

#### Run Manifest Record
```json
{
  "Resolve": {
    "status": "COMPLETED",
    "elapsed_ms": 195.4,
    "metrics": {
      "input_candidates": 5120,
      "unique_entities": 3840,
      "merges_performed": 1280,
      "merges_by_strategy": {
        "PHONE_E164": 940,
        "NAME_CITY": 340
      },
      "multi_source_entities": 820
    },
    "checkpoint": {
      "file": "data/checkpoints/run-20261003-01/04_resolve.jsonl.zst",
      "sha256": "1d2a...884e",
      "record_count": 3840
    }
  }
}
```

---

### Stage 5: Enrich

#### Responsibility & Contract
- Perform external and spatial enrichment without altering core identity:
  1. **Region Inference:** Infer geographical region from address keywords or spatial bounding boxes (`infer_region`, `enricher.py:58-94`).
  2. **Website Liveness & Security Check:** Probe each unique target website URL using single-pass HTTP GET with streaming body cap (`_MAX_BODY_BYTES = 200_000`, `enricher.py:155-189`). Strict classification of outcomes:
     - `LIVE`: Server returned 2xx.
     - `DEAD`: Server returned 4xx/5xx (strong rebuild pitch signal).
     - `UNKNOWN`: DNS error, TLS error, socket timeout, Cloudflare block (never treat as dead; `enricher.py:148-153`).
  3. **Digital Contact Extraction:** Extract mailto links, social handles (Instagram, WhatsApp, LinkedIn), and emails using regex parsers on live HTML bodies (`enricher.py:191-214`).
  4. **SSRF & Localhost Guard:** Filter out RFC 1918 private IPs, loopback, link-local, and cloud metadata endpoints before dispatching HTTP probes.
- Invariant: Does **not** calculate completeness, industry priority, or lead score.

#### Input Type
- `ResolvedBatch` (from Stage 4)

#### Output Type
```python
from enum import Enum

class LivenessState(Enum):
    LIVE = "live"
    DEAD = "dead"
    UNKNOWN = "unknown"

@dataclass(frozen=True)
class HttpProbeTelemetry:
    url: str
    status_code: int | None
    liveness: LivenessState
    response_time_ms: float
    bytes_read: int
    error_kind: str | None               # "DNS_FAILURE", "TIMEOUT", "TLS_ERROR", "HTTP_500"

@dataclass(frozen=True)
class EnrichedRecord:
    canonical_id: str
    sources: list[str]
    name: str
    name_canonical: str
    category: str
    country: str
    region: str | None                  # inferred or preserved
    address: str | None
    city: str | None
    lat: float | None
    lon: float | None
    phone: str | None
    email: str | None                   # merged or scraped from website
    website: str | None
    website_live: bool | None           # True if LIVE, False if DEAD, None if UNKNOWN/unreachable
    facebook: str | None
    instagram: str | None
    whatsapp: str | None
    linkedin: str | None
    rating: float | None
    review_count: int | None
    scraped_at: str
    probe_telemetry: HttpProbeTelemetry | None

@dataclass(frozen=True)
class EnrichedBatch:
    records: list[EnrichedRecord]
    websites_probed: int
    live_count: int
    dead_count: int
    unknown_count: int
    contacts_discovered: dict[str, int]
```

#### Failure Policy
- **Individual Probe Failures:** Non-fatal. Individual site connection resets, SSL timeouts, or 503s are caught and classified as `LivenessState.UNKNOWN`. The pipeline must never fail due to third-party website errors (`enricher.py:236-240`).
- **Worker Pool Crashes:** If worker threads encounter OS socket exhaustion or unexpected errors, catch at task boundary, record `UNKNOWN` probe outcome, and keep record intact.
- **Global Rate Limiting / Ban:** If $>80\%$ of probes in a 200-sample window trigger connection resets or Cloudflare blocks, pause worker concurrency, double probe timeout, and emit a pipeline `WARNING`.

#### Run Manifest Record
```json
{
  "Enrich": {
    "status": "COMPLETED",
    "elapsed_ms": 48210.0,
    "metrics": {
      "input_records": 3840,
      "regions_inferred": 1420,
      "websites_probed": 1950,
      "liveness_summary": {
        "live": 1410,
        "dead": 210,
        "unknown_unreachable": 330
      },
      "contacts_discovered": {
        "email": 412,
        "instagram": 315,
        "whatsapp": 180,
        "linkedin": 94
      },
      "http_traffic": {
        "total_requests": 1950,
        "bytes_transferred": 184500000,
        "avg_latency_ms": 412.5
      }
    },
    "checkpoint": {
      "file": "data/checkpoints/run-20261003-01/05_enrich.jsonl.zst",
      "sha256": "8a3d...51bc",
      "record_count": 3840
    }
  }
}
```

---

### Stage 6: Validate

#### Responsibility & Contract
- Execute data hygiene and integrity contracts before analytical scoring.
- Verify mandatory invariant rules:
  1. `name` is present, non-empty, and $\ge 2$ characters.
  2. `country` matches `{"LB", "SA"}`.
  3. `lat` / `lon` within bounding boxes of claimed country if present.
  4. `email` format matches RFC 5322 regex and is not in domain discard blacklist (`_EMAIL_BLACKLIST`, `enricher.py:135-138`).
  5. `phone` is valid E.164 dialable number or `None`.
  6. `website` is a well-formed HTTP/HTTPS URI or `None`.
- Partition input batch into `Valid` and `Quarantined` records. Only valid records move forward to scoring and sales export. Quarantined records are written to a dedicated DLQ (Dead Letter Queue) artifact with error codes.

#### Input Type
- `EnrichedBatch` (from Stage 5)

#### Output Type
```python
@dataclass(frozen=True)
class ValidationError:
    field: str
    value: Any
    rule: str
    message: str

@dataclass(frozen=True)
class QuarantinedRecord:
    record: EnrichedRecord
    errors: list[ValidationError]
    quarantined_at: str

@dataclass(frozen=True)
class ValidatedRecord(EnrichedRecord):
    """Guaranteed clean schema complying with all business validation constraints."""
    pass

@dataclass(frozen=True)
class ValidationBatch:
    valid_records: list[ValidatedRecord]
    quarantined_records: list[QuarantinedRecord]
    validation_pass_rate: float
    violations_by_rule: dict[str, int]
```

#### Failure Policy
- **Quarantine Isolation:** Defective records are quarantined, not discarded silently.
- **Pipeline SLA Threshold:** If `validation_pass_rate < 0.85` (more than 15% of records are quarantined), abort pipeline execution with `ValidationThresholdExceededError`. This prevents corrupted scrapers from publishing bad data to sales reps.

#### Run Manifest Record
```json
{
  "Validate": {
    "status": "COMPLETED",
    "elapsed_ms": 110.5,
    "metrics": {
      "input_records": 3840,
      "valid_records": 3795,
      "quarantined_records": 45,
      "pass_rate": 0.988,
      "violations": {
        "INVALID_EMAIL_DOMAIN": 22,
        "OUT_OF_BOUNDS_COORDINATES": 14,
        "NAME_TOO_SHORT": 9
      }
    },
    "checkpoint": {
      "file": "data/checkpoints/run-20261003-01/06_validate.jsonl.zst",
      "sha256": "6c1f...45d2",
      "record_count": 3795
    },
    "quarantine_artifact": {
      "file": "data/checkpoints/run-20261003-01/quarantine_dlq.jsonl.zst",
      "sha256": "e3b0...c491",
      "record_count": 45
    }
  }
}
```

---

### Stage 7: Score

#### Responsibility & Contract
- Calculate quantitative quality and commercial viability metrics for each validated lead.
- Invariant ordering:
  1. Calculate `completeness_score` ($0-7$) based on populated channels (`phone`, `email`, `website`, `address`, `social`, `whatsapp`, `linkedin`; `enricher.py:101-118`).
  2. Map `category` to `industry_priority` (`"high"`, `"medium"`, `"low"`) via priority taxonomy rules (`whitelist.py:16-43`).
  3. Calculate authoritative `lead_score` ($0-100$) using the complete set of final signals (`enricher.py:275-312`):
     - Contact channels: Email (+20), WhatsApp (+15), Phone (+15), Instagram (+10).
     - Website signals: Confirmed dead website (+20, rebuild opportunity), Live website (+10).
     - Priority vertical: High (+15), Medium (+8).
     - Review signals: Sub-4.0 rating (+10, reputation pain point).
     - Multi-source corroboration: Corroborated in $\ge 2$ data sources (+5).
- Single source of truth: This is the **only** stage in the pipeline permitted to compute or write `completeness_score`, `industry_priority`, and `lead_score`.

#### Input Type
- `ValidationBatch` (from Stage 6)

#### Output Type
```python
@dataclass(frozen=True)
class ScoreBreakdown:
    channel_points: int
    website_points: int
    priority_points: int
    rating_points: int
    corroboration_points: int
    total_score: int

@dataclass(frozen=True)
class ScoredRecord:
    canonical_id: str
    sources: list[str]
    name: str
    name_canonical: str
    category: str
    industry_priority: str              # "high", "medium", "low"
    country: str
    region: str | None
    address: str | None
    city: str | None
    lat: float | None
    lon: float | None
    phone: str | None
    email: str | None
    website: str | None
    website_live: bool | None
    facebook: str | None
    instagram: str | None
    whatsapp: str | None
    linkedin: str | None
    rating: float | None
    review_count: int | None
    completeness_score: int             # 0 to 7
    lead_score: int                     # 0 to 100
    score_breakdown: ScoreBreakdown
    scraped_at: str

@dataclass(frozen=True)
class ScoredBatch:
    records: list[ScoredRecord]
    avg_lead_score: float
    score_distribution: dict[str, int]   # "0-20", "21-40", "41-60", "61-80", "81-100"
    priority_breakdown: dict[str, int]   # "high", "medium", "low"
```

#### Failure Policy
- **Pure Stage Invariant:** Mathematical evaluation only. Fatal only on unexpected unhandled type error.
- **Score Invariant Check:** Verify that every record has $0 \le \text{lead\_score} \le 100$ and $0 \le \text{completeness\_score} \le 7$. If any record violates bounds, fail stage.

#### Run Manifest Record
```json
{
  "Score": {
    "status": "COMPLETED",
    "elapsed_ms": 42.1,
    "metrics": {
      "input_records": 3795,
      "scored_records": 3795,
      "avg_lead_score": 54.2,
      "score_distribution": {
        "0-20": 420,
        "21-40": 890,
        "41-60": 1340,
        "61-80": 855,
        "81-100": 290
      },
      "priority_breakdown": {
        "high": 1210,
        "medium": 1950,
        "low": 635
      }
    },
    "checkpoint": {
      "file": "data/checkpoints/run-20261003-01/07_score.jsonl.zst",
      "sha256": "5f9e...11b8",
      "record_count": 3795
    }
  }
}
```

---

### Stage 8: Pitch

#### Responsibility & Contract
- Execute the Voxire agency sales recommendation rules (`pitch_recommender.py:25-99`).
- Map digital presence gaps to recommended services across 7 distinct tiers:
  - **Tier 1:** Dead website $\rightarrow$ *"Website rebuild + maintenance"*
  - **Tier 2:** Missing website + active social presence $\rightarrow$ *"E-commerce launch"* or *"Website launch + capture existing audience"*
  - **Tier 3:** Live website + low reviews / poor rating $\rightarrow$ *"SEO audit + visibility upgrade"* or *"Reputation + SEO turnaround"*
  - **Tier 4:** High-volume F&B / hospitality $\rightarrow$ *"RTYLR commerce OS (POS, online ordering, CRM)"*
  - **Tier 5:** High-LTV medical / legal / real estate $\rightarrow$ *"Lead-gen website + Google Ads launch"*
  - **Tier 6:** Zero web / social presence + phone $\rightarrow$ *"Full digital launch (brand + website + social setup)"*
  - **Tier 7:** Mature digital presence $\rightarrow$ *"Digital marketing retainer (Meta + Google + content)"*
  - **Fallback:** *"Discovery call - scope the right service"*
- Assign `is_sales_ready` predicate: Lead has at least one valid outbound contact channel (`phone >= 7 digits`, `valid email`, or `valid instagram`) AND `industry_priority in ("high", "medium")` (`main.py:137-145, 202-205`).

#### Input Type
- `ScoredBatch` (from Stage 7)

#### Output Type
```python
@dataclass(frozen=True)
class PitchedRecord(ScoredRecord):
    recommended_service: str
    pitch_tier: int                     # 1 through 7, or 0 for fallback
    is_sales_ready: bool
    is_qualified: bool                  # completeness_score >= 1

@dataclass(frozen=True)
class PitchedBatch:
    records: list[PitchedRecord]
    sales_ready_count: int
    service_recommendation_counts: dict[str, int]
    pitch_tier_distribution: dict[int, int]
```

#### Failure Policy
- **Pure Rule Evaluation:** Deterministic if-chain. Must never crash on partial fields (all fields guarded by fallback defaults).
- **Rule Coverage Assertion:** Assert that `recommended_service` is non-empty for $100\%$ of records. If any record produces an empty or null pitch string, fail stage.

#### Run Manifest Record
```json
{
  "Pitch": {
    "status": "COMPLETED",
    "elapsed_ms": 28.3,
    "metrics": {
      "input_records": 3795,
      "sales_ready_records": 1820,
      "qualified_records": 3710,
      "service_breakdown": {
        "Website rebuild + maintenance": 210,
        "Website launch + capture their existing audience": 480,
        "E-commerce launch + Instagram-to-store funnel": 190,
        "SEO audit + visibility upgrade": 320,
        "Lead-gen website + Google Ads launch": 240,
        "Full digital launch (brand + website + social setup)": 380
      }
    },
    "checkpoint": {
      "file": "data/checkpoints/run-20261003-01/08_pitch.jsonl.zst",
      "sha256": "7c8a...3312",
      "record_count": 3795
    }
  }
}
```

---

### Stage 9: Export

#### Responsibility & Contract
- Partition records into the 5 target operational deliverables:
  1. `all_businesses.csv`: Cumulative master (all records).
  2. `qualified_businesses.csv`: Records with `completeness_score >= 1`.
  3. `with_websites.csv`: Records with a website URL.
  4. `without_websites.csv`: Records with no website (prime web-design targets).
  5. `sales_ready.csv`: High/medium priority leads with verified outbound contact channels.
- Implement strict atomic file writing (`main.py:108-135`):
  - Write each CSV to a temp file in the staging directory with UTF-8-SIG encoding (ensuring Excel renders Arabic business names correctly without mojibake).
  - Explicitly flush buffer and execute `os.fsync(f.fileno())` to guarantee disk persistence.
  - Perform atomic POSIX rename (`os.replace`) to target filename.
  - Compute and record SHA-256 cryptographic checksums for every exported CSV.

#### Input Type
- `PitchedBatch` (from Stage 8)

#### Output Type
```python
@dataclass(frozen=True)
class ExportTarget:
    filename: str
    path: pathlib.Path
    row_count: int
    file_size_bytes: int
    sha256_hash: str

@dataclass(frozen=True)
class ExportResult:
    exported_targets: dict[str, ExportTarget]
    staging_directory: pathlib.Path
    published_directory: pathlib.Path
    total_files_written: int
    total_bytes_written: int
```

#### Failure Policy
- **Atomic Rollback:** If any file write fails or disk runs out of space, delete all temporary `.tmp` staging files. Never leave the export directory in a partial or mixed generation state (`docs/audits/050-provenance-and-idempotency.md`).
- **Disk Full / Permission Error:** FATAL. Abort run, retain existing master without corruption.

#### Run Manifest Record
```json
{
  "Export": {
    "status": "COMPLETED",
    "elapsed_ms": 380.7,
    "metrics": {
      "files_written": 5,
      "total_bytes": 4820190,
      "exports": {
        "all_businesses.csv": {
          "rows": 3795,
          "bytes": 2104500,
          "sha256": "2d9b...88aa"
        },
        "qualified_businesses.csv": {
          "rows": 3710,
          "bytes": 2045100,
          "sha256": "4a7c...11ef"
        },
        "with_websites.csv": {
          "rows": 1950,
          "bytes": 1102400,
          "sha256": "9b12...66dd"
        },
        "without_websites.csv": {
          "rows": 1845,
          "bytes": 1002100,
          "sha256": "3c44...55ea"
        },
        "sales_ready.csv": {
          "rows": 1820,
          "bytes": 984000,
          "sha256": "7e88...99bb"
        }
      }
    }
  }
}
```

---

### Stage 10: Report

#### Responsibility & Contract
- Synthesize pipeline telemetry, per-stage timing records, data quality metrics, regional distributions, and pitch distributions into two artifacts:
  1. **Structured Run Manifest (`run_manifest.json`):** Machine-readable audit ledger recording Git commit, configuration SHA, inputs, outputs, per-stage durations, and HTTP probe summaries.
  2. **Operational Console & Slack Brief:** Formatted summary preserving existing terminal aesthetics (`main.py:219-243`) while displaying stage execution times, network costs, and data drift alerts.
- Atomically publish the manifest and update the `CURRENT` generation pointer symlink.

#### Input Type
- `ExportResult` (from Stage 9)

#### Output Type
```python
@dataclass(frozen=True)
class RegionalSummary:
    region: str
    count: int
    sales_ready_count: int

@dataclass(frozen=True)
class RunSummary:
    run_id: str
    status: str
    total_duration_ms: float
    total_businesses: int
    qualified_count: int
    sales_ready_count: int
    with_website_count: int
    live_website_count: int
    dead_website_count: int
    regional_breakdown: list[RegionalSummary]
    top_recommended_services: dict[str, int]
    manifest_path: pathlib.Path
    console_report_text: str
```

#### Failure Policy
- **Non-blocking on Notification:** If external notification (e.g. webhook, Slack) fails, log warning and complete successfully.
- **Manifest Persistence:** FATAL if local filesystem cannot write `run_manifest.json`. The manifest is the source of truth for pipeline commit.

#### Run Manifest Record
```json
{
  "Report": {
    "status": "COMPLETED",
    "elapsed_ms": 15.2,
    "metrics": {
      "manifest_written": true,
      "summary_rendered": true
    }
  }
}
```

---

## Per-Stage Timing & Monotonic Instrumentation

A major defect identified in `main.py` is the absence of unified performance instrumentation (`main.py:160-243`). Measuring durations using wall-clock time (`time.time()`) is vulnerable to system clock adjustments and NTP skews. 

The pipeline uses Python's nanosecond monotonic clock (`time.monotonic_ns()`) wrapped inside an injectable `Clock` protocol.

```python
import time

class SystemClock:
    def now_utc(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()

class FakeClock:
    """Deterministic clock for unit testing."""
    def __init__(self, start_ns: int = 1_000_000_000):
        self._current_ns = start_ns

    def advance_ms(self, ms: float) -> None:
        self._current_ns += int(ms * 1_000_000)

    def now_utc(self) -> str:
        return "2026-10-03T12:00:00Z"

    def monotonic_ns(self) -> int:
        return self._current_ns
```

### The Stage Execution Wrapper

The `PipelineRunner` executes every stage through a standard wrapper that handles timing, error interception, checkpointing, and manifest recording:

```python
def execute_stage(stage: Stage[InT, OutT], input_data: InT, ctx: RunContext) -> OutT:
    stage_name = stage.name
    stage_ctx = StageContext(
        run_id=ctx.config.run_id,
        stage_name=stage_name,
        config=ctx.config,
        ports=ctx.ports,
        clock=ctx.clock,
        logger=ctx.logger,
        record_metric=lambda k, v: ctx.manifest.record_stage_metric(stage_name, k, v)
    )

    started_at = ctx.clock.now_utc()
    t_start = ctx.clock.monotonic_ns()
    ctx.logger.info(f"[{stage_name}] Starting execution...")

    try:
        output = stage.run(input_data, stage_ctx)
        status = "SUCCESS"
        return output
    except Exception as exc:
        status = "FAILED"
        ctx.logger.error(f"[{stage_name}] Failed: {exc}", exc_info=True)
        raise
    finally:
        t_end = ctx.clock.monotonic_ns()
        elapsed_ms = (t_end - t_start) / 1_000_000.0
        finished_at = ctx.clock.now_utc()

        timing = StageTiming(
            stage_name=stage_name,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_ms=elapsed_ms,
            status=status
        )
        ctx.manifest.timings.append(timing)
        ctx.logger.info(f"[{stage_name}] Finished in {elapsed_ms:.2f}ms with status {status}")
```

---

## Individually Re-runnable Architecture (Checkpoints & Resumption)

A production run takes over 30 minutes, primarily due to website HTTP probes and Google Places queries. A failure in `Score` or `Export` must never require re-running `Collect` or `Enrich`.

### Checkpoint Storage Model

At the completion of each stage $N$, the `PipelineRunner` serializes the output batch to:
`data/checkpoints/{run_id}/{stage_number}_{stage_name}.jsonl.zst`

1. **Format:** Zstandard-compressed JSON Lines (`.jsonl.zst`). High compression ratio (typically 10:1 for repetitive business records), streaming deserialization, and human-inspectable when decompressed.
2. **Integrity:** The runner computes a SHA-256 hash of the uncompressed payload and records it in `run_manifest.json`.
3. **Resumption CLI Semantics:**
   ```bash
   # Re-run scoring and pitch generation using existing enriched checkpoint:
   python main.py --resume-from-stage Score --run-id 20261003-01

   # Re-run website enrichment only with higher concurrency:
   python main.py --from-stage Enrich --run-id 20261003-01 --workers 80
   ```

### Checkpoint Lifecycle

```python
class CheckpointManager:
    def __init__(self, base_dir: pathlib.Path):
        self.base_dir = base_dir

    def save(self, run_id: str, stage_name: str, data: Any) -> pathlib.Path:
        target = self.base_dir / run_id / f"{stage_name}.jsonl.zst"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write via tempfile + rename
        # Serialize dataclass records to JSONL, compress with zstandard
        return target

    def load(self, run_id: str, stage_name: str, target_type: type[T]) -> T:
        target = self.base_dir / run_id / f"{stage_name}.jsonl.zst"
        if not target.exists():
            raise FileNotFoundError(f"Checkpoint for stage {stage_name} at {target} not found")
        # Decompress and deserialize into target_type
        ...
```

---

## Individually Testable Contract (Zero-Network Test Plan)

A critical limitation of `leadminer` today is zero automated tests (`BRIEF.md:11`). The 10-stage architecture separates pure business logic from network/file I/O, allowing 100% offline unit test coverage.

### Stage Purity & Testing Matrix

| Stage | Purity Class | External Dependencies | Mock / Fake Required in Tests |
|---|---|---|---|
| **1. Collect** | I/O Bound | OSM, Wikidata, Google Places, Master CSV | `FakeScraper`, `FakeMasterStore` |
| **2. Normalise** | **Pure** | None (Unicode, regex, string math) | None (In-memory dataclasses) |
| **3. Filter** | **Pure** | None (Category whitelist lookup) | None (In-memory dataclasses) |
| **4. Resolve** | **Pure** | None (Deduplication matching algorithms) | None (In-memory dataclasses) |
| **5. Enrich** | I/O Bound | Target Websites (HTTP), Bounding Boxes | `MockWebsiteProber` |
| **6. Validate** | **Pure** | None (Schema validation logic) | None (In-memory dataclasses) |
| **7. Score** | **Pure** | None (Scoring formulas) | None (In-memory dataclasses) |
| **8. Pitch** | **Pure** | None (Rule hierarchy matching) | None (In-memory dataclasses) |
| **9. Export** | I/O Bound | Local Filesystem | `FakeFilesystem` / `tmp_path` |
| **10. Report** | I/O Bound | Local Filesystem, Webhooks | `FakeNotifier`, `tmp_path` |

### Unit Testing Example: Pure Scoring Stage

Testing `Score` requires zero network connections, zero filesystem writes, and executes in $<2$ milliseconds:

```python
def test_lead_score_awards_high_priority_and_dead_website():
    # Arrange
    record = ValidatedRecord(
        canonical_id="ent_123",
        sources=["google_places"],
        name="Al Sultan Restaurant",
        name_canonical="al sultan restaurant",
        category="restaurant",
        country="LB",
        region="Beirut",
        address="Hamra, Beirut",
        city="beirut",
        lat=33.89,
        lon=35.48,
        phone="+9611345678",
        email="info@alsultan.com",
        website="https://alsultan.com",
        website_live=False,  # Confirmed DEAD site (Tier 1 rebuild pitch)
        facebook=None,
        instagram="alsultan_lb",
        whatsapp="+96170123456",
        linkedin=None,
        rating=3.6,          # Sub-4.0 rating
        review_count=45,
        scraped_at="2026-10-03T10:00:00Z",
        probe_telemetry=None
    )
    batch = ValidationBatch(valid_records=[record], quarantined_records=[], validation_pass_rate=1.0, violations_by_rule={})
    stage = ScoreStage()
    ctx = create_test_stage_context(stage.name)

    # Act
    result = stage.run(batch, ctx)

    # Assert
    scored = result.records[0]
    assert scored.industry_priority == "high"
    # Verification:
    # Email: +20, WhatsApp: +15, Phone: +15, Instagram: +10 = 60
    # Dead website: +20
    # High priority: +15
    # Sub-4.0 rating: +10
    # Total = 105 -> capped at 100
    assert scored.lead_score == 100
    assert scored.completeness_score == 6
```

---

## Complete Run Manifest Schema (`run_manifest.json`)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "LeadMinerRunManifest",
  "type": "object",
  "required": [
    "manifest_version",
    "run_id",
    "status",
    "started_at",
    "finished_at",
    "git",
    "config",
    "stages",
    "timings",
    "outputs"
  ],
  "properties": {
    "manifest_version": {"type": "integer", "const": 1},
    "run_id": {"type": "string"},
    "status": {"type": "string", "enum": ["COMPLETED", "FAILED", "DEGRADED"]},
    "started_at": {"type": "string", "format": "date-time"},
    "finished_at": {"type": "string", "format": "date-time"},
    "git": {
      "type": "object",
      "required": ["sha", "dirty"],
      "properties": {
        "sha": {"type": "string"},
        "dirty": {"type": "boolean"},
        "branch": {"type": "string"}
      }
    },
    "config": {
      "type": "object",
      "required": ["sha256", "settings"],
      "properties": {
        "sha256": {"type": "string"},
        "settings": {"type": "object"}
      }
    },
    "timings": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["stage_name", "started_at", "finished_at", "elapsed_ms", "status"],
        "properties": {
          "stage_name": {"type": "string"},
          "started_at": {"type": "string", "format": "date-time"},
          "finished_at": {"type": "string", "format": "date-time"},
          "elapsed_ms": {"type": "number"},
          "status": {"type": "string"}
        }
      }
    },
    "stages": {
      "type": "object",
      "required": [
        "Collect", "Normalise", "Filter", "Resolve",
        "Enrich", "Validate", "Score", "Pitch", "Export", "Report"
      ]
    },
    "outputs": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "required": ["rows", "bytes", "sha256"],
        "properties": {
          "rows": {"type": "integer"},
          "bytes": {"type": "integer"},
          "sha256": {"type": "string"}
        }
      }
    }
  }
}
```

---

## Not a bug, but worth knowing

1. **Context Immutability Guard:** `StageContext` must never allow arbitrary state assignment (e.g. `ctx.foo = bar`). Sharing state between stages via a mutable context dictionary reintroduces hidden dependencies. All data passing between stages must occur strictly via the typed input/output return values.
2. **Memory Footprint with Large Batches:** While the current dataset is small ($\approx 5,000$ to $20,000$ businesses), Saudi Arabia scraping expansion will scale entities to $250,000+$. Using frozen dataclasses with `__slots__ = True` reduces Python object overhead by $\approx 65\%$. At $500,000$ records, a batch occupies $\approx 180\text{ MB}$ of RAM, well within GitHub Actions' 7GB runner limit.
3. **Decoupling Pitching from Scoring:** Although `lead_score` uses `industry_priority` (+15 for high, +8 for medium), `recommended_service` in `Pitch` does not feed back into `lead_score`. Maintaining `Score` $\rightarrow$ `Pitch` separation ensures that classification logic can change without altering lead score baselines.

---

## Recommended Order of Work

```
Phase 1: Foundation (Contracts & Models)
  ├── 1.1 pipeline/contracts.py (Stage protocol, RunContext, Clock, Ports)
  └── 1.2 pipeline/models.py (RawRecord, NormalisedRecord, ..., PitchedRecord)

Phase 2: Instrumentation & Runner
  ├── 2.1 pipeline/clock.py (SystemClock, FakeClock)
  ├── 2.2 pipeline/manifest.py (RunManifest builder & JSON serializer)
  ├── 2.3 pipeline/checkpoints.py (Zstd/JSONL serializer & hash verifier)
  └── 2.4 pipeline/runner.py (PipelineRunner execution loop & timing wrapper)

Phase 3: Pure Stages Implementation & Unit Tests
  ├── 3.1 stages/normalise.py + tests/test_stage_normalise.py
  ├── 3.2 stages/filter.py + tests/test_stage_filter.py
  ├── 3.3 stages/resolve.py + tests/test_stage_resolve.py
  ├── 3.4 stages/validate.py + tests/test_stage_validate.py
  ├── 3.5 stages/score.py + tests/test_stage_score.py
  └── 3.6 stages/pitch.py + tests/test_stage_pitch.py

Phase 4: I/O Stages & Adapters
  ├── 4.1 stages/collect.py (wrap scrapers/ and master store)
  ├── 4.2 stages/enrich.py (wrap enricher.py HTTP probe with SSRF protection)
  ├── 4.3 stages/export.py (atomic CSV writes & SHA-256 computation)
  └── 4.4 stages/report.py (manifest commit & terminal summary)

Phase 5: Composition Root & CLI
  ├── 5.1 main.py (refactor into thin dependency injector calling PipelineRunner)
  └── 5.2 CLI options (--resume-from-stage, --run-id, --dry-run)
```
