# 200 — `_is_missing` (dedup.py:79) under this codebase's concurrency

**Target:** `dedup._is_missing` at `dedup.py:79`.
**Lens:** is it safe under the 40-thread pool in `check_websites`, the 5-worker
pool in `GooglePlacesScraper`, and the 3 scrapers in parallel in `main`.

## Verdict

`_is_missing` needs no lock and has none: it is a **pure leaf function**. Disassembly
shows it references **zero** module globals (`dedup.py:86-90` touch only `None`,
`isinstance` and `str.strip`), it holds no state, and it is reentrant. 800,000
concurrent calls from 40 threads returned identical answers with no exceptions.
It is also **unreachable from all three pools**: the only production call site is
`main.py:180`, which runs 11 lines *after* the scraper pool's `shutdown(wait=True)`
at `main.py:169` and 7 lines *before* the 40-thread pool starts at `enricher.py:237`.
**It is currently safe.**

The risk is one level up, and it is the thing an engineer will get wrong next.
`_is_missing` and `dict.get` are **non-atomic reads of a caller-owned record
dict**, and `dedup()` is handed exactly the dicts that `enricher.check_websites`
later writes into from 40 threads. The *only* thing keeping those two phases apart
is statement order in `main()` — there is no lock, no defensive copy, no assertion,
and no test. Demonstrated below: a torn read makes `_pick` return a URL that exists
in **no live dict**, and makes `_validity` score a value that was replaced before
the merge looked at it.

---

## Call graph and reachability (static, AST-verified)

Every call site of a `dedup`-module function in production code:

```
dedup.py:122   _validity._is_missing()
dedup.py:168   _pick._is_missing()
dedup.py:170   _pick._is_missing()
dedup.py:182   _pick.rank._validity()
dedup.py:193   _merge._pick()
dedup.py:205   dedup.normalize_phone()
dedup.py:208   dedup._merge()
dedup.py:216   dedup._merge()
dedup.py:228   dedup.normalize_phone()
main.py:180    main.dedup()          <-- the only production entry point
main.py:194    main.normalize_phone()
(+ tests/test_lead_signal.py:107-322, single-threaded)
```

Every thread-pool site in the repo, and the function each worker runs:

| Pool | Submitted at | Worker body | Reaches `_is_missing`? |
|---|---|---|---|
| 3 scrapers | `main.py:161` | `main.run` closure (`main.py:157-158`) → `scraper.scrape()` | No |
| 5 Places queries | `scrapers/google_places.py:195` | `fetch_query` closure (`:186-192`) → `_scrape_query` | No |
| 40 website fetches | `enricher.py:238` | `_fetch_website` (`:161-222`) | No |

`_fetch_website` touches only its `url` argument and returns a `(str, dict)` tuple
through the `Future`. It never sees `records`. The write-back is in the **caller's**
thread (`enricher.py:249-259`, inside `for future in as_completed(futures)`), so the
40 workers are strictly read-only w.r.t. the corpus. That is correct and it is the
reason nothing is corrupted today.

Ordering inside `main()`, verified by AST statement ranges:

```
main.py:160-168   with ThreadPoolExecutor(max_workers=3) as pool:   <- pool block
main.py:169       __exit__ -> shutdown(wait=True)  (all scraper threads joined)
main.py:180       records = dedup(combined)          <-- _is_missing runs HERE
main.py:187       records = enrich(records)
enricher.py:237   with ThreadPoolExecutor(max_workers=40) as pool: <- 40 workers start
enricher.py:263   __exit__ -> shutdown(wait=True)
```

So `dedup` is provably main-thread-only, and provably *not* concurrent with
`check_websites`. The phase separation is a property of statement order, not of any
mechanism.

---

## Findings

### S2 — `dedup()` reads dicts that `check_websites` mutates from 40 threads; only statement order separates them

