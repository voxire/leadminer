# 390 — `check_websites` dependency map, layering and circular-import risk

**Lens:** deps · **Target:** `enricher.check_websites` at `enricher.py:225`
**Scope:** every module, function and global reachable from the function, transitively.

---

## Verdict

The function's *code* dependencies are almost trivial — one project module (`httpclient`),
two stdlib modules, and eleven of its own module globals — but its *implicit* dependencies
are where the risk lives. `check_websites` reads and writes five record keys that are never
typed against `BusinessRecord`, couples itself to `_fetch_website` through a bare `dict` whose
shape is written out twice, inherits a hidden thread-affinity contract from
`httpclient.get_session()` that nothing documents, and imports `httpclient` while
deliberately bypassing that module's headline API (`fetch_with_retry`) — so the HTTP policy
that commit `607e729` centralised is, for the single largest outbound request generator in the
system, still local.

Structurally, the project is **one line away from a package-level import cycle** and the
current wheel configuration already ships the wrong half of it. There is **no S1 under this
lens**, and I explain below why that is the honest answer rather than a dodge.

---

## Dependency map

Depth counted from `check_websites`. Every edge below was read, and the two marked
**[verified]** claims were executed.

### Layer 0 — the target

| Symbol | Location |
|---|---|
| `check_websites(records: list[dict], workers: int = 40) -> list[dict]` | `enricher.py:225-276` |

Free names resolved directly by the body: `ThreadPoolExecutor` (`:237`),
`as_completed` (`:240`), `_fetch_website` (`:238`), `LIVE`/`DEAD`/`UNKNOWN` (`:250`),
`print` (`:235`, `:262`, `:271`, `:272`), and the builtins `enumerate`/`len`/`sum`.
That is the whole direct surface. Everything below is transitive.

### Layer 1 — in-module callee

| Symbol | Location | Reached via |
|---|---|---|
| `_fetch_website(url: str) -> tuple[str, dict]` | `enricher.py:161-222` | `pool.submit` at `:238` |

`_fetch_website` carries **eleven** of the globals below. It is the real dependency
surface; `check_websites` is the thin consumer.

### Layer 2 — project modules: exactly one

| Symbol | Location | Reached via |
|---|---|---|
| `get_session()` | `httpclient.py:61-71` | `enricher.py:175` |
| `DEFAULT_MAX_BODY_BYTES` | `httpclient.py:54` | `enricher.py:158` → `_MAX_BODY_BYTES` |

`enricher.py:128` is the **only** project-internal import in the whole module, and it is
an **absolute top-level** import (`from httpclient import ...`), not relative. This single
line is what keeps the graph acyclic — see *Circular import* below.

Note that only **2 of `httpclient`'s 8 public symbols** are used. `fetch_with_retry`,
`http_session`, `FetchResult`, `utc_now_iso`, `DEFAULT_TIMEOUT`, `DEFAULT_RETRIES`,
`DEFAULT_BACKOFF` are all unused by this module.

### Layer 3 — transitive through `get_session`

| Symbol | Location | Note |
|---|---|---|
| `threading.local()` | `httpclient.py:58` (`_local`) | the per-thread Session registry |
| `_user_agent()` | `httpclient.py:74-85` | called once per thread at `:67` |
| `import os` | `httpclient.py:79` | **function-local** stdlib import |
| `os.environ["SCRAPER_EMAIL"]` | `httpclient.py:81` | **env-var dependency of the target** |
| `requests.Session()` | `httpclient.py:65` | runtime construction |
| `import requests` | `httpclient.py:43` | **import-time hard requirement** |

**[verified]** With `enricher.py` + `httpclient.py` on `sys.path` and no `requests`
installed:

```
import enricher FAILS: No module named 'requests'
```

So `import enricher` drags the entire `requests`/`urllib3`/`charset_normalizer` stack in at
**import time**, purely via `enricher.py:128 → httpclient.py:43`.

