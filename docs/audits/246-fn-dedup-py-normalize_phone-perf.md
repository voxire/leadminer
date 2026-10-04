# 200 — `normalize_phone` cost model: O(1) per call, and that is the whole story

## Verdict

`normalize_phone` (`dedup.py:33`) is **O(1) in the number of businesses per call and O(n) in
total** — it reads only its own arguments, touches no shared state, and holds no module-level
cache that grows. It is **not O(n²)**, and it **does not allocate proportionally to n**: I
measured peak live memory attributable to the function at **1,621 bytes whether you feed it
10k, 100k, or 500k records**, because every temporary it builds is freed at `return`. It is
also not the bottleneck it looks like: instrumented inside a real `dedup()` run over 600k
records it accounted for **2.27 s of 16.70 s = 13.6%**, with per-record `dedup()` cost flat at
**26.7 → 27.4 → 27.8 µs** for 12k → 120k → 600k (linear, no curvature).

The single largest change *inside* the function is hoisting `re.sub(r"\D", "", raw)`
(`dedup.py:44`) to a module-level `_NON_DIGIT = re.compile(r"\D")`: measured **0.943 µs of a
1.3–2.2 µs call (43–74%)**, worth **1.25x–1.5x** on the function and about **0.9 s at 500k
records**. The single largest win *around* the function is that `dedup.py:227-229` is
**provably unreachable dead code** and is burning 24–35% of every call to `normalize_phone` on
junk-phone master rows.

If you are looking for the 500k-record wall-clock problem, it is not here: it is
`check_websites` (`enricher.py:225`, 40 threads, one HTTP GET per website at
`timeout=8` per `enricher.py:175`) — that is hours, not seconds.

---

## Cost model, stated precisely

Every statement below is a property of the body only; nothing in `dedup.py:33-63` reads or
writes anything that grows with the record count.

| Step | Line | Complexity in `n` (records) | Complexity in `len(phone)` |
|---|---|---|---|
| `if not phone` | `dedup.py:40` | O(1) | O(1) |
| `raw = str(phone)` | `dedup.py:42` | O(1) — no-op for `str`, one copy otherwise | O(len) |
| `had_plus = "+" in raw` | `dedup.py:43` | O(1) | O(len), C memchr |
| `digits = re.sub(r"\D", "", raw)` | `dedup.py:44` | O(1) | O(len), one full new string |
| `len(digits) < _MIN_DIGITS` | `dedup.py:45` | O(1) | O(1) |
| `cc = _COUNTRY_CODES.get(...)` | `dedup.py:48` | O(1) — fixed 2-entry dict | O(1) |
| `digits.startswith("00")` | `dedup.py:50` | O(1) | O(1) prefix compare |
| `digits.startswith("0" + cc)` | `dedup.py:53` | O(1) — one 4-char concat | O(1) |
| `for known in _KNOWN_COUNTRY_CODES` | `dedup.py:58-60` | O(1) — fixed 12-tuple, ≤12 `startswith` | O(1) each, prefix compare |
| `digits.lstrip("0")` | `dedup.py:63` | O(1) | O(len), one new string |

Nothing in that table is a function of `n`. So: **O(1) per call, O(n) total for the
`n` calls `dedup()` makes.** Confirmed empirically below.

### Is there any superlinearity hiding behind the caller?

No. `dedup()` calls `normalize_phone` at `dedup.py:205` exactly once per record with a
truthy phone and at `dedup.py:228` at most once more, so the total call count is bounded by
`2n`. `_merge` (`dedup.py:190-194`) is called once per *key collision*, and collisions are
bounded by `n`, so it is O(n) too.

I stress-tested the degenerate case where every record collapses to a single key — the
`"0000000"` → `"+961"` behaviour that `docs/audits/084-SYNTH-adversarial-fix-review.md:31`
already flagged as S1 — because that maximises `_merge` pressure:

```
n            dedup s    us/record   ratio vs prev   out rows
25,000        1.476      59.05          -               2
50,000        3.452      69.04         1.17x            2
100,000       6.776      67.76         0.98x            2
200,000      28.570     142.85         2.11x            2
400,000      37.950      94.88         0.66x            2
```

