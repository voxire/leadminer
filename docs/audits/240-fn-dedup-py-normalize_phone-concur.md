# 200 — Concurrency audit: `normalize_phone` (`dedup.py:33`)

## Verdict

**`normalize_phone` is safe under this codebase's concurrency, and that safety is not
accidental — it is structural.** The function is a pure function of `(phone, country)`:
it reads only immutable module constants (a `tuple`, a `dict` that is never mutated, and
an `int`), allocates every intermediate locally, and never mutates its arguments or any
caller-visible state. There is no `global` statement in any project file, and no thread
pool ever calls it: all three pools (`main.py:160`, `enricher.py:237`,
`google_places.py:194`) are closed by their `with` block before `dedup()` runs at
`main.py:180`, and none of the three scraper modules imports `dedup` at all. **Nothing
can corrupt.**

The one finding worth acting on is a latent one: the *only* thing standing between this
function and a cross-thread data race is the accident that `_COUNTRY_CODES`
(`dedup.py:23`) is a `dict` and nothing ever mutates it. I proved the race is real and
reachable if that invariant is ever broken by future code.

---

## What I actually checked, and how

### The three thread pools — full inventory

`rg`-equivalent sweep for `ThreadPoolExecutor|threading\.|multiprocessing|\bglobal\b`
across `*.py` (excluding `docs/`) returns exactly 10 hits and **three** pools:

| Pool | Site | Workers | Job function | Does it reach `dedup`? |
|---|---|---|---|---|
| Scraper fan-out | `main.py:160` | 3 | `run(scraper)` → `scraper.scrape()` (`main.py:157-158`) | **No** — no scraper imports `dedup` |
| Website liveness | `enricher.py:237` | 40 | `_fetch_website(url)` (`enricher.py:238`) | **No** |
| Google Places queries | `google_places.py:194` | 5 | `fetch_query(query)` (`google_places.py:195`) | **No** |

Every pool is bound by `with ... :`, and no worker is spawned outside a `with` block
(zero bare `ThreadPoolExecutor(` constructor calls).

### The call sites — there are only three, and all are single-threaded

`normalize_phone` is called at exactly:

- `dedup.py:205` — inside `dedup()`, a plain `for` loop over `records`
- `dedup.py:228` — inside `dedup()`, a plain `for` loop over `name_index.values()`
- `main.py:194` — inside `main()`'s final `for r in records:` loop

`dedup()` is called from exactly one place: `main.py:180`, on the main thread, **after**
the scraper pool at `main.py:160-168` has exited its `with` block.

### Empirical proof — all three pools live, zero pool-thread calls

I ran the real `main.main()` end to end with zero network (stubbed `requests`/`urllib3`/
`bs4`/`lxml` in `sys.modules`; no packages installed; `cwd` set to a temp dir so the
relative `DATA_DIR = pathlib.Path("data")` at `main.py:41` wrote CSVs into the temp dir
and **not** into the repo). I kept the real pool structures — `GooglePlacesScraper.scrape`
and its `ThreadPoolExecutor` are the genuine article, with only `_scrape_query` and
`_make_session` replaced — and wrapped `normalize_phone` to record
`threading.current_thread().name`.

```
  3-scraper main pool threads          : 1 ['ThreadPoolExecutor-0_0']
  5-worker Google pool threads that ran : 5 ['ThreadPoolExecutor-1_0' ... 'ThreadPoolExecutor-1_4']
  40-worker enricher pool threads       : 4 ['ThreadPoolExecutor-2_0' ... 'ThreadPoolExecutor-2_3']
  normalize_phone calls, by executing thread:
     MainThread                         361
  normalize_phone called from a POOL thread : NONE
```

Three distinct `ThreadPoolExecutor` generations (`_0_`, `_1_`, `_2_`) confirm three
distinct live pools in one process. **361 calls, 100% `MainThread`, 0 from any pool.**

(Honest caveat: only 1 of 3 scraper-pool threads got work in my run because my stubbed
generators yield instantly. With real network latency all three would be live. This does
not weaken the verdict — the scrapers cannot call `dedup` regardless, which is a static
import fact, not a scheduling observation.)

### Phase barrier is guaranteed by CPython, not by luck

