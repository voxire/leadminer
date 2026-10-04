# 365 — `infer_region` is linear and allocation-free; its only cost is a 6x regex constant

## Verdict

`infer_region` (`enricher.py:79`) is **O(n) in businesses and O(len(address) × Σalternatives) in
address length** — never quadratic, and it allocates **0 bytes per call**. Measured end to end at
500k records: **1.9 s** on a realistic mix, **8.6 s** in an adversarial all-miss worst case, against
**0.5–6.9 hours** for the HTTP stage in the same `enrich()` call. It is not a bottleneck and there
is nothing here that blocks trust in the output, so this report contains **no S1 and no S2**. The one
change with the largest speedup is a one-line `functools.lru_cache` on the text branch: **16.9x at
500k rows, 0 result differences, break-even (0.99x) even when every address is distinct**. The root
cause of why the uncached scan costs 20–31 µs per 47-character address is `re.IGNORECASE` on a
114-way literal alternation, which makes CPython emit **no prefix/charset skip at all**; removing it
is worth a further **3.6–6.4x** on every cache miss.

## Complexity verdict

| Question | Answer | Evidence |
|---|---|---|
| O(1), O(n) or O(n²) in the number of businesses? | **O(n)** | ns/row flat at 3661 / 4006 / 3813 for 10k / 100k / 500k (§Timings). Worst case x2 rows → x1.98, x2.04, x1.86, x1.96 time for 25k→400k (§Timings, linearity block). No superlinear term. |
| O(1) or O(n) in address length? | **O(len × Σalternatives)** | 615 → 8135 ns/char as the keyword table grows 114 → 1824 entries, and 665 → 873 ns/char as one address grows 47 → 3008 chars (§Scaling). Constant per (char, keyword), so linear, not quadratic. |
| Does it allocate proportionally to n? | **No — 0.0 B/call** | 200k calls: net allocation attributable to `infer_region` measured at **0.0 B/call** via `tracemalloc`; 200k return values share **13 distinct `str` objects**, all literals already interned in `_REGION_MAP`. Whole-pass peak was 1.63 MB = 8 B/row, and that is the caller's result list, not the function. |
| Catastrophic backtracking? | **No** | `enricher.py:53` builds `"|".join(re.escape(k) ...)` — a flat alternation of literals, no nested quantifiers, so no exponential branch. Confirmed by a flat ns/char curve over 47 → 3008 characters. |

## Measurements

All timings: CPython **3.14.8**, macOS/arm64, best-of-3 (best-of-9 for the A/B), `enricher.py`
verified at `sha256:f980120cd937e24d47f78124db10c028cfb8d1592577e69491120cde64df5b5c`. The harness
loaded `enricher.py`'s own source, stripped only the two imports that need `requests`
(`import requests` at `enricher.py:2` and the `httpclient` import at `enricher.py:128`), and
`exec`'d the rest, so the compiled patterns and the function body are byte-identical to the shipped
code. No scraper was run; no network call was made. **Run-to-run variance on this host is roughly
±50%** on absolute ns/char (thermal/frequency drift); the *ratios* below were stable across five
separate processes.

### Per-call cost by branch (`enricher.py:85-93`)

| Input | ns/call |
|---|---|
| `address=""` → falsy, skips the text branch, no coords (`enricher.py:85`, `:89`) | 72 |
| `address=None`, coords hit box 1 (LB) | 145 |
| `address=None`, coords hit box 1 (SA) | 200 |
| `address=None`, coords miss every box (LB) | 344 |
| address hits pattern 1 of 8 — `"Hamra, Beirut, Lebanon"` | 241 |
| address hits pattern 5 of 8 — `"Rue 5, Sidon, Lebanon"` | 11,536 |
| address hits pattern 6 of 8 — `"Main St, Nabatieh, Lebanon"` | 14,517 |
| **worst case**: 41-char address matching nothing, no coords | **19,834 – 31,258** |

The 47x spread between the first and last row is the whole story: `enricher.py:86-87` returns on the
first region whose pattern matches, so a Beirut hit costs 8 literal-prefix probes while a miss pays a
full failed scan of all 114 keywords.

### Timings for 10k / 100k / 500k

Total for the loop at `enricher.py:330-335`:

| rows | realistic mix | adversarial worst case | ns/row (mix) |
|---|---|---|---|
| 10,000 | **0.037 s** | 0.139 s | 3,661 |
| 100,000 | **0.401 s** | 1.414 s | 4,006 |
| 500,000 | **1.906 s** | 8.587 s | 3,813 |

