# 200 — `cli.build_parser` performance profile (lens: perf)

## Verdict

**`build_parser()` is O(1) in the number of businesses and allocates a constant
~4.6 KB. It is not a performance finding, and no change to it can produce a
meaningful speedup.** It takes no arguments, reads no file, opens no socket,
and touches nothing that grows with the dataset; it is called exactly once per
process at `cli.py:313`. Measured end-to-end (`build_parser()` + `parse_args()`)
it is a flat **0.7 ms whether the master CSV holds 10,000 rows or 500,000**.

The dataset-proportional cost the lens is looking for is real, but it lives
**~800 lines below the target**, at `cli.py:87` and `cli.py:143`:
`rows = list(csv.DictReader(f))` materializes the entire CSV as a list of dicts
before a single statistic is computed. At 500k rows that is **893 MB peak RSS**
and **14.3 s**, and `cmd_stats` then re-walks that list **13 separate times**.
The single change with the largest speedup is to stream the CSV instead of
materializing it: **2.13× faster and 59× less memory** (measured below). Nothing
about that change touches `build_parser`.

## Complexity determination

### `build_parser()` — O(1), constant allocation

- **Signature:** `def build_parser() -> argparse.ArgumentParser:` (`cli.py:280`) —
  zero parameters. Nothing about `n` can reach it.
- **Call sites:** exactly one — `args = build_parser().parse_args(argv)`
  (`cli.py:313`). A repo-wide grep for `build_parser` returns only the
  definition and that single call; there is no per-record, per-row, or per-file
  invocation anywhere, so it cannot be amortized into a loop by accident.
- **Inputs are all literals.** Every argument is a string constant or
  `str(DATA_DIR / "all_businesses.csv")` (`cli.py:294`, `cli.py:298`,
  `cli.py:305`), where `DATA_DIR = pathlib.Path("data")` (`cli.py:26`) — a
  `PurePath` join of two constants, no `glob`, no `stat`, no I/O.
- **Output size is fixed.** 2 actions on the root parser, 6 subparsers
  (`doctor`, `run`, `score`, `scrape`, `stats`, `validate`), 11 argparse
  `Action` objects total. Fixed at import-time authoring, independent of `n`.
- **`description=__doc__` (`cli.py:281`) is a constant**, not a file read:
  `len(cli.__doc__) == 704` characters, fixed at module import. It does not
  grow with the dataset.
- **No import side effects proportional to data.** `cli.py`'s module-level
  imports are stdlib-only (`cli.py:20-24`: `argparse`, `collections`, `csv`,
  `pathlib`, `sys`). `main`, the scrapers, and `requests` are all imported
  lazily *inside* the `cmd_*` bodies (`cli.py:35`, `cli.py:249-252`), so
  building the parser never touches the pipeline. This is verified incidentally:
  on a machine with no `requests` installed, `import cli` + `build_parser()`
  succeeds, while `import main` fails at `httpclient.py:43`. That is good
  design, not an accident.

Measured allocation per call via `tracemalloc`, 100 iterations:
**2,892 B live / 4,612 B peak — constant**, and identical whether 0 or 500k rows
are loaded.

### The dataset-proportional code, for contrast — O(n), *not* O(n²)

- **`cmd_stats`** (`cli.py:79-132`): one `list(csv.DictReader(f))` at
  `cli.py:87`, then 13 full passes over `rows` — `cli.py:96` (via `count()`
  called at `cli.py:98-103`), `cli.py:107`, `cli.py:108`, `cli.py:109`,
  `cli.py:116`, `cli.py:120`, `cli.py:124`, `cli.py:129`. Linear, with a large
  constant. Each `Counter(...)` is O(n).
- **`cmd_validate`** (`cli.py:135-201`): same full materialization at
  `cli.py:143`, plus `cli.py:151`, `154`, `165-171`, `176-186` (linear) and the
  set comprehension `{(r.get("name"), r.get("phone")) for r in rows}` at
  `cli.py:190` — O(n) expected, but it allocates a **second** full set of 500k
  2-tuples on top of the 500k dicts. Still linear, not quadratic.
- **`dedup`** (`dedup.py:197-232`) — the stage that *could* have been O(n²) and
  is not: it builds hash indexes `phone_index` / `name_index`
  (`dedup.py:198-199`) and does O(1) dict lookups per record
  (`dedup.py:207`, `dedup.py:215`). The post-pass at `dedup.py:226-230` is a
  single linear scan with O(1) set membership. **No quadratic behaviour found
  anywhere in the pipeline.**

