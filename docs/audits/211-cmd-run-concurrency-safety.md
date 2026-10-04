# 211 — Concurrency safety of `cmd_run`

## Verdict

`cmd_run` itself is trivially safe — three lines, no globals, no locks, no shared state
of its own (`cli.py:33-37`). Everything risky lives in what it calls, and the honest
answer is that **the concurrency in this codebase is currently correct**: worker threads
never touch record dicts (`enricher.py:231,238,249-259`), sessions really are per-thread
(`httpclient.py:58-71`), Google Places' shared `set`/`list` are lock-guarded, and the
nested 3→5 pools cannot deadlock. There is no data race to fix.

What there *is* is three ways a run can die or silently lose an entire source with no
signal at any process boundary, plus a body-size cap that is not a memory cap. `cmd_run`
inherits all of it, because it returns `None` and `cli.main` turns that into exit code 0.

## Scope of the review

`cmd_run` (`cli.py:33`) → `main.main()` (`main.py:148`), which runs:

| Phase | Pool | Location |
|---|---|---|
| 3 scrapers in parallel | `ThreadPoolExecutor(max_workers=3)` | `main.py:160` |
| Google Places, nested | `ThreadPoolExecutor(max_workers=5)` over 67 queries | `scrapers/google_places.py:142,194` |
| Website liveness + contacts | `ThreadPoolExecutor(max_workers=40)` | `enricher.py:225,237` |

Peak concurrency is 8 threads during scrape (3 outer + 5 inner) and 40 during
enrichment. The two never overlap: the scrape pool has fully shut down at
`main.py:169` before `enrich()` is called at `main.py:187`.

---

## Findings

### S1 — `cmd_run` cannot report failure, and the one automated guard is blind to a single-source outage

- **Where:** `cli.py:33-37`, `cli.py:285-286`, `cli.py:315`, `main.py:148`, `main.py:167-168`, `.github/workflows/scrape.yml:69`, `.github/workflows/scrape.yml:78-83`
- **Breaks:** `main.main()` is declared `-> None` (`main.py:148`) and converts every scraper exception into a stderr print (`main.py:167-168`). `cmd_run` returns that `None` (`cli.py:37`); `cli.main` coerces any falsy return to `0` (`cli.py:315`). So `leadminer run` exits **0** after a run in which all three sources failed, produced nothing, or produced only junk.
  The workflow does not even use it — `scrape.yml:69` still runs `python main.py`, so the CLI's exit semantics are exercised by nothing but `tests/test_lead_signal.py` (which never calls `cmd_run`).
  The workflow's only real gate is `ROWS < 100` (`scrape.yml:78-83`), but `ROWS` counts the **cumulative master**, which is monotonically non-decreasing by construction: `combined = raw_filtered + master` (`main.py:179`) and `write_csv` rewrites the previous master plus this week's additions (`main.py:209`). Once the master has 5,000 rows, it has 5,000 rows forever, whatever the scrapers did.
- **Trigger:** `GOOGLE_PLACES_API_KEY` revoked between runs. `google_places.py:169-171` prints `"[Google] GOOGLE_PLACES_API_KEY not set, skipping."` and returns an empty generator; `main.py:166` extends `raw` with nothing; the master is rewritten byte-identical; `rclone copy` (`scrape.yml:88-89`) sees no change and skips; the check is green. All 34 KSA queries (`KSA_QUERIES`, `google_places.py:83-125`) silently produce zero leads for a week, and the row-count gate cannot see it.
- **Fix:** `main.main() -> int` returning a non-zero count of sources that yielded zero records; `cmd_run` returns it unchanged; change the workflow gate to compare a per-source freshness watermark (e.g. count rows whose `scraped_at` is from this run) instead of the cumulative total.

### S1 — One exception in one Google query discards all 67 queries' completed work

- **Where:** `scrapers/google_places.py:168-199` (esp. `197`), `scrapers/google_places.py:226-254`, `main.py:157-158`, `main.py:167-168`
- **Breaks:** `scrape()` is a generator that performs **100% of its work before its first `yield`** (`google_places.py:168-197`, yielding only at `:199`). `run()` wraps it in `list()` (`main.py:158`), so an exception anywhere means `list()` yields nothing and the partially filled `all_records` (`google_places.py:191-192`) — up to tens of thousands of paid-for Places records — becomes unreachable. `f.result()` at `google_places.py:197` re-raises inside the `as_completed` loop, killing the whole generator.
  The exception does not need to be exotic: `resp.json()` at `google_places.py:254` sits **outside** the `try/except` at `:226-252`. That `except` catches `requests.RequestException` (so `raise_for_status()` at `:249` is handled), but the JSON decode on the very next line is not inside any handler.
