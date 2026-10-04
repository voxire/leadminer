# 316 — `_sources_of` contract: the column is a derived field stored in an input field, and nothing type-checks it

## Verdict

`_sources_of` (`dedup.py:143-145`) declares `record: dict` — and that bare `dict` is the whole
problem. `BusinessRecord` (`scrapers/base.py:5-28`) is imported by exactly four modules: itself and
the three scrapers. `dedup`, `enricher`, `main`, `whitelist` all take untyped `dict`;
`pitch_recommender.py:25` takes `Mapping`. The canonical schema never crosses the module boundary,
so the function that parses the pipeline's only provenance column is written against a type that
cannot see the schema. Worse, the one place the schema *is* wrong is exactly this column:
`BusinessRecord.source` is declared non-optional `str` (`base.py:26`), but `load_master`
(`main.py:87-89`) turns every blank CSV cell into `None`, so the real runtime type on the master
path is `str | None`. **The declared input type is wrong, and the wrongness is invisible because
the annotation opts out of the schema that would have caught it.** Compounding this, the sibling
writer `_pick` (`dedup.py:173-174`) can emit a `source` value that appears in **neither** of its
inputs, and that value is then written to the cumulative master where it is irreversible.

Overlaps already reported: `313` (edge cases), `319` (provenance ratchet, fabricated multi-source,
trust-by-co-membership, unknown-token downgrade), `350` (stringly-typed trust keys). This report
owns the type/serialisation contract and does not restate them.

---

## The three questions, answered directly

**Q1 — declared vs. actual input type.**

| | Declared | Actual in practice |
|---|---|---|
| parameter | `record: dict` (`dedup.py:143`) = `dict[Any, Any]` | `BusinessRecord` at the three `yield` sites; `dict[str, str \| None \| float \| int \| bool]` after `load_master` (`main.py:86-104`); `_merge`'s own output is `dict[str, object]` built from `set(a) \| set(b)` (`dedup.py:192`) |
| `record["source"]` | `str` (`base.py:26`, **non-optional**) | `str` for fresh scrapes (`osm.py:119`, `wikidata.py:127`, `google_places.py:302`); `None` for **every blank master cell** (`main.py:87-89`); `""` for a merged-unparseable value; any object at all from a hand-built record |
| return | `set[str]` | `set[str]`, always — the `str()` at `dedup.py:144` makes every token a `str`, so `sorted()` can never fail on mixed types. But the *elements* are unvalidated free text, not the three values the function's consumers assume |

The declared `dict` is both **too loose** (it admits any key set and any value type, so the
`None` source is invisible) and **too narrow in spirit** (it erases the schema that would tell a
reader where `source` is supposed to come from). It is also unenforced: `mypy` is configured
`strict = true` (`pyproject.toml:64-68`) and **never invoked** — `scrape.yml:36-40` installs only
`requirements.txt` and the workflow has no lint or typecheck step. Strict mode is decorative.

**Q2 — can a caller violate the contract silently?** Yes, in five distinct ways, none of which
raise: a missing key, a `None` cell, casing drift, whitespace, and a non-`|` delimiter. All five
land in the same place — `_sources_of` returns a token set the trust table does not recognise, and
`_trust_for` falls through to `_DEFAULT_TRUST = 1` (`dedup.py:111`, `dedup.py:151`) with no log, no
counter and no warning. The one thing that *does* raise — passing a non-mapping, which gives
`AttributeError: 'list' object has no attribute 'get'` from inside `_pick` — is the correct
behaviour happening by accident, not by design. (Also reported as S3 in `319`.)

**Q3 — fields read or written that are not in the `BusinessRecord` TypedDict.**
`_sources_of` itself reads **exactly one** key (`source`, `dedup.py:144`) and writes **nothing**,
and `source` *is* in the TypedDict (`base.py:26`). So: **zero, directly.** The interesting answer
is the surface one level out. `_sources_of`'s return value is consumed by `_trust_for(field, rec)`
and `_pick(field, a, b)`, whose `field` argument is **unconstrained**: `_merge` calls `_pick` once
per key of `set(a) | set(b)` (`dedup.py:192-193`), and for master rows that key set is whatever the
CSV header happens to be (`csv.DictReader`, `main.py:86`) — never validated against `FIELDS`
(`main.py:33-40`) or `BusinessRecord`. Verified: add an `osm_id` column to the master and
`_trust_for("osm_id", record)` returns `1` from the default, the column gets merged like a real
field, and then it is **silently dropped at write time** by `extrasaction="ignore"`
(`main.py:125`). See S2-4.

