# 382 — `_fetch_website` dependency closure: wrong altitude, hidden config, and a latent import cycle

## Verdict

`_fetch_website` depends on exactly **one** intra-project module — `httpclient` — but it depends on
the *wrong thing from it*: `get_session()`, the lowest-level primitive, instead of
`fetch_with_retry()`, the module's published API and the only place in the codebase that can
distinguish "never reached the server" from "the server said no". That single choice is why every
failure mode collapses into an undiagnosable `UNKNOWN`, why a Cloudflare 403 is scored as a
confirmed-dead rebuild pitch, and why the function inherits a module with a configured logger and
emits nothing. Separately, the closure drags in `SCRAPER_EMAIL` — an env var documented as an
Overpass/Wikidata courtesy — and publishes the operator's email address to every third-party small
business the crawler touches.

There is **no import cycle today**, and `_fetch_website`'s closure is a two-node DAG
(`enricher → httpclient → stdlib`). The cycle is latent and is **one type annotation away**; its
safety today rests on `scrapers/__init__.py` being 0 bytes.

---

## Dependency map — transitive closure of `enricher.py:161`

Resolved by reading every reachable symbol. "Runtime" = reachable during a `_fetch_website` call.

| # | Symbol | Kind | Defined at | Reached from | Notes |
|---|---|---|---|---|---|
| 1 | `get_session()` | **intra-project fn** | `httpclient.py:61-71` | `enricher.py:128` → `:175` | Thread-local `requests.Session`. Only project dep. |
| 2 | `DEFAULT_MAX_BODY_BYTES` | **intra-project const** | `httpclient.py:54` | `enricher.py:128` → `:158` | Aliased to `_MAX_BODY_BYTES`. Policy constant living in a transport module. |
| 3 | `_local` | module global | `httpclient.py:58` | via #1 | `threading.local()`. Session lifetime == worker-thread lifetime. |
| 4 | `_user_agent()` | fn | `httpclient.py:74-85` | via #1 | Reads `os.environ["SCRAPER_EMAIL"]` at `:81`. |
| 5 | `os.environ` | stdlib | — | via #4 | Function-local import at `httpclient.py:79`. |
| 6 | `requests.Session()` | 3rd-party | `httpclient.py:65` | via #1 | Never `close()`d. |
| 7 | `threading` | stdlib | `httpclient.py:37` | via #3 | |
| 8 | `requests` | 3rd-party | `httpclient.py:43` | via #6 | Also **directly and unusedly** at `enricher.py:2`. |
| 9 | `UNKNOWN` / `DEAD` / `LIVE` | module globals | `enricher.py:154-156` | `:178`, `:184`, `:222` | Local module, not `httpclient`. Fine. |
| 10 | `_MAX_BODY_BYTES` | module global | `enricher.py:158` | `:188` | Pure alias of #2; reads like a knob, is not one. |
| 11 | `_EMAIL_RE` | module global | `enricher.py:130` | `:200` | Pre-compiled. Fine. |
| 12 | `_INSTAGRAM_RE` | module global | `enricher.py:131` | `:206` | |
| 13 | `_WHATSAPP_RE` | module global | `enricher.py:132-135` | `:212` | |
| 14 | `_LINKEDIN_RE` | module global | `enricher.py:136` | `:216` | |
| 15 | `_EMAIL_BLACKLIST` | module global | `enricher.py:138-141` | `:202` | |
| 16 | `_IG_BLACKLIST` | module global | `enricher.py:142` | `:208` | |
| 17 | `re` | stdlib | `enricher.py:1` | `:200-216` | |
| 18 | `r.raw.read(n, decode_content=True)` | **urllib3 internals** | `enricher.py:188` | direct | `requests.Response.raw` → `urllib3.HTTPResponse.read`. Untyped (see §S3-3). |
| 19 | `r.encoding` | **requests heuristic** | `enricher.py:189` | direct | `requests.utils.get_encoding_from_headers`; returns `ISO-8859-1` for `text/html` with no charset. |
| 20 | `r.close()` | 3rd-party | `enricher.py:195` | direct | Hand-rolled `try/except Exception: pass`. |

