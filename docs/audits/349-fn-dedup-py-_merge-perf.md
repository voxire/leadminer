# 349 — `_merge` (dedup.py:190) performance at 500k records

## Verdict

`_merge` is **O(k) in field count, k ≤ 23, therefore O(1) per call and O(n) for the
whole `dedup()` pass — it is not O(n²), and there is no quadratic path to remove.**
Measured: `dedup()` over 10k→500k records with the duplicate rate held flat at 19.6–19.7%
scales ×2.03, ×2.02, ×2.55, ×1.99, ×2.67 — dead linear, 0.10 s → 5.28 s.

The cost problem is different from the one the code invites you to look for. `_merge`
recomputes a **24-entry constant table 42 times per call** and allocates **~36 KB to
retain 832 B**. On the duplicate-heavy path that is 33.4 µs per merge and it makes
CPython's cyclic GC the single largest line item at 500k. The single change with the
largest speedup is **memoising the per-record trust vector**, worth **1.37× on `_merge`**
(63.6 → 46.0 µs); combined with hoisting the per-field closure it is 1.72× and takes a
500k `dedup()` pass from 6.17 s to 5.14 s (−17%).

But on the run that actually happens every month — re-deduping the cumulative master —
`_merge` is only **16%** of `dedup()` CPU. The other 84% is key computation re-run over
500k rows that were already deduped last time. Optimise `_merge` first because it is
cheap; do not expect it to be the thing that makes the 500k run fast.

---

## Findings

### S2 — `_merge` re-parses `source` 44× and rebuilds the trust table 42× per call, to answer 23 questions

- **Where:** `dedup.py:190-194`, calling `dedup.py:159` (`_pick`) → `dedup.py:148`
  (`_trust_for`) → `dedup.py:143` (`_sources_of`) against the 24-entry
  `_SOURCE_TRUST` table at `dedup.py:97-110`.

- **Breaks:** `_SOURCE_TRUST` (`dedup.py:97-110`) is a compile-time constant: 3 sources,
  24 `(source, field)` pairs, 13 distinct fields. `_trust_for` (`dedup.py:148-152`) is
  a pure function of `(field, record["source"])`. `_merge` calls it **42 times per
  merge** (`dedup.py:181`) to obtain 23 numbers that come out of a 24-entry table.

  Measured call counts per `_merge()` of two 23-key records (cProfile, 100,000 merges):

  | call | count per `_merge()` |
  |---|---|
  | `dict.get` (`dedup.py:151`, `167`, `211`) | 182 |
  | `_is_missing` (`dedup.py:79`) | 88 |
  | `str.strip` (`dedup.py:89`) | 64 |
  | `_sources_of` (`dedup.py:143`) | 44 |
  | `str.split` (`dedup.py:145`) | 44 |
  | `_trust_for` (`dedup.py:148`) | 42 |
  | `rank` closure (`dedup.py:179`) | 42 |
  | `_validity` (`dedup.py:120`) | 42 |
  | `_pick` (`dedup.py:159`) | 23 |

  Component costs, min-of-5×200k, per-process CPU:

  | component | ns/op | × count | µs per merge | share of 63.6 µs |
  |---|---|---|---|---|
  | `_trust_for` (`dedup.py:148`) | 453 | 42 | 19.0 | **30%** |
  | …of which `_sources_of` (`dedup.py:143`) | 231 | 44 | 10.2 | 16% |
  | …of which eager `{}` in `.get(src, {})` (`dedup.py:151`) | 24 | 42 | 1.0 | 1.6% |
  | `rank` `MAKE_FUNCTION` (`dedup.py:179`) | 225 | 42 | 9.5 | **15%** |
  | `set(a) \| set(b)` (`dedup.py:192`) | 1826 | 1 | 1.8 | 2.9% |

  Two separable wastes here:
  1. `_SOURCE_TRUST.get(src, {})` at `dedup.py:151` evaluates the `{}` default
     **eagerly on every call**, including on a hit — 42 throwaway 64-byte dicts per
     merge. Use `.get(src)` and a `None` check.
  2. `_SOURCE_TRUST` only covers 13 fields. `website_live`, `region`, `linkedin`,
     `completeness_score`, `lead_score`, `industry_priority`, `recommended_service` and
     3 others can never beat `_DEFAULT_TRUST = 1` (`dedup.py:111`), so 16 of the 42
     lookups are computing a constant.

