# 200 — `cmd_score` dependency map, layering and cycle risk

**Lens:** dependency graph of `cmd_score` (`cli.py:247`), traced transitively.
**Snapshot:** `git log -1` = `99493b9`, working tree clean for `*.py` at time of writing.

## Verdict

`cmd_score` is a 31-line pure-Python re-scoring loop (`cli.py:247-277`) that reaches into
the pipeline orchestrator (`main`) for its CSV codec, and pays for **11 local modules /
2,277 lines** to call six functions of which exactly two (`load_master`, `write_csv`) live
in `main` and four are one-liners. The dependency direction is inverted: `cli` is not a layer
above `main`, it is a peer reaching sideways into the orchestrator's internals, so the
orchestrator owns the schema (`main.py:33`) and the `DATA_DIR` constant (`main.py:41`) that
the CLI also redefines (`cli.py:26`).

The concrete damage is that `leadminer score` is the command an operator reaches for when a
run looks wrong (`cli.py:14-15`), and **in this repository as checked out it cannot run at
all** — reproduced below.

---

## Dependency map

`cmd_score` (`cli.py:247`) makes **6 external calls** and 1 file-existence check:

| Call site | Symbol | Defined at | Reached via |
|---|---|---|---|
| `cli.py:259` | `pipeline.load_master(path)` | `main.py:77` | `import main` (`cli.py:249`) |
| `cli.py:266` | `pipeline.resolve_country(r)` | `main.py:50` | `import main` (`cli.py:249`) |
| `cli.py:275` | `pipeline.write_csv(out, records)` | `main.py:108` | `import main` (`cli.py:249`) |
| `cli.py:267` | `industry_priority(cat)` | `scrapers/whitelist.py:133` | `from scrapers.whitelist` (`cli.py:252`) |
| `cli.py:268` | `recommend_service(r)` | `pitch_recommender.py:25` | `from pitch_recommender` (`cli.py:251`) |
| `cli.py:269` | `lead_score(r)` | `enricher.py:283` | `from enricher` (`cli.py:250`) |

### Transitive module closure (21 local edges, verified by AST walk)

```
cli.py:249 ──► main ──┬─► scrapers.osm ──┬─► httpclient ──► requests
cli.py:250 ──► enricher ─┤                 ├─► scrapers.base
                       │                  └─► scrapers.wikidata ──► scrapers.base
                       ├─► httpclient
                       └─► (4 module-scope globals below)
cli.py:251 ──► pitch_recommender                       (typing only)
cli.py:252 ──► scrapers.whitelist                      (zero imports)

main.py:25-27 ─► scrapers.osm / scrapers.wikidata / scrapers.google_places
main.py:29 ────► dedup                                  (re, unicodedata)
main.py:28 ────► scrapers.whitelist

scrapers/google_places.py:26 ──► enricher    ◄── LAYERING VIOLATION (see S2-1)
scrapers/google_places.py:27 ──► httpclient
enricher.py:128 ────────────────► httpclient  ◄── import at line 128 of 352 (see S3-1)
```

**11 local modules / 2,277 LOC** are loaded to serve 31 lines of command:

```
cli 321 · main 246 · enricher 352 · pitch_recommender 140 · dedup 232
httpclient 228 · scrapers/__init__ 0 · base 33 · osm 122 · wikidata 130
google_places 328 · whitelist 145                              TOTAL 2,277
```

**5 of those 11 are pure overhead for this command** — reachable *only* through
`import main` at `cli.py:249`, and used by nothing in `cmd_score`:
`scrapers.osm`, `scrapers.wikidata`, `scrapers.google_places`, `scrapers.base`, `dedup`.

### Globals in the closure that `cmd_score` never reads

