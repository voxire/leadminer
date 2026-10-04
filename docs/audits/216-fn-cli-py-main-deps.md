# 200 — Dependency map of `main()` at `cli.py:312`, and the four edges that are wrong

**Target:** `def main(argv: list[str] | None = None) -> int` — `cli.py:312-318`.
**Lens:** transitive dependency tracing. Every module, function and global reachable
from the seven-line dispatcher, then: what is surprising, what inverts the layering, what
becomes a circular import under a src layout, and what is imported only for an annotation
or only for logging.

**Method.** `__import__` hooked in a fresh subprocess per command, hook installed *before*
`import cli` so the cost of the command itself is measured. No network: `requests` was
replaced with the same raising stub the repo's own tests use
(`tests/test_lead_signal.py:23-63`). Circular-import claims were **executed** against
purpose-built replicas in `/private/var/.../opencode/deps200/`, not reasoned about — two of
my hypotheses were wrong and the results are reported as such. No repo file was modified.

## Verdict

The dispatcher itself is clean and its lazy imports are **load-bearing, not laziness**:
`cli.main`'s entire module-scope closure is five stdlib modules (`argparse`, `collections`,
`csv`, `pathlib`, `sys`) and **zero project modules**, and hoisting `cli.py:35` to module
scope is a verified one-way trip into a hard `ImportError`. All the damage is in the four
edges *below* it. `cli.py:35 import main` points at a module that is not a library and is
not shipped, so the installed console script cannot run `leadminer run` at all;
`cli.py:249-252` makes a command documented as offline import all three scrapers,
`enricher`, `httpclient` and `requests` (10 project modules, 115 imports) purely to rewrite a
CSV; `cli.py:47-49` imports all three scrapers to run one Overpass query; and
`enricher.py:26` is the single edge in the repo that points from infrastructure upward into
the processing layer.

---

## Dependency map

### Module-scope closure of `cli` (what `import cli` costs)

```
cli.py:18  from __future__ import annotations   -> __future__
cli.py:20  argparse      cli.py:21  collections
cli.py:22  csv           cli.py:23  pathlib      cli.py:24  sys
cli.py:26  DATA_DIR = pathlib.Path("data")       <- module global, CWD-relative
```

Nothing else. `csv` and `collections` are module-scope but `cmd_doctor`/`cmd_run`/
`cmd_scrape` never use them; only `cmd_stats`/`cmd_validate` do.

### Per-command closure (measured, fresh subprocess per command)

| Command | total imports | project modules pulled in | `requests`? |
|---|---|---|---|
| `leadminer stats FILE` | 66 | `cli` | no |
| `leadminer validate FILE` | 66 | `cli` | no |
| `leadminer doctor` | 67 | `cli` | **yes** |
| `leadminer scrape --source osm` | 111 | `cli`, `enricher`, `httpclient`, `scrapers.base`, `scrapers.osm`, **`scrapers.wikidata`**, **`scrapers.google_places`** | **yes** |
| `leadminer score FILE` | **115** | `cli`, `main`, `dedup`, `enricher`, `httpclient`, `pitch_recommender`, `scrapers.whitelist`, `scrapers.osm`, `scrapers.wikidata`, **`scrapers.google_places`** | **yes** |

