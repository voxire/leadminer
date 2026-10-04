# 200 — `cli.main` return contract vs `BusinessRecord` in practice

## Verdict

`cli.main` declares `-> int` (`cli.py:312`) and does `int(args.func(args) or 0)`
(`cli.py:315`), but the function it wraps, `main.main()`, is annotated `-> None`
(`main.py:148`) and swallows every scraper exception (`main.py:167-168`) — so
`leadminer run` **structurally cannot exit non-zero**, and a run in which all
three sources are dead reports success. That is the whole contract in one line:
the CLI's most important return value is a constant.

Below that, the only command that round-trips records is `cmd_score`
(`cli.py:247-277`), and it inverts the declared contract in both directions: the
record shape on its input path is whatever the CSV *header* says, the output
shape is `main.FIELDS` (`main.py:33-40`), and `write_csv`'s `extrasaction="ignore"`
(`main.py:125`) means **the difference between those two sets is silently
destroyed** — while the file being destroyed is the input file, by default.

---

## Findings

### S1 — `leadminer run` cannot fail; a total scrape outage is a green build

- **Where:** `cli.py:312` (declared `-> int`), `cli.py:33-37` (`cmd_run`), `main.py:148`
  (`def main() -> None`), `main.py:167-168` (per-scraper `except Exception` → stderr),
  `main.py:209-213` (writes all five CSVs regardless).
- **Breaks:** `cmd_run` is `return pipeline.main()`. `main.main()` has no failure
  return value at all — it is annotated `-> None` and every scraper exception is
  caught and *printed* (`main.py:167-168`). So `args.func(args)` is `None`,
  `None or 0` is `0`, and `leadminer run` exits 0 after writing five CSVs built
  from zero scraped rows. Reproduced:
  ```
  main.main() return value : None
  cli.main(['run'])        : 0   <- exit code 0 with all 3 sources dead
  ```
  Note this is not a CLI-only problem: `scrape.yml:69` still invokes
  `python main.py`, which exits 0 for the identical reason. The only thing
  currently preventing a silently-empty master from being uploaded to Drive is
  the inline row-count guard at `scrape.yml:78-83` — the safety net lives in the
  workflow, not in the code that produces the data.
- **Trigger:** unset `GOOGLE_PLACES_API_KEY` *and* have Overpass/Wikidata fail
  (rate-limit, DNS, 5xx). All three scrapers print an error and return nothing.
  `main.main()` proceeds to `enrich([])` and writes 5 CSVs containing only
  whatever the previous master held. `leadminer run` → exit 0.
- **Fix:** make `main.main()` return a result object (record counts + a list of
  failed sources), have it return non-zero when any source fails or when the
  row count collapses vs. the loaded master, and have `cmd_run` propagate that
  instead of `None`.

### S1 — `leadminer score <file>` overwrites its input and silently drops every column outside `main.FIELDS`

- **Where:** `cli.py:274` (`out = pathlib.Path(args.out) if args.out else path`),
  `cli.py:275` (`pipeline.write_csv(out, records)`), `main.py:125`
  (`csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")`).
- **Breaks:** `--out` is optional, so the default target *is the input path*
  (`cli.py:274`). `write_csv` then rewrites the file using `main.FIELDS` as the
  only header, with `extrasaction="ignore"`. Any column the input had that is not
  in `FIELDS` is erased with no warning, no backup, and exit code 0. Reproduced on
  a 3-column CSV (`name,phone,notes`):
  ```
  in  header: name,phone,notes
  exit code : 0
  out header: name,category,region,country,address,lat,lon,phone,email,website,
              website_live,facebook,instagram,whatsapp,linkedin,rating,
              review_count,completeness_score,lead_score,industry_priority,
              recommended_service,source,scraped_at
  'notes' preserved? -> False
  ```
  and with `--out` omitted, on a copy of `sales_ready.csv`:
  ```
  header before: name,phone,notes
  exit code    : 0
  header after : name,category,region,...   <- original destroyed in place
  ```
  The 5 master CSVs (`main.py:209-213`) are the cumulative product and the only
  copy between Drive syncs, so this is unrecoverable data loss.
