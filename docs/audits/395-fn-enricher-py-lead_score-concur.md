# 395 — `lead_score` under concurrency

**Target:** `lead_score` — `enricher.py:283`
**Lens:** concur

## Verdict

`lead_score` is a **pure function of its argument and is currently safe**. It reads eight
keys off `record`, writes nothing, touches no module-level mutable state, and is called
from exactly three places — `enricher.py:350`, `main.py:197`, `cli.py:269` — all of which
execute on the main thread *after* `ThreadPoolExecutor.__exit__` has joined every worker.
There is no data race on the dicts it reads and it is trivially reentrant; **no lock is
needed inside it, and adding one would be cargo-culting.**

The real exposure is not in the function. It is (a) that `lead_score` currently reads
`industry_priority` before any writer in the pipeline sets it, with correctness resting on a
recompute 40 lines later in a different file, and (b) that the concurrency of
`check_websites` — which produces `website_live`, the single largest input to the score —
has two concrete defects (uninterruptibility, and an eager one-Future-per-target submit
that I measured at ~330 MB per 200k websites). Nothing here corrupts a record today. The
first finding is a live 15-point understatement that is currently masked.

---

## Findings

### S2 — `lead_score` reads `industry_priority` before any writer sets it; the safety edge is implicit and undocumented

- **Where:** `enricher.py:350` (the call), `enricher.py:306` (the read),
  `enricher.py:337-350` (enclosing loop), `main.py:190` (the actual writer),
  `main.py:195-197` (the masking recompute), `cli.py:267-269` (the correct ordering)
- **Breaks:** `enrich()` calls `lead_score(r)` at `enricher.py:350`, but
  `industry_priority` is not written until `main.py:190`, after `enrich()` returns. Inside
  a single `enrich()` call the score is therefore computed against two different worlds:

  - **Freshly-scraped rows** carry `industry_priority=None` by construction —
    `osm.py:116`, `wikidata.py:124`, `google_places.py:299` all emit `None`. They score
    **0** priority points at `enricher.py:306`.
  - **Master rows** loaded by `load_master` (`main.py:77-105`) carry the *previous run's*
    priority string. They score **15** or **8**.

  So two identical restaurants in the same run get 15 points of priority credit in
  `enrich()`'s output if one was already in the CSV and 0 if it was not. The exported CSVs
  survive only because `main.py:197` recomputes after `main.py:190`.

  Verified arithmetic (ran the real function, inputs: `category="restaurant"`,
  `phone="+9611234567"`, `website="https://a.example"`, `source="osm"`):

  | call site | `industry_priority` seen | result |
  |---|---|---|
  | `enricher.py:350` (fresh row) | `None` | **15** |
  | `enricher.py:350` (master row) | `"high"` | **30** |
  | `main.py:197` / `cli.py:269` (correct order) | `"high"` | **30** |

  With a server-confirmed dead site (`website_live=False`) it is **35 vs 50**. Medium
  priority understates by 8. `website_live` is `_VOLATILE` in `dedup.py:114`, so the stale
  master verdict is the one that wins the merge — the score instability compounds.

- **Trigger:** Call `enrich()` directly instead of going through `main.main()`. `cli.py`
  already demonstrates the correct order (`cli.py:267` sets priority, `cli.py:269` scores),
  which proves `enricher.py:350` is the odd one out. Any future caller, notebook, or test
  that uses `enrich` as the public API it appears to be gets 15-point-underscored rows.
- **Latent second manifestation — the same finding if the loop is parallelised.** The only
  thing making the read safe is that `ThreadPoolExecutor.__exit__` performs
  `shutdown(wait=True)` → `t.join()` on every worker before `enricher.py:339` runs.
  Nothing in `enrich()` or `lead_score()` states or enforces that precondition. The
  serial loop at `main.py:189-197` is the obvious next thing to parallelise in a codebase
  that already runs three pools. If it is wrapped in a pool, `lead_score` becomes
  **non-atomic over a record** in a way the GIL does not save it from: `check_websites`
  writes `website_live` at `enricher.py:250` and then, separately, `email`/`instagram`/
  `whatsapp`/`linkedin` at `enricher.py:252-259`. A concurrent scorer can observe
  `website_live=True` (+10 at `enricher.py:302`) with `email` not yet written (missing +20
  at `enricher.py:288`) — a **30-point error on one row**. Each individual dict store is
  atomic; the multi-key composite read across `enricher.py:288-317` is not.
