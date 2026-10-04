# 200 — `cli.cmd_score` cost profile at 500k records

**Target:** `cli.py:247` `cmd_score`
**Lens:** performance / scaling / allocation. Is it O(1), O(n) or O(n^2) in the number
of businesses? Does it allocate proportionally? Concrete timings at 10k / 100k / 500k.
What single change buys the most?

## Verdict

`cmd_score` is **O(n) time, O(n) memory with a 1.74 KB-per-record constant.** It is not
quadratic and there is no hidden superlinear step anywhere in its call path — every
function it calls per record is O(1) with a bounded constant. The problem is not the
asymptotics, it is the constant: **85% of the runtime is `csv.DictReader` /
`csv.DictWriter` plumbing that surrounds the scoring, and the scoring itself is only
~15%.** The single largest change is to replace the
`load_master` → in-memory `list[dict]` → `write_csv` round trip with one streaming
positional pass (`csv.reader` → score a small dict → `csv.writer`). Measured: **1.5–1.6×
faster and peak RSS drops from 892 MB to 23 MB — constant instead of proportional.**

No S1. This function is linear, atomic, and correct on its primary path. The findings
below are S2-scale operability problems, plus one wrong output line (`cli.py:276`) that
under-reports on exactly the path the command exists for.

---

## Complexity answer

### It is O(n). Here is the evidence.

**Static argument.** Nothing in the loop body at `cli.py:265-272` retains state across
iterations, and nothing it calls touches a collection that grows with `n`:

| Call | Where | Per-record work | Depends on `n`? |
|---|---|---|---|
| `resolve_country` | `main.py:50` | ≤2 `dict.get`, `str.strip/upper`, 4 `in` scans of one ≤20-char phone | no |
| `industry_priority` | `whitelist.py:133` | 1 `strip().lower()`, 2 set membership tests | no |
| `recommend_service` | `pitch_recommender.py:25` | 11 `dict.get`, 2 `int()` in `try`, ≤4 set lookups, a fixed if-chain that returns on the first hit | no |
| `lead_score` | `enricher.py:283` | 7 `dict.get`, one `re.sub(r"\D", …)` over a ≤20-char phone, one `str()` of `source` | no |
| `write_csv` | `main.py:108` | `DictWriter.writerows` — 23 `.get` per row + one 23-key set difference per row (`extrasaction="ignore"`) | no |

`changed` is an integer counter (`cli.py:264`), not a list. There is no `in`-list
membership over a growing collection, no `list.index`, no `+=` string accumulation.
`load_master` (`main.py:77`) is a single `csv.DictReader` pass.

**Empirical argument.** Total wall time of the exact `cmd_score` body, min-of-4
interleaved runs:

| n | wall time | ratio vs previous | implied exponent `log(Δt)/log(Δn)` |
|---|---|---|---|
| 10,000 | 0.521 s | — | — |
| 100,000 | 5.033 s | 9.66× for 10× n | **0.985** |
| 500,000 | 18.491 s | 3.67× for 5× n | **0.809** |

If it were O(n²) the 100k→500k step would be 25× (it was 3.67×) and the 10k→100k step
would be 100× (it was 9.66×). If it were O(n log n) you would see the exponent creep
*upward* past 1.0; it drifts *down* because fixed interpreter and page-cache costs
amortise. `b ≈ 0.81–0.99` is the signature of linear-plus-a-constant.

The same drift appears in per-phase cost, which rules out a single quadratic phase
being hidden behind two linear ones:

| phase | @10k | @100k | @500k | µs/row @10k → @500k |
|---|---|---|---|---|
| `load_master` | 0.085 s | 1.049 s | 5.365 s | 8.52 → 10.73 |
| score loop | 0.030 s | 0.393 s | 1.729 s | 2.99 → 3.46 |
| `write_csv` | 0.084 s | 1.053 s | 4.606 s | 8.37 → 9.21 |
| **total** | **0.199 s** | **2.494 s** | **11.700 s** | **19.9 → 23.4** |

