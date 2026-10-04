# 200 — `build_parser` under a cron job: what prints, what swallows, what exits 0

## Verdict

`leadminer run` **cannot return a non-zero exit code**. `cmd_run` (`cli.py:33-37`)
returns `pipeline.main()`, and `main.main()` is annotated `-> None`
(`main.py:148`), so `int(args.func(args) or 0)` at `cli.py:315` evaluates
`int(None or 0)` → `0`. Verified by stubbing the pipeline and calling
`cli.main(["run"])`: `return code: 0`. Every other subcommand in the file has
real exit codes (`scrape`→1 on zero records, `validate`→1 on problems,
missing file→2); the one command the cron job actually runs has none, and
`build_parser` (`cli.py:280-309`) gives it no flags to compensate. A run where
every source failed, nothing was scraped, and the enrichment pass blanked
every known `website_live` verdict in the master is reported to cron,
systemd and GitHub Actions as **success**.

Everything below hangs off that. This report answers the assigned questions
concretely; all reproductions are local (no network, no installs).

---

## Findings

### S1 — `run` is structurally incapable of failing: `int(args.func(args) or 0)` over a `-> None` pipeline

- **Where:** `cli.py:315` (`int(args.func(args) or 0)`), `cli.py:37`
  (`return pipeline.main()`), `main.py:148` (`def main() -> None:`)
- **Breaks:** There is no code path by which `leadminer run` exits non-zero.
  Not on scraper failure, not on an empty scrape, not on enrichment collapse,
  not on writing 0 rows. `main.py` contains no `sys.exit`, no `raise
  SystemExit`, no `return 1` — verified by grep. The only failure that reaches
  a non-zero code is an *uncaught exception*, which produces a traceback and
  exit 1.
- **Trigger:** stub `main.main` to return `None` (its real signature) and call
  `cli.main(["run"])` → `return code: 0`, empty stderr. Equivalently: unset
  `GOOGLE_PLACES_API_KEY`, run `leadminer run` →
  `google_places.py:169-171` prints "skipping" and yields nothing, the other
  two sources run normally, exit 0.
- **Why it is S1, not S2:** every other guard in the repo assumes the run
  exits non-zero when it should not have. `.github/workflows/scrape.yml:74-83`
  is a hand-rolled substitute because the CLI has no gate; and `cmd_scrape`
  (`cli.py:63-66`) already demonstrates the correct pattern one function away.
- **Fix:** make `main.main()` return `int` and propagate it — `cmd_run` should
  return the pipeline's code, and the pipeline should return non-zero when a
  source contributed zero records or when the unreachable ratio exceeds a
  threshold.

### S1 — `build_parser` gives `run` no knobs, so the six-hour job has no abort, no dry-run, and no gate

- **Where:** `cli.py:285-286` — `sub.add_parser("run", help=...).set_defaults(func=cmd_run)`
- **Breaks:** `leadminer run --help` is literally:
  ```
  usage: leadminer run [-h]
  options:
    -h, --help  show this message and exit
  ```
  There is no `--sources`, no `--limit`, no `--workers`, no `--fail-under`,
  no `--dry-run`, no `--data-dir`. The only way to shorten a run is to kill it,
  and killing it is broken (S2 below). The only way to gate it is to parse its
  stdout, which is 84 bare `print()` calls with no timestamps and no machine
  format.
- **Trigger:** `cd /some/other/dir && leadminer run` — `DATA_DIR` is
  `pathlib.Path("data")` at `cli.py:26`, resolved against the process CWD, not
  the package root. The run scrapes for hours and writes a brand-new master
  into whatever `./data` happens to be. No warning, exit 0.
- **Fix:** add `--data-dir` (default resolved from `__file__`, overridable),
  plus `--fail-if-zero-records` / `--max-unreachable` / `--dry-run`.

### S1 — `doctor` is a false-green preflight: every connectivity failure still exits 0

- **Where:** `cli.py:233-244`. `ok` is set `False` **only** when
  `GOOGLE_PLACES_API_KEY` is unset (`cli.py:213-214`). The connectivity loop
  prints `FAIL`/`WARN` but never touches `ok`.
