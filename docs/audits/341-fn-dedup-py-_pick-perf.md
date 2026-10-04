# 341 — `_pick` at 500k records: O(1) per call, linear overall — but it rebuilds a closure to choose one field

## Verdict

The algorithmic question has a clean answer: `_pick` (`dedup.py:159-187`) is **O(1) per call in
the number of businesses**, `dedup()` (`dedup.py:197-232`) is therefore **O(n) overall**, and it
is **not** O(n²). I confirmed this by measurement, not just by reading: doubling n from 200k to
400k costs 2.05x, not 4x. `_pick` also does **not** allocate proportionally to n — its
allocation is a fixed ~1.1 KB per call.

The real cost is the constant. Deciding *one* field of *one* record makes ~30 Python-level
function calls and allocates 1097 bytes, because the function **rebuilds a nested `def rank`
closure on every single call** (`dedup.py:179`) and then **always evaluates both complete rank
tuples** even when the first component already decides the winner (`dedup.py:187`). The single
change with the largest speedup is to **compare the rank components lazily and short-circuit**:
**1.52x on its own**, and **1.89x on `_pick` / 2.67x on a whole `_merge`** once combined with
hoisting the closure out.

Severity is **S3**, and I want to defend that plainly rather than inflate it. In the realistic
steady state (`main.py:150` + `main.py:179-180` feed the cumulative master back through
`dedup()` every run), `_pick` accounts for **~1.9 s of a 6.8 s `dedup()` call at a 500k master** —
0.04% of the 300-minute workflow budget. This is a wasteful hot inner loop, not an
operational blocker. Optimising it is real engineering value (2.67x, 5.5x less allocator
traffic) but it will not unblock anything.

---

## Complexity, derived from the source

| Question | Answer | Anchor |
|---|---|---|
| `_pick` per call, in `n` | **O(1)** — no loop over records anywhere in the body | `dedup.py:159-187` |
| `_pick` per call, in value length | O(L): one `.strip()` + one regex + one `.split("\|")` | `dedup.py:89`, `125`, `127`, `145` |
| Only iteration `_pick` reaches | `for src in _sources_of(record)`, ≤3 elements (closed set of sources) | `dedup.py:150-151` |
| `_merge` per call | **O(23)**, constant — both operands are flat 23-key dicts, never cluster-sized | `dedup.py:190-194` |
| `_pick` calls per merge | exactly `len(set(a) \| set(b))` = 23 | `dedup.py:192-193` |
| Total `_pick` calls in `dedup()` | `23 × merges`, and `merges ≤ n − 1` | `dedup.py:197-218` |
| **`dedup()` overall** | **O(n)** — hash-map keyed insert/lookup; each arriving record merges at most once | `dedup.py:198-218` |

The reason there is no quadratic term: `phone_index` / `name_index` (`dedup.py:198-199`) are
dict-keyed, so a cluster of size *k* performs *k − 1* merges, and `_merge` is O(23) **regardless
of k** because it never walks the cluster — only the union of the two argument dicts' keys. Sum
of (k − 1) over all clusters ≤ n − 1. The dedup keys themselves are O(n) with a set lookup at
`dedup.py:207` / `215` / `228`.

### Empirical confirmation of linearity

`dedup()` with **every record colliding into one cluster** (`collide=1.0` — the upper bound on
merge work), min-of-3 reps:

| n | step | best secs | ratio | linear would be | µs/record | ns/pick |
|---:|:---|---:|---:|---:|---:|---:|
| 10,000 | — | 0.820 | — | — | 82.0 | 3567 |
| 50,000 | ×5 | 4.754 | 5.79x | 5x | 95.1 | 4134 |
| 100,000 | ×2 | 10.019 | 2.11x | 2x | 100.2 | 4356 |
| 200,000 | ×2 | 27.827 | 2.78x | 2x | 139.1 | 6049 |
| 400,000 | ×2 | 57.014 | 2.05x | 2x | 142.5 | 6197 |
| 500,000 | — | ~68 | — | — | 136 | 5925 |

Ratios track the input steps. The `ns/pick` creep from 3567 → 6197 (+74%) across the range is
**working-set pressure, not an algorithmic term** — 400k live 23-key dicts is several hundred MB
and the accumulator dict plus both operand dicts fall out of L2.

