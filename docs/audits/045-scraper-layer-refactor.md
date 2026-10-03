# 045 — Scraper Layer Refactoring: Declarative Registry, Resilient HTTP Infrastructure, and Validated Record Contracts

## Verdict

The current scraper layer (`scrapers/base.py`, `osm.py`, `wikidata.py`, `google_places.py`) suffers from three critical architectural flaws: **runtime-inert type contracts**, **an inverted dependency that breaks under standard packaging**, and **uncoordinated, ad-hoc HTTP networking**. Specifically, `BusinessRecord` in `scrapers/base.py:5-29` is a `TypedDict` that provides zero runtime validation, forcing each scraper to redundantly instantiate 23-key dictionaries padded with downstream scoring fields (`lead_score=0`, `completeness_score=0`, `recommended_service=None`). Concurrently, `scrapers/google_places.py:26` executes `from enricher import infer_region`, creating a catastrophic layer violation (an upstream scraper importing from a downstream enrichment stage) that triggers a hard `ModuleNotFoundError` under any `src/` layout or installed wheel.

Furthermore, all three scrapers implement fragmented, uncoordinated networking logic: Overpass uses a hardcoded 200s timeout with arbitrary 30s sleep loops, Wikidata uses 90s timeouts with 15s sleeps, and Google Places spawns per-thread `requests.Session` instances with hardcoded `time.sleep(30)` on 429s. None implement thread-safe token-bucket rate limiting, exponential backoff with jitter, or `Retry-After` header parsing. Finally, `main.py:100` hardcodes an inflexible list of scraper instances, preventing selective execution, per-source configuration, or zero-code scraper additions.

This report specifies a modular, production-grade refactoring of the scraper layer. We design: (1) a declarative plugin registry (`ScraperRegistry`) supporting `@register_scraper` decorators and auto-discovery; (2) a thread-safe, pooled `ScraperHttpClient` with token-bucket rate limiting and jittered exponential backoff; (3) a strictly validated `RawBusinessRecord` data model that decouples raw source ingestion from downstream enrichment attributes; (4) hierarchical per-scraper configurations; and (5) the complete concrete implementation for the new `BaseScraper` and registry infrastructure.

---

## Findings

### S1-1 — `BusinessRecord` is runtime-inert, enforcing zero schema boundaries and polluting scrapers with downstream state
- **Where:** `scrapers/base.py:5-29`, `scrapers/osm.py:95-119`, `scrapers/wikidata.py:75-99`, `scrapers/google_places.py:249-273`.
- **Breaks:** `TypedDict` is completely erased at runtime by Python's interpreter. Calling `BusinessRecord(...)` does not validate types, verify field ranges, check for required fields, or reject unknown keys; it behaves identically to calling `dict(...)`.
  1. A scraper can emit `name=None`, `lat="invalid"`, or `country="XYZ"`, and Python will accept it without warning. Downstream components (`dedup.py:77`, `enricher.py:79`) will silently fail, produce garbled CSV rows, or merge unrelated phone-less records together.
  2. The contract fatally couples ingestion with downstream pipeline stages. Scrapers are forced to populate 8 downstream lifecycle fields with dummy values: `completeness_score=0`, `lead_score=0`, `industry_priority=None`, `recommended_service=None`, `website_live=None`, `whatsapp=None`, `linkedin=None`. If a scraper omits one of these dummy keys, `write_csv` (`main.py:76`) may write an empty cell or throw a `KeyError` depending on writer configuration.
- **Trigger:** In `scrapers/osm.py`, omit `whatsapp=None` or pass `lat=999.0` and `name=""`. The constructor executes cleanly; no exception is raised, and the malformed row enters the pipeline.
- **Fix:** Replace `TypedDict` with a validated runtime model (`RawBusinessRecord`) using Pydantic V2 or validated dataclasses. Strip downstream lifecycle fields (`lead_score`, `recommended_service`, `completeness_score`, etc.) out of the ingestion layer entirely.

### S1-2 — Upstream scraper imports downstream `enricher`, causing fatal packaging failure under `src` layout
- **Where:** `scrapers/google_places.py:26` (`from enricher import infer_region`) and `scrapers/google_places.py:243`.
- **Breaks:** This is a severe architectural inversion and packaging blocker:
  1. **Layer Violation:** Ingestion (scraping) sits at the top of the ETL pipeline. Enrichment sits downstream. Having a scraper import from `enricher.py` creates a reverse dependency cycle between pipeline stages.
  2. **Packaging Failure:** The import `from enricher import infer_region` works exclusively when `leadminer/` is the current working directory and Python places the repository root at `sys.path[0]`. In a standard PEP 518/621 `src/` layout (e.g. `src/leadminer/scrapers/google_places.py`), running the scraper or installing the package via pip triggers an immediate crash: `ModuleNotFoundError: No module named 'enricher'`.
  3. **Operational Inconsistency:** `enricher.py:324` already iterates over all records and calls `infer_region(r.get("address"), r.get("lat"), r.get("lon"), r.get("country", "LB"))`. `osm.py` and `wikidata.py` correctly emit `region=None` and let the enricher do its job. `google_places.py` arbitrarily duplicates this logic during ingestion.
- **Trigger:** Move files to `src/leadminer/` and run `python -m leadminer.scrapers.google_places` or `pytest`.
- **Fix:** Delete `from enricher import infer_region` from `scrapers/google_places.py`. Allow `google_places.py` to yield `region=None`, delegating all region inference to the downstream enrichment stage (or extract geographic utilities into a dedicated, low-level `domain/geo.py` module).

