# 200 — `cli.main()` boundary and hostile-input audit (`cli.py:312`)

**Target:** `def main(argv: list[str] | None = None) -> int` — `cli.py:312-318`, the
seven-line dispatcher every invocation on the machine goes through.

```python
312: def main(argv: list[str] | None = None) -> int:
313:     args = build_parser().parse_args(argv)
314:     try:
315:         return int(args.func(args) or 0)
316:     except KeyboardInterrupt:
317:         print("\ninterrupted", file=sys.stderr)
318:         return 130
```

**Environment note:** all results below were reproduced on the interpreter actually
present on this machine, **CPython 3.14.8**, not the project's declared 3.12 — no 3.12
interpreter is installed here. The `argparse` internals differ slightly between the two
(3.12 coerces `args = list(args)` inside `parse_known_args`; 3.14 calls
`iter(arg_strings)` and subscripts each element as `arg_string[0]`), but the observed
behaviour classes are identical and the `subprocess`-visible exit codes are
version-independent. Re-run on 3.12 before acting if the distinction matters.
`requests` is **not installed** in this environment; `cmd_score`/`cmd_run`/`cmd_scrape`
were exercised with a two-line in-process `sys.modules["requests"]` stub (no `pip
install`, no file written to the repo). Probe scripts ran under `python -B` in a
`tempfile.mkdtemp()` scratch directory.

---

## Verdict

The dispatcher guards **exactly one exception type**. `cli.py:316` catches
`KeyboardInterrupt` and nothing else, so every `OSError`, `UnicodeDecodeError`,
`csv.Error`, `ImportError` and `TypeError` raised inside any subcommand reaches the user
as a raw traceback — including from `stats`, `validate`, `doctor` and `score`, the four
commands whose only selling point, per the module docstring at `cli.py:14-15`, is that
they "never touch the network" and "are what you reach for when a run looks wrong".

Two consequences are worse than a traceback, because they are **silent**:

1. `cli.py:315`'s `or 0` converts `pipeline.main()`'s documented `-> None` return
   (`main.py:148`) into **exit status 0**. `leadminer run` *cannot* report failure. When
   all three scrapers die, `main.py:167-168` swallows the exceptions, prints to stderr,
   re-exports the stale master, and the process exits 0. Green CI, no new leads.
2. `leadminer validate` — the one command that exists *only* to fail — returns
   **exit 0, "all checks passed"** on files whose data columns are absent or empty,
   because all five of its checks are written as *skip-if-absent* rather than
   *fail-if-absent*.

**The single most dangerous input: `leadminer validate` run with no argument against a
master CSV that has an intact 22-column header but empty data columns.** Detailed at the
end of this report.

---

## Systematic input enumeration

Three surfaces: (A) `argv` itself at `cli.py:313`; (B) the return coercion at
`cli.py:315`; (C) values that flow through a dispatched subcommand.

### A. `argv` — the `list[str] | None` contract at `cli.py:313`

