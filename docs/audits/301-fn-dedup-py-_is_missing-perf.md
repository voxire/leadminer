# 301 — `_is_missing` perf: O(1) per call, but its callers are the cost

**Target:** `_is_missing` at `dedup.py:79`
**Lens:** perf — complexity, allocation, timings at 10k/100k/500k, largest single speedup

## Verdict

`_is_missing` is **O(1) in the number of businesses and allocates nothing** — it is not
the problem, and no change to its body can buy more than ~0 because it is already at the
floor (memoising it measured **+35% slower**). `dedup()` as a whole is **strictly linear**,
measured at k = 0.98–1.01 from 10k to 500k. The single change with the largest speedup is at
the **caller**: an equality short-circuit in `_pick` (`dedup.py:167`), which removes 70% of
all `rank()` work and takes **-26.9%** off `dedup()` at 500k with byte-identical output.
The only genuinely dangerous item is latent: `_merge`'s key union (`dedup.py:192`) is
O(keys-in-accumulated-record), so unbounded key growth turns `dedup` **quadratic**
(k = 2.01 measured, ~9.3 h at 500k) — not reachable through today's pipeline, but nothing
enforces the invariant and it would blow the 300-minute workflow timeout if it ever did.

## Measured baseline

Machine: Apple Silicon, CPython 3.14.8, `time.process_time()` (CPU, immune to the ~400
concurrent agents on this box), `gc.disable()` around the timed region, min-of-N.
Instrumentation is by monkeypatching module globals — **no source file was modified**.

Input shape: 23-key `BusinessRecord` schema (`main.py:33-40`), three sources
(`google_places` / `osm` / `wikidata`), ~42% of records colliding on an existing dedup key
(the cumulative-master shape of `main.py:179-180`).

| records | merges | `_is_missing` calls | calls/merge | calls/record | dedup CPU | µs/record |
|--------:|-------:|-------------------:|------------:|-------------:|----------:|----------:|
| 10,000  | 4,212  | 223,643             | 53.0        | 22.4         | **0.170 s** | 17.0 |
| 100,000 | 41,807 | 2,216,898           | 53.0        | 22.2         | **1.791 s** | 17.9 |
| 500,000 | 208,462 | 11,057,515          | 53.0        | 22.1         | **7.957 s** | 15.9 |

Scaling exponent `k = log(T₂/T₁) / log(n₂/n₁)`, from CPU seconds above:

```
 10,000 -> 100,000 : k = 1.02
100,000 -> 500,000 : k = 1.01
```

Confirmed independently with an all-unique-keys input (k = 1.00, 1.01) and with a
single-key worst case (k = 1.03, 1.04). **`_is_missing` calls per merge is flat at 53.0 from
10k to 500k** — that flatness is the direct proof that the predicate is O(1) in n: if it
carried any n-dependence the per-merge ratio would drift.

### Where the 53.0 calls per merge come from

`_merge` (`dedup.py:190-194`) calls `_pick` once per key in `set(a) | set(b)` —
measured at exactly **23.0 calls per merge**, i.e. the full schema, every time. `_pick`
(`dedup.py:159-187`) then calls `_is_missing` twice (`dedup.py:168`, `dedup.py:170`) and,
if both sides are present, reaches `rank()` (`dedup.py:179`), which calls `_validity`
(`dedup.py:120`), which calls `_is_missing` a third and fourth time (`dedup.py:122`).
53.0 / 23.0 = 2.3 `_is_missing` per field on average.

Exact field-level classification of all 1,923,628 `_pick` calls (200k records):

| bucket | share | `_is_missing` calls | reaches `rank()`? |
|---|---:|---:|---|
| both missing | 41.4% | 2 | no |
| exactly one missing | 12.5% | 2 | no |
| both present, **equal** | **19.1%** | **4** | **yes — for nothing** |
| both present, unequal | 27.0% | 4 | yes |

Per-value cost of a single `_is_missing` call (min-of-7, 300k iterations):

