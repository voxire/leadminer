# 200 — `build_parser` / `cli.py:280` vs. the `BusinessRecord` contract

## Verdict

The premise of the lens is only half right, and the half that is wrong matters:
**`build_parser()` touches zero `BusinessRecord` fields.** It takes no arguments,
reads no records, writes no records — it cannot violate the TypedDict contract at
all. What it *does* produce is an untyped `argparse.Namespace` that is consumed
through a duck-typed protocol (`args.func`, `args.file`, `args.source`, `args.out`)
with no declared type anywhere. Every real contract violation in this area is a
**declared-type violation, not an extra field**: `main.load_master()` is annotated
`-> list[dict]` (`main.py:77`), never `list[BusinessRecord]`, so nothing in the
program enforces `base.py:5-28`, and the CLI is the only tool that writes files
which later crash the pipeline on those unvalidated fields. Concretely verified:
`leadminer run` exits **0** after all three sources fail and writes five empty
CSVs, and `leadminer score` on a 3-column CSV manufactures 20 blank columns that
make the *next* `leadminer run` die with `TypeError` at `main.py:206`.

---

## Scope note — what the lens asked vs. what is there

| Lens question | Answer |
|---|---|
| Input type of `build_parser` | **None.** `def build_parser() -> argparse.ArgumentParser` (`cli.py:280`) — no params, no `argv`, no env. Its only inputs are module globals it closes over: `DATA_DIR` (`cli.py:26`, read at `cli.py:294`, `:298`, `:305`) and the six `cmd_*` function objects (`cli.py:286, :291, :295, :299, :302, :307`). |
| Output type declared vs. actual | Declared `argparse.ArgumentParser` and it is exactly that. The *real* output is the `Namespace` it produces. Measured key sets (verified): `run` → `{cmd, func}`; `scrape --source X` → `{cmd, func, source}`; `stats`/`validate` → `{cmd, file, func}`; `doctor` → `{cmd, func}`; `score` → `{cmd, file, func, out}`. |
| Fields read/written by `build_parser` not in `BusinessRecord` | **None.** Every field touched by the code `build_parser` dispatches is in the TypedDict — verified key-by-key: `cmd_scrape` reads `category`/`phone`/`website` (`cli.py:68-70`); `cmd_score` writes `country`/`industry_priority`/`recommended_service`/`lead_score` (`cli.py:266-272`) and reads `country`,`phone` (via `resolve_country`), `category`, `website`,`website_live`,`email`,`phone`,`instagram`,`facebook`,`rating`,`review_count`,`completeness_score`,`source`. All 23 are declared in `base.py:6-28`. |
| Can a caller violate the contract silently? | Yes, and it already happens — but not through `build_parser`'s arguments. It happens through (a) `set_defaults(func=…)` being invisible to argparse, (b) `load_master` hand-parsing CSV strings into a `dict` that is never checked against the TypedDict, and (c) `cmd_stats`/`cmd_validate` bypassing `load_master` entirely and reading raw `csv.DictReader` strings (`cli.py:87`, `cli.py:143`). |

Field-by-field, the three scraper constructors (`osm.py:98-122`,
`wikidata.py:106-130`, `google_places.py:281-305`) are **fully compliant** with
`BusinessRecord` — all 23 keys, correct scalar types. `cmd_scrape` is the only CLI
command that sees `BusinessRecord` values at all, and it is safe. Every violation
below lives on the *read/write* side of the boundary, not the *scrape* side.

---

## Findings

### S1 — `leadminer run` reports success after every source failed, and overwrites all five exports with header-only CSVs

- **Where:** `cli.py:37` (`return pipeline.main()`), `cli.py:33` (`-> int`),
  `cli.py:315` (`int(args.func(args) or 0)`), `main.py:148` (`def main() -> None`),
  `main.py:167-168` (swallows every scraper exception), `main.py:209-213`.
- **Declared vs. actual:** `cmd_run` is annotated `-> int` (`cli.py:33`) and
  returns `None` in practice, because `main.main()` is annotated `-> None`
  (`main.py:148`) and has no `return` statement. `cli.py:315` masks the mismatch
  with `or 0`, so `int(None or 0) == 0`. Verified: with `pipeline.main` replaced
  by a stub returning `None`, `cmd_run(ns)` returned `None`.