### S2-1 — Uncoordinated, ad-hoc HTTP networking and brittle rate-limiting logic
- **Where:** `scrapers/osm.py:34-54`, `scrapers/wikidata.py:40-56`, `scrapers/google_places.py:148-155, 178, 209-217`.
- **Breaks:** Each scraper independently invents networking, session management, retry logic, and sleep intervals:
  1. `osm.py:38-53` attempts 3 retries with a rigid `time.sleep(30)` on any `RequestException` (including DNS drops and 400 Bad Requests) and sets an excessive 200s timeout on Overpass.
  2. `wikidata.py:40-56` attempts 3 retries with a rigid `time.sleep(15)` and a 90s timeout.
  3. `google_places.py:178` instantiates a brand new `requests.Session()` per thread query (68 queries = 68 sessions!), discarding connection pooling and TCP keep-alive benefits. On HTTP 429, it executes `time.sleep(30)` while holding a worker thread in the thread pool, starving other queries.
  4. None of the scrapers parse or honor `Retry-After` HTTP headers.
  5. None implement exponential backoff with full jitter to avoid thundering herd problems against upstream APIs.
  6. Rate limiting is either absent (risking immediate IP blacklisting on Overpass and Wikidata) or hardcoded as arbitrary `time.sleep(2)` calls.
- **Trigger:** A transient network hiccup or temporary Overpass 504 gateway timeout causes thread blocking for 30 seconds, followed by complete abandonment on attempt 3 without intelligent backoff.
- **Fix:** Build a centralized, thread-safe `ScraperHttpClient` that manages connection pooling, configurable per-host token-bucket rate limiting, exponential backoff with jitter, and automatic `Retry-After` header extraction.

### S2-2 — Hardcoded scraper orchestration prevents modular execution and per-scraper configuration
- **Where:** `main.py:100` (`scrapers = [OSMScraper(), WikidataScraper(), GooglePlacesScraper()]`), `scrapers/google_places.py:44-124, 141`, `scrapers/osm.py:35`.
- **Breaks:** The scraper execution list is hardcoded in `main.py`. Scraper parameters—including query lists, target countries, API keys, worker counts, and endpoints—are scattered across module-level constants and direct `os.environ.get()` calls.
  1. Adding a new scraper (e.g. YellowPages, LinkedIn, KSA Commercial Registry) requires modifying `main.py` directly.
  2. Running a targeted scrape (e.g. only OSM for Lebanon, or only Google Places for Riyadh) is impossible without editing source code.
  3. Scrapers cannot be unit tested in isolation without monkey-patching module constants or mocking environment variables.
- **Trigger:** A user needs to execute a lightweight run refreshing only Google Places data in KSA without triggering Overpass or Wikidata.
- **Fix:** Implement a declarative plugin registry (`ScraperRegistry`) where scrapers register via `@register_scraper`. Pair this with a typed configuration hierarchy (`BaseScraperConfig`) allowing per-scraper overrides via YAML or environment variables.

---

## Architectural Specification

```
                                  CLI / Pipeline Runner
                                            │
                                            ▼
                                   ScraperRegistry
                     ┌──────────────────────┼──────────────────────┐
                     ▼                      ▼                      ▼
               OSMScraper             WikidataScraper     GooglePlacesScraper
             (BaseScraper)             (BaseScraper)         (BaseScraper)
                     │                      │                      │
                     └──────────────────────┼──────────────────────┘
                                            │ uses
                                            ▼
                                    ScraperHttpClient
                        ┌───────────────────┴───────────────────┐
                        ▼                                       ▼
               TokenBucketLimiter                      RetryEngine (Jitter)
                        │                                       │
                        └───────────────────┬───────────────────┘
                                            │ emits
                                            ▼
                                    RawBusinessRecord
                                (Strict Pydantic / Model)
                                            │
                                            ▼
                               [Downstream Pipeline]
                        (Whitelist -> Dedup -> Enricher)
```

### 1. Data Contract Separation: `RawBusinessRecord` vs. `EnrichedBusinessRecord`
The data model must strictly differentiate between **raw observed facts** emitted by scrapers and **derived business intelligence** computed by downstream pipeline stages.

- **`RawBusinessRecord` (Ingestion Layer):** Contains only what external data sources provide. Validates coordinate ranges (latitude between -90 and 90, longitude between -180 and 180), ensures non-empty business names, standardizes country codes to ISO 3166-1 alpha-2 ("LB", "SA"), normalizes web URLs, and preserves source metadata.
- **`EnrichedBusinessRecord` (Downstream Layer):** Derived attributes such as `completeness_score`, `lead_score`, `industry_priority`, `recommended_service`, `region`, and `website_live` are initialized during deduplication and enrichment stages, not inside the scrapers.

### 2. Thread-Safe `ScraperHttpClient` with Token Bucket Rate Limiting
Public APIs (Overpass, Wikidata) enforce strict usage policies. Overpass limits concurrent slots, while Wikidata requires polite pacing and identifying User-Agent headers. The shared HTTP client provides:
- **Connection Pooling:** Shared `requests.Session` with an `HTTPAdapter` configured for connection pooling (`pool_connections=20`, `pool_maxsize=20`).
- **Token Bucket Rate Limiting:** A thread-safe token bucket limiter ensures that queries to a given host never exceed configured Requests Per Second (RPS) / Queries Per Second (QPS).
- **Decorrelated Jitter Exponential Backoff:** Retries on 429, 500, 502, 503, 504, and network timeouts using exponential backoff with full jitter to avoid synchronous request clustering.
- **`Retry-After` Respect:** Explicit parsing of `Retry-After` headers (supporting both integer seconds and RFC 2822 date formats).
- **Compliance Headers:** Automatic injection of identifying `User-Agent` strings formatted according to Wikimedia and OSM bot policies (`leadminer/1.0 (+https://github.com/...; contact: <email>)`).

