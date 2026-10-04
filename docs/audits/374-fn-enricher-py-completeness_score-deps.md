# 374 — `completeness_score` dependency audit (lens: deps)

## Verdict

`completeness_score` (`enricher.py:101-117`) has **zero import dependencies** — an AST walk
shows it touches only `record`, `score`, `dict`, `int` and eight string literals — yet its
transitive dependency closure spans **twelve files across four layers**, none of them declared,
none of them enforced, and one of them (`enricher.py:128`) making the pure function unimportable
without `requests` installed. The real dependency is not a module, it is a *record shape*, and
that shape is written independently in four places and mutated after the score is computed, so
the function is **not a pure function of its argument**: four of its seven possible points are
decided by whether an HTTP GET succeeded.

---

## Dependency map

### Lexical dependencies (what the AST sees)

```
completeness_score(record: dict) -> int        enricher.py:101
├── record (untyped dict)                      enricher.py:101  <- no annotation, no validation
├── dict.get                                   builtins
└── 8 hardcoded key literals:
      phone     enricher.py:103
      email     enricher.py:105
      website   enricher.py:107
      address   enricher.py:109
      facebook  enricher.py:111  \
      instagram enricher.py:111  /  one shared point, first-truthy wins, no precedence rule
      whatsapp  enricher.py:113
      linkedin  enricher.py:115
```

**Imports reached: none.** No module, function, global or type is referenced. Every dependency
below is a *data* dependency, which is precisely why it is invisible to `ruff`, `mypy` and
`pyflakes`.

### Module import graph of `enricher.py` today (for contrast)

```
enricher.py
├── line   1  import re                              USED  (lines 53,130-136,199,285)
├── line   2  import requests                        *** UNUSED ***  ruff F401
├── line   3  concurrent.futures                     USED  (check_websites only)
├── line   4  urllib.parse urljoin, urlparse         *** BOTH UNUSED ***  ruff F401
├── line 128  from httpclient import ...             USED  (line 175, 158) — E402, mid-file
└── line 327  import urllib3  (function-local)       vestigial, see S3-4
                          │
                          ▼
httpclient.py
├── line 43  import requests                         USED
└── line  1  import logging                          USED (log.debug, lines 178, 204)
```

### Full transitive dependency closure of `completeness_score` (12 files)

**Writers of the 8 keys it reads:**

| # | File:line | What it writes | Notes |
|---|---|---|---|
| 1 | `scrapers/base.py:5-28` | declares all 8 in `BusinessRecord` | inert `TypedDict`, no runtime check |
| 2 | `scrapers/osm.py:80-93` | `address`, `phone`, `email`, `website`, `facebook`, `instagram` | only source that can set `facebook` |
| 3 | `scrapers/wikidata.py:96-99` | `website`, `phone`, `email`, `address` | `whatsapp/linkedin/facebook/instagram` hardcoded `None` at `:118-121` |
| 4 | `scrapers/google_places.py:286-296` | `address`, `phone`, `website` | `email/facebook/instagram/whatsapp/linkedin` hardcoded `None` at `:290-296` |
| 5 | `dedup.py:159-194` | `_pick`/`_merge` arbitrates all 8 | per-source trust `_SOURCE_TRUST:97-110`, validity `_validity:120-140` |
| 6 | `main.py:77-105` | `load_master` recasts types, `""`→`None` at `:88-89` | re-entry path from CSV |
| 7 | **`enricher.py:225-276`** | `check_websites` writes `email`, `instagram`, `whatsapp`, `linkedin` | **network-dependent writer** |
| 8 | **`main.py:189-197`** | `normalize_phone` rewrites `phone` **after** the score | see S1-1 |

**Readers of the value it returns:**

| # | File:line | Use |
|---|---|---|
| 9 | `main.py:206` | `qualified = [r for r in records if r.get("completeness_score", 0) >= 1]` — the **only functional consumer** in the codebase |
| 10 | `main.py:37,44` | CSV column order + `_INT_FIELDS` cast on reload |
| 11 | `pitch_recommender.py:48-52` | reads `completeness_score`, then **never uses it** — dead read, see S2-6 |
| 12 | `README.md:46,64` | documents "0–7" and "at least one contact signal" |

