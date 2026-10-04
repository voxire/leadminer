# 373 — `completeness_score` is O(1) per record, allocates nothing, and is already optimal

**Target:** `completeness_score` · **Lens:** perf (cost as the dataset grows to 500k businesses)

> **Line-number note:** the brief and sibling report 375 both anchor this function at
> `enricher.py:101`. That line is blank. `grep -n`, `awk` and `ast` all agree the
> `def` is at **`enricher.py:102`** and the body runs to **`enricher.py:118`**. Every
> anchor below uses the verified numbers.

## Verdict

The function is **strictly O(1) per record, O(n) total, and allocates zero bytes**. Over a
50x size increase (10k → 500k records) the *per-record* CPU cost rises only **8.6%**
(351 ns → 381 ns); an O(n²) implementation would have shown 2500x. The whole 500k-record
pass costs **191 ms of CPU** — **0.001% of the 300-minute CI budget**. There is no S1 or
S2 performance defect in the target function, and I could not construct one: the body is
8 `dict.get` calls, 7 integer increments and a return. I tested four rewrites; the fastest
is 1.19x and it **regresses 1.7x the moment a key is missing**, so the honest answer to
"single change with the largest speedup" is *none, not here*. The largest real win in the
immediately surrounding statement is 8 lines away: the 9 defensive `setdefault` calls at
`enricher.py:340-348` cost **197 ms at 500k — more than the score they exist to prepare
for** — and they are provably no-ops.

## Findings

### S2 — `check_websites` queues every site up front: 1.66 KB of pinned objects per site, with no backpressure

- **Where:** `enricher.py:237-238` (the `ThreadPoolExecutor` block), reached from
  `enricher.py:337`, twelve lines above the scoring loop under audit.
- **Breaks:** `enricher.py:238` is
  `futures = {pool.submit(_fetch_website, url): idx for idx, url in targets}` — an
  unbounded dict comprehension. All N sites are submitted before the first result is
  consumed, and `as_completed` at `enricher.py:240` pops nothing, so every submitted
  `_WorkItem`, every `Future`, and every retained `(outcome, contacts)` tuple stays live
  for the entire run. The 40 workers at `enricher.py:237` provide concurrency but **not
  flow control**: memory, not the worker pool, is the queue. Measured with `tracemalloc`
  around the real statement with a no-op submit target:

  | targets | futures created | traced allocation | bytes per site |
  |--------:|----------------:|------------------:|---------------:|
  |   7,054 |           7,054 |            12.0 MB |         1,701 B |
  |  70,628 |          70,628 |           118.6 MB |         1,679 B |
  | 352,780 |         352,780 |           599.7 MB |         1,700 B |

  The bytes-per-site column is flat across a 50x range, which confirms the growth is
  linear and that 1.66 KB/site is the constant to plan against. This is the **only** code
  in `enricher.py` whose memory footprint grows with the dataset — `completeness_score`
  contributes 0 bytes (see below). For reference the 500k-record list itself is 392 MB RSS
  (measured via `ps`), and `dedup.py:210`/`dedup.py:218` hold `dict(record)` copies of
  every row, so peak RSS is roughly 2x the record set before this line runs.
- **Trigger:** a 500k-record master CSV with a 20% website rate → 100,000 targets →
  **~170 MB** of pinned futures. At the 41% rate of a real Google Places scrape →
  205,000 targets → **~340 MB**. On a GitHub-hosted `ubuntu-latest` runner (16 GB) that
  survives; the OOM wall arrives at roughly 3M sites on a 7 GB runner or 9M on a 16 GB
  one. The submit call itself also blocks: 352,780 submissions took 37 s of wall time
  under load before the first response is even awaited.
- **Fix:** bound the in-flight window instead of submitting everything — submit at most
  `workers * 4` tasks, and refill as futures retire (a `wait(..., return_when=FIRST_COMPLETED)`
  loop), which turns an O(n) allocation into O(workers) with identical results.