### Layer 2 — stdlib, used

| Symbol | Location | Note |
|---|---|---|
| `re` | `enricher.py:1` | `_fetch_website` `:200`, `:206`, `:212`, `:216` |
| `concurrent.futures` | `enricher.py:3` | the pool itself |

### Layer 2 — stdlib/third-party, imported and **never used** **[verified]**

`ruff check --select F401` reports exactly three, all in `enricher.py`:

```
enricher.py:2:8:  F401  `requests` imported but unused
enricher.py:4:26: F401  `urllib.parse.urljoin` imported but unused
enricher.py:4:35: F401  `urllib.parse.urlparse` imported but unused
```

These are **not** annotation-only imports; they are pure fossils. `git show 607e729`
(`enricher.py`) shows why: that commit deleted the module-level
`_SESSION = requests.Session()` and its `import requests` became dead in the same diff.
`urljoin`/`urlparse` have no user at all.

### Layer 2 — module globals of `enricher.py` reachable from the target

| Global | Location | Consumer |
|---|---|---|
| `LIVE`, `DEAD`, `UNKNOWN` | `:154-156` | target `:250`; `_fetch_website` `:178`, `:184`, `:192`, `:222` |
| `_MAX_BODY_BYTES` | `:158` | `_fetch_website:188` |
| `_EMAIL_RE` | `:130` | `_fetch_website:200` |
| `_EMAIL_BLACKLIST` | `:138-141` | `_fetch_website:202` |
| `_INSTAGRAM_RE` | `:131` | `_fetch_website:206` |
| `_IG_BLACKLIST` | `:142` | `_fetch_website:208` |
| `_WHATSAPP_RE` | `:132` | `_fetch_website:212` |
| `_LINKEDIN_RE` | `:136` | `_fetch_website:216` |

### Layer 3 — implicit, undeclared (the actual dependency surface)

These are not imports, but they are dependencies the compiler and the type checker cannot see:

1. **Record keys written:** `website_live` (`:250`), `email` (`:253`),
   `instagram` (`:255`), `whatsapp` (`:257`), `linkedin` (`:259`).
2. **Record keys read:** `website` (`:231`) plus the five above via `.get()` (`:252-259`).
3. **`_fetch_website` return contract:** `tuple[str, dict]` where the dict has exactly the
   keys `email`/`instagram`/`whatsapp`/`linkedin`. Written **twice** — `enricher.py:168`
   and `enricher.py:247-248` — with nothing tying them together.
4. **The canonical schema is never imported.** `BusinessRecord` lives at
   `scrapers/base.py:5`; `enricher.py`, `dedup.py` and `main.py` all consume that shape
   and none of them import it.
5. **Thread affinity.** Correctness of `enricher.py:175` depends on
   `httpclient.py:58` being `threading.local()` *and* on the executor at `:237` being
   thread-based. Nothing in either file states this.

**Maximum depth to the outside world: 3 hops** — to `os.environ` and to the full HTTP stack.

---

## Findings

### S2 — The wheel ships `scrapers/` but not the modules this function lives in and imports

- **Where:** `pyproject.toml:36-37` vs `enricher.py:128`, `enricher.py:225`
- **Breaks:** The wheel target is

  ```toml
  [tool.hatch.build.targets.wheel]
  # The package imports a top-level sibling module, so both must ship.
  include = ["scrapers/"]
  ```

  The comment says "**both**". Only one is listed. `enricher.py`, `httpclient.py`,
  `cli.py`, `main.py`, `dedup.py` and `pitch_recommender.py` are absent from the include
  list — and `enricher.py` is precisely the module that `scrapers/google_places.py:26`
  imports from. So the wheel ships the *dependent* and omits the *depended-upon*, which
  makes `scrapers.google_places` unimportable from an installed wheel. Meanwhile
  `pyproject.toml:33` declares `[project.scripts] leadminer = "cli:main"` and `cli.py` is
  also not shipped, so the declared console entry point cannot resolve either.

  There is also no `leadminer/__init__.py`, no `src/`, and no `leadminer.py` anywhere in
  the tree, so hatchling's default package detection for a distribution named `leadminer`
  has nothing to lock onto. Either the build fails outright, or it produces a wheel
  containing only `scrapers/`.