| argument | ns/call |
|---|---:|
| `None` | 64.2 |
| `int` (`review_count`) | 124.7 |
| `float` (`lat`, `rating`) | 155.9 |
| `""` | 140.9 |
| `"Al Janoub Trading"` (17 B) | 111.7 |
| 91-char URL | 182.0 |

Observed argument mix: 55.5% non-empty `str`, 18.1% `float`, 14.4% `None`, 9.0% empty
`str`, 3.0% `int` → **mix-weighted 125 ns/call**.

**Therefore `_is_missing` at 500k realistic input = 11,057,515 × 125 ns ≈ 1.38 s, which is
17.3% of the 7.957 s `dedup()` total.**

### Worst case: dense records, all collapsing on one key

Realistic corpora are sparse (many `None`/empty fields), so the 53.0/merge figure is a
floor. Feed it fully-populated records sharing one phone — a shared landline, a franchise,
or simply a re-scrape where every observation carries all 23 fields — and the per-field
count goes to 4:

| records | merges | `_is_missing` calls | calls/merge | dedup CPU | `_is_missing` share |
|--------:|-------:|-------------------:|------------:|----------:|--------------------:|
| 10,000  | 9,999  | 759,924             | 76.0        | 0.65 s    | 14.6% |
| 100,000 | 99,999 | 7,599,924           | 76.0        | 6.54 s    | 14.5% |
| 500,000 | 499,999 | 37,999,924          | 76.0        | **27.01 s** | 17.6% |

k = 1.03 → 1.04. Linear, but **27 s** — and note the mild upward drift in µs/merge
(49.98 → 53.75 → 57.54) is heap/cache pressure, not an algorithmic term. This is the number
to quote for "what if the merge rate saturates".

## Findings

### S2 — `_merge`'s key union is O(keys in the accumulated record), so unbounded key growth makes `dedup` quadratic

- **Where:** `dedup.py:192` — `for key in set(a) | set(b):`
- **Breaks:** `a` is not an input record; it is the record accumulated so far for that dedup
  key (`phone_index[key] = _merge(phone_index[key], record)`, `dedup.py:208`). So the
  iteration cost is O(|keys(a)| + |keys(b)|) and |keys(a)| grows with every merge. The whole
  thing stays linear *only* as long as key cardinality is bounded — an invariant nothing in
  the code states, asserts, or enforces. Break it and `dedup` becomes O(n²) in merges, each
  step paying a full `_pick` per key.
- **Trigger:** N records that all share one phone key, each introducing one brand-new key:

  ```python
  W = []
  for i in range(n):
      r = {k: None for k in FIELDS, "name": "Same Chain", "country": "LB",
           "phone": "+9611111111", "source": "osm", "scraped_at": "2026-08-01"}
      r[f"extra_{i}"] = f"value_{i}"      # one key that has never been seen before
      W.append(r)
  dedup(W)
  ```

- **Measured (fixed 23-key schema, for contrast):** k = 1.01, 1.00, 1.00, 0.99, 0.98 at
  n = 2k/4k/8k/16k/32k; a flat 14.1 µs per merge. Perfectly linear.
  **Measured (one new key per record):**

  | n | CPU | µs/merge | k |
  |---:|---:|---:|---:|
  | 500 | 0.038 s | 75.97 | — |
  | 1,000 | 0.140 s | 139.93 | 1.88 |
  | 2,000 | 0.533 s | 266.54 | 1.93 |
  | 4,000 | 2.097 s | 524.33 | 1.98 |
  | 8,000 | 8.473 s | 1059.08 | 2.01 |

  Quadratic fit extrapolates to **~1,339 s at 100k and ~33,476 s (9.3 h) at 500k** — versus
  the 300-minute (5 h) `timeout-minutes` in `.github/workflows/scrape.yml`. That is the
  difference between "runs" and "the job is killed with no output".