**Hidden ordering edge (not an import, but load-bearing):**
`enricher.enrich()` calls `check_websites(records)` at `enricher.py:337`, *then* calls
`completeness_score(r)` at `enricher.py:349`. Swap those two lines and the score silently loses
up to 4 points. Nothing in the code, the type system or the test suite records that this order
is mandatory.

---

## Findings

### S1-1 — `completeness_score` reads `phone` **before** it is normalized, so it awards a point for a phone number the export deletes

- **Where:** `enricher.py:103-104` (read) vs `main.py:194` (rewrite). Order is fixed by
  `main.py:187` (`enrich()`) running before the loop at `main.py:189-197`.
- **Breaks:** `completeness_score` awards +1 for any truthy `record["phone"]`. Seven lines later
  in `main.py`, `normalize_phone()` — which returns `""` for anything with fewer than 7 digits
  (`dedup.py:45-46`) — discards the same value. **`completeness_score` is never recomputed.**
  `lead_score` *is* recomputed at `main.py:197` (with a comment at `main.py:195-196` explaining
  exactly this class of staleness) — but only `lead_score`. So the shipped row reads
  `phone="" , completeness_score=1`.
- **Trigger** (measured, `normalize_phone(raw,"LB")` vs `completeness_score({"phone": raw})`):

  | input `phone` | truthy | completeness | exported `phone` | |
  |---|---|---|---|---|
  | `"+9611234567"` | yes | +1 | `"+9611234567"` | ok |
  | `"061 123 4567"` | yes | +1 | `"+961611234567"` | ok |
  | `"none"` | yes | **+1** | `""` | **mismatch** |
  | `"N/A"` | yes | **+1** | `""` | **mismatch** |
  | `"unknown"` | yes | **+1** | `""` | **mismatch** |
  | `"12345"` | yes | **+1** | `""` | **mismatch** |

  `"none"` and `"N/A"` are real values in the wild: OSM permits freeform `phone=*`
  (`scrapers/osm.py:89` reads `tags.get("phone") or tags.get("contact:phone")` with no
  validation), and Google's `nationalPhoneNumber` (`scrapers/google_places.py:289`) is
  unvalidated too. Every one of these rows lands in `qualified_businesses.csv` on the strength
  of a contact channel that does not exist in the file.
- **Fix:** call `completeness_score` after the `main.py:189-197` normalization loop, and score a
  *validated* contact (e.g. `normalize_phone` result, `_EMAIL_OK.match`, `_URL_OK.match` —
  `dedup.py:116-117`) rather than raw truthiness.

### S1-2 — The function has zero imports but is **not pure**: 4 of its 7 points are decided by network outcomes

- **Where:** `enricher.py:337` (`check_websites`) → `enricher.py:349` (`completeness_score`);
  writers at `enricher.py:252-259`.
- **Breaks:** `check_websites` writes `email`, `instagram`, `whatsapp`, `linkedin` — exactly the
  four keys `completeness_score` reads at `:105, :111, :113, :115`. The score is therefore a
  function of *(argument, run order, HTTP outcomes)*, not of its argument. There is no
  `score_version` column and no way to tell a re-run's number from last month's.
- **Trigger** (measured, fully offline — `get_session` replaced with a stub, no sockets):

  ```
  input record: {"name":"Acme","address":"Main St","website":"https://x.test","source":"osm"}

  site reachable, HTML contains a mailto + an instagram link:
      completeness_score BEFORE check_websites : 2
      completeness_score AFTER  check_websites : 4
  site unreachable / Cloudflare-blocked (UNKNOWN):
      completeness_score BEFORE check_websites : 2
      completeness_score AFTER  check_websites : 2
  ```

  Same dict, same code, score differs by 2 purely on network reachability.
