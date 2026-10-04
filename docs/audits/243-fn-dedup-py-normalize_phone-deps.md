# 200 — `normalize_phone`: the import graph is clean, the contract graph is not

## Verdict

`normalize_phone` (`dedup.py:33`) is the cleanest function in the repository: its
transitive closure is **six edges, all terminal** — two stdlib modules, three
module-level literals, and one builtin. It imports nothing from the project, logs
nothing, and has no annotation-only imports. Verified mechanically: the internal
module import graph is a DAG with **no cycles**, and `dedup` is one of its sinks.

The problem is that the dependency map does not describe the risk. `normalize_phone`'s
only real input is `country`, and **`country` is defined five separate times in five
modules with three incompatible value sets, none of them imported by `dedup`.** Worse,
`main.py:180` calls `dedup()` *before* `main.py:183-184` resolves the country field, so
the identity key for every record is computed from a raw, unresolved value using a
locally duplicated `or "LB"` fallback (`dedup.py:205`, `dedup.py:228`). I reproduced a
cross-country merge: a Riyadh and a Beirut business both carrying the national number
`0501234567` collapse into **one row**, and the fabricated `+961501234567` is then
written into the cumulative master by `main.py:194`, where it is permanent.

Layering is already broken in one place that matters for the fix: `scrapers/google_places.py:26`
imports `enricher`, and `enricher.py:128` imports `httpclient`, which imports `requests`
at module scope (`httpclient.py:43`). The moment phone normalization grows a shared home
that `enricher` and the scrapers both want, the identity primitive inherits the network
layer.

---

## Dependency map

Transitive closure of `normalize_phone` (`dedup.py:33-63`). Depth 1 — there is no depth 2.

```
normalize_phone(phone: str, country: str = "LB") -> str      dedup.py:33
│
├── str                                     builtin        dedup.py:42   str(phone)
├── re          ── stdlib, terminal          dedup.py:1,44  re.sub(r"\D", "", raw)
│
├── _MIN_DIGITS          int 7               dedup.py:30    < len(digits)      dedup.py:45
├── _COUNTRY_CODES       dict 2 entries      dedup.py:23-26 .get(country,"961") dedup.py:48
└── _KNOWN_COUNTRY_CODES tuple, 12 entries   dedup.py:8-21  prefix loop        dedup.py:58-60
```

**Total: 5 direct edges, all terminal. Zero project-internal edges. Zero third-party edges.**

### Module-scope imports of `dedup.py` that `normalize_phone` does *not* use

| Import | Used by | Note |
|---|---|---|
| `unicodedata` (`dedup.py:2`) | `normalize_name` only (`dedup.py:67-68`) | Correctly scoped; does not touch `normalize_phone`. |

### The edges that are not in the import graph

These are the actual dependencies. All are **unimported contract dependencies** — changing
any producer silently changes `normalize_phone`'s output.

| Dependency | Defined at | Consumed at | Import edge? |
|---|---|---|---|
| `country` value domain (12 codes) | `dedup.py:8-21` `_KNOWN_COUNTRY_CODES` | `dedup.py:58` | same module |
| `country` value domain (2 codes) | `dedup.py:23-26` `_COUNTRY_CODES` | `dedup.py:48` | same module |
| Accepted country codes + aliases | `main.py:59-67` `resolve_country` | *should* be, via `dedup.py:205,228` | **none** |
| `country` produced from an English query string | `scrapers/google_places.py:127-136` `_country_from_query` | flows to `dedup.py:205` | **none** |
| `country` hardcoded `"LB"` | `scrapers/osm.py:103`, `scrapers/wikidata.py:111` | flows to `dedup.py:205` | **none** |
| `country` from the CSV, blanks already turned into `None` | `main.py:87-89` `load_master` | flows to `dedup.py:205` | **none** |
| "phone → digits" definition | duplicated at `main.py:138`, `enricher.py:285` | — | **none** |
| "`>= 7` digits" definition | duplicated at `main.py:142`, `enricher.py:292`, `enricher.py:133` | — | **none** |

### Internal import graph today (module scope, measured)

```
main ──▶ dedup, enricher, pitch_recommender, scrapers.{osm,wikidata,google_places,whitelist}
scrapers.google_places ──▶ enricher        scrapers.google_places ──▶ httpclient
scrapers.osm ──▶ httpclient               scrapers.wikidata ──▶ httpclient
enricher ──▶ httpclient
tests.test_lead_signal ──▶ dedup, enricher, main, pitch_recommender

sinks (no internal imports): dedup, httpclient, pitch_recommender, cli,
                             scrapers.base, scrapers.whitelist
```