- **Reachability — being honest:** today's pipeline cannot trigger this. The schema is fixed
  at 23 keys (`main.py:33-40`), and `load_master` (`main.py:86`) uses `csv.DictReader`, whose
  `restkey` behaviour adds a *single* `None` key for ragged rows, not a growing set. So this
  is a latent trap, not a live bug — but it becomes live the moment anyone widens the schema
  dynamically, reads records from JSON/Parquet/DB with heterogeneous keys, or adds a
  provenance or per-observation bag to the record (which proposals `docs/audits/050-provenance-and-idempotency.md`
  and `docs/audits/095-dedup-strategy-v2.md` both contemplate).
- **Fix:** bound the merged key set — carry an explicit schema constant and iterate that
  (plus any allowed extras) instead of the union, and drop/log unknown keys at the ingest
  boundary so cardinality can never grow.

### S3 — `_pick` recomputes provenance and validity ranks for values that are already equal

- **Where:** `dedup.py:167-187` — `av, bv = a.get(field), b.get(field)` then the
  missing-value short-circuits, then `rank()` at `dedup.py:179-186`.
- **Breaks:** nothing — this is pure waste. 19.1% of all field comparisons are "both sides
  present and equal" (`_is_missing` false for both, then `av == bv`), and those pay the full
  4-call `_is_missing` path *plus* two `rank()` tuples *plus* two `_sources_of` set builds
  (`dedup.py:143-145`, 412 ns each) *plus* two `_trust_for` lookups (`dedup.py:148-152`,
  678 ns each) *plus* two `_validity` evaluations (`dedup.py:120-140`, 924 ns for a website
  regex) to arrive at a value that is, by definition, already `==`. That is **70.7% of all
  `rank()` work — 366,880 of 519,475 ranked comparisons in the 200k-record trace — spent on
  re-deciding a settled question.** `_pick` is ~4.0–5.9 µs per field; a full 23-field
  `_merge` is 70.4 µs.
- **Trigger:** any duplicate pair sharing a value — the overwhelmingly common case.
  Concretely, at 500k/42% merge rate this is 208,462 merges × 23 fields × 19.1%
  ≈ 916,000 wasted `rank()` evaluations.
- **Fix (one line, immediately after `dedup.py:167`):**

  ```python
  av, bv = a.get(field), b.get(field)
  if field != "source" and av == bv:
      return av
  ```

- **Measured (min of 7 interleaved reps, GC off):**

  | records | baseline | with fast path | delta |
  |--------:|---------:|--------------:|------:|
  | 10,000  | 0.170 s  | 0.129 s       | **-23.8%** |
  | 100,000 | 1.791 s  | 1.349 s       | **-24.7%** |
  | 500,000 | 7.957 s  | 5.817 s       | **-26.9%** |

- **Why `source` must be excluded:** for that field `_pick` canonicalises rather than picks
  (`dedup.py:173-174`, `"|".join(sorted(_sources_of(a) | _sources_of(b)))`). With
  `a["source"] == b["source"] == "osm|"`, the current code returns `"osm"`, while the fast
  path would return `"osm|"`. Not a crash and arguably an improvement, but it *is* a
  behavioural difference, so exclude the field or special-case it.
- **Why it is otherwise value-equivalent:** when `av == bv` and both are non-missing, every
  remaining branch returns something `== av` — `scraped_at` returns `max(av, bv) == av`
  (`dedup.py:177`), and otherwise `av if rank(a, av) >= rank(b, bv) else bv`
  (`dedup.py:187`) picks between two values that are already equal. Verified: output was
  **identical** on all three datasets at all three sizes.
- **Severity rationale:** S3, not S2. 27% of an 8-second stage inside a run whose dominant
  cost is 1 HTTP GET per website at 40 workers (`enricher.py`, ~150k requests at 500k rows
  ≈ 19 minutes). Worth taking because it is one line, but it does not threaten the run.

### S3 — `_merge` allocates two throwaway sets on every call

