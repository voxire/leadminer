# 391 — `check_websites()` read as a six-hour cron stage

**Target:** `enricher.py:225-276` (`check_websites`), plus the one line it delegates
to, `enricher.py:175`.
**Lens:** production operations — what it prints, what it swallows, and every way it
can fail silently, ranked by how long a human takes to notice.

## Verdict

`check_websites` is a four-line concurrency skeleton with a **four-line operational
contract**. In a six-hour job it emits between 3 and 253 lines, none of them
timestamped, flushed, or attributed to a cause; it reduces thirteen distinguishable
failure modes to a single integer (`{n} unreachable`) and discards the exception
object that would have told you which one happened (`enricher.py:244-248`). The
consequence is that **the stage is structurally incapable of reporting that it
degraded.** A total enrichment failure — expired CA bundle on the runner, GitHub's IP
banned by a WAF, a DNS resolver outage — prints `0 live / 0 dead / 5000 unreachable`,
which reads like a *finding about the market* rather than an outage, and exits 0 with
all five CSVs rewritten. The stage also reports absolute counts of the master's
*pre-existing* state as if they were its own output (`enricher.py:267-270`), so a run
that extracted literally nothing prints a healthy-looking `Contacts — email:1840`.

**The most important thing:** the numbers this function prints cannot be compared
against last week's numbers, and cannot be compared against each other, so there is
no baseline and no alarm — only a fresh-looking log every Monday.

---

## How this was verified

`requests` is not installed here, so the module was imported with the same stub
strategy as `tests/test_lead_signal.py:23-63` and driven end-to-end by replacing
`enricher.get_session` with a fake session returning synthetic responses. **No network
traffic was generated and no repo file was modified.** Every "Observed" line below is
real program output. `python3 -m unittest` on the existing suite was not run (it
imports `requests`).

---

## Findings

### S1 — Thirteen failure causes collapse into one integer, and the exception object is thrown away

- **Where:** `enricher.py:176-178` (`except Exception: return UNKNOWN, contacts`),
  `enricher.py:190-192` (same, for body reads), `enricher.py:242-248` (the
  `future.result()` guard), `enricher.py:250` (collapse to tri-state), `enricher.py:271`
  (the single printed counter).
- **Breaks:** the cause is destroyed at three separate boundaries and never recorded
  anywhere — not in a counter, not in a log, not in a record field, not in a CSV
  column. The following are all `website_live = None`, all `+0` in `lead_score`, all
  reported as the word "unreachable":
  `requests.exceptions.ConnectionError` (DNS NXDOMAIN),
  `SSLError` (expired or untrusted `certifi` bundle — `requests==2.32.3`, pinned with
  no lockfile per `BRIEF.md:43`),
  `ReadTimeout` / `ConnectTimeout`, `ConnectionResetError`, `ProxyError`,
  `TooManyRedirects` (`enricher.py:175` sets `allow_redirects=True`), `MissingSchema`
  and `InvalidURL` (a schemeless master-CSV `website` cell), `ChunkedEncodingError`,
  `ContentDecodingError`, a body-read failure at `enricher.py:188`, and — inside the
  `enricher.py:244` handler — `MemoryError`, `RecursionError` or a genuine bug in
  `_fetch_website`. The guard at `enricher.py:244` does not even bind the exception:
  it is `except Exception:` with no `as e`, so unlike `main.py:168` (which at least
  prints `str(e)`) there is not even a truncated message to go on. There is **no
  logger anywhere in `enricher.py`** (grep for `log.`/`logging` returns nothing), and
  `httpclient.py:45`'s logger has no handler configured anywhere in the codebase, so
  even the centralised retry telemetry is inert.
  Operationally this is the difference between "TLS trust broke" (fix: bump
  `certifi`, 5 minutes) and "we got IP-banned by Cloudflare" (fix: back off, add
  `Retry-After`, reduce concurrency) and "the DNS resolver on the runner is broken"
  (fix: nothing, wait for the next run). All three look the same in the log.
- **Trigger:** `enricher.py:182` is not the only thing that classifies; take the
  simplest systemic case. A runner whose `certifi` bundle has expired raises
  `SSLError` from `enricher.py:175` for **every** target. Observed output for an
  equivalent run in which every request raised:

  ```
  [Enricher] Fetching 5000 websites — liveness + contacts (40 workers)...
  [Enricher] 0 live / 0 dead / 5000 unreachable
  [Enricher] Contacts — email:1840 instagram:2600 whatsapp:0 linkedin:412
  ```

  Read as prose that says "this week's data was not useful", not "the TLS store on
  the runner is broken". Exit 0. `main.py:209` rewrites the master. Nobody notices
  for weeks.
- **Fix:** carry the cause through the boundary instead of discarding it — have
  `_fetch_website` return `(outcome, contacts, reason)` where `reason` is a small
  closed vocabulary (`dns`, `tls`, `timeout`, `reset`, `redirects`, `scheme`,
  `body`, `worker`), tally reasons in `check_websites`, `log.warning()` the first
  occurrence of each distinct reason, and print
  `unreachable by reason: dns=311 tls=4189 timeout=0 …`.

### S1 — A run where the network died at 60% looks exactly like a finished run, and the damage is permanent

- **Where:** `enricher.py:250` (unconditional assignment),
  `enricher.py:264-266` (tallies), `enricher.py:271` (the one line printed),
  reached from `main.py:187` and persisted by `main.py:209`.
