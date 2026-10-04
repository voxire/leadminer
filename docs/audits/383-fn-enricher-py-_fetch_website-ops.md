# 383 — `_fetch_website` under the ops lens: it succeeds through every failure

**Target:** `enricher.py:161` — `_fetch_website(url: str) -> tuple[str, dict]`
**Lens:** ops — six hours, unattended, weekly cron, one operator who reads the log once a week.

---

## Verdict

`_fetch_website` is defensively written and operationally mute. Its docstring
(`enricher.py:162-167`) is careful about *semantics* — "never infer dead from a
transport failure" — and it earns that: `LIVE`/`DEAD`/`UNKNOWN` are correctly
distinguished, TLS verification is on, and the body is capped. But it communicates
**nothing**. It returns a verdict and no reason, prints nothing, logs nothing, and
the whole enrichment pass emits ~620 bytes of un-timestamped `print` output for a
3,000-site run. The single most dangerous property is that **a total network
outage, a WAF ban, a wrong TLS trust store and "these businesses genuinely have no
working sites" all produce byte-identical output, a zero exit code, and a
plausible-looking CSV.** I measured it: 500 sites, 100% unreachable, resolved in
56 ms, printed `0 live / 0 dead / 500 unreachable`, exited clean.

Compounding that: nothing bounds a single fetch in wall-clock time, `Ctrl-C`
does not cancel the pass (it drains every queued URL first), and the pass writes
nothing to disk until it finishes — so a run killed at hour 5 of 6 leaves **no CSVs
and an empty log**.

**The one-line version:** this function cannot fail loudly, and its two unbounded
resource consumers (per-read wall clock, per-byte CPU) are the only things in it
that can fail *loudly*, by hanging.

---

## Evidence base

Everything below is either anchored to source or measured locally with a fake
`requests` / `httpclient` that supplies scripted HTTP outcomes. No network calls,
no package installs, no source modified. Measured numbers are quoted as such;
anything reasoned from source but not executed is labelled **[not measured]**.

---

## Findings

### S1 — A total enrichment outage is indistinguishable from "no sites exist", and the job exits 0

- **Where:** `enricher.py:176-178` (transport failure → `UNKNOWN`), `enricher.py:190-192` (body read failure → `UNKNOWN`), `enricher.py:231-233` (no targets → silent early return), `enricher.py:271` (the one aggregate line), `scrape.yml:74-83` (the only automated gate).

- **Breaks:** Every failure mode in this function returns the same two-tuple. Measured, exhaustively:

  | Input to `_fetch_website` | Returns |
  |---|---|
  | `ConnectTimeout` / `ReadTimeout` | `('unknown', all-None)` |
  | `SSLError` (expired cert) | `('unknown', all-None)` |
  | `ConnectionReset` / `ConnectionRefused` | `('unknown', all-None)` |
  | `TooManyRedirects` (>30 hops) | `('unknown', all-None)` |
  | `MemoryError` | `('unknown', all-None)` |
  | `ValueError` (a plain programmer bug) | `('unknown', all-None)` |
  | `''`, `None`, `'   '`, `'not a url'`, `'htp://x'`, `'http://'`, `12345`, `b'http://x'`, `{'website': ...}` | `('unknown', all-None)` |
  | `r.raw.read()` raises `OSError` | `('unknown', all-None)` |
  | `r.raw is None` | `('unknown', all-None)` |
  | `r.status_code` is a non-int | `('unknown', all-None)` |

  There is no `reason` in the return value, no counter, no log line, no metric.
  The only signal a human gets is the single aggregate at `enricher.py:271`:
  `[Enricher] {live} live / {dead} dead / {unknown} unreachable`.

  `unreachable` is *expected to be non-zero* — some Lebanese and Saudi SMB domains
  are genuinely dead, DNS-blackholed, or geo-unreachable from a GitHub runner. So
  the number carries no alarm. There is no threshold, no comparison to the
  previous run, and no non-zero exit. `scrape.yml:74-83` validates **row count
  (≥100)** and nothing else — a run in which 100% of website checks were
  unreachable passes validation and is uploaded to Drive at `scrape.yml:88-89`.

  Measured, 500 unreachable sites:
  ```
  [Enricher] Fetching 500 websites — liveness + contacts (40 workers)...
  [Enricher] 200/500 done...
  [Enricher] 400/500 done...
  [Enricher] 0 live / 0 dead / 500 unreachable
  [Enricher] Contacts — email:0 instagram:0 whatsapp:0 linkedin:0
  500 unreachable sites resolved in 56 ms
  lead_score: 30
  ```
  Exit code 0. No `ERROR`. The `[Enricher]` prefix is the only thing in the line
  that even hints at a problem.

