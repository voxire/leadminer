# 363 — `infer_region` is thread-safe; the concurrency risk is one level out

## Verdict

`infer_region` (`enricher.py:80`) is **currently safe** under all three concurrency
regimes in this codebase — the 40-thread pool in `check_websites`, the 5-worker pool in
`GooglePlacesScraper`, and the 3-scraper pool in `main`. It is a pure function of its
arguments plus four module tables that are frozen at import: it takes no lock, performs
no I/O, mutates no argument, writes no global, and is fully reentrant. I hammered it from
48 threads (24,000 calls across the LB/SA address and coordinate paths, box boundaries
and overlaps, Arabic and ASCII addresses) and got **0 errors, 0 divergences from the
serial baseline, and 0 mutation of any module global**. There is no corruption here.

The real findings are one level out: (1) the four tables it reads are plain unguarded
mutable lists shared by the 5 Google workers, and `REGION_KEYWORDS` is a live-but-already-dead
config name; (2) `check_websites` writes `records[idx]` with no lock, which is sound *only*
because `dedup` happens to return non-aliased dicts — an invariant nothing asserts; and
(3) a measured 4.65× regex cost from a redundant `re.IGNORECASE`.

> **Line-number note.** My assignment anchored the target at `enricher.py:79`. A concurrent
> agent added an import at `enricher.py:3` mid-audit, shifting `infer_region` to
> `enricher.py:80` (verified `md5 9e985b5d42ee001acbb73dc6d732ae81`). All anchors below use
> the current numbering. This is a live example of the hazard itself: `enricher.py` was
> modified by another writer while I was reading it.

## Findings

### S2 — The region tables are unguarded mutable globals shared by 5 workers, and `REGION_KEYWORDS` is live config that no longer does anything

- **Where:** `enricher.py:11-51` (`REGION_KEYWORDS`), `enricher.py:53-56` (`_REGION_MAP`),
  `enricher.py:59-69` (`_LB_COORD_REGIONS`), `enricher.py:71-77` (`_KSA_COORD_REGIONS`);
  concurrent reader at `scrapers/google_places.py:275` under `_WORKERS = 5`
  (`scrapers/google_places.py:142`, pool at `scrapers/google_places.py:194`).
- **Breaks:** Two distinct problems in the same four declarations.
  1. **They are plain lists, not tuples, and nothing marks them read-only.** A `grep` across
     the whole repo finds only 6 references to these names — one comprehension that builds
     `_REGION_MAP` at `enricher.py:54`, and three reads inside `infer_region`
     (`enricher.py:87`, `enricher.py:91`). So today there is **no writer**, and the 5 workers
     only read. That is why it is safe.
  2. But this is the *only* shared resource in the module with no hardening, and the codebase
     has deliberately hardened every other one: sessions became `threading.local()`
     (`httpclient.py:58-71`), and the Google scraper guards `seen_ids` with `seen_lock` and
     `all_records` with `records_lock` (`scrapers/google_places.py:182`, `:184`, `:191-192`).
     The four region tables got no equivalent treatment and no comment saying "must not be
     mutated." A future "let operators configure regions" change — the natural reason these
     names are module-public at all — reintroduces a writer. Because `enricher.py:87` iterates
     `_REGION_MAP` with an index-based `for` loop while 5 workers do the same, an in-place
     `sort()` or `append()` during a run gives different workers different views of the list,
     and `infer_region` returns a **different region for identical input depending on which
     worker observed the table mid-mutation** — a silent, output-affecting data race.
- **Trigger:** the "live but dead" half is a concrete, verified silent no-op:
  ```python
  >>> import enricher
  >>> len(enricher.REGION_KEYWORDS), len(enricher._REGION_MAP)      # (8, 8)
  >>> enricher.REGION_KEYWORDS.append(["Fake", ["zzz-unique-token"]])
  >>> len(enricher._REGION_MAP)                                       # still 8
  >>> enricher.infer_region("zzz-unique-token", None, None, country="LB")   # None — edit ignored
  ```
  `_REGION_MAP` is a snapshot taken once at import (`enricher.py:53-56`); `REGION_KEYWORDS` is
  never consulted again. The racy half needs only one added line — a `register_region()`
  helper or a config loader — and would then be a live S1.
