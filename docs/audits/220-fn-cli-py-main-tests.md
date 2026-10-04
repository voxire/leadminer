# 200 — `cli.main()` (cli.py:312): the test cases it still needs

## Verdict

`cli.main()` is the one function every operator touches and the suite never calls.
All 28 tests in `tests/test_lead_signal.py` invoke `cmd_stats`/`cmd_validate`
directly with a hand-built `argparse.Namespace`
(`tests/test_lead_signal.py:211-264`), so lines 312-318 and `build_parser()`
(`cli.py:280-309`) have **zero** coverage — including the `argv=None` branch that
is the only path the installed console script takes (`pyproject.toml:33`). Writing
those tests immediately exposes two real defects: `leadminer run` returns **0 after
every scraper fails**, and `cmd_score` prints **`0 changed` while rewriting the
master**. Both are reproduced below with literal input.

**Coverage baseline (verified):** `python3 -m unittest discover -s tests` → `Ran 28
tests … OK`. Four touch the CLI; none call `main()` or `build_parser()`. Commit
`43ddb2c`'s message claims "Tests: 20 stdlib-only, covering the CLI paths and
tri-state website_live" — the dispatch layer was never covered.

Everything below was executed against the tree at HEAD (`99493b9`) on CPython
3.14.8. Line numbers are from that tree.

---

## Findings

### S1 — `main()` converts every failure except Ctrl-C into a traceback, and `score` cannot run at all in a bare checkout

- **Where:** `cli.py:314-318` (the `try` covers only `KeyboardInterrupt`),
  `cli.py:249` (`cmd_score`: bare `import main as pipeline`), `cli.py:35`
  (`cmd_run`: same), against `cli.py:226-231` (`cmd_doctor` guards the identical
  import) and the stated invariant at `cli.py:228-229`.
- **Breaks:** `cmd_score` and `cmd_run` import `main` unguarded, and `main.py:25`
  → `scrapers/osm.py:4` → `httpclient.py:43` pulls in `requests`. In a checkout
  without dependencies installed (exactly the state of this repo —
  `python3 -c "import requests"` → `ModuleNotFoundError`), reproduced in a fresh
  interpreter:

  ```
  $ python3 -c "import sys; sys.path.insert(0,'.'); import cli; cli.main(['score','f.csv'])"
  Traceback (most recent call last):
    File "<stdin>", line 1, in <module>
    File ".../cli.py", line 315, in main
      return int(args.func(args) or 0)
    File ".../cli.py", line 249, in cmd_score
      import main as pipeline
    File ".../main.py", line 25, in <module>
      from scrapers.osm import OSMScraper
    File ".../scrapers/osm.py", line 4, in <module>
      from httpclient import fetch_with_retry, get_session, utc_now_iso
    File ".../httpclient.py", line 43, in <module>
      import requests
  ModuleNotFoundError: No module named 'requests'
  ```

  Exit code 1. In the *same* environment `main(["doctor"])` exits 1 with a clean
  `FAIL  requests is not installed - run: pip install -e '.[dev]'`
  (`cli.py:230-231`). One broken install, two failure modes — and `cli.py:228-229`
  says why that is unacceptable: *"doctor exists to diagnose a broken
  environment, so it must never be the thing that tracebacks."* `score` is the
  other command an operator reaches for when a run looks wrong, and it is the one
  that tracebacks.

  Second, independent instance of the same defect: `BrokenPipeError` is not
  caught either. `leadminer stats data/all_businesses.csv | head -3` reproduced
  with a stdout stub that raises after 3 `write()` calls:

  ```
  close after  3 writes -> RAISED BrokenPipeError: [Errno 32] Broken pipe
  close after  5 writes -> RAISED BrokenPipeError: [Errno 32] Broken pipe
  close after 15 writes -> RAISED BrokenPipeError: [Errno 32] Broken pipe
  ```

- **Trigger:** `main(["score", "data/all_businesses.csv"])` with `requests`
  absent; or `main(["stats", p])` with stdout closed early.
- **Why S1:** the brief's S1 is "a run that dies". It does, with a traceback, on
  the first thing a new operator runs. It is also unfixable by the caller: exit 1
  is what `cmd_validate` returns for bad data (`cli.py:201`), so a scheduler
  cannot distinguish "the tool is broken" from "your leads are bad" — the exact
  `EX_SOFTWARE` vs `EX_DATAERR` split required by
  `docs/audits/037-cli-design.md:459-463`. The fix is one `except` clause plus one
  guarded import.
