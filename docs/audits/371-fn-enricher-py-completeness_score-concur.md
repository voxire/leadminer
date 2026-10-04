# 371 — `enricher.completeness_score` under concurrency

## Verdict

**Currently safe, and safe for a non-obvious reason.** `completeness_score`
(`enricher.py:101-117`) is pure — no globals, no locks, no I/O, no recursion, so it is
reentrant and per-call atomic under the GIL. It is also, today, only ever reached
after every worker thread in the process has been joined: `main.py:160-168` joins the
3 scrapers before `enrich()` at `main.py:187`, and `enricher.py:237-262` joins the
40-thread fetch pool *before* `enricher.py:337` returns. That barrier is what makes
the function's input quiescent. The real concurrency defect is one layer up: the
fetch that produces 4 of the 7 fields this function counts is issued **once per
record rather than once per URL** (`enricher.py:231`, `enricher.py:238`), so 40
concurrent workers hit the same host 40 times and the identical business gets a
different `completeness_score` in the same CSV depending on which fetch happened to
succeed. That is an S2 determinism bug in the score column, not in the function.

## Findings

### S2 — `check_websites` fetches per record, not per URL: one host, 40 simultaneous requests, run-varying scores for identical businesses

- **Where:** `enricher.py:231` (target list), `enricher.py:238` (one future per
  target), `enricher.py:251` (contact write-back gated on `LIVE`), `enricher.py:178`
  (every transport failure becomes `UNKNOWN`).
- **Breaks:** `targets` is keyed by **record index**, not by URL:

  ```python
  targets = [(i, r["website"]) for i, r in enumerate(records) if r.get("website")]  # :231
  futures = {pool.submit(_fetch_website, url): idx for idx, url in targets}          # :238
  ```

  A multi-branch business — the exact shape this pipeline produces, and the shape
  `docs/audits/057-chain-detection.md:358` calls out — contributes N records with the
  same `website`. That submits N *independent* HTTP GETs for one host, all admitted to
  the 40-wide pool at once. Consequences, in order of severity:

  1. **A single host receives up to 40 simultaneous connections.** That is the
     request shape that provokes `429` and bot-challenge responses.
  2. **Those responses are silently downgraded.** `enricher.py:176-178` catches every
     exception and returns `UNKNOWN`; `enricher.py:251` then skips the entire
     contact write-back. Per the tri-state design at `enricher.py:154-156` this is
     *correct* for liveness — it correctly refuses to call an unreachable site
     "broken" — but it means a self-inflicted overload turns into permanent, silent
     loss of `email` / `instagram` / `whatsapp` / `linkedin`.
  3. **The loss lands directly in this function's input.** `completeness_score` counts
     `email` (`:105`), `instagram` (`:111`), `whatsapp` (`:113`) and `linkedin`
     (`:115`) — 4 of its 7 points. Branches that got a `200` and branches that got a
     timeout differ by up to 4 points on the same `website`, **in the same run, in the
     same CSV**, for the same real business.
  4. **It propagates to the shipped products.** `main.py:206` splits
     `qualified_businesses.csv` on this value and `pitch_recommender.py:48` reads it
     to choose the pitch.
- **Trigger:** 12 records, one per branch, all with
  `website = "https://starbuckslebanon.com"`, all otherwise identical. Twelve of the
  forty in-flight requests land on the host simultaneously. Say the host serves 8 and
  returns `429` to 4. The 8 successful fetches populate contacts → `completeness_score = 5`;
  the 4 blocked ones get `UNKNOWN` → contacts stay `None` → `completeness_score = 1`.
  Four branches of one chain are filed as near-empty leads. Re-running the job
  produces a *different* split, because which 4 of the 12 lost the race is not
  deterministic.
- **Not a crash, not a torn dict** — the run completes and every row is internally
  consistent. It is wrong *data*, just not *corrupt* data. I rate it S2 rather than
  S1 because the tri-state `UNKNOWN` design (deliberate, per `enricher.py:145-153`)
  means an unreachable host is already a modelled outcome, and because the root cause
  — a URL fetched N times — is a correctness/memoisation bug that 40-way concurrency
  merely amplifies. If the team considers "the same business scores 5 and 1 in one
  export" a trust-breaking result, this is their S1; I would not argue hard against it.
- **Fix:** memoise by URL and fan the single result out to every index —
  `by_url: dict[str, list[int]]` from `:231`, one `pool.submit` per key, then
  `for i in idxs: apply(records[i], outcome, contacts)`. This also cuts GET volume
  from ~1-per-record to 1-per-distinct-host-path, which is the real fix for (1).

### S3 — The serialization of the write-back onto the consumer thread is load-bearing and undocumented: 4 of the 7 fields read here are exactly the 4 written there

