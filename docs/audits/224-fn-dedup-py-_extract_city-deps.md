# 200 — `_extract_city` dependency map: zero code dependencies, five undeclared data dependencies

## Verdict

`_extract_city` (`dedup.py:72-76`) has **no dependencies at all** — not one module
global, not one intra-package import, not one sibling function, not one closure
variable. This is provable from the bytecode (`co_names == ('split', 'strip',
'lower')`, `co_freevars == ()`), and it is the cleanest function in the codebase.

That is exactly the problem. The five lines of pure-string logic it contains encode
**five contracts it does not own and cannot see**, each one owned by a *different
layer*: the `address` key by `scrapers/base.py:8`, the *position* of the city token by
`scrapers/osm.py:80-87`, the trailing country token by `scrapers/google_places.py:286`,
the separator by Google's locale, and the re-parsing of last run's CSV by
`main.py:77-105`. Under a `src/` layout this function breaks **nothing** — it is the
easiest thing in the repo to move — but it is also the function that most needs to be
moved first, because every correct fix for it has no acyclic home in the current
package graph.

---

## Dependency map

### Level 0 — code dependencies: none (verified, not asserted)

```
dedup._extract_city.__code__.co_names    -> ('split', 'strip', 'lower')
dedup._extract_city.__code__.co_freevars -> ()
dedup._extract_city.__code__.co_consts   -> ('', ',', -1)
```

`dedup.py:1-2` imports `re` and `unicodedata`. Neither is reachable from this
function. `unicodedata` is used only by the sibling `normalize_name` (`dedup.py:67-68`)
and `re` only by `normalize_phone` (`dedup.py:44`) and the two validators
(`dedup.py:116-117`). There is no type-only import here and no logging import here.

### Level 1 — callers

| Caller | Line | Notes |
|---|---|---|
| `dedup` | `dedup.py:213` | **sole call site in the repo** (grep for `_extract_city` returns exactly `dedup.py:72` and `dedup.py:213`) |
| `dedup` -> `main.main` | `main.py:180` | `records = dedup(combined)` |
| `main.main` -> `cli.cmd_run` | `cli.py:35-37` | deferred `import main as pipeline` |

`dedup.py:213-214` is the whole consumption contract:

```python
city = _extract_city(record.get("address"))
key = (normalize_name(name), city)
```

The return value is used **only as a dict key**. It is never stored on the record,
never written to a column, never logged, never compared to anything else.

### Level 2 — transitive reverse: who pulls this code in

`cli.cmd_run` (`cli.py:35`) -> `main.main` -> `main.py:29 from dedup import dedup,
normalize_phone` -> `dedup.py:213` -> `_extract_city`.
Note `cli.cmd_inspect` (`cli.py:249-275`) calls `pipeline.load_master` /
`resolve_country` / `write_csv` but **never** `dedup` — the one offline command that
could help diagnose a bad merge does not perform one.

### Level 3 — data dependencies (invisible to every tool in the repo)

These are the real dependency surface. None of them is an `import`, so `ruff`,
`mypy --strict` (`pyproject.toml:64-68`) and `pydeps` all report this function as
clean.

| # | Contract | Owner | Enforced by |
|---|---|---|---|
| D1 | a dict key named `address` exists, `str \| None` | `scrapers/base.py:8` | nothing — `dedup.py:72` annotates `str \| None` but the call site reads `record.get("address")` off a bare `dict`, so mypy sees `Any` |
| D2 | the **city** is the last `", "`-joined element | `scrapers/osm.py:80-87` | nothing — a positional contract expressed as a list literal in another layer |
| D3 | the address ends in the **country name** | `scrapers/google_places.py:286` (`place.get("formattedAddress")`, passed through verbatim) | nothing |
| D4 | `","` is the only separator | Google's locale | nothing |
| D5 | the string is re-parseable on the next run | `main.py:124` writes it, `main.py:85` reads it back with `utf-8-sig` | nothing |

### Level 4 — the knowledge it needs and does not have

The only canonical geographic vocabulary in the repository is in `enricher.py`:
`REGION_KEYWORDS` (`enricher.py:10-50`), `_REGION_MAP` (`enricher.py:52-55`),
`_LB_COORD_REGIONS` (`enricher.py:58-68`), `_KSA_COORD_REGIONS` (`enricher.py:70-76`,
the only place a KSA *city* name appears as a canonical token: "Riyadh", "Jeddah",
"Dammam", "Mecca", "Medina"). `_extract_city` depends on **none** of it. It therefore
cannot produce a stable city token even in principle — it has no list to validate
against and no mapping from neighbourhood to city.

