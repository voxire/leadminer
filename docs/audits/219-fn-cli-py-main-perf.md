# 200 — `cli.main` cost profile at 500k records

**Target:** `cli.py:312` `main`
**Lens:** performance / scaling / allocation. Is it O(1), O(n) or O(n²) in the number
of businesses? Does it allocate proportionally? Concrete timings at 10k / 100k / 500k.
What single change buys the most?

## Verdict

`main()` itself is **O(1) time and O(1) memory** — it is 5 lines with no loop, no
collection, and no dependence on `n`. It is also **not the function that scales**, and it
is **not on the production path at all**: the only automated caller runs
`python main.py` (`scrape.yml:69`), bypassing `pyproject.toml:33`'s `leadminer = "cli:main"`
entirely. The function that actually scales is the one `cmd_run` wraps at `cli.py:37` —
`main.main()` (`main.py:148`) — and the answer there is **O(n) per run in HTTP requests
where `n` is the cumulative master, which makes the archive's lifetime cost Θ(N²/m)**:
that is the only genuinely quadratic behaviour in the system. **Measured: 968 s of pure
network latency for the 500k enrichment pass versus ~41 s for every CPU stage combined.**
The single change with the largest speedup is to stop re-fetching websites that were
already fetched last week.

No S1. Nothing here produces wrong data or kills the run at 500k. The findings are S2
operability/cost problems plus S3 hygiene.

---

## Complexity answer

### `main()` is O(1). Flat. Measured, not assumed.

```python
def main(argv: list[str] | None = None) -> int:      # cli.py:312
    args = build_parser().parse_args(argv)            # cli.py:313
    try:
        return int(args.func(args) or 0)              # cli.py:315
    except KeyboardInterrupt:                         # cli.py:316
        print("\ninterrupted", file=sys.stderr)       # cli.py:317
        return 130                                    # cli.py:318
```

Three operations: one `build_parser()` (`cli.py:280-309`, builds 6 fixed subparsers —
constant), one `parse_args` over `argv` (constant, `argv` does not grow with `n`), one
attribute dispatch. No comprehension, no accumulator, no container that a caller can grow.
`build_parser()` cost is independent of dataset size.

Whole-process startup including the full import graph (`main` → `scrapers.*` →
`httpclient` → `requests`): **0.150 s at Python 3.14.8, 22 MB RSS.** Identical at every
`n` — it does not read a file.

| n | `main()` + imports | RSS |
|---|---|---|
| 10,000 | 0.150 s | 22.0 MB |
| 100,000 | 0.150 s | 22.0 MB |
| 500,000 | 0.153 s | 22.2 MB |

**It does not allocate proportionally. It allocates 22 MB, period.**

### Everything it dispatches to is O(n). There is no O(n²) in the data path.

| stage | where | complexity in n | allocation |
|---|---|---|---|
| `main()` | `cli.py:312` | **O(1)** | O(1) |
| `build_parser()` | `cli.py:280` | O(1) | O(1), 6 subparsers |
| `cmd_stats` | `cli.py:79` | O(n) × **13 walks** | O(n), 1.74 KB/rec |
| `cmd_validate` | `cli.py:135` | O(n) × ~5 walks | O(n), 1.74 KB/rec |
| `cmd_score` | `cli.py:247` | O(n) | O(n) — see `200-fn-cli-py-cmd_score-perf.md` |
| `cmd_run` → `main.main()` | `cli.py:37` → `main.py:148` | O(n) | O(n) × **3 live generations** |
| `dedup` | `dedup.py:197` | O(n + m·F), m = merges ≤ n, F = 23 fields | O(n) |
| `enrich` → `infer_region` | `enricher.py:79` | O(n), ≤8 regex scans/rec | O(1)/rec |
| **`check_websites`** | `enricher.py:225` | **O(n) HTTP per run, n = cumulative master** | **O(n) Futures, all up front** |
| `write_csv` ×5 | `main.py:108` | O(n) | O(1) |

I hunted specifically for the usual superlinear suspects on the whole `cmd_run` path —
`in`-list membership over a growing collection, `list.index` / `list.remove` in a loop,
`+=` string accumulation, nested comprehensions over prior rows, repeated
`collections.Counter` rebuilds, `dedup`'s `set(a) | set(b)` inner loop. **None present.**
`dedup`'s `_merge` (`dedup.py:190`) is the only place with a per-key inner loop, and it
runs once per *merge*, and total merges ≤ n, so it is O(n·F) with F = 23 constant.

