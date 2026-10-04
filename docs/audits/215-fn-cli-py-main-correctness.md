# 200 — `cli.main()` correctness: exit codes, argument handling, and the commands it dispatches to

**Target:** `main(argv)` at `cli.py:312-318`.
**Callers read:** `pyproject.toml:33` (console script `leadminer = "cli:main"`, invoked as `sys.exit(main())` with `argv=None`), `cli.py:322` (`raise SystemExit(main())`).
**Callees read:** `cmd_run` (`cli.py:33`), `cmd_scrape` (`cli.py:40`), `cmd_stats` (`cli.py:79`), `cmd_validate` (`cli.py:135`), `cmd_doctor` (`cli.py:204`), `cmd_score` (`cli.py:247`), `build_parser` (`cli.py:280`), and `main.main` / `main.write_csv` / `main.load_master` (`main.py`).

Everything below was **executed**, not reasoned about. Harness: a copy of the `requests`/`urllib3` stub from `tests/test_lead_signal.py:23-60` (no network — the brief forbids it), run in throwaway temp directories with `os.chdir` so nothing touched the repo. Scraper classes were monkeypatched in-process to simulate failures; no real scraper, Overpass, Wikidata, Places, or business website was contacted.

---

## Verdict

`main()` itself is three lines and is *structurally* fine — `SystemExit` from argparse correctly propagates, and `KeyboardInterrupt` is caught. **The function has no failure channel, and that is the whole problem.** `cli.py:315` is `return int(args.func(args) or 0)`, and `cmd_run` at `cli.py:37` returns `pipeline.main()`, which is annotated `-> None` (`main.py:148`) and can only ever return `None`. So `leadminer run` — the primary documented command (`README.md:9`) — **exits 0 after total pipeline failure, having already replaced the cumulative master with a 225-byte header-only CSV.** Confirmed by execution, including the realistic case where no exception is raised at all. The rest of the report covers five inputs that make `main`'s dispatch or its offline commands produce a wrong or surprising result; the second-worst is `leadminer score` silently deleting operator-added columns from the master, in place, with exit 0.

---

## Findings

### S1 — `leadminer run` exits 0 after every source fails, and has already overwritten the master with an empty CSV

- **Where:** `cli.py:315` (`return int(args.func(args) or 0)`), `cli.py:37` (`return pipeline.main()`), `main.py:148` (`def main() -> None`), `main.py:209` (unconditional write of `all_businesses.csv`).
- **Breaks:** `main()` has no way to learn that the pipeline failed. `main.py:167-168` catches every scraper exception and only prints to stderr, then execution continues into `write_csv` for all five outputs. There is no zero-record guard anywhere between `main.py:170` and `main.py:209`. `write_csv` is atomic (`main.py:130` `os.replace`), so the destruction of the previous master is *clean and complete* rather than partial — which makes this worse, not better. The only guard anywhere in the repo is a shell row-count check in CI (`.github/workflows/scrape.yml:71-82`), which does not run `leadminer` at all (`.github/workflows/scrape.yml:68` runs `python main.py`).
- **Trigger (exact):** `cd` into an empty directory and run `leadminer run` with `GOOGLE_PLACES_API_KEY` unset and Overpass/Wikidata unreachable — i.e. every source down, which is the normal state during an Overpass rate-limit (HTTP 429) or a Places quota exhaustion (HTTP 403).
- **Wrong output (exact, captured):**

  ```
  exit code: 0
  STDERR: ''          <- nothing at all for the two sources that failed silently
  STDOUT:
      [OSM] FAILED after 3 attempts: HTTPError. Continuing without OSM data.
      [WikidataScraper] collected 0 records
      [OSMScraper] collected 0 records
      Total raw records from scrapers: 0
      ...
        Written: data/all_businesses.csv (0 records)
      ...
      Summary:
        Total unique businesses : 0
        SALES-READY (actionable): 0
  all_businesses.csv: 225 bytes, 0 data rows
  ```

  The master is now exactly the 23-column header line plus a newline. Exit status is 0, so any cron, systemd unit, Makefile, or shell `set -e` wrapper records a **successful** run.

