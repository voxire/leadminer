# 303 — `_is_missing` (dedup.py:79), production-ops lens

## Verdict

`_is_missing` is a total, allocation-cheap, exception-free predicate — it cannot be
interrupted, cannot raise, and is not a performance concern. Its operational problem
is the opposite of fragile: **it is a decision function with zero telemetry that
confidently answers "present" for values that carry no information** (`nan`, `inf`,
`0`, `0.0`, `False`, `N/A`, `#N/A`). Those wrong answers are *ratcheted into the
cumulative master CSV* and become permanent, and they invert the product's core
signal — a lead with no website is silently filed into `with_websites.csv` and
excluded from `without_websites.csv`, which is the file salespeople actually work
from. Nothing between the HTTP response and the published Drive artifact checks for
any of this.

---

## The function under review

```python
# dedup.py:79-90
def _is_missing(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False
```

Two call sites, both in the merge hot path:

- `dedup.py:122` — `_validity()` returns `-100` for a missing value, `0..3` for a
  present one. This is the *scoring* half of the merge.
- `dedup.py:168-171` — `_pick()` short-circuits: if one side is "missing", the other
  side wins unconditionally, with no ranking at all.

Verified truth table (`dedup._is_missing`, run against the real module):

| input | `_is_missing` | `_validity("rating")` | `_validity("lat")` |
|---|---|---|---|
| `None` | `True` | -100 | -100 |
| `""`, `"   "`, `"\t\n"` | `True` | -100 | -100 |
| `"N/A"`, `"null"`, `"None"`, `"-"`, `"#N/A"` | **`False`** | 0 | 0 |
| `0`, `0.0`, `-0.0`, `"0"` | **`False`** | **3 (MAX)** | **3 (MAX)** |
| `False`, `True` | **`False`** | **3 (MAX)** | **3 (MAX)** |
| `float("nan")`, `float("inf")` | **`False`** | 0 | **3 (MAX)** |
| `[]`, `{}`, `()` | **`False`** | 0 | 0 |
| `"\u200b"` (zero-width space) | **`False`** | 0 | 0 |

Two rows in that table are actively harmful rather than merely permissive: the
non-finite floats and the numeric zeros are handed `_validity`'s **maximum** score by
`dedup.py:129-139`.

---

## Findings

### S1 — Non-finite floats are classified as perfect coordinates, and become unkillable in the master

- **Where:** `dedup.py:79-90` (`_is_missing`), `dedup.py:128-133` (`_validity` for
  `lat`/`lon`), `dedup.py:184` (`str(value)` tiebreak), `main.py:92-94`
  (`load_master` float coercion).
- **Breaks:** `nan`/`inf` are not "missing" and not merely tolerated — they score the
  **maximum** validity bonus and then win the merge outright against real coordinates,
  because `lat`/`lon` are absent from `_VOLATILE` (`dedup.py:114`) so the final
  tiebreak is a **lexicographic string comparison of untrusted numeric data**, and
  `"nan" > "33.8938"` and `"inf" > "33.8938"` are both `True`.
- **Concrete input (full chain, executed against the real modules):**

  ```
  1. an HTTP body containing the non-standard JSON literals NaN / Infinity
     -> httpclient.py:110-113  FetchResult.json() = json.loads(body)
        (no parse_constant guard; Python's json accepts these by default)
  2. osm.py:68   el.get("lat")            = nan
  3. dedup.py:168  _is_missing(nan)       = False      <-- says PRESENT
  4. dedup.py:131  _validity("lat", nan)  = 3          <-- MAX bonus
  5. dedup.py:187  _merge(a, b)           -> lat=nan lon=inf
                                                    <-- REAL COORDS DESTROYED
  6. main.py:127   csv writer emits       = 'nan' / 'inf'
  7. main.py:92    float('nan')           = nan       <-- NOT rejected
                                                    <-- ratchets forever
  ```

  Step 7 is the trap. `load_master` (`main.py:92-94`) *does* have a guard, and it
  correctly converts `N/A`, `null`, `#N/A` and `-` to `None` via `except (ValueError,
  TypeError)` — but `float("nan")` and `float("inf")` **succeed**, which is the exact
  hole in that guard.
