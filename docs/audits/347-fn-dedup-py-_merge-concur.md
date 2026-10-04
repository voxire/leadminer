# 347 — `_merge` (dedup.py:190) under the concur lens

## Verdict

**`_merge` is currently safe.** It runs on exactly one thread, mutates nothing it does
not own, holds no lock, opens no socket, and reads only module-level tables that nothing
in the repo ever writes. I verified by execution that 8 threads merging the same input
pair 500× each produce exactly one distinct result. There is no data race here and
there is nothing to fix for correctness *today*.

The real finding is one layer down: **`_merge` is commutative but not associative**, and
the order in which `dedup()` folds records into it is supplied by a nondeterministic
`as_completed` at `main.py:162-166`. Roughly **18% of three-source businesses get a
different merged row from one run to the next** with byte-identical input. That is a
determinism defect, not a race — and it becomes silent field loss the moment anyone
parallelises `dedup()`.

---

## Call-graph proof: `_merge` is single-threaded

This is the load-bearing claim, so here is the whole path.

| Step | Anchor | Threads |
|---|---|---|
| 3 scrapers submitted | `main.py:160-161` (`ThreadPoolExecutor(max_workers=len(scrapers))`) | 3 pool workers |
| pool **joined** — `with` block ends, `shutdown(wait=True)` | `main.py:169` (block) → `main.py:180` | back to main |
| `dedup()` called on the main thread | `main.py:180` | **main only** |
| `_merge` called | `dedup.py:208` (phone key), `dedup.py:216` (name key) | **main only** |

Supporting facts:

- `dedup._merge` has exactly two call sites, both inside `dedup()`: `dedup.py:208`,
  `dedup.py:216`. Verified by grep across the repo.
- `dedup()` has exactly one production caller: `main.py:180`, imported at `main.py:29`.
  `cli.py` and `scripts/` contain zero references to `dedup` / `_merge` (verified).
  The only other importer is `tests/test_lead_signal.py:65`.
- `GooglePlacesScraper.scrape` is a **generator**, but its own 5-worker pool
  (`google_places.py:194`) is fully drained by the `as_completed` loop at
  `google_places.py:196-197` *before* the first `yield` at `google_places.py:199`. So no
  record escapes that scraper while its workers are live, and `main.py:158`'s
  `list(scraper.scrape())` is not what makes this safe — the internal drain is.
- `enricher.check_websites` (40 workers, `enricher.py:237`) runs at `enricher.py:337`,
  which is `main.py:187` — **after** `dedup()` at `main.py:180`. The 40 workers never
  see a `_merge` output dict.

## Lens-by-lens

**Shared mutable state.** `_merge` reads `_SOURCE_TRUST` (`dedup.py:97`),
`_DEFAULT_TRUST` (`dedup.py:111`), `_VOLATILE` (`dedup.py:114`), and the compiled
patterns `_EMAIL_OK` / `_URL_OK` (`dedup.py:116-117`), all via `_pick`
(`dedup.py:159-187`). Grep for any write to those names outside `dedup.py` returns
nothing — they are effectively `const`. `re.Pattern.match` is documented thread-safe.

**Session reuse.** None. `dedup.py` imports only `re` and `unicodedata`
(`dedup.py:1-2`) — no `requests`, no `httpclient`. It cannot collide with the
thread-local session at `httpclient.py:58-71`, with `GooglePlacesScraper`'s per-thread
`requests.Session` (`google_places.py:159-166`, `google_places.py:189`), or with the
`check_websites` workers.

**Dict mutation visible across threads.** `_merge` writes only into the dict it
allocates itself at `dedup.py:191`; it reads `a` and `b` and never writes them
(`dedup.py:192-193`). Verified:

```
2000 _merge calls on 40 threads == serial output: True
8 threads x 500 merging the SAME pair -> distinct results: 1
```

This is the same discipline `check_websites` uses — workers receive only a URL string
(`enricher.py:238`) and return a tuple; the record dict is mutated solely by the main
thread (`enricher.py:249-259`). `_merge` follows that template correctly.

