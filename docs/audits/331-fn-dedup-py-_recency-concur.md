# 331 — `_recency` (dedup.py:155) under the concur lens

## Verdict

`_recency` is **currently safe**. It is a pure, stateless string read with no I/O, no
session, and no shared mutable state, and the only path to it —
`_recency` → `_pick` (dedup.py:183) → `_merge` (dedup.py:193) → `dedup` (dedup.py:208,
216) — is reachable only from `main.py:180`, which sits **strictly between** the two
thread pools: the 3-scraper pool has already joined (main.py:160-168, `__exit__` →
`shutdown(wait=True)`), and `check_websites`' 40-thread pool has not started yet
(main.py:187). No thread in this codebase can reach `_recency`.

The real risk is not a race today but that the safety is **incidental, not enforced**.
`dedup`'s `if key in phone_index: ... else: ...` (dedup.py:207-210, 215-218) is a
non-atomic check-then-act that silently drops a record if anyone ever shards that loop
across a pool, and `_recency`'s one real job — deciding `rating`/`review_count`/`website`
survivorship — is done by raw lexicographic string comparison on a value that comes back
from a CSV with no parse and no validation.

## Lens coverage, one dimension at a time

| Dimension | Answer | Evidence |
|---|---|---|
| Shared mutable state | None touched | `_recency` (dedup.py:155-156) reads only its argument; no module global is read or written |
| Session reuse | Out of scope by construction | `_recency` makes no request; `httpclient.get_session` (httpclient.py:61-71) is never on its path |
| Dict mutation visible across threads | Impossible today | `_merge` builds a new dict (dedup.py:191-193) and never mutates `a`/`b`; `dedup` stores `dict(record)` copies (dedup.py:210, 218) |
| Resource lifetime | Nothing to leak | No file/socket/lock held across the call |
| Reentrancy | Trivially reentrant | No state across calls; the only sibling calls, `normalize_phone`/`normalize_name` (dedup.py:33, 66), are also pure (`re.sub` + `unicodedata.normalize` are thread-safe) |

### Why the ordering holds — traced, not assumed

```
main.py:160  with ThreadPoolExecutor(max_workers=3) as pool:   # 3 scrapers
main.py:162      for future in as_completed(futures):          # drains all 3
main.py:166          raw.extend(batch)                          # main thread only
main.py:168      except Exception -> print                      # no raise
              # <-- __exit__ calls shutdown(wait=True): all 3 scraper threads JOINED
main.py:172  raw_filtered = [...]
main.py:180  records = dedup(combined)      <== ONLY caller of dedup; reaches _recency
main.py:187  records = enrich(records)      <== ThreadPoolExecutor(max_workers=40)
                                                    enricher.py:337 check_websites
```

`dedup(` has exactly one call site (`grep -rn --include='*.py' 'dedup(' .` →
`dedup.py:197` def, `main.py:180` call). `cli.py` never calls it. Both pools are
therefore **sequenced either side of** the only `_recency` call site.

### Verified, not assumed

Read-only probe (`dedup` imports only `re` + `unicodedata`; no network, no writes):

- `_merge(a, b)` leaves `a` and `b` byte-identical and returns a third object →
  no aliasing, no in-place mutation, safe to hand a dict that a thread also holds.
- `_merge(old, new) == _merge(new, old)` on every field, including the `rating`
  case `_recency` decides → **merge outcomes are order-independent**.
- `utc_now_iso()` output sorts lexicographically in chronological order, including
  the zero-microsecond case (`2026-10-03T10:00:00+00:00` < `...:00.000001+00:00`),
  because `.` (0x2E) > `+` (0x2B) and the seconds digits differ first.

## Findings

### S2 — `_recency` is a raw string compare on an unvalidated CSV value, and it alone decides survivorship of `rating`, `review_count`, `website`

