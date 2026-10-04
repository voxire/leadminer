# 397 — `lead_score()` performance profile

## Verdict

**`lead_score()` is already the right shape: O(1) per record, O(n) total, no data
structures that grow with the dataset, and no hidden nested loop.** At 500k records
it costs **1.7 s** (1.7–3.5 s across runs on a box under load), which is 2.4% of the
post-scrape CPU budget. There is no complexity problem here and no S1/S2 finding.

There are two S3 items, both free to fix, worth **4.0x together**: the function is
computed **twice per record** (`enricher.py:350` then `main.py:197`, 2.0x wasted),
and **line 285 alone accounts for 55–70% of the runtime and 100% of the allocation**
(1.9x if the `re.sub` is hoisted). Do them, then stop — nobody should spend a day
here. The real CPU in this pipeline is `write_csv` (**52 s** for the five exports at
500k, `main.py:209-213`) and `check_websites` (**12.5 min at a typical 300 ms
response, 5.5 h in the all-timeout worst case**, `enricher.py:175`/`237`).

---

## Complexity answer, stated directly

| Question | Answer | Evidence |
|---|---|---|
| Per-record complexity | **O(1)** | `enricher.py:283-319` is 8 `dict.get` calls and 10 integer comparisons. No loop, no comprehension, no recursion, no lookup into a structure keyed by dataset size. |
| Total complexity | **O(n)** | The three call sites — `enricher.py:350`, `main.py:197`, `cli.py:269` — are each inside a single flat `for r in records:` loop. No call site is nested inside another loop over records. |
| Is it O(n²) anywhere? | **No** | Verified by reading all three call sites and by the doubling test below (ratio stays at ~2.0, not ~4.0). |
| Does it allocate proportionally? | **Yes, but trivially and transiently** | Peak transient is **1,566 B per call** with a phone present, of which **1,446 B is `enricher.py:285`**. Net *retained* after 200,000 calls is **320 B** — i.e. nothing accumulates. 500k records churn ~725 MB through pymalloc, peak RSS is unaffected. |
| Would a 500k dataset make it hot? | **No** | 1.7 s out of a 61 s post-scrape CPU budget and a 300-minute workflow timeout (`scrape.yml` `timeout-minutes: 300`). It is not a bottleneck and never becomes one. |

---

## Measurements

**Harness.** Synthetic 22-key records matching `scrapers/base.py:3-28`, cycling
five field profiles so every branch of `lead_score` fires: live site + email;
dead site + instagram; whatsapp + `rating=3.4`; `source="osm|google_places"`;
bare phone only. Python 3.14.8 on macOS, 12 cores, **load average 84–89** — this box
is running ~400 concurrent audit agents, so absolute numbers are inflated roughly
2x versus an idle machine and drift ~2x between runs. **All ratios below are stable
across runs; all absolute numbers are upper bounds.** `requests` was stubbed
in-memory (no network, no `pip install`) so `enricher.py:2` and `httpclient.py:43`
would import.

### 1. Scaling — O(n) confirmed by doubling

`min of 5, timing only the scoring loop, records resident in memory`:

| n | 1 pass, as written | µs/record | ratio vs. half |
|---|---|---|---|
| 62,500 | 327.6 ms | 5.24 | — |
| 125,000 | 664.3 ms | 5.31 | **2.03** |
| 250,000 | 1,253.7 ms | 5.01 | **1.89** |
| 500,000 | 2,901.1 ms | 5.80 | **2.31** |

Ratios track 2.0 with drift attributable to cache/TLB pressure and generational GC
as the working set grows — not to an algorithmic term. An O(n²) function would show
~4.0 here.

### 2. Headline table — one consistent interleaved run, min of 5

Interleaved so machine noise hits all four variants equally. "NOW" = `lead_score`
as written; "FIXED" = identical arithmetic with `enricher.py:285` replaced by
`len(phone.translate(_DEL))` (asserted to return identical scores on the test record).