**Resource lifetime.** `_merge` acquires nothing. The two locks in the codebase
(`google_places.py:182`, `google_places.py:184`) are function-local to `scrape()` and
are dead by the time `main.py:180` runs.

**Reentrancy.** Call depth is bounded at 4 with no recursion and no callback:
`_merge` → `_pick` → (`_trust_for` → `_sources_of`) / `_validity` / `_recency`. The
`rank` closure at `dedup.py:179-185` is per-call and captures only `field`, `a`, `b`.
No shared accumulator, no re-entry hazard.

---

## Findings

### S2 — `_merge` is not associative, and its fold order is nondeterministic

- **Where:** `dedup.py:190-194` (the fold), with the root cause at `dedup.py:148-152`
  (`_trust_for`) and `dedup.py:173-174` (the `source` union). The nondeterminism is
  injected at `main.py:162-166`.
- **Breaks:** `rank()` (`dedup.py:180-185`) is lexicographic with `_trust_for` **first**
  (`dedup.py:181`). But `_trust_for` reads the record's `source` string, and `_pick`
  sets `source` to the **union** of both records' sources (`dedup.py:173-174`). So a
  value that wins an early merge inherits the maximum trust of *every source folded in
  so far* — including sources that contributed nothing to that field. Whichever
  conflicting value is folded **first** collects that inflated trust and then outranks
  every later candidate. Because trust dominates validity, `_merge` is not a pure
  associative fold.
- **Trigger:** fold order is nondeterministic because `main.py:162-166` extends `raw`
  inside `as_completed`, so batch order = scraper *completion* order. Overpass is one
  200 s POST (`osm.py:39-46`), Wikidata is one SPARQL query (`wikidata.py:70-78`),
  Places is 68 queries with 2 s inter-page sleeps (`google_places.py:174`,
  `google_places.py:310`) — completion order varies with network timing. Worse, the
  Google batch's *internal* order is itself nondeterministic: `google_places.py:191-192`
  appends per-worker results in 5-worker completion order.

  Measured over 40,000 randomized realistic 3-source record sets (one row per scraper,
  master row last as `main.py:179` forces), permuting only the three fresh batches:
  **18.3% of folds produce different output** depending on order. Fields that flipped:
  `category`, `website`, `lat`, `lon`, `rating`, `email`. The Google batch alone also
  has internal order nondeterminism, so Google-vs-Google phone collisions (shared
  franchise hotlines) flip too.

- **Concrete input** (paste into a REPL; no network):
  ```python
  from dedup import _merge
  G = {"source":"google_places","scraped_at":"2026-10-03T10:00:00+00:00","name":"Cafe A",
       "category":None,"phone":"+96170123456","website":"https://cafe-a.example","rating":4.5}
  O = {"source":"osm","scraped_at":"2026-10-03T10:00:00+00:00","name":"Cafe A",
       "category":"cafe","address":"12 Rue X, Beirut","email":"info@cafe-a.example"}
  W = {"source":"wikidata","scraped_at":"2026-09-01T10:00:00+00:00","name":None,
       "category":None,"lat":33.87,"lon":35.52,"website":"cafe-a.example"}
  # fold order decides which conflicting value survives:
  _merge(_merge(G, O), W)   # category -> 'cafe',  website -> 'https://cafe-a.example'
  _merge(_merge(G, W), O)   # category -> 'cafe',  website -> 'https://cafe-a.example'
  _merge(_merge(O, W), G)   # category -> 'cafe',  website -> 'https://cafe-a.example'
  _merge(_merge(O, G), W)   # category -> 'restaurant'  <-- flipped
  _merge(_merge(W, G), O)   # category -> 'restaurant'  <-- flipped
  _merge(_merge(W, O), G)   # category -> 'restaurant'  <-- flipped
  ```
  Read the mechanism off that output: in the last three orders the `restaurant` value
  is folded in while the accumulator already carries Google's `source`, so the
  accumulator's `category` trust is `max(google=3, osm=2, wikidata=1) = 3`, and
  `3 > 2` beats the later-folder OSM `restaurant`. In the first three orders `cafe`
  occupies the accumulator and inherits the same inflated trust 3.

  I searched hard for a case where this nondeterminism selects a *lower-`_validity`*
  value (junk `info@` over a real `info@cafe-a.example`, `lat` outside the plausible
  band) under the real pipeline order with the master row last — **0 hits in 40,000
  trials.** The flips are between two equally-valid candidates. That is why this is S2
  and not S1.