**Answer to the question as posed: `main()` is O(1). The pipeline behind it is O(n). The
quadratic is not in any single run — it is in the sum over runs, and that is the finding.**

### The cumulative-master quadratic

`all_businesses.csv` is append-only by design (`BRIEF.md:51`, `main.py:8`). Each run
loads **all of it** (`main.py:150`), merges the new batch in (`main.py:179`), and calls
`enrich(records)` on **the union** (`main.py:187`). `enrich` calls `check_websites`
unconditionally (`enricher.py:337`), and `check_websites` builds its target list from
**every record that has a `website` column value, with no regard for when it was last
checked** (`enricher.py:231`):

```python
targets = [(i, r["website"]) for i, r in enumerate(records) if r.get("website")]
```

There is no `website_checked_at` column to filter on — `FIELDS` (`main.py:33-40`) has 23
entries and none of them records when the liveness verdict was obtained. So run *R* of *R*
runs performs `w · m · R` HTTP GETs, and the archive's lifetime total is:

```
total GETs over R runs = w · m · R(R+1)/2
necessary (fetch each URL once, ever) = w · N       where N = m·R
ratio = (R+1)/2
```

**The system does (R+1)/2 times more website fetching than it needs to.** At R = 10 runs
to reach 500k that is 5.5×; at R = 50 weekly runs (`.github/workflows/scrape.yml:9`) it is
**25.5×**.

---

## Concrete timings

### Network: `check_websites` — the dominant cost by 20–90×

Fixture: synthetic master with the real 23-column schema (`main.py:33-40`), realistic
sparse distribution, 30% of records carrying a website. `_fetch_website` stubbed to a
`time.sleep()` so the measurement is latency-dominated and therefore **independent of
machine load** — this is the one table in this report I trust absolutely, because it is
measuring sleep, not CPU. Model: `T = (n · w · [(1−u)·L + u·8]) / 40`, with `8` s from
`enricher.py:175`'s `timeout=8` and 40 from `enricher.py:225`'s default.

`u = 0` (no host times out), `L = 250 ms`:

| n | targets (`w`=0.30) | predicted | **measured** | pool efficiency |
|---|---|---|---|---|
| 10,000 | 3,003 | 18.8 s | — | — |
| 100,000 | 30,225 | 188.9 s | **192.21 s** | 98% |
| 500,000 | 150,358 | 939.7 s | **968.57 s** | 97% |

`u = 0.10` (10% of sites unreachable, hitting the 8 s timeout — a normal rate for
small-business hosts in this market):

| n | targets | predicted | share of `timeout-minutes: 300` |
|---|---|---|---|
| 10,000 | 3,003 | 76.9 s | 0.4% |
| 100,000 | 30,225 | 774.3 s | 4.3% |
| 500,000 | 150,358 | **3,858 s = 64.3 min** | **21%** |

Sensitivity at 500k, `u`=0.10, `L`=250 ms — the 30% site rate is *my* assumption; read
the real one off the row count of `without_websites.csv`:

| site rate | targets | predicted |
|---|---|---|
| 20% | 100,000 | 42.8 min |
| 30% | 150,000 | 64.3 min |
| 40% | 200,000 | 85.7 min |

Coordination cost alone (network stubbed to return instantly) — i.e. the pure overhead of
submitting one Future per website:

| n | targets | wall | RSS added |
|---|---|---|---|
| 100,000 | 30,225 | 1.29 s | +73 MB |
| 500,000 | 150,358 | **7.56 s** | **+317 MB** |

### CPU: every stage of `cmd_run` at 500k, marginal over `load_master`

Measured in a fresh subprocess per cell, best-of-3, quiet box. Absolute seconds drift
±60% with machine load (see *Method*) — **use the ratios and the µs/record column.**

| stage | marginal @10k | marginal @100k | marginal @500k | µs/rec @100k → @500k | exponent |
|---|---|---|---|---|---|
| `load_master` (`main.py:77`) | 0.109 s | 1.84 s | 9.99 s | 18.4 → 20.0 | 1.02 |
| `dedup` copy path (`dedup.py:197`) | 0.056 s | 0.94 s | 9.49 s | 9.4 → 19.0 | 1.43 |
| score/tag loop (`main.py:189-197`) | 0.146 s | 0.89 s | 5.01 s | 8.9 → 10.0 | 1.11 |
| 5 subset comprehensions (`main.py:199-206`) | 0.116 s | 0.75 s | 3.38 s | 7.5 → 6.8 | 0.92 |
| `write_csv` ×1 (`main.py:108`) | 0.183 s | 1.61 s | 8.67 s | 16.1 → 17.3 | 1.05 |
| `cmd_stats` counting body (`cli.py:98-131`) | 0.062 s | 0.35 s | 2.11 s | 3.5 → 4.2 | 0.93 |

