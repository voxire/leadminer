# 200 — `normalize_name` (`dedup.py:66`): O(1) per call, O(n) total, nothing retained. The win available is 4.4x, not a scaling fix

## Verdict

**`normalize_name` is not a scaling bug and must not be treated as one.** It is a pure
function of one string: O(L) per call where L is the name's length, **O(1) in the number
of businesses**, and O(n·L) = **O(n)** over the whole corpus. I verified this by
measurement, not by inspection: 10k / 50k / 100k / 250k / 500k records, in three data
regimes, and ns-per-record is flat to within 5% while wall time grows 55x for 50x the
records. It retains **zero bytes** — every temporary dies at return, `tracemalloc`
`current` returns to 0 — so it does not contribute to peak memory either. At the 500k
target it costs **1.28 s CPU / 2.45 s wall** (150k name-branch records), which is
**3–6% of `dedup()`'s 8.3 s** and **under 0.1% of the enrichment stage** that runs after it.

The one real finding is that the function pays full Unicode-machinery cost on names that
need none of it. **The single change with the largest speedup is an `str.isascii()` fast
path at `dedup.py:67`** — a 15 ns O(1) flag read that lets a Latin name skip both the
per-character Python loop at `dedup.py:68` and the regex at `dedup.py:69`:
**2888 ns → 267 ns per name (10.8x)**, **4.4x on a realistic 55/45 Latin/Arabic corpus**,
~1.0 s saved per 500k-record run, and provably byte-identical output (proved below, not
asserted). Memoizing the function — the tempting answer — is **not** worth it: measured
**0.92x–1.00x** at realistic repeat rates.

---

## Direct answers to the lens questions

| Question | Answer | Evidence |
|---|---|---|
| O(1), O(n) or O(n²) in businesses? | **O(1) per call, O(n) over the corpus.** No quadratic term anywhere on this path. | Measured 10k→500k: `dedup()` CPU 0.151 s → 8.339 s (55x time for 50x records); ns/record 15.1 → 16.7 µs. Merge-heavy regime 1.601 s → 16.743 s (10.5x for 10x); all-phone-less regime 2.355 s → 24.185 s (10.3x for 10x). |
| Does it allocate proportionally? | **Transiently yes (O(n·L) bytes of churn), retained no (0 bytes).** ~1.2–1.4 KB transient per call, freed immediately. | `tracemalloc` peak per single call: 1175 B (`"Spinneys"`), 1359 B (`"Al  Baik  Restaurant"`), 1438 B (`"مطعم أبو شقير"`), 1438 B (harakat), 1356 B (fullwidth). Traced *live* memory after 50k calls: **0 bytes**. |
| Cost at 10k / 100k / 500k | **0.025 s / 0.255 s / 1.276 s CPU** over a realistic name corpus. | Table below. Constant 2.55 µs/name; extrapolation for any n is `n × 2.55 µs`. |
| Largest single speedup | **`str.isascii()` fast path: 4.4x end-to-end, 10.8x on Latin names.** | Table below. |

### Measured cost of `normalize_name` alone

Corpus: 200k–500k synthetic names, 55% Latin / 45% Arabic, mean length 11.9 chars
(realistic for `scrapers/osm.py:63`, `scrapers/google_places.py:268`,
`scrapers/wikidata.py:107`). CPU time, min of repetitions.

| n names | current `dedup.py:66-69` | with `isascii()` fast path | speedup |
|---|---|---|---|
| 10,000 | 0.025 s CPU (0.033 s wall) | 0.005 s CPU (0.005 s wall) | 4.66x |
| 100,000 | 0.255 s CPU (0.452 s wall) | 0.058 s CPU (0.080 s wall) | 4.41x |
| 500,000 | **1.276 s CPU (2.448 s wall)** | **0.288 s CPU (0.486 s wall)** | 4.42x |

Ratios are flat (4.66 / 4.41 / 4.42), which is the arithmetic signature of a linear
algorithm with a constant factor — not a hidden quadratic.