- **Trigger:** An edge proxy or a truncated gzip member returns HTTP 200 with `content-type: text/html` for query 40 of 67. `resp.json()` raises `requests.exceptions.JSONDecodeError`, which propagates out of `fetch_query` → `f.result()` at `:197` → out of `scrape()` → caught only by `main.py:167`, which prints `[GooglePlacesScraper] ERROR: ...`. Queries 1-39 finished, their results are in `all_records`, and all of it is thrown away. Google Places is the only paid source; this is a full week of API spend for nothing, and the *other two* sources are unaffected, so nothing else looks wrong.
- **Fix:** Wrap the body of `fetch_query` in `try/except Exception` and return whatever `_scrape_query` accumulated; better, make `scrape()` return a `list` rather than a generator so partial results survive an exception by construction.

### S1 — `_MAX_BODY_BYTES` caps compressed bytes, not memory; 40 workers make it unbounded, and there is no checkpoint

- **Where:** `enricher.py:158`, `enricher.py:188-189`, `enricher.py:190-192`, `httpclient.py:52-54`, `enricher.py:237`, `main.py:187`, `main.py:209-213`
- **Breaks:** `enricher.py:188` does `r.raw.read(_MAX_BODY_BYTES, decode_content=True)` with `_MAX_BODY_BYTES = 200_000`. In urllib3 (1.26 and 2.x — `requirements.txt:1` pins only `requests==2.32.3`, which permits `urllib3>=1.21.1,<3`), `HTTPResponse.read(amt, decode_content=True)` reads `amt` bytes **off the socket** (`_raw_read(amt)`) and then loops `while len(self._decoded_buffer) < amt and data: data = self._raw_read(amt); put(decode(data))`. It keeps pulling 200 KB chunks from the wire until it has 200 KB of **decoded** output.
  DEFLATE's maximum compression ratio is 1032:1. A host that answers with `Content-Encoding: gzip` and a highly repetitive body can therefore turn 200 KB on the wire into ~206 MB in a single worker. `raw.decode(...)` at `enricher.py:189` allocates a second copy. Forty of those concurrently is ~8 GB before the string copies.
  Verified locally: `gzip.compress(b"A" * 200_000_000, 9)` is **194,422 bytes** and decompresses to 200 MB — a 1029:1 ratio, i.e. the whole hostile payload fits inside the 200,000-byte "cap", with room to spare for a 200 MB cap-relative payload.
  The `except Exception` at `enricher.py:190` catches in-process `MemoryError`, but a Linux runner's OOM killer sends SIGKILL, which is uncatchable. And the only writes to disk are `main.py:209-213`, i.e. **after** the entire enrichment phase. There is no intermediate checkpoint, so a kill at hour 3 of the 300-minute budget (`scrape.yml:25`) discards 100% of the run.
- **Trigger:** One record with `website="https://<small-business-host>/"` serving `Content-Encoding: gzip` and a body of repeated bytes. 40 workers hit such hosts within the same second at 40-way concurrency. Reproducible offline without any network.
- **Fix:** Cap the *decoded* stream — read in 64 KB chunks via `resp.iter_content(64 * 1024)` and `break` when the decoded total exceeds the limit, never materialising more than `limit` decoded bytes. Separately: checkpoint the enriched master to disk incrementally (per N completed futures) rather than at `main.py:209`.

### S2 — Identical URLs are fetched up to N times, 40 at a time, from one origin — and there is no retry to absorb the burst