Per-record unit costs, measured directly: `normalize_phone` 1.8–2.7 µs, `normalize_name`
5.8–7.5 µs/name, `dict(record)` copy 0.6–0.8 µs, one `_merge` of two 23-field records
**83–103 µs**.

**Total CPU for a 500k `run`, network excluded: ≈ 41 s** (load 10.0 + dedup 9.5 + score 5.0
+ subsets 3.4 + five writes ≈ 14, one write measured at 8.7 s and the subsets sum to
≈1.6n). Against **968 s** of measured network latency — or **3,858 s** once 10% of hosts
time out.

> **This is the whole finding in one line: at 500k records, network is 20–90× the CPU.
> Every CPU optimisation available — streaming the CSV, fusing `cmd_stats`' walks,
> dropping the `re.sub` — is worth single-digit seconds against a 64-minute problem.
> Optimise the CPU and you will change the runtime by 2%.**

`dedup` is the only stage with exponent > 1.1, and it is **not** algorithmic: I chased it
and it is my fixture's birthday collisions, not the code. Repeating `dedup` in one process
gives a warm/cold ratio of 1.16× / 0.81× / 1.00× at 10k / 100k / 500k, so it is not
page-fault or cold-heap bound; `cProfile` shows the growth is `_pick` calls
(363,952 at 500k, ~0 at 100k), i.e. duplicate phone keys. My generator draws phones from a
9M-value space, so collisions grow as n²/2·9M — a fixture artifact. Real phone numbers have
far more entropy. With collisions removed `dedup`'s copy path is a flat ~1.4 µs/record.
**Do not "fix" `dedup` for this.**

---

## Findings

### S2 — `check_websites` re-fetches the entire cumulative master every run, making lifetime cost Θ(N²/m)

- **Where:** `enricher.py:231` (`targets = [(i, r["website"]) for i, r in enumerate(records) if r.get("website")]`), reached unconditionally from `enricher.py:337` ← `main.py:187`, with `records` = the whole master loaded at `main.py:150`.
- **Breaks:** the liveness verdict for a URL fetched in week 1 is discarded and
  re-derived in week 2, for the entire archive, every run. There is no
  `website_checked_at` column in `FIELDS` (`main.py:33-40`) and no TTL, so nothing can
  distinguish "checked 6 days ago" from "never checked". At 500k that is
  **150,358 GETs per run** — measured at **968 s** of pure latency, or **3,858 s (64 min)**
  once 10% of hosts hit the 8 s timeout. That is **21% of the entire
  `timeout-minutes: 300` budget** (`scrape.yml:25`) spent re-confirming last month's
  findings. It is also **not free politeness**: `enricher.py:250` *overwrites*
  `website_live` on all 500k rows every run, so a transient DNS blip on a host we already
  classified flips a `True` to `None`, which drops `recommended_service` off
  `"Website rebuild + maintenance"` (`pitch_recommender.py:57-58`) — the pitch the
  codebase itself calls "the strongest pitch in the database". Every run churns leads that
  were already correctly scored.
  Across the archive's life this is **(R+1)/2× the necessary network work**: 5.5× at 10
  runs, **25.5× at 50 weekly runs**. At m = 10,000 new records/run that is 3,825,000
  lifetime GETs where 150,000 would do — **≈27 hours of pure HTTP**, versus 0.9 hours.
- **Trigger:** `data/all_businesses.csv` at 500,000 rows, 30% carrying a `website`, run
  under `.github/workflows/scrape.yml`. `main.py:187` submits 150,358 tasks. Time the
  `[Enricher]` block: 968 s with every host answering in 250 ms, 3,858 s with 10%
  unreachable. Nothing in the output says "150,358 of these were already checked last
  week".
- **Fix:** add `website_checked_at` to `FIELDS` and filter `enricher.py:231` to
  `if r.get("website") and _stale(r)`, where `_stale` is
  `not r.get("website_checked_at") or older_than(r["website_checked_at"], TTL)`, and set
  the column at `enricher.py:250`.