- **Fix:** make the tables immutable and collapse the two sources of truth: build them as
  module-private tuples and expose a single `REGION_TABLE` (or delete `REGION_KEYWORDS` once
  `_REGION_MAP` is built). If runtime registration is wanted, it must rebuild `_REGION_MAP`
  and rebind the module name under a lock rather than mutating a list in place.

### S2 — `check_websites` writes `records[idx]` with no lock, and the only thing making that safe is an unasserted `dedup` aliasing invariant

- **Where:** `enricher.py:231` (snapshot), `enricher.py:238` (submit URL, not record),
  `enricher.py:249-259` (unguarded write-back); the invariant is established incidentally
  at `dedup.py:210`, `dedup.py:218` (`dict(record)`) and `dedup.py:191-194`
  (`_merge` allocates a fresh dict).
- **Breaks:** The design at `enricher.py:238` is right — workers receive only the URL string,
  never the record, so no worker touches a record dict. The write-back at
  `enricher.py:249-259` is deliberately lock-free and runs only on the main thread inside
  `as_completed`. That is sound **only if every element of `records` is a distinct dict
  object.** If any two indices name the same object, two futures resolve to one dict and the
  last `as_completed` writer silently wins, writing a `website_live` and contacts that belong
  to a different row — wrong data in the master CSV with no error.
  I verified the precondition currently holds: feeding `dedup` 6 inputs (3 collapsing on one
  phone, 1 same-name/different-phone, 2 phone-less) returned **4 rows with 4 distinct
  `id()`s — no aliasing.** But it holds for a fragile reason. `dedup` only copies because
  `dict(record)` at `dedup.py:210`/`:218` and `_merge` at `dedup.py:191` allocate new dicts,
  and those copies are **shallow**. The dict-identity guarantee survives only because
  `BusinessRecord` (`scrapers/base.py:5-28`) is entirely scalars. The Google scraper already
  parses a list — `types` at `scrapers/google_places.py:278` — so adding one container-valued
  field to a record reintroduces shared mutable *values* with no dict-identity check failing
  and no test failing.
- **Trigger:** `records = [d, d]` with `d = {"website": "http://x.lb", "website_live": None,
  "email": None, ...}`. `enricher.py:231` then yields `targets == [(0, 'http://x.lb'),
  (1, 'http://x.lb')]`; both futures write `d`, and whichever resolves last decides
  `website_live` and the contact fields for the single shared dict.
- **Fix:** assert the precondition at the top of `check_websites`
  (`assert len({id(r) for r in records}) == len(records)`), and make the invariant explicit
  in `BusinessRecord`'s docstring so the next person adding a list-valued field knows the
  shallow-copy contract. Deep-copying is unnecessary; documenting and asserting is enough.

### S3 — `re.IGNORECASE` on 8 alternation patterns costs 4.65× for zero semantic benefit

- **Where:** `enricher.py:53-56` (flag), `enricher.py:87` (`pattern.search(address)`).
- **Breaks:** All 143 shipped keywords are already lowercase — I checked every entry of
  `REGION_KEYWORDS` (`enricher.py:11-51`) and `k != k.lower()` is true for **none**. So the
  case-insensitive compile buys nothing, and costs real time on the two hot call sites
  (`enricher.py:332` and `scrapers/google_places.py:275`). Measured as an interleaved A/B,
  median of 7 rounds, single-threaded, on a 39-character LB address that matches no keyword
  (so all 8 patterns are scanned in full):
  ```
  shipped   re.IGNORECASE on raw   : 81.60 us   [97.5, 96.8, 101.0, 75.9, 80.8, 81.6, 80.7]
  proposed  lower() + case-sensitive: 17.56 us   [20.2, 21.4, 16.0, 17.6, 18.0, 16.7, 15.3]
  ratio 4.65x
  ```
  Extrapolating to a 500k-record serial region pass: **40.8 s → 8.8 s.** The
  `lower()`-then-case-sensitive form is **semantically identical**, verified over 19
  addresses including mixed-case ASCII (`HaMrA, BeIrUt`), all-lowercase, Arabic
  (`طرابلس`, `بعلبك`) and non-matching: **0 divergences**. Arabic has no case, so
  `str.lower()` leaves those literals untouched and exact matching still works.
  Honest severity: the practical saving is seconds, not minutes, and `enrich` skips records
  that already have a `region` (`enricher.py:331`) — which is every Google record. So this is
  a cheap win, not a scaling blocker. It is S3 on merit, not padded to S2.
