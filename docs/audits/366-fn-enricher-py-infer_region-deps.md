# 366 — `infer_region` (enricher.py:79) — dependency lens

## Verdict

`infer_region` has **zero intra-project dependencies**. Its complete closure is the stdlib `re`
plus four module-level literals in its own file, and I extracted it standalone via AST and ran it
successfully with nothing but `re` in the namespace. That is the good news, and it should be stated
plainly rather than dressed up: this function is a pure leaf and cannot create an import cycle.

Everything wrong here is in the *edges other people draw to it*. `scrapers/google_places.py:26`
imports it from the **enrichment stage into the ingestion stage** — the only reverse edge in the
package — and because `infer_region` lives in the same module as the 40-thread HTTP sweep, that one
import drags the entire network stack (`requests`, `httpclient`, `concurrent.futures`, 8 stdlib
modules; **61 modules** measured) into the scraper to evaluate a list membership test. Under a src
layout the same import is a hard `ModuleNotFoundError: No module named 'enricher'`, which I
reproduced. The second-order cost is already paid: `tests/test_lead_signal.py:23-63` is a 41-line
`sys.modules` forgery whose docstring reads *"enricher imports requests at module scope. Stub it if
absent"* — scaffolding that exists solely because a pure string function is unreachable without a
network library installed.

The second real defect is an **unenforced string contract**: `country` is typed `str` but its domain
is `{"LB", "SA"}`, and both producers compute it by substring-matching free text
(`scrapers/google_places.py:127`, `main.py:50`). Add one city-scoped KSA query and Saudi leads are
written to all five CSVs as `country="LB", region=None`. I reproduced the whole chain end to end.

---

## Dependency map — transitive closure of `enricher.py:79`

Computed by AST free-variable walk, then verified by extracting the four globals + the function into
a namespace containing only `re` and executing it.

| # | Symbol | Kind | Defined at | Reached from | Notes |
|---|---|---|---|---|---|
| 1 | `re` | **stdlib** | `enricher.py:1` | `:53` | `re.escape`, `re.compile`, `re.IGNORECASE`. The **only** module import in the closure. |
| 2 | `REGION_KEYWORDS` | module global | `enricher.py:10-50` | `:53-54` | 8 regions, **114 keyword strings** (incl. Arabic). Plain data. |
| 3 | `_REGION_MAP` | module global (derived) | `enricher.py:52-55` | `:86` | 8 compiled patterns, built at import time from #2. |
| 4 | `_LB_COORD_REGIONS` | module global | `enricher.py:58-68` | `:90, :91` | 8 `(region, lat_min, lat_max, lon_min, lon_max)` tuples. |
| 5 | `_KSA_COORD_REGIONS` | module global | `enricher.py:70-76` | `:90` | 5 tuples. Selected only when `country == "SA"`. |
| 6 | `pattern.search` / `.search()` | `re.Pattern` API | — | `:87` | |
| 7 | tuple unpacking | builtin | — | `:91` | |
| 8 | `country == "LB"` / `== "SA"` | **string literal contract** | `:85`, `:90` | — | Not a symbol, but the function's real cross-layer dependency. See S2. |

**Intra-project import edges reachable from `infer_region`: zero.**

Verified by extraction (the only binding in the exec namespace was `re`):

```
infer_region('Hamra, Beirut', None, None)                        -> 'Beirut'
infer_region('Main Square, Nabatieh, Lebanon', None, None)       -> 'South Lebanon'
infer_region(None, 33.855, 35.533)                               -> 'Beirut'
infer_region(None, 24.71, 46.67, country='SA')                  -> 'Riyadh'
```

Import-time cost of building the closure in isolation: **8.65 ms**, all of it the 8 `re.compile`
calls at `enricher.py:53`.

### The module's import surface — nearly all of it irrelevant to this function

`infer_region` lives in `enricher`, so *importing it* means running `enricher.py:1-4` and
`enricher.py:128` in full. Measured `sys.modules` delta with `requests`/`urllib3` stubbed:

```
from enricher import infer_region   ->  61 new modules
  local  : enricher, httpclient
  stdlib : concurrent, contextlib, dataclasses, email, logging, random, threading, (+53 more)
```

