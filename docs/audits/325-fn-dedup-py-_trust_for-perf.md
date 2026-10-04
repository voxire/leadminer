# 325 — `_trust_for` is O(1) but does a third of `dedup()`'s work; it re-parses `record["source"]` 33× per merge

## Verdict

`_trust_for` (`dedup.py:148`) is **O(1) in the number of businesses**, and `dedup()` as a whole
is **O(n) — there is no quadratic behaviour to remove**. The problem is the multiplier: the same
`record["source"]` string is `split()` into a throwaway `set` **33 times per merge** (18–22 times
per input record) because the lookup is driven from inside the per-field loop in `rank`
(`dedup.py:179-185`) rather than once per record. That redundant work is **33% of `dedup()`'s wall
clock** at every size I measured, and each call throws away 6–8 heap objects.

The single change with the largest speedup is to **hoist the per-record trust computation out of
the per-field loop** — one memoized trust row per merge side instead of 33 lookups. Measured on a
500k-record corpus: **48.5 s → 20.3 s (2.38×) with byte-identical output.**

**No S1.** Nothing here produces a wrong answer, loses data, or crashes. This is an S2 cost
problem, and it is a *small* one relative to the pipeline (see "Not a bug, but worth knowing" #3).

---

## Findings

### S2 — `_trust_for` re-parses `record["source"]` once per field instead of once per record

- **Where:** `dedup.py:148-152` (the function), `dedup.py:143-145` (`_sources_of`, called from
  `dedup.py:150`), and the closure `rank` at `dedup.py:179-185` that invokes it once per field
  from `dedup.py:187`.
- **Breaks:** Nothing is wrong with the *answer*. The problem is that the input is a pure
  function of a string with **at most 8 distinct values in the entire corpus** — the three real
  sources written at `scrapers/osm.py:119`, `scrapers/google_places.py:302`,
  `scrapers/wikidata.py:127`, and merged records are just `"|".join(sorted(...))` of a union at
  `dedup.py:174`. So 99.96% of the work is recomputing a value already computed 32 times a
  microsecond earlier.

  The call count is structural, not corpus-dependent. At 100,000 records / 54,937 merges:

  | symbol | calls | per merge | per input record |
  |---|---|---|---|
  | `_pick` (`dedup.py:159`) | 1,263,551 | 23.0 | 12.6 |
  | `rank` (`dedup.py:179`) | 1,820,946 | 33.15 | 18.2 |
  | `_trust_for` (`dedup.py:148`) | 1,820,946 | 33.15 | 18.2 |
  | `_sources_of` (`dedup.py:143`) | 1,930,820 | 35.1 | 19.3 |

  The 23.0 is `len(set(a) | set(b))` at `dedup.py:192` — the 23-key `BusinessRecord`
  (`scrapers/base.py:5-29`). The 33.15 is 23 fields × 1.44 fields-per-pair where both sides are
  populated (`_pick` short-circuits at `dedup.py:168`/`dedup.py:170`), times 2 sides. A second
  independent corpus gives 33.0/merge at both 10k (219,906 / 6,667) and 100k (2,198,786 /
  66,667) — the factor is 33, not corpus-specific.

- **Cost:** `cProfile` of `dedup.dedup()` on 100,000 records — 15.542 s profiled (8.79 s
  unprofiled), `_trust_for` **cumtime 5.146 s = 33.1%**. `_sources_of` accounts for 1,930,820 of
  those calls and 2.075 s of the 5.146 s. Attributing the measured 806 ns/call directly to wall
  clock gives a `_trust_for` share of **30.9% / 32.4% / 34.3%** at 10k / 100k / 500k — the share
  *grows* with scale, which is the part worth acting on.

  Scaled linearly to the 500k corpus: ~9.1M calls, **~16 s of the 48.5 s table below**.

- **Trigger (concrete, copy-pasteable):**
  ```python
  import sys; sys.path.insert(0, '.')
  import dedup
  recs = build(500_000)                      # 23-field records, 3 real source names
  o, n = dedup._trust_for, [0]
  def counted(f, r):
      n[0] += 1; return o(f, r)
  dedup._pick.__globals__["_trust_for"] = counted   # rank shares the module globals
  out = dedup.dedup(recs)
  # len(recs)=500000, merges=274828, len(out)=225172, n[0] == 9_070_000 (approx)
  ```
- **Fix:** compute the record's trust row once per merge, not once per (record, field). Full
  patch in "Recommended order of work" step 1. **Measured 2.38× at 500k, byte-identical output.**

  ⚠️ Do *not* try this as `@functools.lru_cache` directly on `_trust_for`: its second argument is a
  `dict`, which is unhashable, so the decorator raises `TypeError` on the first call. The cache has
  to key on the `source` string.

### S2 — `_merge` allocates a fresh 23-key dict, and `_pick` allocates a fresh closure, for every merge

This is the co-located allocation churn that the fix above also removes.

- **Where:** `dedup.py:191` (`merged = {}`), `dedup.py:192` (`for key in set(a) | set(b)`),
  `dedup.py:179-185` (`rank` defined *inside* `_pick`), `dedup.py:190-194` (`_merge`).
- **Breaks:** two allocation sources sit directly on the merge hot path.
  1. **`_merge` builds a brand-new dict per collision.** At 500k records that is 274,828 dicts ×
     23 slots. A 23-entry CPython dict is ~1.2 KB, so ~330 MB of transient dict memory — the bulk
     of the RSS below.
  2. **`_pick` rebuilds the `rank` closure on every call** that reaches the ranking branch.
     Confirmed in the Python 3.14.8 bytecode for `_pick`: offsets 306–308 are
     `LOAD_CONST <code object rank ...>` → `MAKE_FUNCTION` → `SET_FUNCTION_ATTRIBUTE closure`,
     executed *inside* the function body. That is 910,473 function objects at 100k records,
     **~4.9M at 500k**. `rank` closes over `field` only, so it must be rebuilt per field — but
     its body is four dictionary/set lookups with no other captured state, so it belongs at
     module level as `_rank(field, rec, value, trow)`.

- **Measured memory (macOS, `ru_maxrss` in bytes):**

  | records | corpus RSS before `dedup()` | peak RSS after | `dedup()`'s own delta |
  |---|---|---|---|
  | 100,000 | 126 MB | 176 MB | **+50 MB** |
  | 500,000 | 571 MB | 798 MB | **+227 MB** |

- **Measured cost:** the symptom is not super-linear behaviour, it is **per-record cost drifting
  upward**: 69.9 → 87.9 → **97.1 µs/record** for 10k → 100k → 500k. A 1.39× drift over a 50×
  size increase is allocator + GC pressure on those intermediate dicts, not algorithmic growth —
  but it is real, and it is what the +227 MB buys. The variant in step 1 (which promotes `rank`
  to module scope *and* hoists the trust row) removes both sources.
- **Trigger:** `dedup.dedup(build(500_000))` → 48.5 s, 798 MB peak RSS.
- **Fix:** same change as the S2 above.

### S3 — `_SOURCE_TRUST.get(src, {})` allocates a throwaway empty dict on every loop iteration

- **Where:** `dedup.py:151`.
- **Breaks:** the `{}` default argument to `.get` is evaluated **eagerly**, once per iteration of
  `for src in _sources_of(record)` at `dedup.py:150`, whether or not the key is present.
  Disassembling `_trust_for` on Python 3.14.8 shows `BUILD_MAP` at offset **86**, inside the loop
  body (offsets 36–150) — no constant folding is possible because dicts are mutable. For a merged
  3-source record that is 3 empty dicts per call: **~9.1M calls × 2–3 ≈ 20–27M throwaway dicts at
  500k records.**
- **Trigger:** `ST.get("osm", {})` = **202 ns** vs `ST.get("osm", EMPTY)` = **171 ns**
  (best of 5 × 1,000,000 iterations) → ~31 ns per source. At 9.1M calls × 2 sources that is
  **~0.56 s** at 500k. Individually trivial; free to fix, so fix it.
- **Fix:** `_EMPTY: dict[str, dict[str, int]] = {}` beside `_DEFAULT_TRUST` at `dedup.py:111`,
  then `_SOURCE_TRUST.get(src, _EMPTY)`. Or `(ST.get(src) or _EMPTY).get(field, _DEFAULT_TRUST)`
  — 162 ns, marginally faster still, since it skips the eager default on a hit.

---

## Measurements

**Environment:** Python **3.14.8**, macOS arm64. The project targets 3.12, so absolute
nanoseconds will move (3.12 is typically 10–25% slower on this workload).

**Corpus:** 23-field `BusinessRecord` dicts; each business emitted by 1–3 of the three real
source names; ~30% phone-less (so keyed on `(name, city)`); field fill per
`scrapers/base.py`; ~2 records per unique business. The duplication ratio is the parameter that
moves absolute time most — at zero duplicates `_trust_for` is never called at all.

### Full `dedup()` — original vs. hoisted-trust-row variant

| records | unique out | merges | orig median | orig min | orig µs/rec | variant median | **speedup** | output identical |
|---|---|---|---|---|---|---|---|---|
| 10,000 | 4,549 | 5,451 | 0.699 s | 0.666 s | 69.9 | 0.458 s | **1.53×** | ✅ |
| 100,000 | 45,063 | 54,937 | 8.792 s | 7.130 s | 87.9 | 4.388 s | **2.00×** | ✅ |
| 500,000 | 225,172 | 274,828 | 48.527 s | 42.465 s | 97.1 | 20.349 s | **2.38×** | ✅ |

Median of 3 runs, same corpus for both, `identical` = `list` of dicts compared with `==`.
A second, larger synthetic corpus (999,456 records → 655,947 unique) reproduced the direction:
2.18×, identical output.

### Microbenchmark — `_trust_for` and its parts

Best of 5 × 1,000,000 iterations, `record = {"source": "google_places|osm"}`, `field="website"`:

| expression | ns/call |
|---|---|
| `_trust_for` as-is (`dedup.py:148`) | **806 – 1,788** (run-to-run spread, see caveat) |
| `_sources_of` alone (`dedup.py:143`) | 596 – 860 |
| `_SOURCE_TRUST.get(s, {})` per source | 202 |
| `_SOURCE_TRUST.get(s, _EMPTY)` per source | 171 |
| `lru_cache(raw_source, field)` replacement | **302** |

So `_sources_of` is 40–70% of `_trust_for`, and the *actual trust lookup* is a small slice of
the remainder. A per-`(source_string, field)` memo alone measured 302 ns vs 806 ns in a quiet
run — **2.7× on the function**, but only **1.11–1.31× on `dedup()` as a whole**, because it
leaves the closure allocation and the redundant `dict.get` traffic in place. That is why
"hoist it out of the field loop" beats "cache it in place" as the recommendation.

### Cache cardinality

Hit rate **99.96%** (2,198,696 / 2,198,786 at 100k records; 219,816 / 219,906 at 10k). Bounded at
**≤ 8 distinct source strings × 23 fields ≈ 184 entries** — O(1) memory, independent of n.
The memo is *not* an O(n) leak.

---

## Not a bug, but worth knowing

1. **`_trust_for` is O(1) in n, and so is `dedup()` overall. This lens found no O(n²).**
   `_trust_for`'s cost depends only on the length of `record["source"]`, capped at 3 by
   `dedup.py:174`. `dedup()` walks `records` once (`dedup.py:201`); each `_merge` is O(23) over
   `set(a) | set(b)` (`dedup.py:192`); merges are bounded by n−1; the tail at `dedup.py:222-230`
   is two linear passes plus O(1) amortized set lookups. Worst case is
   `33 × (n−1)` `_trust_for` calls ≈ **16.5M at 500k** — linear.

2. **The 33× multiplier is structural and will not drift** unless the `BusinessRecord` schema
   grows. 23 fields × ~1.44 both-populated fields per pair × 2 sides.

3. **Dedup is not the pipeline bottleneck at 500k — size the priority accordingly.**
   48.5 s for 500k records is ~3% of `enricher.check_websites` on the same rows: one HTTP GET per
   website at 40 workers (`enricher.py:237`) with an 8 s timeout (`enricher.py:175`). At 40% of
   500k rows carrying a website and 300 ms/request, that is ≈ 200k / 40 × 0.3 s ≈ **25 minutes** —
   31× dedup. Worth doing (one hour, measured 2.4×, zero behaviour change), but do not let it
   consume S1 effort, and do not confuse "dedup took 48 s" with "dedup is the problem".

4. **Measurement caveat on the micro numbers.** Run-to-run spread on this machine reached 2.2×
   on the microbenchmarks (`_trust_for` measured anywhere from 806 ns to 1,788 ns on identical
   input, under varying machine load). Treat the micro figures as **proportions**, not budgets.
   The full-`dedup()` medians-of-3 were stable to ±15% and are the numbers to plan against. The
   33.1% `_trust_for` share is confirmed two independent ways (`cProfile` cumtime, and
   attributed wall clock), so it is not load noise.

5. **Don't fix this with in-place mutation of the surviving record.** An obvious-looking
   alternative — merging into the existing dict instead of allocating a new one at `dedup.py:191`
   — would remove the 330 MB of transient dicts but silently changes `_merge`'s contract (input
   records would be mutated). `dedup()` happens to always pass a fresh `dict(record)`
   (`dedup.py:210`/`dedup.py:218`) so it would be safe *today*, but it becomes a footgun the
   moment anyone calls `_merge` from elsewhere. The step-1 patch gets the same win without the
   aliasing change.