- **Concrete triggers, none of which print anything:**
  - Runner loses egress mid-run (Actions VM network flap, or the runner IP is
    blocked by a regional WAF). Every remaining URL returns `ECONNREFUSED` or
    `EAI_AGAIN`.
  - `ca-certificates` bundle change / a corporate MITM proxy on the runner →
    `SSLError` on every site → every site `UNKNOWN`.
  - `SCRAPER_EMAIL` unset (`httpclient.py:81-85` degrades the UA silently, no
    error) → more sites served 403 → **and 403 is `DEAD`, not `UNKNOWN`** (see
    S2 below), which is strictly worse: a UA regression manufactures
    server-confirmed-dead sites.

- **The downstream damage is silent because it is a subtraction.** `lead_score`
  (`enricher.py:300-304`) awards `+10` for `website_live is True` and `+20` for
  `website_live is False`. `UNKNOWN` (`None`) earns **0 for both**. So a total
  outage doesn't create wrong leads, it *quietly demotes every good one by 10
  points* — measured: `lead_score` 30 instead of 40 for a high-priority lead with
  a phone and a working site. Nothing in the summary at `main.py:219-226`
  surfaces a lead-score distribution shift.

- **And the data cannot show it either.** `website_live` is in `dedup._VOLATILE`
  (`dedup.py:114`), and `dedup._pick` (`dedup.py:167-171`) treats `None` as
  missing (`dedup.py:86`), so this run's `UNKNOWN` **loses** to last week's
  `True`. The cumulative master therefore keeps last week's verdicts. During an
  outage, `all_businesses.csv` is *identical* to the previous week for
  `website_live`. The outage is invisible in the log **and** in the diff.

- **Fix (smallest version that works):** thread a machine-readable reason out of
  the function and assert on it at the pass level.
  ```python
  # enricher.py — return (outcome, contacts, reason)
  except requests.RequestException as e:
      return UNKNOWN, contacts, f"{type(e).__name__}"
  except Exception as e:                     # keep the catch-all, but say so
      log.exception("unexpected in _fetch_website(%r)", url)
      return UNKNOWN, contacts, f"Bug:{type(e).__name__}"
  ```
  Then in `check_websites`, `collections.Counter` the reasons, print the top 5,
  and — critically — **raise or `sys.exit(1)` when `unknown / with_website`
  exceeds a configurable threshold (start at 0.5)**, so `scrape.yml:74-83` fails
  the run instead of publishing it.

---

### S1 — Nothing bounds a single fetch in wall-clock time; a dribbling host pins a worker forever

- **Where:** `enricher.py:175` (`timeout=8`), `enricher.py:188` (`r.raw.read(_MAX_BODY_BYTES, ...)`), `enricher.py:237-238` (all N submitted at once, nothing else in flight).

- **Breaks:** In `requests`, a scalar `timeout=8` sets the socket timeout to 8s
  **per socket operation**. It is an *inactivity* timeout, not a deadline, and it
  has three gaps that matter for an unattended run:

  1. **DNS is not covered.** `timeout` is applied to the socket *after*
     `getaddrinfo` returns. `getaddrinfo` is a blocking libc call with no timeout
     of its own; on a runner with a degraded resolver it can block for the OS
     resolver timeout (tens of seconds) and, retried per attempt, far longer.
     **[not measured — no network available]** The mechanism is
     `urllib3.connection.HTTPConnection.connect` → `socket.create_connection`,
     which calls `getaddrinfo` before applying the timeout.
  2. **`read(amt)` loops.** `r.raw.read(200_000)` keeps reading until it has
     200,000 bytes or EOF. A host that sends one byte every 7 seconds never
     exceeds the 8s inactivity window and never returns. **[not measured]**
  3. **The cap is bytes, not time.** 200,000 bytes at 1 byte/7s is 16 days.

  Worst-case cost per URL is therefore `DNS + connect(8) + TLS(8) + N×read(8)`
  with `N` unbounded — not the ~8s the code reads like.

  At 40 workers with no other work in `check_websites`, N such hosts consume the
  whole pool. `as_completed` at `enricher.py:240` simply stops producing. The
  progress print at `enricher.py:261` stops advancing, which is the *only* hint —
  and see S1-#3 for why it never reaches the log.

- **Trigger:** `website` = a host behind a tarpit, a misconfigured cPanel that
  serves headers and then trickles the body, or a shared Lebanese host
  (`idm.com.lb`, `cyberia.net`) that queues rather than rejects. One such host
  per worker is enough.

- **Consequence at the cron level:** `scrape.yml:25` is `timeout-minutes: 300`.
  The job is killed at 5 hours, `main.py:209-213` never runs (the CSVs are
  written only after `enrich()` returns at `main.py:187`), and the week is lost.
  Because `concurrency: cancel-in-progress: false` (`scrape.yml:15-17`), the next
  run is not blocked, but it starts from the same unchanged Drive master
  (`scrape.yml:56`) and redoes the identical work.