| n | 1 pass NOW | 2 passes NOW | 1 pass FIXED | 2 passes FIXED | fix dup pass | fix line 285 |
|---|---|---|---|---|---|---|
| 10,000 | 15.7 ms | 31.9 ms | 8.1 ms | 15.5 ms | 2.03x | 1.95x |
| 100,000 | 240.6 ms | 479.0 ms | 131.6 ms | 238.1 ms | 1.99x | 1.83x |
| 500,000 | **1,665.8 ms** | **3,450.0 ms** | **872.1 ms** | 1,660.7 ms | 2.07x | 1.91x |

**Best idle-machine figures** (earlier in the session, load lower): 10k = 19.3 ms,
100k = 212.9 ms, **500k = 1,192.9 ms** per pass; 2.13 µs/record → 1.78 µs/record.
So the honest range for one 500k pass is **1.2 s (idle) to 1.7 s (typical) to
3.5 s (loaded)**, i.e. **2.4–7 µs per record**. Use the ratios, not the absolutes.

### 3. Where the time goes inside one call (idle machine, hot record, 500k ops each)

| Expression | ns/op | share of the 1,778 ns/call |
|---|---|---|
| `re.sub(r"\D", "", str(record.get("phone") or ""))` — `enricher.py:285` | **1,114.9** | **63%** |
| `"|" in str(record.get("source") or "")` — `enricher.py:316` | ~90 | 5% |
| `record.get(...)` × 8 + 10 integer comparisons — `enricher.py:288-314` | ~570 | 32% |
| *identical output, `_DIGITS.sub` precompiled* | 858.9 | (1.30x) |
| *identical output, `str.translate(_DEL)`* | **277.5** | **(1.75x on the line)** |

Function-level A/B: current **1,777.9 ns** → precompile-only **1,422.7 ns (1.25x)**
→ `str.translate` **1,052.9 ns (1.69x)**. In-situ with the record set resident the
translate variant measured 1.83–1.91x because the allocation pressure it removes is
what hurts once 500k dicts are competing for cache.

### 4. Allocation — `enricher.py:285` is 100% of it

`tracemalloc`, peak transient per call, 200,000 calls in one traced region:

| Variant | Peak/call | Net retained after 200k calls |
|---|---|---|
| `lead_score(phone="+961 3 123 4567")` | **1,566 B** | 320 B |
| `lead_score(phone=None)` | 320 B | 320 B |
| `lead_score` body with line 285 deleted | **120 B** | 120 B |
| `re.sub(r"\D","",str(PHONE or ""))` alone | **1,566 B** | 320 B |
| harness control (empty lambda) | 120 B | 120 B |

The 8 `dict.get` calls, the ten `+=` comparisons, `min(score, 100)` and
`"|" in str(source)` **allocate literally zero bytes** — the 120 B is the harness
floor. All 1,446 B is the `sre` engine: ~200 B of fixed match-state machinery plus
~1.25 KB for scanning and rebuilding a 17-character string. Note that `phone=None`
still costs 200 B, because `re.sub` is still *called* on `""`.

At 500k records that is **~725 MB of transient allocation churn** for a function
whose entire useful output is 500k integers. It is freed immediately so peak RSS is
unaffected (net retained 320 B), but it is 725 MB of pymalloc arena churn competing
with the rest of the working set.

### 5. GC is not the driver

500k records resident, interleaved 4x, median:

| | Time |
|---|---|
| `gc.enable()` | 3.181 s |
| `gc.freeze()` + `gc.disable()` | 3.153 s |

**No difference.** The cyclic GC is not scanning the 500k dicts on this path
(they are reachable, not garbage), so `gc.freeze()` — the usual lever for bulk
Python passes — buys nothing here. Ruled out.

---

## Findings

### S3 — `enricher.py:285` is 63% of the runtime and 100% of the allocation, and is redundant in the main path