| # | Input | Observed result | Class |
|---|---|---|---|
| A1 | `None` (the default; `main()` from `cli.py:322`) | Reads `sys.argv[1:]`; with no args → `SystemExit(2)` | correctly handled |
| A2 | `[]` (empty list) | `SystemExit(2)`, *"the following arguments are required: cmd"* — **raises, does not return `2`** | contract violation (see S3-1) |
| A3 | `["--help"]` | help to stdout, `SystemExit(0)` | correctly handled |
| A4 | `[""]` / `[" "]` / `["\t\n"]` (empty / whitespace cmd) | `SystemExit(2)`, *"invalid choice"* | correctly handled |
| A5 | `["-"]`, `["--"]` | `SystemExit(2)` | correctly handled |
| A6 | `["st"]`, `["do"]` (subcommand abbreviation) | `SystemExit(2)` — subparsers do **not** prefix-match | correctly handled |
| A7 | `["stats", "--nope"]` (unknown flag) | `SystemExit(2)`, *"unrecognized arguments"* | correctly handled |
| A8 | `["score", "a", "b"]` (too many positionals) | `SystemExit(2)` | correctly handled |
| A9 | `["score", "x", "--out"]` (flag missing its value) | `SystemExit(2)`, *"expected one argument"* | correctly handled |
| A10 | `123`, `3.5` (wrong type: not a container) | **`TypeError: 'int' object is not iterable`** — traceback | **crashes** |
| A11 | `"stats"` (a `str` instead of a `list`) | `SystemExit(2)`, *"invalid choice: 's'"* — silently shredded to characters | **silently wrong** (S3-2) |
| A12 | `pathlib.Path("x.csv")` as an element | **`TypeError: 'PosixPath' object is not subscriptable`** — traceback | **crashes** (S3-2) |
| A13 | `["stats", 5]` (int element) | **`TypeError: 'int' object is not subscriptable`** — traceback | **crashes** (S3-2) |
| A14 | `[["stats"]]` (list element) | **`TypeError: cannot use 'list' as a dict key (unhashable type: 'list')`** — traceback | **crashes** (S3-2) |
| A15 | `["stats", None]` (None element) | **`TypeError: ... not 'NoneType'`** — traceback | **crashes** (S3-2) |
| A16 | `(c for c in "score")` (generator) | `list()`-able, so accepted; then shredded to chars → `SystemExit(2)` | correctly handled (quirk) |
| A17 | one 2,000,000-char arg (ASCII / Arabic / emoji) | `RETURNED 2` in 9–14 ms — "no such file" | correctly handled |
| A18 | 5,000-char path | `RETURNED 2` — `Path.exists()` swallows `OSError`/`ValueError` | correctly handled |
| A19 | `"a\0b.csv"` (embedded NUL) | `RETURNED 2` — `Path.exists()` catches `ValueError` | correctly handled |
| A20 | `"\udcff"` (lone surrogate — how CPython encodes undecodable argv bytes) | `RETURNED 2` through a **real** stdout, both under the default locale and under `LC_ALL=C`; stderr's `surrogateescape` handler absorbed it | correctly handled |
| A21 | `["😀"]`, `["م.stats"]`, `["café"]` | `SystemExit(2)`, invalid choice, unicode echoed intact | correctly handled |
| A22 | 6,000 unknown long options `["stats","x.csv","--unknownoptN=1"…]` | `SystemExit(2)` in **22 ms** — no argparse quadratic blowup | correctly handled |
| A23 | `["score","x","--out","a","--out","b"]` (repeated flag) | last wins silently, no warning | **silently wrong** (S3) |

### B. The return coercion at `cli.py:315` — `int(args.func(args) or 0)`

| Return value of `args.func(args)` | `x or 0` | `int(...)` | Class |
|---|---|---|---|
| `None` | `0` | `0` | **silently wrong** — a subcommand that falls off the end reports success |
| `0` / `1` / `2` / `130` | unchanged | unchanged | correctly handled |
| `False` | `0` | `0` | correctly handled |
| `""` (empty string) | `0` | `0` | **silently wrong** (same shape as A4) |
| `[]` / `{}` | `0` | `0` | **silently wrong** |
| `True` | `True` | `1` | correctly handled |
| `"3"`, `" 3 "`, `b"3"`, `"٣"` | unchanged | `3` | correctly handled (lenient) |
| `2.7` (float) | `2.7` | `2` | **silently wrong** — truncated toward zero |
| `-1` | `-1` | `-1` | **silently wrong** — shell sees `$? = 255` (verified via `subprocess`) |
| `-129` | `-129` | `-129` | **silently wrong** — shell sees `$? = 127` |
| `256` / `300` | unchanged | `256` / `300` | **silently wrong** — shell sees `$? = 0` / `44` |
| `[1]` | `[1]` | **`TypeError`** | **crashes** |
| `{"a":1}` | unchanged | **`TypeError`** | **crashes** |
| `"3.5"`, `"exit 0"`, `"0x10"` | unchanged | **`ValueError`** | **crashes** |
| `float("nan")` | unchanged | **`ValueError`** | **crashes** |
| `float("inf")` | unchanged | **`OverflowError`** | **crashes** |
| `object()` | unchanged | **`TypeError`** | **crashes** |

This line is safe **only** because all six shipped subcommands happen to `return` an
`int` literal. Nothing enforces it, nothing tests it, and `main.main()` is annotated
`-> None` (`main.py:148`), so the `None` branch is already live.

### C. Values flowing through a dispatched subcommand

