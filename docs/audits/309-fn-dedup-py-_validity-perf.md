# 309 — `_validity` is O(1) in n; the cost lives in its caller, `rank()`

## Verdict

`_validity` (`dedup.py:120`) is **O(1) in the number of businesses** — a pure, stateless,
per-value predicate with no loop, no accumulation, and no shared state. It is **not** O(n) and
**certainly not O(n²)**. Its *total* cost is O(C), where C = the number of `_merge`
invocations (key collisions), not the record count. At 500k records it costs **1.16s of an
8.36s `dedup()` run — 13.8%** — and the whole `dedup()` is under 10 seconds, which is
noise next to `enrich()`'s 1 HTTP GET per website at 40 workers. **Do not optimize
`_validity`.** The single change with the largest speedup is at its *call site*:
short-circuit the `rank()` comparison at `dedup.py:187` so a tie on trust never reaches
`_validity` at all. That is **1.26–1.40x** on `dedup()` with byte-identical output, and it
simultaneously attacks the real hot spot, `_trust_for`/`_sources_of` (28.8% vs `_validity`'s
10.4%).

## Answer to the brief's question, up front

| Question | Answer |
|---|---|
| O(1), O(n), or O(n²) in businesses? | **O(1) per call.** Total is O(C) in *collisions*, not O(n). |
| Allocates proportionally? | **No.** O(1) objects per call, nothing retained (1M calls → 712 bytes). |
| 10k / 100k / 500k? | **0.141s / 1.477s / 8.360s** for `dedup()`; `_validity` is 13.8% of the 500k figure. |
| Single largest speedup? | Lazy short-circuit at `dedup.py:187` → **1.26–1.40x**, output identical. |

## Method and its limits

All timings: CPython 3.14.8, arm64 macOS, `best-of-5` with GC disabled, all sizes measured
in one process. **The host was heavily contended** (`load average` 16–63 against 12 CPUs —
other audit agents). Repeated measurements of the same 500k workload ranged 8.06s–38.9s.
Absolute seconds are therefore soft; **the call counts below are exact and noise-free**, and
the *ratios* (1.26x, 13.8%, 23.57 calls/merge) are the load-bearing claims. Call graphs were
captured by wrapping the module globals in memory (`dedup._validity = wrapper`), which works
because `_pick` resolves `_validity` as a module global at call time. No file was modified;
no network call was made.

Records were generated at source-realistic fill rates taken from `scrapers/osm.py:96-119`
(email ~4%, website ~7%) and `scrapers/google_places.py:260+` (email ~12%, website ~42%,
rating ~75%), over the 23-key `BusinessRecord` shape (`scrapers/base.py:5-28`), with 30% of
records key-duplicating an earlier one.

## Complexity: `_validity` is O(1) in n — proven three ways

**1. Call count per collision is invariant under a 50x size change.** Exact counts:

| n | `_merge` | `_pick` | `_trust_for` | `_sources_of` | `_is_missing` | `_validity` | **`_validity`/`_merge`** |
|---|---:|---:|---:|---:|---:|---:|---:|
| 10,000 | 3,000 | 69,000 | 70,708 | 76,708 | 181,836 | 70,708 | **23.57** |
| 100,000 | 30,000 | 690,000 | 706,680 | 766,680 | 1,817,928 | 706,680 | **23.56** |
| 500,000 | 150,000 | 3,450,000 | 3,536,906 | 3,836,906 | 9,094,593 | 3,536,906 | **23.58** |

The ratio is flat to two decimals across a 50x size change. `_validity` is invoked only from
`rank()` (`dedup.py:182`), which is reached only from `_pick` (`dedup.py:187`), which is
reached only from `_merge` (`dedup.py:193`), which runs once per key collision
(`dedup.py:208`, `dedup.py:216`). A first-occurrence record takes `dict(record)` at
`dedup.py:210`/`dedup.py:218` and never touches `_validity`. Hence **cost tracks collisions,
not n**.

**2. Decisive control: hold collisions fixed, grow n.** With merges pinned at 20,000 while n
grew from 30,000 to 500,000, `_validity` calls went **509,764 → 455,606** — flat, and if
anything *down* (the pool composition shifted slightly). Wall clock rose only 2.1x, not 16.7x.
If `_validity` had any in-n term, the call count would have tracked n. It does not.

**3. Complementary control: fix n, grow collisions.** At n = 500,000, sweeping merges
0 → 499,000:

