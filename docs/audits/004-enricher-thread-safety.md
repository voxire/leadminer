# 004 — Enricher Thread Safety

## Verdict

The worker threads do not access or mutate business-record dictionaries: only the main thread writes records, before and after the executor, and as futures complete. The shared `requests.Session` does create shared cookie state; more importantly, the unpinned urllib3 dependency permits versions with a `PoolManager` eviction race that this code can turn into silently false “dead website” results.

## Findings

### S2 — Shared session cookies make same-origin fetches order-dependent
- **Where:** `enricher.py:124-125, 142-146`; `requests==2.32.3` in `requirements.txt:1`
- **Breaks:** Every worker uses the same Session, whose cookie jar persists response cookies between requests. For concurrent requests to the same cookie scope, one request may prepare its `Cookie` header before another response stores a `Set-Cookie`, or afterward. Same-name cookies may also be replaced according to response order. Thus identical site fetches can receive different session state and return different HTML/status/contact results. Cookie domains and paths still scope cookies; this is not arbitrary cross-domain cookie leakage or evidence that the cookie jar's internal data structure is corrupted. Requests merges session cookies into a per-request jar and Python's default cookie jar synchronizes jar operations, but that does not make the shared session semantics deterministic.
- **Trigger:** Two leads target the same host (or a redirect within the same cookie scope); one response sets or rotates a cookie while the other request is being prepared.
- **Fix:** Use a thread-local Session (with its own cookie jar) or make this stateless scraper send no persisted cookies.

### S2 — Allowed urllib3 versions can close an in-flight origin pool
- **Where:** `enricher.py:146, 188-193`; `requirements.txt:1` (no urllib3 pin or lockfile; see `BRIEF.md:43-44`)
- **Breaks:** Requests' default adapter uses a urllib3 `PoolManager` with a cache of 10 origin pools. In urllib3 releases before the PoolManager race fix, concurrent requests across more than 10 distinct origins can evict and close a pool after a worker obtains it but before it uses it. The resulting `ClosedPoolError` is swallowed by `_fetch_website`'s broad `except Exception` (`enricher.py:151-152`), so that website is recorded as not live with no contacts instead of surfacing a run error. Requests 2.32.3 allows urllib3 versions from `>=1.21.1,<3`; the vulnerable older 1.26 line is therefore allowed by this project's dependency declaration. urllib3 fixed this in 1.26.19 (and the 2.x line); without a transitive lock/pin, the audit cannot establish which version a deployment uses.
- **Trigger:** Run 40 concurrent fetches spanning more than 10 distinct origins on an allowed pre-fix urllib3 version; a pool eviction lands between pool lookup and connection acquisition.
- **Fix:** Pin/lock urllib3 to a fixed release (at least 1.26.19 on the 1.x line, or a supported fixed 2.x release) and test the resolved dependency set.

## Not a bug, but worth knowing

- **Headers:** `_SESSION.headers` gets its User-Agent once at import (`enricher.py:125`) and is only read by the workers here. Requests builds per-request prepared headers; there is no current concurrent header mutation and therefore no header-dictionary corruption in this code. Mutating `_SESSION.headers` or other Session configuration while fetches run (including from another caller) would reintroduce an unsynchronized shared-state race.
- **Connection pool behavior:** urllib3's per-host `HTTPConnectionPool` is designed for concurrent use. With Requests' default adapter (`pool_maxsize=10`, `pool_block=False`), more than 10 simultaneous requests to one origin can open extra connections; excess connections are discarded rather than returned to the reusable pool. This is connection churn / load, not pool corruption or a 10-request concurrency cap. urllib3 documents the pool as thread-safe and this overflow behavior in its [connection-pool reference](https://urllib3.readthedocs.io/en/stable/reference/urllib3.connectionpool.html); Requests documents the adapter defaults in its [API reference](https://requests.readthedocs.io/en/stable/api/).
- **urllib3 internals:** The connection-pool race above is version-dependent and separate from the thread-safe per-host pool. urllib3's changelog records the many-distinct-origins PoolManager fix in [1.26.19](https://github.com/urllib3/urllib3/blob/1.26.19/CHANGES.rst). On fixed releases, no additional corruption of pool/queue internals is established by this call path; response objects and HTTP connections are otherwise per-request.
- **Record dictionaries:** `check_websites()` snapshots only `(index, website URL)` into `targets` and submits the URL, not the record (`enricher.py:182, 188-189`). Workers return `(live, contacts)`; `as_completed()` consumes those results and the main thread mutates `records[idx]` (`enricher.py:191-204`). Region inference mutates records before this call (`enricher.py:270-277`), and defaults/scores are added after the executor has exited (`enricher.py:279-290`). Therefore there is no worker/main-thread race over record dictionaries in the current implementation.

## Recommended order of work

1. Lock urllib3 to a release containing the PoolManager eviction fix; retain tests that verify exceptions are not silently translated into dead-site results.
2. Isolate or disable Session cookies for independent website checks.
3. Keep records owned by the main thread; no record-copying or locking change is warranted for the current flow.