### 3. Declarative Plugin Registry Pattern
Scrapers register dynamically using a class decorator:
```python
@register_scraper(
    name="google_places",
    description="Google Places Text Search API v1",
    supported_countries=["LB", "SA"],
    requires_api_key=True,
    default_enabled=True,
)
class GooglePlacesScraper(BaseScraper[GooglePlacesConfig]):
    ...
```
The registry provides:
- `ScraperRegistry.register(name, meta)`: Decorator registering scraper classes.
- `ScraperRegistry.get(name)`: Retrieves scraper class by identifier.
- `ScraperRegistry.create(name, config, http_client)`: Instantiates a configured scraper.
- `ScraperRegistry.list_available(country=None)`: Filters scrapers by target market.
- `ScraperRegistry.auto_discover()`: Automatically imports all modules in the `scrapers/` package so decorators execute without manual import lists.

### 4. Per-Scraper Configuration Hierarchy
Each scraper declares its own strongly typed configuration model inheriting from `BaseScraperConfig`. This enables:
- Scraper-specific fields (e.g. `api_key`, `queries`, `field_mask` for Google; `endpoint`, `sparql_query` for Wikidata; `overpass_query` for OSM).
- Common configuration fields (`enabled`, `timeout`, `max_retries`, `rate_limit_qps`, `user_agent_email`).
- Unified loading from YAML config files or environment variable prefixes (`LEADMINER_SCRAPER_GOOGLE_PLACES_API_KEY`).

---

## Concrete Implementation

Here is the complete, production-grade code for the refactored scraper layer.

### 1. Ingestion Data Contract: `leadminer/domain/models.py`

```python
"""
Core domain models for leadminer ingestion.
Enforces runtime validation, coordinate boundaries, and type safety.
"""

from __future__ import annotations

import datetime
import re
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RawBusinessRecord(BaseModel):
    """
    Strict runtime data contract representing an immutable observation
    emitted by an upstream scraper.
    """
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=1, description="Business name")
    source: str = Field(..., min_length=1, description="Originating scraper identifier")
    scraped_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc),
        description="UTC timestamp of the observation",
    )
    country: str = Field("LB", min_length=2, max_length=2, description="ISO 3166-1 alpha-2 code")
    category: str | None = Field(default=None, description="Raw category or tag from source")
    address: str | None = Field(default=None, description="Physical address")
    lat: float | None = Field(default=None, ge=-90.0, le=90.0, description="Latitude in decimal degrees")
    lon: float | None = Field(default=None, ge=-180.0, le=180.0, description="Longitude in decimal degrees")
    phone: str | None = Field(default=None, description="Raw phone number")
    email: str | None = Field(default=None, description="Contact email")
    website: str | None = Field(default=None, description="Website URL")
    facebook: str | None = Field(default=None, description="Facebook profile or URL")
    instagram: str | None = Field(default=None, description="Instagram handle or URL")
    rating: float | None = Field(default=None, ge=0.0, le=5.0, description="User rating")
    review_count: int | None = Field(default=None, ge=0, description="Total review count")
    raw_payload: dict[str, Any] | None = Field(default=None, description="Raw source response payload")

    @field_validator("country", mode="before")
    @classmethod
    def normalize_country(cls, v: Any) -> str:
        if not v or not isinstance(v, str):
            return "LB"
        code = v.strip().upper()
        if code in ("LEBANON", "LBN"):
            return "LB"
        if code in ("SAUDI ARABIA", "KSA", "SAU"):
            return "SA"
        return code

    @field_validator("website", mode="before")
    @classmethod
    def normalize_website(cls, v: Any) -> str | None:
        if not v or not isinstance(v, str):
            return None
        cleaned = v.strip()
        if not cleaned:
            return None
        if not re.match(r"^https?://", cleaned, re.IGNORECASE):
            cleaned = f"https://{cleaned}"
        try:
            parsed = urlparse(cleaned)
            if not parsed.netloc:
                return None
            return cleaned
        except Exception:
            return None

    @field_validator("email", mode="before")
    @classmethod
    def sanitize_email(cls, v: Any) -> str | None:
        if not v or not isinstance(v, str):
            return None
        cleaned = v.strip().lower()
        if "@" not in cleaned or len(cleaned) < 5:
            return None
        return cleaned

    @field_validator("phone", mode="before")
    @classmethod
    def sanitize_phone(cls, v: Any) -> str | None:
        if not v or not isinstance(v, str):
            return None
        cleaned = v.strip()
        return cleaned if len(cleaned) >= 5 else None

    @model_validator(mode="after")
    def validate_coordinates(self) -> RawBusinessRecord:
        # Require paired coordinates: either both are present or both are None
        if (self.lat is None and self.lon is not None) or (self.lat is not None and self.lon is None):
            raise ValueError(f"Incomplete coordinate pair: lat={self.lat}, lon={self.lon}")
        return self
```

---

### 2. Resilient Shared HTTP Infrastructure: `leadminer/infrastructure/http.py`