**Cycles found: NONE.** `cli.py:35` and `cli.py:46-49` use function-local imports
specifically to avoid one; that is the right precedent and it is used nowhere else.

### Answers to the two specific questions asked

- **Imported only for a type annotation:** nothing in `dedup.py`. Repo-wide there are
  four, all in modules `normalize_phone` does not touch: `pitch_recommender.py:22`
  (`Mapping`, used only at `pitch_recommender.py:25`, and that module has **no**
  `from __future__ import annotations`, so it is a genuine runtime import serving only
  a type); `scrapers/osm.py:2`, `scrapers/wikidata.py:2`,
  `scrapers/google_places.py:22` (`Iterator`, annotation-only in all three);
  `httpclient.py:41` (`Any`, already deferred by `from __future__ import annotations`
  at `httpclient.py:32`). `scrapers/base.py:2` cannot be deferred — `TypedDict` is a
  runtime base class at `scrapers/base.py:5`.
- **Imported only for logging:** **nowhere in the repository.** `httpclient.py:35`,
  `scrapers/osm.py:1` and `scrapers/wikidata.py:1` are the only `logging` imports and all
  three back a real `log.debug` call — except `scrapers/osm.py:7` and
  `scrapers/wikidata.py:7`, which create loggers that are **never called**. `dedup.py`
  imports no logger at all (see S3-1).

---

## Findings

### S1 — `dedup()` runs before country resolution, so `normalize_phone` keys Saudi numbers under Lebanon and the corruption is written to the cumulative master

- **Where:** `dedup.py:205`, `dedup.py:228` (raw `record.get("country") or "LB"`),
  `dedup.py:48` (silent `_COUNTRY_CODES.get(country, "961")` fallback),
  consumed by `main.py:180` (`dedup(combined)`) which runs **before**
  `main.py:183-184` (`r["country"] = resolve_country(r)`), then persisted by
  `main.py:194` into `all_businesses.csv` (`main.py:209`).
- **Breaks:** the dedup key is derived from an unresolved `country`, using a fallback
  policy re-implemented locally in `dedup.py` instead of the canonical
  `resolve_country` (`main.py:50-74`). `_COUNTRY_CODES` has exactly two keys
  (`dedup.py:23-26`), so **every** country string it does not recognise — `"SAU"`,
  `"sa"`, `"KSA"`, `"Saudi Arabia"`, `None`, `""` — silently resolves to country code
  `961` (Lebanon) with no warning. `resolve_country` disagrees on six of twelve inputs:

  ```
  input            dedup.py:205 cc  main.py:74 resolve_country
  LB               961              LB
  SA               966              SA
  lb               961              LB
  sa               961              SA   <-- DISAGREE
  LEB              961              LB
  KSA              961              SA   <-- DISAGREE
  SAU              961              SA   <-- DISAGREE
  Lebanon          961              LB
  Saudi Arabia     961              LB   <-- DISAGREE
  ""               961              LB
  "  "             961              LB
  None             961              LB
  ```

  Because `main.py:180` precedes `main.py:184`, a Saudi business whose country cell is
  blank, lowercase, or a long name gets a Lebanese key. Any **Lebanese** business whose
  national number happens to match then merges into it. Verified end to end:

  ```
  input: [{'name':'Riyadh Dental','country':'SAU','phone':'0501234567','address':'Riyadh, Saudi Arabia'},
          {'name':'Beirut Auto',  'country':'LB', 'phone':'0501234567','address':'Beirut, Lebanon'}]

  dedup key used by dedup.py:205      -> +961501234567
  dedup() rows: 1   survivor: 'Riyadh Dental'   (the Beirut row is folded into it)
  ```

  Then `main.py:184` resolves `country` to `'SA'` and `main.py:194` writes
  `phone='+966501234567'` into the master — **a different key from the one dedup used**.
  Verified:

  ```
  after main.py:184 country='SA'  persisted phone='+966501234567'
  key this row will use in the NEXT run = '+966501234567'   (dedup key was +961501234567)
  ```

  Three irreversible consequences: (a) the Beirut business's OSM-sourced `email`,
  `website` and social handles are now attached to a Saudi row that was keyed as
  Lebanese; (b) the row's identity **changes between runs**, so on the next run it no
  longer merges with the recovered Lebanese record — one bad run yields permanent
  duplicate rows; (c) `+961501234567` is now in the cumulative master and `load_master`
  (`main.py:77-105`) never re-normalises `phone`, so the fabricated Lebanese E.164 is
  re-keyed identically forever:

  ```
  run 1 (raw + master):   persisted_phone=+961501234567
  run 2 (master reloaded): persisted_phone=+961501234567
  run 3:                  persisted_phone=+961501234567
  ```