### Measured cost of `dedup()` itself, for scale context

500k does not mean 500k calls. `dedup()` only reaches `normalize_name` for records whose
phone is missing or junk (`dedup.py:205` returns `""`, `dedup.py:212-214` takes the name
branch). Synthetic 23-key records, 70% with phones, 5% duplicate rate:

| n records | `dedup()` CPU | wall | ns/record | `normalize_name` share | peak RSS |
|---|---|---|---|---|---|
| 10,000 | 0.151 s | 0.209 s | 15,141 | 3.7% | — |
| 50,000 | 0.786 s | 1.151 s | 15,714 | 4.0% | — |
| 100,000 | 1.606 s | 2.303 s | 16,061 | 5.8% | — |
| 250,000 | 3.964 s | 5.893 s | 15,857 | 3.1% | — |
| 500,000 | **8.339 s** | **12.890 s** | 16,679 | **4.6%** | 1,094 MB |

Two more regimes at 500k, to confirm no superlinearity under merge pressure:

| regime | 50k | 100k | 250k | 500k | ns/record |
|---|---|---|---|---|---|
| 50% duplicates (cumulative-master shape) | 1.601 s | 3.242 s | 8.294 s | 16.743 s | 32.0 → 33.5 µs |
| 100% phone-less (every record hits `dedup.py:214`) | 2.355 s | 4.736 s | 11.852 s | 24.185 s | 47.1 → 48.4 µs |

Even in the regime where 100% of records call `normalize_name`, the function is ~1.3 s of
a 24.2 s `dedup()` — 5.4%. The rest is `_merge`/`_pick`, consistent with the independent
profile in `200-fn-dedup-py-_extract_city-perf.md:174-178` and
`307-fn-dedup-py-_validity-concur.md:100`.

**Context that settles the severity:** the stage that follows is
`enricher.check_websites(records, workers=40)` (`enricher.py:225`) doing one
`session.get(url, timeout=8)` per record (`enricher.py:175`), called at `main.py:187`.
At 500k records that is 500,000 requests / 40 workers — **62 minutes at a conservative
300 ms each, and up to 27 hours if requests hit the 8 s timeout.** `dedup()`'s entire
16.7 s is 0.03% of the optimistic figure. Optimizing this function is misallocated
effort, which is why every finding below is S3.

---

## Findings

### S3 — `dedup.py:68` runs a Python-level loop, and `unicodedata.combining` per character, over names that are mostly ASCII

- **Where:** `dedup.py:68` — `"".join(c for c in name if not unicodedata.combining(c))`
- **Breaks:** nothing breaks; it just costs 3–10x more than it needs to. The generator
  expression is the single most expensive line in the function — **1780 ns of a 2888 ns
  call at 18 chars (62%)**, and ~**96% of the call at 32,768 chars** (78.2 ns/char vs
  2.9 ns/char for the C-level path). `str.join` also materialises the generator into a
  full list before joining, so the call allocates a list of L pointers plus, for any
  non-Latin-1 character, a fresh 60-byte one-character `str` (verified: 14-char Arabic
  name → `list` of 184 B holding 14 × 60 B strings).