```python
"""
Thread-safe, rate-limited HTTP client with jittered exponential backoff
and connection pooling for web scraping operations.
"""

from __future__ import annotations

import email.utils
import logging
import random
import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("leadminer.http")


class TokenBucketRateLimiter:
    """
    Thread-safe token bucket rate limiter to throttle requests per host.
    """
    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self.rate = float(rate)  # tokens added per second
        self.capacity = float(capacity if capacity is not None else rate)
        self.tokens = self.capacity
        self.last_update = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.last_update
                self.last_update = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return

                deficit = tokens - self.tokens
                wait_time = deficit / self.rate
                time.sleep(wait_time)


class ScraperHttpClient:
    """
    Production HTTP client wrapper providing rate-limiting, jittered retry,
    connection pooling, and standardized User-Agent management.
    """
    def __init__(
        self,
        default_timeout: float = 30.0,
        max_retries: int = 4,
        backoff_factor: float = 1.5,
        default_rate_limit_qps: float = 5.0,
        pool_connections: int = 25,
        pool_maxsize: int = 25,
        user_agent_email: str = "voxire.tech@gmail.com",
    ) -> None:
        self.default_timeout = default_timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.user_agent_email = user_agent_email
        self._session = requests.Session()

        # Mount connection pooling adapter
        adapter = HTTPAdapter(
            pool_connections=pool_connections,
            pool_maxsize=pool_maxsize,
            max_retries=False,  # Custom retry engine handles application-level backoff
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        # Host-specific rate limiters
        self._limiters: dict[str, TokenBucketRateLimiter] = {}
        self._limiters_lock = threading.Lock()
        self.default_rate_limit_qps = default_rate_limit_qps

    def _get_limiter(self, url: str, custom_qps: float | None = None) -> TokenBucketRateLimiter:
        host = urlparse(url).netloc.lower()
        with self._limiters_lock:
            if host not in self._limiters:
                qps = custom_qps or self.default_rate_limit_qps
                self._limiters[host] = TokenBucketRateLimiter(rate=qps)
            return self._limiters[host]

    def _parse_retry_after(self, response: requests.Response) -> float | None:
        retry_after = response.headers.get("Retry-After")
        if not retry_after:
            return None
        try:
            return float(retry_after)
        except ValueError:
            try:
                date_tuple = email.utils.parsedate_tz(retry_after)
                if date_tuple:
                    target_time = email.utils.mktime_tz(date_tuple)
                    return max(0.0, target_time - time.time())
            except Exception:
                pass
        return None

    def request(
        self,
        method: str,
        url: str,
        *,
        timeout: float | None = None,
        rate_limit_qps: float | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        limiter = self._get_limiter(url, custom_qps=rate_limit_qps)
        req_timeout = timeout or self.default_timeout

        req_headers = {
            "User-Agent": f"leadminer/1.0 (+https://github.com/voxire/leadminer; contact: {self.user_agent_email})",
            "Accept-Encoding": "gzip, deflate",
        }
        if headers:
            req_headers.update(headers)

        attempt = 0
        while attempt <= self.max_retries:
            limiter.acquire(1.0)
            attempt += 1

            try:
                resp = self._session.request(
                    method=method,
                    url=url,
                    headers=req_headers,
                    timeout=req_timeout,
                    **kwargs,
                )

                # Return on successful response or non-retryable 4xx (except 429)
                if resp.status_code < 400 or (
                    resp.status_code < 500 and resp.status_code != 429 and resp.status_code != 408
                ):
                    return resp

                if attempt > self.max_retries:
                    resp.raise_for_status()
                    return resp

                # Determine backoff duration
                server_wait = self._parse_retry_after(resp)
                if server_wait is not None:
                    sleep_time = server_wait + random.uniform(0.1, 0.5)
                    logger.warning(f"HTTP {resp.status_code} for {url}. Respecting Retry-After: {sleep_time:.2f}s")
                else:
                    # Exponential backoff with Full Jitter
                    max_backoff = self.backoff_factor * (2 ** (attempt - 1))
                    sleep_time = random.uniform(0.5, max_backoff)
                    logger.warning(
                        f"HTTP {resp.status_code} on attempt {attempt}/{self.max_retries} for {url}. "
                        f"Backing off for {sleep_time:.2f}s..."
                    )

                time.sleep(sleep_time)

            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt > self.max_retries:
                    logger.error(f"Network error on final attempt {attempt} for {url}: {exc}")
                    raise
                max_backoff = self.backoff_factor * (2 ** (attempt - 1))
                sleep_time = random.uniform(0.5, max_backoff)
                logger.warning(
                    f"Transient network exception ({type(exc).__name__}) on attempt {attempt} for {url}: {exc}. "
                    f"Retrying in {sleep_time:.2f}s..."
                )
                time.sleep(sleep_time)

        raise RuntimeError(f"Unexpected termination of retry loop for {url}")

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)
```

---

### 3. Base Scraper and Plugin Infrastructure: `leadminer/scrapers/base.py`