- **Aggravating detail — the common failure path raises no exception at all.** `OSMScraper.scrape` (`scrapers/osm.py:31`) and `WikidataScraper.scrape` are *generator functions*; on a failed fetch they execute a bare `return` (`scrapers/osm.py:56`, `scrapers/wikidata.py:85`). `list(gen)` on that is `[]`, not an error. So `main.py:167-168`'s handler never fires for OSM/Wikidata, and there is not even a stderr line — the only trace is a `[OSM] FAILED after 3 attempts` string on stdout, sitting directly above a cheerful `Summary:` block. Verified: `list(g()) == []` for a generator containing a bare `return`.
- **Contrast:** `cmd_scrape` gets this right (`cli.py:63-66` — zero records → warning + `return 1`). `cmd_run` throws that check away.
- **Fix:** change `main.py:148` to `def main() -> int` and return `1` when `len(raw) == 0` or every scraper yielded zero records, and have `cmd_run` at `cli.py:37` `return pipeline.main()` unchanged — that one line then becomes correct, because `cli.py:315`'s `or 0` stops masking `None`.

---

### S1 — `leadminer score` silently deletes every column outside `main.FIELDS`, in place, on the cumulative master

- **Where:** `cli.py:274-275` (`out = pathlib.Path(args.out) if args.out else path` → **in place by default**), then `pipeline.write_csv(out, records)`; mechanism at `main.py:125` (`csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")`) against the fixed field list at `main.py:33-40`.
- **Breaks:** `load_master` (`main.py:86`) returns whatever `csv.DictReader` produced, extra columns included. `write_csv` then throws them away. The command's entire stated contract is "Re-score an existing CSV with current rules" (`cli.py:248`) — it makes no mention of dropping data, and `main.py:108-119`'s docstring shows this function was hardened specifically to *protect* the cumulative master from loss, so an operator reasonably trusts it. There is no `--dry-run`, no backup, and the `--out` escape hatch is opt-in.
- **Trigger (exact):** a sales team adds three columns to `data/all_businesses.csv`:

  ```
  sales_owner,last_contacted,notes
  mary,2026-02-02,called, no answer
  ```

  then `leadminer score data/all_businesses.csv`.
- **Wrong output (exact, captured):**

  ```
  outcome: returned 0
  stdout:  re-scored 1 records, 1 changed -> .../master_extra.csv
  columns before: ['sales_owner', 'last_contacted', 'notes']
  columns after : ['recommended_service', 'source', 'scraped_at']
  ```

  26 columns in, 23 out. The three columns are gone, unrecoverable, and the only output line is about `lead_score`. Exit 0. Rated S1 because the brief defines S1 as silent data loss and this is exactly that, on the cumulative master, by default, with no warning.
- **Fix:** in `cmd_score`, refuse an in-place write when `set(csv.DictReader(...).fieldnames) - set(main.FIELDS)` is non-empty (print the offending names and return 2), or make `write_csv` accept caller-supplied `fieldnames` so a round-trip preserves unknown columns.

---

### S2 — a directory argument produces a raw `IsADirectoryError` traceback from all three offline commands

- **Where:** `cli.py:82` (`if not path.exists()`), `cli.py:138`, `cli.py:255` — all three use `exists()`, which is `True` for directories; the `open()` calls at `cli.py:86`, `cli.py:142` and (via `main.py:85`) `cli.py:259` then blow up. `main()` at `cli.py:314-318` catches only `KeyboardInterrupt`, so nothing intercepts it.
- **Breaks:** `leadminer stats data/` and `leadminer validate data/` are the most likely operator typo in this tool — `data/` is the documented output directory and shell tab-completion completes the slash. `leadminer score data/` is worse: `path.exists()` passes at `cli.py:255`, then `main.py:85` raises. These are the three commands `cli.py:14` and `README.md:18-20` promise are "safe to run anywhere" and are "what you reach for when a run looks wrong" — exactly when a clean error message matters.
- **Trigger (exact):** `leadminer stats data/`
- **Wrong output (exact, captured):**

  ```
  Traceback (most recent call last):
    File "cli.py", line 315, in main
      return int(args.func(args) or 0)
    File "cli.py", line 86, in cmd_stats
      with open(path, newline="", encoding="utf-8-sig") as f:
  IsADirectoryError: [Errno 21] Is a directory: 'data'
  ```

  Exit 1 from the interpreter, not from the CLI. Same for `leadminer score data/` (`File "main.py", line 85, in load_master`). And `leadminer stats ""` is worse still, because `Path("")` normalises to `.`: `IsADirectoryError: [Errno 21] Is a directory: '.'`.
- **Fix:** change `cli.py:82`, `cli.py:138` and `cli.py:255` to `if not path.is_file():` — or wrap `cli.py:315` in `except OSError as e: print(f"{e}", file=sys.stderr); return 2`.

