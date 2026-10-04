# 342 — `_pick` (dedup.py:159) — dependency audit

## Verdict

`_pick` is an **import-graph leaf**: its transitive closure is six symbols, all inside
`dedup.py`, plus builtins. It imports nothing from the project and nothing third-party,
so it cannot create a circular import today and there is no annotation-only or
logging-only import anywhere in its closure. The interesting dependencies are all
**undeclared data dependencies expressed as string literals** — the source names owned
by the scrapers layer (`dedup.py:97-110` vs `scrapers/osm.py:119`), the field names owned
by `BusinessRecord` (`scrapers/base.py:5-28`), and one field owned by a layer that runs
*downstream* of dedup (`website_live`, `dedup.py:114` vs `enricher.py:250`).

The single most important thing: **one of those string dependencies closes a loop.**
`_pick` writes `source` as a sorted union (`dedup.py:173-174`) and `_trust_for` reads
that union back and takes a `max()` (`dedup.py:151`). `_pick` consumes its own output, and
because the union only ever grows, a record's trust is **monotonically non-decreasing over
its lifetime**. Once any row has been touched by `osm`, its `website` trust is pinned at 3
forever and no fresh single-source observation (google_places = 2, wikidata = 2) can
displace it, because trust sits at tuple index 0 and recency at index 2
(`dedup.py:180-185`). Wrong values therefore become permanently unoverwritable in the
cumulative master.

---

## Dependency map

Transitive closure of `_pick`, computed by AST walk over `dedup.py`:

```
_pick                    dedup.py:159
├── _is_missing           dedup.py:79     -> nothing
├── _sources_of           dedup.py:143    -> builtin str/set
├── _trust_for            dedup.py:148
│   ├── _sources_of       dedup.py:143
│   ├── _SOURCE_TRUST     dedup.py:97     -> literal; keys = "google_places"|"osm"|"wikidata"
│   └── _DEFAULT_TRUST    dedup.py:111    -> literal 1
├── _validity             dedup.py:120
│   ├── _is_missing       dedup.py:79
│   ├── _EMAIL_OK         dedup.py:116    -> re.compile (re, dedup.py:1)
│   └── _URL_OK           dedup.py:117    -> re.compile (re, dedup.py:1)
├── _recency              dedup.py:155    -> builtin str
└── _VOLATILE             dedup.py:114    -> literal set, includes "website_live"
```

| Kind | Count | Detail |
|---|---|---|
| Internal functions in closure | 6 | all in `dedup.py` |
| Module globals in closure | 5 | `_SOURCE_TRUST`, `_DEFAULT_TRUST`, `_VOLATILE`, `_EMAIL_OK`, `_URL_OK` |
| Builtins used | 8 | `str`, `set`, `sorted`, `max`, `float`, `isinstance`, `dict`, `object` |
| Project imports | **0** | — |
| Third-party imports | **0** | verified: `import dedup` leaves `{'requests','urllib3','bs4','lxml'}` absent from `sys.modules` |
| Annotation-only imports | **0** | `dedup.py` has no `typing`, no `__future__`, no `TYPE_CHECKING`, no `cast` |
| Logging-only imports | **0** | `dedup.py` has no `logging` import at all |

Callers, for completeness: `_merge` (`dedup.py:190-194`) is the only one, and `_merge` is
called from `dedup` at `dedup.py:208` and `dedup.py:216`. `_merge` is also imported
directly by the test suite at `tests/test_lead_signal.py:65` — i.e. a private symbol is a
de facto public dependency.

### The edges that are *not* in the import graph

