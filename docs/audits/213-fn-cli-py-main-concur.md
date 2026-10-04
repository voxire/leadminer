# 200 — Concurrency safety of `main()` at `cli.py:312`

## Verdict

`main()` itself is safe and reentrant — it holds no state, mutates nothing shared,
and correctly delegates all record mutation to a single thread — but the function
it dispatches to cannot be stopped. `cli.py:316-318` catches `KeyboardInterrupt`
around three `with ThreadPoolExecutor(...)` blocks that submit every task up front,
and `Executor.__exit__` calls `shutdown(wait=True)`, which joins every worker
thread before the exception can reach the handler. One Ctrl+C is absorbed for
tens of minutes (up to ~1 hour in the scrape phase, `pending × 8s / 40` in the
enrichment phase). The one place where genuine cross-thread corruption is
possible is not in `cli.py` at all: `write_csv` derives a *fixed* temp filename
(`main.py:122`), so two concurrent runs write the same inode and interleave their
bytes into the cumulative master.

---

## What is verified safe (asked explicitly; read this before "fixing" anything)

The lens asked about five things. Four of them are correct in this codebase, and
the reasons are specific enough that a future refactor should preserve them.

**1. No record dict is ever touched by two threads.** `check_websites` snapshots
`(index, url)` pairs at `enricher.py:231` and submits the **URL string**, not the
record, at `enricher.py:238`. Workers build and return a *separate* `contacts`
dict (`enricher.py:168`); every write into `records[idx]` happens in the
consuming thread inside the `as_completed` loop at `enricher.py:249-259`. The
future→index map is touched only by the submitting thread and the consuming
thread. `enrich()` mutates records before the pool (`enricher.py:330-335`) and
after it has exited (`enricher.py:339-350`). `main.py:183-197` runs entirely
after `enrich` has returned. There is no worker/main-thread record race.
`tests/test_lead_signal.py:292` (`test_merge_is_order_independent`) independently
corroborates the merge side.

**2. No aliasing between master and output records.** `dedup` never hands back a
caller's dict: it copies on insert (`dedup.py:210`, `dedup.py:218`) and `_merge`
builds a fresh dict (`dedup.py:191-194`). So `records = dedup(combined)`
(`main.py:180`) shares no object with `master`, and the in-place mutation in
`enrich` cannot write back into the loaded master. Had `dedup` returned input
dicts, `check_websites`' index-keyed writes would still not have raced (distinct
indices → distinct dicts), but this closes the loop.

**3. Session reuse is fixed.** `httpclient.get_session()` is backed by
`threading.local()` (`httpclient.py:58, 61-71`), so each of the 40 enrichment
workers (`enricher.py:175`), the OSM thread (`osm.py:35`) and the Wikidata thread
(`wikidata.py:73`) gets its own `requests.Session`. `GooglePlacesScraper` builds
one per query inside the worker (`google_places.py:189`). There is no shared
`Session` anywhere. Audit `004-enricher-thread-safety.md` is **stale** — it
describes a module-level `_SESSION` and cites line numbers from a pre-rewrite
`enricher.py`; `docs/audits/004-enricher-thread-safety.md:11` ("Every worker uses
the same Session") no longer describes the code. Do not action it as written.

**4. `_pick` is a commutative, deterministic choice function.** The obvious
worry — that `raw.extend(batch)` in `as_completed` order (`main.py:166`) makes
merge results depend on which scraper finished first — is **not** a bug.
`dedup._pick` ranks on `(trust, validity, recency, str(value))`
(`dedup.py:179-187`); with `a` and `b` swapped the winner is unchanged, and the
final `str(value)` tiebreak makes even a full tie deterministic. The
`_trust_for` / `_recency` lookups read the *merged* record's union of sources and
`max(scraped_at)`, both order-independent. Field values do not flap.

**5. Memory visibility is guaranteed.** `future.result()` on a completed future
blocks on the future's condition variable, so every write a worker made to its
returned `contacts` is visible to the consumer. No torn reads are possible under
CPython's GIL, and none are relied upon.

---

## Findings

### S1 — Fixed temp filename lets two concurrent runs interleave bytes into the cumulative master

- **Where:** `main.py:122` (`tmp = path.with_suffix(path.suffix + ".tmp")`),
  `main.py:124` (`open(tmp, "w", ...)`), `main.py:130` (`os.replace(tmp, path)`);
  no mutual exclusion anywhere in `cli.py:312-318`.
- **Breaks:** the temp name is derived from the target, not from the process, so
  two writers open the **same inode**. Sequence, both processes in
  `leadminer run` / `python main.py` on one host:
  1. A opens `data/all_businesses.csv.tmp` `O_TRUNC`, writes 12 MB of 30 MB
     (buffered `csv.writer` flushes in blocks).
  2. B opens the same path `O_TRUNC` — same inode, truncated to 0 — writes its
     own 30 MB, `fsync`s, and `os.replace`s it onto `all_businesses.csv`.
  3. A resumes at file offset 12 MB **inside the inode that is now
     `all_businesses.csv`** and overwrites B's bytes from the middle onward.
  4. A's `os.replace(tmp, path)` raises `FileNotFoundError` (B consumed the tmp
     name) — an unhandled crash at `main.py:130`, *after* the damage.
  Result: a CSV whose middle third is A's rows spliced into B's, with the tail
  intact. The workflow's `wc -l` gate
  (`.github/workflows/scrape.yml:74-84`) still passes. This is silent corruption
  of the product, and it is exactly the failure mode `docs/audits/051-atomic-writes-and-concurrency.md:109-119`
  said a `flock` would prevent — but the *mechanism* 051 did not identify is the
  non-unique temp name, which turns "last writer wins" into byte splicing.