- **Where:** `dedup.py:155-156`, consumed at `dedup.py:183` as tuple element 3 of
  `rank` (`dedup.py:179-185`). `_VOLATILE` = `{"rating", "review_count", "website_live", "website"}`
  (`dedup.py:114`), documented as "the fresher observation wins".
- **Breaks:** `scraped_at` reaches `_recency` as an opaque string. There is no
  `datetime.fromisoformat`, no format check, no fallback. The value's provenance is
  `main.py:150` `load_master(DATA_DIR / "all_businesses.csv")` — and `load_master`
  casts only `_FLOAT_FIELDS`/`_INT_FIELDS` (main.py:90-101), so **`scraped_at` passes
  through from the CSV verbatim, never parsed or validated**. Because `rank` is a tuple
  compared lexicographically and `_recency` is element 3, a mixed-format value silently
  and permanently wins or loses every volatility comparison.
- **Trigger:** a master CSV whose `scraped_at` carries a non-UTC offset. Concrete pair:
  ```
  old = {"rating": 3.0, "scraped_at": "2026-10-03T12:00:00+03:00", ...}  # 09:00 UTC, OLDER
  new = {"rating": 4.8, "scraped_at": "2026-10-03T10:00:00+00:00", ...}  # 10:00 UTC, NEWER
  ```
  Verified output: `max()` returns the **`+03:00`** record, i.e. the genuinely *older*
  `rating: 3.0` survives. Hour `"12"` beats `"10"` as text before the offset is ever
  read. Note the raw `max(av, bv)` at `dedup.py:177` propagates the same bad winner
  into the merged `scraped_at`, so it is written back to the CSV (main.py:39) and
  poisons every subsequent run.
- **Also concrete, non-hypothetical:** the repo's own fixtures are not format-uniform —
  `tests/test_lead_signal.py:204` uses `"2026-01-01T00:00:00Z"` while
  `tests/test_lead_signal.py:305,307` use `+00:00`. For one identical instant,
  `max("...T00:00:00Z", "...T00:00:00+00:00")` returns the `Z` form, because `'Z'`
  (0x5A) > `'+'` (0x2B). Arbitrary, but harmless here — which is precisely the problem:
  it is indistinguishable from a real decision.
- **Honest scoping:** with the single current producer `utc_now_iso()`
  (httpclient.py:229, always `+00:00`), the formats in the pipeline are uniform and
  this is **not producing wrong output today**. It is S2 rather than S3 because the
  input is an untrusted CSV with no validation, the blast radius is three
  customer-facing fields at 500k-row scale, and it fails silently — there is no run in
  which anyone finds out.
- **Fix:** parse in `_recency` and rank on a real datetime, e.g.
  `return _parse_ts(record.get("scraped_at"))` returning a `datetime` with a
  `datetime.min` sentinel for unparseable input; validate `scraped_at` in
  `load_master` alongside the existing casts.

### S2 — `dedup`'s check-then-act is not atomic; parallelising it silently drops a record

- **Where:** `dedup.py:207-210` and the identical shape at `dedup.py:215-218`.
  ```python
  if key in phone_index:
      phone_index[key] = _merge(phone_index[key], record)
  else:
      phone_index[key] = dict(record)
  ```
- **Breaks:** the `in` test at line 207 and the store at line 210 are separate
  bytecode sequences with `dict(record)` — a full 22-key copy
  (`scrapers/base.py:5-28`) — in the window between them. Two threads holding
  observations of the same business can both take the `else` branch; the second
  `dict(record)` **overwrites** the first instead of merging, so one side's fields
  vanish with no error and no log line.
- **Trigger:** sharding the `for record in records` loop (dedup.py:201) across a pool
  keyed by `normalize_phone(...)`. Two records, same phone:
  `old = {"rating": 3.0, "scraped_at": "2026-10-03T09:00:00+00:00", "source": "google_places"}`
  and `new = {"email": "hi@cafe.example", "scraped_at": "2026-10-03T10:00:00+00:00", "source": "osm"}`.
  Ground truth (`_merge` sequential) = `{"rating": 3.0, "email": "hi@cafe.example"}`.
  Forced-interleaving probe: both branches take `else`, last writer wins, and the
  result carries **only one side**.
