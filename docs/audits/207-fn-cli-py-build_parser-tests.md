# 200 — `build_parser` (cli.py:280) — the test cases it still needs

## Verdict

`build_parser` is the entire public surface of the tool and **not one of the 28
existing tests touches it** — `TestCli` (`tests/test_lead_signal.py:191-264`)
hand-builds `argparse.Namespace(file=p)`, so the tests exercise the handlers
against a namespace no parser invocation could ever produce (no `cmd`, no
`func`). The parser↔handler contract is completely unpinned, and probing it
surfaced **three destructive paths in `leadminer score` that all exit `0`** —
silent column deletion, silent schema coercion, and a `--out -` that writes a
file named `-` (already visible in this repo's `git status`) — introduced in a
single commit (`43ddb2c`) and never revisited. Ten test cases below; **three are
red today** (T1, T2, T10), and two more are red characterisation tests written
to fail before the fix (T5's strong form, T6).

## Baseline (verified, not assumed)

```
$ python3 -B -m unittest discover -s tests
Ran 28 tests in 0.019s
OK
$ grep -n "build_parser\|cli.main\|cmd_score\|cmd_scrape\|cmd_doctor\|cmd_run\|DATA_DIR\|prog" tests/test_lead_signal.py
  (none)
```

Subcommands registered, in order (`cli.py:283-307`):
`['run', 'scrape', 'stats', 'validate', 'doctor', 'score']`.

## Findings

### S1 — `leadminer score` silently deletes every column outside `FIELDS` from the cumulative master

- **Where:** `cli.py:274-275` (`out = args.out if args.out else path`) →
  `main.py:125` (`csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")`)
- **Breaks:** `score` with no `--out` rewrites its input **in place**. Any column
  the master carries that is not in `FIELDS` is dropped without a warning, and
  the command exits `0` while printing `re-scored 1 records, 1 changed` — a
  message that says nothing about the columns it just deleted. `all_businesses.csv`
  is the cumulative, rclone-mirrored asset that `.github/workflows/scrape.yml`
  refuses to overwrite when missing; `score` is the one command that can quietly
  strip it.
- **Trigger (executed, verbatim output):**
  ```
  master has 24 columns = FIELDS(23) + "enrichment_version"
  parse_args(['score', p]) -> file='…/all_businesses.csv' out=None
  cmd_score(ns) -> rc: 0 | columns out: 23
  'enrichment_version' survived: False
  ```
- **Fix:** have `write_csv` report dropped keys, and make `cmd_score` refuse
  (or require `--force`) when `set(records[0]) - set(FIELDS)` is non-empty.
  See test **T1**.

### S1 — `leadminer score` accepts any CSV with any header and rewrites it into master shape

- **Where:** `cli.py:254-277`. The only precondition is `path.exists()`
  (line 255). There is no schema check on the header.
- **Breaks:** the single most plausible operator error — pointing `score` at the
  wrong CSV, or at a hand-made file — is not an error. It is a silent
  destructive rewrite. The row's real data is discarded and replaced with a
  23-column record that has a blank `name`, `country=LB` (the
  `_DEFAULT_COUNTRY` fallback at `main.py:47`), `lead_score=0`, and an invented
  `recommended_service`. The command then reports `1 changed`, which is false —
  nothing was re-scored.
- **Trigger (executed, verbatim):**
  ```
  wrong.csv before: "a,b\n1,2\n"
  cmd_score(parse_args(['score', wrong.csv])) -> rc: 0
  wrong.csv after : "\ufeffname,category,region,country,…,0,low,Full digital launch (brand + website + social setup),,\n"
  ```
  Both `1` and `2` are gone. `rc=0`.
- **Fix:** require the header to be a superset of the fields `cmd_score` reads
  (`name`, `category`, `country`, `rating`, `source`, …) before writing.
  See test **T2**.

### S2 — the dispatch invariant is unpinned; the whole job of this function is one line wide

- **Where:** `cli.py:285-307` (`set_defaults(func=…)` × 6) consumed at
  `cli.py:315` (`args.func(args)`).
- **Breaks:** `main` has no fallback. Omit one `set_defaults`, or rename a
  `dest=`, and the first operator to run that subcommand gets a raw
  `AttributeError: 'Namespace' object has no attribute 'func'`. Nothing in CI
  would catch it: `.github/workflows/scrape.yml` still runs `python main.py`,
  **not** `leadminer run`, so the parser is never exercised by automation at
  all. This is precisely the failure shape of the `record.get("country", "LB")`
  dead-default bug fixed in `8a42a67` — a line that looks correct and is
  silently dead.
- **Trigger:** remove `.set_defaults(func=cmd_validate)` at `cli.py:299`;
  `main(["validate"])` then raises `AttributeError` instead of exiting 0.
- **Fix:** tests **T3** (property) + **T4** (property).

### S2 — `--source` choices duplicate a registry that lives inside a function, so the drift is untestable

- **Where:** choices at `cli.py:289-290` (`["osm","wikidata","google_places"]`);
  the real mapping at `cli.py:51-55` inside `cmd_scrape`.
- **Breaks:** two hand-maintained lists of the same three strings. The obvious
  edit — add a fourth source to the registry — passes review and is then
  rejected at runtime with `invalid choice: 'yelp' (choose from 'osm',
  'wikidata', 'google_places')`, an error that points at the wrong place.
  The guard at `cli.py:56-59` is **unreachable dead code**: `choices` already
  rejects any value outside the registry (verified: `parse_args(["scrape",
  "--source","yelp"])` → `SystemExit(2)`), so `args.source not in registry` can
  never be `True`. It reads as the safety net that the duplication does not
  actually need.
- **Trigger:** add `"yelp": YelpScraper` to the `registry` dict at
  `cli.py:51-55`; `leadminer scrape --source yelp` exits 2.
- **Fix:** hoist the registry to module scope and derive the choices from it
  (`choices=sorted(registry)`). Until then only the one-way property **T5** is
  expressible. See *Cannot be tested* below.

### S2 — the default CSV path is CWD-relative and is frozen at parse time

- **Where:** `cli.py:26` (`DATA_DIR = pathlib.Path("data")`), used as the
  `default=` at `cli.py:294`, `cli.py:298`, `cli.py:305`.
- **Breaks:** `leadminer stats` is only correct when the operator happens to be
  standing in the repo root. From anywhere else it resolves
  `data/all_businesses.csv` against that directory and exits `2` with
  `no such file: data/all_businesses.csv` — which reads like "your data is
  gone", not "you are in the wrong directory". Nothing anchors the path to the
  installation, and `cmd_score` with the same default **rewrites** whatever it
  finds at that CWD-relative location.
- **Trigger:** `os.chdir(tmpdir); build_parser().parse_args(["stats"]).file` →
  still the string `'data/all_businesses.csv'`, which `cmd_stats` then resolves
  against `tmpdir` (`cli.py:82`, `cli.py:138`).
- **Fix:** anchor `DATA_DIR` to the package directory, not `Path("data")`.
  Characterise first (**T6**), then flip the assertion.

### S2 — `--out -` silently writes a file literally named `-`, and it has already happened in this repo

- **Where:** `cli.py:306` (`s.add_argument("--out", …)`) accepted verbatim by
  `cli.py:274` (`out = pathlib.Path(args.out) if args.out else path`) and
  `write_csv` at `main.py:121-130`.
- **Breaks:** `-` is the universal convention for "write to stdout". A shell
  script, a habit carried from another tool, or a hand-typed `--out -` produces
  a **file named `-`** in the current working directory, exit code `0`, and the
  line `Written: - (1 records)` — which reads like a stream, not a filename.
  The same happens for `--out ""`, `--out " "` and any other non-path token.
  Nothing validates the value, and `data/` is gitignored (`.gitignore:8`) while
  the repository root is not, so the junk lands in git status.
- **Trigger (executed, in a tempdir):**
  ```
  --out '-'  rc=0  files created: ['-']    prints "Written: - (1 records)"
  --out ' '  rc=0  files created: [' ']
  --out '0'  rc=0  files created: ['0']
  ```
  **This is not hypothetical.** At the time of writing, `leadminer`-shaped
  three such files sit in the repository root, untracked and not gitignored:
  `./-`, `./ ` (one space) and `./0`, each a 374-byte BOM'd master-schema CSV
  with identical content. They are **not** mine — my fixtures are `Cafe` /
  `SEO audit`, these are `Cafe Beirut` / `RTYLR commerce OS (POS, online
  ordering, CRM)` — so per BRIEF rule 1 I have left them in place for the
  coordinator. They are the exact artifact this finding predicts.
- **Fix:** reject `-` explicitly (or honour it as stdout), and require `args.out`
  to name an existing directory or end in `.csv`. See test **T10**.

### S3 — smaller parser gaps

- **No `--version`.** `leadminer --version` → `SystemExit(2)`
  (verified). `pyproject.toml:7` has `version = "0.2.0"`; an operator on a
  field report cannot tell which build they are running.
- **`prog` is a magic string.** `cli.py:281` hardcodes `"leadminer"` while the
  real name lives in `pyproject.toml:33`. They agree today (verified) and
  nothing keeps them agreeing. Test **T7**.
- **Subparsers do not inherit `RawDescriptionHelpFormatter`.** Verified:
  root is `RawDescriptionHelpFormatter`, `scrape` is plain `HelpFormatter`.
  Harmless only because no subparser has a `description=`; adding one
  multi-line description will be silently rewrapped.
- **`allow_abbrev` is on.** Verified `["scrape","--s","osm"]` and
  `["score","--ou","x"]` both parse. Low impact, but it means `--sou` is a valid
  invocation and typos in long options do not always fail.
- **`BRIEF.md:57` says "The 22 columns" and lists 23.** `main.FIELDS`
  (`main.py:33-40`) has 23. Any test that hardcodes a column count should read
  `main.FIELDS`, not the brief.

## The test cases this function still needs

Properties first — each of the four marked **PROPERTY** replaces a table of
examples that would itself become a second copy of the parser's knowledge.
Shared helpers assume the import stubbing already in
`tests/test_lead_signal.py:23-63`.

```python
MINIMAL_ARGV = {          # discovered, not hand-maintained — see note below
    "run": ["run"],
    "scrape": ["scrape", "--source", "osm"],
    "stats": ["stats"], "validate": ["validate"],
    "doctor": ["doctor"], "score": ["score"],
}
```

### T1 — `test_score_in_place_write_preserves_unknown_columns`  (S1)

- **Method:** `tests/test_cli_parser.py::TestScoreIsNotDestructive::test_score_in_place_write_preserves_unknown_columns`
- **Input:** a CSV whose header is `main.FIELDS + ["enrichment_version"]`
  (24 columns), one row, written via `write_csv` then re-headered:
  ```python
  p = Path(d) / "all_businesses.csv"
  row = {k: "" for k in FIELDS + ["enrichment_version"]}
  row.update(name="Cafe", category="cafe", country="LB", source="osm",
             phone="70123456", website="https://x.com", website_live="True",
             rating="4.2", review_count="12", lead_score="30",
             industry_priority="high", recommended_service="SEO audit")
  p.write_bytes(b"\xef\xbb\xbf" +
                ("\n".join([",".join(list(row)), ",".join(row[c] for c in row)]) + "\n").encode())
  ```
  then `cmd_score(build_parser().parse_args(["score", str(p)]))`
- **Asserted output:** `rc == 0`, and `"enrichment_version" in
  p.read_text(encoding="utf-8-sig").splitlines()[0]`. **Fails today** —
  observed 23 columns out, `enrichment_version` gone, `rc=0`.
- **Pins:** new S1. Same class as the cumulative-master damage fixed in
  `8a42a67` (non-atomic write, BOM) — that commit made the master durable
  against crashes; this is the master being mutilated on the happy path.

### T2 — `test_score_rejects_a_csv_whose_header_is_not_the_schema`  (S1)

- **Input:** `wrong.csv` containing exactly `"a,b\n1,2\n"`
- **Asserted output:** `cmd_score(...)` returns non-zero (`2` fits the existing
  convention at `cli.py:256-257`), `stdout`/stderr mentions the missing
  required columns, and — the load-bearing part —
  `p.read_bytes() == b"a,b\n1,2\n"` unchanged.
  **Fails today** — observed `rc: 0` and the file replaced by a 23-column
  master-shaped row.
- **Pins:** new S1. Also pins the non-empty `changed` counter
  (`cli.py:270-271`): a row that was never scored must never be reported as
  "changed".

### T3 — `test_every_subcommand_sets_a_callable_func`  **PROPERTY**  (S2)

- **Input:** `build_parser().parse_args(MINIMAL_ARGV[name])` for every
  registered subcommand, with `MINIMAL_ARGV` **derived, not written**:
  ```python
  for name, sp in subs.choices.items():
      argv = [name]
      for a in sp._actions:
          if a.required and a.dest != "help":
              argv += [a.choices[0]] if a.choices else ["SENTINEL"]
  ```
- **Asserted output:** for every `name`:
  1. `getattr(ns, "func")` is callable;
  2. `ns.func.__name__ == f"cmd_{name}"`;
  3. `ns.func is getattr(cli, f"cmd_{name}")` — binds to the module-level
     function, not a lambda or a partial.
- **Pins:** the dispatch invariant at `cli.py:315`. **Fails today?** No — it
  passes. It exists so the *next* `sub.add_parser(...)` without a
  `set_defaults` fails in CI rather than in front of an operator. This is the
  test that would have caught the S1 class in `043-scoring-upgrade.md` if it
  had been written as a parser change.

### T4 — `test_every_attribute_a_handler_reads_is_defined_by_its_subparser`  **PROPERTY**  (S2)

The real generalisation of T3 — it catches `dest` renames, not just missing
`set_defaults`.

- **Input:** `ast.parse` of `cli.py`; collect, per `def cmd_*`, every
  `args.<name>` read via `ast.Attribute` on a parameter named `args`
  (`cli.py:56`, `cli.py:61` → `source`; `cli.py:82`, `cli.py:139`,
  `cli.py:254` → `file`; `cli.py:274` → `out`). Compare against the dests each
  subparser actually declares, plus `{"cmd", "func"}`.
- **Asserted output:** for every command, `reads ⊆ declared`. Concretely today:
  `{"source"} ⊆ {"cmd","func","source"}`, `{"file"} ⊆ {"cmd","func","file"}`,
  `{"out"} ⊆ {"cmd","func","file","out"}`, `{} ⊆ {"cmd","func"}` for
  `cmd_run`/`cmd_doctor`.
- **Pins:** the dead-default failure mode of `resolve_country`
  (`main.py:59`, fixed in `8a42a67`): a value the handler reads that the
  producer never sets. Also pins that `cmd_run` (`cli.py:33-37`) and
  `cmd_doctor` (`cli.py:204-244`) read **nothing** from `args` — which is the
  property that lets them be dispatched with a bare namespace.

### T5 — `test_every_source_choice_names_an_importable_scraper_module`  **PROPERTY**  (S2)

- **Input:** `["scrape","--source",c]` for each `c` in the `--source` action's
  `choices`.
- **Asserted output:** `importlib.import_module(f"scrapers.{c}")` succeeds.
  Verified today: `osm`→`scrapers.osm` (`OSMScraper`),
  `wikidata`→`scrapers.wikidata` (`WikidataScraper`),
  `google_places`→`scrapers.google_places` (`GooglePlacesScraper`).
- **Pins:** the `cli.py:289-290` / `cli.py:51-55` duplication. **Note the
  tempting-but-false version of this property:** deriving the class name by
  `camel-case` does **not** work — `scrapers.osm` defines `OSMScraper`, not
  `OsmScraper` (verified). There is no mechanical choice→class rule, so the
  strong form of this test is not expressible against the current code. See
  *Cannot be tested*.

### T6 — `test_default_file_does_not_depend_on_cwd`  **PROPERTY**  (S2)

- **Input:**
  ```python
  a = build_parser().parse_args(["stats"]).file          # from repo root
  with tempfile.TemporaryDirectory() as d:
      os.chdir(d)
      b = build_parser().parse_args(["stats"]).file
  ```
- **Asserted output:** `pathlib.Path(b).is_absolute()` and
  `b == a` (identical absolute path from any CWD).
  **Characterisation value:** this **fails today** — both return the string
  `'data/all_businesses.csv'` and it is not absolute. Write it as a failing test
  first, fix `cli.py:26` to anchor `DATA_DIR` to the package dir, then flip the
  assertion.
- **Pins:** the S2 CWD trap. Same shape as the dead `country` default: a value
  that is present and looks right but resolves to nothing.

### T7 — `test_prog_matches_the_console_script_name`  **PROPERTY**  (S3)

- **Input:** `build_parser().prog`; and
  `tomllib.loads(Path("pyproject.toml").read_bytes())["project"]["scripts"]`
- **Asserted output:** `build_parser().prog in scripts`, and the mapped target
  is `"cli:main"`. Verified today: `prog == "leadminer"`, matching
  `pyproject.toml:33`.
- **Pins:** the hardcoded `prog="leadminer"` at `cli.py:281` against the real
  entry point. Without this, renaming the console script silently makes every
  `--help` and every error message name a command that does not exist.

### T8 — `test_build_parser_does_not_import_anything_heavy`  **PROPERTY**  (S3, cheap)

- **Input:** snapshot `set(sys.modules)`, call `build_parser()`, diff.
- **Asserted output:** the gained set contains none of
  `{"requests","urllib3","main","enricher","dedup","pitch_recommencer","httpclient"}`
  nor any `scrapers.*`.
- **Pins:** the stdlib-only module scope at `cli.py:18-24`. This is what makes
  `--help` and `doctor` usable in a broken environment — the property
  `cmd_doctor` explicitly depends on when it handles a missing `requests`
  itself (`cli.py:227-231`: *"doctor exists to diagnose a broken environment,
  so it must never be the thing that tracebacks"*). Verified today: importing
  `cli` and calling `build_parser()` pulls in `NONE` of the above.

### T9 — `test_bad_invocations_fail_with_exit_2_and_never_dispatch`  (S2, safety)

Table-driven, but every row is a *failure* mode, so it is not duplicating the
parser's knowledge:

| literal input | asserted |
|---|---|
| `[]` | `SystemExit(2)`; `"required"` in stderr |
| `["sc"]`, `["st"]`, `["s"]`, `["d"]` | `SystemExit(2)` |
| `["scrape"]` | `SystemExit(2)`; stderr names `--source` |
| `["scrape","--source","yelp"]` | `SystemExit(2)`; stderr contains `invalid choice` |
| `["run","--source","osm"]` | `SystemExit(2)` — `run` takes no options |
| `["score","--output","x"]` | `SystemExit(2)` |
| `["--version"]` | `SystemExit(2)` (documents the gap from S3; flip when the flag lands) |

- **Pins:** that a typo can never silently reach a network-touching, quota-billing
  command. `run`, `scrape` and `doctor` hit Overpass, Wikidata and Google Places;
  `stats`, `validate` and `score` never do (`cli.py:14-15`). Verified: argparse
  does **not** prefix-match subcommand names, so `["sc"]` is rejected rather than
  resolving to `scrape` — worth pinning explicitly, because a future
  "helpfulness" patch that enables it would turn `leadminer sco` into a billed
  scrape.

### Supporting tests for `main()` (cli.py:312-318)

Same lens — `main` is `build_parser`'s only consumer:

- `test_cmd_returning_none_becomes_exit_0` — patch `cmd_doctor` to return
  `None`; `main(["doctor"]) == 0`. Pins the `or 0` at `cli.py:315`.
- `test_cmd_return_code_is_preserved` — return `2`; `main(["doctor"]) == 2`.
  Verified for `None`, `0`, `2`, `False`, `"3"` → `0,0,2,0,3`.
- `test_keyboard_interrupt_exits_130` — verified: `130`.
- `test_other_exceptions_propagate` — a raised `ValueError` escapes `main`
  (verified). Pins the deliberate no-bare-`except` policy; currently only
  `KeyboardInterrupt` is caught (`cli.py:316`).
- `test_main_with_no_subcommand_exits_2` — `main([])` → `SystemExit(2)`,
  raised at `cli.py:313` **outside** the `try` at `cli.py:314`. Verified.

### T10 — `test_score_out_rejects_stream_and_non_path_tokens`  (S2)

- **Input:** `cmd_score(build_parser().parse_args(["score", str(src), "--out", v]))`
  for `v in ("-", "", " ", "0")`, with `os.chdir(tmpdir)` so any file created
  is observable, and `src` a valid 1-row master CSV.
- **Asserted output:** for every `v`, `rc != 0` **and**
  `sorted(os.listdir(tmpdir)) == {"in.csv"}` — no new file, nothing written.
  **Fails today** — observed `rc=0` with `['-']`, `[' ']`, `['0']` created and
  `Written: - (1 records)` printed.
- **Pins:** new S2, and it is the only test in this report whose failure mode is
  visible in the repository's own `git status`. It also pins that
  `write_csv`'s `print(f"  Written: {path} …")` (`main.py:134`) is never reached
  for a rejected `--out`.

## Not a bug, but worth knowing

- **Order is part of the contract.** `['run','scrape','stats','validate',
  'doctor','score']` is the literal insertion order at `cli.py:285-307`, and it
  is the order shown in `--help` and in `README.md:125`. If a future patch
  reorders these, `README.md` and the rendered help go stale simultaneously.
  Either assert the order or stop depending on it.
- **`README.md` examples all parse today.** Verified: all eight `leadminer …`
  lines in `README.md` parse to the expected `cmd`. Worth keeping as a cheap
  guard — but a naive regex over `cli.__doc__` gives a **false positive**:
  line 4 reads `leadminer command line interface.`, so a
  `^\s+leadminer (\w+)` match yields `'command'`, which is not a subcommand.
  Anchor the regex, e.g. `^\s{4}leadminer (\w+)\s{2,}#`.
- **The CI job never exercises the CLI.** `.github/workflows/scrape.yml` runs
  `python main.py`, and its "Validate output before publishing" step only does
  `[ ! -s … ]` plus a row-count floor — it never calls `cmd_validate`, whose
  BOM, coordinate, rating-range and duplicate checks exist specifically for that
  gate. So every test in T1-T10 is the *only* automated coverage the parser
  will ever have. Changing the job to `leadminer run` and `leadminer validate`
  is a one-line change that makes this lens self-maintaining.

## What genuinely cannot be tested here, and why

1. **The strong form of T5 — `set(choices) == set(registry)`.** The registry is
   a function-local dict at `cli.py:51-55`, constructed inside `cmd_scrape`. It
   is not reachable from a test without calling `cmd_scrape`, which at
   `cli.py:61` instantiates a scraper and calls `.scrape()` — a network call,
   and forbidden. Reconstructing it in the test would create a third copy.
   **Unblocked by:** hoisting `registry` to module scope and passing
   `choices=sorted(registry)` to `add_argument` — three lines, after which T5
   becomes an equality assertion and the dead guard at `cli.py:56-59` can be
   deleted outright.
2. **The choice→class-name mapping.** Verified: no mechanical rule exists
   (`osm` → `OSMScraper`, not `OsmScraper`). Until the registry is hoisted, the
   only testable half is the one-way `scrapers.<choice>` import (T5), which
   cannot catch a registry entry whose class is misnamed.
3. **`cmd_run` and `cmd_doctor` behaviour.** `cmd_run` delegates to
   `main.main()` and `cmd_doctor` issues three HTTP GETs
   (`cli.py:233-243`). Neither is reachable from `build_parser` without network
   access. The parser's contribution — that they *dispatch*, and take no
   arguments (T3, T4) — is fully testable; their bodies are not, and belong to
   a `doctor`-specific suite with `requests` mocked.
4. **Whether `doctor`'s exit code is meaningful.** `cmd_doctor` returns `0` even
   when every connectivity check printed `FAIL` (`cli.py:242-244` falls through
   to `return 0 if ok else 1`, and `ok` only tracks `GOOGLE_PLACES_API_KEY`).
   That is a `cmd_doctor` bug, not a parser bug, and it cannot be caught by any
   test of `build_parser` — it needs an injected transport. Noted so it is not
   mistaken for a gap in this lens.
5. **CI coverage.** No amount of unit testing substitutes for
   `.github/workflows/scrape.yml` calling `leadminer run`. Out of scope for a
   unit test; see *Not a bug* above.

## Recommended order of work

1. **T1 and T2** — the two S1s. Both are silent, both `rc=0`, both destroy the
   cumulative master, and T2 is reachable by a plain operator typo. Fix
   `cmd_score`'s schema precondition and `write_csv`'s dropped-key reporting
   first; these tests are red until then.
2. **T10** — the `--out -` footgun. One line of validation, and it is the only
   finding here whose damage is already sitting in the repository root. Clean up
   the stray `./-`, `./ `, `./0` files at the same time (BRIEF rule 1 stopped me
   from deleting them).
3. **T3 and T4** — the dispatch properties. Twenty lines of AST plus a loop, and
   they close the whole parser↔handler contract. Without them every future
   `add_parser` is an untested edit.
4. **Hoist the registry (unblocks the rest of S2),** then upgrade T5 to an
   equality assertion and delete `cli.py:56-59`.
5. **T6** as a red characterisation test, then anchor `DATA_DIR` to the package
   directory.
6. **T9 + the `main()` exit-code tests** — cheap, and they pin the property that
   matters most operationally: a typo must never reach a billed command.
7. **T7, T8** — hygiene; fold into the same file.
8. **Separately:** change `scrape.yml` to `leadminer run` +
   `leadminer validate`, which converts this lens from "the only coverage" to
   "the first line of coverage".