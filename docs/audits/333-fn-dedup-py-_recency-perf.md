# 333 — `_recency` perf: O(1), allocates nothing, and is 0.7% of the stage. Don't optimize it.

## Verdict

`_recency` (`dedup.py:155-156`) is a single `dict.get` plus a `str()` that returns the
*same object* it was given. It is **O(1) per call**, **O(n) in total** (measured 6.9–7.1
calls per merge, constant across 10k → 500k records), and **allocates zero bytes** for any
`scraped_at` that is already a `str` — which is every value the pipeline can produce
(`scrapers/base.py:27`, `scrapers/google_places.py:173`, `main.py:86-104`). At 500k records
it accounts for **124 ms of a 16 s `dedup()` run (0.77%)**. There is no optimization
available here and none is worth making.

The single change with the largest speedup in `_recency`'s call path is *not* about
`_recency`: it is memoizing `_trust_for` (`dedup.py:148-152`), which shares the same tuple
in `rank()` (`dedup.py:179-185`), is called **4× more often** (32 vs 8 per merge) and costs
**9–15× more per call**. Measured end-to-end: **2.0× on `dedup()` at 500k records**
(21.1 s → 10.7 s), with byte-identical output. Even that is 10 seconds out of a
300-minute workflow budget, so this whole file is S3 — details in *Severity defence*.

## Method and measurement conditions

Python 3.14.8, macOS (darwin), repo at commit `99493b9` (`git diff -- dedup.py` empty
during this audit; `_recency` re-verified at `dedup.py:155-156` afterwards). Read-only
auditing: no scrapers run, no network, no files written outside this report. Benchmarks
imported `dedup.py` unmodified; candidate rewrites were local functions in a scratch
process and their outputs were compared to `dedup()`'s with `sorted(sorted(r.items()))`
equality before any timing was believed (both variants: identical).

**Caveat that matters for reading the numbers:** this box is shared and heavily loaded.
Run-to-run spread of an *unchanged* baseline within one process was **1.5–1.8×** (see
the interleaved A/B below). Absolute seconds here are therefore ±50%; the *ratios within
a single interleaved run* are the trustworthy part. Microbenchmarks below are min-of-5 ×
1M calls in a tight loop and are quoted as a range across two sessions where they moved.

Dataset: 23-key `BusinessRecord` rows per `scrapers/base.py:5-28`, 45% with a national-format
phone, 55% without (→ name-key path `dedup.py:211-218`), Arabic-free names, mixed
`source`/`scraped_at`, `None` in `facebook`/`whatsapp`/`linkedin`. "dup50%" = half the rows
collide on an existing key, which is what drives `_merge` → `_pick` → `rank` → `_recency`.

## Findings

### S3 — `_recency` is O(1) per call, O(n) total, and allocates nothing

- **Where:** `dedup.py:155-156`; called only from `rank()` at `dedup.py:183`.
- **Complexity:** one `dict.get` + `str()`. Measured 79.8 ns on a 5-key dict vs 85.2 ns on a
  23-key dict — no growth with dict size, i.e. genuinely hashed O(1), not a scan. Total
  calls = `2 × |volatile fields populated on both sides| × merges`, and `_VOLATILE`
  (`dedup.py:114`) has exactly 4 members `{rating, review_count, website_live, website}` →
  **≤ 8 calls per merge**, and `merges ≤ n`. So O(n), never O(n²).
- **Allocation:** `D._recency(r) is r["scraped_at"]` → `True` (identity preserved, CPython
  `PyObject_Str` fast path), so **0 bytes allocated**. When `scraped_at` is `None`/absent
  (`main.py:88-89` maps blank CSV cells to `None`) it returns the code constant `""`, also
  0 bytes. Only a non-`str` truthy value allocates: measured 128.3 ns for `int` vs 79.8 ns
  for `str` — a 48 ns/call penalty that cannot occur on today's data paths, and would still
  be 0.07 s at 500k.
- **Evidence (call counts, instrumented by wrapping `D._recency`, real `dedup()` run):**

  | records | dup rate | `dedup()` | merges | `_recency` calls | calls/merge | `_recency` @90.5 ns | share |
  |--------:|---------:|----------:|-------:|-----------------:|------------:|--------------------:|------:|
  | 10,000 | 0% | 0.03 s | 1 | 6 | – | 0.0005 ms | 0.00% |
  | 10,000 | 50% | 0.33 s | 4,007 | 28,428 | 7.09 | 2.6 ms | 0.77% |
  | 100,000 | 0% | 0.41 s | 103 | 706 | 6.85 | 0.06 ms | 0.02% |
  | 100,000 | 50% | 3.52 s | 38,759 | 274,180 | 7.07 | 24.8 ms | 0.71% |
  | 500,000 | 0% | 2.71 s | 2,731 | 18,830 | 6.89 | 1.7 ms | 0.06% |
  | 500,000 | 50% | 16.10 s | 194,491 | 1,375,052 | 7.07 | **124.4 ms** | **0.77%** |

  `calls/merge` is flat at 6.85–7.09 from 10k to 500k. Total calls track `n` linearly. That
  is the O(n) claim, measured rather than asserted.
