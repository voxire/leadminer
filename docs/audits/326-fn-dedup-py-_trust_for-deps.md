# 326 — `_trust_for` dependency review

**Target:** `dedup.py:148` — `def _trust_for(field: str, record: dict) -> int`
**Lens:** deps — transitive dependency map, surprising dependencies, layering
violations, src-layout circular-import risk, type-only / log-only imports.

**Snapshot audited** (this repo is being edited concurrently by implementation
agents; every line number below refers to these exact bytes):

| file | md5 | lines |
|---|---|---|
| `dedup.py` | `11ebdd66f40b309179f176ccb4db6ef7` | 232 |
| `enricher.py` | `4c6f1e6040a884cd2fde8951592b9041` | 352 |
| `main.py` | `f5dcc4ff13d09953f08e136409fab28f` | 246 |
| `pitch_recommender.py` | `7372ee22fe14ec1c72916ba96a04f8da` | 140 |
| `httpclient.py` | `060132613a9968fc0df295864c1da26a` | 228 |
| `scrapers/__init__.py` | `d41d8cd98f00b204e9800998ecf8427e` | **0** |
| `scrapers/base.py` | `a2ccfa69483043489d63a4bc961ad529` | 33 |
| `scrapers/google_places.py` | `a1db738377eae3ca55a7761da3b0c954` | 328 |
| `pyproject.toml` | `7121a2f284dc8b4d73835b22869d6b45` | 77 |

Nothing was modified. No scraper was executed and no network call was made.
The src-layout probes were run in a throwaway copy under
`$TMPDIR/opencode/`, never in the repo. (Note: another agent has left an
untracked `src-probe/` directory in the repo root — not mine, not touched.)

---

## Verdict

`_trust_for` is **import-clean and structurally excellent**: its entire
transitive closure is four nodes, zero module imports, zero third-party
dependencies, zero I/O. That is a genuinely good result and I want it on the
record.

The problem is that all of its real dependencies are *data* dependencies, and
they are unenforced. `_trust_for` reads its source names from a dict keyed by
bare string literals (`dedup.py:98,102,106`) that are duplicated as literals in
four other files with no shared constant, no enum and no test; and it is ranked
**first** in `_pick`'s `rank()` tuple (`dedup.py:181`), which starves the
`_recency` dependency that sits behind it — so `website` is declared volatile at
`dedup.py:114` and can never behave volatilely across sources. Both of those
produce wrong rows and a wrong pitch, silently, forever.

**The single most important thing:** fix the position of `_trust_for` in the
`rank()` tuple and stop `max()`-ing over a source union that only grows. Until
then, per-field source trust is a ratchet that can only ever make old data beat
new data.

---

## Dependency map

### Transitive closure of `_trust_for` — 4 nodes, 0 imports

```
_trust_for                       dedup.py:148-152   (leaf)
├── _DEFAULT_TRUST = 1           dedup.py:111       literal int
├── _sources_of(record)          dedup.py:143-145
│   └── str, dict.get, set       builtins only
│       ⚠ DATA dep: reads record["source"]          (see D3)
├── _SOURCE_TRUST                dedup.py:97-110    literal dict
│   ⚠ DATA dep: keys "google_places" / "osm" / "wikidata"  (see D3)
└── max()                        builtin
```

Neither `dedup.py:1` (`import re`) nor `dedup.py:2` (`import unicodedata`) is
reachable from `_trust_for`. Verified:

```
$ python3 -c "...inspect.getsource(_trust_for)..."
  _trust_for uses re?            False
  _trust_for uses unicodedata?   False
  _sources_of uses re?           False
```

`re` is used only by `normalize_phone:44`, `normalize_name:69` and the two
compiled patterns at `dedup.py:116-117`; `unicodedata` only by
`normalize_name:67-68`. Both are entirely outside this function's closure.

### Callers (reverse edges)

```
_pick          dedup.py:181   rank()[0]        ← primary caller
_merge         dedup.py:193   -> _pick         ←
dedup          dedup.py:208,216 -> _merge       ←
main.main      main.py:180    dedup(combined)  ← single-threaded, not in a pool
```

### Cross-module edges that exist because of this function