- **Where:** writes at `enricher.py:250-259`; reads at `enricher.py:103-116`; the
  consumer loop at `enricher.py:240-262`.
- **Breaks:** nothing today. But the safety of `enricher.py:349` rests entirely on an
  accident of *where* the code sits, not on any lock. Note carefully that lines
  `249-259` are in the body of `for future in as_completed(futures)` — i.e. on the
  thread that called `check_websites`, **not** in `_fetch_website`. So all 40 futures
  write into `records[idx]` serially, from one thread, and the whole block is complete
  before `enricher.py:337` even returns.
- **Why it matters:** if anyone "optimises" this by moving the write-back into the
  worker (`return records[idx], outcome, contacts`), then 4 worker threads start
  mutating 4 keys of the same dict while the consumer computes a 7-key read. CPython
  dicts give you **no torn dictionary** — `dict.get` and `__setitem__` are single C-level
  operations that never release the GIL — so nothing crashes and nothing looks wrong.
  What you get instead is a **non-atomic multi-key transaction**: a partially applied
  fetch. I confirmed this is a real mechanism, not a theoretical one. Probe (writer
  sets 4 keys, reader counts them):

  ```
  default sys.setswitchinterval (5 ms):  partial states observed = none
  sys.setswitchinterval(1e-9):           partial states observed = [2]   # 2 of 4 keys
  ```

  At default settings the GIL's 5 ms switch interval makes it improbable, and I want to
  be honest that I could not provoke it without artificially shrinking the interval. It
  is a latent hazard, not an observable bug — which is precisely why it is dangerous.
- **Trigger (post-refactor only):** 4 records sharing one URL, write-back moved into
  `_fetch_website`, thread A sets `email` + `instagram`, thread B sets `whatsapp` +
  `linkedin`; `enricher.py:349` reads during the gap and returns 2 where 4 is correct.
- **Fix:** one comment at `enricher.py:249` — "write-back must stay on the consumer
  thread; `_fetch_website` must not touch `records`" — plus the assertion that a
  per-record result must be applied under one lock or on one thread. One line now,
  saves a very quiet bug later.

### S3 — Thread-local `requests.Session`s are never closed, and the context-manager that would close them is dead code

- **Where:** `httpclient.py:58`, `httpclient.py:61-71`, `httpclient.py:88-91`;
  consumed at `enricher.py:175`, `scrapers/osm.py:35`, `scrapers/wikidata.py:73`,
  `scrapers/google_places.py:189` + `:159-166`.
- **Breaks:** `get_session()` caches a `Session` on `threading.local` and never
  registers a finalizer. `ThreadPoolExecutor` threads are per-pool and die at
  `shutdown`, so at `enricher.py:262` all 40 sessions — each holding a urllib3
  connection pool — become garbage and are reclaimed by the cyclic GC on a schedule
  the interpreter picks. `requests` installs no `atexit` closer, so the sockets are
  not deterministically shut down either. Note the irony: `http_session()` at
  `httpclient.py:88-91` is decorated `@contextmanager` and reads like it closes,
  but it is `yield get_session()` with **no `finally: session.close()`** — and
  `grep` across the repo shows it is never called anywhere. The one abstraction that
  looks like it manages session lifetime is both broken and unused.
  `GooglePlacesScraper._make_session` (`:159-166`) has the same problem: 5 unclosed
  sessions per run.
- **Trigger:** any run. Not observable in output — this is hygiene, not corruption.
- **Fix:** have `get_session` keep a module-level list of created sessions and close
  them all in `check_websites`'s `finally` (`enricher.py:262`), or give
  `http_session()` a real `finally: session.close()` and use it at
  `enricher.py:175`.

### S3 — Connection reuse across a 40-thread pool is weaker than the code implies