**GC is not the superlinear term.** On 3.14 the cyclic collector ran **zero** times during a 59 s
500k run (`gc.get_stats()` deltas all 0; `gc.get_threshold() == (2000, 10, 10)`, enabled). I
checked this specifically because GC thrashing over a large container population is the usual
cause of this shape, and here it is not.

---

## Method and caveats — read before trusting the seconds

- **Python 3.14.8**, macOS 14, 12 cores. `pyproject.toml:39` sets `target-version = "py312"`
  and `pyproject.toml:65` sets `python_version = "3.12"`; **no 3.12 interpreter was available on
  this machine**, so every number is 3.14. The one material difference is noted in S3-1.
- **The machine was heavily contended during measurement** (load average 89 on 12 cores, with
  other audit agents running). Absolute wall-clock seconds are contaminated by up to ~2x — I saw
  the same 500k workload land at 34.9 s, 68.1 s and 130.3 s in three runs. **Treat absolute
  seconds as ±2x; treat the ratios as the result.** Every comparison below is
  **interleaved A/B within one process, min-of-9 to min-of-15 alternating reps**, so machine
  drift and contention cancel between the variants being compared.
- `_pick` variants were verified output-identical (value *and* type) against the real function
  over a **24-record grid × 24-record grid × union-of-keys = 7537 field-pairs, 0 mismatches**,
  covering blank/`None`/whitespace values, `rating=0`, `rating=9.9`, `rating="abc"`,
  `lat="33.8"`, non-URL websites, malformed emails, `website_live=False`, unknown source tags,
  and the 3-source `"osm|wikidata|google_places"` union.
- No scrapers were run and no network calls were made. `dedup.py` was imported read-only;
  variants were swapped in via `dedup._pick = ...` inside the benchmark process only. No repo
  file other than this report was modified.

---

## Findings

### S3-1 — `_pick` rebuilds the `rank` closure on every call, and on 3.14 that is *two* function objects

- **Where:** `dedup.py:179-187`. The `def rank` is nested inside `_pick`, so it is re-executed
  — and two function objects constructed — on every one of the 11.5 million calls a 500k worst
  case makes.

- **Evidence (`dis.dis(_pick)`):**
  ```
  179  LOAD_CONST  4 (<code object __annotate__ ... line 179>)
       MAKE_FUNCTION
       LOAD_FAST_BORROW  0 (field)
       BUILD_TUPLE  1
       LOAD_CONST  5 (<code object rank ... line 179>)
       MAKE_FUNCTION
       SET_FUNCTION_ATTRIBUTE  8 (closure)
       SET_FUNCTION_ATTRIBUTE  16 (annotate)
       STORE_FAST   5 (rank)
  ```
  Two `MAKE_FUNCTION`s, one closure tuple, one `MAKE_CELL` for `field`. The first `MAKE_FUNCTION`
  is for `__annotate__` — a PEP 649 lazy-annotation helper CPython 3.14 synthesises from
  `rank`'s `-> tuple` annotation on line 180. **Its only purpose is to build a 4-tuple that is
  immediately compared against another 4-tuple.**

- **Breaks:** nothing functionally. It is pure overhead: **281 bytes and 2 function objects per
  call, allocated only to be freed 400 ns later.** Measured by `tracemalloc` high-water (median
  = min = deterministic byte counts):

  | `_pick` call | bytes allocated |
  |---|---:|
  | `_pick("scraped_at", …)` (early return) | 104 |
  | `_pick("source", …)` (early return) | 856 |
  | `_pick("region", …)` | 1097 |
  | `_pick("name", …)` | 1097 |
  | `_pick("lat", …)` | 1142 |
  | `_pick("rating", …)` | 1141 |
  | `_pick("website", …)` | 2031 |

  Removing the nested `def` and inlining the two tuples: **1097 → 592 B (−46%), 1.19x.**

- **Trigger:** any collision. `_merge` calls `_pick` once per key (`dedup.py:193`), so one merge
  constructs 46 function objects. At 500k all-colliding that is 499,999 × 23 = **11,499,977
  `_pick` calls → ~11.5 GB of allocator traffic and ~150M object allocations**, none retained.
- **Fix:** delete the nested `def` and write the two tuples inline (see S3-2 for the full
  replacement). Expected 1.19x on its own, 1097 → 592 B/call.
- **Version caveat:** on 3.12 the annotation is evaluated eagerly as a `LOAD_GLOBAL tuple` rather
  than via a synthesised `__annotate__`, so expect **one** `MAKE_FUNCTION`, not two, and a
  correspondingly lower byte figure (~850 B). The structural finding — a closure built per call
  in the hottest loop of the merge phase — is unchanged.

