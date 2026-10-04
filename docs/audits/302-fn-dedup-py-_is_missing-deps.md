# 302 — `_is_missing` is a leaf with no dependencies and a contract that three other modules silently disagree with

**Target:** `dedup.py:79` `_is_missing(value) -> bool`
**Lens:** deps — transitive dependency map, surprising edges, layering violations, src-layout cycle risk, type-only / logging-only imports.

## Verdict

The dependency surface of `_is_missing` itself is as clean as it gets: **three builtins
(`isinstance`, `str`, `.strip`) and nothing else.** `dedup.py` has zero project-local imports
and zero third-party imports; it is a leaf of the import graph, and I confirmed by AST analysis
that there is no cycle today and none under the src-layout fix already recommended in
`docs/audits/020-type-contract-import-cycle.md` S12.

The problem is not what `_is_missing` depends on. It is **what depends on it, and how little
that dependency is worth.** `_is_missing` is the only definition of "missing" in a codebase that
makes ~114 independent present/absent decisions across five modules, using at least six mutually
incompatible definitions. It is `private`, so no other module *can* reuse it. And because it is an
emptiness test rather than a validity test, it is blind to the exact class of junk the merge is
supposed to survive — which produces a real S1: a junk-but-non-empty OSM tag permanently displaces
a real website URL from Google, because `_pick` compares **trust before validity**
(`dedup.py:180-185`) and `_is_missing` is the only gate that could have stopped it.

---

## Dependency map

Transitive closure of `_is_missing` (`dedup.py:79`), computed by AST:

```
_is_missing(value) -> bool                                    dedup.py:79
├── value            (parameter, NO annotation — see S4)      dedup.py:79
├── isinstance       builtin                                   dedup.py:88
├── str              builtin                                   dedup.py:88
└── value.strip()    str method                                dedup.py:89
```

**Direct dependencies: 0 project modules, 0 third-party modules, 0 stdlib modules, 0 globals.**
It does not even reference a module-level constant. This is the smallest possible closure.

**Who depends on it** (only 3 call sites, all inside `dedup.py`):

```
_validity(field, value) -> int          dedup.py:120  ──▶ _is_missing(value)   dedup.py:122
_pick(field, a, b)        -> object     dedup.py:159  ──▶ _is_missing(av)      dedup.py:168
                                                        └▶ _is_missing(bv)      dedup.py:170
```

**One level up — the modules whose globals `_pick` reaches.** These are the real dependency
surface of `_is_missing`'s *only* consumer:

| Global / function | Where | Used for |
|---|---|---|
| `_SOURCE_TRUST` | `dedup.py:97-110` | per-field, per-source trust scores |
| `_DEFAULT_TRUST` | `dedup.py:111` | fallback trust = 1 |
| `_VOLATILE` | `dedup.py:114` | recency tiebreak for `{rating, review_count, website_live, website}` |
| `_EMAIL_OK` | `dedup.py:116` | regex, `_validity` |
| `_URL_OK` | `dedup.py:117` | regex, `_validity` |
| `_sources_of` | `dedup.py:143` | splits `record["source"]` on `"\|"` |
| `_trust_for` | `dedup.py:148` | max trust across a record's sources |
| `_recency` | `dedup.py:155` | `record["scraped_at"]` |
| `rank` (closure) | `dedup.py:179-185` | `(trust, validity, recency, str(value))` |
| `_merge` | `dedup.py:190` | iterates `set(a) \| set(b)` — **every key, whatever its type** |
| `dedup` | `dedup.py:197` | the only public entry point |

**Module-level graph (AST-verified, project-local edges only):**

```
main            ─▶ dedup, enricher, pitch_recommender, scrapers.*
cli             ─▶ enricher, main, pitch_recommender, scrapers.*   (all function-local)
tests           ─▶ cli, dedup, enricher, main, pitch_recommender
scrapers.google_places ─▶ enricher, httpclient, scrapers.base       ← sideways/up out of the package
enricher        ─▶ httpclient
httpclient      ─▶ (leaf)
dedup           ─▶ (leaf)   ← the target
pitch_recommender ▶ (leaf)
scrapers.base   ▶ (leaf)   ← where BusinessRecord lives
```

