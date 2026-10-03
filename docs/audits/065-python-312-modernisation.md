# 065 — Modernise for Python 3.12

## Verdict

The `leadminer` repository explicitly targets Python 3.12 (`pyproject.toml:10`, `requires-python = ">=3.12"`), yet retains obsolete idioms and deprecated APIs across its core modules. Most notably, `datetime.datetime.utcnow()` is invoked in three scraper modules, emitting deprecation warnings and producing naive timestamps whose string comparison with timezone-aware ISO formats in `dedup.py` risks silent data retention inversion. Upgrading to Python 3.12 standard features—including `datetime.UTC`, PEP 695 `type` parameter syntax, `typing.override`, `collections.abc` generic imports, `itertools.batched` chunking for large network worker pools, and fine-grained exception context via `add_note()`—will harden type safety, prevent memory spikes, and align the project with contemporary standard library contracts.

---

## Findings

### S2 — Deprecated naive `datetime.datetime.utcnow()` breaks chronological `max()` deduplication

- **Where:** `scrapers/osm.py:31`, `scrapers/wikidata.py:31`, `scrapers/google_places.py:162`, interacting with `dedup.py:96`.
- **Breaks:** In Python 3.12, `datetime.datetime.utcnow()` is officially deprecated (emitting `DeprecationWarning`) and scheduled for future removal. Furthermore, `utcnow()` returns a *naive* datetime object. The codebase manually appends `"Z"` via string concatenation (`isoformat() + "Z"`). In `dedup.py:96`, `merged[key] = max(av, bv)` evaluates timestamps using Python string lexicographical sorting. If a modern timezone-aware UTC timestamp formatted with standard ISO 8601 offset (`+00:00`) enters the dataset (e.g. from an updated scraper, database, or external feed), the comparison fails silently: in ASCII, `'+'` (code 43) sorts *before* `'Z'` (code 90). Consequently, `max("2026-10-01T00:00:00Z", "2026-10-03T00:00:00+00:00")` evaluates to the older record (`"2026-10-01..."`), discarding fresh scrape updates.
- **Trigger:** Any execution under Python 3.12 emits `DeprecationWarning: datetime.datetime.utcnow() is deprecated and scheduled for removal in a future version. Use timezone-aware objects to represent datetimes in UTC: datetime.datetime.now(datetime.UTC).` Furthermore, merging records with standard aware ISO format `+00:00` against existing master records with `"Z"` silently drops the newer record.
- **Fix:** Replace all occurrences with `datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")` to emit aware UTC timestamps while preserving exact byte-level lexicographical ordering with historical master CSV data.

#### Exact Before and After: Site 1 — `scrapers/osm.py:31`

**Before:**
```python
class OSMScraper(BaseScraper):
    def scrape(self) -> Iterator[BusinessRecord]:
        scraped_at = datetime.datetime.utcnow().isoformat() + "Z"
        print("[OSM] Fetching Lebanon businesses from Overpass API...")
```

**After:**
```python
class OSMScraper(BaseScraper):
    def scrape(self) -> Iterator[BusinessRecord]:
        scraped_at = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
        print("[OSM] Fetching Lebanon businesses from Overpass API...")
```

#### Exact Before and After: Site 2 — `scrapers/wikidata.py:31`

**Before:**
```python
class WikidataScraper(BaseScraper):
    def scrape(self) -> Iterator[BusinessRecord]:
        scraped_at = datetime.datetime.utcnow().isoformat() + "Z"
        print("[Wikidata] Fetching Lebanon businesses...")
```

**After:**
```python
class WikidataScraper(BaseScraper):
    def scrape(self) -> Iterator[BusinessRecord]:
        scraped_at = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
        print("[Wikidata] Fetching Lebanon businesses...")
```

#### Exact Before and After: Site 3 — `scrapers/google_places.py:162`

**Before:**
```python
    def scrape(self) -> Iterator[BusinessRecord]:
        if not self._api_key:
            print("[Google] GOOGLE_PLACES_API_KEY not set, skipping.")
            return

        scraped_at = datetime.datetime.utcnow().isoformat() + "Z"
        all_queries = LEBANON_QUERIES + KSA_QUERIES
```