**Intra-project edges reachable from `_fetch_website`: exactly one.** `enricher.py:128 →
httpclient`. `httpclient.py` imports **nothing** from the project (`httpclient.py:34-43`: stdlib +
`requests` only).

### NOT in the closure, but in the same module's import surface

| Symbol | Defined at | Reachable from `_fetch_website`? |
|---|---|---|
| `requests` | `enricher.py:2` | **No** — unused module-wide (`ruff` F401). |
| `urljoin`, `urlparse` | `enricher.py:4` | **No** — unused module-wide (`ruff` F401). |
| `ThreadPoolExecutor`, `as_completed` | `enricher.py:3` | No — used by `check_websites` (`:237`, `:240`). |
| `urllib3` | `enricher.py:327` (function-local) | No — `enrich()` only. |

---

## Full intra-project import graph (verified by grep over every `.py`)

```
main.py:25-31         -> scrapers.osm, scrapers.wikidata, scrapers.google_places,
                         scrapers.whitelist, dedup, enricher, pitch_recommender
cli.py:35, 249-252    -> main, enricher, pitch_recommender, scrapers.*   (function-local)
scrapers/osm.py:4-5        -> httpclient, .base
scrapers/wikidata.py:4-5   -> httpclient, .base
scrapers/google_places.py:26-28 -> enricher, httpclient, .base
enricher.py:128        -> httpclient
dedup.py               -> (none)
pitch_recommender.py:22 -> (none)
scrapers/base.py:1-2   -> (none: abc, typing)
scrapers/whitelist.py  -> (none)
httpclient.py:34-43    -> (none: stdlib, requests)
```

**This is a DAG. No cycle exists today.** `_fetch_website`'s closure is
`enricher → httpclient → stdlib` — two nodes. Stating that plainly is worth more than inventing a
cycle; `docs/audits/020-type-contract-import-cycle.md:321-322` already reached the same conclusion.

---

## Findings

### S1 — `_fetch_website` imports `get_session()` instead of `fetch_with_retry()`, so the code cannot honour its own LIVE/DEAD/UNKNOWN contract

- **Where:** `enricher.py:128` (`from httpclient import DEFAULT_MAX_BODY_BYTES, get_session`);
  `enricher.py:175` (`r = get_session().get(url, timeout=8, allow_redirects=True, stream=True)`);
  `enricher.py:176-178` (`except Exception: return UNKNOWN, contacts`)
- **Breaks:** `httpclient.py:2` states the module exists as *"Shared HTTP client for every outbound
  request"*, and `httpclient.py:94-117` defines `FetchResult` for exactly the distinction
  `_fetch_website` needs: `status is None` **with** `error` set means "we never reached the server"
  (`httpclient.py:98-99`, populated at `httpclient.py:208-217`), whereas a populated `status` means
  the server answered. `_fetch_website` calls `.get()` directly and throws that away. Every
  transport failure — DNS, TLS, connect timeout, read timeout, connection reset, WAF block — becomes
  the same bare `UNKNOWN` with no exception type retained and **no log line** (see S2-2). Worse, the
  fallback is wrong in the one case that matters: a Cloudflare managed challenge comes back as
  **`HTTP 403`** with `cf-mitigated: challenge`, so `enricher.py:182` (`if status >= 400`) returns
  **`DEAD`**. That is precisely the inversion the docstring at `enricher.py:164-167` claims to
  prevent — *"a site that blocks our crawler is not a site that needs rebuilding"* — and it feeds
  `lead_score` at `enricher.py:303-304` which awards **+20** for it.