"Realistic mix" = 55% LB rows with an address (OSM/Wikidata rows arriving from the cumulative
master), 35% coordinate-only LB, 10% coordinate-only SA — because `google_places.py:275` always calls
`infer_region(None, lat, lon, country=country)`, so every Google Places row reaches only the cheap
coordinate branch and **only OSM/Wikidata rows ever pay for the regex**. "Worst case" = every row has
a ~40-character address matching no keyword and no coordinates.

Linearity check on the worst case, confirming O(n):

```
n= 25000  0.414s  16572 ns/row
n= 50000  0.821s  16418 ns/row   x1.98 time for x2 rows
n=100000  1.672s  16720 ns/row   x2.04 time for x2 rows
n=200000  3.107s  15534 ns/row   x1.86 time for x2 rows
n=400000  6.083s  15208 ns/row   x1.96 time for x2 rows
```

### Scaling

Cost is **per input position, per keyword alternative**, not per position:

| #keywords in the alternation | shipped (IGNORECASE) | `.lower()` + case-sensitive |
|---|---|---|
| 114 (today) | 25,249 ns | 3,923 ns |
| 228 | 46,370 ns | — |
| 456 | 97,497 ns | 16,060 ns |
| 912 | 186,475 ns | — |
| 1824 | 333,548 ns | 56,605 ns |

Measured directly with synthetic patterns: 1 alternative = 10.0 ns/char, 39 alternatives = 22.5
ns/char under IGNORECASE (a ~10 ns/char scan floor plus ~0.3 ns per alternative per character);
case-sensitive, 1 alternative = 2.15 ns/char and 39 alternatives = 6.67 ns/char.

---

## Findings

### S3 — `re.IGNORECASE` deletes CPython's literal skip, making the text branch ~6x slower than it needs to be

- **Where:** `enricher.py:52-55` (the `re.IGNORECASE` flag is on `enricher.py:53`), consumed by
  the scan loop at `enricher.py:86-87`.
- **Breaks:** `_REGION_MAP` compiles each region as one alternation of 114 escaped literals with
  `re.IGNORECASE`. CPython only emits the fast literal-prefix or first-character-charset skip when
  the pattern is *not* case-insensitive: in `re/_compiler.py::_get_charset_prefix` the BRANCH arm
  bails out on the first cased literal (`if op is LITERAL and not (iscased and iscased(av)): …
  else: return None`), and `_get_iscased(IGNORECASE)` returns `_sre.unicode_iscased`, which is true
  for every cased character. Dumping the compiled `INFO` block confirms it:

  ```
  Beirut pattern (16 alts)  INFO mask: IGNORECASE=0   case-sensitive=4
  all-114 pattern           INFO mask: IGNORECASE=0   case-sensitive=4
  (1=PREFIX  2=CHARSET  4=LITERAL)
  ```

  `mask=0` means no skip table at all, so `sre` attempts every alternative at every input position:
  a measured **615–873 ns per character** for the full 8-pattern loop, versus **150–160 ns/char**
  case-sensitive. All 114 keywords are already lowercase in `REGION_KEYWORDS`
  (`enricher.py:10-50`; verified programmatically), so lowercasing the address once and compiling
  case-sensitively is a pure win. Interleaved A/B, min-of-9, same process, no-match address:

  | address length | shipped | `.lower()` + case-sensitive | speedup |
  |---|---|---|---|
  | 47 | 31,258 ns | 5,533 ns | **5.6x** |
  | 188 | 151,526 ns | 24,060 ns | **6.3x** |
  | 752 | 657,342 ns | 103,090 ns | **6.4x** |
  | 3008 | 2,626,182 ns | 359,866 ns | **7.3x** |

  End to end at 500k rows: **2.012 s → 0.553 s (3.64x), 0 result differences** out of 500,000. At
  200k adversarial rows: **5.291 s → 0.885 s (5.98x), 0 differences**.
- **Trigger:** Any LB row whose address matches no keyword. Concrete: a 41-character OSM address
  built from `scrapers/osm.py:87` such as `"Zone 4 Blv 12 Akkar unknown-word, Lebanon"` costs
  ~20–31 µs per call, repeated for every such row on every run.