| merges | `_validity` calls | `dedup()` wall | calls/merge |
|---:|---:|---:|---:|
| 0 | 0 | 2.435s | — |
| 20,000 | 455,606 | 3.850s | 22.78 |
| 100,000 | 2,322,490 | 10.405s | 23.22 |
| 250,000 | 6,116,716 | 26.461s | 24.47 |
| 499,000 | 13,931,836 | 42.575s | 27.92 |

Linear in collisions. Note the count is `~23 × collisions`, never `44 ×` — because `_pick`
early-outs on a missing side at `dedup.py:168-171` before `rank()` is built.

**No quadratic exists anywhere on this path.** `_merge` iterates `set(a) | set(b)`
(`dedup.py:192`), bounded by the field count (~23), not the number of records already merged
into that key. A hot key with k records does k−1 merges of O(fields) each = O(k). The
`source` string grows via `"|".join(sorted(...))` at `dedup.py:174` but is bounded by the
number of distinct source strings (3 in practice). `dedup.py:222-232` is a single linear pass.
I looked for the classic accumulator-quadratic here and it is not present.

## Allocation: O(1) per call, nothing retained

`_validity` allocates at most one stripped string (`dedup.py:125`, `dedup.py:127` — and
`str.strip()` returns the *same object* when there is no surrounding whitespace, and
`str(x)` returns `x` itself for an exact `str`, so realistic clean values allocate nothing) or
one float (`dedup.py:130`, `dedup.py:136`). It holds no state between calls.

Measured: **1,000,000 `_validity` calls → 712 bytes net retained** (`tracemalloc` snapshot
diff). Nothing accumulates.

The memory that *does* scale is elsewhere and is expected: `phone_index`/`name_index`
(`dedup.py:198-199`) and a `dict(record)` per record. Peak `tracemalloc` allocation was
**12.5 MiB at 10k, 121.5 MiB at 100k, 624.2 MiB at 500k** — cleanly linear, and none of it
attributable to `_validity`.

## Concrete timings

`dedup()` end-to-end, 30% duplicate rate, best-of-5, one process:

| n | `_merge` | `_validity` calls | ns per `_validity` call | `dedup()` baseline | `_validity` cost | `_validity` share |
|---|---:|---:|---:|---:|---:|---:|
| 10,000 | 3,000 | 70,708 | 279 ns | **0.141s** | 0.020s | 13.9% |
| 100,000 | 30,000 | 706,680 | 292 ns | **1.477s** | 0.206s | 13.9% |
| 500,000 | 150,000 | 3,536,906 | 327 ns | **8.360s** | 1.157s | 13.8% |

**Scaling is linear:** 10x records → 10.5x time; 5x records → 5.7x time. The per-call cost is
flat at ~280–330 ns, confirming O(1).

Per-branch cost of `_validity` itself (interleaved A/B/A/B, min-of-9):

| branch | ns/call | relative |
|---|---:|---:|
| missing (`None`/`""`) | 128 | 0.54x |
| `source` | 231 | 0.97x |
| `lat` (float) | 216 | 0.90x |
| `scraped_at` | 201 | 0.84x |
| **default (everything else)** | **239** | **1.00x** |
| `rating` (float) | 331 | 1.39x |
| `email` (valid) | 976 | 4.09x |
| `website` (valid) | 1,322 | 5.54x |
| `rating` (non-numeric → `ValueError`) | 2,010 | 8.43x |

The `email`/`website` branches are 4–5.5x the default branch because of the two module-level
compiled regexes (`dedup.py:116-117`) — **which are correctly precompiled at import, so there
is no per-call `re.compile`**. Only ~30% of the ~976ns is the regex; the rest is
`_is_missing` (245ns) plus call overhead. The exception-raising `rating` path is the most
expensive single branch at 2,010ns, but `main.py:90-95` casts `rating` to `float`-or-`None`
on load, so it should be rare in practice.

## Findings

### S3 — `rank()` builds both candidate tuples eagerly, so `_validity` and `str(value)` run even when trust already decided the winner

- **Where:** `dedup.py:187` (`return av if rank(a, av) >= rank(b, bv) else bv`), with
  `rank` at `dedup.py:179-185`.
- **Breaks:** `rank()` is called twice per conflicting field and each call evaluates **all
  four** tuple elements before the comparison happens. Python's tuple `>=` is
  lexicographic and short-circuits at the first differing element, but the code has already
  paid for all four. So `_validity` (10.4% of runtime) and the eager `str(value)`
  (`dedup.py:184`, an unconditional string materialisation for *every* value of *every*
  field on *every* merge) are computed even when `_trust_for` — the first element — already
  decided. Only then does the `>=` discard the work.