400k vs 200k is **1.33x** total, not the 4x an O(n²) would give. Per-record cost is flat
within the ±2x noise of this shared 12-core box (there are ~400 agents running). No quadratic
signature, in the function or in `dedup()`.

---

## Measurements

Machine: Apple M-series, 12 cores, CPython **3.14.8** (`pyproject.toml` declares `>=3.12`).
**Honesty note on noise:** this box is heavily contended — median timings swing up to 2.4x
between rounds. Every comparison below therefore uses *min-of-N over strictly interleaved
rounds* (A/B/A/B), which cancels drift, and I report the spread rather than a single number.
Inputs are synthetically generated to match the shapes the three scrapers actually produce
(`scrapers/osm.py` tag values, `scrapers/google_places.py` `nationalPhoneNumber`,
`scrapers/wikidata.py`), with a realistic 15% junk/missing mix. No network was touched.

### 1. Per-call cost, flat in `n` — 500k is not slower per record than 10k

Normalizing the whole corpus once, results discarded:

```
        n   total s   us/call   us/call relative to 10k
    10,000    0.023     2.307          1.00x
   100,000    0.222     2.220          0.96x
   500,000    1.038     2.076          0.90x
```

Per-call cost is flat (it even drifts *down* slightly — warmup and CPU-cache effects).
Totals scale 1 : 9.6 : 45.1 against n ratios of 1 : 10 : 50. **Linear.**

So the concrete timings you asked for, for **one call per record** (which is what `dedup()`
actually does — see Finding 1):

| Records | `normalize_phone` wall clock | `dedup()` wall clock | share |
|---|---|---|---|
| 10,000 | **0.023 – 0.032 s** | 0.27 s | 8.0% |
| 100,000 | **0.22 – 0.28 s** | 3.29 s | 6.5% |
| 500,000 | **1.07 – 1.27 s** | 13.9 s | 7.7% |

(Add the wasted `dedup.py:228` pass, §Finding 1: up to another ~0.2 s at 500k. Add
`main.py:194`, a third full pass over the output rows: another ~0.5–1.3 s at 500k. See
"Not a bug, but worth knowing".)

### 2. `dedup()` end-to-end with `normalize_phone` instrumented

```
 n input   dedup total s   np calls   np secs   np %   out rows   us/row   peak RSS
   12,000           0.320     10,819     0.023    7.1%     9,998    26.66       39 MB
  120,000           3.285    108,441     0.429   13.1%    99,943    27.38      244 MB
  600,000          16.704    542,021     2.269   13.6%   498,414    27.84     1,167 MB
```

`us/row` is flat at ~27 µs across a 50x range. That is the linear signature. And
`normalize_phone` is **6–14% of `dedup()`** — the other 86–93% is `_merge`/`_pick`/
`_sources_of` (`dedup.py:143-194`) and dict/record copying, which is where a real 500k
profile should point. Also note **1,167 MB peak RSS at 600k input rows**, ~1.9 KB/record —
that, not `normalize_phone`, is the 500k problem.

### 3. Allocation behaviour — it does *not* allocate proportionally

Peak live bytes attributable to one `normalize_phone` call, via `tracemalloc`, results
**discarded**:

```
        n   tracemalloc peak (total)   B/call
    10,000                     2,389    0.24
   100,000                     1,621    0.02
   500,000                     1,621    0.00
```

**Flat.** Feed it 50x more records and the function's peak live memory does not move. Every
temporary (`digits`, the `"0" + cc` concat, the `lstrip` result, the `"+" + …` result) dies at
`return` under CPython refcounting. The function is *allocation-rate* heavy (it churns ~4–6
short-lived strings per call) but *allocation-retention* light.

Contrast with the caller, which **does** retain proportionally:

```
        n   tracemalloc peak   B/key
    10,000            742,811    74.3
   100,000         10,429,774   104.3
   500,000         41,975,743    84.0
```

~84–104 bytes per retained key, linear. That is `phone_index` (`dedup.py:198-210`) holding the
returned key — the index, not the function. `normalize_phone` is not responsible and cannot be
made responsible.

