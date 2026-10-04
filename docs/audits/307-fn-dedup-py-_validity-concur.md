# 307 — `_validity` is provably thread-safe; the concurrency risk is the *contract around it*, not the function

## Verdict

`_validity` (`dedup.py:120`) is a pure function of `(field, value)` with **zero shared mutable
state** — it touches no locks, no session, no I/O, and no rebound global. It is called from
exactly one place on exactly one thread, after every pool in the pipeline has already joined.
**It is currently safe, and safe by construction rather than by luck.** 1.44M concurrent calls
across 40 threads produced zero divergence.

The real exposure is two layers out: (a) that safety rests on an **undocumented, unenforced
single-caller invariant** (`dedup()` must run after all pools join, on records nobody else is
mutating) that the nearest planned refactor sits one line away from breaking; and (b) the three
scrapers run concurrently and emit **heterogeneous, unvalidated types** into the fields
`_validity` is supposed to judge — and of the 20 columns that reach it, it discriminates on only
five, returning a hardcoded `1` for the rest. Wrong data merges in silently and deterministically.

**No S1.** There is no race, no lock ordering problem, no dict torn across threads. The findings
below are S2 wrong-value merges and one S2 contract fragility.

---

## What I checked, against the five concurrency sub-questions

### 1. Shared mutable state — none

Everything `_validity` reads is write-once at import:

| Symbol | `file:line` | Mutable after import? |
|---|---|---|
| `_EMAIL_OK` | `dedup.py:116` | No — `re.Pattern`, compiled once |
| `_URL_OK` | `dedup.py:117` | No — `re.Pattern`, compiled once |
| `_is_missing` | `dedup.py:79` | No — pure, no closure, no globals |
| `float()`, `str()` | `dedup.py:125,127,130,136` | No — C builtins, no module state |

`dedup.py` contains **no** `threading`, `Lock`, `thread_local`, `global `, `nonlocal`,
`random`, `time.`, `requests`, `Session`, or `open(` (verified by token scan). The two compiled
patterns are the only objects shared across threads, and `re.Pattern` is documented-shareable:
CPython's `sre` matcher holds no mutable state and does not release the GIL mid-match, so two
threads calling `.match()` on the same pattern cannot interleave.

**Verified:**

```
=== 5. 40-thread determinism hammer ===
  40 threads x 4000 iterations x 9 (field, value) cases = 1,440,000 calls
  mismatches: []   count: 0
```

### 2. Session reuse — correct, and not `_validity`'s problem

`_validity` performs no I/O and never touches a `requests.Session`. For completeness, the
session story elsewhere in this codebase is actually right:

- `httpclient.get_session()` is backed by `threading.local()` (`httpclient.py:58`, `httpclient.py:61-71`),
  so the 40 workers in `enricher._fetch_website` (`enricher.py:175`) each get their own Session.
  This is the fix the module docstring at `httpclient.py:7-10` describes, and it is in place.
- `GooglePlacesScraper.fetch_query` builds a **per-query** session (`google_places.py:189`) rather
  than sharing `self`'s — correct, since `requests.Session` is not thread-safe.

### 3. Dict mutation visible across threads — the invariant holds, but nothing enforces it

Static call-site scan (AST walk of every `.py` outside `docs/`):

```
_validity  <- dedup.py:182        (_pick)      <- dedup.py:193  (_merge)
_merge     <- dedup.py:208, 216   (dedup)
dedup     <- main.py:180                       <- THE ONLY CALLER
```

`main.py:180` executes on the main thread, **after** the `with ThreadPoolExecutor(max_workers=3)`
block at `main.py:160-168` has exited — that block joins all three scrapers. The 40-thread pool is
*downstream*, at `main.py:187` → `enricher.py:337`. So the program order is:

```
pool(3) join @ main.py:168  ->  dedup @ main.py:180  ->  pool(40) @ enricher.py:237
```