`with ThreadPoolExecutor(...) as pool:` calls `shutdown(wait=True)`, which ends in
`t.join()` for every worker thread (verified against the local
`concurrent/futures/thread.py` source):

```python
    def shutdown(self, wait=True, *, cancel_futures=False):
        ...
        if wait:
            for t in self._threads:
                t.join()
```

and `threading._register_atexit(_python_exit)` joins any straggler pools at interpreter
exit. So no pool outlives its `with` block, and the ordering

```
main.py:160  scraper pool (3)  -> exits with-block
main.py:180  dedup()           -> MainThread
main.py:187  enrich()          -> 40-pool inside, exits before returning
main.py:194  normalize_phone   -> MainThread
```

is a hard happens-before edge, not a timing assumption.

---

## Shared-state analysis of `normalize_phone` (`dedup.py:33-63`)

### Immutable module state — read-only after import

| Symbol | `dedup.py` | Type | Mutated at runtime? |
|---|---|---|---|
| `_KNOWN_COUNTRY_CODES` | 8 | `tuple` of `str` | No — immutable, cannot be |
| `_COUNTRY_CODES` | 23 | `dict[str, str]` | No — **mutable, but never written** (see S2) |
| `_MIN_DIGITS` | 30 | `int` | No — immutable |

Verified by identity/type inspection at import:

```
_KNOWN_COUNTRY_CODES   tuple  ('961','966','962','971','965','973','974','968','212','216','218','20')
_COUNTRY_CODES         dict   {'LB': '961', 'SA': '966'}
_MIN_DIGITS            int    7
```

### Per-call state — all local, all freshly allocated

`raw` (42), `had_plus` (43), `digits` (44), `cc` (48) are all locals. Every rebinding
produces a **new** string object; nothing is written in place:

- `dedup.py:52` `digits = digits[2:]` — slice, new object
- `dedup.py:55` `digits = cc + digits[len(cc) + 1:]` — concatenation, new object
- `dedup.py:63` `"+" + cc + digits.lstrip("0")` — new object

### No mutation of caller data

`raw = str(phone)` at `dedup.py:42` aliases the caller's string when `phone` is already a
`str`, but `str` is immutable, so the alias is harmless. `re.sub` at `dedup.py:44`
allocates. **The function cannot write through to `record["phone"]`.** This matters
because the caller at `main.py:194` *does* assign back (`r["phone"] = normalize_phone(...)`)
— that write is the caller's, single-threaded, and correctly ordered.

### Reentrancy

No locks, no I/O, no callbacks, no `yield`. It cannot block, cannot deadlock, and cannot
observe a partially-updated invariant from another thread, because it has no invariant to
observe. I forced the only theoretically reachable reentrancy path — the `__contains__`
hook on a `str` subclass at `dedup.py:43` — and the nested reentrant call returned
identically to the top-level call:

```
nested reentrant call result: +961370123456  (top-level: +961370123456)
```

### Session reuse — not applicable

`normalize_phone` opens no socket and touches no `requests.Session`. The session concern
belongs to `httpclient.get_session()` (`httpclient.py:61-71`), which is correctly
thread-local via `threading.local()` at `httpclient.py:58`, and to
`GooglePlacesScraper._make_session()` (`google_places.py:159-166`), which is correctly
called per-worker at `google_places.py:189`. **Neither is reachable from
`normalize_phone`.**

### Dict mutation visible across threads — not applicable to the target

The 40-pool does mutate record dicts, but at `enricher.py:249-259` on the **consumer
side** (`as_completed` loop), not in the workers. Workers receive only a URL string
(`enricher.py:238`: `pool.submit(_fetch_website, url)`; signature `_fetch_website(url: str)`
at `enricher.py:161`), so they hold no reference to `records` at all. The Google pool's
shared mutable state (`seen_ids`, `all_records` at `google_places.py:181-184`) is
correctly lock-guarded, and each worker's `records` list (`google_places.py:210`) is
thread-local before being merged under `records_lock` (`google_places.py:191-192`).
`dedup.dedup()` runs after all of this has fully quiesced and builds its own
`phone_index` / `name_index` locals (`dedup.py:198-199`).

