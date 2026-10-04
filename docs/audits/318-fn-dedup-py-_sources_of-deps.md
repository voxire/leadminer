# 318 — `_sources_of` (dedup.py:143): dependency audit

## Verdict

`_sources_of` has **zero import dependencies** — `dedup.py` imports only `re` and
`unicodedata`, and there are **zero module-level import cycles in the whole tree**
(AST-verified). That is the good news, and it is genuinely good: the target function is
the cleanest thing in the codebase from a dependency standpoint.

The bad news is that this cleanliness is achieved by having **no type for the thing the
function parses**. `source` is a magic string with a `|` wire format that `_sources_of` is
supposed to own but doesn't exclusively own: `enricher.py:316` re-implements "is this
multi-source" as a substring test, `cli.py:120` re-implements "count by source" as an
atomic `Counter`, `main.py:85-104` persists the format verbatim with no validation, and
three scrapers hardcode the vocabulary as literals. `_sources_of` is the only *decoder*,
and the only *encoder* is `_pick` at `dedup.py:174`.

The expensive consequence is not an import cycle — it is **`_trust_for` taking `max` over
the source union** (`dedup.py:148-152`). Because `_pick` unions `source` at
`dedup.py:174` and then re-reads that union to compute trust at `dedup.py:181`, a merged
record lends its strongest constituent's trust to a value that constituent never
supplied. That is **S1**: it makes `category` — and therefore `industry_priority`,
`recommended_service`, and membership in `sales_ready.csv` — depend on the order the
`ThreadPoolExecutor` happened to deliver scrapers in (`main.py:160-166`). Proven below
with the same three records producing two different, mutually exclusive business
outcomes.

---

## The dependency map

### Call graph (transitive closure of `_sources_of`, verified with `inspect`)

```
_sources_of                dedup.py:143-145   -> (no project calls)
  ^                          called by: _trust_for  dedup.py:150
  ^                                       _pick      dedup.py:174
_trust_for    dedup.py:148-152   -> _sources_of                 [reads _SOURCE_TRUST :97, _DEFAULT_TRUST :111]
_recency      dedup.py:155-156   -> (nothing)
_validity     dedup.py:120-140   -> _is_missing :79             [reads _EMAIL_OK :116, _URL_OK :117]
_is_missing   dedup.py:79-90     -> (nothing)
_pick         dedup.py:159-187   -> _is_missing, _recency, _sources_of, _trust_for, _validity
                                    [reads _VOLATILE :114]
_merge        dedup.py:190-194   -> _pick
dedup         dedup.py:197-232   -> _merge, normalize_phone :33, normalize_name :66, _extract_city :72
normalize_phone  dedup.py:33-63  -> re (dedup.py:1)  [_COUNTRY_CODES :23, _KNOWN_COUNTRY_CODES :8, _MIN_DIGITS :30]
normalize_name   dedup.py:66-69  -> unicodedata (dedup.py:2), re
_extract_city    dedup.py:72-76  -> (nothing)
```

11 functions, 9 module globals, **2 stdlib imports, 0 project imports, 0 third-party
imports.** Nothing is imported for annotations and nothing is imported for logging in
this closure.

### Module-level import graph (AST-extracted, project edges only)

```
cli.py                      -> (none)              # all project imports are function-local
dedup.py                    -> (none)              <== TARGET MODULE
enricher.py                 -> httpclient, requests
httpclient.py               -> requests
main.py                     -> scrapers.{osm,wikidata,google_places,whitelist},
                               dedup, enricher, pitch_recommender
pitch_recommender.py        -> (none)
scrapers/__init__.py        -> (none)
scrapers/base.py            -> (none)              # abc, typing only
scrapers/google_places.py   -> enricher, httpclient, requests, .base
scrapers/osm.py             -> httpclient, .base
scrapers/whitelist.py       -> (none)
scrapers/wikidata.py        -> httpclient, .base

module-level project cycles found: 0
```

Six modules are import-isolated leaves: `cli.py`, `dedup.py`, `pitch_recommender.py`,
`scrapers/base.py`, `scrapers/whitelist.py`, `scrapers/__init__.py`.

### The `source` field's real producers and consumers (the actual dependency surface)

`_sources_of` has no import edge to any of these. It has a **string-format** edge.