```python
"""
Abstract base classes and configurations for leadminer scrapers.
"""

from __future__ import annotations

import abc
import dataclasses
from typing import ClassVar, Generic, Iterator, TypeVar

from leadminer.domain.models import RawBusinessRecord
from leadminer.infrastructure.http import ScraperHttpClient


@dataclasses.dataclass(frozen=True)
class ScraperMeta:
    """Declarative metadata describing a scraper plugin."""
    name: str
    description: str
    supported_countries: list[str]
    requires_api_key: bool = False
    default_enabled: bool = True
    tags: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class BaseScraperConfig:
    """Base configuration common to all scrapers."""
    enabled: bool = True
    timeout: float = 30.0
    max_retries: int = 3
    rate_limit_qps: float = 5.0
    user_agent_email: str = "voxire.tech@gmail.com"


ConfigT = TypeVar("ConfigT", bound=BaseScraperConfig)


class BaseScraper(abc.ABC, Generic[ConfigT]):
    """
    Abstract base class for all scraper plugins.
    Enforces typed configuration, shared HTTP client injection,
    and a standardized generator contract.
    """
    meta: ClassVar[ScraperMeta]

    def __init__(self, config: ConfigT, http_client: ScraperHttpClient) -> None:
        self.config = config
        self.http_client = http_client

    @abc.abstractmethod
    def scrape(self) -> Iterator[RawBusinessRecord]:
        """
        Execute the scraping process and yield validated raw business records.
        Must handle its own error recovery or let fatal errors propagate cleanly.
        """
        raise NotImplementedError

    def validate_prerequisites(self) -> bool:
        """
        Verify that external dependencies, environment variables, or credentials
        are satisfied before running. Defaults to True.
        """
        return True

    def get_progress(self) -> dict[str, int]:
        """Optional hook to report scraping progress metrics."""
        return {}
```

---

### 4. Plugin Registry: `leadminer/scrapers/registry.py`

```python
"""
Plugin registry for leadminer scrapers supporting declarative registration,
auto-discovery, and factory instantiation.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any, Callable, Type

from leadminer.infrastructure.http import ScraperHttpClient
from leadminer.scrapers.base import BaseScraper, BaseScraperConfig, ScraperMeta

logger = logging.getLogger("leadminer.registry")


class ScraperRegistry:
    """
    Central registry for scraper plugins.
    """
    _registry: dict[str, Type[BaseScraper[Any]]] = {}
    _metadata: dict[str, ScraperMeta] = {}

    @classmethod
    def register(
        cls,
        name: str,
        description: str,
        supported_countries: list[str],
        requires_api_key: bool = False,
        default_enabled: bool = True,
        tags: list[str] | None = None,
    ) -> Callable[[Type[BaseScraper[Any]]], Type[BaseScraper[Any]]]:
        """
        Decorator to register a BaseScraper implementation.
        """
        def decorator(scraper_cls: Type[BaseScraper[Any]]) -> Type[BaseScraper[Any]]:
            meta = ScraperMeta(
                name=name,
                description=description,
                supported_countries=[c.upper() for c in supported_countries],
                requires_api_key=requires_api_key,
                default_enabled=default_enabled,
                tags=tags or [],
            )
            scraper_cls.meta = meta
            cls._registry[name] = scraper_cls
            cls._metadata[name] = meta
            logger.debug(f"Registered scraper: {name} ({scraper_cls.__name__})")
            return scraper_cls

        return decorator

    @classmethod
    def get(cls, name: str) -> Type[BaseScraper[Any]]:
        if name not in cls._registry:
            raise KeyError(f"Scraper '{name}' not found. Available: {list(cls._registry.keys())}")
        return cls._registry[name]

    @classmethod
    def get_meta(cls, name: str) -> ScraperMeta:
        if name not in cls._metadata:
            raise KeyError(f"Metadata for scraper '{name}' not found.")
        return cls._metadata[name]

    @classmethod
    def list_scrapers(cls, country: str | None = None) -> list[ScraperMeta]:
        """List metadata for all registered scrapers, optionally filtered by country."""
        if not country:
            return list(cls._metadata.values())
        country_code = country.upper()
        return [
            meta for meta in cls._metadata.values()
            if country_code in meta.supported_countries
        ]

    @classmethod
    def create(
        cls,
        name: str,
        config: BaseScraperConfig,
        http_client: ScraperHttpClient,
    ) -> BaseScraper[Any]:
        """Instantiate a registered scraper with configuration and HTTP client."""
        scraper_cls = cls.get(name)
        return scraper_cls(config, http_client)

    @classmethod
    def auto_discover(cls, package_name: str = "leadminer.scrapers") -> None:
        """Dynamically import all modules in the scrapers package to trigger registrations."""
        try:
            package = importlib.import_module(package_name)
        except ImportError as e:
            logger.warning(f"Could not import package '{package_name}' for discovery: {e}")
            return

        if not hasattr(package, "__path__"):
            return

        for _, module_name, is_pkg in pkgutil.walk_packages(package.__path__, f"{package_name}."):
            if not is_pkg and not module_name.endswith(".base") and not module_name.endswith(".registry"):
                try:
                    importlib.import_module(module_name)
                except Exception as e:
                    logger.error(f"Failed to load scraper module '{module_name}': {e}")
```

---

### 5. Concrete Refactored Scraper: `leadminer/scrapers/google_places.py`

This refactored implementation eliminates the broken `from enricher import infer_region` import, adopts the typed configuration, uses the shared `ScraperHttpClient`, and outputs strictly validated `RawBusinessRecord` objects.