**The TTL must be materially longer than the run interval, or this fix buys nothing.**
The cron is weekly (`scrape.yml:9`); a 14-day TTL re-fetches every URL every single run and
is a no-op. Use **90 days**, which makes each URL re-checked roughly once per 13 runs:

| | lifetime GETs to reach 500k (m=10k, R=50) | reduction |
|---|---|---|
| today | 3,825,000 | — |
| 90-day TTL | ≈ 165,000 | **23×** |

The two columns genuinely needing per-run freshness (`rating`, `review_count`) come from
Google Places, not from the site fetch — nothing about `website_live` or the extracted
contacts changes week to week at the resolution we act on. If you want freshness anyway,
pair the TTL with conditional requests (`If-None-Match` from a stored `ETag`), which turns
the re-check into a 1-RTT 304 instead of a full body download (`enricher.py:188` caps the
body at `_MAX_BODY_BYTES`, so bodies are the expensive part).

**Severity defence: S2, not S1.** At 500k the run still completes inside
`timeout-minutes: 300` — 64 min of enrichment on a 300-min budget with a fast scrape phase.
It does not produce wrong data on its own and it does not kill the run today. It is S2
because it consumes a fifth of the budget on redundant work and gets monotonically worse:
at ~1.5M records the same formula exceeds 300 min on its own and the run starts dying.

### S2 — all 150k Futures are materialised before the first byte returns

- **Where:** `enricher.py:237-238`
  (`futures = {pool.submit(_fetch_website, url): idx for idx, url in targets}`).
- **Breaks:** the dict comprehension submits every task up front, so before the pool can
  drain anything the process holds one `Future`, one `_WorkItem` and one bound-args tuple
  per target. Measured with the network stubbed to return instantly: **7.56 s and
  +317 MB of pure bookkeeping for 150,358 targets** (~2.2 KB and ~50 µs per website). This
  is the one place the pipeline's footprint grows *on top of* the 500k-record master
  already resident (`main.py:150`), so it is what pushes peak RSS past 1.2 GB.
  `as_completed` (`enricher.py:240`) does not help — by then the whole dict already
  exists. `executor.map` over a generator would not help either, for the same reason.
- **Trigger:** 500k master, 30% site rate, 150,358 targets.
- **Fix:** bound the in-flight set — keep at most `workers * 4` futures, and `submit` a
  replacement inside the completion loop:
  ```python
  it = iter(targets); pending = set()
  for _ in range(workers * 4):
      idx, url = next(it); pending.add(pool.submit(_fetch_website, url))
  while pending:
      for fut in as_completed(pending):
          pending.discard(fut)
          idx = targets[fut][0]           # keep the index alongside the future
          … handle result …
          try: nidx, nurl = next(it)
          except StopIteration: break
          pending.add(pool.submit(_fetch_website, nurl))
          break                            # re-arm as_completed
  ```
  Memory becomes O(workers) instead of O(n) and the 7.56 s of coordination largely
  disappears. The progress print at `enricher.py:261` still works unchanged.

**Severity defence: S2.** It is linear, not quadratic, and the whole pipeline peaks well
under a 7 GB runner. It is S2 because it is O(n) memory that buys nothing and is pure
overhead on the critical path.

### S2 — `leadminer run` exits 0 when every scraper failed

- **Where:** `cli.py:37` (`return pipeline.main()`) → `main.py:162-168` →
  `cli.py:315` (`return int(args.func(args) or 0)`).
- **Breaks:** `main.py:167-168` catches `Exception` per scraper and prints to stderr.
  There is no failure counter. If OSM, Wikidata and Places all fail — expired
  `GOOGLE_PLACES_API_KEY`, Overpass rate-limiting, a network egress block — then `raw` is
  empty, the pipeline proceeds with the master alone, **rewrites all five CSVs from stale
  master rows** (`main.py:209-213`), prints a confident summary
  (`main.py:219-226`), and returns `None`. `cli.main` turns that into `0`. The command
  reports success.
  This gets worse at scale for a specific reason: at 500k the run now spends 64 min in
  enrichment *after* the scrape, so the operator has a 64-minute window in which a failed
  scrape is invisible, and the one check that exists downstream
  (`scrape.yml:78-83`: `ROWS < 100`) passes comfortably because the cumulative master
  still has 500k rows.
