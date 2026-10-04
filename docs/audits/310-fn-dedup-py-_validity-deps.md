# 310 — `_validity` read through its dependency graph

**Target:** `dedup.py:120-140` (`_validity`)
**Lens:** dependencies — what it actually depends on, transitively; what is
surprising; what violates layering; what becomes a circular import under a
`src/` layout; anything imported only for a type annotation or only for logging.

## Verdict

`_validity` has a **perfect** forward dependency closure and a **broken** set of
implicit ones. The explicit closure is two stdlib modules and four builtins, zero
project imports, zero third-party — the cleanest dependency surface in the
repo. Every dependency that actually matters is invisible in the source: the
**field vocabulary** it hardcodes is re-declared in four other modules, its
**email/URL plausibility rules** are a laxer second copy of rules that already
live in `enricher.py`, its **runtime type contract** belongs to
`main.load_master`, and its verdict sits at tuple index 1 of `_pick`'s rank so
it can never outrank `_trust_for`. The duplication is not carelessness — it is
the fossil of an avoided cycle: `dedup` may not import `enricher` (that is +71
modules and a hard `requests` requirement for a pure-data module) and may not
import `main` (that is a **hard `ImportError` today**, reproduced below).

**The single most important thing:** `_validity` decides which value survives
into the cumulative master using knowledge it cannot legally share, so the two
implementations of "is this a plausible email / coordinate" disagree — and
wherever they disagree, `_validity` is the one that is more permissive, while
`leadminer validate`, the pipeline's own quality gate, agrees with `_validity`.
A placeholder address like `sales@example.com` scores the maximum 3, outranks a
real email from another source, survives enrichment, and ships in
`sales_ready.csv`.

---

## The dependency map

### Forward closure (transitive) — clean

```
_validity                      dedup.py:120
├── _is_missing                dedup.py:79   -> isinstance  (builtin)          TERMINAL
├── _EMAIL_OK                  dedup.py:116  -> re.compile (re, dedup.py:1)
├── _URL_OK                    dedup.py:117  -> re.compile (re, dedup.py:1)
├── str(value).strip()         builtin
└── float(value)               builtin
```

Measured cost of the closure:

```
import dedup                +26 modules | project+3rd-party loaded: ['dedup']
from dedup import _validity  +26 modules | project+3rd-party loaded: ['dedup']
```

`dedup.py` imports `re` and `unicodedata` (`dedup.py:1-2`); `unicodedata` is
only used by `normalize_name` (`dedup.py:67-68`), so `_validity`'s true closure
is `re` plus four builtins. **No project import, no third-party import, no I/O,
no logging, no global mutable state.** This is the part of `dedup.py` that is
right, and it is worth protecting.

### Reverse closure (who depends on `_validity`) — one caller

```
dedup.dedup            dedup.py:197
  └─ _merge            dedup.py:190   (called at :208 and :216)
       └─ _pick        dedup.py:159
            └─ rank()  dedup.py:179-185  -- closure over `field`
                 └─ _validity          dedup.py:182
main.main              main.py:180  ->  dedup.dedup
tests                  tests/test_lead_signal.py:65 (imports _merge, never _validity)
```

grep confirms `_validity` appears at exactly two places in the codebase:
its definition (`dedup.py:120`) and its single call site (`dedup.py:182`).
Nothing outside `dedup.py` can observe it.

### What `_pick` actually feeds it — the surprising part

`_validity` is the **second** element of a four-element lexicographic tuple
(`dedup.py:179-185`):

```python
return (
    _trust_for(field, rec),                       # index 0
    _validity(field, value),                       # index 1  <-- here
    _recency(rec) if field in _VOLATILE else "",   # index 2
    str(value),                                    # index 3
)
```

Tuple comparison is lexicographic, so `_validity` is consulted **only when the
two records have identical per-source trust for that field**. See S2.

Two fields bypass `rank` entirely and are therefore never validity-scored at
all: `source` (`dedup.py:173-174`) and `scraped_at` (`dedup.py:176-177`).

### Implicit dependencies — declared nowhere, enforced nowhere