That is a clean happens-before edge. There is no overlap.

`check_websites` also uses the correct pattern for the records it will eventually write: it
**snapshots** `r["website"]` into `targets` on the main thread (`enricher.py:231`) and submits
only that immutable `str`; workers return `(outcome, contacts)` and every dict write
(`enricher.py:250-259`) happens on the main thread inside `as_completed`. Workers never read or
write a record dict.

`dedup()` is additionally **pure with respect to its input** — `_merge` (`dedup.py:190-194`)
builds a fresh dict and `_pick` only ever calls `.get()`. Verified at 2,000-record scale:

```
=== N. dedup() does not mutate its inputs even at scale (purity invariant) ===
  inputs unchanged: True   output len: 2000
```

### 4. Resource lifetime — nothing acquired, nothing leaked

`_validity` holds no lock, no file handle, no session, no cursor, and allocates only a tuple
return value. It is also **not a scaling concern**, which matters because it argues *against*
the obvious "parallelise dedup" fix: at 2.32 µs/call, a 200,000-row cumulative master costs
roughly **20 seconds** serial on the merge hot path (~22 `_pick` × 2 values × N), against a
40-thread HTTP stage that runs for hours.

### 5. Reentrancy — trivially satisfied

No recursion, no state, no memoisation. Even the degenerate self-merge is clean:

```
=== 4. same dict object as both a and b ===
  _merge(z, z) = {...}   z still: {'name':'A','website':'https://a.example', ...}  (unchanged)
```

---

## Findings

### S2 — `_validity` judges 5 of the 20 columns that reach it; for the other 15 it returns a constant, so `str(value)` decides the merge

- **Where:** `dedup.py:140` (`return 1`), consumed at `dedup.py:184` (`str(value)` tiebreak)
- **Breaks:** of the 22 exported columns, `source` and `scraped_at` are special-cased at
  `dedup.py:173-177` and never reach `_validity`. Of the remaining **20**, it has real branches
  for exactly **five** — `email`, `website`, `lat`, `lon`, `rating`. For the other **15**
  (`name`, `address`, `category`, `phone`, `country`, `region`, `facebook`, `instagram`,
  `whatsapp`, `linkedin`, `website_live`, `review_count`, `completeness_score`, `lead_score`,
  `industry_priority`, `recommended_service`) it returns the same `1` for everything, so those
  fields never get a validity judgement at all. Combined with `_VOLATILE` not containing any of
  them (`dedup.py:114`), the `rank` tuple collapses to `(trust, 1, "", str(value))` and
  `_pick`'s `>=` at `dedup.py:187` means **the lexicographically largest string wins**.

  That is a bias toward junk that starts with a high-ASCII letter — which is exactly what OSM
  placeholder tags look like, since `osm.py:94` copies `contact:facebook` / `instagram`
  **raw and unnormalised**.

- **Trigger:** two OSM observations of the same business (both `source="osm"`, so `trust` ties
  at 3):

  ```python
  A = {"name":"Cafe Beirut","facebook":"BeirutFitness","instagram":"beirut.fit",
       "source":"osm","scraped_at":"2026-01-01T00:00:00+00:00"}
  B = {"name":"Cafe Beirut","facebook":"unknown","instagram":"N/A",
       "source":"osm","scraped_at":"2026-01-02T00:00:00+00:00"}
  _merge(A, B) -> facebook='unknown'      # junk wins
  _merge(B, A) -> facebook='unknown'      # deterministically, both orders
  ```

  `"unknown" > "BeirutFitness"` because `'u'` (0x75) > `'B'` (0x42).

- **Why it matters downstream:** `pitch_recommender.py:39` does
  `has_facebook = bool(record.get("facebook"))`. `"unknown"` is truthy, so the record is
  classified as having a social audience and takes Tier 2/3 (`pitch_recommender.py:62-66`)
  instead of Tier 6 `pitch_recommender.py:90-91` — the recommended pitch in
  `sales_ready.csv` is wrong, and `has_any_contact` at `main.py:144` also counts it.
  Note this is *not* order-dependent, so `test_merge_is_order_independent`
  (`tests/test_lead_signal.py:292`) will never catch it.