- **Trigger:** any conflicting non-missing field where the two records differ in source
  trust. Concrete: `_merge({"name":"X","phone":"1","website":"https://a.example","source":"osm"},
  {"name":"X","phone":"1","website":"https://b.example","source":"osm"})` — trust ties, but
  for `name`, `phone`, `category` etc. any google-vs-osm pair resolves on element 0 while
  still paying for `_validity` on both values.
- **Measured:** at n=500,000 / 150,000 merges, replacing `rank()` with an element-wise
  comparison that stops at the first difference gives **8.396s → 6.661s (1.26x)** in one
  measurement and **8.360s → 5.991s (1.40x)** in another, **byte-identical output on all
  250,000 records** in both. It eliminates **22.1% of all `_validity` calls**
  (706,680 → 550,172).
- **Fix:** replace `rank()` with an element-by-element comparison of the same four keys,
  returning as soon as one differs. Semantics are provably identical because tuple comparison
  is already lexicographic:
  ```python
  ta, tb = _trust_for(field, a), _trust_for(field, b)
  if ta != tb: return av if ta > tb else bv          # decided without touching _validity
  va, vb = _validity(field, av), _validity(field, bv)
  if va != vb: return av if va > vb else bv
  ra = _recency(a) if field in _VOLATILE else ""
  rb = _recency(b) if field in _VOLATILE else ""
  if ra != rb: return av if ra > rb else bv
  return av if str(av) >= str(bv) else bv
  ```

### S3 — `_trust_for` costs 2.7x `_validity` because `_sources_of` reallocates a set per call

- **Where:** `dedup.py:148-152`, calling `_sources_of` at `dedup.py:143-145`.
- **Breaks:** this is the actual hot spot on this path, not `_validity`. Cost attribution at
  n=500,000 / 150,000 merges, by stubbing each function to a constant and re-timing
  (`best-of-5`): baseline `dedup()` **8.058s**; `_validity` stubbed → 7.217s, so `_validity`
  costs **0.842s = 10.4%**; `_trust_for` stubbed → 5.741s, so `_trust_for` costs
  **2.317s = 28.8%**. `_trust_for` is called **twice per conflicting field**
  (`dedup.py:181`, once per candidate) and each call does
  `str(record.get("source") or "")` → `.split("|")` → a set comprehension. That is
  **3,836,906 set allocations at n=500k**, plus an equal number of `dict.get` on
  `_SOURCE_TRUST` per source.
- **Trigger:** any `_merge` of two records with a populated `source` — i.e. essentially
  every merge in the pipeline.
- **Fix:** memoise `_sources_of` on the raw `source` string, or better, precompute each
  record's per-field trust vector once when it enters `dedup()` instead of recomputing it
  23 times per merge. Measured standalone: memoising `_sources_of` alone is **1.10x**;
  combined with the lazy `rank()` above it is **1.45x** (`8.396s → 5.787s`), output
  byte-identical.
- Note: a *semantics-preserving* stub of `_is_missing` could not be measured — replacing it
  with `v is None` changes `""` handling and made the run *slower* (13.244s), which simply
  proves `dedup.py:79-90` is on the critical path. I am not claiming a number for it.

### S3 — `_validity`'s regex cost is linear in value length, and nothing caps that length

- **Where:** `dedup.py:125`, `dedup.py:127`; the unbounded input arrives via
  `main.py:77-105` (`load_master`), which does `csv.DictReader` with no field-length or
  content validation.
- **Breaks:** both patterns are single negated character classes with no nested quantifier, so
  CPython's `sre` handles them in **one linear pass — I found no catastrophic backtracking**.
  Measured `_validity("website", "http://" + "a"*L)`:

  | \|v\| | ns/call | ns/char |
  |---:|---:|---:|
  | 57 | 895 | 15.7 |
  | 1,007 | 11,260 | 11.2 |
  | 20,007 | 211,582 | 10.6 |
  | 100,007 | 955,765 | 9.6 |

  Flat `ns/char` = linear. I also probed six adversarial shapes at len=512 — `"a"*512`
  (8.2µs), local-part/tail without a dot (7.5µs), 255 `a@` pairs (0.6µs), `"@"+"a"*510`
  (0.4µs), `"."*512` (6.4µs), interleaved spaces (0.7µs). Worst case is ~8.2µs, i.e. still
  linear with a small constant; there is no ReDoS here.

  The residual exposure is a **pathologically long field**, not a crafted pattern. If every
  `website` field were 100KB, the 3,536,906 calls at n=500k would cost **3,729s (62 min)**;
  at a 20KB field, **717s (12 min)**. A 1KB field costs 33.8s — survivable.