| # | What `_validity` relies on | Who actually owns it | Drift status |
|---|---|---|---|
| 1 | The 23 field names | `scrapers/base.py:5-28` (`BusinessRecord`), `main.py:33-40` (`FIELDS`) | 5 separate declarations; see S3 |
| 2 | The set of *numeric* fields | `main.py:43` `_FLOAT_FIELDS`, `main.py:44` `_INT_FIELDS` | `lat/lon/rating` only; 3 other numeric fields unscored |
| 3 | The runtime **type** of every value | `main.load_master` (`main.py:90-101`) | `dedup → main` is a hard cycle; see S1 |
| 4 | Email plausibility | `enricher._EMAIL_RE` (`enricher.py:130`), `enricher._EMAIL_BLACKLIST` (`enricher.py:138-141`) | `dedup → enricher` costs 71 modules; see S1 |
| 5 | URL plausibility | `enricher._fetch_website` (`enricher.py:161-222`, GET at `:175`) | `_URL_OK` is never consulted before the fetch |
| 6 | Coordinate plausibility | `enricher._LB/_KSA_COORD_REGIONS` (`enricher.py:58-76`), `cli.in_box` (`cli.py:159-162`) | three implementations, three verdicts |
| 7 | Rating range | `enricher.lead_score` (`enricher.py:313`), `cli.cmd_validate` (`cli.py:185`) | `dedup` is the strictest of the three, except for NaN |
| 8 | "A phone has ≥7 digits" | `dedup._MIN_DIGITS` (`dedup.py:30`) | re-typed as a literal at `main.py:142` and `enricher.py:292` |

---

## Findings

### S1 — `_validity`'s email rule is a second, laxer copy of `enricher`'s, and the copy is the one that wins

- **Where:** `dedup.py:116` (`_EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")`),
  applied at `dedup.py:124-125`. The rules it duplicates: `enricher.py:130`
  (`_EMAIL_RE`, which requires a TLD of `{2,}` *letters*) and
  `enricher.py:138-141` (`_EMAIL_BLACKLIST`, which rejects `example.com`,
  `domain.com`, `yourdomain.com`, `email.com`, `sentry.io`, `wixpress.com`,
  `squarespace.com`, `shopify.com`), enforced at `enricher.py:202`.
- **Breaks:** `_validity` knows nothing about the blacklist, so a placeholder
  address scores the **maximum** 3. `_SOURCE_TRUST` (`dedup.py:103`, `:107`)
  gives OSM `email` trust 3 and Wikidata trust 2, so the placeholder also
  outranks on trust. The placeholder therefore wins the field twice over:

  ```
  _validity('email','sales@example.com')   = 3
  _trust_for('email', osm) / (wikidata)   = 3 / 2
  _merge(osm, wikidata)['email']          = 'sales@example.com'
  ```

  and then it is untouchable downstream: `enricher.py:252` is
  `if not r.get("email") and contacts["email"]:` — extraction only *fills* an
  empty email, it never replaces one. The placeholder earns
  `lead_score += 20` (`enricher.py:288-289`), satisfies
  `main.has_any_contact`'s `("@" in email and len(email) > 5)` (`main.py:143`),
  and lands in `sales_ready.csv` (`main.py:202-205`). A helper is handed
  `sales@example.com` as the contact for a business that has a real one.
- **Trigger:** an OSM record with `tags["email"] = "sales@example.com"`
  (`scrapers/osm.py:90` reads exactly that tag) matched by phone to a Wikidata
  record carrying a real address at `P968` (`scrapers/wikidata.py:98`). Run the
  two-line `_merge` probe above.
- **Why the duplication exists (the dependency story):** `dedup` cannot import
  the canonical rules. Measured on this tree:

  ```
  import dedup                    +26 modules | ['dedup']
  from enricher import _EMAIL_RE  +97 modules | ['enricher', 'httpclient']
  ```

  `dedup` would gain `httpclient` → `requests`, `urllib3`, `logging`,
  `threading`, `random`, `email.utils`, `dataclasses`, `contextlib` — a 3.7×
  import cost and a hard third-party requirement, for a module whose entire job
  is pure data. The existing test file proves the pain: `tests/test_lead_signal.py:23-63`
  hand-rolls a 40-line `requests` stub, and the `dedup` tests at `:65` sit right
  next to it needing that stub for no reason of their own.