(Phase decomposition from a quiet-box run, median of 3. The head-to-head table below
is from a later, contended run — see *Method*.)

**Answer to the question as posed: O(n), unambiguously. There is no quadratic to
remove, and anyone claiming one has not measured.**

### It allocates proportionally — linearly, at 1.74 KB per record

`cmd_score:259` calls `pipeline.load_master(path)` and holds the entire result in a
`list[dict]` for the whole command. Peak RSS, one variant per subprocess:

| n | peak RSS | fitted prediction `23.4 MB + n × 1.74 KB` |
|---|---|---|
| 10,000 | **40.6 MB** | 40.8 MB |
| 100,000 | **196.2 MB** | 197.4 MB |
| 500,000 | **891.8 MB** | 893.4 MB |

That fit is essentially exact, which is the strongest statement available: **allocation
is strictly proportional to `n`, with zero fixed-cost creep and zero superlinearity.**

`tracemalloc` attributes the allocation inside `load_master` to
**1,607 bytes per record** (500k → 803.7 MB current, 803.9 MB peak):

| allocation site | objects | bytes @500k | per record | share |
|---|---|---|---|---|
| `csv.py:185` — `d = dict(zip(self.fieldnames, row))`, the per-row 23-key dict | 1,000,004 | 416.0 MB | 832 B | **52%** |
| `csv.py:177` — `row = next(self.reader)`, the list of field strings | 5,620,027 | 345.5 MB | 691 B | 43% |
| `main.py:93` — `row[field] = float(row[field])` | 1,299,791 | 31.2 MB | 62 B | 4% |
| `main.py:99` — `row[field] = int(float(row[field]))` | 213,927 | 6.8 MB | 14 B | 1% |

**Just over half the memory is a 23-key dict allocated 500,000 times, where every one of
those dicts holds the identical 23 keys.** That is 416 MB of pure overhead. The keys are
known once, at compile time; the structure carrying them should be allocated once too.

---

## Where the time actually goes

Component attribution of the score loop, `min-of-3`, 200k-record sample, µs/record:

```
whole score loop                     4.275
  ├ resolve_country                  0.311
  ├ industry_priority                0.314
  ├ recommend_service                1.202
  └ lead_score                      2.014
      └ ^ of which re.sub(r"\D",…)   1.349   (67% of lead_score)
```

So the scoring arithmetic — the part `cmd_score` exists to do — is 4.275 µs/record.
The plumbing around it is 19.1 µs/record. **The command spends 82% of its time moving
data into and out of dictionaries.**

The floor makes this concrete. I built a variant that reads the CSV with `csv.reader`
and writes it straight back with `csv.writer` and **does no scoring at all**:

| n | `cmd_score` today | floor: read + write, zero scoring | scoring's share |
|---|---|---|---|
| 10,000 | 0.521 s | 0.182 s | 65% |
| 100,000 | 5.033 s | 2.061 s | 59% |
| 500,000 | 18.491 s | 6.216 s | **66%** |

Against the quiet-box phase decomposition the split is even starker
(`load 46% / score 15% / write 39%` at 500k). Either way: **a perfect, instant scoring
implementation would still leave two thirds of the runtime on the table.**

I also split the write phase to rule out the obvious suspects:

```
write phase @500k:  DictWriter.writerows  7.921 s
                    flush + os.fsync     ~0.000 s
                    os.replace           0.0003 s
```

`fsync` and the atomic rename are free. **100% of the write cost is pure-Python `csv`
serialisation.** Do not touch `main.py:128-130` — the durability path is not the problem
and removing it would trade correctness for nothing.

---

## Findings

### S2 — `cmd_score` peaks at 892 MB and every byte of it is proportional to row count

- **Where:** `cli.py:259` (`records = pipeline.load_master(path)`), holding all records
  until `cli.py:276`.