- **Fix:** `except (OSError, ImportError, csv.Error)` → message on stderr + rc 3;
  `except BrokenPipeError` → rc 141; wrap `import main as pipeline` in
  `cmd_score`/`cmd_run` with `except ImportError` → rc 3.
- **Closes test cases:** C6, C7, C12, C17.

### S2 — `int(args.func(args) or 0)` makes `leadminer run` structurally incapable of reporting failure

- **Where:** `cli.py:315`, `cli.py:37`, `main.py:148`, `main.py:167-168`.
- **Breaks:** `main.py:148` is annotated `-> None` and returns `None`.
  `cmd_run` returns that `None` (`cli.py:37`). `None or 0` → `0`
  (`cli.py:315`). Meanwhile `main.py:167` swallows each scraper's exception into
  a stderr print and continues. Reproduced with the HTTP layer broken
  (`mock.patch.dict(sys.modules, {"requests": None})`):

  ```
  EXIT CODE: 0
  stderr:
  [WikidataScraper] ERROR: module 'requests' has no attribute 'RequestException'
  [OSMScraper] ERROR: module 'requests' has no attribute 'RequestException'
  stdout tail:
    Written: data/all_businesses.csv (0 records)
    Written: data/qualified_businesses.csv (0 records)
    ... all five CSVs, 0 records ...
    SALES-READY (actionable): 0
  ```

  Every source failed. `leadminer run` exited **0**.
- **Why it matters now:** `.github/workflows/scrape.yml:69` still runs
  `python main.py`, so the only guard against publishing a zero-row run is the
  row-count check *outside* the CLI (`scrape.yml:74-83`). Moving that step to
  `leadminer run` — the obvious next step now that `pyproject.toml:33` defines
  the entry point — leaves a weekly cron in which all three sources are blocked
  producing zero new leads and a green build. This is the third instance in this
  repository of the "CI step reports success over a broken state" class; the first
  two are recorded in the comments this repo now carries at `scrape.yml:53-56`
  (`|| true` on the rclone download) and `scrape.yml:57-63` (a missing master
  treated as first run).
- **Note, stated precisely:** the cumulative master is **not** destroyed. `main.py:150`
  loads it and `main.py:179` folds it into `combined`, so existing rows survive.
  The loss is "zero new leads this week", reported as success.
- **Fix:** `cmd_run` compares record count before/after and returns 4 when
  nothing new was collected (037:462 already reserves `EX_NOHOST` for this).
- **Closes test cases:** C8, C9.

### S2 — `cmd_score` prints `0 changed` while rewriting the cumulative master in place

- **Where:** `cli.py:264-276`, specifically `cli.py:270-271` (the counter) vs
  `cli.py:266-268` (the unconditional rewrites) and `cli.py:274-275` (in-place
  write).
- **Breaks:** the counter compares only `lead_score`. Input row with
  `lead_score=40` (already correct) and `recommended_service="Discovery call -
  scope the right service"` (stale):

  ```
  rc: 0 | re-scored 1 records, 0 changed -> .../f.csv
  MUTATED FIELDS: {'recommended_service': ('Discovery call - scope the right service',
                                            'SEO audit + visibility upgrade')}
  ```

  The master changed on disk and the operator was told nothing changed. Any rule
  change to `recommend_service` or `industry_priority` that does not also move
  `lead_score` is invisible in the one number `cmd_score` prints.
- **Why S2:** `0 changed` is the signal an operator uses to decide whether a
  scoring-rule change retroactively alters leads they have already pitched. It is
  wrong by construction for a whole class of rule changes, and the file it writes
  is the cumulative master.
- **Fix:** count a row as changed if any of `country`, `industry_priority`,
  `recommended_service`, `lead_score` differs.
- **Closes test cases:** C13, C14.

### S3 — the `argv=None` branch is the production entry point and has zero coverage

- **Where:** `cli.py:312` (`argv: list[str] | None = None`), `cli.py:313`
  (`parse_args(argv)`), `cli.py:322` (`raise SystemExit(main())`),
  `pyproject.toml:32-33` (`leadminer = "cli:main"`).
