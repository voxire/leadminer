# 387 — Concurrency safety of `check_websites()` at `enricher.py:225`

**Audited at:** commit `99493b9` (working tree was being edited concurrently — see
"Not a bug" #6 for the line drift and the import cycle that appeared and then
vanished).

**Method:** read + instrumented reproduction on `127.0.0.1` only. No scraper was
run and no external host was contacted. `requests` is not installed in this
environment and BRIEF rule 3 forbids installing it, so `http.client`-backed stubs
were used; the behaviours under test (scalar `timeout` applied per `recv`;
`HTTPResponse.read(amt)` blocking until `amt` bytes or EOF; regex backtracking
cost) are `http.client`/`re` semantics, not `requests`-specific behaviour.

## Verdict

The record-dict design is correct and needs no change: workers return tuples,
and only the consuming thread writes `records`. What 40-way parallelism breaks
is the **verdict**, not the memory model. `_fetch_website` maps every
`status >= 400` to `DEAD` (`enricher.py:181-184`), so the 429s and 403s that our
*own* concurrency provokes — measured at 40 simultaneous requests and 1,169 req/s
against a single host when 300 records share one URL — get recorded as dead
sites, which is the highest-scoring (+20) and highest-pitch
("Website rebuild + maintenance") outcome in the whole product. Separately, the
pool has no wall-clock bound on a task: with all 40 workers on tarpit responses,
`check_websites` had not returned after 45 s, printed no progress line, and the
process could not even exit — and because the five CSVs are written *after*
`enrich` (`main.py:209-213`), a wedge costs the entire run.

---

## What is verified safe (read this before "fixing" anything)

The lens asked about shared mutable state, session reuse, cross-thread dict
mutation, resource lifetime and reentrancy. Three of the five are correct, for
specific reasons a refactor should preserve.

**1. No record dict is ever touched by two threads.** `check_websites` snapshots
`(index, url)` at `enricher.py:231` and submits the **URL string**, not the
record, at `enricher.py:238`. Workers build and return a *separate* `contacts`
dict (`enricher.py:168`); every write into `records[idx]` happens in the
consuming thread inside the `as_completed` loop at `enricher.py:249-259`.
Measured over 400 tasks / 40 workers: 40 distinct worker threads, all distinct
from the main thread, and all 400 records distinct objects. **No lock is needed
here and adding one would be wrong.**

**2. Memory visibility is guaranteed, not assumed.** `future.result()` on a
completed future blocks on the future's condition variable, which establishes
happens-before for every write the worker made to its returned `contacts`. No
torn reads are possible under CPython's GIL and none are relied upon.

**3. Session reuse is fixed, and I verified the fix rather than trusting the
comment.** `httpclient.get_session()` is backed by `threading.local()`
(`httpclient.py:58, 61-71`). Instrumenting the `Session` constructor during a
real `check_websites(records, workers=40)` run over 400 targets produced
**exactly 40 `Session` objects for 40 worker threads**, one per thread, main
thread never among them. `docs/audits/004-enricher-thread-safety.md` is
**stale** — it describes a module-level `_SESSION` at `enricher.py:124-125` that
no longer exists, and its line numbers predate the rewrite. Do not action it as
written. (`005-enricher-http-robustness.md` is also stale, and its headline S1 is
now **backwards** — see the cross-reference at the end.)

---

## Findings

### S1 — 40-way parallelism converts our own rate limiting into DEAD sites, and DEAD is the best outcome in the product

- **Where:** `enricher.py:181-184` (`if status >= 400: return DEAD, contacts`),
  `enricher.py:250` (writes `website_live = False`),
  `enricher.py:303-304` (`elif record.get("website") and live is False: score += 20`),
  `pitch_recommender.py:57-58` (`if has_dead_website: return "Website rebuild + maintenance"`).
  Amplified by `enricher.py:231` (URLs not collapsed) and `enricher.py:237-238`
  (40 workers, every target submitted at once).