`enricher.REGION_KEYWORDS` (`enricher.py:10`), `_REGION_MAP` (`enricher.py:52`, 8 compiled
Arabic-containing regexes built at import), `_LB_COORD_REGIONS` / `_KSA_COORD_REGIONS`
(`enricher.py:58` / `:70`), `_MAX_BODY_BYTES` (`:158`, aliased from
`httpclient.DEFAULT_MAX_BODY_BYTES`), `_EMAIL_RE`/`_EMAIL_BLACKLIST`/`_IG_BLACKLIST`
(`:130-141`), `LIVE`/`DEAD`/`UNKNOWN` (`:154-156`), `main.FIELDS` (`main.py:33`),
`main._FLOAT_FIELDS`/`_INT_FIELDS` (`main.py:43-44`),
`httpclient.DEFAULT_TIMEOUT/DEFAULT_RETRIES/DEFAULT_BACKOFF/MAX_BACKOFF/_local/FetchResult`
(`:47-58`, `:94-117`).

---

## Findings

### S1 — `leadminer score` cannot run in this checkout, and its failure mode is a raw traceback

- **Where:** `cli.py:249` (`import main as pipeline`) → `main.py:25` → `scrapers/osm.py:4`
  → `httpclient.py:43` (`import requests`). Compare `cli.py:226-231`, which guards the exact
  same import in `cmd_doctor`.
- **Breaks:** `leadminer score` is documented at `cli.py:12` as *"re-score existing records,
  no network"* and at `cli.py:14-15` as one of the commands that *"never touch the network
  ... are what you reach for when a run looks wrong."* It nevertheless transitively requires
  `requests` — a package it never calls — and dies with an unhandled
  `ModuleNotFoundError` traceback. `leadminer doctor`, in the *same interpreter*, reports
  the identical missing dependency cleanly and exits 1. The diagnostics command works; the
  thing you'd run because the diagnostics said something is wrong does not.
- **Trigger:** the environment as handed over. `requests` is not installed:
  ```
  $ python3 cli.py score
    File "cli.py", line 249, in cmd_score
      import main as pipeline
    File "main.py", line 25, in <module>
      from scrapers.osm import OSMScraper
    File "scrapers/osm.py", line 4, in <module>
      from httpclient import fetch_with_retry, get_session, utc_now_iso
    File "httpclient.py", line 43, in <module>
      import requests
  ModuleNotFoundError: No module named 'requests'

  $ python3 cli.py doctor
    connectivity (5s timeout each):
      FAIL  requests is not installed - run: pip install -e '.[dev]'
  ```
  Note the irony that `doctor` recommends `pip install -e '.[dev]'` while
  `requirements.txt` (which CI installs, `.github/workflows/scrape.yml:39`) and `pyproject.toml:16-18`
  disagree about what the project even needs.
- **Also:** the same traceback is what a *partial* `src layout` migration produces, so this
  is not hypothetical — see S2-1.
- **Fix:** move `load_master` / `write_csv` / `resolve_country` / `FIELDS` / `DATA_DIR` into a
  dependency-free `storage.py` that imports only `csv`, `os`, `pathlib`; have both `main.py`
  and `cli.py` import it. Then wrap the lazy imports at `cli.py:249-252` in the same
  `try/except ImportError` guard `cmd_doctor` already uses.

---

### S2 — `cmd_score` depends on the orchestrator, not on a storage layer: 5 modules and `requests` for zero functional gain

- **Where:** `cli.py:249`, `cli.py:259`, `cli.py:266`, `cli.py:275`.
- **Breaks:** `main.py:1-16` documents itself as the scrape-everything entry point
  (*"Run: `python main.py`"*). `load_master`/`write_csv`/`resolve_country` are its
  undeclared private helpers — there is **no `__all__` in any module in the tree** to mark
  them as API. So `cli` is not layered above `main`; it is a peer reaching sideways into the
  orchestrator's guts. The cost is that a purely local, offline, read-CSV-and-write-CSV
  operation loads the three scrapers, the dedup engine and the HTTP layer — verified above as
  5 modules of pure overhead, plus `requests` and `urllib3` as third-party requirements.
