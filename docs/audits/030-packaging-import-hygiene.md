# 030 — Packaging and import hygiene

## Verdict

The repository has no build metadata, and its flat-tree imports only work because the checkout root is on `sys.path`; moving the code into a `src` package without rewriting imports will break both the installed application and the scraper module. Adopt one importable package, `leadminer`, use qualified package imports, and install it through the `pyproject.toml` below. From then on the canonical commands are `leadminer` or `python -m leadminer.main`—not `python main.py` or `python -m scrapers...`.

## Findings

### S1 — The flat imports cannot resolve after a `src`-layout install
- **Where:** `main.py:24-30`; `scrapers/google_places.py:26-27`; `scrapers/osm.py:6`; `scrapers/wikidata.py:6`; `requirements.txt:1-3`.
- **Breaks:** There is no `pyproject.toml` or package metadata, so `pip install .` has no project to build. In a `src/leadminer/` package, `main.py`'s unqualified `scrapers`, `dedup`, `enricher`, and `pitch_recommender` imports search for top-level modules, not `leadminer.*`. More directly, `google_places.py` imports `from enricher import infer_region`; importing it as `leadminer.scrapers.google_places` raises `ModuleNotFoundError: No module named 'enricher'`. The sibling `.base` imports in OSM, Wikidata, and Google Places are already package-relative and remain valid. `scrapers/whitelist.py` has only a relative-import example in its docstring, not an actual cross-module import.
- **Trigger:** Install the package in a clean environment and import `leadminer.scrapers.google_places`, or run `python -m leadminer.main` after moving the current modules under `src/leadminer/`; the first unresolved top-level project import stops module loading. From the repository root, `python -m scrapers.google_places` can appear to work today because the checkout root is on `sys.path`, but it is not an installed-package invocation and the module has no `__main__` entry point, so it does not perform a scrape.
- **Fix:** Move application modules under `src/leadminer/`, add package `__init__.py` files, and use qualified imports: in `main.py`, change lines 24–30 to `from leadminer.scrapers.osm import OSMScraper`, `from leadminer.scrapers.wikidata import WikidataScraper`, `from leadminer.scrapers.google_places import GooglePlacesScraper`, `from leadminer.scrapers.whitelist import is_business_category, industry_priority`, `from leadminer.dedup import dedup, normalize_phone`, `from leadminer.enricher import enrich, lead_score as _lead_score`, and `from leadminer.pitch_recommender import recommend_service`; in `scrapers/google_places.py:26`, change to `from leadminer.enricher import infer_region`. Keep all three scraper imports of `.base` relative. Run `python -m leadminer.scrapers.google_places` for an import/module smoke check; add an explicit `__main__` block only if a standalone scraper command is intended.

### S2 — Current dependency pins include unused packages and omit project tooling
- **Where:** `requirements.txt:1-3`; imports across `main.py`, `enricher.py`, and `scrapers/*.py`.
- **Breaks:** `requests` is the only third-party package imported by the application. `beautifulsoup4` and `lxml` are not imported anywhere, yet the three exact pins in `requirements.txt` are the only dependency declaration. There are no declared test, lint, or typing tools and no pytest configuration (the brief records zero tests).
- **Trigger:** A fresh install from the only dependency file installs two unused runtime packages, while a contributor has no declared project command or dev extra for the requested checks.
- **Fix:** Declare only `requests` as a runtime dependency, with a compatible range; put pytest, Ruff, mypy, and requests stubs in a `dev` extra. Add tests under `tests/` and configure pytest to find the `src` package. Keep or remove the legacy requirements file as a separately coordinated migration step; do not maintain conflicting dependency declarations.

## Recommended package layout

```text
.
├── pyproject.toml
├── README.md
├── src/
│   └── leadminer/
│       ├── __init__.py
│       ├── main.py
│       ├── dedup.py
│       ├── enricher.py
│       ├── pitch_recommender.py
│       └── scrapers/
│           ├── __init__.py
│           ├── base.py
│           ├── google_places.py
│           ├── osm.py
│           ├── whitelist.py
│           └── wikidata.py
├── tests/
│   └── ...
├── scripts/
│   └── audit_status.py
├── docs/
└── data/                 # runtime output; keep outside the import package
```

`src/leadminer/__init__.py` may be empty. `scripts/audit_status.py` uses only stdlib imports and resolves its audit directory relative to `__file__` (`scripts/audit_status.py:7-11`), so it can stay outside the installable package unchanged. Preserve `data/` at the project/work directory level: `main.py:40` currently writes to a path relative to the process working directory; packaging does not make that path project-relative.

## Proposed `pyproject.toml`

This is the complete initial file. The `0.1.0` version is a bootstrap version (the repository currently declares none); change it if a release/versioning policy already exists.

```toml
[build-system]
requires = ["setuptools>=69.2,<100"]
build-backend = "setuptools.build_meta"

[project]
name = "leadminer"
version = "0.1.0"
description = "Business lead scraper and enrichment pipeline for Lebanon and Saudi Arabia"
readme = "README.md"
requires-python = ">=3.12,<4.0"
dependencies = [
    "requests>=2.32.3,<3.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3,<9.0",
    "ruff>=0.9,<1.0",
    "mypy>=1.14,<2.0",
    "types-requests>=2.32,<3.0",
]

[project.scripts]
leadminer = "leadminer.main:main"

[tool.setuptools]
package-dir = {"" = "src"}

[tool.setuptools.packages.find]
where = ["src"]
include = ["leadminer*"]

[tool.ruff]
target-version = "py312"
line-length = 100
src = ["src"]

[tool.ruff.lint]
select = ["E", "F", "I"]
ignore = ["E501"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
addopts = "-ra"

[tool.mypy]
python_version = "3.12"
mypy_path = "src"
packages = ["leadminer"]
explicit_package_bases = true
check_untyped_defs = true
warn_unused_configs = true
warn_redundant_casts = true
warn_unused_ignores = true
no_implicit_optional = true
```

The console script calls the existing `main()` function (`main.py:93,183-184`); it does not change scraper behavior. `python -m leadminer.main` is also available once the imports and package layout are migrated. `python -m leadminer.scrapers.google_places` only imports the module unless a scraper-specific `__main__` is deliberately added.

## Recommended order of work

1. Create the `src/leadminer/` tree and move only application modules into it; retain docs, scripts, tests, and generated data outside the package.
2. Update the seven `main.py` imports and the `google_places.py` enricher import exactly as listed above; leave `.base` imports relative.
3. Add the proposed `pyproject.toml`, then validate a clean build/install, import the package and scraper module without invoking `main()` or any scraper, and run `ruff check .`, `mypy`, and `pytest`. Do not use `python -m leadminer.main` as a packaging smoke test: it starts the scrape pipeline and can make network requests.
4. Update README and CI install/run commands to use `pip install -e '.[dev]'` for development and `leadminer` (or `python -m leadminer.main`) for execution. Retire `requirements.txt` only when its CI/local consumers have been migrated.
