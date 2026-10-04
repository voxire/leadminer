# 200 — `build_parser` (`cli.py:280`) dependency map & dependency hygiene

## Verdict

The **eager** dependency closure of `build_parser` is tiny and correct: `import cli` loads
**zero** project modules and **zero** third-party modules, because all eleven project imports
are function-body-local. That deferral is the single best decision in `cli.py` and it must be
preserved through any restructuring.

The damage is entirely in the **deferred** tier and in `pyproject.toml`. Two things break the
moment this is packaged properly: the console script `leadminer = "cli:main"`
(`pyproject.toml:33`) points at a module that `pyproject.toml:35-37` excludes from the wheel
entirely, and the pipeline edge `cli -> main` (`cli.py:35`, `cli.py:249`) is the exact edge that
becomes a hard `ImportError: ... partially initialized module` cycle the instant the code moves
under a package — which I reproduced, with the real Python error, in a scratch tree.

**Layering:** one genuine violation, `scrapers/google_places.py:26` importing `enricher`
(a source adapter depends on a downstream pipeline stage), plus `enricher.py:128` stranding a
module-level import 127 lines into the file.

**Annotation-only imports: there are none.** I looked; I will not manufacture one.
**Logging-only imports: two, and both are dead** (`osm.py:7`, `wikidata.py:7`).

---

## Method and evidence discipline

- No network calls, no scrapers executed, no packages installed.
- All "loaded module" claims were measured by inspecting `sys.modules` after a controlled
  `import`, not inferred from reading.
- The circular-import claim was **reproduced** in
  `/private/var/folders/.../T/opencode/cycletest/src/leadminer/`, not reasoned about.
- **Disclosure:** while testing the `import main` resolution order I invoked `cli.cmd_run(None)`
  with a *stubbed* `requests` module. The stub blocked all egress (OSM/Wikidata raised on the
  stub; Google skipped on the absent API key) but the pipeline still ran to completion and
  wrote five header-only CSVs into `data/`. `data/` did not exist beforehand (`main.py:150-152`
  printed no "Loaded N records"), so I removed it. `git status` confirms I changed no source
  file. The `M enricher.py`, `M scrapers/__init__.py` and `src-probe/` entries visible in
  `git status` are other concurrent agents' work, not mine.

---

## The dependency map

### Tier 0 — eager closure: what `import cli` actually costs

Module-scope imports in `cli.py` are `argparse, collections, csv, pathlib, sys` (`cli.py:20-24`)
plus `from __future__ import annotations` (`cli.py:18`). Measured result of `import cli`:

```
PROJECT MODULES LOADED: none (only `cli` itself)
THIRD-PARTY LOADED:     none (`requests` is NOT imported)
build_parser() OK -> leadminer
```

**This is the finding that reframes everything else: `build_parser` has no third-party and no
project dependency beyond the standard library.** `--help`, `stats`, `validate` and a
`KeyboardInterrupt` all work on a checkout with zero dependencies installed.

### Tier 1 — name resolution at `build_parser()` call time

`build_parser` (`cli.py:280-309`) touches exactly four kinds of global:

| Global | Where defined | Nature |
|---|---|---|
| `__doc__` | `cli.py:1-16` | module docstring, used as `description` (`cli.py:281`) |
| `DATA_DIR` | `cli.py:26` | `pathlib.Path("data")`, interpolated into 3 defaults |
| `cmd_run`..`cmd_score` | `cli.py:33,40,79,135,204,247` | passed as `set_defaults(func=...)` |
| `argparse` | `cli.py:20` | stdlib |

So `build_parser`'s real contract is: *these six functions exist and are callable*. That is the
entire dependency surface at parse time. `args.func` is dispatched at `cli.py:315`.

### Tier 2 — deferred project imports, by command

| Command | Imports (all function-body-local) | Reaches |
|---|---|---|
| `run` | `main as pipeline` (`cli.py:35`) | `main.py:25-31` -> 3 scrapers, whitelist, dedup, enricher, pitch_recommender; then `httpclient` (via osm), `requests` |
| `scrape` | `scrapers.base.BaseScraper` (`cli.py:46`), `scrapers.google_places` (`:47`), `scrapers.osm` (`:48`), `scrapers.wikidata` (`:49`) | all three scrapers **unconditionally**; google_places drags in `enricher` -> `requests`, `httpclient` |
| `stats` | none | stdlib only |
| `validate` | none | stdlib only |
| `doctor` | `os` (`cli.py:206`), `requests` (`:226`) | deliberately guarded, see below |
| `score` | `main as pipeline` (`cli.py:249`), `enricher.lead_score` (`:250`), `pitch_recommender.recommend_service` (`:251`), `scrapers.whitelist.industry_priority` (`:252`) | `main.load_master`/`resolve_country`/`write_csv`, plus 3 scoring modules |