- **Trigger (measured per-line, CPU ns, min of 7):**

  | line | ASCII (17.7 ch) | Arabic (14.2 ch) | mixed (22.8 ch) |
  |---|---|---|---|
  | L67 `unicodedata.normalize("NFKD", …)` | 85 | 140 | 180 |
  | **L68 combining-filter join** | **1780** | **2063** | **2793** |
  | L69b `.lower().strip()` | 75 | 159 | 160 |

  `dedup.py:69`'s regex is not in that table because its cost is dominated by a constant,
  not by L: measured separately at two lengths, `re.sub(r"\s+", " ", ·)` costs **623 ns
  on an 8-char** string and **3261 ns on an 81-char** one, versus **75 ns / 491 ns** for
  `" ".join(·.split())` and **320 ns / 2924 ns** for a hoisted `_WS.sub`. Per character it
  is ~78 ns/char, i.e. it scales linearly but with a ~500 ns floor — the `re` module's
  per-call `_compile` cache lookup, not pattern matching.

  Worth knowing: **L67 is nearly free and usually allocates nothing.** NFKD returns the
  *same object* when the string is unchanged, so `tracemalloc` peak for `L67` on an ASCII
  name is **0 bytes**, and it is also 0 for plain Arabic without hamza or madda
  (`"مطعم بيت"`, `"صيدلية الشفاء"`, `"ورشة الحدادة"`, `"شركة النور"` — all verified
  identity). It allocates only for hamza/madda Latin or Arabic (`"مطعم أبو شقير"` 13→14
  chars because `U+0623` decomposes to `U+0627`+`U+0654`; `"Ångström"` 8→10). So the
  line that *looks* expensive is not; the loop that looks cheap is.
- **Fix:** skip lines 67–68 entirely when `name.isascii()`, and for non-ASCII replace the
  loop with `str.translate` against a lazily built deletion table (next finding).

### S3 — the single largest speedup: an `str.isascii()` fast path at `dedup.py:67`, plus `" ".join(name.split())` for `dedup.py:69`

- **Where:** `dedup.py:66-69`
- **Breaks:** every Latin-script name pays the full Unicode cost for a no-op.
  `"Spinneys"` → 2888 ns; `" ".join(name.split()).lower()` → **267 ns**. Real names in
  this corpus are ~55% Latin (`name:en` at `scrapers/osm.py:63`, `displayName.text` at
  `scrapers/google_places.py:268`, Wikidata `en`-first labels at
  `scrapers/wikidata.py:40`,`:107`), so the corpus-level win is **4.4x**.
- **Trigger:** `"Café de Beyrouth"` → 6046 ns current vs 2134 ns patched;
  `"Al Baik Restaurant"` → 4754 ns vs 267 ns. Corpus of 500k mixed names:
  1.276 s → 0.288 s CPU.
- **Why this is provably output-identical, not a guess:**
  1. **NFKD is the identity on ASCII.** Checked exhaustively: for all 128 ASCII code
     points, `unicodedata.normalize("NFKD", c) == c`.
  2. **No ASCII code point has a combining class.** Checked exhaustively:
     `[chr(c) for c in range(128) if unicodedata.combining(chr(c))]` is empty. Therefore
     line 68's filter removes nothing from an ASCII string.
  3. **`re.sub(r"\s+", " ", s).strip()` ≡ `" ".join(s.split())`.** Checked over **all
     1,114,112 code points** (`a + ch + b` and a multi-whitespace probe) and
     **30,025 fuzz strings** drawn from ASCII / CJK / Arabic-presentation / general-punctuation
     ranges, plus a hand-picked nasty list (NBSP, ZWSP, ZWJ, BOM, fullwidth, ligature,
     emoji, `"İstanbul"`, 255×`"x"`): **0 differences**. Both use the same
     `Py_UNICODE_ISSPACE` predicate, and `str.strip()` returns `self` unchanged when
     there is nothing to strip, so the current code already collapses + trims.
  4. End-to-end equivalence re-confirmed on 30,025 strings: current vs patched differ
     **0 times**, including the 420-char multi-word pathological name.
- **`str.isascii()` is genuinely O(1), not a scan:** 14.9 ns on an 8-char string, 14.8 ns
  on a 1,000,000-char string. It reads the compact-ASCII flag on the `str` object. So the
  branch is free even for a pathological name.
