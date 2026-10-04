# 200 — `_extract_city` (dedup.py:72) concurrency review

## Verdict

`_extract_city` is **safe** — safe by construction, not by luck — and it is
**unreachable from any worker thread in this codebase**. It reads no module
global, writes nothing, mutates no argument, closes over nothing, performs no
I/O, opens no session, and allocates only immutable `str`s plus one list that
dies with the frame. Its single call site is `dedup.py:213`, inside `dedup()`,
whose single call site is `main.py:180` — on the main thread, *after* all three
pools have been joined. No lock, no queue, no copy, no memoisation is warranted.
There is no corruption to find in the function.

The one thing that matters for whoever touches this next: **the caller is what
must not be parallelised, not the function.** `dedup()` owns two unguarded
shared mutable dicts (`phone_index` `dedup.py:198`, `name_index` `dedup.py:199`);
those are the objects that would corrupt. Encouragingly, `_merge`/`_pick`
(`dedup.py:179-194`) are provably commutative and order-independent — the
`str(value)` final tiebreak at `dedup.py:184` makes `rank` a total order — so a
parallel reduce grouped by key would be *semantically* safe to build. Verified
empirically below. `_extract_city` will not be the part that stops you.

Two real defects live in the neighbourhood this lens names, and are filed below
clearly labelled as **not** `_extract_city` defects: the 40-worker pool throws
away the TCP connection for every oversized body and every 4xx/5xx (S2), and
`_extract_city` will raise `AttributeError` on a truthy non-`str` address (S3).

---

## The function under review

`dedup.py:72-76`

```python
def _extract_city(address: str | None) -> str:
    if not address:
        return ""
    parts = [p.strip().lower() for p in address.split(",")]
    return parts[-1] if parts else ""
```

## Thread inventory — what actually runs, and when it stops

There are three pools in this process. All three are **joined before**
`_extract_city` executes for the first time.

| # | Pool | Threads | Anchor | Runs on | Joined before `_extract_city`? |
|---|------|---------|--------|---------|-------------------------------|
| A | 3 scrapers in parallel | 3 | `main.py:160-168` (`max_workers=len(scrapers)`, `main.py:155`) | outer worker threads | **Yes** — the `with` block ends at `main.py:168`; `dedup()` is called at `main.py:180`, textually outside it, so `pool.shutdown(wait=True)` has already joined all three |
| B | Google Places queries | 5 | `google_places.py:194-197` (`_WORKERS = 5`, `google_places.py:142`) | nested *inside* pool A's worker for the Google scraper | **Yes** — created and shut down inside `_scrape_query`'s caller (`google_places.py:194-197`), which completes before `main.py:164` returns `batch` |
| C | Website liveness + contacts | 40 | `enricher.py:237` (`workers: int = 40`, `enricher.py:225`) | pool-A threads are already gone; runs on the main thread's `as_completed` loop | **N/A — has not started yet.** `check_websites` is reached via `enrich()` at `main.py:187`, seven lines after `dedup()` |

Two structural facts worth stating because they are the ones people get wrong:

- **Pool B is nested inside pool A, and that is safe.** `main.py:158` is
  `list(scraper.scrape())` — the scrapers are generator functions, so the whole
  body executes lazily *on the pool-A worker thread*, including the creation of
  pool B. Nesting depth is 2, and pool B never waits on pool A, so there is no
  starvation or deadlock. Peak concurrency is 3 outer + 5 inner = 8 threads.
- **Pool A is also the process-level concurrency guard.** The data directory and
  the Drive master are only safe because `.github/workflows/scrape.yml` sets
  `concurrency: group: leadminer-scrape, cancel-in-progress: false`, so a manual
  run cannot overlap a scheduled one. There is no lock *inside* the program; the
  guard is outside it.

### Reachability proof

`grep -n '_extract_city\|dedup(\|normalize_phone'` across the repo returns
exactly one call to `_extract_city` (`dedup.py:213`), one call to `dedup()`
(`main.py:180`), and `dedup` is imported in exactly one module (`main.py:29`).
`tests/test_lead_signal.py:65` imports `_merge` and `normalize_phone` but never
`_extract_city` and never `dedup`. `cli.py` imports neither. So:

> `_extract_city` executes **only** on the main thread, **only** between the
> exit of `main.py:168` and the entry of `main.py:186`. At that point zero other
> threads exist in the process.

