# 334 — `_recency` (dedup.py:155): dependency & layering audit

## Verdict

`_recency` is one line and imports nothing — but its correctness rests entirely on an
**unwritten, unvalidated, cross-module string-format contract** whose only definition site is
`utc_now_iso()` in `httpclient.py:220-229`, an HTTP *transport* module. Nothing in the type system,
the tests, or the imports binds `dedup.py` to that contract, so the moment any producer emits a
different-but-plausible timestamp spelling, `_recency` silently keeps the **older** observation and
there is no log line anywhere to say so. Worse, the module that owns the clock cannot be imported
for validation without dragging `requests` into the merge layer, and `dedup.py:155` annotates its
parameter as a bare `dict` — which is the only reason the `dedup ↔ scrapers.base` cycle that
`066-typing-at-scale.md:207` recommends has not already fired.

Three concrete S1s follow from that one root cause, plus a genuine layering violation
(`_VOLATILE` at `dedup.py:114` names a field that only `enricher.py` produces, and only *after*
`dedup()` has run).

---

## Dependency map (transitive)

### Nodes

| Node | Kind | Location | Reached from `_recency` |
|---|---|---|---|
| `str` | builtin | — | directly, `dedup.py:156` |
| `dict.get` | builtin method | — | directly, `dedup.py:156` |
| `record["scraped_at"]` | **data contract** | key literal at `dedup.py:156` | directly |
| `_pick` | module fn | `dedup.py:159` | caller |
| `_VOLATILE` | module global | `dedup.py:114` | via `_pick:183` (gate) |
| `_is_missing` | module fn | `dedup.py:79-90` | via `_pick:168,170` |
| `_sources_of` | module fn | `dedup.py:143-145` | via `_pick:174` |
| `_trust_for` | module fn | `dedup.py:148-152` | via `_pick:181` |
| `_SOURCE_TRUST` | module global | `dedup.py:97-110` | via `_trust_for:151` |
| `_DEFAULT_TRUST` | module global | `dedup.py:111` | via `_trust_for:149,151` |
| `_validity` | module fn | `dedup.py:120-140` | via `_pick:182` |
| `_EMAIL_OK` | module global (`re.compile`) | `dedup.py:116` | via `_validity:125` |
| `_URL_OK` | module global (`re.compile`) | `dedup.py:117` | via `_validity:127` |
| `_merge` | module fn | `dedup.py:190-194` | via `_pick` caller |
| `dedup` | module fn | `dedup.py:197-232` | via `_merge` callers `dedup.py:208,216` |
| `normalize_phone` / `normalize_name` / `_extract_city` | module fns | `dedup.py:33,66,72` | sibling of `_pick`, shares the same call site |
| `re` | stdlib | `dedup.py:1` | runtime values only (`dedup.py:44,69,116,117`) |
| `unicodedata` | stdlib | `dedup.py:2` | runtime values only (`dedup.py:67,68`) |

### Value producers (the surprising part — reached by *data*, not by import)

```
scrapers/osm.py:32          scraped_at = utc_now_iso()      ─┐
scrapers/wikidata.py:67     scraped_at = utc_now_iso()       ├─> httpclient.utc_now_iso()
scrapers/google_places.py:173 scraped_at = utc_now_iso()    ─┘      httpclient.py:220-229
                                                                          │
                                        ┌─────────────────────────────────┘
                                        v  (unvalidated, implicit)
                                    dedup._recency  dedup.py:156
```

Schema declaration: `scrapers/base.py:27` (`scraped_at: str`).
Serialization declaration: `main.py:39` (`FIELDS`).
Re-entry point: `main.py:150` `load_master(DATA_DIR / "all_businesses.csv")`.

### Measured call graph

```
main.py:180  dedup(combined)
  -> dedup.py:208 / :216  _merge(a, b)
       -> dedup.py:193  _pick(field, a, b)
            -> dedup.py:183  _recency(rec)      # only when field in _VOLATILE
```

`_recency` has **exactly one caller** (`dedup.py:183`) — confirmed by
`grep -rn "_recency" --include="*.py" .` → `dedup.py:155` (def), `dedup.py:183` (use), nothing else.
`cli.py` never calls `dedup` at all. The function is not even imported by the test suite
(`tests/test_lead_signal.py:65` imports only `_merge` and `normalize_phone`).