- **Trigger:** unset `GOOGLE_PLACES_API_KEY` and point the Overpass/Wikidata URLs at an
  unreachable host. `leadminer run` prints three `ERROR:` lines to stderr, then
  `Total unique businesses : 500000`, then exits **0**.
- **Fix:** count failures in `main.py:162-168` and return non-zero from `main.main()` when
  every scraper failed; `cli.cmd_run` should propagate that instead of discarding it, and
  `cli.py:315`'s `int(... or 0)` should not be what decides the exit code.

**Severity defence: S2, not S1.** No data is lost — `write_csv`'s atomic
tmp-then-rename (`main.py:122-130`) means the master survives intact, and the five exports
are rewritten from it faithfully. It is S2 because it is an operability failure that makes
a broken run indistinguishable from a good one, and the cost of noticing grows with `n`.

### S2 — the only automated caller bypasses `main()` entirely

- **Where:** `.github/workflows/scrape.yml:69` (`run: python main.py`) versus
  `pyproject.toml:33` (`leadminer = "cli:main"`).
- **Breaks:** two entrypoints, one canonical. `cmd_run` (`cli.py:33-37`) is described in
  its own docstring as a "thin wrapper … so there is one code path", but CI calls
  `main.main()` directly, so that claim is false in production. The consequence for this
  lens is concrete: the workflow's "Validate output before publishing" step
  (`scrape.yml:71-83`) hand-rolls a **weaker** version of `cmd_validate` — existence plus
  a row count — instead of calling it. Every check the CLI already implements is skipped
  in CI: blank names (`cli.py:151`), UTF-8 BOM contamination (`cli.py:154`), coordinates
  outside Lebanon/Saudi Arabia (`cli.py:164-173`), ratings outside 0–5
  (`cli.py:175-188`), and duplicate `(name, phone)` pairs (`cli.py:190-192`).
- **Trigger:** a run where 2% of rows land outside the two claimed markets (a broken
  coordinate parse in a scraper, or a country mix-up). `cli.py:172` would catch it;
  `scrape.yml:71-83` will not, and the master is uploaded to Drive
  (`scrape.yml:88-89`) unchallenged.
- **Fix:** `scrape.yml:69` → `leadminer run`; `scrape.yml:71-83` →
  `leadminer validate data/all_businesses.csv`. `cmd_validate` already returns 1 on
  failure (`cli.py:201`), so the existing `set -e` step semantics are preserved. The
  `ROWS < 100` guard is worth keeping as a wrapper since `cmd_validate` does not have it.

**Severity defence: S2.** It is a verification gap, not wrong output, and the exports
themselves stay internally consistent. It is S2 because it means the CLI this audit series
is measuring is not the thing operators actually run.

### S3 — `cmd_run` re-dedups the entire already-deduped master, every run

- **Where:** `main.py:179-180` (`combined = raw_filtered + master` then
  `records = dedup(combined)`).
- **Breaks:** `dedup` is idempotent, and every record in `master` was already deduped by
  the previous run's `dedup(combined)`. So ~all `n` master records are re-normalised
  (`normalize_phone`, `dedup.py:205`) and re-copied (`dict(record)`, `dedup.py:210`)
  only to be thrown away unchanged. Measured marginal cost: **9.49 s at 500k**, every run,
  forever. `main.py:179` also allocates a third full list of `n + m` references on top of
  the master and the raw batch.
- **Trigger:** `leadminer run` against a 500k master — 9.49 s of the ~41 s CPU budget
  spent re-deriving dicts that already exist in memory.
- **Fix:** dedup only `raw_filtered` and merge that result into the master using the same
  `phone_index` / `name_index` keys, so untouched master rows are passed through by
  reference instead of copied. Turns O(n) wasted work per run into O(m).

**Severity defence: S3.** Linear, correct, and 9.5 s against a 64-minute network problem.
Worth doing only because it is nearly free once S2-1 lands — but it is not the lever.

### S3 — `cmd_stats` materialises 500k rows to print 13 aggregate numbers

- **Where:** `cli.py:86-87` (`rows = list(csv.DictReader(f))`), then 13 separate
  generator walks at `cli.py:98-103` (6), `cli.py:107-110` (3), and `cli.py:116`, `120`,
  `124`, `129` (4 × `collections.Counter`).