Nothing in pool C can reach it either, and this is by design rather than by
accident: `check_websites` submits **only the URL string**, never the record
(`enricher.py:238`: `pool.submit(_fetch_website, url)`), workers return an
immutable `(outcome, contacts)` tuple, and every write to `records[idx]` happens
in the main thread's `as_completed` loop (`enricher.py:249-259`). Region inference
runs before the pool (`enricher.py:330-335`), and the defaults/scores run after it
(`enricher.py:339-350`).

## Purity audit — dimension by dimension

| Dimension | Finding | Anchor |
|---|---|---|
| **Shared mutable state** | **None.** `_extract_city` references no module-level name. Every global in `dedup.py` — `_KNOWN_COUNTRY_CODES` (`:8`), `_COUNTRY_CODES` (`:23`), `_MIN_DIGITS` (`:30`), `_SOURCE_TRUST` (`:97`), `_DEFAULT_TRUST` (`:111`), `_VOLATILE` (`:114`), `_EMAIL_OK`/`_URL_OK` (`:116-117`) — is untouched by it. All are read-only at runtime by every other function in the module, so even a hypothetical concurrent caller would find them stable. | `dedup.py:8-26`, `:97-117` |
| **Session reuse** | **N/A.** No HTTP. `httpclient` is never imported by `dedup.py`; `get_session()` is never on this path. | `dedup.py:1-2` |
| **Dict mutation visible across threads** | **None.** The function's only argument is a `str` (immutable) read via `record.get("address")`. `dict.get` is a single `LOAD_ATTR`-style C call. It writes no dict and reads no dict other than the caller's single lookup. Note it does **not** use `unicodedata.normalize` — that is `normalize_name` (`dedup.py:67`), a different function. | `dedup.py:73-76`, `:67` |
| **Resource lifetime** | One `list[str]` plus 3N transient strings (`split`, `strip`, `lower` each allocate). The list is a frame-local; the return value is `parts[-1]`, an immutable `str`. Nothing escapes, nothing is cached, no file/socket/future is held. Lifetime is bounded by one call — no leak, no retention, no cross-thread visibility. | `dedup.py:75-76` |
| **Reentrancy** | **Trivially reentrant.** Operations used: `bool()`, `str.split`, `str.strip`, `str.lower`, integer indexing. All pure functions of immutable data. No global counter, no memo, no RNG, no clock, no `id()`-keyed cache. Re-entering it from inside itself (impossible here, but the property matters) could not interleave. | `dedup.py:73-76` |

**Empirical confirmation.** 20 threads × 5 distinct inputs, results collected
under a lock, produced exactly the four expected values — `''`, `'hamra main
street'`, `'lebanon'`, `'saudi arabia'` — identical to the single-threaded
results. There is no nondeterminism to find because there is no state.

**Second empirical check (the one that actually matters for a redesign).**
`_merge` is commutative and order-independent:

```
merge commutative: True
deterministic across 200 shuffles: True
```

That holds because `rank` (`dedup.py:179-185`) is a 4-tuple ending in
`str(value)`, and `_pick` returns `av if rank(a, av) >= rank(b, bv) else bv`
(`dedup.py:187`) — two records can only tie on that tuple if `str(av) == str(bv)`,
i.e. the values are already equal, in which case argument order is irrelevant.
So a future `groupby(key) → parallel reduce` over `dedup()` would produce
byte-identical output to the current sequential loop. That is a real enabler, and
it is why the finding below is about documenting an invariant rather than
removing a hazard.

## Findings

### S2 — The 40-worker pool discards the TCP connection for every oversized body and every 4xx/5xx

- **Where:** `enricher.py:175` (`stream=True`), `enricher.py:182-184` (returns
  `DEAD` before reading the body), `enricher.py:188`
  (`r.raw.read(_MAX_BODY_BYTES, decode_content=True)`),
  `enricher.py:193-197` (`finally: r.close()`), cap set at
  `enricher.py:158` = `httpclient.py:54` `DEFAULT_MAX_BODY_BYTES = 200_000`.