- **Trigger:** `leadminer score data/sales_ready.csv` — a completely reasonable
  command given `score` defaults to `data/all_businesses.csv` (`cli.py:305`).
- **Fix:** require `--in-place` to write to the input path; default `--out` to a
  sibling (`*.rescored.csv`), and make `write_csv` report ignored keys instead of
  swallowing them.

### S1 — `website_live` is `bool | None` in the type but a *string* on the only path that re-reads it; `"false"` silently deletes the strongest pitch signal

- **Where:** declared `website_live: bool | None` (`scrapers/base.py:16`); written
  as a string by `csv.DictWriter` (`main.py:125-127`); parsed back with strict
  `== "True"` / `== "False"` string comparison at `main.py:103` and again at
  `cli.py:107-110`; consumed with identity checks at `enricher.py:300-304` and
  `pitch_recommender.py:34-36`.
- **Breaks:** `bool | None` never survives a CSV round-trip — it comes back as
  the literal strings `"True"` / `"False"` / `""`. `load_master` recovers that
  with an exact, case-sensitive comparison. Anything else normalises to `None`,
  i.e. `UNKNOWN`, i.e. *"we never got an answer"* — which the code explicitly
  documents as **not a pitch signal** (`enricher.py:151-153`). So one external
  writer silently converts server-confirmed-dead sites (the Tier-1 pitch,
  `pitch_recommender.py:57-58`) into "unreachable". Reproduced:
  ```
  website_live="False" (what write_csv emits)  -> load_master gives False  lead_score=50
  website_live="false" (hand-edited / Sheets)  -> load_master gives None   lead_score=30
  website_live="FALSE"                         -> load_master gives None   lead_score=30
  ```
  20 points gone, and the lead drops out of the strongest pitch bucket. Worse,
  `leadminer stats` (`cli.py:107-110`) uses the *identical* strict comparison, so
  it reports those rows under `unreachable  (not the same as dead)` — actively
  reinforcing the wrong reading. Two independent readers, one shared bug.
- **Trigger:** open `data/all_businesses.csv` in Google Sheets or Excel, let it
  round-trip booleans to lowercase `false`, save, run `leadminer score`. Note the
  project is *built around* Sheets/Drive (`scrape.yml:88`), so this is a routine
  operator action. `write_csv`'s own output is safe; one external writer is not.
- **Fix:** `enricher.py:154-156` already defines the right serialisation —
  `LIVE = "live"`, `DEAD = "dead"`, `UNKNOWN = "unknown"`. Emit those three
  strings instead of `bool`, and parse with `.strip().lower()` on read.

### S2 — `cmd_score` recomputes `lead_score` from a `completeness_score` it never recomputes

- **Where:** `cli.py:264-272` writes exactly four fields — `country`,
  `industry_priority`, `recommended_service`, `lead_score`. `completeness_score`
  is read from the input CSV (`main.py:96-101`) and written straight back out.
- **Breaks:** `completeness_score` is a pure offline function
  (`enricher.py:101-117`) and is recomputed on every real run (`enricher.py:349`),
  but `leadminer score` — whose whole advertised purpose is "re-score existing
  records, no network" (`cli.py:247-248`, `cli.py:304`) — skips it. A blank or
  stale value stays blank, and `main.py:206` gates `qualified_businesses.csv` on
  `completeness_score >= 1`. Reproduced:
  ```
  row has a phone + address, so completeness_score *should* be 2
  cmd_score wrote completeness_score = ''
  main.py:206 `qualified` filter is `completeness_score >= 1` ->
      EXCLUDED from qualified_businesses.csv despite having phone+address
  cmd_score wrote lead_score         = '30'   (that one it did recompute)
  ```
  So the output row has `lead_score=30` and `completeness_score=''` — internally
  inconsistent, and it fails the only quality gate the product has.
- **Trigger:** `leadminer score data/qualified_businesses.csv`, or any CSV whose
  `completeness_score` column is blank/stale.