- **Fix:** extract `_EMAIL_OK`, `_EMAIL_BLACKLIST` and the URL check into a
  leaf module (`validation.py`, importing only `re`) that `dedup` and `enricher`
  both depend on. That removes the inversion *and* the duplication in one move,
  and costs neither module anything.

### S1 — `_validity` uses `float()` as a type oracle, so `nan` and `inf` are perfect coordinates — and `leadminer validate` disagrees

- **Where:** `dedup.py:128-133` (`if field in ("lat","lon"): try: float(value)`).
  The values it inspects are manufactured by `main.py:93`
  (`row[field] = float(row[field])` inside `_FLOAT_FIELDS`, `main.py:43`),
  which accepts anything `float()` accepts.
- **Breaks:** `float()` succeeds for `"nan"`, `"inf"`, `"-inf"` and `"1e999"`,
  and there is **no range check** on the lat/lon branch — unlike the rating
  branch three lines later (`dedup.py:139`), which does check `0 <= v <= 5`.
  Same function, same input type, opposite verdicts:

  ```
  _validity('lat',     'nan') = 3      <- maximum plausibility
  _validity('lon',     'inf') = 3
  _validity('rating',  'nan') = 0      <- rejected, because that branch ranges
  _validity('lat',     True) = 3      <- bool is a number
  ```

  End to end, on a master row whose `lat`/`lon` cells contain `nan`:
  `load_master` yields `float('nan')`; `_validity` scores 3 and the NaN wins the
  merge outright against a real coordinate at equal trust:

  ```
  load_master  -> {'lat': nan, 'lon': nan, 'rating': nan}
  _merge({'lat':'nan'}, {'lat':33.89})['lat'] = 'nan'
  ```

  The NaN then silently disables region inference, because every box test in
  `enricher.infer_region` (`enricher.py:92`) is False for NaN — so
  `infer_region(...)` returns `None` for a record whose address carries no
  keyword, and `region` is written as blank forever after. And the pipeline's
  own quality gate **fails the same row**:

  ```
  leadminer validate data/all_businesses.csv
    PROBLEMS:
      - 1 rows have coordinates outside Lebanon/Saudi Arabia   (cli.py:164-173)
      - 1 rows have a rating outside 0-5                      (cli.py:185)
    exit 1
  ```

  Three implementations of "is this coordinate plausible" — `_validity` (3),
  `cli.in_box` (reject), `enricher._LB_COORD_REGIONS` (silently no-match) — and
  the permissive one is the one that decides what lands in the master, which is
  cumulative and therefore keeps the value indefinitely.
- **Trigger:** open `data/all_businesses.csv` in Google Sheets (which is where
  the master lives — the workflow `rclone copy`s it back every run), let one
  coordinate cell be auto-parsed as the literal `nan`, save. Next run reloads it
  as `float('nan')` and the merge prefers it forever.
- **Fix:** `math.isfinite()` in the lat/lon branch (`if not math.isfinite(v):
  return 0`) and add the same guard to `main.py:93` so the value never enters
  the record; share one `coerce_number()` helper between `load_master`,
  `_validity` and `cli.in_box`.

### S2 — `_validity` sits at tuple index 1 and can never outrank `_trust_for`, so the "is it plausible" gate does nothing wherever trust differs

- **Where:** `dedup.py:179-187`. `rank()` is
  `(trust, validity, recency, str)` and `av if rank(a, av) >= rank(b, bv)`
  — trust is compared first and decides alone on any difference.
- **Breaks:** for every field where the two records' sources disagree on trust,
  `_validity` is computed, discarded, and never affects the outcome. That is
  most fields: `_SOURCE_TRUST` (`dedup.py:97-110`) gives OSM and Google
  different scores for `website`, `email`, `phone`, `name`, `address` and
  `category`. Reproduced:

  ```
  _validity('website', 'example.com')          = 0    <- not a URL, unusable
  _trust_for('website', osm) / (google)        = 3 / 2
  _merge(osm_junk_url, google_real_url)['website'] = 'example.com'
  ```

  A syntactically dead value from the higher-trust source survives, and
  `enricher._fetch_website` (`enricher.py:175`) then issues a GET for it, which
  lands in `website_live` and drives `lead_score` (`enricher.py:300-304`) and
  `recommend_service`. The function's name and docstring promise a quality
  gate; structurally it is a fifth-order tiebreaker.
