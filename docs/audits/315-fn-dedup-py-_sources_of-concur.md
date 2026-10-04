# 315 — `_sources_of` under the pipeline's concurrency

## Verdict

`_sources_of` (`dedup.py:143-145`) is **currently safe**, and safer than almost anything
else in the codebase, for one decisive structural reason: it is only ever reached from
`dedup()`, and `dedup()` has exactly one call site, `main.py:180`, which executes after
every thread pool in the process has already been joined. It holds no globals, opens no
resources, mutates nothing, and returns a freshly-allocated `set` on every call. The
three pools named in the brief cannot reach it. What *is* worth acting on is that this
safety is **positional, not intrinsic** — it depends entirely on the call graph, and the
one plausible future change (parallelising or sharding dedup per
`docs/audits/033-storage-sqlite-migration.md:143` and `docs/audits/035-incremental-scraping.md:391`)
silently breaks it in two distinct ways, one of which produces plausible-looking wrong
rows rather than a crash. Separately, `_sources_of` is load-bearing for `_trust_for`
(`dedup.py:148-152`) in a way that launders provenance, and it returns a `set` whose
iteration order varies per process under hash randomisation.

## Thread topology, as it actually exists

Worth stating precisely, because the target's safety depends on the *exit points*:

| Pool | Site | Threads | Touches `dedup.py`? |
|---|---|---|---|
| Outer scraper pool | `main.py:160` | 3 | No |
| Google Places inner pool | `scrapers/google_places.py:194` | 5 | No |
| Website liveness pool | `enricher.py:237` | 40 | No |

- **Outer pool** — `ThreadPoolExecutor(max_workers=len(scrapers))`. All three futures are
  drained by `as_completed` at `main.py:162-168`. The `with` block exits at `main.py:168`,
  which joins every worker. `dedup()` is called at `main.py:180`, twelve lines later.
- **Google inner pool** — created at `google_places.py:194`. Because `scrape()` is a
  generator (`scrapers/base.py:33`), its body executes inside the *outer* worker thread
  when `main.py:158` does `list(scraper.scrape())`. The inner `with` block exits and
  joins at `google_places.py:198`, before `yield from all_records` at
  `google_places.py:199`. So the inner pool is fully joined *before* the outer future
  even resolves.
- **Liveness pool** — `enricher.py:237`, reached via `enrich()` at `main.py:187`. That is
  **after** `main.py:180`. It cannot run concurrently with dedup; it consumes dedup's
  output.
- **Nested-pool deadlock check** — 3 outer threads each spawn their own inner pool; peak
  is 8 threads. There is no deadlock because inner work is submitted to the inner pool,
  never back to the outer pool, so no outer worker ever blocks waiting on a slot it holds.
  Not a bug; recorded so it is not re-litigated.

So there is **no instant at which a worker thread and `dedup()` are alive at the same
time.** That is the whole safety argument.

## Findings

### S1 — `dedup()` has no cross-shard coordination, so parallelising it silently emits duplicate rows

- **Where:** `dedup.py:198-199` (locals `phone_index`, `name_index`), `dedup.py:207-218`
  (unguarded read-modify-write), single call site `main.py:180`.
- **Severity rationale:** S1 because the failure mode is **wrong results that look
  correct** — no exception, no lost record count, just two rows for one business in
  `all_businesses.csv`, which is the cumulative master the sales team pitches from.
- **Breaks:** Both indexes are function-local, which makes a *single* `dedup()` call
  reentrant and safe. It also means the two indexes know nothing about each other or about
  any other invocation. If `main.py:180` is ever split into per-scraper or per-shard
  calls, each shard builds a disjoint index. A record that is a duplicate of one in
  another shard is invisible to it. `dedup.py:207-210` is a plain
  `if key in phone_index: ... else: phone_index[key] = dict(record)` — no lock, no
  shared registry, no post-merge reconciliation pass.
