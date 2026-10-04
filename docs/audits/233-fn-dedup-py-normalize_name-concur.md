# 200 — Concurrency audit: `normalize_name` (`dedup.py:66`)

## Verdict

**`normalize_name` is safe under this codebase's concurrency. Nothing can corrupt
it, and I verified that four independent ways** (static call-graph census, runtime
thread instrumentation, a 160 000-call / 40-thread hammer, and the CPython 3.12
`unicodedata` source). It is a pure function over immutable module state, and it is
invoked from exactly one place — `dedup.py:214` — on the main thread, at a program
point where **all three thread pools have already been joined** (`threading.active_count() == 1`).

The real defect is one frame up, in its only caller. `dedup()` folds merged records
into an accumulator with a **left-leaning chain** (`dedup.py:208`, `dedup.py:216`), and
`_pick` resolves `scraped_at` as a bare `max(av, bv)` (`dedup.py:176-177`) with *no*
trust or validity ranking. So a record whose `website` was **rejected** on trust still
**donates its timestamp** to the accumulator, and that donated value is then used as
`_recency` to beat the genuinely correct winner (`dedup.py:183`). Whether the rejected
record lands in the accumulator *before or after* the true winner depends on input
order — and input order is **nondeterministic**, because `main.py:162` uses
`as_completed()`. Verified: **2 of 6 orderings of three records emit the wrong
`website`**; with a master row in the fold it is **4 of 6**.

This **refutes** the explicit "not a bug" conclusion in the sibling report
`docs/audits/200-fn-cli-py-main-concur.md:53-60`, which asserts field values cannot
flap. That claim is true for two records and false for three. Details and repro in S1.

---

## What I verified safe (do not "fix" any of this)

The lens named five hazards. All five are clean, for specific structural reasons.

**1. No shared mutable state in the function.** `dedup.py:66-69` reads only
`unicodedata` and `re`; it allocates every intermediate locally; it rebinds its
parameter `name` and never mutates the caller's string. The module-level containers in
`dedup` are `_COUNTRY_CODES` (`dedup.py:23`), `_SOURCE_TRUST` (`dedup.py:97`) and
`_VOLATILE` (`dedup.py:114`) — all read-only after import; nothing in the codebase
mutates them and there is no `global` statement in any project file. Verified at
runtime: input string unchanged, `unicodedata.unidata_version` unchanged,
deterministic across 3 calls, and **idempotent**
(`normalize_name(normalize_name(s)) == normalize_name(s)`).

**2. `unicodedata` has no mutable module state on 3.12.** This was the one plausible
C-level candidate (the function calls `unicodedata.normalize` and
`unicodedata.combining` on every character). Fetching CPython's
`Modules/unicodedata.c` at tag `3.12`: the string `cache` appears **0 times**, and all
43 `static` declarations are `const` tables, method tables or type specs. There is no
lazy global cache to race on. Empirically, 40 threads released from a
`threading.Barrier` onto the *very first* `normalize_name` call produced **0
exceptions**.