- **Honest scoping — and I could not reproduce this naturally:** 20,000 trials with a
  full 23-key payload under CPython's GIL produced **0** lost-field interleavings,
  because `dict.copy` does not release the GIL. So this is **latent, not a live bug**.
  It becomes reachable under free-threading (PEP 703) or the moment anyone adds I/O,
  logging, or a `_validate` call inside that window. I am flagging it because the
  codebase already contains the correct pattern for this exact shape 700 lines away:
  `seen_lock` guarding `if place_id in seen_ids: continue / seen_ids.add(place_id)`
  (google_places.py:182, 263-266) and `records_lock` guarding
  `all_records.extend(results)` (google_places.py:184, 191-192). `dedup` is the only
  unguarded read-modify-write in the project.
- **Fix:** `with lock: idx[key] = _merge(idx[key], record) if key in idx else dict(record)`,
  mirroring `google_places.py:263-266`. Or, if dedup stays sequential, add a comment at
  dedup.py:198 recording that the loop must remain single-threaded.

### S3 — `_VOLATILE` recency arbitration is unreachable for `website_live` in the cross-run merge it was written for

- **Where:** `dedup.py:114` (`website_live` in `_VOLATILE`) vs the short-circuit at
  `dedup.py:168-171`, which runs **before** `rank` and therefore before `_recency`.
- **Breaks:** every scraper emits `website_live=None` unconditionally —
  osm.py:109, wikidata.py:119, google_places.py:292. Every master row has a real bool
  or `None` (`main.py:102-103`). So when a fresh record merges into a master record,
  `_is_missing(bv)` is `True` at dedup.py:170 and the function returns the stale
  master's verdict at dedup.py:171. **`_recency` is never consulted for
  `website_live` in exactly the fresh-vs-stale case `_VOLATILE` exists to arbitrate.**
  Verified: merging a fresh `website_live=None` record into a master
  `website_live=False` record yields `False`, with the newer `rating` correctly taken.
  `_recency` is reached for `website_live` only when **two master rows** both carry a
  non-missing value — verified that this path does work correctly.
- **Why S3 and not S2:** the damage is masked downstream. `check_websites` re-fetches
  every record with a website (enricher.py:231) and unconditionally overwrites
  `website_live` at enricher.py:250, so a stale verdict is corrected in the same run.
  And `website_live` truthy ⟹ `website` truthy holds in every write, so a stale verdict
  is never carried onto a record that `check_websites` skips.
- **Fix:** if the merge is meant to arbitrate freshness, order the checks so `_VOLATILE`
  is consulted before the missing-value short-circuit; otherwise drop `website_live`
  from `_VOLATILE` (dedup.py:114) and stop implying it is arbitrated.

### S3 — output row order is nondeterministic run to run, because 3 parallel scrapers feed a dict-order-dependent `dedup`

- **Where:** `main.py:162` `for future in as_completed(futures)` → `raw.extend(batch)`
  (main.py:166) lands batches in **completion** order, which varies per run.
  `phone_index` (dedup.py:198) is a plain dict, so its insertion order is first-seen
  order, and `dedup` returns `list(phone_index.values())` (dedup.py:222) in that order.
- **Breaks:** `all_businesses.csv` is rewritten in a different row permutation every
  run even when not one field value changed. Verified: `dedup([1,2,3])` → `[1,2,3]`,
  `dedup([2,1,3])` → `[2,1,3]`. There is no sort key, so "did anything actually change
  this week?" is unanswerable by diffing the master.
- **Not caused by `_recency`:** merge *outcomes* are order-independent (verified,
  `_merge(a,b) == _merge(b,a)` for the `rating` case `_recency` decides — the tuple
  total-orders on `str(value)` at dedup.py:184, so a full tie yields an equal value
  either way). Only the *iteration* order varies.