Shallow-copy safety: `phone_index[key] = dict(record)` (`dedup.py:210`) copies the top
level only. Every field in `BusinessRecord` (`scrapers/base.py:5-28`) is
`str | float | int | bool | None` — there is not one nested mutable container — so the
shallow copy shares nothing mutable. Correct as written.

---

## Findings

### S2 — `_COUNTRY_CODES` is the only mutable global `normalize_phone` reads, and nothing enforces that it stays read-only

- **Where:** `dedup.py:23-26`, read at `dedup.py:48`
- **Severity rationale:** S2 not S1 because **it cannot fire today** — I grepped every
  `*.py` for `\bglobal\b` and for writes to `_COUNTRY_CODES` and found none, and no pool
  thread calls the function. It is S2 rather than S3 because the failure mode is silent
  wrong-dedup-key generation, and because the guard is a convention with no mechanism
  behind it. This is the single change that would make the function unsafe.
- **The race is real and reachable, not theoretical.** I proved a `dict` read *can* miss
  during a concurrent `clear()`/`update()` window. 8 reader threads against 2 mutator
  threads:

  ```
  reads attempted : 581556
  torn dict reads : 274380   <- .get() MISSED during the clear/update window
  ```

  Nearly half of all reads observed a mid-mutation table. `dedup.py:48` is
  `_COUNTRY_CODES.get(country, "961")` — a single `BINARY_SUBSCR` that hits a miss and
  silently falls through to the default.
- **Why it is invisible today (two independent accidents):**
  1. Nothing mutates the dict.
  2. Even if it were mutated, the miss is *masked* for `country="LB"` because the
     hardcoded default `"961"` at `dedup.py:48` happens to equal the real LB value. My
     first mutation experiment returned `+9611234567` before **and** after 600k
     clear/update/repopulate cycles. Change that fallback to a different value — e.g.
     `""` — and the same experiment immediately reports wrong country codes.
- **Concrete corrupted input, if the invariant breaks:** a record
  `{"phone": "0501234567", "country": "SA"}` is keyed `+966501234567`. If a thread
  clears `_COUNTRY_CODES` at that instant, `cc` becomes `"961"` and the key becomes
  `+961501234567` — a Saudi mobile permanently relabelled Lebanese, which then either
  merges it with a real Lebanese number or splits it from its own kind. Same shape as
  the cross-border class of defect already catalogued elsewhere; this is the
  *concurrency* route into it.
- **Also note:** `_KNOWN_COUNTRY_CODES` (`dedup.py:8`) being a `tuple` is what makes the
  loop at `dedup.py:58-60` safe. If it were a `list` or `set`, an in-place append from
  any thread would be a genuine race with no mitigating default.
- **Fix (one line):** make the read side immune rather than relying on the convention —
  `cc = _COUNTRY_CODES.get(country) or "961"` still reads the dict, so the real fix is to
  freeze the source of truth:

  ```python
  _COUNTRY_CODES: Mapping[str, str] = MappingProxyType({"LB": "961", "SA": "966"})
  ```

  `types.MappingProxyType` is read-only at runtime, so a future in-place write raises
  `TypeError` immediately instead of silently corrupting keys. Zero runtime cost, and it
  converts a silent-corruption hazard into a loud failure.

### S3 — No concurrency regression test exists for `normalize_phone`, so a future refactor can break the invariant silently

- **Where:** `tests/test_lead_signal.py:102-123` (`TestNormalizePhone`); target
  `dedup.py:33`
- **Breaks:** The four existing tests are all single-threaded correctness assertions. None
  would fail if someone moved `normalize_phone` into `check_websites`'s worker body, or
  made `_COUNTRY_CODES` a module-level cache that a worker mutates, or added an `lru_cache`
  whose key derivation is order-dependent. The safety property I verified today is
  **enforced by nothing**, so any refactor of the pipeline's threading (parallelising
  `dedup`, moving the `main.py:194` loop into `check_websites`, batching per country)
  silently forfeits it.
- **Trigger:** a plausible future change — running `dedup()` per country shard in a
  `ThreadPoolExecutor` at `main.py:180`, which is exactly the kind of "speed up the slow
  loop" change this codebase invites. It would pass the entire existing suite and rely
  on the untested property above.