| Consumer | Literal | Producer | Enforcement |
|---|---|---|---|
| `_SOURCE_TRUST` keys `dedup.py:98,102,106` | `"google_places"` | `scrapers/google_places.py:302` | string equality only |
| `_SOURCE_TRUST` keys `dedup.py:98,102,106` | `"osm"` | `scrapers/osm.py:119` | string equality only |
| `_SOURCE_TRUST` keys `dedup.py:98,102,106` | `"wikidata"` | `scrapers/wikidata.py:127` | string equality only |
| `_pick` `dedup.py:173` | field `"source"` | `BusinessRecord.source` `scrapers/base.py:26` | string equality only |
| `_pick` `dedup.py:176` | field `"scraped_at"` | `BusinessRecord.scraped_at` `scrapers/base.py:27` | string equality only |
| `_validity` `dedup.py:124,126,128,134` | `"email"`,`"website"`,`("lat","lon")`,`"rating"` | `BusinessRecord` `scrapers/base.py:8,10,11,12,17` | string equality only |
| `_recency` `dedup.py:156` | field `"scraped_at"` | `httpclient.utc_now_iso` `httpclient.py:220-229` | string equality only |
| `_VOLATILE` `dedup.py:114` | field `"website_live"` | **downstream** `enricher.py:250`; ordered after dedup at `main.py:187` | string equality only |

Audit 020 established that `BusinessRecord` "never crosses the `scrapers/` boundary (every
downstream function is `dict` / `list[dict]` / `Mapping`)". That is precisely the mechanism
that makes every row of the last table invisible: nothing in `dedup.py` imports the type it
is coupled to, so no type checker, linter, or import cycle will ever flag a rename.

---

## Findings

### S1 — `_pick` reads its own output: source-union + `max()` trust makes stale values permanently unoverwritable

- **Where:** `dedup.py:173-174` (writes `source` as a sorted union) ->
  `dedup.py:143-145` (`_sources_of` splits that union on `|`) ->
  `dedup.py:148-152` (`_trust_for` takes `max` over it) ->
  `dedup.py:180-185` (`rank` puts trust at index 0, recency at index 2) ->
  `dedup.py:187` (comparison)
- **Breaks:** `_pick` merges `a.source` and `b.source` into a union. On the *next* merge
  involving that record, `_trust_for` reads the union back and takes the **maximum** trust
  over every source that has ever touched it. Trust therefore only ever increases, and it
  increases the wrong way round for freshness: the accumulator raises the bar for
  overwriting the accumulated row. Combined with `website` being in `_VOLATILE`
  (`dedup.py:114`), the "fresher observation wins" policy that `_VOLATILE` exists to
  implement can never fire for `website` against a differently-trusted source. The
  surviving value is decided at run 1 and is frozen there for the life of the master CSV.
- **Trigger:** verified end-to-end through the real `dedup.dedup()`, `main.write_csv`,
  `main.load_master`. No network.

  ```
  RUN 1  osm      -> website=http://cafe-real.example   (osm website trust 3)
         google   -> website=https://maps.example       (google website trust 2)
         merged   -> source="google_places|osm"  website=http://cafe-real.example
         (written to CSV, read back: website preserved, website_live=None)

  RUN 2  fresh google_places row (scraped_at 2026-10-03) with the cafe's REAL new domain
         https://cafe-new.example
         master row: source="google_places|osm", website="http://cafe-real.example",
                     scraped_at=2026-01-01

         _trust_for("website", master) = max(google_places=2, osm=3) = 3
         _trust_for("website", fresh)  = 2
         rank(master) = (3, 3, "2026-01-01T00:00:00+00:00", "http://cafe-real.example")
         rank(fresh)  = (2, 3, "2026-10-03T00:00:00+00:00", "https://cafe-new.example")
         3 > 2  ->  master wins

         RESULT: website=http://cafe-real.example   (fresh domain discarded)
                 new domain landed? False
                 surviving website trust = 3
  ```

  Control, isolating the accumulator as the *only* variable — the master website value is
  byte-identical in both rows; only the accumulated `source` string differs:

  ```
  master source="google_places"       -> winner: https://cafe-new.example
  master source="google_places|osm"   -> winner: https://maps.example
  _trust_for("website", master_1src) = 2
  _trust_for("website", master_2src) = 3
  ```

  This is the failure that makes the product wrong. `website` drives the
  `with_websites` / `without_websites` split (`main.py:199-200`) and, via
  `enricher.lead_score` (`enricher.py:300-304`), a dead-site rebuild pitch worth +20 over a
  live site. A business that has *ever* been corroborated by OSM can never have its
  website corrected by Google, on any future run, for any reason. `enricher.check_websites`
  then liveness-checks the stale URL forever.