- **Why a human never notices:** `_merge` discards the good value and prints nothing.
  `main.py:181` prints only `After merge + dedup: N unique businesses`. The row count
  is unchanged, so the CI gate at `scrape.yml:74-83` (`ROWS -lt 100`) passes, the run
  uploads to Drive at `scrape.yml:88-89`, and next week's run downloads that corrupted
  file as the master at `scrape.yml:56`. The bad coordinate is the new normal.
- **Blast radius:** the loss is irreversible. Once `nan` is the record's latitude, a
  real coordinate arriving every subsequent week **cannot dislodge it** — verified
  over 3 simulated runs: `rating=nan` and `lat=nan` survived unchanged while
  `website` and `instagram` were correctly repaired. `enricher.infer_region`
  (`enricher.py:89-93`) returns `None` for any non-finite coordinate because every
  comparison against a bounding box is `False`, so the record silently drops out of
  all 7 Lebanese regional buckets and lands in `Unknown` (`main.py:230`).
- **Fix:** make `_is_missing` treat non-finite numerics as missing (`math.isfinite`),
  make `_validity`'s lat/lon branch reject them, and pass
  `parse_constant=_reject` in `httpclient.FetchResult.json()`.

### S1 — Placeholder strings are treated as real data, which mis-files leads into the wrong revenue CSV

- **Where:** `dedup.py:88-89`; consequences at `main.py:199-200` and
  `main.py:137-145`.
- **Breaks:** `_is_missing` normalises whitespace but has no notion of a *null
  literal*. `"#N/A"` is not empty, so it is "present". Every downstream filter in the
  pipeline tests **truthiness**, not `_is_missing`, so the placeholder flows straight
  through and lands the lead in the wrong file.
- **Concrete input:**

  ```
  record = {"name": "Cafe Hamra", "website": "#N/A", "instagram": "#N/A",
            "industry_priority": "high", ...}

  dedup._is_missing("#N/A")  -> False            # treated as a real URL
  main.py:199  bool(r["website"])   -> True      # -> with_websites.csv
  main.py:200  not bool(...)        -> False     # NOT -> without_websites.csv
  main.py:143  len("#N/A") > 3      -> True      # -> sales_ready.csv
  ```

  `without_websites.csv` is documented in `BRIEF.md:54` and in `main.py:11` as the
  new-site pitch targets. A business with **no website** is silently classified as
  having one and removed from that file, while `sales_ready.csv` hands a salesperson
  an `#N/A` "Instagram handle". Nothing in the log indicates a subset was lost.
- **Why it persists forever:** the master round-trip preserves it. `load_master`
  (`main.py:88-89`) only converts the **exact empty string** to `None`; a string
  field is not in `_FLOAT_FIELDS`/`_INT_FIELDS` (`main.py:43-44`), so no coercion ever
  touches it. `write_csv` (`main.py:127`) writes it back verbatim. `dedup.py:168-171`
  cannot repair it either, because the "other side" is equally empty — the
  short-circuit has nothing better to offer.
- **Fix:** normalise null literals alongside whitespace at `dedup.py:88-89`
  (`_NULL_LITERALS = {"", "n/a", "na", "null", "none", "nil", "-", "--", "?", "nan"}`)
  and apply the same normalisation in `load_master` so the repair happens on read, not
  only on merge.

### S2 — `0` / `0.0` / `False` are scored as maximum-quality information

- **Where:** `dedup.py:79-90`, `dedup.py:134-139` (`_validity` for `rating`),
  `enricher.py:311-314` (the consumer).