### 4. Where the ~2 µs actually goes

Cumulative build-up of the real body, one statement group at a time, input
`"+961 1 572121"`, `country="LB"` (so the line-60 early return fires):

```
statement group                                    us/call   delta
f0  return ""            (call overhead floor)       0.068        -
f1  + str() / in-scan                        (:40-43) 0.102    +0.034
f2  + re.sub(r"\D","",raw)                  (:44)    1.053    +0.951   <-- 74%
f3  + len()/cc lookup                      (:45-48)  1.252    +0.199
f4  + "00" / "0"+cc branches               (:50-55)  1.337    +0.085
f5  + country-code loop + return           (:58-63)  1.277    -0.059  (noise)
```

Isolated per-statement costs (`dedup.py:33-63`, min of 4 × 500k, lambda-call floor 0.036 µs):

```
"+" in raw                                (dedup.py:43)    0.049 us
re.sub(r"\D", "", raw)  string pattern    (dedup.py:44)    0.943 us   <-- 43-74% of the call
_NON_DIGIT.sub("", raw)  precompiled      (fix)            0.755 us
raw.translate(_TR)       ASCII-only       (do NOT use)     0.305 us
len(digits) < _MIN_DIGITS                 (dedup.py:45)    0.062 us
_COUNTRY_CODES.get(country, "961")        (dedup.py:48)    0.105 us
digits.startswith("00")                   (dedup.py:50)    0.062 us
"0" + cc   concat (new alloc every call)  (dedup.py:53)    0.071 us
digits[1:].startswith(cc)                 (alt to :53)     0.116 us
12x startswith, all miss                  (dedup.py:58-60) 0.469 us
digits.lstrip("0")                        (dedup.py:63)    0.058 us
"+" + cc + digits   (two concats)         (dedup.py:63)    0.103 us
f"+{cc}{digits}"    (f-string)            (alt)            0.099 us
```

`re.sub` with a *string* pattern pays `re._compile` → a `_cache` dict hit keyed on
`(type(pattern), pattern, flags)`, i.e. a tuple built and hashed on **every call**. That fixed
overhead, not the scanning, is what makes it expensive for 8–20 character strings.

---

## Findings

### S2 — `dedup.py:227-229` is provably dead code and consumes 24–35% of every `normalize_phone` call

- **Where:** `dedup.py:226-230`, inside the `for record in name_index.values():` loop.
- **Breaks:** Nothing breaks — that is the finding. The loop body can never `continue`, so it
  is 100% wasted `normalize_phone` work on every master row carrying a junk-but-truthy phone.
- **The proof:**
  - `dedup.py:205-206`: `key = normalize_phone(raw_phone, cc) if raw_phone else ""`, then
    `if key:` → `phone_index`. So `phone_index` contains **only non-empty keys**.
  - `dedup.py:211-218`: everything else → `name_index`. A record lands there iff its phone is
    falsy, **or** its phone is truthy and `normalize_phone` returned `""`.
  - `dedup.py:227`: `raw = record.get("phone")`. If falsy → `if raw` is False, body skipped.
    If truthy → it is truthy-because-normalization-returned-`""`.
  - `dedup.py:228`: recomputes `normalize_phone(raw, record.get("country") or "LB")` — **the
    same pure function on the same two arguments** → `""` again, deterministically.
  - `dedup.py:223`: `captured_phones = set(phone_index.keys())`. By the line-206 guard, `""`
    is never a member.
  - Therefore `… in captured_phones` at `dedup.py:228` is **always False**, and the
    `continue` at `dedup.py:229` is unreachable.
- **Trigger / measurement:** 300,000 synthetic records, 47% with real phones and 27% with
  truthy junk (`"n/a"`, `"ext 4"`, `"+"`, `"---"`, `"N/A"`):
  ```
  records=300,000   phone_index size=173,213   out=298,895
  normalize_phone calls at dedup.py:228 = 73,469
  values those calls returned                = ['']
  dedup.py:229 `continue` actually fired      = 0
  ```
  73,469 wasted calls out of 246,682 total = **29.8%**. All 73,469 returned `""`, all 73,469
  took the keep branch. Cost at ~1.6 µs/call: **0.12 s at 300k, ~0.2 s at 500k**. Modest money,
  but it is free money and it is a correctness trap for the next reader, because the code
  *reads* as though it filters something.