```
scrapers/google_places.py:302   source="google_places"  ─┐
scrapers/osm.py:119             source="osm"             ─┼─> record["source"]
scrapers/wikidata.py:127        source="wikidata"        ─┘         │
                                                                       ▼
                                                          dedup._sources_of:145
                                                                       │
dedup.py:98,102,106  _SOURCE_TRUST keys  <── must match, by convention ─┘

dedup._pick:174      writes "src1|src2"  ──┬──> main.py:39  (CSV column)
                                           ├──> dedup._sources_of:145  (read back)
                                           └──> enricher.py:316  lead_score +5
```

### The one layering inversion that matters

```
scrapers/google_places.py:26   from enricher import infer_region
```

The **acquisition** layer imports from the **post-processing** layer. This is
the latent defect that turns every future `dedup -> scrapers` edge into a cycle
(S2 below). It has nothing to do with `_trust_for` directly, and `_trust_for`
is not the cause — but `_trust_for` is the function whose type annotation is
what completes the cycle.

---

## Findings

### S1 — `_trust_for` is ranked above `_recency`, so a 21-month-old OSM website beats today's Google website

- **Where:** `dedup.py:181` (`_trust_for(field, rec)`) inside the `rank()` tuple
  at `dedup.py:179-185`; consumed at `dedup.py:187`.
- **Dependency:** `_trust_for` → `_recency` (`dedup.py:155-156`) and
  `_VOLATILE` (`dedup.py:114`) via `rank()` element ordering.
- **Breaks:** `website` and `rating` are both in `_VOLATILE` (`dedup.py:114`),
  which exists precisely so "the fresher observation wins". But element 0 of
  `rank()` is trust and element 2 is recency, and tuple comparison is
  lexicographic — so whenever the two records come from *different* sources
  whose trust for that field differs, **element 2 is never evaluated**.
  `_recency` is reachable only when trust *and* validity are already equal, i.e.
  only same-source pairs.
- **Trigger:** `dedup.py:104` gives `osm` website trust **3**;
  `dedup.py:100` gives `google_places` website trust **2**. Verified:

  ```
  old = {"website":"http://dead.example","scraped_at":"2025-01-01T00:00:00+00:00","source":"osm"}
  new = {"website":"https://live.example","rating":4.6,"review_count":210,
         "scraped_at":"2026-10-01T00:00:00+00:00","source":"google_places"}

  _trust_for("website", old)  == 3
  _trust_for("website", new)  == 2
  "website" in _VOLATILE      == True     # so recency SHOULD decide
  _pick("website", old, new)  == "http://dead.example"
  _recency(old) = 2025-01-01  vs  _recency(new) = 2026-10-01   # never consulted
  ```

  End-to-end through `main.py:180` → `main.py:187` → `main.py:190-197`:

  ```
  merged website  : http://dead.example         (was: https://live.example)
  merged rating   : 4.6                        (Google's — correct)
  website_live    : False                      (enricher.py:250, DEAD)
  recommended     : 'Website rebuild + maintenance'
  lead_score      : 55

  counterfactual (had Google's URL survived):
  recommended     : 'RTYLR commerce OS (POS, online ordering, CRM)'
  lead_score      : 45
  ```

  A business with a working site is sold a rebuild. `enricher.py:303-304`
  awards `+20` for the DEAD verdict and `pitch_recommender.py:57-58` returns
  the rebuild pitch at Tier 1 — the highest-conviction string in the whole
  output.
- **Why S1:** wrong results that invert the core sales signal, with no warning.
- **Fix:** rank recency above trust for fields in `_VOLATILE`; trust should be
  the tiebreak within an observation window, not a permanent trump.

---

### S1 — `max()` over a monotonically growing source union launders trust, freezing stale values forever

- **Where:** `dedup.py:151` — `best = max(best, _SOURCE_TRUST.get(src, ...) ...)`
  over `_sources_of(record)`, combined with `dedup.py:174` which *unions*
  provenance rather than keeping it per-value.
- **Dependency:** `_trust_for` depends on `record["source"]` being an
  accurate description of *which sources contributed this field*. `_pick:174`
  guarantees it is not — it unions sources from the whole record.
- **Breaks:** `dedup()` merges repeatedly into the same slot
  (`dedup.py:208,216`). After the first merge the accumulator's `source` is
  `"google_places|osm"`, so `_trust_for` returns Google's trust-3 rating weight
  *forever*, even in later runs where Google contributed nothing. Because trust
  outranks recency (S1 above), a months-old Google rating permanently beats a
  rating observed today. There is no decay, no TTL, no "last observation"
  bookkeeping — and because the value written to the master CSV
  (`main.py:39`) is the merged one, the error is persisted across runs.