**After:**
```python
    def scrape(self) -> Iterator[BusinessRecord]:
        if not self._api_key:
            print("[Google] GOOGLE_PLACES_API_KEY not set, skipping.")
            return

        scraped_at = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
        all_queries = LEBANON_QUERIES + KSA_QUERIES
```

---

### S2 — Unbounded future allocation in `enricher.py` and query dispatching without `itertools.batched`

- **Where:** `enricher.py:229-234` and `scrapers/google_places.py:183-186`.
- **Breaks:** `check_websites()` extracts all URLs from records into `targets` and submits all of them into a `ThreadPoolExecutor` at once:
  ```python
  futures = {pool.submit(_fetch_website, url): idx for idx, url in targets}
  ```
  When processing cumulative master files containing 20,000 to 50,000+ businesses, this instantiates tens of thousands of `concurrent.futures.Future` objects and work queue items simultaneously. This causes significant unmanaged memory retention, starves the threadpool, makes progress logging non-linear, and prevents incremental garbage collection of processed payloads.
- **Trigger:** Executing `main.py` or `enrich()` against a loaded master CSV with `>10,000` URLs. Memory spikes significantly while queued futures hold references to closures, strings, and result slots.
- **Fix:** Use Python 3.12's new `itertools.batched(iterable, n)` to feed work to the executor in manageable chunks (e.g. `workers * 10 = 400` tasks per batch). This bounds in-flight futures, reduces memory overhead, and allows clean memory recycling between batches.

#### Exact Before and After: Site 1 — `enricher.py:223-255`

**Before:**
```python
def check_websites(records: list[dict], workers: int = 40) -> list[dict]:
    """Single-pass: check liveness and extract contacts in one GET per site.

    website_live is set to True (LIVE), False (DEAD) or None (UNKNOWN), so a
    site we merely failed to reach is never confused with a broken site.
    """
    targets = [(i, r["website"]) for i, r in enumerate(records) if r.get("website")]
    if not targets:
        return records

    print(f"[Enricher] Fetching {len(targets)} websites — liveness + contacts ({workers} workers)...")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_website, url): idx for idx, url in targets}
        done = 0
        for future in as_completed(futures):
            idx = futures[future]
            try:
                outcome, contacts = future.result()
            except Exception:
                # _fetch_website is already defensive, but never let one worker
                # kill a multi-hour run.
                outcome, contacts = UNKNOWN, {"email": None, "instagram": None,
                                              "whatsapp": None, "linkedin": None}
            r = records[idx]
            r["website_live"] = True if outcome == LIVE else (False if outcome == DEAD else None)
            if outcome == LIVE:
                if not r.get("email") and contacts["email"]:
                    r["email"] = contacts["email"]
                if not r.get("instagram") and contacts["instagram"]:
                    r["instagram"] = contacts["instagram"]
                if not r.get("whatsapp") and contacts["whatsapp"]:
                    r["whatsapp"] = contacts["whatsapp"]
                if not r.get("linkedin") and contacts["linkedin"]:
                    r["linkedin"] = contacts["linkedin"]
            done += 1
            if done % 200 == 0:
                print(f"[Enricher] {done}/{len(targets)} done...")
```