| # | Input | Observed result | Class |
|---|---|---|---|
| C1 | `leadminer stats missing.csv` | `RETURNED 2`, "no such file" | correctly handled (`cli.py:82-84`) |
| C2 | **`leadminer stats .`** (a directory) | **`IsADirectoryError: [Errno 21] Is a directory: '.'`** — traceback | **crashes** (S2-1) |
| C3 | **`leadminer stats <binary / non-UTF-8 file>`** | **`UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff`** — traceback | **crashes** (S2-1) |
| C4 | `leadminer validate <binary / non-UTF-8 file>` | same `UnicodeDecodeError` | **crashes** (S2-1) |
| C5 | **`leadminer stats <chmod 000 file>`** | **`PermissionError: [Errno 13]`** — traceback | **crashes** (S2-1) |
| C6 | **2 MB in one CSV cell** (both `stats` and `validate`) | **`csv.Error: field larger than field limit (131072)`** — traceback | **crashes** (S2-1) |
| C7 | 0-byte CSV | `RETURNED 1`, "is empty" | correctly handled |
| C8 | canonical header, 0 data rows | `RETURNED 1`, "is empty" — message is wrong (the file is *not* empty, it is data-empty) | minor (S3) |
| C9 | Arabic + French + emoji cell content (`مطعم Frédéric Café 😀`) | `RETURNED 0`, content preserved and counted | **correctly handled** |
| C10 | blank / whitespace / `0` / negative / wrong-typed cells (`rating=-1`, `review_count=-99`, `name=""`, `region="  Beirut  "`) | `validate` → `RETURNED 1` with the rating problem flagged; `stats` → `RETURNED 0` | correctly handled |
| C11 | **`leadminer score` on a foreign-schema CSV** (cols `company,town,tel,url,notes`) | **`RETURNED 0`**, "re-scored 1 records, 1 changed". Output is a 22-column file in which `name`, `town`, `tel`, `url`, `notes` are **all blank**; only `country=LB`, `industry_priority=low`, a real-looking pitch string and `lead_score=0` survive | **silently wrong** (S2-2) |
| C12 | **`leadminer score master.csv --out ""`** | **`RETURNED 0`**, master **rewritten in place**. The final line *does* print the input path, so the mistake is visible only if read | **silently wrong** (S2-3) |
| C13 | `leadminer score master.csv --out " "` (whitespace) | `RETURNED 0`, writes a file literally named `" "` **in `$CWD`, not next to the input** | **silently wrong** (S2-3) |
| C14 | `leadminer score master.csv --out "-"` | `RETURNED 0`, writes a file literally named `-` (no stdout convention) | **silently wrong** (S2-3) |
| C15 | `leadminer score master.csv --out "0"` | `RETURNED 0`, writes a file literally named `0` in `$CWD` | **silently wrong** (S2-3) |

  C13-C15 are not theoretical. Reproducing them wrote the stray files `" "`, `-` and
  `0` into the **repository root** — because `main.py:121`'s
  `path.parent.mkdir(parents=True, exist_ok=True)` resolves a bare name's parent to `.`,
  so the output lands wherever the operator happened to be standing, not where the input
  lives. Under `systemd`, `cron`, or a CI runner with a workspace `$CWD`, that is
  invisible.
| C16 | `leadminer score master.csv --out <missing dir>/x.csv` | `RETURNED 0`, directory created (`main.py:121`) | correctly handled |
| C17 | **`leadminer score`/`run`/`scrape` with `requests` absent** | **`ModuleNotFoundError: No module named 'requests'`** — traceback. `cmd_doctor` guards exactly this at `cli.py:225-231`; `cmd_score` does not | **crashes** (S2-4) |
| C18 | `leadminer run` when `pipeline.main()` returns `None` | **`RETURNED 0`** — success | **silently wrong** (S1-2) |
| C19 | `SIGINT` during `run`/`scrape` | `RETURNED 130` — but only after **4.01 s** in a synthetic pool with a 4 s worker; `ThreadPoolExecutor.__exit__` calls `shutdown(wait=True)` | **silently wrong** (S3-4) |
| C20 | `leadminer validate` from the wrong `$CWD` | `RETURNED 2`, "no such file: data/all_businesses.csv" — `DATA_DIR` is CWD-relative (`cli.py:26`) | minor (S3-5) |
| C21 | **`leadminer validate` on a 1-column CSV (`name` only)** | **`RETURNED 0`, "all checks passed"** | **silently wrong** (S1-1) |
| C22 | **`leadminer validate` on 5,000 rows with every field but `name` blank** | **`RETURNED 0`, "checked 5,000 rows / all checks passed"** | **silently wrong** (S1-1) |
| C23 | **`leadminer validate` on 3,000 rows where the `lon` column is absent and `lat=51.5` (London)** | **`RETURNED 0`, "all checks passed"** — the bounding-box check at `cli.py:159-173` skipped 100 % of rows | **silently wrong** (S1-1) |