- **Fix:** Move the priority assignment before enrichment (`industry_priority` is a pure
  function of `category`, so hoist `main.py:190` above `main.py:187`), delete the
  duplicate write, and delete `main.py:195-197` entirely. Then state the precondition in
  `enrich()`'s docstring: *"must not be called while a website pool is in flight."*

---

### S2 — Ctrl-C does not stop `check_websites`; the eagerly-submitted queue is fully drained

- **Where:** `enricher.py:238` (submit comprehension), `enricher.py:237`
  (`with` → `shutdown(wait=True)`), `enricher.py:175` (`timeout=8`)
- **Breaks:** `enricher.py:238` submits **every** target before the first result is
  consumed. `ThreadPoolExecutor.shutdown` defaults to `cancel_futures=False`, and `__exit__`
  calls `shutdown(wait=True)` with no arguments (verified in CPython
  `concurrent/futures/thread.py`). So on `KeyboardInterrupt` the executor joins all 40
  threads, and the workers keep working through the entire remaining queue before the
  interpreter can act on the signal.

  `_fetch_website` catches `Exception`, and `KeyboardInterrupt` is a `BaseException`, so
  the interrupt does propagate — but only *after* the queue drains. Empirically:

  ```
  submitted all 400                0.00s
  raised KeyboardInterrupt         0.11s     <- after 5 results
  shutdown(wait=True) returned     5.37s     <- 5.26s of shutdown
  tasks actually executed: 400 of 400       <- all 395 remaining ran anyway
  ```

  Scaled to a cumulative master: with 200k queued URLs at 40 workers and an 8s socket
  timeout (`enricher.py:175`), the worst case for an operator who hits Ctrl-C is roughly
  `200000 / 40 × 8s ≈ 11 hours` of work that will not stop. GitHub Actions
  (`timeout-minutes: 300`) hits the same wall: the job is killed, but the wall-clock cost
  is already sunk and nothing partial is written, because `write_csv` only runs at
  `main.py:209-213`.
- **Trigger:** Run against a large `data/all_businesses.csv` and press Ctrl-C during the
  `[Enricher] N/M done...` progress output (`enricher.py:261-262`). The prompt appears to
  hang.
- **Fix:** Pass `cancel_futures=True` — requires calling `pool.shutdown(wait=True,
  cancel_futures=True)` in an `except BaseException:` / `finally:` rather than relying on
  `__exit__`, or better, submit through a bounded window (see next finding) so the queue is
  never deeper than a few hundred items.

---

### S2 — One `Future` per target, submitted up front: ~330 MB per 200k websites (measured)

- **Where:** `enricher.py:231` (`targets` list), `enricher.py:238` (submit comprehension)
- **Breaks:** `enricher.py:238` builds a `dict` of every target → `Future` before
  `as_completed` at `enricher.py:240` starts draining. Three containers reference all N
  futures simultaneously: the `futures` dict, the executor's unbounded work queue, and —
  because CPython's `as_completed` opens with `fs = set(fs)` — a third full set copy.
  `as_completed`'s `_yield_finished_futures(..., ref_collect=(fs,))` then holds a strong
  reference to that set for the whole drain, so completed results are not released early.

  Measured on this machine (CPython 3.14.8):

  | what | cost |
  |---|---|
  | 200,000 bare `concurrent.futures.Future` objects | **314.4 MB** (1,648 B each — dominated by the `threading.Condition` each `Future` constructs) |
  | 200k-entry `dict` comprehension at `max_workers=40` | **329.1 MB** live |

  A cumulative master grows monotonically (`main.py:150` loads it, `main.py:179` appends
  to it, nothing prunes it), so this scales with total history rather than with new
  records. It is the plausible cause of an OOM kill late in a long run — which, per the
  workflow's `timeout-minutes: 300`, looks identical to a timeout and produces no output
  at all, since the atomic-write path at `main.py:108-134` never runs.