- **Trigger:** `SCRAPER_EMAIL` set (as in `.github/workflows/scrape.yml:68`) and any
  Cloudflare-fronted Lebanese business site returns
  `HTTP/1.1 403 Forbidden` + `cf-mitigated: challenge`. `_fetch_website` → `DEAD` →
  `enricher.py:250` sets `website_live = False` → `lead_score` `enricher.py:303`
  (`record.get("website") and live is False`) → `+20`, and the row is recommended a rebuild that
  does not need doing.
- **Fix:** `fetch_with_retry("GET", url, timeout=..., max_bytes=_MAX_BODY_BYTES)` and branch on
  `FetchResult.status is None` (→ `UNKNOWN`) vs `.status` (→ `LIVE`/`DEAD`); treat 403 with
  `cf-mitigated` or `server: cloudflare` as `UNKNOWN`; `log.warning` the `.error` string.

### S2 — The closure silently publishes `SCRAPER_EMAIL` to every third-party business website

- **Where:** `enricher.py:175` → `httpclient.py:61-71` (`get_session`) → `httpclient.py:74-85`
  (`_user_agent`) → `httpclient.py:81` (`os.environ.get("SCRAPER_EMAIL", "")`)
- **Breaks:** `_user_agent()`'s own comment (`httpclient.py:75-77`) and `README.md:88` both scope
  this address to *Overpass and Wikidata*, which request a contact UA. But the UA is baked once
  into the shared Session header dict at `httpclient.py:66-69`, and that same Session factory is
  shared by `scrapers/osm.py:4`, `scrapers/wikidata.py:4`, `scrapers/google_places.py:27` **and**
  `enricher.py:128`. There is no per-caller override and `enricher.py:175` passes no `headers=`. So
  every HTTP request to every scraped business site carries
  `User-Agent: leadminer/0.2 (+https://github.com/voxire/leadminer) (ops@…)`, writing the
  operator's address into the access logs of thousands of unrelated third-party hosts and every
  WAF/CDN vendor in between. `_fetch_website` neither knows this nor can suppress it. This is the
  clearest *surprising transitive dependency* in the closure.
- **Trigger:** `SCRAPER_EMAIL=ops@voxire.com python main.py`, then fetch
  `https://beirutbakery.example` → that host's access log records
  `User-Agent: leadminer/0.2 (+https://github.com/voxire/leadminer) (ops@voxire.com)`.
- **Fix:** make `get_session(headers_overrides: dict | None = None)` and pass
  `headers={"User-Agent": "leadminer/0.2 (+https://github.com/voxire/leadminer)"}` at
  `enricher.py:175`, so the contact address reaches only the APIs that asked for it.

### S2 — Two disagreeing dependency manifests; CI installs the one that is not the package metadata

- **Where:** `requirements.txt:1-3` vs `pyproject.toml:16-18`; installed by
  `.github/workflows/scrape.yml:39` (`uv pip install --system -r requirements.txt`)
- **Breaks:** nothing ever installs the project, so `pyproject.toml`'s `[project.scripts]`
  (`:33`), `[project.optional-dependencies]` (`:21-30`) and `[project.dependencies]` (`:16-18`)
  are **decorative**. The effective dependency contract is `requirements.txt`. And 2 of its 3
  entries — `beautifulsoup4==4.12.3` and `lxml==5.2.2` — are **imported nowhere**: grep for
  `bs4|BeautifulSoup|lxml` across every `.py` file returns zero hits outside `docs/`. So two thirds
  of the installed runtime dependency surface is dead weight on every weekly run. Meanwhile
  `urllib3` — which `enricher.py:327` imports **directly** — is declared in neither file; it
  resolves only through `requests==2.32.3`'s own transitive pin. `pyproject.toml:70-73` even adds
  `urllib3.*` to the mypy `ignore_missing_imports` override list, which proves the authors already
  treat it as a direct import. And nothing runs `ruff` or `mypy` in CI (grep for
  `ruff|mypy|pytest` under `.github/` returns nothing).