| Role | Site | What it does with `source` |
|---|---|---|
| producer | `scrapers/osm.py:119` | `source="osm"` (literal) |
| producer | `scrapers/wikidata.py:127` | `source="wikidata"` (literal) |
| producer | `scrapers/google_places.py:302` | `source="google_places"` (literal) |
| type contract | `scrapers/base.py:26` | `source: str` — says nothing about the encoding |
| consumer / encoder | `dedup.py:173-174` | `"|".join(sorted(_sources_of(a) \| _sources_of(b)))` |
| consumer / decoder | `dedup.py:144-145` | `raw.split("|")` — the only real decoder |
| consumer / trust | `dedup.py:151` | `_SOURCE_TRUST.get(src, {})` |
| consumer / scoring | `enricher.py:316` | `if "\|" in str(record.get("source") or "")` — **substring test, never splits** |
| consumer / reporting | `cli.py:120` | `Counter(r.get("source"))` — **treats it as atomic** |
| persistence | `main.py:39`, `main.py:85-104` | `FIELDS` + `load_master`; format stored and re-read, never validated |
| CLI vocabulary | `cli.py:52-54`, `cli.py:290` | `registry` keys + argparse `choices` — 2 more copies |
| test fixtures | `tests/test_lead_signal.py:74,204,273,276` | more copies |

`_sources_of`'s behaviour across every shape the system can hand it (13 inputs, all
executed against the real function):

```
  fresh scraper record         'osm'                        -> {'osm'}                    recognised={'osm'}
  two merged scrapers          'google_places|osm'          -> {'google_places','osm'}   recognised=both
  three merged                 'google_places|osm|wikidata' -> all three                  recognised=all three
  load_master blank cell       None                         -> set()                      recognised={}
  key absent entirely          {}                           -> set()                      recognised={}
  operator typed spaces        'osm | wikidata'             -> {'osm ',' wikidata'}       recognised={}     <-- ALL LOST
  operator typed a comma       'osm, wikidata'              -> {'osm, wikidata'}          recognised={}     <-- ALL LOST
  operator typed a capital     'Google_Places|OSM'          -> {'Google_Places','OSM'}    recognised={}     <-- ALL LOST
  operator typed a list        ['osm','wikidata']           -> {"['osm', 'wikidata']"}    recognised={}     <-- ALL LOST
  value is an int              123                          -> {'123'}                    recognised={}
  double separator             'osm||wikidata'              -> {'osm','wikidata'}         recognised=both
  trailing separator           'osm|'                       -> {'osm'}                    recognised={'osm'}
```

The function is **total** — it never raises on any of these. That is the correct shape
for a merge function, but it means a corrupted vocabulary can never announce itself; it
degrades to "one unrecognised token", which `_trust_for` then scores as
`_DEFAULT_TRUST` (dedup.py:111).

### `src/` layout simulation

Built a verbatim copy of all 8 modules at `<tmp>/src/leadminer/` (repo untouched) and
imported each one:

```
A) tree at src/leadminer/, sys.path = <repo>/src   (the correct src layout)
   OK       leadminer.dedup                              <-- target is layout-agnostic
   OK       leadminer.cli
   FAILS    leadminer.scrapers.osm            ModuleNotFoundError: No module named 'httpclient'
   FAILS    leadminer.scrapers.wikidata       ModuleNotFoundError: No module named 'httpclient'
   FAILS    leadminer.scrapers.google_places  ModuleNotFoundError: No module named 'enricher'
   FAILS    leadminer.main                    ModuleNotFoundError: No module named 'scrapers'

B) same tree installed into site-packages  (what pyproject.toml:33 promises)
   FAILS    every module                       ModuleNotFoundError: No module named 'leadminer'

C) control: current flat layout
   OK  dedup / scrapers.osm / scrapers.wikidata / scrapers.google_places / main / cli
```

**Nothing here is a cycle.** The `src/` move is 6 `..` prefixes
(`main.py:25-28` ×4, `main.py:29-30` ×2, `main.py:31` ×1, `scrapers/osm.py:4`,
`scrapers/wikidata.py:4`, `scrapers/google_places.py:26-27`, `enricher.py:128`). Note that
`leadminer.dedup` imports cleanly in **both** A and B — because it has no project
imports, the target module is the one module in the tree that a restructure cannot break.