**Q4 — every use of the return value.** There are exactly two, and **neither can raise**:

| Use | Can it break? | How |
|---|---|---|
| `_trust_for`, `dedup.py:150-151` | No. Iterates the set; `_SOURCE_TRUST.get(src, {})` accepts any string. | Degrades silently to `_DEFAULT_TRUST = 1` |
| `_pick`, `dedup.py:174` | No exception, but **yes, semantically**. | `"|".join(sorted(...))` can return `""`, a value present in neither input (S1-1); and it re-serialises unnormalised tokens straight into the cumulative master (S2-1) |

Everything else that cares about "multi-source" re-derives the concept independently and never
calls `_sources_of`: `enricher.py:316` (`"|" in str(source)`), `cli.py:120` (a `Counter` over whole
`source` strings). See S3-1.

---

## Findings

### S1 — `_pick` can write a `source` value that exists in neither input, and the master makes it permanent

- **Where:** produced at `dedup.py:173-174` (`"|".join(sorted(_sources_of(a) | _sources_of(b)))`),
  written by `write_csv` at `main.py:209`, re-read by `load_master` at `main.py:150`.
- **Breaks:** For every field in the record, `_pick` can only ever return a value that was present in
  one of the two inputs — that is the invariant the whole merge engine rests on, and
  `tests/test_lead_signal.py:321-326` (`test_no_field_is_lost`) encodes it. `_sources_of` breaks it.
  `_is_missing("||")` is `False` (`dedup.py:88-89`: it is a `str` whose `.strip()` is non-empty), so
  the early returns at `dedup.py:168-171` do **not** fire, both sides are "present", and the union
  of two empty token sets is joined into `""`. Verified through the real
  `write_csv` → `load_master` path, not just in memory:

  ```
  _merge({"source": "||"}, {"source": "|"})["source"]      -> ''      # in neither input
  write_csv writes an empty source cell
  load_master returns source=None (NoneType)               # main.py:87-89
  _sources_of(loaded)                                      -> set()
  _trust_for("category", loaded)                           -> 1       # was 2 (osm)
  ```

  So one character of corruption does three irreversible things at once: it destroys the fact that
  two observations were fused, it turns "known provenance" into "no provenance", and it drops that
  record to `_DEFAULT_TRUST = 1` for every field on every future run — because `""` reloads as
  `None`, and `None` reloads as `None`, forever.
- **Trigger:** any `source` cell that is non-blank but contains no token — `"|"`, `"||"`, `" | "`,
  or a cell where a spreadsheet paste put the delimiter in and nothing around it. This arrives from
  the master CSV boundary, the same threat model `319` establishes for this column.
- **Why S1:** silent data loss in the cumulative master, provable from two inputs, unrecoverable
  without manual surgery on the Drive copy (`scrape.yml:88-89`) — and the next run re-corrupts it,
  because `None` is not "missing", it is "a source named nothing".
- **Fix:** never let the derivation produce less than its inputs —
  `"|".join(sorted(...)) or (av if av else bv)` at `dedup.py:174`.
- **Note for the implementer:** the correct long-term fix is to stop storing a set in a string
  column (see "Not a bug" → *the encoding is not injective*). But the one-liner above stops the
  bleeding and is safe to land first.

### S2 — `source` is re-serialised, not preserved, so unparseable tokens are written into the master and accumulate without bound

- **Where:** `dedup.py:174`; persisted `main.py:209`; re-read `main.py:150`; counted `cli.py:120`.
- **Breaks:** `"|".join(sorted(...))` has no `.strip()`, no `.lower()` and no membership check
  against `_SOURCE_TRUST`. Its output is therefore **not a canonical form of its input**, so the
  file the next run reads is strictly worse than the file the previous run wrote, and the
  degradation is monotone. Verified — three merges in:

  ```
  start        'osm,wikidata'
  + google     'google_places|osm,wikidata'
  + osm        'google_places|osm|osm,wikidata'
  + wikidata   'google_places|osm|osm,wikidata|wikidata'
  tokens       {'wikidata', 'google_places', 'osm', 'osm,wikidata'}   # one junk token, permanent
  ```

  Two distinct junk shapes that `sorted()` cannot converge: a trailing space never merges into its
  clean twin, because `sorted({'osm ', 'osm'})` → `['osm', 'osm ']` (verified:
  `_merge({"source": "osm "}, {"source": "osm"})["source"] == 'osm|osm '`). Embedded newlines
  survive too — `_merge({"source": "osm\nwikidata"}, {"source": "google_places"})["source"]` is
  `'google_places|osm\nwikidata'`, which `write_csv` quotes into a single cell but which
  `scrape.yml:58` and `scrape.yml:78` count as two physical lines, inflating the `ROWS` figure that
  is the **only publish gate** in the workflow (`scrape.yml:80-83`).