**After:**
```python
import itertools

def check_websites(records: list[dict], workers: int = 40) -> list[dict]:
    """Single-pass: check liveness and extract contacts in one GET per site.

    website_live is set to True (LIVE), False (DEAD) or None (UNKNOWN), so a
    site we merely failed to reach is never confused with a broken site.
    """
    targets = [(i, r["website"]) for i, r in enumerate(records) if r.get("website")]
    if not targets:
        return records

    print(f"[Enricher] Fetching {len(targets)} websites — liveness + contacts ({workers} workers)...")

    batch_size = workers * 10
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for batch in itertools.batched(targets, batch_size):
            futures = {pool.submit(_fetch_website, url): idx for idx, url in batch}
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    outcome, contacts = future.result()
                except Exception:
                    outcome, contacts = UNKNOWN, {"email": None, "instagram": None,
                                                  "whatsapp": None, "linkedin": None}
                r = records[idx]
                r["website_live"] = True if outcome == LIVE else (False if outcome == DEAD else None)
                if outcome == LIVE:
                    if not r.get("email") and contacts["email"]:
                        r["email"] = contacts["email"]
                    if not r.get("instagram") and contacts["instagram"]:
                        r["instagram"] = contacts["instagram"]
                    if not r.get("whatsapp") and contacts["whatsapp"]:
                        r["whatsapp"] = contacts["whatsapp"]
                    if not r.get("linkedin") and contacts["linkedin"]:
                        r["linkedin"] = contacts["linkedin"]
                done += 1
                if done % 200 == 0:
                    print(f"[Enricher] {done}/{len(targets)} done...")
```

#### Exact Before and After: Site 2 — `scrapers/google_places.py:183-188`

**Before:**
```python
        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            futures = [pool.submit(fetch_query, q) for q in all_queries]
            for f in as_completed(futures):
                f.result()  # re-raises any exception
```

**After:**
```python
        import itertools

        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            for query_batch in itertools.batched(all_queries, _WORKERS * 2):
                futures = [pool.submit(fetch_query, q) for q in query_batch]
                for f in as_completed(futures):
                    f.result()  # re-raises any exception
```

---

### S3 — Missing PEP 698 `@override` decorator on scraper subclasses

- **Where:** `scrapers/osm.py:29-30`, `scrapers/wikidata.py:29-30`, `scrapers/google_places.py:157`.
- **Breaks:** In Python 3.12, PEP 698 introduced `@typing.override` (and `typing_extensions.override`). `OSMScraper`, `WikidataScraper`, and `GooglePlacesScraper` all inherit from `BaseScraper` (`scrapers/base.py`) and implement `scrape(self) -> Iterator[BusinessRecord]`. Without `@override`, static type checkers (`mypy` in `tool.mypy strict = true`) cannot detect if `BaseScraper.scrape` is renamed, signature-altered, or decoupled, leading to silent interface divergence.
- **Trigger:** Refactoring `BaseScraper.scrape` (e.g. adding parameters or renaming to `iter_scrape`) leaves the subclasses unflagged by linters until runtime abstract instantiation fails.
- **Fix:** Import `override` from `typing` and decorate each scraper subclass `scrape()` method.

#### Exact Before and After: Site 1 — `scrapers/osm.py:5, 29-30`

**Before:**
```python
from typing import Iterator
from .base import BaseScraper, BusinessRecord
...
class OSMScraper(BaseScraper):
    def scrape(self) -> Iterator[BusinessRecord]:
```

**After:**
```python
from collections.abc import Iterator
from typing import override
from .base import BaseScraper, BusinessRecord
...
class OSMScraper(BaseScraper):
    @override
    def scrape(self) -> Iterator[BusinessRecord]:
```

#### Exact Before and After: Site 2 — `scrapers/wikidata.py:5, 29-30`

**Before:**
```python
from typing import Iterator
from .base import BaseScraper, BusinessRecord
...
class WikidataScraper(BaseScraper):
    def scrape(self) -> Iterator[BusinessRecord]:
```

**After:**
```python
from collections.abc import Iterator
from typing import override
from .base import BaseScraper, BusinessRecord
...
class WikidataScraper(BaseScraper):
    @override
    def scrape(self) -> Iterator[BusinessRecord]:
```

#### Exact Before and After: Site 3 — `scrapers/google_places.py:22, 157`

**Before:**
```python
from typing import Iterator
...
class GooglePlacesScraper(BaseScraper):
    ...
    def scrape(self) -> Iterator[BusinessRecord]:
```

**After:**
```python
from collections.abc import Iterator
from typing import override
...
class GooglePlacesScraper(BaseScraper):
    ...
    @override
    def scrape(self) -> Iterator[BusinessRecord]:
```

---

### S3 — Outdated typing imports and unparameterized collection types