- **Trigger (verified locally, no network):**
  ```python
  records = [{"name": "Cafe Antoine", "phone": "+96170123456", "source": "osm"},
             {"name": "Le Petit",    "phone": "+96170123456", "source": "google_places"}]
  len(dedup(records))                                    # -> 1, source 'google_places|osm'
  # 2-way shard by index parity:
  out = dedup(records[0::2]) + dedup(records[1::2])
  len(out)                                                # -> 2, sources ['osm', 'google_places']
  ```
  Both rows carry the same phone. `sales_ready.csv` (`main.py:202-205`) will happily
  include both; the rep sees one business twice.
- **Aggravating detail specific to `_sources_of`:** the surviving `source` string is what
  a human uses to adjudicate a suspected duplicate ("Google says X, OSM says Y"). Once
  sharded, each row carries a *single* source, so the one column that would have exposed
  the bug is exactly the column the sharding destroys.
- **Fix:** Do not shard `dedup()`. If per-source parallel reconciliation is required
  (`docs/audits/033-storage-sqlite-migration.md:143`), put the identity index in one place
  — a SQLite `business` table with a UNIQUE constraint on the dedup key — so the
  uniqueness enforcement is in the storage engine's transaction, not in a Python dict.

### S2 — `dedup()` reads aliased values out of the shared input list; `records` is mutated in place after it returns

- **Where:** `dedup.py:210` and `dedup.py:218` (`phone_index[key] = dict(record)`),
  `_merge` at `dedup.py:190-194`, mutations at `main.py:183-184`, `main.py:189-197`,
  and `enricher.py:249-259`.
- **Severity rationale:** S2, not S1, because it cannot fire in the current call graph —
  `main.py:180` is the last reader of the scraper output before it becomes private, and
  the `dict(record)` copies at `dedup.py:210`/`218` mean the *returned* rows are fresh
  dicts, so `enricher.py:249-259` writing into them does not touch the input list. The
  gap is one scheduling change wide.