- **Trigger:** any machine with the data files and no scraper dependencies. `cmd_stats`
  (`cli.py:79`) and `cmd_validate` (`cli.py:135`) do *not* have this problem — they use
  `csv` directly. `cmd_score` is the only offline command that cannot do so, purely because
  the codec lives in the wrong module.
- **Fix:** extract `storage.py` (see S1 fix). `cmd_score` then imports `storage`,
  `enricher.lead_score`, `pitch_recommender.recommend_service`,
  `scrapers.whitelist.industry_priority` and nothing else — 5 modules, zero third-party.

---

### S2 — `resolve_country` is a write-only dependency: it silently rewrites the `country` column and nothing in the scoring closure reads it

- **Where:** `cli.py:266` — `r["country"] = pipeline.resolve_country(r)`.
- **Breaks:** `cmd_score` is advertised as *"re-score existing CSV with current rules"*
  (`cli.py:248`). It is also, unlabelled, a country-column migration. Verified: none of
  `lead_score` (`enricher.py:283-319`), `recommend_service` (`pitch_recommender.py:25-99`) or
  `industry_priority` (`scrapers/whitelist.py:133-145`) reference the string `country` anywhere in
  their source. The line contributes **zero** to any score. Its only effect is to overwrite
  the value in the output CSV.
- **Trigger:** a Riyadh clinic row whose `country` cell is blank (or whose phone column was
  dropped by a `--out`-filtered export) is rewritten from blank to `LB`, because
  `resolve_country` falls back to `_DEFAULT_COUNTRY = "LB"` (`main.py:47`, `:74`):
  ```
  before: ['No Country Co','cafe','Riyadh','', ...]
  after : ['No Country Co','cafe','Riyadh','LB', ...]     # silent
  ```
  The operator asked for a re-score and got a data correction they were not told about, on
  the master file, in place (`cli.py:274` defaults `--out` to the input path).
- **Fix:** either drop `cli.py:266`, or — better, because the correction is genuinely
  desirable — move it behind an explicit `--fix-country` flag and report the count, the way
  `cmd_score` already reports `changed` at `cli.py:276`.

---

### S2 — `cmd_score` skips the `normalize_phone` step that `main.main` applies, so `score` and `run` disagree on the same record

- **Where:** `cli.py:265-272` vs `main.py:192-197`.
- **Breaks:** `main.main` normalises the phone **before** scoring:
  ```python
  main.py:192-194   raw_phone = r.get("phone")
                    if raw_phone:
                        r["phone"] = normalize_phone(raw_phone, r.get("country") or _DEFAULT_COUNTRY)
  main.py:197       r["lead_score"] = _lead_score(r)
  ```
  `cmd_score` calls `lead_score` directly (`cli.py:269`) with the raw cell and never
  normalises. `lead_score` (`enricher.py:285`) strips non-digits and awards +15 at
  `len(phone) >= 7`; `normalize_phone` (`dedup.py:44-63`) can *shrink* the digit count below
  that threshold. So the two entry points produce different `lead_score` for the same row.
- **Trigger:** `phone = "0961234"` (malformed 7-char number), `country = LB`:
  ```
  phone='0961234'  normalize-> '+961234'   score(direct)=30   score(after run's normalize)=15   DIVERGES
  ```
  A `leadminer run` that produced this master scored the row **15**. Running
  `leadminer score` over any CSV that still holds the un-normalised form scores it **30** and
  rewrites the master in place. This only bites on CSVs `run` did not produce (hand-assembled,
  `--out` extracts, pre-fix historical masters) — but "only bites on the files you most need
  to re-score" is exactly the wrong failure surface.
- **Fix:** move the normalise-then-score sequence into one shared function
  (`score_record(record) -> dict`) called by both `main.py:189-197` and `cli.py:265-272`, so
  the ordering cannot drift again. This is the same class of bug already recorded for
  `industry_priority` at `main.py:195-196`.

---

### S2 — the output schema is owned by `main.FIELDS`, so `score` silently drops any column the orchestrator does not know about