Case B is the real packaging finding and it is not an import problem at all:
`pyproject.toml:35-37` declares `include = ["scrapers/"]`, so the wheel ships `scrapers/`
and nothing else. `dedup.py`, `enricher.py`, `httpclient.py`, `main.py`, `cli.py` and
`pitch_recommender.py` are all absent from the artifact — yet `pyproject.toml:33`
advertises `leadminer = "cli:main"`. Per hatchling's file-selection semantics `include`
is a whitelist filter, so this is the correct reading of the config; **I could not
execute `pip wheel` to confirm (hatchling is not installed and BRIEF.md:75 forbids
installing), so verify with one command before acting on it.** If it holds, the wheel is
100% broken at import: it ships the three scrapers and none of their dependencies.

---

## Findings

### S1 — `_trust_for` takes `max` over the source *union*, so `category` (and therefore `industry_priority`, `recommended_service`, and `sales_ready.csv` membership) depends on scraper completion order

- **Where:** `dedup.py:148-152` (`max` over `_sources_of(record)`),
  `dedup.py:174` (unions the sources), consumed at `dedup.py:181`
  (`_trust_for(field, rec)` inside the `rank` tuple).
- **Breaks:** `_pick` builds `merged = _merge(acc, record)` and *re-reads the merged
  dict* on the next iteration (`dedup.py:208`). Once `merged["source"]` is
  `"google_places|wikidata"`, `_trust_for("category", merged)` returns
  `max(3, 1) = 3` — Google's trust — even though the surviving `category` came from
  Wikidata, which is worth 1. The trust score therefore describes "the best trust any
  constituent assigns to this field", not "the trust of the source that supplied the
  winning value". Those coincide only when the highest-trust contributor also won.
- **Enumerate the blast radius, don't assume it.** An error requires
  `tQ=1` (owner), `tP=3` (union partner), `tS=2` (challenger) — a three-level gap.
  Across all 13 fields in `_SOURCE_TRUST`:

  ```
  address       {gp:3, osm:2, wd:2}      email       {gp:1, osm:3, wd:2}   <-- would need gp to own it
  category      {gp:3, osm:2, wd:1}  FLIPABLE     facebook    {gp:1, osm:3, wd:1}
  instagram     {gp:1, osm:3, wd:1}      lat/lon     {gp:3, osm:1, wd:1}
  name          {gp:3, osm:2, wd:2}      phone       {gp:2, osm:2, wd:2}     rating      {gp:3, osm:1, wd:1}
  review_count  {gp:3, osm:1, wd:1}      website     {gp:2, osm:3, wd:2}     whatsapp    {gp:1, osm:3, wd:1}
  ```

  `email` is nominally flippable but unreachable: the required owner is
  `google_places`, which never emits an email (`google_places.py:290` `email=None`).
  **`category` is the only reachable field** — and it is the one that decides priority,
  pitch, and whether the row ships at all.
- **The precondition is reachable.** `_pick` short-circuits on missing
  (`dedup.py:168-171`), so Google's `category` must be absent for Wikidata's to survive.
  `google_places.py:324-328` `_pick_category` returns `types[0] if types else "business"`
  — for `types=[null]` from the Places API that is `None`:

  ```
  _pick_category([]                      ) -> 'business'
  _pick_category(['restaurant']          ) -> 'restaurant'
  _pick_category([None]                  ) -> None          <-- missing
  _pick_category(['establishment', None] ) -> 'establishment'
  ```
- **Trigger (concrete).** Three records sharing phone `+96170123456`, using categories
  the two scrapers actually emit (`osm.py:71-78` reads the OSM `shop`/`amenity` tag;
  `wikidata.py:100` reads a `?categoryLabel`):

  ```
  WD  = {name:'Cafe X', phone:'+96170123456', source:'wikidata',
         category:'chain store',  address:None,    scraped_at:'2026-09-01T00:00:00+00:00'}
  GP  = {name:'Cafe X', phone:'+96170123456', source:'google_places',
         category:None,           address:'Beirut', scraped_at:'2026-09-02T00:00:00+00:00'}
  OSM = {name:'Cafe X', phone:'70123456',     source:'osm',
         category:'cafe',        address:'Beirut', scraped_at:'2026-09-02T00:00:00+00:00'}
  ```

  Step 1 `_merge(WD, GP)` → `source='google_places|wikidata'`, `category='chain store'`
  (Google has none). Step 2 `_merge(that, OSM)`:

  ```
  rank(merged)    = (3, 1, '', 'chain store')    # trust inflated by google_places
  rank(OSM record)= (2, 1, '', 'cafe')           # OSM's real trust
  winner: 'chain store'                          # wrong: 1 < 2
  ```