- **Aggravating factor:** `cli.py:247-277` (`leadminer score`) is documented as "re-score
  existing records" and recomputes `lead_score` (`:272`) — but never `completeness_score`.
  Because `main.py:44` casts it back to `int` and `write_csv` writes it back out
  (`main.py:125-127`), `leadminer score` **re-writes a stale `completeness_score` into the file
  while updating `lead_score`**, and `main.py:206` then selects `qualified_businesses.csv` from
  that stale number.
- **Fix:** either score from a snapshot recorded at enrichment time (with a `score_version` /
  `scored_at` column), or make `completeness_score` take the explicit inputs it depends on
  (e.g. `(fields: Mapping[str, object])` computed by the caller) so its purity is visible in the
  signature. Add `completeness_score` to `cli.cmd_score`.

### S2-1 — Layering violation: the ingestion layer imports the analysis layer, and a src layout turns that into an order-dependent circular import

- **Where:** `scrapers/google_places.py:26` — `from enricher import infer_region`. Upstream
  (a scraper) depends on downstream (the enrichment stage). `osm.py` and `wikidata.py` do not
  need this; only `google_places` does, because it wants region tagging at `:275`.
- **Breaks:** I reconstructed the tree as `src/leadminer/{analysis/enricher.py, scrapers/*}` and
  ran it. With the **current empty `scrapers/__init__.py`** everything imports. The moment two
  ordinary refactors land — (a) type the scorer as `BusinessRecord`, which
  `docs/audits/066-typing-at-scale.md:209` explicitly recommends for `enricher.py:101`, and
  (b) populate `scrapers/__init__.py` with the re-exports any real package needs — the cycle is
  real and **order-dependent**:

  ```
  $ python3 -c "from leadminer.analysis.enricher import completeness_score"
  File ".../leadminer/analysis/enricher.py", line 2, in <module>
    from leadminer.scrapers.base import BusinessRecord
  File ".../leadminer/scrapers/__init__.py", line 4, in <module>
    from .google_places import GooglePlacesScraper
  File ".../leadminer/scrapers/google_places.py", line 26, in <module>
    from leadminer.analysis.enricher import infer_region
  ImportError: cannot import name 'infer_region' from partially initialized module
  'leadminer.analysis.enricher' (most likely due to a circular import)
  ```

  `import leadminer.main` **still succeeds** (it happens to import `scrapers.osm` first at
  `main.py:25`, warming the cache in a lucky order). That is the worst failure mode: CI green on
  the entrypoint, broken the first time anything imports the scorer directly.
- **Correction to an existing audit:** `docs/audits/045-scraper-layer-refactor.md:5` claims this
  "triggers a hard `ModuleNotFoundError` under any `src/` layout". It does not — with an empty
  `__init__.py` the src layout imports cleanly. The real failure is the circular `ImportError`
  above.
- **Fix:** move region inference into a leaf module with no scraper dependency
  (`leadminer/regions.py`), have both `enricher.py:79` and `google_places.py:275` import from
  it, and give the record type its own top-level module (`leadminer/records.py`) that both
  layers may import.

### S2-2 — Three false import edges make a pure scoring function unimportable without `requests`

- **Where:** `enricher.py:2` (`import requests`, never referenced), `enricher.py:4`
  (`urljoin`, `urlparse`, never referenced), `enricher.py:128` (`from httpclient import ...` →
  `httpclient.py:43` `import requests`).
- **Breaks (measured):**

  ```
  $ # simulate a bare checkout, requests absent:
  from enricher import completeness_score
  -> IMPORT FAILED: ImportError No module named 'requests'

  $ # remove ONLY enricher.py:2, keep enricher.py:128:
  from enricher_noreq import completeness_score
  -> IMPORT FAILED: ImportError No module named 'requests'    # httpclient.py:43 is a second, independent edge
  ```

  Two independent edges. Even after deleting the unused `import requests`, the function-local
  `httpclient` import of a *different concern* still drags in `requests`, `urllib.parse`,
  `email.utils`, `http`, `socket`, `ssl` and `threading`. Confirmed by diffing `sys.modules`
  before and after the import: `requests`, `email.utils`, `urllib`, `urllib.parse` and
  `leadminer.httpclient` all load as side effects of importing a function that touches nothing
  but a dict.