`dedup.py` in-degree = 2 (`main.py:29`, `tests/test_lead_signal.py:65`). Out-degree = 0.

---

## Findings

### S1 — A junk-but-non-empty OSM value permanently overwrites a real URL, because `_pick` ranks trust above validity and `_is_missing` only tests emptiness

- **Where:** `dedup.py:79-90` (the gate), `dedup.py:180-185` (the ranking order),
  `dedup.py:98-109` (the trust table that makes it happen), `osm.py:95-96` (the producer)
- **Breaks:** `_is_missing` answers "is this container empty?" — `None`, or a string that is blank
  after `.strip()`. It never asks "is this a value?". `osm.py:95-96` turns any non-empty
  `website`/`contact:website`/`url` tag that does not start with `http` into `"https://" + tag`,
  which is *always* non-empty. So the tag escapes `_is_missing`, reaches `rank()`, and
  `_validity` correctly scores it `0` — but `rank()` is
  `(trust, validity, recency, str(value))`, so **trust is compared first**. OSM's website trust
  is `3` (`dedup.py:103`) against Google's `2` (`dedup.py:100`). `3 > 2` decides the field before
  validity is ever examined. The junk wins permanently and is written to the cumulative master,
  where the next run re-merges it and re-loses.
- **Trigger:** an OSM record and a Google record for the same business, same key:

  ```python
  google = {"name":"Cafe","source":"google_places","website":"https://maps.example/Cafe"}
  osm    = {"name":"Cafe","source":"osm","website":"https://-"}   # tag was literally "-"
  _merge(google, osm)["website"]  ->  'https://-'      # the real URL is gone
  ```

  Verified for five realistic OSM tags, all losing the real URL:

  | raw OSM tag | after `osm.py:95-96` | `_is_missing` | google trust/validity | osm trust/validity | merged `website` |
  |---|---|---|---|---|---|
  | `"-"` | `https://-` | `False` | 2 / 3 | 3 / 0 | `https://-` |
  | `"n/a"` | `https://n/a` | `False` | 2 / 3 | 3 / 0 | `https://n/a` |
  | `"   "` | `https://   ` | `False` | 2 / 3 | 3 / 0 | `https://   ` |
  | `"."` | `https://.` | `False` | 2 / 3 | 3 / 0 | `https://.` |
  | `"TODO"` | `https://TODO` | `False` | 2 / 3 | 3 / 0 | `https://TODO` |

  Every one of those is truthy, so `main.py:199` (`with_websites`) claims the business **has** a
  site and `main.py:200` (`without_websites` — the "new-site pitch target" product) does not
  contain it. `enricher.check_websites` (`enricher.py:231`) then tries to GET `https://-`.
- **Also hits `email`:** `_SOURCE_TRUST["osm"]["email"] == 3` (`dedup.py:103`), and
  `enricher.py:252` guards with `if not r.get("email")`. A merged `email` of `"-"` is truthy, so a
  **real address scraped from the business website is thrown away** in favour of a placeholder:
  `_merge({"source":"google_places","email":None}, {"source":"osm","email":"-"})["email"] == "-"`.
- **Fix:** reorder `rank()` to `(validity, trust, ...)` — validity is a *correctness* filter, trust
  is a *preference*, and the tuple currently encodes the opposite priority — and teach
  `_validity`/`_is_missing` to reject the normalized-placeholder family
  (`https://-`, `https://n/a`, `https://TODO`, `https://.`), or stop `osm.py:95-96` from prefixing
  a value that is not a plausible host.

### S2 — The one definition of "missing" is private, so all five other modules invent their own, and they disagree on `"   "`