- **Breaks:** the command is a pure CPU operation — no network, no threads, no disk
  dependency beyond one file — yet it needs 1.74 KB of heap per row to do it. At the
  500k target that is 892 MB, of which 416 MB is per-row `dict` overhead for 23
  identical key sets (measured above). The cost is not superlinear, but it is
  *unbounded*: at the same 1.74 KB/record the GitHub Actions standard runner (7 GB)
  is hit at **≈4.0M records**. Widening the market from Lebanon to Saudi Arabia
  (see `096-osm-to-saudi.md`) plus the multi-year cumulative master
  (`all_businesses.csv` is append-only by design, `BRIEF.md:51`) puts that within reach,
  and the failure mode is an OOM kill mid-`write_csv`.
- **Trigger:** `leadminer score data/all_businesses.csv` against a ~4M-row master. The
  process is killed by the runner's memory limit; because `write_csv` writes to
  `all_businesses.csv.tmp` and only then renames (`main.py:122-130`), the master itself
  survives — but the re-score silently never happens.
- **Fix:** stream — never build the `list[dict]`; read one row, score it, write it,
  discard it. Peak memory becomes a constant.

**Severity defence:** not S1, because at the stated 500k target the command completes
in ~12 s inside 892 MB and does not die. It becomes S1 at the next order of magnitude.

### S2 — the `{changed}` count is wrong on the only path `leadminer score` exists for

- **Where:** `cli.py:270-271` (counts only `lead_score` deltas), printed at
  `cli.py:276`.
- **Breaks:** the loop mutates **four** columns — `country` (`cli.py:266`),
  `industry_priority` (`cli.py:267`), `recommended_service` (`cli.py:268`) and
  `lead_score` (`cli.py:272`) — but the counter only compares `lead_score`
  (`if new != r.get("lead_score")`). `lead_score` consumes `industry_priority`
  (`enricher.py:306-310`) but **not** `recommended_service`. So any rule change that
  touches only the pitch logic leaves `lead_score` untouched and the command reports
  `0 changed` while having rewritten `recommended_service` on thousands of rows.
  `recommended_service` is the column sales reps read to choose a pitch
  (`BRIEF.md:55`, `pitch_recommender.py:7-8`).
- **Trigger:** established and reproduced end-to-end against the real code. Take a
  100k-row master that already carries correct scores, relabel the Tier 7 string at
  `pitch_recommender.py:96`, then run `leadminer score`:
  ```
  pass 1 (cold master):      reported changed =  99,028   rows rewritten =  99,974
  pass 2 (no rule edit):     reported changed =       0   rows rewritten =       0
  pass 3 (service relabel):  reported changed =       0   rows rewritten =   2,487   <-- under-reported 2487x
  ```
  The operator sees `re-scored 100,000 records, 0 changed` and concludes the new pitch
  rules are a no-op. 2,487 rows were rewritten on disk.
- **Fix:** diff all four columns in the loop body, not just `lead_score`:
  ```python
  before = (r.get("country"), r.get("industry_priority"),
            r.get("recommended_service"), r.get("lead_score"))
  … recompute all four …
  if before != (r["country"], r["industry_priority"],
                r["recommended_service"], r["lead_score"]):
      changed += 1
  ```

**Severity defence:** this is wrong operator-facing output rather than wrong data on
disk, and the brief puts "operability" at S2. It is the highest-priority S2 in this
report because it is the command's *only* output (`cli.py:276`) and it lies precisely
when the operator is checking whether a rule change worked — the stated purpose of the
command at `cli.py:248` and of the CLI's offline commands generally (`cli.py:14-15`).

### S2 — `cmd_score` rewrites one CSV, never the four that are actually the product

- **Where:** `cli.py:265-275`.
- **Breaks:** the loop recomputes four derived columns and writes exactly one file
  (`pipeline.write_csv(out, records)`, `cli.py:275`). The other four exports —
  `qualified_businesses.csv`, `with_websites.csv`, `without_websites.csv`,
  `sales_ready.csv` — are written *only* by `main.py:209-213`, i.e. only by
  `leadminer run`. So after `leadminer score` the master and the four derived CSVs
  disagree, and the command prints nothing about it. Three of those four are
  row-subset or filter views of columns `cmd_score` just changed
  (`sales_ready.csv` filters on `industry_priority`, `main.py:204`;
  `qualified_businesses.csv` on `completeness_score`, `main.py:206`) — so the split is
  not hypothetical.