- **Trigger:** `pip install .` then `leadminer` — `ModuleNotFoundError: No module named
  'enricher'`, or `No module named 'cli'`.
- **Not S1 because:** CI never exercises this path. `.github/workflows/scrape.yml:39`
  installs `requirements.txt` and `:69` runs `python main.py` from the repo root, so
  `sys.path[0]` is the repo and every absolute sibling import resolves. The breakage is
  latent for anyone who installs the package.
- **Verification I could not run:** `hatchling` is not installed here and the brief forbids
  installing. `python -m build` on a scratch copy would settle which of the two branches is
  real in one command.
- **Fix:** Define the package. `packages = ["leadminer"]` (or move to `src/leadminer/` and
  set `sources = ["src"]`) and drop the hand-written `include` entirely — see the next
  finding for the mechanical change that makes this possible.

### S2 — `enricher` imports `httpclient` and then bypasses the API that module exists to provide

- **Where:** `enricher.py:128` + `enricher.py:175` + `enricher.py:188`, versus
  `httpclient.py:149` (`fetch_with_retry`), `httpclient.py:157` (`max_bytes`),
  `httpclient.py:183` (bounded read), `httpclient.py:185` (`ok = status < 400`)
- **Breaks:** The commit message for `607e729` says "`fetch_with_retry` takes max_bytes so
  a hostile host cannot stream an unbounded body into memory" — then the same commit
  imports `DEFAULT_MAX_BODY_BYTES` and hand-rolls the bounded read at `enricher.py:188`:

  ```python
  r = get_session().get(url, timeout=8, allow_redirects=True, stream=True)   # :175
  raw = r.raw.read(_MAX_BODY_BYTES, decode_content=True) or b""              # :188
  ```

  Three consequences, in increasing order of cost:

  1. **No retries.** `enricher.py:175` is a single `GET`. `scrapers/osm.py:39` and
     `scrapers/wikidata.py:70` both route through `fetch_with_retry`; `check_websites` —
     the largest outbound request generator in the system — does not. One transient DNS or
     TLS blip becomes `UNKNOWN`, which `lead_score` (`enricher.py:300-304`) scores as
     *neither* live (+10) nor dead (+20). A record whose site was up for 8 of 9 runs
     scores lower than a record with no website at all.
  2. **`timeout=8` bypasses `DEFAULT_TIMEOUT = 15`** (`httpclient.py:47`), so the tuning
     knob exists but is not reachable from here.
  3. **The LIVE/DEAD/UNKNOWN policy is encoded twice.** `FetchResult` already carries the
     `status is None` vs `status >= 400` distinction (`httpclient.py:98-100`, `:213`)
     that `_fetch_website:182-184` re-derives by hand. The two agree today. No test pins
     them together — see the test-coverage note below.

  `scrapers/google_places.py:227` is the *second* instance of this pattern (raw
  `session.post(..., timeout=30)` with a hand-rolled 429 counter at `:212-213`), and
  `google_places.py:159-166` duplicates the per-thread-Session factory that
  `httpclient.get_session()` already provides. So "shared HTTP layer" is currently 2 of
  4 call sites.
- **Trigger:** `records = [{"website": "https://flaky.example"}]`, where the host returns
  one `ConnectionResetError` then `200 OK`. `_fetch_website:176-178` catches the first
  attempt's exception and returns `UNKNOWN`; `check_websites:250` sets
  `website_live = None`; `lead_score:300-304` awards nothing. `fetch_with_retry` with
  `retries=3` and full jitter (`httpclient.py:201-203`) would have recovered inside the
  existing 8s timeout budget.