**I found no O(n²) in this codebase's hot paths.** If your performance backlog
is hunting quadratic blowups, `dedup.py` and `cli.py` are not where they are.

## Concrete timings

Machine: macOS 27.0.1, 12-core, 25 GB RAM, **CPython 3.14.8** (the project
targets 3.12 per `pyproject.toml:10`; 3.12 was not available on this machine).
Absolute numbers are 3.14-on-this-box; the *ratios* and the flatness are the
portable result.

**Input:** synthetic 22-column CSVs using the exact `FIELDS` list from
`main.py:33-40`, `utf-8-sig`, mixed blanks (`website` blank on 1 of 3 rows,
`email` blank on 1 of 2, `instagram` blank on 3 of 4) so the counters exercise
both branches. Written to `$TMPDIR`; **the repo was not written to.**

| n | CSV size | `list(DictReader)` | peak RSS | `cmd_stats` | `cmd_validate` | **`build_parser()` + `parse_args`** | `cmd_stats` µs/row |
|---|---|---|---|---|---|---|---|
| 10,000 | 2.0 MB | 0.06 s | 34 MB | 0.15 s | 0.12 s | **0.7 ms** | 15.00 |
| 100,000 | 20.5 MB | 0.93 s | 193 MB | 1.90 s | 1.66 s | **0.7 ms** | 19.00 |
| 500,000 | 103.8 MB | 6.40 s | 898 MB | 10.09 s | 8.44 s | **0.7 ms** | 20.18 |

**The `build_parser` column is the answer to the lens question.** 0.7 ms at
10k, 0.7 ms at 100k, 0.7 ms at 500k — one decimal place of agreement across a
50× increase in data. Warm-call microbenchmarks agree: median **753.2 µs /
753.0 µs / 1021.7 µs** for 10k / 100k / 500k, and when interleaved to expose
drift rather than a trend (`min / median / max` per size): 10k
`963.5/1044.6/1757.0`, 100k `966.1/1080.3/1263.8`, 500k
`946.2/1028.9/1336.5` µs. Overlapping distributions, no monotone term.

**Share of wall clock:** at 500k rows, `build_parser()` + `parse_args` is
**0.0007 s out of `cmd_stats`' 10.09 s = 0.0069 %**. Even if you made
`build_parser` infinitely fast you would save 0.7 ms.

### Measurement-honesty note

A first pass, run inside one long-lived process that had already loaded several
dataset sizes, showed `cmd_stats` at 13.57 s and `cmd_validate` at 23.54 s for
500k, which looks superlinear. **That was an artifact.** Re-measured with one
fresh subprocess per size (`ru_maxrss` is monotonic within a process, and
repeated 500k-row loads leave allocator/GC pressure behind), the per-row cost is
15.0 → 19.0 → 20.2 µs, i.e. **linear** — ×10.08 for ×10 data, ×5.31 for ×5
data. Any future perf audit of this repo should use fresh processes per size;
the numbers in this section are the clean ones.

## Findings

### S2 — `cmd_stats`/`cmd_validate` materialize the entire CSV, then walk it 13 times
- **Where:** `cli.py:87` (`cmd_stats`), `cli.py:143` (`cmd_validate`), and the
  13 redundant passes at `cli.py:96,107,108,109,116,120,124,129`.
- **Breaks:** both commands build a complete `list` of ~500k 22-key dicts just
  to compute aggregates that are all order-independent. At 500k rows that is
  **893 MB peak RSS** and **14.31 s** for `leadminer stats`. The memory is the
  real hazard: `all_businesses.csv` is the cumulative all-time master (README /
  `main.py:8`), so this grows every run, and the GitHub Actions runner in
  `.github/workflows/scrape.yml` has `timeout-minutes: 300` on a single-digit-GB
  VM. This is the CLI-side instance of the pipeline-wide problem already written
  up in `052-memory-streaming.md` — but the CLI is the *easy* half of it,
  because unlike `dedup`, `cmd_stats` only aggregates and is trivially a single
  forward pass.
- **Trigger:** `leadminer stats data/all_businesses.csv` with 500,000 rows
  (a 103.8 MB CSV). Measured: 14.31 s wall, 893 MB peak RSS.
- **Fix:** replace the `list(...)` + 13 passes with one streaming pass over
  `csv.DictReader` accumulating into counters — measured below at 1.62× faster
  and **59.5× less RAM**; using `csv.reader` with a column-index map instead of
  `DictReader` gives 2.13× and the same memory.

Measured at 500k rows / 103.8 MB, **one isolated process per variant** (peak RSS
is monotonic per process, so variants cannot share one):