- **Trigger:** the two dicts in the probe above, both sharing `+96170123456` so
  they collide on the phone index at `dedup.py:208`.
- **Fix:** promote validity above trust (`(validity, trust, recency, str)`), or
  gate on validity independently: a value scoring 0 should be discarded rather
  than outranked. Either way, say so in the docstring — today it advertises
  "0-3 bonus" when the answer is "tiebreaker of last resort".

### S2 — The runtime type contract `_validity` depends on belongs to `main.py`, and closing that dependency is a hard `ImportError`

- **Where:** `dedup.py:130` and `:136` both call `float(value)` and depend on
  somebody having coerced the type first. The only place that happens is
  `main.load_master` (`main.py:90-101`), driven by `main.py:43-44`. And
  `main.py:29` is `from dedup import dedup, normalize_phone`.
- **Breaks:** the minimal fix for S1/S2-divergence — let `_validity` consult
  `main._FLOAT_FIELDS` / `main._INT_FIELDS` instead of guessing which fields are
  numeric — is a circular import, in the **current flat layout**, with no
  `src/` move required. Reproduced on a scratch copy of the tree:

  ```
  BASELINE   import main                     -> OK
  A) dedup.py:  + "from main import _FLOAT_FIELDS, _INT_FIELDS"
     import dedup -> ImportError: cannot import name 'dedup' from partially
                     initialized module 'dedup' (most likely due to a circular
                     import)
  B) enricher.py: + "from dedup import normalize_phone"   (replacing :285)
     import enricher -> OK
  D) dedup.py:  + "from scrapers.base import BusinessRecord"
     import main   -> OK
  E) D, plus scrapers/base.py: + "from dedup import normalize_name"
     import main   -> ImportError: cannot import name 'BusinessRecord' from
                     partially initialized module 'scrapers.base' (most likely
                     due to a circular import)
  ```

  Row **A** is the one to internalise: `_validity` depends on a contract owned
  by the module that already imports it. Row **B** is the other half of the
  trap — that fix is *available* today, which means it is the one someone will
  reach for first, and it simultaneously makes row **A** unfixable. The two
  minimal deduplications are mutually exclusive. Row **E** is the cycle that a
  `src/` move plus a stable `record_key()` (the fix both `035` and `067` ask
  for) would produce.
- **Trigger:** add `from main import _FLOAT_FIELDS, _INT_FIELDS` to the top of
  `dedup.py` and run `python -c "import dedup"`.
- **Fix:** the field vocabulary and the coercion belong in a leaf module with no
  project imports — `contracts.py` holding `BusinessRecord`, `FIELDS`,
  `_FLOAT_FIELDS`, `_INT_FIELDS`, `_VOLATILE`, `_SOURCE_TRUST`, `_EMAIL_OK`,
  `_URL_OK` and `coerce_number()`. `scrapers`, `enricher`, `dedup` and `main` all
  point inward at it. That is the same move `020` §S12 recommends for
  `BusinessRecord`; `_validity` is the second reason it is needed.

### S3 — The `-100` sentinel is unreachable, and its contract lives in a tuple literal in a different function

- **Where:** `dedup.py:121-123` (docstring says "0-3 bonus", returns `-100`),
  guarding `dedup.py:168-171`, which already calls `_is_missing` on both sides
  before `rank()` ever runs.
- **Breaks:** instrumenting `_validity` and running two `_merge` calls that
  cover both the blank-string and the `None` paths:

  ```
  _validity calls: 8    returning -100: 0
  ```

  `-100` is dead code on every reachable path. Worse, its *meaning* is not
  local: it exists only because index 1 of the tuple at `dedup.py:180-185` must
  lose to any present value. Reuse `_validity` as a standalone plausibility
  score — the obvious thing to do when someone notices it is the only field
  validator in the codebase — and you get an undocumented `-100` that is
  meaningless to the caller, returned from a function whose docstring promises
  `0-3`.
- **Trigger:** `_merge({"website": ""}, {"website": "https://a.example"})` —
  `dedup.py:168` short-circuits and `_validity` is never asked.
- **Fix:** either delete the `dedup.py:122-123` branch and narrow the annotation
  to `-> int` in `0..3`, or make the sentinel a named module constant
  (`_IMPOSSIBLE = -100`) and say in the docstring that it exists to lose.