- **Breaks:** the module's own rule, stated at `enricher.py:151-153`, is that a
  site which "blocks our crawler is not a site that needs rebuilding" and that
  "this is NOT a pitch signal". **403 and 429 never reach that branch.** They
  satisfy `status >= 400` and become `DEAD`. Measured, end to end through the
  real `_fetch_website`, real `lead_score` and real `recommend_service` on an
  otherwise identical record (`category=cafe, rating=4.5, review_count=3,
  industry_priority=high, source=osm`):

  | HTTP status | outcome | `website_live` | `lead_score` | recommended pitch |
  |---|---|---|---|---|
  | 200 | `live` | `True` | **25** | SEO audit + visibility upgrade |
  | **403** | `dead` | `False` | **35** | **Website rebuild + maintenance** |
  | **429** | `dead` | `False` | **35** | **Website rebuild + maintenance** |
  | 500 | `dead` | `False` | 35 | Website rebuild + maintenance |

  A rate-limited site outscores a working site by 10 points and receives the
  single strongest pitch in the database. A sales rep opening that site on their
  phone finds it working, and the pitch dies on the first call. **The more we
  parallelise, the more healthy sites we relabel as rebuild prospects** — the
  error scales with the very concurrency the function exists to provide. 503
  ("temporarily unavailable") and Cloudflare's 520/521/522/523 edge codes are
  wrong for the same reason.
- **Trigger:** 300 records sharing one `website` value — a chain or franchise
  with one corporate site, a shared `linktr.ee`/`salla`/`facebook.com` page, or
  a common site-builder template across Lebanese/KSA SMBs. Measured against a
  local host serving 50 ms pages: **300 requests, peak 40 concurrent, 1,169
  req/s to one host**. Any host with a rate limiter (Cloudflare, cPanel,
  ModSecurity, Fail2Ban) answers 429 or 403, and all 300 records become
  `DEAD`. Even without duplicates, any single site that 403s a non-browser
  `User-Agent` (`httpclient.py:74-85`) is recorded as a rebuild prospect.
- **Fix:** route 401/403/405/429 and 5xx-that-means-unavailable (503, 520-524) to
  `UNKNOWN`; reserve `DEAD` for 404/410 and other 4xx that unambiguously mean
  the resource is gone. Then dedupe URLs and cap per-host concurrency (below).

### S1 — No wall-clock bound on a task: 40 tarpit responses wedge the pool permanently and the run produces nothing

- **Where:** `enricher.py:175` (`timeout=8`, a scalar),
  `enricher.py:188` (`raw = r.raw.read(_MAX_BODY_BYTES, decode_content=True)`),
  `enricher.py:193-197` (`finally: r.close()`),
  `enricher.py:240` (`for future in as_completed(futures)`),
  `enricher.py:237` (`with ThreadPoolExecutor(...)` → `shutdown(wait=True)`).
- **Breaks:** `timeout=8` is an **inactivity socket timeout, not a deadline**.
  Each `recv()` gets 8 s; a server that sends one byte every 2 s never trips it.
  `HTTPResponse.read(amt)` then blocks until it has `amt` bytes or hits EOF, so
  the single `read(200_000, ...)` at `enricher.py:188` can block indefinitely.
  Measured, single worker against a host dribbling 1 byte / 2 s: **still blocked
  after 25 s**, with `timeout=8` never firing. Measured, **all 40 workers** on
  tarpits: `check_websites` had **not returned after 45 s**, not one progress
  line printed (`enricher.py:261-262` only fires on a *completed* future), and the
  harness process **could not exit** — `ThreadPoolExecutor` registers an atexit
  hook that joins every worker, so even interpreter shutdown hangs on the
  wedged reads. Worst case for a single worker: `200,000 × 2 s ≈ 4.6 days`.