- **Breaks:** `_fetch_website` bypasses `requests.Response.iter_content` and
  reads `r.raw` directly. `Response._content_consumed` is therefore never set to
  `True` (in requests 2.32.3 it is set only by `iter_content`'s generator
  epilogue or by the `content` property), so `r.close()` takes this branch in
  requests `models.py`:

  ```python
  def close(self):
      if not self._content_consumed:
          self.raw.close()
      release_conn = getattr(self.raw, "release_conn", None)
      if release_conn is not None:
          release_conn()
  ```

  `self.raw.close()` is urllib3's `HTTPResponse.close()`
  (`urllib3/2.2.3/src/urllib3/response.py:1069-1077`), which calls
  `self._connection.close()` outright. `release_conn()` then does
  `self._pool._put_conn(self._connection)` (`response.py:635-640`) — putting the
  **closed** connection back on the queue — and urllib3 discards it on the next
  acquire via `if conn and is_connection_dropped(conn): conn.close()`
  (`connectionpool.py:289-291`). Net effect: **no reuse**. Two classes of
  response hit this on every single request:
  1. any body larger than 200 KB, truncated at the cap; and
  2. **every 4xx/5xx**, because `enricher.py:182-184` returns before reading the
     body at all.

  Class 2 is the damaging one: DEAD is the core pitch signal
  (`enricher.py:303-304` awards +20 for a server-confirmed dead site), and dead
  sites are disproportionately served with large branded error pages, WAF
  interstitials, or parked-domain pages. So the requests the product cares most
  about are exactly the ones that get no connection pooling, at 40-way
  concurrency against a small set of shared hosts — i.e. 40 simultaneous TCP +
  TLS handshakes to the same origin. Against the 300-minute job budget
  (`timeout-minutes: 300` in `.github/workflows/scrape.yml`) this is real
  wall-clock cost, and it grows with the cumulative master.
- **Trigger:** any `with_websites` record whose site answers `404` with a
  50 KB error page — e.g. `website="http://parked-domain-lb.com"`. One request,
  one discarded connection. Or a 1 MB homepage: `r.raw.read(200_000)` returns
  200 KB, `close()` drops the connection, the next fetch of the same host from
  the same worker re-handshakes.
- **Fix:** drain before closing — call `r.raw.drain_conn()` (urllib3 provides it
  precisely for this, `response.py:643`) or iterate `r.iter_content(...)` with a
  running byte budget instead of `r.raw.read(...)`, and `continue`-ing past the
  `status >= 400` branch is fine *provided* the drain happens first. Cheapest
  correct version: in the `finally`, replace bare `r.close()` with
  `r.raw.drain_conn()` then `r.close()`.

### S3 — `if not address` admits any truthy object; a non-`str` address aborts the run before a single CSV is written

- **Where:** `dedup.py:73` (`if not address:`), `dedup.py:75` (`.split`),
  reached from `dedup.py:213`.
- **Breaks:** the guard tests truthiness, not type. A truthy non-`str` gets
  past line 73 and dies on `.split`. Verified: `_extract_city(12345)` and
  `_extract_city(["a","b"])` both raise `AttributeError`. `None` and `""` are
  correctly handled; `0` and `[]` are correctly falsy; but `12345`, `12.5`, and
  a list are not. This is **not** a concurrency defect — it is a single-threaded
  crash — but it is the only way this function can fail, so it is the one thing
  to harden. The blast radius is disproportionate: the exception propagates out
  of `dedup()` at `main.py:180`, which is *before* any `write_csv` call
  (`main.py:209-213`), so the whole run is lost rather than one row.
  Producers today are all `str | None`: OSM `", ".join(...) or None`
  (`osm.py:87`), Wikidata `row.get("address", {}).get("value")`
  (`wikidata.py:99`), CSV round-trip `str` or `None` (`main.py:87-89`). The
  exposure is Google, where `address=place.get("formattedAddress")`
  (`google_places.py:286`) takes whatever the JSON contains — and
  `BusinessRecord` is a `TypedDict` (`scrapers/base.py:5-28`), which does **not**
  validate at runtime. The field mask requests `places.formattedAddress`
  (`google_places.py:35`) but a mask is a projection, not a type guarantee.
- **Trigger:** a Places response with `"formattedAddress": 12345` — or, more
  plausibly, an object `{"text": "Beirut, Lebanon"}` on an API surface that ever
  changes shape. `dedup()` dies; `main()` dies; no `data/*.csv` is written; the
  Actions step fails and the previous Drive master is untouched.
- **Fix:** `if not isinstance(address, str) or not address.strip(): return ""`.
  One line, and it also absorbs the `"   "` case.