- **Trigger:** add a category to `PRIORITY_INDUSTRIES` (`whitelist.py:17`), run
  `leadminer score data/all_businesses.csv`. `data/sales_ready.csv` keeps its old
  membership and its old `lead_score` values. Nothing in the output says so.
- **Fix:** either have `cmd_score` regenerate all five exports from the re-scored
  records, or have it refuse to run without `--out` and print an explicit
  "derived exports in `data/` are now stale; re-run `leadminer run` to rebuild them".

**Related, smaller:** `cmd_score` also never recomputes `completeness_score`, which
`enricher.enrich` sets at `enricher.py:349` and which `main.py:206` gates
`qualified_businesses.csv` on. Today that omission is harmless because
`pitch_recommender.py:48-52` assigns `completeness` and **never reads it** — the
Tier 3 "weak completeness" branch described in the comment at
`pitch_recommender.py:68-69` does not exist. It will start mattering the moment someone
implements that branch, and `leadminer score` will then be silently wrong for that
column. Flagging so the dead local is either wired up or deleted with the `cmd_score`
gap closed in the same change.

### S2 — `cmd_score` is a second, independent writer of `country`, and it persists `resolve_country`'s default without enrichment

- **Where:** `cli.py:266` (`r["country"] = pipeline.resolve_country(r)`).
- **Breaks:** `resolve_country` (`main.py:50-74`) falls through to
  `_DEFAULT_COUNTRY = "LB"` (`main.py:47`) when `country` is blank and the phone is not
  in `+966`/`00966`/`+961`/`00961` form. `cmd_score` **writes that fallback straight to
  disk** on every row, with no `enricher.enrich` pass in between to correct it. In the
  live pipeline `resolve_country` runs at `main.py:184` and is followed by
  `enricher.enrich` at `main.py:187`, whose `infer_region` (`enricher.py:85`) at least
  gets a second look. In `cmd_score` the guess is final. A Saudi record that arrived
  from OSM with a blank `country` column and no phone number is stamped `LB`
  permanently — and a later `main.py:194`
  (`normalize_phone(raw_phone, r.get("country") or _DEFAULT_COUNTRY)`) will then apply
  the Lebanese country code to it.
- **Trigger:** one row with `country` blank, `phone` blank, `lat` 24.7, `lon` 46.7
  (Riyadh). `leadminer score` writes `LB`. Re-run the pipeline and that record is
  country-Lebanon with a Lebanese phone prefix.
- **Fix:** in `cmd_score`, treat `country` as not-a-scoring-column and leave it
  untouched; if it must be recomputed, write it only where the incoming value was
  already a recognised code.

**Severity defence:** S2, not S1, because the damage is confined to rows that arrive
with both a blank `country` and a blank `phone`, and the same fallback already exists
on the main pipeline path (`main.py:184`) so `cmd_score` is a second writer of an
existing weakness rather than the origin of it. Cross-referenced against
`094-infer-region-correction.md`, which owns the `infer_region` side.

### S3 — up to 500,000 silent iterations with no progress output

- **Where:** `cli.py:265`.
- **Breaks:** the loop prints nothing until `cli.py:276`. At the 500k target that is a
  ~12 s silent gap on a healthy run. Compare `enricher.py:261`, which prints every 200
  website fetches, and `enricher.py:235`, which prints the target count up front — the
  long-running parts of this codebase already do this and `cmd_score` does not. It gets
  worse if the master grows: the silence scales linearly with no upper bound.
- **Trigger:** `leadminer score` against a 500k master with stdout attached to a CI log
  and a 30 s step timeout. The step is killed at 30 s having produced no output.
- **Fix:** print the record count after `load_master` and every 25,000 rows inside the
  loop, matching `enricher.py:261`.