### Tier 3 — transitive closure (project edges only)

```
EAGER (module scope)                       DEFERRED (function bodies)
enricher              -> httpclient        cli -> main, enricher, pitch_recommender,
main -> dedup, enricher,                      scrapers.{base,google_places,osm,
      pitch_recommender,                              wikidata,whitelist}
      scrapers.{google_places,osm,
      wikidata,whitelist}              CYCLES FOUND: NONE — graph is a DAG
scrapers.google_places -> enricher,
      httpclient, scrapers.base         EAGER CYCLES FOUND: NONE — graph is a DAG
scrapers.osm          -> httpclient, scrapers.base
scrapers.wikidata     -> httpclient, scrapers.base
```

**Both tiers are acyclic today.** Every cycle below is *latent*, not live. I am not going to
claim an `ImportError` that does not currently happen.

---

## Findings

### S1 — the console script names a module the wheel does not ship

- **Where:** `pyproject.toml:33` (`leadminer = "cli:main"`) vs `pyproject.toml:35-37`
  (`[tool.hatch.build.targets.wheel]` / `include = ["scrapers/"]`)
- **Breaks:** `[project.scripts]` entry points are `module:function` and are resolved by
  `importlib` **after installation**. Hatchling's `include` selects *exactly* what ships
  ("You can set the `include` and `exclude` options to select exactly which files will be
  shipped in each build" — hatch docs, Build configuration / Patterns). `include = ["scrapers/"]`
  therefore ships six files:

  ```
  scrapers/__init__.py  scrapers/base.py  scrapers/google_places.py
  scrapers/osm.py       scrapers/whitelist.py  scrapers/wikidata.py
  ```

  and **not** `cli.py`, `main.py`, `enricher.py`, `dedup.py`, `httpclient.py`, or
  `pitch_recommender.py`. The console script then fails at line 3 of the generated shim.

- **Trigger:** install from a built wheel, then run any subcommand. Reproduced with a shim
  equivalent to the one hatchling/pip generates:

  ```
  $ ./bin/leadminer run
  Traceback (most recent call last):
    File "./bin/leadminer", line 3, in <module>
      from cli import main
  ModuleNotFoundError: No module named 'cli'
  ```

- **Why it has never been noticed:** `cli.py:230` tells the user to run
  `pip install -e '.[dev]'`. Hatchling's *editable* mode derives its path entries from the
  selected files' location, so an editable install puts the **project root** on `sys.path` and
  `import cli` works. Only the real wheel is broken. The dev loop masks the release artifact
  permanently.
- **The comment already concedes the problem:** `pyproject.toml:36` reads *"The package imports
  a top-level sibling module, so both must ship."* — then ships one.
- **Fix:** this is not a one-line fix, it is the src-layout move. Do them together:
  `git mv` the six top-level modules to `src/leadminer/`, change the entry point to
  `leadminer = "leadminer.cli:main"`, set `[tool.hatch.build.targets.wheel] packages =
  ["src/leadminer"]`, and rewrite the absolute imports (see the src-layout section for the
  exact file:line list).

### S1 — `cli.main()` lets every failure except `KeyboardInterrupt` escape as a traceback

- **Where:** `cli.py:312-318`
- **Breaks:** `build_parser` wires six commands into a process whose dispatcher catches exactly
  one exception. Any `ImportError`, `OSError`, `csv.Error` or `TypeError` from any command
  reaches the user as a raw Python traceback. This is the CLI that `cli.py:14-15` and
  `README.md:19-20` position as *"what you reach for when a run looks wrong"* — so the traceback
  *is* the diagnostic path, and it is the one path with no formatting, no exit-code discipline
  and no mention of which subcommand failed.
- **The inconsistency is the evidence.** `cmd_doctor` proves the authors already knew the
  correct pattern and applied it in exactly one place — `cli.py:226-231` wraps `import requests`
  in `try/except ImportError` with the comment *"doctor exists to diagnose a broken
  environment, so it must never be the thing that tracebacks."* Six of seven call sites do not.
- **Trigger — this checkout, right now, no setup at all.** `requests` is not installed here:

  ```
  leadminer doctor -> exit 1 | "  FAIL  requests is not installed - run: pip install -e '.[dev]'"
  leadminer score  -> UNCAUGHT ModuleNotFoundError: No module named 'requests'
  ```

  Two commands, same broken environment, opposite behaviour. `doctor` answers the question
  cleanly; `score` dumps a stack trace.
- **Fix:** wrap the dispatch at `cli.py:315` in `except Exception`, print
  `f"{args.cmd}: {type(e).__name__}: {e}"` to stderr, and return 1 — keeping the traceback behind
  an env var / `--traceback` flag.

### S2 — a scraper depends on the enrichment stage (layering violation)

- **Where:** `scrapers/google_places.py:26` — `from enricher import infer_region`
- **Breaks:** the acquisition layer imports a downstream pipeline stage. `infer_region` is a
  pure text/coordinate function (`enricher.py:79-94`) with no business in being inside
  `enricher.py`; putting it there and then importing it *upward* means the source adapters can
  never be used, tested, or reasoned about without the enrichment stage.
- **Trigger (measured):** importing one scraper loads the whole enrichment stage:

  ```
  $ python -c "import scrapers.google_places"
     enricher         loaded: True
     requests         loaded: True
     httpclient       loaded: True
  ```

  So the Google Places adapter cannot be exercised without `requests` installed — which is why
  `tests/test_lead_signal.py:23-30` has to hand-roll a `_stub_requests()` shim just to run the
  tests.
- **Aggravating:** `leadminer scrape --source osm` loads it too, because `cmd_scrape` imports
  all three scrapers up front. Measured after `--source osm`:

  ```
  scrapers.google_places  True    enricher  True    requests  True
  ```

  The enrichment stage is a hard dependency of a scrape-only command.
- **Fix:** move `infer_region` (and its `REGION_KEYWORDS` / `_REGION_MAP` / `*_COORD_REGIONS`
  tables, `enricher.py:10-76`) into a neutral `geo.py` that both layers may import. Nothing
  imports upward.

### S2 — `import main as pipeline` depends on a module name owned by nobody

- **Where:** `cli.py:35` and `cli.py:249`
- **Breaks:** `import main` is a **global top-level namespace lookup** resolved from `sys.path`
  at call time. `main` is not a name this project owns, and it is close to the worst possible
  choice: it is the single most commonly used module filename in the Python ecosystem.
  Verified: `importlib.util.find_spec("main")` in this repo returns
  `origin='/Users/mhomsi/dev/dummy/leadminer/main.py'` — i.e. the resolution is decided purely
  by `sys.path` order, with no package namespace to constrain it.
- **I tried to trigger a hijack and could not, and here is why:** in every development
  invocation (`python cli.py`, `python main.py`, `python -m ...`) the repo root is `sys.path[0]`,
  so the correct `main.py` wins. I demonstrated the impostor path resolves correctly rather than
  being taken. **Treat this as latent, not live.**
- **Why it still matters:** it is the same unnamespaced-root problem that produces the S1, and
  it is the edge that turns fatal under a package. Concretely, it means `cli` cannot be moved
  into a package without rewriting this line — a package cannot contain a module that its own
  siblings address by bare top-level name.
- **Fix:** `from . import pipeline` (after `git mv main.py src/leadminer/pipeline.py`). If the
  name must stay `main`, it must at least be `leadminer.main`.

### S2 — `cmd_scrape` imports all three scrapers to use one

- **Where:** `cli.py:47-49`
- **Breaks:** the docstring at `cli.py:41-45` promises this command exists to *"check a single
  source after an API change"*. It cannot: a broken or renamed `google_places.py` takes down
  `--source osm`.
- **Trigger (measured):** with `scrapers/google_places.py` moved aside —

  ```
  $ leadminer scrape --source osm
  ModuleNotFoundError: No module named 'scrapers.google_places'
  ```

  Restored afterwards; `git diff --name-only -- scrapers/google_places.py` is empty, the
  move was lossless.
- **Fix:** import inside the `registry` lookup, or key the registry by lazy import path
  (`"google_places": "scrapers.google_places:GooglePlacesScraper"`) resolved with `importlib`.

### S3 — `BaseScraper` imported for a type relationship that was never written

- **Where:** `cli.py:46` — `from scrapers.base import BaseScraper  # noqa: F401  (import check)`
- **Breaks:** this is the lens's "imported only for a type annotation" case, and it is
  instructive precisely because the annotation **does not exist**. `grep -n BaseScraper cli.py`
  returns this one line. `registry` (`cli.py:51-55`) is an untyped
  `dict[str, type[<nothing>]]`, so the one symbol that would let the registry be typed is
  imported and discarded.
- **The `# noqa: F401` also disables the linter permanently** for a line whose stated purpose
  ("import check") the code never implements: there is no `try/except`, no assertion, no
  reference. It can only ever raise.
- **Fix:** delete it, or make it real —
  `registry: dict[str, type[BaseScraper]] = {...}` and drop the `noqa`.

### S3 — a module-level import stranded 127 lines into `enricher.py`

- **Where:** `enricher.py:128` — `from httpclient import DEFAULT_MAX_BODY_BYTES, get_session`
- **Breaks:** this is a genuine module-scope import (confirmed eager: `enricher` is in
  `sys.modules` on plain import) sitting *after* the `REGION_KEYWORDS` tables and the
  `_REGION_MAP` comprehension (`enricher.py:52-55`). The transport concern — body cap, session —
  is attached in the middle of the enrichment business logic. Ruff's isort rule
  (`"I"`, `pyproject.toml:47`) only governs the top import block and will **not** flag this, so
  it survives lint indefinitely.
- **Fix:** move to `enricher.py:1-4`.

### S3 — undeclared `urllib3` plus a stale, process-global warning suppression

- **Where:** `enricher.py:327-328`, inside `enrich()`
- **Breaks (three distinct problems, one line):**
  1. `urllib3` is **not a declared dependency** — `pyproject.toml:16-18` declares only
     `requests>=2.32.3,<3`. `urllib3` is reachable here purely as a transitive of `requests`.
     `pyproject.toml:70-73` even has an override for `urllib3.*` in the mypy config, so the
     module is knowingly depended on while being undeclared.
  2. It is a **process-global side effect** (warning filters are interpreter-wide) hidden
     inside a stage function, so it fires as a side effect of `leadminer run`
     (`cli.py:35` -> `main.main` -> `enrich`) for any code in the process.
  3. It is **stale**. `enricher.py:170-175` documents that the call is now `verify=True`
     ("This was `verify=False`..."); `grep -rn verify *.py scrapers/*.py` finds no
     `verify=False` anywhere. `InsecureRequestWarning` can no longer be raised by this code
     path, so the suppression can only ever silence a warning caused by something *else* —
     including a future change that reintroduces `verify=False`.
- **Fix:** delete lines 327-328. If a suppression is ever genuinely wanted, do it once at
  process entry in `cli.main()` with a comment saying which code it protects.

### S3 — two dependency manifests that disagree, and two pins nothing imports

- **Where:** `requirements.txt:1-3` vs `pyproject.toml:16-18`
- **Breaks:** `requirements.txt` pins `beautifulsoup4==4.12.3` and `lxml==5.2.2`.
  `grep -rn "bs4\|BeautifulSoup\|lxml" *.py scrapers/*.py` finds **zero** usages — HTML is
  handled by regex in `enricher.py:199-220`. Meanwhile `pyproject.toml` declares only `requests`,
  so the two manifests describe different worlds. Under a hatchling build `requirements.txt` is
  **not read at all**; those two "pins" are decorative, and the exact-pinning the audit trail
  credits the project with (`requirements.txt`) does not exist in the build path.
- **Fix:** delete `requirements.txt`, or reduce it to `-e .[dev]`. Add `urllib3` to
  `pyproject.toml` dependencies or stop importing it.

### S3 — `DATA_DIR` is defined twice and can drift

- **Where:** `cli.py:26` and `main.py:41` — both `pathlib.Path("data")`
- **Breaks:** `cmd_doctor` (`cli.py:217-222`) reports on `cli.DATA_DIR` while `cmd_run`
  (`cli.py:35` -> `main.py:150`) writes to `main.DATA_DIR`. They agree today only because both
  happen to be CWD-relative and identical. Making either configurable (see audit
  `036-config-management.md`) updates one and not the other, and `doctor` then reports on a
  directory the pipeline never writes to.
- **Fix:** one definition, imported. This is a free side-effect of the src-layout move.

---

## Circular imports under a src layout

**Status today: none.** Both the eager and the deferred graph are DAGs (verified above). The
following is what *creates* them.

### The `cli <-> pipeline` cycle (reproduced)

The console-script entry point (`pyproject.toml:33`) already treats `cli` as the process entry
point, which invites the reverse edge "`main.py` exposes the CLI" — to share the command list,
dispatch through it, or document it in its docstring (`main.py:14-15` currently says
`python main.py`). Combined with `cli.py:35`'s `import main`, that is a 2-cycle.

Because `cli.py:35`'s import is currently *inside a function body*, the deferral accidentally
breaks the cycle. Hoist it to module scope — which is what `ruff`'s isort rule (`"I"`,
`pyproject.toml:47`) **tells you to do** — and it detonates. Reproduced in a scratch src tree
with the import placed at the top of the import block, i.e. correctly styled:

```
ImportError: cannot import name 'build_parser' from partially initialized module
'leadminer.cli' (most likely due to a circular import)
```

This is the trap: **the deferral that saves you today is the same deferral a formatter/linter
will tell you to remove.**

### The package `__init__.py` variant

`scrapers/google_places.py:26`'s upward edge becomes
`from ..enricher import infer_region`. Now any import of `leadminer.scrapers.google_places`
executes `leadminer/__init__.py` first. The moment `__init__.py` is non-empty — exporting
`__version__`, a `SCRAPERS` registry, or re-exporting `leadminer.enricher.infer_region` — the
failure moves from a module to the **package `__init__`**, which is harder to read and fails
every entry point at once, including the console script.

### Every absolute import that must be rewritten

`scrapers/` currently mixes two styles: intra-package via relative (`.base`, `osm.py:5`),
inter-package via bare top-level (`httpclient`). The second kind breaks under a package:

| File:line | Import | Becomes |
|---|---|---|
| `scrapers/osm.py:4` | `from httpclient import ...` | `from ..httpclient import ...` |
| `scrapers/wikidata.py:4` | `from httpclient import ...` | `from ..httpclient import ...` |
| `scrapers/google_places.py:27` | `from httpclient import ...` | `from ..httpclient import ...` |
| `scrapers/google_places.py:26` | `from enricher import ...` | `from ..enricher import ...` |
| `enricher.py:128` | `from httpclient import ...` | `from .httpclient import ...` |
| `cli.py:35`, `cli.py:249` | `import main as pipeline` | `from . import pipeline` |
| `cli.py:46-49`, `cli.py:252` | `from scrapers.X import Y` | `from .scrapers.X import Y` |
| `cli.py:250`, `cli.py:251` | `from enricher/pitch_recommender import` | `from .enricher/... import` |
| `main.py:25-31` | 7 absolute imports | `from .scrapers... / .dedup / ...` |
| `pyproject.toml:33` | `cli:main` | `leadminer.cli:main` |
| `pyproject.toml:36-37` | `include = ["scrapers/"]` | `packages = ["src/leadminer"]` |