---

### S2 — the default file paths are CWD-relative, so the no-argument commands only work from the repo root

- **Where:** `cli.py:26` (`DATA_DIR = pathlib.Path("data")`), consumed as the argparse default at `cli.py:294` (`stats`), `cli.py:298` (`validate`) and `cli.py:305` (`score`). Entry point `pyproject.toml:33` means `leadminer` is on `$PATH` and will be run from anywhere.
- **Breaks:** `leadminer stats` is advertised as "summarise a CSV (no network)" and "the first thing to check" (`cli.py:80`). Run from a terminal profile, a cron entry, a systemd unit with a different `WorkingDirectory`, or a Docker exec, it looks for `$PWD/data/all_businesses.csv`. The error message is indistinguishable from "the pipeline has never run".
- **Trigger (exact):** `leadminer stats` with `cwd=/tmp` while the real data is at `<repo>/data/all_businesses.csv`.
- **Wrong output (exact, captured):**

  ```
  from repo dir  : returned 0
  from elsewhere : returned 2 | no such file: data/all_businesses.csv
  cli.DATA_DIR = data (relative: True )
  ```

- **Fix:** `DATA_DIR = pathlib.Path(__file__).resolve().parent / "data"` at `cli.py:26` (or honour a `LEADMINER_DATA_DIR` env var, falling back to the package root).

---

### S2 — `leadminer score` rewrites only the file it was pointed at, desynchronising the four derived exports

- **Where:** `cli.py:274-275` performs a single `write_csv`. But the exports at `main.py:199-213` are all derived from fields `cmd_score` rewrites at `cli.py:267-268`: `sales_ready` membership is `has_any_contact(r) and r["industry_priority"] in ("high","medium")` (`main.py:202-205`), and `qualified` is `completeness_score >= 1` (`main.py:206`).
- **Breaks:** after `leadminer score data/all_businesses.csv`, `all_businesses.csv` holds new priorities while `sales_ready.csv` — the file the sales team actually works from — still reflects the old ones. Nothing prints a warning; exit 0.
- **Trigger (exact):** master row `{category: "dentist", industry_priority: "low", recommended_service: "Stale pitch", phone: "70123456"}`; `leadminer score data/all_businesses.csv`.
- **Wrong output (exact, captured):**

  ```
  industry_priority('dentist') per current rule = high
  industry_priority in master now: high (was 'low')
  recommended_service now       : Lead-gen website + Google Ads launch (was 'Stale pitch')
  files rewritten by `score`    : ['all_businesses.csv']
  files left stale              : ['sales_ready.csv', 'qualified_businesses.csv']
  ```

  That row now satisfies `main.py:202-205` and belongs in `sales_ready.csv`, which is still empty and byte-identical.
- **Fix:** after the re-score loop at `cli.py:265-272`, regenerate the four derived files with the same predicates as `main.py:199-206`; or require `--out` for in-place writes so the caller decides when to re-export.

---

### S2 — `cmd_score` has drifted from `main.main`, so `score` and `run` no longer produce the same row

- **Where:** `cli.py:34` claims *"Thin wrapper over main.main so there is one code path."* `cmd_score` re-implements `main.py:183-197` and has already diverged in two ways: it never sets `completeness_score` (`cli.py:250` imports only `lead_score`; the field is computed at `enricher.py:349` inside `enrich`, which `cmd_score` never calls), and it never normalises the phone (`main.py:192-194` does; `cli.py:264-272` does not).
- **Breaks:** after `leadminer score data/all_businesses.csv`, three of the 22 columns (`industry_priority`, `recommended_service`, `lead_score`) come from the current ruleset while two (`phone`, `completeness_score`) come from an older one. `qualified_businesses.csv`'s filter (`main.py:206`) then reads the stale column. The un-normalised `phone` also breaks phone-keyed dedup (`dedup.normalize_phone`) on the *next* run, since `main.py:180` merges master rows back into the dedup index.
- **Trigger (exact):** `{name: "X", category: "dentist", country: "LB", phone: "0096613701234", email: "a@b.co", website: "https://x.example", website_live: True, rating: 4.5, source: "osm"}` run through both paths.
- **Wrong output (exact, captured):**

  ```
  run    -> {'phone': '+96613701234', 'lead_score': 60, 'completeness_score': 4}
  score  -> {'phone': '0096613701234', 'lead_score': 60}  completeness_score NOT set
  lead_score identical: True | phone identical: False
  ```

  Note `lead_score` agrees here only by luck: `enricher.py:285` reduces the phone to digits and tests `len >= 7`, so normalisation cannot change it. The `completeness_score` divergence is structural, not incidental.