- **Where:** `dedup.py:79` (the definition); the disagreeing copies at `main.py:59-61`,
  `main.py:137-145`, `main.py:199-201`, `enricher.py:103-116`, `enricher.py:231`,
  `enricher.py:252-259`, `enricher.py:288-294`, `pitch_recommender.py:31-53`, `cli.py:96`,
  `cli.py:107-110`, `cli.py:151-154`
- **Breaks:** `dedup.py` has no logger, no shared contract, and `_is_missing` is private, so
  nothing outside `dedup.py` can reach it. I counted **114** present/absent decisions across
  `dedup.py` (19), `enricher.py` (44), `main.py` (21), `cli.py` (19),
  `pitch_recommender.py` (11) — and at least **six** different semantics. The most damning
  duplicate is 20 lines from `main.py:29`'s own `from dedup import dedup, normalize_phone`:
  `main.py:59-61` reimplements `_is_missing` verbatim inline as
  `isinstance(value, str) and value.strip()`. Verified identical on `None`, `""`, `"   "`,
  `" "`, `"x"`.
- **Trigger — the divergence, concretely.** One record, every field three spaces (reachable from
  the CSV path: `main.py:88` converts only `row[k] == ""` to `None`, so a `"   "` cell survives
  the round trip):

  | Consumer | Predicate | Verdict on `"   "` |
  |---|---|---|
  | `dedup._is_missing` (`dedup.py:88-89`) | `not value.strip()` | **missing** |
  | `main.resolve_country` (`main.py:60`) | `isinstance(str) and strip()` | **missing** |
  | `enricher.completeness_score` (`enricher.py:103-116`) | truthiness of `.get()` | **present** — returns `7` of 7 |
  | `enricher.lead_score` (`enricher.py:288-294`) | truthiness | **present** — returns `53` |
  | `main.py:199` `bool(r.get("website"))` | truthiness | **present** |
  | `main.has_any_contact` (`main.py:138`) | `len(re.sub(r"\D","","   ")) >= 7` | **missing** |

  So `enricher.completeness_score` awards a perfect `7` and `lead_score` awards `53` to a record
  whose every field is blank-but-not-empty, while dedup correctly throws the value away during
  the merge. The two systems disagree about the same bytes in the same run.
- **Fix:** promote `_is_missing` to a shared, public `is_blank()` in a dependency-free module
  (`contracts.py` or a new `values.py`) and route all 114 sites through it. This is a pure
  dependency-inversion change; it needs no behaviour change anywhere except the six predicates
  that are currently wrong.

### S2 — Tri-state merge precedence is an undocumented side effect of `str()` alphabetical order, and it inverts under the status-string domain another audit already recommends

- **Where:** `dedup.py:184` (`str(value),  # deterministic final tiebreak`), `dedup.py:114`
  (`_VOLATILE` includes `website_live`), `dedup.py:168-171` (the `_is_missing` short-circuit),
  `enricher.py:154-156` (the competing string constants)
- **Breaks:** for `website_live` the correct precedence — live beats dead, and both beat unreachable
  — is produced by two accidents that happen to agree: (a) `_is_missing(None) is True`
  (`dedup.py:86-87`) short-circuits at `dedup.py:168-171` before `rank()` ever runs, giving
  live/dead a `+100` over unreachable; and (b) `"False" < "True"`, so `str(True)` wins the
  tiebreak. Neither is written down and neither is enforced. The moment the value domain changes,
  both disappear.
- **Trigger:** `docs/audits/020-type-contract-import-cycle.md` S7 recommends widening
  `website_live` to `Literal["live","dead","blocked","error"] | None`. That is the right call for
  the product, and it silently breaks the merge. Verified:

  | today (`bool \| None`) | winner | proposed (`str`) | winner |
  |---|---|---|---|
  | `True` vs `None` | `True` | `"live"` vs `"unknown"` | **`"unknown"`** |
  | `False` vs `None` | `False` | `"live"` vs `"dead"` | `"live"` |
  | `False` vs `True` | `True` | `"live"` vs `"blocked"` | `"live"` |
  | | | `"live"` vs `"error"` | `"live"` |

  `_is_missing("unknown")` is `False` — a non-empty string — so the `+100` penalty at
  `dedup.py:122-123` never fires, and `"unknown" > "live"` decides. **An unreachable site would
  permanently outrank a live one**, which is the exact signal inversion
  `docs/audits/043-scoring-upgrade.md` exists to prevent.