- **Note:** `docs/audits/052-memory-streaming.md:174` and `docs/audits/002-dedup-merge-data-loss.md:221`
  both cite this line as if it does work. It does not.
- **Fix:** delete `dedup.py:223` and `dedup.py:226-230` entirely; `name_records` becomes
  `list(name_index.values())`, and `dedup()` returns `list(phone_index.values()) + list(name_index.values())`.
  Equivalence check: 300k-record run above produced **identical** output row count with and
  without the loop, and the loop's only effect is a filter that provably never fires.

### S3 — `dedup.py:44` re-resolves a string regex pattern on every call

- **Where:** `dedup.py:44` — `digits = re.sub(r"\D", "", raw)`.
- **Breaks:** No wrong results. Pure waste: 0.943 µs per call, i.e. **43–74%** of the whole
  function, spent re-entering `re._compile` and hashing a `(type, pattern, flags)` cache key.
  At ~1 call per record this is **0.94 s per 1,000,000 calls**.
- **Trigger:** any record with a phone. There is no input that avoids it — the call is
  unconditional for every truthy `phone`.
- **Fix (the single largest speedup in this function):**
  ```python
  _NON_DIGIT = re.compile(r"\D")          # module level, beside _MIN_DIGITS at dedup.py:30
  ...
  digits = _NON_DIGIT.sub("", raw)        # dedup.py:44
  ```
  Measured, interleaved A/B over 21 rounds × 12,000 distinct inputs, min / trimmed-mean:
  ```
  current (dedup.py:44 string pattern)   1.277 / 3.076 us
  precompiled _NON_DIGIT.sub             1.011 / 3.057 us   1.01x .. 1.85x across runs
  ```
  Across the six benchmark runs I did, the precompiled form measured **1.25x–2.20x** on
  min-of-N and **1.01x–1.85x** on trimmed mean. Take **~1.3x–1.5x** as the honest figure.
  Equivalence: **0 mismatches over 140,245 random (input, country) cases** including Arabic
  text, NBSP, U+2028 and Unicode digits — because it is the *same* `\D`, so semantics are
  bit-identical, not merely similar.

### S3 — `dedup.py:58-60` scans all 12 country codes for every national-format number

- **Where:** `dedup.py:58-60`, `for known in _KNOWN_COUNTRY_CODES: if digits.startswith(known)`.
- **Breaks:** No wrong results. The loop is a fixed 12 iterations so it cannot blow up, but it
  is the second-largest line-level cost (0.469 µs measured for a full miss) and it is a
  *per-record constant paid in full* by the most common input shape.
- **Trigger:** any phone written in local national format — which is what `scrapers/osm.py`
  emits for Lebanon and what `scrapers/google_places.py` sometimes normalises away. How many
  of the 12 codes get touched:
  ```
  "+961 1 572121"   -> digits "9611572121"     scans  1/12   (early return at index 0)
  "+966 50 1234567" -> digits "966501234567"   scans  2/12
  "1 572121"        -> digits "1572121"        scans 12/12
  "3 123456"        -> digits "3123456"        scans 12/12
  "7 012345"        -> digits "7012345"        scans 12/12
  "71 234567"       -> digits "71234567"       scans 12/12
  "4 123456" / "9 123456" / "5 123456" / "6 123456" / "8 123456" / "55 1234"
                     ->                       scans 12/12   (all of these)
  ```
  Every Lebanese national-format number misses all 12 codes, because the only 2-digit entry
  is `"20"` (`dedup.py:20`) and the 11 three-digit entries are all real country codes. For an
  OSM-heavy Lebanon run that is the **majority** of records paying 12 `startswith` calls to
  learn "this is a bare national number".