- **Where:** `cli.py:275` → `main.py:125`
  (`csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")`).
- **Breaks:** `cmd_score` re-reads and rewrites the whole file through `main.FIELDS`, a
  hardcoded 23-element list (`main.py:33-40`). `extrasaction="ignore"` is exactly the right
  default for a scraper export, but it is the wrong default for a **re-scoring** command whose
  contract is "give me the same rows back". Any column an operator added by hand is deleted
  without warning, and `cmd_score` reports success.
- **Trigger:** a 26-column CSV with `linkedin_handle`, `owner_email`, `internal_notes`
  appended:
  ```
  columns in : 26   ['linkedin_handle','owner_email','internal_notes']
  columns out: 23
  DROPPED SILENTLY: ['linkedin_handle','owner_email','internal_notes']
  ```
  Exit code 0.
- **Note:** the brief's "22 columns" (`docs/audits/BRIEF.md:57-62`) and "22 keys"
  (`docs/audits/BRIEF.md:18`) are both stale — `main.FIELDS` and `scrapers/base.py:5-28` both hold
  **23**.
- **Fix:** `write_csv` should derive its fieldnames from the input header when one exists
  (`reader.fieldnames`), falling back to `FIELDS` only for new files.

---

### S2 — the layering inversion is one line from a hard ImportError, and a concurrent agent shipped it mid-audit

- **Where:** `scrapers/google_places.py:26` — `from enricher import infer_region`.
- **Breaks:** the acquisition layer (`scrapers/*`) imports the enrichment layer
  (`enricher`), while `enricher` also contains an orchestration function, `enrich`
  (`enricher.py:326`), that sequences `infer_region` → `check_websites` → score. So
  `enricher` is simultaneously a leaf kernel *and* a pipeline stage, and it is the **only**
  module that both `main` and a scraper depend on. Any need inside `enricher` for a
  scraper-side symbol closes the loop — the most likely candidate is the
  multi-source bonus at `enricher.py:316` (`if "|" in str(record.get("source") or "")`),
  which today parses a `"|"`-joined string with no shared definition of what a source name
  is, and would naturally reach for a `SOURCES` constant living next to
  `scrapers/whitelist.py:17`/`:45`.

  **This is not theoretical.** During this audit, a concurrent agent's in-flight edit added
  exactly that edge and the tree stopped importing. The observed diff:
  ```diff
  --- a/enricher.py
  +++ b/enricher.py
   import re
  -import requests
  +from scrapers.base import BusinessRecord

  --- a/scrapers/__init__.py      (empty)
  +++ b/scrapers/__init__.py
  +from .base import BaseScraper, BusinessRecord
  +from .osm import OSMScraper
  +from .wikidata import WikidataScraper
  +from .google_places import GooglePlacesScraper
  +from .whitelist import is_business_category, industry_priority
  ```
  Result, live:
  ```
  File "enricher.py", line 3, in <module>
      from scrapers.base import BusinessRecord
  File "scrapers/__init__.py", line 4, in <module>
      from .google_places import GooglePlacesScraper
  File "scrapers/google_places.py", line 26, in <module>
      from enricher import infer_region
  ImportError: cannot import name 'infer_region' from 'enricher'
  ```
  Both edits were reverted before this report was written (`git status --short` clean for
  `*.py`), and `BusinessRecord` was **not used anywhere** in `enricher.py` — the import was
  dead on arrival. It was the *shape* of the import that broke the tree, not its content.
  Note the failure mode is worse than a clean cycle error: `enricher` is already half
  initialised in `sys.modules`, so the error names `enricher` as the broken module and
  `google_places.py` as the culprit, pointing the reader at the wrong file entirely.
- **Fix:** invert it. `infer_region` is pure region-inference logic with zero HTTP
  dependency; it belongs in a `regions.py` (or `geo.py`) leaf that both `enricher` and
  `scrapers/google_places` import, and which imports nothing local. Then
  `enricher → regions` and `scrapers.google_places → regions` with no edge back.

