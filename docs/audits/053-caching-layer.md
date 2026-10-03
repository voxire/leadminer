# 053 — Persistent HTTP cache for repeatable runs

## Verdict

Add a SQLite-backed cache at the HTTP boundary, prioritizing website checks: today `check_websites()` issues one GET per record on every run (`enricher.py:217-230`), while CI starts from a fresh checkout and only restores CSVs (`.github/workflows/scrape.yml:35-44`). Cache actual HTTP responses, not just the derived `website_live` boolean, so contacts can be re-extracted after parser changes and a cache hit follows the same LIVE/DEAD/UNKNOWN rules as a network response. Persist the database between CI runs; otherwise SQLite only saves repeat work within a single job.

## Findings

### S2 — Every enrichment run re-fetches previously checked websites

- **Where:** `enricher.py:167-214`, `enricher.py:217-230`
- **Breaks:** `check_websites()` submits a GET for each website on every invocation. It has no durable state, so repeats spend time and send the same traffic again; with 40 workers, URLs repeated across records can also be fetched concurrently before any shared result exists.
- **Trigger:** Run `python main.py` twice with the same master CSV and websites: all those website URLs are requested again on the second run.
- **Fix:** Place a SQLite response cache before `_SESSION.get()`, with TTL and a per-process single-flight guard keyed by cache key; use the cached HTTP status/body through the same classifier and contact extractor as a fresh response.

### S2 — A local SQLite file alone does not make hosted CI runs persistent

- **Where:** `.github/workflows/scrape.yml:14-16`, `.github/workflows/scrape.yml:35-50`
- **Breaks:** GitHub-hosted jobs use a fresh runner. The workflow downloads and uploads CSV files, not a cache DB, so an otherwise-correct `data/http-cache.sqlite3` disappears at job end and subsequent runs still re-fetch everything.
- **Trigger:** Add a local cache DB, run the workflow twice on separate hosted runners, and observe the second job has no cache file.
- **Fix:** Explicitly restore and save the DB in CI (simplest with this workflow: `rclone copy` a dedicated `cache/http-cache.sqlite3` before the run, then checkpoint and upload it after; alternatively use a carefully versioned Actions cache). Serialize workflows that update the same DB, and use SQLite's backup API or checkpoint WAL before copying.

## Recommended design

### Key and policy

- Website GET key: SHA-256 of `GET\n` + normalized request URL. Lowercase scheme/host, remove fragment and default port; preserve path and query bytes/order rather than risk changing server semantics. Key the originally requested URL, and store the final redirected URL for diagnostics.
- General HTTP key: SHA-256 of method + normalized URL + SHA-256 of the exact request body (empty for GET). For JSON POST requests, serialize deterministically (`sort_keys=True`, compact separators) before sending and hashing. Include response-varying headers (for example `Accept` and a non-reversible credential/account fingerprint) in the key if callers can vary them; never store API keys in the DB.
- Store the bounded response body and final HTTP status, not just extracted fields. That allows current contact extraction code to be re-run on a cache hit. Keep the website fetch cap at `_MAX_BODY_BYTES` (`enricher.py:155`, `enricher.py:178-181`). Do not cache API authorization failures, 429 rate limits, or transport errors as reusable successful responses.
- Suggested TTLs: LIVE website responses 24 hours; DEAD responses 1 hour for 5xx, 5 minutes for 429, and 24 hours for other HTTP errors. Make TTLs configurable. These are freshness bounds, not claims that a website cannot change during the interval.
- A received website HTTP error response is negative-cached as DEAD under the current contract (`enricher.py:145-150`, `enricher.py:173-177`). Preserve its status/body so a cache hit maps to the same DEAD verdict. In particular, use a short TTL for 5xx and 429 to limit stale transient failures. Do not reuse scraper API 401/429 responses as data results. A DNS/TLS/timeout/reset error, or a body-read failure, is UNKNOWN and must not be stored as a reusable DEAD result; let the next run retry it.

### SQLite schema

This uses only Python's standard-library `sqlite3`; no dependency change is needed (`requirements.txt:1-3`). Store response bytes as a BLOB and cache metadata separately from any business rows:

```sql
CREATE TABLE IF NOT EXISTS http_cache (
    cache_key       TEXT PRIMARY KEY,
    method          TEXT NOT NULL,
    request_url     TEXT NOT NULL,
    body_sha256     TEXT,
    status_code     INTEGER NOT NULL,
    final_url       TEXT NOT NULL,
    response_body   BLOB NOT NULL,
    content_type    TEXT,
    outcome         TEXT NOT NULL CHECK (outcome IN ('live', 'dead')),
    fetched_at      INTEGER NOT NULL, -- UTC Unix seconds
    expires_at      INTEGER NOT NULL,
    policy_version  INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS http_cache_expiry ON http_cache(expires_at);
```

Only LIVE and DEAD responses are rows. The absence of a row means cache miss; UNKNOWN is not a cache entry. Expired entries are misses and may be deleted opportunistically. Bump `policy_version` (or clear the DB) when outcome classification or response interpretation changes. Since raw response bodies can include contact information, keep the DB out of git and restrict access like the generated CSVs.

### Lookup / write path

Illustrative code for the cache boundary. Open short-lived connections so worker threads do not share a `sqlite3.Connection`; WAL plus a busy timeout handles concurrent readers/writers. `response_body` must be the same bounded payload the website extractor already reads. The normal fetch path should only call `put()` after a response and body have both been read successfully.

```python
import hashlib
import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

POLICY_VERSION = 1

@dataclass(frozen=True)
class CachedResponse:
    status_code: int
    final_url: str
    body: bytes
    content_type: str | None
    outcome: str  # "live" or "dead"
    fetched_at: int

def normalize_url(url: str) -> str:
    p = urlsplit(url)
    scheme = p.scheme.lower()
    host = (p.hostname or "").lower()
    port = p.port
    netloc = host if port is None or (scheme, port) in {
        ("http", 80), ("https", 443)
    } else f"{host}:{port}"
    if p.username or p.password:
        raise ValueError("credentials must not be embedded in cache URLs")
    return urlunsplit((scheme, netloc, p.path or "/", p.query, ""))

def make_key(method: str, url: str, body: bytes = b"") -> tuple[str, str | None]:
    body_hash = hashlib.sha256(body).hexdigest() if body else None
    material = "\n".join((method.upper(), normalize_url(url), body_hash or ""))
    return hashlib.sha256(material.encode("utf-8")).hexdigest(), body_hash

class HttpCache:
    def __init__(self, path: str):
        self.path = path
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS http_cache (
                cache_key TEXT PRIMARY KEY, method TEXT NOT NULL,
                request_url TEXT NOT NULL, body_sha256 TEXT,
                status_code INTEGER NOT NULL, final_url TEXT NOT NULL,
                response_body BLOB NOT NULL, content_type TEXT,
                outcome TEXT NOT NULL CHECK(outcome IN ('live','dead')),
                fetched_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                policy_version INTEGER NOT NULL DEFAULT 1
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS http_cache_expiry "
                       "ON http_cache(expires_at)")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def get(self, method: str, url: str, body: bytes = b"") -> CachedResponse | None:
        key, _ = make_key(method, url, body)
        now = int(time.time())
        with self._connect() as db:
            row = db.execute("""SELECT status_code, final_url, response_body,
                    content_type, outcome, fetched_at
                FROM http_cache
                WHERE cache_key=? AND expires_at>? AND policy_version=?""",
                (key, now, POLICY_VERSION),
            ).fetchone()
        if row is None:
            return None
        return CachedResponse(*row)

    def put(self, method: str, url: str, body: bytes, status: int,
            final_url: str, response_body: bytes, content_type: str | None,
            ttl_seconds: int) -> None:
        # Mirrors _fetch_website's present contract. Call only for completed
        # responses; transport/body-read failures remain UNKNOWN and uncached.
        outcome = "dead" if status >= 400 else "live"
        now = int(time.time())
        key, body_hash = make_key(method, url, body)
        with self._connect() as db:
            db.execute("""INSERT INTO http_cache
                (cache_key,method,request_url,body_sha256,status_code,final_url,
                 response_body,content_type,outcome,fetched_at,expires_at,policy_version)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(cache_key) DO UPDATE SET
                  status_code=excluded.status_code, final_url=excluded.final_url,
                  response_body=excluded.response_body, content_type=excluded.content_type,
                  outcome=excluded.outcome, fetched_at=excluded.fetched_at,
                  expires_at=excluded.expires_at, policy_version=excluded.policy_version""",
                (key, method.upper(), normalize_url(url), body_hash, status,
                 final_url, sqlite3.Binary(response_body), content_type, outcome,
                 now, now + ttl_seconds, POLICY_VERSION),
            )

    def prune(self) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM http_cache WHERE expires_at<=? OR policy_version<>?",
                       (int(time.time()), POLICY_VERSION))
```