- **Trigger (the case that maximizes `_recency`):** two fully-populated 23-key records sharing
  a phone key. `dedup.py:192` iterates `set(a) | set(b)`, so all 23 fields reach `_pick`;
  the 4 volatile ones fall through to `rank()` (`dedup.py:187`), which is called twice per
  field → `4 × 2 = 8` `_recency` calls per merge. Instrumented count on exactly this input:
  `{'recency': 8, 'trust': 32, 'sources': 34, 'validity': 32, 'pick': 23, 'merge': 1}`.
- **Fix:** none. If you want the code to say so, add the comment
  `# O(1); called <= 8x per _merge, ~7 measured` — do not restructure it for speed.

### S3 — The largest single speedup near `_recency` is memoizing `_trust_for`: measured 2.0×

- **Where:** `_trust_for` `dedup.py:148-152` → `_sources_of` `dedup.py:143-145`, reached from
  `rank()` `dedup.py:181`. `_pick` calls `rank` twice per comparable field
  (`dedup.py:187`), so 2 `_trust_for` per field per merge.
- **Breaks:** nothing breaks — this is a cost problem, not a correctness one. But
  `_sources_of` (`dedup.py:144-145`) does `str(...)`, `.split("|")` and a set comprehension,
  i.e. **it allocates a fresh set on every call**, 34 times per merge. Measured cost per
  call: `_sources_of` **626.5 ns**, `_trust_for` **1411.5 ns** (first session) /
  **~800 ns** (second, loaded session) → `_trust_for`/`_recency` ratio **8.9–15.6×**, and
  it is called 4× more often. That makes `_trust_for` the single largest line item in the
  merge path.
- **Trigger:** any run where new scraper rows collide with master rows — 500k records with
  50% collisions. Counted in that run: `_trust_for` **6,433,322** calls, `_sources_of`
  **6,822,304** allocations, vs `_recency` **1,375,052**.
- **Fix:** resolve `source → best trust per field` once per distinct source *set* (there are
  at most 2³ = 8 of them for `osm|google_places|wikidata`) and look it up twice per merge
  instead of recomputing 32 times:
  ```python
  _TRUST_BY_SOURCES: dict[frozenset[str], dict[str, int]] = {}
  def _trust_row(srcs: frozenset[str]) -> dict[str, int]: ...   # built once per new srcs
  # in _merge:  ta, tb = _trust_row(frozenset(_sources_of(a))), _trust_row(frozenset(_sources_of(b)))
  # in rank:    ta.get(field, _DEFAULT_TRUST)
  ```
  Hoist `_recency(a)`/`_recency(b)` in the same breath (`_merge` → 2 calls instead of 8) and
  precompute `field in _VOLATILE` out of the per-field path while you are there.
- **Measured (interleaved, 4 rounds, same process, 100k records / 50% dup, `gc.collect()`
  before each):**

  | variant | min | median | max | spread | speedup (med/med) |
  |---|---:|---:|---:|---:|---:|
  | baseline `dedup.py:197` | 6.11 s | 7.14 s | 9.28 s | 1.52× | 1.00× |
  | **v1** hoist `_recency` only | 4.42 s | 5.94 s | 8.13 s | 1.84× | 1.20× *(noise — see below)* |
  | **v2** memo `_sources_of` + trust table | 2.91 s | 3.63 s | 5.34 s | 1.84× | **1.97×** |
  | baseline, `gc.disable()` | 4.98 s | 6.14 s | 9.10 s | 1.83× | 1.16× |

  Confirmed at 500k in a separate process: baseline **21.12 s** → v2 **10.65 s** = **1.98×**,
  same 305,509 output rows. v2's predicted saving from the call counts alone is ~9 s of a
  16–21 s run, so 2× is what the arithmetic says and 2× is what it measures.
- **Do not believe v1.** Hoisting `_recency` saves 6 calls × 90.5 ns = 0.54 µs per merge =
  **0.09 s of 21 s (0.4%)**. The measured "1.20×" is inside the baseline's own 1.5×
  run-to-run spread; the honest number is **1.003×, i.e. unmeasurable**. This is the trap
  this file exists to head off: `_recency` is the *readable* line in `rank()`, and it is the
  cheapest one.

### S3 — `dedup.py:227-228` is provably dead code that costs 1.4–2.4 µs per junk-phone row