- **Breaks:** `dict(record)` is a **shallow** copy. The returned row shares every value
  *object* with the caller's record. Values are immutable today (`str | float | int |
  bool | None` per `scrapers/base.py:5-28`, and `csv.DictReader` produces only
  `str`/`None`/`float`/`int` at `main.py:86-103`), so this is currently harmless.
  Verified: `merged["extra"] is shared_list -> True` for a synthetic container-valued
  field. The moment any field holds a mutable container — which is exactly what the
  per-source contact/provenance design in `docs/audits/033-storage-sqlite-migration.md:127`
  and `:143` proposes — `_merge` hands the *same list object* to two different output
  rows. Then `enricher.py:252-259` (or any future per-row mutation) writing into one row
  mutates the other, and the corruption is invisible because both rows are "correct"
  until the second write.
- **Trigger:** a `website` field normalised to a list of URLs, or per-source `contacts`
  rows attached to a record; then any code path that mutates one merged row's container.
- **Fix:** Either deep-copy values in `_merge`, or (better) keep the invariant explicit:
  `BusinessRecord` values are scalars only. Enforce it with a type check in
  `scrapers/base.py` or at the export boundary so the aliasing trap cannot be sprung
  silently.

### S3 — `_sources_of` returns a `set`, whose iteration order varies per process

- **Where:** `dedup.py:145` (returns `{s for s in raw.split("|") if s}`).
- **Breaks:** `str` hash randomisation is on by default (`PYTHONHASHSEED` unset), so
  `set` iteration order for a fixed input differs between interpreter runs. Verified —
  three consecutive processes, same input `"osm|google_places|wikidata"`:
  ```
  ['osm', 'wikidata', 'google_places']
  ['google_places', 'osm', 'wikidata']
  ['wikidata', 'osm', 'google_places']
  ```
  Both current consumers are order-insensitive, so **nothing is broken today**:
  `_trust_for` takes `max()` over the set (`dedup.py:151`), which is commutative, and
  `_pick` wraps the union in `sorted()` (`dedup.py:174`), which is deterministic. The
  trap is that the safety is a property of the *call sites*, not of the return type. The
  first person who writes `"|".join(_sources_of(r))` instead of
  `"|".join(sorted(_sources_of(r)))` gets a `source` column whose byte content changes
  run to run, which will show up as a spurious diff in the cumulative master and defeat
  the output-hash manifest proposed in `docs/audits/100-run-manifest-design.md:15`.
- **Trigger:** `PYTHONHASHSEED` unset (the default) plus any unsorted join of the return
  value.
- **Fix:** Return `frozenset` (documents immutability, still unordered) **and** add a
  one-line comment at `dedup.py:145` stating that callers must `sorted()` before
  serialising. Or return a sorted `tuple[str, ...]`, which makes the requirement
  unrepresentable rather than merely documented.

### S3 — `_sources_of` launders provenance into `_trust_for`, and `_pick`'s recency term inherits the newest contributor's timestamp

- **Where:** `_sources_of` at `dedup.py:143`, consumed by `_trust_for` at
  `dedup.py:148-152` (reached from `rank` at `dedup.py:181`) and by `_pick` at
  `dedup.py:174`.
- **Breaks:** `source` is stored as a **union** of every source that has ever
  contributed to the row (`dedup.py:174`), and `_trust_for` then reads that union as
  "these sources vouch for this value". They are not the same claim. Verified:
  ```python
  m1 = _merge(osm_record, google_record)      # source 'google_places|osm'
  _trust_for("website", m1)                  # -> 3   (OSM's rating)
  m2 = _merge(m1, wikidata_record)           # source 'google_places|osm|wikidata'
  # m2["website"] is still the OSM value here, but if the wikidata value had won:
  _trust_for("website", m2)                  # -> 3   even though wikidata's trust is 2
  ```
  Once any OSM-sourced row has been merged, every subsequent merge of that row is scored
  at OSM's trust level for *all* fields OSM is trusted on (`email`, `website`,
  `facebook`, `instagram`, `whatsapp`) regardless of which source actually supplied the
  candidate value. Trust escalates monotonically with the number of merges, and the
  ceiling is set by the most trusted source that ever touched the row.
- **Same mechanism, second field:** `scraped_at` is `max()` over contributors
  (`dedup.py:176-177`), and `rank` uses that single timestamp as the recency term for
  every volatile field (`dedup.py:183`). Verified: merging a Google `rating` of 2.0
  stamped `2020-01-01` with an OSM record stamped `2026-09-01` yields
  `rating=2.0, scraped_at='2026-09-01...'` — the stale reading survives *because* the
  row now carries the fresh timestamp.
- **Severity rationale:** S3 under this lens because it is **deterministic**, not racy —
  I verified all 6 permutations of a 3-record merge collapse to exactly one outcome, and
  all 6 permutations of the input list to `dedup()` produce byte-identical output. It is
  filed here because it is the semantic consequence of what `_sources_of` returns, and
  because a parallel dedup (S1 above) would make the escalation order-dependent instead.
- **Trigger:** a row that OSM has enriched once, re-merged every run against the master
  CSV (`main.py:179`). Its `source` grows monotonically and its per-field trust is
  permanently pinned at OSM's 3.
- **Fix:** Track provenance per field, not per row — the `contacts`/`sources` table split
  in `docs/audits/033-storage-sqlite-migration.md:127` already models this. `_trust_for`
  should score the source that *supplied the candidate value*, which requires carrying
  `(value, source, observed_at)` triples through `_merge` instead of a union string.

## Not a bug, but worth knowing

- **`_sources_of` cannot observe a torn read in CPython 3.12.** It performs exactly one
  `dict.get` (`dedup.py:144`) and one `str.split`. `dict.get` is a single C-level call
  that does not release the GIL, so it cannot interleave with another thread's
  `__setitem__`. Stress-tested: 2 mutator threads doing 200k `__setitem__` each against
  2 reader threads calling `_sources_of` 200k times each — zero exceptions, zero wrong
  values. The classic `RuntimeError: dictionary changed size during iteration` requires
  *iteration*, which `_sources_of` does not do (it iterates the list returned by
  `split`). I am recording this because "reads a dict another thread writes" normally
  earns an S1 reflexively, and here it does not.
- **`.get("source") or ""` also absorbs a concurrent writer leaving `source` unset or
  `None`.** `dedup.py:144` treats both as empty, so the worst case under a race is a
  silently dropped trust bonus, not a crash.
- **`_sources_of` holds no resource.** No socket, no file, no lock, no session, no lazy
  initialisation. Resource lifetime is trivially correct — there is nothing to leak and
  nothing whose lifetime can outlive a caller. Contrast `httpclient.py:58`, where
  `threading.local()` Session lifetime *is* tied to pool-thread lifetime: the 40
  `enricher.py` worker threads get 40 Sessions that are never explicitly closed and are
  reclaimed only on GC at pool shutdown. Not a `_sources_of` concern, and not a bug.
- **Google Places' shared state is correctly guarded, for contrast.** `seen_ids` is
  mutated under `seen_lock` (`google_places.py:182`, `263-266`) and `all_records` under
  `records_lock` (`google_places.py:184`, `191-192`); each of the 5 workers builds its
  own Session (`google_places.py:189`, per the comment at `:188`). `dedup.py`'s two
  indexes need the same discipline only if they ever become shared.
- **The liveness pool never sees a record dict.** `enricher.py:231` snapshots
  `(index, url)` pairs and `enricher.py:238` submits the **url**, not the record.
  Workers return tuples; the main thread performs every record mutation at
  `enricher.py:249-259`. So the 40-thread pool has no shared-dict exposure at all — this
  agrees with `docs/audits/004-enricher-thread-safety.md:26`.
- **`dedup()` does not mutate its input list or its input dicts.** `dedup.py:210`/`218`
  store copies; `_merge` (`dedup.py:191`) builds a fresh dict. Verified: calling
  `dedup()` twice from two threads over the *same* 500-record list produces equal results
  and leaves `recs[0]` unchanged. `dedup()` is therefore genuinely reentrant — the single
  strongest thing to say in this function's favour.
- **`main.py:166` `raw.extend(batch)` order is nondeterministic** (`as_completed` at
  `main.py:162`), so `combined` at `main.py:179` is ordered by whichever scraper finished
  first. This *looks* like it should make the merge order-dependent. It does not, because
  `_pick`'s `rank` is a total order whose final tiebreak is `str(value)` (`dedup.py:184`)
  and `">="` ties are value-equal, so the merged output is a function of the input
  *multiset*. Verified: 6 permutations -> 1 distinct outcome. Do not "fix" this by
  sorting `raw`; that would be churn.
- **`_pick` returns the winning `source` via `sorted()` at `dedup.py:174`, so the union is
  idempotent and commutative.** Re-merging the same row against the master CSV
  (`main.py:179`) cannot grow `source` without bound — `"google_places|osm"` re-merged
  with `"osm"` stays `"google_places|osm"`.

## Recommended order of work

1. **Keep `dedup()` single-threaded and say so in the code.** It is the only structural
   guarantee protecting `_sources_of`, `_trust_for` and `_merge`. A two-line comment at
   `dedup.py:197` stating "not thread-safe by design: identity resolution is a
   single-writer operation; parallelism belongs in the storage layer" costs nothing and
   is the cheapest insurance in this report.
2. **Route any future parallel reconciliation through a UNIQUE constraint** in the
   SQLite `business` table (`docs/audits/033-storage-sqlite-migration.md:127`). That
   makes the S1 sharding failure impossible to express rather than merely discouraged.
3. **Carry provenance per field** — `(value, source, observed_at)` — so `_trust_for`
   scores the source that actually supplied the candidate. This retires both the trust
   escalation and the recency inheritance in one change, and is a prerequisite for the
   `contacts`/`sources` schema already planned.
4. **Document the `sorted()` requirement at `dedup.py:145`**, or change the return type
   to a sorted tuple so the S3 hash-order trap cannot be sprung.
5. **Add the missing invariants to the test suite** while the semantics are still
   cheap to pin: `_sources_of` returns a fresh object each call; mutating it does not
   affect the record; `dedup()` output is invariant under input permutation (the
   property `main.py:166`'s `as_completed` ordering depends on and currently gets for
   free).