At the website fetch call site, do a cache lookup before the network request. A hit returns the cached status/body to the existing classifier and contact extraction; a miss falls through to the current GET and writes the completed response. The generic key supports POST callers, but this initial integration writes only completed website responses; scraper API adapters must apply their own response eligibility rules and must not cache 401/429. The behavior must be:

```python
cached = cache.get("GET", url)
if cached is not None:
    outcome = LIVE if cached.status_code < 400 else DEAD
    # For LIVE, run the current email/social extractors on cached.body.
    # For DEAD, return no contacts, as the current code does.
    return outcome, extract_contacts(cached.body) if outcome == LIVE else empty_contacts()

# On successful response/body read:
cache.put("GET", url, b"", r.status_code, r.url, raw,
          r.headers.get("Content-Type"), ttl_for(r.status_code))
```

Adapt the existing `_fetch_website()` to factor its extraction into `extract_contacts()` and to cache status/body before returning. A valid hit is the prior observation and keeps its tri-state meaning: cached 2xx → LIVE (`website_live=True`), cached HTTP error → DEAD (`False`), no usable cache entry or network/body-read failure → UNKNOWN (`None`). A cache hit must not collapse DEAD into UNKNOWN just because it avoided a fresh request, nor treat UNKNOWN as a negative result. Optionally log cache age/hit counts; do not change the existing CSV tri-state representation (`main.py:68-70`).

### Invalidation and CI operations

1. TTL expiration is the normal freshness mechanism. Provide `--refresh-cache` / an environment flag to bypass reads and replace entries, plus a targeted delete-by-key/URL administrative operation; never require manual SQL for routine invalidation.
2. Bump `POLICY_VERSION` when changing liveness classification or the cached representation. Because the response body is stored, contact regex changes need no forced network fetch; they can be re-applied on the next hit.
3. In CI, restore the DB before `python main.py`, then persist it after the run. Prefer downloading to a temporary path and replacing the canonical file only after a successful run; don't upload on failed/interrupted runs. Use an Actions `concurrency` group or equivalent lock so two jobs cannot overwrite each other's updated cache.
4. Keep cache files out of Git. Avoid copying a live WAL-mode DB as only the main `.sqlite3` file; use `sqlite3.Connection.backup()` to a clean upload file or run `PRAGMA wal_checkpoint(TRUNCATE)` after all worker connections close.
5. Add cache tests with a stubbed HTTP session/clock: miss then hit makes one request; expiry makes another; 404 caches/replays DEAD; transport and body-read errors stay UNKNOWN and are retried; LIVE hits re-extract contacts; URL and POST-body differences yield distinct keys; concurrent same-key work is single-flight.

## Not a bug, but worth knowing

- The generic key supports POST APIs as requested, but the immediate cost-reduction target is website GETs. Caching Google Places POSTs has billing, freshness, page-token, and credential-vary concerns; roll that in separately with short TTLs and explicit account scoping. Overpass and Wikidata POST/GET snapshots are also mutable source data, not permanent fixtures (`scrapers/osm.py:38-45`, `scrapers/wikidata.py:40-49`, `scrapers/google_places.py:203-221`).
- SQLite solves persistence and atomic row updates, not duplicate in-flight fetches by itself. A per-key single-flight mechanism is still needed if the same URL can enter the thread pool more than once.
- No cache makes a live website stay live. CI is deterministic only with respect to cached HTTP observations within the chosen TTL and an explicitly persisted cache snapshot; expose refresh/bypass controls so operators can request fresh checks.

## Recommended order of work

1. Implement a small `http_cache.py` module (stdlib only), response-body bounds, TTLs, and tests; integrate only the website GET path first.
2. Add same-process single-flight for duplicate website URLs and preserve the existing LIVE/DEAD/UNKNOWN contract exactly.
3. Persist/restore the SQLite DB in the workflow, with serialization and safe checkpoint/backup handling; document refresh and invalidation controls.
4. Consider scraper API caching only after separate decisions on freshness, pagination, authorization partitioning, and whether each API permits reuse.
