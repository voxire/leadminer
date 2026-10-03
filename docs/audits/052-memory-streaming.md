# 052 — Memory blowups: the whole pipeline is N full lists in RAM

## Verdict

Every stage of the pipeline holds its entire result set in memory at once, and
nothing is ever freed because `main()` keeps every intermediate list alive as a
local for the whole function. A record dict costs ~2.8 KB (measured, 22 keys +
string/number values), and the same record ends up resident **two to three times
simultaneously** — once in `master`/`raw`, once again as the `dict(record)` copy
inside `dedup`'s two indexes. At a 500k cumulative master + a 150k fresh scrape,
peak is ~3.6 GB of dicts; the master is "all time" (`all_businesses.csv`) so it
grows every run, and the run dies well before a 1M master on any single-digit-GB
runner. The fix is a streaming/chunked pipeline over a disk-backed dedup index
(SQLite), which is the one stage that genuinely cannot be a single forward pass.

## Findings

### S1 — `dedup` materializes a full second copy of every record, and `main` holds master + raw + raw_filtered + combined all at once
- **Where:** `main.py:95,99,117,124,125` and `dedup.py:113,115,123`
- **Breaks:** `dedup` does `dict(record)` on every input record to populate
  `phone_index` / `name_index` (`dedup.py:115,123`). The caller passes
  `combined = raw_filtered + master` (`main.py:124`), so the full set of
  (new + cumulative) records is copied a second time. Meanwhile `master`,
  `raw`, `raw_filtered`, and `combined` are all still bound locals — Python
  never drops them, and there is no `del`/`gc` anywhere in `main()`. Peak dict
  count is `M (master) + R (raw) + (F + M) (dedup copies)`.
- **Trigger:** 500k master + 150k fresh scrape (135k pass the whitelist). Peak =
  (500k + 150k + 635k) × ~2.8 KB ≈ **3.6 GB**, plus ~50–80 MB of enrichment
  futures on top. The master is cumulative, so this is a when-not-if at scale.
- **Fix:** Stream records into a disk-backed index (see pipeline below) instead
  of holding `master` in RAM and copying it in `dedup`. At minimum, `del master,
  raw, raw_filtered, combined` the instant each is consumed.

### S1 — Dedup's two full in-RAM indexes (phone_index + name_index) are the one stage that cannot be a forward stream
- **Where:** `dedup.py:102-137` (indexes at `dedup.py:103-104`, copies at
  `dedup.py:115,123`, `captured_phones` at `dedup.py:128`)
- **Breaks:** Dedup requires a *global* index keyed by normalized phone, with a
  fallback key `(normalized_name, city)` for records with no phone
  (`dedup.py:117-119`). It is inherently random-access: a phone at record #1
  must collide with the same phone at record #500,000. Two dicts are built, one
  per key space, each holding a full dict copy of every record. `captured_phones
  = set(phone_index.keys())` (`dedup.py:128`) then duplicates every phone key
  string a third time, and the second `normalize_phone` call at `dedup.py:133`
  re-allocates each number just to do a membership test.
- **Trigger:** 500k unique records → ~1.4 GB for the two index copies alone,
  before the output list and the input lists are counted.
- **Fix:** Replace the two dicts with a single SQLite table with `UNIQUE`
  indexes on `(normalized_phone)` and `(normalized_name, city)` and an
  upsert/merge. This is the disk-backed index the pipeline needs — not a
  streaming trick.

### S1 — Scrapers defeat their own `Iterator` contract by materializing full lists
- **Where:** `main.py:103` (`list(scraper.scrape())`), `google_places.py:170-188`
  (`all_records` + `seen_ids`), `osm.py:55`, `wikidata.py:58`
- **Breaks:** Every scraper returns `Iterator[BusinessRecord]` (they `yield`),
  but `main.py:103` wraps each in `list(...)`, so the streaming design is thrown
  away and all three sources land in `raw` at once. Google Places is the worst:
  `_scrape_query` already paginates (20/page, `google_places.py:203-277`) but
  `fetch_query` accumulates into a global `all_records` list under a lock
  (`google_places.py:179-181`) and a `seen_ids` set, then `yield from all_records`
  (`google_places.py:188`). OSM and Wikidata both call `resp.json()`, which
  decodes the entire HTTP body in memory (`osm.py:55`, `wikidata.py:58`).