### S3-2 — Both rank tuples are always fully built, so a decision that trust alone settles still pays for validity, recency and two string conversions

- **Where:** `dedup.py:187` — `return av if rank(a, av) >= rank(b, bv) else bv`.
- **Breaks:** nothing; it is waste. `rank` is a 4-tuple `(trust, validity, recency, str(value))`
  compared lexicographically (`dedup.py:179-185`). Python's tuple `>=` short-circuits at the
  first differing element — but only **after both tuples have been constructed in full**. For the
  common case where the two records come from different sources, `_trust_for` already returns
  different numbers (e.g. `google_places` website = 2 vs `osm` website = 3, `dedup.py:100,103`),
  and everything after element 0 is computed for nothing.
- **Trigger (real function, real field, real trust split):**
  ```python
  a = {"source": "google_places", "website": "https://a.example"}   # trust 2, validity 3
  b = {"source": "osm",           "website": "http://a.example"}    # trust 3, validity 3
  # _pick("website", a, b) -> b on element 0 alone.
  # It still builds: 2x rank tuples, 2x _sources_of (split+set), 2x _validity
  #   (2 regex .match objects), 2x str(), 1x the nested closure.
  ```
- **Measured (min-of-15 interleaved, 6000 iterations × 13 fields, `ns` per `_pick` call):**

  | | eager tuple compare | lazy short-circuit |
  |---|---:|---:|
  | nested `def rank` (**current**) | **4251 ns / 1097 B** | 2792 ns / 1025 B — **1.52x** |
  | tuples inlined, no closure | 3578 ns / 592 B — 1.19x | **2249 ns / 512 B — 1.89x** |

  The two changes are complementary and slightly super-additive (1.52 × 1.19 = 1.81, measured
  1.89). **Whole-`_merge` effect: 46.3 µs → 17.3 µs per merge = 2.67x.**
- **Fix — this is the single change with the largest speedup.** Replace `dedup.py:179-187` with:

  ```python
  volatile = field in _VOLATILE

  ta, tb = _trust_for(field, a), _trust_for(field, b)
  if ta != tb:
      return av if ta > tb else bv

  va, vb = _validity(field, av), _validity(field, bv)
  if va != vb:
      return av if va > vb else bv

  if volatile:
      ra, rb = _recency(a), _recency(b)
      if ra != rb:
          return av if ra > rb else bv

  return av if str(av) >= str(bv) else bv
  ```

  Verified: 7537 field-pairs, 0 mismatches, and `_merge` output identical on a 14×14 record grid.

- **Projected effect on the merge phase at 500k all-colliding (using the measured 46.3 µs/merge):**

  | n | merges | current | optimised |
  |---:|---:|---:|---:|
  | 10,000 | 9,999 | 0.5 s | 0.2 s |
  | 100,000 | 99,999 | 4.6 s | 1.7 s |
  | 500,000 | 499,999 | **23.1 s** | **8.7 s** |

### S3-3 — `_sources_of` is recomputed twice per field and 46 times per merge, and `str.split` + a set comprehension is the single most expensive helper in the chain

- **Where:** `dedup.py:143-145`, reached from `dedup.py:150` inside `_trust_for`, called
  twice per `_pick` from `dedup.py:181`, and therefore 46 times per `_merge`.
- **Measured helper costs (`ns` per call, min-of-N batched):**

  | helper | ns |
  |---|---:|
  | `_sources_of(record)` | 253 |
  | `_trust_for("website", rec)` | 549 |
  | `_validity("website", url)` | 701 |
  | `_validity("rating", 4.5)` | 263 |
  | `_is_missing("x")` | 93 |

  `_sources_of` allocates **480 B** and is ~11% of a full `_pick` call.
- **Fix:** the `source` field is a **closed set** — `_SOURCE_TRUST` (`dedup.py:97-110`) defines
  exactly three, and `_sources_of` splits on `"|"`. Memoise the split on the raw string:
  ```python
  _SRC_CACHE: dict[str, frozenset[str]] = {}
  def _sources_of(record: dict) -> frozenset[str]:
      raw = str(record.get("source") or "")
      s = _SRC_CACHE.get(raw)
      if s is None:
          s = _SRC_CACHE[raw] = frozenset(x for x in raw.split("|") if x)
      return s
  ```
  Bounded by construction — the key space is whatever distinct `source` strings the scrapers
  emit, which is 3. **Measured: 2249 → 1827 ns/pick (total 2.33x vs current), and 512 → 200 B/call
  (−82% allocation).** Note `_pick` already depends on the return being iterable only
  (`dedup.py:174`, `151`), so `frozenset` is a safe substitution for `set`.