### S3 — the `re.sub(r"\D", …)` in `lead_score` looks expensive; measured, it is not worth changing

- **Where:** `enricher.py:285`.
- **Breaks:** nothing. This is a **rejected** optimisation, recorded so it is not
  re-litigated. It is 1.349 µs/record — 67% of `lead_score`, and `lead_score` is 2.014
  µs/record, which is 15% of the command. So the whole line is ~2.7% of `cmd_score`.
  Measured end-to-end at 500k with round-robin interleaving to control for machine
  load, both alternatives verified to produce byte-identical `lead_score` on 50,000
  records:
  ```
  current re.sub(pattern)  : min 5.382s  med 6.089s   (+0.0%)
  precompiled NON_DIGIT.sub: min 5.007s  med 5.426s   (+7.0% of the loop)
  "".join(c.isdigit())     : min 5.479s  med 6.263s   (-1.8% of the loop)
  ```
  +7% of a loop that is 15% of the runtime is ~1% of the command — inside the noise on
  this hardware, and not worth a diff. Do the streaming rewrite instead; it is 1.58×.
- **Trigger:** none. Do not spend a review cycle here.

---

## Not a bug, but worth knowing

- **`cmd_score` is genuinely linear and I could not find anything superlinear.** Checked
  explicitly for the usual suspects — `in`-list membership over growing collections,
  `list.index` / `list.remove` in a loop, string `+=` accumulation, nested comprehensions
  over prior rows, repeated `collections.Counter` rebuilds. None present. The exponent
  is 0.81–0.99 by log-log fit across a 50× size range. This is not a code that will fall
  over; it is a code that is 5× slower than it needs to be.
- **In-place overwrite by default is safe.** `cli.py:274` defaults `--out` to the input
  path, which looks alarming, but `write_csv` (`main.py:108-133`) writes
  `<path>.tmp`, fsyncs, then `os.replace`s — atomic on POSIX. A reader sees either the
  complete previous file or the complete new one, never a partial. `os.replace` measured
  0.0003 s at 500k.
- **Do not "optimise" the durability path.** `f.flush()` + `os.fsync` measured
  ~0.000 s against a 7.921 s `DictWriter.writerows`. The whole write cost is
  pure-Python `csv` serialisation; stripping fsync buys nothing and costs the guarantee.
- **`write_csv` does not double peak memory.** It uses `writerows`, which streams rows
  out (`main.py:127`). There is no second full copy. The 892 MB is one copy.
- **Peak memory is not returned to the OS.** Repeating `cmd_score` in one process leaves
  `ru_maxrss` at ~892 MB between iterations — pymalloc retains the arenas. Relevant if
  you ever wrap this in a long-lived service or a test loop.
- **23 columns, not 22.** `main.py:33-40` `FIELDS` has 23 entries (`website_live` and
  the five derived columns added since `BRIEF.md:59-62`). The per-record dict overhead
  figure above is for 23 keys.
- **Parallelism is the largest untapped lever and I could not measure it.** All four
  scorers are pure functions over independent records with no I/O and no shared state —
  textbook `ProcessPoolExecutor` work, and the only remaining path to a multi-x win once
  the plumbing is fixed. I did not quote a number because the measurement box had a load
  average of **84 on 12 cores** for the whole session (other audit agents), which makes
  any parallelism benchmark meaningless. Flagged explicitly as unmeasured.
- **Column order is a contract.** `resolve_country`, `industry_priority`,
  `recommended_service` and `lead_score` all *read* `industry_priority` or feed it to
  `lead_score` (`enricher.py:306-310`). The current order at `cli.py:267-272`
  (priority before `lead_score`) is correct and must be preserved by any rewrite — the
  same ordering constraint that `main.py:190-197` documents in a comment.

---

## Recommended order of work

1. **Fix the `{changed}` counter** (`cli.py:270-271`) so it diffs all four mutated
   columns. Smallest diff in this report, and it is the only finding that makes the
   command lie to its operator. Do this first, before the refactor, so the refactor's
   before/after numbers are trustworthy.
