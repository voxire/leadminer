# 317 — `_sources_of` (dedup.py:143) perf review

## Verdict

`_sources_of` is **not a performance problem**. It is O(k) in the token count of the
`source` string and O(1) in the number of businesses, it is called exactly
Θ(merges) times — **zero calls when zero records merge** — and it accounts for
**9.6%** of `dedup()` runtime, which is itself **~0.1%** of the pipeline's worst
case. There is no n² anywhere in this function or its call path; the memory it
allocates is flat at 0.511 KB/record from 50k to 500k. I found **no S1**.

The one real defect I did find is not about speed: `dedup.py:144` accepts an
arbitrary-length `source` string and re-splits it ~2.3M times at 500k, costing
~1000× more per token than a well-formed value, and because `dedup.py:174`
*unions* rather than intersects, a malformed value is **written into the
cumulative master and never heals**. That is the finding worth acting on.

---

## Method and honesty about the numbers

**Measurement caveat, stated up front.** The box was heavily contended for the
whole session (loadavg **70–113 on 12 cores**, `rustc` at 400% from concurrent
agents). Absolute wall-clock seconds are therefore unreliable — I measured the
*identical* baseline at 3.49s and 6.42s in two runs 20 minutes apart. Every
number below is one of:

- **(a) deterministic counts** — instrumented call counts and `tracemalloc` bytes.
  These are exact and load-independent, and they carry the complexity claims.
- **(b) median of paired ratios** — baseline and variant timed back-to-back in
  the same process, drift cancels. Ranges and stdev shown.
- **(c) best-of-N wall clock**, loadavg printed next to it.

Fixture: 23-field `BusinessRecord`-shaped dicts (the full `FIELDS` list,
`main.py:33-40`), 50% of fields `None`, phone-keyed dupes with conflicting
`source` values. No network, no scrapers, no repo mutation — monkeypatched in
memory only, benchmark scripts kept outside the repo in the opencode temp dir.

---

## Complexity: O(1) in n, O(k) in token count, never O(n²)

```python
143: def _sources_of(record: dict) -> set[str]:
144:     raw = str(record.get("source") or "")
145:     return {s for s in raw.split("|") if s}
```

Per call: one `dict.get`, one `or`, one no-op `str()` (returns the same object
for a `str`), one `split("|")` allocating a list of k+1 substrings, and a set of
≤ k unique members. So **O(k+1) time, O(k) allocation**, where `k` = number of
`|`-separated tokens.

In the number of businesses `n`: **O(1) per call**. The aggregate is governed by
the call count, which is a *deterministic* function of merge count — and this is
where I can settle the question with certainty rather than estimation.

### Exact call counts at 500k (instrumented, load-independent)

| input `n` | dup ratio | merges (`n − M`) | `_sources_of` calls | per record | per merge |
|---|---|---|---|---|---|
| 500,000 | 0.0 | 0 | **0** | 0.00 | — |
| 499,999 | 0.5 | 126,094 | 1,520,432 | 3.04 | 12.06 |
| 500,000 | 1.0 | 191,211 | 2,295,072 | 4.59 | 12.00 |
| 499,998 | 2.0 | 259,575 | 3,097,708 | 6.20 | 11.93 |

Zero-duplicate fixture, the brief's three scales:

| `n` | merges | `_sources_of` calls |
|---|---|---|
| 10,000 | 0 | **0** |
| 100,000 | 0 | **0** |
| 500,000 | 0 | **0** |

**This is a proof, not an estimate.** Calls = exactly `c · merges` with `c ≈ 12`
at 50% field density, and `merges = n − M ≤ n`. Therefore

```
total_cost = c · (n − M) · O(k)  =  O(n · k)
```

Strictly linear in `n`, and identically **zero** when `M = n`. There is no
possible n² term in this function or its call path. The apparent superlinearity
I saw in early wall-clock runs (10k→100k taking 12–18× for 10× n) was load drift
and cache pressure at a 300 MB working set, not algorithmic — the fine-grained
curve in §"Timings" settles it at ~2× per 2× n.

### Where the 12 comes from, and its ceiling

`c` is data-density dependent, because `dedup.py:168-171` short-circuits before
`rank` when either value is missing. Measured at `n=100,000`, 1:1 dup:

| fields `None` | merges | calls | per record | per merge |
|---|---|---|---|---|
| 85% | 38,027 | 254,592 | 2.55 | 6.70 |
| 55% | 38,299 | 460,998 | 4.61 | 12.04 |
| 25% | 38,314 | 665,808 | 6.66 | 17.38 |
| 0% | 38,035 | 828,908 | 8.29 | 21.79 |