**3. `re`'s pattern cache is GIL-atomic.** `re.sub(r"\s+", ...)` at `dedup.py:69` uses
a string pattern, so `re._compile` consults the module cache. Under CPython's GIL the
dict lookup and store are atomic; the worst case is two threads compiling the same
pattern and one being discarded. Idempotent, so harmless. (Not relied upon anyway,
per #5.)

**4. Session reuse is correct, and `normalize_name` never touches one.** Sessions live
in `threading.local()` (`httpclient.py:58`, `get_session` at `61-71`), and
`GooglePlacesScraper` builds its own per-query session inside the worker
(`google_places.py:159-166`, `189`). Verified with 8 threads holding strong references
simultaneously: **8 distinct `Session` objects**, none shared. `normalize_name` opens
no socket and holds no session.

**5. `normalize_name` is never executed on a worker thread.** AST census over every
`*.py` outside `docs/` finds exactly **one** call site, `dedup.py:214`, inside `def
dedup`, not nested in any `ThreadPoolExecutor`/`submit`/`as_completed` construct. And
`dedup()` has exactly one call site, `main.py:180`, inside `def main`.

The ordering that guarantees this is worth writing down, because it is the whole
safety argument:

| Pool | Site | Workers | Joined before `dedup()`? |
|---|---|---|---|
| Scraper fan-out | `main.py:160` | 3 | **Yes** — `with` exits at `main.py:168`, `shutdown(wait=True)` |
| Google Places queries | `google_places.py:194` | 5 | **Yes** — `with` exits at `google_places.py:198`, before the final `yield from` at `:199` |
| Website liveness | `enricher.py:237` | 40 | **Runs after** — `enrich()` is called at `main.py:187`, i.e. *after* `dedup()` at `main.py:180` |

`OSMScraper.scrape` (`osm.py:31`) and `WikidataScraper.scrape` (`wikidata.py:66`) are
single-threaded generators with no pool at all. `main.py:158`'s
`list(scraper.scrape())` drains each generator fully, so `google_places.py`'s inner
pool is joined inside the future, before `as_completed` even yields it.

**Runtime confirmation** (real `main.main()`, `requests`/`urllib3` stubbed, `DATA_DIR`
redirected to a temp dir, `write_csv` no-op'd, nested 5-worker pool preserved):

```
--- enter dedup() ---
  thread        : MainThread  (is_main=True)
  active_count  : 1
  live threads  : ['MainThread']
normalize_name call count observed: 15
calls from a NON-main thread: 0
max active_count during normalize_name: 1
```

For contrast, the same instrumentation around `enrich()`:

```
max active_count during dedup()      : 1    (1 = no pool alive)
max active_count during enrich()     : 41   (40 workers + main)
normalize_name calls during enrich() : 0
```

**6. 40-thread stress.** 160 000 `normalize_name` calls across 40 threads over 8
inputs (Latin-with-accents, Arabic, Turkish dotted-I, German umlaut, ligature, mixed
case): **0 mismatches** against a serial reference, 0 exceptions.

**7. Under a free-threaded (PEP 703) build this stays safe**, because there is no
shared mutable state in the function to begin with. Worth stating explicitly since
the rest of this report is about order-sensitivity.

---

## Findings

### S1 — `dedup`'s chain fold is order-dependent: a *rejected* record donates its `scraped_at` to the accumulator, and `as_completed` makes the order nondeterministic

- **Where:** `dedup.py:176-177` (`scraped_at` resolved as bare `max`), `dedup.py:183`
  (`_recency` used inside `rank`), `dedup.py:187` (winner selection),
  `dedup.py:208` / `dedup.py:216` (the left-leaning fold),
  `dedup.py:173-174` (`source` union). Nondeterminism enters at `main.py:162-166`,
  flows through `main.py:172` and `main.py:179`, into `main.py:180`.
- **Breaks:** For a business observed by ≥3 records under one dedup key, the emitted
  `website` depends on the order the scrapers happened to finish in. The emitted
  website is not cosmetic — it cascades:
  - `main.py:199-200` decides `with_websites.csv` vs `without_websites.csv` membership,
    i.e. whether the row is a **"new-site pitch target"** at all;
  - `enricher.py:231` fetches **only the winning URL**, so `website_live`
    (`enricher.py:250`) is a verdict about a different URL on different runs;
  - `enricher.py:303-304` awards `+20` lead score when that URL answers 4xx/5xx;
  - `pitch_recommender` may then pitch *"Website rebuild + maintenance"*.

  So the liveness verdict, the lead score and the recommended service can all be
  computed about a different URL from one run to the next on byte-identical upstream
  data. Per the brief's severity table that is *"wrong results … blocks trust in the
  output"*, hence **S1**.

  The mechanism, precisely: `_pick` special-cases `scraped_at` at `dedup.py:176-177`
  and returns `max(av, bv)` **before** the `rank()` closure at `dedup.py:179-185` is
  ever consulted. So `scraped_at` ignores `_trust_for` and `_validity` entirely. Fold a
  wikidata record (website trust 2, `dedup.py:107`) into an osm record (website trust
  3, `dedup.py:103`) and the wikidata **website is correctly rejected** — but its
  newer `scraped_at` is **accepted**. The accumulator now reports a `scraped_at` that
  belongs to a record whose website was thrown away. On the *next* fold step that
  inflated date is read back by `_recency` (`dedup.py:183`) and used to beat the real
  freshest observation from a *trusted* source. Whether the contaminated accumulator
  exists yet depends on fold order.

- **Trigger** (3 records, one dedup key — no phone, so this is the `normalize_name`
  branch at `dedup.py:212-214`; no master row needed):

  ```python
  A = {"name": "Al Salam", "address": "1 St, Beirut", "phone": None,
       "website": "https://STALE.example", "source": "osm",
       "scraped_at": "2026-02-01T00:00:00+00:00"}      # older OSM observation
  B = dict(A, website="https://FRESH.example",
           scraped_at="2026-05-01T00:00:00+00:00")      # newer OSM -> CORRECT winner
  C = dict(A, website="https://WD.example", source="wikidata",
           scraped_at="2026-09-01T00:00:00+00:00")      # freshest date, site rejected
  ```

  Pairwise, `_pick` is commutative and always right — the sibling report's argument:

  ```
  _pick(website, A vs B) = 'https://FRESH.example'   reversed = 'https://FRESH.example'   same=True
  _pick(website, B vs C) = 'https://FRESH.example'   reversed = 'https://FRESH.example'   same=True
  _pick(website, A vs C) = 'https://STALE.example'   reversed = 'https://STALE.example'   same=True
  ```

  The chain fold disagrees with itself:

  ```
  OK  fold A then B then C -> website='https://FRESH.example'
  BAD fold A then C then B -> website='https://STALE.example'    <-- wrong
  OK  fold B then A then C -> website='https://FRESH.example'
  OK  fold B then C then A -> website='https://FRESH.example'
  BAD fold C then A then B -> website='https://STALE.example'    <-- wrong
  OK  fold C then B then A -> website='https://FRESH.example'
  wrong outcomes: 2 / 6
  ```

  The losing step, shown directly:

  ```
  _merge(A, C):
     website    = 'https://STALE.example'   <- C rejected on trust (osm 3 > wikidata 2)
     scraped_at = '2026-09-01T00:00:00+00:00' <- C's date ACCEPTED, bare max
  _pick('website', _merge(A, C), B) = 'https://STALE.example'   <- inflated recency wins
  ```

  And the same shape with a `master` row in the fold (`main.py:179`) reaches **4 of 6**
  wrong, so this is not a knife-edge.

  **Conditions required** (all three, which is why the row count never changes and the
  bug hides): (i) ≥3 records share one dedup key; (ii) two candidates tie on
  `_trust_for` and differ on `scraped_at`; (iii) a third record with a *newer*
  `scraped_at` whose value is *rejected*. Condition (i) is routine — the master CSV
  accumulates one merged row per business forever, and the 68 Google queries at
  `google_places.py:46-125` return different place ids for the same shop.

- **Note on scope:** this is **not** a defect in `normalize_name`. `normalize_name` is
  correct and is what decides which records land in one fold; the bug is in the fold's
  accumulator bookkeeping. It is in scope for this lens because `dedup.py:214` is the
  function's sole call site, and because `normalize_name`'s under-normalization
  (per `docs/audits/058-english-arabic-name-matching.md`) makes these groups *larger*,
  which makes the trigger more frequent.

- **Note on the sibling report:** `docs/audits/200-fn-cli-py-main-concur.md:53-60`
  states the opposite ("Field values do not flap"), resting on `_pick` commutativity
  and calling the merged record's `max(scraped_at)` "order-independent". It *is*
  order-independent as a *value*; the problem is that it is **order-dependent as a
  function of fold grouping**, because it is an input to the *next* comparison. That
  report's S3 "Output row order is completion-ordered" is separately correct and
  unaffected — content flaps here, not just row order.

- **Fix:** rank `scraped_at` like every other field, and stop letting a merged
  accumulator impersonate a single observation. Cheapest correct change, in `_pick`:

  ```python
  if field == "scraped_at":
      # Was: return max(av, bv) -- ignores trust, so a record whose *website*
      # was rejected still donates its date to the accumulator, and that
      # inflated date then wins the next _VOLATILE comparison via
      # _recency (dedup.py:183).
      return bv if (_trust_for("scraped_at", b), _validity("scraped_at", bv),
                    _recency(b), str(bv)) > \
             (_trust_for("scraped_at", a), _validity("scraped_at", av),
                    _recency(a), str(av)) else av
  ```

  Stronger and simpler: make the fold order-independent by construction — reduce the
  whole key group in one pass with `functools.reduce` over a **sorted** group, or
  accumulate per-field candidates in lists and pick the max rank at the end, so no
  intermediate merge is ever fed back in. Then add a regression test that asserts
  `dedup()`'s full output is invariant under permutation of its input (see S3).

### S3 — `normalize_name`'s purity and determinism are unpinned; the sibling test audit covers the gap but not the concurrency property

- **Where:** `dedup.py:66-69`; `tests/test_lead_signal.py:65` imports `_merge` and
  `normalize_phone` and omits `normalize_name`.
- **Breaks:** nothing at runtime — this is insurance. The entire safety result of this
  report rests on "pure function + single-threaded call site + single-threaded caller",
  and **no test asserts any of the three**. The one structural guard that would catch a
  future refactor that moves `dedup()` into a thread pool (see "Not a bug" #2 below) is
  `main.py:180` sitting between two `with ThreadPoolExecutor` blocks with no comment
  marking the boundary. A perf-minded change that shards `dedup()` across a pool would
  pass the whole suite.
- **Trigger:** add `ThreadPoolExecutor().map(dedup, chunks)` anywhere in `main()`;
  `python3 -m unittest discover -s tests` still reports green.
- **Fix:** add three cheap tests to `tests/test_lead_signal.py` — (1) purity/no-arg-mutation
  + idempotence for a mixed-script corpus; (2) a 40-thread hammer asserting
  `normalize_name` output equals a serial reference (I ran 160 000 calls, 0 mismatches,
  so this is not flaky); (3) `dedup()` output invariance under input permutation,
  which **fails today** per S1 and therefore doubles as that regression test.
  `docs/audits/200-fn-dedup-py-normalize_name-tests.md` already specifies the
  normalization properties in detail; it does not specify the concurrency ones, which
  is why this is listed separately rather than deferred to it.

---

## Not a bug, but worth knowing

- **`dedup()` itself would lose data if anyone naively sharded it.** `phone_index` and
  `name_index` are function locals (`dedup.py:198-199`), so there is no race *today*.
  Hoist them to module scope and feed a pool and `dedup.py:207-210` / `215-218` become a
  check-then-set race. I built it: row count survived, but **field values did not** —
  the racy `dict(a, **rec)` returns whichever source was written last, including
  overwriting a real value with `None`:

  ```
  correct  : source='google_places|osm' email='real@cafe.example' website='http://cafe-real.example' rating=4.9
  racy #1  : source='osm'                email='real@cafe.example' website='http://cafe-real.example' rating=None
  racy #2  : source='google_places'      email=None                website=None                          rating=4.9
  ```

  `_merge` (`dedup.py:190-194`) is commutative (`tests/test_lead_signal.py:292` proves the
  pairwise case); `dict(a, **b)` is not. If `dedup` ever needs to be parallel, shard by
  **process** into disjoint key partitions, never threads over a shared index.
- **Don't bother parallelising `dedup` for speed.** Measured single-threaded:
  10k → 0.04 s, 100k → 1.17 s, 400k → 5.89 s (~25 µs/record). At the workflow's
  5000-row Wikidata limit and realistic Places volumes this is noise next to the
  40-thread enrichment, and the GIL means `ThreadPoolExecutor` would not help anyway.
- **`_merge` non-associativity: not claimed.** I looked for a pair where
  `_merge(_merge(X,Y),Z) != _merge(X,_merge(Y,Z))` and did not find one. The S1
  mechanism is narrower and is demonstrated above: it is the accumulator's
  *membership* at each step, driven by `_pick`'s bare `max` on `scraped_at`. Don't
  action a "make `_merge` associative" fix on my authority — fix the `scraped_at`
  ranking.
- **`normalize_name` is idempotent** — verified across the mixed-script corpus. Worth
  pinning (`dedup` re-reads names from the master CSV every run, so a record's name is
  normalized at most once per run, but idempotence is what makes a future
  normalize-on-write change safe).
- **Sessions are never explicitly closed** — 40 + 3 + 68 `requests.Session` objects
  created, none `.close()`d. Thread-local storage dies with the worker thread at pool
  shutdown so CPython reclaims them, but it is implicit. Already noted in
  `200-fn-cli-py-main-concur.md`; flagged here only because my 40-thread session probe
  had to work around it (short-lived threads returned `id()` values that CPython then
  reused — a measurement trap worth knowing about before writing any test that asserts
  on object identity across threads).
- **`requests` is not installed in this checkout.** Every probe needed the same stub the
  repo's own harness uses (`tests/test_lead_signal.py:23-63`). Packaging, not
  concurrency — flagged only so the next agent does not read the stub as evidence that
  `requests` works.

---

## Recommended order of work

1. **Fix the `scraped_at` ranking in `_pick`** (`dedup.py:176-177`) — one edit,
   removes the S1 content flap. Verify with the 3-record trigger above: all 6
   permutations must emit `https://FRESH.example`.
2. **Add the permutation-invariance test for `dedup()`** (`tests/test_lead_signal.py`).
   It fails before step 1 and passes after, so it is both the regression test and the
   proof. Include the phone-keyed variant as well as the name-keyed one.
3. **Add the purity + 40-thread hammer tests for `normalize_name`**
   (`tests/test_lead_signal.py`) to pin the property this report verified.
4. **Comment the pool-join boundary at `main.py:169-180`** — one line stating that
   `dedup()` must run single-threaded, because its indices are unsynchronised locals.
   Cheap, and it is the only thing standing between the next agent and the
   `200-fn-cli-py-main-concur.md` claim being repeated as fact.
5. **Cross-reference, do not re-litigate:** `docs/audits/200-fn-cli-py-main-concur.md:53-60`
   needs a correction note pointing at S1; its row-order S3 stands. The
   session-per-thread and worker/main-division findings in that report are correct and
   independently confirmed here.