- **Where:** `enricher.py:237-238`, `httpclient.py:61-71`.
- **Breaks:** `requests`' default `HTTPAdapter` is `pool_connections=10,
  pool_maxsize=10, pool_block=False`. Each worker thread's session therefore caches at
  most 10 host pools, and with `block=False` it **discards and closes** connections
  past `maxsize` rather than queueing. Across a few hundred thousand distinct
  websites, consecutive fetches land on different hosts, keep-alive hits are rare, and
  TLS handshakes dominate. That inflates wall-clock time against the workflow's
  300-minute timeout (`scrape.yml`) and — the part that matters here — increases total
  request volume against each host, which feeds straight back into the S2 above.
- **Fix:** mount an adapter with a larger `pool_connections`/`pool_maxsize` in
  `httpclient.get_session`, once the per-URL memoisation lands so requests are not
  wasted.

### S3 — `check_websites` mutates the caller's list in place and returns it, making `records = check_websites(records)` a no-op rebinding

- **Where:** `enricher.py:233`, `enricher.py:249-259`, `enricher.py:276`,
  called as `enricher.py:337`.
- **Breaks:** nothing today, because `dedup` guarantees the record objects are fresh
  (see below). But the signature advertises a transformation and delivers a mutation.
  Any future caller that shares a record object across two lists — a cache, a
  "compare before/after" snapshot, a retry buffer — gets aliasing and lost updates,
  and will not get a `TypeError` or an exception to point at it.
- **Fix:** drop the `return records` and make the mutation explicit at the call site,
  or return `list(records)`.

### S3 — `enrich()` mutates process-global urllib3 warning state that is now vestigial

- **Where:** `enricher.py:327-328`.
- **Breaks:** `urllib3.disable_warnings(InsecureRequestWarning)` is called on every
  `enrich()`, but `verify=True` was restored at `enricher.py:175` (the reasoning is in
  the comment at `enricher.py:169-174`). With certificate verification on, that warning
  class is never raised, so this line suppresses nothing. It is a global, unsynchronised
  mutation in an enrichment path — harmless single-threaded, a latent global write the
  moment anyone calls `enrich` from a pool (which is exactly what the incremental-scraping
  ideas in `docs/audits/035-incremental-scraping.md` contemplate).
- **Fix:** delete both lines.

## Not a bug, but worth knowing

- **The barrier chain is real and complete.** I verified it rather than assuming it.
  After a `with ThreadPoolExecutor(...)` block, zero worker threads remain alive:

  ```
  alive threads after with-block: []
  ```

  And the chain is: `google_places.py:194-197` joins its 5 workers before
  `yield from all_records` at `:199`; `main.py:157-158` drains that generator with
  `list()` *inside* the outer worker, so the outer future cannot resolve until the
  inner pool is gone; `main.py:160-168` joins all 3 scrapers before `enrich()` at
  `main.py:187`; `enricher.py:237-262` joins all 40 fetchers before
  `enricher.py:337` returns; `enricher.py:349` then runs with no live threads anywhere.
  `threading.Thread.join()` also gives the happens-before edge, so the writes at
  `enricher.py:250-259` are visible to the reader at `enricher.py:349`.
- **The function is pure and reentrant.** `enricher.py:101-117` reads only its
  argument via `dict.get`, mutates one local `int`, and calls nothing. No module-level
  mutable state is touched (contrast `_EMAIL_BLACKLIST` at `:138`, which `_fetch_website`
  reads concurrently — read-only, so fine). No bytecode boundary in the body releases
  the GIL, so even in the counterfactual where 40 threads called it simultaneously,
  no interleaving would be observable. The only multi-key race available would come
  from the *caller's* writes, which is the S3 above.
- **Row order is genuinely nondeterministic, and this function is immune to it.** Two
  independent sources of ordering nondeterminism exist: `raw.extend(batch)` inside
  `as_completed` at `main.py:166`, and `all_records.extend(results)` at
  `google_places.py:192` following per-query completion order. That nondeterminism
  propagates into the dedup fold order at `main.py:180`. But `dedup._pick`
  (`dedup.py:159-187`) is **presence-monotone** — `dedup.py:168-171` returns the
  non-missing side, and the rank tuple ends in `str(value)` (`dedup.py:184`), a total
  order on the value — so once a field is present in a merge it can never become absent,
  and the merge is commutative on presence. Since `completeness_score` counts only
  *presence* of 7 fields, its output is invariant to fold order. Verified empirically
  over all 6 permutations of 3-record groups, 500 randomised groups:

  ```
  fold orders giving differing completeness_score sets: 0/500 trials
  _merge(a,b) vs _merge(b,a) on presence: ('b@x.com','b@x.com') ('https://a.com','https://a.com')
  ```

  So the score column is reproducible across runs even though the surrounding row
  contents are not. **This is a property worth protecting with a test**, not a property
  to rely on: anyone who makes `_pick` rank on something recency- or order-sensitive for
  one of the seven counted fields will silently make this column run-varying, and nothing
  will fail.
- **No aliasing anywhere.** `dedup` builds fresh dicts on every path —
  `dict(record)` at `dedup.py:210` and `:218`, and `merged = {}` at `dedup.py:191` — so
  the objects in `records` are never the same objects as those in `master`
  (`main.py:150`) or `raw_filtered` (`main.py:172`). `enrich()`'s in-place mutation
  therefore cannot corrupt the loaded master, and no two entries in `records` alias
  each other. This is what makes the S3 above hypothetical rather than live.
- **`GooglePlacesScraper`'s shared state is correctly locked.** `seen_ids` check-then-add
  at `google_places.py:263-266` is inside `with seen_lock`, so there is no TOCTOU on
  the `place_id` dedup, and `all_records.extend` at `:191-192` is inside
  `records_lock`. Both are the two places this codebase actually had to get right, and
  both are right. Per-thread `Session` at `:189` is also correct.
- **Response lifetime is clean on every path.** In `_fetch_website`, the `finally` at
  `enricher.py:193-197` closes `r` on both the `LIVE`/`DEAD` and the read-failure path,
  and when `get_session().get(...)` raises there is no response object to leak.
  `except Exception` (`enricher.py:176`, `:190`) correctly does not swallow
  `KeyboardInterrupt`. Peak memory is bounded at 40 × 200 KB = ~8 MB of body
  (`enricher.py:158`, `httpclient.py:54`) — fine.
- **Out of lens, but it undermines this column:** `completeness_score` uses raw
  truthiness (`enricher.py:103-116`) while `dedup._is_missing` (`dedup.py:79-90`) treats
  whitespace-only strings as missing. A `website` of `"   "` therefore survives as
  `website` after merge but scores +1. Flagging for whichever lens owns field validation.

## Recommended order of work

1. **Memoise `check_websites` by URL** (`enricher.py:231`/`:238`) and fan the single
   result out to every record index. Fixes the S2, cuts request volume, and removes the
   self-inflicted `429`s that drive the silent contact loss in the first place.
2. **Comment the write-back invariant** at `enricher.py:249` and add a test that
   asserts `check_websites` returns only after every index has been written — this is
   the one thing protecting `enricher.py:349`, and it is currently implicit.
3. **Add the order-invariance test** for `completeness_score` under permuted dedup
   fold order (the 500/500 property above). Cheap, and it pins down a property the
   pipeline currently gets right by accident of `dedup._pick`'s design.
4. Close sessions properly: fix `http_session()` (`httpclient.py:88-91`) to actually
   close, and close the thread-local sessions when `check_websites` drains
   (`enricher.py:262`). Then widen the HTTPAdapter pools.
5. Delete `enricher.py:327-328`, and make `check_websites`'s in-place mutation obvious
   in its signature.

### Reproducing the probes

No network, no installs. `requests` is stubbed so the real `enricher` module imports;
`completeness_score` and `dedup` are exercised unmodified.

```python
import sys, types
stub = types.ModuleType("requests")
class _Exc(Exception): pass
stub.RequestException = _Exc; stub.Session = object
sys.modules.setdefault("requests", stub)
sys.path.insert(0, "/Users/mhomsi/dev/dummy/leadminer")
from enricher import completeness_score
from dedup import dedup, _merge