- **Trigger:** every CI run. `uv pip install --system -r requirements.txt` installs
  beautifulsoup4 (+ `soupsieve`) and the `lxml` C extension for code that imports neither. Then
  narrow `requests`' urllib3 constraint (or land urllib3 3.x) and `enricher.enrich()` raises
  `ModuleNotFoundError: No module named 'urllib3'` from `enricher.py:327`.
- **Fix:** delete `beautifulsoup4` and `lxml` from `requirements.txt`; add `urllib3` explicitly;
  either generate `requirements.txt` from `pyproject.toml` or delete it in favour of
  `pip install -e ".[dev]"`; add `ruff check` + `mypy` to CI.

### S2 — `enricher.py`'s only project dependency is declared mid-file at line 128, flagged `E402` by the project's own linter, and excluded from the wheel by `pyproject.toml:37`

- **Where:** `enricher.py:124-128` (the import sits below a 4-line explanatory comment, 128 lines
  after the real import block); `pyproject.toml:35-37`
- **Breaks:** `ruff check enricher.py` under the project's own config
  (`pyproject.toml:43-55` selects `E`, `F`, `I`, `SIM`, …) reports **8 errors**, 4 auto-fixable:
  ```
  I001  Import block is un-sorted or un-formatted
  F401  `requests` imported but unused                 enricher.py:2
  F401  `urllib.parse.urljoin` imported but unused     enricher.py:4
  F401  `urllib.parse.urlparse` imported but unused    enricher.py:4
  E402  Module level import not at top of file         enricher.py:128
  SIM105 / SIM113 / RUF003
  ```
  `E402` on line 128 is precisely my target's dependency edge, and no CI step runs ruff, so it
  drifts silently. Separately, `pyproject.toml:35-37`:
  ```toml
  [tool.hatch.build.targets.wheel]
  # The package imports a top-level sibling module, so both must ship.
  include = ["scrapers/"]
  ```
  The comment states the requirement and the setting violates it: an explicit `include` replaces
  hatchling's default file selection, so the wheel would contain `scrapers/` **only** —
  not `httpclient.py` (which `_fetch_website` cannot run without), not `enricher.py`,
  not `dedup.py`, not `main.py`, not `cli.py`. `[project.scripts] leadminer = "cli:main"`
  (`pyproject.toml:33`) then points at a module that was never shipped.
  *(I could not build to confirm the wheel contents: `hatchling` is not installed here and the
  brief forbids installing packages. The `include` directive itself is quoted verbatim and is
  unambiguous.)*
- **Trigger:** `pip install . && leadminer` → the console script imports `cli`, which is absent from
  the wheel. Or, under the src layout this project is heading for, `python -c "import enricher"`
  from `.../src` → `enricher.py:128` `ModuleNotFoundError: No module named 'httpclient'`, because
  an absolute top-level import resolves against `sys.path`, never against the package directory.
- **Fix:** move the import to line 1-5 (kills `E402`), then move the tree to `src/leadminer/` and
  replace `pyproject.toml:35-37` with `packages = ["leadminer"]`.
- **New information vs `020`:** `docs/audits/020-type-contract-import-cycle.md:321-322` enumerates
  `enricher`'s imports as *"(only `re`, `requests`, `concurrent.futures`, `urllib.parse`)"*. That
  snapshot predates `enricher.py:128` (file mtime 19:20 vs audit 14:41). The absolute
  `from httpclient import` at line 128 is a **fourth** layout-dependent import the existing audit
  does not cover.

### S3 — `urllib3` at `enricher.py:327` suppresses a warning that can no longer fire, and is an undeclared direct dependency

- **Where:** `enricher.py:327-328` (`import urllib3` / `urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)`);
  contrast the comment at `enricher.py:170-174` recording that TLS verification moved from
  `verify=False` to `verify=True`
- **Breaks:** `InsecureRequestWarning` is raised by urllib3 only when certificate verification is
  disabled. `_fetch_website` at `enricher.py:175` passes no `verify=`, so requests' default
  `verify=True` applies and the warning is unreachable. The suppression is dead code that has
  never been removed, and it is an undeclared direct dependency (see S2 above). It is also placed in
  a **function-local** import inside `enrich()`, which means a missing `urllib3` surfaces as a
  `ModuleNotFoundError` *after* scraping and dedup have completed — it kills the enrichment stage
  and the run's entire output rather than failing fast at startup.
