# 379 — `_fetch_website` under 40 threads: safe shared state, unsafe body handling

## Verdict

**`_fetch_website` itself is currently thread-safe: it shares no mutable state with any
other thread, and `check_websites` keeps `records` single-writer.** Every module-level name
the function touches is read-only after import, `contacts` is call-local, and
`get_session()` is `threading.local()`-backed. I stress-tested all of this (40 threads ×
4000 iterations on the shared compiled patterns: 0 exceptions, 0 result mismatches).

**The concurrency failure is not in the sharing — it is in what one worker does while
holding the GIL.** `_EMAIL_RE` (`enricher.py:130`, called at `enricher.py:200`) is
quadratic; measured exponent **b = 2.24**, and at the 200,000-byte cap
(`enricher.py:158`) **one page costs ~13 minutes**. CPython's `re` does not release the
GIL, so that is not one slow worker — it is a whole-process stall. Compounding it,
`timeout=8` (`enricher.py:175`) is a *per-recv inactivity* timeout, not a deadline, so the
body read at `enricher.py:188` is unbounded in wall-clock. 40 such pages do not degrade the
run, they end it: at `timeout-minutes: 300` (`scrape.yml:25`) with **zero CSVs written**,
because `enrich()` runs at `main.py:187`, before any `write_csv`.

The 3-scraper pool, the 5-worker Places pool, and `check_websites` never overlap, so there
is no cross-pool interference. Details and the S2/S3s below.

---

## Findings

### S1 — `_EMAIL_RE` is quadratic and holds the GIL: one page stalls all 40 workers for ~13 minutes

- **Where:** pattern at `enricher.py:130`; invoked at `enricher.py:200`; cap at
  `enricher.py:158`; pool at `enricher.py:237-238`.
- **Why it is quadratic:** the leading `[a-zA-Z0-9._%+\-]+` is a *charset prefix*, so the
  SRE engine's fast-skip optimisation cannot help — **every** position in a run of
  alphanumeric/`._+-` characters is a valid start. When the suffix is unsatisfiable, the
  engine backtracks that entire greedy run, then restarts one character later: O(n) work at
  each of O(n) start positions.
- **Measured** (stdlib only, no network):

  | input | best-of-N time |
  |---|---|
  | `"a"*8000 + "@!"` | 0.127 s |
  | `"a"*16000 + "@!"` | 1.100 s |
  | `"a"*32000 + "@!"` | 4.974 s |

  Log-log fit over n = 2000…32000: **b = 2.24**. Extrapolating to the
  `DEFAULT_MAX_BODY_BYTES = 200_000` cap (`httpclient.py:54`, re-exported at
  `enricher.py:158`): **~793 s ≈ 13.2 minutes for a single page.**

- **It is the only offender.** I measured the other four patterns on the same adversarial
  shapes at n = 8000/16000/32000:

  | pattern | line | verdict |
  |---|---|---|
  | `_EMAIL_RE.findall` | `enricher.py:200` | **super-linear, 4.97 s at n=32000** |
  | `mailto` `re.findall` | `enricher.py:199` | linear, 0.0003 s |
  | `_INSTAGRAM_RE.finditer` | `enricher.py:206` | linear, 0.0007 s (bounded `{2,30}`) |
  | `_WHATSAPP_RE.search` | `enricher.py:212` | linear, <0.0001 s (bounded `{7,15}`) |
  | `_LINKEDIN_RE.search` | `enricher.py:216` | linear, 0.0004 s |

- **Why concurrency makes this catastrophic.** CPython's `re`/`sre` matching holds the GIL.
  Demonstrated: 8 threads each doing an equal slice of `_EMAIL_RE` work took 5.465 s,
  versus 1.110 s for one thread doing 1/8 of it — a speedup of **1.63× against an ideal
  8×**. So the parse phase at `enricher.py:199-220` is effectively serialised, and a single
  pathological page blocks every other worker for its full duration.
- **Trigger (concrete).** A business whose homepage serves `"a" * 200000 + "@!"`. The URL is
  attacker-chosen: it comes from OSM `contact:website`/`url` tags (`osm.py:91`) or Google
  `websiteUri` (`google_places.py:291`), so whoever registers that business listing picks
  the page. One such page ≈ 13 min; 40 of them ≈ 9 hours, well past `timeout-minutes: 300`.