- **Fix:** give `_validity` real branches for the string fields that need them (`name`,
  `address`, `phone`, the social handles) — at minimum reject placeholder tokens
  (`unknown`, `n/a`, `null`, `-`, `0`, `test`) and require a shape match for handles.

---

### S2 — `_validity` can never veto a value from a higher-trust source: trust is element 0 of `rank`

- **Where:** `dedup.py:181` (`_trust_for`) evaluated before `dedup.py:182` (`_validity`); plus
  the `>=` at `dedup.py:187`
- **Breaks:** `_validity` is a *tiebreaker*, not a *validator*, despite the docstring at
  `dedup.py:121` ("whether a value is merely present or actually plausible"). A value that
  `_validity` scores `0` still wins whenever its source outranks the other source for that
  field. The trust table at `dedup.py:97-110` makes this reachable for real fields:

  | field | google | osm | wikidata | effect |
  |---|---|---|---|---|
  | `website` | 2 | **3** | 2 | a junk-but-plausible OSM URL beats a valid Google URL |
  | `lat`/`lon` | **3** | 1 | 1 | a broken Google coordinate beats a real OSM one |
  | `rating` | **3** | 1 | 1 | a non-float-ish Google rating beats a real OSM one |

- **Trigger:** a `google_places` record whose `location.latitude` came back as `1e400`
  (→ `inf`, `google_places.py:273-274`) merging with an OSM record carrying a real `35.5018`:

  ```
  === H. inf lat from master CSV: does it win a google_places lat (trust 3)? ===
    merged: inf inf     # Google wins on trust, before _validity is consulted
  ```

- **Note on blast radius:** for `lat`/`lon`/`rating` the OSM-vs-Wikidata pair both sit at trust
  `1` (neither table has a `lat` key), so *within that cohort `_validity` is the only
  differentiator* — which is precisely where its `lat`/`lon` gap below bites.
- **Fix:** make a `_validity` score of `0` a hard veto ahead of trust — e.g.
  `rank = (0 if _validity(field, value) == 0 else 1, _trust_for(...), _validity(...), ...)`.

---

### S2 — `lat`/`lon` get a full 3/3 bonus for values that cannot be coordinates; the `rating` branch proves the check is expected

- **Where:** `dedup.py:128-133` versus `dedup.py:134-139`
- **Breaks:** the `rating` branch does `float(value)` **and** range-checks `0 <= v <= 5`. The
  `lat`/`lon` branch does `float(value)` and nothing else, so it cannot tell a coordinate from
  garbage. Measured:

  ```
  _validity('lat', nan)   = 3      # identical to a real coordinate
  _validity('lat', inf)   = 3
  _validity('lat', -inf)  = 3
  _validity('lat', 999)   = 3
  _validity('lat', True)  = 3      # bool is a valid float in Python
  _validity('lat', 'abc') = 0
  _validity('rating', nan) = 0     # range check catches it -- lat/lon do not
  ```

  When trust ties (OSM vs OSM, or OSM vs Wikidata — see the table above), the decision falls to
  `str(value)` at `dedup.py:184`, and `"nan" > "33.8931"`.