- **Fix:** `return sorted(phone_records + name_records, key=lambda r: (normalize_name(r.get("name") or ""), r.get("name") or ""))`
  in `dedup`, so the export is stable regardless of scraper arrival order.

## Not a bug, but worth knowing

- **`check_websites`' 40-thread pool is safe, and correctly so — keep the shape.**
  Workers only compute: `_fetch_website(url)` (enricher.py:161-222) takes a `str`,
  builds a local `contacts` dict, returns a tuple. It never touches a record. The
  `as_completed` loop in the *submitting* thread does all mutation
  (enricher.py:249-259). `targets` is built from `enumerate` (enricher.py:231) so each
  index is written exactly once. This is the correct workers-compute / main-thread-writes
  split, and it is why `dedup` at main.py:180 has nothing to race against.
- **`get_session()` is thread-local and that is what makes the 40 threads safe**
  (httpclient.py:58, 61-71), replacing a shared module-level `requests.Session`.
  `GooglePlacesScraper` does the same per worker (google_places.py:188-189). Sessions
  outlive individual tasks because `ThreadPoolExecutor` reuses its threads, so
  connection pooling actually works — that is desirable, not a leak.
- **`GooglePlacesScraper`'s `scraped_at` is one timestamp for the entire scrape, not
  per record** (google_places.py:173, computed once before the pool, passed by
  reference to all 5 workers at line 190, used at line 303). Workers only read it, so
  there is no race — but a Places record scraped in the last second of a 3-minute run
  and one scraped in the first second carry the same `scraped_at`. Within a single
  source `_recency` is therefore a constant and cannot arbitrate; it only ever decides
  *between sources* or *between runs*. Not a bug; just do not read `_recency` as
  "when was this specific row observed".
- **The two `print`-to-stderr error paths in `main` (main.py:167-168) mean a scraper
  that raises contributes zero rows and the run continues.** That is a data-quality
  hazard for `dedup`'s inputs (a source silently absent), but it is upstream of
  `_recency` and is a different lens.
- **Overlapping runs are already guarded at the CI layer**: `.github/workflows/scrape.yml:15-17`
  sets `concurrency: {group: leadminer-scrape, cancel-in-progress: false}`, so two
  Actions runs cannot race on `data/` or the Drive master, and main.py:130 `os.replace`
  makes each write atomic. A **local** `python main.py` concurrent with a CI run is
  still unguarded — there is no lockfile — but that is a workflow concern, not a
  `_recency` concern.
- **Hot path note (not concurrency):** `rank` (dedup.py:179-185) is called twice per
  field per merge, and `_recency` is only evaluated for the 4 `_VOLATILE` fields
  (dedup.py:183 short-circuits the rest), so `_recency` itself is called at most
  8× per `_merge` and is **not** a performance concern. `_trust_for`/`_sources_of`
  (dedup.py:148-152, 143-145) rebuilding a `set` from a `split` on every comparison is
  the actual cost, and it is on the critical path for 500k master rows.

## Recommended order of work

1. **Parse `scraped_at` in `_recency`** and rank on a real `datetime` (S2 finding 1).
   One function, removes the entire class of mixed-format survivorship bugs, and is the
   only change that alters what `_recency` *decides*.
2. **Guard `dedup`'s check-then-act** with a lock, or annotate `dedup.py:198` as
   single-thread-only (S2 finding 2). Cheap insurance before anyone shards the loop.
3. **Sort `dedup`'s output** so `all_businesses.csv` is stable across runs (S3 finding 3).
   Directly improves the master's auditability, which is the whole point of a cumulative
   master.
4. **Resolve the `website_live` / `_VOLATILE` contradiction** at `dedup.py:114` vs
   `dedup.py:168-171` (S3 finding 4) — either make the arbitration reachable or stop
   claiming it happens.