- **Fix:** stop running an unbounded `findall` over the whole body. Cheapest correct fix is
  to scan for candidate positions instead of letting the engine try every one — e.g.
  iterate `re.finditer(r"[\w.%+-]+@[\w.-]+", html)` with a non-backtracking prefix, or
  pre-filter with a cheap `if "@" not in html` guard plus a character-set
  `str.translate`-style filter. Better still: move extraction into a `ProcessPoolExecutor`
  so it cannot hold the GIL at all, and cap the body by *time* as well as bytes (next
  finding).

---

### S1 — `timeout=8` bounds inactivity, not total read time: 40 trickle-feeding hosts wedge the pool forever

- **Where:** `enricher.py:175` (`timeout=8, stream=True`) and `enricher.py:188`
  (`r.raw.read(_MAX_BODY_BYTES, decode_content=True)`); pool teardown at `enricher.py:237`.
- **Confirmed from source, not assumed:**
  - `requests==2.32.3` `src/requests/adapters.py:661-664` converts a scalar `timeout` into
    `TimeoutSauce(connect=timeout, read=timeout)`.
  - `urllib3` `connectionpool.py:536` assigns that to `conn.timeout` — i.e. it becomes a
    **socket-level inactivity deadline, reset on every `recv()`**.
  - `urllib3` `response.py:1177-1188` loops `while len(self._decoded_buffer) < amt and data:`
    issuing *another* `_raw_read(amt)` per iteration, each with a fresh 8 s budget.
- **Demonstrated on loopback** (local socket only, no external network): with
  `socket.settimeout(8)` and a server sending 1 byte every 0.2 s, 400 bytes transferred over
  **83 s across 401 `recv()` calls and the timeout never fired.** Scaled to
  `read(200_000)`: at 1 byte per 7 s a single worker is blocked for ≈ **16 days**.
- **Concurrency consequence.** `stream=True` makes the read the long pole, and there is no
  per-task wall-clock budget. 40 trickling hosts pin all 40 workers. Then:
  - `ThreadPoolExecutor.__exit__` (`enricher.py:237`) blocks in `shutdown(wait=True)`, so
    the pool never drains;
  - worker threads are non-daemon and are joined at interpreter exit via
    `concurrent.futures.thread._python_exit`, registered with
    `threading._register_atexit` at stdlib `concurrent/futures/thread.py:37` — so the
    process **cannot even exit**;
  - `enrich()` is called at `main.py:187`, before every `write_csv` (`main.py:209-213`),
    so the job dies at `timeout-minutes: 300` (`scrape.yml:25`) having produced **nothing**.
    The `always()` artifact step (`scrape.yml:91-98`) uploads only `data/`, which is empty.
- **Overlap with existing coverage, stated honestly:** `docs/audits/005-enricher-http-robustness.md`
  flags the trickle/tarpit mechanism generically. That audit was written against the old
  `stream=False` code. What is new here: (i) the defect survived the `stream=True` rewrite,
  and (ii) the *interaction* with the GIL and with pool teardown, which is what turns a
  per-worker stall into a total-loss run.
- **Fix:** enforce a total read deadline, not just a byte cap — read in bounded slices and
  track `time.monotonic()` against a per-URL budget, aborting with UNKNOWN past it; or use
  `asyncio.timeout` over an `httpx`/aiohttp client. Add a `deadline` concept to
  `httpclient.fetch_with_retry` (`httpclient.py:149-217`) so every caller inherits it.

---

### S2 — Every exception becomes a silent `UNKNOWN`, and nothing anywhere asserts the UNKNOWN rate is sane

- **Where:** `except Exception: return UNKNOWN, contacts` at `enricher.py:176-178`; tally
  printed but never checked at `enricher.py:264-275`; validation at
  `.github/workflows/scrape.yml:71-83`.
- **Breaks.** `_fetch_website` returns UNKNOWN for *every* failure mode and the caller only
  `print`s the result. `scrape.yml:74-83` validates that the CSVs are non-empty and have
  ≥100 rows — nothing about `website_live` distribution. So a run in which **100% of
  websites are UNKNOWN publishes cleanly and looks successful.**
- **Concrete trigger — file-descriptor exhaustion.** `get_session()`
  (`httpclient.py:61-71`) creates a `requests.Session` per worker thread and parks it in
  `threading.local()`; **nothing in the codebase ever calls `Session.close()`.**
  `requests==2.32.3` sets `DEFAULT_POOLSIZE = 10` (`adapters.py:71-72`) and builds
  `PoolManager(num_pools=connections)` (`adapters.py:259-260`), and
  `urllib3` `poolmanager.py:208, 229-230` caches that many origin pools per manager. So each
  of the 40 sessions holds up to 10 idle keep-alive sockets for the whole multi-hour pass:
  **≈400 idle sockets, plus up to 40 in flight.** On a host with a low fd soft limit,
  `socket()` raises `OSError: [Errno 24]`, `requests` wraps it as `ConnectionError`, and
  `enricher.py:176` swallows it. Every remaining target becomes UNKNOWN.
  *(This machine's `ulimit -n` is 1048576, and GitHub-hosted runners are typically 1024, so
  the trigger is host-dependent — but the silent-failure consequence is not.)*