---

## Findings

### S1 — `scrapers/osm.py:85` puts `addr:district` *after* `addr:city`, so `parts[-1]` reads the district, and the same business gets a different key from each source

- **Where:** `dedup.py:75-76` (`parts[-1]`) and `scrapers/osm.py:80-87` (the join order)
- **Breaks:** `scrapers/osm.py:80-86` builds the address as
  `housenumber, street, suburb, city, district`. `addr:district` is the **last**
  element appended, so `_extract_city` returns the district whenever that tag is
  present. `scrapers/osm.py:83` (`addr:suburb`) sits *before* `addr:city`, so suburb
  never wins — the code reads five address fields and the only one that can influence
  the dedup key is the fifth, which is the least-covered tag in the set. The result is
  that `dedup`'s key namespace simultaneously holds three different semantic domains:
  a district (`"jounieh"`, from OSM), a city (`"beirut"`, from OSM when
  `addr:district` is absent), and a country (`"lebanon"`, from every Google record).
  A Beirut shop and a Jounieh shop keyed `"jounieh"` merge; the *same* Beirut shop seen
  by OSM (`"jounieh"`) and by Google (`"lebanon"`) does not merge at all.
- **Trigger** (reproduces against current `dedup.py`, no network):

  ```python
  # tags = {addr:housenumber:12, addr:street:Main Street, addr:suburb:Badaro,
  #         addr:city:Beirut, addr:district:Jounieh}  -> scrapers/osm.py:80-87
  _extract_city("12, Main Street, Badaro, Beirut, Jounieh")  -> 'jounieh'
  _extract_city("Badaro, Beirut, Lebanon")                   -> 'lebanon'

  recs = [
    {"name":"Cafe Mozart","address":"12, Main Street, Badaro, Beirut, Jounieh",
     "phone":None,"country":"LB","source":"osm","website":None,"rating":None},
    {"name":"Cafe Mozart","address":"Badaro, Beirut, Lebanon",
     "phone":None,"country":"LB","source":"google_places","rating":4.6,"review_count":310},
  ]
  len(dedup(recs))  ->  2      # one business, two output rows
  ```

  The 98-line per-field merge engine at `dedup.py:97-194` never runs. Google's
  `rating=4.6` / `review_count=310` stay stranded on a row that has no website, while
  OSM's `website` sits on a row that has no rating. `enricher.lead_score`
  (`enricher.py:311-317`) then awards neither row the `+5` multi-source bonus
  (`"|" in record.get("source")`) that a correct merge would have produced, and
  `without_websites.csv` / `with_websites.csv` disagree with each other about the
  same shop.
- **Relation to audit 003:** `003-dedup-identity-model.md:86-91` tabulates the OSM key
  as `Hamra, Beirut -> beirut`. That row is correct **only when `addr:district` is
  absent**. The ordering defect in `osm.py:85` is not captured anywhere in 003, and it
  changes what "the city key" means for a subset of records. Note also that 003 cites
  `dedup.py:34-38` and `:77-79`; the function is now at `dedup.py:72-76` and the key
  at `dedup.py:213-214` — the file has been edited underneath those audits.
- **Severity defence:** S1 because the output is wrong and silently so — duplicated
  businesses in the cumulative master, a merge engine bypassed, and no field anywhere
  records that a decision was made. I am *not* claiming the frequency is high:
  `addr:district` is a minority tag, so this hits a subset of OSM records. The
  frequency argument belongs with the country-token failure already logged in 003.
- **Fix:** make the position contract explicit and per-source rather than positional —
  have the scrapers emit a canonical `city` field (OSM: prefer `addr:city`, fall back
  `addr:suburb`; Google: strip the trailing country token), store it on
  `BusinessRecord`, and have `_extract_city` read that field. Do **not** fix it by
  changing `parts[-1]` to `parts[-2]`: that silently breaks the no-country OSM case
  (`"Hamra, Beirut"` -> `"hamra"`) that 003 relies on.

### S1 — the city key is destroyed on merge and never persisted, so a bad merge is unrecoverable and self-reinforcing across runs

- **Where:** `dedup.py:214` (key built), `dedup.py:216` (`name_index[key] = _merge(...)`),
  `main.py:33-40` (`FIELDS` has no `city`), `main.py:150` / `main.py:180` / `main.py:209`
  (master -> dedup -> master).