---

## Findings

### S1 — `validate` reports "all checks passed" on files it structurally cannot check

- **Where:** `cli.py:151-192`, specifically the coordinate loop at `cli.py:164-171`.
- **Breaks:** Every one of the five checks is *skip-if-absent*:

  | Check | Skips when |
  |---|---|
  | blank name (`:151`) | the `name` key is absent → `r.get("name")` is `None` → not counted blank |
  | BOM (`:154`) | `name` absent → `(None or "")` → `""` → `startswith` false |
  | geo bounds (`:165-171`) | `float(r["lat"]), float(r["lon"])` raises `KeyError` for a missing column, and `cli.py:168` catches `(ValueError, TypeError, KeyError): continue` — **a missing column and a malformed value are the same branch: skipped** |
  | rating range (`:177-186`) | `rating` absent → `""` → `continue` |
  | duplicate pairs (`:190`) | absent columns → a constant `(None, None)` tuple for every row → dedups fine, no problem reported |

  There is no header assertion anywhere in `cmd_validate`, and `load_master`
  (`main.py:77-105`) has none either — it just hands `csv.DictReader` whatever columns
  it finds. So "I could not check this" is encoded as "this is fine", and `main` then
  faithfully turns that `0` into process exit `0`.
- **Trigger (three independent reproductions):**
  1. `leadminer validate` (no argument, so `cli.py:298`'s default
     `data/all_businesses.csv`) against a 5,000-row master whose header is intact but
     whose every data cell is blank →
     `checked 5,000 rows in data/all_businesses.csv` / `all checks passed` / exit `0`.
     100 % zero phone, zero email, zero region, zero coordinates, zero website.
  2. A one-column CSV whose single column is named `name` → `all checks passed`, exit `0`.
  3. A 22-column-header CSV with the `lon` column removed and every `lat` set to
     `51.5` → `all checks passed`, exit `0`. C23 above.
- **Why it matters here specifically:** this is not an exotic file. `load_master`
  converts *every* blank cell to `None` (`main.py:88-89`) and *every* unparseable
  numeric to `None` (`main.py:94-96`, `:100-101`). The most common real failures of
  this pipeline — a broken column, a schema change on the Drive copy, a truncated
  `rclone` pull, a partially-migrated CSV — all land the file in exactly the all-`None`
  state this gate blesses. And the consequence is not recoverable: `write_csv`'s own
  docstring (`main.py:108-116`) calls the master "cumulative master, all time", the
  workflow rclones `data/` back to Drive, and there is no second copy or version
  history. A green gate is the signal to trust the output; trusting this output means
  overwriting the good copy.
- **Also note:** nothing downstream catches it. `leadminer stats` on the same file
  exits `0` and prints a wall of zeros (`by region: Unknown 5,000`), and `leadminer
  score` on a foreign file emits a full 22-column artifact with a plausible
  `recommended_service` string (C11). Every layer agrees the data is fine.
- **Fix:** In `cmd_validate`, assert the header before any check —
  `missing = set(main.FIELDS) - set(rows[0])`; if `missing`, append a problem naming
  them and return `1` immediately. Split `cli.py:168` so `KeyError` counts as a
  *problem* (missing column) and only `ValueError`/`TypeError` count as *skipped*.
  Add a fill-rate floor: any field with a non-zero expected fill rate that is `< 5 %`
  populated across the file is a problem. One line each, and it closes C21/C22/C23.

### S1 — `leadminer run` can never report failure; `or 0` converts `None` into exit 0