```python
"""
Google Places API scraper plugin.
Scrapes Lebanon and KSA using Google Places Text Search (New API v1).
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterator

from leadminer.domain.models import RawBusinessRecord
from leadminer.infrastructure.http import ScraperHttpClient
from leadminer.scrapers.base import BaseScraper, BaseScraperConfig
from leadminer.scrapers.registry import ScraperRegistry

logger = logging.getLogger("leadminer.scrapers.google_places")

PLACES_API_URL = "https://places.googleapis.com/v1/places:searchText"

FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.location",
    "places.websiteUri",
    "places.nationalPhoneNumber",
    "places.types",
    "places.rating",
    "places.userRatingCount",
    "nextPageToken",
])

_GENERIC_TYPES = {
    "establishment", "point_of_interest", "place", "premise",
    "subpremise", "street_address", "route", "country",
    "administrative_area_level_1", "administrative_area_level_2",
    "administrative_area_level_3", "locality", "sublocality",
    "neighborhood", "postal_code",
}


@dataclasses.dataclass
class GooglePlacesConfig(BaseScraperConfig):
    """Configuration for Google Places Text Search scraper."""
    api_key: str | None = None
    rate_limit_qps: float = 4.0
    timeout: float = 25.0
    workers: int = 5
    queries_lb: list[str] = dataclasses.field(default_factory=lambda: [
        "restaurants in Lebanon", "cafes in Lebanon", "hotels in Lebanon",
        "boutique hotels in Lebanon", "fashion stores in Lebanon", "hospitals in Lebanon",
        "dental clinics in Lebanon", "real estate developers in Lebanon", "law firms in Lebanon",
    ])
    queries_sa: list[str] = dataclasses.field(default_factory=lambda: [
        "fashion brands in Riyadh", "perfume brands in Saudi Arabia", "restaurant groups in Riyadh",
        "boutique hotels in Riyadh", "real estate developers in Riyadh", "fintech companies in Riyadh",
    ])


@ScraperRegistry.register(
    name="google_places",
    description="Google Places API Text Search v1 (Lebanon & KSA)",
    supported_countries=["LB", "SA"],
    requires_api_key=True,
    default_enabled=True,
    tags=["places", "commercial", "geo"],
)
class GooglePlacesScraper(BaseScraper[GooglePlacesConfig]):
    """
    Production-grade Google Places scraper conforming to BaseScraper.
    Does NOT depend on downstream enricher.py for region inference.
    """
    def __init__(self, config: GooglePlacesConfig, http_client: ScraperHttpClient) -> None:
        super().__init__(config, http_client)
        if not self.config.api_key:
            self.config.api_key = os.environ.get("GOOGLE_PLACES_API_KEY")

    def validate_prerequisites(self) -> bool:
        if not self.config.api_key:
            logger.warning("[GooglePlaces] GOOGLE_PLACES_API_KEY is not configured. Skipping.")
            return False
        return True

    def _infer_country_from_query(self, query: str) -> str:
        q = query.lower()
        if any(term in q for term in ["saudi", "riyadh", "jeddah", "dammam", "mecca", "medina"]):
            return "SA"
        return "LB"

    def _pick_category(self, types: list[str]) -> str | None:
        for t in types:
            if t and t not in _GENERIC_TYPES:
                return t
        return types[0] if types else None

    def scrape(self) -> Iterator[RawBusinessRecord]:
        if not self.validate_prerequisites():
            return

        queries: list[str] = []
        if "LB" in self.meta.supported_countries:
            queries.extend(self.config.queries_lb)
        if "SA" in self.meta.supported_countries:
            queries.extend(self.config.queries_sa)

        logger.info(f"[GooglePlaces] Scraping {len(queries)} queries with {self.config.workers} workers...")

        seen_place_ids: set[str] = set()

        def process_query(query: str) -> list[RawBusinessRecord]:
            country = self._infer_country_from_query(query)
            headers = {
                "X-Goog-Api-Key": self.config.api_key or "",
                "X-Goog-FieldMask": FIELD_MASK,
                "Content-Type": "application/json",
            }
            records: list[RawBusinessRecord] = []
            page_token = None
            page = 0

            while True:
                body: dict[str, Any] = {"textQuery": query}
                if page_token:
                    body["pageToken"] = page_token

                try:
                    resp = self.http_client.post(
                        PLACES_API_URL,
                        json=body,
                        headers=headers,
                        timeout=self.config.timeout,
                        rate_limit_qps=self.config.rate_limit_qps,
                    )
                    if resp.status_code == 401:
                        logger.error("[GooglePlaces] Invalid API Key.")
                        return records
                    resp.raise_for_status()
                except Exception as e:
                    logger.error(f"[GooglePlaces] Error fetching query '{query}': {e}")
                    return records

                data = resp.json()
                places = data.get("places", [])
                page += 1
                scraped_at = datetime.datetime.now(datetime.timezone.utc)

                for place in places:
                    place_id = place.get("id")
                    if not place_id or place_id in seen_place_ids:
                        continue
                    seen_place_ids.add(place_id)

                    name = (place.get("displayName") or {}).get("text") or ""
                    if not name:
                        continue

                    location = place.get("location") or {}
                    lat = location.get("latitude")
                    lon = location.get("longitude")
                    types = place.get("types") or []

                    try:
                        record = RawBusinessRecord(
                            name=name,
                            category=self._pick_category(types),
                            country=country,
                            address=place.get("formattedAddress"),
                            lat=lat,
                            lon=lon,
                            phone=place.get("nationalPhoneNumber"),
                            website=place.get("websiteUri"),
                            rating=place.get("rating"),
                            review_count=place.get("userRatingCount"),
                            source="google_places",
                            scraped_at=scraped_at,
                            raw_payload={"id": place_id, "types": types},
                        )
                        records.append(record)
                    except ValueError as err:
                        logger.debug(f"[GooglePlaces] Dropping invalid record '{name}': {err}")

                page_token = data.get("nextPageToken")
                if not page_token:
                    break

            return records

        with ThreadPoolExecutor(max_workers=self.config.workers) as pool:
            futures = [pool.submit(process_query, q) for q in queries]
            for future in as_completed(futures):
                try:
                    batch = future.result()
                    yield from batch
                except Exception as e:
                    logger.error(f"[GooglePlaces] Query worker failed: {e}")
```

---

### 6. Concrete Refactored Scraper: `leadminer/scrapers/osm.py`