- **Where:** `dedup.py:167` (`a.get(field)`) → `dedup.py:168` (`_is_missing(av)`),
  `dedup.py:170`, `dedup.py:192` (`set(a) | set(b)` snapshot), `dedup.py:122`
  (`_is_missing` inside `_validity`). Writers: `enricher.py:250` (`r["website_live"]`),
  `enricher.py:253`, `:255`, `:257`, `:259`.
- **Breaks:** three distinct corruptions, all silent, all landing in the CSVs:
  1. **Phantom resurrection.** `_pick` reads `av` at `:167`, the other thread sets
     `a["website"] = None`, then `_is_missing(av)` at `:168` still sees the old URL,
     `_is_missing(bv)` at `:170` is `True`, and `_pick` returns `av` — a website that
     is no longer in `a` and was never in `b`. The merged row resurrects a deleted URL.
  2. **Stale validity scoring.** `_validity` (`:120-140`) is reached from
     `rank` at `:182`. If the value changed between the `.get` and the `_validity`
     call, `rank(a, av) >= rank(b, bv)` at `:187` compares a **stale** validity
     against a live one and the wrong field wins the merge.
  3. **Field dropped from the merge.** `set(a) | set(b)` at `:192` is a one-shot
     snapshot of the record's *shape*. A key created after that snapshot —
     exactly what `enricher.py:257` (`r["whatsapp"] = ...`) does — is absent from
     `merged` entirely.
- **Trigger:** any pipeline shape that puts the two in the same phase. Three
  realistic ones, all of which other reports in `docs/audits/` propose:
  - moving `dedup` inside the pool (`main.py:180` → a submitted task);
  - reordering to dedup *before* enrich so the fetch set shrinks
    (`main.py:180` ↔ `:187`) — the ordering now silently becomes unsafe rather
    than safely reversed;
  - having workers write their own results (`_fetch_website` gaining an `idx`
    parameter and writing `records[idx]` directly), which is the natural
    "optimisation" for the write-back at `enricher.py:249-259`.
- **Evidence (deterministic, through the real `_pick`; the window at `:167`→`:168`
  already exists, the test only makes it observable by yielding the GIL inside `.get`):**

  ```python
  # C3 deterministic interleaving through the REAL functions
  rec = {"website": "https://real-cafe.example"}
  oth = {"website": ""}
  rec["website"] = None              # <-- another thread writes here
  out = dedup._pick("website", rec, oth)
  # rec now      : {'website': None}
  # _pick output : 'https://real-cafe.example'
  # phantom?     : True   <-- a URL in NO live dict
  ```

  ```
  C1 PHANTOM WEBSITE : _pick returned 'https://real-cafe.example' while
                       a['website'] is None and b['website'] is ''
                       -> a URL that exists in NO live dict
  C2 _validity('website', stale='not-a-url') -> 0
     (live value 'https://real-cafe.example' would score 3)
  C0 power: writer thread got 967,145 slices during 200,000 _pick calls
     -> switch rate 4.836 per call   (the test has power; see "Not a bug" #1)
  ```

- **Honest probability note:** with a pure-Python writer and
  `sys.setswitchinterval(1e-9)`, a natural hit rate of **0 in 500,000** — the window
  is ~6 bytecodes wide and the GIL rarely switches inside it. The race is real but
  needs a C-level block in the window to fire (a subclassed dict, a `__missing__`
  hook, a debug hook, or a future `dict` implementation with a different eval-breaker
  cadence). I am not claiming a live bug here; I am claiming there is **no guardrail
  between a real refactor and silent row corruption.**
- **Fix:** copy at the phase boundary and say so — `dedup([dict(r) for r in combined])`
  at `main.py:180`, plus a comment at `enricher.py:249` stating that the write-back
  must stay in the collecting thread. Then add the test: submit `_merge` to a
  `ThreadPoolExecutor` over records that another thread mutates, assert every merged
  value is present in one of the two inputs.

### S2 — dedup emits two rows for one business when only one observation has a phone, so the 40-thread pool fetches the same host twice (50% redundant GETs, measured)