- **Where:** `cli.py:315`, fed by `cli.py:37` (`return pipeline.main()`), typed
  `-> None` at `main.py:148`.
- **Breaks:** `main.main()` ends by falling off the end of the function (`main.py:242`),
  so it returns `None`. `None or 0` → `0`, and `raise SystemExit(main())` at
  `cli.py:322` exits **0**. Meanwhile `main.py:167-168` catches every scraper
  exception, prints `[GooglePlacesScraper] ERROR: ...` to stderr, and continues to
  write all five CSVs from whatever was already in the master. So: all three sources
  down → five non-empty, plausible-looking CSVs → `re-scored`/summary output → exit 0.
- **Trigger:** revoke `GOOGLE_PLACES_API_KEY` and point the workflow at a host where
  Overpass and Wikidata both return 503. Verified: `cli.main(["run"])` with
  `pipeline.main = lambda: None` returns `0`.
- **Fix:** Have `main.main()` return an `int` (e.g. `1` if any scraper failed and zero
  records survived the whitelist, `main.py:172`), and delete the `or 0` from
  `cli.py:315` — replace with an explicit check that rejects `None` and non-int
  returns loudly rather than laundering them into success.

### S2 — The `try` block catches one exception type, so the diagnostic commands traceback on trivially reachable inputs

- **Where:** `cli.py:314-318`.
- **Breaks:** `KeyboardInterrupt` is the only handled type. C2-C6 and C17 are all
  reachable from a shell with no malformed input whatsoever:
  - `leadminer stats .` — `IsADirectoryError`. `path.exists()` (`cli.py:82`) returns
    `True` for a directory, so the guard at `cli.py:82-84` misses it and `open()` at
    `cli.py:86` raises.
  - `leadminer stats <corrupt.csv>` — `UnicodeDecodeError`. `leadminer validate`'s
    entire purpose is to detect corruption; it cannot survive the first byte of it.
  - `leadminer stats <chmod 000>` — `PermissionError`.
  - 2 MB in one cell — `csv.Error: field larger than field limit (131072)`. Note the
    message is just `Error: ...` with no file or row context, so even the traceback
    does not tell you which record.
  - `leadminer score` with `requests` absent — `ModuleNotFoundError`. `cmd_doctor`
    guards this exact case at `cli.py:225-231`, with a comment at `cli.py:228-229`
    saying "doctor exists to diagnose a broken environment, so it must never be the
    thing that tracebacks." The author identified the principle and applied it to one
    command out of six.
- **Aggravating factor — exit-code collision:** `cmd_validate` returns `1` for "data has
  problems" (`cli.py:201`), and an uncaught exception also exits `1`. A caller cannot
  distinguish "the gate rejected the data" from "the gate broke". This is exactly what
  audit 037's exit-code table fixes: `5 = EX_DATAERR` vs `1 = EX_SOFTWARE`.
- **Why S2 not S1:** every one of these is loud — non-zero exit, visible traceback —
  and `write_csv` is atomic (`main.py:123-133`, including the `except BaseException:
  tmp.unlink(); raise` cleanup), so none of them can corrupt the master.
- **Fix:** add a `except OSError as e: print(f"{type(e).__name__}: {e}", file=sys.stderr); return 2`
  arm for the I/O family, hoist the `ImportError` guard from `cmd_doctor` into a shared
  `_require(module)` helper used by `cmd_score`/`cmd_scrape`/`cmd_run`, and adopt the
  037 exit-code table so `1` and `2` stop colliding.

### S2 — `score` silently converts a foreign-schema CSV into 22 blank columns

- **Where:** `cli.py:259` (`pipeline.load_master`, no header check anywhere in
  `main.py:77-105`) into `cli.py:275` → `main.py:125`
  (`csv.DictWriter(..., extrasaction="ignore")`).
- **Breaks:** input `company,town,tel,url,notes` / `Acme SAL,Tripoli,+9611,https://acme.lb,great lead`
  produces, with exit `0` and the message `re-scored 1 records, 1 changed`:

  ```
  name,category,region,country,...,source,scraped_at
  ,,,LB,,,,,,,,,,,,,,,0,low,Full digital launch (brand + website + social setup),,
  ```

  `extrasaction="ignore"` silently drops the five real columns; `DictWriter`'s default
  `restval=""` silently blanks the other nineteen. `cmd_score`'s only guard is
  `if not records` (`cli.py:260`) — a list of dicts is non-empty, so it proceeds. The
  input file itself is untouched (verified), so this is a poisoned *output*, not a
  destroyed input; but the output is the product.