- **Trigger:** `leadminer run` in one terminal and `python main.py` in another on
  the same checkout, both reaching `main.py:209`. Plausible here because the
  workflow itself invokes `python main.py` (`.github/workflows/scrape.yml:69`),
  not the CLI, so operators mix both. `concurrency: group: leadminer-scrape`
  (`.github/workflows/scrape.yml:15-17`) serialises **CI only** — nothing guards a
  local host, and `cli.py:312` has no lock, no PID file, and no `--force`.
- **Fix:** two lines, in this order. (a) `main.py:122` → use
  `tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.tmp.")` so the name is
  unique per process — this alone removes the splicing. (b) Add an `flock` on
  `data/.leadminer.lock` acquired at the top of `main()` (`main.py:148`) so two
  runs fail fast instead of both burning API quota. Note that
  `tests/test_lead_signal.py:178` asserts the *current* deterministic name
  (`p.with_suffix(".csv.tmp")`) and must be updated with (a) — the test currently
  locks in the vulnerability.

### S2 — Ctrl+C cannot stop a run; `cli.py`'s `KeyboardInterrupt` handler is unreachable for tens of minutes

- **Where:** `cli.py:314-318` (the handler), wrapping `main.py:160`,
  `enricher.py:237` and `google_places.py:194`.
- **Breaks:** all three pools are `with ThreadPoolExecutor(...)` blocks that
  submit **every** task up front (`main.py:161`, `enricher.py:238`,
  `google_places.py:195`). `Executor.__exit__` calls `shutdown(wait=True)`, whose
  body is `for t in self._threads: t.join()` (CPython
  `concurrent/futures/thread.py`), and which does **not** cancel queued work — the
  workers keep draining the queue until it is empty. So a `KeyboardInterrupt`
  raised in the main thread is not delivered to `cli.py:316` until every queued
  task has completed. Nothing downstream checks a cancellation flag either:
  `httpclient.fetch_with_retry` loops unconditionally (`httpclient.py:170-206`)
  and sleeps at `httpclient.py:206`; the 429 path sleeps up to 60 s per attempt
  (`google_places.py:241-246`).
- **Trigger — enrichment phase.** 30,000 records with a website, Ctrl+C five
  minutes in (~1,500 completed at 40 workers × 8 s `timeout=8`,
  `enricher.py:175`): remaining 28,500 × 8 s / 40 ≈ **95 minutes** of silent
  wall-clock before `interrupted` is printed at `cli.py:317`.
- **Trigger — scrape phase.** OSM is one `fetch_with_retry` at `timeout=200`,
  `retries=3` (`osm.py:39-46`) → ~660 s uninterruptible. Wikidata is 90 s × 3
  (`wikidata.py:70-78`) → ~330 s. Google is the worst: 68 queries
  (`google_places.py:174`) over 5 workers, each with a 180 s budget
  (`google_places.py:152, 217`) that is only checked *before* each request, so one
  query can overrun by a 30 s timeout plus a 60 s backoff ≈ 270 s → 68 / 5 × 270 s
  ≈ **61 minutes**. The outer pool at `main.py:160` waits on the Google thread,
  which is itself waiting on the inner pool's `__exit__`.