- **Trigger:** cafe rated 4.9 by Google in Jan 2025; Google is later
  rate-limited or the key is revoked; OSM reports **2.1** in Oct 2026.

  ```
  run1 = _merge(google{rating:4.9, @2025-01-01, google_places},
                osm    {website,      @2025-01-01, osm})
  # master row -> rating=4.9  source=google_places|osm  trust(rating)=3

  run2 = _merge(osm{rating:2.1, @2026-10-01}, run1)     # main.py:179-180 order
  # fresh osm: trust=1, recency=2026-10-01
  # master   : trust=3, recency=2025-01-01
  # merged row -> rating=4.9                              <-- WRONG, and permanent

  truth (fresh OSM only) : rating=2.1 reviews=31
                           pitch='Reputation + SEO turnaround'  lead_score=50
  what actually ships    : rating=4.9 reviews=95
                           pitch='RTYLR commerce OS (...)'          lead_score=45
  ```

  `enricher.py:312-314` (`rating < 4.0` → `+10`) and
  `pitch_recommender.py:73-74` (`"Reputation + SEO turnaround"`) both become
  unreachable for that business, permanently. `enricher.py:316-317` also grants
  a `+5` "multi-source confirmation" bonus for the `|` it inherited, so the
  laundering pays twice.
- **Why S1:** silent, permanent, self-reinforcing wrong results.
- **Fix:** carry provenance per field (a `{field: {source: (value, ts)}}`
  shadow map) or at minimum make `_trust_for` read the *contributing* source
  for that field rather than the record-level union.

---

### S2 — the source identifiers are string literals duplicated 10× across 5 files, with no constant, no enum, and no test

- **Where:** `_SOURCE_TRUST` keys at `dedup.py:98, 102, 106`.
- **Dependency:** `_trust_for` → `scrapers/{google_places,osm,wikidata}.py`
  *by string equality across a module boundary*.
- **Breaks:** the contract "a scraper writes `source=<its own module name>` and
  `dedup` keys its trust table on that exact string" is asserted in ten
  production literals in five files with nothing to enforce it:

  | site | literal |
  |---|---|
  | `scrapers/google_places.py:302` | `source="google_places"` |
  | `scrapers/osm.py:119` | `source="osm"` |
  | `scrapers/wikidata.py:127` | `source="wikidata"` |
  | `dedup.py:98` | `"google_places": {` |
  | `dedup.py:102` | `"osm": {` |
  | `dedup.py:106` | `"wikidata": {` |
  | `cli.py:52,53,54` | registry keys |
  | `cli.py:290` | `choices=[...]` |

  `_SOURCE_TRUST.get(src, {})` at `dedup.py:151` swallows a miss with no
  `KeyError`, no log line, and no warning. Rename one literal and the trust row
  dies silently.
- **Trigger:** someone renames `source="wikidata"` to
  `source="wikidata_sparql"` at `scrapers/wikidata.py:127` — a completely
  reasonable edit given `docs/audits/019-wikidata-sparql.md` and
  `docs/audits/096-osm-to-saudi.md` exist. Simulated:

  ```
  _SOURCE_TRUST["wikidata_sparql"] = _SOURCE_TRUST.pop("wikidata")
  _trust_for("email", {"source":"wikidata"})        == 1   # old master CSV rows
  _trust_for("email", {"source":"wikidata_sparql"}) == 2   # new rows
  ```

  No exception. Same business, two trust regimes depending on which CSV vintage
  the row came from — and because `main.py:150` reloads the old master every
  run, the corpus is now split by run date with nothing marking the boundary.
- **Aggravating:** every degradation funnels to the same floor
  (`_DEFAULT_TRUST = 1`, `dedup.py:111`), which is *identical to what a record
  with no provenance at all scores*. So "known-untrusted source", "typo",
  "renamed source" and "source key absent" are indistinguishable:

  ```
  source=None       -> 1      source='OSM'      -> 1
  source=''         -> 1      source=' osm'    -> 1
  source='seirpapi' -> 1      source='osm '    -> 1
  ```

  Case sensitivity (`"OSM"` → 1) and whitespace (`" osm"` → 1) are also silent.
  A source name that ever picks up a trailing space, a different case, or a
  `str.upper()` somewhere in the pipeline drops that source to the floor with
  no diagnostic whatsoever.