- **Breaks:** `main.py:167-168` catches `Exception` per scraper and continues, so
  zero records is indistinguishable from success. The run then rewrites
  `all_businesses.csv`, `qualified_businesses.csv`, `with_websites.csv`,
  `without_websites.csv` and `sales_ready.csv` with header-only files
  (`main.py:209-213`) and **exits 0**. Whatever consumes the exit code — the
  `workflow_dispatch` job, an rclone upload, a cron — reports green.
- **Trigger (executed offline, scrapers replaced by stubs that raise; no network):**
  ```
  process exit code : 0
  [DeadScraper] ERROR: 403 Forbidden (simulated total source failure)   x3
  Total raw records from scrapers: 0
  Written: .../all_businesses.csv (0 records)      (+4 more)
  all_businesses.csv: 1 line(s) total = 1 header + 0 data rows
  ```
  Accidental protection: if `data/all_businesses.csv` already exists, `main.py:150`
  loads it and `dedup` re-emits it, so the master survives. The empty overwrite
  bites on a **first run**, on a **fresh clone**, or after anyone deletes `data/`.
- **Fix:** make `main.main() -> int` and return non-zero when `len(raw) == 0` or
  when every scraper raised; have `cmd_run` propagate it verbatim, and drop the
  `or 0` coercion at `cli.py:315` so a handler returning `None` cannot masquerade
  as success.

---

### S1 — a blank `completeness_score` anywhere in the master kills the next run with `TypeError`, and `leadminer score` can manufacture one

- **Where:** producer `cli.py:265-275` (`cmd_score`) → `main.py:96-101`
  (`load_master` `_INT_FIELDS` cast) → consumer `main.py:206`.
- **Breaks:** `main.py:206` is
  `qualified = [r for r in records if r.get("completeness_score", 0) >= 1]`.
  The `0` default never fires, because `main.py:89` turns a blank cell into
  `None` — a *present* key. `None >= 1` raises
  `TypeError: '>=' not supported between instances of 'NoneType' and 'int'`,
  after enrichment has already burned the full website-fetch budget. The
  TypedDict declares `completeness_score: int` and `lead_score: int`
  (non-optional, `base.py:25`, `base.py:28`), but `load_master` is annotated
  `-> list[dict]` (`main.py:77`), so the violation is unchecked and silent.
- **Trigger:** a 3-column CSV fed to `leadminer score`:
  ```
  $ printf 'name,phone,website\nCafe,70123456,https://x.com\n' > partial.csv
  $ leadminer score partial.csv
  re-scored 1 records, 0 changed -> partial.csv        # exit 0
  ```
  `write_csv` (`main.py:125`) uses `fieldnames=FIELDS` and `extrasaction="ignore"`,
  so the file becomes **23 columns, 20 of them blank**. Verified output:
  `completeness_score=''`, `rating=''`, `website_live=''`, and
  `recommended_service='Discovery call - scope the right service'`. Re-read via
  `load_master` and fed to the `main.py:206` expression:
  ```
  completeness_score=None
  main.py:206 `r.get('completeness_score', 0) >= 1` -> TypeError: '>=' not supported between instances of 'NoneType' and 'int'
  ```
  So `leadminer score` hands you a file that bricks `leadminer run`. The same
  crash fires for any hand-edited master: open `all_businesses.csv` in Google
  Sheets, clear one `completeness_score` cell, save. `cmd_score` never
  recomputes `completeness_score`, so it cannot repair it.
- **Fix:** in `cmd_score`, recompute `completeness_score` (it is a pure function
  of already-present columns, `enricher.py:101`); and make `main.py:206`
  `int(r.get("completeness_score") or 0) >= 1`.

---

### S2 — `leadminer score` says `0 changed` after rewriting two of the 23 columns

- **Where:** `cli.py:264`, `cli.py:266-272`.
- **Breaks:** the counter is wired only to `lead_score` (`cli.py:270-271`), but
  `cmd_score` also overwrites `country` (`cli.py:266`), `industry_priority`
  (`cli.py:267`) and `recommended_service` (`cli.py:268`). The tool whose entire
  purpose is "re-score existing records with current rules" reports how many
  records it touched using one of the four fields it touched, and prints nothing
  about the other three.
- **Trigger (verified):** input row with `industry_priority='low'`,
  `recommended_service='TOTALLY STALE LABEL'`, `lead_score='40'` (already
  correct):
  ```
  re-scored 1 records, 0 changed -> stale2.csv
  output: industry_priority='high', recommended_service='SEO audit + visibility upgrade'
  ```
  Two columns rewritten in place, reported as no change. On a real master this
  hides exactly the migration the operator ran the command to see.