- **Breaks:** the exact preflight the README prescribes
  (`README.md:95-96`: `leadminer doctor` then `leadminer run`) is a no-op guard.
  Verified with a stubbed `requests.get` raising `ConnectionError` (DNS dead):
  ```
  connectivity (5s timeout each):
    FAIL  Overpass       ConnectionError
    FAIL  Wikidata       ConnectionError
    FAIL  Google Places  ConnectionError
  rc = 0
  ```
  Worse, `cli.py:240` treats **any** status `< 500` as healthy. Verified with
  403/404 responses (a corporate proxy, a revoked key, a POST-only endpoint):
  ```
    OK   Overpass
    OK   Wikidata
    OK   Google Places
  rc = 0
  ```
  `doctor` never sends the API key, and `https://places.googleapis.com/v1/places:searchText`
  (`cli.py:236`) is a POST endpoint probed with GET — so it can never detect a
  dead or unbilled `GOOGLE_PLACES_API_KEY`, which is the single most likely
  thing to break.
- **Trigger:** `leadminer doctor && leadminer run` in a crontab or Makefile.
  `doctor` exits 0, `run` starts a six-hour scrape with a revoked key and
  collects zero Places records, then exits 0. `MISSING data` (`cli.py:222`) is
  also non-fatal.
- **Fix:** set `ok = False` on any `FAIL` line; treat `4xx` as `FAIL`; probe the
  Places endpoint with a real authenticated request; add `--strict` for cron.

### S1 — a total network outage re-writes the master with every `website_live` verdict erased

This is the concrete payload of the two findings above, and the worst silent
failure in the file by notice time.

- **Where:** `main.py:167-168` (scraper exceptions printed to stderr, loop
  continues), `osm.py:47-56` / `wikidata.py:79-85` /
  `google_places.py:169-171,250-252` (all failure modes `return` an empty
  generator), `main.py:179` (`combined = raw_filtered + master` — no guard on
  empty `raw_filtered`), `main.py:187` (`enrich(records)` over the **whole
  historical master**), `enricher.py:249-250` (unconditional overwrite of
  `website_live`).
- **Breaks:** network drops at minute 0. All three scrapers yield 0 records.
  `main.py:170` prints `Total raw records from scrapers: 0` and keeps going.
  `combined` is the master alone, `dedup` returns it intact, and then
  `enrich()` re-probes **every website ever recorded**. Every probe returns
  `UNKNOWN` (`enricher.py:176-178`), and `enricher.py:250` writes
  `r["website_live"] = None` for each one, unconditionally — there is no rule
  that preserves a previous verdict when the new observation is `UNKNOWN`.
  `main.py:197` then recomputes `lead_score`, which loses the `+10` for live
  (`enricher.py:301-302`) or `+20` for dead (`enricher.py:303-304`) on
  essentially every row. `main.py:209-213` writes all five CSVs and
  `scrape.yml:88-89` publishes them over the Drive master. Exit 0.
  The CI row-count guard (`scrape.yml:78-83`) passes, because the master is
  large. The tri-state column that made the pipeline trustworthy is gone, and
  it is not recoverable from any CSV — the enriched verdict only ever existed
  in the master that was just overwritten.
- **Trigger:** `sudo tc qdisc add dev lo root netem loss 100%` (or just pull
  the plug / rotate a revoked secret) and run `leadminer run`.
- **Fix:** abort before enriching when `len(raw_filtered) == 0`; never overwrite
  a prior `True`/`False` with `UNKNOWN` (keep the old verdict and count it);
  fail the run when the unreachable ratio exceeds a threshold.

### S2 — nothing configures logging, and `build_parser` exposes no `--log-level`

- **Where:** `httpclient.py:45`, `scrapers/osm.py:7`, `scrapers/wikidata.py:7`
  create loggers and emit `log.debug(...)` at `httpclient.py:178-179` and
  `204-205`. Grep across the whole repo: **zero** `basicConfig`, `dictConfig`,
  `setLevel` or `addHandler` calls. `build_parser` (`cli.py:280-309`) adds no
  logging flag.