- **Breaks:** measured at 500k, the `list(csv.DictReader(f))` alone costs ~10 s and
  ~800 MB, and the command then walks that list 13 times. Fusing the 13 walks into one
  measured **1.78×** on the counting body (5.385 s → 3.027 s, 10.8 → 6.1 µs/row) — but
  that is optimising the cheap half. The real cost is that 800 MB exists at all: every
  number the command prints is a counter, and counters need O(distinct values) memory, not
  O(rows).
- **Trigger:** `leadminer stats` on a 500k master — 800 MB RSS and ~12 s to print output
  that fits on one screen. This is the command the docstring at `cli.py:80` calls "the
  first thing to check", i.e. the one people reach for when a run looks wrong and the data
  is largest.
- **Fix:** one `for row in csv.DictReader(f):` loop accumulating the counters. Memory
  becomes O(distinct categories/regions/services) — kilobytes — and the 13 walks become 1.

**Severity defence: S3.** An offline human tool; the CPU is not the constraint and it
completes. Listed because it is a 20-line change that turns an O(n)-memory command into
an O(1)-memory one.

---

## Not a bug, but worth knowing

- **`main()` is genuinely clean and I could not fault it.** No loop, no state, no
  collection, O(1) in `n` with a flat 0.15 s / 22 MB. Its `except KeyboardInterrupt`
  (`cli.py:316`) is *sufficient* for the threading model, which is a common place to get
  this wrong: `enricher.py:176` and `enricher.py:244` catch `Exception`, not
  `BaseException`, so they do **not** swallow `KeyboardInterrupt`, and `ThreadPoolExecutor`
  re-raises it in the main thread where `cli.py:316` can catch it. Ctrl-C during a
  64-minute enrichment exits 130 cleanly. Verified by reading all three handlers; I did not
  execute it.
- **Ctrl-C during enrichment does leave the pool to finish.** `cli.py:316` returns 130,
  but `enricher.py:237`'s `with ThreadPoolExecutor(...)` still waits for in-flight workers
  (up to 40 × 8 s = 320 s worst case) before unwinding. Cosmetic; the data is atomic
  regardless (`main.py:122-130`).
- **`int(args.func(args) or 0)` at `cli.py:315` is a lossy exit-code funnel.** It maps
  `None` → 0 and coerces anything truthy through `int()`. Today every subcommand returns
  `int` or `None`, so it works — but it is the reason `cmd_run`'s exit code is structurally
  incapable of reflecting pipeline failure (S2 above). Fix it there, not here.
- **`write_csv`'s durability path is free — do not touch it.** `flush()` + `os.fsync`
  measured ~0.000 s against a 8.67 s `DictWriter.writerows` at 500k. 100% of the write
  cost is pure-Python `csv` serialisation. Removing `fsync` would trade a real guarantee
  (`main.py:112-116`) for nothing.
- **`write_csv` does not double peak memory.** `writerows` (`main.py:127`) streams rows
  out. The 800 MB is one copy, not two.
- **The `40`-worker default at `enricher.py:225` is not the lever.** Raising it looks like
  the obvious fix and is the wrong one: the bottleneck is per-host latency (measured
  968 s ≈ 939 s of ideal pool time, so the pool is already **97% utilised** — there is no
  scheduling slack to recover), small-business hosts queue or rate-limit rather than
  serving in parallel, and more sockets is more memory against the S2-2 ceiling. The only
  real speedup is to make fewer requests.
- **Five `write_csv` calls means five fsyncs of the same directory** (`main.py:209-213`).
  Measured negligible. Not a finding.
- **`enricher.py:261` already prints progress every 200 fetches.** At 150k targets and 40
  workers that is a line every ~1.25 s, so the 64-minute enrichment phase is *not* silent.
  Do not add more.
- **Per-record CPU costs worth knowing but not worth changing:** `normalize_name` at
  5.8–7.5 µs/name (`dedup.py:66-69`, dominated by a per-character
  `unicodedata.combining()` generator expression) is the most expensive single function in
  the dedup path, and it only runs for records with no usable phone. `lead_score`'s
  `re.sub(r"\D", …)` (`enricher.py:285`) is ~1.3 µs/record. Both are single-digit-percent
  of a run whose bottleneck is 968 s of network. Recorded so they are not re-litigated —
  `200-fn-cli-py-cmd_score-perf.md` already reached the same conclusion from the other
  direction.
- **A 25% site rate is my assumption, not a measurement.** I picked 30% for the fixture.
  Read the real number off the line count of `without_websites.csv` — the pipeline already
  writes it (`main.py:212`) — and re-run the sensitivity table above. The conclusion holds
  across 20–40%.