- **Corroborating evidence:** `tests/test_lead_signal.py:23-63` is a **38-line stub shim** that
  fabricates `requests` and `urllib3` in `sys.modules` purely so that
  `from enricher import DEAD, LIVE, UNKNOWN, infer_region, lead_score` at `:66` can execute. Its
  own docstring at `:24` says "enricher imports requests at module scope. Stub it if absent so
  these tests run in a bare checkout". Note it does **not** import `completeness_score` — the
  test author hit this wall and stopped.
- **Fix:** delete `enricher.py:2` and `enricher.py:4`; move `_MAX_BODY_BYTES`, `_fetch_website`,
  `check_websites` and the four `_CONTACT_RE` globals (`enricher.py:130-276`) into
  `enricher_http.py`. `enricher.py:101-117` then imports cleanly with zero third-party deps and
  the shim at `tests/test_lead_signal.py:23-63` can be deleted.

### S2-3 — `facebook` is a write-only dependency: no code in the pipeline can populate it for 2 of 3 sources

- **Where:** `enricher.py:111` (`record.get("facebook") or record.get("instagram")`).
- **Breaks:** grepping every writer of `facebook`: `scrapers/osm.py:92` is the **only** one.
  `scrapers/wikidata.py:118` and `scrapers/google_places.py:293` hardcode `facebook=None`.
  `_fetch_website` (`enricher.py:199-220`) extracts email/Instagram/WhatsApp/LinkedIn and has no
  Facebook branch at all. `enricher.py:342` only `setdefault`s it to `None`.
  So for every Google and Wikidata record, line 111's left disjunct is permanently `None` and
  the point is decided entirely by `instagram`.
- **Why it matters:** `completeness_score`'s maximum is 7 for `osm` records and effectively 6
  for `google_places` records **unless** `check_websites` found an Instagram handle. Two
  incomparable populations are being scored on one scale, with the gap undocumented in
  `README.md:64` ("0–7") and `scrapers/base.py:28` (`completeness_score: int`).
- **Fix:** either add a Facebook regex to `_fetch_website` (the `contact:facebook` tag exists in
  OSM, so the data is there), or drop `facebook` from line 111 and score `instagram` alone.

### S2-4 — The record schema is declared independently in four places, and the two authoritative-looking ones disagree on order

- **Where:** `scrapers/base.py:5-28`, `main.py:33-40` (`FIELDS`), `main.py:43-44`
  (`_FLOAT_FIELDS`/`_INT_FIELDS`), and implicitly `enricher.py:101-117`'s 8-key subset.
- **Measured:** `FIELDS` and `BusinessRecord` contain **identical key sets** (23 keys, symmetric
  difference empty) but **different order** — `FIELDS[2] == 'region'` vs
  `BusinessRecord[2] == 'address'`. `BusinessRecord`'s declaration order is the only thing a
  reader has to go on for what the CSV looks like, and it is not the CSV order.
- **Why this is a `deps` finding:** `completeness_score`'s 8 keys are a *fourth* declaration that
  nothing validates against the other three. Change `FIELDS` and the scorer silently stops
  reading a column; change `BusinessRecord` and mypy (`strict = true`, `pyproject.toml:66`)
  cannot tell you, because the scorer is annotated `record: dict` (`enricher.py:101`) and
  therefore type-checks against `Any`.
- **Fix:** generate `FIELDS`, the TypedDict and the scorer's key list from one
  `leadminer/records.py` constant.

### S2-5 — The only consumer (`main.py:206`) implements a predicate that contradicts its own documentation

- **Where:** `main.py:206`, documented at `main.py:9` and `README.md:46` as
  "completeness_score >= 1 / at least one contact signal".