- **Trigger:** `leadminer score ~/Downloads/leads_export.csv --out out.csv` where that
  file came from a CRM, a previous schema, or another tool.
- **Fix:** In `cmd_score`, check `set(records[0]) >= set(main.FIELDS)` before the
  rescore loop and return `2` listing the missing columns. Same assertion belongs in
  `load_master`.

### S2 — `args.out` is tested for truthiness, so an empty path means "overwrite in place"

- **Where:** `cli.py:274` — `out = pathlib.Path(args.out) if args.out else path`.
- **Breaks:** `""` is falsy, so `--out ""` is indistinguishable from omitting the flag,
  and `pipeline.write_csv(out, records)` at `cli.py:275` atomically overwrites the
  **cumulative master** (`main.py:108-116`) with re-scored values. Verified: exit `0`,
  and the last line prints `-> .../master.csv` — the input path — so the mistake is
  visible only to a reader who compares two paths that are the same. Meanwhile `" "`,
  `"-"` and `"0"` *are* truthy and so create literal files named `" "`, `-` and `0` in
  `$CWD`, also exit `0`. Four spellings of "where should this go", one of which
  destroys the file you meant to preserve, none of which errors.
- **Trigger:** `--out "$OUT"` in a shell script with `OUT` unset — an extremely common
  pattern, and the single most likely way this fires in the GitHub Actions workflow.
- **Fix:** `if args.out is None:` instead of `if args.out:`; additionally reject
  whitespace-only and `-` values with a usage error, and refuse an `--out` that
  resolves to the input path unless `--in-place` was passed.

---

## Not a bug, but worth knowing

- **The exit-code scheme is the best thing about this file.** `0` ok / `1` problems /
  `2` missing file / `130` interrupted, with all diagnostics on stderr (`cli.py:84`,
  `:89`, `:139`, `:145`, `:256`, `:261`) and data on stdout. That separation is what
  makes every finding above *fixable* rather than merely reportable — adopt the 037
  table and only `validate`'s collision needs resolving.
- **`main` cannot corrupt the master.** `write_csv` writes to `<path>.tmp`, fsyncs,
  then `os.replace` (`main.py:122-130`), with an `except BaseException: tmp.unlink(); raise`
  cleanup at `main.py:131-133`. Every traceback in this report leaves the previous
  complete version intact. That caps the severity of S2-1/S2-2 at "bad artifact"
  rather than "lost master", and it is worth preserving through any refactor.
- **`stats` deliberately does not collapse blank `website_live` into `False`**
  (`cli.py:105-113`, with the comment naming the original bug). Tri-state reporting is
  correct and the `unreachable` line is genuinely useful.
- **Unicode is handled properly everywhere it matters.** Arabic (`مطعم`), French
  accents (`Frédéric`), and emoji (`😀`) survive argv, CSV read, CSV write and console
  output. A lone surrogate (`"\udcff"`, how CPython represents undecodable argv bytes)
  round-trips through a real stderr under both the default locale and `LC_ALL=C`
  without a `UnicodeEncodeError`, because CPython's stdio uses `surrogateescape`.
- **No argv-volume denial of service.** 6,000 unknown long options parse in 22 ms; a
  single 2 MB argument is rejected in 9–14 ms. The `Path.exists()` guard also absorbs
  5,000-character paths and embedded NULs rather than crashing. The input surface is
  genuinely well-defended — the failures here are all about *file contents and return
  values*, not about `argv` volume or encoding.
- **`cmd_stats` is internally inconsistent about blank cells.** The count section
  normalises (`(r.get(field) or "").strip()`, `cli.py:96`) but the grouping sections do
  not, so a blank `industry_priority` is counted under the key `""` and rendered as
  `f"    {str(k):<10}"` → a row of ten spaces with no label (`cli.py:116-131`). `by
  region` alone collapses blanks to `"Unknown"` (`cli.py:124`). On an all-blank master
  the output reads `5,000` three times with nothing beside it. (S3, cosmetic.)