- **Trigger:** any record reaching `dedup()` with `country` not exactly `"LB"`/`"SA"`,
  plus any Lebanese record with the same trailing 8 digits. `load_master` turns every
  blank cell into `None` (`main.py:87-89`), and `.github/workflows/scrape.yml:56`
  downloads the master from Drive every run — so this fires on every pre-`resolve_country`
  master and on every hand-edited `country` cell. `scrapers/google_places.py:127-136`
  derives `country` by substring-matching the English query text, so adding a query
  containing neither "lebanon"/"beirut" nor a KSA token silently reclassifies it as LB.
- **Fix:** resolve the country **inside** `dedup()` — `normalize_phone(raw_phone,
  resolve_country(record))` at `dedup.py:205` and `:228` — and move `resolve_country`
  into `dedup.py` (or a shared leaf module) so there is exactly one country policy;
  then move `main.py:183-184` and `main.py:192-194` before `main.py:180`.

### S1 — Multi-number and extension phone tags produce order-dependent, non-E.164 keys that are then persisted

- **Where:** `dedup.py:44` (`re.sub(r"\D", "", raw)` — concatenates *every* digit
  found), `dedup.py:58-63` (no upper length bound), fed by `scrapers/osm.py:89`
  (`tags.get("phone")`, a free-text tag) and `scrapers/wikidata.py:97`
  (`P1329`, RFC3966). Documented contract: `README.md:58` — "`phone` | Normalized to
  E.164"; `dedup.py:34` — "Normalize to E.164".
- **Breaks:** OSM `phone` tags routinely hold several numbers separated by `;` or `/`.
  `re.sub(r"\D", "", raw)` fuses them into one digit string, and because the result is
  then **order-dependent** rather than order-normalised, the same business keys
  differently when the tag order changes — so a re-scrape produces a *second* row
  instead of merging. Verified:

  ```
  '+961 1 234 567; +961 3 456 789'  LB -> '+96112345679613456789'   (20 digits; E.164 max is 15)
  '+961 1 234 567; +961 3 456 780'  LB -> '+96112345679613456780'   (same business, other order)
  '+961 1 234 567 ext. 4'           LB -> '+96112345674'            (extension glued to subscriber)
  '+9611234567'                     LB -> '+9611234567'             (canonical — never matches the above)
  ```

  The extension case is the same class: `"+961 1 234 567 ext. 4"` and `"+9611234567"`
  are the same line but produce different keys, and the first is written into
  `sales_ready.csv` as a number a human cannot dial. `dedup.py:45` enforces only a
  *lower* bound (`_MIN_DIGITS = 7`, `dedup.py:30`) and nothing rejects the 20-digit
  result, so the function returns a value it documents as impossible.
- **Trigger:** the OSM record at `scrapers/osm.py:89` with
  `phone="+961 1 234 567; +961 3 456 789"`. Re-run the scrape after an upstream OSM
  tag reorder and the master gains a duplicate.
- **Fix:** split on `[,;/]` before normalising and normalise each part, keeping the
  first that yields a plausible E.164; reject anything whose digit count exceeds 15
  (or hand the whole thing to `phonenumbers`, see S3-2).

### S2 — Five definitions of the country domain, three implementations of "phone → digits", four restatements of the `>= 7` threshold

- **Where:** `_COUNTRY_CODES` (`dedup.py:23-26`, 2 entries); `_KNOWN_COUNTRY_CODES`
  (`dedup.py:8-21`, 12 entries); `resolve_country`'s accepted set + aliases
  (`main.py:59-67`); `_country_from_query` (`scrapers/google_places.py:127-136`);
  hardcoded `"LB"` (`scrapers/osm.py:103`, `scrapers/wikidata.py:111`).
  Digit-stripping is duplicated at `dedup.py:44`, `main.py:138`, `enricher.py:285`, and
  `enricher.py:214` assembles a fourth phone value (`"+" + m.group(1)`).
