# 350 — `_merge` (dedup.py:190) — dependency lens

## Verdict

`_merge`'s **import** closure is immaculate: 11 symbols, all inside `dedup.py`, with
only two stdlib module-level imports (`re`, `unicodedata` at `dedup.py:1-2`). It is a
leaf in the project graph, and a src-layout move requires **zero** import edits to it.

The problem is the *inverse* graph — the contracts `_merge` depends on that exist only
as duplicated string literals. `_SOURCE_TRUST` (`dedup.py:97-110`) is keyed by literals
that are re-typed at three producer sites (`scrapers/google_places.py:302`,
`scrapers/osm.py:119`, `scrapers/wikidata.py:127`) with **no import, no enum, no shared
constant and no test**. Renaming any one of those literals silently drops that scraper's
trust weight to `_DEFAULT_TRUST` (`dedup.py:151`) and permanently freezes four product
columns at their last-master values. Every tool in the repo — ruff, mypy, pyflakes, and
the existing `tests/test_lead_signal.py` — passes green while that happens.

## Dependency map

Computed by AST free-variable walk from `_merge` (`dedup.py:190`), not by inspection:

```
_merge                      dedup.py:190   entry
├─ _pick                    dedup.py:159
│  ├─ _is_missing           dedup.py:79    also <-- _validity
│  ├─ _sources_of           dedup.py:143   pipe-split of record["source"]
│  ├─ _trust_for            dedup.py:148
│  │  ├─ _sources_of        dedup.py:143   (re-invoked, uncached)
│  │  ├─ _SOURCE_TRUST      dedup.py:97    *** literal-keyed, unguarded ***
│  │  └─ _DEFAULT_TRUST     dedup.py:111
│  ├─ _validity             dedup.py:120
│  │  ├─ _is_missing        dedup.py:79
│  │  ├─ _EMAIL_OK          dedup.py:116
│  │  └─ _URL_OK            dedup.py:117
│  ├─ _recency              dedup.py:155
│  └─ _VOLATILE             dedup.py:114    *** literal-keyed ***
└─ (builtins: set, dict)
```

Module-level imports of `dedup.py`: `re` (line 1), `unicodedata` (line 2) — both stdlib.
`re` reaches `_merge` transitively through `_EMAIL_OK`/`_URL_OK`; `unicodedata` does
**not** reach `_merge` (it is used only by `normalize_name`, `dedup.py:67`).

**Edges crossing the module boundary: zero.** Intra-project import edges in the whole
package, for contrast:

```
main.py:25-31            -> scrapers.osm, scrapers.wikidata, scrapers.google_places,
                            scrapers.whitelist, dedup, enricher, pitch_recommender
scrapers/google_places.py:26 -> enricher        <- adapter depends on the layer above it
enricher.py:128          -> httpclient
scrapers/{osm,wikidata,google_places}.py:4,27 -> httpclient
dedup.py                -> (nothing)
```

`dedup` is the only leaf. There is no cycle today and none is reachable from `_merge`.

## Findings

### S1 — `_SOURCE_TRUST` keys are unguarded stringly-typed copies of the scrapers' `source=` literals; renaming one silently freezes `rating`, `review_count`, `lat`, `lon` at their last-master values

- **Where:** `dedup.py:97-110` (the table), consumed at `dedup.py:151`
  `return max(best, _SOURCE_TRUST.get(src, {}).get(field, _DEFAULT_TRUST))`;
  producers at `scrapers/google_places.py:302` (`source="google_places"`),
  `scrapers/osm.py:119` (`source="osm"`), `scrapers/wikidata.py:127` (`source="wikidata"`).
- **Breaks:** `_trust_for` uses default-*allow*: an unrecognised source name is not an
  error, it is a silent downgrade to `_DEFAULT_TRUST = 1` (`dedup.py:111`). Google's
  `rating`/`review_count`/`lat`/`lon` weight of `3` (`dedup.py:99`) becomes `1`. Because
  master rows loaded from CSV (`main.py:77-105`) still carry the **old** literal while
  fresh rows carry the **new** one, the master's stale snapshot outranks the fresh
  scrape and wins on every run, forever. No exception, no warning, no log line —
  `dedup.py` imports no `logging` at all.