- **Where:** `enricher.py:231`, `enricher.py:238`, `enricher.py:175`, `enricher.py:176-178`, `enricher.py:303`, `dedup.py:210`, `dedup.py:218`, `dedup.py:226-230`
- **Breaks:** `targets` is built per **record**, not per distinct URL (`enricher.py:231`), and every target is submitted (`enricher.py:238`). Duplicates are guaranteed by two mechanisms:
  1. OSM `contact:website` on many nodes is the same small-business site or a shared directory/hosting page (`osm.py:91`).
  2. `dedup` can legitimately return two rows for one business. A record with a usable phone goes into `phone_index` (`dedup.py:210`); its twin without one goes into `name_index` (`dedup.py:218`); and the cross-filter at `dedup.py:226-230` only drops a name-keyed record **if it has a phone** that is already captured. So a business whose Google listing has a phone and whose OSM node has only `contact:website` keeps two rows with the same website.
  40 workers then open up to 40 simultaneous connections to a single origin. `_fetch_website` deliberately bypasses `fetch_with_retry` (`enricher.py:175` calls `get_session().get` directly), so there is no backoff to absorb a refusal or a reset: one rejection lands in the bare `except Exception` at `enricher.py:176` → `UNKNOWN` → and per `enricher.py:303` an unknown site earns no rebuild-pitch credit at all.
- **Trigger:** 800 OSM nodes tagged `contact:website=https://www.lebanonrestaurants.com/directory`. One wave of 40 concurrent GETs to that origin. If it rate-limits or serialises on a PHP session lock, all 800 come back `UNKNOWN` and stay `UNKNOWN` in the master — because the next run repeats the same burst. The lead is lost permanently, not just this week.
- **Fix:** `for url in set(...)` — fetch each distinct URL once, then fan the single `(outcome, contacts)` back out to every record index that shares it. This also cuts enrichment wall-clock by the duplicate ratio.

### S2 — Two concurrent `leadminer run` processes corrupt the master through a shared temp filename

- **Where:** `main.py:122`, `main.py:124`, `main.py:130`, `main.py:131-133`, `.github/workflows/scrape.yml:15-17`, `README.md:92-97`
- **Breaks:** `051-atomic-writes-and-concurrency.md` asked for `fcntl.flock`. What landed is `open(tmp, "w")` + `fsync` + `os.replace` (`main.py:124-130`), which is atomic for one writer and for all readers — **but the temp name is derived from the target name** (`main.py:122`: `path.with_suffix(path.suffix + ".tmp")` → always `data/all_businesses.csv.tmp`), so it is identical in every process. Two concurrent runs both `open()` it with `O_TRUNC`, each with its own file offset starting at 0; their `writerows` interleave across the same bytes and the shorter writer's tail is left as whatever the longer one did not overwrite. The final `os.replace` publishes a file that is neither run's data.
  The Actions `concurrency` group (`scrape.yml:15-17`) serialises *GitHub* runs only. `README.md:92-97` explicitly tells operators to run `leadminer run` locally, and nothing prevents a local run overlapping another local run or a local run overlapping a checkout sharing `data/`.
- **Trigger:** `leadminer run` in a terminal at 03:05 UTC Monday, while the 03:00 cron (`scrape.yml:9`) is in flight and `data/` is a shared checkout — or two shells running `leadminer run` against the same directory. `data/all_businesses.csv` ends up with torn/interleaved rows. `leadminer validate` (`cli.py:190-192`) will often flag it as duplicate `(name, phone)` pairs, but only after the corrupted file has already been rclone'd to Drive.
- **Fix:** `fcntl.flock` an advisory lock on `data/.leadminer.lock` for the duration of `cmd_run` and exit 2 with a clear message if it is held; make the temp name process-unique (`f".{os.getpid()}.tmp"`).

### S2 — Neither pool can be cancelled; "interrupted" does not interrupt anything

- **Where:** `enricher.py:237-238`, `scrapers/google_places.py:194-197`, `main.py:160-169`, `cli.py:316-318`, `.github/workflows/scrape.yml:25`
- **Breaks:** `ThreadPoolExecutor.__exit__` calls `shutdown(wait=True)` with `cancel_futures=False`, so every already-queued work item still runs. `enricher.py:238` eagerly submits **every** website before consuming a single result, so the queue holds the entire remainder of the run. Worker threads are non-daemon and joined by `threading._register_atexit`, so even a `KeyboardInterrupt` that escapes the `with` block will not let the interpreter exit until the queue drains. `cli.main` catches `KeyboardInterrupt` and prints `"\ninterrupted"` (`cli.py:316-318`) — which is false: the process is still fetching and will keep fetching.
  In CI, `timeout-minutes: 300` (`scrape.yml:25`) sends SIGTERM. There is no SIGTERM handler anywhere, so the process dies instantly, `main.py:131-133` never runs, and a stale `data/all_businesses.csv.tmp` is left behind.