- **Breaks:** `_MIN_DIGITS = 7` has one definition (`dedup.py:30`) and four
  independent restatements — `main.py:142` (`len(phone) >= 7`), `enricher.py:292`
  (`len(phone) >= 7`), `enricher.py:133` (`\d{7,15}`). Change the threshold in one
  place and the pipeline's four notions of "has a phone" disagree. The country domain is
  worse: the S1 table above is what five independent definitions produce. `resolve_country`
  itself hardcodes its codes at `main.py:62-67` rather than reading `dedup.py:23-26`.
- **Trigger:** add `"EG": "20"` to `_COUNTRY_CODES` only. `normalize_phone` starts
  emitting Egyptian keys; `resolve_country` still returns `"LB"` (it does not know
  `"EG"`), `main.py:209` writes `country=LB` next to a `+20…` phone, and
  `scrapers/whitelist.py` is untouched. No test, no import, and no type checker fails.
- **Fix:** one `leadminer.countries` leaf module exporting `COUNTRY_CODES`,
  `resolve_country`, and `KNOWN_CALLING_CODES`; import it from all five sites.

### S2 — `leadminer validate` counts duplicates on the raw `(name, phone)` pair, so the only data-quality gate structurally disagrees with the pipeline's identity function

- **Where:** `cli.py:190` — `dupes = total - len({(r.get("name"), r.get("phone")) for r in rows})`.
  It never imports or calls `normalize_phone`.
- **Breaks:** the gate is blind in both directions. It cannot see the collisions
  `dedup()` silently performed (S1), and it *does* flag rows that `dedup()` correctly
  kept apart — two spellings of the same number count as distinct. Verified:

  ```
  rows:  ('Beirut Auto', '+961 1 234 567')      same business as next row
         ('Beirut Auto', '+9611234567')
         ('Riyadh Auto', '0501234567')
         ('Beirut Auto', '0501234567')           DIFFERENT business (Lebanon)

  distinct raw keys: 4 of 4 rows -> `validate` reports 0 duplicates
  dedup() output rows: 3          <- one real cross-country collision, invisible to validate
  ```
- **Trigger:** run `leadminer validate data/all_businesses.csv` after a run that merged
  two businesses. It reports "all checks passed".
- **Fix:** compute the duplicate count over `normalize_phone(phone, resolve_country(row))`
  plus `normalize_name`/`_extract_city` — i.e. have `validate` import the *same* key
  function `dedup` uses, and export that key builder from one module.

### S2 — `leadminer score` and `leadminer run` write different `phone` columns from the same input file

- **Where:** `cli.py:265-272` (`cmd_score`) calls `pipeline.resolve_country` at
  `cli.py:266` and then **never** calls `normalize_phone`; `main.py:192-194` does both.
- **Breaks:** two operator commands on the same CSV disagree on the product's identity
  column. `cmd_score` also rewrites the file in place by default (`cli.py:274-275`), so
  running `leadminer score` on the master *reverts* every `+961…`/`+966…` phone to the
  raw form it had before the last `run` — and the next `run` will re-derive keys from
  that. Verified:

  ```
  input {'name':'X','country':'SA','phone':'0501234567'}
  `leadminer score` phone -> '0501234567'
  `leadminer run`   phone -> '+966501234567'
  ```
- **Trigger:** `leadminer score data/all_businesses.csv` (no `--out`), then
  `leadminer run`. Every phone key in the master changes shape between the two commands.
- **Fix:** have `cmd_score` call the same normalisation helper as `main.py:194`, and
  extract that loop into one function both call.

### S3 — `dedup.py` emits no telemetry at all, and the two loggers that do exist are dead

- **Where:** `dedup.py` imports only `re` and `unicodedata` (`dedup.py:1-2`) — no
  `logging`, no `print`. `scrapers/osm.py:7` and `scrapers/wikidata.py:7` create
  `logging.getLogger(__name__)` and **never call it** (both files use `print`
  throughout). `httpclient.py:45` is the only logger with call sites
  (`httpclient.py:178`, `httpclient.py:204`).
- **Breaks:** `dedup()` is the only stage that makes irreversible merge decisions, and
  it is the only decision-making module with no way to report one. The S1 cross-country
  merge, the S1 junk-concatenation, and every `_MIN_DIGITS` rejection at `dedup.py:46`
  are silent — and after `main.py:209` writes the master, they are unrecoverable.
  `.github/workflows/scrape.yml:69` runs `python main.py`, not the CLI, so not even the
  CLI's shallow checks run in CI.
- **Trigger:** any run containing a bad key; `grep` the workflow log for the business
  name and find nothing.