- **Where:** `scrapers/base.py:2`, `pitch_recommender.py:22, 25`, `scrapers/google_places.py:194`, `dedup.py:103-104`.
- **Breaks:**
  1. `scrapers/base.py`, `scrapers/osm.py`, `scrapers/wikidata.py`, and `scrapers/google_places.py` import `Iterator` from `typing`. Under PEP 585 (Python 3.9+) and Python 3.12 conventions, importing generic collections from `typing` is deprecated in favor of `collections.abc.Iterator`.
  2. `pitch_recommender.py:22` imports `Mapping` from `typing` and uses it unparameterized (`def recommend_service(record: Mapping) -> str:`). This raises `ruff` UP006/UP035 warnings and causes mypy strict checks to fail under Python 3.12.
  3. `scrapers/google_places.py:194` types `seen_ids` as a bare `set` instead of `set[str]`.
  4. `dedup.py:104` annotates `name_index: dict[tuple, dict] = {}` with bare unparameterized `tuple` and `dict`.
- **Trigger:** Running `mypy --strict .` or `ruff check --select UP,RUF .` on the codebase flags all bare/deprecated typing imports.
- **Fix:** Replace `typing.Iterator` and `typing.Mapping` with `collections.abc.Iterator` and `collections.abc.Mapping`, and fully parameterize collection arguments.

#### Exact Before and After: Site 1 — `scrapers/base.py:2`

**Before:**
```python
from abc import ABC, abstractmethod
from typing import Iterator, TypedDict
```

**After:**
```python
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TypedDict
```

#### Exact Before and After: Site 2 — `pitch_recommender.py:22-25`

**Before:**
```python
from typing import Mapping


def recommend_service(record: Mapping) -> str:
```

**After:**
```python
from collections.abc import Mapping
from typing import Any


def recommend_service(record: Mapping[str, Any]) -> str:
```

#### Exact Before and After: Site 3 — `pitch_recommender.py:135`

**Before:**
```python
def _is_low_rating(rating) -> bool:
    """True if the Google rating signals reputation problems."""
```

**After:**
```python
def _is_low_rating(rating: float | int | str | None) -> bool:
    """True if the Google rating signals reputation problems."""
```

#### Exact Before and After: Site 4 — `scrapers/google_places.py:194`

**Before:**
```python
    def _scrape_query(
        self,
        session: requests.Session,
        query: str,
        seen_ids: set,
        seen_lock: threading.Lock,
        scraped_at: str,
        country: str,
    ) -> list[BusinessRecord]:
```

**After:**
```python
    def _scrape_query(
        self,
        session: requests.Session,
        query: str,
        seen_ids: set[str],
        seen_lock: threading.Lock,
        scraped_at: str,
        country: str,
    ) -> list[BusinessRecord]:
```

#### Exact Before and After: Site 5 — `dedup.py:103-104`

**Before:**
```python
def dedup(records: list[dict]) -> list[dict]:
    phone_index: dict[str, dict] = {}
    name_index: dict[tuple, dict] = {}
```

**After:**
```python
from typing import Any

def dedup(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    phone_index: dict[str, dict[str, Any]] = {}
    name_index: dict[tuple[str, str], dict[str, Any]] = {}
```

---

### S3 — Adoption of PEP 695 `type` statement for domain type aliases

- **Where:** `dedup.py:102-124`, `enricher.py:10, 52, 58, 70`, `scrapers/base.py:5-29`.
- **Breaks:** Currently, record schemas, dedup keys, coordinates, and contact dictionaries are informally typed as `dict`, `tuple`, or `list[tuple[str, ...]]`. PEP 695 in Python 3.12 introduces the first-class `type` statement (`type Name = ...`), which creates lazily evaluated, inspectable `TypeAliasType` objects. Failing to use PEP 695 type aliases leaves domain types scattered, repetitive, and loosely checked across the pipeline.
- **Trigger:** Type checking with `mypy --python-version 3.12` or inspecting types dynamically at runtime.
- **Fix:** Define formal type aliases with the Python 3.12 `type` statement for `RecordDict`, `DedupNameKey`, `GeoBoundingBox`, and `ContactDict`.