Measured invocation cost: 2 calls per conflicting volatile field per merge, 0 for non-volatile
fields (verified by monkeypatching `dedup._recency` and merging a `rating` conflict vs an `address`
conflict: `rating → 2 calls`, `address → 0 calls`). Negligible cost.

### The good news, stated plainly

`dedup.py` is the **only** domain module in the repo with zero project-internal imports
(`dedup.py:1-2` is the entire import block: `re`, `unicodedata`). Compare `enricher.py:2,4,128`
(`requests`, `urllib.parse`, `httpclient`) and `scrapers/google_places.py:24-28`
(`requests`, `enricher`, `httpclient`, `.base`). There is **no import cycle today**, and
`dedup.py` is the reason there isn't one. That is a real asset and it should not be spent.

---

## Findings

### S1 — `_recency` compares raw strings with no format validation, and the master CSV is a human-editable Google Drive file that feeds straight back in

- **Where:** `dedup.py:155-156` (`_recency`), consumed at `dedup.py:183`. Contract owner:
  `httpclient.py:220-229`. Round-trip path: `scrape.yml:56` (rclone **download** master from Drive)
  → `main.py:150` `load_master` → `dedup.py:180` → `scrape.yml:89` (rclone **upload** back to the
  same Drive folder).
- **Breaks:** `_recency` is a lexicographic string compare. Any timestamp spelling that is not
  byte-identical in shape to `utc_now_iso()`'s output sorts to the wrong side, and the **older**
  observation wins silently. There is no `re`, no `datetime.fromisoformat`, no assertion, and no log
  statement anywhere in the codebase touching `scraped_at`
  (`grep -rn "scraped_at" --include="*.py" . | grep -iE "valid|assert|isoparse|fromisoformat|re\.compile"`
  → no matches).
  The design explicitly invites the round trip: `main.py:118-120` writes the master with
  `encoding="utf-8-sig"` and the comment says *"so Excel and Google Sheets detect UTF-8 and render
  Arabic business names correctly"*, and the file is published to `gdrive:leads/` — a shared,
  human-facing Drive folder (`scrape.yml:88-89`). One person opening the master in Sheets and
  re-saving it is enough.
- **Trigger (verified end-to-end through `_merge`):** master row edited by a human /
  re-saved by Sheets, whose timestamp cell comes back in display form rather than ISO-8601:

  ```python
  edited = {"name":"Cafe A","phone":"+9611234567","country":"LB","source":"google_places",
            "website":"https://cafe-a.example","rating":4.9,
            "scraped_at":"2026-10-03 22:41:52"}          # 22:41, i.e. the NEWEST observation
  fresh  = {"name":"Cafe A","phone":"+9611234567","country":"LB","source":"google_places",
            "website":"https://cafe-a.example","rating":4.1,
            "scraped_at":"2026-10-03T09:00:00+00:00"}    # 09:00, i.e. 13h41m OLDER
  _merge(edited, fresh)["rating"]  ->  4.1                 # the stale value won
  ```

  Cause: `' '` is `0x20`, `'T'` is `0x54`. **Every space-separated timestamp sorts below every
  ISO timestamp on the same date**, regardless of time of day. The inverse also holds for other
  display formats:

  ```
  '10/03/2026 22:41:52'  vs '2026-10-03T09:00:00+00:00'  -> winner: 2026-10-03T09:00:00+00:00
  ```

  And an offset-form timestamp beats its own UTC twin because `'Z'` (`0x5A`) > `'+'` (`0x2B`):

  ```
  later-in-reality : "2026-10-03T11:00:00Z"          # 11:00 UTC
  earlier-in-reality: "2026-10-03T14:30:00+04:00"    # 10:30 UTC
  _merge -> rating 4.1  (the STALE value won), winner scraped_at 2026-10-03T14:30:00+04:00
  ```

  Note that `docs/audits/063-output-contract-for-humans.md:162` proposes rendering column 23 as
  `YYYY-MM-DD HH:MM`. That is exactly the poisoned spelling. If that audit is implemented by
  changing what `write_csv` emits rather than by adding a separate display column, this finding
  fires on day one.
- **Blast radius:** `rating` and `review_count` feed `enricher.lead_score`
  (`enricher.py:312-314`, +10 for `rating < 4.0`) and therefore `industry_priority`-adjacent
  ranking in `sales_ready.csv`. `website` feeds `without_websites.csv` /
  `with_websites.csv` (`main.py:199-200`). All silently keep the stale value.