- **Trigger:** `SCRAPER_EMAIL` unset and `requests` swapped for a build whose urllib3 constraint
  excludes the installed version → `enrich()` at `enricher.py:327` raises
  `ModuleNotFoundError`, after the multi-hour scrape has already completed and been discarded.
- **Fix:** delete `enricher.py:327-328`. If the suppression is ever wanted again it belongs in
  `httpclient.py` next to the session factory, and `urllib3` must be declared.

### S3 — The import header over the target function is 3/4 dead, and one dead line already cost 41 lines of global test monkeypatching

- **Where:** `enricher.py:2` (`import requests`), `enricher.py:4` (`from urllib.parse import urljoin, urlparse`)
- **Breaks:** ruff confirms all three names are `F401` — imported and never referenced anywhere in
  `enricher.py`. `_fetch_website` reaches `requests` only *indirectly*, through `httpclient.py:43`
  and the `Response` object it is handed; it never names the module. The concrete cost is already
  paid downstream: `tests/test_lead_signal.py:23-63` is a 41-line `_stub_requests()` whose docstring
  (`:24`) says outright *"enricher imports requests at module scope. Stub it if absent"* — it
  fabricates `sys.modules["requests"]` (`:50`) and `sys.modules["urllib3"]` (`:60`) with a
  `Session.get` that raises `RuntimeError("no network in tests")` (`:37-38`). Those stubs are
  installed **process-globally and never removed**, before `main` is imported at `:67` — so
  `scrapers/google_places.py:24 import requests` also binds the fake, and that stub `Session`
  (`:33-35`) defines only `self.headers`: no `cookies`, `request`, `mount`, or `close`. Remove one
  dead line and 41 lines of scaffolding plus a whole class of latent test lie disappear.
- **Also:** `urlparse` is the one name in that header the function arguably *should* depend on — it
  is what a scheme/host allow-list or private-IP guard would be built on. Imported and unused reads
  as a check that was planned and never wired in. Cross-reference
  `docs/audits/008-enricher-ssrf-security.md` and `026-enricher-ssrf-security.md`.
- **Trigger:** `python3 -m unittest discover -s tests` → 28 tests pass, and the stub is silently in
  effect for all of them. Any future test that constructs a `GooglePlacesScraper` gets a
  `Session` whose `.get` raises `RuntimeError` and which has no `.cookies` — an `AttributeError`
  from inside the stub, not the intended clean failure.
- **Fix:** delete `enricher.py:2` and `enricher.py:4`; delete `_stub_requests()` and the
  `# noqa: E402` chain at `tests/test_lead_signal.py:65-68`.

### S3 — Hardcoded transport policy sitting beside the house defaults it silently ignores

- **Where:** `enricher.py:175` (`timeout=8`), `enricher.py:158`
  (`_MAX_BODY_BYTES = DEFAULT_MAX_BODY_BYTES`), `httpclient.py:47-54`, `enricher.py:225`
  (`check_websites(records, workers: int = 40)`)
- **Breaks:** `httpclient` publishes exactly five tuning knobs as its configuration surface
  (`httpclient.py:47-54`: `DEFAULT_TIMEOUT`, `DEFAULT_RETRIES`, `DEFAULT_BACKOFF`, `MAX_BACKOFF`,
  `DEFAULT_MAX_BODY_BYTES`). `_fetch_website` honours **one** of them — the body cap, via the alias
  at `:158` — and hardcodes the other as a bare literal `8`, which is 53% of the house default
  `DEFAULT_TIMEOUT = 15`. Raising `DEFAULT_TIMEOUT` has zero effect on the 40-worker website sweep,
  which is where nearly all wall-clock time is spent. `check_websites` exposes `workers` as a
  parameter (`:225`) and exposes neither timeout nor body cap, so an operator who needs to be
  gentler on small-business servers, or more aggressive against the 300-minute CI budget
  (`.github/workflows/scrape.yml:25`), has no seam at all. Separately, `enricher.py:158` is a pure
  alias: `_MAX_BODY_BYTES` is never anything but `DEFAULT_MAX_BODY_BYTES`. It reads like a local
  knob and is not overridable.