- **Fix:** add `log = logging.getLogger(__name__)` to `dedup.py` and log at INFO: the
  number of records keyed by phone vs name, the count of `normalize_phone` rejections
  (`dedup.py:46`), and any key that changed shape between runs. Delete the two unused
  loggers.

### S3 — The manifest already declares the replacement for this whole function; no module references it

- **Where:** `pyproject.toml:30` — `geo = ["phonenumbers>=8.13"]`, commented
  "Heavy, optional, and deliberately not required to run the pipeline."
- **Breaks:** `grep -rn phonenumbers --include='*.py'` returns **nothing**. The
  packaging declares a dependency whose entire purpose is to replace `dedup.py:8-63`
  (country-code detection, plausibility, E.164 output) and the dependency graph does not
  know it exists. `.github/workflows/scrape.yml:39` installs `requirements.txt`, not
  `pyproject.toml`, so the extra can never be present in CI regardless. Meanwhile
  `requirements.txt:2-3` still pins `beautifulsoup4` and `lxml`, which nothing imports.
- **Trigger:** `pip install -e . && python -c "import phonenumbers"` → `ModuleNotFoundError`.
- **Fix:** either wire `phonenumbers` in behind the `geo` extra and make
  `normalize_phone` delegate to it, or delete the extra. Leaving a declared dependency
  that no module imports makes the dependency graph untrustworthy for exactly the kind
  of analysis this report performs.

### S3 — The circular import this restructure will create

- **Where:** `scrapers/google_places.py:26` (`from enricher import infer_region`),
  `enricher.py:128` (`from httpclient import DEFAULT_MAX_BODY_BYTES, get_session`),
  `httpclient.py:43` (`import requests`). `enricher.py:2` also imports `requests` at
  module scope.
- **Breaks:** `normalize_phone` is currently a leaf and moves anywhere for free. The
  natural next step is to give it a shared home — `leadminer/normalize.py` — so
  `dedup.py`, `main.py`, `enricher.py` and the scrapers stop duplicating it. The moment
  `enricher.py` imports that module for the `_WHATSAPP_RE` work at `enricher.py:214`
  and `enricher.py:285`, **and** that module reaches back for `infer_region` (the
  obvious way to infer a country from `lat`/`lon`, which is what
  `scrapers/google_places.py:275` already does), the edge closes:

  ```
  enricher ──▶ normalize ──▶ enricher      CYCLE
  ```

  `docs/audits/030-packaging-import-hygiene.md` proposes the
  `src/leadminer/{main,dedup,enricher,pitch_recommender,scrapers/}` layout but does not
  name this edge, so the cycle would be introduced *by* the restructure.
- **Trigger:** add `from enricher import infer_region` to the new shared module.
- **Fix:** before the restructure, break the existing inversion —
  `scrapers/google_places.py:26` should not import `enricher`. Move `infer_region`
  (`enricher.py`, called from `scrapers/google_places.py:275`) into the same leaf
  module as `normalize_phone`; then `scrapers`, `dedup` and `enricher` all depend on a
  `leadminer.normalize` leaf that imports nothing but the stdlib, and stays one.

### S3 — A pure, dependency-free function cannot be unit-tested without a 40-line `requests` stub

- **Where:** `tests/test_lead_signal.py:20` inserts the repo root into `sys.path`;
  `tests/test_lead_signal.py:23-63` is `_stub_requests()`; `tests/test_lead_signal.py:65-68`
  imports `dedup`, `enricher`, `main`, `pitch_recommender`; `tests/test_lead_signal.py:66`
  is what forces the stub (importing `enricher` reaches `requests` via `httpclient`).
- **Breaks:** `normalize_phone` has no third-party dependency, yet testing it in the
  same file as anything else requires faking one. Reproduced in a clean checkout with no
  dependencies installed:

  ```
  >>> from main import resolve_country
  File ".../scrapers/osm.py", line 4, in <module>
      from httpclient import fetch_with_retry, get_session, utc_now_iso
  File ".../httpclient.py", line 43, in <module>
      import requests
  ModuleNotFoundError: No module named 'requests'
  ```

  This is the cost of the `enricher` inversion (S3 above) showing up as test
  infrastructure. Also `tests/test_lead_signal.py:122` contains
  `.replace("2", "2")`, a no-op left in the assertion.
- **Fix:** split the phone tests into their own module that imports only `dedup`; delete
  `_stub_requests` once the `scrapers → enricher` edge is removed.

---

## Not a bug, but worth knowing