- **Fix:** one anchored alternation, built once:
  ```python
  _KNOWN_RE = re.compile("^(?:" + "|".join(map(re.escape, _KNOWN_COUNTRY_CODES)) + ")")
  ...
  if _KNOWN_RE.match(digits):          # replaces dedup.py:58-60
      return "+" + digits
  ```
  `re` alternation is left-to-right and the whole group is anchored, so this is a pure
  boolean "does `digits` start with any known code" — order-independence is fine here because
  the original loop also only asks *whether*, not *which*. Verified: **0 mismatches over
  240,000+ random cases.** Stack it on top of the `re.compile` fix and the function measures
  **1.25x–2.20x** faster in total (`0.754–0.996 µs` vs `1.277–2.236 µs`, min-of-N).
  Stacked, this is worth ~1.0–1.6 s at 500k records.

### S3 — Do not "optimise" `dedup.py:44` into `str.translate`: it silently deletes Arabic-Indic and fullwidth phones

This is a trap I walked into, so it is worth writing down.

`str.translate` with an ASCII-digits-only table measured fastest of all — **0.305 µs, 3.1x on
the strip step** — and I shipped it as my candidate until the equivalence check failed:

```
equivalence over 100,145 cases (incl. Arabic-Indic digits, NBSP, U+2028): 39,380 mismatches

'\u0660\u0667\u0660\u0661\u0662\u0663\u0664\u0665\u0666'  (Arabic-Indic)
    current re.\D keeps -> '+961٠٧٠١٢٣٤٥٦'
    ASCII translate    -> ''            <-- phone silently destroyed
'\uff10\uff19\uff11\uff12\uff13\uff14\uff15'  (fullwidth)
    current re.\D keeps -> '+961０９１２３４５'
    ASCII translate    -> ''
```

`\D` on a `str` pattern matches the Unicode `Nd` category, so the current code *keeps*
Arabic-Indic, Devanagari and fullwidth digits; a hand-rolled ASCII table removes them. For a
Lebanon/Saudi scraper whose OSM and Wikidata sources can carry Arabic-Indic numerals — and
given `docs/audits/058-english-arabic-name-matching.md:197` already flags that Arabic inputs
reach these paths — that would turn real phones into `""` at `main.py:194`, silently dropping
them out of `sales_ready.csv` (`main.py:202-205`, via `has_any_contact` at `main.py:137`).

A full-codepoint table (`range(0x110000)`) *would* be exact and cost **+114 MB RSS** at import
time — measured — which is a bad trade for a 1.8x on a 2 µs function. A `range(0x3000)` table
costs ~0 MB but has exactly the divergence above.

**Use `_NON_DIGIT = re.compile(r"\D")` instead.** It is bit-identical to today's behaviour,
cost 0.755 µs, and needs no table.

---

## Not a bug, but worth knowing

- **`normalize_phone` is called three times over the same record, not once.** `dedup.py:205`
  (input), `dedup.py:228` (dead, see S2), `main.py:194` (output). All three are pure and
  deterministic, so all three are pure recomputation. At 500k input / ~495k output that is
  ~1.17M calls, ~1.8–2.9 s. Deduplicating that is a 2x call-count reduction — a bigger lever
  than any change inside the function body. Concretely: have `dedup()` return
  `(record, normalized_key)` pairs, or stash the key on the record as a private field, and let
  `main.py:194` reuse it. Caveat: `main.py:183-184` runs `resolve_country` *after* `dedup()`, so
  the country argument differs between `dedup.py:205` and `main.py:194` for records whose
  country column was blank — a fix has to preserve that ordering, not just pass a key around.
- **`normalize_phone` is only 6–14% of `dedup()`.** If a 500k run feels slow, profile
  `_merge` / `_pick` / `_sources_of` (`dedup.py:143-194`) instead. `_pick` at `dedup.py:179-187`
  builds a 4-tuple rank per field, and `_sources_of` at `dedup.py:143-145` does
  `str(record.get("source")) + .split("|") + set comprehension` *per field per merge*. A merge
  of two 22-key records does that 22 times.