- **Trigger:** A master with 200k rows of which 120k have a `website` → ~120k targets →
  ~198 MB of `Future` objects, on top of the ~500k record dicts already resident.
- **Fix:** Bound the in-flight window. Submit at most `workers * 4` tasks ahead, refilling
  from `as_completed` as each finishes. This also fixes the previous finding for free,
  since the queue can then actually be cancelled.

---

### S3 — No per-origin coalescing or cap: one host takes up to 40 concurrent connections

- **Where:** `enricher.py:231` (targets built per record, not per URL),
  `enricher.py:238`, `enricher.py:237` (`workers=40`), `dedup.py:197-232` (dedup keys)
- **Breaks:** `enricher.py:231` keys targets by record index, so the same URL is fetched
  once per record. `dedup()` keys only on normalized phone, else
  `(normalize_name, _extract_city(address))` (`dedup.py:206-218`) — it never collapses on
  website, so every branch of a chain survives as its own row. A single origin therefore
  receives up to 40 simultaneous TLS handshakes from 40 different thread-local Sessions
  (`httpclient.py:61-71`), with no per-host semaphore, no jitter, and no in-flight
  de-duplication.

  The failure mode is not corruption — it is **silent score deflation**. A 429, a
  connection reset or a TLS handshake failure lands in `except Exception` at
  `enricher.py:176-178` and returns `UNKNOWN`. I verified the cost of that exactly:
  `lead_score` for a high-priority record with a phone goes **50 with `website_live=False`
  vs 30 with `website_live=None`** — a flat **20-point** loss, which is precisely the
  rebuild-pitch signal from `enricher.py:304`. The records most likely to be rate-limited
  are the high-value ones (big chains, popular domains, anything behind a CDN), so the
  pipeline under-scores exactly the leads it should surface. Note `_fetch_website` does
  **not** use `fetch_with_retry` (`httpclient.py:149`), so there is no backoff here at all.
- **Trigger:** 300 rows carrying `https://www.carrefour.com.lb` (one per branch, distinct
  phones and names, all surviving dedup). 300 concurrent-ish handshakes to one origin;
  the 429/timeout subset records `website_live=None` and loses 20 points each.
- **Fix:** Build `targets` from `set(urls)` and fan the single outcome back out to every
  record index that shares that URL; add a per-host semaphore (e.g. 2) in front of the
  fetch. Cuts requests by the duplication factor *and* removes the rate-limit deflation.

---

### S3 — `website_live` is re-derived every run with no timestamp, so `lead_score` is not reproducible; `dedup` makes a transient verdict permanent

- **Where:** `enricher.py:337` (refetches the whole master every run), `main.py:33-40`
  (`FIELDS` — no `website_checked_at` column), `dedup.py:114` (`_VOLATILE`), `dedup.py:179-187`
- **Breaks:** `main.py:150` loads the cumulative master and `main.py:179` re-appends it, so
  `check_websites` re-fetches **every** website on **every** run, including ones whose
  verdict was established last month. There is no cache keyed on URL and no timestamp
  column — `FIELDS` at `main.py:33-40` has `scraped_at` but nothing recording when the
  liveness probe actually happened.

  So a row's `lead_score` is a function of transient network conditions at probe time. A
  site that answered on run 1 and hit a CDN challenge on run 2 goes 40 → 30 (or 50 → 30 with
  `False`), and the CSV records no evidence that either verdict is three months old. Worse,
  `website_live` is in `dedup._VOLATILE` (`dedup.py:114`), so `_pick`'s rank tuple
  (`dedup.py:179-185`) prefers the more recent observation — a one-off network blip
  *overwrites* a good verdict and is then treated as authoritative forever after.
  Concurrency amplifies this: 40 workers behind one unreliable origin is a much higher
  blip rate than 5.
- **Trigger:** Two consecutive runs against a master where one branch's host rate-limited
  the probe. Row `X` scores 50 in run 1 and 30 in run 2. Nothing in either CSV says why,
  and `leadminer stats` (`cli.py:107-113`) will happily report the 30.