### S3 — The field vocabulary is declared five times and three numeric fields have no validity rule at all

- **Where:** `dedup.py:124-139` (the dispatch), `dedup.py:97-110`
  (`_SOURCE_TRUST` keys), `dedup.py:114` (`_VOLATILE`), `main.py:43-44`
  (`_FLOAT_FIELDS` / `_INT_FIELDS`), `scrapers/base.py:5-28`
  (`BusinessRecord`), `main.py:33-40` (`FIELDS`). Six, counting the two
  disjoint trust tables.
- **Breaks:** `_validity` special-cases `email`, `website`, `lat`, `lon`,
  `rating`. `main.py:44` declares three more numeric fields —
  `review_count`, `completeness_score`, `lead_score` — for which there is **no
  rule**, so they fall through to `dedup.py:140` and score a flat `1`, the same
  as a perfectly good value:

  ```
  _validity('review_count', 'abc') = 1
  _validity('lead_score', 'n/a')  = 1
  _validity('country', 'SA')      = 1
  ```

  and `review_count` *is* in `_VOLATILE` (`dedup.py:114`), so at equal trust the
  fresher junk wins: `_merge({'review_count':'abc','scraped_at':'2026-09-…'},
  {'review_count':412,'scraped_at':'2026-01-…'})['review_count'] == 'abc'`.
  Reachability today is low (`main.py:99` `int(float(...))` casts the CSV path
  and the scrapers emit real ints), which is exactly why it will be introduced
  by the next source that changes and nobody will notice.
- **Trigger:** a future source, or any column added to the master by hand,
  carrying a non-numeric string in `review_count`.
- **Fix:** drive `_validity` from a table keyed by field name, declared next to
  `FIELDS` in the shared `contracts.py` from S2, so "which fields are numeric"
  has exactly one answer.

### S3 — `dedup.py`'s private constants leak their semantics into two other modules

- **Where:** `dedup.py:30` `_MIN_DIGITS = 7`, `dedup.py:44`
  `digits = re.sub(r"\D", "", raw)`.
- **Breaks:** both are private with no public accessor, so the knowledge is
  retyped as a literal elsewhere, three times each:

  ```
  dedup.py:44     re.sub(r"\D", "", raw)                            the original
  main.py:138     re.sub(r"\D", "", str(record.get("phone") or ""))  copy 1
  enricher.py:285 re.sub(r"\D", "", str(record.get("phone") or ""))  copy 2

  dedup.py:45     len(digits) < _MIN_DIGITS
  main.py:142     len(phone) >= 7
  enricher.py:292 len(phone) >= 7
  ```

  Change `_MIN_DIGITS` to 8 and `main.has_any_contact` and
  `enricher.lead_score` keep counting 7-digit strings as contactable. Separately,
  `dedup.py` holds two conventions for the same dependency: `_EMAIL_OK`/
  `_URL_OK` are precompiled globals (`:116-117`) while `:44`, `:69`, `:83` call
  `re.sub(r"...", ...)` with an inline literal.
- **Trigger:** change `dedup.py:30` to `_MIN_DIGITS = 8`; `enricher.lead_score`
  still awards `+15` for a 7-digit string.
- **Fix:** export `MIN_PHONE_DIGITS` and `digits_of(phone) -> str` from `dedup`
  and import them at `main.py:138` and `enricher.py:285`. (That makes
  `enricher → dedup`, which is row **B** above and is fine today — it is only
  `dedup → main` that must never happen.)

### S3 — Imports that exist only for typing, only for logging, or not at all

Directly asked for by the lens. `ruff check .` (0.16.10, read-only) over the
tree's own config (`pyproject.toml:44-55`):

- **Annotation-only.** `scrapers/osm.py:2`, `scrapers/wikidata.py:2` and
  `scrapers/google_places.py:22` each do `from typing import Iterator`, used
  only in return annotations (`osm.py:31`, `wikidata.py:66`,
  `google_places.py:168/209`). None of the three modules has
  `from __future__ import annotations`, so it is a real runtime import; ruff
  flags all three as `UP035` ("import from `collections.abc` instead").