- **Fix:** stop feeding `_pick` its own output — carry the *provenance of the observation
  that supplied the surviving value* (per-field, as audit 095 already proposes) and score
  `_trust_for` from that observation only, not from the union of all observations that ever
  touched the record. If union-sources must stay for the `source` column, compute it
  *after* selection and keep the ranking input as the pre-merge record's own source.

### S2 — `dedup.py` has an undeclared dependency on the scrapers layer: the source names are hardcoded string literals

- **Where:** `dedup.py:97-110` (keys), produced at `scrapers/google_places.py:302`,
  `scrapers/osm.py:119`, `scrapers/wikidata.py:127`
- **Breaks:** `dedup.py:1-2` imports only `re` and `unicodedata`. It does not import
  `scrapers.base`, does not import a source constant, and does not validate the key set at
  startup. So the entire trust model rests on three string literals matching three string
  literals in another package. A rename on either side raises no error, emits no warning,
  and silently drops the source to `_DEFAULT_TRUST = 1` (`dedup.py:111`, `dedup.py:151`) —
  below wikidata's 2, so the renamed source loses to sources it should beat.
- **Trigger:** rename `source="osm"` to `source="openstreetmap"` at `scrapers/osm.py:119`.
  Verified, no exception raised:

  ```
  both sides source="osm"           -> website: https://zzz.example   (tie at trust 3, str tiebreak)
  one side  source="openstreetmap"  -> website: https://aaa.example   (trust 1 < 3)
  changed? True
  ```

  The OSM-specific advantage at `dedup.py:103-104` (`email`, `website`, `facebook`,
  `instagram`, `whatsapp` all at trust 3) evaporates, and OSM rows start losing `website`
  to google_places rows they are more authoritative for. Note this is *worse* than an
  exception: with a typo the failure is loud, with a rename it is a slow drift in output
  quality that no run summary reports.
- **Fix:** define the source names once (module constants in `scrapers/base.py`, or a new
  dependency-free `records.py`) and import them in both `dedup.py` and the scrapers; add a
  startup assertion that `set(_SOURCE_TRUST) == set(SOURCES_EMITTED)` so a new scraper that
  forgets to register is caught on run 1.

### S2 — The `_VOLATILE` policy is unreachable: `_recency` is ranked below `_trust_for`

- **Where:** `dedup.py:113-114` (comment + set), `dedup.py:180-185` (rank tuple)
- **Breaks:** the comment at `dedup.py:113` states the intent — "Fields whose value
  changes over time, so the fresher observation wins" — and `_pick` honours it at
  `dedup.py:183` by inserting `_recency(rec)` into the rank tuple. But the tuple is
  `(trust, validity, recency-if-volatile, str(value))`, so trust is compared first and
  short-circuits. `_recency` is consulted **only when trust and validity both tie**. For
  every `_VOLATILE` field, that requires the two records to come from sources of *equal*
  trust — in production, the same source. So the set as written is inert except in the
  single-source case.
  - `website` (osm 3, google_places 2, wikidata 2): the **older** OSM value always wins.
  - `rating`, `review_count` (google_places 3, osm 1, wikidata absent → 1): the **older**
    google_places value always wins.
  Verified: `_pick("website", osm+old, google_places+new)` returns `https://stale.example`.
- **Trigger:** the existing regression test gives false confidence here.
  `tests/test_lead_signal.py:303-309` (`test_volatile_field_prefers_the_fresher_observation`)
  puts `source: "google_places"` on **both** records, so trust ties at 3, `_recency` fires,
  and the test passes. It does not cover the only shape the real pipeline produces — an
  accumulated master row merging with a fresh row from a *different* source — which is
  precisely the case where the policy fails. A green suite here does not mean the comment
  at `dedup.py:113` is implemented.
