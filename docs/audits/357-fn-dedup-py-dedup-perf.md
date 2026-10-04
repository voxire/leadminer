# 357 — `dedup.dedup()` perf: O(n) and correctly so, but it pays a 1 KB/record tax for nothing

**Lens:** perf. **Target:** `dedup.py:197` (`dedup`).
**Verdict:** `dedup` is genuinely **O(n) in the number of businesses — there is no O(n²) anywhere**, and it allocates linearly. At the 500k target it costs **2.5 s** on the realistic mix, which is a non-event against the 300-minute workflow budget. The real defect is that `dedup.py:210` and `dedup.py:218` copy every record into the index while `main.py:179` keeps the originals alive, so the process holds **two** complete record sets: **+476.5 MB and 40 % of runtime, for zero benefit**. That is the single change with the largest speedup, and it is a two-line diff.

Everything I tried to make the merge path itself faster made it **slower or wrong**, and the reason is interesting enough to be its own finding (`_pick`'s rank tuple is not a function of the individual observations — see S3).

---

## Method / reproducibility

All numbers measured on **CPython 3.14.8, macOS arm64, 12 cores** (project targets 3.12; the code is
pure-Python dict/regex work so relative scaling is what carries over). Timings are **min-of-N** —
the least noisy estimator on a shared machine; run-to-run median varied up to 3× under load, min did not.
Timing and `tracemalloc` passes were run **separately**: `tracemalloc` inflates `dedup` by ~4× and
mixing the two produced a bogus 37 µs/record in my first pass.

Records are synthetic, shaped like `BusinessRecord` (`scrapers/base.py:5`), with the 22 keys of
`main.py:33`, realistic per-field null rates, and three source passes (`google_places`, `osm`,
`wikidata`) as `main.py:154-168` does. **Two regimes are reported throughout**, because they behave
completely differently and conflating them is how you would draw the wrong conclusion:

- **realistic** — 3 passes over n businesses, ~5 % cross-source collision (what the code actually does today)
- **merge-heavy** — every business seen 20× (worst case for the `_merge` path)

---

## Headline numbers

### Baseline `dedup()`, realistic 3-source mix (min of 5)

| raw records | unique out | seconds | µs/record | tracemalloc peak | bytes/record |
|---|---|---|---|---|---|
| 9,999 | 9,490 | **0.035** | 3.49 | 9.8 MB | 984 |
| 99,999 | 95,169 | **0.392** | 3.92 | 96.0 MB | 960 |
| 500,001 | 475,016 | **2.515** | 5.03 | 479.3 MB | 959 |

10× the records → 11.2× the time. µs/record creeps 3.49 → 5.03 (**+44 %**) over a 50× size range.
That creep is **entirely CPython GC**, not algorithmic (see S3).

### Baseline `dedup()`, merge-heavy (every record collides 20:1, min of 3)

| raw records | merges | seconds | µs/record | **µs/merge** |
|---|---|---|---|---|
| 50,000 | 47,500 | 1.541 | 30.81 | **32.43** |
| 200,000 | 190,000 | 6.333 | 31.67 | **33.33** |
| 500,000 | 475,000 | 15.817 | 31.63 | **33.30** |

**This is the load-bearing evidence that there is no O(n²).** µs/merge is flat at **33.3 µs across a
10× range of merge count** (47,500 → 475,000). A quadratic term would show µs/merge growing 10× over
that span; it grows 2.6 %. I also fixed the unique-business count at 8,000 and raised the copies per
business from 5 to 80 (32k → 632k merges): µs/merge stayed 36.99 / 38.97 / 39.42 / 44.05 / 39.27 —
flat, i.e. **accumulation depth is O(1) per merge**, not O(depth).

### Complexity verdict

| Question | Answer | Evidence |
|---|---|---|
| O(1) / O(n) / O(n²) in #businesses? | **O(n)** | flat µs/merge and µs/record over 10× ranges |
| Per-merge cost? | **O(k)**, k = key count = **22**, measured at exactly 22.0 `_pick` calls per merge | cProfile: `_pick` 6,380,000 calls / 290,000 merges = 22.00 |
| Allocates proportionally? | **Yes, linear** — ~959 bytes/record peak, flat across 10k→500k | tracemalloc column above |
| Any super-linear component? | **No algorithmic one.** A 44 % GC-induced creep (S3) | §S3 |

There is exactly one loop over `records` (`dedup.py:201`), one over `name_index` (`dedup.py:226`),
and every lookup is a dict/set membership test. `dedup.py:222-223` are O(unique). No nested scan over
`records` exists. **I could not construct an input that degrades super-linearly, and I tried.**

---

## Findings

### S2 — `dict(record)` at `dedup.py:210` and `dedup.py:218` doubles the process footprint and costs 40 % of runtime for nothing

- **Where:** `dedup.py:210` (`phone_index[key] = dict(record)`), `dedup.py:218` (`name_index[key] = dict(record)`), interacting with `main.py:179` (`combined = raw_filtered + master`) and `main.py:180` (`records = dedup(combined)`).
- **Breaks:** `dedup` never mutates the record it stores. On a collision it *replaces* the slot with a fresh `_merge` result (`dedup.py:208`, `:216`). So the copy at `:210`/`:218` buys nothing. But those copies **are** the return value, so they must live; and `main.py`'s `combined`, `raw_filtered`, `master` and `raw` all stay reachable in the frame for the whole of `main()`. Result: the process holds the record set twice, permanently.

  Measured with `ru_maxrss`, realistic 500k records:

  | point | RSS | delta |
  |---|---|---|
  | interpreter start | 17.6 MB | |
  | after building 500k records (caller side) | 620.0 MB | +602.4 |
  | after `dedup()` returns | 1096.5 MB | **+476.5** |
  | same, with the copy removed | 1096.5 MB | **+0.0** |

  **476.5 MB, i.e. 1003 bytes of pure duplication per surviving record**, and 44 % of the
  1096.5 MB total. Speed, min-of-4 at realistic 500k:

  | variant | seconds | speedup |
  |---|---|---|
  | `D.dedup` (baseline) | 2.540 | 0.99× |
  | **no `dict(record)` copy** | **1.807** | **1.40×** |
  | no copy + no re-normalize | 1.783 | 1.42× |
  | `gc.disable()` wrapper | 1.928 | 1.31× |
  | no final re-normalize only | 2.604 | 0.97× (no gain) |

- **Trigger:** any run. 500k records → 476.5 MB wasted and 0.73 s wasted. Scales linearly.
- **Caveat, stated honestly:** at merge-heavy 500k this change is worth **1.00×** (15.79 s vs
  15.76 s), because nearly every record goes through `_merge`, which allocates a fresh dict anyway
  and immediately orphans the copy.
- **Fix:** store `record` instead of `dict(record)` at both sites. **This is a contract change** —
  `dedup` would return dicts aliased to its input. Safe against today's `main.py`: `master`
  (`main.py:150`), `raw_filtered` (`:172`) and `raw` (`:154`) are all dead after `:179`, and the
  in-place mutation at `main.py:183-197` only writes to records that are about to be written out.
  I verified `dedup_nocopy` returns **byte-identical** output to `D.dedup` on realistic 20k,
  realistic 500k and merge-heavy 200k. But document it, or take `copy` as a keyword, because the
  next caller will not know.

### S2 — the `_merge` path costs 33 µs/merge and is 87 % of runtime when records collide; no cheap fix exists

- **Where:** `dedup.py:190-194` (`_merge`), `dedup.py:159-187` (`_pick`), reached from `dedup.py:208` and `:216`.
- **Breaks:** `_merge` rebuilds a 22-key dict and runs `_pick` on every key for **every** colliding
  record. cProfile on merge-heavy 300k records (290k merges):

  | function | calls | calls/merge | cum share |
  |---|---|---|---|
  | `_merge` | 290,000 | 1.0 | **87 %** |
  | `_pick` | 6,380,000 | **22.00** | |
  | `_trust_for` | 6,011,352 | 20.7 | |
  | `_sources_of` | 6,591,352 | 22.7 | |
  | `_validity` | 6,011,352 | 20.7 | |
  | `dict.get` | 44,580,672 | 153.7 | |

  `_sources_of` (`dedup.py:143-145`) re-runs `str()` + `.split("|")` + a set comprehension
  **22.7 times per merge** on two records whose source strings never change during that merge.

  At 500k merge-heavy this is **15.8 s vs 2.5 s** for the same record count — a **6.3× penalty for
  the same input size**, purely in merge count.
- **Trigger:** a scraper returning many rows per business. Realistic for OSM (a mall with 40 tagged
  nodes) and for Google Places textSearch returning the same chain across 68 queries
  (`scrapers/google_places.py`, per `BRIEF.md`).
- **Fix:** **not a micro-optimization.** See the three negative results below — memoizing
  `_sources_of` gives 1.09×, collapsing the double `rank()` gives 1.30× and is *slower* on the
  realistic mix, and precomputing rank tables is 0.47× (much slower) once it is made correct.
  The only real fix is upstream: **key on a stable business identity before the merge** so that
  `_merge` runs once per business rather than once per observation.

### S3 — 44 % of the cost at 500k is CPython GC, and it is the only super-linear component

- **Where:** the whole of `dedup.py:197-232`. `dedup` allocates one 22-key dict per *surviving*
  record (`dedup.py:210`, `:218`) plus one per collision (`dedup.py:191`), and every container it
  creates is reachable from the index for the rest of the loop — so the cyclic collector has
  essentially **nothing to find** but still rescans the growing heap.
- **Breaks:** collections triggered by one `dedup()` call:

  | raw N | gen0 | gen1 | gen2 |
  |---|---|---|---|
  | 30,000 | 19 | 1 | 0 |
  | 120,000 | 75 | 6 | 0 |
  | 300,000 | 185 | 16 | 1 |
  | 500,001 | 308 | 28 | 2 |

  gen0 scales linearly (19 → 308) and gen1 collections each scan an ever-growing surviving set.
  With `gc.disable()` around the call, per-record cost flattens: **5.67 µs/rec at 30k → 6.01 µs/rec
  at 500k** (vs 7.11 → 8.72 µs/rec with GC on). That is the whole of the +44 % creep in the headline
  table.
- **Speedup, min-of-4 realistic 500k:** `gc.disable()` wrapper 2.540 → 1.928 s = **1.31×**
  (measured 1.22–1.58× across runs).
- **Caveat that kills it as a recommendation:** in the **merge-heavy** regime `gc.disable()` measured
  **0.92–1.00×** — no benefit, and at 200k it was *slower* than baseline. It is a regime-dependent
  trick, not a fix.
- **Fix:** if you want it, `gc.disable()`/`gc.enable()` around `dedup()` in `main.py:180` — but
  fix S2 first, which removes most of the same pressure for the same reason (fewer live dicts).

### S3 — the second `normalize_phone` in the final loop (`dedup.py:228`) is genuinely redundant, and removing it buys nothing

- **Where:** `dedup.py:225-230`, specifically `:228`.
- **Breaks:** nothing. `normalize_phone` was already called on this record's phone at
  `dedup.py:205`, on the same input, and it is deterministic — so for every name-keyed record that
  was not merged, `:228` recomputes a value it already had and throws it away. Measured cost of the
  whole re-normalize pass: **0.595 s / 2.09 µs per record** at realistic 300k.
- **But:** removing it is **not free to do naively** and the end-to-end gain is **0.97×, i.e.
  nothing** (2.604 s vs 2.540 s baseline). The record in `name_index` is the *merged* record, so its
  `phone` may differ from the input's; the recomputation is only redundant when no merge happened.
  The honest reading: 0.6 s of a 2.5 s budget is real, but it is inside the noise band of this
  machine and I would not spend the semantic risk on it. Listed so nobody re-derives it.
- **Fix:** if you want it, carry the key alongside the record in the index instead of recomputing —
  i.e. the same shape as S2's fix.

---

## Not a bug, but worth knowing

- **The obvious optimizations do not work, and the reason is a semantics property, not a perf accident.**
  I built and benchmarked three candidates; all are worse or negligible once implemented correctly.

  | candidate | realistic 500k | merge-heavy 500k | output identical? |
  |---|---|---|---|
  | memoize `_sources_of` on the source string | ~1.00× (slightly slower) | 1.09× | yes |
  | single `rank()` call + lazy tiebreak (skip `str(value)` unless reached) | slower | 1.30× | yes |
  | precompute a per-record rank table | **0.47×** (7.69 s vs 2.54 s) | **0.79×** | yes, once corrected |

  The precomputed rank table first appeared to be a **1.47–1.66×** win — but only because the version
  I wrote reproduced a *different* merge rule. The moment I made it match `_pick` exactly, it became
  3× **slower**. That regression is the finding:

- **`_pick`'s rank tuple ratchets, so the merge path cannot be memoized.**
  `dedup.py:179-185` ranks a value against `rec`, and for the accumulator in a merge chain `rec` is
  the *merged* dict. Its `source` is the pipe-joined union (`dedup.py:174`) and its `scraped_at` is
  the max (`:177`). So both trust and recency **ratchet upward with every merge**, and a surviving
  field's rank is not a function of the observations that produced it. Concretely:

  ```python
  a = google_places, review_count=111, scraped_at=2026-01-01
  b = osm,         review_count=999, scraped_at=2026-09-01
  m = _merge(a, b)   # -> review_count=111  (google trust 3 beats osm trust 1)
                      #    source="google_places|osm",  scraped_at="2026-09-01"

  _trust_for("review_count", m)   # -> 3   via the source UNION, not via 111's own provenance
  _recency(m)                    # -> 2026-09-01  (b's timestamp, not a's)

  c = wikidata, review_count=222, scraped_at=2026-12-01
  _merge(m, c)["review_count"]   # -> 111, even though 222 was observed 3 months later
  ```

  A field's rank therefore must be recomputed against the merged record on **every** merge. Any
  precomputation keyed on the individual observations is unsound, and any that skips the
  recomputation is wrong. **Anyone attempting to speed up `_merge` will hit this wall; it is not
  fixable without changing the merge rule itself** (e.g. recording per-field provenance instead of
  collapsing it into one merged record).

- **`_merge` is not associative — a stale value inherits a newer observation's timestamp, and it
  changes the output.** Surfaced by the above while hunting for why precompute failed; it is a
  correctness observation outside my lens, so I am flagging it for whoever owns that lens rather than
  claiming it. Reproducible, 3 records, all keying to `("acme", "beirut")`:

  ```
  ((A B) C) -> website=33.446
  (A (B C)) -> website=112
  ```

  `website` is in `_VOLATILE` (`dedup.py:114`), so `dedup.py:183` uses `_recency(rec)`. In the left
  grouping the accumulator has absorbed `B`'s `2026-09-26`, so `A`'s January value `33.446` is ranked
  as if it were observed in September and beats `C`'s fresher `112`. Rare — **2 of 20,000 random
  triples** — but it means `dedup`'s result depends on input order, which is the same property
  `_pick`'s docstring at `dedup.py:160-165` claims to have eliminated.

- **Memory at 500k is not an OOM risk today.** 1.1 GB against the 16 GB of a standard
  `ubuntu-latest` runner (`.github/workflows/scrape.yml:24`; GitHub's runner table lists
  `ubuntu-latest` as 4 CPU / 16 GB RAM / 14 GB SSD). The 476.5 MB in S2 is a third of the footprint,
  not a crash. It is a waste finding, not an availability finding, and I have graded it S2 for that
  reason. At 1M+ records — the stated growth direction — it becomes 2.2 GB from `dedup` alone.

- **`enrich` does not add to the peak.** `enricher.py:326-346` mutates the same list in place and
  `enricher.py:250` writes back into the record, so `dedup`'s output is the only record set
  `enrich` allocates.

- **Numbers to distrust from anyone benchmarking this.** `tracemalloc` inflates `dedup` ~4×
  (2.5 s → 17.0 s at 500k in my first pass) — timing and memory passes must be separate. And
  `cProfile` inflated merge-heavy 500k from 15.8 s to 197.9 s (12.5×), which makes its *call counts*
  trustworthy but its *percentages* badly skewed toward small, frequently-called functions.

---

## Recommended order of work

1. **Remove the `dict(record)` copy at `dedup.py:210` and `dedup.py:218`** — 1.40× and −476.5 MB at
   500k for a two-line diff. Take a `copy: bool = False` keyword or document the aliasing, because
   the next caller will trip on it. Verify with a byte-identical-output assertion against the
   current implementation on realistic + merge-heavy fixtures — I did, it holds.
2. **Do not touch `_pick`/`_merge` for performance.** I benchmarked the three plausible routes and
   all are ≤1.00× on the realistic mix or semantically wrong. If merge cost ever actually hurts,
   the fix is upstream — key on a stable business identity so `_merge` runs once per business.
3. **Optional: `gc.disable()` around `main.py:180`** — 1.31× at realistic 500k, but 0.92–1.00× at
   merge-heavy. Only worth it after step 1, and measure both regimes before keeping it.
4. **Hand the non-associativity finding (above) to the correctness lens.** It is load-bearing for
   anyone who later tries to restructure `_merge`, and it contradicts the claim in the `_pick`
   docstring at `dedup.py:160-165`.
5. **Skip `dedup.py:228`.** It is redundant work but worth 0.97× end-to-end. Not worth the semantic
   risk.