- **Fix:** stop making trust a function of the accumulated `source`. Compute trust from
  the source that *originated the value*, e.g. carry `(value, source)` pairs through the
  fold, or make `source` union happen only *after* all per-field picks. Alternatively
  make the fold order deterministic — sort `combined` by a stable key before
  `dedup()` (`main.py:179`) — which fixes the symptom but leaves `_merge` non-associative
  and unsafe to parallelise. I recommend the first; it is the one that also makes a
  future parallel `dedup()` correct.

### S3 — `dedup()`'s accumulator is an unprotected read-modify-write

- **Where:** `dedup.py:207-208` and `dedup.py:215-216`.
- **Breaks:** `if key in phone_index: phone_index[key] = _merge(phone_index[key],
  record)` is a read → compute → write with no lock. Two threads resolving the same key
  both read the same accumulator and both write; the loser's merge is discarded
  **silently**, with no error and no log. Because `_merge` is not associative (above),
  a parallel `dedup()` is also not reproducible even when it does not lose updates.
- **Trigger:** 8 records sharing `phone="+96170000001"`, each carrying a unique field,
  through a copy of the `dedup.py:201-210` loop in an 8-thread pool with a realistic
  2 ms critical section:
  ```
  serial dedup() keeps 8/8 contributions
  8-thread dedup() contribution counts over 20 runs: [2, 3]
  ```
  Three quarters of the merge work vanishes. Second pass also iterates the live index
  (`dedup.py:222` `list(phone_index.values())`, `dedup.py:226` `for record in
  name_index.values()`); a concurrent insert raises `RuntimeError: dictionary changed
  size during iteration` (CPython raises this deterministically — verified).
- **Fix:** if `dedup()` ever needs threading, do the key→group bucketing in parallel
  and the fold serially per key; the GIL buys nothing here anyway, since `_merge` is
  pure Python and `re`-bound.

### S3 — `_merge` output aliases caller-owned values

- **Where:** `dedup.py:193` combined with `_pick` returning `av`/`bv` at
  `dedup.py:168-171` and `dedup.py:187`.
- **Breaks:** `_merge` is a shallow copy. Every non-missing value in the result is a
  reference to an object owned by `a` or `b`. Today that is harmless because
  `BusinessRecord` (`base.py:5-28`) declares all 22 values as `str | float | int | bool |
  None`, and every producer obeys it (`google_places.py:281-305`,
  `osm.py:98-122`, `wikidata.py:106-129`, and `main.py:90-102` casts CSV cells). But
  the signature is `_merge(a: dict, b: dict)` (`dedup.py:190`) and `dedup(records:
  list[dict])` (`dedup.py:197`) — plain `dict`, no contract — and `main.py:104` feeds it
  raw `csv.DictReader` rows. One stray `list` or `set` in one field and every merged
  record, every `phone_index`/`name_index` entry, and the CSV writer all share one
  object; a later in-place edit then rewrites rows that were already "merged".
- **Fix:** type the parameters as `BusinessRecord` and either `copy.deepcopy` non-scalar
  values in `_merge` or assert scalar-ness at the `dedup()` boundary.

### S3 — A test asserts a property that gives false confidence