- **Why this is S2 not S1:** no wrong data and no dying run. It is a systematic,
  whole-dataset quality and politeness regression concentrated in one function — and it
  partially self-heals, because `main.py:150` reloads the cumulative master every run so
  every website is re-fetched next week.
- **Fix:** `fetch_with_retry("GET", url, timeout=8, max_bytes=DEFAULT_MAX_BODY_BYTES)` and
  map `FetchResult.status` → `LIVE`/`DEAD`/`UNKNOWN`.

### S2 — `get_session()` carries an undocumented thread-affinity contract that an async refactor would silently break

- **Where:** `enricher.py:175` and `enricher.py:237`, against `httpclient.py:58`
  (`_local = threading.local()`), `httpclient.py:61-71`
- **Breaks:** `enricher.py:175` reads as an innocuous accessor. Its correctness rests on an
  invariant stated in neither file: *one `requests.Session` per concurrently-executing
  thread*. Today `ThreadPoolExecutor(max_workers=40)` (`:237`) satisfies that, which is
  exactly the bug `enricher.py:124-127` and `httpclient.py:7-10` were written to eliminate.

  The obvious next step — replacing 40 OS threads with `asyncio` + `httpx`, which is what
  anyone would do to raise concurrency — **silently reinstates that bug**: asyncio tasks
  share one thread, so `getattr(_local, "session")` returns the *same* `Session` to every
  concurrent task. Cookie jar, header state and urllib3 pool all mutate per request
  (`httpclient.py:9-10`). The failure mode is not an exception; it is cross-talk between
  concurrent requests to unrelated hosts.

  A second, coupled hidden constraint: `fetch_with_retry`'s backoff is
  `time.sleep(retry_hint)` (`httpclient.py:206`) — blocking. So there are **two**
  thread-affinities in `httpclient`, neither documented at the call site that depends on
  them. Nothing in `enricher.py:175` tells a future maintainer that this line cannot be
  moved into a coroutine.
- **Trigger:** replace `enricher.py:237` with an `asyncio` fan-out over the same
  `_fetch_website`. All N concurrent tasks share one `Session`; with no errors raised, the
  symptom is intermittent wrong-header or wrong-cookie responses against third-party hosts.
- **Fix:** make the accessor async-safe at the source — an `httpx.AsyncClient` per event
  loop (or a `contextvars`-keyed cache, which is the correct generalisation of
  `threading.local` and is loop- and thread-affine), then update the
  `enricher.py:124-127` comment to name the invariant it encodes.

### S2 — One GET per record, not per URL; no cache

- **Where:** `enricher.py:231`
- **Breaks:**

  ```python
  targets = [(i, r["website"]) for i, r in enumerate(records) if r.get("website")]
  ```

  One entry per *record*, with no `set()` over URLs and no memoisation. `dedup` keys on
  normalised phone, else `(normalised_name, city)` (`dedup.py:197-232`) and **never** on
  `website`, so records sharing a URL routinely survive as separate rows. Two concrete
  shapes:

  - A three-branch franchise with one corporate site `https://group.com` and three
    different phones/cities → 3 rows after dedup → 3 GETs to `group.com` in the same run.
  - A record with no phone at all whose `(name, city)` differs in punctuation from a
    phone-keyed record: `dedup.py:228` only discards name-keyed duplicates whose phone is
    in `captured_phones`, so a phone-less row with the same site survives as a duplicate.

  Because `main.py:150` reloads the cumulative master every run and the master only grows
  (`main.py:209` rewrites it with all rows), the duplicate-URL rate rises monotonically
  week over week. The same host is hit repeatedly at 40-way concurrency, and the same page
  is re-parsed N times inside the 200 KB window.
- **Trigger:** `records = [{"website": "https://group.com"}] * 3` → three futures, three
  GETs, three parses. Identical observable result from one.