- **Fix:** make volatility a *pre-trust* signal for the fields that declare it — reorder to
  `(validity, recency-if-volatile, trust, str(value))` — or drop `_VOLATILE` and the comment
  and state plainly that trust dominates. Whichever you pick, extend
  `tests/test_lead_signal.py:303-309` with a cross-source case (`osm` old vs `google_places`
  new) so the two policies cannot silently diverge again.

### S2 — 19 of the 21 fields `_pick` merges have no policy and are resolved by lexicographic string comparison

- **Where:** `dedup.py:140` (`return 1` catch-all), `dedup.py:184` (`str(value)`),
  `dedup.py:190-194` (`_merge` iterates `set(a) | set(b)`)
- **Breaks:** `_pick` special-cases exactly two field names, `"source"` (`dedup.py:173`)
  and `"scraped_at"` (`dedup.py:176`). Every other key that `_merge` hands it — including
  all 22 `BusinessRecord` fields — falls through `_validity` to `return 1` at
  `dedup.py:140` and finds no entry in any `_SOURCE_TRUST` dict, so its rank degenerates to
  `(1, 1, "", str(value))` and the winner is `max` over the **stringified** value
  (`dedup.py:184`, `dedup.py:187`). This is a real ordering bug for any field the tables
  do not mention, not merely a weak default.
- **Trigger:** `region` is emitted by google_places (`scrapers/google_places.py:275`,
  `:284`) and read back from the master CSV, but appears in no `_SOURCE_TRUST` dict and has
  no `_validity` branch — so it is never in `_VOLATILE` either. Verified through the real
  `dedup()`:

  ```
  master  region="South Lebanon"   (prior run's enricher.infer_region)
  fresh   region="North Lebanon"   (google_places re-geocoded the pin)
  _trust_for("region", ...) = 1 and 1 ; _validity("region", ...) = 1 (dedup.py:140)
  _pick("region", a, b)  = "South Lebanon"      ('S' > 'N')
  RESULT: region='South Lebanon'   ->  region corrected? False
  ```

  `enricher.infer_region` cannot fix it: `enricher.py:331` only infers
  `if not r.get("region")`, so a wrongly-merged region is never recomputed.
  `industry_priority` behaves the same way — `"low" > "high"`, so `"low"` wins. The derived
  ints are worse: `_pick("lead_score", {"lead_score": 95}, {"lead_score": 100})` returns
  **95**, because `"95" > "100"` as strings. Those are currently harmless only because
  `main.py:189-197` and `enricher.py:349-350` overwrite them unconditionally downstream —
  i.e. the correctness of the whole policy rests on four assignment statements in another
  file that `_pick` cannot see.
- **Fix:** replace the implicit fallthrough with an explicit per-field policy table and
  `raise` on an unknown field name in `_merge`, so adding a `BusinessRecord` key without
  deciding how it merges is a startup error rather than a lexicographic accident.

### S3 — `_VOLATILE` references a field owned by a layer that runs strictly downstream of dedup

- **Where:** `dedup.py:114` (`"website_live"` in `_VOLATILE`); produced only at
  `enricher.py:250` and read back from CSV at `main.py:102-103`; ordering at `main.py:180`
  (`dedup`) → `main.py:187` (`enrich`)
- **Breaks:** `website_live` is a reverse dependency on the enricher layer's output
  vocabulary. No scraper emits it — `scrapers/osm.py:98-120`, `scrapers/wikidata.py:106-128`
  and `scrapers/google_places.py:281-303` all construct `BusinessRecord` without that key —
  so at `_pick` time the only rows that can carry it are master-CSV rows. For a fresh
  scraper row, `a.get("website_live")` is `None`, `_is_missing(None)` is true, and
  `dedup.py:168-169` returns before `_VOLATILE` is ever consulted. Verified: the entry is
  dead for scraper rows and reachable only via the CSV round-trip.