---

### S3 — dead module-scope imports that still hard-fail the import

- **Where:** `enricher.py:2` (`import requests`), `enricher.py:4`
  (`from urllib.parse import urljoin, urlparse`).
- **Breaks:** neither `requests`, `urljoin` nor `urlparse` is referenced anywhere at runtime
  in `enricher.py` — grep finds `requests` only at `enricher.py:2` and in a comment at
  `:125`; `urljoin`/`urlparse` only at `:4`. Dead code that nonetheless makes the module
  unimportable without `requests`, which is the direct cause of S1. The in-repo test suite
  has to monkeypatch `sys.modules` with a hand-rolled fake to work around it
  (`tests/test_lead_signal.py:23-63`, 40 lines of stub for two dead names).
- **Note:** `requests` is still genuinely required at `httpclient.py:43` (used at `:65` and
  `:176`), so deleting `enricher.py:2` alone fixes nothing — it just removes one of the two
  reasons the import can fail.
- **Fix:** delete `enricher.py:2` and `enricher.py:4`; delete `tests/test_lead_signal.py:23-63`
  once `enricher` no longer needs the stub.

---

### S3 — the logging dependency in the closure is entirely inert; 84 `print()`s and 2 dead `log.debug()`s

- **Where:** `httpclient.py:35`/`:45`, `scrapers/osm.py:1`/`:7`, `scrapers/wikidata.py:1`/`:7`.
- **Breaks:** three `getLogger` calls exist and only `httpclient`'s logger is ever used —
  twice, both at `log.debug` (`httpclient.py:178`, `:204`), both inside the retry path that
  `cmd_score` never reaches. `scrapers/osm.py:7` and `scrapers/wikidata.py:7` create loggers that are **never
  called** (zero `log.*` sites in either file). There is **no `basicConfig`, `dictConfig` or
  `setup_logging` anywhere in the tree**, so even the two real calls go to the root logger's
  last-resort handler and `DEBUG` is silently discarded. Print census across the closure:
  `cli.py` 47, `main.py` 19, `google_places.py` 8, `enricher.py` 4, `osm.py` 3,
  `wikidata.py` 3 — versus 2 `log.debug` calls total. `dedup.py`, `pitch_recommender.py` and
  `httpclient.py` have zero prints, so the codebase has three coexisting output channels and
  no policy.
  The layering symptom is visible inside one command: `cmd_score` prints its own summary at
  `cli.py:276`, and `pipeline.write_csv` prints a second line from a different layer
  (`main.py:134`) for the same operation:
  ```
    Written: /tmp/m.csv (2 records)
  re-scored 2 records, 2 changed -> /tmp/m.csv
  ```
- **Fix:** delete `scrapers/osm.py:1`/`:7` and `scrapers/wikidata.py:1`/`:7`; convert `httpclient.py:178`/`:204`
  to `log.warning` or add one `logging.basicConfig` in `cli.main()`; make
  `main.write_csv` return a summary instead of printing it.

---

### S3 — imports that exist only for a type annotation

- **Where:** `httpclient.py:41`, `pitch_recommender.py:22`, `scrapers/base.py:2`,
  `scrapers/osm.py:2`, `scrapers/wikidata.py:2`, `scrapers/google_places.py:22`.