---

## Recommended order of work

1. **Stop re-fetching the whole master.** Add `website_checked_at` to `FIELDS`
   (`main.py:33-40`), filter `enricher.py:231` on a **90-day** TTL (materially longer than
   the weekly cron at `scrape.yml:9`, or the fix is a no-op), set the column at
   `enricher.py:250`. **23× less lifetime website traffic; ~1 hour instead of ~27.**
   This is the answer to "the single change with the largest speedup" — nothing else in
   this codebase comes close, because it is the only cost that is not already linear.
2. **Bound the in-flight futures** (`enricher.py:237-238`). Constant memory instead of
   O(n), and −7.56 s of coordination at 500k. Same PR as step 1 if you can.
3. **Make a total scraper failure a non-zero exit** (`main.py:162-168`, `cli.py:37`).
   Smallest diff in this report, and it is the one that stops a broken run being reported
   as a good one. Do it before step 1, because step 1 makes runs much shorter and you
   want the failure signal trustworthy before you start shortening them.
4. **Point CI at the CLI** (`scrape.yml:69` → `leadminer run`,
   `scrape.yml:71-83` → `leadminer validate`). Turns on five data-quality checks that
   already exist and are already tested-by-inspection, and makes this whole audit series
   describe the binary that actually runs.
5. **Dedup only the new batch** (`main.py:179-180`). −9.5 s per run at 500k.
6. **Stream `cmd_stats`** (`cli.py:86-131`). O(1) memory instead of 800 MB. Do it last;
   it is the smallest win and the command is a human tool.

---

## Method

- **Real code, synthetic data.** Benchmarks import the actual `main`, `cli`, `dedup`,
  `enricher`, `pitch_recommender` and `scrapers.whitelist` from the repo and call the real
  functions. `requests` is not installed on this machine and installing is forbidden
  (`BRIEF.md` rule 3), so `requests` and `urllib3` are **stubbed as two files in the
  session temp directory** on `PYTHONPATH`; nothing in the paths I timed performs network
  I/O, so no stubbed call is ever reached. **No scraper was run and no network request was
  made** (`BRIEF.md` rule 2).
- **Fixture:** synthetic `all_businesses.csv` with the real 23-column schema from
  `main.py:33-40` — Arabic and Latin names, sparse optional columns, `website_live` split
  True/False/blank, mixed `+961`/`+966` phone formats, ~2% near-duplicate names. Measured
  **199 bytes/row at 500k (99.5 MB)**.
- **The headline number is load-independent by construction.** `check_websites` was timed
  with `_fetch_website` replaced by a `time.sleep()`, so the measurement is of a sleep, not
  of this CPU. Measured / predicted: 192.21 s vs 188.9 s at 100k, 968.57 s vs 939.7 s at
  500k — 97–98% pool efficiency, which also confirms the pool is not the bottleneck and
  that raising `workers` would buy nothing.
- **Concurrency caveat, stated plainly.** The measurement box was heavily contended for
  part of this session (other audit agents), and sequential phases drifted by up to 60%
  between passes — a single `load_master` @10k reading was 0.109 s on a quiet pass and
  0.408 s later. CPU numbers are therefore **best-of-3 in a fresh subprocess per cell**,
  and the report quotes **µs/record and ratios** wherever it can. Treat absolute CPU
  seconds as ±60%; treat the network table as exact.
- **I chased the one apparent superlinearity and it is a fixture artifact.** `dedup`'s
  marginal cost implied exponent 1.43. Ruled out page faults (warm/cold repeat ratio 1.00×
  at 500k) and ruled out CPython's cyclic GC (`gc.freeze()` and `gc.disable()` each moved
  it ≤10%, contrary to the usual GC-scaling story). `cProfile` located it in `_pick` call
  count growing from ~0 at 100k to 363,952 at 500k — duplicate phone keys from a 9M-value
  random phone space in my generator. Reported as **not a code defect** rather than
  dressed up as one.
- **Python 3.14.8**, not the 3.12 of `BRIEF.md:12`. The `csv` module is pure Python in
  both, so per-record constants shift slightly; scaling exponents do not.
- **Nothing in the repo was modified.** Benchmarks and generated fixtures lived in the
  session temp directory and the multi-MB fixtures were deleted afterwards.
  `git status --porcelain` shows only other agents' untracked audit files.