- **Fix:** one `leadminer/sources.py` with `SOURCE_OSM = "osm"` etc., imported
  by `dedup`, all three scrapers and `cli`; plus a test that asserts
  `set(_SOURCE_TRUST) == set(ALL_SOURCES)`.

---

### S2 — under a src layout, the type annotation `mypy strict` already demands becomes a hard `ImportError`

- **Where:** `dedup.py:148` (`record: dict`), consumed against
  `scrapers/base.py:5-28` (`BusinessRecord`).
- **Dependency:** the correct annotation is `record: BusinessRecord`, which
  requires `dedup` to import from `scrapers`. Under the current flat layout that
  is safe only because **`scrapers/__init__.py` is 0 bytes**
  (md5 `d41d8cd98f00b204e9800998ecf8427e`).
- **Breaks:** `pyproject.toml:66` sets `strict = true`, and mypy's `--strict`
  includes `--disallow-any-generics` ("you can't use a bare `x: list`") — so
  `record: dict` is already a mypy error today and the fix is forced. But there
  is no `leadminer/` package, so `from leadminer.scrapers import BusinessRecord`
  (the natural form an engineer writes) fails immediately:

  ```
  ImportError: cannot import name 'BusinessRecord' from 'leadminer.scrapers'
  ```

  Add the obvious re-export facade to `scrapers/__init__.py` — four lines, and
  an agent attempted exactly this edit *during this audit* — and `dedup`
  transitively imports `scrapers.google_places`, which imports `enricher`
  (`scrapers/google_places.py:26`). Add the equally natural second fix,
  `enricher` reusing `dedup.normalize_phone` in `lead_score`, and the cycle
  closes.
- **Reproduced** in a src-layout copy under `$TMPDIR/` (no repo files touched):

  ```
  entry import leadminer.dedup
    -> ImportError: cannot import name 'normalize_phone' from partially
       initialized module 'leadminer.dedup' (most likely due to a circular import)

  cycle: leadminer.dedup -> leadminer.scrapers(__init__)
                        -> leadminer.scrapers.google_places
                        -> leadminer.enricher
                        -> leadminer.dedup
  ```

  Worst property: **it is entry-point dependent.** `import leadminer.main`
  succeeds; `import leadminer.dedup` and
  `from leadminer.dedup import _merge, normalize_phone` both fail. CI would
  keep passing (`.github/workflows/scrape.yml:69` runs `python main.py`) while
  the test suite breaks — and the test suite imports dedup *first*
  (`tests/test_lead_signal.py:65`). Reordering `scrapers/__init__.py` so
  `from .base import ...` comes last — equally natural — breaks the production
  entry point too:

  ```
  entry import leadminer.main
    -> ImportError: cannot import name 'BusinessRecord' from partially
       initialized module 'leadminer.scrapers'
  ```
- **Why S2, not S1:** it does not affect the data today. It is one mandatory
  annotation fix away, and the fix is mandatory.
- **Fix:** put `BusinessRecord` (and the source-name constants) in a leaf module
  both sides can import — `leadminer/types.py` / `leadminer/sources.py` — and
  delete `scrapers/google_places.py:26`'s dependency on `enricher` by moving
  `infer_region` down into a region-inference leaf module. Then neither
  `scrapers/__init__.py` nor `enricher` needs to be on `dedup`'s path.

---

### S3 — `record: dict` and three sibling signatures are bare generics; `mypy strict` rejects all four

- **Where:** `dedup.py:148` (`record: dict`), `dedup.py:143` (`record: dict`),
  `dedup.py:190` (`a: dict, b: dict -> dict`), `dedup.py:159`
  (`a: dict, b: dict`).
- **Also incomplete (mypy `--disallow-untyped-defs`):** `dedup.py:79`
  `_is_missing(value)`, `dedup.py:120` `_validity(field, value)`,
  `dedup.py:179` `rank(rec, value)`. `ruff --select ANN` confirms the latter
  three (`ANN001` at `dedup.py:79:17`, `120:27`, `179:25`).
- **Breaks:** `pyproject.toml:64-68` declares `strict = true`. mypy `--strict`
  enables `--disallow-any-generics` and `--disallow-untyped-defs`. So the repo
  as configured cannot type-check. It also means `_trust_for` cannot be given
  the `BusinessRecord` annotation that S2 depends on without a wider edit.