- **The nondeterminism, measured.** Same three records through `dedup()`, varying only
  the input order — which `main.py:160-166` does not control, because `raw.extend(batch)`
  fires in `as_completed` order across a 3-worker `ThreadPoolExecutor`:

  ```
  input order wikidata, google, osm   source='google_places|osm|wikidata'  category='chain store'
      industry_priority   = 'low'
      recommended_service = 'Full digital launch (brand + website + social setup)'
      in sales_ready.csv? = False          # main.py:202-205 needs high|medium

  input order osm, google, wikidata   source='google_places|osm|wikidata'  category='cafe'
      industry_priority   = 'high'
      recommended_service = 'RTYLR commerce OS (POS, online ordering, CRM)'
      in sales_ready.csv? = True

  input order osm, wikidata, google   source='google_places|osm|wikidata'  category='cafe'
      industry_priority   = 'high'   ->  in sales_ready.csv = True
  ```

  Same three observations, same `source` string, two mutually exclusive business
  outcomes — a high-priority F&B lead either ships or does not, and a helper is pitched
  a full build instead of an RTYLR sale. 2 of 3 orderings are correct, which is why no
  single golden test catches it.
- **Why the existing tests miss it:** `test_merge_is_order_independent`
  (`tests/test_lead_signal.py:292-295`) merges two records with the *same* source `osm`,
  where union and single-source agree. `test_sources_are_unioned_and_ordered`
  (`tests/test_lead_signal.py:316-319`) checks the `source` string but never a
  third-source field conflict.
- **Fix:** track per-field provenance so `rank` can score the value with the trust of the
  record that *supplied* it. Minimal correct form: carry a `field -> frozenset[Source]`
  sidecar through `_merge` and pass that to `_trust_for` instead of
  `_sources_of(merged_record)`. A cheap stopgap that removes most of the harm: raise
  `_SOURCE_TRUST["wikidata"]["category"]` from 1 to 2 so no field has a 1/2/3 spread —
  but that papers over the design, it does not fix it.

### S2 — The `source` vocabulary is spelled out as literals in 7 places; any mismatch silently degrades a source to `_DEFAULT_TRUST`, the lowest value in the table

- **Where:** `dedup.py:98,102,106` (`_SOURCE_TRUST` keys) vs `scrapers/osm.py:119`,
  `scrapers/wikidata.py:127`, `scrapers/google_places.py:302`, `cli.py:52-54`,
  `cli.py:290`, and 5 test fixtures.
- **Breaks:** `_trust_for` at `dedup.py:151` is
  `_SOURCE_TRUST.get(src, {}).get(field, _DEFAULT_TRUST)`. A token that misses the table
  scores 1, which is the *minimum* of the 3-level scale — so a typo doesn't degrade a
  source, it demotes it below every honest value, for every field, forever. Nothing
  raises; nothing logs.
  ```
  _trust_for('rating', {'source': 'google_places'}) -> 3
  _trust_for('rating', {'source': 'google'})        -> 1
  _trust_for('rating', {'source': 'Google_Places'}) -> 1
  _trust_for('rating', {'source': 'google places'}) -> 1
  ```
- **Trigger:** the highest-value case is the master CSV, because `_sources_of` is fed
  arbitrary operator text on the read path. The master is round-tripped through Google
  Drive every run (`scrape.yml:56`) and is deliberately written with a UTF-8 BOM so
  Excel and Sheets render Arabic correctly (`main.py:118-119`) — it is a file humans
  edit. A full round trip through the *real* `write_csv` / `load_master`:

  ```
  written  : ...,LB,,,,,,https://x.example,,,,,,,,,,,,Google_Places | OSM ,2026-01-01T00:00:00+00:00
  reloaded : 'Google_Places | OSM '
  _sources_of          -> [' OSM ', 'Google_Places ']   recognised = []
  _trust_for('website', reloaded) = 1                   (OSM's real trust is 3)
  merge vs a fresh google record ->
        https://maps.example    # Google's maps URL beats the real OSM website
  ```
  `load_master` (`main.py:87-89`) nulls blank cells and does nothing else; there is no
  `strip()`, no case-fold, and no membership check on `source` anywhere.
- **Aggravating:** the `+5` multi-source bonus at `enricher.py:316` still fires on
  `'Google_Places | OSM '` (it only tests for a `|`), so the record *looks* healthy in
  the score while its entire field-trust profile has collapsed.