- **The bigger version of this fix:** `_merge` could compute `_sources_of` and `_recency` once per
  operand and reuse them across all 23 fields, instead of 46 times. I built and verified this
  shape — `_merge` output identical on a 14×14 grid — and it lands at **2.14x** on a whole
  `_merge` (76.9 µs → 34.6 µs in that run). It is more invasive than S3-2 and buys roughly what
  S3-2 + S3-3 already buy between them, so I rank it below them.

### S3-4 — `_is_missing` runs four times per `_pick` where two suffice, because `_validity` re-derives a fact `_pick` already proved

- **Where:** `dedup.py:168-171` establishes that neither `av` nor `bv` is missing. `_validity`
  then calls `_is_missing` again at `dedup.py:122` — twice, because `_validity` is called twice.
- **Evidence (`cProfile` call counts per 1000 `_pick("website", …)` calls):**
  ```
     10.00  <method 'get' of 'dict' objects>
      6.00  <method 'strip' of 'str' objects>
      4.00  _is_missing            (dedup.py:79)
      4.00  builtins.isinstance
      2.00  rank                   (dedup.py:179)
      2.00  _validity              (dedup.py:120)
      2.00  _trust_for             (dedup.py:148)
      2.00  _sources_of            (dedup.py:143)
      2.00  <method 'split' of 'str' objects>
      2.00  <method 'match' of 're.Pattern' objects>
  ```
  ~30 Python-level calls to decide one field. Two of the four `_is_missing` calls are provably
  dead: on the path reaching `dedup.py:181`, both values are non-empty by construction.
- **Measured:** I tried inlining this guard as a `_validity_present` helper with the `_is_missing`
  branch removed. It was **0.97x — i.e. within noise, no gain.** The `isinstance` + call
  overhead is cheaper than passing an extra argument. **Not worth doing.** Reporting it as a
  null result so nobody else spends the time.

### S3-5 — `_SOURCE_TRUST.get(src, {})` allocates a throwaway dict on every call, including on the hit path

- **Where:** `dedup.py:151` — `best = max(best, _SOURCE_TRUST.get(src, {}).get(field, _DEFAULT_TRUST))`.
- **Breaks:** nothing. CPython evaluates the `{}` argument eagerly, so a fresh empty dict is
  built and discarded **even when `src` is a key** — i.e. on the overwhelmingly common path.
  Measured at **128 B per `.get()` with a hit, vs 64 B for `_SOURCE_TRUST[src]`** (64 B is the
  noop-lambda floor). Two per `_pick` call, 46 per `_merge`.
- **Fix:** bind a module-level `_NO_TRUST: dict[str, int] = {}` next to `_SOURCE_TRUST` and use
  it as the default, or better use `.get(src)` + a `None` check so no dict is built on the hit
  path at all:
  ```python
  row = _SOURCE_TRUST.get(src)
  if row is not None:
      v = row.get(field, _DEFAULT_TRUST)
      if v > best:
          best = v
  ```
  Folded into S3-3's rewrite of `_sources_of`/`_trust_for`; worth ~64–128 B/call on its own.

---

## What the 500k picture actually looks like operationally

The brief asks for timings at 10k / 100k / 500k. Those two tables answer it from opposite ends:

**Worst case — every record lands in one cluster** (all-colliding, which is the upper bound on
merge work; §Complexity above). `_pick` is essentially all of `dedup()`: **0.82 s / 10 s / ~68 s
at 10k / 100k / 500k.**

**Realistic case — a mature master plus one new scrape.** This is the shape the pipeline actually
reaches, because `main.py:150` loads `all_businesses.csv` and `main.py:179-180` pushes
`raw_filtered + master` back through `dedup()`. The master is *already* deduped, so master
records do not collide with each other: **merges ≈ number of new businesses already seen**, not
the corpus size. Measured, min-of-3 interleaved:

| master | new | total in | merges | `dedup()` now | after S3-2+3 | speedup | `_pick` share |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 10,000 | 2,000 | 12,000 | 1,000 | 0.137 s | 0.087 s | 1.58x | 54% |
| 100,000 | 20,000 | 120,000 | 10,000 | 1.959 s | 1.284 s | 1.53x | 38% |
| 500,000 | 50,000 | 550,000 | 25,000 | **6.827 s** | 5.185 s | 1.32x | 27% |

At the 500k steady state `_pick` is **~1.9 s of a 6.8 s `dedup()` call**. The remaining 73% is
`normalize_phone` called once per record at `dedup.py:205` and again at `dedup.py:228`, the
`dict(record)` copies at `dedup.py:210` / `218`, and `load_master`'s CSV parse
(`main.py:85-98`). `_pick` is *not* the bottleneck — it is the largest *individually
addressable* item in the merge path, which is why it is S3 and not S2.

The all-colliding 500k case is **not** reachable through `main.py`: master records are deduped
before they are written. It becomes reachable if `data/all_businesses.csv` is ever hand-edited,
or externally concatenated, or if the dedup key logic loosens (relaxing `normalize_phone`'s
`_MIN_DIGITS` at `dedup.py:30`, or the name key at `dedup.py:214`, grows cluster sizes directly).
Worth keeping the 23 s figure in mind as the cliff, not as the expectation.

---

## Not a bug, but worth knowing

- **`_pick` is not the quadratic risk in `dedup.py`.** If you are triaging for an O(n²), look at
  the string-keyed work instead: `normalize_name` (`dedup.py:66-69`) and `_extract_city`
  (`dedup.py:72-76`) both run per phone-less record, and `normalize_phone` runs twice per record
  (`dedup.py:205` and again at `dedup.py:228`) — all O(L) in one string, all linear in aggregate,
  but together they are the larger share of `dedup()` at 500k.
- **The whole corpus is resident.** At a 500k master, `load_master` (`main.py:85-98`) plus
  `combined = raw_filtered + master` (`main.py:179`) plus the rebuilt `dict(record)` per key
  (`dedup.py:210`, `218`) means ~550k live 23-key dicts ≈ 0.7–1 GB RSS. That is what produces the
  +74% `ns/pick` creep in the table above. It is the actual scaling wall for this module, and it
  is a streaming/architecture question, not a `_pick` question.
- **`_pick`'s cost is field-dependent, and one field dominates.** `email` and `website` cost
  ~2031 B/call (two `_EMAIL_OK`/`_URL_OK` `.match()` objects, `dedup.py:125`/`127`); `scraped_at`
  costs 104 B because it early-returns at `dedup.py:177`. The 23-field average is dominated by
  these two.
- **The `source` and `scraped_at` special cases at `dedup.py:173-177` are already correctly
  placed.** They sit *after* the two `_is_missing` checks, which is required for correctness
  (a blank `source` must not become `"|osm"`). Any refactor that hoists them above the blank
  checks will corrupt merges — worth a test, since `tests/test_lead_signal.py:319` only covers
  the both-populated case.

---

## Recommended order of work

1. **S3-2 — short-circuit the rank comparison and inline the closure** (`dedup.py:179-187`).
   Largest single win: **1.52x alone, 1.89x with the inlining, 2.67x on a whole `_merge`**,
   1097 → 512 B/call. Verified output-identical over 7537 field-pairs. Do this one first and
   stop there if nothing else gets done.
2. **S3-3 — memoise `_sources_of` on the raw `source` string** (`dedup.py:143-145`). Takes the
   total to **2.33x and 200 B/call (−82% allocation)**. Safe by construction: the key space is
   the 3 source names in `_SOURCE_TRUST`. Also absorbs **S3-5** (`dedup.py:151`) if `_trust_for`
   is rewritten alongside it.
3. **S3-4 — do nothing.** Measured at 0.97x, within noise.
4. If S3-2 and S3-3 land and the merge phase is *still* the thing you care about, revisit the
   per-`_merge` precompute of `_sources_of`/`_recency` (S3-3, "bigger version", measured 2.14x
   on `_merge` standalone). It is more invasive and largely subsumed by step 2.

**Not recommended:** parallelising the merge, rewriting the index as a trie, or moving `_pick` to
Cython/`orjson`-style tricks. The function is already O(1) per call and `dedup()` is already
O(n); there is no algorithmic prize here, only a constant-factor one worth ~2.7x on a phase that
costs under 7 seconds.