#### Exact Before and After: Site 1 — `dedup.py:102-106`

**Before:**
```python
def dedup(records: list[dict]) -> list[dict]:
    phone_index: dict[str, dict] = {}
    name_index: dict[tuple, dict] = {}
```

**After:**
```python
from typing import Any

type DedupRecord = dict[str, Any]
type DedupNameKey = tuple[str, str]

def dedup(records: list[DedupRecord]) -> list[DedupRecord]:
    phone_index: dict[str, DedupRecord] = {}
    name_index: dict[DedupNameKey, DedupRecord] = {}
```

#### Exact Before and After: Site 2 — `enricher.py:10, 52, 58, 70`

**Before:**
```python
REGION_KEYWORDS: list[tuple[str, list[str]]] = [
...
_REGION_MAP: list[tuple[str, re.Pattern]] = [
...
_LB_COORD_REGIONS: list[tuple[str, float, float, float, float]] = [
...
_KSA_COORD_REGIONS: list[tuple[str, float, float, float, float]] = [
```

**After:**
```python
type RegionKeywordMap = list[tuple[str, list[str]]]
type RegionRegexMap = list[tuple[str, re.Pattern[str]]]
type GeoBoundingBox = tuple[str, float, float, float, float]
type ContactPayload = dict[str, str | None]

REGION_KEYWORDS: RegionKeywordMap = [
...
_REGION_MAP: RegionRegexMap = [
...
_LB_COORD_REGIONS: list[GeoBoundingBox] = [
...
_KSA_COORD_REGIONS: list[GeoBoundingBox] = [
```

---

### S3 — Exception suppression masks Python 3.12 improved tracebacks and lacks PEP 678 `add_note()`

- **Where:** `main.py:167-169`, `enricher.py:168, 182, 236`.
- **Breaks:**
  1. In `main.py:168`, scraping failures print `f"[{type(futures[future]).__name__}] ERROR: {e}"` to `sys.stderr`. This discards the entire traceback, neutralizing Python 3.12's enhanced fine-grained column position error reporting (PEP 657 / PEP 683) and name/import typo suggestions.
  2. In `enricher.py:168, 182, 236`, broad `except Exception:` blocks catch all exceptions, including potential runtime bugs (`TypeError`, `AttributeError`, `KeyError`), rather than expected network faults (`requests.RequestException`). This hides bugs that would otherwise display precise Python 3.12 error diagnostics.
- **Trigger:** An unhandled exception or parsing failure in an individual scraper thread logs a single truncated error line with no stack trace or context.
- **Fix:** Use PEP 678's `Exception.add_note()` to attach structured context to errors, narrow exception catching in `enricher.py` to `requests.RequestException`, and print full tracebacks in `main.py`.

#### Exact Before and After: Site 1 — `main.py:167-169`

**Before:**
```python
            except Exception as e:
                print(f"[{type(futures[future]).__name__}] ERROR: {e}", file=sys.stderr)
```

**After:**
```python
            except Exception as e:
                scraper_name = type(futures[future]).__name__
                e.add_note(f"Scraper execution failed inside ThreadPoolExecutor for {scraper_name}")
                print(f"[{scraper_name}] ERROR: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc(file=sys.stderr)
```

#### Exact Before and After: Site 2 — `enricher.py:166-171, 182-185`

**Before:**
```python
    try:
        r = _SESSION.get(url, timeout=8, allow_redirects=True, verify=False, stream=True)
    except Exception:
        # DNS failure, TLS failure, timeout, reset, blocked. We learned nothing.
        return UNKNOWN, contacts

    try:
        status = r.status_code
        if status >= 400:
            # A real response from a real server that says "not working".
            return DEAD, contacts

        # Cap the body before decoding it: r.text would materialise the whole
        # response, and a hostile or misconfigured host can stream forever.
        raw = r.raw.read(_MAX_BODY_BYTES, decode_content=True) or b""
        html = raw.decode(r.encoding or "utf-8", errors="replace")
    except Exception:
        # Reached the host but could not read the body. Still unknown.
        return UNKNOWN, contacts
```