- **Fix:** add `r["completeness_score"] = completeness_score(r)` to the
  `cli.py:265-272` loop; it costs nothing and needs no network.

### S2 — every default path is relative to the process CWD, so an installed `leadminer` run from the wrong directory starts a fresh empty master

- **Where:** `cli.py:26` (`DATA_DIR = pathlib.Path("data")`), `main.py:41` (same),
  `main.py:150` (`load_master(DATA_DIR / "all_businesses.csv")` → `[]` if absent),
  `main.py:208-213` (`DATA_DIR.mkdir(exist_ok=True)` then write).
- **Breaks:** `leadminer` is installed as a **global console script**
  (`pyproject.toml:33`), but its data root is CWD-relative and there is no
  `--data-dir` flag anywhere in `build_parser()` (`cli.py:280-309`) and no env
  override anywhere in the repo (grep for `DATA_DIR|environ|getenv` finds only
  `DATA_DIR` literals plus `GOOGLE_PLACES_API_KEY` / `SCRAPER_EMAIL`). So
  `cd /tmp && leadminer run` finds no master, silently creates `data/`, scrapes
  into a brand-new empty history, and — per the S1 above — exits 0.
- **Trigger:** `cd ~ && leadminer run`.
- **Fix:** resolve `DATA_DIR` from `LEADMINER_DATA_DIR` or a `--data-dir` flag,
  `Path(...).resolve()`, and fail closed when the master is missing but a Drive
  sync is expected (mirroring the fail-closed logic already at `scrape.yml:57-63`).

### S2 — `cmd_validate` reports "passed" for rows it never checked; a fully truncated CSV clears the coordinate gate

- **Where:** `cli.py:165-173`.
- **Breaks:** `float(r["lat"]), float(r["lon"])` is wrapped in
  `except (ValueError, TypeError, KeyError): continue`. A missing or `None`
  coordinate is therefore *skipped*, and a file where **every** row is missing
  coordinates reports `bad_coords == 0` → `all checks passed`, exit 0. The gate
  conflates "not applicable" with "valid", which is the one thing a data-quality
  gate must not do — especially since it is the advertised replacement for the
  hand-rolled guard at `scrape.yml:71-83`. Reproduced:
  ```
  === cmd_validate on a CSV whose rows are truncated ===
  header: name,category,lat,lon,website,rating
    Cafe Hamza,restaurant,33.89,35.50,hamza.com,4.9
    Broken Row,restaurant
  checked 2 rows ... all checks passed
  exit code: 0
  ```
- **Trigger:** any CSV with a ragged final row, or a truncated export.
- **Fix:** count skipped rows and append
  `f"{skipped} rows have no parseable coordinates"` to `problems` instead of
  `continue`-ing silently.

### S3 — `int(args.func(args) or 0)` is an unverifiable coercion over an `Any`, and `cmd_run` returning `None` from `-> int` is a mypy error no CI step can see

- **Where:** `cli.py:312` (`-> int`), `cli.py:315`, `cli.py:33` (`cmd_run -> int`),
  `cli.py:37` (`return pipeline.main()`), `pyproject.toml:66` (`strict = true`),
  `scrape.yml` (no lint/typecheck/test step at all).
