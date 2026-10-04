# 200 — `cli.main()` read as a six-hour cron process

**Target:** `cli.py:312-318` (plus its caller `cli.py:322`)
**Lens:** production operations — what it prints, what it swallows, and every
silent failure mode, ranked by how long a human takes to notice.

## Verdict

`main()` is a six-line dispatcher whose entire operational contract is one
`except KeyboardInterrupt`. It has **no channel for reporting a degraded run**:
because `pipeline.main()` returns `None` (`main.py:148`) and `cli.py:315` does
`int(args.func(args) or 0)`, `leadminer run` exits `0` after a run in which all
three scrapers failed, and the only trace is one stderr line per source near the
top of a six-hour log. Worse, the single exception it does handle is the one
signal the production environment never sends — GitHub Actions enforces
`timeout-minutes: 300` with **SIGTERM**, which is not a `KeyboardInterrupt`, so
the `\ninterrupted` message at `cli.py:317` is unreachable exactly when you need
it, and the progress log that would tell you how far the run got is sitting in an
unflushed 8 KB stdout buffer (`enricher.py:262` is the one heartbeat that omits
`flush=True`, while `osm.py:54` gets it right). Compounding this: enrichment
cost grows linearly with the cumulative master while the timeout is a constant,
so the pipeline is *mathematically guaranteed* to start dying at the wall it
cannot see.

**The most important thing:** the function cannot distinguish "succeeded" from
"succeeded at getting nothing", and the wall it eventually hits is invisible.

---

## Findings

### S1 — `or 0` erases the failure channel: a run where every source died exits 0

- **Where:** `cli.py:315` (`return int(args.func(args) or 0)`), feeding from
  `cli.py:37` (`return pipeline.main()`), whose signature is
  `def main() -> None` at `main.py:148`.
- **Breaks:** `cmd_run` returns `None`. `None or 0` → `0`. This is the *only*
  reason the command exits successfully, and it means the CLI is structurally
  incapable of reporting a degraded run — there is no return value on
  `main.main()` for a degraded run to travel through, and `or 0` would convert
  it to success if there were. Meanwhile the underlying sources swallow their own
  failures and never raise: `osm.py:47-56` prints `FAILED after N attempts` and
  `return`s; `google_places.py:228-233` prints `FATAL: invalid API key (401)`
  and returns empty; `google_places.py:250-252` catches
  `requests.RequestException` and returns partial records. The last line of
  defence is `main.py:167-168`, which catches `Exception` and `print`s to stderr.
  So: bad DNS → 3 stderr lines → `raw = []` → `combined = [] + master`
  (`main.py:179`) → dedup → enrich → **all 5 CSVs rewritten** (`main.py:209-213`)
  → a complete, normal-looking `Summary:` block (`main.py:219-226`) → exit `0`.
- **Trigger:** unset `GOOGLE_PLACES_API_KEY`, point `OVERPASS_URL` at a
  blackholed host, and run `leadminer run`. Observe `$? == 0`, five freshly
  timestamped CSVs on disk, and a `SALES-READY (actionable): N` line that is
  just last week's N. Cron sends no mail for exit 0; the run is green.
- **Concrete detection gap:** the workflow's own guard
  (`.github/workflows/scrape.yml`, "Validate output before publishing") checks
  `ROWS >= 100` on the *cumulative* master, which is still last week's file. A
  total source outage passes that check, because the master was never truncated.
- **Fix:** give `main.main()` an explicit return type (`-> int`, `0` ok / `1`
  degraded) driven by a per-source success count, drop the `or 0`, and make
  `cmd_run` propagate it verbatim.

### S1 — SIGTERM is not handled; the one handler present is unreachable in production

- **Where:** `cli.py:316-318`. `KeyboardInterrupt` is raised by CPython only on
  **SIGINT**. SIGTERM's default disposition terminates the process without
  raising anything catchable.