- **Trigger:** `infer_region("Rue Mohammad Al-Amin, Building 12, Bldg B", None, None,
  country="LB")` — 81.6 µs today; the same lookup costs 17.6 µs with a single `.lower()`.
- **Fix:** compile the patterns case-sensitively at `enricher.py:54` and call
  `address.lower()` once at `enricher.py:86`. Add a regression test asserting
  `infer_region("HaMrA, BeIrUt", ...) == "Beirut"` so the equivalence cannot silently break.

### S3 — Nothing in the test suite pins any of these invariants

- **Where:** `tests/` (only `tests/test_lead_signal.py` exists); the single `infer_region`
  assertion is `tests/test_lead_signal.py:147-153`, which covers the address path only,
  single-threaded.
- **Breaks:** `grep -rln "Thread|concurren|threading" tests/` returns **nothing** — there is
  no concurrency test in the repository. So both S2 findings above are unguarded by
  anything executable. This matters more than usual here because this codebase has already
  shipped one shared-state concurrency bug and fixed it: the shared module-level
  `requests.Session` fired at 40 threads, documented retrospectively at
  `httpclient.py:7-11` and `enricher.py:124-127`. The fix was applied to sessions and to the
  Google scraper's two collections; the region tables and the record-aliasing invariant were
  left to inspection. Inspection is what this audit is.
- **Trigger:** any future refactor that makes `infer_region` memoize into a module dict, or
  adds a `register_region()` helper, passes the current suite.
- **Fix:** add one test that calls `infer_region` from 8 threads over a fixed corpus and
  asserts equality to the serial baseline, plus the `id()` assertion from the previous
  finding. Together they are ~20 lines and cover both S2s.

## Not a bug, but worth knowing

- **Why it is safe — the proof, not the conclusion.** `infer_region` (`enricher.py:80-95`)
  writes nothing. Its only reads are: its four parameters (`address`, `lat`, `lon`,
  `country` — all scalars or `str | None`), the loop variable, and four module-level names
  (`_REGION_MAP`, `_LB_COORD_REGIONS`, `_KSA_COORD_REGIONS`, bound to a *local* by the
  ternary at `enricher.py:91`). The ternary rebinds a local reference; it does not mutate the
  table. Return values are `str | None` — immutable. I snapshotted `id()`, length and
  element-identity of all four tables plus `len(re._cache)` before and after 48 threads ×
  500 calls: **all identical.** The 24,000 concurrent results matched a serial baseline on
  every one of the 190 corpus cases: **0 mismatches.**
- **`re._cache` never grows during concurrent use.** This closes the one real `re`
  thread-safety question. CPython's pattern cache in `re._cache` is the only mutable state
  in the `re` module and is the documented reason not to compile patterns from multiple
  threads. `infer_region` never compiles: all 8 patterns are built once at import
  (`enricher.py:53-56`), which runs single-threaded under the import lock. Measured
  `re._cache` 24 → 24 across the whole concurrent run, confirming no compile-time cache
  access. `re.Pattern.search` on a shared compiled pattern is safe.
- **Reentrancy: verified, depth 500.** `infer_region` takes no lock, so there is no
  reentrancy hazard and no lock-ordering risk. I called it recursively 500 deep from a single
  thread; it returned `"Beirut"` for `("Hamra, Beirut", 33.889, 35.503, "LB")` at every depth.
  It is also not *callback-driven* (no `__eq__`/`__hash__`/`__len__` on user types in the
  hot path), so no user code can run inside it and re-enter it.