- **Breaks:** the root logger sits at `WARNING`, so every retry/backoff
  diagnostic — exactly the evidence needed to explain why a source returned
  zero records over six hours — is discarded. The only record of the run is
  84 `print()` calls (47 in `cli.py`, 19 in `main.py`, 8 in
  `google_places.py`, 4 in `enricher.py`, 3 each in `osm.py`/`wikidata.py`)
  with no timestamp and no run identifier, appended to whatever log file cron
  points at.
- **Fix:** `--log-level/-v` in `build_parser` calling `logging.basicConfig`,
  and put the retry counters in the final summary line.

### S2 — interrupting the run does not interrupt it: `cli.py:316-318` can only fire after the queue drains

- **Where:** `enricher.py:237-238` — `with ThreadPoolExecutor(max_workers=40)`
  followed by `futures = {pool.submit(...) for ...}`, i.e. **one future per
  website, all submitted up front**. `main.py:160-161` does the same for the
  three scrapers.
- **Breaks:** `KeyboardInterrupt` in the main thread triggers `__exit__` →
  `shutdown(wait=True)`, which does not cancel queued futures. So the
  `except KeyboardInterrupt` at `cli.py:316` cannot run until every queued
  fetch has completed. Demonstrated structurally (1000 tasks, 4 workers,
  `interrupt_main()` at t=0.05s):
  ```
  KeyboardInterrupt caught at t=5.95s
  tasks completed anyway: 1000/1000 (5.95s)
  ```
  Extrapolated: ~100k websites at an 8s timeout (`enricher.py:175`) across 40
  workers is up to ~5.5 hours of "shutting down" before the word `interrupted`
  appears. Ctrl-C is not an abort; it is a request to finish. A second Ctrl-C
  does not help: worker threads are non-daemon and joined at interpreter exit.
- **The SIGTERM path is different and worse in a different way:** there is no
  SIGTERM handler, so GitHub Actions' `timeout-minutes: 300`
  (`scrape.yml:25`) kills the process instantly with 143 — no cleanup, no
  summary, and a `*.csv.tmp` left in `data/` (harmless to the master thanks to
  the atomic rename, but `scrape.yml:89` copies `data/` with no `--include`
  filter, so the stray temp file is uploaded to `gdrive:leads/`).
- **Fix:** `pool.shutdown(wait=False, cancel_futures=True)` on interrupt; add
  a SIGTERM handler that sets the same flag; report progress on interrupt.

### S2 — `score` overwrites the master in place with no backup, and narrows the schema

- **Where:** `cli.py:274-276` — `out = pathlib.Path(args.out) if args.out else path;
  pipeline.write_csv(out, records)`.
- **Breaks:** `leadminer score` (no args) reads and rewrites
  `data/all_businesses.csv` in place. There is no `.bak`, no
  `--in-place-required` flag, no confirmation. If
  `recommend_service` or `industry_priority` regresses, the only copy of the
  re-scored master is destroyed by the command that produced it — and Drive
  gets the same state on the next `scrape.yml:89`. It also writes through
  `main.FIELDS` (23 names, `main.py:33-40`) with `extrasaction="ignore"`
  (`main.py:125`), so any column not in that list is silently dropped.
  Verified: a 24-column input row round-tripped through `leadminer score`
  came back with 23 columns, `rc = 0`, no warning.
- **Trigger:** `leadminer score data/all_businesses.csv`
- **Fix:** require `--out` or `--in-place`, write a timestamped `.bak`
  alongside, and error if the input has columns outside `FIELDS`.

### S3 — unhandled I/O errors escape `main()` as tracebacks

- **Where:** `cli.py:86`, `cli.py:142` — bare `open(...)`; `main`'s `try`
  (`cli.py:314-318`) only catches `KeyboardInterrupt`.