Hard ceiling: `2 × 23` contended fields (`_pick` calls `rank` twice,
`dedup.py:187`) `+ 2` from the `"source"` branch (`dedup.py:174`) = **48 calls per
merge**. So the worst case is `48(n − M)`, still linear.

### Allocation is proportional, with a flat per-record constant

`tracemalloc` across the brief's range (1:1 dup, 50% sparse):

| `n` | peak | retained | KB/record retained |
|---|---|---|---|
| 50,000 | 32.1 MB | 26.3 MB | 0.513 |
| 200,000 | 124.5 MB | 104.8 MB | 0.512 |
| 500,000 | **305.8 MB** | **261.8 MB** | **0.511** |

Per-record bytes are flat to three significant figures over a 10× range. Memory is
linear with no multiplier creep. At 500k the whole of `dedup()` peaks at ~306 MB,
which is not a memory concern on any realistic runner.

---

## Timings

Best-of-N, `gc` disabled. Loadavg printed per row because it was 84–113.

### `dedup()` end to end

| `n` | no duplicates | 1:1 dup ratio (master accumulation) |
|---|---|---|
| 10,000 | 0.091s (110k rec/s) | 0.370s (27.1k rec/s) |
| 100,000 | 1.093s (91.5k rec/s) | 3.967s (25.2k rec/s) |
| 500,000 | **4.924s** (101k rec/s) | **26.164s** (19.1k rec/s) |

The dup-heavy column is 5.3× the clean column because `_merge` (`dedup.py:190`)
runs 191,211 times instead of zero — this is the only lever that moves `_sources_of`,
and it is data-driven, not code-driven.

### Fine-grained n curve (1:1 dup, 50% sparse, best-of-2)

| `n` | wall clock | multiplier | ratio for doubling `n` |
|---|---|---|---|
| 20,000 | 0.695s | — | — |
| 40,000 | 1.471s | 2.12× | 2.12 |
| 80,000 | 2.407s | 3.46× | 1.64 |
| 160,000 | 4.991s | 7.18× | 2.07 |
| 320,000 | 11.255s | 16.19× | 2.26 |
| 500,000 | 18.214s | 26.20× | 1.62 |

Roughly 2× per 2× n at every step. Linear, with mild cache degradation at the
top end. **Not quadratic.** (Absolute values are ~2.5× lower on an idle box.)

### Isolated per-call cost of `_sources_of`

| k tokens | `len(source)` | unloaded | contended (loadavg ~85) |
|---|---|---|---|
| 1 | 7 | 371 ns | 737 ns |
| 2 | 15 | 380 ns | 847 ns |
| 3 | 23 | 432 ns | 2,363 ns |
| 10 | 79 | 993 ns | 2,318 ns |
| 100 | 889 | 8,807 ns | 29,166 ns |
| 1,000 | 9,889 | 114,304 ns | 251,258 ns |
| 10,000 | 108,889 | — | **3,363,165 ns** |

Linear in `k` at ~330 ns/token, with a fixed ~370 ns call overhead. A well-formed
record (k ≤ 3) costs ~0.4 µs; a malformed one at k=10,000 costs 3.4 ms — a
**4,565× amplification** on a single call.

### Where the time actually goes at 500k (cProfile, n=500,000, 1:1 dup)

| rank | function | line | ncalls | cumtime | % of 62.06s |
|---|---|---|---|---|---|
| 1 | `_merge` | 190 | 191,417 | 50.703s | 81.7% |
| 2 | `_pick` (tottime) | 159 | 4,402,591 | 10.505s | 16.9% |
| 3 | `rank` | 179 | 2,111,104 | 24.715s | 39.8% |
| 4 | `_trust_for` | 148 | 2,111,104 | 12.654s | 20.4% |
| **5** | **`_sources_of`** | **143** | **2,300,574** | **5.962s** | **9.6%** |
| 6 | `_is_missing` | 79 | 9,108,608 | 9.072s | 14.6% |
| 7 | `_validity` | 120 | 2,111,104 | 6.052s | 9.8% |
| 8 | `normalize_phone` | 33 | 375,320 | 3.795s | 6.1% |

Call chain is `_merge` → `_pick` → `rank` → `_trust_for` → `_sources_of`.
`_sources_of` is the **leaf** and the cheapest link. 92% of its calls (2,111,104 of
2,300,574) come from `_trust_for` (`dedup.py:150`); the remaining 8% from the
`"source"` branch (`dedup.py:174`).

---

## Findings

### S1 — none

`_sources_of` cannot produce a wrong result, cannot exhaust memory, and cannot
blow up superlinearly in `n`. Zero duplicates means zero executions. I looked
hard for an S1 and there isn't one; inventing one would waste a day of the
implementer's time.