2. **Rewrite the plumbing: one streaming positional pass.** Replace
   `cli.py:259` + `cli.py:274-275` with a `csv.reader` loop that builds a small dict of
   only the 15 fields the scorers read, casts those, scores, patches the 4 changed
   values back into the raw row by index, and emits via `csv.writer`. Measured effect:
   - **1.58× at 500k** (18.491 s → 11.732 s contended; estimated 11.7 s → ~7.4 s on a
     quiet box, from the phase shares)
   - **peak RSS 891.8 MB → 23.1 MB, and flat** — allocation stops being proportional to
     row count entirely
   - same speedup at every size tested: 1.62× @10k, 1.51× @100k, 1.58× @500k
   - Decomposition matters: streaming alone while keeping `DictReader`/`DictWriter` is
     only **1.05×**. The remaining ~1.5× comes from never materialising a 23-key dict
     per row. Do not stop at streaming.
   - Preserve the column ordering at `cli.py:267-272`.
3. **Decide what `cmd_score` does about the other four CSVs** (S2 above): regenerate them,
   or refuse in-place writes, or print a loud staleness warning. Do not leave it silent.
4. **Add progress output** (`cli.py:265`) — 20 seconds of work, and it makes step 2
   debuggable.
5. **Drop `country` from the `cmd_score` rewrite** (S2-4), or restrict it to rows whose
   incoming value was already a recognised code.
6. **Only then consider parallelism.** With the constant-memory streaming version in
   place, a `ProcessPoolExecutor` over line ranges is the only remaining lever above
   ~2×. Measure it on a quiet machine.

---

## Method

- **Real code, synthetic data.** Benchmarks import the actual `main`, `enricher`,
  `pitch_recommender` and `scrapers.whitelist` from the repo and call the real
  functions. `requests` is absent from this machine, so it is stubbed in `sys.modules`;
  nothing in the `cmd_score` path does network I/O, so no stubbed call is ever reached.
- **Fixture:** a synthetic `all_businesses.csv` with the real 23-column schema from
  `main.py:33-40` and a realistic sparse distribution — Arabic and Latin names, ~65%
  blanks in the 12 optional columns, `website_live` split True/False/blank, phone
  formats `+961 1 222333` / `+966 …`. Measured **228.5 bytes/row at 500k (114.3 MB)**,
  which is representative of the real export width.
- **Attribution:** `tracemalloc` peak/current during `load_master`, plus
  `compare_to(..., "lineno")` to attribute allocations to the stdlib `csv` internals and
  to `main.py:93` / `main.py:99`. `resource.getrusage(RUSAGE_SELF).ru_maxrss` (bytes on
  macOS) for peak RSS, one variant per subprocess so peaks do not contaminate each other.
- **Concurrency caveat, stated plainly.** The measurement box reached a load average of
  **84 on 12 cores** during this session. Two different contention regimes are visible
  in my own numbers: the phase-decomposition table (0.199 s / 2.494 s / 11.700 s) was
  taken while the box was quiet; the head-to-head table (0.521 s / 5.033 s / 18.491 s)
  was taken later under load and is inflated ~1.6–2.5×. **Use the phase *shares*
  (46% / 15% / 39%) and the head-to-head *ratios*; treat the absolute seconds from the
  contended run as an upper bound, not a prediction.** All comparative numbers use
  round-robin interleaved repeats with min-of-N, because under load min is the only
  robust estimator and sequential phases drift by 40%+ (one sequential measurement of
  the `re.sub` micro-opt inverted its sign entirely, from −39% to +7%, purely from
  contention ordering).
- **Python 3.14.8**, not the 3.12 of `BRIEF.md:12`. The `csv` module is pure Python in
  both, so the per-record constants move slightly but the scaling exponents do not.
- **Nothing in the repo was modified.** `git status --porcelain` is clean. Benchmarks
  live in the session temp directory; all runs used `PYTHONDONTWRITEBYTECODE=1`. No
  scrapers were run and no packages were installed.