- **Trigger:** `DEFAULT_TIMEOUT = 30` in `httpclient.py:47` to tolerate slow Gulf-region hosts; run
  against 4,000 sites at `enricher.py:175` `timeout=8`. The sweep still cuts off at 8s and files
  every slow site as `UNKNOWN`.
- **Fix:** add `timeout: float = DEFAULT_TIMEOUT` to `check_websites`, thread it into
  `_fetch_website(url, timeout)`, and delete the `_MAX_BODY_BYTES` alias.

---

## Imports used only for typing / only for logging

The lens asks specifically. Honest answers, both partly "none":

**Type-annotation-only imports: none, because the module annotates nothing.**
`enricher.py` is the **only** module in the project without `from __future__ import annotations`
(present at `httpclient.py:32` and `cli.py:18`), and every one of its functions uses bare builtin
generics — `enricher.py:161` `-> tuple[str, dict]`, `:225` `-> list[dict]`, `:283` `-> int`,
`:326` `-> list[dict]` — rather than the `BusinessRecord` TypedDict that `scrapers/base.py:5`
already defines for exactly these rows. Under the project's own `mypy strict = true`
(`pyproject.toml:66`) this module would fail wholesale; mypy is not installed and not run in CI.

The dependency consequence is specific rather than cosmetic. Because `r` is untyped, the three
third-party internals the function's correctness rests on are all unchecked:

- `r.raw.read(_MAX_BODY_BYTES, decode_content=True)` — `enricher.py:188`
- `r.encoding` — `enricher.py:189`
- `r.close()` — `enricher.py:195`

`requests.Response.raw` is `None` until a response has been received in requests' own
implementation. The **only** thing making it non-`None` here is the `stream=True` argument on
`enricher.py:175` — an invariant asserted by an argument six lines above the site that relies on
it, recorded in neither the type system nor any test. `httpclient.fetch_with_retry` encapsulates
exactly this correctly (`with resp:` then `resp.raw.read(max_bytes)`, `httpclient.py:181-183`);
`_fetch_website` re-derives it by hand at `enricher.py:180-197`.

**Logging-only imports in the closure: exactly one, and this path never uses it.**
`httpclient.py:35` (`import logging`) and `httpclient.py:45` (`log = logging.getLogger(__name__)`)
exist for `fetch_with_retry` only (`httpclient.py:178-179`, `:204-205`). `_fetch_website` calls
`get_session()`, not `fetch_with_retry()` — so it inherits a module with a fully configured logger
and produces **zero** log output. `enricher.py` has no logger at all (`grep -c "logging\|log\."
enricher.py` → `0`) and uses bare `print` at `:235`, `:262`, `:271`, `:272`.

The result: the failure mode the code's own comments identify as the most dangerous one —
*"DNS failure, TLS failure, timeout, reset, blocked. We learned nothing."* (`enricher.py:177`) —
is the one failure mode with **no telemetry whatsoever**. The only signal that UNKNOWN sites exist
is a single aggregate `print` at `enricher.py:271` at the *end* of a 5-hour run. An operator
cannot distinguish 50 unreachable hosts from 5,000, and cannot tell whether a spike is DNS, TLS,
or a provider-side block. `enricher.py:193-197` hand-rolls `try: r.close() except Exception: pass`
— which ruff's own `SIM105`, in the selected `SIM` ruleset (`pyproject.toml:51`), flags —
instead of using `contextlib.suppress` or the `with resp:` that `httpclient.py:181` uses.

---

## Circular imports under a src layout