- **Trigger:** `leadminer run`, Ctrl-C at minute 40 with 30,000 websites still queued. The terminal prints `interrupted` and then appears hung for roughly `30_000 / 40 × 8s ≈ 100 minutes` (`enricher.py:175` uses `timeout=8`), because the pool is draining, not cancelling. A second Ctrl-C kills it, discarding the run — and `main.py:209` was never reached, so nothing is saved.
- **Fix:** Keep only `max_workers * 4` fetches in flight and refill as futures complete, then call `pool.shutdown(wait=False, cancel_futures=True)` on the interrupt path; install a SIGTERM handler that raises so `finally` blocks run.

### S3 — `http_session()` is a context manager that manages nothing; no `Session` is ever closed

- **Where:** `httpclient.py:58`, `httpclient.py:61-71`, `httpclient.py:88-91`, `enricher.py:175`, `osm.py:35`, `wikidata.py:73`
- **Breaks:** `http_session()` yields `get_session()` and closes nothing on exit (`httpclient.py:88-91`). A session's lifetime is the **thread's**, not the block's, and `ThreadPoolExecutor` reuses threads — so the session created for query 1 of 67 is still carrying query 1's cookie jar and pooled connections when query 40 lands on the same worker. Nothing in the tree calls `Session.close()`.
  For today's one-shot CLI run this is survivable: the thread dies at pool shutdown, the `threading.local` slot is dropped, CPython refcounts the `HTTPAdapter` and the sockets close. It stops being survivable the moment `check_websites` runs twice in one process (a partial-failure retry, a `--sources a,b` then `b,c` loop, a test), because 40 sessions then hold pooled connections to hosts from every pass for the life of the process.
- **Trigger:** `enricher.check_websites(records)` called twice back-to-back in one process: the second pass reuses all 40 thread-local sessions, each still holding keep-alive connections from pass one.
- **Fix:** Either drop the contextmanager and rename it `thread_session()` so the contract is honest, or keep a registry keyed by thread so `close()` can actually be called at pool teardown.

### S3 — No logging is configured anywhere, so every retry diagnostic the concurrency work added is discarded

- **Where:** `httpclient.py:45`, `httpclient.py:178-179`, `httpclient.py:204-205`, `cli.py:33-37`, `enricher.py:261-262`
- **Breaks:** `httpclient` is the only module that uses `logging` (`httpclient.py:45`) and it logs exactly the things a concurrent run needs: per-attempt transport errors (`httpclient.py:178-179`) and the jittered backoff it is about to sleep (`httpclient.py:204-205`). There is no `basicConfig`/`dictConfig` in `cli.py`, `main.py`, or `httpclient.py`, so those records go to a root logger with no handler at WARNING level and are dropped.
  Meanwhile the phase that *does* print during the 40-thread pool prints once per 200 URLs (`enricher.py:261-262`) — roughly every 40 seconds at full tilt — so a run that is silently retrying 40 URLs in lockstep is indistinguishable from a healthy one. This is narrower than `068-logging-in-threads.md`'s redesign proposal: the point is only that the retry telemetry `httpclient.py` was written to produce is currently unreachable by any operator.
- **Trigger:** 40 workers hitting an origin that returns 503. The run takes many times longer, no output mentions it, and `leadminer run` still exits 0.
- **Fix:** `logging.basicConfig(level=logging.INFO, format=...)` at the top of `cli.main` before dispatch.

---

## Not a bug, but worth knowing

These are the specific things that *could* corrupt under this concurrency model and
provably do not. They are the reason this report is short on data races.