- **Where:** `dedup.py:205` (phone key) vs `dedup.py:212-214` (name key);
  `dedup.py:226-230` (the name-keyed record is kept because its `phone` is falsy,
  so it is not in `captured_phones`); consumer at `enricher.py:231` → `:238`.
- **Breaks:** two rows for one physical business survive dedup, both carrying the
  same `website`. `check_websites` enqueues one `Future` per **row**, not per URL
  (`enricher.py:231`, `:238`), so a 40-thread pool puts **two or more simultaneous
  GETs at the same host**. For a small Lebanese host that is self-inflicted
  connection pressure; the loser of that race gets `UNKNOWN` via the bare
  `except Exception` at `enricher.py:176`, and because the contact extraction is
  gated on `outcome == LIVE` at `enricher.py:251-259`, that row **loses its
  email/instagram/whatsapp/linkedin too**. The duplicated row also double-counts in
  all five CSVs and in the region/service summaries at `main.py:228-242`.
  This is the normal case, not an edge case: `main.py:179` re-feeds the cumulative
  master (`main.py:150`) every run, so last run's phone-less row and this run's
  phone-bearing row coexist forever.
- **Trigger (concrete input, run against the real `dedup`):**

  ```python
  A = {"name": "Cafe", "phone": "+96170123456", "website": "https://cafe-real.example",
       "address": "Verdun, Beirut, Lebanon", "source": "osm",
       "scraped_at": "2026-01-01T00:00:00+00:00"}
  B = dict(A, phone=None, source="google_places")   # same business, phone blank
  dedup.dedup([A, B])
  ```

  ```
  dedup([A, B]) -> 2 records
      {'phone': '+96170123456', 'website': 'https://cafe-real.example', 'source': 'osm'}
      {'phone': None,          'website': 'https://cafe-real.example', 'source': 'google_places'}
  enricher.py:231 would enqueue 2 GETs to 1 distinct host
  ```

  At scale (2,000 businesses, each present from a current scrape *and* from the
  master with a blank phone):

  ```
  input records            : 4,000
  records after dedup      : 4,000   (=> 2,000 duplicate businesses survive)
  GETs enqueued            : 4,000
  distinct hosts           : 2,000
  redundant GETs           : 2,000  (50% of all fetches)
  hosts hit >1x concurrently: 2,000 (max 2 in flight to one host, from a 40-thread pool)
  ```
- **Fix:** dedup the fetch targets, not just the records —
  `urls = {u: [idx...]}` at `enricher.py:231`, one `Future` per distinct URL, then
  fan the single `(outcome, contacts)` back out to every `idx`. This is a strict
  improvement regardless of the dedup quality: even with perfect identity, the
  master re-feed guarantees some overlap. Pair it with the identity fix (a phone-less
  record whose `(name, city)` matches a phone-keyed record should be merged into it,
  not kept alongside it).

### S3 — `_is_missing` reports non-string empties as present, and four different "is this field present" predicates disagree across the thread boundary

- **Where:** `dedup.py:88-90`. `if isinstance(value, str): return not value.strip()`
  — anything that is not `None` and not a `str` returns `False` ("has data").
- **Breaks:** `_is_missing(b"")`, `_is_missing([])`, `_is_missing({})`,
  `_is_missing(float("nan"))` all return **`False`** — "this field carries
  information". Meanwhile `dedup.py:42` defensively does `raw = str(phone)`,
  signalling that the author already expects non-`str` to reach these functions.
  Not reachable from today's producers (`csv.DictReader` → `str`; `json.loads` →
  `str`/`float`), so this is hygiene, not a live bug.