- **Fix:** Drop the flag at `enricher.py:53` (`re.compile("|".join(...))`) and lower-case the address
  once at `enricher.py:86` (`for region, pattern in _REGION_MAP: if pattern.search(address.lower())`).
  Residual semantics gap worth a comment: `str.lower()` does not case-fold `İ`, `ẞ`, `ſ` or `K`, so
  a case-sensitive keyword match on those inputs would differ. No current keyword or plausible
  address contains them.

  **Caveat, measured:** `.lower()` copies the whole string *before* the scan starts, so on the
  early-exit path the shipped code is actually faster — 196 ns vs 231 ns at 47 characters, 223 ns vs
  649 ns at 752 characters. Lower-casing alone trades a ~35 ns win on hits for a 6x win on misses.
  That is why the two findings below compose rather than compete.

### S3 — No memoisation: the identical address string is re-scanned per row, per run, forever

- **Where:** `enricher.py:79` (the function is pure in its arguments), driven from
  `enricher.py:330-335`.
- **Breaks:** Nothing caches the address→region mapping, and `enricher.py:331` skips inference only
  when `region` is already truthy. So every row whose region resolved to `None` — SA rows outside
  the five boxes at `enricher.py:70-76`, rows with no coordinates, rows whose address contains no
  keyword — is re-scanned by **every** future run, because `main.py:150` reloads the cumulative
  master (`all_businesses.csv`), `main.py:180` re-dedups the whole thing and `main.py:187` re-enriches
  it. With the master approaching 500k and only a few thousand new rows per scrape, the re-scanned
  fraction approaches 100%. Measured: a 500k master with 200k unresolved rows costs **4.66 s of pure
  repeated work per run, forever**. Within a single run the waste is larger still — scrape addresses
  are highly repetitive (`"Verdun, Beirut, Lebanon"`), so the same handful of strings are scanned
  tens of thousands of times.
- **Trigger:** 200,000 rows drawn from 2,000 distinct address strings, all keyword misses:
  **5.092 s uncached → 0.070 s cached (72.9x)**. Scaled to 500k: **12.73 s → 0.175 s**. A real
  500k mixed set with 368 distinct addresses: **2.012 s → 0.119 s (16.9x), 0 result differences**.
- **Fix:** `@lru_cache(maxsize=65536)` on a small text-branch helper
  (`_region_from_address(address)`) called from `enricher.py:86`. It is safe because the function is
  pure, and measured to be **break-even, never a penalty, when every address is distinct**: fresh
  cache per pass, every call a miss → 1.00x / 0.99x / 1.03x / 0.99x / 0.99x at address lengths
  24 / 47 / 188 / 752 / 3008. Memory ceiling measured at **5.6 MB** for a warm 65,536-entry cache
  keyed on 752-character strings (85 B/entry — `lru_cache` holds a reference to the key the record
  dict already owns, so it does not copy addresses). Do **not** cache on the coordinate arguments as
  well: `(lat, lon)` cardinality is unbounded and would turn the cache into a leak.

### S3 — Per-call cost is linear in the keyword table, so the constant has no headroom for the Saudi expansion

- **Where:** `enricher.py:10-50` (`REGION_KEYWORDS`, 114 entries today) and `enricher.py:86`.
- **Breaks:** Because there is no skip table, every added keyword adds a per-character probe at
  every position. Measured on one 41-character non-matching address: 114 → 25,249 ns, 456 → 97,497
  ns, 1824 → 333,548 ns — a straight line in Σalternatives. Audit 007 recommends adding Saudi
  address-keyword coverage (it currently returns `None` for Taif, Abha and every Saudi address
  string, because `enricher.py:85` gates the text branch on `country == "LB"`). That change is
  correct and should be made, but it lands directly on this cost curve: going from 114 to roughly
  500 keywords (KSA has far more districts than the 8 Lebanese regions) triples the miss cost from
  ~20 µs to ~100 µs per row, i.e. ~50 s per 500k-row run. The case-sensitive rewrite above holds the
  same growth at ~16 µs and is what makes the expansion affordable.
- **Trigger:** Add 300 Saudi keywords and re-measure `mixed(200_000)`: expect ~2.5x the baseline
  miss time, not a step change — but the *unfixed* number goes from ~5.3 s to ~13 s at 200k rows.
- **Fix:** Land the `.lower()` + case-sensitive change *before* expanding `REGION_KEYWORDS`, and add
  a benchmark asserting per-call cost at a fixed address so table growth stays visible.

---

## Not a bug, but worth knowing