- **Breaks:** the job is bounded by `timeout-minutes: 300` in
  `.github/workflows/scrape.yml`. GitHub Actions sends SIGTERM at the timeout,
  then SIGKILL ~after a grace period. Neither raises `KeyboardInterrupt`, so:
  the `\ninterrupted` at `cli.py:317` **never prints**; the `return 130` never
  happens (the shell observes 143 from the signal); and `write_csv`'s
  `except BaseException: tmp.unlink(); raise` guard (`main.py:131-133`) does not
  run, because there is no exception to guard — leaving an orphaned
  `data/all_businesses.csv.tmp` behind. `concurrency: cancel-in-progress: false`
  means SIGINT is never sent either, so in CI the handler has literally zero
  coverage.
- **Trigger:** `timeout-minutes: 1` on a run, or `kill -TERM $(pgrep -f leadminer)`
  at minute 40. Zero output from `main()`; exit 143; the log's last line is
  whatever survived stdout buffering.
- **Fix:** install a SIGTERM handler that raises a dedicated exception (or sets a
  cooperative cancellation flag the pools check), and treat it identically to
  SIGINT.

### S1 — Cost grows linearly with the cumulative master against a fixed 300-minute wall

- **Where:** `main.py:150` (loads the cumulative master), `main.py:179`
  (merges it into the run), `enricher.py:225` (`check_websites(records,
  workers=40)`), `enricher.py:237` (40-thread pool), `enricher.py:175`
  (`timeout=8`), vs `timeout-minutes: 300` in the workflow.
- **Breaks:** `enrich()` at `enricher.py:337` calls `check_websites(records)`
  with no args, so every run re-fetches a website for **every record ever
  accumulated**, not just the new ones. `_fetch_website` issues one
  `get_session().get(url, timeout=8)` — note it bypasses
  `httpclient.fetch_with_retry` entirely, so none of the centralised
  backoff/jitter retry logic applies to the dominant cost centre. Worst-case
  enrichment wall time is `ceil(N/40) × 8s`:

  | master rows `N` | worst-case enrichment |
  |---|---|
  | 20,000 | ~1.1 h |
  | 50,000 | ~2.8 h |
  | 100,000 | ~5.6 h — **over the 300-min job timeout on its own** |

  `N` is monotonically non-decreasing across runs and the timeout is a constant,
  so the pipeline is *guaranteed* to cross the wall within a handful of weekly
  runs — and per the previous finding, it crosses it silently.
- **Trigger:** leave the job running for several weeks without noticing; the run
  that finally exceeds 300 min dies at SIGTERM with an unflushed heartbeat
  (`enricher.py:262`) and a `data/*.csv.tmp` remnant, and the master on Drive is
  one week stale because the upload step never runs.
- **Fix:** make the job timeout a function of the master size (or cap enrichment
  per run to a time budget), and cache liveness per URL with a TTL so unchanged
  sites are not re-fetched every week.

### S2 — Ctrl-C does not return: the interrupt is swallowed by the executor drain

- **Where:** `cli.py:316-318`, reached via `enricher.py:237`
  (`with ThreadPoolExecutor(max_workers=workers) as pool`).
- **Breaks:** `ThreadPoolExecutor.__exit__` calls `shutdown(wait=True)`, which
  pushes a `None` sentinel onto the **tail** of the work queue and then joins
  every worker thread. Every already-submitted future was queued ahead of that
  sentinel, so **shutdown drains the entire queue rather than cancelling it**.
  A Ctrl-C at 10% through enrichment therefore continues fetching the remaining
  90% — at `N=50,000`, 40 workers, `timeout=8`, that is up to
  `(45000/40) × 8s ≈ 2.5 hours` of work performed *after* the operator asked to
  stop. The `\ninterrupted` message is printed only after that, because it sits
  in `main()` and the entire `main.py` stack — including both `with` blocks —
  must unwind first. Only a *second* Ctrl-C (raised during the join) escapes and
  reaches `cli.py:316`.