- **Breaks:** nothing is broken; this is a pure coverage hole and the highest-value
  one, because the generated console-script wrapper calls `main()` with no
  argument, so `sys.argv[1:]` is the *only* path a real user ever takes — and it
  is the path no test exercises.
- **Fix:** test C1 first.
- **Closes test cases:** C1.

### S3 — `main()` has a mixed return/raise contract, and the three file defaults are duplicated, cwd-relative strings

- **Where:** `cli.py:313` (`parse_args` is outside the `try`), `cli.py:26`
  (`DATA_DIR = pathlib.Path("data")`), `cli.py:294` / `cli.py:298` / `cli.py:305`
  (three copies of `str(DATA_DIR / "all_businesses.csv")`).
- **Breaks:** verified — `main([])` → `SystemExit(2)`;
  `main(["frobnicate"])` → `SystemExit(2)`; `main(["--help"])` → `SystemExit(0)`;
  `main(["stats", p])` → returns `0`. One function, two failure protocols.
  `raise SystemExit(main())` (`cli.py:322`) and hatch's wrapper both cope, but the
  in-process `cli.main(["run"])` compat proxy proposed at
  `docs/audits/037-cli-design.md:652` will receive an exception where it expects
  an `int`.

  Second: the defaults are resolved against the *current working directory*, not
  the install location. Verified from a temp dir:

  ```
  ['stats', 'validate', 'score'] -> ['data/all_businesses.csv'] * 3
  bare stats in empty cwd -> (rc 2, stderr 'no such file: data/all_businesses.csv')
  ```

  Since `leadminer` is installed globally (`pyproject.toml:33`), running it from
  anywhere but the checkout root prints a "no such file" error for a master that
  exists. The error text names the relative path, so the operator cannot tell the
  difference between "you have no data yet" and "you are in the wrong directory".
- **Fix:** one module-level `DEFAULT_MASTER = str(DATA_DIR / "all_businesses.csv")`
  used by all three; anchor `DATA_DIR` to the package root when installed.
- **Closes test cases:** C2, C3, C4, C5, C10.

---

## Not a bug, but worth knowing

- `tests/test_lead_signal.py:203` sets `recommended_service="SEO audit"`, which
  **no rule can produce**. The real string is `"SEO audit + visibility upgrade"`
  (`pitch_recommender.py:71`). Reusing `_fixture` for a pitch assertion tests
  fiction. Build expected pitches from a real record shape (e.g. a confirmed-dead
  site → `"Website rebuild + maintenance"`, `pitch_recommender.py:58`).
- `cmd_score --out` into a non-existent nested directory **succeeds** (verified
  rc 0) because `write_csv` does `path.parent.mkdir(parents=True, exist_ok=True)`
  (`main.py:121`). Do not write a test asserting otherwise.
- Subcommand dispatch is already exact-match. Verified: `st`, `sco`, `sc`, `val`,
  `doct`, `r`, `ru`, `stat`, `Stats`, `STATS` all exit 2 with `invalid choice`.
  C4 exists to keep it that way. But `allow_abbrev` is still on for *options*:
  `score x.csv --o y.csv` parses; `--OUT` does not. Harmless while `--out` is the
  only `--o*` flag; add `allow_abbrev=False` before adding a second one.
- No `--version`/`-V`; both exit 2 (`SystemExit` 2 verified). Adding one is a
  feature, not a bug — do not write a passing test for it.
- `cmd_run` ignores its `args` entirely (`cli.py:33-37`). `leadminer run` accepts
  no flags. Fine today; a hazard the moment 037:425-429's `--region`,
  `--skip-enrich`, `--allow-partial` land.