**No cycle exists today** (see graph above). `_fetch_website`'s closure is `enricher → httpclient`,
and `httpclient` has no project imports. The interesting part is the *margin* on which that
survives.

**Break #1, today:** `enricher.py:128`'s absolute `from httpclient import ...`.
```
flat repo root (today):  sys.path[0] = <repo root>      -> 'httpclient'  OK
src/leadminer/…        : sys.path[0] = …/src            -> 'httpclient'  ModuleNotFoundError
installed wheel        : sys.path[0] = site-packages    -> not shipped at all (pyproject.toml:37)
```
A package's own directory is never placed on `sys.path`, so a bare top-level name is unreachable
from inside a package. The `from .base import` siblings in `scrapers/osm.py:5`,
`scrapers/wikidata.py:5` and `scrapers/google_places.py:28` are already relative and survive;
`enricher.py:128` is not.

**Break #2, latent, and one annotation away.** `check_websites`/`enrich` take `list[dict]` and the
natural next step is `list[BusinessRecord]` — and `BusinessRecord` lives in `scrapers/base.py:5`.
Under `src/leadminer/`:

```
leadminer.scrapers.google_places     google_places.py:26  `from enricher import infer_region`
                                                   -> must become `from ..enricher import …`
  -> leadminer.enricher              enricher.py:128      -> `from .httpclient import …`
      -> leadminer.scrapers.base     for BusinessRecord
```

This survives **only** because `scrapers/__init__.py` is **0 bytes** and `scrapers/base.py:1-2`
imports nothing but `abc`/`typing`. It becomes a **hard** `ImportError`/`AttributeError` the moment
either changes:

- someone adds a convenience re-export to `scrapers/__init__.py` — `from .google_places import
  GooglePlacesScraper` is the obvious candidate, and `main.py:25-27` currently does precisely that
  by hand; or
