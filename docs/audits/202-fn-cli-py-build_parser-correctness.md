# 200 — `build_parser` correctness (`cli.py:280`)

**Lens:** read `build_parser()` and every caller; execute the parser and the
dispatched handlers against concrete inputs rather than reasoning abstractly.

**Method:** `import cli` and drive `build_parser().parse_args(...)` directly, plus
run the dispatched commands against fixture CSVs in a throwaway sandbox
(`/private/var/.../opencode/cliaudit`, outside the repo). No network: `requests`
was stubbed with a module that raises on any call, and the three endpoint probes
were never reached. Repo source is unmodified (`git status --porcelain` on
`cli.py main.py dedup.py enricher.py pitch_recommender.py scrapers/ httpclient.py
pyproject.toml README.md` is empty).

---

## Verdict

`build_parser()` itself is structurally sound — it is pure, idempotent, and its
one load-bearing invariant (`required=True` on the subparsers, `cli.py:283`) is
correctly what makes `args.func(args)` at `cli.py:315` safe. **The parser is not
where the bugs are; the argument *contracts* it declares are.** Two of the three
biggest problems are the parser advertising a guarantee it does not deliver:
`score` is labelled "no network" (`cli.py:304`) but imports the entire scraper +
HTTP stack and dies with an `ImportError` traceback in exactly the broken-
environment case it exists to diagnose; and `score`'s `--out` is optional
(`cli.py:306`) so the default is a destructive in-place rewrite that **silently
deletes every CSV column not in `main.FIELDS` and exits 0**.

The single most consequential item is the silent column deletion (S1-1): it is
data loss with a success exit code and no warning line.

---

## Findings

### S1-1 — `leadminer score <file>` with no `--out` silently deletes non-schema columns, exits 0

- **Where:** `cli.py:274` (`out = pathlib.Path(args.out) if args.out else path`),
  enabled by `cli.py:304-307` (`--out` is optional with no in-place opt-in),
  landing in `main.py:125` (`csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")`)
  and `main.py:130` (`os.replace(tmp, path)` — same path, so it replaces the input).
- **Breaks:** `cmd_score` rewrites the master CSV in place through `write_csv`,
  which projects every row onto the hard-coded 22-column `main.FIELDS` and
  discards anything else via `extrasaction="ignore"`. Any column an operator or
  a later migration added to the cumulative master — `notes`, `last_contacted`,
  `owner`, `do_not_contact`, `enriched_at` — is gone after one `leadminer score`,
  with exit code 0 and a success message. The cumulative master is the product
  (BRIEF: "cumulative master, all time"), and `run` (`cli.py:285`) never touches
  it destructively, so `score` is the only command that can quietly shrink it.
- **Trigger:** input CSV with 25 columns — the 22 in `main.FIELDS` plus
  `notes="called 2026-01-05"` and `last_contacted="2026-02-01"` — then:

  ```
  $ leadminer score master.csv
  ```

- **Exact wrong output (executed):**

  ```
  BEFORE cols: 25
  BEFORE hdr : ...,source,scraped_at,notes,last_contacted
  BEFORE note: ,,called 2026-01-05,2026-02-01

    Written: master.csv (1 records)
  re-scored 1 records, 0 changed -> master.csv
  rc= 0

  AFTER  cols: 23
  AFTER  hdr : ...,source,scraped_at
  EXTRA COLUMNS SURVIVED: False
  ```

  25 columns in, 23 out, `rc=0`, and no line anywhere says a column was dropped.
  The `0 changed` also confirms the loss is entirely invisible in the report line
  at `cli.py:276`.
- **Fix:** make `--out` required (`s.add_argument("--out", required=True)`) and
  add an explicit opt-in `--in-place` that prints `f"{n} columns dropped: {...}"`
  before writing.

---

### S2-1 — `leadminer score` is documented "no network" but hard-crashes on `import requests`

- **Where:** `cli.py:249` (`import main as pipeline`) and `cli.py:250`
  (`from enricher import lead_score`). Import chain, all at module scope:
  `cli.py:249` → `main.py:25` (`from scrapers.osm import OSMScraper`) →
  `scrapers/osm.py:4` (`from httpclient import fetch_with_retry, ...`) →
  `httpclient.py:43` (`import requests`). Also `cli.py:250` → `enricher.py:2`
  (`import requests`).