- **Fix:** separate the two timeouts and put a real deadline around the read.
  ```python
  r = get_session().get(url, timeout=(4, 8), allow_redirects=True, stream=True)
  deadline = time.monotonic() + 15          # whole-request wall clock
  chunks, total = [], 0
  for chunk in r.iter_content(65536):
      chunks.append(chunk); total += len(chunk)
      if total >= _MAX_BODY_BYTES or time.monotonic() > deadline:
          break
  ```
  A deadline check in the read loop is what actually caps this; the socket
  timeout alone cannot.

---

### S1 — `Ctrl-C` / `SIGTERM` does not cancel the pass: it drains every queued URL, then dies with an empty log and no CSVs

- **Where:** `enricher.py:237` (`with ThreadPoolExecutor(...) as pool:`), `enricher.py:238` (every target submitted up front), `main.py:187` (`records = enrich(records)`), `main.py:209-213` (the only writes), `enricher.py:261-262` (the only progress output).

- **Breaks:** Three things compound into a genuinely nasty operational trap.

  **(a) The pool drains instead of cancelling.** `ThreadPoolExecutor.__exit__`
  calls `shutdown(wait=True)` with `cancel_futures=False`. All N targets are
  submitted at `enricher.py:238`, so every queued URL still runs.

  Measured — a `SIGALRM`-delivered `KeyboardInterrupt` raised in the main thread
  at t=0.30 s against 600 targets:
  ```
  interrupt at t=0.30s -> KeyboardInterrupt
  HTTP requests performed: 600/600
  ```
  All 600 fetches happened anyway. Scaled to a real run (3,000 targets, blackholed
  network at 16 s/URL worst case): **Ctrl-C at minute 20 buys you another ~20
  minutes of work before the process actually exits** — and on a cron runner,
  `scrape.yml` sends `SIGINT` then `SIGKILL` seconds later, so the drain is
  simply cut off mid-flight.

  **(b) Nothing is on disk.** Every result lives only in the in-memory `records`
  dicts (`enricher.py:249-259`). `main.py:209-213` writes all five CSVs *after*
  `enrich()` returns. Interruption ⇒ zero output. That is the right call for
  atomicity, but there is **no checkpoint and no resume**, so the next weekly run
  re-fetches all 3,000 sites from scratch.

  **(c) The log is empty, so you can't even tell how far it got.** `check_websites`
  uses bare `print` with no `flush=True` (`enricher.py:235`, `261`, `271-275`).
  On a non-tty (GitHub Actions step) stdout is block-buffered. The *entire*
  enrichment log for a 3,000-target run is **623 bytes**; measured with a
  subprocess killed via `os._exit(9)` (a faithful SIGKILL stand-in — no atexit,
  no flush):
  ```
  stdout bytes surviving os._exit(9): 0
  ```
  A killed run therefore produces **zero bytes** of enrichment output. The
  operator sees a truncated log with no `[Enricher]` lines at all — no count, no
  progress, no summary. Reconstructing the failure requires re-running locally.

- **Trigger:** any of — the operator hits Ctrl-C; `scrape.yml:25` fires at 300
  minutes; Actions cancels the job on a repo change; the VM is reaped. All four
  produce the same nothing-to-see-here log.