- **Fix:** count and report per-field, e.g.
  `changed = sum(1 for r in records if r != before[i])`, and print which columns
  moved.

---

### S2 — `leadminer score` silently drops every column outside `FIELDS`, then rewrites the file in place

- **Where:** `cli.py:274-275` → `main.py:125`
  (`csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")`).
- **Breaks:** `load_master` returns whatever columns the CSV header happens to
  have; `write_csv` throws the rest away without a warning, and `cmd_score`
  defaults `--out` to the input path (`cli.py:274`), so the operator's file is
  overwritten atomically and irreversibly. No backup, no `--dry-run`.
- **Trigger (verified):** a master with a `notes` column
  (`notes="called by Sara on 2026-02-01"`):
  ```
  re-scored 1 records, 1 changed -> extra_col.csv     # exit 0
  columns after score: 23  'notes' present? False
  ```
  Sales notes, tags, or any column another tool added are gone, and the tool
  printed nothing about it.
- **Fix:** diff the input header against `FIELDS` and refuse (or require
  `--drop-unknown-columns`) when the input has extras; default `--out` to
  `<name>.rescored.csv` instead of in place.

---

### S2 — all three file defaults are CWD-relative, so the installed console script is broken outside the repo root

- **Where:** `cli.py:26` (`DATA_DIR = pathlib.Path("data")`), baked into the
  defaults at `cli.py:294`, `cli.py:298`, `cli.py:305`; consumed by
  `cli.py:217` (`cmd_doctor`) and `cli.py:219`.
- **Breaks:** `pyproject.toml:33` installs `leadminer = "cli:main"` precisely so
  operators need not be in the repo. But `DATA_DIR` is resolved against
  `os.getcwd()` at import time, so from any other directory `leadminer stats`,
  `leadminer validate` and `leadminer score` all exit 2 with
  `no such file: data/all_businesses.csv` while the real data sits in the repo.
  `leadminer doctor` is worse: it reports `MISSING  data/ (no run has produced
  output yet)` when a run has in fact produced output — the operator's first
  diagnostic actively lies about state.
- **Trigger (verified):**
  ```
  cwd=.../leadminer
  parse_args(['stats']).file = 'data/all_businesses.csv'
  after chdir(/tmp/...): parse_args(['stats']).file = 'data/all_businesses.csv'
  cmd_stats(default) from wrong cwd -> rc=2, stderr='no such file: data/all_businesses.csv'
  cmd_doctor's DATA_DIR.exists() here -> False
  ```
- **Fix:** resolve `DATA_DIR` from an env var with a fallback to the package's
  repo root, not `cwd` — e.g.
  `DATA_DIR = pathlib.Path(os.environ.get("LEADMINER_DATA_DIR", Path(__file__).parent / "data"))`.

---

### S2 — `leadminer stats` loses rows from all three `website_live` buckets unless the cell is exactly `"True"`/`"False"`

- **Where:** `cli.py:107-110`; underlying coercion `main.py:103`.
- **Breaks:** `cmd_stats` bypasses `load_master` and reads raw
  `csv.DictReader` strings (`cli.py:87`), then compares against Python-repr
  literals:
  `== "True"` / `== "False"`. `main.py:103` coerces with the same exact-match
  rule. Any CSV not produced by `main.write_csv` — re-saved from Google Sheets,
  hand-edited, produced by a future export path, or written by `csv` with
  different casing — puts the row in **none** of the buckets: `unknown` at
  `cli.py:109-110` requires the cell to be *empty*, so `"true"` counts as
  neither live, dead, nor unreachable. `README.md:63` documents the column as
  `` `true` if reachable, `false` if dead `` — lowercase, i.e. the documented
  contract and the code disagree on the wire format.
- **Trigger (verified), one row with a website, varying only the cell:**
  ```
  website_live='True'   -> live=1, dead=0
  website_live='true'   -> live=0, dead=0     # row accounted for in no bucket
  website_live='TRUE'   -> live=0, dead=0
  website_live='1'      -> live=0, dead=0
  website_live='False'  -> live=0, dead=1
  ```
  A 100%-live dataset reports `website live : 0`, `website dead : 0`,
  `unreachable : 0` — the headline diagnostic of the tool contradicts itself
  silently, with exit 0.
- **Fix:** parse once through a shared helper
  (`{"true","1","yes"} -> True`, `{"false","0","no"} -> False`, else `None`)
  used by both `main.py:103` and `cli.py:107-108`; and add a fourth bucket,
  `unparseable`, so a value that matches nothing is never invisible.