- **Fix:** dedupe `targets` on the URL, submit one future per distinct URL, then fan the
  single `(outcome, contacts)` result out to every index that referenced it.

### S2 — The record schema is owned by `scrapers`, and typing this function properly creates a package-level cycle

- **Where:** `scrapers/base.py:5` (`BusinessRecord`) vs `enricher.py:225`
  (`records: list[dict]`), combined with `scrapers/google_places.py:26`
  (`from enricher import infer_region`)
- **Breaks:** Two facts in tension:

  1. `BusinessRecord` (`scrapers/base.py:5-28`) is the canonical 23-key schema. It is
     imported by `google_places.py:28`, `osm.py:5` and `wikidata.py:5`. It is imported by
     **nobody** that consumes records: `enricher.py`, `dedup.py` and `main.py` all pass
     plain `dict`s around. So producer and consumer share no type.
  2. `scrapers/google_places.py:26` imports `infer_region` *out of `enricher`* — the
     dependency runs upstream-to-downstream.

  `check_websites` writes five keys (`:250`, `:253`, `:255`, `:257`, `:259`) against an
  untyped `dict`. Typing it as `list[BusinessRecord]` is the obviously correct next step —
  and `pyproject.toml:66` (`mypy`, `strict = true`) will push exactly there. That fix
  points `enricher → scrapers.base`, which is the **reverse** of the edge
  `scrapers.google_places → enricher`. The two packages become mutually dependent.

  Right now that is not a hard `ImportError`, because `enricher` would only touch
  `scrapers/__init__.py` (empty) and `scrapers/base.py` (imports `abc` and `typing` only).
  It is a layering inversion with the safety margin of one empty `__init__.py`: the moment
  `scrapers/__init__.py` re-exports anything, or `enricher` imports any `scrapers.*`
  module other than `base`, the cycle becomes a runtime `ImportError`.
- **Trigger:** add `from scrapers.base import BusinessRecord` to `enricher.py` and change
  `:225` to `records: list[BusinessRecord]`. Now move `infer_region` into
  `scrapers/geo.py` (a natural-looking refactor) and re-export it from `enricher`:
  `scrapers/__init__.py` → `scrapers.geo` → `enricher` → `scrapers.base` →
  `scrapers/__init__.py`. Import fails.
- **Why S2:** it blocks two fixes everyone wants (typing the function, moving to a src
  layout) in a way that only surfaces *after* the good change lands. The remedy is a
  module move, which is why it is worth doing deliberately.
- **Fix:** move `BusinessRecord` and `infer_region` into a new `leadminer/models.py` (and
  `leadminer/geo.py` for region inference) that neither package imports from.
  `pyproject.toml:30` already reserves the name for an optional extra
  (`geo = ["phonenumbers>=8.13"]`), so the seam is anticipated but does not exist.

---

## Circular import under a src layout

**The graph is currently acyclic, and it is one line away from a cycle.** That is the
finding; the current state is only safe by the accident of not needing a type.

### Present edges (project-internal only)

```
cli ──► main ──► scrapers.{osm,wikidata,google_places,whitelist} ──► scrapers.base
 │               scrapers.{osm,wikidata} ──► httpclient
 │               scrapers.google_places ──► enricher ──► httpclient ──► requests
 │               dedup
 │               enricher
 │               pitch_recommender
 └──► enricher, pitch_recommender, scrapers.*

scrapers.base ──► (nothing project-internal)
httpclient    ──► (nothing project-internal)
dedup         ──► (nothing project-internal)
```

### The 25 edits a src layout forces

There is no `src/` and no `__init__.py` anywhere; every project-internal import is
absolute and top-level. Under `src/leadminer/` **25 import statements** become
`ModuleNotFoundError`, and only **3** are already in relative form.