- `cmd_score` preserves the `website_live` tri-state through a full CSV round
  trip. Verified: `"True"`→`True` (lead 40), `"False"`→`False` (lead 50, "Website
  rebuild + maintenance"), `""`→`None` (lead 30, *not* a rebuild). This is
  correct and is exactly what C15 locks down.

---

## Implementation notes the test author needs

1. **Runner.** The house style is stdlib `unittest` and the documented command is
   `python3 -m unittest discover -s tests` (`tests/test_lead_signal.py:6`).
   `pytest>=8.0` is declared (`pyproject.toml:22`, configured at
   `pyproject.toml:75-77`) and collects `unittest.TestCase` subclasses, so a new
   `tests/test_cli_main.py` written in plain `unittest` runs under both. Use
   `subTest` for the property cases; do **not** use bare `parametrize`, which the
   unittest runner will silently skip.
2. **`requests` shim.** `_stub_requests()` (`tests/test_lead_signal.py:23-63`)
   must run before `import main`. Any test touching `cmd_score` or `cmd_run`
   needs it — verified: without the shim, `cli.main(["score", p])` in a bare
   checkout raises `ModuleNotFoundError` from `cli.py:249`. Import it as
   `from test_lead_signal import _stub_requests`.
3. **Patching a handler works through `main()`.** `build_parser()` resolves
   `cmd_score` from module globals at call time (`cli.py:307`) and `main()` calls
   `build_parser()` on every invocation (`cli.py:313`), so
   `mock.patch.object(cli, "cmd_stats", …)` changes dispatch. Verified: a
   `KeyboardInterrupt` side effect yields rc 130 with the handler called once.
4. **Patching a scraper must target the defining module.** `cmd_scrape` does
   `from scrapers.osm import OSMScraper` *inside* the function (`cli.py:48`), so
   the target is `scrapers.osm.OSMScraper`, **not** `cli.OSMScraper`. Getting this
   wrong makes a real Overpass POST.
5. **Patching `doctor`'s connectivity:** `mock.patch("requests.get", …)`.
   `cli.py:239` is the only network call in that command.
6. **Simulating a missing `requests` install:** `mock.patch.dict(sys.modules,
   {m: None for m in ("main", "requests", "httpclient", "scrapers",
   "scrapers.base", "scrapers.osm", "scrapers.wikidata",
   "scrapers.google_places", "scrapers.whitelist")})`. Setting only
   `{"requests": None}` does **not** work once another test has imported `main` —
   `import main` then succeeds from cache and the test silently passes for the
   wrong reason. Verified: the full eviction list reproduces
   `ModuleNotFoundError: import of main halted; None in sys.modules`.
7. **Never `chdir` without restoring** — use `os.chdir` in a `try/finally` or a
   `tempfile.TemporaryDirectory()` context, because C10 depends on cwd.
8. `write_csv` prints `Written: …` to **stdout** (`main.py:134`), so `score`
   output assertions must use `in` on the whole buffer, never an exact match.

---

## Test cases `cli.main()` still needs

### C1 — `test_none_argv_reads_sys_argv` · the production entry point
- **Input:** `mock.patch.object(sys, "argv", ["leadminer", "stats", str(p)])`, then
  `cli.main()` (no argument).
- **Assert:** returns `0`; stdout contains `"(1 rows)"`; stderr == `""`.
  Equivalence form: `main()` == `main(sys.argv[1:])`.
- **Pins:** the `argv=None` default (`cli.py:312`) and `parse_args(None)`
  (`cli.py:313`) — the only path `pyproject.toml:33` and `cli.py:322` ever take.
  Currently zero coverage.

### C2 — `test_bare_invocation_exits_2_without_traceback`
- **Input:** `main([])`.
- **Assert:** `assertRaises(SystemExit)` with `exc.code == 2`; stderr contains
  `"the following arguments are required: cmd"`; stderr does **not** contain
  `"Traceback"`.
- **Pins:** `sub.add_subparsers(dest="cmd", required=True)` (`cli.py:283`). Drop
  `required=True` and this becomes
  `AttributeError: 'Namespace' object no attribute 'func'` at `cli.py:315` — a
  traceback for the single most common operator mistake.

### C3 — `test_help_exits_zero_and_lists_every_command`
- **Input:** `main(["--help"])`.
- **Assert:** `SystemExit` with `code == 0`; stdout contains all six of `run`,
  `scrape`, `stats`, `validate`, `doctor`, `score`; stderr == `""`.
- **Pins:** `prog="leadminer"` and `RawDescriptionHelpFormatter` (`cli.py:281-282`),
  i.e. the module docstring usage block (`cli.py:6-12`) stays the help text. Also
  pins the *raise* protocol for argparse outcomes.

### C4 — `test_no_prefix_or_case_variant_is_accepted` (property: the whole rejection space)
- **Input:** each of `["frobnicate"]`, `["st"]`, `["sco"]`, `["sc"]`, `["val"]`,
  `["doct"]`, `["r"]`, `["ru"]`, `["stat"]`, `["Stats"]`, `["STATS"]`,
  `["scrape"]` (missing `--source`), `["stats", "--nope"]` → `main(argv)`.
- **Assert (per argv, in a `subTest`):** `SystemExit` with `code == 2`; stderr
  contains `"invalid choice"` or `"required"`; no `"Traceback"`.
- **Pins:** exact-match subcommand dispatch, currently a property of argparse's
  `_check_value` and nothing in this repo. `["sc"]` matters most: if it ever
  resolved to `scrape`, the CLI would silently make a live network call on a typo.

### C5 — `test_every_registered_command_dispatches_to_a_callable` (property)
- **Input:** `build_parser().parse_args([name, *extra])` for
  `[("run", []), ("scrape", ["--source", "osm"]), ("stats", [p]),
  ("validate", [p]), ("doctor", []), ("score", [p])]`.
- **Assert (per case):** `callable(ns.func)` and
  `ns.func.__name__ == f"cmd_{name}"`.
- **Pins:** all six `set_defaults(func=…)` sites (`cli.py:286, 291, 295, 299, 302,
  307`). This is the invariant that makes `cli.py:315` safe; a seventh subcommand
  added without `set_defaults` produces `AttributeError` at dispatch time.

### C6 — `test_handler_exit_code_is_returned_verbatim` (property: the exit-code table)
- **Input / expected:** drive `main()` and assert the exact `int`:

  | argv | fixture | expected |
  |---|---|---|
  | `["stats", missing]` | — | `2` |
  | `["validate", missing]` | — | `2` |
  | `["score", missing]` | — | `2` |
  | `["stats", p]` | clean row | `0` |
  | `["validate", p]` | clean row | `0` |
  | `["validate", p]` | `rating="9.9"` | `1` |
  | `["validate", p]` | `lat="10.0", lon="10.0"` | `1` |
  | `["score", p]` | clean row | `0` |
  | `["scrape", "--source", "osm"]` | `EmptyOsm` | `1` |
  | `["scrape", "--source", "osm"]` | `FakeOsm` yielding 2 rows | `0` |
  | `["doctor"]` | env cleared, `requests.get` → 200 | `1` |

  Also assert `isinstance(rc, int)` in every case.
- **Pins:** `int(args.func(args) or 0)` (`cli.py:315`) returning the handler's code
  unchanged. Note this test **documents a deliberate deviation** from
  `037-cli-design.md:463`, which reserves `5`/`EX_DATAERR` for validation failure;
  today `cmd_validate` returns `1` (`cli.py:201`). Writing the assertion makes a
  future change to `5` a deliberate edit rather than an accident.

### C7 — `test_falsy_handler_return_becomes_zero`
- **Input:** `mock.patch.object(cli, "cmd_stats", return_value=None)` →
  `main(["stats", p])`; repeat with `return_value=False`.
- **Assert:** returns `0`; no exception.
- **Pins:** the `or 0` half of `cli.py:315`. Load-bearing because `cmd_run`
  returns `main.py:148`'s `None`. See C8 for why that is a defect rather than a
  convenience.

### C8 — `test_run_cannot_signal_failure` · characterisation of the S2
- **Input:** `mock.patch.object(main.main, return_value=None)` →
  `main(["run"])`; and `mock.patch.object(main.main, side_effect=RuntimeError("Overpass 504"))`
  → `main(["run"])`.
- **Assert (current behaviour):** the first returns `0`; the second propagates
  `RuntimeError`. i.e. `leadminer run` has exactly two outcomes, 0 or a traceback.
- **Desired (write this as the failing test):** rc `4` when the pipeline collects
  nothing new, per `037-cli-design.md:462`. Reproduced today: all three scrapers
  failing → `EXIT CODE: 0`.

### C9 — `test_run_returns_the_pipelines_exit_code_if_it_ever_gains_one`
- **Input:** `mock.patch.object(main.main, return_value=4)` → `main(["run"])`.
- **Assert:** `rc == 4`.
- **Pins:** `cli.py:315` must forward a non-zero pipeline code once
  `main.py:148` stops returning `None`. Cheap forward-compatibility test; today it
  would fail, which is the point.

### C10 — `test_keyboard_interrupt_from_any_command_returns_130` (property: the only branch with no coverage)
- **Input:** for each of the six `(name, extra)` pairs from C5,
  `mock.patch.object(cli, f"cmd_{name}", side_effect=KeyboardInterrupt)` →
  `main([name, *extra])`.
- **Assert (per case):** `rc == 130`; `stderr == "\ninterrupted\n"`; `stdout == ""`;
  the mock's `call_count == 1` (no retry, no re-dispatch).
- **Pins:** `cli.py:316-318`, and 130 = 128 + SIGINT, which is what a shell
  reports for a Ctrl-C'd child. Verified for all six. This is the *entire* body of
  `main()`'s `except` clause and has never been executed by a test.

### C11 — `test_all_diagnostics_go_to_stderr_stdout_is_payload_only` (property)
- **Input:** `main(["stats", missing])`, `main(["validate", p])` with
  `rating="9.9"`, `main(["scrape", "--source", "osm"])` with `EmptyOsm`,
  `main(["doctor"])` with env cleared.
- **Assert (per case):** `rc != 0`; the diagnostic string (`"no such file"`,
  `"outside 0-5"`, `"zero records"`, `"MISSING"`) is in the **stderr** buffer and
  **not** in the stdout buffer.
- **Pins:** `cli.py:317` sends the interrupt marker to stderr, and
  `cli.py:83/139` do the same for file errors. The property matters because
  `leadminer stats data/x.csv > report.txt` must not interleave failure text into
  the report — while `write_csv`'s `Written:` line (`main.py:134`) legitimately
  goes to stdout.

### C12 — `test_no_command_ever_escapes_as_a_bare_traceback` (property) · closes the S1
- **Input:** each of the six commands, run with the HTTP layer broken
  (`mock.patch.dict(sys.modules, {…EVICTED…})` per implementation note 6).
- **Assert (per case):** the outcome is an `int` in `{0, 1, 2, 3, 130}` **and**
  `"Traceback"` not in stderr. Today: `stats` → 0 ✓, `validate` → 0 ✓,
  `doctor` → 1 ✓, `score` → raises `ModuleNotFoundError` ✗, `run` → raises ✗.
- **Pins:** `cli.py:314-318`. The comparative form is deliberate: `doctor`
  already satisfies this invariant (`cli.py:226-231`, comment at `cli.py:228-229`)
  while `score` and `run` do not, and that contradiction is the finding.

### C13 — `test_score_changed_count_covers_every_field_it_rewrites` · closes the S2
- **Input:** one row with `lead_score="40"` (already correct),
  `recommended_service="Discovery call - scope the right service"` (stale) →
  `main(["score", p])`.
- **Assert (desired, currently failing):** stdout contains `"1 changed"`.
  Current output: `"re-scored 1 records, 0 changed"` **and** the file's
  `recommended_service` becomes `"SEO audit + visibility upgrade"`.
- **Pins:** the counter at `cli.py:270-271` vs the rewrites at `cli.py:266-268`.

### C14 — `test_score_is_idempotent_and_leaves_the_master_intact`
- **Input:** a clean row; `main(["score", p])` twice.
- **Assert:** first run stdout contains `"1 changed"`; second run contains
  `"0 changed"`; `p.read_bytes()` is **byte-identical** before and after the
  second run. Second variant: `main(["score", src, "--out", dst])` leaves
  `src.read_bytes()` unchanged and `dst` parses.
- **Pins:** `write_csv`'s atomicity (`main.py:108-133`, the fix from commit
  `8a42a67` "atomic CSV writes") reached through the CLI, combined with
  `cmd_score`'s default in-place write (`cli.py:274-275`). Nothing today proves a
  re-score cannot churn months of cumulative leads. Both halves verified
  currently correct — this test exists so a future rule change cannot break them.