---

### S2 — `leadminer validate` passes rows whose coordinates it could not read at all

- **Where:** `cli.py:164-173`, specifically `cli.py:167-168`.
- **Breaks:** `float(r["lat"])` inside `except (ValueError, TypeError, KeyError): continue`
  conflates three distinct states — "column absent", "cell unparseable", and
  "value out of the market" — into one silent skip. `cmd_validate` also reads raw
  `csv.DictReader` strings (`cli.py:143`), so it never sees the typed values
  `BusinessRecord` promises. `README.md:14` calls this a "data-quality gate" and
  the docstring says "Exits non-zero if checks fail"; it exits 0 when it could
  not check.
- **Trigger (verified), single-row CSVs:**
  ```
  PASS  no lat/lon columns at all
  PASS  lat/lon present but garbage  (lat="", lon="")
  FAIL  lat/lon out of the market (lat=52.52, lon=13.40 = Berlin)
  PASS  genuinely clean baseline
  ```
  Only the case the check was written for fails. A file where every coordinate
  is corrupt, or where the columns have been renamed to `latitude`/`longitude`,
  is reported as `all checks passed`.
- **Fix:** count unparseable/absent coordinates as their own problem
  (`f"{n} rows have missing or unparseable coordinates"`) rather than `continue`.

---

### S3 — the `Namespace` protocol `build_parser` establishes is entirely undeclared and unchecked

- **Where:** `cli.py:280-309` (producer), `cli.py:315` (consumer),
  `tests/test_lead_signal.py:219`, `:238`, `:263` (existing callers).
- **Point:** `build_parser` takes no arguments, so nothing about *its* signature
  can be violated. Its contract is the side effect "every subparser gets a
  `func` default, plus the attributes that subparser declared", and that contract
  lives only in `set_defaults` calls that argparse knows nothing about. Verified:
  all six subcommands currently populate `func`, and the handlers tolerate any
  missing attribute — which is why `tests/test_lead_signal.py:219` can call
  `cmd_stats(argparse.Namespace(file=p))` with no `cmd` and no `func` and pass.
  The handlers genuinely depend on no invariant `build_parser` establishes.
- **Breaks (latent):** a future subparser added without `.set_defaults(func=…)`
  does not fail at parse time. Verified failure mode:
  ```
  Namespace keys: ['cmd']
  cli.py:315 -> AttributeError: 'Namespace' object has no attribute 'func'
  ```
  No usage message, no exit 2 — an uncaught traceback and exit 1 from the
  installed entry point. Same for a handler reading an attribute its subparser
  never declared (`args.out` before `cli.py:306`, `args.source` before
  `cli.py:289`).
- **Fix:** after building, assert the invariant in one place —
  `for a in p._subparsers._group_actions[0].choices.values(): assert "func" in a.get_default("__dict__")` —
  or type the handlers with per-subcommand `Protocol`/`TypedDict` namespaces and
  a `Callable[[Any], int]` alias instead of bare `argparse.Namespace`.
- Also: `cmd` (`cli.py:283`) is set on every Namespace and read by nothing;
  `cmd_run` (`cli.py:33-37`) and `cmd_doctor` (`cli.py:204-206`) ignore their
  `args` parameter entirely.

---

### S3 — adjacent: `recommended_service` is recomputed by `cmd_score` from an input it never reads

- **Where:** `cli.py:268` → `pitch_recommender.py:48-52` (dead variable) vs.
  `pitch_recommender.py:68-69` (comment claiming it is used).
- **Point:** `recommend_service` parses `completeness_score` into `completeness`
  and then never reads it; the Tier-3 comment advertises "no real reviews, low
  ratings, weak completeness" as the condition, but only review count and rating
  appear in the `if`. `cmd_score` writes the result back to the master
  (`cli.py:268`, `cli.py:275`), so a field the operator can edit has no effect on
  the pitch it supposedly drives.
- **Trigger (verified, correctly-typed record via `load_master`):**
  ```
  completeness 0 -> 'SEO audit + visibility upgrade'
  completeness 7 -> 'SEO audit + visibility upgrade'
  same pitch either way: True
  ```
- **Fix:** either drop lines 48-52 and correct the comment, or add the
  completeness threshold to the Tier-3 condition.

---

## Not a bug, but worth knowing