| File | Lines | Import |
|---|---|---|
| `main.py` | 25, 26, 27, 28, 29, 30, 31 | `scrapers.osm`, `scrapers.wikidata`, `scrapers.google_places`, `scrapers.whitelist`, `dedup`, `enricher`, `pitch_recommender` |
| `enricher.py` | **128** | `httpclient` |
| `scrapers/google_places.py` | 26, 27 | `enricher`, `httpclient` |
| `scrapers/osm.py` | 4 | `httpclient` |
| `scrapers/wikidata.py` | 4 | `httpclient` |
| `cli.py` | 35, 46, 47, 48, 249, 250, 251, 252 | `main`, `scrapers.*`, `enricher`, `pitch_recommender` |
| `tests/test_lead_signal.py` | 65, 66, 67, 68 | `dedup`, `enricher`, `main`, `pitch_recommender` |

Already correct: `scrapers/google_places.py:28`, `scrapers/osm.py:5`,
`scrapers/wikidata.py:5` (`from .base import ...`).

`enricher.py:128` is the one that matters most, because it is the edge that keeps
`enricher` out of the `scrapers` cycle. It is also, awkwardly, an import statement in the
**middle of the file** — at `:128`, between the section comment at `:120-122` and the regex
constants at `:130`. ruff's `I001` flags the block at `enricher.py:1:1` as unsorted for
this reason, and it is the only `I001` in the module. Mid-file placement also means
`DEFAULT_MAX_BODY_BYTES` is bound at `:128` but aliased at `:158`, so the value's origin is
30 lines from its definition and after a five-line comment about TLS.

### Where the cycle appears

Not from the move itself — from the *typing* fix that the move makes natural. See the
S2 above: `enricher → scrapers.base` (to get `BusinessRecord`) + the existing
`scrapers.google_places → enricher` (line 26) = mutual package dependency.

**Cheapest cycle-free order:**
1. Create `leadminer/models.py` with `BusinessRecord` (moved from `scrapers/base.py:5`).
   `scrapers/base.py` re-exports it for one release.
2. Move `infer_region` + `REGION_KEYWORDS` + `_REGION_MAP` + `_LB_COORD_REGIONS` +
   `_KSA_COORD_REGIONS` (`enricher.py:10-94`) to `leadminer/geo.py`. This is the clean
   cut: it removes `scrapers.google_places → enricher`, which is the only edge that points
   upstream, and `enricher.py` re-exports `infer_region` so `enricher.py:25`-adjacent
   callers and `tests/test_lead_signal.py:66` keep working.
3. Only then type `check_websites` as `list[BusinessRecord]` and move to `src/leadminer/`.

---

## Imports present only for typing, or only for logging

The brief asks for both explicitly.

### Annotation-only

**None in `enricher.py`.** Every stdlib import there is either used at runtime or unused
entirely (§Layer 2 above) — there is no third symbol in the dead-import bucket, so nothing
is hiding behind `if TYPE_CHECKING:` or a string annotation.

The nearest thing is a genuine annotation/runtime coupling one hop out:

- `httpclient.py:61` (`-> requests.Session`), `httpclient.py:153`
  (`session: requests.Session | None`) — `httpclient.py:32` has
  `from __future__ import annotations`, so these two annotations alone would *not* require
  `requests` at import time. The runtime requirement is real and comes from
  `requests.Session()` at `:65` and `requests.RequestException` at `:176`. Worth knowing
  before someone "cleans up" `:43`.
- `httpclient.py:41` `from typing import Any` — used at `:110`, `:120`, `:158`. Real
  usage, not annotation-only.

The structural typing gap is the opposite problem: `enricher.py:161`
(`-> tuple[str, dict]`), `enricher.py:168` (`contacts: dict`) and `enricher.py:225`
(`list[dict]`) use bare `dict` where `BusinessRecord` and a contacts `TypedDict` both exist
or are trivial to write. Under `pyproject.toml:66` (`mypy strict = true`), these are
untyped and the module will not pass.

### Logging-only