- **Fix:** extract `main.py:183-197` into one `rescore(record) -> dict` in `enricher.py` that sets `country`, `industry_priority`, `recommended_service`, normalised `phone`, `completeness_score` and `lead_score`, and call it from both `main.py:183` and `cli.py:265`.

---

### S3 — the `KeyboardInterrupt` handler at `cli.py:316` cannot return until the scraper threads finish

- **Where:** `cli.py:316-318`, `main.py:160` (`with ThreadPoolExecutor(max_workers=3) as pool:`), `main.py:164` (`future.result()`). `ThreadPoolExecutor.__exit__` calls `shutdown(wait=True)`, so the exception raised by the SIGINT at `main.py:164` cannot escape the `with` body until every submitted worker returns. Budgets: `scrapers/osm.py:44` `timeout=200` with `retries=3`; `scrapers/wikidata.py:75` `timeout=90` with `retries=3`.
- **Breaks:** Ctrl-C appears to do nothing. In a 300-minute CI job (`scrape.yml:33`) or an interactive run against a hung host, the operator will SIGKILL — which is precisely the state `main.py:108-119` was written to protect the master from.
- **Trigger (exact):** press Ctrl-C during `leadminer run` while an Overpass request is in flight.
- **Wrong output (exact, captured):** workers sleeping 6 s, `SIGINT` delivered at t=0.7 s →

  ```
  cli.main returned after 6.0s -> 130
  stderr: 'interrupted'
  honoured within 1s of Ctrl-C? False
  ```

  Scaled to the real timeouts that is up to ~10 minutes (3 × 200 s plus backoff) before `interrupted` is printed. The catch at `cli.py:316` is correct as written; the executor it wraps is what swallows the signal.
- **Fix:** wrap the pool in `try/finally` with `pool.shutdown(wait=False, cancel_futures=True)` at `main.py:162` so `KeyboardInterrupt` propagates to `cli.py:316` immediately.

---

### S3 — `cmd_doctor` reports `OK` for any HTTP status below 500, including the failures doctor exists to find

- **Where:** `cli.py:240-241` (`note = "" if r.status_code < 500 else f"HTTP {r.status_code}"`, `print(f"  {'OK ' if r.status_code < 500 else 'WARN'}  ...")`).
- **Breaks:** `leadminer doctor` is billed as "check credentials and connectivity" (`cli.py:11`) and is the first thing you run when a run looks wrong. HTTP 429 (rate limited) and HTTP 403 (no/invalid key, wrong method) both render as `OK`. The Google Places probe at `cli.py:236` is an unauthenticated `GET` against `.../places:searchText`, a `POST`-only resource, so it is guaranteed to be rejected with a 4xx — and that rejection is reported as healthy.
- **Trigger (exact):** any of the three endpoints answering 429 or 403.
- **Wrong output (exact):** with a stub returning 403 for all three, the captured output is

  ```
  connectivity (5s timeout each):
    OK   Overpass
    OK   Wikidata
    OK   Google Places
  ```

  *Caveat:* I did not make network calls (brief rule 2), so the 429/403 statuses above are static reasoning from the endpoints and status codes, not an observed response. The 403-for-unauthenticated-GET case is not in doubt; the 429 case is documented Overpass behaviour under load.
- **Fix:** `ok = r.status_code < 400` at `cli.py:240-241`, and probe the Places endpoint with `POST` + the real key, or check the key's presence instead of the endpoint's.

---

### S3 — unreachable defensive code on the `run`/`scrape` path

- **Where:** `cli.py:56-59`. `build_parser` already restricts `--source` with `choices=["osm", "wikidata", "google_places"]` at `cli.py:289-290` — exactly the three keys of the registry at `cli.py:51-55`.
- **Trigger (exact):** `leadminer scrape --source nope`
- **Wrong output:** never reaches `cli.py:56`; argparse exits 2 first. Captured: `leadminer scrape: error: argument --source: invalid choice: 'nope'`. The `if args.source not in registry:` branch, its error message and its `return 2` are dead.
- Same file, same smell: `cli.py:29-30` — `_fmt`'s `None → "n/a"` branch is unreachable (`count()` at `cli.py:95-96` always returns `int`) and its `suffix` parameter is never passed by any of the six call sites (`cli.py:98-103`).
- **Fix:** delete `cli.py:56-59` and reduce `_fmt` to `def _fmt(n: int) -> str: return f"{n:,}"`.