- **Trigger:** OSM returns every Lebanese node + way in one POST; the decoded
  `elements` list and the full JSON string coexist for the lifetime of `scrape`.
- **Fix:** Consume generators incrementally (drop `list(...)`); have Google
  Places `yield` per page and dedup `seen_ids` against the disk index; stream-
  parse OSM with `ijson` over `iter_content`.

### S2 — `check_websites` submits every future up front and holds them all in a dict
- **Where:** `enricher.py:223` (`targets` list) and `enricher.py:230` (the
  `futures` dict)
- **Breaks:** `targets = [(i, r["website"]) for ...]` builds a list of one tuple
  per site, then `futures = {pool.submit(_fetch_website, url): idx ...}` submits
  **all** of them at once. The `ThreadPoolExecutor` queues 200k work items behind
  40 workers, and the `futures` dict pins every `Future` (and, via `as_completed`,
  every result) until the loop finishes. Per-request the body *is* capped
  (`_MAX_BODY_BYTES = 200_000`, `enricher.py:155,180`) — good — but the task
  queue itself is unbounded.
- **Trigger:** 500k records with ~40% having a website → ~200k simultaneous
  futures → tens of MB of `Future`/queue overhead, and every enriched record must
  stay in the resident `records` list to be mutated (`enricher.py:241-251`).
- **Fix:** Submit in a sliding window (bounded semaphore or
  `ThreadPoolExecutor.map(..., chunksize=...)`) and process records in chunks so
  only a chunk is resident.

### S2 — Five output CSVs are each written from a full resident list, and `with_social` is dead weight
- **Where:** `main.py:139-153`
- **Breaks:** `with_websites`, `without_websites`, `with_social`, `sales_ready`,
  and `qualified` are five more reference lists, which is cheap in pointers but
  means `records` cannot be released until all five `write_csv` calls finish.
  `with_social` (`main.py:141`) is never written to a file — it is computed and
  held only to print `len(with_social)` at `main.py:165`.
- **Trigger:** Any run large enough that `records` is the dominant allocation —
  it stays resident for the entire tail of `main`.
- **Fix:** Emit each CSV with a `SELECT ... WHERE` from the disk-backed store and
  stream rows out; drop `with_social` or compute its count with a counter.

## Streaming / chunked pipeline design

Goal: peak memory stays flat regardless of `N`, bounded by a chunk size and a
concurrency window. Current design keeps ~3× N dicts alive; the target is one
chunk (e.g. 10k records) + the disk index.

```
scraper A ─┐
scraper B ─┼─▶ (yield, never list) ─▶ whitelist filter ─▶ normalize keys
scraper C ─┘        per record               per record        per record
                                                                    │
                                        ┌───────────────────────────┘
                                        ▼
                         SQLite index (disk-backed): the one non-streamable stage
                         • table records(…22 cols…)
                         • UNIQUE(normalized_phone), UNIQUE(normalized_name, city)
                         • UPSERT w/ _merge logic; seen_ids lives here
                                        │
                                        ▼
                  chunked read ─▶ enrich (sliding-window website fetch, 40 workers)
                                        │
                                        ▼
                  per-row score/tag ─▶ write 5 CSVs via SELECT … WHERE (streamed)
```

Concrete stages and their memory profile:

1. **Scrape (stream).** Drop `list(...)` in `main.py:103`. OSM is the hard case —
   it is a single giant POST, so stream-parse with `ijson` over
   `resp.raw.read(...)` instead of `resp.json()` (`osm.py:55`); Wikidata is
   bounded (`LIMIT 5000`, `wikidata.py:26`) and can stay as-is; Google Places
   should `yield` per page and consult the disk index for `seen_ids` rather than
   a growing set (`google_places.py:170-172`).