- **Breaks:** `leadminer stats "$UNSET_VAR"` (empty string → `Path("")` →
  `Path(".")`, which exists) raises `IsADirectoryError: [Errno 21] Is a
  directory: '.'` — verified for both `stats` and `score`. A non-UTF-8 CSV (a
  Drive-corrupted or zipped export) raises
  `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff`. Both exit 1 with
  a raw traceback, and exit 1 is indistinguishable from an argparse-style
  failure (which is 2).
- **Fix:** catch `OSError`/`UnicodeDecodeError` in `main` and map to
  `return 2`, one line, like the `.exists()` guard at `cli.py:82`.

### S3 — the required-subcommand error names an internal variable

- **Where:** `cli.py:283` — `p.add_subparsers(dest="cmd", required=True)`
- **Breaks:** `leadminer` with no arguments prints
  `leadminer: error: the following arguments are required: cmd`.
  `cmd` is the argparse `dest`, not a command a user can type. Verified
  verbatim.
- **Fix:** `add_subparsers(dest="cmd", required=True, metavar="{run,scrape,stats,validate,doctor,score}")`
  or `title="command"`.

### S3 — `prog` is hardcoded, so usage lies whenever the entry point differs

- **Where:** `cli.py:281` — `prog="leadminer"`
- **Breaks:** `python cli.py run`, `python -m cli run`, or a renamed console
  script all emit `usage: leadminer ...`. A cron operator debugging a failure
  is told to run a command that may not be on `PATH`.
- **Fix:** `prog=os.path.basename(sys.argv[0])` (the console-script name is
  already `leadminer` per `pyproject.toml:33`).

### S3 — `--help` is the module docstring, so it drifts silently and disappears under `-OO`

- **Where:** `cli.py:281-282` — `description=__doc__` with
  `RawDescriptionHelpFormatter`
- **Breaks:** the top-level help text is whatever is at the top of `cli.py`.
  Editing the module docstring silently rewrites user-facing help; building or
  running under `python -OO` strips docstrings, `__doc__` becomes `None`, and
  the entire description vanishes with no error.
- **Fix:** a literal `description=` string, and a `--version` action reading
  `importlib.metadata.version("leadminer")` (`pyproject.toml:7`).

### S3 — `cmd_scrape`'s registry check is dead code guarding a hand-synced list

- **Where:** `cli.py:289-290` (`choices=["osm", "wikidata", "google_places"]`)
  vs `cli.py:51-55` (the `registry` dict) vs `cli.py:56-59` (the runtime check)
- **Breaks:** argparse already rejects an unknown `--source`, so
  `cli.py:56-59` is unreachable. The real hazard is the two hand-maintained
  lists: add a scraper to `registry` and it is unreachable from the CLI with a
  confusing `invalid choice`; add it to `choices` first and you get an
  uncaught `KeyError` traceback. Nothing tests the correspondence.
- **Fix:** derive `choices` from `registry.keys()` and delete `cli.py:56-59`.

### S3 — `int(x or 0)` coerces a forgotten `return` into a silent success

- **Where:** `cli.py:315`
- **Breaks:** the `-> int` annotations on the `cmd_*` functions are not
  enforced at runtime. Any `cmd_*` that forgets its `return` statement is a
  green cron run. `tests/test_lead_signal.py:191-264` exercises `cmd_stats` and
  `cmd_validate` directly but never `main()` and never `cmd_run`, so this seam
  is untested.
- **Fix:** `raise SystemExit(...)` semantics via a typed helper, or one test
  asserting `main(["stats", fixture]) == 0` and `main(["doctor"]) == 0`.

### S3 — `validate` exists but nothing calls it

- **Where:** `cli.py:135-201`; `.github/workflows/scrape.yml:69` runs
  `python main.py`, not `leadminer run`
- **Breaks:** the data-quality gate is reachable only by a human who remembers
  to type it. Nothing in the pipeline invokes it, and the workflow's inline
  `wc -l` check (`scrape.yml:78-83`) covers exactly one of its five checks.
- **Fix:** call `cmd_validate` at the end of `cmd_run` and propagate its code,
  which requires S1's first fix anyway.

---

## Ranked silent-failure table