- **Trigger:** any two records that share a dedup key. Cost scales with how *full* the
  records are, not with how many businesses there are:

  | record shapes merged | µs per `_merge()` |
  |---|---|
  | 23-key × 23-key, both fully populated (master row × master row) | **63.6** |
  | 23-key × 9-key (master row × fresh scrape) | 30.6 |
  | 9-key × 9-key (osm × google_places) | 25.2 |
  | 23-key × 0-key | 8.4 |
  | 1-key × 1-key | 2.6 |

  Same function, same key, 2.5× the cost, decided entirely by field density.

- **Fix:** build `{field: trust}` once per record (26 `_trust_for` → 2), and skip the
  lookup entirely for fields outside `_TRUSTED`. Measured **1.37×** on `_merge`
  (63.6 → 46.0 µs) from the memo alone.

---

### S2 — `_merge` allocates ~36 KB per call to retain 832 B, and that churn makes cyclic GC the biggest cost at 500k

- **Where:** `dedup.py:191-194`.

- **Breaks:** two allocation problems compound.

  1. `rank` is a nested `def` at `dedup.py:179`, so CPython rebuilds the function object
     **and its closure cell for `field` 42 times per merge**. Measured 225 ns each =
     9.5 µs = 15% of the merge, and 42 × 168 B = 7.1 KB of garbage.
  2. `set(a) | set(b)` at `dedup.py:192` builds three sets (1240 B each for 23 keys) to
     enumerate ≤ 23 keys.

  Allocation per `_merge()` call, derived from the cProfile counts above ×
  `sys.getsizeof`:

  | object | count | B each | total |
  |---|---|---|---|
  | `set` of 23 keys (`dedup.py:192`, ×3) | 3 | 1240 | 3,720 |
  | single-element `set` from `_sources_of` (`dedup.py:145`) | 44 | 216 | 9,504 |
  | 1-element `list` from `.split("\|")` (`dedup.py:145`) | 44 | 96 | 4,224 |
  | closure function object (`dedup.py:179`) | 42 | 168 | 7,056 |
  | closure cell for `field` | 42 | ~40 | 1,680 |
  | 4-tuple rank key (`dedup.py:180`) | 84 | 80 | 6,720 |
  | eager `{}` default (`dedup.py:151`) | 42 | 64 | 2,688 |
  | **merged dict (`dedup.py:191`) — the only survivor** | 1 | 832 | **832** |
  | **total** | | | **~36 KB allocated, 832 B retained** |

  That is **~35 KB of pure garbage per merge**, ~3.5 GB of transient garbage across a
  500k pass at a 20% duplicate rate.

  The consequence is visible and it is not subtle. At 500,000 records:

  | duplicate rate | `dedup()` CPU | µs/record | gen-0 collections | gen-1 | gen-2 |
  |---|---|---|---|---|---|
  | 0% | 1.69 s | 3.38 | 260 | 23 | 2 |
  | 20% | 5.31 s | 10.62 | 210 | 19 | 1 |
  | 80% | 14.78 s | 29.56 | 57 | 5 | 0 |

  Timed via `gc.callbacks` on the 0%-duplicate pass: **1.08 s of 2.10 s wall (51%) is
  spent inside the collector** across 286 collections. A single full `gc.collect(2)`
  over the resulting 1,000,000 live dicts costs **306 ms**. Records read from
  `load_master` (`main.py:77-105`) are built by `csv.DictReader` and contain **no
  reference cycles at all**, so every one of those traversals is wasted work whose only
  purpose is to find cycles that cannot exist.

- **Trigger:** measured, 500k records, any merge rate. `gc.freeze()` alone takes the
  0%-duplicate pass from 1.58 s to 1.43 s (−10%).

- **Fix:** (a) hoist `rank` from `dedup.py:179` to module scope, taking `field` as a
  parameter — kills 42 closures + 42 cells per merge, **1.17×** on its own; (b)
  `gc.freeze()` after `load_master` at `main.py:150`, since the loaded set is
  cycle-free by construction.

  Combined with the S2 finding above, `dedup()` over 500k records at a 20% duplicate
  rate goes **6.17 s → 5.14 s (−17%)**, verified byte-identical output
  (400,742 records both runs).

---

### S2 — on the monthly re-run, `_merge` is 16% of `dedup()` and the other 84% is re-keying the master

- **Where:** `main.py:179-180` — `combined = raw_filtered + master; records = dedup(combined)`.

