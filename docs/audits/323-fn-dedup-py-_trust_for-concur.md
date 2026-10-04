# 323 — `_trust_for` (dedup.py:148) under concurrency

## Verdict

**`_trust_for` is currently safe, and the reason is structural rather than lucky: it is
reachable from exactly one call site, `main.py:180`, which runs on the main thread after
all three thread pools have been joined and before the 40-thread pool is created. It
writes nothing — its only shared state is `_SOURCE_TRUST`, which has no writer anywhere in
the repo. There is no race, no shared-mutable-dict hazard, and no resource-lifetime problem.**

The interesting finding is not a concurrency bug at all: it is that `rank()` at
`dedup.py:180-185` compares **trust before validity**, so a syntactically broken value
from a high-trust source permanently masks a valid value from a lower-trust source. That
is a wrong-result bug in the output CSV (S1). Second, `dedup()` keys on `country` one
pipeline stage before `resolve_country` normalises it (S2).

## The concurrency regions, and why none of them touch `_trust_for`

### Region A — 3 scrapers, `main.py:160-168`

```python
with ThreadPoolExecutor(max_workers=len(scrapers)) as pool:   # main.py:160
    futures = {pool.submit(run, s): s for s in scrapers}       # 161
    for future in as_completed(futures):                       # 162
        name, batch = future.result()                          # 164
        raw.extend(batch)                                      # 166
```

Scrapers never share a Python object. They communicate only by returning a value through
`Future.result()` (`main.py:164`), and `raw.extend` (`main.py:166`) runs on the main
thread. `ThreadPoolExecutor.__exit__` calls `shutdown(wait=True)`, so line 168 is a join
of all three worker threads.

### Region B — 5 Google workers, `google_places.py:194-198`

Nested inside Region A's Google thread; drained by `list(scraper.scrape())` at
`main.py:158`. Its `with` block exits (joining all five) before
`yield from all_records` (`google_places.py:198-199`), so nothing escapes lazily.

Its two pieces of shared state are both correctly guarded:

- `seen_ids` — check-and-insert is inside `seen_lock`, with the `continue` also inside the
  lock (`google_places.py:263-266`). Atomic and correct.
- `all_records` — `list.extend` under `records_lock` (`google_places.py:191-192`).

Sessions are per-query (`google_places.py:189`, one `requests.Session` per query), so the
five workers never share one. No lock needed and none present.

### Region C — 40 website workers, `enricher.py:237-262`

```python
futures = {pool.submit(_fetch_website, url): idx for idx, url in targets}  # enricher.py:238
...
r = records[idx]                                                          # 249
r["website_live"] = ...                                                   # 250
if not r.get("email") and contacts["email"]:                              # 252
    r["email"] = contacts["email"]                                        # 253
```

**This is the load-bearing detail, and it is deliberate:** workers receive only the URL
*string* (`enricher.py:238`) and build a fresh `contacts` dict per call
(`enricher.py:168`). Every write to `records` happens on the **main** thread inside the
`as_completed` loop (`enricher.py:249-259`). No record is ever touched by a worker.

### Where `_trust_for` actually runs