- **Breaks:** the parser's own help string (`cli.py:304`,
  `"re-score an existing CSV (no network)"`) and the module docstring
  (`cli.py:14-15`, "Commands other than `run`/`scrape`/`doctor` never touch the
  network, so they are safe to run anywhere and are what you reach for when a run
  looks wrong") are both false for `score`. `stats` and `validate` really are
  stdlib-only — I verified they import zero project modules
  (`httpclient`, `enricher`, `scrapers.*` all absent from `sys.modules` after
  `leadminer validate master.csv` returns `rc=0`). `score` is the sole outlier
  because it drags in the scraper stack for two pure functions.
  The failure mode is the painful one: `cmd_score` dies with an unhandled
  traceback whose deepest frames are `scrapers/osm.py` and `httpclient.py`, so
  the operator is sent hunting in the scraping layer for a command whose help says
  it does no scraping.
- **Trigger:** any environment where `requests` is not importable — a fresh
  clone that has not been `pip install`ed, a venv where `requests` was removed, or
  a container built from `pyproject.toml` with a partial install. Then:

  ```
  $ leadminer score data/all_businesses.csv
  ```

- **Exact wrong output (executed, `requests` genuinely absent from this env):**

  ```
    File "/Users/mhomsi/dev/dummy/leadminer/cli.py", line 315, in main
      return int(args.func(args) or 0)
    File "/Users/mhomsi/dev/dummy/leadminer/cli.py", line 249, in cmd_score
      import main as pipeline
    File "/Users/mhomsi/dev/dummy/leadminer/main.py", line 25, in <module>
      from scrapers.osm import OSMScraper
    File "/Users/mhomsi/dev/dummy/leadminer/scrapers/osm.py", line 4, in <module>
      from httpclient import fetch_with_retry, get_session, utc_now_iso
    File "/Users/mhomsi/dev/dummy/leadminer/httpclient.py", line 43, in <module>
      import requests
  ModuleNotFoundError: No module named 'requests'
  ```

  exit code 1, raw traceback.
- **Why not S1, and when to escalate:** in a correctly installed environment this
  is dead import weight, not a failure, so on its own it is operability rather
  than wrong output. It becomes S1 under the project's *own* stated standard,
  which the author already wrote down for `doctor` at `cli.py:228-229`: *"doctor
  exists to diagnose a broken environment, so it must never be the thing that
  tracebacks."* `doctor` implements that with an explicit `except ImportError`
  (`cli.py:226-231`) and a clean `pip install -e '.[dev]'` message (`cli.py:230`).
  `cmd_score` reintroduces the identical failure it was guarded against. **If
  you hold `score` to the same bar as `doctor`, this is S1.** I rate it S2 on the
  narrow reading that it needs a broken env to fire.
- **Fix:** extract `load_master` / `resolve_country` / `write_csv` plus
  `lead_score` / `industry_priority` / `recommend_service` into a `scoring.py`
  with no scraper imports, and have `cmd_score` (`cli.py:249-252`) import only
  that.

---

### S2-2 — Every default in the parser is relative to `$PWD`, so `data/` is found by accident

- **Where:** `cli.py:26` (`DATA_DIR = pathlib.Path("data")`), consumed as the
  default `file` for three subparsers at `cli.py:294`, `cli.py:298`,
  `cli.py:305`, and again by `cmd_doctor` at `cli.py:217-219`. The same relative
  constant is duplicated verbatim at `main.py:41`.
- **Breaks:** `leadminer` is a console script (`pyproject.toml:33`), so it runs
  from whatever cwd the operator, cron job, or CI step happens to be in. The
  defaults are only correct from the repo root. Three distinct wrong behaviours,
  in increasing severity:
  1. The read commands report a file that does not exist.
  2. `doctor` — whose docstring (`cli.py:205`) is *"Check credentials and
     connectivity before blaming the data"* — prints a **factually false
     statement** and still returns 0.
  3. `run` creates a *new*, empty `data/` in the new cwd, so
     `main.py:150` `load_master(DATA_DIR / "all_businesses.csv")` finds nothing
     and the cumulative master silently restarts from empty. Two copies of the
     same relative constant (`cli.py:26`, `main.py:41`) can also drift apart.
- **Trigger:** `cd` anywhere that is not the repo root, then `leadminer stats`.
  (Reproduced from `/private/var/.../opencode/cliaudit`, an empty directory.)
- **Exact wrong output (executed):**

  ```
  $ cd /private/var/.../cliaudit && leadminer stats
  no such file: data/all_businesses.csv
  rc = 2

  $ leadminer validate
  no such file: data/all_businesses.csv
  rc = 2
  ```

  and `leadminer doctor` from the same cwd (executed with a `requests` stub, so
  the three connectivity probes never touched the network):

  ```
  data directory:
    MISSING  data (no run has produced output yet)
  ```

  The parenthetical is a lie — output does exist, it is just not under `$PWD`.
- **Fix:** `DATA_DIR = pathlib.Path(__file__).resolve().parent / "data"` at
  `cli.py:26`, import it in `main.py` instead of redefining it at `main.py:41`,
  and add a global `--data-dir` option so a caller can override.

---

### S2-3 — `doctor` exits 0 with `SCRAPER_EMAIL` unset, so CI gating on it is a false green

- **Where:** `cli.py:213` (`if var == "GOOGLE_PLACES_API_KEY" and not present:`).
  Only that one variable can set `ok = False`.
- **Breaks:** `SCRAPER_EMAIL` is a real, consumed input — `httpclient.py:81`
  reads it and `httpclient.py:84-85` builds the `User-Agent` with
  `tail = f" ({email})" if email else ""`. So when it is unset the tool silently
  ships an anonymous UA to Overpass and Wikidata, contradicting the policy
  requirement the code's own comment cites at `httpclient.py:75-77`
  (*"Overpass and Wikidata both ask for a contact address in the UA"*). `doctor`
  prints `MISSING  SCRAPER_EMAIL`, so the information is not hidden — but it
  prints it while returning success, which is the only thing a CI step reads.
- **Trigger:** `GOOGLE_PLACES_API_KEY=k leadminer doctor` with `SCRAPER_EMAIL`
  unset from the environment.
- **Exact wrong output (executed):**

  ```
  credentials:
    MISSING  GOOGLE_PLACES_API_KEY
    MISSING  SCRAPER_EMAIL
  ...
  rc = 1
  ```

  `rc = 1` here comes *only* from the API key. Set it and the exit code flips to
  0 with `MISSING  SCRAPER_EMAIL` still on screen.
- **Note:** this is in the dispatched handler rather than in `build_parser`, but
  it is on the parser's dispatch surface and the parser is what presents `doctor`
  as the pre-flight gate.
- **Fix:** `if not present: ok = False` inside the loop, dropping the
  `var == "GOOGLE_PLACES_API_KEY"` special case.

---

### S3-1 — `leadminer` with no arguments prints an internal dest name instead of help

- **Where:** `cli.py:283` (`sub = p.add_subparsers(dest="cmd", required=True)`).
  `dest` is the namespace key, but with no `metavar` argparse also uses it as the
  user-facing name in the required-argument error.
- **Breaks:** the one thing a first-time user types leaks a Python identifier.
- **Trigger:** `leadminer` with an empty argv.
- **Exact wrong output (executed):**

  ```
  usage: leadminer [-h] {run,scrape,stats,validate,doctor,score} ...
  leadminer: error: the following arguments are required: cmd
  exit 2
  ```

- **Fix:** `metavar="COMMAND"` plus a `p.error` override that calls
  `self.print_help()` when `argv` is empty.

---

### S3-2 — `--source` choices duplicate the `cmd_scrape` registry; `cli.py:56-59` is unreachable

- **Where:** `cli.py:290` (`choices=["osm", "wikidata", "google_places"]`) versus
  the `registry` dict at `cli.py:51-55`. I confirmed the two lists are byte-identical
  and that `cmd_scrape` re-checks `if args.source not in registry` at `cli.py:56`.
- **Breaks:** the re-validation can never fire — argparse rejects a bad value
  before `cmd_scrape` runs. So the live failure mode is the *opposite* of what
  the dead code implies: add a fourth scraper to `registry` and it becomes
  unreachable from the CLI, with no error at the call site to explain why.
- **Trigger:** add `"serpapi": SerpApiScraper` to `registry` (`cli.py:51-55`) and
  run it.
- **Exact wrong output (executed):**

  ```
  leadminer scrape: error: argument --source: invalid choice: 'serpapi'
    (choose from 'osm', 'wikidata', 'google_places')
  exit 2
  ```

  …even though `registry['serpapi']` exists and `cmd_scrape` would run it.
- **Fix:** hoist `registry` to module scope and pass `choices=sorted(registry)` at
  `cli.py:290`, deleting the duplicate check at `cli.py:56-59`.

---

### S3-3 — `leadminer run --help` is empty, and the `RawDescriptionHelpFormatter` never reaches subparsers

- **Where:** `cli.py:285-286` creates `run` with `help=` but no `description=`.
  Separately, `cli.py:282` sets `formatter_class=RawDescriptionHelpFormatter` on
  the parent only.
- **Breaks:** (a) `run` — the only command that costs money and takes minutes —
  has no help body. (b) I verified every subparser is constructed with plain
  `HelpFormatter`, not the parent's `RawDescriptionHelpFormatter`, because
  `add_subparsers` picks `parser_class=type(self)`, which is the bare
  `argparse.ArgumentParser`. Harmless today (no subparser has a multi-line
  description) but it will silently surprise the next person who adds one.
- **Trigger:** `leadminer run --help`.
- **Exact wrong output (executed):**

  ```
  usage: leadminer run [-h]

  options:
    -h, --help  show this help message and exit
  ```

- **Fix:** give `run` a `description=`, and pass
  `parser_class=functools.partial(argparse.ArgumentParser,
  formatter_class=argparse.RawDescriptionHelpFormatter)` to `add_subparsers` —
  note that `add_subparsers(formatter_class=...)` raises
  `TypeError: _SubParsersAction.__init__() got an unexpected keyword argument
  'formatter_class'` (verified), so `parser_class` is the only route.

---

### S3-4 — `--out` pointing at a directory produces an unhandled `IsADirectoryError`

- **Where:** `cli.py:274-275`. `main.py:122` computes
  `tmp = path.with_suffix(path.suffix + ".tmp")`; with no suffix that is
  `outdir.tmp` in the *current* directory, and `main.py:130` `os.replace` then
  fails. `main()` (`cli.py:314-318`) catches only `KeyboardInterrupt`.
- **Breaks:** a stack trace where a one-line error belongs. Partial credit: the
  stray `outdir.tmp` *is* cleaned up by `main.py:132`, which I confirmed — no
  litter left behind.
- **Trigger:** `leadminer score master.csv --out outdir` where `outdir` exists
  as a directory.
- **Exact wrong output (executed):**

  ```
  IsADirectoryError: [Errno 21] Is a directory: 'outdir.tmp' -> 'outdir'
  ```

  exit code 1, full traceback. (`outdir.tmp` cleaned up; `outdir` left empty.)
- **Fix:** in `cmd_score`, `if out.is_dir(): print(f"--out is a directory: {out}",
  file=sys.stderr); return 2` before `pipeline.write_csv(out, records)`.

---

### S3-5 — `--source` rejects every spelling a human would actually type

- **Where:** `cli.py:290`.
- **Breaks:** `google_places` is the only accepted spelling of the KSA source,
  which is the one command in this CLI that a sales-ops person is likely to run
  by hand.
- **Trigger / exact output (all executed, each `exit 2`):**

  ```
  $ leadminer scrape --source google-places
  leadminer scrape: error: argument --source: invalid choice: 'google-places'
    (choose from 'osm', 'wikidata', 'google_places')
  $ leadminer scrape --source OSM
  leadminer scrape: error: argument --source: invalid choice: 'OSM'
    (choose from 'osm', 'wikidata', 'google_places')
  ```

- **Implementation trap, verified:** you cannot fix this with
  `type=str.lower` — argparse runs `_check_value` **before** the `type` callable,
  so `type=lambda v: v.replace("-", "_")` is dead code and still exits 2 on
  `'b_c'`.
- **Fix:** `choices=["osm", "wikidata", "google_places", "google-places",
  "places"]` and add the alias keys to `registry` at `cli.py:51-55`.

---

## Not a bug, but worth knowing

- **`build_parser()` is pure and idempotent.** Verified: two calls return
  independent trees with no shared mutable state, and importing `cli` has no
  side effects (the network-touching imports are all function-local,
  `cli.py:35/46-49/206/226/249-252`). Safe to call from tests.
- **The invariant that makes `main()` safe:** `required=True` on
  `cli.py:283` guarantees `args.func` is always bound by the subparser's
  `set_defaults`, so `cli.py:315` `int(args.func(args) or 0)` cannot raise
  `AttributeError`. If anyone ever relaxes that to `required=False`, the
  invariant breaks silently — there is no `getattr(args, "func", None)` guard.
- **Latent abbreviation trap.** Option abbreviation is on by default and is
  currently safe only by coincidence of naming: `--o` → `--out` and `--s` →
  `--source` both resolve uniquely. Add a second option to `score` starting with
  `--o` (e.g. `--out-dir`) and `--o` silently becomes ambiguous and starts
  failing. Consider `allow_abbrev=False` on the subparsers.
- **Subcommand names are matched exactly; option names are not.** Verified:
  `leadminer sco` → `invalid choice: 'sco'`, exit 2, while `leadminer score -o x`
  is accepted. Inconsistent-feeling, but standard argparse and not worth changing.
- **`prog="leadminer"` is hardcoded** (`cli.py:281`), so `python cli.py --help`
  prints `usage: leadminer ...`. Cosmetic, and correct for the packaged entry
  point at `pyproject.toml:33`.
- **Dash-prefixed paths need `--`.** `leadminer stats -data/x.csv` → `unrecognized
  arguments: -data/x.csv`; `leadminer stats -- -data/x.csv` works. Expected
  argparse behaviour, noted so nobody files it as a bug.
- **Exit codes are consistent and well-chosen:** `0` success, `1` a check failed
  or a source returned zero records (`cli.py:66`), `2` usage/IO error, `130`
  SIGINT (`cli.py:318`). `130` in particular is correct and worth keeping.
- **`--source` is `required=True`** (`cli.py:289`), so `leadminer scrape` alone
  exits 2 with `the following arguments are required: --source` rather than
  defaulting to a source. Correct choice — the cost is not implicit.
- **`stats`/`validate` really are dependency-free.** Verified they import zero
  project modules. Only `score` drags in the scraper stack (S2-1).
- **Adjacent packaging risk, unverified, overlaps report 030.**
  `pyproject.toml:33` declares the entry point `leadminer = "cli:main"`, but
  `pyproject.toml:35-37` sets `[tool.hatch.build.targets.wheel] include = ["scrapers/"]`.
  Per Hatch's build docs, `include`/`exclude` "select **exactly** which files
  will be shipped in each build", and "**if no file selection options are
  provided**, then what gets included is determined by each build target" — i.e.
  specifying `include` overrides the default selection. If that reading is right,
  the wheel ships `scrapers/` only, and `cli.py` — the module the console script
  imports — is absent, so `leadminer` is an entry point pointing at a module that
  is not installed. I did **not** build a wheel to confirm (hatchling is not
  installed and the brief forbids installs), so treat this as a pointer for 030
  rather than a confirmed defect. One command settles it: `hatch build && unzip -l
  dist/*.whl | grep -c 'cli\.py'`.

---

## Recommended order of work

1. **S1-1** — require `--out` for `score`, or add an explicit `--in-place` that
   reports the columns it is about to drop. One-line parser change; removes
   silent data loss from the cumulative master. Do this first.
2. **S2-1** — split the pure scoring helpers into a `scoring.py` with no scraper
   imports so `score` keeps its promised "no network / safe anywhere" contract.
   Also gives `doctor`'s existing `except ImportError` (`cli.py:226-231`) a
   counterpart in `score`.
3. **S2-2** — make `DATA_DIR` absolute and de-duplicate the copy at `main.py:41`;
   add `--data-dir`. Fixes three wrong behaviours with one edit.
4. **S2-3** — `if not present: ok = False` in the `doctor` credential loop.
5. **S3-4** — guard `--out` against directories; it is the only uncaught-exception
   path reachable from the CLI surface.
6. **S3-1 / S3-2 / S3-3 / S3-5** — the argument-surface polish: `metavar` + help
   on empty argv, derive `choices` from the registry, describe `run`, accept
   `google-places`. All cosmetic relative to 1-5; bundle into one pass.