2. **Filter (stream).** `raw_filtered` becomes a generator expression, not a list
   (`main.py:117`). Zero copy.
3. **Dedup (disk-backed index — cannot stream).** One SQLite database with a
   `records` table and `UNIQUE` indexes on `(normalized_phone)` and
   `(normalized_name, city)`. Port `_merge` (`dedup.py:83-99`) to an upsert:
   `INSERT ... ON CONFLICT(phone) DO UPDATE` (prefer the higher-`_field_count`
   side, union `source`, `max(scraped_at)`). Phone fallback ordering means a
   record with no phone goes to the name key; keep that exact rule as two
   indexes. This is the part that fundamentally needs random access and cannot
   be a single forward pass.
4. **Enrich (chunked + bounded concurrency).** Read records back from SQLite in
   chunks; within each chunk run the website check with a sliding window of
   ≤40 in-flight futures instead of submitting all at once
   (`enricher.py:229-230`). Mutate the chunk, write it back, release it.
5. **Score/tag (per-row).** The `main.py:131-137` loop is already per-record;
   run it inside the chunk loop.
6. **Output (streamed projections).** Replace `write_csv(path, list)` calls
   (`main.py:149-153`) with `SELECT … WHERE` queries: `all_businesses` = full
   table, `qualified` = `completeness_score >= 1`, `with_websites` /
   `without_websites` = website null/non-null, `sales_ready` = contact +
   priority filter. Stream each via `csv.writer`.

**Genuinely cannot stream → needs disk:**

- **Dedup** — global duplicate detection across the whole corpus; needs a
  random-access index (SQLite `UNIQUE` indexes), or an external-sort + merge of
  keyed chunks. No in-memory trick removes the full-corpus index.
- **The cumulative master** — "all time" accumulation (`all_businesses.csv`,
  `main.py:4,9`) grows unboundedly; it must live on disk (SQLite), not be
  re-loaded whole every run (`load_master`, `main.py:46-71`).
- **Overpass response** — one giant POST by design; the HTTP *response* is a
  single document, so even `ijson` only bounds the decoded side, not the wire
  payload. Accept a bounded buffer, or page the query spatially.

**Everything else** (filter, enrich, score, all five CSV writes, the
`by_region`/`by_service` tallies at `main.py:168-182`) is naturally streamable
and should be.

## Not a bug, but worth knowing

- `_fetch_website` already caps the response body at 200 KB and uses
  `stream=True` (`enricher.py:155,167,180`) — that per-request bound is correct
  and should be preserved in the rewrite.
- `normalize_phone` is called twice per phone record (`dedup.py:110` and
  `dedup.py:133`), re-allocating each E.164 string just for the
  `captured_phones` membership test. With 500k numbers that is transient GC
  pressure, not an OOM, but it disappears once dedup moves to SQLite.
- `_merge`'s `source` union (`dedup.py:92-94`) grows unboundedly only in the
  pathological case of a record appearing in all three sources repeatedly; not a
  memory risk at real data sizes.
- `raw_filtered` / `combined` / the five output lists are pointer arrays
  (~8 bytes/record), not dict copies — cheap in isolation; they only matter
  because they pin the shared dict objects alive for all of `main()`.

## Recommended order of work

1. Replace `dedup`'s two dicts with a SQLite-backed index (UNIQUE phone and
   (name, city), upsert/merge). This is the highest-leverage single change and
   the only stage that truly requires disk.
2. Make `master` live in that SQLite table instead of `load_master`
   (`main.py:46-71`) so the cumulative store is never fully re-loaded.
3. Remove `list(...)` from `main.py:103` and make Google Places yield per page
   against the disk index; add `ijson` for OSM.
4. Chunk the enrich stage and submit website futures in a bounded window
   (`enricher.py:229-230`).
5. Stream the five CSVs as `SELECT … WHERE` projections; drop the unused
   `with_social` list.
6. Add explicit `del`/`gc` at stage boundaries as a stopgap until 1–5 land.