- **Breaks:** `_validity`'s range check is `0 <= v <= 5` (`dedup.py:139`), and
  `False == 0` and `0.0 == 0`, so a **zero rating receives the maximum bonus of 3**.
  That is then consumed as a pain-point signal:

  ```
  rating=None  -> _is_missing True  -> lead_score 10
  rating=0.0   -> _is_missing False -> lead_score 20    # +10 phantom points
  rating=4.5   -> _is_missing False -> lead_score 10
  ```

  `enricher.py:311` documents this branch as the "Low rating = pain point worth
  pitching" signal. A zero therefore **manufactures a pitch** out of a record that
  carries no rating information.
- **Concrete input:** `_merge({'rating': 0.0, 'source': 'google_places'},
  {'rating': 4.5, 'source': 'osm'})` returns `rating=0.0`. Google's rating trust is 3
  (`dedup.py:99`) and `_SOURCE_TRUST["osm"]` has no `rating` key, so it falls back to
  `_DEFAULT_TRUST` 1 (`dedup.py:111`, `dedup.py:151`). The trust axis at
  `dedup.py:181` decides before validity is ever compared — so the zero wins on
  validity *and* on trust. Same for `lat=0.0`/`lon=0.0`: `_is_missing(0.0)` is
  `False` and `_validity("lat", 0.0)` is 3, producing a row with `region=Beirut` and
  coordinates in the Gulf of Guinea (verified: `infer_region("Hamra, Beirut, Lebanon",
  0.0, 0.0, "LB")` -> `"Beirut"`).