# (a) barrier: no worker survives the `with` block
from concurrent.futures import ThreadPoolExecutor, as_completed
with ThreadPoolExecutor(max_workers=8) as pool:
    for f in as_completed([pool.submit(lambda i=i: i) for i in range(8)]): f.result()
print([t.name for t in threading.enumerate() if t is not threading.main_thread()])  # []

# (b) partial multi-key reads (needs an artificially small switch interval)
sys.setswitchinterval(1e-9)
shared = {"email": None, "instagram": None, "whatsapp": None, "linkedin": None}
stop = threading.Event(); seen = set()
def writer():
    vals = {"email": "e@x.com", "instagram": "ig", "whatsapp": "+961", "linkedin": "li"}
    while not stop.is_set():
        for k, v in vals.items(): shared[k] = v
        for k in vals: shared[k] = None
w = threading.Thread(target=writer, daemon=True); w.start()
for _ in range(200000):
    n = sum(1 for k in shared if shared.get(k))
    if n not in (0, 4): seen.add(n); break
stop.set(); w.join(); print(sorted(seen))  # [2] at 1e-9, [] at the 5 ms default

# (c) order-invariance of the score across dedup fold orders
FIELDS = ["email","website","address","facebook","instagram","whatsapp","linkedin"]
def mk(i, src, j):
    d = {"name": f"BIZ{i}", "phone": "+9611234567", "source": src,
         "country": "LB", "scraped_at": f"2026-10-0{i}T00:00:00Z"}
    for f in FIELDS:
        d[f] = None if (i + FIELDS.index(f) + j) % 3 == 0 else f"{f}-v{i}"
    return d
random.seed(7); bad = 0
for t in range(500):
    g = [mk(i, random.choice(["google_places","osm","wikidata"]), random.randint(0,2)) for i in range(3)]
    s = {tuple(sorted(completeness_score(r) for r in dedup([dict(p) for p in perm])))
         for perm in itertools.permutations(g)}
    bad += len(s) > 1
print(f"{bad}/500")  # 0
```