- **Breaks:** these are the module-scope imports that a `TYPE_CHECKING` block exists for, but
  the codebase uses none.
  - `httpclient.py:41` `from typing import Any` — **100% dead at runtime.** The file has
    `from __future__ import annotations` at `:32`, so the three uses (`headers: dict[str, Any]`
    at `:105`, `headers: Any` at `:120`, `**kwargs: Any` at `:158`) are never evaluated. It is
    imported purely for a static checker.
  - `pitch_recommender.py:22` `from typing import Mapping` — used once, in the
    `recommend_service(record: Mapping)` annotation at `:25`. This file does **not** have
    `from __future__ import annotations`, so `Mapping` *is* evaluated at def time and the
    import is load-bearing. It is also deprecated style: `typing.Mapping` has been
    soft-deprecated since PEP 585 (3.9); `collections.abc.Mapping` is the replacement.
  - `Iterator` at `scrapers/base.py:2`, `scrapers/osm.py:2`, `scrapers/wikidata.py:2`,
    `scrapers/google_places.py:22` — same story. None of those four files has
    `from __future__ import annotations`, so the `Iterator[BusinessRecord]` return
    annotations (`scrapers/base.py:33`, `scrapers/osm.py:31`, `scrapers/wikidata.py:66`, `scrapers/google_places.py:168`) are
    eagerly evaluated and the imports are load-bearing **only because the `__future__` line is
    missing**. Adding `from __future__ import annotations` to all five files would make all
    four dead at runtime.
  - Only `httpclient.py` and `cli.py` have the `__future__` import; `enricher.py`, `main.py`,
    `dedup.py`, `pitch_recommender.py` and every `scrapers/*` module do not.
  - The genuinely load-bearing annotation import in the closure is
    `scrapers/base.py:2`'s `TypedDict`, which is a real class base at `:5` and cannot be made
    lazy.
- **Fix:** add `from __future__ import annotations` to the five modules above, move
  `typing` imports into `if TYPE_CHECKING:` blocks, switch `Mapping` to
  `collections.abc.Mapping`. This is the same finding as audit `066-typing-at-scale`; the
  dependency-specific consequence is that `typing` (and `urllib.parse` at
  `enricher.py:4`) are eagerly imported by every `leadminer score` invocation for nothing.

---

### S3 — `DATA_DIR` is defined twice as two distinct objects, and the `score` default path is cwd-relative

- **Where:** `cli.py:26` (`DATA_DIR = pathlib.Path("data")`) and `main.py:41` (identical).
  Verified `main.DATA_DIR is cli.DATA_DIR` → **False**; two independent `Path` objects.
- **Breaks:** `build_parser` bakes the cwd-relative literal `"data/all_businesses.csv"` into
  three subcommand defaults at `cli.py:294`, `:298` and `:305`. So `leadminer score` with no
  argument reads and **overwrites in place** whichever `data/all_businesses.csv` happens to be
  under the current working directory. Confirmed by running from `/private/tmp`:
  ```
  no such file: data/all_businesses.csv
  rc = 2
  ```
  `main.DATA_DIR` is the constant that actually decides where the pipeline writes
  (`main.py:150`, `:208-213`); nothing ties the two together, and nothing anchors either to
  the project root or to an env var.
- **Fix:** one `DATA_DIR` in `storage.py`, resolved from an env var with the package parent
  as the default, imported by both `cli.py` and `main.py`.

---

## Circular-import risk under a `src` layout

There is **no cycle in the tree today** (verified: 21 local edges, DAG). The risk is
structural, and the packaging layer has already recorded it.

**1. Every cross-module edge is a top-level absolute import, so none of them survives a
`src` move mechanically.** `main.py:25-31`, `enricher.py:128`, `scrapers/osm.py:4`,
`scrapers/wikidata.py:4`, `scrapers/google_places.py:26-27` and all eight `cli.py` lazy
imports use bare `from enricher import ...` / `from httpclient import ...`. Move the code to
`src/leadminer/` and every one of those must become `from leadminer.enricher import ...`, or
they resolve to *whatever else on `sys.path` happens to be called `enricher`*.

**2. The packaging config confirms the problem is known.** `pyproject.toml:35-37`:
```toml
[tool.hatch.build.targets.wheel]
# The package imports a top-level sibling module, so both must ship.
include = ["scrapers/"]
```
The comment is an admission: the wheel target has to be told to ship a *directory* because
the six top-level modules are not in a package. Under hatchling's auto-detection for
`name = "leadminer"`, neither `leadminer/` nor `src/leadminer/` exists, so `include` is a
band-aid over a missing layout. The console script at `pyproject.toml:33`
(`leadminer = "cli:main"`) points at a module that may not be in the wheel at all.