**`enricher.py` imports no logging at all** and issues **no log records**. It uses `print`
at `:235`, `:262`, `:271` and `:272`.

That is the wrong side of the existing seam: `httpclient.py:45` creates a real logger
(`log = logging.getLogger(__name__)`) and uses `log.debug` at `:178-179` and `:204-205`;
`scrapers/osm.py:1` and `scrapers/wikidata.py:1` import `logging`. `enricher.py` is the
module with the most outbound traffic to third-party hosts and it is the only one that
cannot answer "why did this URL come back UNKNOWN?"

Concretely: `_fetch_website:176` swallows every exception with a bare
`except Exception: return UNKNOWN, contacts`. Per-URL diagnosis is impossible today —
`enricher.py:264-275` reports only aggregate counts, so a run where 40% of sites are
UNKNOWN and one regional ISP's DNS is broken is indistinguishable from a run where 40% of
sites are genuinely down. Note also that `enricher.py:327-328` disables
`urllib3.exceptions.InsecureRequestWarning` process-wide inside `enrich()`, for a
condition this module no longer creates: `enricher.py:175` relies on the `requests`
default `verify=True`, and `grep -rn verify --include="*.py"` finds no `verify=False`
anywhere in the tree. Nothing is suppressing anything.

### Test-visible consequence

**[verified]** The 28-test suite passes on a machine with **no `requests` installed**
(`python3 -m unittest discover -s tests` → `Ran 28 tests ... OK`). That is a genuine win
for the `httpclient` quarantine — but it is bought with a 40-line fake-module harness at
`tests/test_lead_signal.py:23-63` that injects fake `requests` and `urllib3` modules into
`sys.modules` before importing the project.

Its docstring (`tests/test_lead_signal.py:24`) says *"enricher imports requests at module
scope"*. That is now **wrong**: `enricher.py:2` is dead. The import that actually forces
the stub is `enricher.py:128 → httpclient.py:43`, one level removed. So the comment
misdirects exactly the maintenance it exists to protect — someone who removes the dead
`import requests` at `:2` and trusts the comment may also delete the stub, and then
`tests/test_lead_signal.py:65` fails with `ModuleNotFoundError: requests` on any machine
that has not installed dependencies.

Finally: **`grep -rn "check_websites\|_fetch_website" tests/` returns nothing.** Zero
tests cover the function under audit or its only callee — the entire contact-extraction
and LIVE/DEAD/UNKNOWN classification path is untested. The commit that introduced
`httpclient` asserts "Tests still 20, all green" (`607e729`) while the module with the
highest network surface gained no coverage.

---

## Not a bug, but worth knowing

- **Declared dependencies ≠ actual dependencies.** `requirements.txt:2-3` pins
  `beautifulsoup4==4.12.3` and `lxml==5.2.2`, and
  `grep -rn "bs4\|BeautifulSoup\|lxml" --include="*.py"` returns **zero** importers.
  Meanwhile `_fetch_website:199-220` regex-parses untrusted HTML by hand
  (`enricher.py:130-136`) with a real HTML parser installed and unused. `requests` is
  constrained twice with different policies: `requirements.txt:1` (`==2.32.3`, the file CI
  actually installs at `scrape.yml:39`) vs `pyproject.toml:17` (`>=2.32.3,<3`).
- **The User-Agent version is a third source of truth.** `httpclient.py:85` hardcodes
  `leadminer/0.2` while `pyproject.toml:7` declares `version = "0.2.0"`. They will drift,
  and nothing records which build produced a given CSV. Use
  `importlib.metadata.version("leadminer")`.
- **`SCRAPER_EMAIL` is a real dependency that nothing enforces.**
  `check_websites → get_session → _user_agent → os.environ["SCRAPER_EMAIL"]`
  (`httpclient.py:81`), and `httpclient.py:75-77` states the Overpass policy reason it
  exists. But `cli.py:210-214` checks it and only hard-fails on `GOOGLE_PLACES_API_KEY`
  (`:213-214` sets `ok = False`), and `check_websites` has no way to know the header was
  sent without a contact address. The pipeline runs and exports CSVs while violating the
  third-party policy the code documents itself as complying with.