- **`prog="leadminer"` is hardcoded** at `cli.py:281`, so `python cli.py --help`,
  `python -m cli --help` and `python -c 'import cli; cli.main()'` all print help headed
  `usage: leadminer`. (S3, cosmetic.)

### S3 — Minor

1. **`main([])` raises `SystemExit(2)` instead of returning `2`**, violating its own
   `-> int` annotation (`cli.py:312`) and the evident intent of the normalising
   `int(... or 0)` at `cli.py:315`. Correct for a shell entry point, wrong for any
   programmatic caller. Fix: wrap line 313 in `try: … except SystemExit as e: return
   int(e.code or 0)`.
2. **Non-`str` argv elements traceback** (A10, A12-A15). `main(["stats", Path("x.csv")])`
   — the most natural call a Python wrapper or test would make — raises
   `TypeError: 'PosixPath' object is not subscriptable`, because `argparse` iterates and
   subscripts each element. Fix: `argv = [os.fspath(a) if isinstance(a, os.PathLike) else a
   for a in argv]` with a `str()` fallback.
3. **A `str` passed as `argv` is silently shredded into characters** (A11):
   `main("stats")` → `invalid choice: 's'`. The error message does not hint that a
   string was passed instead of a list. Fix: `if isinstance(argv, str): argv = [argv]`.
4. **`int()` at `cli.py:315` is an unvalidated coercion** (table B). It truncates
   floats (`2.7` → `2`), passes negative and >255 codes straight through to the shell
   (`-1` → `$? = 255`, `256` → `$? = 0`, both verified via `subprocess`), and
   tracebacks on `"3.5"`, `[1]`, `nan`, `inf`. None of these is reachable today only
   because all six subcommands return `int` literals. Fix: assert
   `isinstance(rc, int) and not isinstance(rc, bool) and 0 <= rc <= 255`, else `return 1`.
5. **`SIGINT` handling waits for in-flight threads before it can report.** The
   `except KeyboardInterrupt` at `cli.py:316` is inside `main`, but the `with
   ThreadPoolExecutor` blocks at `main.py:160` and `enricher.py:237` (40 workers) run
   *inside* `args.func(args)`, so `__exit__` → `shutdown(wait=True)` fires first.
   Measured: 4.01 s to return `130` for a single 4 s worker. At production settings
   (`timeout=15`, `retries=3`, jittered backoff to 60 s — `httpclient.py:47-50`,
   `:196-204`) the worst case is roughly **105 s per URL**, so Ctrl-C on `leadminer run`
   looks like a hang and an operator will SIGKILL, losing the run and the Google Places
   quota. Fix: `pool.shutdown(wait=False, cancel_futures=True)` in the handlers, or
   install a `signal.SIGINT` handler that raises inside `main` before the `with` exits.
6. **`DATA_DIR` is CWD-relative** (`cli.py:26`), so the defaults baked into
   `cli.py:294`, `:298`, `:305` resolve against the caller's working directory.
   `leadminer validate` from `/tmp` → `no such file: data/all_businesses.csv`, exit 2
   (verified). Worse, from a directory that happens to contain an unrelated `data/`,
   it silently validates *that*. Fix: resolve relative to the package root.
7. **`--out ""` and a bare-header-only CSV** produce actively misleading messages
   (`is empty` for a schema-valid, zero-row file at `cli.py:89`/`:145`). Cosmetic, but
   "empty" and "zero rows" are different diagnoses.

---

## The single most dangerous input

**`leadminer validate` — no arguments at all, against a `data/all_businesses.csv` whose
22-column header is intact but whose data columns are empty.**

Reproduced exactly:

```
$ leadminer validate            # 5,000-row master, header intact, every field but name blank
checked 5,000 rows in data/all_businesses.csv
  all checks passed
$ echo $?
0
```

100 % of rows with zero phone, zero email, zero region, zero lat/lon, zero website, and
the gate reports the file is clean.

It is the worst of the enumerated inputs because of what it combines:

1. **The user cannot cause it.** `cli.py:298` makes `file` optional with a hardcoded
   default, so the operator never names the file. There is no typo to catch, no path to
   mistype, no argument to get wrong.