### S3 — The 9 `setdefault` calls at `enricher.py:340-348` cost more than `completeness_score` itself

- **Where:** `enricher.py:340-348`, executed immediately before `completeness_score(r)`
  at `enricher.py:349`.
- **Breaks:** nine `dict.setdefault` calls per record cost **394 ns/record**, against
  `completeness_score`'s 351–381 ns/record. The defensive defaulting is **1.03x as
  expensive as the score it is preparing for** — 197 ms versus 152–191 ms at 500k. And it
  is dead work: every record reaching this loop already has all 22 keys. All three
  scrapers construct full dicts including `completeness_score`/`lead_score`
  (`scrapers/osm.py:118-121`, `scrapers/wikidata.py:126-129`,
  `scrapers/google_places.py:301-304`), and `load_master` materialises every column from
  the CSV header (`main.py:86-104`). Measured, interleaved, CPU time, min-of-13, 200k
  records, scaled to 500k:

  | variant | ns/rec | vs today | 500k ms | 500k delta |
  |---|---:|---:|---:|---:|
  | A today's `enricher.py:339-350` | 1,920 | 1.00x | 960 | — |
  | B body of `completeness_score` inlined into the loop | 1,904 | 1.01x | 952 | +8 |
  | C nine `setdefault` lines deleted | 1,496 | **1.28x** | 748 | **+212** |
  | D the nine `setdefault` calls alone | 394 | 4.88x | 197 | +763 |

  Row B is the important negative result: **inlining the function body buys 1%**, so the
  per-record function call is not a cost worth removing. (An earlier wall-clock run
  suggested 1.43x; that was contention on a shared box, and the interleaved CPU-time run
  above refutes it.) Removing the defaults is 8x more valuable than any rewrite of the
  function body.
- **Trigger:** any run at all — 500k records pays 197 ms. It is S3 rather than S2 only
  because 197 ms against an 18,000 s budget is 0.001%.
- **Fix:** delete `enricher.py:340-348`. `csv.DictWriter` fills absent keys from
  `restval` (default `''`) rather than raising, and every consumer of these fields uses
  `.get()`, so nothing downstream observes the difference. If the defaults are kept for
  defensiveness, hoist them out of the per-record loop into a one-time pass over
  `FIELDS` (`main.py:33-40`).

### S3 — `lead_score` is computed twice per record; the duplicate costs 563 ms at 500k

- **Where:** computed at `enricher.py:350`, recomputed at `main.py:197`.
- **Breaks:** `lead_score` costs **1,126 ns/record** — 3.3x `completeness_score` — because
  `enricher.py:285` runs `re.sub(r"\D", "", str(record.get("phone") or ""))` on every call
  (`re.sub` carries per-call dispatch overhead that `dict.get` does not) plus 11 field
  reads. The `main.py:195-196` comment acknowledges the ordering problem but keeps the
  recompute instead of reordering. At 500k that duplicate is **563 ms**, and
  `completeness_score`'s entire pass is 191 ms — so **the redundant line is 2.9x more
  expensive than the function this audit is about.** Deleting `main.py:197` and setting
  `industry_priority` before `enrich()` is worth 84x more than the best available rewrite
  of `completeness_score`.
- **Trigger:** 500k records → 563 ms. Like the item above, S3 because it is 0.003% of
  the budget; it is listed because it is the highest-leverage single line in the
  neighbourhood and it is one line long.
- **Fix:** hoist the `main.py:189-191` `industry_priority` assignment to before
  `enrich(records)` at `main.py:187`, then delete `main.py:197`.

## Not a bug, but worth knowing