**3. The `scrapers` package is already half-migrated, and the seam is exactly where it
breaks.** `scrapers/osm.py:5` and `scrapers/wikidata.py:5` use relative
`from .base import ...` — they believe they are in a package. But `scrapers/osm.py:4`,
`scrapers/wikidata.py:4` and `scrapers/google_places.py:26-27` reach *outside* the package
with absolute top-level `from httpclient import ...` / `from enricher import ...`. So the
package boundary is declared on one side of each file and violated on the other. Renaming the
directory alone leaves 4 broken imports.

**4. `scrapers/__init__.py` is the amplifier.** It is currently **empty (0 bytes)** — which is
why the acquisition→enrichment edge (S2 above) is survivable. The moment it re-exports
anything from `google_places`, the eager `__init__` turns `from scrapers.base import X` into
`from scrapers import ...` and closes the loop. That is precisely what happened transiently
during this audit; the traceback is in S2 above.

**5. CI cannot catch any of it.** `.github/workflows/scrape.yml:39` installs `requirements.txt` (not
`pyproject.toml`) and `.github/workflows/scrape.yml:69` runs `python main.py` — never the installed console
script, never `leadminer score`, never `pip install .`. No import graph is ever validated in
an environment that resembles a user's.

**Migration order that avoids every cycle** (lowest layer first):

1. `storage.py` — `csv`/`os`/`pathlib` only. `FIELDS`, `DATA_DIR`, `load_master`,
   `write_csv`, `resolve_country`. Kills S1, S2-1, S2-4, S3-DATA_DIR at once.
2. `regions.py` — `REGION_KEYWORDS`, `_REGION_MAP`, `_LB_COORD_REGIONS`,
   `_KSA_COORD_REGIONS`, `infer_region`, no local imports. Kills the S2 layering inversion.
3. `scoring.py` — `lead_score`, `recommend_service`, `industry_priority`, the category
   sets. One place where "the score" is defined, callable by `main` and `cli` identically.
   Kills S2-3.
4. `sources.py` — the `SOURCES` set that `enricher.py:316` and `dedup._SOURCE_TRUST`
   (`dedup.py:97-110`) both currently hardcode.
5. Only then `src/leadminer/` + relative imports, mechanically, with
   `scrapers/whitelist.py` promoted out of `scrapers/` (it has no scraper dependencies at
   all — `whitelist.py` imports nothing).

---

## Not a bug, but worth knowing

- **`industry_priority` living in `scrapers/whitelist.py` is a naming smell, not a
  dependency bug.** `scrapers/whitelist.py:133` is a pure function of one string, imports nothing, and
  is the single source of truth for both the scraper filter (`is_business_category`,
  `scrapers/whitelist.py:105`) and the sales priority tier. `cmd_score`'s dependency on it
  (`cli.py:252`) is therefore *correct* — but reaching into a module whose docstring
  (`scrapers/whitelist.py:1-14`) is entirely about "dropping geographic noise leaking from OSM and
  Wikidata" to obtain a sales-priority rule is confusing. Fold into step 3 above.
- **`recommend_service` reads `completeness_score` and throws it away.** `pitch_recommender.py:48-52`
  parses and coerces `completeness` (including a `try/except` for `ValueError`/`TypeError`),
  and no tier from `:57` to `:99` ever references it — the only other mention is the word
  "completeness" in a comment at `:69`. Four lines of dead defensive parsing in
  `cmd_score`'s hot loop (one call per row, `cli.py:268`). Either implement the Tier-3
  completeness gate the comment at `:68-69` describes, or delete `:48-52`.
- **`load_master` does the type casting that `cmd_stats`/`cmd_validate` do not.**
  `cmd_score` is the only offline command whose numeric columns are typed (`main.py:90-101`,
  `_FLOAT_FIELDS`/`_INT_FIELDS` at `main.py:43-44`), which is why `lead_score`'s
  `rating < 4.0` comparison at `enricher.py:313` is safe there and why the `int()` coercion
  inside `recommend_service` (`pitch_recommender.py:45`, `:50`) is redundant when reached via
  `cmd_score`. Two independent casting strategies, applied at two different layers.