- **Honest scoping:** unlike the NaN case, I could not find a path in the *current*
  scrapers that emits a zero rating — `google_places.py:297` and `osm.py:114` pass
  `place.get("rating")` / `None` through. This is reachable if any dependency returns
  `0`, and it is **guaranteed** to reach the record if a human types `0` into a
  spreadsheet cell, which `main.py:118-119` explicitly invites ("so Excel and Google
  Sheets detect UTF-8"). Severity reflects blast radius and the fact that `_is_missing`
  is the only gate here, not observed frequency.
- **Fix:** treat `0`/`0.0`/`False` as missing for the numeric columns specifically —
  add a field-aware zero check in `_validity` (`dedup.py:124-139`) rather than
  changing `_is_missing` globally, since `website_live=False` legitimately *is*
  information.

### S2 — Zero observability at the one place where irreversible per-field decisions are made

- **Where:** `dedup.py:79-90` (no `print`, no `logging`), `main.py:181` (the only
  merge-stage output), `scrape.yml:71-83` (the only automated gate).
- **Breaks:** `_is_missing` emits **nothing** — no return code, no counter, no log.
  It is invoked roughly 55 times per merged field-pair (`_pick` twice per field at
  `dedup.py:168,170`, `_validity` once more at `dedup.py:122`) and decides, with full
  authority, whether a real value survives. The entire run's only signal about the
  merge is a single integer:

  ```
  main.py:181   print(f"After merge + dedup: {len(records)} unique businesses")
  ```

  A count cannot detect corruption because every finding above leaves the count
  unchanged. Verified: a 400-row master in which **every** row has `website="#N/A"`,
  `lat=nan`, `rating=0.0` produces
  `with_websites = 400`, `without_websites = 0`, `sales_ready = 400`, and passes
  `scrape.yml:80` (`ROWS >= 100`). `main.py:222-226` prints
  `With website: 400 / Without website: 0 / SALES-READY: 400`, which reads like a
  healthy weekly report to anyone skimming.
- **Missing counters** (each would have caught a distinct finding above): merges
  performed; fields resolved by the `dedup.py:168-171` short-circuit vs. by `rank`;
  values discarded by `rank` (i.e. real URLs thrown away); fields rejected by
  `_validity` as implausible; records carrying a sentinel literal or non-finite float.
- **Fix:** return a reason code from `_is_missing` and accumulate counters in
  `dedup()`, then print them next to `main.py:181`; assert
  `len(without_websites) > 0` and `len(sales_ready) > 0` in the `scrape.yml` gate.

### S3 — The existing test suite tests `_is_missing`'s intent but not its blind spots

- **Where:** `tests/test_lead_signal.py:267-326` (`TestMergeSurvivorship`).
- **Detail:** the suite covers `""` (`test_empty_string_does_not_beat_a_real_value`,
  line 286), a malformed email (line 297), and an out-of-range `rating=99`
  (`test_out_of_range_rating_is_rejected`, line 311). It has **no** case for
  `0`/`0.0`, `False`, `nan`, `inf`, or any sentinel string. Note the irony of line
  311: `99` is rejected because it is out of range, while `0` is *in* range and is
  therefore actively rewarded.
- **Fix:** add a `_is_missing` truth-table test and one `_merge` case per sentinel.

### S3 — Latent: containers and zero-width space are "present"

- **Where:** `dedup.py:88-90`.
- **Detail:** `[]`, `{}`, `()` and `"\u200b"` all return `False`. `"\u200b"` is
  U+200B ZERO WIDTH SPACE, for which `str.isspace()` is `False` and `.strip()` is a
  no-op, so `dedup.py:89` cannot see it — a cell containing only an invisible
  character counts as a website. Harmless today (no column holds a container), but it
  means `_is_missing` gives no type guarantee to `_pick`.
- **Fix:** switch `dedup.py:88` to `isinstance(value, str)` plus an explicit
  `collections.abc.Container` rejection, and strip U+200B–U+200D explicitly.

---

## Answers to the specific operational questions asked

**What does it print?** Nothing. `_is_missing` contains no `print`, no `logging`, no
counter. It is called in the hottest loop in the merge and reports nothing at all
about any of its decisions.

**What does it swallow?** Nothing — and this is a genuine strength. `_is_missing`
cannot raise: fuzzed against 19 pathological inputs (`bytes`, `bytearray`,
`memoryview`, `set`, `range`, an object whose `__str__` raises, `object`, `type`,
`Ellipsis`, `NotImplemented`, …) the only input that raised was a hand-crafted `str`
subclass with a deliberately broken `.strip()`, which no CSV or JSON path can
produce. No `try`/`except` is needed around it and there is no hidden failure to
unpack.

**Network drops halfway?** `_is_missing` is not involved, and this case is handled
correctly. `check_websites` (`enricher.py:161-192`) maps a transport failure to
`UNKNOWN`, which becomes `website_live = None` (`enricher.py:250`), and
`_is_missing(None)` is `True` — the right answer. The real exposure is that the
already-corrupted master still gets published afterwards (`scrape.yml:88-89`), so a
network drop costs you the run but not the accumulated NaN.

**A dependency returns garbage?** This is the S1s. `NaN`/`Infinity` in a JSON body
become real Python floats via `json.loads` at `httpclient.py:113` (no
`parse_constant` guard), then survive `_is_missing`, score maximum `_validity`,
destroy real coordinates in `_pick`, and ratchet into the master permanently.
Sentinel strings survive `_is_missing` and mis-file leads into the wrong CSV.

**Called with an empty list?** `dedup([])` returns `[]`; `_is_missing` is never
called at all (the `dedup.py:201` loop body never executes). The consequence is
downstream: `write_csv` (`main.py:108-134`) would rewrite the **cumulative master**
as a header-only file. This one *is* caught — `scrape.yml:74-76`'s `-s` test passes
(header bytes are non-empty) but `scrape.yml:78-83` computes `ROWS = wc -l - 1 = 0`
and fails the `< 100` check. Good.

**When interrupted?** `_is_missing` cannot be interrupted. It has no I/O, no locks,
no `yield` points, and allocates nothing, so `KeyboardInterrupt`/`SIGTERM` cannot land
inside it; it is atomic with respect to signals. Interruption is also safe downstream:
`write_csv` writes to a temp file, `fsync`s, then `os.replace`s (`main.py:124-133`),
and `scrape.yml:25`'s `timeout-minutes: 300` would fire during `check_websites`
(`main.py:187`) — *before* any `write_csv` at `main.py:209` — so there is no partial
output. **The trap is the artifact step**: `scrape.yml:91-97` runs
`if: always()` and uploads `data/`, which at that moment holds only the *previous*
week's master downloaded at `scrape.yml:56`. An engineer debugging a timeout opens a
full-looking, healthy CSV and concludes the run was fine. Also note the requested
"six hours" exceeds `timeout-minutes: 300` (`scrape.yml:25`) — a genuine 6-hour
budget is killed at 5, with no partial output and no signal beyond a red X.

---

## Not a bug, but worth knowing

- **Not a performance concern.** Measured on this machine: `_merge` costs **61 µs**
  per pair, i.e. **~244 ms** for 4,000 duplicate records in a run — under 0.02% of the
  300-minute budget. The 6-hour budget is spent entirely in `check_websites` (40
  workers × 8s timeout × N sites). Do not spend optimisation effort here.
- **The `("", "")` mega-key at `dedup.py:214` is benign.** I expected 500
  address-less records to collapse into one row and destroy real leads. They do
  collapse (500 → 3 rows), but `_pick`'s missing-branch (`dedup.py:168-171`) means a
  named record's `name` always beats `""`, so both genuinely-named businesses in the
  batch survived **intact**. This is `_is_missing` working as designed — a `""` name
  carries no information and is correctly overwritten. Worth a metric, not a fix.
- **`_merge` shallow-copies values** (`dedup.py:191-194` assigns values, not copies).
  Safe today because all 22 columns in `main.py:33-40` are scalars, but a future
  list- or dict-valued column would be aliased across every merged record.
- **A stale `website_live=False` cannot survive** — I traced this looking for a
  separate bug and could not construct one. Any record with a truthy `website` has
  `website_live` unconditionally overwritten by `check_websites`
  (`enricher.py:231`, `enricher.py:250`), so the `dedup.py:114` recency tiebreak for
  that field is effectively dead code. **However**, `main.py:103` compares
  `wl == "True"` / `wl == "False"` case-sensitively against Python's `repr`; a
  lowercase `true` from any hand-edit collapses to `None`, silently forfeiting the
  +20 server-confirmed-dead rebuild pitch (`enricher.py:303`). A one-way ratchet in
  the safe direction, but still a silent score change.
- **`_pick`'s "deterministic final tiebreak" (`dedup.py:184`) is a string comparison
  of untrusted data.** It is deterministic, which is what the tests at
  `tests/test_lead_signal.py:292-295` check, but it is the direct mechanism by which
  `"nan"` and `"inf"` outrank real coordinates. Determinism was the requirement;
  lexicographic ordering was an arbitrary implementation choice with a live
  consequence.

---

## Recommended order of work

1. **S1 — non-finite floats.** Three-line guard, unbounded downside: `math.isfinite`
   in `_is_missing`, reject non-finite in `_validity`'s lat/lon branch, and
   `parse_constant=_reject` in `httpclient.py:113`.
2. **S1 — sentinel strings.** Normalise null literals in `_is_missing` and in
   `load_master`, so `without_websites.csv` and `sales_ready.csv` stop mis-filing.
3. **S2 — counters + gate.** Print merge counters beside `main.py:181`; add
   `without_websites > 0` and `sales_ready > 0` assertions to `scrape.yml`. Without
   this, fixes 1 and 2 stay invisible and the next corruption is as slow to catch as
   this one.
4. **S2 — zero-value semantics** for the numeric columns only, leaving
   `website_live=False` alone.
5. **S3 — tests** for the truth table, then the latent container / zero-width-space
   tightening.