### C15 — `test_score_preserves_the_website_live_tri_state`
- **Input:** three one-row CSVs, identical except `website_live` ∈
  `{"True", "False", ""}` → `main(["score", p])` for each.
- **Assert (per case, after re-reading with `load_master`):**

  | `website_live` in | `website_live` out | `recommended_service` | `lead_score` |
  |---|---|---|---|
  | `"True"` | `True` | `"SEO audit + visibility upgrade"` | 40 |
  | `"False"` | `False` | `"Website rebuild + maintenance"` | 50 |
  | `""` | `None` | **not** a rebuild (RTYLR) | 30 |

- **Pins:** the lead-signal inversion fixed in commit `5222d32` and covered at
  the unit level by `tests/test_lead_signal.py:76-96`. What is missing is the
  *round-trip* proof: that `score` re-reading and re-writing the master does not
  collapse the blank back to `False` — the exact bug `cli.py:105-106` calls
  "the original bug". Verified correct today.

### C16 — `test_score_re_resolves_a_blank_country`
- **Input:** row with `country=""`, `phone="+966501234567"` → `main(["score", p])`.
- **Assert:** `rc == 0`; after `load_master`, `r["country"] == "SA"`.
- **Pins:** the dead-country-default fix from commit `8a42a67`, covered at unit
  level by `tests/test_lead_signal.py:130-153`. Missing: the proof that
  `cli.py:266`'s `resolve_country` call is actually reached on the CLI path.
  Verified `SA` today.