- `enricher.py` writes `from .scrapers import BusinessRecord` (attribute lookup on a
  partially-initialised package) instead of `from .scrapers.base import BusinessRecord`
  (submodule import, which CPython's `sys.modules` fallback tolerates).

So the cycle is not hypothetical — it is latent, and **the exact spelling of one future import
decides whether it is a soft cycle or a hard one.** The 0-byte `__init__.py` is load-bearing.

**Fix:** hoist `BusinessRecord` to a top-level `contracts.py` that nothing reaches *down* into —
`contracts ← scrapers`, `contracts ← enricher`, `contracts ← dedup`, `contracts ← main`. This
matches the recommendation at `020:335-338`; I am re-confirming it from `_fetch_website`'s side and
adding the mechanism detail (the 0-byte `__init__.py`, and the submodule-vs-attribute distinction)
that `020` does not have.

---

## Not a bug, but worth knowing

- **The thread-local Session buys `_fetch_website` nothing, and costs a little.** `httpclient.py:56-71`
  builds a `threading.local()` Session per worker thread to solve a real problem
  (`httpclient.py:7-10` — `requests.Session` is documented not thread-safe), and that fix is
  correct. But urllib3's `PoolManager` caches pools in a `RecentlyUsedContainer(num_pools=10)`
  LRU keyed by `(scheme, host, port)`, evicting and `close()`ing on overflow. The website sweep
  fetches *one distinct business domain per record* — thousands of unique hosts — so the LRU hit
  rate is ~0: every fetch evicts a pool and opens a fresh TCP + TLS connection. The pooling
  machinery provides no reuse here, while the never-`close()`d Session, its `RequestsCookieJar`
  (which accumulates across every site that worker touches), and two `PoolManager`s per thread
  remain for the thread's lifetime. Note the jar is domain-keyed, so there is **no cross-site
  cookie disclosure** — cookies are not sent to a domain they weren't set for. It is bounded
  memory, not a leak, and the threads do die at `enricher.py:237` shutdown. Worth knowing so nobody
  spends effort hardening something that is already bounded.

- **The mid-file import at `enricher.py:128` is at least load-bearing in one respect:** it sits
  directly under the comment at `enricher.py:124-127` that documents *why* it exists ("The previous
  module-level `_SESSION` was shared by 40 workers"). If the import is moved to the top for the
  `E402` fix, move that comment with it or the rationale is lost.

- **`r.encoding` (`enricher.py:189`) is a requests heuristic, not a fact.** For
  `Content-Type: text/html` with no `charset`, `requests.utils.get_encoding_from_headers` returns
  `ISO-8859-1` (RFC 2616 behaviour), so `raw.decode(r.encoding or "utf-8", errors="replace")`
  decodes UTF-8 Arabic content as Latin-1 and produces mojibake. `r.text` would hit the same
  heuristic. The fix is not to read `r.encoding` but to sniff the `<meta charset>` in the first
  bytes — a regex-precision question, not a dependency one, but it originates in reaching for
  `requests`' implicit encoding rather than an explicit one.

- **`_MAX_BODY_BYTES = DEFAULT_MAX_BODY_BYTES` (`enricher.py:158`) is a copy that reads like a
  policy override.** It cannot currently differ. If a future refactor makes it diverge, the two
  knobs would be silently independent: `httpclient`'s own `fetch_with_retry` cap
  (`httpclient.py:183`) and `enricher`'s cap would disagree, and only one would apply.

- **`_fetch_website` has zero test coverage.** `grep -E "_fetch_website|check_websites|get_session|httpclient|DEFAULT_MAX_BODY"`
  over `tests/` returns nothing; `tests/test_lead_signal.py:66` imports `DEAD, LIVE, UNKNOWN,
  infer_region, lead_score` but never the function that produces them. The suite runs green (28
  tests, verified) while the module's entire I/O dependency surface is untested — which is why the
  three `F401`s, the `E402`, and the `verify=False`→`verify=True` dead suppression all survived.
  Any fix in this report should land with a test that stubs `httpclient.get_session` and asserts
  the LIVE/DEAD/UNKNOWN boundary, especially the 403 case in S1.

---

## Recommended order of work

1. **S1** — switch `enricher.py:175` to `fetch_with_retry(..., max_bytes=...)`; branch on
   `FetchResult.status` vs `.error`; classify `cf-mitigated` 403 as `UNKNOWN`. This is the finding
   that puts wrong `lead_score` values in the CSV, and the capability already exists in the
   dependency `_fetch_website` already imports. Land with a boundary test.
2. **S3 (dead imports)** — delete `enricher.py:2` and `enricher.py:4`, then delete
   `_stub_requests()` from `tests/test_lead_signal.py:23-63`. Zero-risk, immediately kills 3 of the
   8 ruff errors and 41 lines of test scaffolding.
3. **S2 (manifests)** — drop `beautifulsoup4`/`lxml` from `requirements.txt`; add `urllib3`; add a
   `ruff check` step to `.github/workflows/`. Do this *before* the src move, so `pyproject.toml`
   is the source of truth rather than a second file to reconcile afterwards.
4. **S2 (packaging)** — move to `src/leadminer/`, replace `pyproject.toml:35-37`'s `include` with
   `packages = ["leadminer"]`, and rewrite all eight intra-project imports as relative. Add
   `scrapers/__init__.py` re-exports in the *same* commit — never leave the 0-byte file as the
   thing preventing the cycle described above.
5. **S2 (`SCRAPER_EMAIL`)** — add a per-call header override to `get_session()` and strip the
   contact address from the UA sent to business websites.
6. **S3 (`urllib3` at 327)** — delete `enricher.py:327-328`.
7. **S3 (timeouts)** — thread `DEFAULT_TIMEOUT` through `check_websites`; drop the
   `_MAX_BODY_BYTES` alias.
8. **Logging** — give `enricher` a module logger and log at `warning` the `FetchResult.error` for
   every `UNKNOWN`, so the UNKNOWN bucket is diagnosable instead of a single end-of-run integer.