- **Where:** `dedup.py:192` — `for key in set(a) | set(b):`
- **Breaks:** nothing, but it is the second-largest single allocation in the merge path.
  `set(a) | set(b)` over two 23-key dicts measures **4.92 µs standalone** — ~7% of a 70.4 µs
  merge — and allocates two intermediate sets plus the union per merge. At 208,462 merges
  that is ~1.0 s of pure set churn at 500k realistic input.
- **Trigger:** every merge. `dedup.py:192`.
- **Fix:** copy the larger side and only call `_pick` for genuinely shared keys:

  ```python
  merged = dict(a)
  for k, bv in b.items():
      merged[k] = _pick(k, a, b) if k in merged else bv
  ```

  This is behaviour-preserving: today, a key present only in `b` reaches `_pick`, fails
  `_is_missing(av)` on the `None` from `a.get(key)`, and returns `bv` (`dedup.py:168-169`).
- **Measured:** **-3.4%** (10k), **-8.3%** (100k), **-8.9%** (500k), output identical.

### S3 — `_trust_for` rebuilds the source set once per field instead of once per record

- **Where:** `dedup.py:148-152` calls `_sources_of` (`dedup.py:143-145`) on every
  invocation; `_sources_of` is called **19.2 times per merge** to re-split the same
  `record["source"]` string.
- **Breaks:** nothing; `raw.split("|")` + set comprehension costs 412 ns and allocates a
  fresh set every time, 4.0M times at 500k. `_trust_for` is fully determined by
  `(field, str(record.get("source") or ""))` — a domain of at most 23 fields × ~4 source
  strings, i.e. a cache of under 100 entries that never evicts.
- **Trigger:** every `_pick` that reaches `rank()`. `dedup.py:150`.
- **Fix:** `@lru_cache(maxsize=None)` on a `_trust_for(field, raw_source_string)` helper,
  called as `_trust_for(field, str(record.get("source") or ""))`.
- **Measured:** **-19.0%** (10k), **-20.8%** (100k), **-15.0%** (500k), output identical.
- **Note the interaction:** this and the `_pick` fast path attack the *same* 19.1% of work,
  so they are near-substitutes, not additive. Measured together: **-34.2% / -35.9% / -31.4%**
  at 10k / 100k / 500k — a real gain over either alone, but take the `_pick` line first
  because it is one line and needs no cache.

## Not a bug, but worth knowing

- **`_is_missing` itself is at the performance floor. Do not "optimise" it.** Two facts:
  1. **It allocates nothing.** `str.strip()` returns the *same object* when there is
     nothing to strip (verified: `v.strip() is v` → `True`), so the common case is a
     pointer bump. Over 2,000,000 calls across a mixed 8-value workload the net allocation
     was **144 bytes total, 0.000 B/call**. The only allocating path is a genuinely
     whitespace-padded value, which `load_master` cannot even produce (`main.py:87-89`
     converts `""` to `None`, and `csv` does not emit padded cells).
  2. **Its branch order is already optimal.** `dedup.py:86` tests `value is None` (64 ns)
     *before* `dedup.py:88` tests `isinstance(value, str)` and calls `.strip()`
     (112–182 ns). 14.4% of real arguments are `None` and they pay the cheap path.

- **Memoising `_is_missing` is a measured regression.** Replacing it with a memo table
  returning the correct cached answer cost **+35.0%** at 500k (12.656 s vs 7.957 s). A dict
  lookup on a `(type, value)` tuple — building the tuple, hashing the string — costs more
  than the 64 ns call it replaces. Same for an `lru_cache` over `_validity`
  (`dedup.py:120-140`): **+22%** at 500k. The lesson: *this predicate must be made to be
  called less, not called faster.* Neither S3 finding above is a cache; both are guards
  that skip the call entirely.