**After:**
```python
    try:
        r = _SESSION.get(url, timeout=8, allow_redirects=True, verify=False, stream=True)
    except requests.RequestException:
        # DNS failure, TLS failure, timeout, reset, blocked. We learned nothing.
        return UNKNOWN, contacts

    try:
        status = r.status_code
        if status >= 400:
            # A real response from a real server that says "not working".
            return DEAD, contacts

        # Cap the body before decoding it: r.text would materialise the whole
        # response, and a hostile or misconfigured host can stream forever.
        raw = r.raw.read(_MAX_BODY_BYTES, decode_content=True) or b""
        html = raw.decode(r.encoding or "utf-8", errors="replace")
    except (requests.RequestException, OSError):
        # Reached the host but could not read the body. Still unknown.
        return UNKNOWN, contacts
```

---

### S3 — Removal of dead standard library batteries and dead import cleanup

- **Where:** `enricher.py:4`.
- **Breaks:**
  1. Python 3.12 removed 19 legacy standard library modules under PEP 594 (including `asynchat`, `asyncore`, `distutils`, `imp`, `smtpd`, etc.). An audit of `leadminer` confirms that no removed standard library modules are imported in production code.
  2. However, `enricher.py:4` imports `from urllib.parse import urljoin, urlparse`. Neither `urljoin` nor `urlparse` is referenced anywhere in `enricher.py`. Unused imports add dead symbol overhead and trigger linter warnings.
- **Trigger:** Running `ruff check --select F401 .` flags `urljoin, urlparse imported but unused`.
- **Fix:** Remove line 4 of `enricher.py`.

#### Exact Before and After: Site 1 — `enricher.py:4`

**Before:**
```python
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse
```

**After:**
```python
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
```

---

## Not a bug, but worth knowing

- **No PEP 594 removals affect leadminer directly:** An exhaustive audit of all imports across `main.py`, `dedup.py`, `enricher.py`, `pitch_recommender.py`, `cli.py`, and `scrapers/*.py` confirmed zero dependencies on removed modules (`imp`, `distutils`, `cgi`, `cgitb`, `pipes`, `smtpd`, `asynchat`, `asyncore`, etc.).
- **Lexicographical timestamp sort invariant:** `dedup.py:96` resolves `scraped_at` conflicts with `merged[key] = max(av, bv)`. If timezone offsets are formatted as `+00:00` instead of `Z`, string comparison will prefer `Z` over `+00:00` regardless of the numerical timestamp value because ASCII `'Z'` (90) > `'+'` (43). When switching to aware UTC datetimes, either normalize to `"Z"` or parse timestamps into `datetime` objects before comparing.
- **`datetime.UTC` availability:** Python 3.11 and 3.12 added `datetime.UTC` as an alias for `datetime.timezone.utc`. Because `pyproject.toml` pins `requires-python = ">=3.12"`, `datetime.UTC` is universally available and preferred over `datetime.timezone.utc`.

---

## Recommended order of work

1. **Replace deprecated `utcnow()` calls (Finding S2):** Update `scrapers/osm.py:31`, `scrapers/wikidata.py:31`, and `scrapers/google_places.py:162` to `datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")`.
2. **Implement `itertools.batched` chunking (Finding S2):** Refactor `enricher.py:check_websites` and `scrapers/google_places.py:scrape` to consume targets in bounded batches, eliminating unbounded in-flight futures.
3. **Clean up typing imports & add `@override` (Finding S3):** Switch all `typing.Iterator` and `typing.Mapping` imports to `collections.abc`, add `@override` to scraper implementations, and fully parameterize container annotations.
4. **Adopt PEP 695 `type` syntax (Finding S3):** Introduce formal `type` statements for record structures, bounding boxes, and dedup dictionary keys.
5. **Modernize exception handling (Finding S3):** Narrow `enricher.py` catch blocks to `requests.RequestException` and integrate `Exception.add_note()` with full tracebacks in `main.py:168`.
6. **Remove unused imports (Finding S3):** Delete `from urllib.parse import urljoin, urlparse` from `enricher.py:4`.