- **Fix:** one `class Source(str, Enum)` in `scrapers/base.py`; producers pass
  `Source.OSM`; `_sources_of` does `{Source(s.strip().lower()) for s in raw.split(SEP)}`
  and `load_master` raises on an unknown token. That also closes S3-4 below.

### S2 — `cli.py stats` reports per-*combination* counts, so the only per-source view an operator has is wrong in exactly the cases that matter

- **Where:** `cli.py:119-121` —
  `collections.Counter(r.get("source") for r in rows).most_common(8)`
- **Breaks:** `r.get("source")` is used as an opaque scalar, so a merged record lands in
  a bucket named `'google_places|osm'` instead of counting toward both sources. The
  numbers an operator needs ("did OSM break? is Wikidata contributing anything?") are
  the ones they cannot get. This is the direct cost of `_sources_of` being private to
  `dedup.py`.
- **Trigger** (a plausible post-dedup distribution):

  ```
  as printed ('osm',180,000) ('google_places',40,000) ('google_places|osm',30,000)
            ('google_places|osm|wikidata',9,000) ('wikidata',6,000) ('osm|wikidata',2,000)
            ('google_places|wikidata',500)

  true totals, which need _sources_of:  osm 221,000   google_places 79,500   wikidata 17,500
  ```
  OSM is reported as 180,000 when 221,000 rows carry it; Google as 40,000 when 79,500 do.
  And `.most_common(8)` truncates by *combination*, so single-source `wikidata` — the
  source most likely to be silently broken — is ranked below three composites.
- **Note the irony:** `cli.py:56-57` has a `registry` membership check that prints
  `unknown source ...; choose from ...` for exactly this class of error. It is applied to
  CLI arguments and not to CSV cells.
- **Fix:** `Counter(s for r in rows for s in _sources_of(r))` — which requires
  exporting `_sources_of` as `sources_of` and placing it in a module both `dedup` and
  `cli` can import. That is the same change that makes S2-1 fixable; do them together.

### S2 — `_pick` short-circuits on a missing `source`, so master provenance is *discarded* instead of unioned

- **Where:** `dedup.py:168-171` (missing short-circuit) vs `dedup.py:173-174` (the union).
- **Breaks:** `_is_missing(None)` is `True`, so `_pick` returns `bv` at `dedup.py:169`
  and never reaches the union. `load_master` turns a blank CSV cell into `None`
  (`main.py:87-89`), which is a *present key holding `None`*. A master row whose `source`
  cell is blank therefore loses its entire provenance history to the next scrape:

  ```
  master.source = None (was 'google_places|osm|wikidata' before an operator cleared it)
  _merge(master, fresh)['source'] = 'osm'          # master provenance discarded
  with a populated master cell:  _merge -> 'google_places|osm|wikidata'   (correct)
  ```
- **Breaks downstream:** the record now reports *fewer* sources than were observed, so
  `enricher.py:316`'s `+5` disappears and `_trust_for` has lost its strongest
  contributor for the rest of the record's life — the union can never be reconstructed,
  because the only place it lived was overwritten.
- **Fix:** handle `source` before the missing-check, exactly as `scraped_at` is handled
  at `dedup.py:176-177`. `"|".join(sorted(_sources_of(a) | _sources_of(b)))` is already
  correct for a missing operand (`_sources_of` returns `set()`), so it just needs
  moving above `dedup.py:168`.

### S3 — `_VOLATILE` lists `website`, which can never be reached, and every field outside `_SOURCE_TRUST` is decided by a *text* comparison

- **Where:** `dedup.py:114` (`_VOLATILE`), `dedup.py:179-185` (the rank tuple).
- **Breaks:** `rank` compares `trust` first and `recency` third, so the recency term is
  only read when trust *and* validity both tie. `_SOURCE_TRUST["website"]` is
  `osm=3, google_places=2, wikidata=2`, so a cross-source website contest is always
  decided by trust and the volatility entry never fires:
  ```
  osm(old) vs osm(new)      -> 'https://new.example'   (same trust 3, recency used)
  osm(old) vs wikidata(new) -> 'https://old.example'   (trust 3 > 2, recency never read)
  ```
  Benign today, but it is a table entry that asserts a behaviour the ordering above it
  makes impossible, so it will mislead the next person who tunes trust weights.