- **Trigger:** two OSM observations of the same pharmacy:

  ```python
  E1 = {"lat":33.8931,"lon":35.5018,"source":"osm","scraped_at":"2026-01-01T..."}
  E2 = {"lat":float("nan"),"lon":float("nan"),"source":"osm","scraped_at":"2026-01-02T..."}
  _merge(E1, E2) -> lat=nan, lon=nan     # NaN won
  ```

  Then `enricher.infer_region` (`enricher.py:89-93`) evaluates `33.8931 <= nan <= 34.720`,
  which is `False`, so it returns `None` — a business whose address reads "Beirut" is filed
  under an unknown region purely because a coordinate won a string comparison.

  **Concrete ingestion paths into `_validity`:**
  1. `main.py:93` — `float(row["lat"])` accepts `nan`, `NaN`, `inf`, `-inf`, and `1e400`
     from any cell of the cumulative `all_businesses.csv`. Verified round-trip:
     `float('1e400') = inf -> _validity('lat', ...) = 3`. Because the master is cumulative and
     re-read every run (`main.py:150`), a single poisoned cell is permanent.
  2. `osm.py:68` — `el.get("lat") or (el.get("center") or {}).get("lat")`. The `or` idiom
     only collapses its **left** operand, so an OSM **way** (which has `center` and no `lat`)
     with `center.lat == 0.0` yields `lat = 0.0` — Null Island survives, verified:
     `osm.py:68 expression yields lat = 0.0; _validity('lat', lat) = 3`.
  3. `google_places.py:273-274` — `location.get("latitude")` is stored without any bound check.
- **Fix:** give `lat`/`lon` the range check `rating` has, and make it country-aware so a
  `lat=9.5, lon=35.5` (mid-Atlantic) cannot outrank Beirut — see below.

---

### S2 — `lat`/`lon` are picked independently and tie-broken by string ordering, so coordinates are spliced and the worse one wins

- **Where:** `dedup.py:190-194` (per-key loop, no lat/lon coupling), `dedup.py:184`
  (`str(value)`), `dedup.py:114` (`_VOLATILE` omits `lat`/`lon`)
- **Breaks:** two separate defects from the same line.
  1. **Spliced pairs.** `_merge` chooses `lat` and `lon` independently, so the merged coordinate
     can be a pair *no source ever reported*. Verified:

     ```
     === lat and lon are picked INDEPENDENTLY -> spliced coordinate pair ===
       "34.05" > "21.30"  -> True   (C wins lat)
       "35.62" > "35.48"  -> True   (D wins lon)

       C = (34.05, 35.48)  region: Mount Lebanon
       D = (21.30, 35.62)  region: None
       merged = (34.05, 35.62)   # lat from C, lon from D
       lat from C? True | lon from D? True
       reverse _merge(D,C) = (34.05, 35.62)   # deterministic
     ```

     The merged pair `(34.05, 35.62)` was reported by neither observation, yet `region` is then
     inferred from it via `enricher._LB_COORD_REGIONS` (`enricher.py:58-68`, `enricher.py:89-93`)
     and written to the `region` column. Note the splice is deterministic — both argument
     orders agree — so it is a standing data-quality defect, not a race.
  2. **Geographically wrong winner.** Because the tiebreak is a lexicographic string compare
     with the **largest** winning, any latitude whose first character exceeds `'3'` beats
     Beirut's `33.x` regardless of distance:

     ```
     === D. lat/lon tiebreak is str(value) -> lexicographic ===
       A lat=33.8547 (Beirut)  B lat=9.5 (mid-Atlantic)
       _merge(A,B) -> lat 9.5        _merge(B,A) -> lat 9.5     # deterministic and wrong
       "9.5" > "33.8547"  ->  True
     ```
- **Trigger:** any two same-trust OSM observations of one shop whose coordinates disagree
  (GPS drift, a node and a `way` centroid for the same building — Overpass returns both).
- **Fix:** treat `lat`/`lon` as one atomic pair; when they disagree, pick by distance from the
  other candidate, and reject any pair failing `lat ∈ [-90, 90]`, `lon ∈ [-180, 180]`, or the
  record's country bounding box.

---

### S2 — The single-threaded contract on `dedup()` is real, undocumented, and one line from breaking

- **Where:** `main.py:180` (sole caller) · `main.py:160-168` (the join that makes it safe) ·
  `enricher.py:337` (the pool that runs after) · `dedup.py:197` (`dedup()` has **no docstring
  at all**)