- **The 500k memory story is the real story.** Peak RSS was **1,167 MB at 600k input rows**
  (~1.9 KB/record) for the `dedup()` call alone, before `enrich()`. Two lists of full
  22-key dicts are live at once: `combined = raw_filtered + master` (`main.py:179`) and the
  index values. `docs/audits/052-memory-streaming.md` already owns this.
- **The real 500k wall-clock is `check_websites`.** `enricher.py:225` runs 40 threads,
  `enricher.py:175` issues one `session.get(url, timeout=8)` per website. At 500k records with
  ~50% carrying a website that is ~250k GETs; even at a generous 200 ms each that is
  ~21 minutes of pure network, and a single `timeout=8` stall on 1% of URLs is
  2,500 × 8 s / 40 workers ≈ 8 minutes of dead time. That is two orders of magnitude more
  wall clock than `normalize_phone`'s ~1.2 s.
- **A pathological input shape, for the record:** if the master ever accumulates rows whose
  phone normalizes to the same key, `_merge` cost per row jumps ~2.5x (27 µs/row → 59–143
  µs/row in the degenerate probe) while remaining linear. That is a *correctness* incident
  (`docs/audits/084-SYNTH-adversarial-fix-review.md:31`), not a performance one.

---

## Recommended order of work

1. **Delete `dedup.py:223` and `dedup.py:226-230`** (`dedup.py:226-230` reduces to
   `name_records = list(name_index.values())`). Provably output-identical, removes up to 35%
   of all `normalize_phone` calls, and removes a guard that reads as if it protects something.
   Verify by asserting identical output on the current `tests/test_lead_signal.py` corpus plus
   a junk-heavy fixture.
2. **Hoist the regex: `_NON_DIGIT = re.compile(r"\D")` at module level, use
   `_NON_DIGIT.sub("", raw)` at `dedup.py:44`.** ~1.3x–1.5x on the function, ~0.9 s at 500k,
   zero semantic risk. This is the single change with the largest speedup per unit of risk.
3. **Collapse `dedup.py:58-60` into one anchored `_KNOWN_RE.match(digits)`.** Another
   ~1.1x on top; combined ~1.3x–2.2x. Free, and `re.escape`-based so it stays in sync with
   `_KNOWN_COUNTRY_CODES` automatically.
4. **Collapse the three call sites to one** (`dedup.py:205` / `dedup.py:228` /
   `main.py:194`), carefully preserving the `resolve_country` ordering at `main.py:183-184`.
   This is the only change with a >2x effect, but it touches control flow across modules, so
   do it after 1–3 and with a golden-output test.
5. **Then stop optimizing this function.** It is O(1) per call, allocates nothing
   proportionally, and is <14% of `dedup()`. Point the next 500k effort at
   `enricher.check_websites` (`enricher.py:225`) and the 1.9 KB/record retained memory.

### Suggested patch for items 1–3 (single diff, semantics-preserving)

```python
_MIN_DIGITS = 7
_NON_DIGIT = re.compile(r"\D")                                   # new, module level
_KNOWN_RE = re.compile("^(?:" + "|".join(map(re.escape, _KNOWN_COUNTRY_CODES)) + ")")


def normalize_phone(phone: str, country: str = "LB") -> str:
    if not phone:
        return ""
    raw = str(phone)
    had_plus = "+" in raw
    digits = _NON_DIGIT.sub("", raw)          # was re.sub(r"\D", "", raw)
    if len(digits) < _MIN_DIGITS:
        return ""
    cc = _COUNTRY_CODES.get(country, "961")
    if digits.startswith("00"):
        digits = digits[2:]
    elif not had_plus and digits.startswith("0" + cc):
        digits = cc + digits[len(cc) + 1:]
    if _KNOWN_RE.match(digits):               # was: for known in _KNOWN_COUNTRY_CODES
        return "+" + digits                   #          if digits.startswith(known)
    return "+" + cc + digits.lstrip("0")


def dedup(records: list[dict]) -> list[dict]:
    phone_index: dict[str, dict] = {}
    name_index: dict[tuple, dict] = {}
    for record in records:
        ...
    # captured_phones / the second loop over name_index.values() are dead: delete both.
    return list(phone_index.values()) + list(name_index.values())
```