- **Escape requires SIGTERM or SIGKILL.** A second Ctrl+C may break out of
  `t.join()` (the main thread's lock acquire is signal-interruptible), but
  `threading._register_atexit(_python_exit)` re-joins every registered worker at
  interpreter shutdown (`concurrent/futures/thread.py`), and any surviving worker
  still runs its queued item. Practically, the operator's only prompt exit is
  `kill -9`. On GitHub Actions this means a cancelled run burns its 10 s grace
  period and is SIGKILLed — which, combined with the split-state risk in
  `docs/audits/051-atomic-writes-and-concurrency.md:17-21`, can leave 4 new CSVs
  beside 1 old one.
- **Fix:** hold the pools as named variables, register
  `signal.signal(SIGINT, lambda *_: pool.shutdown(wait=False, cancel_futures=True))`
  in `cli.main()` (`cli.py:313`) before dispatching, and pass a `threading.Event`
  into `fetch_with_retry` and the 429 backoff so in-flight sleeps end promptly.
  Second-best, if you do not want signal plumbing: switch
  `enricher.py:238` from one `submit` per URL to a bounded submission window
  (e.g. `ThreadPoolExecutor.map` with an explicit iterator drained in chunks of
  `workers * 4`), which bounds both cancellability and the future-object memory
  for a 30k-row run.

### S2 — `leadminer run` exposes no concurrency control and always exits 0

- **Where:** `cli.py:33-37` (`cmd_run` accepts `argparse.Namespace` and reads
  nothing from it), `cli.py:285-286` (`run` subparser declares no options),
  `enricher.py:225` (`workers: int = 40`, called with the default at
  `enricher.py:337`), `google_places.py:142` (`_WORKERS = 5`, a module constant),
  `main.py:160` (`max_workers=len(scrapers)`), `main.py:167-168` (every scraper
  exception is caught and the run continues), `cli.py:315` (`int(args.func(args) or 0)`).
- **Breaks:** fan-out is hardcoded in three separate modules and cannot be tuned
  from the command line, so an operator watching `leadminer run` collect 429s
  from Google or get blocked by a host has no lever except killing the process —
  which per the finding above takes an hour. `cmd_run`'s `args` parameter is the
  natural plumbing point and is currently dead. Separately, `pipeline.main()`
  returns `None`, so `int(None or 0)` is always `0`: **every** `leadminer run`
  exits 0, including when all three scrapers failed (Overpass 504s are routine)
  and the run republished a master containing nothing new.
- **Trigger:** unset `GOOGLE_PLACES_API_KEY` and run `leadminer run`; you get
  `[Google] GOOGLE_PLACES_API_KEY not set, skipping.` (`google_places.py:170`),
  `leadminer run` writes 5 CSVs, `echo $?` → `0`.
- **Fix:** add `--workers` (enrichment) and `--places-workers` to the `run`
  subparser and have `cmd_run` forward them into `enrich(records, workers=...)` and
  an env override for `google_places._WORKERS`; make `main.main()` return a count
  of failed sources and have `cmd_run` map `> 0` to exit 1.

### S2 — Google Places marks a place "seen" before checking it is usable, so a nameless place is dropped forever

- **Where:** `google_places.py:181-182` (`seen_ids`, `seen_lock`),
  `google_places.py:263-270`.
- **Breaks:** the lock itself is correct — check-and-add at
  `google_places.py:263-266` is atomic, so there is no double-count of a place id
  across the 5 workers. The bug is the *order of operations inside the critical
  section*: `seen_ids.add(place_id)` (line 266) executes **before** the
  `if not name: continue` at lines 268-270. A place whose `displayName` is
  missing or has no `text` is therefore burned: whichever query touches it first
  consumes the id, and no later query can recover it, in this run or any future
  one. Under 5 concurrent queries over 68 near-duplicate query strings
  (`"restaurants in Lebanon"` vs `"cafes in Lebanon"`), a place can be reached by
  several queries, so the ordering determines which query claims it.
- **Trigger:** a Places response containing `{"id": "ChIJ...", "location": {...}}`
  with no `displayName` — e.g. after a FIELD_MASK or API-shape change — is
  silently discarded by all queries; the run reports
  `[GooglePlacesScraper] collected N records` with N quietly short, and the
  workflow's `ROWS >= 100` check (`.github/workflows/scrape.yml:79-84`) still
  passes because the cumulative master carries the count.
- **Fix:** move `seen_ids.add(place_id)` to after the name check (i.e. build the
  record first, then claim the id under the lock, discarding if already claimed),
  and log a per-source dropped-for-missing-name counter so a shape change is
  visible instead of silent.

### S3 — Output row order is completion-ordered, so identical inputs produce different CSVs

- **Where:** `main.py:162-166` (`as_completed` → `raw.extend(batch)`),
  `google_places.py:190-192` (`all_records.extend(results)` on query completion),
  `dedup.py:222-232` (`phone_records + name_records`, built from dict insertion
  order), `main.py:209-213`.
- **Breaks:** the *content* is order-independent (see "What is verified safe" #4),
  but the *order of rows* is not: whichever of the 3 scrapers, and whichever of
  the 68 Google queries, finishes first lands first in `raw`, and `dedup` emits
  records in dict-insertion order. Two runs over identical upstream data produce
  byte-different `all_businesses.csv`, so `rclone copy` churns, a diff-based
  review of "what changed this week" is impossible, and any consumer that reads
  row position (a spreadsheet sorted by hand, a de-dupe by index) silently sees
  different businesses each run.
- **Trigger:** run `leadminer run` twice and `diff` the two
  `data/all_businesses.csv` files.
- **Fix:** sort `records` on a stable key before writing — e.g.
  `records.sort(key=lambda r: (str(r.get("country")), str(r.get("name")), str(r.get("phone"))))`
  at `main.py:208`, and apply the same key to the four derived subsets.

---

## Not a bug, but worth knowing

- **Sessions are never closed.** `httpclient.get_session()` (`httpclient.py:61-71`)
  and `GooglePlacesScraper._make_session()` (`google_places.py:159-166`) create 40
  + 3 + 68 `requests.Session` objects and none is ever `.close()`d. Under CPython
  refcounting this is collected when the worker thread's `threading.local` entry
  dies at pool shutdown, but it is implicit. On a 40-worker run it can hold up to
  40 × `pool_maxsize` (10) idle keep-alive sockets for the duration of
  enrichment — worth knowing on macOS, where `ulimit -n` defaults to 256. An
  explicit `atexit`/`finally` that closes each session is cheap insurance.
- **Partial body reads forfeit connection reuse.** `enricher.py:188` caps the read
  at 200 KB and `enricher.py:195` closes the response; for any site with a body
  larger than the cap urllib3 closes the socket instead of returning it to the
  pool, so every large site costs a fresh TCP+TLS handshake. Not a correctness
  issue, but it multiplies load on exactly the hosts most likely to rate-limit.
- **`enricher.py:328` mutates process-global state.** `urllib3.disable_warnings(
  InsecureRequestWarning)` is idempotent, so it is reentrancy-safe, but it is now
  pointless (TLS verification was restored at `enricher.py:169-175`) and it
  suppresses the warning for any *other* insecure request in the process. Remove
  it.
- **Two `DATA_DIR` constants.** `cli.py:26` and `main.py:41` each define
  `pathlib.Path("data")`. They are equal and both resolve lazily against the CWD,
  so `cmd_doctor` (`cli.py:217`) and `main.main()` agree today. They would
  silently diverge the moment anything calls `os.chdir`. Have `cli` import
  `DATA_DIR` from `main` rather than redeclaring it.
- **`import main as pipeline` (`cli.py:35`) resolves by bare module name.** The
  wheel installs a top-level module literally called `main` (see
  `pyproject.toml` `[project.scripts] leadminer = "cli:main"`), so another
  distribution shipping a `main.py` can shadow it. The deferred, in-function
  import is also what keeps `cli.main()` reentrant (the module is cached in
  `sys.modules` after the first call), which is why this has never bitten.
- **`docs/audits/004-enricher-thread-safety.md` and
  `docs/audits/051-atomic-writes-and-concurrency.md` are stale.** Both cite
  pre-rewrite line numbers and describe code that no longer exists (the shared
  `_SESSION`; `open(path, "w")` without fsync; a workflow with no `concurrency`
  block). Re-derive before implementing either. This report supersedes 004 for
  the shared-session finding, which is now genuinely fixed.
- **Nothing in the 329-line test suite touches concurrency.** `tests/` contains
  one atomic-write test (`tests/test_lead_signal.py:157`) and it is single-process
  by construction. Every finding above is uncovered by construction, and the S1
  in particular needs a two-process test to be caught.

---

## Recommended order of work

1. **Unique temp filename** (`main.py:122`) — one line, removes the only
   cross-process corruption path. Update `tests/test_lead_signal.py:178` with it.
2. **`flock` on `data/.leadminer.lock`** at `main.py:148` — makes a second local
   run fail fast instead of splicing.
3. **Cancellable pools** — hold the three executors as named objects, install a
   `SIGINT` handler in `cli.main()` (`cli.py:313`) that calls
   `shutdown(wait=False, cancel_futures=True)`, and thread a `threading.Event`
   through `fetch_with_retry` and the Google backoff.
4. **`--workers` plumbing** through `cmd_run` (`cli.py:33-37`), plus a non-zero
   exit when any source failed.
5. **Move `seen_ids.add` after the name check** (`google_places.py:263-270`) and
   count the drops.
6. **Deterministic output ordering** (`main.py:208`).