- **The same ordering has a sharper edge.** Any field with no `_SOURCE_TRUST` entry —
  verified: `region`, `country`, `scraped_at`, `website_live`, `industry_priority`,
  `recommended_service`, `completeness_score`, `lead_score`, `source` — gets
  `_trust_for == 1` from every source and `_validity == 1`, so the winner is
  `str(value)` compared as **text** (`dedup.py:184`):
  ```
  _pick('completeness_score', 7, 10)  -> 7      rank a=(1,1,'','7')   rank b=(1,1,'','10')
  _pick('completeness_score', 9, 12)  -> 9      rank a=(1,1,'','9')   rank b=(1,1,'','12')
  _pick('completeness_score', 3, 30)  -> 30
  ```
  A master row scored 10 is overwritten by an older row scored 7, and 9 loses to 12 —
  because `"10" < "7"`. `scraped_at` and `source` escape only because they are
  special-cased at `dedup.py:173-177`.
- **Fix:** delete `"website"` from `_VOLATILE`; and either add trust entries for
  `completeness_score`/`lead_score` or make the final tiebreak type-aware
  (`(isinstance, value)` for numerics) instead of `str(value)`.

### S3 — Two of the 24 `_SOURCE_TRUST` entries are provably dead, and the table encodes knowledge about `scrapers/` that nothing verifies