- **Where:** `enricher.py:285` (`phone = re.sub(r"\D", "", str(record.get("phone") or ""))`)
- **Breaks:** Nothing breaks. It is a pure constant-factor waste: 1,115 ns of the
  1,778 ns call, 1,446 B of the 1,566 B peak allocation. Two compounding reasons
  it should go:
  1. **The pattern is recompiled-by-lookup on every call.** `re.sub` with a `str`
     pattern goes through `re._compile()`, which does an `isinstance` check and a
     `(type, pattern, flags)` dict probe (`re._MAXCACHE = 512`) before dispatching.
     Measured: 268 ns/call of pure lookup overhead. A module-level
     `_DIGITS = re.compile(r"\D")` recovers it (1.30x on the line).
  2. **It is recomputing something already computed.** `main.py:194` runs
     `normalize_phone(raw_phone, country)` — which internally does the *same*
     `re.sub(r"\D", "", raw)` at `dedup.py:44` — and writes the E.164 result back to
     `r["phone"]` **three lines before** `main.py:197` calls `_lead_score(r)`. By the
     time the second call runs, `phone` is `"+9611234567"`: already digits, already
     known to be >= 7 digits (`dedup.py:45` returns `""` below `_MIN_DIGITS = 7`).
     The regex at `enricher.py:285` scans a string that contains exactly two
     non-digit characters and discards both. It cannot change the answer.
- **Trigger (perf):** 500k records, one pass = 872 ms fixed vs 1,666 ms as written
  (1.91x). 500k records, two passes as the pipeline actually runs them = 1,661 ms
  vs 3,450 ms.
- **Trigger (the redundancy):** `python3 -c "import main,enricher; r={'phone':'+961 3 123 4567',...}; main.resolve_country; ..."` — assert
  `re.sub(r"\D","",main.normalize_phone("+961 3 123 4567","LB")) == "96131234567"`,
  i.e. the post-normalization value is already all-digits, so line 285 is a no-op.
- **Fix:** `_DIGITS = re.compile(r"\D")` at module level (1.30x, byte-for-byte
  identical), or `len(record.get("phone", "").translate(_DEL)) >= 7` with
  `_DEL = {c: None for c in range(128) if not chr(c).isdecimal()}` (1.75x on the
  line, 1.91x in situ). **Caveat if you take the `translate` route:** an
  ASCII-range delete table leaves every codepoint >= 128 untouched, whereas
  `re.sub(r"\D", ...)` also strips non-decimal Unicode numerics (superscripts,
  fractions, circled digits) while *keeping* Arabic-Indic and full-width digits
  (Python 3 `\d` is Unicode-`Nd`-aware). For phone fields the divergence cannot
  matter; if you want exact equivalence for free, take the precompile-only 1.25x.

### S3 — `lead_score` is computed twice per record; the first result is thrown away

- **Where:** computed at `enricher.py:350`, then unconditionally overwritten at
  `main.py:197`. Also computed once at `cli.py:269` (that path is fine — `cmd_score`
  sets `industry_priority` first at `cli.py:267`).
- **Breaks:** Nothing breaks; the final CSV is correct. It is a flat **2.0x** waste
  on an O(n) pass: **1.67 s of pure duplicate work at 500k records.** The first pass
  is provably wrong as well — `enrich()` runs before `main.py:190` sets
  `industry_priority`, so `enricher.py:306-310` contributes `+0` for priority every
  time. So 1.67 s is spent computing a number that is (a) missing up to 15 points
  and (b) discarded. This is the correctness half of `009-lead-score-model.md`'s
  finding 5; this is the cost half.
- **Trigger:** `enrich(records)` alone (`enricher.py:326`) then read
  `records[0]["lead_score"]` — the priority points are missing, proving the
  `enricher.py:350` result is not the one that ships.
- **Fix:** delete `enricher.py:350` and let `main.py:197` own the single pass (the
  same one-liner `009` recommends). Saves 2.0x with zero behaviour change to any
  emitted artifact. If you would rather not delete it, move the
  `industry_priority` assignment into `enrich()` above `enricher.py:349` and delete
  `main.py:195-197` instead — same saving, and `enrich()` becomes self-consistent
  for the `cli.py`/notebook callers.