---

## Recommended order of work

### 1. Hoist the per-record trust row out of the per-field loop — `dedup.py:148-194`

The single change with the largest speedup. Compute the trust row once per merge side, and promote
`rank` to module scope:

```python
# beside _DEFAULT_TRUST at dedup.py:111
_EMPTY: dict[str, dict[str, int]] = {}

@functools.lru_cache(maxsize=None)
def _trust_row(raw_source: str) -> dict[str, int]:
    """field -> trust for one record. Computed once per merge instead of once per field."""
    row: dict[str, int] = {}
    for src in raw_source.split("|"):
        for field, value in _SOURCE_TRUST.get(src, _EMPTY).items():
            if value > row.get(field, _DEFAULT_TRUST):
                row[field] = value
    return row

def _trust_for(field: str, record: dict) -> int:
    """Same result, now a single dict lookup. Hot path in _merge uses _trust_row directly."""
    return _trust_row(str(record.get("source") or "")).get(field, _DEFAULT_TRUST)

def _rank(field, rec, value, trow) -> tuple:      # was the closure at dedup.py:179
    return (
        trow.get(field, _DEFAULT_TRUST),
        _validity(field, value),
        _recency(rec) if field in _VOLATILE else "",
        str(value),
    )

def _merge(a: dict, b: dict) -> dict:            # dedup.py:190
    merged = {}
    ta = _trust_row(str(a.get("source") or ""))
    tb = _trust_row(str(b.get("source") or ""))
    for key in set(a) | set(b):
        av, bv = a.get(key), b.get(key)
        if _is_missing(av):      merged[key] = bv; continue
        if _is_missing(bv):      merged[key] = av; continue
        if key == "source":      merged[key] = "|".join(sorted(_sources_of(a) | _sources_of(b))); continue
        if key == "scraped_at":  merged[key] = max(av, bv); continue
        merged[key] = av if _rank(key, a, av, ta) >= _rank(key, b, bv, tb) else bv
    return merged
```

Requires `import functools` at `dedup.py:1`.

- **Verified equivalent:** `dedup.dedup(corpus) == variant(corpus)` → `True` at 7,039 / 65,869 /
  333,706 / 999,456 records and at 10k / 100k / 500k records. No result changes.
- **Speedup:** 1.53× / 2.00× / **2.38×** at 10k / 100k / 500k. 48.5 s → 20.3 s at 500k.
- **Cache:** ≤ 184 entries, 99.96% hit rate, O(1) memory.
- ⚠️ `@lru_cache` on `_trust_for` itself raises `TypeError` — `dict` is unhashable.

### 2. Apply the `_EMPTY` module constant — `dedup.py:151`

Folded into step 1 above; ~31 ns per source, zero risk, removes ~20–27M throwaway dicts at 500k.

### 3. Nothing further in this function

`_trust_for` is already O(1), `dedup()` is already O(n), and after step 1 it will no longer be a
third of the runtime. Re-profile before spending anything else here — and redirect the remaining
perf effort to `enricher.check_websites`, which is ~31× more expensive on the same rows.