- **The target function allocates nothing.** `tracemalloc` across 500,000 calls on one
  record shows a net delta of **832 bytes total — 0.0017 bytes/call**, at a single site
  that does not grow with call count (it is tracemalloc's own snapshot bookkeeping). The
  return value is always a cached small int: all of 0–7 verified as interned singletons
  (`completeness_score(...) is i` for i in 0..7), and the observed distribution over 50k
  records is `{0: 11630, 1: 7450, 2: 3527, 3: 6929, 4: 9465, 5: 2010, 6: 6042, 7: 2947}`.
- **The call site allocates nothing either.** `r["completeness_score"] = completeness_score(r)`
  at `enricher.py:349` is an **overwrite**, not an insertion, because the key is
  pre-seeded by `scrapers/osm.py:121`, `scrapers/wikidata.py:129` and
  `scrapers/google_places.py:304`, and by `load_master` (`main.py:86-104`). No dict
  resize, no new slot.
- **Measured scaling, authoritative.** Interleaved sizes, CPU time via
  `time.process_time`, min-of-15 rounds, all keys present, one call per record exactly as
  at `enricher.py:349`:

  | records | CPU min (ms) | CPU median (ms) | ns/record | ns per `.get()` | ratio vs 10k |
  |--------:|--------------:|----------------:|----------:|-----------------:|-------------:|
  |  10,000 |          3.51 |           3.69 |     351.2 |             43.9 |        1.000x |
  | 100,000 |         37.17 |          40.83 |     371.7 |             46.5 |        1.058x |
  | 500,000 |        190.70 |         203.93 |     381.4 |             47.7 |        1.086x |

  **O(n), with a 8.6% cache-locality tail.** Extrapolating: 1M records ≈ 0.38 s,
  2M ≈ 0.77 s. The 8.6% rise is real and worth understanding: the 500k-record set is
  392 MB RSS, so the 8 dict probes per record increasingly miss L2/L3. It is bounded —
  it does not compound — because the working set is streamed once, not re-scanned.
- **Wall-clock at 500k is dominated 5-to-4600x over by the HTTP stage, not by scoring.**
  `check_websites` (`enricher.py:225-276`) does one GET per site at 40 workers with
  `timeout=8` (`enricher.py:175`): 250,000 sites at a 300 ms mean is 31 min, at 800 ms is
  83 min, at 2 s is 208 min, and at the 8 s timeout is **833 min — over the
  `timeout-minutes: 300` at `.github/workflows/scrape.yml:25`.** Scoring the same 500k
  records costs 0.19 s. Any perf work on `completeness_score` is invisible next to this.
- **`bind g = record.get` — refuted, do not do it.** The obvious micro-optimisation
  (hoist the bound method) measured **0.96x, 0.98x, 0.90x, 0.88x — slower at every key
  configuration.** CPython 3.11+ already specialises `LOAD_METHOD`/`CALL` on `dict.get`;
  introducing a local costs an extra `STORE_FAST`/`LOAD_FAST` pair.
- **`rec[k]` + `try/except` — 1.19x, and a trap.** Fastest variant measured: 331 ns →
  **279 ns/record, saving 26 ms at 500k.** But it depends on all 8 keys being present, and
  the moment one is missing it raises and re-enters the original function:

  | keys absent | current | `rec[k]`+try | speedup |
  |---:|---:|---:|---:|
  | 0 | 331 ns | 279 ns | **1.19x** |
  | 1 | 318 ns | 521 ns | 0.61x |
  | 3 | 293 ns | 515 ns | 0.57x |
  | 7 | 278 ns | 490 ns | 0.57x |

  It trades a 26 ms win for a 106 ms loss. Not worth it. (Equivalence was verified
  exhaustively — all 2^8 subsets of the 8 scored keys plus six falsy-value shapes
  (`0`, `""`, `None`, `[]`, `False`, `"0"`) — so the variant differences below are speed
  only, not behaviour.)
- **`len(record.keys() & frozenset(SIX))` — slower AND wrong.** 381/373/321/322 ns
  (0.87x–0.91x) because it allocates a keys view plus an intersection set per record —
  the only rewrite that allocates at all. It also changes results: it counts key
  *presence*, not truthiness, so `completeness_score({"phone": 0})` returns **1** instead
  of **0**. Same failure for `""`, `None`, `[]`, `False`. That is a correctness bug
  dressed as a performance patch.
- **Generator/`map` rewrites are 2–3x slower.** `sum(1 for k in KEYS if rec.get(k))` at
  0.36x–0.47x and `sum(map(bool, map(rec.get, KEYS)))` at 0.51x–0.58x: the generator
  frame and iterator protocol cost more than seven straight-line `if` statements.
- **Instruction-count floor.** `dis` on the shipped body: **89 bytecode ops**, of which
  8 are `LOAD_ATTR` + 8 are `CALL` (the 8 `dict.get` calls), plus 16 `LOAD_FAST_BORROW`,
  8 `LOAD_SMALL_INT`, 8 `STORE_FAST`, 8 `TO_BOOL`, 8 `NOT_TAKEN`, 7 `POP_JUMP_IF_FALSE`,
  7 `BINARY_OP`, 1 each of `RESUME`/`POP_JUMP_IF_TRUE`/`RETURN_VALUE`. The 500k pass is
  44.5M ops in 191 ms = **4.3 ns/op** — interpreter-dispatch-bound, with nothing left to
  remove in pure Python.
- **Methodology.** Absolute wall-clock on this box is unreliable: it is a 12-core Mac
  running ~400 concurrent audit agents, and wall-clock per-record figures swung between
  320 ns and 1087 ns for identical work across runs. Every number above is therefore
  `time.process_time()` (CPU) with min-of-N, and the compared variants are **interleaved
  A/B/C/D within each round** so contention hits all arms equally. Ratios are trustworthy
  to ~5%; absolute CPU figures are trustworthy to ~10%. Runtime is CPython **3.14.8**;
  the shipped target is 3.12 (`pyproject.toml:9`, `.github/workflows/scrape.yml:35`).
  Both have `dict.get` specialisation, so the 3.12 numbers should be within a few
  percent — treat 191 ms at 500k as "roughly 0.2 s", not as a precise figure. Sibling
  report 375 measured 1.01 µs/call; that is wall-clock under load and is consistent with
  the 0.32–1.09 µs wall-clock range observed here, not with the 0.35 µs CPU figure.

### Reproduction

Benchmarks live outside the repo and import nothing from it — each extracts the target
function's source with `ast.get_source_segment` and `exec`s it, so what is measured is
byte-identical to `enricher.py:102-118` and the repo is neither imported nor written:

```
python3 bench_scaling.py    # the 10k/100k/500k table + linearity ratios
python3 bench_variants.py   # the four rewrites, equivalence gate, failure modes
python3 bench_inline.py     # setdefault / inline / today, interleaved CPU time
python3 bench_ratio.py      # completeness vs lead_score vs infer_region vs the tail
```

Synthetic record shape: the 22 `BusinessRecord` keys (`scrapers/base.py:5-28`) with
deterministic fill rates — phone 62%, address 77%, instagram 55%, website 41%, whatsapp
22%, email 18%, facebook 12%, linkedin 6%, rating 44% — which reproduces a realistic
score distribution (mean ≈ 2.9 of 7).

## Recommended order of work

1. **Do not touch `enricher.py:102-118`.** It is O(n), allocation-free and dispatch-bound
   at 0.001% of the CI budget. Record that and move on.
2. **Bound `enricher.py:238`'s in-flight window** (S2) — the only O(n) allocation in the
   module, 1.66 KB per site, no backpressure. Highest value per line changed.
3. **Delete `enricher.py:340-348`** (S3) — 212 ms at 500k, provably no-ops, 8x more than
   any rewrite of the target function.
4. **Delete the duplicate `lead_score` at `main.py:197`** (S3) — 563 ms at 500k, by moving
   `main.py:189-191` above `enrich()` at `main.py:187`.
5. If someone insists on touching the target function, the only defensible change is a
   comment recording that it is O(1)/zero-alloc and why `rec[k]` and `keys() & frozenset`
   are both worse — this report is that comment.