- **Breaks (measured):**

  | record | `completeness_score` | in `qualified_businesses.csv` |
  |---|---|---|
  | `{"name":"A","address":"Main St, Beirut"}` | 1 | **yes** |
  | `{"name":"B","rating":4.9,"review_count":300}` | 0 | no |
  | `{"name":"C","lat":33.89,"lon":35.50}` | 0 | no |
  | `{"name":"E","phone":"+9611234567"}` | 1 | yes |

  A street address is not a contact signal. A row with only an address outranks a row with a 4.9★ /
  300-review Google profile, because `rating` and `review_count` are not scored at all
  (`enricher.py:101-117` reads neither) despite being in the schema.
- **Fix:** rename the export to `with_any_signal.csv`, or split `completeness_score` into
  `contact_score` (the five reachable channels) and `profile_score` (rating/reviews/coords) and
  filter on the former.

### S2-6 — `pitch_recommender.py` reads `completeness_score` and then throws it away

- **Where:** `pitch_recommender.py:48-52`. AST store/load analysis: `completeness` is stored at
  lines 48, 50, 52 and read **only** at line 50 (inside its own `int()` coercion). There is no
  load after line 52.
- **Breaks:** the comment at `pitch_recommender.py:68-69` says Tier 3 fires on "weak
  completeness", but the value is never consulted. So `completeness_score` has exactly **one**
  functional consumer in the entire codebase (`main.py:206`), which is why S1-1 and S2-5 have
  gone unnoticed.
- **Fix:** delete lines 48-52, or actually gate Tier 3 on them.

### S3-1 — Logging-only imports that are entirely dead

- `scrapers/osm.py:1` `import logging` + `:7` `log = logging.getLogger(__name__)` — `log` is
  never called (grep for `log.` in `osm.py`: zero hits). Same in
  `scrapers/wikidata.py:1,7` (zero hits). Both modules report every failure with bare `print`
  (`osm.py:51-55`, `wikidata.py:81-85`), which is why nothing was ever logged. Meanwhile
  `httpclient.py:45` **does** use `log.debug` (`httpclient.py:178,204`) — so the project's own
  HTTP layer is the only place a failed request is visible in a log at all.
- **Fix:** delete the two dead `logging` blocks; route the scraper failure messages through
  `httpclient.log`.

### S3-2 — Annotation-only imports

- `scrapers/osm.py:2`, `scrapers/wikidata.py:2`, `scrapers/google_places.py:22`
  — `from typing import Iterator`, used only in the return annotation
  (`osm.py:31`, `wikidata.py:66`, `google_places.py:168`).
- `httpclient.py:41` — `from typing import Any`, used only at `httpclient.py:110,120,158`
  (return annotations and `**kwargs`).
- Harmless today, but they are the reason `httpclient.py:32` needs
  `from __future__ import annotations` while `enricher.py` does not — an inconsistency worth
  noting when the package is restructured.

### S3-3 — `BusinessRecord` key order is authoritative-looking and wrong

See S2-4. Recorded separately because it is a pure-clarity item: `scrapers/base.py:5-28` lists
`address` before `region` and `lead_score` before `source`, while the export order
(`main.py:33-40`) is neither. A `TypedDict` key order reads like a schema; don't let it lie.

### S3-4 — Vestigial function-local `urllib3` import, imported only to suppress a warning that can no longer fire

- **Where:** `enricher.py:327-328`, inside `enrich()`:
  `import urllib3; urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)`.
- **Breaks:** `InsecureRequestWarning` is raised only when certificate verification is disabled.
  Grepping the whole non-docs tree for `verify` returns exactly two hits, both in comments —
  `enricher.py:170` ("verify=True. This was verify=False"). No code path passes `verify=False`.
  The suppression is dead, and it is the sole reason `tests/test_lead_signal.py:52-60` has to
  fabricate a fake `urllib3`.
- **Fix:** delete `enricher.py:327-328` and the `urllib3` stub at `tests/test_lead_signal.py:52-60`.

### S3-5 — `enricher.py:128` is a mid-file import

`ruff check --select E402` confirms: `enricher.py:128:1: E402 Module level import not at top of
file`. It is legal (the preceding 127 lines are constants), but it is the load-bearing edge in
S2-2 and it hides `httpclient`'s cost at the bottom of a file a reader will skim only at the top.