- **`_KNOWN_COUNTRY_CODES` ordering is correct.** No code in the tuple is a prefix of
  another (checked all 144 pairs), so the loop at `dedup.py:58-60` cannot shadow. The
  two-digit `"20"` (Egypt, `dedup.py:20`) is correctly ordered last — no three-digit code
  begins with `20`. The problem with `"20"` is only that it is a *two*-digit code matched
  by `startswith` against un-normalised digits: `normalize_phone("20123456", "LB")`
  returns `"+20123456"`, a 6-digit Egyptian subscriber number, for any 7–8 digit input
  beginning `20`.
- **Unknown country codes are relabelled, not rejected.** `dedup.py:63` prepends the
  *requested* code to anything it does not recognise, so
  `normalize_phone("0044 20 7123 4567", "LB")` → `"+961442071234567"` — a
  plausible-looking but wrong E.164, which is worse than the `""` that `dedup.py:46`
  returns for short input. With `_KNOWN_COUNTRY_CODES` limited to 12 codes
  (`dedup.py:8-21`), every ex-pat and international number in the region hits this.
- **`dedup.py:228` re-derives the whole phone index to filter name-keyed records** — a
  second pass calling `normalize_phone` on every name-keyed record. Correct, but it
  means any future non-determinism in `normalize_phone` is evaluated twice per record.
- **`pyproject.toml:36-37` is a stale workaround.** The comment reads "The package
  imports a top-level sibling module, so both must ship" with
  `include = ["scrapers/"]` — but `main.py:29-31`, `enricher.py:128`,
  `scrapers/*.py:4` and `cli.py:35` all still use top-level absolute imports, so the
  wheel ships `scrapers/` while `dedup`/`enricher`/`httpclient`/`main` remain
  unshippable top-level modules. See `docs/audits/030-packaging-import-hygiene.md`.
- **`resolve_country` is only used in 3 of 5 places.** `main.py:184`, `cli.py:266` and
  `tests/test_lead_signal.py:67` call it; `dedup.py:205` and `dedup.py:228` do not. That
  asymmetry is the whole of S1.

---

## Recommended order of work

1. **Create `leadminer/countries.py`** (leaf; stdlib only) exporting `COUNTRY_CODES`
   (= today's `_COUNTRY_CODES`, `dedup.py:23-26`), `KNOWN_CALLING_CODES`
   (= `_KNOWN_COUNTRY_CODES`, `dedup.py:8-21`) and `resolve_country`
   (moved from `main.py:50-74`). Re-point `_country_from_query`
   (`scrapers/google_places.py:127-136`) and the hardcoded `"LB"` values
   (`scrapers/osm.py:103`, `scrapers/wikidata.py:111`) at the same constant.
2. **Fix S1-1.** Call `resolve_country(record)` inside `dedup()` at `dedup.py:205` and
   `dedup.py:228`; make `_COUNTRY_CODES.get` fail loudly instead of defaulting to
   `"961"` (`dedup.py:48`). Move `main.py:183-184` and `main.py:192-194` **above**
   `main.py:180`. Add a regression test for the verified trigger: a `country="SAU"`
   record and a `country="LB"` record both on `0501234567` must stay 2 rows.
3. **Fix S1-2.** In `normalize_phone`, split `raw` on `[,;/]`, normalise each part, keep
   the first plausible E.164 (digits 8–15), and return `""` otherwise. Add the upper
   bound at `dedup.py:45` so the function cannot emit a non-E.164 value.
4. **Export one key builder** (`identity_key(record) -> str | tuple`) from the same leaf
   module and consume it from `dedup.py`, `cli.py:190` and `cli.py:265-272` — resolves
   S2-5 and S2-6 together and makes `validate` mean something.
5. **Break the `scrapers → enricher` edge** (S3) by moving `infer_region` into the leaf
   module beside `normalize_phone`, *before* the `src/` restructure in
   `030-packaging-import-hygiene.md` — otherwise that restructure introduces a cycle.
6. **Hygiene:** delete the dead loggers at `scrapers/osm.py:7` / `scrapers/wikidata.py:7`;
   add a real logger to `dedup.py` with the counters listed in S3-7; add
   `from __future__ import annotations` to `pitch_recommender.py` and move `Mapping`
   under `TYPE_CHECKING`; remove the no-op `.replace("2", "2")` at
   `tests/test_lead_signal.py:122`; wire `phonenumbers` in behind the `geo` extra or
   delete it from `pyproject.toml:30`.