- **The cost is in the caller chain, not the predicate.** At 500k realistic input, `dedup()`
  attributes roughly as: `_pick` frame overhead + `rank` tuples + `_sources_of` + `_trust_for`
  + `_validity` ≈ 55%, `_is_missing` ≈ 17%, `normalize_phone` (`dedup.py:33-63`, 2.57 µs per
  phone) ≈ 8%, `normalize_name` (`dedup.py:66-69`, **9.26 µs**) ≈ 14%, `set(a)|set(b)` ≈ 7%.

- **`normalize_name` is 66× more expensive than `_is_missing` and runs in the same loop.**
  `dedup.py:66-69` does `unicodedata.normalize("NFKD", ...)` then
  `"".join(c for c in name if not unicodedata.combining(c))` — a Python-level *per-character*
  generator, measured at 9.26 µs/call — then another `re.sub`. It runs once per phone-less
  record (`dedup.py:212-214`). At 500k with ~28% phone-less that is ~1.3 s, i.e. the same
  order as all 11.06M `_is_missing` calls combined, from ~140k calls instead of 11M.
  Out of scope for this target, but it is the larger fish in this function's neighbourhood
  and `unicodedata.normalize("NFKD", ...)` + `str.translate` with a precomputed combining-mark
  table, or `re.sub(r"[̀-ͯ]", "", name, flags=re.U)`, would collapse it.

- **`dedup()` allocates proportionally and linearly — that part is fine.** `tracemalloc` peak
  during `dedup()`: **61.0 MB at 100k, 302.1 MB at 500k** → 610 and 604 bytes/record. Flat
  bytes-per-record is the signature of O(n) allocation with a bounded per-record footprint.
  The cause is `dict(record)` per new key (`dedup.py:210`, `dedup.py:218`) plus the
  per-merge `merged = {}` (`dedup.py:191`); note the *transient* garbage is much larger
  than the resident set — 208k merges × 2 sets of 23 keys — which is what the 302 MB figure
  understates.

- **`dedup()` is not on the critical path.** For calibration: 500k rows with ~30% carrying a
  website is ~150k HTTP GETs at 40 workers (`enricher.py`) ≈ 19 minutes, against 8 s
  (realistic) or 27 s (worst case) for `dedup()`. Optimising this stage is housekeeping,
  not risk reduction — which is why the three perf findings above are S3 rather than S2.
  The one exception is the S2, whose failure mode is 9.3 h against a 5 h timeout.

## Recommended order of work

1. **S2 first, and it is a one-line guard, not a rewrite:** bound the merged key set in
   `_merge` (`dedup.py:191-194`) — iterate a declared schema constant rather than
   `set(a) | set(b)` — and reject/normalise unknown keys at the ingest boundary
   (`load_master`, `main.py:77`). Cheap insurance against a 9.3-hour blowup, and it also
   subsumes the S3 set-allocation finding for the fixed-schema case.
2. **S3, the single largest speedup:** add the equality short-circuit to `_pick` immediately
   after `dedup.py:167`, excluding `source`. `-26.9%` at 500k, one line, output-identical.
3. **S3, if you want the rest of it:** `@lru_cache` on `_trust_for(field, raw_source_string)`.
   `-15%` to `-21%` alone, `-31%` to `-36%` when combined with step 2.
4. **Do not** memoise `_is_missing` or `_validity`. Both measured as regressions (+35% / +22%).
5. **Out of scope here, biggest single remaining item in the same loop:** rewrite
   `normalize_name` (`dedup.py:66-69`) to drop the per-character generator. ~1.3 s at 500k.

### Reproducing

Everything above is measured, not estimated. The instrumentation is monkeypatching of module
globals (`D._is_missing`, `D._pick`, `D._merge`, `D._trust_for`) plus `time.process_time()`
around `D.dedup(records)` with `gc.disable()`; no file in the repo was created or modified.
Timing must use CPU time and min-of-N — wall-clock min-of-N on this box varies by up to 35%
run-to-run because of the concurrent audit agents, which is what produced an apparently
contradictory "+15% for inlining `_is_missing`" in an early pass before switching to
interleaved CPU-time measurement.