- **Breaks:** `_validity` is immune by construction, but *what it judges* is not. `dedup()`
  reads live record dicts via `.get()`, and `check_websites` writes to those same dicts
  (`enricher.py:250-259`). Today the two stages are strictly ordered, so there is no overlap.
  The failure mode of any reorder is not a crash — it is a **silently timing-dependent merge
  outcome**, which is the worst class of bug here because `test_merge_is_order_independent`
  (`tests/test_lead_signal.py:292`) is a unit test and would keep passing.

  Concretely, moving `enrich()` before `dedup()` — which is tempting, since you would want to
  check websites *before* choosing the canonical website URL — would let `enricher.py:250`
  (`r["website_live"] = ...`) interleave with `dedup.py:208`
  (`phone_index[key] = _merge(...)`). `website_live` is in `_VOLATILE` (`dedup.py:114`), so it
  is exactly the field whose `rank` depends on a comparison (`dedup.py:183`) that a racing
  write can flip between `True` and `None`.

  The same class of risk reappears under the planned CLI stage split
  (`docs/audits/037-cli-design.md`, `leadminer scrape` / `dedup` / `enrich` as separate
  commands): as soon as those stages can overlap, or dedup is sharded across workers, the
  invariant this report certifies silently evaporates.
- **Trigger:** `main.py:186-187` reordered to `records = enrich(raw_filtered + master)` before
  `main.py:180`, plus a `check_websites` run long enough for the pools to overlap.