- **Trigger:** `dedup._is_missing(b"")` → `False`; `dedup._is_missing("")` → `True`.
- **The part that matters for this lens:** the *same question* is asked four
  different ways, and the answers cross a thread boundary.
  - `dedup._is_missing` (`dedup.py:86-90`) — `None` or whitespace-only string
  - `enricher.completeness_score` (`enricher.py:103-116`) — plain truthiness
  - `enricher.check_websites` target filter (`enricher.py:231`) — plain truthiness
  - `main.has_any_contact` (`main.py:137-145`) — a hand-rolled digit/char-length rule

  So a `website` of `"   "` is **missing** to dedup (so `_pick` prefers the other
  source's URL) but **present** to `enricher.py:231`, which enqueues
  `session.get("   ")` at `enricher.py:175`; `requests` raises `MissingSchema`,
  the bare `except Exception` at `:176` swallows it, and the row gets
  `website_live = None` — permanently `UNKNOWN` — instead of the real URL that
  dedup had available. A whitespace-only field is thus *simultaneously* discarded as
  a value and fetched as a target.
- **Fix:** one `is_blank(value) -> bool` in a shared module, handling `None`, any
  `str`/bytes-like after strip, empty containers, and NaN; use it at all four sites.

### S3 — 40 thread-local `Session`s are never closed, holding up to 400 idle sockets to third-party hosts for the whole run

- **Where:** `httpclient.py:58` (`_local = threading.local()`),
  `httpclient.py:61-71` (`get_session`), `enricher.py:175`
  (`get_session().get(...)`), `enricher.py:237` (a fresh executor per call).
  There is no `close_session` anywhere in `httpclient.py`; the `http_session()`
  context manager at `httpclient.py:88-91` yields the shared thread-local and closes
  nothing.
- **Breaks:** one `check_websites()` call creates **40** sessions (measured), one
  per worker thread. Each owns an `HTTPAdapter` with
  `pool_connections=10, pool_maxsize=10` (verified against `requests==2.32.3`
  `src/requests/adapters.py`: `DEFAULT_POOLSIZE = 10`). `_fetch_website` calls
  `r.close()` at `enricher.py:195`, which returns the connection to the pool rather
  than dropping it, so up to **40 × 10 = 400 idle keep-alive TLS sockets to 400
  distinct small-business hosts** stay open for the duration of the run. No orderly
  TLS `close_notify` is ever sent. This is a politeness and resource-ceiling issue,
  not a leak — see the measurement below.
- **Trigger:** any run with more than ~400 websites. On a GitHub runner
  (`ulimit -n` typically 1024) a *second* HTTP stage in the same process — which
  `035-incremental-scraping`, `053-caching-layer` and `099-pipeline-stage-design`
  all propose — would put 400 idle + new sockets against that ceiling. `OSError:
  [Errno 24]` from `enricher.py:175` is swallowed by `except Exception` at `:176`
  and becomes `UNKNOWN`, i.e. silent loss of both `website_live` and every extracted
  contact.
- **Fix:** add `close_session()` to `httpclient.py` and call it from a
  `pool.submit`-teardown hook, or hold the session per *task* in a `with` rather
  than per thread.

---

## Not a bug, but worth knowing

- **1. The window really is narrow, and I could not hit it naturally.** With
  `sys.setswitchinterval(1e-9)` and a writer thread spinning on
  `record["website"] = <url> / None`, 500,000 `_pick` calls produced **0** torn
  reads — while the same harness gave the writer **4.836 GIL slices per `_pick`
  call**, so the test had ample power. `_pick`'s `:167`→`:168` gap is roughly six
  bytecodes. This is why I rate the S2 above as a missing guardrail rather than a
  live defect: it needs a C-level block in a six-bytecode window.
- **2. `print` from concurrent threads does *not* splice mid-line.** I expected
  interleaved output from the 5 Places workers (`google_places.py:257`) racing the
  main thread's progress line (`enricher.py:262`) and went looking for it. It does
  not happen: `TextIOWrapper.write` is a single C call, and every `print` in this
  repo passes one pre-formatted f-string, so one message is one write. Log *ordering*
  is nondeterministic, so you cannot attribute a page to a worker — but no line is
  corrupted. Do not spend time here.
- **3. The thread-local `Session`s do not leak.** I expected the sessions in S3 to
  accumulate. Measured with weakrefs only: 40 created per `check_websites()` call,
  **0 still alive** after the pool shut down and a `gc.collect()` — CPython
  refcounting reclaims them when the worker thread's `threading.local` storage is
  dropped at thread exit. The S3 finding is about sockets held *open during* the run
  and the absence of an explicit teardown hook, not about a leak.
- **4. `GooglePlacesScraper`'s concurrent dedup is the right pattern; `dedup.py`'s is
  right for the opposite reason.** `google_places.py:263-266` does a
  check-then-add on the shared `seen_ids` set and it *is* correct, because
  `with seen_lock:` makes the `in` test and the `add` atomic together — otherwise
  two workers could both pass the test and both emit the same `place_id`.
  `dedup.py`'s `phone_index`/`name_index` (`:198-199`) need no lock precisely
  because they are function-local and `dedup` is single-threaded. The asymmetry is
  a trap for the next reader: one identical-looking index needs a lock, the other
  must never get one. Worth one comment at each site saying *why*.
- **5. `dedup()` would be safe even if called from three threads at once.** All nine
  module globals (`dedup.py:8-30`, `:97-117`) are bound once at import and no
  function rebinds any of them; every piece of mutable state (`phone_index`,
  `name_index`, `merged`, `captured_phones`) is function-local. Verified: eight
  threads calling `dedup()` on 1,000 records each all returned the correct 500.
  So the S2 above is *specifically* about sharing the record dicts, not about the
  function.
- **6. Nested pools are fine here, and there is no deadlock.** `main`'s 3 workers
  each run `scraper.scrape()`, and the Google worker opens its own 5-worker pool
  (`google_places.py:194`), so the process runs 8 concurrent outbound streams
  (1 Overpass + 1 Wikidata + 5 Places + main). No worker ever waits on another
  worker, so the usual nested-`ThreadPoolExecutor` deadlock does not apply. It
  *would* apply the moment someone submitted a task that itself waits on a task in
  the same 3-worker pool — e.g. "fan out the enrichment of one scraper's output".
- **7. `check_websites` cannot submit into its own pool**, and that is load-bearing.
  `enricher.py:238` submits `_fetch_website`, which makes no further submissions —
  so the 40-worker pool cannot deadlock on itself. If `_fetch_website` ever gains
  retry-with-backoff via `httpclient.fetch_with_retry`, note that that function
  `time.sleep`s (`httpclient.py:206`) rather than submitting, so it stays correct.
- **8. Returning `False` for `0` and `False` is correct, not a bug.** `_is_missing(0)`
  and `_is_missing(False)` both return `False` ("has data"), which is what you want:
  `enricher.py:250` writes `website_live = False` for a server-confirmed dead site,
  and `dedup.py:183` puts `website_live` in `_VOLATILE` so a stale `True` loses to a
  fresh `False`. If `_is_missing` had been "falsy means missing", merging a dead
  site with a blank one would have silently upgraded `False` to blank and thrown away
  the rebuild signal.
- **9. `check_websites` cannot double-write one record.** `records` cannot contain
  the same dict object at two indices: `dedup` always produces fresh dicts via
  `dict(record)` (`dedup.py:210`, `:218`) or `_merge` (`dedup.py:191`). If a future
  refactor to dedup starts returning input objects unchanged (a plausible
  "optimisation" — skip the copy), two futures could resolve to the same dict and
  the second write-back would silently clobber the first.
- **10. `_is_missing` has no documented thread-safety contract**, which is why this
  lens needed a disassembly to settle. One line in the docstring at `dedup.py:80-85`
  — "pure, reentrant, holds no state; safety depends on callers not mutating the
  record dicts concurrently" — would prevent the next audit from re-deriving it.

---

## What could corrupt, concretely — and is it safe today?

| # | Corruption | Mechanism | Reachable today? |
|---|---|---|---|
| 1 | Merged row carries a URL/email/handle that exists in **no live dict** | torn read at `dedup.py:167`→`:168`, value returned at `:170` | **No.** `dedup` is main-thread-only; `check_websites`' pool is joined before `dedup` runs |
| 2 | Wrong field wins the merge | `_validity` (`:122`) scores a value replaced before `rank` (`:182`) compared it | **No.** same reason |
| 3 | A live contact channel silently vanishes from the merged row | key added after the `set(a) \| set(b)` snapshot at `dedup.py:192` | **No.** same reason |
| 4 | Row keeps `website_live = None` and loses all four contact fields | duplicate rows for one business (`dedup.py:205` + `:226-230`) → concurrent GETs to one host → one loses the race → `UNKNOWN` at `enricher.py:176`, extraction gated on `LIVE` at `:251` | **Yes — live.** Measured 50% redundant GETs |
| 5 | Row is fetched despite being discarded as a value | `enricher.py:231` truthiness vs `dedup.py:88` strip-aware check disagree on `"   "` | **Yes — live**, narrow |
| 6 | Up to 400 sockets open to third-party hosts; `EMFILE` under a second HTTP stage | no `Session.close()` (`httpclient.py:58-91`) | **Partly.** sockets held during the run; `EMFILE` needs a second stage |

Rows 1-3 are the concurrency answer and they are all **latent**. Row 4 is the only
live defect this lens found, and it is not in `_is_missing` — it is in `dedup`'s
identity keys, surfacing as wasted and self-inflicted load on a 40-thread pool.

---

## Recommended order of work

1. **Dedupe fetch targets in `check_websites`** (`enricher.py:231`, `:238`) — one
   `Future` per distinct URL, fan the result out to every row index. Biggest live
   win, smallest diff, and it halves outbound traffic as a side effect.
2. **Copy at the phase boundary**: `dedup([dict(r) for r in combined])` at
   `main.py:180`, plus the two comments (at `main.py:180` and `enricher.py:249`)
   that state the invariant. Ten lines; removes findings 1-3 from the table
   permanently.
3. **Add the regression test** — run `_merge` from a pool over records another
   thread mutates, assert every merged value is present in one of the two inputs,
   and assert `check_websites` issues one GET per distinct URL.
4. **Unify the blank predicate** into one `is_blank()` used by `dedup.py:86`,
   `enricher.py:103-116`, `enricher.py:231` and `main.py:137`, handling
   `None`/whitespace/empty containers/NaN.
5. **Add `close_session()` to `httpclient.py`** and a teardown hook in
   `enricher.py:237`, before any second HTTP stage lands.
6. **Fix the identity gap** (`dedup.py:226-230`): a phone-less record whose
   `(name, city)` matches a phone-keyed record should merge into it. Larger change,
   belongs with `002-dedup-merge-data-loss` / `095-dedup-strategy-v2`; finding 1
   above is what you get if that slips.

### Reproducing this report

All experiments are read-only w.r.t. the repo and make no network calls. They import
`dedup` directly (stdlib-only) and stub `requests` to exercise `httpclient`'s
`threading.local`:

```python
# purity / reentrancy / globals
import sys, dis, threading; sys.path.insert(0, ".")
import dedup
dis.get_instructions(dedup._is_missing)          # -> LOAD_GLOBAL: none
print([dedup._is_missing(v) for v in (None, "", "  ", b"", [], {}, 0, False, float("nan"))])
# -> [True, True, True, False, False, False, False, False, False]
#    note b"", [] and {} report "present"

# torn read through the real _pick (deterministic)
class Y(dict):
    def get(self, k, d=None):
        v = super().get(k, d); time.sleep(0); return v
rec = Y({"website": "https://real-cafe.example"}); oth = {"website": ""}
rec["website"] = None                             # other thread lands here
print(dedup._pick("website", rec, oth))            # -> 'https://real-cafe.example'  (phantom)

# duplicate fetches into the 40-thread pool
A = {"name": "Cafe", "phone": "+96170123456", "website": "https://cafe-real.example",
     "address": "Verdun, Beirut, Lebanon", "source": "osm"}
print(len(dedup.dedup([A, dict(A, phone=None)])))  # -> 2, same website twice
```