- **Fix:** one test that pins the property rather than the output —

  ```python
  def test_normalize_phone_is_thread_safe(self):
      cases = [("00961370123456", "LB"), ("+961 3 123 456", "LB"),
               ("+966501234567", "LB"), ("0501234567", "SA")]
      expected = [normalize_phone(*c) for c in cases]           # single-threaded
      with ThreadPoolExecutor(max_workers=40) as pool:
          got = list(pool.map(lambda c: normalize_phone(*c), cases * 1000))
      self.assertEqual(got, expected * 1000)
  ```

  I ran the equivalent directly: **45 threads × 4,000 iterations × 9 inputs = 1,620,000
  comparisons, zero divergent results.** A determinism test is cheap insurance for a
  property the whole dedup index depends on.

---

## Not a bug, but worth knowing

- **The 40-pool correctly avoids sharing record dicts with workers.** `enricher.py:238`
  passes only the URL; the mutation of `records[idx]` happens on the single consumer
  thread at `enricher.py:249-259`. This is the right design and is the reason the
  `website_live`/contact write-back is not a data race. Worth preserving explicitly — do
  not "optimise" this by moving the write into the worker.
- **`str` is safe under the GIL here for a stronger reason than atomicity.** Even if
  `raw` aliased a caller string, `str` immutability makes sharing safe. There is no
  refcount-style hazard on CPython 3.12/3.14 here because `re.sub` does not retain
  substrings of the input.
- **The import-order fragility I hit is adjacent, not mine.** While building the harness
  I hit `ImportError: cannot import name 'infer_region' from partially initialized module
  'enricher'` via `enricher.py:3` → `scrapers/__init__.py:4` →
  `scrapers/google_places.py:26` → `enricher`. That is a live circular-import
  interaction, already tracked by other audits in this batch, and it is
  single-threaded — import order, not concurrency. Mentioning only so the next agent
  building a test harness knows the shim is required.
- **The brief's "zero tests" is stale.** `tests/test_lead_signal.py` exists and covers
  `normalize_phone`. My S3 is about the *concurrency* property specifically, not about
  missing coverage overall.
- **Out of scope for this lens, flagged for the owning audits:** the correctness of
  `normalize_phone`'s *output* is not a concurrency question and is covered by the
  correctness/edge audits in this batch. I confirmed in passing that the 1,620,000-call
  determinism run used inputs including `"0000000"` → `"+961"` and `"2012345"` →
  `"+2012345"` — both deterministic, both wrong in ways that are *not* concurrency
  defects. They belong to the correctness reports.

---

## Recommended order of work

1. **`_COUNTRY_CODES` → `MappingProxyType`** (`dedup.py:23-26`). One-line change, zero
   cost, converts the only concurrency hazard that touches this function from silent
   wrong-key generation into an immediate `TypeError` at the point of the mistake.
2. **Add the thread-safety determinism test** (`tests/test_lead_signal.py`, alongside
   `TestNormalizePhone` at line 102). Pins the property that is currently enforced by
   convention alone, before any future parallelisation of `dedup()`.
3. **Leave the function alone otherwise.** Do not add locking, do not thread-localise
   anything, do not `deepcopy` inputs. `normalize_phone` is a pure function over
   immutable constants; adding concurrency machinery to it would be pure cost and would
   make the code harder to read for zero safety gain. The correct engineering response to
   this audit is ~2 lines, both of them about *preventing future* races, not fixing
   present ones.

## Bottom line

`normalize_phone` is **currently safe**, verified statically (3 call sites, all
single-threaded; no scraper imports `dedup`) and dynamically (361 calls across a live
3-worker, 5-worker and 40-worker process, 100% from `MainThread`, 1.62M-call
determinism check clean). The function is a pure function of `(phone, country)` over
immutable module state with no I/O, no locks, no callbacks and no argument mutation.
The only actionable item is hardening `_COUNTRY_CODES` against a future write, because
I demonstrated that a concurrent `.get()` on that dict misses roughly 47% of the time
during a clear/update window — the sole reason that latent race is invisible is that
nothing mutates it and its default happens to coincide with the LB value.