- **Fix:** `def _trust_for(field: str, record: BusinessRecord) -> int` with
  `Mapping[str, object]` for the mutating helpers (`_merge`, `_pick` write to
  the result), and `object` for `_validity`/`_is_missing` parameters.

---

### S3 — two dependency declarations, and CI installs the dead one

- **Where:** `requirements.txt:1-3` vs `pyproject.toml:16-18`;
  `.github/workflows/scrape.yml:39`.
- **Dependency:** the pipeline's entire third-party closure is
  `requests` (`enricher.py:2`, `httpclient.py:43`) plus `urllib3` via requests.
- **Breaks:** `scrape.yml:39` runs `uv pip install --system -r requirements.txt`
  — the *stale* file — not `pyproject.toml`. Meanwhile `requirements.txt:2-3`
  declare `beautifulsoup4==4.12.3` and `lxml==5.2.2`, and
  `grep -rn 'bs4\|BeautifulSoup\|lxml' *.py scrapers/*.py` returns **nothing**:
  both are dead weight pinned in the only install path production actually uses.
  The two files also disagree on `requests` (`==2.32.3` vs `>=2.32.3,<3`), so
  "the version we tested" and "the version CI runs" are different claims.
- **Fix:** delete `requirements.txt` and change `scrape.yml:39` to
  `uv pip install --system .` (or `-e ".[dev]"`).

---

### S3 — dead imports left behind by the `httpclient` extraction

- **Where:** `enricher.py:2` (`import requests`), `enricher.py:4`
  (`from urllib.parse import urljoin, urlparse` — **both names unused**).
- **Dependency:** leftover edges from before `enricher` started calling into
  `httpclient` (`enricher.py:128`). `ruff check --select F401` confirms all
  three.
- **Why it matters for this lens:** `enricher.py:2` is the reason
  `tests/test_lead_signal.py:23-60` has to hand-roll a fake `requests` module
  and a fake `urllib3` module before it can import anything.
  **`dedup.py` needs no such stub** — its closure is pure stdlib — which is
  precisely why `dedup` tests stay cheap. Removing the dead imports removes the
  need for that stub scaffolding entirely.
- **Also:** `enricher.py:128` is a module-level import buried 127 lines into the
  file, after unrelated code. It works, but it is invisible to a reader
  scanning the import block, and it is exactly the kind of thing that gets
  duplicated when someone adds a second consumer.

---

## Not a bug, but worth knowing

- **`_trust_for`'s transitive closure imports nothing.** Four nodes, zero module
  imports, zero third-party deps, zero I/O, zero logging. If you want one thing
  preserved through any refactor of this file, preserve that. It is the only
  reason `dedup` is testable in a bare checkout without the `requests` stub that
  `tests/test_lead_signal.py:23-60` needs for `enricher`.
- **`dedup.py` has no logging at all — and that is why the two S1s are
  silent.** `dedup` runs outside every executor (`main.py:180`; the pool at
  `main.py:160-168` has already been joined), so a misconfigured source id, an
  unranked field or a laundered trust value produces no output whatsoever. One
  `log.warning` when `_sources_of(record)` yields a token missing from
  `_SOURCE_TRUST` would convert S2's silent failure into a visible one at zero
  cost.
- **`_trust_for` is not a thread-safety concern.** `_SOURCE_TRUST` is read-only
  and `_merge` builds fresh dicts (`dedup.py:191-194`); `dedup` runs
  single-threaded at `main.py:180`. The 40-thread pool at `enricher.py:237`
  never touches this code. (`httpclient.py:58` uses thread-local sessions for
  the opposite reason — that pool *does* share state.)
- **`_pick` deliberately bypasses `_trust_for` for `source` and `scraped_at`**
  (`dedup.py:173-177`). Provenance is unioned, not ranked — correct. But it is
  the *mechanism* by which a record gains provenance it never had for a given
  field, which is the root of the S1 laundering. Worth knowing before anyone
  "fixes" `_pick` into ranking provenance too.
- **The 0-byte `scrapers/__init__.py` is load-bearing.** It is the only thing
  keeping `dedup -> scrapers.base` from dragging in
  `scrapers.google_places -> enricher`. An agent wrote a re-export facade into
  that file at 23:02 during this audit, which immediately produced
  `ImportError: cannot import name 'infer_region' from 'enricher'`, and then
  reverted it. The file is currently empty again. It needs a comment saying so
  before someone "tidies" it.