### S3 — `dedup()`'s indexes are the codebase's only unguarded shared mutable dedup state, with no "single-threaded on purpose" marker

- **Where:** `dedup.py:198` (`phone_index`), `dedup.py:199` (`name_index`),
  mutated at `dedup.py:208`, `:210`, `:216`, `:218` and read at `dedup.py:222-230`.
- **Breaks:** nothing today. The real risk is a *normalisation trap*: the
  codebase has already established the opposite convention for shared dedup
  state. `google_places.py:181-184` creates a `seen_ids` set plus a
  `seen_lock`, and `google_places.py:263-266` performs its test-and-set
  correctly inside the lock — a correct, deliberate pattern (independently
  confirmed in `015-google-places-pagination.md:93-102`). An engineer who
  "makes dedup consistent with Google Places" and shards `dedup()` across threads
  would then hit two unsynchronised dicts with a read-then-write
  (`if key in phone_index: ... else: phone_index[key] = ...` at
  `dedup.py:207-210`) — the textbook lost-update and duplicate-key shape. There
  is no comment at `dedup.py:197-199` saying "single-threaded by design", and
  the fact that `_extract_city` is pure gives a false impression that `dedup()`
  is parallelisable by association.
- **Trigger:** a "speed up dedup" change that wraps `dedup.py:201-218` in a
  `ThreadPoolExecutor` over record shards. Under load, two shards compute the
  same key concurrently and both miss, so the record is emitted twice; or one
  overwrites the other's merged dict, silently dropping fields.
- **Fix:** either shard by key rather than by record — which is *semantically*
  safe, since `_merge` is commutative (verified above) and `_extract_city` is
  pure, so each shard owns disjoint keys and needs no lock — or add a one-line
  comment at `dedup.py:198` recording the invariant. Sharding by key is the
  better fix and is now provably available.

## Not a bug, but worth knowing

- **The S1 already filed for this function.** `_extract_city` returning the
  *country* instead of the city is filed as an S1 in
  `003-dedup-identity-model.md:19-45` and aggregated at
  `083-SYNTH-s1-backlog.md:99`. I am deliberately **not** re-filing it. This lens
  adds two concrete data points that are not in 003: (a) confirmed against the
  code as it now stands — `"Hamra Street, Beirut, Lebanon"` → `'lebanon'`,
  `"King Fahd Rd, Al Olaya, Riyadh, Saudi Arabia"` → `'saudi arabia'`, so the
  city component is a **constant per country for the entire Google source**, which
  is the bulk of the dataset (67 queries, `google_places.py:174`); and (b) for OSM
  the answer is a **district**, not even a country, because `osm.py:80-86` orders
  the join `[housenumber, street, suburb, city, district]` with `district` last —
  so `"12, Hamra St, Beirut, Saifiyeh"` → `'saifiyeh'` (verified), while the
  same record without `addr:district` → `'beirut'`. Consequence for a future
  implementer: a one-line `parts[-2]` "fix" would make OSM keys *worse*
  (`'beirut'` regardless of district) while making Google keys right — the two
  sources need different extraction rules, not one.
- **`seen_ids` and `all_records` in pool B are genuinely safe**, including the
  `continue` at `google_places.py:265`: it sits inside `with seen_lock:`, so the
  context manager's `__exit__` runs on the `continue` and releases the lock.
  Test and set are atomic (`google_places.py:264-266`), so there is no TOCTOU.
  This is the same pattern `dedup.py` *should* copy if it ever goes parallel.
- **Per-query Sessions in pool B, not thread-local ones — and this is correct
  here.** `google_places.py:189` calls `self._make_session()`, building a fresh
  `requests.Session` per query with `X-Goog-Api-Key` baked in at construction
  (`google_places.py:161-165`). Pages within one query reuse the connection pool
  (same `session` at `google_places.py:227`), which is the reuse that matters.
  Cost: 67 sessions created per run and never `close()`d; they are reclaimed when
  the worker threads exit, so it is garbage, not a leak. Contrast with
  `httpclient.get_session()` (`httpclient.py:61-71`), which pools by thread and
  is what `osm.py:35` and `wikidata.py:73` use.