2. **It inverts the one contract that exists to be inverted.** Every other subcommand
   produces data or prints statistics; only `validate` is documented to *fail*
   (`cli.py:136`: "Cheap data-quality gate. No network. **Exits non-zero if checks
   fail**"). The single most dangerous possible behaviour for it is to exit 0.
3. **Absence of data is encoded as absence of problems.** `cli.py:168` catches
   `(ValueError, TypeError, KeyError): continue` — the same branch for "the column does
   not exist" and "this cell is malformed". The skip is *indistinguishable from a pass*
   by construction, and there is no header assertion to catch it upstream
   (`cmd_validate` has none; `load_master` at `main.py:77-105` has none).
4. **It is the pipeline's own most likely failure state.** `load_master` converts every
   blank to `None` (`main.py:88-89`) and every unparseable numeric to `None`
   (`main.py:94-96`, `:100-101`). A dropped column, a schema change on the Drive copy, a
   half-finished `rclone` pull, a bad `HEADER=` in a spreadsheet export — all of them
   produce exactly this file.
5. **`main` launders it.** `cmd_validate` returns `0` → `int(0 or 0)` → `0` →
   `raise SystemExit(0)` (`cli.py:315`, `:322`). Nothing between the verdict and the
   shell can flag it.
6. **Every other layer agrees.** `leadminer stats` on the same file exits `0` and prints
   zeros; `leadminer score` on a foreign file exits `0` and emits a plausible
   `recommended_service`. There is no second opinion available anywhere in the codebase.
7. **The follow-on is irreversible.** The master is "cumulative master, all time"
   (`main.py:108-116`), the workflow rclones `data/` back to Drive, and there is no
   version history or second copy. A green gate is the human's or CI's signal to trust
   the output; trusting *this* output means overwriting a good cumulative dataset with a
   blanked one. `write_csv`'s atomicity guarantees you still have the old file on disk
   right up until the moment someone uploads the new one.

Compare the alternatives: `IsADirectoryError` (C2) and `UnicodeDecodeError` (C3) are
loud — non-zero exit, unmistakable traceback, and the atomic write guarantees nothing was
damaged. `--out ""` (C12) at least prints the path it wrote. This input is the only one
that reports *success* while the product is empty.

**If you fix exactly one thing:** assert the header in `cmd_validate` before any check —
`missing = set(main.FIELDS) - set(rows[0]); if missing: report and return 1` — and split
`cli.py:168` so `KeyError` is a *problem* rather than a *skip*. Two lines, and it closes
C21, C22 and C23 together. Second: make `main.main()` return an `int` and delete the
`or 0` at `cli.py:315`, so `leadminer run` is capable of reporting failure at all.

---

## Recommended order of work

1. **`cmd_validate`: header assertion + `KeyError` counts as a problem, not a skip**
   (`cli.py:151`, `cli.py:168`). Closes the most dangerous input in this report.
2. **`main.main()` returns `int`; remove `or 0` from `cli.py:315`** and validate the
   return (int, not bool, 0-255). Restores `leadminer run`'s ability to fail.
3. **Exception arms in `main`** (`cli.py:314-318`): `OSError` → 2, `UnicodeDecodeError` /
   `csv.Error` → 1 with file and row context, plus the `ImportError` guard hoisted out
   of `cmd_doctor` (`cli.py:225-231`) into a shared helper. Adopt the 037 exit-code table
   so `1` (EX_SOFTWARE) and `5` (EX_DATAERR) stop colliding.
4. **`--out` truthiness → `is None`** (`cli.py:274`), reject whitespace/`-`, require
   `--in-place` to write over the input.
5. **`load_master` header validation** (`main.py:79`) so `score` refuses a foreign-schema
   CSV instead of emitting 22 blank columns.
6. **argv normalization** (`cli.py:312-313`): `str` → `[str]`, `os.PathLike` → `os.fspath`,
   non-str → clear error; return an int from `SystemExit` instead of propagating it.
7. **`SIGINT` responsiveness**: `shutdown(wait=False, cancel_futures=True)` in the
   handlers, or a signal handler that raises before the pool `__exit__`.
8. **`DATA_DIR` anchored to the package root** (`cli.py:26`) instead of `$CWD`.