### S2 — `source` token count is unbounded, unvalidated, and permanently persisted

- **Where:** `dedup.py:144-145` (splits anything), `dedup.py:174` (unions,
  never intersects), `main.py:209` (writes it out), `main.py:150` +
  `load_master` `main.py:86-104` (reads it back verbatim).
- **Breaks:** `raw.split("|")` is linear in the string length with no cap, and
  the cost of a call grows ~330 ns per token. Worse, the damage is *permanent*:
  `_pick` at `dedup.py:174` computes `"|".join(sorted(_sources_of(a) | _sources_of(b)))`,
  a **union**. A junk token in the master therefore survives every future merge
  and is re-split on every subsequent merge touching that record, forever, across
  every run. Measured: poisoned rows in → poisoned rows out, 222→194 (k=3) and
  214→186 (k=3000). Nothing in the pipeline prunes unknown source names.
- **Concrete input:** hand-edit one row of `data/all_businesses.csv` so its
  `source` cell is `osm|legacy_tag|legacy_export_2024|...` with 3,000 tokens
  (≈109 KB in one cell). On the next run, `_sources_of` costs 743 µs per call
  unloaded / 3.4 ms contended on that record. At ~12 calls per merge and ~2.3M
  calls at 500k, if 0.4% of records (2,000) carry such a value, that is
  2,000 × 12 × 743 µs ≈ **18 seconds of pure `split()` on that one column** —
  and it recurs on every run, because `load_master` reads the poison straight
  back in. `set(a) | set(b)` guarantees the token count cannot shrink, and the
  union means a bad token can only ever be added to.
- **Fix:** cap and sanitise at the boundary — in `_sources_of`, intersect against
  `_SOURCE_TRUST`'s keys (or a module-level `_KNOWN_SOURCES` frozenset) so
  unknown tokens are discarded at parse time rather than unioned forward; and
  have `write_csv`/`load_master` normalise the `source` column on write so the
  master cannot carry junk forward. That converts an unbounded, permanent,
  amplifying field into a bounded, self-healing one.

Severity defended as S2 not S1: it degrades cost and operability, but linearly,
and it is data-dependent — no valid scraper output triggers it today. It becomes
S1-adjacent only in the sense that it silently persists in the cumulative asset
and there is zero validation of that column anywhere in the repo.

### S2 — the only genuine n² in the system is the cumulative master, not this function

- **Where:** `main.py:179` — `combined = raw_filtered + master`, then
  `dedup(combined)` at `main.py:180` re-processes the **entire accumulated**
  master on every run.
- **Breaks:** per-run cost is O(n) (proved above), but if the master grows by a
  roughly constant δ per run then `n_k ≈ k·δ` and total work over K runs is
  `Σ k·δ = O(K²δ) = O(n²)`. That is a real quadratic, and it is the one thing in
  this code path that will actually hurt at 500k. It surfaces through `dedup()`
  but is a `main.py` design property.
- **Trigger:** 30 runs of `main.py`, each adding ~16k new businesses. Run 30
  re-dedups 500k records (≈4.9s clean, ≈26s contended) instead of the ~16k it
  actually added (≈0.09s). Cumulative CPU across the 30 runs ≈ 9× the sum of the
  incremental work.
- **Fix:** give `dedup()` an optional `existing: dict` index so the master is
  loaded into the phone/name indexes once (`O(n)` once) rather than re-merged
  into itself every run; or make the master a keyed store (sqlite/Parquet) and
  dedup only the delta. Out of scope for `_sources_of`, but it is the change
  that actually moves the 500k number, and `dedup.py:198-199` is where it lands.

### S3 — 2.3M fresh list+set allocations for a 7-valued lookup table

- **Where:** `dedup.py:144-145`, reached 2,300,574 times at `n=500,000` with a
  1:1 duplicate ratio.
- **Breaks:** every call allocates a fresh list of k+1 substrings and a fresh
  set, to compute one of at most **7 distinct values** (`2³ − 1` unions of the
  three scraper names at `dedup.py:97-110`). 9.6% of `dedup()` runtime is spent
  rebuilding a constant. Not an operability problem — `dedup()` at 500k is
  ~5–26s, dwarfed by `enricher.py:225`'s 40-thread HTTP stage — but it is free
  money and this is the cheapest 10% in the module to claim.
- **Trigger:** any run where the master is large and overlaps the new scrape.
  500k with ~190k merges → 2.3M calls → ~0.92s of `split()`.