- **Where:** `dedup.py:97-110`.
- **Breaks / dead weight:**
  - `_SOURCE_TRUST["wikidata"]["category"] == 1` (`dedup.py:108`) is exactly
    `_DEFAULT_TRUST` (`dedup.py:111`), so removing the key changes nothing. A
    hand-tuned entry that documents no decision.
  - `_SOURCE_TRUST["osm"]["whatsapp"] == 3` (`dedup.py:103`) is never read on an OSM
    value: `osm.py:112` hardcodes `whatsapp=None`, and enrichment (`enricher.py:212-214`)
    runs *after* dedup (`main.py:187`), so no record carrying an OSM-sourced whatsapp
    ever reaches `_pick`.
  - Conversely, `osm.py:104-105` and `wikidata.py:112-113` both emit `lat`/`lon`, and
    neither has an entry, so OSM and Wikidata coordinates score 1 while Google's score 3.
    That one is defensible — it matches the comment at `dedup.py:93-96` ("Google knows
    ... coordinates because they are its own data") — but it is undocumented as a
    deliberate choice.
- **The structural point:** `_SOURCE_TRUST` is the one place in the codebase that claims
  to know *what each scraper emits*. It is maintained by hand, in a module that cannot
  import the scrapers without creating an edge it deliberately avoids (see S3-2), and
  nothing checks it against reality. That is how it acquired both a no-op and a phantom.
- **Fix:** generate it, or at minimum add the assertion that would have caught both —
  for each source, `set(_SOURCE_TRUST[src]) <= set(fields_that_scraper_emits)`.

### S3 — `pyproject.toml` ships `scrapers/` only, so the wheel breaks before any import cycle could

- **Where:** `pyproject.toml:35-37` (`include = ["scrapers/"]`),
  `pyproject.toml:33` (`leadminer = "cli:main"`).
- **Breaks:** `dedup.py`, `enricher.py`, `httpclient.py`, `main.py`, `cli.py` and
  `pitch_recommender.py` are all excluded from the wheel, while the console script points
  at one of them. `pip install .` yields a `leadminer` command that raises
  `ModuleNotFoundError: No module named 'cli'` on first use, and a `scrapers/` package
  whose `from httpclient import ...` (`osm.py:4`) cannot resolve. Under hatchling's
  documented file-selection semantics `include` is a whitelist filter, so this reading is
  direct; **I could not run `pip wheel` (hatchling absent, BRIEF.md:75 forbids
  installing), so confirm with one command before acting.**
- **Why it belongs in this report:** it is the answer to "what breaks under a src
  restructure", and the answer is *packaging*, not imports. The `src/` move itself is
  6 `..` prefixes (see the simulation above); `leadminer.dedup` survives it untouched.
- **Fix:** after the `src/leadminer/` move, replace the `include` line with
  `packages = ["leadminer"]`. Until then, the honest state is that this project is
  not installable and `.github/workflows/scrape.yml:39` correctly installs from
  `requirements.txt` instead.

### S3 — Dead and vestigial imports in the modules that produce `source`; one import exists purely to be an import check

- **Where / evidence** (all verified by grep over the tree):
  - `enricher.py:2 import requests` — **dead.** The only other occurrence of the
    identifier is inside a comment (`enricher.py:125`). This is not cosmetic: it is the
    sole reason `tests/test_lead_signal.py:23-63` carries a 40-line `types.ModuleType`
    stub shim, and the reason `import main` fails in a bare checkout
    (`ModuleNotFoundError: No module named 'requests'`, reproduced). `enricher.py:175`
    uses `get_session()` from `httpclient`, never `requests` directly.
  - `enricher.py:4 from urllib.parse import urljoin, urlparse` — **dead**, zero
    occurrences in the file. Presumably residue from the URL-sanitising work in audits
    008/026.
  - `scrapers/osm.py:1` and `scrapers/wikidata.py:1` `import logging`, with
    `log = logging.getLogger(__name__)` at `:7` in both — **imported only for logging,
    and that logging is entirely vestigial.** Neither module ever calls `log.<level>`;
    every message is `print` (`osm.py:33,51,59`, `wikidata.py:68,80,88`). Two dead
    imports that read as if the modules are instrumented.
  - `httpclient.py:45`'s logger is the only one in the tree that emits
    (`httpclient.py:178,204`), and **nothing in the repository calls
    `logging.basicConfig` / `dictConfig` / `fileConfig`.** Both messages are `log.debug`,
    below the last-resort handler's WARNING level, so they are always discarded — the
    retry diagnostics that `httpclient.py:12-15` cites as the reason retries were
    centralised are unobservable. This is the module the scrapers depend on for their
    entire failure story, and its failures are silent by default.
  - `httpclient.py:88-91` `http_session()` — **zero call sites.** Its own usage example
    at `httpclient.py:23-29` is also wrong: it shows
    `fetch_with_retry(session, "GET", url)`, but the signature at
    `httpclient.py:149-153` is `fetch_with_retry(method, url, *, session=None, ...)`.
    Copying the documented example raises `TypeError`.
  - `httpclient.py:116-117` `FetchResult.text` — zero call sites.
  - `enricher.py:327-328` lazy `import urllib3` +
    `urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)` — vestigial.
    `enricher.py:170-175` now uses `verify=True` (the default), so no `InsecureRequest`
    can be emitted from this module. This is the dependency that forces the `urllib3`
    stub in the test shim too.
  - `cli.py:46` `from scrapers.base import BaseScraper  # noqa: F401  (import check)` —
    the only import in the tree with no binding purpose, kept alive deliberately. (It
    is also redundant: lines 47-49 import the three concrete scrapers, each of which
    imports `.base` at `osm.py:5` / `wikidata.py:5` / `google_places.py:28`.)
- **Fix:** delete `enricher.py:2`, `enricher.py:4`, `osm.py:1`, `osm.py:7`,
  `wikidata.py:1`, `wikidata.py:7`, `httpclient.py:88-91`, `httpclient.py:116-117`,
  `enricher.py:327-328`, `cli.py:46`. Then `tests/test_lead_signal.py:23-63` can be
  deleted outright and the suite runs with the stdlib alone, as its own docstring at
  `:4-6` already claims.

---

## Not a bug, but worth knowing

- **`_sources_of` is a leaf today *only because* `source` is a magic string.** This is
  the answer to "would anything become circular under a src layout": nothing does, and
  nothing will — *unless you fix S2-1 the obvious way.* Today `dedup.py` holds a
  duplicated copy of the vocabulary rather than importing it, so there is no edge to
  cycle through. Introduce `class Source(str, Enum)` in `scrapers/base.py` and
  `dedup.py:151` becomes `dedup -> scrapers.base`, while `scrapers/google_places.py:26`
  is already `scrapers.google_places -> enricher`. Audit 020 S12 recommends moving
  `BusinessRecord` into a shared `contracts.py` that `enricher` imports; combine that
  with a `Source` enum in the same module and the moment `contracts.py` ever needs to
  parse the field it declares (`source: set[Source]`), you have
  `contracts -> dedup -> contracts`. **Constraint for whoever does S2-1: put
  `Source`, `parse_sources` and `format_sources` in the contract module and let `dedup`
  import the codec. `dedup.py` must never own the encoding.** Then
  `dedup -> contracts <- scrapers`, and the cycle is structurally impossible.
- **`dedup.py:227-229` is unreachable.** The comment at `dedup.py:220-221` says
  name-keyed records that "don't share a phone with an already-captured record" are
  excluded. They never are. A record reaches `name_index` only when
  `normalize_phone(...)` returned `""` (`dedup.py:205`), which requires fewer than 7
  digits after `re.sub(r"\D", "", raw)` (`dedup.py:30,44-46`); `dedup.py:228` re-runs the
  same function with the *same* `record.get("country") or "LB"` expression, so it
  necessarily returns `""` again — and `""` is never a `phone_index` key because of the
  `if key:` guard at `dedup.py:206`. Verified over 17 phone shapes (`''`, `None`, `-`,
  `...`, `n/a`, `ext 4`, `12345`, `0`, `+`, `00`, `  `, `abc`, `961`, `tel:`, `961-7`,
  `070`) × 6 country values: all return `""`. It is dead, not harmful — but it is the
  guard that was *supposed* to stop a record from being dropped when its phone merged
  elsewhere, and a future change to `normalize_phone` (e.g. adopting the optional
  `phonenumbers` extra at `pyproject.toml:30`) would make it live and start silently
  dropping records. Worth knowing before that dependency is added.
- **`_pick_category` returning `types[0]` is a second, independent way to get a
  missing `category`.** For `types=[]` it returns `"business"` (truthy, and not in
  `PRIORITY_INDUSTRIES`, so `industry_priority` = `low`). For `types=[null]` it returns
  `None`. Both land in S1's precondition path, from different inputs.
- **`main.py:180` is the only caller of `dedup`**, so `_sources_of` runs exactly once per
  record per run, on the combined fresh + master set (`main.py:179`). The order
  sensitivity in S1 is therefore a *per-run* coin flip on the whole accumulated master,
  not an occasional edge case.
- **`enricher.py:316` and `dedup.py:174` are two encoders of one fact with no link.**
  Changing the separator in `_pick` alone silently removes the `+5` multi-source bonus
  from every merged record, with no exception and no test coverage (the multi-source path
  is not exercised anywhere in `tests/test_lead_signal.py`; `BASE` at line 74 uses a
  single source):
  ```
  'osm|wikidata'    +5 fires=True   _sources_of -> ['osm','wikidata']  _trust_for('website')=3
  'osm, wikidata'   +5 fires=False  _sources_of -> ['osm, wikidata']  _trust_for('website')=1
  'osm,wikidata'    +5 fires=False  _sources_of -> ['osm,wikidata']   _trust_for('website')=1
  ```
  Note the CSV itself survives a comma separator — `csv.DictWriter` quotes it
  (`'Cafe,"osm,wikidata"'`) — so nothing would fail; the CSV just stops meaning the same
  thing to three different modules.
- **`scrapers/base.py:26` declares `source: str`**, which says nothing about the
  encoding, the vocabulary, or the ordering guarantee that `dedup.py:174` provides.
  Nothing in the contract prevents any of S2-1.

---

## Recommended order of work

1. **S1** — carry per-field provenance through `_merge` so `_pick` scores a value with
   the trust of the record that supplied it. Only finding here that produces a wrong,
   run-to-run-varying *sales decision*. Add the three-source order test first so the fix
   is provable.
2. **S2 (master cell) + S3 (contract) together** — one `Source` StrEnum plus
   `strip()`/case-fold in `_sources_of`, and a membership check in `load_master`. Put
   the enum and its `parse_sources` / `format_sources` in the module `dedup` will
   import, per the "not a bug" note above, so this does not create the cycle.
3. **S2 (cli stats)** — `Counter(s for r in rows for s in sources_of(r))`, and drop the
   `.most_common(8)` truncation so a broken single-source scraper cannot hide behind a
   composite. Depends on step 2 for the import.
4. **S2 (provenance discarded)** — move the `source` union above the missing-check at
   `dedup.py:168`. Two lines; stops master provenance being erased by a blank cell.
5. **S3 (dead imports)** — delete the 10 dead imports and the test shim; the suite then
   runs on the stdlib alone as its docstring already claims.
6. **S3 (packaging)** — confirm the wheel contents with `pip wheel . && unzip -l
   dist/*.whl`, then either drop `pyproject.toml:35-37` until the `src/leadminer/` move
   happens or do the move with `packages = ["leadminer"]`.
7. **S3 (`_VOLATILE` / `str` tiebreak)** — drop `"website"` from `_VOLATILE`; make the
   `dedup.py:184` tiebreak type-aware so `completeness_score` 10 stops losing to 7.