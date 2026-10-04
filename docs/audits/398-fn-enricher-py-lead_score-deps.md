# 398 — `lead_score`: dependency map and layering analysis (lens: deps)

## Verdict

`lead_score` (`enricher.py:283`) is 37 lines of pure dictionary arithmetic with exactly **one**
non-builtin dependency (`re`, `enricher.py:1`). It nevertheless sits inside the one module in the
project that owns a 40-thread HTTP crawler, so `from enricher import lead_score` executes
`enricher.py:2` (`requests`), `enricher.py:4` (two unused `urllib.parse` symbols) and
`enricher.py:128` (`httpclient`, which itself imports `requests` at `httpclient.py:43`) — **94
top-level modules and the whole network stack to load a scoring rule.** Worse, the project's own
test suite has to fabricate a fake `requests` module just to test it
(`tests/test_lead_signal.py:23-25`: *"enricher imports requests at module scope. Stub it if absent
so these tests run in a bare checkout with no dependencies installed."*).

The structural root cause is one misplaced leaf: `lead_score` needs the priority vocabulary
`"high"` / `"medium"`, which is owned by `scrapers/whitelist.py:142-144` — a module with **zero
imports** that has been filed inside the package whose scrapers import `enricher` back
(`scrapers/google_places.py:26`). That makes importing the vocabulary a **circular import**, and I
have reproduced the `ImportError`. It is not hypothetical: at `2026-10-03T20:03Z` a concurrent
implementation agent wrote exactly that cycle into the working tree, and it broke the import of
`lead_score` outright until it was reverted.

Everything below is measured against a pinned snapshot (`enricher.py` sha256
`f980120cd937e24d47f78124db10c028cfb8d1592577e69491120cde64df5b5c`) because the working tree
changed twice while this audit was running. See *Snapshot integrity* at the end.

---

## Dependency map

### 1. Module dependencies of `from enricher import lead_score`

Everything in this table runs at **import time**, before a single line of `lead_score` executes.

| Line | Dependency | Used by `lead_score`? | Who actually uses it |
|---|---|---|---|
| `enricher.py:1` | `re` | **yes** (`:285`) | many |
| `enricher.py:2` | `requests` | no — **dead** | nobody in the file |
| `enricher.py:3` | `concurrent.futures` | no | `check_websites` (`:237`, `:240`) |
| `enricher.py:4` | `urllib.parse.urljoin` | no — **dead** | nobody |
| `enricher.py:4` | `urllib.parse.urlparse` | no — **dead** | nobody |
| `enricher.py:52-55` | `_REGION_MAP` — 8 compiled regexes, 114 keyword alternatives | no | `infer_region` (`:86`) |
| `enricher.py:58-76` | 13 coordinate boxes | no | `infer_region` (`:90-92`) |
| `enricher.py:128` | `from httpclient import DEFAULT_MAX_BODY_BYTES, get_session` — **mid-file import, 128 lines below the top** | no | `_fetch_website` (`:175`, `:188`) |
| `enricher.py:130-136` | 4 more compiled regexes | no | `_fetch_website` |
| `enricher.py:138-142` | `_EMAIL_BLACKLIST`, `_IG_BLACKLIST` | no | `_fetch_website` |
| `enricher.py:158` | `_MAX_BODY_BYTES = DEFAULT_MAX_BODY_BYTES` | no | `_fetch_website` |
| `enricher.py:327` | `import urllib3` (function-local, inside `enrich`) | no | nothing — see F7 |

Transitive closure pulled in by the single import:

```
lead_score
└─ enricher                          (enricher.py)
   ├─ re                             (enricher.py:1)
   ├─ requests            [DEAD]      (enricher.py:2)
   ├─ concurrent.futures             (enricher.py:3)
   ├─ urllib.parse        [DEAD x2]   (enricher.py:4)
   └─ httpclient                     (enricher.py:128)
      ├─ requests                    (httpclient.py:43)
      ├─ urllib3                     (httpclient.py:43, via requests)
      ├─ logging / random / threading / time / email.utils
      ├─ contextlib / dataclasses / typing
      └─ _local = threading.local()  (httpclient.py:58)  <- module-level mutable global
```

Measured: **94 non-dunder modules loaded; `httpclient` loaded; `requests` + `urllib3` loaded.**
The region-regex compilation I initially suspected as the cost is negligible — **0.11 ms** for all
8 patterns. The cost is the module graph, not the regexes.

**Notably absent** (good): `lead_score` has **no** dependency on `dedup`, `scrapers`,
`pitch_recommender`, `main`, or `cli`. Its module-level footprint is correctly limited to `re`.

### 2. Data dependencies — the 9 record keys `lead_score` reads

`lead_score` has no Python-level coupling to its collaborators. All of its coupling is
**stringly-typed, via dictionary keys on a bare `dict`**. Every row below is a cross-module
contract with no schema, no enum, no shared constant and no type gate (`lead_score(record: dict)`,
`enricher.py:283` — not `BusinessRecord`, even though `scrapers/base.py:5-28` defines one).

| Key | Read at | Producer | Declared type | Guarded on the consumer side? |
|---|---|---|---|---|
| `phone` | `:285`, `:292` | `osm.py:89`, `wikidata.py:97`, `google_places.py:289`; re-normalized `main.py:194` | `str \| None` (`base.py:13`) | yes — `str(... or "")` |
| `email` | `:288` | `osm.py:90`, `wikidata.py:98`, `enricher.py:203` | `str \| None` | yes — truthiness |
| `whatsapp` | `:290` | `enricher.py:214` only | `str \| None` | yes — truthiness |
| `instagram` | `:294` | `osm.py:93`, `enricher.py:209` | `str \| None` | yes — truthiness |
| `website` | `:303` | 3 scrapers + `main.py:101` | `str \| None` | yes — truthiness |
| `website_live` | `:300-303` | `enricher.py:250`; **string→bool decoded at `main.py:103`** | `bool \| None` | yes — `is True` / `is False` |
| `industry_priority` | `:306` | `scrapers/whitelist.py:142-144` | `str \| None` (`base.py:23`) | **no** — bare literals `:307`, `:309` |
| `rating` | `:312-313` | `google_places.py:297` | `float \| None` (`base.py:21`) | **no** — bare `<` comparison |
| `source` | `:316` | `dedup.py:174` (`"\|".join(...)`) | `str` | **no** — bare `"|" in str(...)` |

### 3. Callers (the other half of the contract)

| Caller | Sets `industry_priority` before calling? | Net effect |
|---|---|---|
| `main.py:197` (`_lead_score`) | yes — `main.py:190` | correct score |
| `cli.py:269` (`cmd_score`) | yes — `cli.py:267` | correct score |
| `enricher.py:350` (inside `enrich`) | **no — key does not exist yet** | **score 15 points low** |

---

## Findings

### S1 — `enricher` ⇄ `scrapers` is a live circular import, and `lead_score` is the function that provokes it

- **Where:** the cycle is `enricher` → `scrapers/<anything>` → `scrapers/google_places.py:26`
  (`from enricher import infer_region`) → `enricher`. Armed by `scrapers/__init__.py`, which today is
  empty (`sha256 e3b0c44…`, i.e. a 0-byte file) but which exists precisely so the scrapers can be
  re-exported. `lead_score` is the victim because the natural fix for F5 below is
  `from scrapers.whitelist import industry_priority`.
- **Breaks:** `import enricher` raises
  `ImportError: cannot import name 'infer_region' from partially initialized module 'enricher'
  (most likely due to a circular import)`. That kills `python main.py`, `leadminer run`
  (`cli.py:35`), `leadminer score` (`cli.py:250`) and the entire test suite
  (`tests/test_lead_signal.py:66`). Nothing degrades gracefully; there is no fallback.
- **Trigger (reproduced, not hypothesised).** I copied the repo to a scratch dir and applied
  candidate patches, importing `enricher` first in a clean subprocess each time:

  | State | Patch | Result |
  |---|---|---|
  | A | pinned snapshot as-is (`scrapers/__init__.py` empty) | `OK: imported lead_score` |
  | B | `+ from scrapers.base import BusinessRecord` at `enricher.py:3` | `OK: imported lead_score` |
  | C | `B` + `scrapers/__init__.py` re-exports the 5 scrapers | **`ImportError: ... partially initialized module 'enricher'`** |
  | D | `C` + `lead_score` imports the priority vocabulary | **`ImportError: ... partially initialized module 'enricher'`** |

  State C is not a contrivance. At `2026-10-03T20:03Z` the live working tree contained exactly
  state C: `enricher.py:3` read `from scrapers.base import BusinessRecord` and
  `scrapers/__init__.py` read
  `from .base import … / from .osm import OSMScraper / from .wikidata import … / from .google_places import … / from .whitelist import …`.
  It was reverted within ~60 s. Two lines, written by a well-meaning agent, made `lead_score`
  unimportable.
- **Under a src layout this gets worse, not better.** `src/leadminer/__init__.py` re-exporting the
  public API (`from .enricher import enrich, lead_score`) is the conventional thing to write, and
  it is fatal: `leadminer/__init__` → `leadminer.enricher` → `leadminer.scrapers.base` →
  `leadminer/__init__` (partially initialised). The `scrapers.google_places → enricher` back-edge
  is what makes it unrecoverable, because a plain lazy import inside the function body is the only
  thing that would survive.
- **Fix:** break the back-edge, not the cycle symptom. `enricher` must never import from
  `scrapers`, and `scrapers` must never import from `enricher`. Move `infer_region` +
  `REGION_KEYWORDS` + the two coordinate tables (`enricher.py:10-94`) into a leaf module
  (`geo.py`, zero imports) and move `BusinessRecord` out of `scrapers/base.py` into
  `schema.py`. Then both `enricher` and `scrapers` depend on leaves and the graph is a DAG.

### S2 — A pure scoring function cannot be imported without the HTTP stack, and its `requests` import is dead

- **Where:** `enricher.py:2` (`import requests`), `enricher.py:4` (`urljoin`, `urlparse`),
  `enricher.py:128` (`from httpclient import DEFAULT_MAX_BODY_BYTES, get_session`).
- **Breaks:** `requests` appears **nowhere** in `enricher.py`'s body — only in a comment at
  `:125` and the docstring history. `urljoin` and `urlparse` likewise. `ruff check --select F401
  enricher.py` confirms all three:
  ```
  F401 [*] `requests` imported but unused            --> enricher.py:2:8
  F401 [*] `urllib.parse.urljoin` imported but unused --> enricher.py:4:26
  F401 [*] `urllib.parse.urlparse` imported but unused --> enricher.py:4:35
  I001 [*] Import block is un-sorted                  --> enricher.py:1:1   (caused by the :128 import)
  ```
  The consequence is not cosmetic. `lead_score` is a pure function of a `dict`, yet it cannot be
  imported, tested, or re-used without a working `requests` install. The project has already
  written a workaround: `tests/test_lead_signal.py:23-63` builds a fake `requests` module with a
  stub `Session` and a fake `urllib3`, purely so the scoring tests can run. A pure function whose
  unit tests need an HTTP library faked into existence is a function in the wrong module.
- **Trigger:** `pip install leadminer` into a clean venv, then `python -c "from enricher import
  lead_score"` → `ModuleNotFoundError: No module named 'requests'`. Or, more practically, the next
  agent who touches `enricher.py` and reorders those imports breaks the test suite.
- **Fix:** split `enricher.py` into `scoring.py` (`lead_score`, `completeness_score` — zero imports
  but `re`) and `enrich.py` (`infer_region`, `check_websites`, `_fetch_website`). Delete
  `enricher.py:2` and `enricher.py:4`; move `enricher.py:128` to the top of the file.

### S2 — `enrich()` calls `lead_score` before `industry_priority` exists, so it writes a score that is 15 points low

- **Where:** `enricher.py:350` (`r["lead_score"] = lead_score(r)`) inside `enrich`; the read is
  `enricher.py:306`. `enrich()` never `setdefault`s `industry_priority` (compare
  `enricher.py:340-348`, which sets defaults for nine other keys but not this one).
- **Breaks:** `lead_score` has an **undocumented temporal precondition** — `industry_priority` must
  already be in the record — that is satisfied only by a caller three modules away. `enrich()` is
  that caller's *predecessor*, so it violates the precondition every time. Measured:

  ```python
  recs = [{"name": "Cafe Beirut", "category": "cafe", "country": "LB",
           "phone": "70123456", "address": "Hamra, Beirut",
           "source": "osm|google_places"}]
  out = enrich([dict(r) for r in recs])
  # out[0]["lead_score"]                    -> 20
  # "industry_priority" in out[0]           -> False
  # lead_score(out[0] after priority set)   -> 35
  # understatement: 15 points, i.e. the entire high-priority bonus (enricher.py:307-308)
  ```
  This is not currently visible in the product: `main.py:197` recomputes the score after
  `main.py:190` sets the priority, and `main.py:195-196` says so in a comment
  (*"enrich() computed lead_score before industry_priority was set, so recompute here"*). But that
  is a **workaround in the wrong layer** — `main.py` now has to know the internal ordering of
  `enrich()`. `enrich()` has exactly one caller today (`main.py:187`), and the CLI is already being
  grown command-by-command (`cli.py:280-309`); the first person to add an `enrich` subcommand, or
  to call `enrich()` from a test, ships wrong scores with no error. Nothing in the suite catches it,
  because `tests/test_lead_signal.py:74` pre-seeds `industry_priority` in its `BASE` fixture and
  never calls `enrich()`.
- **Trigger:** any record with `category` in `PRIORITY_INDUSTRIES` (`whitelist.py:17-42`) read
  between `enrich()` returning and `main.py:197` running — e.g. a future `leadminer enrich` that
  writes CSVs, or any log/print placed after `main.py:187`. 15 points low, silently.
- **Fix:** make `lead_score` total. Either move the `industry_priority` assignment into `enrich()`
  (before `:350`), or drop `enricher.py:350` entirely so there is exactly one place that computes
  the score and it is downstream of all its inputs.

### S2 — `leadminer score` re-applies today's weighting to yesterday's `website_live`, whose semantics were redefined

- **Where:** `cli.py:247-277` (`cmd_score`) → `lead_score` at `cli.py:269`, reading `website_live`
  decoded from CSV at `main.py:103`. The semantics are declared at `enricher.py:145-156`.
- **Breaks:** `website_live` is not a boolean, it is a **three-state encoding whose meaning
  changed**. `enricher.py:145-153` is explicit that conflating LIVE/DEAD/UNKNOWN *"was the single
  worst bug in the pipeline (see docs/audits/043-scoring-upgrade.md)"*, and
  `docs/audits/043-scoring-upgrade.md` records the old behaviour as: *"`except Exception: return
  False` … any business that blocks the crawler … is stamped `website_live = False` and awarded a
  **+20 point dead-site bonus**"*. So every `all_businesses.csv` written before that fix is full of
  `website_live=False` values that actually mean **"we never reached the host"**.
  `leadminer score` is advertised as *"Re-score an existing CSV with current rules, without
  scraping"* (`cli.py:248`) and it re-derives only the **rule**, never the **inputs**. It will apply
  the corrected `enricher.py:303-304` weighting (`live is False and record["website"]` → `+20`,
  commented *"server-confirmed dead site = rebuild pitch"*) to records whose `False` was never
  server-confirmed, and print a reassuring `N changed` count as if it were an improvement. The
  export carries no `score_version` or `probe_version` column, so nothing can detect the mismatch.
  043 explicitly recommended *"strict semantic versioning (`score_version`)"*; it was not
  implemented.
- **Trigger:** `leadminer score data/all_businesses.csv` on a master produced by the pre-043 code.
  Every Cloudflare-blocked or timed-out site — the ones `enricher.py:176-178` and `:190-192` now
  correctly classify as UNKNOWN — gets `+20` "server-confirmed dead" points and rises to the top of
  the sales list as rebuild pitches.
- **Fix:** add a `probe_version` (or `score_version`) column written by `check_websites`, and have
  `cmd_score` refuse — or loudly warn and skip the `website_live` term — when the CSV's version
  predates the current `LIVE`/`DEAD`/`UNKNOWN` contract.

### S2 — `rating` is compared with no coercion and no guard: the only unguarded consumer of a field all three siblings guard

- **Where:** `enricher.py:312-314`:
  ```python
  rating = record.get("rating")
  if rating is not None and rating < 4.0:
      score += 10
  ```
- **Breaks:** `lead_score` is the **only** consumer in the project that trusts `BusinessRecord`'s
  `rating: float | None` declaration (`base.py:21`) without re-checking it. Every sibling wraps it:

  | Consumer | Code | Behaviour on `rating="3.5"` |
  |---|---|---|
  | `lead_score` | `enricher.py:313` | **`TypeError: '<' not supported between instances of 'str' and 'float'`** |
  | `recommend_service` | `pitch_recommender.py:135-140` (`_is_low_rating`, `float()` in `try`) | returns a pitch normally |
  | `dedup._validity` | `dedup.py:134-139` (`float()` in `try`) | returns `3`, treats it as valid |
  | `main.load_master` | `main.py:90-95` (`_FLOAT_FIELDS`, `float()` in `try`) | `None` |

  A `TypeError` here is not caught. `enricher.py:350` is inside `enrich`, inside
  `ThreadPoolExecutor`-free straight-line code, inside `main.main()` — one bad row kills the entire
  multi-hour run with a traceback, after the scrapers have already spent the API budget. And the
  signature `lead_score(record: dict)` (`enricher.py:283`) means no type checker can catch it
  either: `mypy --strict` is configured (`pyproject.toml:64-68`) and `scrapers/base.py:5` defines
  the TypedDict, but `lead_score` declines to use it.
  **Honest scoping:** I could not construct a path through `main.py` or `cli.py` that reaches this
  today — all four producers coerce or the field is `None` (`osm.py:114`, `wikidata.py:122`,
  `google_places.py:297`, `main.py:90-95`). This is a latent trap armed on the seam, not a live
  crash, which is why it is S2 and not S1. It becomes live the moment a source yields a string
  rating (e.g. Wikidata `P3781`, or any `str()` coercion added for tidiness) or the moment
  `load_master`'s `float()` cast is "simplified" away.
- **Trigger:** `lead_score({"name": "X", "rating": "3.5", "website_live": None, "source": "osm"})`
  → `TypeError`. Verified.
- **Fix:** `try: rating = float(record["rating"]) except (TypeError, ValueError): rating = None`,
  and change the signature to `lead_score(record: BusinessRecord)`.

### S2 — The `"|"` multi-source separator is an undeclared contract shared with the merge trust table

- **Where:** producer `dedup.py:174` (`return "|".join(sorted(_sources_of(a) | _sources_of(b)))`),
  consumer `enricher.py:316` (`if "|" in str(record.get("source") or "")`), and the third reader
  `dedup.py:145` (`raw.split("|")`). Three sites, no shared constant, no validation.
- **Breaks:** the separator is load-bearing for **two unrelated systems**, and a single divergence
  damages both. Measured:

  | `record["source"]` | `lead_score` multi-source bonus | `dedup._sources_of` | `_trust_for("rating", …)` |
  |---|---|---|---|
  | `"osm\|google_places"` | **+5** | `['google_places', 'osm']` | **3** |
  | `"osm, google_places"` | **0** | `['osm, google_places']` | **1** |

  So a comma where a pipe belongs does not just cost 5 points — it collapses the entire
  per-field trust table (`dedup.py:97-111`) to `_DEFAULT_TRUST = 1` (`dedup.py:111`), because
  `_trust_for` (`dedup.py:148-152`) looks up `"osm, google_places"` in `_SOURCE_TRUST` and misses.
  Every conflicting field in every subsequent merge is then decided by recency and a
  `str(value)` tiebreak (`dedup.py:179-187`) instead of by source authority.
- **Trigger:** a comma is the separator a human reaches for. `data/all_businesses.csv` is written
  with `utf-8-sig` specifically so Google Sheets renders it (`main.py:118-119`) and the CSVs are
  uploaded to Drive (BRIEF); a rep fixes one `source` cell in Sheets, types `osm, google_places`,
  saves, and the next run silently degrades both the merge and the score. `csv.DictWriter` quotes
  it happily, so nothing errors.
- **Fix:** one module-level constant (`SOURCE_SEP = "|"`) imported by both modules, plus a
  `validate`-time check in `cli.cmd_validate` that every `source` value is in
  `_SOURCE_TRUST.keys()` — the keys are already enumerated at `dedup.py:98-109`.

### S3 — `"high"` / `"medium"` are spelled out in three places, in the wrong direction

- **Where:** `enricher.py:307` and `enricher.py:309`; `main.py:204`
  (`in ("high", "medium")`); the definitions at `scrapers/whitelist.py:142` and `:144`. Plus
  `tests/test_lead_signal.py:74`, `:203`.
- **Breaks:** the scoring policy in the *middle* layer hardcodes a vocabulary owned by the
  *lowest* layer. Any edit to `whitelist.py:142` (say `"high"` → `"top"`) leaves `lead_score`
  scoring every row 15 points low with no error and no test failure, because the test fixture at
  `tests/test_lead_signal.py:74` hardcodes `"high"` too.
- **Fix:** the fix is not to import the constant (see S1 — that is the cycle). Make
  `scrapers/whitelist.py` a top-level leaf `taxonomy.py` with `HIGH = "high"`, `MEDIUM = "medium"`
  at module scope, and move `infer_region` to a second leaf `geo.py`. Then both `enricher` and
  `scrapers` import leaves, the DAG is acyclic, and S1 is fixed as a side effect.

### S3 — `enricher.py:327` imports an undeclared package to silence a warning that can no longer fire

- **Where:** `enricher.py:326-328`:
  ```python
  def enrich(records: list[dict]) -> list[dict]:
      import urllib3
      urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
  ```
- **Breaks:** three things at once. (a) `urllib3` is not a declared dependency —
  `pyproject.toml:16-18` lists only `requests>=2.32.3,<3`; this is an **undeclared direct import of
  a transitive dependency**, reaching through `requests` into its internals. (b) The warning is
  dead: `InsecureRequestWarning` is only emitted when `verify=False`, and `verify=False` appears
  nowhere in the codebase any more — the sole occurrence is the historical note at
  `enricher.py:170` (*"This was `verify=False`"*), and `httpclient.get_session()`
  (`httpclient.py:61-71`) and `fetch_with_retry` (`httpclient.py:149-217`) never set it. (c) It is
  a **function-local import inside the orchestrator**, so the module graph of `enrich` depends on a
  package the project does not declare — if `requests` ever stops vendoring `urllib3`,
  `leadminer run` dies at `enricher.py:327` with a bare `ImportError`.
- **Trigger:** not reachable today. Reachability risk: `requests` vendoring change, or a
  `pip install --no-deps` / vendored-offline install.
- **Fix:** delete both lines. There is no insecure request left to silence.

### S3 — Dead imports and a logger that is constructed but never used

- **Where / evidence:** `ruff check --select F401,F811,F841` over the whole transitive closure of
  `lead_score` returns exactly four hits, three of them in `enricher.py` and all three on the import
  lines that `lead_score` inherits:

  ```
  F401 `requests` imported but unused              --> enricher.py:2:8
  F401 `urllib.parse.urljoin` imported but unused   --> enricher.py:4:26
  F401 `urllib.parse.urlparse` imported but unused  --> enricher.py:4:35
  F401 (unused)                                    --> pitch_recommender.py:37:5  [F841 has_email]
  ```

  Separately, `scrapers/osm.py:1,7` and `scrapers/wikidata.py:1,7` each do
  `import logging` + `log = logging.getLogger(__name__)` and then **never use `log`** — verified,
  `grep -c 'log\.' scrapers/osm.py` = 0 and `grep -c 'log\.' scrapers/wikidata.py` = 0. Both modules
  `print` instead. So logging is configured in the loggers of the two modules that reach the
  network and never used, while `httpclient.py:45` — the module that *does* log
  (`httpclient.py:178`, `:204`) — is the only one that does it correctly. Those two modules are not
  in `lead_score`'s closure, but they are in `enricher`'s (`enricher.py:128`), so they are in the
  blast radius of the S1 cycle.
- **Trigger:** `ruff check .` in CI. Currently 42 errors repo-wide, 8 in `enricher.py`.
- **Fix:** `ruff --fix`; swap `print` for `log.info` in `osm.py`/`wikidata.py` (the CLI parses
  stdout today, so that is a separate piece of work — do it deliberately).

### S3 — `lead_score` is not in the artifact that `pip install` produces

- **Where:** `pyproject.toml:35-37`
  ```toml
  [tool.hatch.build.targets.wheel]
  # The package imports a top-level sibling module, so both must ship.
  include = ["scrapers/"]
  ```
  and `pyproject.toml:33` `leadminer = "cli:main"`.
- **Breaks:** the only file selector is `include = ["scrapers/"]`. Hatchling's default wheel
  selection looks for a `leadminer/` or `src/leadminer/` package directory; neither exists (the
  modules are top-level siblings), so `scrapers/` is the entire artifact. That means the installed
  distribution contains `scrapers/` and **not** `enricher.py`, `dedup.py`, `httpclient.py`,
  `main.py`, `pitch_recommender.py` or `cli.py`. Two consequences:
  1. The `leadminer` console script at `pyproject.toml:33` points at `cli:main`, a module that is
     not shipped → `ModuleNotFoundError: No module named 'cli'` on first invocation.
  2. Even the shipped part is broken: `scrapers/google_places.py:26` does
     `from enricher import infer_region` and `:27` `from httpclient import utc_now_iso`, neither of
     which is in the wheel → `import scrapers` fails in an installed environment.
  And the function this report is about, `lead_score`, is not importable from an installed
  distribution at all. This is squarely a src-layout problem, and it is the problem that a src
  layout will force you to confront.
  *Caveat, stated plainly:* I did not build the wheel — the brief forbids installing packages and
  `hatchling` is not present in any interpreter here. This is a static reading of
  `pyproject.toml:33-37`, not a verified artifact listing. `pip install -e .` (what
  `README.md:80` tells operators to run) hides the problem entirely because editable installs put the
  project root on `sys.path`.
- **Trigger:** `pip install dist/leadminer-0.2.0-py3-none-any.whl && leadminer stats`.
- **Fix:** this report's headline recommendation resolves it — move to `src/leadminer/` with
  relative or `leadminer.`-absolute imports, delete `include = ["scrapers/"]`, and change
  `[project.scripts]` to `leadminer = "leadminer.cli:main"`.

---

## Not a bug, but worth knowing

- **The 7-digit phone rule is duplicated three times — I looked for a divergence and there is none.**
  `enricher.py:292` (`len(phone) >= 7`), `dedup.py:30` (`_MIN_DIGITS = 7`) +
  `dedup.py:45`, and `main.py:142`. I specifically tested whether the two call sites of
  `lead_score` could disagree, because `main.py:194` rewrites `phone` to E.164 *between* the
  `enricher.py:350` call and the `main.py:197` call, so the same business is scored twice from
  different strings. It cannot diverge: `normalize_phone` (`dedup.py:44-46`) uses the identical
  `re.sub(r"\D", "", …)` and returns `""` below 7 digits, and above 7 digits normalisation only
  ever *adds* digits (`dedup.py:63`). Measured — raw `"01 7 123 456"` → 9 digits → `+15`; normalised
  `"+96117123456"` → 11 digits → `+15`; junk `"ext 4812"` → `normalize_phone` returns `""` and
  `lead_score` awards nothing. Three copies of one business rule is still three places to change
  it, but it is **S3 hygiene, not a bug**. Use `dedup._MIN_DIGITS`.

- **`lead_score` and `recommend_service` are siblings that must agree, and nothing ties them
  together.** Both branch on `website_live` (`enricher.py:300-304` vs
  `pitch_recommender.py:34-36,57-58`), on `rating` (`enricher.py:313` vs
  `pitch_recommender.py:73,135-140`) and on `industry_priority` (`enricher.py:307` vs
  `main.py:204`). They share no module. The regression tests that exist
  (`tests/test_lead_signal.py:71-99`) assert the *pair* behave consistently, which is good, but the
  coupling is enforced only by that test file. If the src-layout refactor moves them, keep
  `tests/test_lead_signal.py:66` importing both from the same place.

- **`enricher.py:158` snapshots a configurable constant at import time.**
  `_MAX_BODY_BYTES = DEFAULT_MAX_BODY_BYTES` copies `httpclient.py:54` into a module global, so the
  body cap is no longer overridable per-call without monkeypatching `enricher._MAX_BODY_BYTES`. The
  value is used once, at `enricher.py:188`. Harmless today; do not let it become a second source of
  truth.

- **`main.py:30` imports the function under a private alias for no reason:**
  `from enricher import enrich, lead_score as _lead_score`, while `cli.py:250` imports it plainly.
  Two spellings of the same symbol in two entry points makes grep-based impact analysis harder.

- **`requirements.txt` is stale and lists two packages nothing imports.** It pins
  `beautifulsoup4==4.12.3` and `lxml==5.2.2`; `grep -rn 'bs4\|BeautifulSoup\|lxml'` across every
  `.py` file returns nothing. `pyproject.toml:16-18` correctly dropped them, and
  `README.md:79-80` points operators at `uv pip install -e ".[dev]"`, so `requirements.txt` is now
  a trap for anyone who follows BRIEF's description of the project instead of the README.

- **`httpclient.py:58` creates a module-level `threading.local()` at import time.** Benign, but it
  means `import enricher` has a side effect on the importing thread's heap. Worth knowing before
  anyone imports `lead_score` in a forked worker where the parent's session should not be inherited
  — `get_session()` (`httpclient.py:61-71`) would hand a child process a `Session` built on the
  parent's thread. Out of scope for this target; flagging for the `concur` lens.

---

## Recommended order of work

1. **Break the cycle (S1).** Extract `geo.py` (`infer_region` + `REGION_KEYWORDS` +
   `_REGION_MAP` + both coordinate tables, `enricher.py:10-94`) and `schema.py`
   (`BusinessRecord`, `scrapers/base.py:5-28`) as dependency-free leaves. Repoint
   `scrapers/google_places.py:26` and `enricher` at them. Then populate `scrapers/__init__.py`
   freely — or leave it empty. **Verify by re-running the four-state probe above; state C must
   import cleanly.**
2. **Make the score table importable on its own (S2).** Create `scoring.py` holding `lead_score`
   and `completeness_score` with `re` as its only import; leave `enrich`, `infer_region`,
   `check_websites` in `enrich.py`. Delete `enricher.py:2`, `enricher.py:4` and the mid-file import
   at `:128`. Then delete the `requests`/`urllib3` stubbing hack at
   `tests/test_lead_signal.py:23-63`.
3. **Fix the two live correctness traps (S2).** Guard `rating` at `enricher.py:313` and change the
   signature to `BusinessRecord`; and either drop `enricher.py:350` or set `industry_priority`
   before it. Add the two tests that would have caught them: one calling `enrich()` end-to-end and
   asserting the score matches `lead_score` on the enriched record, one asserting a string `rating`
   does not raise.
4. **Version the probe (S2).** Add `probe_version` to `FIELDS` (`main.py:33-40`), write it in
   `check_websites`, and have `cmd_score` (`cli.py:247`) refuse to re-score a CSV whose
   `probe_version` predates the LIVE/DEAD/UNKNOWN split. Without this, `leadminer score` will
   confidently manufacture rebuild pitches out of pre-043 data.
5. **Name the shared contracts (S2/S3).** `SOURCE_SEP` in one place, consumed by `dedup.py:145`,
   `dedup.py:174` and `enricher.py:316`; `HIGH`/`MEDIUM` in the new `taxonomy.py`, consumed by
   `enricher.py:307-309` and `main.py:204`; `dedup._MIN_DIGITS` for `enricher.py:292` and
   `main.py:142`. Add a `cmd_validate` check that every `source` value is a known `_SOURCE_TRUST`
   key.
6. **Finish the layout (S3).** Move to `src/leadminer/`, delete
   `[tool.hatch.build.targets.wheel] include`, repoint `[project.scripts]`. Delete
   `enricher.py:327-328`. Run `ruff --fix` and delete `requirements.txt`.

---

## Snapshot integrity

The working tree changed **twice** while this audit ran, both times in the S1 blast radius:

| Time (UTC) | Observation |
|---|---|
| ~20:03 | `enricher.py:3` became `from scrapers.base import BusinessRecord`; `scrapers/__init__.py` became 10 lines re-exporting all five scrapers. **This is state C — `lead_score` was unimportable.** |
| ~20:04 | Both reverted. `enricher.py` back to `sha256 f980120c…`, `scrapers/__init__.py` back to `sha256 e3b0c442…` (empty). |

Every line anchor in this report is against the pinned copy at
`…/opencode/snap398`, taken `2026-10-03T20:04:46Z`:

```
f980120cd937e24d47f78124db10c028cfb8d1592577e69491120cde64df5b5c  enricher.py
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  scrapers/__init__.py
ad20effe2de32ae0de604ae8d737cf7a92f45158db131abebb67dbf872f2410d  scrapers/google_places.py
c97a557ea87e6f9d05039165eb05139fe9b77eabcbb6cb0d14fac1fdd02668a7  scrapers/whitelist.py
be14ccef324a0378ffb30eae6a9e2c97d294e0a4145dad72ca12002c5313ce93  main.py
846f19122861c8941707408e807c7cce666f80274e7fbf2e612c4c60320a6b43  dedup.py
cdd8b9d803b42db4bce0d5026d4c956740a5ee41e322483f0075540f755f82af  httpclient.py
7e9d0252401c23be0b7c4c460f1fd4455ad595b099cffecb1a13d049cc0396fb  cli.py
```

`lead_score` is at `enricher.py:283` in both the snapshot and the live tree, so the target
function itself did not move. No file in the repository was modified by this audit; all
experiments ran against copies in a scratch directory, with no network access and no package
installs.