- **No ReDoS / catastrophic backtracking risk.** All 8 patterns are flat alternations of
  `re.escape`d literals: `groups=0`, zero quantifiers (`has_quantifier=False` for all 8).
  Cost is linear in (alternatives × haystack length), measured at 9.7 / 16.5 / 40.7 / 64.8 /
  120.2 / 400.0 µs for 10 / 20 / 40 / 80 / 160 / 320 non-matching characters on the
  35-alternative Mount Lebanon pattern. A hostile long address cannot stall a worker beyond
  linear time. (This matters for concurrency: a ReDoS here would block whichever of the 5
  Google workers hit it, and the module already budgets 180 s per query at
  `scrapers/google_places.py:152`.)
- **The two call sites disagree about arguments, which looks like a race but is not.**
  `scrapers/google_places.py:275` calls `infer_region(None, lat, lon, country=country)` —
  address deliberately `None`, so Google records get a coordinate-only region. Then
  `enricher.py:331-335` refills any *falsey* region with an **address-aware** call. Net
  effect: a Google record whose coordinates missed the boxes gets a second chance from its
  address, but a Google record whose coordinates hit the **wrong** box (audit 007's overlap
  problem) receives a truthy wrong region that `enrich` will never repair. This is a
  correctness issue covered by audits 007/094 — not a concurrency one — but it is worth
  knowing that `region` in the output is a function of *which scraper produced the row*, and
  that reads exactly like the non-determinism a threading bug would produce. Reviewers should
  not re-investigate it as a race.
- **`enrich` never runs concurrently with anything.** `main.py:160-169`'s
  `with ThreadPoolExecutor(...)` calls `shutdown(wait=True)` on exit, so all three scrapers
  have fully joined before `enrich` is called at `main.py:187`. `check_websites`' pool
  likewise joins at `enricher.py:237` before the summary counts at `enricher.py:264-270`.
  So the only genuinely concurrent execution of `infer_region` is
  `scrapers/google_places.py:275` across 5 workers — and that path is safe per above.
- **40 thread-local Sessions are never explicitly closed.** Each of the 40 workers builds a
  `requests.Session` plus a urllib3 `PoolManager` on first `get_session()`
  (`enricher.py:176` → `httpclient.py:61-71`), and `check_websites` returns at
  `enricher.py:276` with no `close()` anywhere. Reclamation is implicit: the sessions die
  with the worker threads as their `threading.local` state is freed. That works today, but
  it is undocumented and unasserted — `httpclient.py:88-91` provides an
  `http_session()` context manager that `enricher.py` never uses. Corollary: `enrich` must
  never be called from inside a pool worker, or that worker would reuse whatever Session its
  thread already owns. Nothing does that today.
- **Unmeasured: whether `re` releases the GIL here.** I tried to demonstrate that
  `infer_region` cannot be parallelized on a GIL build, and **I could not measure it in this
  environment** — a GIL-free control (`hashlib.sha512` on 8 KB) and a pure-Python control
  both saturated at the same ~2× from N=1 to N=40, so the harness is capacity-limited here,
  not GIL-limited. Do not treat GIL serialization of this function as established. It is
  conventional knowledge about CPython `re`, not a finding I verified.
- **Dead leftover:** `enricher.py:327-328` disables `InsecureRequestWarning`, but
  `_fetch_website` uses `verify=True` (the default) per the comment at
  `enricher.py:170-174`, so nothing can raise that warning any more.

## Recommended order of work

1. **Assert and document the record-aliasing invariant** (S2, second finding) — one
   `len({id(r) for r in records}) == len(records)` assertion in `check_websites` plus a line
   in `BusinessRecord`. It is the only guard standing between a future refactor and silently
   wrong rows in the master CSV.
2. **Freeze the region tables** (S2, first finding) — make them tuples, collapse
   `REGION_KEYWORDS` into `_REGION_MAP`, and comment that they are shared read-only across
   the 5-worker pool. Do this *before* anyone adds a config-driven region.
3. **Add the two concurrency tests** (S3) — an 8-thread `infer_region` determinism test and
   the aliasing assertion — so the above cannot silently regress.
4. **Drop `re.IGNORECASE`, lowercase the haystack once** (S3) — a 4.65× measured win on the
   region pass, plus a mixed-case regression test.