Ordered by how long a human takes to notice. "Silent" = exit 0, or exit 0 with
the only evidence on stderr in an unmonitored log.

| # | Failure | Detected by | Notice time |
|---|---|---|---|
| 1 | Total outage → `website_live` wiped to null on the whole master, `lead_score` drops on every row, Drive overwritten (`main.py:187` → `enricher.py:250`) | nothing | months; nobody reads `website_live` |
| 2 | One source silently returns 0 for many weeks — Overpass 429 (`osm.py:47-56`), SPARQL timeout (`wikidata.py:79-85`), revoked key (`google_places.py:169-171`) | the `[source] collected 0 records` line, on stderr-adjacent stdout nobody greps | weeks |
| 3 | Partial Overpass response: `[timeout:180]` in the query (`osm.py:12`) yields a valid 200 with a truncated `elements` array, printed as `[OSM] Got 12000 elements.` (`osm.py:59`) | nothing; no expected-count baseline | weeks |
| 4 | One Google query returns non-JSON → `resp.json()` (`google_places.py:254`) raises inside `_scrape_query` → `f.result()` at `google_places.py:197` re-raises **unguarded** out of the `as_completed` loop → `yield from all_records` (line 199) never runs → the whole Places batch, including 67 healthy queries, is discarded | `[GooglePlacesScraper] ERROR: ...` on stderr | weeks |
| 5 | `doctor` says `FAIL` on all three endpoints and exits 0 (`cli.py:244`) — the guard that was supposed to stop #1–#4 does not | nothing | weeks |
| 6 | Network drops **halfway** through enrichment: the remaining sites become `None`, `live`/`dead` counts collapse, `[Enricher] 38000 live / 200 dead / 62000 unreachable` (`enricher.py:271`) is printed and ignored | only that summary line | weeks |
| 7 | Retry/backoff diagnostics discarded — no logging config anywhere, no `--log-level` (`httpclient.py:178-179, 204-205`) | nothing | only when you go looking, days later |
| 8 | CWD-relative `data/` (`cli.py:26, 294, 298, 305`): under cron, CWD is `$HOME`, so `stats`/`validate`/`score` read or rewrite `$HOME/data/...` | the relative path in the error message *looks* right | hours, and only if you read the log |
| 9 | `score` rewrites the master in place, no backup, drops non-`FIELDS` columns (`cli.py:274-276`) | nothing | hours, if you notice the numbers moved |
| 10 | `run` exits 0 in every scenario above — the single root cause | nothing | never, by definition |

### Loud failures, for contrast (do not spend effort here)

- `leadminer` with no args → `SystemExit(2)` + usage. Cron emails it nightly;
  noisy but correct.
- Typos (`leadminer runs`) → exit 2. Correct.
- `leadminer scrape --source nope` → argparse exit 2, before any import.
- `leadminer scrape --source osm` on a total outage → `[osm] 0 records` +
  `WARNING` on stderr + **exit 1** (`cli.py:63-66`). The right pattern; it just
  is not on the cron path.
- Header-only or empty CSV → `stats` exit 1, `validate` exit 1 (verified).
- A kill mid-`write_csv` does **not** truncate the master: `main.py:122-133`
  writes to `*.csv.tmp`, `fsync`s, then `os.replace`s, cleaning up on
  `BaseException`. Covered by `tests/test_lead_signal.py:156-178`.

### Direct answers to the assigned questions

- **What does it print?** Nothing, ever, for `run`. `cmd_run` (`cli.py:33-37`)
  contains no `print` and no stderr write; every line you see came from
  `main.py` or a scraper. On a failed run those lines are indistinguishable
  from a successful one except by a human reading them.
- **What does it swallow?** `cli.py:314-318` catches only `KeyboardInterrupt`
  and only *after* `args.func` returns, which — per S2 — can be hours later.
  Everything else propagates as a traceback. The swallowing that matters is
  one layer down: `main.py:167-168` catches every scraper exception and
  continues, and `cli.py:315` then converts the result to `0`.