- **Fix:** three independent changes.
  1. `pool.shutdown(wait=False, cancel_futures=True)` in a `finally`, so
     in-flight work stops instead of draining.
  2. `print(..., flush=True)` on `enricher.py:235`, `261`, `271`, `273` — or
     switch to `logging`, which is line-buffered to a tty and flush-capable
     (see S2-#3).
  3. Stream results to disk as they complete (append-only NDJSON + a final CSV
     pass) so a killed run still leaves usable enrichment behind.

---

### S2 — `_EMAIL_RE` is quadratic: the 200,000-byte cap is a byte cap, not a CPU cap

- **Where:** `enricher.py:130` (`_EMAIL_RE`), `enricher.py:200` (the call), `enricher.py:186-188` (the cap whose comment claims to defend against exactly this).

- **Breaks:** The comment at `enricher.py:186-187` says:

  > Cap the body before decoding it: `r.text` would materialise the whole
  > response, and a hostile or misconfigured host can stream forever.

  The byte cap closes the memory hole. It does **not** close the CPU hole, because
  `_EMAIL_RE` is `[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}` — a greedy
  `+` that must backtrack one character at a time when the `@` never arrives, at
  *every* start position in the body. Cost is **O(n²) in the length of a single
  unbroken run of `[A-Za-z0-9._%+-]` containing no `@`.**

  Measured, raw regex:

  | unbroken run | time |
  |---|---|
  | 3,200 | 20.3 ms |
  | 6,400 | 119 ms |
  | 12,800 | 778 ms |
  | 51,200 | 9.6 s |

  Measured **end-to-end through the real `_fetch_website`** (100 responses × 200
  workers, so genuinely concurrent):

  | body | wall time for one `_fetch_website()` |
  |---|---|
  | 25,600 unbroken chars | **4.12 s** |
  | 51,200 unbroken chars | **15.86 s** |
  | 102,400 unbroken chars | **57.11 s** |
  | 200,000 (= the cap) | **~215 s (extrapolated)** |

  The `ms/KB` column doubles every KB (0.16 → 0.31 → 0.56) — textbook quadratic.
  A single adversarial URL costs **~3.6 minutes** of CPU inside one worker; 40 of
  them pin all 40 workers at `enricher.py:237` for 3.6 minutes each.

  **Honest calibration — normal pages are fine**, and I measured them so this
  isn't overstated:

  | 200 KB body | time |
  |---|---|
  | typical marketing HTML | 5–12 ms |
  | minified JS | 3.2 ms |
  | real base64 (`/` breaks runs every ~64 chars) | 52 ms |
  | ~3 KB base64url JWT | 45 ms |

  So the trigger is narrow: a body that is one long unbroken `[A-Za-z0-9._%+-]`
  token. Reachable via a long base64url token (JWT / Sentry DSN / Turnstile key /
  WP nonce — `_` and `-` are *inside* the class), a `text/plain` or asset served
  as the site, a broken template emitting an unbroken identifier, or a hostile
  host. That last one is the point: the code documents a hostile-host threat model
  and then leaves a CPU denial-of-service in the same function.

  Note the failure is *silent in the worst way*: it doesn't crash, it doesn't log,
  it just stops the pool from draining until `scrape.yml:25` kills the run — which
  is S1-#3's empty log.

- **Fix:** don't regex a bounded-but-still-hostile buffer. Cheap and effective:
  ```python
  # bound the *candidate set*, not the scan
  scan = html[:80_000]                      # contacts live in <head>/first screen
  candidates = set(_EMAIL_RE.findall(scan))
  ```
  plus (a) bail if a single run exceeds a threshold, or (b) replace the pattern
  with a non-backtracking equivalent: `[A-Za-z0-9._%+\-]+@(?:[A-Za-z0-9\-]+\.)+[A-Za-z]{2,}`
  is still backtracking; the robust fix is to pre-split on `@`, or move to the
  `regex` module with `timeout=2`. Also worth adding: `_EMAIL_RE` and friends
  are run on *every* body including binary — a `Content-Type` guard (only parse
  `text/html` and `application/xhtml+xml`) kills most of the risk.

---

### S2 — A 200 KB truncation is recorded as `LIVE` with zero contacts, and the lead silently leaves the funnel

- **Where:** `enricher.py:188` (cap), `enricher.py:189` (decode), `enricher.py:222` (`return LIVE, contacts`), `enricher.py:251-259` (contact writes), `main.py:137-145` (`has_any_contact`), `main.py:202-205` (`sales_ready`).

- **Breaks:** `_MAX_BODY_BYTES = 200_000` (`httpclient.py:54`). Any page whose
  contact block sits past byte 200,000 is read *successfully* — `read(amt)`
  truncates without raising — and returned as `LIVE` with all four contacts `None`.
  The truncation is not recorded anywhere: no flag, no log line, no field.

  Measured, identical page content, only the padding size differs:

  | body | contact block starts at | result |
  |---|---|---|
  | 160,237 B | byte ~160,013 | `LIVE`, `email='info@bayt-lehban.com'`, `instagram='bayt'`, `whatsapp='+96170123456'`, `linkedin='.../bayt-lehban'` |
  | 320,237 B | byte ~320,013 | `LIVE`, **all four contacts `None`** |

  This is a *silent funnel loss*. `has_any_contact` (`main.py:137-145`) needs a
  phone ≥7 digits **or** an email **or** an Instagram handle >3 chars, so a
  record whose only contact channel was the website footer drops out of
  `sales_ready.csv` (`main.py:202-205`) — while `website_live=True` still earns it
  `+10` in `lead_score` (`enricher.py:301-302`), so it also *looks* like a healthy
  lead. Modern WordPress/Shopify themes routinely emit >200 KB of inline CSS and
  JS before the footer.

  The aggregate line cannot reveal it either: `found_email`/`found_ig`/
  `found_wa`/`found_li` (`enricher.py:267-270`) count over **all** records,
  including ones with no website and ones that already had an email. Measured:

  ```
  records: A(website + pre-existing email), B(website), C(no website)
  [Enricher] Contacts — email:2 instagram:2 whatsapp:2 linkedin:2
  record A email after enrichment: 'pre@existing.test'
  ```
  If `_fetch_website`'s extraction broke *entirely*, that line would still say
  `email:2` — one from A's pre-existing address, one from… A's, again. The
  counter cannot attribute a single contact to the fetcher. **You cannot alert on
  "extraction stopped working" from this log.**

- **Trigger:** `website` = `https://bayt-lehban.com/` on a heavily-themed Shopify
  or WordPress build whose footer mailto is past 200 KB.

- **Fix:** (a) count bytes actually read and expose it —
  `contacts["_truncated"] = total_read >= _MAX_BODY_BYTES` — then log the
  truncated count in the summary and treat truncation as `UNKNOWN`, not `LIVE`;
  (b) fix the counters to count *only* fields the fetcher populated
  (`enricher.py:252-259` can increment a local tally at the point of assignment);
  (c) if a page is truncated, say so in the log so a human can look.

---

### S2 — 40-way concurrency with no backoff, no per-host cap, and no `Retry-After` handling turns a WAF into a pitch list

- **Where:** `enricher.py:175` (no `Retry-After` handling), `enricher.py:182-184` (`status >= 400` → `DEAD`), `enricher.py:237` (`max_workers=40`), `httpclient.py:142-146` / `httpclient.py:194` (the backoff machinery that exists but is never called).

- **Breaks:** Sibling report `380-fn-enricher-py-_fetch_website-contract.md:104`
  covers the *semantics* (429 → `DEAD` is wrong). This is the ops consequence.

  Measured status → outcome:

  | status | outcome |
  |---|---|
  | 200, 201, 204, 301, 304 | `LIVE` |
  | 400, 403, 404, 410, **429**, 500, 502, **503** | **`DEAD`** |

  Nothing throttles the crawl and nothing backs off. `httpclient.py` has a correct
  implementation of exactly this — `_should_retry` (`httpclient.py:142-146`),
  `_retry_after` (`httpclient.py:120-139`), full-jitter sleep
  (`httpclient.py:196-206`) — and `_fetch_website` never calls it
  (see `382-fn-enricher-py-_fetch_website-deps.md:88`). So a `Retry-After: 3600`
  on a rate-limited host is not merely ignored, it is recorded as
  `website_live=False` → `+20` in `lead_score` (`enricher.py:303-304`) → Tier-1
  "Website rebuild" pitch to a business whose site is **fine and actively rate
  limiting us**.

  This is a self-reinforcing loop, which is what makes it ops rather than
  semantics: 40 workers burst a host → the host's WAF returns 403/429 → those are
  scored `DEAD` and become the **highest-pitch-score leads in the dataset** → next
  week we hit the same host again (same 40-way burst, same UA) → repeat. The
  businesses we are most likely to hammer are precisely the ones we most likely
  mislabel as broken. Lebanese shared cPanel hosts and KSA CDN-fronted sites are
  the realistic blast radius.

  Related and free: `timeout=8` and the 40-wide fan-out are hardcoded at
  `enricher.py:175` and `:237` while `httpclient.py:47-48` carries
  `DEFAULT_TIMEOUT = 15` / `DEFAULT_RETRIES = 3`. Two sources of truth for
  transport policy, one of them ignored.

- **Trigger:** 15 businesses from the scrape share one shared cPanel IP. 40
  workers issue GETs to that IP in the same ~100 ms window; ModSecurity returns
  403 to all of them; all 15 become `DEAD`, `+20` each, Tier 1.

- **Fix:** per-host serialization (group `targets` by `urlparse(url).netloc`, cap
  concurrency per host at 1, add a per-host delay), and route through
  `fetch_with_retry` with `_should_retry`-style handling so 429/503 back off
  instead of becoming `DEAD`. At minimum: `if status in (429, 503): return UNKNOWN,
  contacts`.

---

### S2 — No `logging` handler anywhere in the project; the whole enrichment pass is four un-timestamped `print`s

- **Where:** `enricher.py:168-222` (function prints and logs nothing), `enricher.py:235`, `261`, `271-275` (the only four emissions), `enricher.py:327-328` (a `urllib3` warning suppressor), `httpclient.py:45`, `httpclient.py:178-179`, `httpclient.py:204-205` (`log.debug` to nowhere), `cli.py:204-244` (`doctor` checks the wrong hosts).

- **Breaks:** `logging` is imported in three modules and **`logging.basicConfig`
  is never called anywhere in the repo** — verified by grep across all `.py`
  files; the only hits for `basicConfig` are in audit prose. `httpclient.py:178`
  and `:204` therefore emit to the "handler of last resort", which prints
  WARNING+ to stderr — and everything `_fetch_website` and `fetch_with_retry` log
  is `log.debug`, so **nothing at all is emitted**. `enricher.py` doesn't import
  `logging` at all.

  What survives is:

  ```
  [Enricher] Fetching N websites — liveness + contacts (40 workers)...
  [Enricher] {done}/{N} done...            # every 200 (enricher.py:261)
  [Enricher] L live / D dead / U unreachable
  [Enricher] Contacts — email:E instagram:I whatsapp:W linkedin:K
  ```

  No timestamps (so you cannot correlate a slow patch with a network event, or
  time a single fetch), no levels, no thread identity, no per-URL detail, no
  duration, no bytes read, no exception text. Total for a 3,000-site run: 623
  bytes.

  And the diagnostic that *does* exist points at the wrong target: `cmd_doctor`
  (`cli.py:224-243`) probes only Overpass, Wikidata and Google Places. The
  enrichment pass — the only thing that touches third-party business sites, and
  the only thing subject to WAF bans, TLS interception and DNS blackholes — has
  **no connectivity probe at all**. An operator debugging "why did enrichment
  collapse?" is told the internet is fine, because it is.

  Corroborating sibling report on the same gap at the CLI level:
  `docs/audits/200-fn-cli-py-main-ops.md:153`.

- **Fix:** one `logging.basicConfig(...)` in `main()`, level from
  `LEADMINER_LOGLEVEL` (default `INFO`), `py_compile`-safe. Then in
  `_fetch_website`: `log.debug("fetch %s -> %s (%d B in %.2fs)", url, outcome,
  len(raw), elapsed)`, and `log.warning` with the exception text on the transport
  path. Add an egress probe to `cmd_doctor` that hits a generic business site, not
  just the three API hosts.

---

### S3 — Called with nothing to do, it says nothing at all

- **Where:** `enricher.py:231-233`.

- **Breaks:** `targets` empty → `return records` at `enricher.py:233`, *before*
  the first `print` at `:235`. Measured:

  | input | stdout |
  |---|---|
  | `[]` | `''` (0 bytes) |
  | `[{"name": "x"}]` | `''` (0 bytes) |
  | `[{"name": "x", "website": None}]` | `''` (0 bytes) |

  "No business in this dataset has a website" and "the enricher was never called"
  produce identical, empty output. It never sets `website_live` either — those
  records are only defaulted to `None` later at `enricher.py:346`.

  Note the good news in the same place: `_fetch_website` called directly with
  junk (`''`, `None`, `12345`, `b'http://x'`, a dict) returns `UNKNOWN`
  cleanly rather than raising, so `check_websites`'s targets filter at
  `enricher.py:231` is doing its job.

- **Fix:** `print(f"[Enricher] 0 websites to check (of {len(records)} records)")`
  before the early return.

### S3 — Progress granularity of 200 means small runs get no progress at all

- **Where:** `enricher.py:260-262`.

- **Breaks:** `if done % 200 == 0` — a run with fewer than 200 targets prints only
  the header and the summary. Combined with S1-#3 (block buffering, no flush), a
  150-site run that hangs on a tarpit prints **nothing** while it hangs.

- **Fix:** print every 25, or on a time interval (`if done % 200 == 0 or
  time.monotonic() - last > 30`), with `flush=True`.

### S3 — One `Session` per worker thread, never closed

- **Where:** `httpclient.py:58-71`, `enricher.py:175`.

- **Breaks:** `get_session()` is `threading.local`, so `check_websites`'s
  40-worker pool builds 32–40 `requests.Session` objects (measured: 32 sessions
  after a 500-URL run), each with its own urllib3 pool holding keep-alive
  sockets. `httpclient.py` exposes no `close_session()`, and `_fetch_website`
  never calls `close()`. They are released by refcounting when the pool's threads
  exit, which also **throws away all connection reuse between runs** — every
  weekly run pays full TCP + TLS for its first request to every host.

  This is bounded and not urgent, but it is the reason a 3,000-URL run opens
  thousands of sockets instead of a few hundred. It also means `r.close()` at
  `enricher.py:195` is the *only* connection hygiene in the function — and its own
  failure is silently ignored (see next finding).

- **Fix:** a `close_session()` in `httpclient.py` called from a
  `finally` in `check_websites`, or one shared `Session` with a correctly sized
  `HTTPAdapter(pool_connections=40, pool_maxsize=40)`.

### S3 — `except Exception: pass` around `r.close()` hides a socket leak

- **Where:** `enricher.py:193-197`.

- **Breaks:** A `close()` that raises is swallowed and the function continues,
  returning a **normal verdict**. Measured:

  ```
  r.close() raises RuntimeError -> ('live', {...all None...})
  ```

  So a connection that cannot be released becomes an invisible fd leak, and the
  run still reports the site as healthy. Small on its own — but it is the exact
  mechanism that would make S3-above's session accumulation unbounded, and it can
  never be observed.

- **Fix:** `log.warning("response close failed for %s: %s", url, e)` in the inner
  `except`, or drop the inner `try` and let `close()` be best-effort under a
  `contextlib.suppress(Exception)` that still logs.

---

## The silent-failure catalogue, ranked by time-to-notice

This is the explicit question the lens asked. "Silent" here means: *the run exits
0, the CSVs are published, and nothing in the log, the exit code, the artifact, or
the run-over-run diff distinguishes the failure from normal operation.*

| # | Failure | How it presents | Time until a human notices | Anchor |
|---|---|---|---|---|
| 1 | **Total/partial network outage** (egress flap, runner IP banned, DNS blackhole) | `0 live / 0 dead / N unreachable`, exit 0, CSV published. Master keeps last week's verdicts so the diff is empty | **Weeks to never.** `unreachable` is expected to be non-zero, so it is not anomalous; nobody re-runs weekly CSVs to check a column that legitimately varies. Needs a hard threshold + non-zero exit to ever be caught | `enricher.py:176-178`, `:271`; `scrape.yml:74-83` |
| 2 | **Extraction silently broken** (regex edit, charset change, site template change) | Contacts line unchanged (it counts pre-existing values too); row counts unchanged | **Weeks.** The counter can't attribute anything to the fetcher, so a 100% extraction failure is invisible by construction | `enricher.py:267-270` |
| 3 | **WAF/UA regression turning healthy sites into `DEAD`** (`SCRAPER_EMAIL` unset → 403 → `+20` rebuild pitch) | More `dead`, more Tier-1 pitches — which *looks like a successful run* | **Days–weeks**, and it surfaces as a *sales* complaint ("I opened the site, it works") rather than an ops alert | `enricher.py:182-184`, `httpclient.py:81-85` |
| 4 | **200 KB truncation dropping all contacts** | `LIVE` with empty contacts; lead leaves `sales_ready.csv`; `website_live=True` makes it look healthy | **Days–weeks**, via a shrinking `sales_ready.csv` that nobody attributes to enrichment | `enricher.py:188`, `:222`, `main.py:202-205` |
| 5 | **Every site 429/503 mid-run** (no backoff, `Retry-After` ignored) | `dead` count spikes, run *speeds up* | **Days.** Only visible if someone is already staring at the dead/live ratio | `enricher.py:182-184`, `httpclient.py:194` |
| 6 | **Body read blows up after a successful status line** | `UNKNOWN` for that URL only | **Never, individually.** Indistinguishable from a network blip; only visible as a slow drift in `unreachable` | `enricher.py:190-192` |
| 7 | **`MemoryError` / `ValueError` inside `_fetch_website`** | `UNKNOWN`, all contacts `None` — same as a timeout | **Never.** A crash-in-disguise with no traceback anywhere | `enricher.py:176`, `:190` |
| 8 | **`r.close()` raising** | Verdict returned as if nothing happened | **Never** | `enricher.py:193-197` |
| 9 | **`TooManyRedirects`** (>30-hop loop) | `UNKNOWN` | **Never** | `enricher.py:176` |
| 10 | **Empty target list** | 0 bytes of output | **Never** | `enricher.py:232-233` |

Ranked inversely: the items at the bottom are individually invisible but bounded;
item 1 is the one that can silently halve the value of the dataset for months.

---

## Scenario answers the brief asked for

**Network drops halfway through the pass.** Nothing detects it. Each remaining URL
fails, the pool keeps draining, and `check_websites` produces a summary with a
plausibly-mixed ratio (`enricher.py:271`) that looks like a normal week. There is
no per-record fetch timestamp — `scraped_at` is the *source's* timestamp, set
upstream, so you cannot correlate `UNKNOWN` records with a time window. The run
then publishes: `main.py:209` writes `all_businesses.csv` containing both the good
half and the `UNKNOWN` half, and `scrape.yml:88-89` copies it to Drive. Because
`dedup._pick` treats `None` as missing (`dedup.py:86`, `:167-171`), previously
known `website_live` values survive the merge — so the outage leaves **no trace in
the data at all**, only a missing chunk of *newly* enriched leads (the `UNKNOWN`
half contributes no contacts). Recovery is manual: the only tool that could
re-fetch, `cmd_score` (`cli.py:247-276`), explicitly never touches the network.

**A dependency returns garbage.** Every shape I threw at it returned
`('unknown', all-None)` cleanly — `raw.read()` raising `OSError`, `r.raw is None`,
`status_code` being a string, a `close()` that raises. No exception escapes the
function and no traceback is printed. That is robust behaviour and it is also
the reason the function cannot report *why*: a real `MemoryError` and a missing
`requests` internals attribute are indistinguishable from a DNS timeout, and both
look like `unreachable` in the log. One genuine gap: nothing checks
`Content-Type`, so a binary body is fed to `raw.decode(..., errors="replace")`
(`enricher.py:189`) and to four regexes. That works (replacement chars, no crash)
but it is wasted work and, worse, it means an AI-generated or obfuscated page's
arbitrary bytes are scanned for `mailto:` patterns.

**Called with an empty list.** `_fetch_website` is never reached —
`check_websites` returns at `enricher.py:233`. Zero bytes of output, `records`
returned untouched and unmodified. `website_live` is not even set; that only
happens later at `enricher.py:346`. So an empty list is handled correctly and
reported not at all.

**When it is interrupted.** It is *not* interrupted — it drains. Measured:
`KeyboardInterrupt` in the main thread at t=0.30 s against 600 targets, and
**600/600 requests were still performed**. `ThreadPoolExecutor.__exit__` uses
`shutdown(wait=True, cancel_futures=False` (`enricher.py:237` has no alternative).
Then the process dies with nothing written (`main.py:209-213` is downstream of
`enrich()`) and **zero bytes of its 623-byte log flushed** — verified with
`os._exit(9)`: `stdout bytes surviving: 0`. From the operator's chair, a killed
run is indistinguishable from a run that never started enriching.

---

## Not a bug, but worth knowing

- **Prior audit `005-enricher-http-robustness.md` is substantially obsolete.**
  Its S1s (`status_code < 500` treated 4xx as live, blanket `verify=False`,
  `stream=False` unbounded buffering, shared process-global `Session`) are all
  fixed in the current code: `enricher.py:182` uses `>= 400`, `:175` uses the
  default `verify=True`, `:175` passes `stream=True` and `:188` caps the read,
  and `httpclient.py:58-71` makes sessions thread-local. In particular its S2
  about connection-pool thrashing under 40 threads no longer applies — with
  thread-local sessions each pool serves exactly one thread, so the default
  `pool_maxsize=10` is sufficient. Read that report as history, not as a
  work list.
- **What the current code got right, and should not be "simplified" away.**
  `LIVE`/`DEAD`/`UNKNOWN` are genuinely distinct (`enricher.py:154-156`), the
  `>= 400 → DEAD` boundary is right (modulo 429/503, above), `verify=True` is
  the default, the body is capped before decoding, sessions are thread-local, and
  the blanket `except Exception` at `enricher.py:176` is the right *shape* — it
  just needs to carry a reason.
- **`check_websites`'s own belt-and-braces works.** `enricher.py:244-248` catches
  anything escaping `_fetch_website` and substitutes `UNKNOWN`, so one bad worker
  genuinely cannot kill a six-hour run. That is the right instinct and it is also
  the last place a traceback could have surfaced — and it discards it.
- **DNS is unbounded and uncached.** `getaddrinfo` sits outside `timeout=8`
  **[not measured]**, and there is no negative-result cache, so a run that hits a
  resolver outage pays the resolver timeout per URL, 40 at a time.
  `005-enricher-http-robustness.md:339` reached the same conclusion.
- **`enricher.py:327-328` disables an `InsecureRequestWarning` that can no longer
  fire**, now that `verify=True` is the default. Dead code that will mislead the
  next reader into thinking TLS verification is still off.
- **The `website_live` → `lead_score` contract is the thing protecting the
  product.** `enricher.py:300-304` correctly gives `UNKNOWN` no points in either
  branch. Every finding above is about *detecting* that `UNKNOWN` was returned for
  the wrong reason — not about the scoring rule, which is sound.

---

## Recommended order of work

1. **Make the failure visible, then make it loud** (S1-#1). Return a `reason` from
   the transport and body-read paths (`enricher.py:176-178`, `:190-192`), `Counter`
   the reasons in `check_websites`, print the top 5, and **exit non-zero when
   `unknown / with_website` exceeds a threshold**. Without this, no other fix on
   this list can be verified, and every one of them is currently invisible.
2. **Give it a deadline and make it cancellable** (S1-#2, S1-#3).
   `timeout=(4, 8)` plus a `time.monotonic()` deadline inside the read loop;
   `pool.shutdown(wait=False, cancel_futures=True)` in a `finally`; `flush=True` on
   the four prints at `enricher.py:235`, `:261`, `:271`, `:273`. This is the
   difference between "the run was cancelled" and "the run vanished".
3. **Configure `logging`** (S2-#4). One `basicConfig` in `main()`, `log.debug`
   per fetch with URL + outcome + bytes + elapsed, `log.warning` with exception
   text on the transport path. Until this exists, #1 has nowhere to print to.
   Add a business-site egress probe to `cmd_doctor` (`cli.py:224-243`).
4. **Bound the regex work** (S2-#5). Add a `Content-Type` guard, cap the scanned
   slice at ~80 KB, and switch to a non-backtracking form or the `regex` module
   with `timeout=2`. Cheap, self-contained, and it removes the only unbounded CPU
   consumer in the function.
5. **Stop manufacturing pitches from rate limits** (S2-#6). Route through
   `fetch_with_retry`, add per-host serialization with a delay, and at minimum
   `if status in (429, 503): return UNKNOWN`. This one actively damages the
   product today.
6. **Record truncation and fix the counters** (S2-#7). Expose
   `truncated = bytes_read >= _MAX_BODY_BYTES`, treat it as `UNKNOWN`, count only
   fields the fetcher actually populated.
7. **Housekeeping** (S3s). One line at `enricher.py:233` for the empty case;
   time-based rather than count-based progress at `:261`; `close_session()` in
   `httpclient.py`; log the `close()` failure at `:195`; delete the dead
   `urllib3.disable_warnings` at `:328`.

Cross-references: `380-fn-enricher-py-_fetch_website-contract.md` (semantics of
the tri-state and the `DEAD` boundary), `382-fn-enricher-py-_fetch_website-deps.md`
(the `get_session` vs `fetch_with_retry` import),
`200-fn-cli-py-main-ops.md:153` (the `basicConfig` gap from the CLI side).