- **Trigger:** one space in one cell in `all_businesses.csv`.
- **Detection:** `leadminer stats` "by source" (`cli.py:120`) counters whole `source` strings, so
  each junk variant becomes its own bucket — the symptom is visible only as a slowly growing tail
  of one-row buckets. `leadminer validate` (`cli.py:135-201`) never reads the column at all.
- **Fix:** canonicalise inside `_sources_of` (`{t.strip().lower() for t in raw.split("|") if
  t.strip()}`) and reject unknown tokens with a warning; add a `source`-column check to
  `cmd_validate`.

### S2 — `_DEFAULT_TRUST = 1` makes "unknown provenance" indistinguishable from a real source, and the single field where they collide is `category` — the `sales_ready` gate

- **Where:** `dedup.py:111`, consumed `dedup.py:151`; decides `dedup.py:184`'s `str(value)`
  tiebreak; `category` feeds `industry_priority` (`whitelist.py:133-145`) and therefore
  `sales_ready.csv` (`main.py:202-205`).
- **Breaks:** flattening `_SOURCE_TRUST` (`dedup.py:97-110`) shows **exactly one** declared value
  equals `_DEFAULT_TRUST = 1`: `("wikidata", "category")` at `dedup.py:108`. For `name`, `address`,
  `phone`, `email`, `website`, `lat`, `lon`, `rating`, `review_count` an unknown source is
  strictly *less* trusted than any real one, so it loses cleanly. For `category` — the one field
  that decides whether a lead is in the sales product — a record with a corrupted `source` is
  trusted **exactly as much as a genuine Wikidata record**, and the winner is then decided by
  alphabetical string comparison at `dedup.py:184`. `_validity` returns a flat `1` for `category`
  (`dedup.py:140`), so nothing catches it.
- **Trigger:** OSM `bar` (trust 2, and `bar` ∈ `PRIORITY_INDUSTRIES` → `high`) versus Wikidata
  `chain of restaurants` (trust 1 → `low`). Verified:

  ```
  source='osm'    -> category='bar'                    industry_priority='high'  in sales_ready=True
  source=' osm'   -> category='chain of restaurants'   industry_priority='low'   in sales_ready=False
  trust(category): ' osm'=1  'wikidata'=1  'osm'=2
  ```

  One leading space in one master cell demotes a high-priority bar out of `sales_ready.csv`, and
  nothing in the artifact changes shape — the row is still there, still populated, still plausible.
- **Fix:** `_DEFAULT_TRUST = 0` so absent provenance loses every tiebreak, and rank `category` by
  `whitelist.industry_priority` rather than by `str(value)` so survivorship tracks pitch value.

### S2 — the declared type opts out of the only schema in the codebase, and where the schema is wrong, it is wrong about this column

- **Where:** `dedup.py:143` (`record: dict`), `scrapers/base.py:26` (`source: str`),
  `dedup.py:197` (`list[dict] -> list[dict]`), `main.py:180` (the call).
- **Breaks:** verified by grep — `BusinessRecord` appears only in `base.py` and the three scrapers.
  Downstream, the same object is declared four different ways: bare `dict` (`dedup.py:143,148,155,
  159,190,197`; `enricher.py:101,225,283,326`; `main.py:50,77,108,137`), `Mapping`
  (`pitch_recommender.py:25`), and the TypedDict. `main.py:180` passes
  `list[BusinessRecord] + list[dict]` into `dedup(records: list[dict])` and gets `list[dict]` back,
  which is exactly what `enrich` (`enricher.py:326`) then takes — a type erasure at the boundary
  that makes `_sources_of`'s contract unreviewable from the call site.

  Two concrete contradictions in the schema itself:
  1. `source: str` is declared non-optional, but every blank master cell becomes `None`
     (`main.py:87-89`). The real type on the master path is `str | None`. `_sources_of`'s bare
     `dict` is the only reason this is not already a type error.
  2. `BusinessRecord` is a **total** TypedDict — all 23 keys required — but `enrich`
     (`enricher.py:340-348`) `setdefault`s only 8 of them, so a record in flight after enrichment
     can still be missing `source`, `country`, `name`, `address`, `scraped_at`. Records are never
     `BusinessRecord` past the scraper boundary. `_sources_of`'s `.get("source")` is the load-bearing
     safety net for a contract that nothing upstream upholds.