---

## Not a bug, but worth knowing

- **The `requests` / `urljoin` / `urlparse` imports are already flagged.** `ruff check --select F`
  on this repo returns `enricher.py:2:8: F401`, `enricher.py:4:26: F401`,
  `enricher.py:4:35: F401`, `enricher.py:128:1: E402`. The dependency edges are known to the
  linter; nobody ran the fix. (`ruff` also flags `pitch_recommender.py:37:5: F841 has_email
  assigned but never used` — another dead read of an enrichment field.)
- **`enrich()` does not `setdefault` `email`, `phone`, `website` or `address`**
  (`enricher.py:340-348` covers only `lat`, `lon`, `facebook`, `instagram`, `whatsapp`,
  `linkedin`, `website_live`, `rating`, `review_count`). Nothing crashes today because every
  reader uses `.get()`, and `csv.DictWriter` fills missing keys with `""`. It does mean
  `completeness_score` must keep using `.get()` rather than `[...]` — which is the only thing
  protecting it from `KeyError` today.
- **Duplicate mapping in `REGION_KEYWORDS`.** `enricher.py:34-37` (South Lebanon) and
  `enricher.py:38-41` (Nabatieh) both list `"النبطية"`, `"bint jbeil"`, `"marjayoun"`,
  `"hasbaya"`. Since `_REGION_MAP` (`enricher.py:52-55`) is first-match-wins, every South
  Lebanon record with one of those four tokens is labelled `South Lebanon` and `Nabatieh` can
  only ever be reached by coordinate box (`_LB_COORD_REGIONS:65`). Not a dependency of
  `completeness_score`, but it is the same "two lists, no shared constant" pattern that produced
  S2-4, in the module above it.
- **`docs/audits/032-fixtures-offline-replay.md:84` plans `test_enricher_pure.py` for exactly
  these three functions.** That file cannot import them without a `requests` stub (S2-2). Fix
  S2-2 first and the test file becomes trivial.

---

## Recommended order of work

1. **S1-1** — move the `completeness_score` call to after `main.py:189-197`, and score validated
   contacts rather than raw truthiness. One-line move, removes a class of wrong exported scores.
2. **S1-2** — add `completeness_score` to `cli.cmd_score` (`cli.py:264-276`) so `leadminer score`
   stops writing a stale number, then stamp a `score_version` (or `scored_at`) column so a
   network-dependent score is at least identifiable across runs.
3. **S2-2** — delete `enricher.py:2` and `:4`; split `_fetch_website` / `check_websites` /
   `enrich()` into `enricher_http.py`. This is a prerequisite for step 4 and for
   `test_enricher_pure.py`.
4. **S2-1** — extract region inference to `leadminer/regions.py` and the record type to
   `leadminer/records.py`, so `scrapers.google_places` no longer imports `enricher`. Do this
   *before* anyone populates `scrapers/__init__.py` or applies the `BusinessRecord` annotation
   from `066-typing-at-scale.md:209` — either one alone is survivable, together they are the
   circular import demonstrated above.
5. **S2-4** + **S2-5** — one schema constant; split `contact_score` from `profile_score` and
   align `qualified_businesses.csv` with its own documentation.
6. **S2-3**, **S2-6**, **S3-1..5** — cleanup. `ruff check --fix` handles S3-2's neighbours and
   the `F401`/`F541` batch; the rest are hand edits.
7. **Packaging (not in scope above but adjacent):** `pyproject.toml:35-37` sets
   `include = ["scrapers/"]` with no `packages` or `only-include`. Per Hatch's file-selection
   docs, `include` is a Git-style glob allowlist, so a wheel built today ships `scrapers/` and
   **none** of the six top-level modules — while `[project.scripts] leadminer = "cli:main"`
   (`pyproject.toml:33`) points at a module the wheel does not contain. There is no `src/`, no
   `leadminer/` directory and no top-level `__init__.py`. Fixing this is the change that makes
   S2-1 an outage instead of a latent bug, so sequence it with step 4.