- **Imported for the import itself.** `cli.py:46`:
  `from scrapers.base import BaseScraper  # noqa: F401  (import check)`. The
  symbol is never used; the statement exists purely so a broken scrapers
  package fails `leadminer scrape` loudly. That is a legitimate intent, but it
  belongs in a `try/except ImportError` with a message, not in a `noqa`.
- **Logging-only, and never called.** `scrapers/osm.py:1` + `:7` and
  `scrapers/wikidata.py:1` + `:7` each import `logging` and construct
  `log = logging.getLogger(__name__)`; `grep` finds **zero** `log.*` call sites
  in either file. Two dead logging imports that survive ruff only because
  `getLogger` counts as a use.
- **Logging-only, and inert in production.** `httpclient.py:35` + `:45` is the
  one logger that is called — twice, both `log.debug`
  (`httpclient.py:178`, `:204`) — and `grep` for
  `basicConfig|dictConfig|setLevel|logging.config` across the tree returns
  **nothing**. No handler is ever installed, so the retry telemetry
  `httpclient.py:1-30` exists to emit produces zero bytes. (Report `200` §S2
  files this from the ops side; it is the same single fact and should be fixed
  once, in `main()`.)
- **Not imported at all, yet load-bearing.** `enricher.py:2` `import requests`
  is flagged `F401` — the only reference to `requests` in the file is the word
  "requests" inside a comment at `enricher.py:125`; the code uses
  `httpclient.get_session()` (`enricher.py:175`). `enricher.py:4`
  `from urllib.parse import urljoin, urlparse` is `F401` twice over: neither
  name appears again in the file. So the sole runtime dependency declared in
  `pyproject.toml:16-18` is required by an import that does nothing, which is
  what forces the 40-line `requests` stub in `tests/test_lead_signal.py:23-63`
  and is what would be inherited by `dedup` if anyone ever "fixed" the
  duplication in S1 by importing from `enricher`.
- **Misplaced.** `enricher.py:128`
  `from httpclient import DEFAULT_MAX_BODY_BYTES, get_session` is a module-level
  import at **line 128 of a 352-line file**, sitting between a comment block and
  the compiled-regex constants. ruff flags it `E402`. The one mid-file import in
  the repo, in the one module that half the tree depends on.

---

## Not a bug, but worth knowing

- **There is no circular import anywhere in the tree today.** The project-local
  graph is a DAG, and `_validity`'s own closure is a leaf. Measured DAG:
  `main → {scrapers.osm, scrapers.wikidata, scrapers.google_places,
  scrapers.whitelist, dedup, enricher, pitch_recommender}`;
  `scrapers.{osm,wikidata} → httpclient, scrapers.base`;
  `scrapers.google_places → enricher, httpclient, scrapers.base`;
  `enricher → httpclient`; `dedup → {}`; `httpclient → {}`. The one remaining
  absolute-import violation inside a package is `scrapers/google_places.py:26`
  (`from enricher import infer_region`), already documented in `020` §S11 and
  `030` §S1; it is a `ModuleNotFoundError` under a wheel or a `src/` move, not a
  cycle. Everything filed above is a *latent* cycle, reproduced on a scratch
  copy rather than asserted.
- **`_validity` is the only field validator in the codebase and it is
  deterministic.** Given a fixed input pair it always returns the same verdict,
  which is why `tests/test_lead_signal.py:292-309` can assert
  `_merge(a, b) == _merge(b, a)` for the field conflicts it covers. That property
  is worth keeping through any refactor; the tests for it are indirect and only
  cover `website`, `rating` and `email`.
- **`_merge` is not restricted to `FIELDS`.** `dedup.py:192` iterates
  `set(a) | set(b)`, so an unexpected key from any source is merged, ranked and
  written (subject to `extrasaction="ignore"` at `main.py:125`). `_validity` will
  happily return `1` for a column it has never heard of. `020` §S9 flagged this;
  it is repeated here because `_validity`'s `return 1` at `dedup.py:140` is the
  mechanism that makes unknown columns merge silently instead of noisily.
- **`_validity` never validates `phone`.** `dedup.py:140` gives every phone the
  flat `1`. That is defensible — `normalize_phone` (`dedup.py:33`) is the phone
  validator, and it runs on the *dedup key* at `dedup.py:205` and `:228`, not on
  the stored value — but the stored value is raw until `main.py:194`, so between
  `_merge` and `main.py:194` a record's `phone` field is unnormalised and
  unscored. Pre-existing, out of scope here, noted so the next person does not
  "fix" it inside `_validity`.