- **`write_csv`'s tmp path collides under concurrency.** `main.py:122` derives
  `data/all_businesses.csv.tmp`, so two concurrent `leadminer score` runs against the same
  file (the default in-place mode at `cli.py:274` makes this easy) share one temp file, and
  the loser of the race hits the `except BaseException: tmp.unlink()` at `main.py:131-132` —
  deleting the winner's in-progress temp. `.github/workflows/scrape.yml:15-17` added a `concurrency` group for
  the *workflow*; the CLI has no equivalent guard. Owned by audit `051-atomic-writes-and-concurrency`;
  listed here because `cmd_score` defaults to the in-place write that triggers it.
- **`main.write_csv` returning `None` while printing** (`main.py:134`) means `cmd_score`
  cannot report or suppress the write it just performed. `cli.py:276` prints its own line
  immediately after, producing the two-line duplicate output shown in S3.
- **Third-party dependency declaration is inconsistent with reality.** `requirements.txt`
  pins `beautifulsoup4` and `lxml`; neither is imported anywhere in the tree (0 import
  sites). `pyproject.toml:16-18` declares only `requests`. CI installs the former
  (`.github/workflows/scrape.yml:39`), the console script ships the latter.
- **Severity of the missing `requests` guard is environment-dependent.** `enricher.py:2` is a
  dead import, so once it is deleted, `httpclient.py:43` becomes the sole reason `score`
  needs `requests` — and `httpclient` will then be needed only by S2-1's extraction target
  for `cmd_score`. After step 1 of the migration order, `leadminer score` should need zero
  third-party packages. That is the goal state to assert in CI.

---

## Recommended order of work

1. **Extract `storage.py`** (`FIELDS`, `DATA_DIR`, `load_master`, `write_csv`,
   `resolve_country`; stdlib only) and point `cli.py:249` and `cli.py:26` at it. Kills S1,
   S2-1, S2-4 and the duplicate `DATA_DIR` in one move, and drops `cmd_score` from 11
   modules to 5 with no third-party imports.
2. **Add the `ImportError` guard** from `cli.py:226-231` to `cmd_score` anyway — defence in
   depth, and it is 4 lines.
3. **Extract `regions.py`** and re-point `enricher.py:79` and
   `scrapers/google_places.py:26` at it. Removes the one real layering inversion and makes
   the `enricher → scrapers` cycle impossible.
4. **Introduce `scoring.py`** with one `score_record(record)` that normalises the phone,
   sets priority, sets the pitch, then scores — called identically by `main.py:189-197` and
   `cli.py:265-272`. Kills S2-2 and S2-3 structurally; neither can regress if there is one
   code path.
5. **Give `--out` a different default** from the input path, or require `--in-place`, so the
   un-advertised `country` rewrite and the column-dropping round-trip both need an explicit
   operator decision.
6. **Delete the dead imports**: `enricher.py:2`, `enricher.py:4`, `scrapers/osm.py:1`+`:7`,
   `scrapers/wikidata.py:1`+`:7`, and `tests/test_lead_signal.py:23-63`. Then add
   `from __future__ import annotations` to the five modules that lack it and gate their
   `typing` imports behind `TYPE_CHECKING`.
7. **Make CI install the package and import it.** Replace `.github/workflows/scrape.yml:39` with
   `uv pip install --system -e .`, add a `python -c "import leadminer"`-equivalent smoke step
   and a run of `leadminer score --help` from a directory that is *not* the repo root. Until
   this exists, no finding in this report — and no cycle — can be caught before a user hits it.
8. **Only then move to `src/leadminer/`**, converting all 21 absolute edges mechanically
   once the layers above are real. Doing this first, as the config currently invites, is what
   produces the S1 traceback.