- **Trigger:** forty distinct hosts (or one host behind N paths) that stall
  rather than refuse — common for under-maintained Lebanese shared hosting and
  for any site fronted by a tarpit/WAF. Enough of them, and the pool stops
  making progress for good. The consequence is total, not partial: `enrich()` is
  called at `main.py:187` and the five CSVs are not written until
  `main.py:209-213`, so a wedge means the job is killed at
  `timeout-minutes: 300` (`.github/workflows/scrape.yml:25`) having written
  nothing. The `rclone` steps at `scrape.yml:56,88-89` never run, Drive keeps
  last week's CSVs, and the cumulative master is unchanged. A second Ctrl+C is
  also ineffective — see `docs/audits/200-fn-cli-py-main-concur.md` (S2, "Ctrl+C
  cannot stop a run"), which reaches the same `with` block.
- **Also:** because `r.close()` sits in the `finally` at `enricher.py:193-197`,
  it never runs while the read is blocked — the socket is neither returned to nor
  evicted from that worker's urllib3 pool, so a wedged worker also pins its
  socket for the remaining life of the thread.
- **Fix:** read in chunks against an explicit deadline and shrink the socket
  timeout to the remaining budget, so `socket.timeout` can actually fire:
  `deadline = time.monotonic() + budget`; loop `raw.read(min(8192, remaining))`
  with the per-recv timeout set to `remaining`; on timeout return `UNKNOWN`.
  A watchdog thread cannot fix this (it cannot interrupt a blocking read), so
  the chunked deadline is the actual fix.

### S2 — `_EMAIL_RE` cost scales super-linearly with the longest unbroken run, and it is the only GIL-bound part of the pipeline

- **Where:** `enricher.py:130` (`_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@...")`),
  `enricher.py:200` (`_EMAIL_RE.findall(html)`),
  `enricher.py:158` (`_MAX_BODY_BYTES = DEFAULT_MAX_BODY_BYTES` = `200_000`,
  from `httpclient.py:54`).
- **Breaks:** the unbounded `+` immediately before the `@` literal means that at
  every start position inside a long run of that character class the engine
  consumes to the end of the run and then fails. Measured on 200 KB bodies:

  | body shape (200 KB) | longest unbroken run | `_EMAIL_RE` |
  |---|---|---|
  | realistic HTML markup | 6 | 0.007 s |
  | CSS `@media`/`@font-face` | 9 | 0.008 s |
  | random base64 data URI | 685 | 0.058 s |
  | 200,000-char unbroken run | 200,000 | **86 s – 228 s** |

  (the last row measured three times as 86.2 s, 183.8 s, 227.8 s on a machine
  concurrently running the rest of this audit; the spread is machine load, not
  variance in the pattern.) **The 40 threads do not help — they cannot.** This is
  pure CPU under the GIL, so one pathological page roughly halves the throughput
  of the other 39 workers in flight and inflates every other record's latency.
  **Scope, stated honestly:** the driver is the *longest run*, not the page size
  — 32,000 chars costs 14 s while 200,000 costs ~90-230 s, and realistic pages
  cost microseconds. Reaching the extreme needs a 200 KB body that is largely
  free of punctuation: a hex dump, a punctuation-stripped base64 blob, or a
  binary asset mislabelled `text/html` and served as a homepage. That population
  is small — but it is *broken sites*, which is precisely who we pitch, so it is
  not hypothetical.
- **Fix:** bound the local part. RFC 5321 caps it at 64 octets, so
  `[a-zA-Z0-9._%+\-]{1,64}@` is both more correct and dramatically faster.
  Measured on the 200 KB-run body: **86.228 s → 0.051 s**, with byte-identical
  matches on a probe containing `info@sidiaboud.com`, `sales@acme-lb.com`,
  `.png@z.com` and `user_name+tag@sub.domain.co`. Adding
  `(?<![a-zA-Z0-9._%+\-])` as a lookbehind takes the pathological case to
  0.006 s and additionally stops matches beginning mid-token.

### S2 — N records with the same website produce N fetches; nothing collapses them, so all the concurrency is spent re-hitting one host

- **Where:** `enricher.py:231`
  (`targets = [(i, r["website"]) for i, r in enumerate(records) if r.get("website")]` —
  keyed by **index**, not by URL), `enricher.py:238` (one `submit` per element).
- **Breaks:** measured, 300 records → 1 distinct URL: **300 GETs, peak 40
  concurrent, 1,169 req/s to a single host, 0.26 s wall clock of which ~100 % was
  waste.** At 8 workers it is still 300 requests (135 req/s to that one host) —
  lowering the pool width does not fix it, only slows it. One fetch reused across
  all 300 records: **0.06 s**. This is simultaneously (a) the direct trigger of
  the S1 above, (b) wasted wall clock and egress, and (c) the cheapest fix in the
  function — a `dict` keyed by URL, then a second pass over `targets` to stamp
  every index that shares a URL.
- **Trigger:** chains and franchises with one corporate site, shared
  `linktr.ee`/`salla`/`facebook.com` pages, common site-builder templates, or
  imperfectly-deduplicated businesses. It compounds over time: `main.py:150`
  loads the entire cumulative master every run, so duplicate-URL density grows
  and this cost is re-paid on every historical row on every run.
- **Fix:** `results_by_url: dict[str, tuple[str, dict]]`, populate it from the
  futures, then apply it to each index in `targets`. Also cap in-flight
  requests per `urlparse(url).netloc` at 1-2 — that is the part `054-politeness-and-ssrf.md`
  asks for, and it is what stops a burst of *distinct* URLs on shared hosting
  (IDM/Cyperia/cPanel/Salla) from tripping Fail2Ban for the whole run.

### S2 — The only component running 40-wide is the only HTTP caller that bypasses the project's own retry/backoff layer

- **Where:** `enricher.py:175` calls `get_session().get(...)` directly.
  `httpclient.fetch_with_retry` (`httpclient.py:149-217`) exists for exactly this
  and *is* used by `osm.py:35-46` and `wikidata.py:70-78`;
  `google_places.py:234-247` even carries its own 429 backoff with jitter.
- **Breaks:** no retry, no exponential backoff and **no full jitter** on the part
  of the system holding the most concurrent sockets. The rationale is written in
  the file that does have it — `httpclient.py:198-203`: *"Full jitter: uniform in
  [0, backoff * 2^n]. Prevents N workers from retrying in lockstep and hammering
  the host in a thundering herd."* That protection is denied to the 40 workers
  who need it most. A single `ConnectionReset` or `ReadTimeout` at 40-way
  concurrency becomes a permanent verdict for that run, and because there is no
  cache the same loss recurs every run (see `053-caching-layer.md`).
- **Trigger:** any one transient reset while 40 sockets are open. The site is
  written `UNKNOWN` (or `DEAD` if the transient failure surfaced as a 5xx), its
  homepage contacts are lost, and nothing retries.
- **Fix:** call `fetch_with_retry("GET", url, retries=2, max_bytes=DEFAULT_MAX_BODY_BYTES)`
  and map its typed `FetchResult` — which already separates "server said no"
  from "never reached the server" (`httpclient.py:96-108`) — onto the
  LIVE/DEAD/UNKNOWN triple. This fixes the S1 status mapping and this finding in
  one change, and removes the third HTTP policy in the codebase.

### S3 — Progress reporting is completion-triggered, so a wedged pool is indistinguishable from a slow one

- **Where:** `enricher.py:261-262` (`if done % 200 == 0`), `enricher.py:240`.
- **Breaks:** the only liveness signal during a multi-hour enrichment phase is
  emitted from inside the `as_completed` loop. Measured: with all 40 workers
  wedged, **zero progress lines in 45 s**. An operator watching CI sees
  `Fetching 30,000 websites — liveness + contacts (40 workers)...` and then
  nothing for up to 300 minutes — no way to tell a stuck run from a slow one, and
  no hint that the remedy is to kill it (which per
  `docs/audits/200-fn-cli-py-main-concur.md` takes about an hour).
- **Fix:** make the heartbeat time-based rather than count-based — print
  `done/total` plus the in-flight count every 60 s from a monotonic clock. A flat
  `done` with a rising in-flight count is exactly the wedged signature, and it
  also gives the operator the lever the `--workers` flag would give them.

### S3 — `_fetch_website`'s thread-safety is an invisible property of the caller's thread

- **Where:** `httpclient.py:58, 61-71` (`threading.local()`),
  `enricher.py:175` (the only `get_session()` call in the module),
  `enricher.py:161` (`def _fetch_website(url: str)`).
- **Breaks:** `_fetch_website(url)` is correct *only* because it happens to run on
  a `ThreadPoolExecutor` worker, and `get_session()` keys on `threading.local()`.
  Verified working today (40 sessions / 40 threads). But the call site gives no
  hint of the dependency: it takes a URL and nothing else. If it were ever called
  from `asyncio`, from a `ProcessPoolExecutor` (no thread-local at all → a fresh
  `Session` per task, zero reuse), or nested inside another task already running
  on that worker thread, the guarantee silently changes shape and **nothing fails
  loudly**.
- **Fix:** change the signature to `_fetch_website(session, url)` and create the
  session once per worker. That makes the invariant explicit, testable and
  reviewable at the call site, and costs nothing.

### S3 — The three pools never overlap, and that is the only reason the thread-local design is currently sound — but nothing enforces it

- **Where:** `main.py:160-169` (the 3-worker pool closes before
  `records = enrich(records)` at `main.py:187`), `main.py:158`
  (`list(scraper.scrape())` fully drains the generator *inside* the outer pool),
  `google_places.py:194` (the 5-worker pool lives inside `scrape()`),
  `enricher.py:237` (the 40-worker pool lives inside `check_websites`).
- **Verified by reading the control flow:** the 3 scraper threads, the 5 Places
  threads and the 40 enrichment threads are strictly **sequential**, so they get
  disjoint `threading.local` sessions and there is **zero** cross-pool session or
  cookie sharing today. Worth stating because the shape of the code invites the
  opposite assumption. Two edits would break this silently, and neither is
  currently present: moving `enrich()` inside the `main.py:160` pool, or moving
  `GooglePlacesScraper` onto `get_session()` — its `_make_session`
  (`google_places.py:159-166`) puts `X-Goog-Api-Key` into the session headers, and
  sharing that into the thread-local sessions would put the Places API key into
  every session the enricher uses.
- **Fix:** none needed now. Add a one-line comment at `enricher.py:237` recording
  that the pool must not be nested inside another pool, so the invariant
  survives.

---

## Not a bug, but worth knowing

- **Reentrancy is fine as written.** Two calls to `check_websites` produce two
  pools and two disjoint sets of threads, hence disjoint thread-local sessions.
  A nested call from inside a worker is also safe: child threads do not inherit
  thread-locals, so each would build its own `Session`. The only process-global
  state touched is `urllib3.disable_warnings(...)` at `enricher.py:328`, which is
  idempotent and therefore reentrancy-safe (and is now pointless, since TLS
  verification was restored — see `docs/audits/200-fn-cli-py-main-concur.md`).
- **In-place mutation does not corrupt the loaded master.** `check_websites`
  returns and mutates the *same* list (verified: `out is records` → `True`), which
  looks alarming next to `main.py:179-187`. It is safe because `dedup` never
  hands back a caller's dict — it copies on insert (`dedup.py:210`, `dedup.py:218`)
  and `_merge` builds a fresh dict (`dedup.py:191-194`) — so `records` shares no
  object with `master`.
- **Sessions are never explicitly closed.** `get_session()` creates one `Session`
  per worker and nothing calls `.close()`; under CPython refcounting they are
  collected when the worker's `threading.local` entry dies at pool shutdown. That
  is implicit, not guaranteed — and with the S1 wedge, `r.close()` never runs
  either. Cross-reference: `docs/audits/200-fn-cli-py-main-concur.md` ("Sessions
  are never closed"). An explicit `atexit`/`finally` close is cheap insurance.
- **Eager submission allocates one `Future` per target before any task runs.**
  Measured with `tracemalloc`: **86.3 MB at 50,000 targets, 344.9 MB at 200,000**,
  resident before task 1 is dispatched by the dict comprehension at
  `enricher.py:238`. On a runner already holding a cumulative master that is a
  real, bounded chunk. `ThreadPoolExecutor.map` over a chunked iterator, or a
  bounded in-flight window, fixes this and improves cancellability at the same
  time — this is the same fix `docs/audits/200-fn-cli-py-main-concur.md` proposes
  as its second-best option.
- **Partial body reads forfeit connection reuse.** `enricher.py:188` caps the read
  at 200 KB and `enricher.py:195` closes the response, so for any larger page
  urllib3 closes the socket instead of returning it to the pool. Not a
  correctness issue, but it multiplies handshake load on exactly the hosts most
  likely to rate-limit — which feeds the S1 above.
- **`workers=40` is declared and unreachable.** No caller passes it
  (`enricher.py:337` uses the default) and neither `main.py` nor the `run`
  subparser (`cli.py:285-286`) exposes it. An operator watching healthy sites get
  relabelled as rebuild prospects has no lever but killing the process, which per
  `docs/audits/200-fn-cli-py-main-concur.md` takes about an hour.
  (`388-fn-enricher-py-check_websites-contract.md` S3 covers the same gap from the
  contract angle; the `--workers` plumbing is the shared fix.)
- **This file is a moving target, and one transient state broke the module
  outright.** I audited `99493b9`. While I was reading, another agent added
  `from scrapers.base import BusinessRecord` at `enricher.py:3`, which shifted
  every line below by one *and* created a circular import —
  `enricher` → `scrapers/__init__.py` → `google_places.py:26`
  (`from enricher import infer_region`) → partial-module failure — so
  `import enricher` raised `ImportError` outright. The edit was reverted while I
  was testing. Two consequences: (a) all line anchors above are **HEAD** line
  numbers, matching the assigned `enricher.py:225`; add 1 if that module-scope
  import lands. (b) If it does land, it must not import `scrapers.base` from
  module scope — move `BusinessRecord` out of the package, or import it under
  `TYPE_CHECKING`.
- **Cross-references / staleness.** `004-enricher-thread-safety.md` describes a
  shared `_SESSION` that no longer exists — superseded by the "verified safe"
  section above. `005-enricher-http-robustness.md` is **backwards** on the
  central point: it argues (S1, lines 143-164) that 4xx and 429 yield
  `website_live = True`. Since the rewrite at `enricher.py:181-184` they yield
  `DEAD`. The direction of the error has flipped, and the new direction is worse
  (DEAD is the +20 / Tier-1 outcome). Fix S1 above; do not implement 005's
  `live = 200 <= r.status_code < 400` suggestion, which would re-break it by
  calling 403/429 "live". `200-fn-cli-py-main-concur.md` already covers
  Ctrl+C, the fixed temp filename, worker tunability and output ordering.

## Recommended order of work

1. **Status mapping** (`enricher.py:181-184`) — send 401/403/405/429/503 and
   Cloudflare 520-524 to `UNKNOWN`; keep `DEAD` for 404/410 and friends. This is
   the finding that writes wrong values into the product. One `if`.
2. **Collapse duplicate URLs** (`enricher.py:231, 238, 249-259`) — fetch each
   distinct URL once, apply the result to every index that shares it, and cap
   in-flight requests per host. Removes the S1 trigger, 300× the wasted egress
   in the measured case, and satisfies `054-politeness-and-ssrf.md`.
3. **Real read deadline** (`enricher.py:175, 188`) — chunked read against a
   monotonic deadline with the socket timeout shrunk to the remaining budget.
   Removes the wedge, the unreachable `finally`, and the pinned sockets.
4. **Bound `_EMAIL_RE`** (`enricher.py:130`) — `{1,64}` on the local part
   (RFC 5321), optionally with a lookbehind. 86.2 s → 0.051 s on the pathological
   body, identical matches.
5. **Route through `fetch_with_retry`** (`enricher.py:175`) — retries, jitter and
   a typed result that already encodes "server said no" vs "never reached it".
   Removes the third HTTP policy and retires the S1 fix into one place.
6. **Time-based progress heartbeat** (`enricher.py:261-262`) — plus
   `--workers` through `cli.py:33-37` and `cli.py:285-286`, which together give
   the operator a way to see and respond to a wedge.
7. **Bounded submission window** (`enricher.py:238`) — caps the 345 MB of
   `Future` objects at 200k targets and improves cancellation.
8. **Thread `_fetch_website`'s session explicitly** (`enricher.py:161, 175`) —
   make the thread-local dependency visible at the call site.