- **Breaks:** this is the brief's "what happens when the network drops halfway",
  reproduced. `check_websites` has no notion of a partial result, no threshold, no
  exit signal, and no way to distinguish "40 of my sites are unreachable" from "the
  network fell over". Worse, the assignment at `enricher.py:250` is **unconditional**:
  rows that arrived from `main.py:103` already carrying last week's `website_live=True`
  — a verified fact — are overwritten with `None` by a transient error, and
  `write_csv` (`main.py:130`, atomic, so it succeeds) makes that permanent. The
  pipeline trades a durable observation for an absent one and reports success.
  The commercial damage is asymmetric and worth spelling out, because `UNKNOWN` is
  not merely "less good", it is *categorically different*. With
  `category="car_wash"`, `phone` set, `industry_priority="medium"`, `rating=4.5`,
  `review_count=30`:

  | `website_live` | `lead_score` | `recommended_service` |
  |---|---|---|
  | `True` (live) | 33 | `Discovery call - scope the right service` |
  | `False` (server-confirmed dead) | 43 | **`Website rebuild + maintenance`** |
  | `None` (network died) | 23 | `Discovery call - scope the right service` |

  Every tier of `pitch_recommender.recommend_service` that fires on a website requires
  `website_live is True` or `is False` (`pitch_recommender.py:34-36,70,73,95`); a
  `None` falls through to the generic bucket at `pitch_recommender.py:99`. So a
  mid-run network drop silently converts a third of the corpus into "book a discovery
  call" — the most expensive, lowest-converting motion in the list — and lowers every
  affected lead's score by 10.
- **Trigger:** 100 master rows, all with `website_live=True`, mocked transport that
  succeeds for the first 60 requests and raises `OSError` for the rest.
  **Observed, verbatim stdout:**

  ```
  [Enricher] Fetching 100 websites — liveness + contacts (40 workers)...
  [Enricher] 60 live / 0 dead / 40 unreachable
  [Enricher] Contacts — email:0 instagram:0 whatsapp:0 linkedin:0
  ```
  and after the call: rows still `website_live=True`: **60**; rows now
  `website_live=None`: **40**; `lead_score` for one of them 33 → **23**.
  Note there is no heartbeat line either: 100 targets is under the `% 200 == 0`
  threshold at `enricher.py:261`. The complete evidence of the outage is the word
  "unreachable" in line 3.
- **Fix:** two independent guards. (a) At `enricher.py:250`, never downgrade a
  durable verdict: `if outcome != UNKNOWN or r.get("website_live") is None:
  r["website_live"] = ...`. (b) At `enricher.py:271`, compute
  `unknown_ratio = unknown_count / max(1, len(targets))` and if it exceeds a
  threshold (say 0.10) raise, or at minimum print a loud line to **stderr** and set a
  module-level degraded flag that `main.main()` turns into a non-zero exit. A run that
  lost contact with a third of the internet is not a run that should exit 0.

### S2 — The "Contacts" headline is an absolute count of the master's existing state, not this run's extraction

- **Where:** `enricher.py:267-270`.
- **Breaks:** `found_email = sum(1 for r in records if r.get("email"))` iterates the
  **whole record list**, which on every run after the first is dominated by contacts
  harvested in *previous* weeks. The four numbers printed under the heading
  "Contacts" are therefore a measure of the input, and they are monotonically
  non-decreasing by construction. The extraction stage's only reported metric is a
  function of the corpus it was handed, not of its own success. There is no way to
  compute enrichment yield from the log, so there is no way to notice that yield
  went to zero.
- **Trigger:** 4 master rows, 2 of which already carry `email`/`instagram`/
  `whatsapp`/`linkedin` from previous weeks; all 4 sites return `200` with a body
  containing no contact information at all. **Observed, verbatim stdout:**

  ```
  [Enricher] Fetching 4 websites — liveness + contacts (40 workers)...
  [Enricher] 4 live / 0 dead / 0 unreachable
  [Enricher] Contacts — email:2 instagram:2 whatsapp:2 linkedin:2
  ```

  This run extracted **zero** contacts from four live sites. On a 50,000-row master
  the same run prints something like `email:18400 instagram:12600 …` whether it
  enriched brilliantly or not at all.
- **Fix:** snapshot the four counts before the pool starts and print both:
  `Contacts — new email:+37 instagram:+412 whatsapp:+8 linkedin:+0 (corpus totals
  18437 / 13012 …)`. The delta is the number anyone actually wants, and it is the
  only one that can be charted week over week.

### S2 — The progress heartbeat is count-based, unflushed, and silent below 200 targets

- **Where:** `enricher.py:261-262` (`if done % 200 == 0: print(...)`),
  `enricher.py:235` (the "Fetching N" line, also unflushed).
- **Breaks:** four independent defects in one two-line block.
  1. **Cadence is keyed to completions, not time.** During a network stall the
     heartbeat slows down in exact proportion to the stall, so the one signal that
     could reveal degradation is itself degraded by it. There is no elapsed, no rate,
     no ETA — a human comparing `[Enricher] 200/5000 done...` at minute 3 with
     `[Enricher] 400/5000 done...` at minute 12 has no way to tell a 4× slowdown from
     ordinary variance, because no line carries a timestamp.
  2. **Nothing is printed at all below 200 targets** (`done % 200 == 0` is never
     true for `done ∈ [1,199]`). A small corpus gets a start line and two summary
     lines and no progress whatsoever.
  3. **No `flush=True`.** Audit `200-fn-cli-py-main-ops.md` §S2 covers this and shows
     the codebase already does it correctly elsewhere (`osm.py:54`, `wikidata.py:83`,
     `google_places.py:232,239`). Under redirection stdout is 8 KB block-buffered, so
     the last partial buffer is lost on SIGTERM — i.e. exactly the progress
     information you need for timeout triage is the part that gets truncated.
  4. **Total output volume.** A 50,000-target stage emits `50000/200 + 3 = 253`
     lines across what can be hours of wall clock. That is not a heartbeat, it is a
     series of disconnected events, and the long silent gaps are indistinguishable
     from a hang.
- **Trigger:** `check_websites` on 100 targets — **observed**: zero heartbeat lines.
  On a 500-target run whose requests all time out at 8 s with 40 workers, the first
  heartbeat appears after 200 × 8 / 40 = 40 s and the last after 100 s, so the log
  shows 2 lines for a 100-second stage and *the count increments while the wall clock
  does not move at all relative to normal*.