- **Fix:** give `dedup()` a docstring stating the contract ("single-threaded; call after all
  pools have joined, on records no worker will mutate") — and, since `dedup()` is already pure
  w.r.t. its input, keep it that way: never write through the dicts handed to it.

---

### S3 — The `-100` sentinel is unreachable from its only caller

- **Where:** `dedup.py:122-123`, short-circuited by `dedup.py:168-171`
- **Breaks:** `_pick` returns early for both missing cases *before* `rank` is ever constructed:
  `if _is_missing(av): return bv` / `if _is_missing(bv): return av`. Verified:

  ```
  _pick('x', {"x": None}, {"x": None}) -> None        # never reaches _validity
  _pick('x2', {"x2": "  "}, {"x2": "v"}) -> 'v'       # never reaches _validity
  ```

  So `_validity`'s most emphatic possible output can never influence a merge. That is
  harmless today, but it misleads: a reader reasonably concludes missing values participate in
  ranking and can be out-voted, which is not how the code behaves.
- **Trigger:** n/a — no input reaches it.
- **Fix:** delete the sentinel and document that `_pick` owns the missing-value decision, or
  move the branch out of `_validity` into `_pick` where it is reachable.

---

### S3 — The `str(value)` tiebreak is not type-normalised, so a float and its string form tie on all four elements

- **Where:** `dedup.py:184`, interacting with `>=` at `dedup.py:187`
- **Breaks:** `str(33.85) == str("33.85") == "33.85"`, and `_validity` scores both `3`, so
  every element of the `rank` tuple is equal and the winner is decided purely by argument
  order — reintroducing exactly the order-dependence `_pick`'s own docstring
  (`dedup.py:161-165`) says it removed:

  ```
  === I. float vs str with identical text: ALL FOUR rank elements tie ===
    _merge(a,b)['lat'] = 33.85    type: float
    _merge(b,a)['lat'] = '33.85'  type: str     # flipped on argument order alone
  ```
  (`rating` is spared only because it is in `_VOLATILE`, so recency breaks the tie —
  `lat`/`lon` are not: `'lat' in _VOLATILE -> False`.)
- **Trigger:** a `lat` arriving as `str` rather than `float`.
- **Not currently reachable:** all three producers emit floats — `osm.py:68-69` (Overpass
  JSON), `google_places.py:273-274` (Places JSON), `wikidata.py:60-61` (`_parse_point` returns
  `float`), and `main.py:93` casts master cells or nulls them on failure. So this is latent,
  and I am reporting it as such rather than as a live bug.
- **Fix:** normalise the tiebreak to the parsed value (`float(value)` for `lat`/`lon`/`rating`)
  or add a canonical-form comparison, so type never becomes the deciding signal.

---

## Not a bug, but worth knowing

- **`_validity` is the safest function in the module.** Pure, reentrant, stateless, no I/O,
  no locks. If you ever do need it from a worker, it will behave. Nothing about it needs to
  change for concurrency reasons.
- **The session story is already fixed and correct.** `threading.local()` at `httpclient.py:58`
  backs all 40 `enricher` workers; `google_places.py:189` builds a per-query session. Both
  avoid the classic shared-`requests.Session` corruption.
- **`check_websites` uses the right worker pattern.** Workers compute and return; the main
  thread is the only writer (`enricher.py:231` snapshot, `enricher.py:250-259` writes). The
  same pattern in `main.py:162-168` (`raw.extend(batch)` on the consuming thread) is correct
  because only one thread drains `as_completed`.
- **`GooglePlacesScraper` correctly guards its two shared collections**: `seen_ids` under
  `seen_lock` (`google_places.py:182`, `263-266`) and `all_records` under `records_lock`
  (`google_places.py:184`, `191-192`), with `yield from` after the pool joins
  (`google_places.py:198-199`).
- **Do not parallelise `dedup()` for speed.** 2.32 µs per `_validity` call → ~20 s serial for a
  200k-row master, against hours of HTTP. The hazard here is correctness, not throughput.
- **One real resource leak nearby (outside `_validity`'s scope):** `google_places.py:189`
  creates a `requests.Session` per query and never closes it — 68 unclosed Sessions, each with
  its own urllib3 connection pool, over a run.
- **`_VOLATILE` (`dedup.py:114`) omits `lat`, `lon`, `phone`, `email`, and every social
  handle**, so for those fields the recency tiebreak at `dedup.py:183` is hardcoded to `""`.
  That is a deliberate-looking choice with no comment; it is the root enabler of both
  coordinate findings above.
- **Two of the 22 columns never reach `_validity` at all** — `source` and `scraped_at` are
  special-cased at `dedup.py:173-177` before `rank` is built.

---

## Recommended order of work

1. **Range-check `lat`/`lon` in `_validity`** (`dedup.py:128-133`) and make `_validity == 0`
   a hard veto ahead of `_trust_for` (`dedup.py:181-182`). Two lines; stops NaN/inf/`999`
   from outranking real coordinates and stops junk from outranking valid values.
2. **Give `_validity` real branches for `name`, `address`, `phone`, `facebook`, `instagram`,
   `whatsapp`, `linkedin`** — reject placeholder tokens and require handle/phone shapes. Today
   15 of the 20 columns that reach it return a constant `1` and `str(value)` silently decides,
   which is how `facebook="unknown"` reaches `sales_ready.csv` and flips the recommended pitch.
3. **Make `lat`/`lon` atomic and distance-aware** in `_merge` (`dedup.py:190-194`), so a
   spliced or out-of-region coordinate pair can never be assembled.
4. **Document the single-threaded contract on `dedup()`** (`dedup.py:197`) and add a regression
   test that runs `dedup()` against records concurrently mutated by a thread pool, asserting
   the result is unchanged — cheap insurance for the CLI-stage-split refactor in
   `docs/audits/037-cli-design.md`.
5. **Delete the unreachable `-100` sentinel** (`dedup.py:122-123`) and normalise the `str(value)`
   tiebreak (`dedup.py:184`) against the parsed value so a future type change cannot make the
   merge order-dependent again.
6. Housekeeping, not blocking: close the per-query Sessions at `google_places.py:189`.