- **`infer_region` is ~0.03–0.5% of `enrich()`.** `check_websites` (`enricher.py:225-276`) issues one
  GET per row at 40 workers with `timeout=8` (`enricher.py:175`, default `workers: int = 40` at
  `enricher.py:225`, all targets submitted at `enricher.py:237-238`). At 500k rows that is
  **0.5 h** at 0.15 s mean latency, **1.4 h** at 0.4 s, **3.5 h** at 1 s, **6.9 h** at 2 s — against
  `infer_region`'s 1.9 s (realistic) / 8.6 s (worst). If you are optimising `enrich()`, this is the
  wrong 20 seconds of the run. It is also the stage that actually threatens the 300-minute workflow
  timeout.
- **The real proportional allocation in `enrich()` is next door, not here.** `enricher.py:238` submits
  every target at once and keeps a `Future` per target for the life of the `as_completed` loop.
  Measured retained memory for that dict: **17.3 MB at 10k, 172.6 MB at 100k, 857.2 MB at 500k
  rows (1,714 B/row)** — a perfectly linear but very large constant, and one that scales with the
  dataset exactly as `infer_region`'s does not. Bounding it (submit in windows of ~50k) is worth far
  more than any `infer_region` optimisation if the 500k target is real.
- **A combined single regex is a trap — measured, do not try it.** Merging all 114 keywords into one
  alternation and mapping the match back to a region was only **1.24x** (1.620 s vs 2.012 s at 500k)
  and **changed 39,879 of 500,000 results**, because the shipped order resolves by *region priority*
  (`enricher.py:86`) while a combined pattern resolves by *position in the address*. It is strictly
  worse on both axes.
- **An explicit leading charset guard is also a trap.** Wrapping the alternation as
  `(?=[hHjJkK…])kw1|kw2|…` to force the character-class skip measured **1,581 ns → 23,827 ns, 15x
  worse**: the assertion defeats the optimiser that made the case-sensitive compile fast in the
  first place.
- **`lower()` + 114 `in` tests is a viable simpler alternative** if the regex table is ever removed:
  0.473 s at 500k (4.26x, 0 diffs), and it degrades more gracefully with table growth than IGNORECASE
  does. Do not call `.lower()` inside the loop, though — that variant measured 1.213 s (1.66x)
  because it copies the address 114 times.
- **Coordinate branch is already near-optimal and needs nothing.** 145 ns for a first-box hit,
  344 ns for a full 8-box miss; the tuples at `enricher.py:58-68` and `enricher.py:70-76` are
  module-level constants and the list is selected by a single string comparison at
  `enricher.py:90`.
- **Nothing found wrong.** I looked specifically for quadratic behaviour (nested quantifiers,
  repeated list scans inside the loop, per-row re-compilation — all absent), for re-compilation
  (all patterns are module-level at `enricher.py:52-55`), for `re`'s internal cache thrash (no
  dynamic patterns), and for per-call allocation (none). Per the brief's own guidance, reporting
  that the code is fine on this axis is the result.

## Recommended order of work

1. **Cache the text branch** — `@lru_cache(maxsize=65536)` on `_region_from_address`, called from
   `enricher.py:86`. Largest measured speedup (**16.9x at 500k**, up to 72.9x when scrape addresses
   repeat), **0 result differences**, break-even when all addresses are distinct, 5.6 MB ceiling.
   Do it before or with step 2 — they compose, and caching first means `.lower()` is paid once per
   distinct address instead of once per row.
2. **Drop `re.IGNORECASE` at `enricher.py:53` and `.lower()` once at `enricher.py:86`.** Worth
   **3.6–6.4x** on every cache miss, 0 differences, and it keeps the cost of the Saudi keyword
   expansion that audit 007 asks for at ~16 µs instead of ~100 µs per row. Note the early-exit
   trade-off above, which is why step 1 should land first.
3. **Bound the `as_completed` window at `enricher.py:238`** if 500k rows is a real target — 857 MB of
   `Future` objects is the only proportional allocation in `enrich()`, and it dwarfs everything
   measured in this report.
4. **Land a micro-benchmark** for `infer_region` at a fixed 47-character address (assert < 8 µs per
   call) so the cost of growing `REGION_KEYWORDS` cannot silently reappear.
5. **Do not** combine the region patterns into one regex, and do not add a leading charset guard —
   both measured worse, the first also silently changing 8% of results.

### Reproduction

The harness is external to the repo (nothing under `/Users/mhomsi/dev/dummy/leadminer` was
modified). It reads `enricher.py`, strips `import requests` (`enricher.py:2`) and the `httpclient`
import (`enricher.py:128`), `exec`s the remainder, and times the result. Re-verify with
`shasum -a 256 enricher.py` → `f980120cd937e24d47f78124db10c028cfb8d1592577e69491120cde64df5b5c`;
if that hash differs, re-measure before trusting any ns figure above.