- **Where:** `tests/test_lead_signal.py:292-295` (`test_merge_is_order_independent`).
- **Breaks:** the test only checks `_merge(a, b) == _merge(b, a)` for one pair. That is
  true and it holds — I confirmed 0 commutativity failures across 50,000 randomized
  *pairwise* comparisons — but commutativity is exactly the property that does **not**
  imply the fold is order-independent. A reader (or a future refactorer) who takes this
  test as "merges are order-independent, so I can parallelise `dedup()`" is wrong, and
  the test will keep passing while they ship silent field loss.
- **Fix:** add `test_merge_fold_is_order_independent` that permutes a 4-record set over
  all 24 orders and asserts one output, *and* first fix `_trust_for` per the S2 finding
  so that test can pass.

---

## Not a bug, but worth knowing

- **`_pick` is commutative only modulo type.** `dedup.py:187` uses `>=`, so an exact tie
  returns `a`. With `rating=4.0` (float) vs `rating="4.0"` (str) from the same source,
  `_merge(a,b)["rating"]` is `4.0` and `_merge(b,a)["rating"]` is `'4.0'` — order-dependent
  *type*, identical rendered string, no CSV impact. `main.py:90-102` casts master cells,
  so this is unreachable today. It will become reachable the moment a JSON source can
  yield `4.0` on one run and `"4.0"` on the next.
- **The trust union is itself a data-quality bug, independent of ordering.** Because
  `_trust_for` reads the unioned `source` (`dedup.py:148-152`, `dedup.py:173-174`), a
  malformed value from a source the pipeline does *not* trust for that field can inherit
  the top trust and defeat `_validity` (which is only the *second* key,
  `dedup.py:182`). Concrete: OSM `contact:email="info@"` (`osm.py:90`) and Wikidata
  `P968="info@"` (`wikidata.py:98`) merge to `info@` at union trust 3, which then beats
  a stored `info@cafe-a.example` at trust 2 — because `_EMAIL_OK` (`dedup.py:116`) scores
  `info@` as 0. That is silent loss of a real contact address and belongs to a
  data-quality lens; grading it is out of scope here, but it is the same root cause as
  the S2 above.
- **`enricher.check_websites` is the correct template.** Workers get a URL
  (`enricher.py:238`); the record dict is touched only by the main thread inside
  `as_completed` (`enricher.py:249-259`). If `dedup()` is ever parallelised, copy this
  shape: pass an index, mutate the accumulator on the main thread.
- **Google Places can never supply an `email`.** `google_places.py:290` hardcodes
  `email=None`. Combined with `_SOURCE_TRUST` giving Google no `email` entry
  (`dedup.py:98-101`, so trust falls back to `_DEFAULT_TRUST = 1` at `dedup.py:151`),
  every merged email in this pipeline originates from OSM or Wikidata. Useful to know
  when reasoning about which collisions can actually flip.

---

## Recommended order of work

1. **S2** — make `_trust_for` depend on the value's originating source rather than the
   accumulator's unioned `source`. This is the only change that makes the merged output
   a function of the *set* of input records rather than their arrival order, and it is a
   prerequisite for any future parallelism of `dedup()`.
2. **S2 (cheap companion)** — sort `combined` at `main.py:179` by a stable key before
   `dedup()`. Do this even if you fix #1, as a belt-and-braces guarantee that two runs
   over the same input produce byte-identical `all_businesses.csv`.
3. **S3** — extend `tests/test_lead_signal.py:267` with a 24-permutation fold test
   alongside the existing pairwise test at `tests/test_lead_signal.py:292`.
4. **S3** — annotate `_merge` / `dedup` with `BusinessRecord` instead of bare `dict`
   (`dedup.py:190`, `dedup.py:197`), and add a one-line comment at `main.py:180`
   recording that `dedup()` is main-thread-only and why (both pools are joined at
   `main.py:169`).
5. **S3 (only if parallel `dedup()` is ever attempted)** — bucket keys in parallel,
   fold serially per key. Never wrap `dedup.py:201-218` in a `ThreadPoolExecutor`.