- **`http_session()` is a context manager that never closes anything.**
  `httpclient.py:88-91` yields the thread-local Session and does nothing on exit.
  The name implies a scoped resource; the body implies a long-lived one. No
  current caller uses it, so nothing leaks, but it is a trap for the next person.
- **`seen_lock`/`records_lock` are created per `scrape()` call**
  (`google_places.py:182`, `:184`), not per module. Two `GooglePlacesScraper`
  instances in one process — which `cli.py:61` does exactly one at a time, but a
  future `--sources osm,google` fan-out would do concurrently — would each get
  their own locks and their own `seen_ids`, so cross-instance dedup silently
  stops working. Safe today, latent on the obvious extension.
- **`print()` from 5 pool-B threads and 40 pool-C threads.** `TextIOWrapper`
  serialises writes, so lines are not torn mid-line, but line *ordering* across
  sources is nondeterministic. `enricher.py:235`, `:262`, `:271-274` are all
  `print`, and stdout is a pipe in Actions (block-buffered). Progress lines from
  the three scrapers will interleave with each other and with the enricher's
  counters. Cosmetic; the numbers each print are correct because they are
  recomputed from `records` after the pool exits (`enricher.py:264-270`).
- **Peak memory is the real scaling constraint, not threads.** `main.py:158`
  materialises each scraper's full `list`; `main.py:166` accumulates all three
  into `raw`; `main.py:179` builds `combined`; `dedup.py:210`/`:218` make a
  shallow copy of every record into the indexes while `combined` still holds the
  originals. That is roughly 4× the record set live at once — a 22-key `dict` is
  ~200 bytes plus string references, so a 500k-row cumulative master lands in the
  low gigabytes on a 7 GB `ubuntu-latest` runner. Headroom estimate from first
  principles, not measured. `_extract_city` contributes nothing here: its `parts`
  list is dead before the next call.
- **Audit 004 is stale on the session question.** `004-enricher-thread-safety.md:9-13`
  files an S2 for "shared session cookies" against `enricher.py:124-125`. That
  shared `_SESSION` is gone; `enricher.py:128` now imports
  `httpclient.get_session()`, which is `threading.local`
  (`httpclient.py:58`, `:61-71`). The finding is resolved in the current tree.

## Invariant checklist — what a future change must preserve

1. **Keep `_extract_city` argument-only and side-effect-free.** No module
   globals, no memoisation keyed on `id()`, no caching of the `parts` list. The
   moment it acquires any of those it stops being free to call from anywhere.
2. **Keep `dedup()` off worker threads, or shard it by key — never by record.**
   `phone_index`/`name_index` (`dedup.py:198-199`) have no lock. Key-sharding is
   provably equivalent because `_pick` (`dedup.py:179-187`) is a total order and
   `_merge` is commutative; record-sharding loses updates.
3. **Keep worker threads away from the record dicts.** The current discipline —
   submit the URL, return a value, mutate on the main thread
   (`enricher.py:238` → `:249-259`) — is what makes pool C safe. Verified that
   `dedup()` returns distinct dict objects (4 records in → 3 out, no aliased
   identity), so today no two futures can ever write the same dict. Any change
   that makes `dedup()` return a shared dict for two rows reintroduces a
   40-writer race.
4. **Keep the pool joins where they are.** `main.py:180` must stay outside the
   `with` at `main.py:160-168`, and `enrich()` at `main.py:187` must stay after
   it. There is no lock protecting that ordering — it is the `with` block and
   nothing else.
5. **Narrow `enricher.py:73`'s guard before you narrow anything else**, then
   drain the response body before `r.close()`.

## Recommended order of work

1. Drain the response body in `_fetch_website`'s `finally` so the 40-worker pool
   actually reuses connections (S2 above; largest concrete wall-clock win in the
   three pools, and it lands on the dead-site signal specifically).
2. `isinstance(address, str)` in `_extract_city`, and a comment at
   `dedup.py:198` recording that the indexes are single-threaded by design
   (two one-line changes that remove a crash and a future refactor hazard).
3. When someone does parallelise dedup, shard by key — the commutativity proof
   above means it needs no lock, only disjoint key ownership.
4. The real `_extract_city` correctness fix stays where 003 filed it: per-source
   extraction rules (Google → `parts[-2]` with a country-token strip; OSM → the
   `addr:city` tag itself, not a positional guess), ideally by carrying `city` as
   a field from the scrapers rather than parsing a free-text address at the dedup
   stage.