- **Breaks:** `master` is the output of the previous run's `dedup()` (`main.py:209`), so
  it is already unique. Every run re-runs `normalize_phone` (`dedup.py:205`),
  `normalize_name` (`dedup.py:214`), `_extract_city` (`dedup.py:213`) and `dict(record)`
  (`dedup.py:210`/`218`) over the entire cumulative history, forever. Only the net-new
  scrape actually needed that work.

  Measured, 480,000 master rows + 20,000 fresh, 1.04% phone-collision rate
  (shared-hotline chains, the realistic case):

  | | |
  |---|---|
  | input records | 500,000 |
  | `dedup()` CPU | **2.03 s** (4.06 µs/record) |
  | `_merge` calls | 5,195 (1.04% of records) |
  | cost of those merges at 63.6 µs | 0.33 s = **16%** |
  | key computation + dict copies | **1.70 s = 84%** |
  | output | 494,805 |

  So the ceiling on any `_merge` optimisation along this path is 16% of 2 s. The waste is
  structural, not algorithmic.

  A separate, smaller doubling: `dedup.py:228` calls `normalize_phone` a **second** time
  on every name-keyed record, on the same `raw` string already normalised at
  `dedup.py:205`. Phone-less rows are normalised twice; rows that reached `name_index`
  have already proven their phone unusable.

- **Trigger:** the second and every subsequent run of the pipeline, at whatever size the
  master has grown to.

- **Fix:** out of scope for this function, but the lever is large — key the master rows
  once at `load_master` and dedup only `raw_filtered` against that index
  (see audit 035 on incremental scraping). For the immediate case: drop the redundant
  `normalize_phone` at `dedup.py:228` by testing `if raw and key:` on the value already
  computed at `dedup.py:205`.

---

### S3 — `set(a) | set(b)` at `dedup.py:192` is 2.9% of the merge; do not spend effort here

The obvious suspect is not the problem. Measured 1,826 ns for a 23-key union against a
63.6 µs total. Replacing the union with `merged = dict(a)` plus a loop over `b`'s keys
(provably equivalent — for `k` in `a` but not `b`, `_pick` at `dedup.py:167-171` returns
`a[k]`; for `k` in `b` but not `a` it returns `b[k]`) measured:

| | dense × dense | dense × sparse | sparse × sparse |
|---|---|---|---|
| `dict(a)` + iterate `b` only | 1.01× | 1.19× | 1.01× |

Worth folding in only as part of the inlined rewrite in the first two findings, where it
falls out for free. On its own it is noise.

---

### S3 — `_merge` is not order-independent when both values are blank strings

- **Where:** `dedup.py:168-171`.

- **Breaks:** when `a`'s value is missing *and* `b`'s value is missing, the first
  `_is_missing` branch at `dedup.py:168` fires and `_pick` returns `b`'s value. Swap the
  arguments and you get `a`'s. Both are missing, but the survivor differs:

  ```python
  >>> dedup._merge({"email": "",    "source": "osm"}, {"email": None, "source": "osm"})["email"]
  None
  >>> dedup._merge({"email": None,  "source": "osm"}, {"email": "",   "source": "osm"})["email"]
  ''
  ```

  Reproduced in a randomised hunt over 30,000 adversarial pairs; a minimised three-field
  case diverges on `address`, `email` and `whatsapp` simultaneously:

  ```python
  x = {"address": "",  "email": "",  "whatsapp": "  "}
  y = {"address": "  ", "email": None, "whatsapp": None}
  dedup._merge(x, y) == {'whatsapp': None, 'address': '  ', 'email': None}
  dedup._merge(y, x) == {'whatsapp': '  ', 'address': '',  'email': ''}
  ```

  This contradicts `tests/test_lead_signal.py:292` (`test_merge_is_order_independent`),
  and input order is thread-scheduling dependent — `main.py:162` iterates
  `as_completed(futures)`. No wrong data reaches the CSVs (both spellings serialise to
  an empty cell), but the invariant the rest of the merge design leans on is false for
  whitespace-only values, which is exactly the shape `load_master` (`main.py:88-89`)
  turns `""` into `None` for — so the two orderings disagree on representation, not just
  on timing.

- **Fix:** normalise in `_pick` — `return bv if not _is_missing(av) else av` so a
  missing-vs-missing collision deterministically keeps `a`.

---

## Not a bug, but worth knowing

- **There is no O(n²) in this function, and you should not go looking for one.**
  `_merge` is O(k) in `|keys(a) ∪ keys(b)|`, measured flat at **~1.85 µs per key from
  k=1 to k=92**:

  | keys in each | µs per `_merge()` | ns per key | ratio vs previous |
  |---|---|---|---|
  | 1 | 2.05 | 2048 | — |
  | 2 | 3.61 | 1803 | ×1.76 |
  | 4 | 7.04 | 1761 | ×1.95 |
  | 8 | 14.18 | 1772 | ×2.01 |
  | 16 | 28.92 | 1808 | ×2.04 |
  | 23 | 42.98 | 1869 | ×1.49 |
  | 46 | 85.49 | 1858 | ×1.99 |
  | 92 | 170.20 | 1850 | ×1.99 |

  k is bounded by the schema — 23 columns at `main.py:33-40`, 23 keys at
  `scrapers/base.py:5-28` — and the accumulator's key count does **not** grow with chain
  length: 499 successive merges into one accumulator kept the key count flat. Chains cost
  O(m·23) for m duplicates, not O(m²). So `_merge` is O(1) per record and `dedup()` is
  O(n).

  The only way this becomes superlinear is a future change that makes the field set open
  (e.g. records parsed from arbitrary JSON rather than a fixed CSV header). `_merge`
  unions whatever keys it is handed, which audit 070 already flags. Add `k <= 64` to the
  guard rails and a schema assertion before this is allowed to grow.