- **Breaks:** the value `_extract_city` returns exists for exactly one statement
  (`dedup.py:214`) and is never stored. `main.py:33-40` defines 22 columns and `city`
  is not among them. So when `dedup.py:216` folds two records together, the evidence of
  *why* they were folded — the city token that made the keys collide — is gone, and
  `all_businesses.csv` cannot distinguish "merged correctly" from "merged because both
  keys were `('cafe mozart', 'lebanon')`". Worse, the merge is written back to the
  cumulative master (`main.py:150` loads, `main.py:209` overwrites), so a wrong fusion
  is permanent and each subsequent run re-derives keys from the already-fused address.
  There is no counter, no warning, and no log line: `dedup.py` imports no `logging`
  and prints nothing. Diagnosing a suspected over-merge means re-running the whole
  pipeline offline and reimplementing the key by hand.
- **Trigger:** the two-record input above; `leadminer inspect` (`cli.py:249-275`)
  shows two rows for one shop with no indication that `dedup()` considered them equal.
  Conversely, after the KSA collision (`"Al Baik"` in Riyadh and Jeddah both keying
  `"saudi arabia"` -> `len(dedup(...)) == 1`, verified) the surviving row carries
  `address="Al Malaz, Riyadh, Saudi Arabia"` and the Jeddah branch has vanished with no
  trace that it ever existed.
- **Severity defence:** S2 not S1 because the *decision* being wrong is 003's finding;
  what is new here is that it is unrecoverable and silent, which is an operability
  and auditability problem rather than a fresh correctness one.
- **Fix:** add a `city` column to `FIELDS` (`main.py:33-40`), persist the token that
  formed the key on the merged record, and log one line per name-index merge with both
  input addresses.

### S2 — the fix everyone wants here has no acyclic home: `dedup -> enricher` collides with `enricher -> dedup`

- **Where:** prospective `dedup.py` import of `enricher.REGION_KEYWORDS`
  (`enricher.py:10-50`) or the KSA city tokens (`enricher.py:70-76`); the reverse pull
  is visible today as duplicated logic at `enricher.py:285` + `enricher.py:292`,
  `main.py:138` + `main.py:142`, and the named original at `dedup.py:44` +
  `dedup.py:30`.
- **Breaks:** 003's prescribed fix is "validate against a known-city list"
  (`003-dedup-identity-model.md:43-44`, `:98`). There is no such list in the repo. The
  only place city and region knowledge exists is `enricher.py`, which is a *later*
  pipeline stage. Meanwhile `enricher.py:285` re-implements `re.sub(r"\D", "", ...)`
  from `dedup.py:44` and hardcodes `>= 7` at `enricher.py:292` — the same magic number
  that is a named constant, `_MIN_DIGITS`, at `dedup.py:30` — and `main.py:138`/`:142`
  does it a third time. So the two modules each need something from the other:
  `dedup` needs `enricher`'s gazetteer, `enricher` needs `dedup.normalize_phone`.
  Adding either edge alone is acyclic; adding both — which any "share the
  normalisation helpers" cleanup will do — yields
  `leadminer.dedup <-> leadminer.enricher`. This is not hypothetical under `src/`:
  today the flat layout hides it because neither import exists yet, so nothing has
  been forced to be resolved. It is also tangled into the layering violation audit 020
  already flags: `scrapers/google_places.py:26 from enricher import infer_region` makes
  a scraper depend on a pipeline stage, so `enricher` is already not a leaf.
- **Trigger:** implement 003's fix as written (`from enricher import REGION_KEYWORDS` in
  `dedup.py`) plus the obvious `enricher.py:285` cleanup (`from dedup import
  normalize_phone`), then move everything under `src/leadminer/`: import fails with
  `ImportError: cannot import name 'normalize_phone' from partially initialized module
  'leadminer.dedup' (most likely due to a circular import)`.
- **Severity defence:** S2 — it is a trap in the refactor path, not a live break. It
  would be S1 the moment someone takes the obvious route, and the failure mode is a run
  that dies at import, which is why it needs writing down before the `src/` move rather
  than after.
- **Fix:** land the canonical city token in a new leaf module that imports nothing from
  the project — `leadminer/geo.py` holding the city gazetteer plus
  `canonical_city(address, country)` — and have `dedup`, `enricher.infer_region` and
  both scrapers import *that*. `geo.py` must import nothing from `leadminer.*`, so it
  stays a leaf from every direction. Do the `src/` move before the `_extract_city`
  fix, not after.

### S3 — the two halves of the dedup key obey different normalization contracts, four lines apart

- **Where:** `dedup.py:66-69` (`normalize_name`) vs `dedup.py:72-76` (`_extract_city`),
  combined into one key at `dedup.py:214`.