- **Fix (the single change with the largest speedup):** memoise the *trust
  table* on the source **string**, not on the record. Strings are hashable;
  records are not, so `_sources_of` cannot itself be cached on its argument —
  but its only consumer can be:

  ```python
  _TRUST_BY_SOURCE_STRING: dict[str, dict[str, int]] = {}

  def _trust_for(field: str, record: dict) -> int:
      raw = str(record.get("source") or "")
      table = _TRUST_BY_SOURCE_STRING.get(raw)
      if table is None:
          table = _TRUST_BY_SOURCE_STRING[raw] = {
              f: max([_DEFAULT_TRUST] + [_SOURCE_TRUST.get(s, {}).get(f, _DEFAULT_TRUST)
                                        for s in raw.split("|") if s])
              for f in _MERGE_FIELDS
          }
      return table.get(field, _DEFAULT_TRUST)
  ```

  Keeps `_sources_of` exactly as-is for `dedup.py:174` (which needs the set) and
  removes it from the hot path entirely.

  **Measured, paired A/B, n=40,000, 15 rounds: 1.097× median, range 0.97–1.79,
  stdev 0.206.** A second paired run at n=100,000, 9 rounds gave 1.309× median,
  range 1.06–1.46. **Output is byte-for-byte identical** to the original on a
  50,000-in / 30,894-out fixture (`base_out == new_out` → `True`).

  Note this matches its own theoretical ceiling: `_sources_of` is 9.6% of
  `dedup()` per cProfile, so eliminating it entirely cannot beat ~1.107×. The
  measured 1.097× is essentially the whole win, which is a good sign the
  measurement is trustworthy despite the noise. Scope the memo dict to a single
  `dedup()` call so it cannot grow unbounded on a poisoned `source` column.

---

## Candidates I measured and rejected

Reporting these so nobody re-runs them. All paired or best-of-N on the same
fixture.

| Change | Result | Verdict |
|---|---|---|
| Memoise `_trust_for` on the source **string** | **1.097–1.309×**, identical output | **Take it** |
| Memoise `_sources_of` on `id(record)` | 1.19× | **Rejected** — `id()` is reused after GC → silently wrong trust values. A correctness bug, not a speedup |
| Memoise `_trust_for` on `id(record)` | 0.87× | Slower, same `id()` hazard |
| Per-record `{field: trust}` dict computed once (22 fields, 23 keys) | **0.31×** — 3× *slower* | You pay 23 dict inserts per record to save ~23 lookups |
| Trust-delta early exit (compare trust, then validity, then recency, instead of building both tuples) | 0.63× | Slower; two Python-level branches cost more than one C-level tuple compare |
| Drop `set(a) \| set(b)` from `_merge` (`dedup.py:192`), iterate `a` then `b`-only | 0.85–0.86× | Slower; the set union is one C-level op |
| `_pick` early-out when `av == bv` | 0.94–1.50×, inconsistent across runs | Real effect on real data but I could not measure it reliably under this load. Worth doing for correctness-adjacent reasons, not on this evidence |
| Inline `_is_missing` into `_pick` + hoist `rank` out of `_pick` (`dedup.py:179`) | 1.195× (range 0.89–1.32) in one run, 0.93× in another | Directionally the biggest lever left — `_is_missing` is 9.1M calls / 14.6% at 500k — but I could not separate it from noise. `rank` being a nested closure allocated 2,111,104 times per 500k run is real waste; measure it on an idle box before committing |

---

## Not a bug, but worth knowing

- **`_sources_of` is free when nothing merges.** Zero duplicates → literally zero
  executions at any `n`. If you ever profile this function showing up in a hot
  path, the cause is duplicate volume, not this code.
- **Call count is a function of *merge* count, not record count.** 12 calls per
  merge at 50% density, ceiling 48. So the lever is "reduce how many records
  overlap between the master and the new scrape", which is a data-collection
  question, not a code question.
- **`_sources_of` cannot be memoised on its argument** — `dict` is unhashable.
  Any caching has to key on the source string or an id, and the id route is
  unsound. That constraint is what forces the `_trust_for` fix rather than a
  `_sources_of` fix.
- **`rank` at `dedup.py:179` is a nested function**, so CPython allocates a fresh
  closure object on every one of the 2,111,104 calls per 500k run. Hoisting it to
  module level is free and correct; it just didn't measure as a win on a box
  this noisy.
- **Pipeline context, so nobody over-invests here.** `enricher.py:225` runs
  `check_websites` at 40 workers with `timeout=8` (`enricher.py:175`). At 500k
  records, even 100k websites is `100,000 / 40 × 8s ≈ 20,000s ≈ 5.5 hours`
  against `timeout-minutes: 300` in `.github/workflows/scrape.yml:25`. `dedup()`
  at 26s is ~0.1% of that. **The scaling wall for this system is the HTTP
  enrichment stage, not dedup.** Optimise there first; the S3 above is a freebie
  to be taken when convenient, not a fire.