### C17 — `test_a_closed_stdout_is_not_a_traceback` · second half of the S1
- **Input:** a stdout stub whose `write()` raises `BrokenPipeError(32, …)` after
  N calls, for N ∈ {3, 5, 15} → `main(["stats", p])`.
- **Assert (desired, currently failing):** `rc in (0, 141)` and no
  `"Traceback"`. Current: `BrokenPipeError` escapes for every N.
- **Pins:** `cli.py:314-318`. `leadminer stats data/all_businesses.csv | head -3`
  is the canonical invocation for a 500k-row master and it currently tracebacks
  with exit 1 — indistinguishable from a data-quality failure.

### C18 — `test_broken_pipe_and_import_errors_are_distinguishable` (property, pairs C8/C12/C17)
- **Input:** `(["score", p] with HTTP layer broken)`,
  `(["stats", p] with closed stdout)`,
  `(["validate", p] with `rating="9.9"`)`.
- **Assert (per case):** the three outcomes carry **three distinct** exit codes.
  Today: `ModuleNotFoundError` → uncaught, `BrokenPipeError` → uncaught, bad
  rating → `1`. Two of the three have no code at all.
- **Pins:** the `EX_SOFTWARE` / `EX_USAGE` / `EX_DATAERR` separation required by
  `037-cli-design.md:456-464`. This is the single test that would have caught all
  three S1/S2 items, and it is the one to write first if only one gets written.