- **Network drops halfway?** `write_csv` is atomic, so the master is intact;
  but `enricher.py:250` has already overwritten live/dead verdicts with
  `None`, and exit is 0. See S1 #4 and rank #6.
- **A dependency returns garbage?** `FetchResult.json()`
  (`httpclient.py:110-113`) raises `JSONDecodeError` inside the generator;
  caught at `main.py:167` (source silently contributes nothing) or, for
  Google, re-raised unguarded at `google_places.py:197` (whole source
  discarded). Rank #4.
- **Called with an empty list?** `cli.main([])` → `SystemExit(2)` with
  `error: the following arguments are required: cmd`. Loud. In the pipeline
  sense, an empty scrape is the S1 case above and is entirely silent.
- **When it is interrupted?** SIGINT runs the entire remaining queue before
  printing `interrupted` (demonstrated: 1000/1000 tasks completed after
  `interrupt_main()` at t=0.05s). SIGTERM dies instantly at 143 with no
  cleanup. Neither is a usable abort.

---

## Not a bug, but worth knowing

- `README.md:63` documents `website_live` as "`true` if reachable, `false` if
  dead" — boolean only. The tri-state that `enricher.py:154-156` exists to
  protect is invisible in the docs, so an operator staring at a column of blanks
  has no reason to suspect a network problem. `cmd_stats` (`cli.py:105-113`)
  is the only place the tri-state is explained, and only if you run it.
- `build_parser()` is called fresh on every `main()` (`cli.py:313`). Fine for
  a one-shot CLI, but it gives tests no seam, which is why
  `tests/test_lead_signal.py` calls the `cmd_*` functions directly and skips
  the dispatch layer entirely.
- `pyproject.toml:33` names the entry point `leadminer`, but
  `scrape.yml:39` installs from `requirements.txt`, so the console script is
  never installed in CI and `cli.py` never runs in the scheduled job. The
  `build_parser` findings above are therefore currently latent rather than
  firing — they activate the moment someone switches `scrape.yml:69` to
  `leadminer run`, which is the stated direction of travel.
- `requirements.txt` pins `beautifulsoup4` and `lxml`, which nothing in the
  repo imports; `pyproject.toml:16-18` depends on `requests` alone. Two
  dependency manifests, already divergent.

---

## Recommended order of work

1. **`run` must be able to fail.** Change `main.main()` to return `int`;
   propagate through `cmd_run`; stop laundering `None` at `cli.py:315`.
   Everything else on this list is invisible until this lands.
2. **Gate the run on its own inputs.** `main.py:179` — abort if
   `raw_filtered` is empty when the master was previously non-empty. Add
   `cmd_validate`'s checks to the end of the run and propagate the code.
3. **Stop erasing enrichment state.** `enricher.py:250` must not overwrite a
   `True`/`False` verdict with `UNKNOWN`; preserve the prior value and count
   how many were preserved. This is what turns a bad night into an
   unrecoverable one.
4. **Make `doctor` a real gate.** `cli.py:240-244`: non-2xx and any `FAIL`
   sets `ok = False`; authenticate the Places probe.
5. **Make Ctrl-C work.** `enricher.py:237-238` / `main.py:160` —
   `shutdown(wait=False, cancel_futures=True)` on interrupt, plus a SIGTERM
   handler that does the same, so the 300-minute CI timeout and an operator's
   Ctrl-C behave identically.
6. **Turn on logging.** `--log-level` in `build_parser` → `basicConfig`, and
   put attempts/elapsed/error for each source in the final summary line so the
   run log has one greppable summary per run.
7. **Add `--data-dir`, resolved from `__file__` by default**, and remove the
   CWD-relative `pathlib.Path("data")` at `cli.py:26`.
8. **Make `score` non-destructive by default** (`cli.py:274-276`): require
   `--out` or `--in-place`, keep a timestamped backup, and error on columns
   outside `main.FIELDS`.
9. Polish: `--version`, `prog` from `argv[0]`, `metavar` for the subcommand
   error, `choices=sorted(registry)` derived from one list, and a
   `main()`-level exit-code test.