- **`dedup()` end-to-end, 19.7% duplicate rate, 20% dense 23-key rows.** Flat µs/record
  across a 50× range is the proof of linearity:

  | n | `dedup()` CPU | µs/record | `_merge` calls | µs/merge |
  |---|---|---|---|---|
  | 10,000 | 0.10 s | 9.54 | 1,962 | 48.7 |
  | 20,000 | 0.19 s | 9.66 | 3,940 | 49.1 |
  | 40,000 | 0.39 s | 9.77 | 7,858 | 49.7 |
  | 100,000 | 0.99 s | 9.94 | 19,692 | 50.5 |
  | 200,000 | 1.98 s | 9.91 | 39,373 | 50.3 |
  | 500,000 | **5.28 s** | 10.57 | 98,504 | 53.6 |

  A second independent generator at 500k gave 6.17 s, so the honest range for a 500k
  20%-duplicate pass is **5.3–6.2 s**. Sweeping the duplicate rate at 500k and fitting
  a line gives `cost = 3.41 µs/record + 33.4 µs/merge`, so `_merge` is 91% of a
  20%-duplicate pass and 9.8× the cost of hashing one record. At a 100% duplicate rate
  the same 500k records would cost ~26.8 s in `_merge` alone; at 0%, ~1.7 s.

- **Peak memory is ~2× the record set during `dedup()`.** `dedup.py:210`/`218` store
  `dict(record)` — a full copy — into `phone_index`/`name_index`, while the caller's list
  (`combined` at `main.py:179`) still holds the originals. `sys.getsizeof` of a dense
  23-key record is 832 B, so 500k dense records is ~416 MB per copy, ~830 MB before
  `_merge`'s transient 36 KB/call churn is counted. In-place accumulation (merging `b`
  into `a` and reusing `a`) would halve the index copy, but it mutates the caller's dicts
  and would break the non-mutation contract that `tests/test_lead_signal.py:279-326`
  relies on — so it needs a separate function, not an edit to `_merge`.

- **Measurement method.** The machine was under load average 80 on 12 cores (concurrent
  agents), which made wall-clock `timeit` unusable — it produced a 3× spread and an
  internally impossible ordering (`"a".split()` measured slower than the function that
  calls it). Every figure above is `time.process_time_ns` — per-process CPU time,
  immune to other tenants — with min-of-N over interleaved A/B rounds, fresh input dicts
  per measurement to stop memoisation variants from polluting later ones. Python 3.14.8;
  the project targets 3.12, so expect the same shape with modestly different constants.
  No network calls, no scrapers executed, no packages installed, no source modified.

---

## Recommended order of work

1. **Memoise the per-record trust vector and source set** (first S2). One
   `{field: trust}` dict per record instead of 42 `_trust_for` calls and 44
   `.split("|")` + set-comprehensions. **1.37× on `_merge`**, ~46 µs → 46.0 µs from
   63.6 µs. Cheapest real win on the page.
2. **Hoist `rank` out of `_pick` (`dedup.py:179`) to module scope.** Takes `field` as an
   argument. Kills 42 closure objects + 42 cells per merge. **1.17×** on its own.
   Steps 1 + 2 together: **1.72× on `_merge`**, and `dedup(500k)` 6.17 s → **5.14 s**.
   Both changes verified byte-identical to `dedup._merge` over 20,000 randomised
   adversarial record pairs plus a 200-step chained-accumulator merge.
3. **`gc.freeze()` after `load_master` (`main.py:150`)** and unfreeze before writing.
   The loaded set is cycle-free by construction; this is 10% of the key-computation path
   today and it is the only fix that stops scaling with master size.
4. **Delete the redundant `normalize_phone` at `dedup.py:228`** — the value is already
   computed at `dedup.py:205`.
5. **Only then attack the master re-dedup at `main.py:179-180`**, which is where the
   remaining 84% of the monthly run lives. Steps 1–4 are worth ~17% of a duplicate-heavy
   pass and are all contained in one file; step 5 is the 500k enabler.