Read that table against the docstring at `cli.py:14-15` ("Commands other than
`run`/`scrape`/`doctor` never touch the network, so they are safe to run anywhere"). For
`stats` and `validate` the claim is materially true: zero project modules, zero third-party.
For `score` it is false in the sense that matters — it needs `requests` installed to do
nothing but arithmetic and CSV I/O.

### The full graph (module-scope edges, then function-local edges)

```
cli  --fn-->  main, scrapers.base, scrapers.{osm,wikidata,google_places},
               enricher, pitch_recommender, scrapers.whitelist, os, requests
main        --> scrapers.osm, scrapers.wikidata, scrapers.google_places,
               scrapers.whitelist, dedup, enricher, pitch_recommender
scrapers.google_places --> enricher          <-- the only upward edge in the repo
scrapers.osm / .wikidata --> httpclient, scrapers.base
enricher    --> httpclient                  (import sits at enricher.py:128, mid-file)
httpclient  --> (no project module)
dedup, pitch_recommender, scrapers.whitelist, scrapers.base --> (no project module)
```

`cli.main` reaches its six handlers through `args.func` (`cli.py:285-307`), so **no handler
is a static callee** — that edge is invisible to every static tool in the repo and to
`pyproject.toml`'s mypy config, and it is why `tests/test_lead_signal.py:215,232,246,259`
has to hand-build `argparse.Namespace(file=p)`: no parser invocation would produce that
namespace.

### Globals `main()` depends on

- `DATA_DIR` (`cli.py:26`) — a *separate* `pathlib.Path("data")` from `main.py:41`. Same
  value by coincidence, not by reference. Baked into three argparse defaults at
  `cli.py:294,298,305` at parser-construction time.
- `__doc__` (`cli.py:281`) — user-facing `--help` text is the module docstring, which still
  documents `python main.py` as the way to run (`cli.py:4`, `README.md:11-16`), an
  invocation CI still uses and the CLI was built to replace.
- `_fmt` (`cli.py:29`) — `cmd_stats` only.
- `sys.stderr` (`cli.py:24`) — used at `:57, :65, :83, :89, :139, :145, :256, :261, :317`.
  `print` and bare `os` (function-local, `:206`) are the only other I/O channel.

---

## Findings

### S1 — `cmd_run`'s single dependency is a script that is not a library and is not shipped

- **Where:** `cli.py:35` (`import main as pipeline`), `pyproject.toml:33`
  (`leadminer = "cli:main"`), `pyproject.toml:36-37`
- **Breaks:** `main` is a top-level module at the repo root that is simultaneously a library
  and a script (`main.py:245-246`: `if __name__ == "__main__": main()`). Two things follow.
  (1) `pyproject.toml:36-37` says in a comment that "the package imports a top-level sibling
  module, so both must ship" and then ships `include = ["scrapers/"]` — `main.py` and
  `cli.py` are not in any `include`/`packages`/`sources` list, and there is no
  `leadminer/__init__.py` or `src/leadminer/` for hatchling to infer. (2) Even granting a
  successful build, `leadminer run` cannot work, because `import main` at `cli.py:35` looks
  for a top-level `main` on `sys.path`, and a console script's `sys.path[0]` is the `bin/`
  directory, not the checkout.
- **Trigger:** install the distribution and run the entry point it declares. Simulated by
  staging exactly what `pyproject.toml:37` ships (`scrapers/` + `cli.py`) as the only
  importable content:
  ```
  leadminer stats /dev/null -> 1
  leadminer run            -> ModuleNotFoundError: No module named 'main'
      File ".../cli.py", line 315, in main      return int(args.func(args) or 0)
      File ".../cli.py", line 35, in cmd_run    import main as pipeline
  ```
  Unhandled: `cli.py:316` catches only `KeyboardInterrupt`, so the console entry point
  surfaces a raw traceback. Same failure with `requests` simply absent (verified on this
  machine, where `requests` is not installed for `python3`):
  `import main` -> `main.py:25` -> `scrapers/osm.py:4` -> `httpclient.py:43`
  `ModuleNotFoundError: No module named 'requests'`.
- **Fix:** `main.py` becomes `leadminer/pipeline.py` inside a real package, `cli.py` becomes
  `leadminer/cli.py`, `pyproject.toml:33` becomes `leadminer = "leadminer.cli:main"`, and
  `main.py:25-31` + `enricher.py:128` + `cli.py:35,46-49,249-252` all become relative or
  qualified imports.
- **Delta from prior art:** `030-packaging-import-hygiene.md` and
  `020-type-contract-import-cycle.md` were written before `pyproject.toml` existed (both
  open by asserting there is none). Neither addresses the `cli:main` entry point versus the
  wheel include list. `246-x.md:342` lists "a distribution providing a top-level `main` or
  `cli` module" as a *risk*; this is the verified hard failure.

### S1 — `cmd_score` inherits the whole pipeline to do local CSV arithmetic, and dies without `requests`

- **Where:** `cli.py:249-252` (`import main as pipeline`, `from enricher import lead_score`,
  `from pitch_recommender import recommend_service`, `from scrapers.whitelist import
  industry_priority`), consumed at `cli.py:259` (`pipeline.load_master`), `:266`
  (`pipeline.resolve_country`), `:275` (`pipeline.write_csv`)
- **Breaks:** three of the four things `cmd_score` needs — the column list, the type-casting
  contract, and the atomic writer — live in the **orchestration** module, so a purely local
  `CSV -> dicts -> CSV` transform has no choice but to import `main`, which imports all
  three scrapers. Measured: **10 project modules and 115 imports**, versus 1 and 66 for
  `stats`. There is no `storage`/`io` module, so *every* consumer of the CSV contract (the
  CLI, `tests/`, any future database writer) must import the pipeline. The root cause of the
  S1 already filed four times over by siblings (schema-narrowing in-place rewrite deleting
  every column outside `main.FIELDS`: `cli.py:274-275` -> `main.py:33-40,125`) is this same
  edge: the CLI has no schema of its own, so it inherits a writer that cannot preserve
  unknown columns and says nothing.
- **Trigger:** `pip install requests` omitted on a machine that only needs to re-score a
  local file, or `leadminer score` under a src-layout install where `main` is not
  importable (see S1 above). Both are raw tracebacks, because `cli.py:314-318` catches only
  `KeyboardInterrupt`.
- **Fix:** extract `leadminer/storage.py` owning `FIELDS`, `load_records`, `write_records`
  (with unknown-column preservation and a header assertion), and have `main` and `cli`
  depend on *that*. `cmd_score`'s closure drops to `cli` + `storage` + the three pure
  scoring functions, with no scraper and no `requests` reachable.
- **Not new:** `200-fn-cli-py-build_parser-correctness.md` S2-1 already reports the
  `import requests` crash. What is new here is the measurement and the ownership argument:
  the crash is a symptom; the cause is that the CSV contract lives in `main.py`.

### S2 — `leadminer scrape --source osm` imports two scrapers it will never run, plus `enricher`, `httpclient` and `requests`

- **Where:** `cli.py:47-49` (all three classes imported unconditionally inside the function
  body, *before* the registry lookup at `:51-56`)
- **Breaks:** the registry at `cli.py:51-55` maps names to already-imported classes, so
  there is no laziness available: the cheapest diagnostic command in the tool (README:19-20
  calls `stats`/`validate`/`score` what you reach for first; `cli.py:41-44` calls `scrape`
  "useful for checking a single source after an API change") drags in the whole stack.
  Measured for `--source osm`: 111 imports, 7 project modules, including
  `scrapers.wikidata` and `scrapers.google_places` — and `enricher` + `httpclient` +
  `requests` arrive only because `scrapers/google_places.py:26` does
  `from enricher import infer_region`. So the module the CLI most wants to keep isolated
  (the paid API) is the one that couples the probe to the enrichment layer.
- **Trigger:** add a fourth source. `registry[args.source]` at `cli.py:61` can only reach it
  if the class is imported at `:47-49`, so every new source must edit that import block
  *and* `cli.py:290`'s `choices` *and* `main.py:155`'s `[OSMScraper(), WikidataScraper(),
  GooglePlacesScraper()]` — three hand-synced lists, already duplicated (siblings
  `200-fn-cli-py-build_parser-correctness.md` S3-2 and
  `200-fn-cli-py-build_parser-concur.md` S3 flag the first two).
- **Fix:** put the registry in `leadminer/scrapers/__init__.py` keyed by module path, and
  resolve it with `importlib.import_module` inside `cmd_scrape`; drive `main.py:155` and
  `cli.py:290` from the same object. `choices` becomes impossible to drift.

### S2 — `enricher` imports `httpclient` for one symbol and skips its retry layer, so the highest-volume caller in the system has no retries at all

- **Where:** `enricher.py:128` (imports only `DEFAULT_MAX_BODY_BYTES` and `get_session`),
  `enricher.py:175` (`get_session().get(url, timeout=8, allow_redirects=True, stream=True)`)
- **Breaks:** `httpclient.py:12-15` states that hand-rolled retry loops in `osm.py` and
  `wikidata.py` "are now centralised with exponential backoff and full jitter". True for
  those two (`osm.py:39-46`, `wikidata.py:70-78` both pass `retries=3`). The **largest**
  caller bypasses it: `enricher._fetch_website` calls `session.get` directly, so the
  `1 HTTP GET per website at 40 workers` sweep has zero retries, no jitter, no `Retry-After`
  handling, and the raw `requests` timeout rather than `DEFAULT_TIMEOUT`/`DEFAULT_RETRIES`.
  Every transport error becomes `UNKNOWN` at `enricher.py:176-178`, which is the correct
  *classification* and the wrong *recovery*. The dependency is also gratuitously narrow: the
  module that owns the whole HTTP policy is used for one constant and a session factory.
- **Trigger:** Overpass/Wikidata get 3 attempts each; a business website that resets the
  connection on first connect (`_fetch_website` -> `requests.ConnectionError` ->
  `UNKNOWN`) gets one. At 40 workers over a cumulative master this is the single largest
  source of `website_live=None` rows, i.e. records the tool declines to score and sell.
- **Fix:** `enricher._fetch_website` calls
  `httpclient.fetch_with_retry("GET", url, max_bytes=DEFAULT_MAX_BODY_BYTES, retries=2)` and
  reads `FetchResult.status`/`body` instead of touching `requests` at all. That also removes
  the `requests` reachability noted above.

### S2 — `enricher` imports `urllib3`, which `pyproject.toml` does not declare, to disable a warning that can no longer fire

- **Where:** `enricher.py:327-328`
  (`import urllib3; urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)`),
  called from `enrich()` at `enricher.py:337`; `pyproject.toml:16-18` declares only
  `requests>=2.32.3,<3`
- **Breaks:** a direct import of a *transitive* dependency with no declaration — a phantom
  dependency that works only because `requests` happens to pin `urllib3`. It is also
  self-defeating: the comment at `enricher.py:170-174` documents that `verify=False` was
  replaced by `verify=True` (default — `_fetch_website` at `:175` passes no `verify`), so
  `InsecureRequestWarning` can no longer be raised on this path. The call is pure downside:
  it cannot report anything today, and if anyone reintroduces `verify=False` it will silence
  the only signal that the TLS regression happened.
- **Trigger:** delete `enricher.py:327-328` — nothing changes, verified by inspection of the
  only two `_fetch_website` call paths (`:238`). Or, invert it: someone "temporarily" passes
  `verify=False` to debug a TLS error, and the warning that would have caught it is globally
  suppressed for the process.
- **Fix:** delete both lines. If a project-wide policy is wanted, configure it once in
  `leadminer/httpclient.py` and only for warning classes that can actually fire.

### S2 — `enricher -> main.resolve_country` is the one refactor that turns this graph into a hard cycle (verified), and `resolve_country` is already on the wrong side of the boundary

- **Where:** `main.py:50-74` (`resolve_country`) vs `enricher.py:79-94` (`infer_region`);
  `main.py:184` calls the former, `enricher.py:332-335` calls the latter
- **Breaks:** `main.resolve_country`'s entire docstring (`main.py:51-58`) is about
  `enricher.infer_region`: it exists because `country=None` disabled address-based region
  inference and made `normalize_phone` assume a country. Country resolution is therefore
  *conceptually* a property of region inference, but it physically lives in the
  orchestration module that imports `enricher` at `main.py:30`. The obvious refactor — move
  `resolve_country` next to `infer_region` so `enricher.enrich` can call it itself instead of
  requiring `main.main` to pre-resolve country for every row — creates a hard cycle. Verified:
  ```
  leadminer/pipeline.py:  from .enricher import infer_region
  leadminer/enricher.py:   from .pipeline import resolve_country
  ImportError: cannot import name 'resolve_country' from partially initialized module
  'leadminer.pipeline' (most likely due to a circular import)
  ```
- **Three refactors I expected to cycle, and verified do not** (reported because a wrong
  warning here costs someone a day):
  1. Populating `scrapers/__init__.py` with the registry **and** typing `enricher` with
     `BusinessRecord` from `scrapers.base` — **safe**. Both edges are submodule imports,
     which resolve fine while the parent package is mid-initialisation. `import
     leadminer.scrapers`, `import main` and `import cli` all succeed in a replica.
  2. Hoisting `cli.py:249`'s lazy imports to module scope **without** `main` needing
     anything from `cli` — **safe**, no cycle (nothing points back).
  3. `leadminer/__init__.py` re-exporting `main` while `cli` imports the pipeline — **safe**.
- **The one that does cycle** is the `DATA_DIR` DRY-up. `DATA_DIR` is duplicated at
  `cli.py:26` and `main.py:41` (sibling `200-fn-cli-py-main-ops.md` S3 notes the
  duplication). The tempting fix is `from cli import DATA_DIR` in `main.py`; combined with
  "also clean up those lazy imports", verified:
  ```
  leadminer/cli.py:       from .pipeline import run      # hoisted from cli.py:35
  leadminer/pipeline.py:  from .cli import DATA_DIR
  ImportError: cannot import name 'DATA_DIR' from partially initialized module
  'leadminer.cli' (most likely due to a circular import)
  ```
  Keeping the import lazy survives it (verified: `main() -> data`). **The lazy imports at
  `cli.py:35,46-49,249-252` are the only thing preventing this cycle, and nothing in the
  code says so.** One comment line is the entire mitigation.
- **Fix:** move `resolve_country` into `leadminer/geo.py` (or `enricher`) with
  `infer_region`, and move `DATA_DIR` into `leadminer/config.py`. Both duplications and the
  cycle risk disappear together, and `enricher.enrich` stops depending on its caller to have
  done the right thing first.

### S3 — Import hygiene: one dead import in `cli.py`, three phantom imports in `enricher.py`, one misplaced, and two that look annotation-only but are not

Verified with `ruff check --no-cache .` (the repo's own config, `pyproject.toml:39-58`):
**42 errors, 28 auto-fixable.** The relevant ones:

- `cli.py:46` — `from scrapers.base import BaseScraper  # noqa: F401  (import check)`.
  `BaseScraper` is **never referenced** in `cli.py` (grep: `cli.py:46` is the only hit). It
  is imported for nothing, and it is the only import in the file that converts a missing
  dependency into a traceback instead of the CLI's own error path. Verified with `scrapers/`
  absent from the import path:
  ```
  File ".../cli.py", line 46, in cmd_scrape
      from scrapers.base import BaseScraper  # noqa: F401  (import check)
  ModuleNotFoundError: No module named 'scrapers'
  ```
  The message at `cli.py:57` ("unknown source ...; choose from [...]") is never reached,
  because the failure happens three lines earlier. The `noqa` also hides the one linter that
  would have flagged it.
- `enricher.py:2` — `import requests`, **unused** (`F401`). It became dead when the session
  moved to `httpclient` (commit `607e729`). Yet `tests/test_lead_signal.py:23-25` still
  carries a 40-line stub whose stated justification is that exact import: *"enricher imports
  requests at module scope. Stub it if absent so these tests run in a bare checkout."* The
  test harness is load-bearing for a dead import.
- `enricher.py:4` — `from urllib.parse import urljoin, urlparse`, both **unused** (`F401`),
  left behind by the SSRF fix.
- `enricher.py:128` — `from httpclient import DEFAULT_MAX_BODY_BYTES, get_session` sits
  mid-file (`E402`), wedged between the `_REGION_MAP` tables and the fetch regexes, with a
  four-line thread-safety comment above it. There is **no functional reason for the
  placement**: `httpclient` imports no project module (verified — nothing in the graph points
  at it), so there is no cycle to dodge. It reads as deliberate and is not.
- `cli.py:206` — function-local `import os` for a stdlib module that `main.py:19` imports at
  module scope, inside the one command whose author demonstrably cared about import timing
  (`cli.py:228-230`).
- **Annotation-only imports: there are none that are free.** `cli.py:18` and
  `httpclient.py:32` are the only two modules with `from __future__ import annotations`, so
  every other annotation in the repo is **evaluated at runtime**. `typing.Iterator`
  (`scrapers/base.py:2`, `osm.py:2`, `wikidata.py:2`, `google_places.py:22`) is used only in
  `-> Iterator[BusinessRecord]`, yet `Iterator[BusinessRecord]` is constructed on every
  `def scrape` at import time; ruff flags all four as `UP035` (should be
  `collections.abc.Iterator`). `pitch_recommender.py:22`'s `typing.Mapping` is the same.
  `scrapers/base.py:2`'s `TypedDict` is *not* annotation-only — it is a base class, so it is a
  genuine runtime import by construction.
- **Fix:** delete `cli.py:46`, `enricher.py:2` and `enricher.py:4`; move
  `enricher.py:128` to line 5 with a comment saying why `httpclient` owns sessions; move
  `cli.py:206`'s `os` to module scope; `ruff check --fix` for the `UP035`/`I001` set.

### S3 — Two scrapers create loggers they never use, and the one real logger is configured by nobody

- **Where:** `scrapers/osm.py:1` + `:7`, `scrapers/wikidata.py:1` + `:7` (`log =
  logging.getLogger(__name__)`), `httpclient.py:35` + `:45` with the only two calls at
  `httpclient.py:178` and `:204`
- **Breaks:** grep for `log\.` across the repo returns exactly two hits, both in
  `httpclient.py`. `osm.py` and `wikidata.py` import `logging`, bind `log`, and then
  `print` everything (`osm.py:33,51,59`; `wikidata.py:68,80,88`). So the two modules
  doing single-threaded HTTP hold unused loggers, while the module that owns all the retry
  telemetry logs at `DEBUG` to a logger that nothing in the project ever configures — no
  `logging.basicConfig` anywhere, and `cli.py` exposes no `--log-level`. The result: the
  retry-and-jitter layer added in `607e729` emits nothing in any run, and every
  operator-visible surface in the tool is `print` to stdout, which cannot be routed,
  filtered or captured per-run.
- **Trigger:** a 429 storm during `leadminer run`. `fetch_with_retry` logs each attempt at
  `httpclient.py:204`; the operator sees only `[OSM] FAILED after 3 attempts: ...`
  (`osm.py:51-55`) with no per-attempt timing. Under `leadminer run`, stdout is not
  separable from the summary block (`main.py:219-242`), so the diagnostic and the data are
  interleaved in one stream.
- **Fix:** delete the two unused `log = ...` bindings; add one `logging.basicConfig` in
  `cli.main` gated on a `--log-level` flag; convert the `[OSM]`/`[Wikidata]`/`[Google]`
  `print`s to that logger. Sibling `200-fn-cli-py-main-ops.md` S2 flags the missing
  `basicConfig`; what is new here is that two of the three loggers are dead and the live one
  sits on the path with the most to say.

### S3 — The lint and test gates that would have caught all of the above are wired to nothing

- **Where:** `pyproject.toml:39-58` (ruff, 15 rule families selected), `:64-68` (mypy
  `strict = true`), `:75-78` (pytest), vs `.github/workflows/scrape.yml` — whose steps are
  checkout, setup-python, `uv pip install -r requirements.txt`, rclone setup, download
  master, **`python main.py`**, row-count gate, upload, upload artifact. No ruff, no mypy,
  no pytest.
- **Breaks:** three consequences, all load-bearing for this report. (1) 42 ruff errors are
  invisible; five of them are import hygiene (`F401` ×3, `E402`, `I001` ×5) and one is
  `UP035` ×5. (2) mypy `strict` has never run, so `cmd_run`'s `-> int` returning
  `main.main()`'s `-> None` (`cli.py:37` vs `main.py:148`) is an unchecked type error;
  `build_parser`'s duck-typed `args.func`/`args.file`/`args.out` protocol is unchecked too.
  (3) **CI runs `python main.py`, never `leadminer`.** So the function this report targets
  has zero CI coverage even in principle, and `tests/test_lead_signal.py` covers only
  `cmd_stats` and `cmd_validate` (`:215,232,246,259`) — nothing for `main()`,
  `build_parser()`, `cmd_run`, `cmd_scrape`, `cmd_doctor` or `cmd_score`.
- **Trigger:** any of the S1s above reaching a scheduled run. `leadminer run` is currently
  the most expensive entry point in the repo and the least exercised.
- **Fix:** add `ruff check`, `mypy` and `python -m unittest discover -s tests` steps to the
  workflow before the scrape step, and change the scrape step to `leadminer run`. That last
  change is what makes `pyproject.toml:33` real rather than decorative.

---

## Not a bug, but worth knowing

- **The graph is a DAG today, and the obvious src-layout moves keep it that way.** I tested
  four plausible refactor shapes; three are safe (see S2). Report 020 predicted that
  `scrapers/google_places.py:26` "dies" under a src layout — true, but as an *unqualified
  import* (`ModuleNotFoundError: No module named 'enricher'`), not as a cycle. The real
  circular-import hazard in this repo is `resolve_country`, not `infer_region`.
- **`cli.main`'s zero-project-module closure is a real design win and should be protected.**
  `import cli` costs 5 stdlib modules; `stats` and `validate` pull in nothing else at all.
  The single most valuable test in the suite would be the property that
  `200-fn-cli-py-build_parser-tests.md` T8 gestures at: assert that `--help`, `stats` and
  `validate` never import `requests`, `scrapers` or `main`.
- **`cmd_doctor`'s dependency hygiene is already covered, in depth, by `246-x.md`** — the
  `httpclient` bypass, the split read of `SCRAPER_EMAIL`, and the `DATA_DIR` divergence are
  all argued there with the same measurements. Deliberately not repeated.
- **Already filed by siblings on this exact target, not repeated here:** the constant exit 0
  (`200-fn-cli-py-main-contract.md` S1, `-ops.md` S1, `-correctness.md` S1);
  `score`'s schema-narrowing in-place rewrite (four reports); CWD-relative defaults
  (`-contract.md` S2, `-edge.md` S2); the `website_live` string-literal parse in
  `cmd_stats` (`200-fn-cli-py-build_parser-contract.md` S2); `validate` passing rows it never
  checked (`-edge.md` S1); `--source` choices vs the `cmd_scrape` registry
  (`-correctness.md` S3-2); `cmd_run` ignoring `args` (`-ops.md` S3).
- **`cli.py:281`'s `description=__doc__` means `--help` disappears under `python -OO`**
  (docstrings are stripped), which is the only dependency the CLI has on its own source
  text. Cosmetic, but it is a dependency.
- **`__pycache__/` at the repo root contains `cli.cpython-314.pyc` and `main.cpython-314.pyc`**,
  confirming every module is imported as a top-level sibling from the checkout root. That is
  the single condition that makes all of the above work today and is exactly what a
  `pip install` removes.

---

## Recommended order of work

1. **Create `leadminer/config.py` and `leadminer/storage.py`; move `DATA_DIR`,
   `resolve_country`, `FIELDS`, `load_master` and `write_csv` into them.** This is the
   prerequisite for everything else: it ends the duplicated `DATA_DIR`, removes the
   `enricher -> main` cycle risk, and gives `cmd_score` a schema-owning dependency that
   does not drag in `requests`. Add a comment at `cli.py:35` recording that the lazy import
   is load-bearing.
2. **Do the src-layout move in the same change**: `leadminer/` package,
   `pyproject.toml:33` -> `leadminer.cli:main`, wheel `packages`, all absolute imports
   qualified. Do not do this piecemeal — half of it is worse than none, because the failure
   mode is a `ModuleNotFoundError` from a console script rather than from a source checkout.
3. **Make `write_csv` preserve unknown columns and assert the header on read.** The S1 that
   four sibling reports filed is unfixable at the `cli.py` layer; it is fixable in one place
   in `storage.py`.
4. **Point `enricher._fetch_website` at `fetch_with_retry`** (S2). Smallest change with the
   largest reliability win, and it removes `requests` from `enricher`'s reachable set
   entirely.
5. **Delete the dead imports** (`cli.py:46`, `enricher.py:2`, `enricher.py:4`,
   `enricher.py:327-328`), move `enricher.py:128` to the top of the file, and run
   `ruff check --fix`. Then wire `ruff` + `mypy` + the unittest suite into CI and switch the
   scrape step from `python main.py` to `leadminer run`.
6. **Add the registry to `leadminer/scrapers/__init__.py`** and resolve it lazily, so
   `--source` stops needing three hand-synced lists and `scrape --source osm` stops
   importing Google Places.
7. **Only then** touch the exit-code and `or 0` contract that the other four reports on this
   target cover — it needs `main.main()` to return something, which is easier once
   `pipeline.py` is a library module with a `run(...) -> RunResult` signature.