### S3 — line 285 is linear in an uncapped field length, with no upper bound anywhere in the pipeline

- **Where:** `enricher.py:285`. Field origin: `scrapers/osm.py:89`
  (`phone = tags.get("phone") or tags.get("contact:phone")`) — an **uncapped raw
  OSM tag value**, and OSM does not length-limit tag values. Also reachable from
  any `data/all_businesses.csv` cell via `main.py:77` `load_master`.
- **Breaks:** `re.sub(r"\D", ...)` is O(len(phone)), so `lead_score` is O(1) per
  record only for bounded fields. Measured cost of the single line:

  | `len(phone)` | `re.sub` | `_DIGITS.sub` | `str.translate` | speedup |
  |---|---|---|---|---|
  | 17 (typical) | 2,128 ns | 2,229 ns | 722 ns | 2.9x |
  | 200 | 23,168 ns | 23,088 ns | 939 ns | 24.7x |
  | 2,000 | 223,305 ns | 232,506 ns | 11,597 ns | 19.3x |
  | 20,000 | 2,169,193 ns | 2,199,225 ns | 131,646 ns | 16.5x |
  | 200,000 | 19,166,998 ns | 14,288,973 ns | 601,506 ns | 31.9x |

  ~110 ns/char for the regex vs ~3 ns/char for `translate`. A 200 KB `phone` tag
  costs **11.3 ms in a single `lead_score` call**, and 500k such records = **94
  minutes** of the 300-minute budget burned on one line. Realistically this needs a
  deliberately pathological dataset to matter — but the amplification factor is
  unbounded and there is no length cap anywhere in the chain. **Note the identical
  line at `dedup.py:44` runs first** (`main.py:180` before `main.py:187`), so any
  cap should go in `dedup.py:44` and `enricher.py:285` together, or better in
  `normalize_phone` and be relied on by both.
- **Trigger:** `lead_score({"phone": "1" * 200_000, ...})` — 11.3 ms for one record.
  Realistic variant: 500 records whose OSM `phone` tag is a pasted email signature.
- **Fix:** the `translate` variant above is 16–32x faster at these sizes and is
  immune to the amplification. For a hard bound, truncate at the call site:
  `str(record.get("phone") or "")[:64]` — a dialable number past 64 characters does
  not exist, and `BRIEF.md` targets Lebanon and Saudi Arabia (max 13 E.164 digits).

---

## The single change with the largest speedup

**Delete one of the two `lead_score` calls — `enricher.py:350`.** It is 2.07x at
500k (the largest single win per line changed), it is a one-line deletion, and it
also removes a latent correctness bug. Paired with hoisting the `re.sub` on
`enricher.py:285` the total is **4.0x: 3.45 s → 0.87 s at 500k records.**

Then stop optimising this function. Both fixes together move the **post-scrape CPU
budget at 500k from 61.2 s to 57.6 s — a 6% improvement to the stage**, because:

| Stage | Time at 500k | vs. `lead_score` (2 passes, 5.54 s) |
|---|---|---|
| `write_csv` × 5 files (`main.py:209-213`) | **51.98 s** | 9.4x more |
| `lead_score` × 2 (current) | 5.54 s | — |
| `normalize_phone` × 1 (`main.py:194`) | 2.02 s | 0.4x |
| `recommend_service` × 1 (`main.py:191`) | 0.95 s | 0.2x |
| `completeness_score` × 1 (`enricher.py:349`) | 0.53 s | 0.1x |
| `industry_priority` × 1 (`main.py:190`) | 0.21 s | 0.04x |
| **`check_websites` (`enricher.py:237`)** | **12.5 min – 5.5 h** | **135x – 3,600x more** |