- **Downstream damage, anchored.** `enricher.py:300-304` awards `+20` only for
  `website_live is False` and `0` for `None`, and `pitch_recommender.py:34-35` branches on
  the same field. So a total-UNKNOWN run under-scores every lead by up to 20 points and
  routes all of them away from the rebuild pitch — with no error in the log, the CSV, or the
  workflow result.
- **Fix:** after the tally at `enricher.py:264-275`, fail loudly when
  `unknown_count == len(targets)` or exceeds a sane fraction (e.g. > 30%), and log a
  per-exception-type counter rather than a bare tally. Separately, close worker sessions —
  `pool.submit` one teardown task per thread, or use a `threading.local` finaliser, so fds
  are released instead of accumulating across a multi-hour pass.

---

### S3 — Duplicate URLs are fetched concurrently, with no dedup and no per-host cap

- **Where:** `targets` built at `enricher.py:231`; compare the Places scraper, which does
  this correctly at `google_places.py:181-182` and `263-266` (a `seen_ids` set under
  `seen_lock`).
- **Breaks.** `targets` is built straight from `records` with no dedup, so N records sharing
  a website produce N GETs fired at the same host simultaneously — from N *different*
  thread-local sessions, so there is no connection reuse and each pays a fresh TCP+TLS
  handshake. This is guaranteed after `dedup` merges on `(normalized_name, city)`
  (`BRIEF.md:33`) for chains that list several branches on one site, and common on shared
  cPanel/Wix/Salla hosts.
- **Trigger:** 12 records from one host all with `website = "https://shop.example/"` →
  12 concurrent GETs to one origin, no keep-alive reuse, and a burst shape that WAFs treat
  as abuse.
- **Fix:** group `targets` by `(host, url)`, submit one task per *distinct* URL, and fan the
  single result out to every index. Then cap per-host concurrency at 1–2 before submitting.

---

### S3 — Thread-local sessions removed the cookie *race* but not cookie *nondeterminism*

- **Where:** `httpclient.py:58-71`; `enricher.py:175`.
- **Breaks.** `get_session()` returns the *same* `Session` for a thread's entire life, so its
  cookie jar accumulates every cookie set by every site that thread visits. Two records with
  the same URL landing on different workers therefore see different cookie state → different
  HTML → different extracted contacts and possibly a different `website_live`, **run to run**.
- **Scope this correctly, because the easy version of this claim is wrong:** the jar is
  domain/path-scoped, so there is **no cross-domain cookie leakage**, and
  `RequestsCookieJar` synchronises its own mutations, so **the jar itself does not
  corrupt**. The nondeterminism comes only from the duplicate fetches in the previous
  finding. This also means `docs/audits/004-enricher-thread-safety.md`'s first S2 ("shared
  session cookies make same-origin fetches order-dependent") is **fixed as a race** — the jar
  is now per-thread — but the underlying nondeterminism survives by another route.
- **Fix:** for a one-shot liveness probe, send no persisted cookies at all. Install a
  deny-all `CookiePolicy` on the session, or use a throwaway session per URL and drop the
  pooling benefit (which the S3 above makes largely worthless anyway).

---

## Not a bug, but worth knowing

- **The single-writer invariant is load-bearing and looks accidental.**
  `futures = {pool.submit(_fetch_website, url): idx ...}` (`enricher.py:238`) *reads* like
  parallel submission, but `pool.submit` is evaluated inside a dict comprehension **on the
  calling (main) thread**, and `as_completed` (`enricher.py:240`) is consumed **on the main
  thread** too. So `records[idx] = ...` (`enricher.py:249-259`) is executed by exactly one
  thread. This is the correct design and it is why there is no dict race. **It is also one
  refactor away from a 40-thread write race:** `future.add_done_callback(...)` runs its
  callback *in the worker thread that completed the future*, so "optimising" the merge into
  callbacks would silently introduce unsynchronised writes over shared dicts — no exception,
  no test, no log. Put a comment on `enricher.py:238` and add a test that asserts it.