- **Zero direct test coverage for `_trust_for`.** `grep -rn '_trust_for\|_SOURCE_TRUST'`
  matches only `dedup.py`. `tests/test_lead_signal.py:278-284` exercises it
  transitively through `_merge`, but with `source` values that are all valid,
  all same-source for the multi-field cases, and never `None`, never unknown,
  never compound, never `"OSM"`. Every failure mode in S2 is untested.
- **`main.py:183-184` runs `resolve_country` *after* `dedup` at `main.py:180`,
  while `dedup` itself calls `normalize_phone` at `dedup.py:205` using
  `record.get("country") or "LB"`.** So the country used for the dedup key and
  the country written to the row are resolved by two different code paths at two
  different times. `_trust_for` is unaffected, but any future work that wants
  source-aware keying will have to deal with this ordering first.

---

## Answers to the lens's two explicit questions

**Imported only for a type annotation:** none in `dedup.py`. There is not a
single `TYPE_CHECKING` block, a `from __future__ import annotations`, or a
project-module import anywhere in the file — `dedup.py:1-2` is the complete
import list. That is why S2 is a *future* problem and not a present one: the
type-only import you would add is precisely the edge that closes the cycle.

**Imported only for logging:** none. `dedup.py` imports no logging module, and
`_trust_for` emits nothing.

**Elsewhere in the dependency chain, for completeness:**

| import | site | actually used? |
|---|---|---|
| `requests` | `enricher.py:2` | **no** — `F401`, dead since the `httpclient` extraction |
| `urljoin`, `urlparse` | `enricher.py:4` | **no** — both `F401` |
| `from httpclient import ...` | `enricher.py:128` | yes, but buried mid-file |
| `import urllib3` | `enricher.py:327` | yes, inside `enrich()` — suppresses TLS warnings even though `_fetch_website:175` now uses `verify=True`; the suppression is vestigial |
| `requests` | `httpclient.py:43` | yes — the only real third-party import in the project |
| `from .base import BusinessRecord` | `scrapers/google_places.py:28` | type-only in practice, but not marked `TYPE_CHECKING` |

---

## Recommended order of work

1. **Fix the `rank()` tuple order** (`dedup.py:179-185`): recency must be
   evaluated before trust for `_VOLATILE` fields. One-line change, removes an
   entire class of "old data beats new data" wrongness, and unblocks everything
   else. (S1 #1)
2. **Make trust attributable to a field's actual contributor**, not the
   record-level union — otherwise `_VOLATILE` can never work even after step 1.
   Cheapest interim version: have `_pick:174` record which sources actually
   supplied each surviving value, and let `_trust_for` read that. (S1 #2)
3. **Extract `leadminer/sources.py`** with `SOURCE_OSM` / `SOURCE_SA`-style
   constants plus `ALL_SOURCES`, import it from `dedup`, the three scrapers and
   both `cli` sites, and add `assert set(_SOURCE_TRUST) <= set(ALL_SOURCES)` as
   a test. Then add a `log.warning` in `_sources_of` for unrecognised tokens.
   (S2, first half)
4. **Break `scrapers/google_places.py:26 -> enricher`** by moving
   `infer_region` into a leaf module. Do this *before* step 3 is allowed to
   touch `scrapers/__init__.py`, and add a comment to that 0-byte file saying it
   must stay empty until step 5 lands. (S2, second half — and this one is
   time-sensitive, see the concurrent-edit note above)
5. **Then and only then** do the `src/` layout, annotate `_trust_for` with
   `BusinessRecord`, and delete `scrapers/__init__.py`'s emptiness constraint.
   Add a test that asserts `import dedup` works standalone so the cycle can
   never silently return. (S2, second half)
6. **Delete `requirements.txt`** and point `scrape.yml:39` at `pyproject.toml`
   so `beautifulsoup4` and `lxml` stop being installed in production for
   nothing. (S3)
7. **Strip the dead imports** at `enricher.py:2` and `enricher.py:4`, hoist
   `enricher.py:128` to the import block, and fix the `dedup.py:1:1` `I001`
   lint failure (the repo's own `ruff check .` reports 42 errors; this is the
   only one in `dedup.py`). (S3)

Steps 1-2 are the only ones that change what a customer is pitched. Steps 3-5
are the ones that stop the next engineer from shipping a silent regression. Do
1 and 2 before touching anything structural.