- **The three scrapers fully honour `BusinessRecord`.** `osm.py:98-122`,
  `wikidata.py:106-130` and `google_places.py:281-305` each pass all 23 keys
  with correct types (OSM lat/lon are JSON floats; `wikidata.py:54-62` parses
  `Point(lon lat)` into `tuple[float, float]`; `google_places.py:281` passes the
  API's `rating` float and `userRatingCount` int). `cmd_scrape`
  (`cli.py:61-76`) is the only CLI consumer of typed records and it only reads
  `category`/`phone`/`website` via `.get`, which is safe against a missing key.
- **`BusinessRecord` is 23 keys, not 22.** `BRIEF.md:18` and the brief's column
  list say 22; `base.py:6-28` declares 23 and they match `main.FIELDS`
  (`main.py:33-40`) exactly — verified `set(BusinessRecord.__annotations__) ==
  set(FIELDS)`, no key on either side. Whichever document is canonical, one of
  them is wrong.
- **`BusinessRecord` enforces nothing at runtime**, and only the scrapers use it.
  `grep` confirms the imports are `osm.py:5`, `wikidata.py:5`,
  `google_places.py:28`, `base.py:33` — nothing in `main.py`, `dedup.py`,
  `enricher.py`, `cli.py` or `tests/` ever references it. `main.load_master` is
  annotated `-> list[dict]` (`main.py:77`) and every consumer takes `dict`, so
  the TypedDict documents an aspiration rather than a boundary. This overlaps
  with the S1 in `docs/audits/045-scraper-layer-refactor.md`; the CLI-specific
  consequence is the two S1s above, since `cli.py` is the only writer of files
  whose shape the pipeline then trusts.
- **`cli.py:56-59` is unreachable.** `choices=["osm","wikidata","google_places"]`
  at `cli.py:290` already rejects anything else — verified:
  `parse_args(["scrape","--source","bogus"])` → `SystemExit(2)`. The registry at
  `cli.py:51-55` and the `choices` list are two copies of the same truth; adding
  a scraper to one and not the other makes the other silently wrong. Derive
  `choices=sorted(registry)` from the registry (keys are strings) or delete the
  guard.
- **`cmd_score` does not re-normalize phones** the way `main.py:194` does. In
  practice the master already holds normalized numbers so this is a no-op, but
  running it on a raw export would leave national-format numbers in place while
  `lead_score` (`enricher.py:285`) still scored them.
- **`main(argv)` (`cli.py:312-318`) catches only `KeyboardInterrupt`.** Every
  handler failure — including the `TypeError` chain above — escapes as a raw
  traceback rather than a diagnostic line and a chosen exit code. `cmd_stats`,
  `cmd_validate`, `cmd_score` and `cmd_doctor` all handle their own expected
  errors well (`cli.py:83`, `cli.py:139`, `cli.py:256`); it is only the
  unanticipated shapes that are unhandled.
- `cmd_scrape` materialises the whole iterator with `list(...)` at `cli.py:61`
  and has no `--limit`, so `leadminer scrape --source google_places` pays the
  full 68-query bill to print 12 lines. Fine for a probe, but it is the one
  command where an operator is most likely to fat-finger it.

---

## Recommended order of work

1. **S1 exit code.** `main.main() -> int`, return non-zero on zero records /
   total scraper failure, propagate through `cmd_run`, drop the `or 0` mask at
   `cli.py:315`. Smallest diff in the report, largest effect on CI trust.
2. **S1 `completeness_score`.** Recompute it in `cmd_score` and harden
   `main.py:206` with `int(r.get(...) or 0)`. Two lines, removes a crash class
   and closes the loop where the CLI manufactures the crash.
3. **S2 `cmd_score` honesty** (`0 changed` lie, silent column drop, in-place
   default). Bundle these: count per-field diffs, refuse unknown columns,
   default `--out` to a new file.
4. **S2 `DATA_DIR`** resolution. One line, fixes `stats`/`validate`/`score`/
   `doctor` for every installed user outside the repo root.
5. **S2 `website_live` literal parsing**, shared by `main.py:103` and
   `cli.py:107-108`, plus an `unparseable` bucket. Fix the README to match.
6. **S2 `cmd_validate`** counting unparseable/absent coordinates.
7. **S3 typed Namespace protocol** + the `build_parser` invariant assertion, then
   the `choices`/registry duplication, then the dead `completeness` variable in
   `pitch_recommender`.

*Verification method: all findings reproduced offline with `requests`/`urllib3`
stubbed and `main.DATA_DIR` redirected to a temp directory. No scraper was run
and no network call was made. `cmd_scrape` and the `cmd_doctor` connectivity
loop were not executed.*