```
_trust_for (dedup.py:148)
  <- rank (dedup.py:181)  x2 per _pick
  <- _pick (dedup.py:193)
  <- _merge (dedup.py:208, 216)
  <- dedup (dedup.py:197)
  <- main.py:180        <-- the ONLY call site (grep: `dedup(` appears once)
```

`main.py:180` is after Region A's join (`main.py:168`), after Region B's transitive join,
and before `enrich()` → `check_websites` (`main.py:187` → `enricher.py:337` →
`enricher.py:225`). **Exactly one thread exists at that moment.** No race is possible.

### Shared mutable state that `_trust_for` touches

| State | Location | Written? |
|---|---|---|
| `_DEFAULT_TRUST` | `dedup.py:111` | No writer in repo |
| `_SOURCE_TRUST` | `dedup.py:97-110` | No writer in repo (grep: 2 refs — the literal at `:97`, the read at `:151`) |
| `record["source"]` | read via `dedup.py:144` | Never mutated by `_trust_for` |
| `{}` default arg | `dedup.py:151` | Fresh dict per call, never stored |

`_merge` builds a **new** dict (`dedup.py:191-194`) and the index stores
`dict(record)` shallow copies (`dedup.py:210`, `:218`). Verified: mutating the merged
output left both inputs untouched. `phone_index`/`name_index` (`dedup.py:198-199`) *are*
mutable, but they are function-local and unreachable outside the call.

`dedup.py` imports only `re` and `unicodedata` — no threading primitives, no module-level
memoisation cache. `_trust_for` is pure and reentrant with respect to module state.

---

## Findings

### S1 — Trust is compared *before* validity, so a malformed value from a high-trust source permanently masks a valid value from a lower-trust source

- **Where:** `dedup.py:180-185` (the `rank` tuple order), with `dedup.py:148-152` supplying the top axis.
- **Breaks:** `rank` is a lexicographic tuple `(trust, validity, recency, str(value))`.
  Trust is decided **first**. `_validity` can return a gap of 3 (`:125`, `:127`, `:133`,
  `:139` return 3 for valid and 0 for invalid), but a trust gap of **1** is enough to
  override it, because trust is the higher-priority axis and the tuple never reaches
  validity. `_validity` is structurally incapable of vetoing a trust win.

  This bites because the trust table and the validity check disagree about which source
  is reliable. OSM scores 3 for `email` (`dedup.py:103`); Wikidata scores 2
  (`dedup.py:107`). OSM's email is raw, unvalidated, community-edited free text
  (`scrapers/osm.py:90`: `tags.get("email") or tags.get("contact:email")`). So a single
  bad `contact:email` tag on Overpass outranks a good Wikidata `P968` value forever, and
  the regex guard that exists precisely to catch this (`_EMAIL_OK`, `dedup.py:116`) can
  never overrule it.
- **Trigger (verified):**
  ```python
  osm = {'name':'X','email':'shop@nowhere','source':'osm',
         'scraped_at':'2026-02-01T00:00:00+00:00'}
  wd  = {'name':'X','email':'real@real.com','source':'wikidata',
         'scraped_at':'2026-01-01T00:00:00+00:00'}
  _pick('email', osm, wd)   # -> 'shop@nowhere'
  ```
  Ranks: `_validity('email','shop@nowhere') == 0`, `_trust_for('email',osm) == 3`
  → `(3, 0, '', 'shop@nowhere')`; valid side → `(2, 3, '', 'real@real.com')`.
  `3 > 2` on axis 0 decides it. Verified **order-independent** — the reversed call returns
  the same junk value, so it is not a merge-ordering bug, it is a priority bug.
- **Downstream damage (verified):** `'shop@nowhere'` is truthy, so
  - `enricher.py:104` grants `+1` to `completeness_score`, which is the exact gate for
    `qualified_businesses.csv` (`main.py:206`) — a fake contact qualifies the row;
  - `enricher.py:289` grants `+20` to `lead_score`;
  - `has_any_contact` (`main.py:142`) is `True`, because `'@' in 'shop@nowhere'` and
    `len('shop@nowhere') > 5` — so the row lands in `sales_ready.csv`
    (`main.py:202-205`) with an undeliverable address as its only contact channel.
- **Fix:** make validity the higher-priority axis: return
  `(validity, trust, recency, str(value))` at `dedup.py:180-185`. For the four fields
  `_validity` actually discriminates (`email`, `website`, `lat`/`lon`, `rating`) this
  strictly dominates — no case improves by putting trust first. For all other fields
  `_validity` returns a constant (`1`, or `-100` which is already short-circuited by
  `dedup.py:168-171`), so the change is a no-op there. Better still: carry per-field
  provenance so `_trust_for` can answer "did *this* source supply *this* field" instead of
  "did *some* source in the group once have an opinion about it".

### S2 — `dedup()` keys on `country` one stage before `resolve_country` normalises it, splitting single KSA businesses into duplicate rows

- **Where:** `main.py:180` (dedup) runs before `main.py:183-184` (`resolve_country`).
  Also `main.py:87-89` (`load_master` blank→`None`, otherwise verbatim).
- **Breaks:** `dedup()` computes the phone key from
  `record.get("country") or "LB"` (`dedup.py:205`, and again at `:228`). `load_master`
  only casts floats/ints and `website_live` — it never normalises `country`, so free text
  like `KSA`, `SAU`, `SAU-AR`, `LEB` reaches `dedup` untouched. `_COUNTRY_CODES.get("KSA",
  "961")` falls back to Lebanon (`dedup.py:48`). `resolve_country` — whose own docstring
  at `main.py:52-58` explains exactly this failure — is called eight lines too late, at
  `main.py:184`.
- **Trigger (verified):**
  ```python
  master = {'name':'Falafel House','category':'restaurant','country':'KSA',
            'phone':'0501234567','address':'Riyadh','source':'wikidata', ...}
  fresh  = {'name':'Falafel House','category':'restaurant','country':'SA',
            'phone':'+966501234567','address':'Riyadh','source':'google_places', ...}
  dedup([master, fresh])   # -> 2 rows, one business
  ```
  `normalize_phone('0501234567','KSA') == '+961501234567'` vs
  `normalize_phone('0501234567','SA') == '+966501234567'`.
- **Why this survives spot checks:** if the number carries its own country code
  (`+966…`, `00966…`), `dedup.py:58-60` self-corrects and the bug is masked. It only bites
  bare national numbers on rows whose `country` cell is a non-canonical string — i.e.
  exactly the master rows `resolve_country` was written to rescue.
- **Fix:** hoist the normalisation into a pass immediately before `main.py:180`:
  `for r in combined: r["country"] = resolve_country(r)`, then drop the `r["country"]`
  write at `main.py:184`.

### S3 — `_sources_of` does not strip, so one hand-edit in Google Sheets silently disables the entire trust table

- **Where:** `dedup.py:145`, consumed at `dedup.py:151`.
- **Breaks:** `raw.split("|")` with no `.strip()`, so `"google_places | osm"` yields
  `{' osm', 'google_places'}`. Verified: website trust drops 3→1, email trust drops 3→1.
  `"google_places, osm"` (Sheets converting the pipe to a comma) → 1. `"KSA"`-style
  country problems aside, the whole point of `_SOURCE_TRUST` switches off with **no error
  and no log line** — merges silently fall back to a bare `_validity` comparison.
- **Why it matters more than it looks:** the five CSVs are the product and are
  rclone-synced to Google Drive, i.e. they get opened and edited in Sheets by humans.
  `|` is one keypress from `,`, and the merged source string it produces is written by
  `_pick` at `dedup.py:174` and read back verbatim from the master CSV at `main.py:86`.
- **Fix:** `{s.strip().lower() for s in raw.split("|") if s.strip()}` at `dedup.py:145`.

### S3 — `_SOURCE_TRUST` is mutable by construction with no freeze; a future writer turns `_trust_for` into a race read

- **Where:** `dedup.py:97-111`.
- **Today:** safe. Grep finds exactly two references in the repo — the literal
  (`dedup.py:97`) and the read (`dedup.py:151`). There is no writer.
- **The hazard:** it is module-global mutable config holding *nested* dicts, with nothing
  preventing a write. The obvious next feature ("tune trust weights from config/CLI")
  would make `_trust_for` the read side of a data race the moment dedup is parallelised.
  Rebinding a top-level key is atomic under the GIL, but an in-place
  `_SOURCE_TRUST["osm"]["email"] = 5` or a `dict.update()` loop is not — readers can
  observe a half-applied table and score the same field with old weight on one record and
  new weight on the next.
- **Fix:** type it as
  `_SOURCE_TRUST: Mapping[str, Mapping[str, int]] = MappingProxyType({...})` so an
  accidental write raises instead of silently corrupting merges. While there, hoist the
  `{}` default at `dedup.py:151` to a module constant — it currently allocates a fresh
  empty dict on every source miss, which is every call for any row carrying an
  unrecognised source string (see the S3 above).

### S3 — `_merge` shallow-copies, so the first non-scalar field added will be aliased across the index and the output

- **Where:** `dedup.py:191-194`, `dedup.py:210`, `dedup.py:218`.
- **Today:** no live bug. Verified that `_merge(a, b)["nested"] is a["nested"]` →
  `True`, but every `BusinessRecord` value is a scalar (`scrapers/base.py:5-28`), so
  nothing nested is ever shared. The shallow copies at `dedup.py:210`/`:218` are
  sufficient *only* because of that invariant.
- **Why it will break:** `enrich()` mutates records in place afterwards
  (`enricher.py:349-350` sets `completeness_score`/`lead_score`;
  `enricher.py:250-259` sets `website_live` and four contact fields), and `main.py:183-197`
  mutates them again. The moment a `categories: list[str]` or `sources: dict` field is
  added, two rows in `phone_index` will share one object, `write_csv` will happily emit
  the same list twice (`main.py:127`), and in-place enrichment will write through the
  alias.
- **Fix:** `copy.deepcopy(record)` at `dedup.py:210`/`:218`, or document the
  scalar-only invariant on `BusinessRecord` and add an assertion in `_merge`.

---

## Not a bug, but worth knowing

- **The 40-thread pool does not mutate shared records.** This is the single fact that
  makes `dedup` safe, and it is easy to destroy by accident. Workers get only the URL
  string (`enricher.py:238`); all writes happen on the main thread
  (`enricher.py:249-259`). If anyone moves `r["website_live"] = ...` into
  `_fetch_website`, distinct-`records[idx]`-slot writes are probably still fine under
  CPython — but `enricher.py:252` (`if not r.get("email")` then assign) is a
  read-modify-write that is **not** atomic, and it would race against 39 workers on a
  list of dicts that `dedup` later reads. Add a comment on `check_websites` stating that
  workers must not touch `records`.
- **Sessions are handled correctly in both places, for the right reasons.**
  `httpclient.get_session()` is `threading.local`-backed (`httpclient.py:58-71`), so the
  40 workers never share a `requests.Session`. `GooglePlacesScraper._make_session()` is
  called once per query (`google_places.py:189`), so the 5 workers never share one either.
  Neither needs a lock; neither has one.
- **Resource lifetime works out, by luck of thread teardown.** The thread-local Sessions
  are never explicitly closed. It is fine because the pool threads are joined at the end of
  the `with` (`enricher.py:262`) and `threading.local` state dies with the thread, so at
  most 40 Sessions per run become collectable garbage. Note the cost though: one Session
  per query (`google_places.py:189`) means 68 separate urllib3 pools and 68 TLS handshake
  chains instead of 5, which is pure overhead against the same 429 budget the comment at
  `google_places.py:139-142` is trying to protect. A thread-local in `GooglePlacesScraper`
  would restore reuse.
- **The generator boundary is currently correct and load-bearing.** `scrape()` is drained
  by `list(...)` on a pool thread (`main.py:158`), and the nested executor's join happens
  before the final `yield from` (`google_places.py:198-199`). If anyone drops the `list()`
  at `main.py:158`, the join would move onto whichever thread drains the generator, and
  `raw.extend` (`main.py:166`) could observe partial state.
- **`google_places.py`'s locks are belt-and-braces but correct.** `list.extend`
  (`google_places.py:192`) is already atomic under the GIL, and the
  `records_lock` is harmless. The `seen_lock` at `google_places.py:263-266` is genuinely
  required — the check-and-insert would otherwise double-emit a place that two concurrent
  queries both returned, and the `continue` correctly sits *inside* the lock.
- **Interleaved output.** `google_places.py:257` prints from 5 workers without
  `flush=True`, unlike `osm.py:51` and `wikidata.py:82`. Progress lines buffer and appear
  in bursts. Cosmetic only — the query text in the message makes lines attributable.
- **`_VOLATILE` (`dedup.py:114`) is narrower than it looks.** All three live scrapers emit
  `website_live=None` (`google_places.py:292`, `osm.py:109`, `wikidata.py:121`), so the
  `website_live` entry is only ever exercised on master rows reloaded from CSV
  (`main.py:102-103`). Not dead — just worth knowing that the `website` recency axis
  compares an enricher-set value from the last run against a fresh scrape in this one.
- **Forward-looking perf note on my own target.** `_trust_for` is the top of the `rank`
  tuple, so it runs **twice per field per merge** — the hottest CPU path in the pipeline,
  and single-threaded. If anyone parallelises `dedup`, the correct decomposition is to
  shard `records` and give each worker its own `phone_index`/`name_index`
  (`dedup.py:198-199`), merging shards afterwards. `_trust_for` itself is already pure and
  needs no change; the *trust table* is the only thing that must stay immutable (S3 above).

## Recommended order of work

1. **S1** — swap the `rank` tuple at `dedup.py:180-185` to `(validity, trust, recency,
   value)`. One line, strictly dominant, and it makes `_validity` actually able to veto a
   garbage value. Add the `_pick('email', osm, wd)` case above to the test file.
2. **S2** — hoist `resolve_country` to immediately before `main.py:180`. One moved loop.
3. **S3 (strip)** — `.strip().lower()` in `_sources_of` at `dedup.py:145`.
4. **S3 (freeze)** — `MappingProxyType` on `_SOURCE_TRUST`, and hoist the `{}` default.
   Cheap insurance before anyone adds a weight-tuning feature.
5. **S3 (deepcopy)** — decide scalar-only vs deepcopy *before* a nested field is added,
   and write the invariant into `BusinessRecord`.
6. Comment `enricher.py:225` with the invariant that workers must not touch `records` —
   it is the only thing standing between this codebase and a real race in Region C.