- **Fix:** make `scraped_at` a real parsed value at the boundary and stop comparing strings —
  `def _recency(record): return _parse_ts(record.get("scraped_at"))` returning a
  timezone-aware `datetime` (sentinel `datetime.min` for missing), and normalise every producer
  through one `core/clock.py` helper. Independently: validate the column in
  `main.load_master` (`main.py:88-89` already rewrites empty cells; add the same fail-closed
  treatment for an unparseable `scraped_at`) so a poisoned master is rejected loudly at
  `main.py:150`, not silently mis-merged at `dedup.py:180`.

---

### S1 — `dedup.py:176-177` handles the same timestamp contract a second time, *without* `_recency`'s type coercion — so a `datetime` typed `scraped_at` kills the run

- **Where:** `dedup.py:176-177` vs `dedup.py:156`.
- **Breaks:** `_recency` defends its input with `str(...)`; the sibling path does not. The same
  function therefore has two different robustness levels for one contract, and the unguarded one
  is on the code path that every single record traverses (`_merge` iterates
  `set(a) | set(b)` at `dedup.py:192`, so `scraped_at` is `_pick`ed for every merge).
- **Trigger (verified):**

  ```python
  _pick("scraped_at", {"scraped_at": datetime.datetime(2026,1,1)}, {"scraped_at": "2026-01-01T00:00:00Z"})
  # TypeError: '>' not supported between instances of 'str' and 'datetime.datetime'

  _pick("scraped_at", {"scraped_at": "2026-01-01T00:00:00Z"}, {"scraped_at": 20260101})
  # TypeError: '>' not supported between instances of 'int' and 'str'
  ```

  `_is_missing` does not save you: a `datetime` is not `None` and not a `str`, so
  `dedup.py:86-90` returns `False` and execution reaches line 177.