| Symbol | Defined at | Needed by `infer_region`? |
|---|---|---|
| `requests` | `enricher.py:2` | **No — unused module-wide.** Verified by AST name-usage walk; `F401`. |
| `urljoin`, `urlparse` | `enricher.py:4` | **No — unused module-wide.** `F401` ×2. |
| `ThreadPoolExecutor`, `as_completed` | `enricher.py:3` | No — `check_websites` only (`:237`, `:240`). |
| `DEFAULT_MAX_BODY_BYTES`, `get_session` | `enricher.py:128` | No — `_fetch_website` only (`:158`, `:175`). |
| `urllib3` | `enricher.py:327` (function-local) | No — `enrich()` only. |
| `httpclient` transitively (`logging`, `random`, `threading`, `email.utils`, `contextlib`, `dataclasses`) | `httpclient.py:34-43` | **No.** Inherited for nothing. |

Cross-reference `docs/audits/382-fn-enricher-py-_fetch_website-deps.md`, which reached the same
`enricher.py:2`/`:4`/`:128` conclusions from `_fetch_website`. The delta from `infer_region`'s side
is worse: `_fetch_website` at least *needs* `httpclient`. `infer_region` needs none of it, and is
nevertheless made to pay for it by `scrapers/google_places.py:26`.

---

## Full intra-project import graph (verified by AST over all 14 `.py` files)

```
main.py:25-31              -> scrapers.osm, scrapers.wikidata, scrapers.google_places,
                               scrapers.whitelist, dedup, enricher, pitch_recommender
cli.py:35, 249-252         -> main, enricher, pitch_recommender, scrapers.*   (function-local)
scrapers/google_places.py:26-28 -> enricher  <-- REVERSE EDGE, scrapers -> enricher, httpclient, .base
scrapers/osm.py:4-5        -> httpclient, .base
scrapers/wikidata.py:4-5   -> httpclient, .base
enricher.py:128            -> httpclient
dedup.py:1-2               -> (none)
pitch_recommender.py:22    -> (none)
scrapers/base.py:1-2       -> (none: abc, typing)
scrapers/whitelist.py      -> (none; file has no imports at all)
scrapers/__init__.py       -> (none — file is 0 bytes)
httpclient.py:34-43        -> (none: stdlib + requests)
```

**This is a DAG. No cycle exists today.** `infer_region`'s closure contains no project edge at all,
so it is not in a position to create one. The reverse edge `scrapers → enricher` is what makes the
future cycle reachable; see *Circular imports under a src layout*.

---

## Findings

### S1 — `scrapers/google_places.py:26` imports the enrichment stage into the ingestion stage: the only reverse edge in the package, and a hard `ModuleNotFoundError` under a src layout

- **Where:** `scrapers/google_places.py:26` — `from enricher import infer_region`, consumed at
  `:275`. Compare `scrapers/osm.py:4` and `scrapers/wikidata.py:4`, which import `httpclient`
  (a genuine leaf with zero project imports) and correctly emit `region=None` for the enricher to
  fill in later.