### C19 — `test_the_three_offline_commands_share_one_default_file` (property)
- **Input:** `build_parser().parse_args(["stats"] | ["validate"] | ["score"])`.
- **Assert (per case):** `ns.file == "data/all_businesses.csv"` and
  `isinstance(ns.file, str)`; all three values are equal.
- **Pins:** the three duplicated `str(DATA_DIR / …)` literals at `cli.py:294`,
  `cli.py:298`, `cli.py:305`. Verified identical today. No existing test touches a
  default path — `TestCli._fixture` (`tests/test_lead_signal.py:195-209`) always
  passes `file=…` explicitly.

### C20 — `test_a_bare_command_resolves_against_cwd` (characterisation of the S3)
- **Input:** `os.chdir(tempfile.mkdtemp())` (no `data/`), then `main(["stats"])`.
- **Assert (current, characterisation):** `rc == 2`; stderr ==
  `"no such file: data/all_businesses.csv\n"`.
- **Desired (the failing version):** either the path resolves relative to the
  install root, or the message names the cwd so the operator can tell "no data
  yet" from "wrong directory". Anchored to `cli.py:26` + `pyproject.toml:33`.

---

## Properties vs examples

Three real properties exist here; the rest are examples:

| Property | Cases | Why it is a property |
|---|---|---|
| No command ever escapes as a bare traceback | C12, C18 | The `try` at `cli.py:314` is meant to be exhaustive over the failure space. It is not, and each hole is a different exception type. Testing one instance proves nothing about the next. |
| Exit code is a total function of (command, condition) | C6, C18 | A scheduler's whole contract with this CLI is the code. A table of 11 examples *is* the property here; the point is that it is exhaustive over the six commands and never asserted on a subset. |
| Argparse rejection is uniform | C4, C5 | `required=True` + `choices` make the rejection space enumerable. One example (`["frobnicate"]`) would pass identically if `required=True` were removed. |

Everything else (C1, C3, C10, C13-C17, C19, C20) is genuinely a single-input
example — there is no generalisation worth asserting.