- **`import os` inside `_user_agent` with an unreachable handler.**
  `httpclient.py:78-83` wraps `import os` in `try/except Exception`. `os` is stdlib and
  always importable, so that handler can never fire. Same pattern at
  `httpclient.py:111`, `:135`, `:227` (`json`, `datetime`) — not in this closure, but
  `httpclient.py:79` is.
- **Deny-list policy is half-hoisted.** `_EMAIL_BLACKLIST` (`:138`) and `_IG_BLACKLIST`
  (`:142`) are module constants; two siblings are inline literals in the same function —
  `not addr.endswith(".png")` at `:202` and `slug not in {"company", "in", "pub"}` at
  `:219`. Four filters, one policy, two homes.
- **`_fetch_website` and `check_websites` agree on a `dict` shape by convention only.**
  The key set is written at `:168` and again in the fallback at `:247-248`. Nothing
  verifies the two match, and nothing verifies the keys `check_websites` reads at
  `:252-259` exist. Adding a fifth channel to `:168` silently drops it at `:250-259`.
- **The body cap is unreachable per call.** `_MAX_BODY_BYTES` (`:158`) is bound once at
  import time from `httpclient.py:54`. `check_websites` exposes `workers` as a tuning knob
  (`:225`) but no cap and no timeout, while both live in the module it already imports.
- **`workers=40` has no backpressure.** `:237` submits every target at once
  (`:238`). Nothing bounds concurrency against the number of distinct hosts, which
  matters given the S2 above about duplicate URLs.

---

## Recommended order of work

1. **`src/leadminer/` restructure + `packages` in `pyproject.toml`.** Fixes the S2 wheel
   finding and forces the 25 absolute imports to become relative. Do the mechanical pass
   (`enricher.py:128` → `from .httpclient import ...`) before anything semantic.
2. **Cut the `enricher` → `scrapers` edge before adding any.** Move `BusinessRecord` to
   `models.py` and `infer_region` + region tables to `geo.py`, re-exporting from
   `enricher.py` for compatibility. Without this, step 3 creates a cycle.
3. **Type the contract.** `Contact` TypedDict shared by `enricher.py:168` and `:247-248`;
   `check_websites(records: list[BusinessRecord], ...)`; `FetchOutcome` instead of bare
   `str`. Then `check_websites` as `list[BusinessRecord]` is safe.
4. **Route through `fetch_with_retry`** with `max_bytes=DEFAULT_MAX_BODY_BYTES` and
   `timeout=8`, and map `FetchResult.status` → `LIVE`/`DEAD`/`UNKNOWN`. Single change,
   largest quality win, and it deletes the hand-rolled read at `:188`.
5. **Dedupe `targets` on URL** (`:231`) and fan one result out to all referencing indices.
   Cuts requests and load on third-party hosts immediately, and is a prerequisite for any
   sane retry policy.
6. **Log instead of print** in `enricher.py`; log the exception in the `except Exception`
   at `:176` and `:190` with the URL. Delete the dead `urllib3.disable_warnings` at
   `:327-328`. Then the aggregate at `:264-275` becomes diagnosable.
7. **Fix `tests/test_lead_signal.py:24`** to name `enricher.py:128` instead of
   `enricher.py:2`, and delete the three dead imports. Cheap, and removes an active
   misdirection.
8. **Add a test for `check_websites`.** Zero coverage today on the module with the most
   network surface. A single `_fetch_website` test asserting LIVE/DEAD/UNKNOWN for
   200 / 500 / `ConnectionError`, plus one on `check_websites` mapping the three outcomes
   onto `True`/`False`/`None` at `:250`, pins the contract that steps 3-4 change.