- **Fix:** make precedence explicit rather than emergent — either an explicit per-field
  preference table consulted before the `str(value)` tiebreak, or give `website_live` a numeric
  rank. Do not ship 020 S7's `Literal` change without doing this first.

### S3 — `_is_missing`'s real contract is an undeclared dependency on `scrapers/base.py:5-28`, and the project's strict type checker cannot see it

- **Where:** `dedup.py:79`, `dedup.py:120`, `dedup.py:159`, `dedup.py:167`, `dedup.py:190-194`;
  contract at `scrapers/base.py:5-28`; checker config at `pyproject.toml:64-68`
- **Breaks:** `_is_missing` accepts any object and makes a per-field decision based on its runtime
  type, but `dedup.py` imports nothing — so the invariant "every value reaching `_is_missing` is
  `str | None | float | int | bool`" lives only in `scrapers/base.py:5-28` and only in the
  caller's head. `_pick` applies it to **every key of `set(a) | set(b)`** (`dedup.py:192`), so any
  helper-added column or CSV-derived type is judged by a str-shaped rule with no declaration.
  `dedup.py` is also the only module in the package with **zero** type annotations on parameters
  (`_is_missing(value)`, `_validity(field, value)`, `_pick(field, a, b) -> object`, and an inner
  `rank(...) -> tuple` with no type parameters at `dedup.py:179`). `pyproject.toml:66` sets
  `strict = true`; under it every one of those is an error. The module that decides which data
  survives is the one the type checker cannot check.
- **Trigger:** add `notes` to the master CSV. `_merge` (`dedup.py:190`) carries it through
  unjudged, `_is_missing` classifies it by runtime type, and mypy sees none of it.
- **Fix:** `_is_missing(value: object) -> bool` and `def rank(...) -> tuple[int, int, str, str]`;
  then, once the contract moves out of `scrapers/` (020 S12), annotate `_pick`/`_merge` as
  `BusinessRecord` so the real dependency edge becomes visible instead of implicit.

### S3 — Dead and stale imports that distort the dependency graph and one of them is the reason `enricher` cannot be imported in a bare checkout

- **Where:** `enricher.py:2`, `enricher.py:4`, `enricher.py:128`, `enricher.py:327-328`,
  `enricher.py:175`, `scrapers/osm.py:1,7`, `scrapers/wikidata.py:1,7`,
  `tests/test_lead_signal.py:23-63`
- **Breaks:** AST-verified unused-import list for the whole repo:

  | Site | Import | Status |
  |---|---|---|
  | `enricher.py:2` | `requests` | **dead** — `enricher.py` fetches via `get_session()` (`enricher.py:175`); zero `requests.` references |
  | `enricher.py:4` | `urljoin`, `urlparse` | **dead** — zero references |
  | `enricher.py:327-328` | `urllib3` | **dead** — only used to `disable_warnings(InsecureRequestWarning)` while `enricher.py:175` runs with default `verify=True` (the comment at `enricher.py:170` confirms `verify=False` was removed) |
  | `scrapers/osm.py:1,7` | `logging` + `log` | **logger created, never used** |
  | `scrapers/wikidata.py:1,7` | `logging` + `log` | **logger created, never used** |
  | `cli.py:46` | `BaseScraper` | intentional (`# noqa: F401 (import check)`) |

  The `enricher.py:2` one has a concrete cost. In a checkout with no dependencies installed:

  ```
  $ python3 -c "import enricher"
  ModuleNotFoundError: No module named 'requests'
  ```

  …for a module that does not use `requests`. That single dead import is the entire reason
  `tests/test_lead_signal.py:23-63` carries a **41-line `_stub_requests()` shim** that
  hand-builds a fake `requests` and `urllib3` module tree. The test suite's largest piece of
  scaffolding exists to work around an import nobody needs.