- **Trigger** (verified by running the real module):

  ```python
  g = {"name":"A","rating":4.8,"review_count":120,"lat":33.9,
       "source":"gplaces",                      # google_places.py:302, renamed
       "scraped_at":"2026-09-05T00:00:00+00:00"}
  m = {"name":"A","rating":4.1,"review_count":5,"lat":33.8,
       "source":"google_places",               # still in last month's master CSV
       "scraped_at":"2026-09-01T00:00:00+00:00"}
  _merge(g, m)  # -> {'rating': 4.1, 'review_count': 5, 'lat': 33.8}
  ```

  A fresh Google scrape reporting 4.8 / 120 reviews is discarded in favour of the
  4.1 / 5-review row from the previous run. Directly contradictory inputs are also
  silently reconciled: `_merge` of a correct `{"source":"google_places"}` pair yields
  `review_count == 120`; the same pair with the literal respelled yields `5`.
- **Why nothing catches it:** the whole table is a module-level `dict[str, dict[str, int]]`
  (`dedup.py:97`). There is no `StrEnum`, no `Final`, no `Literal`, and
  `dedup.py:144` types `record` as bare `dict`. The link between producer and consumer
  is a coincidental string match across a package boundary, so ruff/mypy cannot see it
  and `tests/test_lead_signal.py` (which does exercise `_merge` at lines 279-322) uses
  hand-written literals at lines 273/276 rather than the producers' constants.
- **Fix:** introduce `class Source(StrEnum)` in a neutral module, use it as the literal at
  all three producer sites, type `_SOURCE_TRUST: dict[Source, dict[str, int]]`, and have
  `_sources_of` (`dedup.py:143`) return `set[Source]` so a stray value fails loudly.

### S1 — the final tiebreak is `str(value)`, so `review_count`, `region` and `linkedin` merge alphabetically

- **Where:** `dedup.py:184` (`str(value),  # deterministic final tiebreak`) reached from
  `dedup.py:187`; reached only after `_trust_for`, `_validity` and `_recency` all tie.
  `_validity` has cases for exactly five fields — `email` (`dedup.py:124`), `website`
  (`dedup.py:126`), `lat`/`lon` (`dedup.py:128`), `rating` (`dedup.py:134`) — and returns
  a flat `1` for everything else (`dedup.py:140`), so `review_count`, `region`,
  `linkedin` and all four scoring columns are unvalidated.
- **Breaks:** 10 of the 23 columns in `main.py:33-40` have no `_SOURCE_TRUST` entry and
  no special case in `_pick`. Three of them (`source` at `dedup.py:173`,
  `scraped_at` at `dedup.py:176`, `website_live` via `_VOLATILE` at `dedup.py:114`) are
  handled; the remaining **seven** — `region`, `country`, `linkedin`,
  `completeness_score`, `lead_score`, `industry_priority`, `recommended_service` — fall
  straight through to the alphabetical tiebreak. `"45" > "120"` and `"Zahle" > "Acre"`.
- **Trigger** (verified):

  ```
  _merge({'review_count':120}, {'review_count':45})    -> 45    (24x data loss)
  _merge({'region':'Acre'}, {'region':'Zahle'})        -> 'Zahle'
  _merge({'linkedin':'https://a.example/x'},
         {'linkedin':'https://z.example/x'})           -> 'https://z.example/x'
  _merge({'completeness_score':9},  {'completeness_score':10})  -> 9
  _merge({'completeness_score':100},{'completeness_score':99})  -> 99
  _validity('review_count', -5) -> 1   (a negative count is "valid")
  ```
- **Honest blast radius.** I checked which of the seven are recovered downstream and they
  mostly are: `country` is overwritten by `resolve_country` (`main.py:184`),
  `industry_priority` by `main.py:190`, `recommended_service` by `main.py:191`,
  `completeness_score` by `enricher.py:349`, `lead_score` by `main.py:197`. The residual
  exposure is `review_count` (consumed at `pitch_recommender.py:70`), `linkedin` and
  `region`. Reaching it requires a **master-vs-master** collision — two rows already in
  `all_businesses.csv` landing on one dedup key, which happens after any change to
  `normalize_phone`/`_KNOWN_COUNTRY_CODES` (`dedup.py:8-21`, `dedup.py:33`) re-keys
  previously-distinct rows. Rare by construction, but permanent once it happens: the
  corruption is written back to the cumulative master on every subsequent run.