- **Fix (drop-in, 4 lines added, 1 line changed):**

  ```python
  def normalize_name(name: str) -> str:
      # ASCII fast path: NFKD is the identity on all 128 ASCII code points and no
      # ASCII code point has a non-zero combining class, so for a Latin name both
      # the normalize() and the mark filter are provably no-ops. str.isascii() is
      # a flag read (~15 ns), not a scan, so the branch is free.
      if name.isascii():
          return " ".join(name.split()).lower()
      name = unicodedata.normalize("NFKD", name)
      name = "".join(c for c in name if not unicodedata.combining(c))
      return " ".join(name.split()).lower()   # == re.sub(r"\s+", " ", …).strip()
  ```

  This also removes the last un-precompiled regex literal from the function, which is
  inconsistent with `dedup.py:116-117` where `_EMAIL_OK` and `_URL_OK` *are* hoisted.
  Hoisting alone (`_WS.sub`) would recover 623→320 ns on an 8-char name; `split`/`join`
  gets to 75 ns — the regex engine is 8.3x more expensive than the C-level equivalent on
  short names and 6.6x on 81-char ones.

### S3 — the non-ASCII half can drop the per-character loop too, via `str.translate` with a 934-entry table

- **Where:** `dedup.py:68`
- **Breaks:** non-ASCII names still pay ~100 ns per character for a filter the C layer can
  do in one pass.
- **Trigger:** Arabic + mixed pool — current 3455 ns/name, `translate` variant **1276
  ns/name (2.71x)**. At 500k records with 45% Arabic names this is the difference
  between 1.276 s and 0.288 s on top of the ASCII win.
- **Table:** `{cp: None for cp in range(sys.maxunicode + 1) if unicodedata.combining(chr(cp))}`
  is **934 entries**, and its output was verified identical to the current implementation
  on the same 30,025-string fuzz corpus (0 differences). A full scan costs **0.120 s
  CPU** (241 ns/name amortised over 500k) — so build it **lazily on the first non-ASCII
  name**, not at import, or every `leadminer stats` invocation pays 0.12 s for a code
  path it may never take.
- **Fix:** the lazy-table variant sketched in "Recommended order of work" below. Optional —
  the ASCII fast path alone already delivers 4.4x.

---

## Not a bug, but worth knowing

- **`re.sub(r"\s+", …)` on `dedup.py:69` costs 1126 bytes of transient allocation to
  produce a 49-byte string.** Calibrated against `len(x)` and `x + ""` (both 0 traced
  bytes), the `tracemalloc` peak of a single `re.sub` on an 8-char string is 1126 B; the
  pattern is constant so the module cache is always warm — this is pure per-call lookup
  overhead, not a cache miss. `" ".join(s.split())` peaks at 75 B.
- **Allocation volume vs. retained memory.** At 500k records, ~150k name-branch calls ×
  ~1.3 KB transient ≈ **195 MB of allocate-then-free traffic per run**. It never shows up
  in RSS (`tracemalloc` live returns to 0; the 500k run's 1,094 MB peak RSS is the record
  dicts and index keys), but it is real allocator churn. The only memory this function
  causes to be *retained* is the `name_index` keys at `dedup.py:199` — up to ~150k
  normalized strings, roughly 10 MB, which is the index doing its job, not waste.
- **No length cap anywhere on `name`.** Cost is linear in L, so it cannot blow up
  superlinearly, but for calibration: a 32,768-char name costs **2.56 ms** and allocates
  ~2.3 MB transient (the 32k-pointer list plus 32k one-character strings). Real sources
  cap this in practice (Google `displayName`, OSM tag length), so this is a curiosity
  rather than a risk.
- **The cost is re-paid on the whole history every run, and that is a pipeline-shape
  issue, not this function's.** `main.py:150` loads the cumulative master,
  `main.py:179` concatenates `raw_filtered + master`, `main.py:180` dedups the union — so
  every run re-normalizes every historical name, forever. That is the quadratic-*across-
  runs* shape already documented in `213-x.md:6`,`:29`,`:89`; this function is one of the
  many flat passes it rides on. Do not "fix" it here.