- **Also worth noting:** `enricher.py:128` is a **mid-module** import
  (`from httpclient import DEFAULT_MAX_BODY_BYTES, get_session`) sitting 127 lines below the top
  of the file, with no comment explaining the placement. `enricher.py` has no
  `from __future__ import annotations`, so there is no deferral reason for it. It is harmless but
  it makes the dead `requests` above it look deliberate.
- **Fix:** delete `enricher.py:2` and the `urllib3` block at `enricher.py:327-328` (keep the local
  import only if you actually want the suppression), delete `enricher.py:4`, move `enricher.py:128`
  to the top, delete the unused `log` in both scrapers, and delete the `_stub_requests()` shim.

### S3 — `dedup.py` is the only module that silently discards data and the only one with no logger

- **Where:** `dedup.py` (whole file), `dedup.py:190-194`; compare `httpclient.py:45`
- **Breaks:** observability census:

  | Module | has logger | `log.*` calls | `print()` calls |
  |---|---|---|---|
  | `dedup.py` | **no** | 0 | **0** |
  | `pitch_recommender.py` | no | 0 | 0 |
  | `enricher.py` | no | 0 | 4 |
  | `httpclient.py` | yes | 2 | 0 |
  | `scrapers/osm.py` | yes | **0** | 3 |
  | `scrapers/wikidata.py` | yes | **0** | 3 |
  | `main.py` | no | 0 | 19 |

  `dedup._merge` is the only place in the codebase where a field value is chosen away from an
  existing record and never reported. `_is_missing` is the predicate that makes that decision
  (`dedup.py:168-171`) and it returns a bare `bool` with no record of what it displaced. `main.py`
  prints a count of surviving records (`main.py:181`) and nothing about what was lost, so the S1
  above is undiagnosable after the fact from any artifact the pipeline produces.
- **Fix:** `log = logging.getLogger(__name__)` at the top of `dedup.py` and emit one summary line
  per run from `_merge`: `kept=<field> dropped=<n> reason=<trust|validity|recency>`. At 1.4k LOC
  this is the cheapest observability in the repo, and it is the only thing that would have caught
  S1 in production.

---

## Not a bug, but worth knowing

- **`dedup.py` is a genuine leaf and there is no circular-import problem today.** Verified by AST
  on all 14 project modules: `dedup.py` has zero project-local and zero external imports (only
  `re` and `unicodedata`, both stdlib), and the full graph is acyclic. State this plainly rather
  than manufacturing a cycle that is not there — `dedup.py` is the cleanest module in the repo.

- **The src-layout fix *removes* the latent cycle rather than creating one.** I modelled the
  change `docs/audits/020-type-contract-import-cycle.md` S12 recommends (move `BusinessRecord` into
  a top-level `contracts.py`) plus letting `contracts` own `LIVE`/`DEAD`/`UNKNOWN`, which is the
  natural home for them. Result: **still acyclic.** `enricher → contracts` is a legal edge;
  `contracts` stays a leaf.

- **There *is* one reachable cycle, and it comes from doing the same fix the other way.** If the
  contract is annotated in place instead of relocated — `dedup → scrapers.base` and
  `enricher → scrapers.base`, both recommended by 020 S2 — and `BaseScraper` is then given a
  `dedup_key()` method (the natural next step for `095-dedup-strategy-v2`), then:

  ```
  scrapers.base ─▶ dedup ─▶ scrapers.base     # CYCLE
  ```

  Computed and confirmed. Note also that `scrapers/google_places.py:26 → enricher` already exists
  in the tree, so `enricher` is not a free agent. The takeaway for whoever implements 020 S12:
  **relocate the contract, do not import it from `scrapers/`.** One decision, done now, prevents a
  cycle that is otherwise three small steps away.