- **Breaks:** the name half gets NFKD decomposition, combining-mark removal, whitespace
  collapsing and lowercasing. The city half gets `.strip().lower()` and nothing else.
  So `"Rue Clémenceau, Beyrouth, Liban"` yields city `"liban"` while the name half
  handles `"Clémenceau"` and `"Café"` correctly. The city component is the weak half of
  the key and it is the half every downstream decision rests on.
- **Trigger:** `_extract_city("Beyrouth, Lebanon") -> 'liban'` vs
  `_extract_city("Beirut, Lebanon") -> 'lebanon'`; two transliterations of the same
  city never key together. (`normalize_name("Beyrouth") -> 'beyrouth'` and
  `normalize_name("Beirut") -> 'beirut'` likewise never match — audit 058.)
- **Fix:** route both halves through one `normalize_text()` in the proposed `geo.py`
  (or `leadminer/textnorm.py`) so the tuple has a single normalization contract.

### S3 — `_extract_city` will raise on a non-`str` address, and `_is_missing` already knows to guard for that

- **Where:** `dedup.py:72-76` vs `dedup.py:88-89` in the same file.
- **Breaks:** `_is_missing` explicitly branches on `isinstance(value, str)`
  (`dedup.py:88`) because it is handed every field of every record. `_extract_city`
  calls `address.split(",")` (`dedup.py:75`) with no such guard, and its only caller
  passes it the output of `.get("address")` on a bare `dict` (`dedup.py:213`) — which
  is `Any` to mypy because `dedup(records: list[dict])` (`dedup.py:197`) is unparameterized.
  A row whose `address` is a list or a number raises `AttributeError` and kills the
  run at `dedup.py:213`.
- **Trigger:** `_extract_city(["Beirut"])` -> `AttributeError: 'list' object has no
  attribute 'split'`. Reaching it requires a scraper or a hand-edited master CSV to
  produce a non-string; no current scraper does.
- **Severity defence:** S3 — defensive-consistency, not a live defect.
- **Fix:** `if not isinstance(address, str): return ""`, or type `dedup` against
  `BusinessRecord` (see 020) so the call site is checked.

---

## Not a bug, but worth knowing

- **There is no circular import today, and I want to be exact about that.**
  `dedup.py` imports nothing from the project, so the transitive graph out of
  `_extract_city` is a clean 3 hops with no back-edges:
  `cli -> main -> dedup -> _extract_city`. The cycle in S2 above is created by the
  *fix*, not found in the code as written.

- **The `src/` layout change breaks 22 import sites and 0 lines of `_extract_city`.**
  Measured: 12 eager absolute intra-project imports (`main.py:25-31` = 7,
  `enricher.py:128` = 1, `scrapers/google_places.py:26-27` = 2,
  `scrapers/osm.py:4` = 1, `scrapers/wikidata.py:4` = 1) plus 10 deferred ones
  (`cli.py:35`, `:46-49`, `:249-252`), against only **3** relative imports
  (`.base` at `osm.py:5`, `google_places.py:28`, `wikidata.py:5`). Every absolute one
  must be rewritten. `_extract_city` itself needs zero changes — but it is the one
  function whose *correctness* depends on the shape of `osm.py:80-87`, so it should
  be settled before, not after, the move.

- **`pyproject.toml:35-37` already encodes the flat-layout debt, in a comment:**
  `include = ["scrapers/"]` with "The package imports a top-level sibling module, so
  both must ship". There is no `leadminer` package at all — the wheel installs
  `dedup`, `enricher`, `main`, `cli`, `httpclient`, `pitch_recommender` as top-level
  modules into `site-packages`, and `[project.scripts] leadminer = "cli:main"`
  (`pyproject.toml:32-33`) depends on that. Audit 030 proposes the `src/leadminer/`
  move; this report is a reason to sequence it *before* the dedup work, per S2.