**Do not** write `@given`/hypothesis cases for `main()` itself. Its input domain is
six strings and a `Path`; the hypothesis value is in `lead_score`,
`recommend_service` and `normalize_phone`, which are already covered
(`tests/test_lead_signal.py:71-153`) and where `hypothesis>=6.100`
(`pyproject.toml:24`) would pay off.

---

## What genuinely cannot be tested here, and why

1. **`main(["run"])` and `main(["scrape", …])` end-to-end.** `cmd_scrape`
   constructs a real scraper (`cli.py:61`) and `cmd_run` (`cli.py:37`) invokes the
   whole three-thread pipeline — 68 Places queries plus 40 enrichment threads.
   Executing either is a network operation, forbidden by `BRIEF.md:73-74` and
   unacceptable in CI. Only *dispatch* is unit-testable, by patching
   `scrapers.<mod>.<Class>` and `main.main` respectively (C8, C9, C6's last two
   rows). The scrapers' own behaviour belongs in recorded-fixture replay, which
   `docs/audits/032-fixtures-offline-replay.md` already specifies.
2. **`doctor`'s connectivity verdicts.** `cli.py:239` issues three real
   `requests.get` calls with a 5s timeout. The verdict arithmetic
   (`status_code < 500` → OK else WARN, `cli.py:240-241`) **is** testable with a
   stub returning 200/503; actual reachability of Overpass, Wikidata and Google
   Places is not, from a unit test or from CI. Note `cli.py:242-243` catches
   broadly and prints only `type(e).__name__`, so even a test can distinguish
   `ConnectionError` from `Timeout` but cannot distinguish "we are offline" from
   "a proxy returned 403".
3. **A real SIGINT.** C10 injects `KeyboardInterrupt` as an exception rather than
   sending `signal.SIGINT`. Sending a genuine signal requires a subprocess
   (`python -c "import signal; signal.raise_signal(signal.SIGINT)"` under a
   handler installed by `cli.main`), which is an integration test with its own
   timing flake profile. The injection is sufficient: `except KeyboardInterrupt`
   matches both.
4. **Whether `leadminer` works as an installed command.** That needs a venv and
   `pip install`, which `BRIEF.md:75` forbids here and which belongs in CI, not
   in a unit suite. It is also not obviously going to pass:
   `pyproject.toml:35-37` sets `include = ["scrapers/"]` for a wheel whose entry
   point is the top-level module `cli` (`pyproject.toml:33`), and there is no
   `__init__.py` at the root (only `scrapers/__init__.py`, and it is empty).
   C1 is the closest unit-level proxy — it proves the dispatch, not the install.
5. **Whether exit code 1 from a traceback is distinguishable from exit code 1 from
   `cmd_validate`.** This is not a testability gap, it is the S1 finding. No test
   can fix it; only C18 failing and `cli.py:314-318` changing can.
6. **Import-order robustness.** During this audit a transient edit by another
   agent briefly produced a circular import
   (`enricher` → `scrapers.base` → `scrapers/__init__.py` → `google_places` →
   `from enricher import infer_region`) that made `import enricher` fail
   depending on which module was imported first. That state is not in HEAD —
   `scrapers/__init__.py` is empty and `enricher.py` does not import `scrapers` —
   so I am not reporting it as a finding. But it shows that `main()`'s lazy
   per-command imports (`cli.py:35, 46-49, 249-252`) make CLI behaviour depend on
   which subcommand runs first, and a test that imports every command module in
   one process would catch a regression there.

---

## Recommended order of work

1. **C1** (`argv=None`) — 5 lines, covers the only path real users take.
2. **C18 + C12** — the property that failed; it is the executable form of the S1.
3. **C8 + C9** — `leadminer run` returning 0 after total scraper failure. Fix
   `cli.py:37`/`:315`, then flip the characterisation to the assertion.
4. **C10** — the KeyboardInterrupt branch, one `subTest` loop over six commands.
5. **C13** — the `0 changed` lie; one-line fix at `cli.py:270-271`.
6. **C6** — the exit-code table, so any later change to a code is deliberate.
7. **C2, C3, C4, C5, C19** — the parser invariants, cheap and they stop a future
   subcommand from breaking dispatch.
8. **C14, C15, C16** — the master-integrity and round-trip regressions that reach
   `main()` only through `score`.
9. **C11, C17, C20** — stream hygiene and the cwd-relative default.

Items 1-5 are the ones that change production behaviour. Items 6-9 are
regression insurance and can follow in a single follow-up commit.