- **`country` has no rule and resolves by lexicographic `str()` max.**
  `_trust_for("country", ...)` is `1` for every source (no `country` key in
  `_SOURCE_TRUST`), so `country` conflicts fall through to
  `str(value)` at `dedup.py:184` — meaning `"SA"` always wins and `"LB"` always
  loses, order-independently. Unreachable today: OSM (`osm.py:103`) and Wikidata
  (`wikidata.py:111`) only ever emit `"LB"`, and only Google emits `"SA"`
  (`google_places.py:285`), so no two-country pair is ever merged. `main.resolve_country`
  (`main.py:184`) also runs after dedup and would repair it. Fragile, not broken.
- **`pyproject.toml:35-37` does not match its own comment.** The wheel target is
  `include = ["scrapers/"]` with the comment "The package imports a top-level
  sibling module, so both must ship" — but the root modules are not included, so
  `scrapers/osm.py:4` `from httpclient import ...` would fail inside an installed
  wheel, as would the console script `leadminer = "cli:main"`
  (`pyproject.toml:33`). I could not execute a build to confirm the artifact
  contents (hatchling is not installed and the brief forbids installing it), so
  treat this as **verify with one command**:
  `python -m build --wheel && unzip -l dist/*.whl`. It is latent only because
  `.github/workflows/scrape.yml:39,69` still installs from `requirements.txt` and
  runs `python main.py`, never `pip install .`.

---

## Recommended order of work

1. **Add `contracts.py` and move the vocabulary into it** — `BusinessRecord`,
   `FIELDS`, `_FLOAT_FIELDS`, `_INT_FIELDS`, `_VOLATILE`, `_SOURCE_TRUST`,
   `_DEFAULT_TRUST`, `_EMAIL_OK`, `_EMAIL_BLACKLIST`, `_URL_OK`, and one
   `coerce_number()`. Leaf module, `re` and `math` only. This is the single
   change that unblocks items 2, 3 and 5 and it makes the `dedup → main` cycle
   (S2) structurally impossible rather than merely discouraged. `020` §S12
   already argues for the `BusinessRecord` half; `_validity` is the second
   reason.
2. **Delete `dedup._EMAIL_OK`; import the canonical email rule.** One line
   removes the placeholder-email displacement (S1) and makes `_validity` agree
   with `enricher.py:138-141` for the first time.
3. **Add `math.isfinite()` to the lat/lon branch of `_validity` and to
   `main.py:93`** (S1). Smallest possible change that stops `nan` coordinates
   entering the cumulative master; do it before item 2 if you want the bleeding
   stopped first.
4. **Decide whether `_validity` is a gate or a tiebreaker** (S2). Either move it
   to index 0 of the `rank()` tuple at `dedup.py:180-185`, or rename it and fix
   the docstring. Right now it is neither, and the `dedup.py:179-187` ordering
   is the reason two bad values survive per run.
5. **Delete the `-100` branch or name it** (S3), then **add rules for
   `review_count` / `lead_score` / `completeness_score`** (S3) driven by the
   shared table from item 1.
6. **Housekeeping, all ruff-verifiable, one commit:** delete the dead
   `import requests` (`enricher.py:2`) and `urljoin, urlparse`
   (`enricher.py:4`); move `from httpclient import ...` to the top of
   `enricher.py` (`enricher.py:128`, `E402`); delete the unused loggers in
   `scrapers/osm.py:1,7` and `scrapers/wikidata.py:1,7`; switch the three
   `from typing import Iterator` to `collections.abc` (`UP035`); replace
   `cli.py:46`'s `noqa` import-check with a real `try/except ImportError`; add one
   `logging.basicConfig` in `main()` to make `httpclient.py:178,204` reachable.
7. **Export `MIN_PHONE_DIGITS` and `digits_of()` from `dedup`** and use them at
   `main.py:138` and `enricher.py:285` (S3). Do this **before** anyone attempts
   item 1's `dedup → main` route; it is the other half of the mutually exclusive
   pair in S2.
8. **Verify the wheel** with one build command before the layout work in
   `030` starts, so the `src/` move is not built on a target that already ships
   a broken `scrapers/` package.