- **Breaks — three separate ways.**
  1. **Layering.** The pipeline is `scrape → filter → dedup → enrich → score` (`BRIEF.md:16`). The
     ingestion layer reaching up into the enrichment layer means `enricher` can now never legitimately
     import *anything* from `scrapers` without creating a cycle, and it means the scrapers are not
     independently runnable. `cli.py:40-49` (`leadminer scrape --source osm`) already documents the
     intent — "run a single scraper and print what it found" — and that intent is broken for
     `google_places` by this one line.
  2. **Packaging.** Verified by copying the tree to `src/leadminer/` and importing unchanged:
     ```
     FAIL  leadminer.scrapers.osm:           ModuleNotFoundError: No module named 'httpclient'
     FAIL  leadminer.scrapers.wikidata:      ModuleNotFoundError: No module named 'httpclient'
     FAIL  leadminer.scrapers.google_places: ModuleNotFoundError: No module named 'enricher'   <-- :26
     FAIL  leadminer.enricher:               ModuleNotFoundError: No module named 'httpclient'
     FAIL  leadminer.main:                   ModuleNotFoundError: No module named 'scrapers'
     ```
     A package's own directory is never placed on `sys.path`, so a bare top-level name is unreachable
     from inside a package. Separately, `pyproject.toml:35-37` (`include = ["scrapers/"]`, with a
     comment that literally states *"The package imports a top-level sibling module, so both must
     ship"*) ships only `scrapers/`. The wheel therefore cannot satisfy this edge even under today's
     flat layout, and `[project.scripts] leadminer = "cli:main"` (`:33`) points at `cli`, which is
     likewise not shipped.
  3. **Cost.** Importing one pure function costs 61 modules (measured) and a hard dependency on
     `requests` (see S2).
- **Trigger (the layering violation, demonstrated today):**
  ```python
  # tests/test_lead_signal.py:23-63 must fabricate `requests` and `urllib3` into sys.modules
  # before it can run:  `from enricher import infer_region`   (:66)
  ```
  and in this checkout, where `requests` is not installed:
  ```
  $ python3 -c "import enricher"
  ModuleNotFoundError: No module named 'requests'      # enricher.py:2 — and nothing in infer_region uses it
  ```
- **Fix:** delete `scrapers/google_places.py:26` and let `google_places` emit `region=None`
  (`:284` already accepts `None`, and `BusinessRecord.region` at `scrapers/base.py:9` is
  `str | None`). `osm.py` and `wikidata.py` already work this way. `enricher.py:331`
  (`if not r.get("region")`) then fills it in with the *corrected* country, which is strictly better
  than today's duplicate call. Same change removes the S2 `country` trap below.

### S2 — a dead `import requests` makes a zero-network function require a network library, and cost 41 lines of test scaffolding

- **Where:** `enricher.py:2` (`import requests`), `enricher.py:4`
  (`from urllib.parse import urljoin, urlparse`), `enricher.py:128`
  (`from httpclient import DEFAULT_MAX_BODY_BYTES, get_session`); paid for at
  `tests/test_lead_signal.py:23-63` and `:66`.
- **Breaks:** an AST name-usage walk over `enricher.py` (collecting `Name` and the base of every
  `Attribute` chain) reports:
  ```
  USED    L1    re                          <- the only import infer_region needs
  UNUSED  L2    requests
  UNUSED  L4    urljoin
  UNUSED  L4    urlparse
  USED    L3    ThreadPoolExecutor / as_completed      (check_websites only)
  USED    L128  DEFAULT_MAX_BODY_BYTES / get_session   (_fetch_website only)
  USED    L327  urllib3                                (enrich only)
  ```
  `requests` appears in `enricher.py` only inside comments (`:125-126`). It is nevertheless the
  reason `import enricher` — and therefore `import leadminer.scrapers.google_places` and
  `from enricher import infer_region` in the test suite — hard-fails without a network stack
  installed. The consequence is already written down: `tests/test_lead_signal.py:23-63` fabricates
  `sys.modules["requests"]` (`:50`) and `sys.modules["urllib3"]` (`:60`) with a `Session.get` that
  raises `RuntimeError("no network in tests")` (`:37-38`), and installs them **process-globally and
  never removes them** (`:63`), before `main` is imported at `:67`. So the fake also captures
  `scrapers/google_places.py:24`. That stub `Session` (`:33-35`) defines only `self.headers` — no
  `cookies`, `request`, `mount`, or `close`.
- **Trigger:**
  ```
  $ python3 -c "import enricher"          # requests absent, as in a bare checkout
  ModuleNotFoundError: No module named 'requests'
  ```
  Any environment that installs `pyproject.toml:16-18` (`requests>=2.32.3,<3`) to run a *string
  matcher* pays a TLS/urllib3 import chain — `ssl`, `socket`, `http.client`, `email`, `hashlib`,
  `idna`, `certifi` — on every process start.
- **Fix:** move `REGION_KEYWORDS`, `_REGION_MAP`, `_LB_COORD_REGIONS`, `_KSA_COORD_REGIONS` and
  `infer_region` verbatim (`enricher.py:10-94`, 85 lines) into a new top-level `leadminer/geo.py`
  with **zero imports** except `re`. Repoint `enricher.py:332`, `scrapers/google_places.py:26` and
  `tests/test_lead_signal.py:66` at it. Then delete `enricher.py:2` and `enricher.py:4`, and delete
  `_stub_requests()` plus the `# noqa: E402` chain at `tests/test_lead_signal.py:65-68`.
  (Deleting `:2`/`:4` alone clears 3 of the 8 ruff errors `382` lists and makes the stub
  unnecessary — but it does **not** fix the layering violation in S1, which needs the move.)

### S2 — `country` is typed `str` but its domain is `{"LB","SA"}`, and both producers compute it by substring-matching free text

- **Where:** the parameter at `enricher.py:83` (`country: str = "LB"`), consumed by two string
  comparisons at `enricher.py:85` (`country == "LB"`) and `enricher.py:90`
  (`_KSA_COORD_REGIONS if country == "SA" else _LB_COORD_REGIONS`). Producers:
  `scrapers/google_places.py:127-136` (`_country_from_query`) → `:187` → `:275`, and
  `main.py:50-74` (`resolve_country`) → `main.py:184`.
- **Breaks:** the parameter is the single most load-bearing value `infer_region` receives, it has a
  two-element closed domain, and **nothing** enforces it — no `Literal`, no `StrEnum`, no assert, no
  normalisation at the boundary. Two different substring heuristics produce it, and they can disagree.
  `_country_from_query` recognises `"SA"` only if the query text contains one of
  `saudi|riyadh|jeddah|dammam|mecca|medina`. I ran all 67 shipped queries through it: **none
  misclassify today**, so this is latent, not active. But it is one query away:
  ```
  _country_from_query('restaurants in Khobar') -> 'LB'      # Khobar, Abha, Taif, Tabuk all unknown
  _country_from_query('hotels in Taif')        -> 'LB'
  _country_from_query('banks in Abha')         -> 'LB'
  ```
  And because `scrapers/google_places.py:289` takes `place.get("nationalPhoneNumber")` — the
  **national**-format number, e.g. `0138940000`, with no `+966` — `main.py:70`'s
  `"+966" in phone` fallback never fires, so `main.py:184 resolve_country` cannot recover it either.
  End-to-end reproduction, running the real `infer_region` against the real `_country_from_query`
  and `resolve_country` bodies:
  ```
  google_places.py:275  infer_region(None, 26.2794, 50.2083, country='LB')  ->  None
  main.py:184           resolve_country(...)                               ->  'LB'
  enricher.py:332       infer_region('Abdulaziz Bin Rd, Al Khobar, ...', 26.2794, 50.2083, 'LB')  ->  None
  FINAL ROW: country='LB'  region=None
  control:              infer_region(None, 26.2794, 50.2083, country='SA') ->  'Dammam'
  ```
  The row is written to all five CSVs (`main.py:209-213`) as a **Lebanese** lead from Khobar.
  Note the failure compounds: with `country == "LB"` the address branch at `enricher.py:85` *runs*,
  so a Saudi street address is searched against 114 Lebanese keywords, and then the coordinate branch
  searches 8 boxes that top out at latitude 34.72°N.
- **Related, same parameter:** `enricher.py:334` passes `country=r.get("country", "LB")`. `dict.get`
  does **not** fall back on an explicit `None`, and `main.py:88` manufactures exactly that state
  (`if row[k] == "": row[k] = None`). `main.py:184` repairs it before `main.py:187` calls `enrich`,
  so the shipped path is safe — but `enrich()` called directly (external script, a future
  `leadminer enrich` subcommand) silently loses address-based inference:
  ```
  infer_region('Hamra, Beirut', None, None)        -> 'Beirut'
  infer_region('Hamra, Beirut', None, None, None)  -> None
  ```
  The regression test at `tests/test_lead_signal.py:147-153` **routes around** this rather than
  fixing it: it threads `resolve_country(r)` in by hand at `:152` instead of exercising
  `enricher.py:334`. So the call site remains untested and unfixed.
- **Also:** the type says `str`, but `scrapers/google_places.py:275` feeds it `country=country`
  (typed `str` at `:208`, from `_country_from_query` — fine) and `lat`/`lon` come from
  `resp.json()` at `:273-274`, i.e. `Any`. `enricher.py:92`'s `lat_min <= lat <= lat_max` is an
  unguarded comparison on an unvalidated boundary. `None` is handled (`:89`); a JSON string is not.
- **Fix:** `type Country = Literal["LB", "SA"]`; normalise at both producers (one call to
  `resolve_country`); change `enricher.py:334` to `country=resolve_country(r) or "LB"` (or move
  `resolve_country` into the neutral module from S1's fix); and derive KSA-ness from the
  *coordinates* (`_KSA_COORD_REGIONS` bounds) rather than from the query string, so the two inputs
  cannot disagree.

### S3 — the one importer of `infer_region` uses only the coordinate half, so it drags 114 keyword strings and 8 compiled regexes to do it

- **Where:** `scrapers/google_places.py:275` — `infer_region(None, lat, lon, country=country)`;
  the `address` argument is a **literal `None`**, not `place.get("formattedAddress")` (which is
  available at `:286`).
- **Breaks:** the `_REGION_MAP` branch at `enricher.py:86-88` is unreachable from this call site,
  so the 114 keyword strings at `enricher.py:10-50` and the 8 patterns at `:52-55` are built
  (`re.compile` × 8, measured 8.65 ms) and carried into the scraper's import for nothing. The SA
  half is worse than dead weight — it is **structurally unreachable**: `enricher.py:85` gates the
  entire address branch on `country == "LB"`, so
  ```
  infer_region('King Fahd Rd, Riyadh', None, None, 'SA')  ->  None
  infer_region('Main Square, Nabatieh, Lebanon', None, None)  ->  'South Lebanon'   # first match wins
  ```
  `REGION_KEYWORDS` has zero KSA entries, so for every Saudi record the address table is not merely
  unused, it is gated off. `docs/audits/094-infer-region-correction.md:34` and `:276-279` already
  propose widening the gate; this is the dependency-shaped version of the same observation, and the
  reason the fix is cheap — there is no Saudi data to add, only a `!=` to change.
- **Trigger:** `google_places.py:26`'s import plus `:275`'s hardcoded `None`. Any future edit to
  `:275` that passes the address now silently changes the ingestion layer's behaviour relative to
  `enricher.py:332`, which passes `r.get("address")` — the two call sites will diverge with no test
  comparing them.
- **Fix:** fold into S1 — pass `region=None` from the scraper and let `enrich()` compute it once,
  from the real address, with the real country.

### S3 — `enricher.py` lacks `from __future__ import annotations`, so `re.Pattern` is evaluated at import time purely to annotate a module global

- **Where:** `enricher.py:10`, `:52`, `:58`, `:70` are module-level `AnnAssign` nodes; the module
  has no `from __future__ import annotations` (`httpclient.py:32` and `cli.py:18` do; 2 of 14
  modules do).
- **Breaks:** without PEP 563 the annotation expressions are evaluated when the module is imported,
  so `list[tuple[str, re.Pattern]]` at `enricher.py:52` makes `re` an **import-time** dependency
  twice over — once for `re.compile` at `:53`, once for the type object in the annotation. That is
  cosmetic on 3.12, where `re.Pattern` exists as a runtime attribute. It matters for the next
  person: under the project's own `mypy strict = true` (`pyproject.toml:66`), the bare `re.Pattern`
  fails `disallow_any_generics` and should be `re.Pattern[str]`. mypy is not installed and not run
  in CI (per `382`), so the annotation is currently unchecked in both directions.
- **Fix:** add `from __future__ import annotations` to `enricher.py` and parameterise
  `_REGION_MAP: list[tuple[str, re.Pattern[str]]]`.

---

## Imports used only for typing / only for logging

The lens asks specifically. For `infer_region`'s closure the honest answer to both is **none**, and
that is worth stating rather than padding:

**Type-annotation-only imports: zero.** `re` (`enricher.py:1`) is a hard runtime dependency —
`re.escape` and `re.compile` execute at import time at `enricher.py:53`. The signature at
`enricher.py:79-84` uses only builtin generics (`str | None`, `float | None`), needing no `typing`
import. This is *better* than the sibling functions in the same module: `_fetch_website` (`:161`),
`check_websites` (`:225`) and `lead_score` (`:283`) all annotate bare `dict` when
`scrapers/base.py:5-28` defines a 23-key `BusinessRecord` TypedDict for exactly these rows — so
their third-party surface (`r.raw.read`, `r.encoding`) is unchecked. `infer_region` at least gets its
own types right, and that is precisely why its dependency set is one module.

The gap is a **missing** dependency, not a spurious one: `infer_region` depends on the *values*
`"LB"`, `"SA"` and the 13 region-name strings by literal, shared with nothing (see S2).

**Logging-only imports: zero.** `enricher.py` imports no `logging` at all
(`grep -c "logging\|log\."` → 0) and uses bare `print` at `:235`, `:262`, `:271`, `:272`. It does
*inherit* `httpclient.py:35` (`import logging`) and `:45` (`log = logging.getLogger(__name__)`)
via the `enricher.py:128` edge, and uses neither. `382` covers the consequence for `_fetch_website`
(zero log output from the module that owns the pipeline's riskiest failure mode); the same is true
here, with a specific consequence for this function:

`infer_region` resolves three overlapping lookup tables by **first match wins**, and
`docs/audits/007-enricher-region-inference.md:30` and `087-SYNTH-test-gap.md:242` document
governorates that are permanently unreachable because an earlier box swallows them. There is no seam
to observe that: no logger, no counter, no return of the discarded candidate. A misclassification
that is deterministic and invisible is exactly the class of defect that reaches a customer CSV
unnoticed. If `geo.py` lands, give it a `log.debug` on a non-first box hit — it is the cheapest
possible instrumentation and the only thing that would make the overlap table auditable.

---

## Circular imports under a src layout

**No cycle exists today**, and I verified that rather than assuming it: the graph above is a DAG, and
`infer_region`'s closure contains no project edge whatsoever. It is in the one position a function
can be in where a restructure is a no-op — *if it lived in its own module*. It does not.

**Break #1, today, from this function's dependency:** `scrapers/google_places.py:26`. Reproduced in
`src/leadminer/` with imports unchanged → `ModuleNotFoundError: No module named 'enricher'`. Same for
the wheel: `pyproject.toml:37` ships `scrapers/` and not `enricher.py`.

**Break #2, latent, one line away — and it fails *differently depending on import order*, which is
the worst failure mode.** The reverse edge `scrapers → enricher` means `enricher` currently owns no
edges into `scrapers`. That is a constraint with no expiry. I built the smallest src-layout tree that
adds **one** plausible line — `enricher` reaching for a constant that currently lives in the scraper
package — and got a hard cycle in both import orders:

```
# google_places.py:26 -> ..enricher -> enricher.py -> .scrapers.google_places -> (partially initialised)
import leadminer.scrapers.google_places
ImportError: cannot import name 'API_URL' from partially initialized module
'leadminer.scrapers.google_places' (most likely due to a circular import)

# and in the other order:
import leadminer.enricher
ImportError: cannot import name 'infer_region' from partially initialized module
'leadminer.enricher' (most likely due to a circular import)
```

Two properties make this worse than an ordinary cycle. First, **which line reports the error depends
on which module the interpreter reached first**, so the traceback points at `infer_region` — the
innocent symbol — in one order and at the scraper's constant in the other. Second, the graph is
protected today only by `scrapers/__init__.py` being **0 bytes** (verified: `wc -c` → `0`), which
means Python can complete the package `__init__` before any submodule import and the reverse edge
stays soft. Add `from .google_places import GooglePlacesScraper` to that file — the obvious
convenience re-export, and precisely what `main.py:25-27` does by hand today — and the same code
becomes a hard `ImportError`. `382` reaches the same conclusion from `_fetch_website`'s side; I am
re-confirming it and adding the reproduction.

**Fix (one change, all three symptoms):** `src/leadminer/geo.py` holding `REGION_KEYWORDS`,
`_REGION_MAP`, `_LB_COORD_REGIONS`, `_KSA_COORD_REGIONS`, `infer_region` and a `Country` Literal —
imported by `enricher` *and* by `scrapers.google_places`, both as `from ..geo import …`. `geo` then
has no project imports, so the cycle is structurally impossible rather than accidentally absent, and
the S1 layering violation, the S2 dead-`requests` cost, and the S3 wasted-regex cost all disappear
with the same move.

---

## Not a bug, but worth knowing

- **`infer_region` itself is clean.** It is a pure function of four module-level tables and its
  arguments, it has no I/O, no clock, no randomness, and no global mutation. It extracted into a
  namespace containing only `re` and executed correctly on all five probes above. I found **no
  dependency defect inside the function body**. Every finding in this report is about the module it
  happens to live in, or about callers. That is the single most useful thing to know before changing
  anything: the fix is a file move, not a logic change.
- **The `enricher.py:128` mid-file import is `E402`-flagged by the project's own linter**
  (`pyproject.toml:46` selects `E`) and is 128 lines below the real import block, sitting under a
  4-line changelog comment (`:124-127`). For `infer_region` specifically it is pure noise: it is the
  reason a string matcher needs `httpclient`. `382` covers the `E402` mechanics; do not move that
  import without moving the comment that explains it.
- **`pyproject.toml:35-37` under-ships and says so.** The comment states the requirement and the
  setting violates it: an explicit `include` replaces hatchling's default file selection, so the
  wheel would contain `scrapers/` only — not `enricher.py`, not `httpclient.py`. I could not build to
  confirm (hatchling is not installed and the brief forbids installing); the directive is quoted
  verbatim and unambiguous. *(I cannot confirm that `hatchling` even resolves a package here: with
  project name `leadminer` and no `leadminer/` directory and no `src/`, its default file selection
  has no candidate to find. This needs a real `pip install .` to settle and is adjacent to, not
  inside, this lens.)*
- **Import-time cost is paid once, not per call.** The 8.65 ms to build `_REGION_MAP` and the 61
  modules of import cost are per-process. At `main.py:160` (3 scraper threads) and
  `enricher.py:237` (40 workers) this is noise. It is only worth fixing because it is what forces the
  layering violation and the test stub — not because 8.65 ms matters.
- **Ordering is a real dependency on list order, and it is undocumented.** `_REGION_MAP` (`:52-55`),
  `_LB_COORD_REGIONS` (`:58-68`) and `_KSA_COORD_REGIONS` (`:70-76`) are consumed by
  first-match-wins loops (`:86-88`, `:91-93`). The only ordering hint in the file is the comment at
  `:59` — `most-specific-first` — attached to the one list where it does not hold (Beirut, the most
  specific, is first; that is what works). `_REGION_MAP`'s order comes from `REGION_KEYWORDS`, and
  `Nabatieh` at `:38-41` duplicates `nabatieh`, `النبطية`, `bint jbeil`, `hasbaya`, `marjayoun`
  from `South Lebanon` at `:33-37` — so `infer_region('Main Square, Nabatieh, Lebanon', None, None)`
  returns `'South Lebanon'`, verified. Sorting or de-duplicating either list silently reclassifies
  real leads, and nothing — no test, no comment, no type — records that the order is load-bearing.
  This is correctness rather than dependency, but it is the reason the tables must not be casually
  moved into a config file where a formatter might reorder them.
- **`infer_region` has one test.** `tests/test_lead_signal.py:147-153`, a single assertion that a
  `country="LB"` address resolves to `Beirut` — and it reaches past `enrich()` to call
  `infer_region` directly. `docs/audits/031-test-suite-design.md:98-99` and
  `087-SYNTH-test-gap.md:628` both specify the missing suite. From the dependency side, the test that
  matters most is the one nobody wrote: **an assertion that `geo.py` imports nothing but `re`**
  (e.g. `leadminer.geo` has no project import edges). That single invariant would have caught S1,
  S2 and S3 at once, and it is cheap to write.

---

## Recommended order of work

1. **S1 + S2 + S3, one move.** Create `src/leadminer/geo.py` containing `enricher.py:10-94`
   verbatim plus `Country = Literal["LB", "SA"]`. Repoint `enricher.py:332`,
   `scrapers/google_places.py:26` and `tests/test_lead_signal.py:66` at `from ..geo import …`.
   Make `scrapers/google_places.py:275` emit `region=None` and let `enrich()` compute it. This
   deletes the only reverse edge in the package, removes 61 modules from the scraper's import, and
   makes the src-layout cycle structurally impossible rather than accidentally absent. Add
   `leadminer/geo.py`'s has-no-project-imports assertion in the same commit.
2. **S2 (`country` contract)** — introduce the `Country` Literal, derive KSA-ness from coordinates
   rather than the query string, and change `enricher.py:334` to use `resolve_country` so a
   `country=None` master row cannot silently disable address inference. Fix
   `tests/test_lead_signal.py:147-153` to exercise `enrich()` rather than routing around it.
3. **S2 (dead imports)** — delete `enricher.py:2` and `enricher.py:4`, then delete
   `_stub_requests()` and the `# noqa: E402` chain at `tests/test_lead_signal.py:65-68`. Zero-risk;
   clears 3 of the 8 ruff errors `382` lists. Do this after step 1 so the stub's removal is not
   needed to unblock the move.
4. **S3 (annotations)** — add `from __future__ import annotations` to `enricher.py`; parameterise
   `re.Pattern` at `enricher.py:52`; run the `094-infer-region-correction.md:276` change that lets
   the address branch see SA, now that the caller only computes region once.
5. **Instrumentation** — give the new `geo.py` a `log.debug` on a non-first box hit and a counter on
   the `None` return. The overlap ambiguity documented in `007`/`087` is currently unobservable, and
   that is why it has survived.
6. **Then** the src move proper: `pyproject.toml:35-37` → `packages = ["leadminer"]`, all eight
   intra-project imports relative, `scrapers/__init__.py` re-exports added in the *same* commit —
   never leave the 0-byte file as the thing preventing the cycle.