- **Trigger:** start `leadminer run`, wait for `[Enricher] Fetching N websites`,
  press Ctrl-C once. The process appears hung with no output for hours.
- **Fix:** pass `cancel_futures=True` where the pool is constructed, and/or set a
  module-level cancellation flag that `_fetch_website` checks before returning.

### S2 — The progress heartbeat is the one line that is not flushed

- **Where:** `enricher.py:262` (`print(f"[Enricher] {done}/{len(targets)} done...")`),
  and every `print` in `main.py:134,152,165,170,174,181,186,219-242`.
- **Breaks:** stdout is block-buffered (~8 KB) whenever it is redirected to a file
  or pipe, and neither the workflow nor `cli.py` sets `PYTHONUNBUFFERED` or calls
  `sys.stdout.reconfigure(line_buffering=True)`. The codebase clearly knows the
  pattern — `osm.py:54`, `wikidata.py:83`, `google_places.py:232` and `:239` all
  pass `flush=True` — but the `[Enricher] N/M done` line, which is the *only*
  progress signal during the multi-hour phase, omits it. Combined with S1-SIGTERM,
  the last partial buffer of the log is discarded on kill, so the one thing you
  need to triage a timeout ("was it at 12,000 or 48,000 of 50,000?") is the thing
  that got truncated.
- **Trigger:** `leadminer run > run.log 2>&1`, then SIGKILL at minute 200.
  `tail run.log` shows a `done...` count up to 8 KB behind the truth.
- **Fix:** `flush=True` on `enricher.py:262` and the `main.py` progress prints,
  or set `PYTHONUNBUFFERED=1` in the job env.

### S2 — No `logging.basicConfig`, so the entire retry/telemetry layer emits nothing

- **Where:** absence in `cli.py:312-318`; the orphaned loggers are
  `httpclient.py:45`, `scrapers/osm.py:7`, `scrapers/wikidata.py:7`.