- **Answering the brief's two specific questions.**
  - *Imported only for a type annotation:* two real cases, neither reachable from
    `_extract_city`. `from typing import Iterator` at `scrapers/base.py:2`,
    `scrapers/osm.py:2`, `scrapers/wikidata.py:2`,
    `scrapers/google_places.py:22` is used **only** in `-> Iterator[BusinessRecord]`
    return annotations (`base.py:33`, `osm.py:31`, `wikidata.py:66`,
    `google_places.py:168`). And `from typing import Mapping` at
    `pitch_recommender.py:22` is used **only** in the parameter annotation at
    `pitch_recommender.py:25` — which is notable because it is the single place in the
    repo that bothers to type a record as an abstract mapping, while `dedup.py` uses
    bare `dict` everywhere (`dedup.py:143`, `:148`, `:159`, `:190`, `:197`). Both are
    harmless at runtime; `Iterator` is deprecated in favour of
    `collections.abc.Iterator` and `UP035` is enabled at `pyproject.toml:51`, so
    `ruff check` should already be failing on those four lines.
  - *Imported only for logging:* `import logging` at `scrapers/osm.py:1` and
    `scrapers/wikidata.py:1`, with `log = logging.getLogger(__name__)` at `osm.py:7`
    and `wikidata.py:7` — and **`log.` is never called in either file** (grep returns
    nothing for both). Both modules report with bare `print` instead (`osm.py:33`,
    `osm.py:51`, `osm.py:59`, `wikidata.py:68`, `wikidata.py:80-84`). The only real
    logger in the repository is `httpclient.py:35`. So the two scrapers carry dead
    logging scaffolding, and `dedup.py` — whose decisions are destructive and
    irreversible — has no logger at all. That asymmetry is the direct cause of the
    observability gap in the second finding.

- **`_extract_city`'s output is not a city in any of the three real cases**, verified
  directly against the current function:

  | Input | Source | Returns |
  |---|---|---|
  | `Clemenceau, Beirut, Lebanon` | Google `formatted_address` | `lebanon` |
  | `King Abdulaziz Road, Al Malaz, Riyadh, Saudi Arabia` | Google | `saudi arabia` |
  | `12, Main Street, Badaro, Beirut, Jounieh` | OSM, all five `addr:*` | `jounieh` |
  | `Tripoli, Lebanon, ` (trailing comma) | OSM or CSV round-trip | `''` |
  | `شارع الملك فهد، العليا، الرياض، السعودية` | Google, Arabic locale | the entire string |

  The last row is the mirror image of 003's S1: Arabic-localized addresses use U+060C
  `،`, which `str.split(",")` (`dedup.py:75`) does not split on, so the whole address
  becomes the key. Verified end to end: the same place as
  `("Al Olaya, Riyadh, 12211, Saudi Arabia", "العليا، الرياض، السعودية")` produces
  **2 output rows** instead of 1. Under-merging rather than over-merging, but the
  same business is duplicated into both `all_businesses.csv` and the sales CSVs.

- **The OSM query cannot produce a KSA record, so the country token is asymmetric.**
  `scrapers/osm.py:13` pins `area["ISO3166-1"="LB"]`, and `scrapers/wikidata.py:27`
  pins `wdt:P17 wd:Q822` (Lebanon). Only Google emits KSA addresses
  (`google_places.py:83-125`). Any city-key logic that special-cases "Saudi Arabia"
  is therefore Google-specific, while anything that special-cases Lebanon applies to
  two sources with different address shapes.

---

## Recommended order of work

1. **Do the `src/leadminer/` move first** (audit 030), rewriting all 22 absolute
   intra-project imports. Doing it after the `_extract_city` fix means debugging an
   import cycle that looks like a packaging bug.
2. **Create `leadminer/geo.py` as a project-import-free leaf**: `canonical_city(address,
   country) -> str` plus the city gazetteer. Move `enricher.py:10-76` (`REGION_KEYWORDS`,
   `_REGION_MAP`, both coord-region tables) into it at the same time, so the gazetteer
   arrives with a home and `enricher` keeps its behaviour. This closes the S2 cycle
   before it can be created.
3. **Then fix the key.** Make the scrapers emit a canonical `city` field
   (`scrapers/osm.py:80-87`: prefer `addr:city`, then `addr:suburb`, ignore
   `addr:district` for keying; `scrapers/google_places.py:286`: strip the trailing
   country token; both splitting on `,` **and** `،`), add `city` to `BusinessRecord`
   (`scrapers/base.py:5-28`) and to `FIELDS` (`main.py:33-40`), and reduce
   `_extract_city` to a lookup over the emitted field.
4. **Add the tests that would have caught all of this** before step 3, so the change is
   provable: table-driven cases for each of the five address shapes above, and an
   assertion that one business seen by two sources produces exactly one row.
5. **Housekeeping, cheap:** delete the dead `logging` scaffolding at `scrapers/osm.py:1,7`
   and `scrapers/wikidata.py:1,7`; replace `typing.Iterator` with
   `collections.abc.Iterator` at the four sites; unify the three copies of the
   `>= 7` digit threshold onto `dedup._MIN_DIGITS` once step 1 removes the cycle risk.