- **Record dicts are only ever mutated by the main thread.** `enricher.py:231` snapshots `(index, url)` *before* submitting and submits the URL, not the record; workers return `(outcome, contacts)`; the mutation happens in the `as_completed` loop (`enricher.py:249-259`). No lock is needed and none is present. This is the single most important property in the file and it is correct.
- **`dedup` cannot hand the same dict to two threads.** This is the one way `check_websites` *could* corrupt: if `dedup` returned the same object at two indices, two workers' results would race into one dict and `website_live` could describe URL A while `website` says URL B. It cannot happen — `dedup.py:210` and `dedup.py:218` both store `dict(record)` (a copy), and `_merge` always builds a fresh dict (`dedup.py:190-195`), so every element of the list returned at `dedup.py:232` is a distinct object. **Keep it that way:** any future "return the originals, merged in place" optimisation reintroduces the race.
- **Thread-local sessions are genuinely per-thread.** `httpclient.py:58-71`: `get_session()` inside `_fetch_website` (`enricher.py:175`) builds one `requests.Session` per pool thread. This is the fix `004-enricher-thread-safety.md` asked for and it landed. The 40-way enrichment now has no shared cookie jar, no shared header dict, no shared `HTTPAdapter`. Google Places additionally builds its own per-query session (`google_places.py:189`) that never touches the thread-local at all.
- **Google Places' shared collections are correctly locked.** `seen_ids` is guarded by `seen_lock` (`google_places.py:263-266`); the `continue` at `:265` is inside the `with`, so `__exit__` releases the lock — no deadlock, no double-insert. `all_records.extend` is under `records_lock` (`google_places.py:191-192`). `scraped_at` (`:173`) and `self._api_key` (`:157`) are read-only after construction on the main thread.
- **The nested pools cannot deadlock.** One outer worker (Google) creates its own 5-worker pool and drives it with `as_completed` from *inside* the outer worker (`google_places.py:194-197`). Inner workers are independent threads, so the blocked outer worker holds no slot an inner task needs. The scrape pool has fully exited at `main.py:169` before `enrich()` runs at `main.py:187`, so the 40-thread pool never overlaps the 5 — peak is 40, not 45.
- **Shared randomness is safe.** `random.uniform` (`httpclient.py:201-203`, `google_places.py:243`) uses the process-global Mersenne Twister. Each call is a single C-level call under the GIL, so there is no torn state, and nothing in the tree calls `random.seed()`, so there is no cross-thread reseed hazard. The only cost is contention and irreproducibility.
- **The scrape phase is time-boxed; the enrichment phase is not.** `_QUERY_BUDGET_SECONDS = 180` (`google_places.py:152`) plus a 30 s request timeout bounds the whole scrape phase at roughly `67 / 5 × 210s ≈ 47 min`. Nothing bounds enrichment. At `40 workers × 8 s` (`enricher.py:175`), 50,000 websites take ~2.8 hours on their own — so as the cumulative master crosses that size the run reliably hits `timeout-minutes: 300` mid-enrichment with nothing written. That is the same cliff as the S1 above, approached from the arithmetic side.
- **`urllib3.disable_warnings` at `enricher.py:328` is a process-global side effect** triggered from inside `enrich()`. Idempotent and harmless today, but calling `enrich` from any other entry point silently mutates global warning policy.

## Recommended order of work

1. **Exit codes + a freshness-aware gate** (S1 #1). Smallest diff, largest trust win: `main.main() -> int`, `cmd_run` propagates, and the workflow stops measuring the cumulative total.
2. **Google Places partial-result survival** (S1 #2). Catch per-query, and return a `list` from `scrape()` so partials cannot be lost.
3. **Decoded-byte cap, then incremental checkpointing** (S1 #3). The cap alone stops the OOM; the checkpoint alone bounds the loss when something else kills the run. Do both.
4. **Deduplicate URLs before fetching** (S2 #4) — one fetch per distinct URL, fanned out to all record indices. Fixes lost leads, cost, and politeness in one change.
5. **`flock` + process-unique temp name** (S2 #5), then **`--data-dir`** as a follow-up to `cmd_run`/`cmd_doctor`/`cmd_stats`.
6. **Bounded submission + `cancel_futures=True` + a SIGTERM handler** (S2 #6).
7. **`logging.basicConfig` in `cli.main`** (S3), then decide whether `http_session()` should actually close anything (S3).

## Cross-references

- `004-enricher-thread-safety.md` — recommended the thread-local session; landed, correctly. Its PoolManager-eviction concern is still live because `requirements.txt` still pins no urllib3.
- `051-atomic-writes-and-concurrency.md` — recommended `fcntl.flock`; only the `os.replace` half landed, which introduced the shared-temp-name hazard in S2 #5.
- `037-cli-design.md` — reported "exits 0 on credential failure" against the old `python main.py`. The library-level fixes landed (typed outcomes, `doctor`, atomic writes); the process-boundary fix in `cmd_run` did not.
- `068-logging-in-threads.md` — the full logging redesign. S3 here is only the narrow point that `httpclient`'s retry logs currently reach nobody.
- `014-google-places-quota.md` — query budget and 429 handling; the same `f.result()` at `google_places.py:197` is the failure mode that turns a partial outage into a total one.