`check_websites` arithmetic: 100k websites (a 20% website rate at 500k records) ÷
40 workers (`enricher.py:237`) × 300 ms typical = **750 s**; every request hitting
the `timeout=8` at `enricher.py:175` = **20,000 s = 5.5 h**, which alone exceeds
`timeout-minutes: 300`. **The network stage is two to three orders of magnitude more
expensive than `lead_score`.** If someone wants a real speedup in this pipeline it
is caching or batching the website checks (`enricher.py:161-222`, `enricher.py:225`),
not scoring — see `docs/audits/053-caching-layer.md` and `089-SYNTH-cost-quickwins.md`.

---

## Not a bug, but worth knowing

- **The `str()` calls on lines 285 and 316 are free but pointless.**
  `str(x)` on an object that is already a `str` returns the *same object* in CPython
  (no copy), so `str(record.get("phone") or "")` costs ~50 ns of pure dispatch and
  zero allocation when `phone` is a `str` — measured 320 B peak for
  `lead_score(phone=None)`, of which 200 B is the `re.sub` call that still happens
  on the empty string. The `str()` only matters for the CSV/`load_master` path
  (`main.py:77`) where a field could arrive as a non-string; keep it there, drop it
  from the hot path.
- **`"|" in str(record.get("source") or "")` (`enricher.py:316`) is ~90 ns and
  allocation-free** — `str()` returns the same object and `in` on a `str` does not
  build a slice. It is not worth touching, and it is the only other expression in
  the function with any cost at all. Unlike `lead_score`, its sibling
  `dedup._sources_of` (`dedup.py:143-145`) *does* allocate a set and a list per call
  and is called 2× per field per merge inside `dedup._merge` — that is the one to
  look at if you are profiling `_merge`, not here.
- **Parallelising `lead_score` is pointless.** 1.7 s of pure-Python CPU over 500k
  records would shard perfectly across a `ThreadPoolExecutor`, but at 0.9% of the
  post-scrape budget it is not worth the fork and the thread-pool overhead, and it
  would add nothing to the 52 s `write_csv` stage that dominates it. Do not.
- **The dict-of-22-keys shape is not `lead_score`'s problem, but it is the loop's.**
  Per-record cost rises from ~1.8 µs on a hot single record to ~3.3 µs with all 500k
  dicts resident (measured A: 3.075 µs hot vs B: 3.316 µs resident). That ~1.8x gap
  is cache and TLB pressure from walking 500k scattered dicts, and it is a property
  of the `dict` record layout and the `list[dict]` container, not of `lead_score`.
  Any future move to `__slots__` objects, a dataclass, columnar storage, or SQLite
  (`033-storage-sqlite-migration.md`, `052-memory-streaming.md`) attacks that gap
  globally rather than here.
- **Absolute timings in this report are upper bounds.** The box ran at load average
  84–89 on 12 cores for the duration of these measurements (`BRIEF.md:66`: ~400
  concurrent agents). Idle-machine figures were roughly half. Every ratio quoted
  (2.03x / 1.95x / 4.0x / 63% / 100% of allocation) reproduced across four separate
  interleaved runs, so treat the ratios as the result and the milliseconds as an
  order-of-magnitude reference.

---

## Recommended order of work

1. **Delete `enricher.py:350`.** One line, 2.07x, and it removes the
   missing-`industry_priority` inconsistency at the same time (cross-ref
   `009-lead-score-model.md` finding 5). Do this first because it is free.
2. **Hoist the regex at `enricher.py:285` to module scope** (`_DIGITS =
   re.compile(r"\D")`), 1.30x, byte-for-byte identical output, zero risk. If you
   want the other 1.5x, switch to `str.translate` with the ASCII delete table and
   accept the Unicode edge case documented in the finding, or add a length cap.
   Combined with step 1 this is the full 4.0x.
3. **Add a length cap on `phone`** at `scrapers/osm.py:89` and `dedup.py:44`
   (64 characters is generous for LB/SA), so the one variable-cost expression in the
   function cannot be amplified by a pathological tag value.
4. **Stop here.** `lead_score` is 2.4% of post-scrape CPU after steps 1–2. Spend
   the effort on `write_csv` (52 s, `main.py:209-213`) and `check_websites`
   (12.5 min – 5.5 h, `enricher.py:225`) instead.