- **Fix:** make `rank` type-aware — numeric fields compare numerically, not as text —
  and give `_validity` a numeric case with range checks for `review_count` (and a
  non-empty check for `linkedin`).

### S2 — `_merge` hardcodes nine `BusinessRecord` key literals and imports nothing from `scrapers/base.py`; the obvious fix inverts the layering and the follow-up creates a real cycle

- **Where:** field-name literals at `dedup.py:114` (`_VOLATILE`), `dedup.py:124`, `:126`,
  `:128`, `:134` (`_validity`), `dedup.py:173`, `:176` (`_pick`). Schema at
  `scrapers/base.py:5-28`. Signatures at `dedup.py:190` (`a: dict, b: dict -> dict`) and
  `dedup.py:159` (`a: dict, b: dict -> object`).
- **Breaks (layering):** `dedup` is the core domain; `scrapers/base.py` is the adapter
  layer, and it already imports only `abc`/`typing` (`scrapers/base.py:1-2`) so it is a
  clean leaf. Writing `from scrapers.base import BusinessRecord` at the top of
  `dedup.py` to close the gap would point the core at the outer adapter. Under
  `hatchling`'s wheel target, `pyproject.toml:36-37` already flags that these are
  "top-level sibling modules" and hand-lists `include = ["scrapers/"]` — a src layout
  makes `leadminer.dedup` reach *into* `leadminer.scrapers.base`, which is the shape
  that later makes every merge adapter look like a core dependency.
- **Breaks (cycle, concretely):** today `dedup → ∅`, so nothing. The cycle to design
  against is this sequence, each step of which is individually reasonable:
  1. `enricher` imports `dedup` to reuse `_is_missing` (`dedup.py:79`) or
     `normalize_phone` (`dedup.py:33`). `enricher → dedup`.
  2. `scrapers/google_places.py:26` already does `from enricher import infer_region`, so
     the adapter chain becomes `scrapers → enricher → dedup`. Still acyclic.
  3. Someone notices `_VOLATILE` (`dedup.py:114`) races `website_live`, whose tri-state
     domain is defined only at `enricher.py:154-156` and `enricher.py:250`, and adds
     `from enricher import LIVE, DEAD, UNKNOWN` to validate it in `_validity`. Now
     `dedup → enricher` **and** `enricher → dedup`: a genuine 2-node cycle, with
     `scrapers/google_places.py:26` pulled into it.
- **Fix (neutralises all three steps at once):** create `leadminer/schema.py` holding
  `BusinessRecord`, `Source`, `FIELDS`, and the `LIVE/DEAD/UNKNOWN` tri-state. Point
  `scrapers/base.py`, `dedup.py` and `enricher.py` at it. The cycle is impossible
  because nothing imports anything from it.
- **Note the reverse hidden edge:** `enricher.py:316` does
  `if "|" in str(record.get("source") or "")` to award the multi-source bonus, depending
  on the pipe-joined format produced at `dedup.py:174` — with no import and no shared
  constant. That is the dependency that closes the cycle in step 3, and it should move
  to `schema.py` as `MERGED_SOURCE_SEP`.

### S2 — `website_live` is raced on recency with a `str()` tiebreak over a tri-state domain that `dedup.py` does not model

- **Where:** `dedup.py:114` puts `website_live` in `_VOLATILE`; the ranking it feeds is
  `dedup.py:179-186`. `_validity` has **no case** for it, so `_validity("website_live",
  True)` and `_validity("website_live", False)` both return `1` (`dedup.py:140`);
  only `None` scores `-100` (`dedup.py:122-123`). Final tiebreak is `str(value)`
  (`dedup.py:184`), and `"False" < "None" < "True"`.