- **Trigger:** `data/all_businesses.csv` is a cumulative master round-tripped through Google
  Drive (`main.py:209`, `.github/workflows/scrape.yml`) and re-read verbatim by
  `load_master`. A single corrupted cell (a truncated multi-KB paste, a mangled encoding, a
  hand-edit in Sheets) reaches `_validity` uncapped.
- **Fix:** cap the scanned length in `_validity` — `str(value).strip()[:512]` before
  `match()` — which is free for all realistic values and bounds the worst case. `[:512]` on an
  already-short string still allocates a copy; guard with a length test, or bound at the
  `load_master` boundary where it also protects the CSV writer.

### S3 — `_is_missing` is computed up to 3x per value

- **Where:** `dedup.py:168` and `dedup.py:170` (two calls inside `_pick`), then again at
  `dedup.py:122` inside `_validity`.
- **Breaks:** pure redundancy. At n=500,000 / 150,000 merges, `_is_missing` is called
  **9,094,593** times versus `_validity`'s **3,536,906** — a ratio of 2.57, matching
  `2 × _pick_fields + _validity` exactly. Each call costs ~200–245ns and most hit the
  `isinstance(value, str)` + `value.strip()` path (`dedup.py:88-89`).
- **Why S3 not higher:** the S3 lazy-`rank()` fix above already removes `_validity` from 22%
  of conflicts, and folding the `_pick` early-outs into a single computed pair of flags would
  remove at most another ~2 calls per conflicting field. Not worth its own change; do it if
  you are already in the function.
- **Fix:** hoist to `am, bm = _is_missing(av), _is_missing(bv)` once in `_pick` and pass the
  booleans down.

## Not a bug, but worth knowing

- **`_validity` is correctly micro-optimised already.** `_EMAIL_OK` and `_URL_OK` are compiled
  once at import (`dedup.py:116-117`). There is no per-call `re.compile`, and no
  `re.compile` cache lookup (which `normalize_phone` at `dedup.py:44` *does* pay — `re.sub`
  with a string pattern hits `re._compile`'s cache every call — but that is outside this
  function's scope and is an order of magnitude below the merge path).
- **`_validity`'s `-100` sentinel for missing is a fixed-width int, so it is never a
  comparison hazard.** `_is_missing` returns before any `field ==` dispatch, which is why
  the missing branch is the *cheapest* at 128ns — the common case for sparse lead-gen data is
  already fast-pathed.
- **The `if field in ("lat", "lon")` tuple membership (`dedup.py:128`) builds a constant
  tuple, not a list** — CPython folds this to a constant, so it is free. Not worth changing.
- **`dedup()` is not your bottleneck.** At 500k records it completes in **8.4s**.
  `enricher.check_websites` (`enricher.py:225-276`) issues one GET per website at 40 workers;
  at 200k websites and ~150ms/response that is ~12.5 minutes, roughly **90x** the entire dedup
  pass. Optimising `_validity` moves a rounding error. Fix the crawl or its timeout budget
  before touching this file.
- **Steady-state accumulation actually protects you.** `main.py:179-180` dedups
  `raw_filtered + master`. Because `_validity` cost tracks *collisions* and not n, a run that
  loads a 500k master but scrapes only a few thousand new rows pays for only those few
  thousand collisions. The growth term you should worry about is the crawl, not the merge.

## Recommended order of work

1. **Nothing, for correctness or capacity reasons.** `_validity` is O(1) in n, allocates
   nothing proportional, and `dedup()` finishes 500k records in 8.4s. There is no perf bug
   here.
2. **Lazy short-circuit `rank()` at `dedup.py:187`** — 1.26–1.40x, byte-identical output,
   eliminates 22.1% of `_validity` calls, and is a strict improvement with no semantic risk.
   Verified equivalent: tuple comparison is already lexicographic.
3. **Then memoise or precompute `_sources_of` (`dedup.py:143-152`)** — the real hot spot at
   28.8%. Together with #2: 1.45x.
4. **Cap scanned field length at ~512 chars** (`dedup.py:125`, `dedup.py:127`, or at the
   `load_master` boundary `main.py:77-105`) — pure defence-in-depth against a corrupted
   master CSV; no benefit for clean data.
5. **Fold the duplicated `_is_missing` calls** (`dedup.py:168`, `dedup.py:170`, `dedup.py:122`)
   if already editing `_pick`. Lowest value of the four.