- **Do not memoize.** Measured at n=200k with a rebuild-per-repetition harness:
  0% repeats → **0.92x** (slower), 5% → 0.95x, 10% → 1.00x, 20% → 1.12x, 30% → 1.22x,
  40% → 1.44x, 60% → 2.12x. Break-even is ~10% exact-name repeats; real corpora with
  chain branches (`Starbucks` ×N, `Al Baik` ×N) sit below that, and the memo's own cost
  is **125 B/entry** (25.1 MB traced live for 200k distinct names). The ASCII fast path is
  repeat-rate-independent, needs no memory, and beats the memo at every rate measured.
- **This lens is clean.** No correctness defect in `normalize_name` is in scope here; the
  Arabic hamza erasure (`"مطعم أبو شقير"` → `"مطعم ابو شقير"`) and the punctuation /
  invisible-character gaps are already owned by `058-english-arabic-name-matching.md`
  and `200-fn-dedup-py-normalize_name-contract.md`, and my patch is designed to compose
  with both rather than conflict.

---

## How I measured (reproducibility)

- **Interpreter:** CPython **3.14.8** — the only one installed on this box
  (`pyproject.toml:10` requires `>=3.12`; `pyproject.toml:40` targets `py312`). Relative
  ratios between C-level string ops and the per-character Python loop are stable across
  3.11–3.14, and the same caveat is recorded in
  `200-fn-dedup-py-_extract_city-perf.md:192-194`; absolute µs may drift 10–20% on 3.12.
- **Timer:** `time.process_time` (CPU), min of 3–9 repetitions. This box was ~1.5–2.9x
  oversubscribed during measurement (measured `wall/cpu` ratios of 1.55x, 2.55x, 2.92x
  on identical work), so CPU time is reported as primary and wall as secondary.
- **Isolation:** the target function was re-declared verbatim in the benchmark process.
  `dedup.py` was only ever imported read-only. For the "share of `dedup()`" column,
  `dedup.normalize_name` was rebound **in memory only** to a no-op for one comparison
  pass; `dedup.py` on disk was not modified, and the patched pass also produces fewer
  distinct index keys, so treat that column as ±2 points.
- **No network, no installs.** Corpora are synthetic: 55% Latin / 45% Arabic names with
  a realistic length distribution, mixed with a distinct-base corpus for the repeat-rate
  sweep. Real name-length and script-mix distributions for `Lebanon`/`Riyadh` were not
  measurable without running the scrapers, which the brief forbids — the 10.8x ASCII
  number is exact, the 4.4x corpus number inherits my 55/45 script-mix assumption (at 0%
  ASCII it degrades to 1.41x, at 100% ASCII it is 10.8x).
- No files other than this report were created or modified.

---

## Recommended order of work

1. **Apply the ASCII fast path** (S3, second finding). ~4 lines, provably
   output-identical, 4.4x on a realistic corpus, ~1.0 s saved per 500k-record run. Add a
   regression test asserting `normalize_name` output for a Latin name is unchanged — the
   30,025-string equivalence set above is the reference; `tests/test_lead_signal.py` has
   no `normalize_name` coverage today (it imports only `_merge` and `normalize_phone`,
   `tests/test_lead_signal.py:65`), so nothing existing can break.
2. **Coordinate the gate with the two correctness audits before merging either.**
   `200-fn-dedup-py-normalize_name-contract.md:350-355` already proposes gating a
   punctuation collapse on `name.isascii()`, and `:313-317` proposes an
   invisible-character table. Land one `isascii()` branch and put all three behaviours
   behind it, rather than nesting three gates.
3. **Optionally** replace the `dedup.py:68` loop with a lazily built `str.translate`
   combining-mark table (2.71x on non-ASCII names, 934 entries, 0.120 s one-time, build
   lazily not at import).
4. **Do not** memoize, and do not spend further optimization effort here: at 500k
   records this function is 3–6% of `dedup()` and <0.1% of the run. If someone wants a
   real 500k-scale win, the profile says it is in `_pick`/`_merge`/`_validity`
   (`307-fn-dedup-py-_validity-concur.md:100`) and above all in the 40-thread HTTP stage
   (`enricher.py:225`), not in name normalization.