- **Breaks:** a live/dead conflict resolved at equal recency is decided by which word
  sorts later, not by which observation is fresher or better. `True` beating `False`
  happens to match intent, but it matches by accident, and `False` losing to a `True`
  means a server-confirmed-dead site is relabelled live — which removes it from the
  rebuild pitch at `pitch_recommender.py:73` and `:95` and cancels the `+20` at
  `enricher.py:304`. That is a lost sale, not a cosmetic diff.
- **Trigger:** two rows carrying a verdict, same `scraped_at`:
  `_merge({"website_live": True, "scraped_at": T}, {"website_live": False, "scraped_at": T})`
  → `True` on the strength of `"True" > "False"`.
- **Reachability caveat:** `main.py:180` runs `dedup` before `main.py:187` runs `enrich`,
  and all three scrapers hardcode `website_live=None` (`scrapers/osm.py:109`,
  `scrapers/google_places.py:292`, `scrapers/wikidata.py:117`). So this only fires on a
  master-vs-master collision, and the common master-vs-new case is accidentally safe
  (fresh `None` scores `-100`, the master's verdict survives). Same trigger class as the
  finding above.
- **Fix:** give `website_live` an explicit `_validity` case — `False` (server-confirmed)
  must outrank `None` (never reached) — and import the tri-state from `schema.py`.

### S2 — `httpclient.utc_now_iso()`'s timestamp format is an implicit input to two lexicographic comparisons in `_pick`, documented only in the other module

- **Where:** `dedup.py:177` `return max(av, bv)` and `dedup.py:183`
  `_recency(rec) if field in _VOLATILE else ""`, versus the producer contract at
  `httpclient.py:220-229`.
- **Breaks:** the coupling is acknowledged in `httpclient.py:224-225` — *"the naive
  value it returned also sorted incorrectly against any offset-aware timestamp in
  dedup._merge's string comparison"* — but `dedup.py` states no such requirement
  anywhere. If `utc_now_iso` moves to epoch seconds, to local time, or starts emitting
  mixed `Z`/`+00:00` suffixes, `_pick` silently mis-orders recency with no error.
  Mixed-offset inputs already order wrongly (verified):
  `max("2026-09-02T11:00:00", "2026-09-02T03:04:05+02:00")` returns the naive `11:00`
  string, though the aware value is `01:04` UTC.
- **Also latent:** `_recency` coerces with `str(...)` (`dedup.py:156`) but `max()` at
  `dedup.py:177` does not. A non-`str` `scraped_at` raises
  `TypeError: '>' not supported between instances of 'str' and 'int'` (reproduced).
  Not reachable today — `load_master` returns CSV strings (`main.py:86`) and `_is_missing`
  absorbs `None` first (`dedup.py:167-171`) — but it is an unguarded assertion that
  `scraped_at` is always a `str`.
- **Fix:** parse both operands to timezone-aware `datetime` in `_pick` and compare
  datetimes; assert offset-awareness so a naive value fails at the boundary rather than
  sorting wrong.

### S3 — `_pick`'s five-way helper fan-out re-derives `_sources_of` ~34× per merge and cannot be memoised without signature changes

- **Where:** `dedup.py:187` evaluates `rank(a, av)` *and* `rank(b, bv)` for every key;
  each `rank` (`dedup.py:179-186`) calls `_trust_for`, which calls `_sources_of`
  (`dedup.py:150`). `_sources_of` (`dedup.py:143`) does `str(...)` + `.split("|")` every
  time and has only **two** distinct inputs per merge.
- **Measured:** on a 23-key record, `_sources_of` is invoked 34× per `_merge`
  (136,000 calls / 4,000 merges), `_trust_for` 32×. Throughput is 14,324 merges/s on
  23-key records; `cProfile` puts 46% of `_merge`'s cumulative time in `_trust_for` +
  `_pick`.
- **Severity is low on purpose:** `dedup()` only calls `_merge` on actual collisions
  (`dedup.py:208`, `dedup.py:216`) and takes the `dict(record)` fast path otherwise
  (`dedup.py:210`, `dedup.py:218`), so this is O(collisions), not O(master size). At
  ~14k merges/s it will not be the bottleneck. Worth fixing because the *shape* of the
  dependency graph — five leaf helpers each taking `(field, record)` — means no caller
  can hoist the loop-invariant work without changing every signature.
- **Fix:** compute `{field: (trust, validity, recency)}` per record once at
  `dedup.py:191` and pass the precomputed table into `_pick`.

### S3 — `_merge`'s only out-of-module consumer imports it by its private name

- **Where:** `tests/test_lead_signal.py:65` `from dedup import _merge, normalize_phone  # noqa: E402`.
- **Breaks:** eight assertions (`tests/test_lead_signal.py:279-322`) pin `_merge`'s
  behaviour through an underscore-prefixed name with no `__all__`. Once the package
  gains a `src/` root and real exports, this becomes a test-suite dependency on an
  implementation detail, and any refactor of the rank tuple at `dedup.py:179-186`
  breaks tests for reasons unrelated to behaviour.
- **Fix:** expose `merge_records = _merge` publicly and have the test import that.

## Not a bug, but worth knowing

- **The import graph really is clean, and I want to be explicit about it rather than
  manufacture a violation.** `_merge`'s transitive closure is 11 symbols, every one of
  them in `dedup.py`. It cannot create a circular import, because it has no
  intra-project imports to create one with. A src-layout restructure leaves `dedup.py`
  untouched — unlike `main.py:25-31`, `enricher.py:128`, `scrapers/google_places.py:26-27`,
  `scrapers/osm.py:4` and `scrapers/wikidata.py:4`, which are all absolute top-level
  imports and all break on the move.
- **Nothing is imported only for a type annotation, and nothing only for logging.**
  `dedup.py` has zero `TYPE_CHECKING` blocks and does not import `logging` at all. The
  annotation-only import the brief asks about is absent *by omission* rather than by
  laziness: `dedup.py:190` and `dedup.py:159` type their parameters as bare `dict` when
  `scrapers/base.py:5` defines the schema. That is a **missing** dependency, and it is
  why the S1 above is invisible to mypy. The complete absence of logging is the other
  half of the same problem — `_merge` cannot report "I do not recognise this source", so
  S1 fails in total silence.
- **`pyproject.toml:36-37`** (`include = ["scrapers/"]`, with a comment about top-level
  sibling modules) is the constraint that makes the eventual src layout non-trivial. It
  is adjacent to this lens; flagging it because `dedup.py` is one of the modules that has
  to move.
- **Measured merge cost, for whoever profiles the pipeline next:** 1.40 s for 20,000
  sequential merges of 23-key records on this machine.

## Recommended order of work

1. `leadminer/schema.py` holding `BusinessRecord`, `Source` (a `StrEnum` matching
   `dedup.py:98/102/106`), `FIELDS` (from `main.py:33-40`), `MERGED_SOURCE_SEP`, and
   the `LIVE/DEAD/UNKNOWN` tri-state. Repoint `scrapers/base.py:1-2`, `enricher.py:154-156`
   and `dedup.py` at it. This closes S1-#1's root cause and pre-empts the
   `dedup ↔ enricher` cycle in S2-#1 before anyone writes the `dedup → enricher` edge.
2. Key `_SOURCE_TRUST` by `Source` and make `_sources_of` return `set[Source]` so a
   renamed producer literal raises instead of silently downgrading to
   `_DEFAULT_TRUST`. Add a test that iterates `Source` and asserts every member appears
   in `_SOURCE_TRUST` — that test is the thing that was missing and would have caught
   the S1.
3. Make `rank` (`dedup.py:179-186`) type-aware: numeric compare for the `_FLOAT_FIELDS`
   and `_INT_FIELDS` sets already declared at `main.py:43-44`, plus a range-checked
   `_validity` case for `review_count` and a non-empty case for `linkedin`. Closes S1-#2.
4. Add an explicit `_validity` case for `website_live` so server-confirmed `False`
   outranks `None` (S2-#2), then re-verify the rebuild-pitch path at
   `pitch_recommender.py:73` and `enricher.py:304`.
5. Parse `scraped_at` to aware `datetime` at `dedup.py:177` and `dedup.py:183` instead
   of comparing strings (S2-#3), and hoist the per-record trust/validity/recency table
   out of `rank` (S3-#1).
6. Rename `_merge` to a public `merge_records` (S3-#2) — do this at the same time as the
   src move, not before.