- **Breaks:** `httpclient.py` was written specifically so failures stop being
  indistinguishable from emptiness ("Failures were indistinguishable. Everything
  was `print` then a silent `return`", `httpclient.py:17-19`). But its two
  diagnostic calls — `log.debug("... attempt %d/%d transport error", ...)` at
  `httpclient.py:178` and the retry/backoff notice at `httpclient.py:204` — go
  to a logger with no handler anywhere in the codebase. In production they
  produce **zero bytes**. The same is true of the `osm.py` and `wikidata.py`
  loggers. So the machinery that would tell you a request was retried three times
  with 60 s of backoff versus failing once is completely inert, and `main()` —
  the one function that owns process setup — never configures it.
- **Trigger:** any run with a transient network blip. The log shows a source
  simply having fewer records; nothing distinguishes a 2 s blip from a 4-hour
  outage.
- **Fix:** `logging.basicConfig(level=os.environ.get("LEADMINER_LOGLEVEL", "INFO"),
  format="%(asctime)s %(levelname)s %(name)s %(message)s")` in `main()` before
  dispatch.

### S2 — `cmd_doctor` exits 0 with 3/3 connectivity failures, and never checks the credential it guards

- **Where:** `cli.py:238-244`, specifically the bare `except Exception` at
  `cli.py:242-243` and `return 0 if ok else 1` at `cli.py:244`, where `ok` is
  only ever cleared at `cli.py:213-214`.
- **Breaks:** two separate defects compound.
  1. Every connectivity exception is caught and printed as `FAIL`, but `ok` is
     **never set to `False`**. `leadminer doctor` reporting
     `FAIL Overpass SSLError` / `FAIL Wikidata ConnectionError` /
     `FAIL Google Places ConnectTimeout` still exits `0`.
  2. The "credential" check at `cli.py:210-213` is `bool(os.environ.get(var))` —
     it only tests that the string is non-empty. A revoked, truncated, or
     mistyped key passes `doctor` and then produces a 401 on all 68 queries at
     run time (`google_places.py:228`), which returns zero records.
  Also note `cli.py:240`: `note = "" if r.status_code < 500 else ...` combined
  with `requests.get` at `cli.py:239` against `places.googleapis.com/v1/places:searchText`,
  a **POST-only** endpoint. The probe reliably gets a 4xx and prints `OK`.
- **Trigger:** `GOOGLE_PLACES_API_KEY=deadbeef leadminer doctor; echo $?` → `0`.
  Then `GOOGLE_PLACES_API_KEY=deadbeef leadminer run` → 5 hours, exit `0`, no
  Google rows.
- **Fix:** set `ok = False` in the exception branch, make a real authenticated
  request (or at minimum a correctly-methed probe) for the credential, and
  return non-zero on any `FAIL`/`WARN`.

### S2 — `int(args.func(args) or 0)` also converts `False` and `""` to success

- **Where:** `cli.py:315`.
- **Breaks:** distinct from S1 but the same line. `or` treats `0`, `False`, `""`,
  `None` and `[]` identically. Any subcommand that returns `False` on a failure
  path — a natural thing to write, and `cmd_run` already does return a falsy
  value — is reported to cron as success. The line converts every falsy failure
  signal into exit 0 rather than asserting a strict `int` contract.
- **Trigger:** add `return False` to any `cmd_*` failure branch. Cron sees 0.
- **Fix:** `rc = args.func(args); return rc if isinstance(rc, int) else 0` — or
  better, assert the return type so a `None` is a loud error.

### S3 — Production never calls this function

- **Where:** `.github/workflows/scrape.yml` runs `python main.py`, reaching
  `main.py:245-246` (`if __name__ == "__main__": main()`). The `cli.py`
  entrypoint exists only as `[project.scripts] leadminer = "cli:main"` in
  `pyproject.toml:33`.
- **Breaks:** the audited function has **zero production coverage**. The two
  entrypoints differ in observable ways — `cli.py:322` does
  `raise SystemExit(main())` while `main.py:246` discards the return value
  entirely — so any fix applied to `cli.py` will not change cron behaviour until
  the workflow is switched over. An audit that only reads `cli.py` will
  mis-predict production.
- **Trigger:** Fix `cli.py:316` to handle SIGTERM. Re-run the workflow. Same
  timeout, same silence.
- **Fix:** change the workflow step to `leadminer run` (or `python -m cli run`)
  as part of the same change, and delete one of the two entrypoints.

### S3 — `parse_args` sits outside the `try`, so `main()` is not library-safe

- **Where:** `cli.py:313` precedes the `try:` at `cli.py:314`.
- **Breaks:** `parse_args` raises `SystemExit` on bad input and on `--help`.
  `SystemExit` derives from `BaseException`, so it would not be caught even if it
  were inside the `try`. Consequence: `rc = cli.main(["--help"])` raises instead
  of returning `0`, and `cli.main(["stats", "typo.csv"])` raises `SystemExit(2)`
  instead of returning `2`. Any wrapper, test, or `scripts/` caller that treats
  `main()` as a function gets an exception, not a return code. This is correct
  for a top-level CLI but is unstated, and `pyproject.toml:33` exposes `main` as
  a public entrypoint.
- **Trigger:** `python -c "import cli; print(cli.main(['--help']))"` →
  `SystemExit: 0` instead of printing `0`.
- **Fix:** document the contract, or wrap the parse in its own
  `except SystemExit as e: return e.code`.

### S3 — `BrokenPipeError` produces a traceback plus a shutdown-time "Exception ignored"

- **Where:** `cli.py:315` — nothing catches `OSError`.
- **Breaks:** every subcommand writes substantial output to stdout
  (`cmd_stats` alone prints ~40 lines). `leadminer stats data/all_businesses.csv | head -5`
  raises `BrokenPipeError` out of `print`, which is an `OSError` and not a
  `KeyboardInterrupt`, so it escapes as an unhandled traceback; CPython then emits
  a second, uglier `Exception ignored in: <_io.TextIOWrapper name='<stdout>' ...>`
  at interpreter shutdown. Same for `leadminer run | tee` to a full disk. Exit
  code becomes 1 for what the operator experiences as normal pipeline behaviour.
- **Trigger:** `leadminer stats data/all_businesses.csv | head -5`.
- **Fix:** catch `BrokenPipeError` in `main()`, redirect stdout to `os.devnull`,
  and `sys.exit(0)` — the standard recipe.

### S3 — `DATA_DIR` is relative and duplicated in two modules

- **Where:** `cli.py:26` and `main.py:41`, both `pathlib.Path("data")`.
- **Breaks:** output location depends entirely on the process working directory.
  A crontab entry `30 3 * * 1 /usr/local/bin/leadminer run` with no `cd` writes
  to `$HOME/data/`. The run exits `0`, the five CSVs are correct, and they are
  in the wrong directory — so the master never accumulates and every run
  re-scrapes from zero. `cmd_doctor`'s `DATA_DIR.exists()` check at
  `cli.py:217` inspects a *different* directory than the one `cmd_run` writes to,
  so `doctor` can report `MISSING data` on a host where runs are silently
  succeeding elsewhere (and vice versa: `doctor` reports `OK` against a stale
  `$PWD/data/`).
- **Trigger:** `cd / && leadminer run` — succeeds, writes `/data/`.
- **Fix:** resolve `DATA_DIR` from an env var with a default anchored to the
  package or platform config dir, not `Path("data")`.

### S3 — `main.py:168` can print an error message with no reason in it

- **Where:** `main.py:168` — `print(f"[{...}] ERROR: {e}", file=sys.stderr)`.
- **Breaks:** `str(e)` is empty for `KeyError()`, `IndexError()`, and
  `ZeroDivisionError()`. The log line becomes `[OSMScraper] ERROR: ` with a
  trailing space and nothing after it. This is the only surviving evidence of
  the failure, so the one case you most need to diagnose is the one that reads as
  blank.
- **Trigger:** a `KeyError()` raised inside a scraper yields `ERROR: ` and no
  way to tell which key.
- **Fix:** `traceback.format_exc()` or `repr(e)`, not `str(e)`.

### S3 — SIGTERM leaves an orphaned `.csv.tmp` because `except BaseException` does not cover signals

- **Where:** `main.py:122-133` — the cleanup guard is exception-based only.
- **Breaks:** a SIGTERM landing between `f.flush()`/`os.fsync()` at
  `main.py:128-129` and `os.replace(tmp, path)` at `main.py:130` terminates the
  process with no exception, so `tmp.unlink()` at `main.py:132` never runs. The
  workflow's artifact step uploads `data/` with `if: always()`, so the partial
  file is archived with the run. Not a correctness bug (the fixed `.tmp` name is
  reused and the real master is only ever replaced atomically), but it is a
  stray multi-hundred-MB file in the artifact on exactly the runs that already
  went wrong.
- **Trigger:** SIGTERM during the 5-CSV write block at `main.py:209-213`.
- **Fix:** sweep stale `*.csv.tmp` at startup; make the guard
  `try/finally` with an `os.replace` in the success path.

### S3 — `cmd_score` defaults to overwriting the master, irreversibly

- **Where:** `cli.py:274` — `out = pathlib.Path(args.out) if args.out else path`.
- **Breaks:** `leadminer score` with no `--out` rewrites
  `data/all_businesses.csv` in place, replacing every `lead_score`,
  `industry_priority`, and `recommended_service` with values from the *current*
  rules. The write is atomic (`main.py:130`) so there is no corruption, but there
  is no snapshot and no `--dry-run`: the previous scores — the ones a sales rep
  may already have worked from — are gone. `cli.py:276` reports only a count of
  how many changed, not what they were. The destructive path is the default path.
- **Trigger:** `leadminer score` after a scoring-rule change, then decide you
  want to compare against last month's scores.
- **Fix:** make `--out` required (or default to `path.with_suffix('.rescored.csv')`),
  and add `--dry-run` that prints a before/after sample.

---

## Silent-failure ranking — how long until a human notices

Ordered by **time to detection**, longest first. "Silent" = exit code 0 and/or no
output the operator will read.

| # | Failure | Signal | Time to notice |
|---|---|---|---|
| 1 | **All 3 sources dead** (bad DNS, ban, revoked key). `main.py:167` prints 3 stderr lines; 5 CSVs rewritten from the existing master; `Summary:` looks normal; exit `0`. | 3 lines at the very top of a 6 h log | **Weeks–months.** Only when a rep calls to say "you already sent me this lead". The workflow's `ROWS >= 100` guard cannot catch it: the master is cumulative and was never truncated. |
| 2 | **One source dead** (Google 401 at `google_places.py:228`, OSM rate-limit at `osm.py:47`). Half the new leads, silently. `FATAL: invalid API key` is printed with `flush=True` but still exits `0`. | 1 flushed line | **Weeks.** Detectable only by diffing row counts per `source` across weeks — which `cmd_stats` *does* print (`cli.py:119-121`), but nobody diffs. |
| 3 | **Retry/backoff telemetry entirely absent** — `httpclient.py:178,204` log to unconfigured loggers. | none at all | **Weeks.** You can never distinguish a 2 s blip from a 4 h outage, so you will never calibrate when to worry. |
| 4 | **Wrong CWD** — `DATA_DIR = Path("data")` at `cli.py:26` / `main.py:41`. Master never accumulates; every run re-scrapes from zero. | none | **Weeks–months.** `doctor` inspects a different directory and lies either way. |
| 5 | **SIGTERM at the 300-min timeout.** No `interrupted`, exit 143, unflushed heartbeat lost, orphaned `.tmp`, stale Drive master. | exit code only | **Days** — *if* anyone watches for non-zero exits. Invisible to a `>> log` cron entry; the log's last line understates progress. |
| 6 | **Network drops mid-pagination.** `google_places.py:250-252` returns partial records per query; the diagnostic `print` at `:251` lacks `flush=True`. Up to 68 queries degrade independently. | up to 68 buffered lines | **Days.** Only visible as a slow drop in new rows. |
| 7 | **`doctor` exits 0 with 3/3 `FAIL`**, so a preflight gate wired to it passes a total outage. | `FAIL` lines, wrong exit code | **Days** — and only because the 6 h run that follows it also produces nothing, which circles back to #1. |
| 8 | **Ctrl-C hangs** in `shutdown(wait=True)` drain for up to ~2.5 h before `interrupted` prints. | a hang, not a message | **Immediate if watched** — but reads as a frozen process, and the operator's second Ctrl-C is the only exit. |
| 9 | **Non-`KeyboardInterrupt` exception** (e.g. Overpass returns HTTP 200 with an HTML error page → `osm.py:58` `result.json()` → `JSONDecodeError`). Caught at `main.py:167`, run continues, exit `0`. | 1 stderr line, possibly blank (§`str(e)`) | **Immediate** — the only category that reliably surfaces. |
| 10 | **`BrokenPipeError`** from `leadminer stats \| head`. | traceback + `Exception ignored` | **Immediate**, but misattributed to a crash. |

**The pattern:** the failure modes ranked 1-7 are all *exit 0 with plausible-looking
output*. That is the whole problem with this function as a cron entrypoint — not
that it crashes, but that it succeeds indistinguishably from having done nothing.

---

## Not a bug, but worth knowing

- **The empty-list case is handled in the diagnostic command and ignored in the
  production one.** `cmd_scrape` gets it right: `cli.py:63-66` returns `1` and
  prints `WARNING: zero records. The source may be broken or blocked.` That is
  exactly the right behaviour. `cmd_run` has no equivalent — zero new records
  flows through `main.py` into a full export and exit `0`. The inversion is
  deliberate-looking and backwards: the command you run to check things is
  strict, the command that writes the product is permissive.
- **Inverted exception strictness between the two paths.** `cmd_scrape`
  (`cli.py:61`) lets scraper exceptions escape as a traceback; the full pipeline
  (`main.py:167`) catches and continues. So the cheap diagnostic command fails
  loudly on a broken source while the expensive one shrugs. Someone will
  eventually conclude the diagnostic command is the buggy one.
- **`_fetch_website` bypasses `fetch_with_retry`.** `enricher.py:175` calls
  `get_session().get(...)` directly, so the retry/backoff/jitter work in
  `httpclient.py` — the module docstring's stated reason for existing — does not
  apply to the pipeline's single largest cost centre. That amplifies S1-cost:
  every one of N websites gets exactly one 8 s attempt, and every transient
  blip becomes a permanent `UNKNOWN`.
- **`cmd_doctor`'s credential check is presence, not validity.** `cli.py:210-213`
  tests `bool(os.environ.get(var))`. `SCRAPER_EMAIL` missing never affects the
  exit code at all, even though it degrades the User-Agent that Overpass and
  Wikidata both ask for (`httpclient.py:74-85`).
- **`--out` on `cmd_score` is the only safe way to use it**, which makes the
  unsafe way the default. Worth a line in `--help`.
- **`main.py`'s atomic write is genuinely good.** `os.replace` at `main.py:130`
  means a reader never sees a half-written master, and the `try/finally` around
  the temp file protects every *exception* path. The only gap is signals (S3
  above). This is the one part of the six-hour story that already works.

---

## Recommended order of work

1. **Give the run an exit code.** Change `main.main()` to return an `int` that
   reflects per-source success; remove `or 0` at `cli.py:315`. Until this lands,
   no amount of logging helps, because cron cannot see the difference between
   success and zero leads. Covers rank 1, 2, 6.
2. **Handle SIGTERM and stop swallowing Ctrl-C.** Signal handler in
   `cli.main()`; `cancel_futures=True` on the pools at `main.py:160` and
   `enricher.py:237`. Covers rank 5, 8, and the S1-cost cliff.
3. **Make the timeout a function of the job.** Derive `timeout-minutes` from the
   master row count, or give enrichment its own wall-clock budget and record
   "enrichment truncated at N" in the output rather than letting the job die.
   Add URL liveness caching with a TTL so week-over-week runs stop re-fetching
   every site. Covers the S1-cost cliff permanently.
4. **Configure logging in `main()`** — one `basicConfig` call brings
   `httpclient.py`'s entire retry telemetry to life. Cheapest fix in this report.
   Covers rank 3.
5. **Flush the heartbeat.** `flush=True` on `enricher.py:262` and the `main.py`
   progress prints, or `PYTHONUNBUFFERED=1` in the job env. One-character-class
   change, outsized diagnostic value. Covers rank 5.
6. **Fix `cmd_doctor`'s exit code and make it actually probe** the API key and
   use the right HTTP method. It is the designated pre-flight; it currently
   cannot fail. Covers rank 7.
7. **Unify the entrypoints.** Switch the workflow to `leadminer run` and delete
   one of `cli.py:322` / `main.py:245`, so fixes here and fixes there stop being
   different fixes. Anchor `DATA_DIR` to a real config location in the same
   change. Covers rank 4.
8. **Housekeeping:** `str(e)` → `repr(e)` at `main.py:168`; `BrokenPipeError` in
   `main()`; `try/finally` + startup `.tmp` sweep; make `cmd_score` require
   `--out`.