- **`_is_missing` classifies `nan` and `inf` as present, and `_validity` scores them 3.** Verified:

  ```
  _is_missing(nan)                -> False
  _validity('lat', nan)           -> 3      # float('nan') succeeds, dedup.py:130
  _validity('lat', inf)           -> 3
  _is_missing('0')                -> False
  _is_missing([])                 -> False
  _is_missing({})                 -> False
  ```

  `main.py:92-95` does `float(row[field])`, so a master cell literally containing `nan`, `NaN`,
  `inf` or `Infinity` becomes a real float and is treated as a valid coordinate forever.
  `cli.cmd_validate` does catch it (`cli.py:166-172`, `in_box(nan, ...)` is False), which is the
  only reason this is filed as S3 rather than higher — but `validate` is a manual gate, not part
  of the run.

- **`_SOURCE_TRUST` (`dedup.py:97-110`) is a fourth hand-maintained declaration of the schema.**
  Verified against the contract and the CSV header: it rates 13 of 23 fields. The 10 unrated keys
  (`country`, `region`, `website_live`, `source`, `scraped_at`, `linkedin`, `lead_score`,
  `completeness_score`, `industry_priority`, `recommended_service`) all fall to
  `_DEFAULT_TRUST = 1`. Two of those (`source`, `scraped_at`) are special-cased in `_pick`
  (`dedup.py:173-177`); `website_live` is correctly unrated because it is computed, not scraped.
  The remaining seven are correct-by-luck. Adding a field means editing
  `scrapers/base.py`, `main.FIELDS`, `_SOURCE_TRUST` and possibly `_VOLATILE`, with nothing
  linking them.

- **`_merge` is key-agnostic and `_is_missing` inherits that.** `dedup.py:192` iterates
  `set(a) | set(b)`, so a helper-added `notes` column is carried through *and* judged by a
  str-shaped emptiness rule. `_merge`'s signature is `-> dict` (`dedup.py:190`), not
  `-> BusinessRecord`, so nothing resists.

- **`dedup.py` and `pitch_recommender.py` are the only two modules with neither a logger nor a
  single `print`.** `dedup.py` at least participates in `main.py`'s printed summary
  (`main.py:181`); `pitch_recommender.py` contributes 11 present/absent decisions
  (`pitch_recommender.py:31-53`) and is completely silent.

---

## Recommended order of work

1. **S1** — swap the `rank()` tuple to `(validity, trust, recency, str(value))` at `dedup.py:180-185`.
   Three tokens, and it stops junk from displacing real values in the cumulative master on the very
   next run. Then make `osm.py:95-96` refuse to prefix a value that is not a plausible host.
2. **S2 (tiebreak)** — make the `website_live` precedence explicit *before* anyone implements
   020 S7's `Literal` status change. This is a 20-minute change that prevents a silent inversion
   of the product's #1 signal.
3. **S2 (shared contract)** — promote `_is_missing` to a public `is_blank()` in a dependency-free
   module and route the 114 sites through it, starting with the six in
   `enricher.py`/`main.py` that are demonstrably wrong about `"   "`. This is the change that makes
   every other finding in this report cheaper to write and verify.
4. **S3 (dead imports)** — delete `enricher.py:2`, `enricher.py:4`, `enricher.py:327-328`, the two
   unused `log` objects, and the 41-line `_stub_requests()` shim. Half a day, zero behaviour change.
5. **S3 (logging)** — add a logger to `dedup.py` and report dropped fields. Do this before step 1
   ships so the S1 fix's effect is measurable.
6. **S3 (annotations)** — annotate `dedup.py` and let `pyproject.toml:66`'s `strict = true` do its
   job, once step 3 has given `dedup.py` an honest home for the `BusinessRecord` dependency.