- **Trigger:** none that corrupts data — `enricher.check_websites` (`enricher.py:231`,
  `:250`) re-derives `website_live` for every row that has a website. The cost is
  misleading: the entry implies dedup has a recency policy for a field that does not exist
  in its input domain, which is what led to the S2 above being missed.
- **Fix:** delete `"website_live"` from `_VOLATILE` and note in the comment that it is an
  enricher-owned field that never reaches `_pick` from a scraper.

### S3 — Under a src layout `_pick` stays acyclic only because it imports nothing; the fix for S2 creates the first `dedup -> scrapers` edge

- **Where:** `dedup.py:1-2`, `scrapers/__init__.py` (0 bytes),
  `scrapers/google_places.py:26`, `enricher.py:128`, `httpclient.py:43`,
  `pyproject.toml:35-37`
- **Breaks:** there is no circular-import risk today — `_pick` has no project imports at
  all. The risk appears precisely when you fix S2. The natural home for the source names is
  `scrapers/base.py`, already home to `BusinessRecord` (`scrapers/base.py:5-28`). Verified
  counterfactual: `import scrapers.base` is acyclic and free **only because
  `scrapers/__init__.py` is empty**. The moment it re-exports the scrapers — the ergonomic
  pattern `cli.py:46-49` already demonstrates — the chain is
  `dedup -> scrapers/__init__ -> scrapers.google_places -> enricher (google_places.py:26)
  -> httpclient (enricher.py:128) -> requests (httpclient.py:43)`, so `dedup` stops being
  importable without `requests` installed. Today it is: verified that `import dedup` loads
  zero third-party modules, which is what lets `tests/test_lead_signal.py:65` import
  `_merge` standalone.
  The genuine latent cycle: the moment `scrapers/base.py` wants to *validate* records using
  `dedup._is_missing` or the field list, you get `dedup <-> scrapers.base`. That is the
  cycle to design against now, because audit 020 already argues `BusinessRecord` should
  enforce something at runtime.
- **Fix:** put the record and source vocabulary in a new **dependency-free** module
  (`records.py`) that both `scrapers/base.py` and `dedup.py` import, and keep
  `scrapers/__init__.py` empty. `records.py` has no importers outside the package, so no
  cycle is reachable from it. `pyproject.toml:36` already flags the flat-sibling problem;
  do the vocabulary extraction in the same change.

### S3 — `_pick`'s ordering depends on an undocumented `scraped_at` string-format contract

- **Where:** `dedup.py:177` (`max(av, bv)`), `dedup.py:183` (`_recency(rec)`),
  `dedup.py:155-156` (`_recency` returns `str(record.get("scraped_at") or "")`)
- **Breaks:** `scraped_at` is ordered by plain string comparison in two separate places
  (`dedup.py:177` for the field itself, `dedup.py:183` via tuple comparison). Correctness
  therefore depends entirely on every producer emitting fixed-width ISO-8601 UTC, which
  holds only because `httpclient.utc_now_iso()` (`httpclient.py:220-229`) happens to
  return `datetime.now(timezone.utc).isoformat()`. `_pick` neither parses nor validates the
  value. This contract has already been broken once: the docstring at
  `httpclient.py:221-226` says the timezone-aware value replaced `datetime.utcnow()`
  precisely because the naive timestamp "sorted incorrectly against any offset-aware
  timestamp in `dedup._merge`'s string comparison".
- **Trigger:** a `scraped_at` written as `2026-10-03T22:27:31Z` sorts *after*
  `2026-10-03T22:27:31+00:00` (`'Z'` > `'+'`) for the same instant; a space separator
  (`"2026-10-03 22:27:31"`) sorts before `"2026-10-03T..."` (`' '` < `'T'`) for the same
  day. Both silently pick the wrong record as "fresher", and neither raises.