- **Where:** `dedup.py:226-230`.
- **Breaks:** it cannot drop anything, but it still re-runs `normalize_phone` for every
  name-indexed record that has a truthy `phone` string. Proof: a record only reaches
  `name_index` when line 205 produced an empty key, i.e. either no phone or a phone whose
  `normalize_phone` returned `""` (`dedup.py:45-46`, and `normalize_phone` is idempotent on
  such junk). So at line 228 `normalize_phone(raw, ...)` returns `""`, and
  `captured_phones` (`dedup.py:223`) contains only non-empty keys → `"" in captured_phones`
  is always `False` → `continue` never runs.
- **Trigger:** instrumented 200k-record runs with junk phone strings injected
  (`"12"`, `"+961"`, `"abc"`, `"0"`, `"+96 1"`):

  | junk-phone share | dup | phone_index | name_index | guard reached | guard fired | wasted `normalize_phone` |
  |---:|---:|---:|---:|---:|---:|---:|
  | 0% | 0% / 50% | 90,027 / 122,418 | 109,973 / 77,582 | 0 | **0** | 0 ms |
  | 10% | 0% / 50% | 89,883 / 122,744 | 110,117 / 77,256 | 20,031 / 10,093 | **0** | 29.9 / 17.7 ms |
  | 30% | 0% / 50% | 90,000 / 122,499 | 110,000 / 77,501 | 59,979 / 30,139 | **0** | 120.4 / 38.7 ms |

  60,000 attempts, zero filters. At 500k records with 10% junk phones that is ~0.3 s of
  provably wasted `normalize_phone` calls (measured 1.43 µs for `"+961 1 234 567"`, 1.77 µs
  for `"03 123 4567"`; 2.37–3.04 µs in a second, loaded session).
- **Fix:** delete lines 227-228, or store the key alongside the value at
  `dedup.py:214-218` if the guard is ever meant to be real.

### S3 — `dedup()` allocates ~1 dict + 2 sets + 23 closures + 64 tuples + 64 strings per merge

- **Where:** `_merge` `dedup.py:190-194`, `rank` `dedup.py:179-185`.
- **Breaks:** nothing; it is why per-merge cost (measured 83–109 µs) is ~20× the sum of the
  "useful" work. Per merge the code builds: one result dict (~201 ns, `dict()` of 23 keys),
  `set(a) | set(b)` (2 set allocations), one `rank` closure **per field** (23 `MAKE_FUNCTION`
  + cell objects), 2 rank tuples per field, and `str(value)` for 64 values
  (`dedup.py:184`) — `str()` on a non-`str` (e.g. `lat`, `review_count`) allocates.
- **Trigger:** the 500k/50% run: 194,491 merges × ~150 objects ≈ 29 M short-lived
  container objects, with 1044 MB peak RSS.
- **Fix:** make `rank` a module-level function taking `(field, rec, value)` (kills 23
  closures/merge) and drop the `str(value)` in favour of a comparison only when the earlier
  tuple elements tie. Both fold into the v2 change above; neither is worth a separate PR.

### S3 — `dedup()` is O(n) with a flat per-record cost — confirmed, not O(n²)

- **Where:** `dedup.py:197-232`.
- **Breaks:** nothing. This is the negative result the brief asked for, stated with numbers
  so nobody has to re-derive it. Fixed-key dict indexing (`dedup.py:207`, `:215`) plus
  "compare only against the record already stored under this key" means no pairwise
  comparison, so there is no quadratic term.
- **Trigger:** same 50%-dup dataset, prefix slices of one 500k generation (so the
  collision structure accumulates realistically):

  | records | `dedup()` | µs/record | merges | µs/merge | step vs previous |
  |---:|---:|---:|---:|---:|---|
  | 25,000 | 1.01 s | 40.3 | 9,888 | 101.8 | – |
  | 50,000 | 2.13 s | 42.5 | 19,565 | 108.6 | 2.11× for 2× n |
  | 100,000 | 4.24 s | 42.4 | 38,759 | 109.4 | 2.00× for 2× n |
  | 200,000 | 7.29 s | 36.4 | 77,567 | 94.0 | 1.72× for 2× n |
  | 400,000 | 16.24 s | 40.6 | 155,401 | 104.5 | 2.23× for 2× n |
  | 500,000 | 26.31 s | 52.6 | 194,491 | 135.3 | 1.62× for 1.25× n |

  µs/record is flat within ±20% over a 20× size increase (40.3 → 52.6); an O(n²) term would
  have shown 400× growth. The drift at the top is allocator/GC/cache pressure, not an
  algorithmic term. Same shape with the index path alone (0% dup): 3.0 µs/record at 10k,
  4.1 at 100k, 5.4 at 500k — a 1.8× constant growth over 50× n, i.e. mild
  "constant × n^0.13" behaviour, which is what generational GC and allocator pressure look
  like, not a hidden loop.