- **Why S2:** the annotations are the cheapest available contract enforcement and all of them are
  inert. `mypy strict = true` is configured and never run, so even a corrected annotation would be
  advisory.
- **Fix:** `source: str | None` in the TypedDict, add
  `SourceName = Literal["osm", "wikidata", "google_places"]` and annotate
  `_sources_of(record: BusinessRecord) -> set[SourceName]`, then add a `mypy` step to the workflow.
  Expect this to surface at four call sites at once — that is the point, not a problem.

### S2 — the `field` argument fed by `_sources_of`'s result is unconstrained, so a column that is not in `BusinessRecord` becomes a merge field and is then silently dropped

- **Where:** `_merge` iterates `set(a) | set(b)` and calls `_pick(key, a, b)` for each
  (`dedup.py:192-193`); for master rows the key set is the CSV header (`main.py:86`), unvalidated;
  `_trust_for` is then called with that key (`dedup.py:181` → `dedup.py:148-152`); the write drops
  it (`main.py:125`).
- **Breaks:** verified end-to-end:

  ```
  _merge({"source":"osm","osm_id":"n/1"}, {"source":"wikidata","osm_id":"n/2"})
      -> keeps {'osm_id': 'n/2', 'notes': 'y'}          # extra keys survive _merge
  _trust_for("osm_id", merged) -> 1                     # _DEFAULT_TRUST, no error
  write_csv({'name':'Acme','source':'osm','osm_id':'node/123'})
      -> header has 23 columns, 'osm_id' absent        # extrasaction="ignore"
  load_master -> 23 keys, 'osm_id' gone
  ```

  So an operator-added column is used as a merge field with invented trust weights and then
  discarded. Today `FIELDS` and `BusinessRecord.__annotations__` agree exactly (verified: 23 keys
  each, empty symmetric difference), so this is latent rather than active — but it is one schema
  edit away, and the edit fails silently in both directions.
- **Fix:** validate `set(FIELDS) ^ set(row)` in `load_master` and fail loudly on a header mismatch;
  derive `FIELDS` from `BusinessRecord.__annotations__` (already recommended in `021`/`028`/`087`).

### S3 — three uncoordinated implementations of "multi-source", with the delimiter hardcoded in two modules

- **Where:** `dedup.py:145` and `dedup.py:174` (`"|"`), `enricher.py:316-317`
  (`if "|" in str(record.get("source") or ""): score += 5`), `cli.py:120`.
- **Breaks:** `enricher.py:316` pays out in `sales_ready.csv` for a literal `|` anywhere in the
  column — a character that is a **serialisation artefact** of `dedup.py:174`, not evidence of
  anything. Verified:

  ```
  _merge({"source":"osm "}, {"source":"osm"})["source"] -> 'osm|osm '   # ONE source, two tokens
  tokens {'osm', 'osm '}   -> "|" in source: True
  lead_score: source='osm' -> 50      source='osm|osm ' -> 55       delta = +5
  ```

  Conversely, any future normalisation of the serialisation (a different delimiter, a real list
  column) silently removes the bonus from every row with no test failure — except
  `tests/test_lead_signal.py:316-319` pins the exact string `"google_places|osm"`, so that test is
  the only thing standing between a refactor and a silent scoring change. Flagging it so the
  implementer knows the coupling exists.
- **Fix:** give `lead_score` a precomputed `source_count` (or `confirmed_by`) so the scorer never
  re-parses a string; put the delimiter in one module-level constant shared by the writer and the
  reader; update `tests/test_lead_signal.py:316-319` in the same commit.
- **Why S3:** the scoring hazard itself is S1-shaped, but `319` already reports the
  fabricated-multi-source S1 and this is the *mechanism* by which a formatting detail reaches the
  revenue artifact. It is one line to fix while the area is open.

---

## Not a bug, but worth knowing

- **Fields outside the TypedDict: none.** `_sources_of` reads exactly `source` and writes nothing.
  The out-of-schema surface is one level out (the unconstrained `field` argument, S2-4), not in the
  function itself. Worth stating plainly so nobody goes hunting for a phantom bug here.
- **The return value cannot raise, in either use.** `_trust_for` (`dedup.py:150-151`) only iterates
  and does `dict.get`; `_pick` (`dedup.py:174`) only does set-union, `sorted` and `join`. Every
  token is a `str` by construction (`str(...)` at `dedup.py:144`), so `sorted()` cannot fail on
  mixed types even when `source` holds a list or an int. Every finding in this report is therefore
  **silent degradation, never a crash** — which is precisely why all of them are undetectable from
  the output.