| variant | time | peak RSS | vs shipped |
|---|---|---|---|
| `cmd_stats` as shipped — `list` of dicts, 13 passes | 14.31 s | 893 MB | 1.00× / 1.00× |
| fused streaming `csv.DictReader`, 1 pass | 8.84 s | 15 MB | **1.62× time / 59.5× RAM** |
| fused streaming `csv.reader` + column index, 1 pass | 6.72 s | 15 MB | **2.13× time / 59.5× RAM** |

**This is the single change with the largest speedup** — and it is the answer to
the lens question, just not the one the lens pointed at. `build_parser` offers
none: it is already O(1) and costs 0.7 ms.

### S3 — `import argparse` costs ~36 ms of one-time startup, and it is not actionable
- **Where:** `cli.py:20`; the lazy `shutil` import lives inside CPython's
  `argparse.HelpFormatter` (`argparse` imports `shutil` for terminal width).
- **Breaks:** the *first* `ArgumentParser()` construction in a process costs
  **36.12 ms**; the second costs **0.21 ms**. `-X importtime` attributes this to
  `argparse → shutil → {re, fnmatch, lzma, zstd, bz2}`. So a bare
  `leadminer stats --help` pays ~36 ms of import tax before doing any work.
- **Trigger:** any invocation — `leadminer --help` on a 2 s budget.
- **Fix:** none worth taking. Dropping `argparse` to save 36 ms is a bad trade
  for the ergonomics it buys. **Listed for completeness so nobody later
  "optimizes" `build_parser` chasing this number** — the 36 ms is in `argparse`'s
  import graph, not in `cli.py:280`.

  Measured decomposition of the warm ~700 µs, for the record: one bare
  `ArgumentParser()` 77.2 µs (11 %), bare + `add_subparsers` 236.6 µs (35 %),
  the remaining ~1.17 ms is the 6 `add_parser` + 5 `add_argument` calls
  (`cli.py:285-307`). A constant ~1.2 ms of parser plumbing for 6 subcommands is
  the right price.

## Not a bug, but worth knowing

- **`build_parser()` is rebuilt on every `main()` call** (`cli.py:313`). Under
  pytest, a suite that calls `main([...])` in a loop pays 0.7 ms per call. That
  is noise; caching it with `functools.lru_cache` would only matter above ~1400
  calls. **Do not "fix" this** — the other `build_parser` lenses (contract,
  correctness, edge, ops, tests, concur) have that file for it, and I am not
  duplicating their ground.
- **`argparse` is fast at parsing but `parse_args` re-validates `choices`**
  (`cli.py:290`) linearly over 3 items. Constant. Irrelevant.
- **`DATA_DIR = pathlib.Path("data")` is relative** (`cli.py:26`), so the
  no-argument defaults at `cli.py:294`, `cli.py:298`, `cli.py:305` only resolve
  when the CWD is the repo root. `leadminer stats` from `/tmp` silently looks
  for `/tmp/data/all_businesses.csv` and exits 2 with `no such file`
  (`cli.py:83-84`). That is an operability issue rather than a perf one, and it
  belongs to the `ops` lens — but it is the first thing anyone will hit when
  trying to reproduce these timings on the 500k dataset.
- **The whole CLI is stdlib-only at import time** (`cli.py:20-24`), which is why
  `stats`, `validate`, and `doctor` work on a box with no `requests` and no
  network. Worth preserving if anyone is tempted to hoist an import.

## Recommended order of work

1. **Stream `cmd_stats`** — replace `cli.py:87` + the 13 passes with one
   `csv.reader` pass over a column-index map. 2.13× faster, 893 MB → 15 MB at
   500k rows. Cheapest high-value change in this file.
2. **Stream `cmd_validate`** — same pattern at `cli.py:143`, and it lets
   `cli.py:190` keep a single rolling `set` of `(name, phone)` rather than a
   second full copy of the dataset as tuples.
3. **Nothing for `build_parser`.** It is O(1), 0.7 ms, 4.6 KB allocated. Record
   it as measured-and-fine and close the perf lens on `cli.py:280`.

## Method / reproducibility

Benchmarks live in `$TMPDIR/lmbench/` (`bench.py` – `bench4.py`), outside the
repo; `git status --porcelain` shows no modified tracked files. Datasets are
generated locally from the `FIELDS` list at `main.py:33-40`. No network calls
were made, no scrapers were run, and no packages were installed — note that
`main.py` could not be imported at all on this machine because
`httpclient.py:43` requires `requests`, which is why `FIELDS` was inlined
verbatim in the harness and why `cmd_score` was not benchmarked here.