- **Fix:** Add a `website_checked_at` column, only re-probe websites older than N days,
  and carry the previous verdict forward when the new probe returns `UNKNOWN` instead of
  overwriting it with a blank.

---

### S3 — `get_session()` has no close counterpart; `enrich()` mutates process-global urllib3 state, pointlessly

- **Where:** `httpclient.py:58-71` (`_local = threading.local()`, `get_session`),
  `enricher.py:175` (worker-side session acquisition), `enricher.py:327-328`
  (`urllib3.disable_warnings`)
- **Breaks:** Two halves.
  1. `get_session()` creates a `requests.Session` lazily per thread and never closes it.
     `check_websites` spawns 40 threads (`enricher.py:237`); when `shutdown` joins them the
     sessions become unreachable and are reclaimed only by refcounting. It is not a real
     leak — `_fetch_website`'s `finally: r.close()` (`enricher.py:193-197`) returns
     connections properly and closes the response on every path including the `DEAD`
     early return at `enricher.py:184` — but socket teardown is non-deterministic, and on
     a 300-minute CI runner with the `InsecureRequestWarning` filter globally disabled
     (see 2) a failed TLS close is silent. Same applies to `osm.py:35` and
     `wikidata.py:73`, which call `get_session()` on the main.py:160 pool threads.
  2. `enricher.py:327-328` calls `urllib3.disable_warnings(InsecureRequestWarning)`, which
     mutates the **process-global** `warnings.filters` list. It is the only
     process-global mutation in the enrich path and nothing under this lens justifies it:
     `enricher.py:175` passes no `verify` kwarg and so defaults to `verify=True`, which is
     what the comment at `enricher.py:170-174` claims. The call is dead code left over from
     the old `verify=False`, and it now actively suppresses the warning that would tell you
     if some other code path regressed to `verify=False`.
- **Trigger:** `enricher.py:328` runs on every `enrich()` call; the filter is then process-wide
  for the rest of the run, including the Google Places and Overpass requests on sibling threads.
- **Fix:** Delete `enricher.py:327-328` (or move the suppression to an explicit opt-in env
  var scoped to the website probe). Add `close_session()` to `httpclient.py` mirroring
  `get_session()`, and call it from a `finally` in `_fetch_website` — or accept refcount
  reclamation and say so in a comment rather than leaving it undocumented.

---

## Not a bug, but worth knowing

- **`lead_score` needs no lock and is fully reentrant.** Read the whole body
  (`enricher.py:284-319`): every write is to the local `score`, the only global touched is
  `re`, and it is called for its return value only. Two threads calling it on the same
  `record` concurrently produce identical results. Adding a `threading.Lock` here would add
  contention and hide the real ordering contract described in the first finding.
- **The happens-before edge is real, and it comes from the `with` statement.**
  `ThreadPoolExecutor.__exit__` is `self.shutdown(wait=True)`, and `shutdown` ends with
  `for t in self._threads: t.join()`. That join is the memory barrier that makes
  `enricher.py:339-350` safe. It is correct today — but it is the *only* thing making it
  correct, it is not asserted anywhere, and no test covers it.
- **Workers never touch record dicts — confirmed, and this is the strongest part of the
  design.** `enricher.py:231` snapshots `(index, url)` and `enricher.py:238` submits the
  **URL string**, not the record. `_fetch_website` receives an immutable `str` and returns
  a fresh `contacts` dict it allocated at `enricher.py:168`. All record mutation happens on
  the main thread inside the `as_completed` loop (`enricher.py:249-259`). There is no
  shared-dict write from a worker, so the classic "two threads mutate one record" race
  cannot occur. `audit 004` reached the same conclusion
  (`docs/audits/004-enricher-thread-safety.md:26`); it is still true after the Session fix.
- **`BusinessRecord` is a `TypedDict` (`scrapers/base.py:5-28`), so at runtime every record
  is a plain `dict`.** There is no `__slots__`, no custom `__setitem__`, no immutability
  guarantee, and no GIL-adjacent magic — which means the safety argument rests entirely on
  *which thread* writes, not on any type-level protection. Worth stating explicitly so a
  future reader does not assume a TypedDict gives concurrency safety. It does not.