- **The `or ""` at `dedup.py:144` is load-bearing. Keep it.** Without it, `str(None)` → `"None"` →
  a token named `"None"`, i.e. *fabricated provenance* that looks legitimate in the CSV and scores
  as an unknown-but-present source. The `or ""` is what converts `None` into "no provenance"
  rather than "provenance called None".
- **`sorted()` makes line 174 deterministic and order-independent — preserve that.** `_merge(a, b)`
  and `_merge(b, a)` agree on `source` (pinned by `tests/test_lead_signal.py:316-319`). The rest of
  the pipeline *is* order-dependent (scraper completion order via `as_completed`, `main.py:162`) and
  non-deterministic (Google pagination, jittered backoff), so this is the one merge path you can
  reason about statically. Every fix above keeps it.
- **The encoding is not injective, and that is the structural defect behind S1-1 and S2-1.**
  `"osm|wikidata"`, `"wikidata|osm"`, `"OSM|wikidata"`, `" osm |wikidata"` and `"||"` all reduce to
  overlapping token sets, and `"|".join` cannot reproduce which input it came from — while `""` and
  `"||"` collapse to the *same* representation. One column is doing two jobs: an input field
  declared `str` in the schema, and a derived set written back into it. Store provenance as its own
  structure (`source_list` column, or `{source: last_seen}` per `319` S1-1) and the whole class
  goes away.
- **`FIELDS` (`main.py:33-40`) and `BusinessRecord.__annotations__` currently agree exactly** —
  23 keys each, empty symmetric difference, verified. The duplication is a maintenance risk, not a
  live bug. (`BRIEF.md:59-62` says "22 columns" and lists 23; harmless, but it is the same
  drift-by-hand-counting failure mode.)
- **Performance needs no work.** `319` measured 0.52 µs/call and ~6 s for a 540k-record `dedup`.
  Do not spend a cycle here; the function's problem is its contract, not its speed.

---

## Recommended order of work

1. **Stop the data loss.** One line at `dedup.py:174`: never let the derived `source` be emptier
   than its inputs. Lands today, testable with `{"source": "||"}`. (S1-1)
2. **Canonicalise and validate tokens.** `.strip().lower()`, drop empties, warn on any token
   outside `_SOURCE_TRUST`, and add a `source`-column check to `cli.py cmd_validate` so the gate
   that already exists would catch the Excel edit. (S2-2, S3-1)
3. **Set `_DEFAULT_TRUST = 0`.** One character; removes the `category` collision that lets an
   unparseable source outrank OSM and drop a lead out of `sales_ready.csv`. Then rank `category` by
   `industry_priority` rather than alphabetically. (S2-3)
4. **Make the contract real.** `source: str | None`, add `SourceName`, annotate
   `_sources_of`/`_trust_for`/`_pick` with `BusinessRecord`, and add `mypy` to the workflow so
   `strict = true` stops being decorative. Expect new errors at four call sites — that is the
   first time anyone can see this column's true shape. (S2-4)
5. **Validate the CSV header against the schema** in `load_master`, and derive `FIELDS` from
   `BusinessRecord.__annotations__`. (S2-5)
6. **Then** do the structural fix `319` asks for — per-field provenance with a `last_seen` — which
   supersedes 1, 2 and 3 and makes the delimiter question disappear. Steps 1–3 are the cheap
   containment you can ship this week; step 6 is the real one.

---

### Reproducing

Every claim above came from local pure-Python probes against the repo — no network, no `pip
install`; `requests` and `urllib3` were shimmed in-process purely to satisfy `enricher.py`'s
module-level import. Load-bearing reproductions, in order:

1. `_merge({"source": "||"}, {"source": "|"})["source"] == ""` followed by
   `write_csv` → `load_master` → `_sources_of` → `_trust_for` (S1-1).
2. Three-step token accumulation to `'google_places|osm|osm,wikidata|wikidata'`, and
   `_merge({"source": "osm\nwikidata"}, {"source": "google_places"})` retaining the newline (S2-2).
3. Flattening `_SOURCE_TRUST` to show `("wikidata", "category") == _DEFAULT_TRUST == 1` is the only
   collision, then the `bar` vs `chain of restaurants` pair flipping `industry_priority` (S2-3).
4. `grep -rn "BusinessRecord" --include='*.py'` returning only `base.py` and the three scrapers;
   `_merge` retaining `osm_id`/`notes`, `_trust_for("osm_id", …) == 1`, and `write_csv` dropping
   both (S2-4, S2-5).
5. `lead_score` 50 → 55 from `_merge({"source": "osm "}, {"source": "osm"})["source"] ==
   "osm|osm "` (S3-1).