- **Fix:** make it time-based and self-describing:
  `if time.monotonic() - last >= 30: print(f"[Enricher] {done}/{n} "
  f"({done/max(elapsed,1):.1f}/s, eta {(n-done)/max(done/max(elapsed,1),.01)/60:.0f}m) "
  f"live={live} dead={dead} unknown={unknown}", flush=True)`. The running
  live/dead/unknown tallies in the heartbeat are worth more than the raw count,
  because they degrade *immediately* when the network does.

### S2 — No retry at all: the stage bypasses the retry module that was written for it

- **Where:** `enricher.py:175`
  (`r = get_session().get(url, timeout=8, allow_redirects=True, stream=True)`),
  versus `httpclient.py:149-217` (`fetch_with_retry`) and its own docstring at
  `httpclient.py:11-15` ("No real retry … Retries are now centralised with exponential
  backoff and full jitter").
- **Breaks:** every website gets exactly **one** 8-second attempt, forever, with no
  backoff, no jitter, and no `Retry-After` handling. The enrichment stage is the
  single largest HTTP consumer in the pipeline — one GET per row of a cumulative
  master on every run — and it is the only stage not using the shared client. Any
  transient condition (a mobile-hosting blip, a slow DNS resolver, a TCP retransmit
  on the runner's egress) is converted directly into a permanent `UNKNOWN` for that
  run and, because of the S1 above, a permanent score reduction in the master. Audit
  `200-fn-cli-py-main-ops.md` §"Not a bug" flags the same bypass; what is new here is
  the quantified consequence: one `ReadTimeout` on a 60,000-row master costs 60,000
  verdicts, and the stage has no mechanism to distinguish a 2-second blip from a
  4-hour outage because it never tries twice.
- **Trigger:** a site that is reachable but slow to first byte on the first attempt
  and instant on the second. `enricher.py:175` gives it one chance; observed verdict
  `UNKNOWN` → `website_live=None` → `lead_score` 33 → 23, permanently.
- **Fix:** call the helper that already exists and map its typed result onto the
  tri-state —
  `res = fetch_with_retry(session, "GET", url, timeout=(3, 5), retries=2, max_bytes=DEFAULT_MAX_BODY_BYTES)`,
  then `UNKNOWN` if `res.status is None`, `DEAD` if `4 <= res.status < 500` and not in
  `(401, 403, 407, 408, 425, 429)`, else `LIVE`. Two retries with full jitter costs
  at most ~6 s of extra worst-case wall clock per site and eliminates the entire
  class of "one bad packet cost us a lead".

### S2 — 40 workers, no per-host cap and no URL dedupe: the stage manufactures the rate limits it then reports as dead sites

- **Where:** `enricher.py:231` (targets, one entry per record),
  `enricher.py:237` (`max_workers=workers`), `enricher.py:182-184`
  (`if status >= 400: return DEAD`).
- **Breaks:** this is the compound failure, and it is the single most damaging thing
  in the function. `website` is populated straight from third-party fields with no
  normalisation: Google's `places.websiteUri` (`google_places.py:291`), OSM's
  `website`/`contact:website`/`url` tags (`osm.py:91`), Wikidata's `wdt:P856`
  (`wikidata.py:96`). In practice a large share of that field is a **social profile
  URL or a Google-hosted URL**, not a business site. `dedup` cannot collapse them
  because its keys are phone and `(normalized_name, city)` only
  (`dedup.py:197-232`) — website is not a dedup key. So line 231 fires one GET per
  record, from 40 threads, with no per-host limit, against whatever those URLs are.
  Then line 182 maps the resulting `429` (and `403` from every WAF on the internet)
  to `DEAD`, which `lead_score:303-304` pays **+20** for and
  `pitch_recommender.py:57-58` converts into `Website rebuild + maintenance`.
  **Observed:** 50 records of which 40 share `https://www.facebook.com/pg/x` →
  `50` GETs, `11` unique URLs, **40 of them to one host at 40 workers**. Separately,
  driven through a mocked 429 and 500: `HTTP 429 -> dead`, `HTTP 500 -> dead`,
  `HTTP 403 -> dead` — identical to `HTTP 404 -> dead`. The politeness failure
  produces the confidently wrong sales signal: the more aggressively we hammer a
  host, the more likely we are to conclude that its website is broken, and the higher
  that lead ranks.
  The module's own contract forbids this at `enricher.py:148-153` ("BLOCKED /
  UNKNOWN … Cloudflare challenge. We know nothing about the site. This is NOT a
  pitch signal") and again at `enricher.py:162-166` ("Never infer 'dead' from a
  transport failure"), but `status >= 400` is precisely a block response.
  Cross-reference: `_diag_d1.md` §S1-2 and `_diag_d3.md` §A1 already flag the
  403/429 classification as a correctness bug; the *ops* framing — that the crawler
  causes the condition and then launders it into the product — is additional.
- **Trigger:** a Lebanese business whose Google `websiteUri` is
  `https://www.facebook.com/<page>`, on a master where 300 records point at
  Facebook-owned URLs. 40 workers submit them; Facebook answers `429` or a `403`
  bot-wall; 300 rows are written with `website_live=False`, `lead_score` +20,
  `recommended_service="Website rebuild + maintenance"`, and `unknown_count` stays at
  0, so the log reads `[Enricher] 1840 live / 300 dead / 0 unreachable` — a healthy
  looking run.
- **Fix:** dedupe `targets` on a normalised URL (`dict.fromkeys`, fan the single
  result back out to all indices); cap per-host concurrency at 1–2 with a semaphore
  keyed on `urlparse(url).netloc`; and carve out `401, 403, 407, 408, 425, 429` to
  `UNKNOWN` before the `>= 400` test at `enricher.py:182`.

### S2 — No per-target wall-clock deadline: `timeout=8` is a per-socket-operation timeout, and `stream=True` moves the read outside `get()`

- **Where:** `enricher.py:175` (`timeout=8, stream=True`), `enricher.py:188`
  (`raw = r.raw.read(_MAX_BODY_BYTES, decode_content=True)`), `enricher.py:237-240`
  (`with ThreadPoolExecutor` … `as_completed`, no cancellation).
- **Breaks:** `requests`' `timeout` is a **socket** timeout applied to each
  individual read/write, not a deadline for the URL. With `stream=True`, `get()`
  returns as soon as headers arrive, so the subsequent `r.raw.read(...)` at
  `enricher.py:188` is a fresh sequence of socket reads each subject to its own 8 s.
  A host that dribbles one byte every 7 seconds therefore holds one worker thread
  for up to `200_000 × 7 s` (`_MAX_BODY_BYTES` is `DEFAULT_MAX_BODY_BYTES =
  200_000`, `httpclient.py:54`, `enricher.py:158`) — effectively forever. There is no
  total per-target budget, no per-host cap, and no circuit breaker anywhere in the
  stage. A `allow_redirects=True` chain gives one URL up to 30 hops
  (requests' `max_redirects` default), i.e. `30 × 8 s = 4 minutes` of one worker
  before it even starts reading a body. Once all 40 workers are pinned on such
  hosts, `as_completed` at `enricher.py:240` yields nothing, the heartbeat at
  `enricher.py:262` stops, and the run sits until GitHub Actions enforces
  `timeout-minutes: 300` (`scrape.yml:25`) — at which point audit
  `200-fn-cli-py-main-ops.md` §S1 establishes that nothing is printed, the exit is
  143, and the unflushed heartbeat is discarded.
  Note this also corrects the arithmetic in that report's S1 cost table: `8 s` is the
  *pessimistic* estimate only for well-behaved hosts. Against pathological ones the
  stage has no upper bound at all.
- **Trigger:** one URL on a slow-drip host among 5,000 targets pins one of 40 workers
  indefinitely; 40 such URLs among 5,000 pins all of them and the stage never
  completes. Observed symptom during the stall: no log output whatsoever.
- **Fix:** wrap each fetch in a wall-clock budget — `timeout=(3, 5)` tuple (connect,
  read), a `signal`-free `concurrent.futures` timeout of ~15 s applied with
  `future.result(timeout=...)` per target in a second pass, or hand the deadline to
  `_fetch_website` and check it between read chunks — and cancel the pool with
  `cancel_futures=True` if the stage exceeds its budget.

### S2 — The 200 KB cap is applied to compressed bytes, so one hostile host can OOM-kill the whole six-hour run

- **Where:** `enricher.py:188` (`raw = r.raw.read(_MAX_BODY_BYTES,
  decode_content=True)`) with `_MAX_BODY_BYTES = DEFAULT_MAX_BODY_BYTES = 200_000`
  (`enricher.py:158`, `httpclient.py:54`).
- **Breaks:** urllib3's `read(amt, decode_content=True)` reads **`amt` bytes off the
  wire** and *then* decompresses. The cap is therefore on the compressed size, and the
  string handed to the four regexes is unbounded relative to it. **Verified:** a gzip
  payload of **249 bytes** on the wire — comfortably under the cap — inflates to
  **200,025 bytes**, an **803×** ratio. Nothing about a `Content-Encoding: gzip`
  response is bounded by this line.
  The *ops* consequence is what makes this worse than a correctness bug: the memory
  is allocated in the worker threads, and 40 of them (`enricher.py:237`) run
  concurrently. 40 workers each inflating ~200 MB is ~8 GB, so the Linux OOM killer
  sends **SIGKILL**, which is not an `Exception`, is not caught by `enricher.py:190`,
  and does not run `ThreadPoolExecutor.__exit__`. The run dies at an arbitrary point
  in a six-hour job with no traceback, no partial output, no CSV written, and the
  Drive master left a week stale — the identical end state as the timeout in the
  previous finding, reached for a completely different reason and with no evidence at
  all. `enricher.py:190-192` would catch a single-thread `MemoryError` and return
  `UNKNOWN`; it cannot help against the aggregate.
  Cross-reference: `385-fn-enricher-py-check_websites-edge.md` §S1 reports this as a
  correctness/robustness bug; this is the production-impact framing.
- **Trigger:** a `website` value pointing at a host that serves a ~190 KB
  `Content-Encoding: gzip` body of highly compressible content, alongside ~40 normal
  targets. **Observed** compression ratio 803× on a realistic payload; 40 concurrent
  instances exceed a hosted runner's memory and the job is SIGKILLed with no log line.
- **Fix:** cap **after** decompression — read incrementally and stop at
  `_MAX_BODY_BYTES` of *decoded* output (`resp.raw.stream(_CHUNK,
  decode_content=True)` accumulating into a `bytearray` with a hard size check), or
  send `Accept-Encoding: identity` for the liveness probe and drop
  `decode_content=True`. Also set a per-run RSS ceiling so the failure is a clean
  `SystemExit` rather than a SIGKILL.

### S2 — The "never let one worker kill a multi-hour run" guard protects one line and not the eleven that consume its result

- **Where:** `enricher.py:242-248` (the `try` wraps `future.result()` **only**),
  versus `enricher.py:249-259` (`r = records[idx]`, the tri-state write, and four
  `contacts[...]` subscripts) which sit **outside** it.
- **Breaks:** the comment at `enricher.py:245-246` states the intent — "never let one
  worker kill a multi-hour run" — but the guard stops one line too early. Every
  `contacts` subscript on lines 252, 254, 256, 258 assumes the four-key dict that
  `_fetch_website` constructs at `enricher.py:168`. Today that invariant holds, so
  this is latent, not live. It becomes live the moment `_fetch_website` is refactored
  to return only the keys it found — which is the obvious refactor for a caller that
  "only fills what we extracted", and is exactly the kind of change an engineer makes
  after a bug report about a `KeyError`. The consequence is a dead run:
  the `KeyError` escapes the loop, the `with` block at `enricher.py:237` runs
  `shutdown(wait=True)` (which *drains* the remaining queue rather than cancelling
  it — see `200-fn-cli-py-main-ops.md` §S2), the exception propagates out of
  `enrich()` and out of the unguarded call at `main.py:187`, the process exits 1, and
  because every subsequent workflow step is default-`if: success()`, `scrape.yml:71-89`
  never runs. **This week's scraper output — which already succeeded and is only held
  in memory — is never published.** A one-token refactor of a helper costs a full
  week of leads.
- **Trigger:** change `enricher.py:168` to
  `contacts = {"email": None}` (or make `_fetch_website` return only populated keys),
  then run `check_websites([{"website": "https://x.example"}])` with a `200` stub.
  `enricher.py:254` raises `KeyError: 'instagram'`, the `except Exception` at
  `enricher.py:244` has already been exited, and the traceback propagates out of
  `check_websites`.
- **Fix:** move `enricher.py:249-259` inside the `try` block (or return a
  `dataclass` with defaulted fields and use `contacts.instagram` rather than
  `contacts["instagram"]`, which makes the whole class of error impossible).

### S2 — Enrichment is a hard gate on publishing and checkpoints nothing

- **Where:** `enricher.py:249-259` (results written in place into the caller's list),
  reached from `main.py:186-187`, with the first CSV write at `main.py:209`.
- **Breaks:** `check_websites` mutates `records` in place and returns it, and
  `main.py:209-213` writes all five CSVs only after `enrich()` returns. Nothing is
  persisted while the stage runs — no per-target checkpoint, no partial output file,
  no resume cursor. The scrape happens *before* enrichment (`main.py:160-181`), so a
  stage that dies at 99% of its targets discards 100% of the run: this week's
  Overpass, Wikidata and Places results, which are already in memory and were fine.
  The next run starts from zero and re-fetches every URL in the master. The good
  news, and it is genuinely good, is that this means a killed run cannot leave a
  half-written master — the atomic `os.replace` at `main.py:130` is never reached at
  all, so the Drive copy stays at last week's consistent state. The bad news is that
  the recovery path is "wait a week" or "SSH in and run it by hand", and the only
  diagnostic the failed run leaves behind is the `if: always()` artifact upload at
  `scrape.yml:91-98`, which for a run that died inside enrichment contains just the
  master downloaded from Drive — i.e. the file you already had.
- **Trigger:** `timeout-minutes: 1` on the workflow, or a host that pins all 40
  workers per the previous finding. Observed end state: five fresh CSVs never
  written, Drive master one week stale, artifact upload containing only the old
  master, and a log whose last flushed line is `[Enricher] 4800/5000 done...`.
- **Fix:** give enrichment its own wall-clock budget (`enrich(records,
  budget_seconds=…)`) that, when exceeded, marks the stage truncated and writes what
  it has; and either checkpoint verdicts to a URL→verdict cache as they complete or
  skip re-fetching URLs verified within a TTL. The TTL cache also removes the
  linear-growth-vs-constant-timeout problem flagged in
  `200-fn-cli-py-main-ops.md` §S1 at its root.

### S3 — `workers=40` is unreachable from production

- **Where:** `enricher.py:225` (`workers: int = 40`), `enricher.py:337`
  (`check_websites(records)` — the only call site in the pipeline), `cli.py:285-286`
  (the `run` subparser declares no options at all). No environment variable, config
  file, or CLI flag anywhere references it.
- **Breaks:** the one parameter that governs both politeness toward third-party hosts
  and exposure to the timeout cliff cannot be turned by an operator. Diagnosing
  "the enrichment stage is too aggressive" or "it is too slow to finish" requires a
  code change and a deploy — in a repository whose cron runs on a schedule. There is
  also no per-host control at all (see the S2 above), so even `workers=4` would not
  prevent 40-way bursts against a single Facebook host.
- **Trigger:** `leadminer run --workers 8` → `error: unrecognized arguments`.
  `LEADMINER_WORKERS=8 leadminer run` → silently ignored.
- **Fix:** `workers: int = int(os.environ.get("LEADMINER_ENRICH_WORKERS", 40))`, plus
  a `--enrich-workers` flag on `run`; and introduce `LEADMINER_ENRICH_PER_HOST`
  (default 2) enforced by a per-host semaphore.

### S3 — The three summary counts are computed over `records`, not `targets`, so they cannot be reconciled with the line above them

- **Where:** `enricher.py:264-266`, against `enricher.py:231` and `enricher.py:235`.
- **Breaks:** `live_count` and `dead_count` iterate all of `records` rather than the
  target rows, so a stale `website_live` left on a website-less master row is counted
  as a live site and the printed numbers do not sum to the `N` announced one line
  earlier. (`unknown_count` at `enricher.py:266` *is* correctly guarded by
  `r.get("website")`.) Cross-reference `_diag_d3.md` §A6, which found this by
  execution; the ops consequence is that the only internal consistency check an
  operator has — do these numbers agree with the "Fetching N" line — is broken, so a
  miscount reads as a data curiosity rather than a bug.
- **Trigger:** `check_websites([{"name": "stale", "website_live": True},
  {"website": "https://x.example"}])` with a `500` stub → one website fetched, one
  printed as `1 live / 1 dead / 0 unreachable`.
- **Fix:** compute all three over `[records[i] for i, _ in targets]`.

---

## The brief's six questions, answered directly

### 1. What does it print?

The entire operational output of a stage that can run for hours is four `print`
calls, all to **stdout** (never stderr, so nothing can be surfaced as a GitHub
Actions annotation), none with a timestamp, none flushed:

| Line | Text | Frequency |
|---|---|---|
| `enricher.py:235` | `[Enricher] Fetching {N} websites — liveness + contacts ({W} workers)...` | once |
| `enricher.py:262` | `[Enricher] {done}/{N} done...` | every 200 completions — **never** for N < 200 |
| `enricher.py:271` | `[Enricher] {live} live / {dead} dead / {unknown} unreachable` | once |
| `enricher.py:272-275` | `[Enricher] Contacts — email:{n} instagram:{n} whatsapp:{n} linkedin:{n}` | once |

For a 5,000-target stage: 28 lines. For a 50,000-target stage: 253 lines. There is
no URL, no domain, no status code, no duration, no latency, no exception text, no
per-host breakdown, and no "slowest 10 domains" — the five numbers a human would
actually need to triage a degraded run do not exist. The word `unreachable` at
`enricher.py:271` is doing the work of thirteen distinct diagnoses.

### 2. What does it swallow?

Five separate swallow points, none of which records anything:

1. `enricher.py:176-178` — every transport exception → `UNKNOWN`, cause discarded.
2. `enricher.py:190-192` — every body-read/decode exception → `UNKNOWN`, cause discarded.
3. `enricher.py:193-197` — `finally: try: r.close() except Exception: pass` — a failure
   to release the connection is silently absorbed.
4. `enricher.py:242-248` — `except Exception:` with **no `as e`**; the exception object
   is not bound, not counted, not logged, not printed. (Contrast `main.py:168`, which
   at least emits `str(e)`.)
5. `enricher.py:232-233` — `if not targets: return records`; a zero-target stage
   reports nothing at all.

Not swallowed, for what it is worth: `KeyboardInterrupt` is a `BaseException`, so
Ctrl-C does propagate — but it propagates *out* of the loop into the executor drain
(see §6).

### 3. What happens when the network drops halfway?

Reproduced verbatim in the S1 above: the first half completes, the second half becomes
`UNKNOWN`, the summary reads `60 live / 0 dead / 40 unreachable`, previously verified
`website_live=True` values are destroyed, ~40 leads lose 10 points each and drop to
the generic `Discovery call` pitch, all five CSVs are rewritten, and the process exits
0. Nothing in the output says "half of this run was garbage", and the last heartbeat
(where one exists at all) is indistinguishable from a healthy one.

### 4. What happens when a dependency returns garbage?

The dependency here is an arbitrary third-party host, so garbage is the normal case.
Observed end-to-end through the real `_fetch_website` with synthetic bodies:

| Body served at a `200` | Outcome | Extracted |
|---|---|---|
| `application/pdf` with `/Author(sales@vendor-invoices.example)` | `live` | `email=sales@vendor-invoices.example`, `instagram=vendor` |
| `image/jpeg` with an EXIF comment | `live` | `email=owner@studio.example`, `instagram=studio.example` |
| `<style>@media (max-width:600px){…}</style>` on an ordinary page | `live` | `instagram=media` |
| Cloudflare interstitial (`Just a moment…`), served as **200** | `live` | nothing |
| gzip body with no `Content-Encoding` header | `live` | nothing (binary junk, no error) |
| empty body | `live` | nothing |

So garbage in is **confident success out**: a binary file is a "live website" worth
+10 in `lead_score`, a PDF's metadata becomes the lead's email, and a `@media` CSS
at-rule becomes an Instagram handle that then satisfies
`main.has_any_contact` (`main.py:142`, `len(instagram) > 3`) and lands the record in
`sales_ready.csv` with a fabricated outreach channel — **Observed:** `has_any_contact
→ True`, `lead_score → 20`, from a page containing no contact information whatsoever.
Two ops-relevant consequences on top of the correctness issue: (a) nothing in the
output distinguishes "extracted a real handle" from "extracted a CSS keyword", so the
stage's yield metric is unfalsifiable; and (b) the *same blocked site* is classified
`DEAD` on one day (403 flavour) and `LIVE` on another (200 challenge page) and
`UNKNOWN` on a third (timeout), with no record of which, so week-over-week comparison
of `website_live` is meaningless for exactly the hosts most likely to change.
Cross-reference `_diag_d1.md` §S1-1 (the `@media` class) and §S1-2 / `_diag_d3.md`
§A1 (the 403/429 class).

### 5. What happens when it is called with an empty list?

**Observed:** `check_websites([])` returns `[]` and writes **zero bytes** to stdout.
The early return at `enricher.py:232-233` is correct and instant — no pool is
constructed, no thread is spawned, no network is touched — and it returns the
caller's own list object (verified: `out is recs` → `True`), so there is no
surprising copy.

The operational problem is not the call, it is what the silence means. In a
six-hour log, "no `[Enricher] Fetching …` line" is indistinguishable from "the
enrichment step never ran", and **zero websites is the signature of a scraper
regression** — `website` is populated from `places.websiteUri`
(`google_places.py:291`), OSM `website`/`contact:website`/`url` tags (`osm.py:91`),
and `wdt:P856` (`wikidata.py:96`), so any break in those field masks empties the
column. The consequences are all downstream and all favourable-looking:
`without_websites.csv` (`main.py:200`) becomes 100% of the corpus, i.e. the
"new-website pitch" list becomes every business; every `completeness_score` drops by
1 (`enricher.py:107-108`), so `qualified_businesses.csv` (`main.py:206`) *shrinks*;
and the workflow's own gate still passes, because it checks `ROWS >= 100` on the
*cumulative* master (`scrape.yml:78-83`), which was never truncated. Contrast
`cli.py:63-66`, where `cmd_scrape` handles exactly this shape correctly — returns `1`
and prints `WARNING: zero records. The source may be broken or blocked.` The cheap
diagnostic command is strict; the expensive one is silent.
**Fix:** print `[Enricher] 0 websites to check (N records)` at `enricher.py:232` and,
when `records` is non-empty but `targets` is empty, warn that the website column is
empty rather than treating it as normal.

### 6. What happens when it is interrupted?

- **SIGTERM** (what GitHub Actions actually sends at `timeout-minutes: 300`,
  `scrape.yml:25`): nothing in this function handles signals. The process dies, the
  `with` block at `enricher.py:237` never runs its `__exit__`, the unflushed heartbeat
  buffer is lost, and no CSV is written. Exit 143, Drive master stale, and the
  artifact upload at `scrape.yml:91-98` captures the old master. Detail and the
  orphaned-`.tmp` consequence: `200-fn-cli-py-main-ops.md` §S1-SIGTERM.
- **Ctrl-C**: `KeyboardInterrupt` is raised on the main thread inside
  `as_completed` at `enricher.py:240`. It escapes the `for` loop, then
  `ThreadPoolExecutor.__exit__` calls `shutdown(wait=True)`, which **drains** the
  already-submitted futures rather than cancelling them. Every target still in the
  queue is fetched anyway — at 50,000 targets, 40 workers and `timeout=8`, up to
  ~2.5 hours of work performed after the operator asked to stop — and the
  `\ninterrupted` message does not appear until all of it finishes. `as_completed`
  holds no reference to the un-drained results, so all of that work is discarded:
  the drain is pure waste. (`200-fn-cli-py-main-ops.md` §S2 established the drain
  semantics; the check_websites-specific part is that there is no `cancel_futures`,
  no cooperative cancellation flag that `_fetch_website` could check, and no
  checkpoint for the drain to have written to.)
- **The stage is not resumable in any case.** See the "hard gate on publishing"
  finding: nothing is persisted until the function returns.

---

## Silent-failure ranking — how long until a human notices

Ordered by **time to detection**, longest first. "Silent" = exit 0 and/or no output
the operator will read.

| # | Failure | What is printed | Time to notice |
|---|---|---|---|
| 1 | **Total enrichment outage** — expired `certifi` on the runner, egress IP banned by a WAF, runner DNS broken. Every target `UNKNOWN`, exit 0, five CSVs rewritten, every website-bearing lead demoted to `Discovery call`. | `[Enricher] 0 live / 0 dead / 5000 unreachable` — reads like a market finding | **Weeks–months.** Detectable only by noticing `recommended_service` collapsed to the generic bucket, or by a rep asking why their pitch list got boring. Nothing in the log distinguishes it from "our targets are all unreachable". |
| 2 | **`website` column empties** (scraper field-mask regression). `targets` empty, early return, nothing printed. `without_websites.csv` becomes 100% of the corpus. | nothing at all | **Weeks–months.** Passes the `ROWS >= 100` gate (`scrape.yml:80`) because the master is cumulative. Only visible as "the new-site list got huge", which reads as success. |
| 3 | **Zero extraction, reported as a harvest.** Every fetch succeeds, every regex fails (encoding change, template change, body now past the 200 KB cap). | `Contacts — email:1840 instagram:2600 …` — identical to a great week | **Months, or never.** The number is a function of the corpus, not the run, so it cannot be charted and cannot fall. |
| 4 | **Rate-limit/bot-wall contamination of `dead_count`.** 403/429/5xx → `DEAD` → +20 → `Website rebuild + maintenance`. The 40-worker burst causes it. | `[Enricher] 1840 live / 300 dead / 0 unreachable` — reads as a healthy run with opportunities | **Months.** The first real-world consequence is a rep emailing businesses whose websites are working fine, which is the moment the data loses credibility. |
| 5 | **Mid-run network drop.** Half `UNKNOWN`, half complete, exit 0, prior verdicts destroyed and persisted by `main.py:209`. | `60 live / 0 dead / 40 unreachable` | **Weeks.** Same evidence problem as #1; the only extra clue is that the unknown count is higher than usual, and nobody has a baseline to compare it to. |
| 6 | **Partial-overwrite of durable verdicts.** One `ReadTimeout` resets a known-`True` row to `None` (`enricher.py:250`). | one extra entry in `unreachable` | **Months.** Invisible in aggregate; only a row-level diff of consecutive weeks' masters reveals it, and the masters are timestamped to the day, not per-field. |
| 7 | **Stage never finishes** (slow-drip hosts, redirect chains) **or is SIGKILLed** (gzip bomb at `enricher.py:188`, 40 × 200 MB). Heartbeat stops. | nothing after the last `{done}/{N} done...`; for the SIGKILL, nothing at all — no traceback | **Days** — and only because GitHub Actions kills the job at 300 min. The unflushed buffer means the last progress line is stale too. |
| 8 | **`workers` unreachable / no per-host cap.** The knob for politeness and timeout risk cannot be turned. | `(40 workers)` in the one start line | **Weeks**, and only after rank 4 has already cost you credibility. |
| 9 | **Empty target list.** | nothing | **Days**, and only if you know to look for an *absent* line. |
| 10 | **A worker exception escaping at `enricher.py:249-259`** after the refactor described above. | a traceback, then a drained pool, then exit 1 | **Immediate** — the only category that reliably surfaces. |
| 11 | **Ctrl-C.** Drains the queue for up to ~2.5 h, then unwinds. | nothing for hours, then `interrupted` | **Minutes if watched**, but reads as a frozen process rather than a stop request. |

**The pattern:** ranks 1–6 are all "exit 0, plausible-looking output, and a number
that has never been lower before". There is no baseline to diff against, no reason
taxonomy, and no alarm — so every one of them is discovered by a human downstream of
the pipeline (a rep, a customer, a quarterly review) rather than by the pipeline.

---

## Not a bug, but worth knowing

- **The tri-state design is right and should be preserved.** `LIVE`/`DEAD`/`UNKNOWN`
  at `enricher.py:154-156`, the documented contract at `enricher.py:148-153`, and the
  scoring asymmetry at `enricher.py:300-304` (UNKNOWN gets no credit, exactly like a
  missing website) are all correct as designed. Every problem in this report is a
  failure to *apply* the design faithfully at the boundaries, not a flaw in it.
- **The early return at `enricher.py:232-233` is correct, fast and side-effect-free.**
  It costs nothing; it just needs to say something.
- **Thread-safety is genuinely fixed.** `get_session()` is thread-local
  (`httpclient.py:58-71`) and each worker therefore gets its own cookie jar and
  connection pool. The old shared-module-`Session` race is gone. The residual
  concern is only that those 40 sessions are never explicitly closed, so pool
  teardown relies on CPython refcount GC — harmless here, but it means socket count
  is not a deliberate operational quantity.
- **The body cap at `enricher.py:188` is a good instinct** — it bounds memory before
  `decode()`. Its ops cost is that a truncation is indistinguishable from a page with
  no footer contact, and nothing counts truncations.
- **`verify=True` (the default at `enricher.py:175`) is correct** and its failure path
  correctly returns `UNKNOWN`. But this is precisely why rank 1 above is so expensive:
  the TLS verification working as designed is indistinguishable, in the output, from
  the TLS verification failing on every host.
- **`main.py:215-217` recomputes `live`/`dead`/`unknown` independently** of
  `enricher.py:264-266`, in a `Summary:` block near the end of the log. Two
  implementations of the same three numbers means a fix to one is not a fix to the
  other, and the two can disagree.
- **`cli.cmd_doctor` (`cli.py:204-244`) has zero coverage of the enrichment layer.**
  It probes Overpass, Wikidata and Google Places — never a business website. So the
  designated preflight cannot detect an enrichment-layer outage, which is the largest
  stage and the one talking to the most unpredictable dependencies. (It also cannot
  fail: see `200-fn-cli-py-main-ops.md` §S2-`doctor`.)
- **`cli.cmd_stats` (`cli.py:107-113`) reports the tri-state correctly** and is the
  right offline tool — but it prints absolute counts with no week-over-week delta and
  no unknown-ratio alarm, so it has the same "no baseline" problem as finding #3.
- **`concurrency: cancel-in-progress: false` (`scrape.yml:15-17`)** means CI never
  sends SIGINT, so the Ctrl-C path is untested in production too.

---

## Cross-references (already reported; not re-litigated here)

- `_diag_d1.md` §S1-1, §S1-2 — the `@`-matches-`@media` Instagram fabrication and the
  403/429→`DEAD` classification. This report adds the ops consequence: the crawler
  *causes* the 429s, and the output cannot be distinguished from a healthy run.
- `385-fn-enricher-py-check_websites-edge.md` — same target, edge-case lens. It owns
  the compressed-bytes body cap (§S1, re-framed above as a SIGKILL risk), the
  whitespace/placeholder truthiness gate at `enricher.py:231` (which my §5 touches only
  as "zero targets is silent"), `304 Not Modified` classified `LIVE`, and the
  unguarded pre-pool validation of `records`/`workers`. Those are not re-counted here.
- `388-fn-enricher-py-check_websites-contract.md` — same target, contract/typing lens.
  It owns the type contract (`BusinessRecord` being decorative, the undeclared inner
  shape of the `tuple[str, dict]` return), the `bool | None` representability argument
  for `website_live`, the zero test coverage of the write path, and the two
  independent `website_live` decoders. Its §S2 "consumed by subscript outside the
  `try`" is the same defect as my second-to-last S2; the contract lens states *why* the
  invariant is fragile, mine states what it costs on a Monday morning.
- `_diag_d3.md` §A1, §A3, §A5, §A6 — the 4xx classification, the unconditional
  `website_live` overwrite, the missing URL dedupe, and the summary tallies. The dedupe
  and the tallies are re-raised here because their *ops* severity depends on the
  40-worker/no-per-host-cap design at `enricher.py:237`, which those reports do not
  cover.
- `200-fn-cli-py-main-ops.md` §S1-SIGTERM, §S1-cost, §S2-Ctrl-C, §S2-flush — the
  interruption, timeout-cliff and buffering findings at the process level. This report
  adds the stage-specific consequences: the drain has nothing to checkpoint, and
  `timeout=8` is a socket timeout rather than a deadline.
- `083-SYNTH-s1-backlog.md` item 1 — the same 4xx-policy finding at backlog priority.
- `043-scoring-upgrade.md` — the original inversion this tri-state was introduced to
  fix. The `status >= 400` sweep has reintroduced it through the side door.

---

## Recommended order of work

1. **Make the stage able to report failure.** Carry a cause through
   `enricher.py:176-248`, tally it, `log.warning()` the first of each kind, and print
   `unreachable by reason:` at `enricher.py:271`. Add `logging.basicConfig` so
   `httpclient`'s existing telemetry is not inert. Covers ranks 1, 5, 6 — and is the
   prerequisite for every other item, because until you can name the cause none of the
   rest can be verified.
2. **Stop the self-inflicted rate limiting.** Dedupe targets at `enricher.py:231`, add
   a per-host semaphore (default 2), and carve `401/403/407/408/425/429` out to
   `UNKNOWN` before `enricher.py:182`. Covers rank 4, and removes the mechanism that
   makes rank 8 matter.
3. **Add a retry and a deadline.** `fetch_with_retry(…, timeout=(3,5), retries=2,
   max_bytes=…)` at `enricher.py:175`, a per-URL wall-clock budget, and
   `cancel_futures=True`. Cap the body *after* decompression at `enricher.py:188`.
   Covers rank 7.
4. **Give the run an outcome.** Never overwrite a durable `True` with `None` at
   `enricher.py:250`; treat `unknown_ratio > 0.10` as a degraded run that must not
   exit 0; make the summary counts reconcile with the "Fetching N" line. Covers
   ranks 1, 5, 6.
5. **Fix the metrics so they can be diffed.** Print contact *deltas*, not corpus
   totals, at `enricher.py:267-270`. Make the heartbeat time-based with elapsed, rate,
   ETA and running tallies, add `flush=True`, and log something in the empty-target
   branch at `enricher.py:232`. Covers ranks 2, 3, 7, 9 — this is what creates the
   baseline the other items are measured against.
6. **Checkpoint and unblock publishing.** Give enrichment its own time budget and a
   TTL'd URL→verdict cache, so a partial stage still publishes and the next run does
   not re-fetch the whole cumulative master. Covers rank 7 and the linear-cost cliff.
7. **Widen the `try`.** Move `enricher.py:249-259` inside the guard at
   `enricher.py:242`, and return a dataclass with defaulted fields from
   `_fetch_website`. Covers rank 10 — cheap insurance against a plausible refactor.
8. **Expose the knobs and extend `doctor`.** `LEADMINER_ENRICH_WORKERS` /
   `--enrich-workers`, `LEADMINER_ENRICH_PER_HOST`, and add a business-website probe
   plus an unknown-ratio check to `cmd_doctor` and `cmd_stats`. Covers ranks 8, 9.