- **Fix:** none needed for asymptotics.

## Not a bug, but worth knowing

- **The 500k memory ceiling is ~2.1 KB of RSS per input record, not CPU.** Measured:
  500k records → 729 MB resident after construction, **1044 MB peak during `dedup()`**
  (input dict payload 416 MB + one `dict(record)` copy per surviving key + index overheads).
  So `main.py:179-180` (`combined = raw_filtered + master` then `dedup`) needs roughly
  **1.0–1.2 GB for a 500k master** before enrich or CSV writing. `runs-on: ubuntu-latest`
  (16 GB) is comfortable; a 7 GB runner caps out near ~3.3M records. `load_master`
  (`main.py:77-105`) holding every row as a 23-key dict is the reason the coefficient is
  ~2 KB and not ~300 B — a dict-per-row master is the single biggest lever on this stage's
  footprint, far bigger than anything in `rank()`.
- **`gc.disable()` is not the fix.** Interleaved measurement: 1.16× median / 1.23× min on one
  pass, but 0.86× (i.e. *slower*) in another round. Inside the noise band. Don't ship it.
- **`_recency` would start allocating only if `scraped_at` stops being a `str`.**
  `docs/audits/045-scraper-layer-refactor.md:159` proposes
  `scraped_at: datetime.datetime`; if that lands, every call allocates a fresh ~60-byte
  string (measured 128.3 ns vs 79.8 ns) and the `max(av, bv)` at `dedup.py:177` becomes a
  `datetime` comparison. Still ~0.1% of the stage — but do it in the same PR as the
  `_pick` signature change so it is measured once.
- **`normalize_name` is the biggest cost on the *index* path, not the merge path.**
  `dedup.py:66-69` does NFKD + a per-character generator + `re.sub`: measured 3.65 µs
  (first session) / 6.73 µs (second) per call, vs `normalize_phone` at 1.43–3.04 µs. With
  55% of records taking the name path, that is **1.85 s of the 2.71 s index-only 500k run**.
  A `str.translate` table over U+0000–U+036F (111 code points, built once at import)
  produced byte-identical output on 8 samples including `Café`, `Pâtisserie`, `Crème
  brûlée` and Arabic text, at 3.475 µs → **1.94× on that function, ~0.9 s saved at 500k**.
  Smaller prize than the `_trust_for` memo but it is on the path that runs at full 500k
  scale, whereas `_trust_for` only runs on merges. Out of my assigned target; flagging it for
  whoever owns `normalize_name`.
- **Where the 500k wall actually is.** `enricher.check_websites` (`enricher.py:225-262`) does
  one GET per website at 40 workers with `timeout=8` (`enricher.py:175`). At 500k websites:
  ~52 min at a 250 ms average response, and **~27.8 hours** if requests run to the 8 s
  timeout — 5.6× over `timeout-minutes: 300` (`.github/workflows/scrape.yml:25`). Compare
  with `dedup()`'s 16–21 s. If you are looking for the thing that breaks at 500k, it is
  there, not in `dedup.py`.

## Severity defence

Everything above is S3, and the reason is arithmetic rather than modesty: the entire
`dedup()` stage costs **16–21 s** on a 500k-record master with a worst-case 50% collision
rate, inside a workflow whose budget is **300 minutes** (`scrape.yml:25`) and whose
dominant cost is `enricher.check_websites`. Even a perfect 2× here removes 10 s. Per the
brief's own table, S2 means *materially degrades cost or operability at scale* — 10 s of
1800 s is 0.6% and does not qualify. I found **no** S1 in this lens: nothing here produces
wrong results or kills a run, and `_recency` is not on any path where input size changes its
complexity class.

Two things would promote the `_trust_for` finding to S2, and neither is true today:
(i) the merge path becoming the dominant cost, e.g. re-merging a full multi-million-row
master every run (the architectural fix is incremental/delta state — see
`docs/audits/035-incremental-scraping.md` — not micro-optimizing `rank()`); or
(ii) `dedup()` being moved off the single-threaded critical path into a per-run budget that
already has < 30 s of slack.

## Recommended order of work

1. **Nothing, for `_recency`.** Add the one-line complexity comment and move on.
2. **Memoize the source→trust resolution** (`dedup.py:143-152`, consumed at `dedup.py:181`)
   and make `rank` module-level with `_recency`/`_VOLATILE` hoisted into `_merge`. One PR,
   ~2.0× on the merge path, output verified identical.
3. **Delete the dead guard at `dedup.py:227-228`** (0 filters in 60,000 instrumented
   attempts).
4. **Only if dedup ever shows up in a profile:** stop re-reading and re-merging the whole
   master each run (`main.py:150`, `:179-180`), and make the master row representation
   smaller than a 23-key dict. Those two move the 1.04 GB / 21 s; nothing in `rank()` does.