# 200 — `build_parser` boundary & hostile-input audit

**Target:** `build_parser` at `cli.py:280`, plus the contract it encodes.
**Method:** every input below was executed against the real `cli.main(argv)` on
Python 3.14.8 / macOS. Nothing in the repo was modified; `requests` was stubbed
*in memory* only where noted, to reach `cmd_score`.

## Verdict

`build_parser` itself is **clean** — it takes no arguments, is side-effect free,
does not leak state between calls, validates its one enum, and gives every
subcommand a handler. Nothing hostile makes *it* raise.

The damage is entirely in what it **declines to validate**: the `file` and `--out`
arguments are accepted as raw strings, so a single token — `leadminer score
<path>` — reaches `write_csv` and irreversibly overwrites the target with
leadminer's 23-column schema, **exiting 0** and printing a reassuring
`re-scored 2 records, 2 changed`. Compounding it, the sibling command documented
as the data-quality gate (`cli.py:136`, `README.md:14`) prints **"all checks
passed"** for a 5,000-row master in which **zero** rows have a phone, email, or
website. Both failures are silent and both block trust in the output.

The single most dangerous input is `leadminer score <path>` — see
[Most dangerous input](#most-dangerous-input) below.

---

## Contract of the target

```python
def build_parser() -> argparse.ArgumentParser:      # cli.py:280
```

There is **no input surface**. `build_parser()` accepts nothing, so the hostile
inputs that matter are two indirect ones:

1. **What it accepts without checking.** `file` (`cli.py:294`, `:298`, `:305`)
   and `--out` (`cli.py:306`) have no `type=`, no `choices=`, no existence or
   is-file check. Whatever string the shell produces is placed verbatim into the
   `Namespace` and handed to a handler.
2. **What it bakes in.** The defaults at `cli.py:294`, `:298`, `:305` are
   computed from `DATA_DIR = pathlib.Path("data")` (`cli.py:26`) — **relative**,
   therefore CWD-dependent — and `--out`'s implicit default is *"destroy the
   input"* (`cli.py:274`: `out = pathlib.Path(args.out) if args.out else path`).

Its caller, `main` (`cli.py:312-318`), catches **only** `KeyboardInterrupt`. So
any exception from any handler becomes a raw traceback and process exit 1, while
argparse errors exit 2 and handled conditions exit 0/1/2. There are three
different failure exit codes and no way for a caller to tell them apart.

---

## Findings

### S1 — `leadminer score <path>` overwrites the input with leadminer's schema and exits 0

- **Where:** `cli.py:306` (`--out` has no default → `None` → in-place branch at
  `cli.py:274`), `cli.py:259` (`load_master` with no header check),
  `cli.py:275` → `main.py:125` (`extrasaction="ignore"`), `main.py:130`
  (`os.replace` over the input).
- **Breaks:** `cmd_score` reads `args.file`, re-scores, and writes **back to
  `args.file`** unless `--out` is truthy. Nothing checks that the file is a
  leadminer master. `csv.DictReader` will happily read any header, and
  `write_csv` emits only `main.FIELDS`, so every column not in `FIELDS` is
  **silently dropped**, and every row is relabelled with inferred
  `country`/`lead_score`/`industry_priority`/`recommended_service`.
- **Trigger** — a three-line contacts list:
  ```
  $ leadminer score ~/Downloads/contacts.csv
  first_name,last_name,email
  Ada,Lovelace,ada@x.io
  Grace,Hopper,g@y.io

  $ cat ~/Downloads/contacts.csv
  name,category,region,country,...,email,...,lead_score,industry_priority,recommended_service,...
  ,,,LB,,,,,ada@x.io,...,20,low,Full digital launch (brand + website + social setup),,
  ,,,LB,,,,,g@y.io,...,20,low,Full digital launch (brand + website + social setup),,

  exit=0
  Written: /Users/x/Downloads/contacts.csv (2 records)
  re-scored 2 records, 2 changed -> /Users/x/Downloads/contacts.csv
  ```
  `first_name` and `last_name` are gone; two people are now rows claiming
  `country=LB`, `lead_score=20`, "Low priority, pitch a full digital launch".
  Exit code 0. No backup. No `--dry-run`.
- **Fix:** require an explicit destination (`--out` mandatory, or refuse when
  `args.out is None`), verify the header contains `main.FIELDS` before writing,
  and report dropped columns as an error rather than `extrasaction="ignore"`.

### S1 — `leadminer validate` prints "all checks passed" on a master with zero contacts

- **Where:** `cli.py:148-201`. The four checks are: any non-blank `name`
  (`:151`), BOM in names (`:154`), coordinates inside the two bounding boxes
  (`:164-173`), rating in 0–5 (`:175-188`), and >5% duplicate `(name, phone)`
  (`:190`). There is **no schema check, no completeness check, and no volume
  check**. Critically, `cli.py:168` `continue`s past any row whose `lat`/`lon`
  fail to parse, so **absent coordinates are indistinguishable from correct
  ones** — the coordinate check can only ever catch *wrong* coords, never
  *missing* ones.
- **Breaks:** the command documented at `cli.py:136` as *"Cheap data-quality
  gate. Exits non-zero if checks fail"* and at `README.md:14` as the
  *"data-quality gate, exits non-zero on failure"* passes a completely
  worthless dataset. These all exit 0 with `all checks passed`:
  - 5,000 rows, 23 correct columns, in-box coordinates, **0 with phone, 0 with
    email, 0 with website, 0 with any social handle** (514 KB).
  - 3-column CSV (`name,phone,scraped_at`) with no `region`/`lat`/`lon` at all.
  - Correct 23-column header plus one junk row `row_without_enough,cols` — every
    field empty except two.
  - A file whose entire body is four NUL bytes (header `name,category`) — the
    "name" is `\x00\x00\x00\x00`, which `.strip()` leaves non-blank.
  - A master that collapsed from 500,000 rows to 1. CI's own shell guard at
    `scrape.yml:78-83` catches that one; `validate` does not.
- **Trigger:** `leadminer validate data/all_businesses.csv` on the 5,000-row
  zero-contact file →
  ```
  checked 5,000 rows in .../no_contacts.csv
    all checks passed
  exit=0
  ```
  while `leadminer stats` on the *same file* reports
  `with phone: 0 / with email: 0 / with website: 0`. The two commands
  contradict each other and the gate picks the wrong answer.
- **Fix:** assert the header equals `main.FIELDS`; add a
  `rows with >= 1 contact channel` check (this is the `completeness_score >= 1`
  definition from `BRIEF.md:52`) and a `--min-rows` floor; count rows whose
  `lat`/`lon` are blank as a *problem* rather than `continue`-ing past them.

### S2 — `leadminer stats ''` dies with `IsADirectoryError`, not exit 2

- **Where:** `cli.py:82` and `cli.py:137` guard with `path.exists()`. But
  `pathlib.Path("")` → `PosixPath('.')`, and `Path('.').exists()` is `True`, so
  the guard passes and `open()` at `cli.py:86` / `cli.py:142` explodes.
- **Breaks:** the repo already encodes the intent that this must not traceback —
  `tests/test_lead_signal.py:255-264` is literally named
  `test_missing_file_is_an_error_not_a_traceback`. It only covers the
  *nonexistent* path, so `''`, `.`, `..`, and `/` all still traceback.
- **Trigger:**
  ```
  $ leadminer stats ''
  IsADirectoryError: [Errno 21] Is a directory: '.'
      File "cli.py", line 86, in cmd_stats
        with open(path, newline="", encoding="utf-8-sig") as f:
  ```
  Same for `leadminer stats .`, `leadminer stats ..`, `leadminer stats /`, and
  `leadminer validate` with any of those. Process exits **1**, not the 2 the
  neighbouring `if not path.exists()` branch returns — so a script cannot tell
  "bad argument" from "internal error".
- **Fix:** one line in a shared `_resolve_csv(arg)` helper —
  `if not path.is_file(): ... return 2`.

### S2 — Non-UTF-8 or non-text input crashes both `stats` and `validate`

- **Where:** `cli.py:86` and `cli.py:142` both use `encoding="utf-8-sig"` with no
  `errors=` policy. `utf-8-sig` decodes strictly.
- **Breaks:** the file argument is unvalidated, so anything the operator (or a
  `find`, or a shell glob) can produce reaches the decoder. All of these raise
  `UnicodeDecodeError` with a traceback, in *both* commands:
  - a **latin-1** CSV (`0xe9` in `Café`) — position 224
  - a **UTF-16** CSV (`0xff` at position 0)
  - a **gzip** archive — `0x8b` at position 1
  - arbitrary **binary** — `0x80` at position 128
  - a **renamed .xlsx** (`PK\x03\x04`) happens to be caught only because its
    lone row parses as empty
- **Trigger:** `leadminer stats /tmp/gdrive_dump.csv.gz`
- **Fix:** `errors="replace"` for the read path, plus a magic-byte sniff
  (`PK` → zip, `\x1f\x8b` → gzip, `\xff\xfe`/`\xfe\xff` → UTF-16) with a clean
  exit-2 message.

### S2 — `leadminer score` cannot run in a non-installed checkout

- **Where:** `cli.py:249` bare-imports `main as pipeline`; `main.py:25-27`
  imports the scrapers; `scrapers/osm.py:4` imports `httpclient`;
  `httpclient.py:43` does `import requests`. `requests` is a declared dependency
  (`pyproject.toml:17`) but is **not installed in this checkout**, and there is no
  venv.
- **Breaks:** in the repo as committed, `leadminer score` — documented at
  `cli.py:12` and `README.md:16` as the safe triage command — terminates with a
  bare traceback whose deepest frame is `httpclient.py:43`, four files away from
  anything an operator would look at. `cmd_doctor` guards *exactly* this
  condition 18 lines earlier in spirit (`cli.py:227-231`,
  `# doctor exists to diagnose a broken environment, so it must never be the
  thing that tracebacks`) — the two commands disagree about what "must not
  traceback" means.
- **Trigger:** `python3 -c "import cli; cli.main(['score','x.csv'])"` →
  `ModuleNotFoundError: No module named 'requests'`
- **Fix:** hoist the `try: import requests / except ImportError` guard into a
  shared helper and call it from `cmd_score`, `cmd_scrape`, and `cmd_run` too;
  or better, move `load_master`/`write_csv` out of `main.py` into a module with
  no scraper imports so `score` genuinely has no HTTP dependency.

### S2 — `--out` edge cases invert or scatter the operator's intent

- **Where:** `cli.py:274` tests **truthiness** (`if args.out`), not
  `is not None`. `cli.py:306` supplies no `type=`.
- **Breaks / triggers:**
  - `--out ''` → falsy → **writes in place**, exit 0. The operator asked to
    "write elsewhere"; the tool overwrote the source instead and said
    `re-scored 1 records, 1 changed -> .../src.csv`. Verified: the input file's
    bytes changed.
  - `--out '   '` → truthy → writes a file **literally named three spaces** into
    `$CWD`, exit 0, printing `Written:     (1 records)`.
  - `--out .` → `ValueError: PosixPath('.') has an empty name` traceback from
    `main.py:122` (`path.with_suffix(...)`).
  - `--out /dev/null` → `PermissionError: [Errno 1] ... '/dev/null.tmp'`
    traceback from `main.py:124`.
  - `--out relative_out.csv` → resolved against **$CWD**, not the input file's
    directory. Verified: the file appeared in `$CWD`, not next to the input.
- **Note:** the last two are `main.write_csv` bugs, but they are only reachable
  because `build_parser` accepted `--out .` and `--out /dev/null` unchallenged.
- **Fix:** `out = pathlib.Path(args.out) if args.out is not None else None`, then
  `if out is None or out.resolve() == path.resolve(): error or require --force`.
  Add a `type=` that rejects blank/whitespace and directories.

### S2 — Every default is CWD-relative and none of them appear in `--help`

- **Where:** `cli.py:26` (`DATA_DIR = pathlib.Path("data")`), consumed at
  `cli.py:294`, `:298`, `:305`.
- **Breaks:** `leadminer stats`, `leadminer validate`, and `leadminer score` with
  no arguments resolve `data/all_businesses.csv` against the **process CWD**.
  Under `cron`, `systemd`, `launchd`, `make`, or `python -m` from anywhere but
  the repo root, all three print `no such file: data/all_businesses.csv` and exit
  2 — verified from two different CWDs. Worse for `score`: if the *wrong* CWD
  happens to contain a `data/all_businesses.csv`, the destructive in-place
  rewrite of the previous finding lands on that file instead.
- **Breaks (discoverability):** the `file` positional is registered with no
  `help=` text at `cli.py:294`/`:298`/`:305`, and the parser uses
  `RawDescriptionHelpFormatter` (`cli.py:282`) rather than
  `ArgumentDefaultsHelpFormatter`, so argparse prints neither the default nor a
  description. `leadminer score --help` shows a bare `file` and buries
  "in place" as a parenthetical in `--out OUT   write elsewhere instead of in
  place`. An operator cannot learn from the CLI that `score` writes, or what file
  it defaults to.
- **Fix:** `DATA_DIR` anchored to the package (`pathlib.Path(__file__).parent /
  "data"`) or a `LEADMINER_DATA_DIR` env var; add `help=` with `%(default)s` to
  every positional and option.

### S3 — Dead validation in `cmd_scrape`

- **Where:** `cli.py:56-59` re-checks `if args.source not in registry`, but
  `cli.py:290` already declares `choices=["osm", "wikidata", "google_places"]`
  and the registry keys at `cli.py:52-54` are identical. The branch is
  unreachable.
- **Breaks:** nothing. `leadminer scrape --source OSM` exits 2 at argparse with
  `invalid choice: 'OSM'` before `cmd_scrape` runs.
- **Fix:** delete `cli.py:56-59`; keep `choices` as the single source of truth.

### S3 — Terminal-control injection through the echoed path

- **Where:** `cli.py:83`, `:93`, `:194`, `:222` interpolate the user-supplied
  path into stdout/stderr with no escaping.
- **Breaks:** a file named `"\x1b[31mred\x1b[0m.csv"` makes `stats` paint the
  terminal; `"two\nlines.csv"` forges an extra output line; `"a\u202eb.csv"`
  (RIGHT-TO-LEFT OVERRIDE) makes the printed path *lie about its own name*,
  which is nasty on a machine where an operator is deciding which file to trust.
- **Fix:** `print(f"no such file: {path!r}", ...)` — `repr` escapes control
  characters and bidi controls.

### S3 — `build_parser` has zero test coverage and CI never invokes it

- **Where:** `tests/test_lead_signal.py:215-264` imports `cmd_stats` and
  `cmd_validate` and calls them with a hand-built `argparse.Namespace(file=p)`.
  `build_parser` appears in exactly one place in the whole repo — its own call
  site at `cli.py:313`. `cmd_score` is never tested. `scrape.yml:69` runs
  `python main.py`, never `leadminer`.
- **Breaks:** every finding in this report is invisible to CI by construction. A
  test that constructs a `Namespace` by hand cannot catch a wrong `dest`, a wrong
  default, a missing `required`, or a destructive default.
- **Fix:** add table-driven tests over `build_parser().parse_args(argv)` for at
  least: bare `[]`, each subcommand, `--out ''`/`.`/`'   '`, `''`/`.`/`/` as
  `file`, and a non-UTF-8 file. Assert exit codes, not just handler returns.

### S3 — The `leadminer` console script is probably not in the wheel

- **Where:** `pyproject.toml:33` declares `leadminer = "cli:main"`, but
  `pyproject.toml:36-37` ships only `include = ["scrapers/"]` — no `packages=`
  and no `cli.py`. The comment above it, *"The package imports a top-level
  sibling module, so both must ship"*, describes the opposite of what the config
  does. `cli.py`, `main.py`, `dedup.py`, `enricher.py`, `pitch_recommender.py`,
  and `httpclient.py` all appear absent from the wheel.
- **Breaks:** if so, the installed console script fails with
  `ModuleNotFoundError: No module named 'cli'` — i.e. `build_parser` is
  unreachable in production, and the only working invocation is
  `python cli.py` from a source checkout. **Not verified**: I did not build.
- **Verify:** `python -m build && unzip -l dist/*.whl | grep -E 'cli|main'`
- **Fix:** `[tool.hatch.build.targets.wheel] packages = ["."]` (or move the
  modules into a `leadminer/` package). Likely already tracked in `030` and
  `037`; flagged here because it gates this function's only entry point.

---

## Input enumeration and classification

Every row was executed. "Crash" = uncaught exception + traceback, process exit 1.

### Correctly handled

| Input | Command | Result |
|---|---|---|
| `[]` | any | `error: the following arguments are required: cmd`, exit 2 |
| `--help`, `-h`, `<sub> --help` | any | help on stdout, exit 0 |
| `stats --help a.csv` | stats | help wins over the positional, exit 0 |
| `nope` / `RUN` / `Stats` / `📊` | any | `invalid choice`, exit 2 |
| `" "` / `"\t "` as `file` | stats, validate | `no such file:  `, exit 2 |
| 300 / 4 095 / 4 096 / 100 000-char filename | stats | `no such file`, exit 2 — `Path.exists()` swallows `ENAMETOOLONG` |
| `"\ud800"` (lone surrogate) | stats | `no such file`, exit 2 |
| `"\x00"`, `"ok\x00bad.csv"` | stats | `no such file`, exit 2 |
| `/dev/null` | stats, validate | `is empty`, exit 1 |
| 0-byte file, header-only file, `"\n\n\n"`, BOM-only file | stats, validate | `is empty`, exit 1 |
| `{"error": {"code": 429, ...}}` (JSON body as `.csv`) | stats, validate | `is empty`, exit 1 |
| Arabic `المطاعم.csv` | stats | 2 rows, exit 0 |
| French NFC `café.csv` **and** NFD `café.csv` | stats | 2 rows, exit 0 both (APFS is normalization-insensitive) |
| Emoji `🍽🚪.csv` | stats | 2 rows, exit 0 |
| `inv␣isible.csv` (U+200B) | stats | 2 rows, exit 0 |
| `-lead.csv` via `stats -- -lead.csv` | stats | 2 rows, exit 0 |
| `stats --` | stats | falls back to the default file, exit 2 |
| `stats -foo.csv` | stats | `unrecognized arguments`, exit 2 (argparse's `--` hint) |
| `stats a.csv b.csv` | stats | `unrecognized arguments: b.csv`, exit 2 |
| `stats --file a.csv`, `--fi a.csv` | stats | `unrecognized arguments`, exit 2 |
| `run --unknown`, `run extra` | run | `unrecognized arguments`, exit 2 |
| `scrape --source OSM` / `None` / `osm2` | scrape | `invalid choice`, exit 2 |
| `scrape --sou osm` (abbreviation) | scrape | accepted — `allow_abbrev` is `True`, only one long option, harmless here |
| `--out sub/dir/out.csv` (dirs absent) | score | parent dirs created, exit 0 |
| `build_parser()` called twice | — | `p1 is p2 → False`, `p1._actions == p2._actions → False`: no state leakage |
| `import cli` with `requests` absent | — | succeeds — `cli.py:20-24` imports stdlib only, unlike `main.py:25` |

**Caveat on the "correct" rows for `"0"`, `"-1"`, `"false"`, `"None"`:** these
are rejected only because no file of that name happens to exist. There is no
validation; a file literally named `0` would be read without complaint.

### Crashes with a traceback

| Input | Command | Exception |
|---|---|---|
| `""` as `file` | stats, validate | `IsADirectoryError: [Errno 21]` (`Path('') == '.'`) |
| `"."` / `".."` / `"/"` as `file` | stats, validate | `IsADirectoryError` |
| `--out .` | score | `ValueError: PosixPath('.') has an empty name` (`main.py:122`) |
| `--out /dev/null` | score | `PermissionError: '/dev/null.tmp'` (`main.py:124`) |
| latin-1 CSV | stats, validate | `UnicodeDecodeError` at `0xe9` |
| UTF-16 CSV | stats, validate | `UnicodeDecodeError` at `0xff` |
| gzip file | stats, validate | `UnicodeDecodeError` at `0x8b` |
| arbitrary binary | stats, validate | `UnicodeDecodeError` at `0x80` |
| any `file` when `requests` is not installed | score | `ModuleNotFoundError: No module named 'requests'` (`httpclient.py:43`) |
| `None` inside `argv` | stats | `TypeError` from `pathlib` |
| `42` / `3.14` / `True` / `PosixPath(...)` inside `argv` | stats, score | `TypeError: 'int' object is not subscriptable` inside `argparse._parse_optional` |
| `["a"]` inside `argv` | stats | `TypeError` from `pathlib` |

The non-`str` rows are reachable only through programmatic `main([...])`
(the type is `list[str] | None`, `cli.py:312`) — i.e. from a wrapper or a test.
They matter because `main` is a documented importable entry point and does no
`isinstance` guard on `argv`.

### Returns a silently wrong value

| Input | Command | What is wrong |
|---|---|---|
| `~/Downloads/contacts.csv` (any non-leadminer CSV) | score | Overwritten with leadminer's schema, columns dropped, rows relabelled. Exit 0 |
| master with extra columns | score | Extra columns silently dropped by `extrasaction="ignore"`. Exit 0. Verified: `salesforce_id_abc`, `owner_notes_hello` gone |
| `--out ''` | score | Writes **in place** instead of elsewhere. Exit 0 |
| `--out '   '` | score | Writes a file named three spaces into `$CWD`. Exit 0 |
| 5 000 rows, 0 contacts | validate | `all checks passed`, exit 0 |
| 3-column CSV, no region/lat/lon | validate | `all checks passed`, exit 0 |
| header + one junk row | validate | `all checks passed`, exit 0 |
| NUL-bytes file | validate | `all checks passed`, exit 0 |
| master collapsed 500k → 1 row | validate | `all checks passed`, exit 0 |
| HTML 503 error page | stats | Prints a confident all-zeros report for 4 "rows", exit 0 (`validate` does catch this one, via `blank name`) |
| `\x1b[31m…`, `two\nlines.csv`, `a\u202eb.csv` | stats | Exit 0, but injects ANSI / a forged line / a bidi-reversed path into the operator's terminal |
| `""` for `file` in `score` | score | Would be in-place — masked here by the `requests` import failing first |

---

## Most dangerous input

> ### `leadminer score <path>` — a bare path, no `--out`

Concretely, the zero-token form **`leadminer score`**, whose parser default
(`cli.py:305`) is `data/all_businesses.csv` and whose `--out` default
(`cli.py:306` → `cli.py:274`) is *"overwrite the input"*.

Why this one, over the tracebacks and the `validate` gaps:

1. **It is the input an operator reaches for first.** `cli.py:14-15` and
   `README.md:19` explicitly group `score` with `stats` and `validate` as the
   commands that *"never touch the network, so they are safe to run anywhere and
   are what you reach for when a run looks wrong."* That framing reads as
   read-only. `score` is the **only one of the three that writes**.
2. **It needs no typo.** One word. `leadminer stats <path>` cannot corrupt
   anything, so instinct says "these are all the same kind of command".
3. **The destructiveness is invisible in the help.** The `file` positional has no
   `help=` text and no `%(default)s`; `leadminer score --help` reduces the
   warning to a parenthetical: `--out OUT   write elsewhere instead of in place`.
4. **It fails open, silently, and successfully.** No header check, no schema
   check, no `--dry-run`, no backup, no "N columns will be dropped" prompt.
   Exit **0**. The output is routine bookkeeping —
   `re-scored 2 records, 2 changed -> …/contacts.csv` — which reads as a normal
   maintenance task, not as the destruction of an unrelated file.
5. **The damage is corruption, not just deletion.** The contacts example ends up
   as two rows with `country=LB`, `lead_score=20`, `industry_priority=low`, and
   `recommended_service="Full digital launch (brand + website + social setup)"`.
   Persons are now low-priority Lebanese businesses awaiting a website pitch. If
   that file reaches a sales rep it is *actively* wrong, which is worse than
   missing.
6. **The target is the only copy.** `BRIEF.md:50` and `main.py:8` call
   `all_businesses.csv` the *"cumulative master (all time)"*. `write_csv`
   finishes with `os.replace(tmp, path)` (`main.py:130`) — atomic, but atomic is
   not reversible. There is no snapshot, and `scrape.yml:88-89` copies `data/`
   up to Drive *after* the pipeline, so a corrupted local master is a corrupted
   Drive master.
7. **Nothing tests it.** `tests/test_lead_signal.py` covers `cmd_stats` and
   `cmd_validate` via hand-built Namespaces; `cmd_score` and `build_parser` are
   untested, and CI never runs `leadminer` at all.

Run `leadminer score` on a *copy* until it demands `--out` and validates the
header.

---

## Not a bug, but worth knowing

- **`build_parser` is genuinely well built.** No argument means no input to
  fuzz; it is import-safe (stdlib only, unlike `main.py:25`), allocates a fresh
  parser per call with no shared state, pins `prog="leadminer"` so
  `python cli.py --help` and the installed script print identical usage,
  requires a subcommand (`cli.py:283`), sets `func=` on all six subparsers so
  `args.func` at `cli.py:315` can never be an `AttributeError`, declares
  `choices` on the one enum, and every optional positional has a default so no
  invocation raises argparse's "expected one argument". `RawDescriptionHelpFormatter`
  (`cli.py:282`) keeps the `__doc__` usage examples readable. None of the S1/S2
  findings above are *in* the parser — they are in what it declines to check.
- **`argparse`'s own error handling is used correctly.** Bad choice, unknown
  flag, extra positional, and missing subcommand all produce usage text on
  stderr and exit 2. The crash cases are all *past* argparse.
- **Unicode filenames work end to end.** Arabic, French in both NFC and NFD,
  and emoji all read correctly. APFS normalization-insensitivity means `café.csv`
  opens whether the shell hands over NFC or NFD — a real hazard that this code
  does not have.
- **`long_300`-style filenames are fine.** `Path.exists()` swallows `ENAMETOOLONG`,
  so `ENAMETOOLONG` becomes a clean exit 2 rather than a traceback.
- **`'0'`, `'-1'`, `'false'`, `'None'` as filenames are rejected by accident, not
  by design.** No validation exists; they only fail because no such file exists.
- **`allow_abbrev=True` is harmless here.** `score` has exactly one long option
  and `scrape` exactly one, so `--o` and `--sou` resolve unambiguously. Worth
  setting `allow_abbrev=False` anyway for habit's sake.
- **Three failure exit codes coexist** — argparse's 2, handlers' 0/1/2, and
  uncaught exceptions' 1 — with no way for a caller to distinguish "bad
  argument" from "internal error". A `main()` that catches `OSError` and
  `UnicodeDecodeError` and maps them to 2 would collapse the mess.
- **The HTML-error-page case is caught, but by luck.** `csv.DictReader` makes
  line 1 the header, so `r.get("name")` is `None` on every row and the
  `blank name` check fires. Change the error page's first line to contain a
  comma and the gate passes it.

## Recommended order of work

1. **`score` stops writing in place by default** — require `--out` (or `--force`),
   validate the header against `main.FIELDS`, report dropped columns as an error
   instead of `extrasaction="ignore"`. Fixes both S1s' worst half.
2. **Make `validate` a real gate** — assert the schema, add a
   "rows with ≥ 1 contact channel" check and a `--min-rows` floor, and count
   blank `lat`/`lon` as a problem instead of `continue`-ing past it. Fixes S1-2
   and is the cheapest high-value check in the codebase.
3. **One `_resolve_csv(arg)` helper** returning `(path | None, error)`: reject
   blank/whitespace, require `is_file()`, read with `errors="replace"`, sniff
   gzip/zip/UTF-16. Kills every traceback row in the enumeration at once and
   unifies the exit codes.
4. **`--out` truthiness → `is not None`**, plus a `type=` rejecting
   directories and blank names; reject `--out` that resolves to the input
   without `--force`.
5. **Anchor `DATA_DIR`** to the package or an env var, and switch to
   `ArgumentDefaultsHelpFormatter` with `help=` text on `file` and `--out` so the
   defaults and the in-place write are discoverable in `--help`.
6. **Test `build_parser().parse_args(argv)`** as a table, not hand-built
   Namespaces; add `cmd_score` coverage; wire `leadminer validate` into
   `scrape.yml` so the gate actually gates.
7. Housekeeping: delete `cli.py:56-59`; `repr` the echoed paths
   (`cli.py:83`, `:93`, `:194`, `:222`); confirm the wheel ships `cli.py`.