```python
"""
OpenStreetMap (Overpass API) scraper plugin.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
from typing import Iterator

from leadminer.domain.models import RawBusinessRecord
from leadminer.infrastructure.http import ScraperHttpClient
from leadminer.scrapers.base import BaseScraper, BaseScraperConfig
from leadminer.scrapers.registry import ScraperRegistry

logger = logging.getLogger("leadminer.scrapers.osm")

DEFAULT_OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"

OVERPASS_LB_QUERY = """
[out:json][timeout:180];
area["ISO3166-1"="LB"]->.lb;
(
  node["name"]["shop"](area.lb);
  node["name"]["amenity"](area.lb);
  node["name"]["office"](area.lb);
  node["name"]["tourism"](area.lb);
  node["name"]["craft"](area.lb);
  node["name"]["healthcare"](area.lb);
  way["name"]["shop"](area.lb);
  way["name"]["amenity"](area.lb);
  way["name"]["office"](area.lb);
  way["name"]["tourism"](area.lb);
);
out center tags;
"""


@dataclasses.dataclass
class OSMConfig(BaseScraperConfig):
    endpoint: str = DEFAULT_OVERPASS_ENDPOINT
    rate_limit_qps: float = 0.5  # Polite Overpass pacing: 1 request per 2 seconds
    timeout: float = 180.0
    query: str = OVERPASS_LB_QUERY


@ScraperRegistry.register(
    name="osm",
    description="OpenStreetMap Overpass API for Lebanon",
    supported_countries=["LB"],
    default_enabled=True,
    tags=["osm", "gis", "open_data"],
)
class OSMScraper(BaseScraper[OSMConfig]):
    """
    Overpass API scraper utilizing shared HTTP client and emitting RawBusinessRecord.
    """
    def scrape(self) -> Iterator[RawBusinessRecord]:
        logger.info(f"[OSM] Querying Overpass API endpoint: {self.config.endpoint}")
        scraped_at = datetime.datetime.now(datetime.timezone.utc)

        try:
            resp = self.http_client.post(
                self.config.endpoint,
                data={"data": self.config.query},
                timeout=self.config.timeout,
                rate_limit_qps=self.config.rate_limit_qps,
            )
            resp.raise_for_status()
            elements = resp.json().get("elements", [])
        except Exception as e:
            logger.error(f"[OSM] Query failed: {e}")
            return

        logger.info(f"[OSM] Received {len(elements)} raw elements from Overpass.")

        for el in elements:
            tags = el.get("tags", {})
            name = tags.get("name") or tags.get("name:en") or tags.get("name:ar")
            if not name:
                continue

            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")

            category = (
                tags.get("shop")
                or tags.get("amenity")
                or tags.get("office")
                or tags.get("tourism")
                or tags.get("craft")
                or tags.get("healthcare")
            )

            addr_parts = [
                tags.get("addr:housenumber"),
                tags.get("addr:street"),
                tags.get("addr:suburb"),
                tags.get("addr:city"),
                tags.get("addr:district"),
            ]
            address = ", ".join(p for p in addr_parts if p) or None

            phone = tags.get("phone") or tags.get("contact:phone")
            email = tags.get("email") or tags.get("contact:email")
            website = tags.get("website") or tags.get("contact:website") or tags.get("url")
            facebook = tags.get("contact:facebook") or tags.get("facebook")
            instagram = tags.get("contact:instagram") or tags.get("instagram")

            try:
                yield RawBusinessRecord(
                    name=name,
                    category=category,
                    address=address,
                    country="LB",
                    lat=lat,
                    lon=lon,
                    phone=phone,
                    email=email,
                    website=website,
                    facebook=facebook,
                    instagram=instagram,
                    source="osm",
                    scraped_at=scraped_at,
                    raw_payload={"osm_id": el.get("id"), "osm_type": el.get("type")},
                )
            except ValueError as err:
                logger.debug(f"[OSM] Dropping element with invalid data: {err}")
```

---

### 7. Orchestration Integration Example: `leadminer/pipeline/runner.py`

This demonstrates how `main.py` uses the registry dynamically instead of hardcoding scraper instances:

```python
"""
Orchestration layer utilizing ScraperRegistry for dynamic execution.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterator

from leadminer.domain.models import RawBusinessRecord
from leadminer.infrastructure.http import ScraperHttpClient
from leadminer.scrapers.base import BaseScraperConfig
from leadminer.scrapers.registry import ScraperRegistry

logger = logging.getLogger("leadminer.orchestrator")


def run_scrapers(
    target_country: str | None = None,
    enabled_scrapers: list[str] | None = None,
    http_client: ScraperHttpClient | None = None,
) -> list[RawBusinessRecord]:
    """
    Dynamically discover, instantiate, and execute all active scrapers in parallel.
    """
    # 1. Discover all plugins dynamically
    ScraperRegistry.auto_discover()
    client = http_client or ScraperHttpClient()

    # 2. Filter scrapers by country and enabled list
    available_metas = ScraperRegistry.list_scrapers(country=target_country)
    active_scrapers = []

    for meta in available_metas:
        if enabled_scrapers and meta.name not in enabled_scrapers:
            continue
        if not meta.default_enabled and (not enabled_scrapers or meta.name not in enabled_scrapers):
            continue

        # In production, custom per-scraper configs are loaded here
        config = BaseScraperConfig()
        scraper_instance = ScraperRegistry.create(meta.name, config, client)
        if scraper_instance.validate_prerequisites():
            active_scrapers.append(scraper_instance)

    logger.info(f"Executing {len(active_scrapers)} active scrapers: {[s.meta.name for s in active_scrapers]}")

    all_records: list[RawBusinessRecord] = []

    def execute_scraper(scraper) -> list[RawBusinessRecord]:
        logger.info(f"Starting [{scraper.meta.name}]...")
        results = list(scraper.scrape())
        logger.info(f"Completed [{scraper.meta.name}]: collected {len(results)} records")
        return results

    with ThreadPoolExecutor(max_workers=len(active_scrapers) or 1) as pool:
        futures = {pool.submit(execute_scraper, s): s.meta.name for s in active_scrapers}
        for future in as_completed(futures):
            scraper_name = futures[future]
            try:
                batch = future.result()
                all_records.extend(batch)
            except Exception as e:
                logger.error(f"Scraper [{scraper_name}] failed during execution: {e}")

    logger.info(f"Total raw observations collected: {len(all_records)}")
    return all_records
```