---

## Not a bug, but worth knowing

- **The `int(... or 0)` at `cli.py:315` is dead coercion that hides a type error.** All five `cmd_*` functions except `cmd_run` return a real `int`, and `int()` on an `int` is a no-op. Its only current effect is converting `cmd_run`'s `None` into `0` — which is exactly what makes S1 invisible. Under `mypy strict` (`pyproject.toml:66`), `cli.py:37`'s `return pipeline.main()` should already be flagged as returning `None` from a function declared `-> int`; the `or 0` suppresses the symptom at the call site without fixing the signature. (`mypy` is not installed here, so this is a static claim, not a run.)
- **`SystemExit` propagating out of `main()` is correct.** `build_parser().parse_args(argv)` at `cli.py:313` sits outside the `try`, so argparse's own exits are untouched. Captured: `main([])` → `SystemExit 2`, `main(["--help"])` → `SystemExit 0`. Both right.
- **`leadminer score` on a header-only CSV behaves correctly.** Captured: returns `1`, prints `... has no usable rows` to stderr, and leaves the file byte-identical (verified). Good.
- **`leadminer score` needs `requests` installed, despite the "safe to run anywhere" promise.** `cli.py:249` does `import main as pipeline`, which pulls in `scrapers.*` → `httpclient.py:43 import requests`. Captured on a simulated bare checkout: `ImportError: No module named 'requests'` traceback. `cli.py:14` ("Commands other than run/scrape/doctor never touch the network, so they are safe to run anywhere") and `README.md:18-20` are therefore optimistic for `score` specifically. `cmd_doctor` handles exactly this case at `cli.py:226-231`; `cmd_score` should too, or should import only `lead_score`/`recommend_service`/`industry_priority` without going through `main`.
- **`SCRAPER_EMAIL` being `MISSING` in doctor does not fail the check — and that is correct.** `httpclient.py:81-84` treats it as optional (empty UA tail), and Overpass/Wikidata only *ask* for it. `cli.py:213` correctly gates only on `GOOGLE_PLACES_API_KEY`. No change wanted.
- **`cli.py:107-110`'s tri-state handling is right.** `website_live` is `True`/`False`/blank in the CSV, `load_master` (`main.py:103`) and `cmd_stats` (`cli.py:107-110`) use the same literal mapping, and `cli.py:109-110` correctly excludes rows with no website from the `unreachable` bucket. Covered by `tests/test_lead_signal.py:211`.
- **CI is not affected by S1 today, by luck.** `.github/workflows/scrape.yml:68` runs `python main.py`, whose exit code is *also* always 0; the run is only caught by the row-count guard at `scrape.yml:71-82`. If the CLI is ever adopted as the CI entry point — which `README.md:9` invites — S1 becomes a live data-loss path, because that guard runs *after* `main.py` has already replaced the master.

---

## Recommended order of work

1. **Give `main()` a failure channel** (S1 #1): `main.py:148` → `-> int`, return non-zero on zero raw records or all-sources-failed; `cli.py:37` then needs no change. One-line invariant: `leadminer run` must exit non-zero whenever `main.py:170` reports 0 raw records. Also gate `main.py:209` so the master is never replaced by a header-only file. `cli.py:63-66` already contains the correct predicate — copy it.
2. **Stop `score` from destroying the master** (S1 #2): reject in-place writes when the input carries columns outside `main.FIELDS`, and default `--out` to a new file rather than overwriting.
3. **`path.is_file()` guards** (S2 #3) — three tokens at `cli.py:82`, `cli.py:138`, `cli.py:255`, or one `except OSError` at `cli.py:315`. Cheapest fix in the report.
4. **Absolute `DATA_DIR`** (S2 #4) at `cli.py:26`, and regenerate the derived exports after `score` (S2 #5) — same change, both make `leadminer score` safe to run from a scheduler.
5. **Collapse the duplicated scoring path** (S2 #6): one `rescore()` used by `main.py:183-197` and `cli.py:265-272`.
6. **Ctrl-C responsiveness** (S3 #7) at `main.py:162`.
7. **Housekeeping** (S3 #8, #9): doctor's `status_code < 400`; delete `cli.py:56-59` and the dead `_fmt` branches.