- **Why this is S1 and not theoretical:** the change that lights the fuse is **already specced
  in this repo**. `docs/audits/045-scraper-layer-refactor.md:772` writes
  `scraped_at = datetime.datetime.now(datetime.timezone.utc)` — a `datetime` object, not a string.
  Implementing 045 as written turns every run into a `TypeError` at `main.py:180`. (`032` at
  `:592-597` proposes `isoformat() + "Z"`, which is safe but produces the `Z` form that mixes
  badly with today's `+00:00` form — see the previous finding.)
- **Also note what `str()` does to a `datetime`:** `_recency({"scraped_at": datetime.datetime(2026,1,1)})`
  returns `'2026-01-01 00:00:00'` — the space-separated, offset-less form, i.e. the exact spelling
  that loses every lexicographic comparison on the same date. So under 045's proposal `_recency`
  degrades *silently* to date-only resolution while `dedup.py:177` crashes outright. Both failure
  modes come from the same missing parse.
- **Fix:** delete the special case at `dedup.py:176-177` and let `scraped_at` go through the same
  `rank()` path as every other field, with a `_parse_ts`-based `_recency` and a
  `_validity("scraped_at", …)` branch that rejects unparseable values (returning `-100` via the
  existing `_is_missing` path at `dedup.py:122-123`).

---

### S1 — `website_live` is the one field where `_recency` is the *sole* authority, and it is fed the wrong clock: `scraped_at`, which advances without any website check

- **Where:** `dedup.py:114` (`_VOLATILE` includes `website_live`), `dedup.py:120-140` (`_validity`),
  `dedup.py:97-110` (`_SOURCE_TRUST`), `dedup.py:183` (`_recency`).
- **Breaks:** measured authority of `_recency` per `_VOLATILE` field:

  | field | `_trust_for` | `_validity` | `_recency`'s actual power |
  |---|---|---|---|
  | `rating` | google 3, others 1 | 3 if `0<=v<=5` | decides **same-source** merges only |
  | `review_count` | google 3, others 1 | 1 (falls through `dedup.py:140`) | decides **same-source** merges only |
  | `website` | **osm 3 > google 2** | 3 or 0 | **dead cross-source** — verified: a 2026 Google `https://new.example.com` loses to a 2020 OSM `https://stale.example.com` |
  | `website_live` | **1 for every source** | **1 for every value** | **sole discriminator** |

  Two structural surprises fall out:
  1. `website_live` is declared volatile (`dedup.py:114`) but appears in **none** of the three
     `_SOURCE_TRUST` dicts (`dedup.py:97-110`), so its trust is always `_DEFAULT_TRUST`. And
     `_validity("website_live", True)` and `_validity("website_live", False)` both return the
     generic `1` from `dedup.py:140`, because `website_live` matches no branch at
     `dedup.py:124/126/128/134`. So `_recency` is the entire decision procedure — verified:
     `_pick("website_live", newer_False, older_True) -> False`.
  2. But the value `_recency` compares is the **wrong clock**. `website_live` is produced by
     `enricher.check_websites` (`enricher.py:250`), which runs *after* `dedup()`
     (`main.py:180` dedup, `main.py:187` enrich). Every scraper hardcodes `website_live=None`
     (`osm.py:109`, `google_places.py:292`, `wikidata.py:117`), so within a run the fresh side
     always short-circuits at `dedup.py:170`. And there is **no `website_checked_at` field
     anywhere in the repo** — `grep -rn "website_checked_at"` finds it only inside the proposal at
     `docs/audits/035-incremental-scraping.md:279`.
- **Trigger (verified, three simulated runs through `_merge`):**

  ```
  RUN 1  day 1  : master row {website:"https://cafe-a.example", website_live:True,
                             scraped_at:"2026-01-01T09:00:00+00:00"}      # actually checked day 1
  RUN 2  day 30 : google re-lists it, new rating 4.9, website_live=None (google_places.py:292)
                  -> merged: scraped_at=2026-10-03T19:00:00+00:00  website_live=True
                     _recency now claims day 30.  It was checked on day 1.
  RUN 3  day 60 : site's DNS is dead -> enricher returns UNKNOWN -> website_live=None again
                  -> merged: scraped_at=2026-12-03T19:00:00+00:00  website_live=True  (site is dead)
  ```

  Because `scraped_at` is the max across *all* fields (`dedup.py:176-177`), any run that refreshes
  *any* field re-dates the record without re-checking the website. `website_live=True` is
  therefore **immortal** for any business whose website URL never changes.
- **Why that inverts the product's core signal:** `enricher.lead_score` awards `+20` for a
  server-confirmed dead site (`enricher.py:303-304`) and `+10` for live (`enricher.py:301-302`).
  An immortal `True` suppresses the highest-value pitch in the product — "your site is down" —
  in `sales_ready.csv` and in `without_websites.csv` (`main.py:199-205`), forever, with no signal
  that anything is wrong.
- **Fix:** add `website_checked_at` (as 035:275-283 already proposes) stamped in
  `enricher.check_websites` next to `enricher.py:250`, add it to `main.FIELDS`
  (`main.py:33-40`), and make `_recency` field-aware: `website_live` and `website` resolve
  against `website_checked_at`, `rating`/`review_count` against `scraped_at`. Do not fold two
  clocks into one column.

---

### S2 — The clock contract for a pure merge function is owned by an HTTP transport module that imports `requests` at module scope, which blocks the fix every audit recommends

- **Where:** `httpclient.py:220-229` (`utc_now_iso`), `httpclient.py:43` (`import requests`),
  `httpclient.py:1-30` (module docstring: *"Shared HTTP client for every outbound request"*),
  consumed by `osm.py:4`, `wikidata.py:4`, `google_places.py:27`; needed by `dedup.py:155`.
- **Breaks:** this is the single most surprising dependency in `_recency`'s map. A domain-layer
  pure function's correctness is governed by a helper that lives at the bottom of a transport
  module whose stated purpose is HTTP. `_recency` cannot import `utc_now_iso` to *assert* its input
  is well-formed, because that import would pull `requests` (`httpclient.py:43`) plus `threading`,
  `random`, `time`, `email.utils` and `contextlib` into `dedup` — the one module today that has
  none of them. The layering is precisely why nobody has added the validation: the obvious
  one-line fix is architecturally unavailable.
  It is a leaf module **by luck, not by design**. `google_places.py:26-27` currently gets
  `utc_now_iso` without dragging in enrichment only because `httpclient` happens to sit at the
  bottom of the graph. If the clock is ever moved next to `Observation` (which is where 045 and
  095 both put it), `scrapers.google_places` transitively imports the entire enrichment stack —
  `requests` and a 40-thread executor module — to obtain a 32-character string.
- **Evidence that this is already eroding:** `enricher.py:128` puts
  `from httpclient import DEFAULT_MAX_BODY_BYTES, get_session` **in the middle of the file**, 127
  lines below the top, after the region tables, the coordinate tables and two `print`-based
  sections. That is an import-graph problem showing up as a source-layout problem.
- **Fix:** move `utc_now_iso` to `src/leadminer/core/clock.py` (leaf, stdlib-only, no
  `requests`), re-export from `httpclient` for compatibility, and have both the scrapers and
  `dedup._recency` import from `core/clock`. Then `dedup` can validate its input without a
  transport dependency, and the clock stops being coupled to HTTP.

---

### S2 — `_recency` sits at tuple index 2 behind a gate it cannot influence, and one of its four volatile fields is a trust-forbidden zombie

- **Where:** `dedup.py:179-187`, specifically `dedup.py:183` and the ordering at
  `dedup.py:180-185`.
- **Breaks:** `rank()` is `(trust, validity, recency, str(value))`. `_recency` is compared only
  after `_trust_for` **and** `_validity` are exactly equal, and a strict-trust difference wins
  outright. Measured consequences (all verified by direct evaluation):
  - `website`: `_SOURCE_TRUST["osm"]["website"] == 3` (`dedup.py:103`) beats
    `_SOURCE_TRUST["google_places"]["website"] == 2` (`dedup.py:100`). So `_recency` is **dead**
    for the cross-source case: a Google website scraped today loses to an OSM website scraped in
    2020. `_VOLATILE` says "the fresher observation wins" (`dedup.py:113`) and the code does not
    do that for the field most worth being fresh.
  - `website_live`: no trust entry and no validity branch, so `_recency` is everything
    (previous finding).
  - `rating` / `review_count`: `_recency` decides **only** when both sides are
    `source="google_places"` — verified `_merge` returns `4.9` in both argument orders for a
    fresh/stale same-source pair.
  So `_recency`'s real, non-dead remit is narrower than `_VOLATILE` implies: two fields, one
  source. That is worth writing down, because the next person to touch `_pick` will assume the
  ordering means what the comment says it means.
- **Trigger:** `_pick("website", google_today, osm_2020) -> "https://stale.example.com"`.
- **Fix:** either promote `_recency` above `_validity` for fields in `_VOLATILE`
  (recency is stronger evidence than plausibility for a *changed* value), or delete
  `website_live` from `_VOLATILE` and give `website` an explicit `trust: recency` policy instead
  of an absolute integer — and put `website_live` in `_SOURCE_TRUST` so it is not decided by
  accident.

---

### S2 — `dedup.py` has no logging at all, so every `_recency` mis-ordering is unobservable; the two scrapers that *do* import `logging` never call the logger

- **Where:** `dedup.py` (zero `logging` import, zero `log`); `enricher.py` (zero `logging`;
  `print` at `:235, :262, :271, :273`); `scrapers/osm.py:1,7` and `scrapers/wikidata.py:1,7` both
  do `import logging` + `log = logging.getLogger(__name__)` and **never call `log`** (verified by
  grep); `scrapers/google_places.py` has no logging import at all. Only `httpclient.py:35,45`
  actually logs (`log.debug` at `:178, :204`).
- **Breaks:** this is what converts the S1s from "wrong" to "wrong and undetectable". A merge that
  silently prefers a 2020 observation over today's leaves no trace in stdout, no trace on stderr,
  and nothing in the CSVs except a plausible-looking value. The pipeline's only merge-stage
  visibility is the single line `print(f"After merge + dedup: {len(records)} unique businesses")`
  at `main.py:181`, which reports a count and nothing about decisions.
- **Trigger:** any of the S1 inputs above; look for the absence of output.
- **Fix:** at minimum emit one `log.info` per `_VOLATILE` conflict resolved by `_recency`, with
  both timestamps and both chosen values. Delete the two dead `logging` imports in `osm.py:1,7`
  and `wikidata.py:1,7` — a logger that is never called is worse than no logger, because it reads
  as observability that does not exist.

---

### S3 — The bare `dict` annotation at `dedup.py:155` is the only thing preventing a `dedup ↔ scrapers.base` import cycle

- **Where:** `dedup.py:155` (`_recency(record: dict) -> str`), `dedup.py:159` (`_pick`),
  `dedup.py:190` (`_merge`), `dedup.py:197` (`dedup`); type declared at `scrapers/base.py:5-28`.
- **Breaks:** `docs/audits/066-typing-at-scale.md:207` recommends
  `_merge(a: BusinessRecord, b: BusinessRecord) -> BusinessRecord`. Implementing that pulls a
  domain module's type dependency into the **scraper** package, and it is a cycle generator:
  `scrapers/base.py` wants canonical `normalize_phone` for storage, giving
  `scrapers.base → dedup` and, per 066, `dedup → scrapers.base`. The cycle is invisible today
  *only* because `dedup.py` uses bare `dict` everywhere and never imports `BusinessRecord`.
  Same for `_recency`: with `record: BusinessRecord`, `record.get("scraped_at")` returns `str` and
  the `or ""` becomes statically dead code (`scrapers/base.py:27` declares `scraped_at: str`,
  non-optional) — the compiler would be lying about `dedup.py:156`, which must tolerate the
  `None` that `main.load_master` really produces at `main.py:88-89`. The annotation and reality
  are already in conflict; the bare `dict` is hiding it rather than resolving it.
- **Fix:** under a `src` layout put the record type in `src/leadminer/core/records.py` (or a
  `contracts` module, as `066:205` says) as the single leaf both `scrapers` and `dedup` import.
  One direction, no cycle. Then correct `scraped_at` to `str | None` there before tightening
  `dedup`.

---

### S3 — `_VOLATILE` names a field the merge layer's own upstream never produces, and only a downstream module does — an undeclared dependency on `enricher`'s vocabulary

- **Where:** `dedup.py:114` (`_VOLATILE`), `dedup.py:183`; producers `osm.py:109`,
  `google_places.py:292`, `wikidata.py:117` (all `None`); consumer `enricher.py:250`; order
  `main.py:180` (dedup) → `main.py:187` (enrich).
- **Breaks:** the merge layer's volatility policy is written against the *enrichment* layer's
  output schema, without importing it and without any guard saying the dependency exists. Nothing
  stops someone adding a field to `enricher.check_websites` and forgetting `_VOLATILE`, or vice
  versa. Under a `src` layout, if `_VOLATILE` is lifted into `core/` (it must be, to be shared
  with the storage layer that 033 wants) then `core` needs `enricher`'s field vocabulary while
  `enricher` will need `core`'s `normalize_phone` — a cycle with no import statement to point at.
- **Fix:** declare the volatile-field set in the same `core/records.py` as the record type, next
  to the fields it names, and add a test that every name in `_VOLATILE` is a key in
  `main.FIELDS` (`main.py:33-40`) and a key some producer actually writes.

---

### S3 — The only test that reaches `_recency` cannot detect a format drift, and the wheel config does not ship the module that defines the contract

- **Where:** `tests/test_lead_signal.py:303-309` (`test_volatile_field_prefers_the_fresher_observation`
  — both timestamps are `"...T00:00:00+00:00"`, identical shape);
  `pyproject.toml:35-37`.
- **Breaks:** a single-format fixture cannot catch a cross-format bug — that is the whole class of
  the S1s. And `[tool.hatch.build.targets.wheel]` carries the comment
  *"The package imports a top-level sibling module, so both must ship"* followed by
  `include = ["scrapers/"]`, which does not name the siblings actually imported
  (`httpclient` at `osm.py:4` / `wikidata.py:4` / `google_places.py:27`; `enricher` at
  `google_places.py:26`; plus `dedup`, `main`, `cli`). The config does not express its own
  comment, so the distribution may ship `scrapers/` without the module that defines
  `_recency`'s input contract.
- **Fix:** parameterise the recency test over mixed formats (`...Z` vs `...+00:00`, `+04:00` vs
  `Z`, `YYYY-MM-DD HH:MM`, unparseable) and assert the *chronologically* newer observation wins;
  fix `pyproject.toml:35-37` to declare the whole import closure, or adopt a real `src/` layout
  where the closure is explicit by construction.

---

## Not a bug, but worth knowing

- **`dedup.py` is the healthiest module in the repo.** Its entire import block is
  `re` + `unicodedata` (`dedup.py:1-2`), both used for runtime value computation
  (`re` at `:44, :69, :116, :117`; `unicodedata` at `:67, :68`). **Neither is
  `TYPE_CHECKING`-only, and `dedup.py` imports nothing for logging** — there is no
  `TYPE_CHECKING` block, no `from __future__ import annotations`, and no logger anywhere in the
  file. So the answer to "imported only for a type annotation or only for logging" is *none*,
  within `_recency`'s tree. That is a clean result; the problems above are all in the *data*
  dependencies and in neighbouring modules.
- **Adjacent dead imports worth deleting while you are here:** `enricher.py:2 import requests` and
  `enricher.py:4 from urllib.parse import urljoin, urlparse` are **both entirely unused** (no
  `requests.`, `urljoin`, or `urlparse` reference anywhere in the file — only a comment at
  `enricher.py:125` mentions `requests.Session`). `enricher` needs `requests` only transitively
  through `httpclient.get_session` (`enricher.py:128`). And `scrapers/osm.py:1,7` /
  `scrapers/wikidata.py:1,7` import `logging` and create a logger that is never called.
- **`_recency` handles types more gracefully than its sibling, which is backwards.** For the
  record, `_recency` alone survives every one of these (verified):
  `datetime(2026,1,1) -> '2026-01-01 00:00:00'`, `20260101 -> '20260101'`,
  `None -> ''`, missing key `-> ''`, `0 -> ''`, `2026.1 -> '2026.1'`. Note that two of those
  degradations are *worse than useless*: `0` is indistinguishable from absent (the `or ""` at
  `dedup.py:156`), and `datetime` produces the space-separated form that loses every comparison
  (S1 #1). The coercion hides type errors instead of surfacing them.
- **`_recency` is not called for non-volatile fields**, gated at `dedup.py:183`
  (measured: 2 calls for a conflicting `rating`, 0 for a conflicting `address`). The `""` sentinel
  keeps the tuple homogeneous, which is fine. No perf concern; the `str()` on an existing `str`
  is a no-op in CPython.
- **`scrapers/whitelist.py:9` looks like a lazy import but is not** — it is inside the module
  docstring's usage example. There is no lazy-import surprise in the tree.
- **`_recency` has exactly one caller and no test of its own** (`dedup.py:183` only;
  `tests/test_lead_signal.py:65` imports `_merge` and `normalize_phone`). `cli.py` never calls
  `dedup`. Every merge decision in this system goes through one unexercised-in-isolation function.
- **A nice irony, free of charge:** `scrape.yml:87` names run archive directories with
  `TIMESTAMP=$(date -u +%Y-%m-%dT%H-%M)` — a fixed-width, lexicographically sortable form — while
  the `scraped_at` *inside* those archives is not. The project already knows how to spell a
  sortable UTC timestamp; it just doesn't do it in the column `_recency` depends on.

---

## Recommended order of work

1. **Parse, don't compare** (`dedup.py:155-156`, `dedup.py:176-177`). Introduce
   `core/clock.py::parse_ts` returning an aware `datetime`; make `_recency` return it; delete the
   `max(av, bv)` special case so there is exactly one comparison path. This closes S1 #1 and S1 #2
   together and removes the type-coercion divergence.
2. **Fail closed on a poisoned master** (`main.py:150`, `main.py:88-89`). Validate `scraped_at` on
   load and refuse the run rather than mis-merge, because the master is a human-editable Drive file
   on a closed rclone loop (`scrape.yml:56` → `main.py:150` → `scrape.yml:89`). One column, one
   `try`, one loud error.
3. **Separate the clocks** (`enricher.py:250`, `main.py:33-40`, `dedup.py:114`, `dedup.py:183`).
   Add `website_checked_at`, stamp it beside `website_live`, and make `_recency` field-aware so
   `website`/`website_live` resolve on the check clock. Without this, `website_live=True` is
   immortal and the dead-site pitch — the product's best signal — can never fire for a stable URL.
4. **Make the merge observable and test the contract** (`dedup.py`, `tests/test_lead_signal.py:303-309`).
   One `log.info` per `_recency`-resolved conflict; delete the dead `logging` imports in
   `osm.py:1,7` / `wikidata.py:1,7`; parameterise the recency test over mixed formats.
5. **Fix `_pick`'s ordering and `_VOLATILE`'s policy** (`dedup.py:180-185`, `dedup.py:114`,
   `dedup.py:97-110`). Give `website_live` a trust entry or drop it from `_VOLATILE`; decide
   explicitly whether recency outranks trust for volatile fields.
6. **Then, and only then, restructure** (S3s). `core/records.py` as the single leaf for the record
   type and the volatile-field set; `core/clock.py` as the stdlib-only clock; then tighten
   `dedup.py`'s annotations to `BusinessRecord`. Doing the typing first, as
   `066-typing-at-scale.md:207` proposes, walks you straight into `dedup ↔ scrapers.base`.
   While you are there, fix `pyproject.toml:35-37`, whose `include` list does not match its own
   comment.