**14 edits across 8 files plus `pyproject.toml`.** Every one of them must land in a single
commit — a half-moved tree has no working import path at all. This is the single highest-risk
refactor in the codebase and it should be one PR with no behavioural change.

---

## Imports only for annotations, and imports only for logging

The lens asked for both explicitly. Direct answers:

**Annotation-only: none.** `cli.py` needs no `TYPE_CHECKING` block because the only annotation
it uses is `argparse.Namespace` (`cli.py:33,40,79,135,204,247`) and `argparse` is a real
module-scope import. The three `from typing import Iterator` statements —
`scrapers/osm.py:2`, `scrapers/wikidata.py:2`, `scrapers/google_places.py:22` — are used solely
in the `-> Iterator[BusinessRecord]` return annotation (`osm.py:31`, `wikidata.py:66`,
`google_places.py:168`), but they are **runtime** imports, not annotation-only, because none of
those three modules has `from __future__ import annotations`. That is a consistency gap, not a
dependency: `cli.py:18` and `httpclient.py:32` do have it. Cost is ~0 (`typing` is already in
`sys.modules` via stdlib). `scrapers/base.py:2`'s `Iterator` is likewise only the abstract
signature at `base.py:33`.

The closest thing to an annotation-only import is the S3 `cli.py:46` finding above: `BaseScraper`
is imported in the *shape* of a type import, but the annotation it was imported for was never
written.