- **Breaks:** two layers of nothing. (a) `argparse.Namespace.__getattr__` returns
  `Any` in typeshed, so `args.func(args)` is `Any` and `int(Any)` typechecks —
  nothing binds a subcommand's return type to `int`. (b) `main.main()` is annotated
  `-> None` (`main.py:148`) and `cmd_run` is `-> int`, so `cli.py:37` is a
  mypy `return-value` error ("Returning `None` from function declared to return
  `int`"). `mypy` is declared in `pyproject.toml:26` and `strict = true` at
  `:66`, but no workflow step ever runs it, so the violation ships silently.
  Meanwhile `int(...)` also *hides* shape errors rather than surfacing them:
  ```
  subcommand returns None (cmd_run today) -> int(x or 0) = 0
  subcommand returns list                 -> int(x or 0) = 0   <- silent!
  subcommand returns bool True            -> int(x or 0) = 1
  subcommand returns str 'ok'             -> ValueError: invalid literal for int()
  ```
  A subcommand that returns a list is silently reported as success.
- **Trigger:** add any subcommand whose `func` returns a non-int (e.g.
  `return records`).
- **Fix:** type `build_parser` as returning a parser whose `set_defaults(func=...)`
  values are `Callable[[Namespace], int]`, drop the `int(...)` coercion, and add a
  `mypy --strict` step to CI so `cli.py:37` fails the build.

### S3 — the two entry points disagree about a non-int return

- **Where:** `cli.py:322` (`raise SystemExit(main())`) vs. the wrapper
  `pyproject.toml:33` (`leadminer = "cli:main"`) generates `sys.exit(main())`.
- **Breaks:** `SystemExit("ok")` prints `ok` to stderr and exits 1;
  `sys.exit("ok")` prints `'ok'` and exits 1. Different diagnostics for the same
  bug, and neither mentions which subcommand failed.
- **Trigger:** any subcommand returning a non-int (see previous finding).
- **Fix:** `main()` returning a validated `int` makes the question moot; until
  then, log the failing command name in the `except` path.

### S3 — `cmd_scrape` returns 1 for both "no API key" and "source is broken"

- **Where:** `cli.py:63-66` (`return 1` + "The source may be broken or blocked"),
  `google_places.py:169-171` (missing key → `print(...)` then bare `return`, an
  empty generator).
- **Breaks:** the two causes are indistinguishable at the exit code and in the
  message. `leadminer scrape --source google_places` with no key exits 1 telling
  you the source "may be broken or blocked" — and the scraper's own
  configuration warning is buried above it.
- **Trigger:** `env -u GOOGLE_PLACES_API_KEY leadminer scrape --source google_places`.
- **Fix:** check `os.environ` before constructing the scraper and exit 2
  ("misconfigured") vs 1 ("broken source").

### S3 — `cmd_run` ignores `args` entirely; `run` has no options, contradicting the module docstring

- **Where:** `cli.py:33-37` (`args` is never read), `cli.py:285-286` (the `run`
  subparser adds no arguments), `cli.py:14-15` ("Commands other than
  run/scrape/doctor never touch the network, so they are safe to run anywhere and
  are what you reach for when a run looks wrong").
- **Breaks:** the diagnostic commands exist, but there is no way to *reproduce* a
  suspicious run cheaply — no `--dry-run`, no `--limit`, no `--skip-enrich`, no
  `--source`. A 5-hour run either executes fully or not at all.
- **Trigger:** `leadminer run --help` → no options beyond the subcommand name.
- **Fix:** add `--source/-s` and `--limit` passthroughs; `main.main()` needs the
  parameters first.

### S3 — `cli.main` has zero test coverage

- **Where:** `tests/test_lead_signal.py:222`, `:238`, `:252`, `:263` all call
  `cmd_stats` / `cmd_validate` **directly**, bypassing `cli.main` entirely.
- **Breaks:** the target of this report — the dispatch, the `int(... or 0)`
  coercion, the `KeyboardInterrupt` → 130 path, and the `parse_args` → `SystemExit(2)`
  path — is untested. The S1 above is exactly the class of bug that a
  `test_cli_main_returns_nonzero_on_scraper_failure` would have caught.
- **Trigger:** n/a — this is a coverage gap.
- **Fix:** add `tests/test_cli_main.py` with `main(["stats", str(p)])` /
  `main(["validate", ...])` / a monkeypatched failing pipeline / `main(["run"])` with
  `KeyboardInterrupt`.

### S3 — only `KeyboardInterrupt` is caught; `leadminer stats big.csv | head -5` tracebacks

- **Where:** `cli.py:314-318` — the `try` covers `args.func(args)` but catches
  nothing except `KeyboardInterrupt`.
- **Breaks:** `BrokenPipeError` (any `| head`/`| less`), plus every genuine
  bug in any subcommand, escapes as a raw traceback. For a CLI whose stated
  purpose is debuggability, a stack trace on stdout piping is a poor first
  impression, and it masks the one-line message that would identify the fault.
  (`SystemExit` propagating is *correct* here — that is how `parse_args` yields
  exit 2.)
- **Trigger:** `leadminer stats data/all_businesses.csv | head -5`.
- **Fix:** add `except BrokenPipeError: devnull-dup stdout; return 0`, and wrap
  unexpected `Exception` with a one-line `f"{args.cmd}: {type(e).__name__}: {e}"`
  before re-raising under `--debug`.

### S3 — the 23-key schema is maintained twice, by hand, in two different orders, with nothing asserting equality

- **Where:** `scrapers/base.py:5-28` vs. `main.py:33-40`. Verified by AST parse:
  ```
  BusinessRecord keys: 23   main.FIELDS: 23
  set difference, both directions: empty      # sets ARE equal today
  same order?: False
  TD order : name, category, address, region, country, ... lead_score, source, scraped_at, completeness_score
  FL order : name, category, region, country, address, ... completeness_score, lead_score, ... source, scraped_at
  ```
  `BusinessRecord` is a `total=True` TypedDict (no `__total__` override), so all
  23 keys are *required and non-omittable* — yet `main.py:179` concatenates
  scraper records with `load_master` rows (`main.py:86-104`), which are plain
  `csv.DictReader` dicts whose key set is the arbitrary CSV header. Nothing
  anywhere asserts `set(FIELDS) == set(BusinessRecord)`.
- **Breaks:** add a column to `FIELDS` and forget `BusinessRecord` (or vice
  versa) and mypy cannot see it — no code path ever passes a `BusinessRecord`
  where a `dict` is expected, or vice versa (`recommend_service` takes `Mapping`,
  `lead_score`/`completeness_score`/`enrich`/`load_master`/`write_csv` all take
  bare `dict`, `scrapers/base.py:33` is the only `Iterator[BusinessRecord]` and
  its callers immediately widen to `dict`). The 23-key list is currently
  duplicated in at least three more places (the brief, `README`, the CSV headers).
- **Trigger:** any schema change.
- **Fix:** `FIELDS = list(get_type_hints(BusinessRecord))` in `main.py`, plus a
  test asserting `set(FIELDS) == set(BusinessRecord)`.

---

## Fields read or written by `cli.main`'s dispatch surface vs. `BusinessRecord`

Direct answer to the question asked: **no field *name* used anywhere under
`cli.main` is outside the TypedDict, and the two key sets are provably equal
today** (AST check above). But that is a coincidence, not a guarantee, and the
*values* and the *shape* both diverge.

### Fields written

| Field | Written at | In `BusinessRecord`? |
|---|---|---|
| `country` | `cli.py:266` | yes (`base.py:10`) |
| `industry_priority` | `cli.py:267` | yes (`base.py:23`) |
| `recommended_service` | `cli.py:268` | yes (`base.py:24`) |
| `lead_score` | `cli.py:272` | yes (`base.py:25`) |
| all 23 `FIELDS` | `cli.py:275` → `main.py:125-127` | yes (set-equal) |

Nothing extra is written. Nothing is dropped *silently by name* either — the
drop happens because `write_csv` narrows to `FIELDS` (`main.py:125`), which is
the S1 finding above.

### Fields read

| Field(s) | Read at | In `BusinessRecord`? | Actual type on this path |
|---|---|---|---|
| `category`, `phone`, `website` | `cli.py:68-70` (`cmd_scrape`) | yes | real `str \| None` (scraper output) |
| `phone, email, website, instagram, whatsapp, region` | `cli.py:98-103` (`cmd_stats`) | yes | `str` (DictReader) |
| `website`, `website_live` | `cli.py:107-110` | yes | `str` — compared as `"True"`/`"False"` (S1) |
| `industry_priority` | `cli.py:116` | yes | `str` |
| `source` | `cli.py:120` | yes | `str` |
| `recommended_service` | `cli.py:129` | yes | `str` |
| `name` | `cli.py:151, 154, 190` | yes | `str` |
| `lat`, `lon` | `cli.py:167` | yes (`float \| None`) | `str` — needs `# type: ignore[arg-type]` |
| `rating` | `cli.py:177` | yes (`float \| None`) | `str` — `float()`-coerced inline |
| `name`, `phone` | `cli.py:190` | yes | `str` |
| **`every column in the CSV header`** | `main.py:86-104` (`load_master`, via `cli.py:259`) | **user-controlled** | only the 10 keys in `_FLOAT_FIELDS`/`_INT_FIELDS`/`website_live` are cast |
| `country`, `phone` | `main.py:59, 69` (`resolve_country`, via `cli.py:266`) | yes | `str \| None` |
| `category` | `cli.py:267` → `whitelist.py:133` | yes | `str \| None`; **return is `-> str`, non-Optional** |
| `website, website_live, email, phone, instagram, facebook, rating, review_count, completeness_score, category` | `cli.py:268` → `pitch_recommender.py:31-53` | yes | `Any` (param is `Mapping`, untyped) |
| `phone, email, whatsapp, instagram, website_live, website, industry_priority, rating, source` | `cli.py:269` → `enricher.py:285-316` | yes | `Any` (param is bare `dict`) |

**Type divergence summary (declared vs. actual):**

| Declared | Actual on the `cmd_score`/`cmd_stats`/`cmd_validate` path |
|---|---|
| `lat/lon/rating: float \| None` | `str` (or `None`) — the `float \| None` annotation is unreachable there |
| `review_count: int \| None` | `str` until `main.py:96-101` casts it; `str` forever in `cmd_stats` |
| `website_live: bool \| None` | one of the 4 strings `"True"`/`"False"`/`""`/anything else (`base.py:16` vs `main.py:103`) |
| `lead_score: int`, `completeness_score: int` (**required**, non-Optional, `base.py:25,28`) | `None` whenever the CSV cell is blank (`main.py:87-89`) |
| `source: str`, `scraped_at: str` (**required**, non-Optional, `base.py:26-27`) | `''` when absent — and `load_master` never `setdefault`s them |
| key set | the CSV header, not the TypedDict |

And the structural point: **`BusinessRecord` has zero runtime presence in the
entire CLI path.** `BusinessRecord(...)` at `osm.py:98`, `wikidata.py:106` and
`google_places.py:281` is an ordinary `dict()` call — TypedDict enforces nothing
at runtime — and `cli.main`'s only record-consuming commands (`cmd_score`,
`cmd_stats`, `cmd_validate`) never touch it. `enricher.enrich` is what
materialises the full shape, via `r.setdefault(...)` at `enricher.py:340-348`
(and it `setdefault`s only 9 of the 23 keys), which `cmd_scrape` never calls.
A `total=True` TypedDict that is only ever constructed by three literal dicts is
documentation, not a contract.

### Where the return value breaks on an unexpected shape

| Site | Breaks when |
|---|---|
| `cli.py:315` `int(args.func(args) or 0)` | `list`/`tuple` return → silently `0` (success). `str` return → `ValueError` traceback. `None` → `0` (which is the current, wrong behaviour for `cmd_run`). |
| `cli.py:322` `raise SystemExit(main())` | non-`int` → message on stderr + exit 1. |
| `pyproject.toml:33` console script → `sys.exit(main())` | non-`int` → different message on stderr + exit 1. Inconsistent with the line above. |
| `tests/test_lead_signal.py:222/238/252/263` | call `cmd_*` directly, so the `int(... or 0)` layer and both `SystemExit` paths are never exercised. |
| `cli.py:37` `return pipeline.main()` | typed `-> int`, receives `-> None` — a live mypy `return-value` error with no gate to catch it. |

---

## Not a bug, but worth knowing

- **The brief and `README` say "22 keys"; there are 23.** `docs/audits/BRIEF.md:58`
  lists 23 names and `scrapers/base.py:5-28` defines 23. Cosmetic, but it means
  nobody has recently diffed the schema against the brief.
- **`recommend_service` never uses `completeness_score` or `email`.**
  `pitch_recommender.py:48-52` computes `completeness`, reassigns it at line 50
  (`int(completeness) if completeness else 0`) — so linters see a read and stay
  quiet — and it is then never referenced by the Tier 1-7 chain at
  `pitch_recommender.py:57-99`. `has_email` at `pitch_recommender.py:37` is
  genuinely dead and *is* flagged (`ruff check --select F841` → 1 hit). So the
  Tier-3 comment "weak completeness" at `pitch_recommender.py:69` does not match
  the code, which gates on `review_count < 20`. This matters for this lens
  because `cmd_score` recomputes `recommended_service` on every invocation and
  the operator will reasonably believe completeness participates.
- **`cmd_score` correctly leaves `website_live` and `phone` alone** — both require
  network or are already normalised (`main.py:194`). The result is that
  `leadminer score` is a *re-ranking*, not a re-measurement; its output must not
  be described as refreshed data.
- **`cli.py` is not lint-clean** under its own config: `ruff check cli.py` → 8
  errors (2× `PTH123`, 5× `RUF010`, 1× `W292` no trailing newline at
  `cli.py:322`). The repo-wide default also flags `UP035` in
  `scrapers/wikidata.py:2`. No CI step runs ruff, mypy or pytest
  (`scrape.yml` has only checkout/setup/install/rclone/scrape/validate/upload).
- **`cmd_run`'s failure reporting has no machine-readable form at all.** Even
  after the S1 is fixed, the CLI prints human text; `leadminer run --json` does
  not exist, so nothing downstream can assert on results.
- **`cmd_doctor` is the only command that survives a broken environment**
  (`cli.py:226-231` catches `ImportError` for `requests`). `leadminer score` is
  advertised as `(no network)` (`cli.py:304`) yet transitively imports `requests`
  via `main` → `scrapers.osm:4` → `httpclient:43`; without it you get a raw
  `ModuleNotFoundError: No module named 'requests'` traceback instead of the
  actionable message `doctor` gives.
- **`cmd_score`'s `--out` can be a directory**, in which case
  `main.py:124`'s `open(tmp, "w")` raises `IsADirectoryError` uncaught.
- **`cmd_validate`'s duplicate check (`cli.py:190`) counts blank-name/blank-phone
  rows as duplicates.** All such rows collapse to the single key `(None, None)`,
  so `dupes` is inflated by `N-1` and can trip the 5% threshold
  (`cli.py:191`) on a legitimately sparse master. False positives here train
  operators to ignore `leadminer validate`.

---

## Recommended order of work

1. **Make failure observable.** `main.main()` returns a result (rows in/out,
   per-source status); `cmd_run` maps that to an exit code; drop
   `int(... or 0)`. Wire `leadminer run` into `scrape.yml:69` and delete the
   ad-hoc row-count guard once the exit code is trustworthy. (S1 #1, S3 #3)
2. **Stop `leadminer score` from being destructive.** Require `--in-place`;
   default to a sibling file; make `write_csv` report ignored keys. (S1 #2)
3. **Fix `website_live` at the serialisation boundary.** Write
   `LIVE`/`DEAD`/`UNKNOWN` (`enricher.py:154-156`); parse case-insensitively in
   both `main.py:103` and `cli.py:107-110`. (S1 #3)
4. **Recompute `completeness_score` in `cmd_score`** and add `region`/`phone`
   re-normalisation if cheap. (S2 #4)
5. **Add `--data-dir` / `LEADMINER_DATA_DIR`, resolved absolute, and fail closed
   when the master is absent.** (S2 #5)
6. **Make `cmd_validate` count skipped rows as a problem**, not `continue`. (S2 #6)
7. **Add a CI gate** (`ruff check`, `mypy --strict`, `pytest`) — today the
   declared `strict = true` and the declared `-> int` are both unenforced — then
   fix `cli.py:37` and derive `FIELDS` from `BusinessRecord`. (S3 #3, #8, #11)
8. **Add `tests/test_cli_main.py`** covering exit codes for every subcommand, the
   failing-pipeline case, and `KeyboardInterrupt` → 130. (S3 #7)