---

## Architectural Comparison Matrix

| Dimension | Legacy Implementation (`scrapers/*`) | Proposed Architecture (`045-refactor`) |
|---|---|---|
| **Type Contract** | `TypedDict` in `base.py` (runtime-inert; no type or boundary checks). | `RawBusinessRecord` (Pydantic V2 model; validates bounds, sanitizes URLs, enforces ISO country). |
| **Schema Scope** | Mixed: Ingestion records contain downstream scoring keys (`lead_score=0`). | Strict separation: Ingestion produces raw observations; enrichment computes scores. |
| **Packaging & Imports** | `google_places.py` imports `from enricher import infer_region` (crashes under `src/`). | Decoupled: Ingestion emits coordinates; enrichment infers regions uniformly. |
| **HTTP Management** | Fragmented: ad-hoc sessions, hardcoded timeouts (200s, 90s), unconfigured connection pools. | Centralized: `ScraperHttpClient` with pooled sessions, keep-alive, and standardized User-Agent. |
| **Rate Limiting** | None on OSM/Wikidata; crude `time.sleep(30)` on Google Places. | Thread-safe `TokenBucketRateLimiter` per host; respects `Retry-After`. |
| **Retry Strategy** | Naive `time.sleep(15)` or `time.sleep(30)` loops; crashes on attempt 3. | Exponential backoff with full jitter on 429, 500, 502, 503, 504, and network blips. |
| **Scraper Discovery** | Hardcoded list in `main.py:100` (`[OSMScraper(), WikidataScraper(), ...]`). | Declarative `ScraperRegistry` with `@register_scraper` and `auto_discover()`. |
| **Configurability** | Hardcoded query constants and scattered `os.environ.get()` calls. | Typed `BaseScraperConfig` hierarchy with per-scraper parameters and CLI overrides. |

---

## Not a Bug, but Architectural Debt

1. **Category Mapping Divergence Across Scrapers:**
   - OSM yields categories based on OSM tag hierarchy (`shop`, `amenity`, `office`).
   - Wikidata yields human-readable English entity labels (`rdfs:label`).
   - Google Places yields Google Place Types (`point_of_interest`, `lodging`, `store`).
   - While `whitelist.py` attempts to harmonize them, this mapping currently occurs downstream in an ad-hoc manner. The plugin metadata should define an optional `normalize_category()` hook so scrapers can map source-specific taxonomies into a standard system ontology early.

2. **Query String Duplication in Google Places:**
   - `LEBANON_QUERIES` (34 items) and `KSA_QUERIES` (41 items) are currently hardcoded in Python arrays in `google_places.py`. This bloats the source code and prevents marketing or sales operators from tuning target queries without code changes. Queries should be moved to external configuration files (e.g. `config/queries/lebanon.yaml` and `config/queries/ksa.yaml`).

3. **Missing Scraper Health Checks and Dry-Run Probing:**
   - In production, upstream APIs frequently change formats or deprecate endpoints. The `BaseScraper` contract provides `validate_prerequisites()`, which should be extended to support a lightweight health probe (`check_health() -> HealthStatus`) that validates connectivity and authentication before triggering multi-hour scraping runs.

---

## Recommended Order of Work

```
Step 1: Sever Inverted Import
    │   Delete `from enricher import infer_region` in google_places.py.
    │   Verify scrapers have zero dependencies on downstream modules.
    ▼
Step 2: Deploy Validated Ingestion Model
    │   Implement `RawBusinessRecord` in `leadminer/domain/models.py`.
    │   Strip downstream scoring fields from ingestion schema.
    ▼
Step 3: Build Shared HTTP Client
    │   Implement `TokenBucketRateLimiter` and `ScraperHttpClient` in `leadminer/infrastructure/http.py`.
    │   Configure exponential backoff, jitter, and Retry-After header parsing.
    ▼
Step 4: Implement BaseScraper and ScraperRegistry
    │   Define `BaseScraper`, `ScraperMeta`, and `BaseScraperConfig` in `leadminer/scrapers/base.py`.
    │   Build `ScraperRegistry` in `leadminer/scrapers/registry.py` with `@register_scraper`.
    ▼
Step 5: Migrate Concrete Scrapers
    │   Refactor `GooglePlacesScraper` to inherit from `BaseScraper`.
    │   Refactor `OSMScraper` and `WikidataScraper` to use `ScraperHttpClient`.
    ▼
Step 6: Update Pipeline Orchestrator
        Update `main.py` to use `ScraperRegistry.auto_discover()` and dynamic scraper execution.
        Add CLI flags for country filtering (`--country LB`) and selective execution (`--scrapers osm,google_places`).
```