**Logging-only: two, both dead.**

| File:line | Statement | Uses |
|---|---|---|
| `scrapers/osm.py:1,7` | `import logging` / `log = logging.getLogger(__name__)` | **0** |
| `scrapers/wikidata.py:1,7` | `import logging` / `log = logging.getLogger(__name__)` | **0** |
| `httpclient.py:35,45` | `import logging` / `log = ...` | 2 (live: `httpclient.py:178,204`) |

`osm.py` and `wikidata.py` each construct a logger and emit **nothing through it**. All their
output is bare `print` (`osm.py:33,51,59`; `wikidata.py:68,80,88`). The consequence is not
cosmetic: these are the two sources whose *failure* is the thing you most need to see
(`osm.py:51` and `wikidata.py:80` are the "a source broke, do not confuse it with an empty
source" messages). They are untyped, unlevelled, unfilterable stdout lines. Any log-based
alerting built on `httpclient`'s real logger will see a silent run. And `cli.py` has **no
logging at all** — zero `logging` import, zero `getLogger`, zero `log.` — so the CLI layer
itself cannot emit a structured event.

- **Fix:** convert `osm.py`/`wikidata.py` `print` to `log.warning`/`log.error`, and add a
  `log = logging.getLogger(__name__)` to `cli.py` (coordinated with audit `040-observability.md`).

---

## Not a bug, but worth knowing

- **The deferral is genuinely good and is load-bearing.** Every one of the eleven project
  imports in `cli.py` is function-body-local. That is why `import cli` costs nothing, why
  `leadminer stats`/`validate`/`--help` work with no dependencies installed, and why the
  `cli <-> pipeline` cycle does not fire today. When you move to a src layout, **keep the
  imports inside the function bodies.** Moving them to module scope is what converts a latent
  2-cycle into a hard `ImportError`, and `ruff`'s isort rule will push you to do exactly that.
- **`cmd_doctor`'s `requests` guard (`cli.py:226-231`) is the correct pattern and is used
  exactly once.** It is the model for the dispatch-level fix in the S1 above.
- **`build_parser` has no real dependency risk of its own.** Its only fragile inputs are the
  three CWD-relative path defaults (`cli.py:294,298,305`) and the `--source` choices
  (`cli.py:290`) duplicating the registry inside `cmd_scrape` — both already covered by
  `200-fn-cli-py-build_parser-contract.md` and `...-tests.md`. I am not re-reporting them.
- **`main.py`'s import block (`main.py:25-31`) is the widest edge in the codebase** — one line
  that reaches 7 modules and transitively 4 more. It is the reason the whole pipeline is
  all-or-nothing at import time and the reason `cmd_score` (`cli.py:249`) cannot re-score
  without `requests`. Narrowing it (e.g. `main.py` importing scrapers inside `main()`) is a
  cheap, behaviour-preserving precursor to the src-layout move.

---

## Recommended order of work

1. **Fix the dispatch guard** (`cli.py:312-318`) — ~5 lines, no restructuring, turns every
   future traceback into a clean message and an exit code. Do this first because it makes steps
   2-5 safe to attempt.
2. **Do the src-layout move as one atomic commit** — the 14 edits in the table above plus
   `pyproject.toml:33,36-37`. No behaviour change. This is the S1 and it is the prerequisite for
   everything else on this list. Do it before adding a fourth scraper, because that is when the
   14-edit surface doubles.
3. **Verify the wheel, not the editable install** — `python -m build` (or `pip wheel .`) then
   install the artifact into a clean venv and run `leadminer --help`, `leadminer stats`,
   `leadminer doctor`. The editable install (`pip install -e .`) cannot detect this class of bug.
4. **Fix the layering violation** — move `infer_region` + its tables (`enricher.py:10-94`) to a
   neutral `geo.py`; rewrite `scrapers/google_places.py:26` to import downward. Now safe and
   one-line once step 2 has landed.
5. **Small, independent, can be done in any order:**
   - `cli.py:47-49` — import only the scraper you are about to run.
   - `cli.py:46` — type the registry with `BaseScraper` or delete the import.
   - `enricher.py:128` — move to the import block; delete `enricher.py:327-328`.
   - Delete `requirements.txt`; add `urllib3` to `pyproject.toml`.
   - `osm.py:7` / `wikidata.py:7` — either use the loggers or drop them; add one to `cli.py`.