- **`re.sub` at `enricher.py:285` is thread-safe.** I checked the implementation rather than
  assuming: `re._compile` has **no lock** (`hasattr(re, '_compile_lock')` is `False`), so
  I verified it does not need one. Every `_cache` operation is a single dict `get`/`pop`/
  `__setitem__`/`del` (atomic under the GIL), and the one genuinely dangerous step —
  `del _cache[next(iter(_cache))]` for LRU eviction — is wrapped in
  `except (StopIteration, RuntimeError, KeyError): pass`, which is exactly the guard for the
  "dictionary changed size during iteration" race. Two threads may compile the same pattern
  concurrently; that wastes work, not correctness. Also moot in practice: `lead_score` only
  runs on the main thread after the join.
- **Per-thread `Session` via `threading.local()` (`httpclient.py:58-71`) is the right fix
  and it is correctly done.** Each of the 40 `check_websites` workers gets its own Session,
  cookie jar and urllib3 pool. The stale-jar hazard that `004` flagged is genuinely gone.
- **`GooglePlacesScraper` is the only correctly-locked concurrent code in the repo.**
  `google_places.py:182-184` creates `seen_lock` and `records_lock`; `google_places.py:263-266`
  guards the `seen_ids` check-then-add correctly (this is the pattern people get wrong —
  it is a proper critical section, not a bare `add`), and `google_places.py:191-192`
  guards `all_records.extend`. Its 5-worker pool (`google_places.py:142`) and per-worker
  Session (`google_places.py:188-189`) are both sound. Nothing to fix, and it is the right
  template for anything added later.
- **`main.py:160-168` is safe but lossy by design.** The 3-scraper pool correctly
  re-raises nothing silently — each future's exception is caught at `main.py:167-168` and
  that source contributes zero records, which then looks identical to "the source found
  nothing". That is a `020-error-handling-partials` concern, not a concurrency one, but it
  compounds here: a partial `all_records` silently produces a partial master and therefore
  a partial `lead_score` distribution.
- **`lead_score` is called twice per record** (`enricher.py:350` then `main.py:197`). The
  first call is wasted work *and* the source of the first finding. Removing
  `main.py:195-197` once priority is hoisted fixes both at once.
- **Unclamped maximum is 110, clamped to 100 at `enricher.py:319`.** Verified: an
  all-signals record returns exactly `100`. The best leads are therefore indistinguishable
  from each other, which defeats any "top N" sort. Not a concurrency bug, but it means a
  lost 20-point signal (findings 4 and 5) can be entirely invisible at the top of the list.
- **All progress output is main-thread only** (`enricher.py:235`, `261-262`, `271-275`), so
  there is no interleaved-print corruption. `google_places.py` *does* print from its workers
  (`:218`, `:237`, `:244`, `:251`, `:257`) — unsynchronised, but `print` to a single
  stream is line-buffered well enough in practice that this is cosmetic, not a finding.

---

## Recommended order of work

1. **Hoist `industry_priority` above `enrich()` and delete `main.py:195-197`.** One move
   removes a live 15-point understatement, the stale-`dedup._VOLATILE` interaction, and the
   duplicate `lead_score` computation. Highest value per line changed in this report.
2. **Bound the submit window in `check_websites`** (`enricher.py:238`). Fixes the ~330 MB
   measured allocation *and* makes Ctrl-C responsive, by making the queue cancellable.
3. **Deduplicate `targets` by URL and add a per-host concurrency cap.** Fixes the silent
   20-point deflation that hits the highest-value leads hardest, and cuts request volume.
4. **Add `website_checked_at`; stop overwriting a good `website_live` verdict with
   `UNKNOWN`.** Makes `lead_score` reproducible across runs.
5. **Delete `enricher.py:327-328`;** add `close_session()` to `httpclient.py` or document
   that refcount reclamation is the intended teardown.
6. **Add a regression test asserting the happens-before contract**: a test that runs
   `check_websites` over a pool and asserts `lead_score` sees a fully-populated
   `website_live`/`email` pair. That contract is currently load-bearing and untested.