- **Fix:** store `scraped_at` as an epoch integer or a parsed `datetime` and compare
  numerically, or validate the format at the `dedup()` boundary.

---

## Not a bug, but worth knowing

- **Nothing is imported only for a type annotation.** `dedup.py` has no `typing` import, no
  `from __future__ import annotations`, no `TYPE_CHECKING` block, and no `cast`. The
  annotations at `dedup.py:159` (`a: dict, b: dict`, `-> object`) are runtime-evaluated
  builtins with zero import cost. Note `-> object` is close to useless as documentation —
  the function returns `""` (`dedup.py:169`), `bool` (`dedup.py:123`), `float`
  (`dedup.py:133`), `int`, and `str` depending on the field. Audit 066's typed-field-policy
  table is where this should get real.
- **Nothing is imported only for logging — because `dedup.py` has no logging at all.** For
  contrast, `scrapers/osm.py:1,7` and `scrapers/wikidata.py:1,7` do `logging.getLogger`
  and use it. `_pick` and `_merge` make irreversible survivorship decisions with zero
  observability, which is why every finding above is silent. One
  `log.debug("field=%s a=%r b=%r rank_a=%r rank_b=%r", ...)` on a conflict would have made
  the S1 stale-website pin obvious on run 2. This is the single cheapest observability win
  in the module.
- **`re` and `unicodedata` (`dedup.py:1-2`) are not in `_pick`'s closure at all.**
  `unicodedata` is used only by `normalize_name` (`dedup.py:67-68`); `re` only by
  `normalize_phone` / `normalize_name` (`dedup.py:44`, `:69`) and the `_EMAIL_OK` /
  `_URL_OK` compiles (`dedup.py:116-117`), which `_validity` uses only as pre-compiled
  matchers. Deleting `_pick` and `_validity` would still leave both imports needed by the
  normalisation functions, and adding a new source would still not touch them.
- **`_pick` is invoked with `a` first and uses `>=` at `dedup.py:187`.** This makes the
  result order-independent in value but not in *provenance*: on an exact four-tuple tie the
  left record's value wins, so `_merge(a, b)` and `_merge(b, a)` return equal dicts with
  equal values here (verified by `tests/test_lead_signal.py:292-295`) but the deciding
  observation differs. Harmless today; it is the reason audit 095 wants provenance kept
  per-field rather than inferred from the surviving row.
- **Out of scope for this target, spotted while tracing:** `enricher.py:4` imports
  `urljoin, urlparse` from `urllib.parse` and neither name appears anywhere else in the
  file. Two unused imports in the layer that `_pick`'s downstream fields depend on. Not a
  finding for `_pick`; flagging so it is not missed by whoever owns `enricher.py`.

## Recommended order of work

1. **S1** — decouple trust from the accumulated `source` union (per-field provenance, or
   score trust from the pre-merge observation). This is the one that makes the master CSV
   permanently wrong, and every fix below is easier to verify once it is fixed.
2. **S2 (source literals)** — move the source names into one dependency-free `records.py`,
   import them from `dedup.py` and all three scrapers, and assert the key set at startup.
   Do this with the S3 src-layout extraction in the same change so the new module is
   dependency-free from the start.
3. **S2 (volatility)** — decide whether trust or recency dominates for `_VOLATILE` fields,
   then extend `tests/test_lead_signal.py:303-309` with the cross-source case so the
   comment at `dedup.py:113` and the code cannot diverge again.
4. **S2 (unknown fields)** — add the explicit per-field policy table and raise on an
   unrecognised key in `_merge`; that will immediately surface `region` and
   `website_live` as the two fields needing a decision.
5. **S3s** — drop `"website_live"` from `_VOLATILE`; move `scraped_at` to a numeric or
   parsed comparison.
6. **Cross-cutting** — add `log.debug` to `_pick`'s conflict branch. Cheap, and it is what
   would have caught item 1 in production.