- **The thread-local session fix closed `004`'s second S2 as well.** That finding needed
  *concurrent* requests flowing through one `PoolManager` for the eviction race to have a
  window. With one in-flight request per thread-local session there is no longer any such
  window. `docs/audits/004-enricher-thread-safety.md` is now stale and should be marked as
  superseded; do not spend implementation time on its urllib3 pinning argument as written.
  (Pinning urllib3 is still good hygiene — see `BRIEF.md:43-44` — but it is no longer
  load-bearing for thread safety.)
- **This is not a decompression bomb — I checked.** `urllib3` `response.py:1173, 1186`
  passes `max_length=amt - len(self._decoded_buffer)` into the decoder, so a gzip body
  cannot inflate past the 200 KB cap; the `_decoded_buffer` is trimmed by
  `get(amt)` (`response.py:1189`). Peak memory per worker stays bounded. The
  `enricher.py:186-187` comment ("Cap the body before decoding it") is slightly misleading
  — the cap is enforced *on* the decoded buffer, not before decoding — but the cap holds.
- **`enricher.py:327-328` `urllib3.disable_warnings(InsecureRequestWarning)` is now dead and
  mildly harmful.** With `verify=True` at `enricher.py:175` the enricher cannot emit that
  warning any more. It is a *process-global* `warnings` filter mutation, and leaving it in
  place means a future reintroduction of `verify=False` would fail silently. It also runs
  before the pool starts (`enricher.py:337`), so it is at least not a data race today.
  Delete it.
- **No cross-pool interaction — the three pools are independent and sequential.**
  `main.py:160-168` runs the 3 scrapers to completion *before* `enrich()` at
  `main.py:187`; `check_websites` never overlaps them. `google_places.py:159-166` builds
  its own sessions and never touches `httpclient._local`, so the 5 Places workers and the 40
  enricher workers share nothing. `scrape.yml:15-17` now serialises whole runs via
  `concurrency.group`, so two CI jobs cannot race on process state either.
  (`osm.py:35` and `wikidata.py:73` do use `get_session()`, but they run on the scraper
  threads, which exit before `check_websites` creates its pool — so no session is inherited
  into the enricher workers.)
- **`futures` is built eagerly** (`enricher.py:238`): for a cumulative master with 100k
  websites that is 100k `Future` objects plus a 100k-entry dict allocated up front, and it
  grows every run. Bounded and not urgent, but it argues for a bounded submission window
  (e.g. `map` over a chunked iterator) once the master gets large.
- **`check_websites` is not safe to call twice concurrently** on the same `records` list —
  two pools would race their `records[idx]` writes. Nothing does this today, and nothing in
  the signature says so.
- **Reentrancy of `_fetch_website` is trivial:** it makes no nested calls and takes no lock,
  and its only caller is `check_websites` (`enricher.py:238`).
- **The `except Exception` at `enricher.py:244-248` is near-dead** (no path in
  `_fetch_website` raises `Exception` today) but is a reasonable last-resort net. Keep it.
- **Zero test coverage.** `tests/` contains only `test_lead_signal.py`, which never imports
  `_fetch_website` or `check_websites`. Nothing guards the single-writer invariant, the
  quadratic scan, or the fd accounting.

---

## Recommended order of work

1. **Kill the quadratic `_EMAIL_RE` scan** (S1a). Highest blast radius, cheapest fix, and it
   is the difference between a run finishing and not. Add a regression test with body
   `"a" * 200_000 + "@!"` asserting it completes in well under a second.
2. **Add a wall-clock deadline to the body read** (S1b) — bounded *time*, not just bounded
   bytes. Best done once in `httpclient` so `osm.py`, `wikidata.py` and `enricher.py` all
   inherit it.
3. **Assert the UNKNOWN rate and close worker sessions** (S2). Turns a silent total-loss run
   into a loud failure, and stops fd accumulation over a multi-hour pass.
4. **Dedup `targets` by URL and cap per-host concurrency** (S3) — also removes the
   run-to-run cookie nondeterminism for free.
5. **Pin the single-writer invariant with a test**, and mark
   `docs/audits/004-enricher-thread-safety.md` as superseded.
6. **Delete the dead `disable_warnings`** (`enricher.py:327-328`) and the misleading
   "before decoding" comment at `enricher.py:186-187`.

---

### Reproduction of the measurements (stdlib only, no network, no installs)

```python
import re, time
P = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")   # enricher.py:130
for n in (8000, 16000, 32000):
    t = time.perf_counter(); P.findall("a" * n + "@!"); print(n, time.perf_counter() - t)
# -> 0.127s, 1.100s, 4.974s   (log-log exponent b = 2.24; ~793s extrapolated to n=200_000)
```
