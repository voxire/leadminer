# 020 — The type contract is documentation, not a contract; and one import escapes the package

## Verdict

`BusinessRecord` enforces nothing. It is a `TypedDict` used only as a constructor, so it
accepts missing keys, extra keys, and wrong types at runtime; it never crosses the
`scrapers/` boundary (every downstream function is `dict` / `list[dict]` / `Mapping`);
and the one place a *real* shape enters the system — `load_master()` reading the CSV back
in — performs zero header validation. The result is that the contract is violated
**silently**: there is no reachable `KeyError` or `TypeError` anywhere in the codebase,
because `csv.DictWriter`'s `restval=""` + `extrasaction="ignore"` convert every shape
violation into a blank cell. The two things that do corrupt data today are a UTF-8 BOM
silently deleting `name` from every record, and `.get(key, default)` used four times on a
field declared `str | None`.

Separately, `scrapers/google_places.py:26` (`from enricher import infer_region`) is the
**only** absolute top-level project import anywhere inside a package. It works solely
because the repo root is `sys.path[0]`; under a `src/` layout or an installed wheel it
is a hard `ModuleNotFoundError` on the same line that already correctly uses a relative
import.

---

## Findings

### S1 — `load_master` never validates the CSV header, so a UTF-8 BOM silently deletes `name` and collapses the dataset

- **Where:** `main.py:46-71` (the read path), `dedup.py:77-79` (where it detonates)
- **Breaks:** `main.py:51` opens with `encoding="utf-8"`, not `utf-8-sig`. A UTF-8 BOM
  (the default output of every Excel-on-Windows save, and of many Google Sheets
  round-trips) makes the first header cell parse as `'\ufeffname'`. Every loaded row then
  has **no `name` key**. `dedup.py:77` does `name = record.get("name") or ""`, so every
  phone-less business in the same city hashes to the key `("", "beirut")` and
  `_merge()` folds them into one record. Silent, irreversible, total data loss — and the
  output CSV is written with a correct `name` header and empty `name` cells, so the file
  looks structurally fine.
- **Trigger:** the master lives in Google Drive and is re-downloaded every run
  (`.github/workflows/scrape.yml` → `rclone copy "gdrive:leads/" data/`), and it is
  produced for humans to work in. Verified against the real code paths:

  ```
  header as read with encoding='utf-8': ['\ufeffname', 'country', 'phone', ...]
    'name' present on the row? -> False
  no-BOM dedup keys: [('name','cafe a','beirut'), ('name','cafe b','beirut'), ('name','cafe c','beirut')]
  BOM dedup keys   : [('name','','beirut')]   <- 3 businesses collapsed to 1
  ```
- **Fix:** `open(path, ..., encoding="utf-8-sig")` **and** validate the header before
  accepting a single row — `missing = set(FIELDS) - set(reader.fieldnames); if missing: raise`.
  The validation is the real fix; `utf-8-sig` only covers one cause of the same class
  (a mangled or renamed column behaves identically).

### S2 — `BusinessRecord` is runtime-inert and dies at the `scrapers/` boundary

- **Where:** `scrapers/base.py:5-28`, used at `osm.py:95`, `wikidata.py:75`,
  `google_places.py:249`
- **Breaks:** `BusinessRecord(...)` is a plain `dict` constructor. It validates nothing:

  ```
  missing key -> {'a': 'x'}
  bogus key   -> {'a': 'x', 'b': 1, 'zzz': 9}
  wrong type  -> {'a': 1, 'b': 'nope'}
  type of ctor result: <class 'dict'>
  ```

  So a dropped kwarg, a typo (`lead_scor=0`), or `review_count="12"` in one scraper ships
  clean. Then the annotation stops mattering entirely: grep shows `BusinessRecord`
  appears in **4 files, all under `scrapers/`**. Every function downstream is untyped —
  `dedup(records: list[dict])` (`dedup.py:64`), `enrich(records: list[dict]) -> list[dict]`
  (`enricher.py:266`), `completeness_score(record: dict)` (`enricher.py:101`),
  `lead_score(record: dict)` (`enricher.py:227`), `write_csv(..., records: list[dict])`
  (`main.py:74`), `has_any_contact(record: dict)` (`main.py:82`),
  `recommend_service(record: Mapping)` (`pitch_recommender.py:25`). And **`main.py` never
  imports `BusinessRecord` at all** — so the entry point, the CSV writer, and the loader
  are entirely outside the type system.
- **Trigger:** delete `whatsapp=None` from `osm.py:109`. Nothing complains — not the
  constructor, not a type checker (there is no type checker), not a test (there are no
  tests).
- **Fix:** (a) annotate the downstream boundary as `list[BusinessRecord]` rather than
  `list[dict]`; (b) make the scraper output honest with `NotRequired[...]` — see S4;
  (c) add the header assertion from S1 as the runtime backstop.

### S3 — The 23-key shape is declared twice (`BusinessRecord` and `main.FIELDS`) with nothing linking them

- **Where:** `scrapers/base.py:5-28` vs `main.py:32-39`
- **Breaks:** the two lists agree today — verified `same set as FIELDS: True`, 23 keys
  each — but the agreement is a coincidence of two hand-maintained lists. Add a column to
  `FIELDS` and scrapers keep working while `enricher`'s `setdefault` list
  (`enricher.py:280-288`) silently fails to cover it; drop a key from `BusinessRecord` and
  no tool notices.
- **Trigger:** add `linkedin_owner` to `FIELDS`. `write_csv` starts emitting a column
  that is `None` for every row forever, because nothing ever populates it.
- **Fix:** derive `FIELDS` from the contract — `FIELDS = list(BusinessRecord.__annotations__)`
  — or, better, delete `FIELDS` and let one canonical ordered contract drive both.

**Note on the brief:** `BRIEF.md:18` and `BRIEF.md:57` both say **22** keys/columns.
The code has **23**. The brief's own enumeration at `BRIEF.md:59-62` lists 23 items. The
count has never been reconciled, which is itself mild evidence that no one is checking.

### S4 — Required vs. optional is declared backwards: 4 fields are marked required that `main.py` does not guarantee

- **Where:** `scrapers/base.py:25-28` (`lead_score: int`, `source: str`,
  `scraped_at: str`, `completeness_score: int`) vs `main.py:117-137`
- **Breaks:** the contract says four fields are non-optional, but `main.py` filters
  (`raw_filtered`, `main.py:117`) and dedups (`main.py:125`) *before* anything ever sets
  them — `industry_priority` and `recommended_service` are only assigned at
  `main.py:132-133`, and `completeness_score` / `lead_score` only at `enricher.py:289-290`.
  Meanwhile a `master` record that never matched a fresh scrape keeps
  `lead_score: None`, `source: <whatever>`, `completeness_score: None`. So the four
  "required" keys are exactly the four most likely to be missing at the boundary where
  the type stops existing.
- **Fix:** mark `lead_score`, `completeness_score`, `industry_priority`,
  `recommended_service` as `NotRequired[int]`, and mark `name` required (it is the only
  field the product genuinely cannot function without, per S1). Verified `NotRequired`
  permits the partial dicts the pipeline actually produces.

### S5 — `.get(key, default)` used as an "if missing" guard on a `str | None` field; the default never fires

- **Where:** `main.py:136`, `dedup.py:71`, `dedup.py:93`, `enricher.py:274` — all
  `country`
- **Breaks:** `dict.get(k, default)` returns the default only when the **key is absent**.
  `country` is declared `str | None`, and `load_master` (`main.py:53-55`) turns an empty
  cell into `None` — a **present** key holding `None`. So every one of these four sites
  passes `None` where `"LB"` was intended. Two distinct corruptions follow:

  1. **Phone numbers.** `dedup.py:71` / `main.py:136` feed `None` into
     `normalize_phone`, whose `_COUNTRY_CODES.get(None, ("961","+961"))` (`dedup.py:13`)
     silently falls back to Lebanon. Verified:

     ```
     SA phone, country='SA' : +966501234567
     SA phone, country=None : +961966501234567     <- 15 digits, wrong country code
     ```

     A Saudi number becomes `+961` + `966501234567`. This is also the **dedup key**, so
     the corruption is stable but the CSV column is wrong — and `README.md:35` explicitly
     promises "Normalized to E.164 (+961 Lebanon, +966 KSA)".
  2. **Region inference is switched off.** `enricher.py:274` passes `country=None` into
     `infer_region`, whose default is `"LB"` — but an explicit `None` overrides the
     default. Then `enricher.py:85` (`if address and country == "LB"`) is False, so the
     **entire address-keyword path is skipped**, and `enricher.py:90` falls to
     `_LB_COORD_REGIONS`. A Lebanese master row with a blank `country` cell therefore
     loses region inference it would otherwise get, and a Saudi one is matched against
     Lebanon bounding boxes.

- **Trigger:** a row in `all_businesses.csv` whose `country` cell is empty. This is not
  hypothetical — `_merge` only repairs `country` when a record *matches* a fresh scrape
  (`dedup.py:50-51`), so the rows that keep `None` are precisely the old, never
  re-matched records, i.e. the accumulating historical value of the master.
- **Fix:** `r.get("country") or "LB"` at all four sites. The codebase already uses the
  correct idiom elsewhere — `main.py:83-85`, `main.py:168`, `main.py:176` all use `or`.

### S6 — `enricher.lead_score` guards `website_live` asymmetrically, awarding a live-site bonus to businesses with no site

- **Where:** `enricher.py:241-244`
- **Breaks:** the two branches do not test the same thing:

  ```python
  if record.get("website_live") is True:
      score += 10                                    # no website check
  elif record.get("website") and record.get("website_live") is False:
      score += 20                                    # website check present
  ```

  `load_master` (`main.py:68-69`) re-derives `website_live` from the CSV for *every* row,
  and `check_websites` (`enricher.py:182`) only re-fetches rows that *have* a website —
  so a row with `website_live=True` and no `website` keeps `True` all the way through
  `enrich()` (`enricher.py:286`'s `setdefault` only fills absent keys) and collects a +10
  "live website" bonus for a site that does not exist. `recommend_service` is correctly
  guarded against the same case (`pitch_recommender.py:33` derives `has_dead_website`
  from `has_website`), so the score and the recommendation disagree.
- **Trigger:** master row with `website_live` = `True` and `website` cell blank.
- **Fix:** make line 241 `if record.get("website") and record.get("website_live") is True:`.

### S7 — `website_live: bool | None` cannot express 403-vs-404-vs-5xx, so the product's #1 pitch tier fires on anti-bot blocks

- **Where:** `enricher.py:147-149`, `scrapers/base.py:16`, `pitch_recommender.py:33,54`
- **Breaks:** `live = r.status_code < 500`, then `if not live or r.status_code >= 400:
  return live, contacts`. So 403, 404, 429, 451, 500 and 502 all collapse to the single
  value `False`. Cloudflare / bot-protection 403s are extremely common on small-business
  sites. `pitch_recommender.py:54` treats `website_live is False` as
  *"Tier 1: dead website is the strongest pitch in the database... Sell the rebuild"*,
  and `enricher.py:244` gives +20 "dead website = sales opportunity". A helper is
  therefore told to pitch a rebuild at businesses whose sites are perfectly fine and
  simply refuse automated requests.
- **Trigger:** any site behind a WAF returning 403 to a non-browser UA
  (`enricher.py:125` sends `Mozilla/5.0 (compatible; leadminer/1.0)`).
- **Fix:** the contract is the problem — `bool | None` has no room for the distinction
  the product needs. Make it `website_status: Literal["live","dead","blocked","error"] | None`
  and gate Tier 1 on `== "dead"`.

### S8 — No shape violation can ever fail loudly: `restval=""` + `extrasaction="ignore"`

- **Where:** `main.py:76`, `main.py:53-55`
- **Breaks:** `write_csv` is configured to swallow both directions of shape drift.
  Verified:

  ```
  row missing 'name' + extra 'notes' written as: ',,03123456'
  restval default = ''
  ```

  A record missing all 23 keys writes 23 empty cells; a record carrying a helper-added
  `notes` column has it deleted without a word. Combined with S1 and S5, this is why the
  codebase produces **no** exceptions — it produces empty columns and wrong phone numbers
  instead.
- **Fix:** keep `extrasaction="ignore"` (forward tolerance is right) but add a
  `validate_record(r)` in `load_master` that raises on unknown keys and logs the diff on
  missing ones, so drift is visible on the run that introduces it.

### S9 — `dedup._merge` carries unknown keys through and lets them steer merge priority

- **Where:** `dedup.py:46` (`all_keys = set(a) | set(b)`), `dedup.py:60`
  (`_field_count(a) >= _field_count(b)`)
- **Breaks:** `_merge` is key-agnostic — it will merge and propagate any column present in
  either input, including ones the contract doesn't declare. Worse, `_field_count`
  (`dedup.py:42`) counts non-`None` values across **all** keys, so a helper-added
  `notes` column (always populated) inflates one side's score and silently flips which
  record wins the `av if ... else bv` decision for **every** field. The record that wins
  is chosen by a count the operator cannot see.
- **Trigger:** add a `notes` column to the master CSV; OSM-vs-Google conflicts now resolve
  differently depending on whether a human filled in notes.
- **Fix:** restrict `_merge` to `FIELDS`, and compute `_field_count` over the contract's
  keys only.

### S10 — Two of three pinned dependencies are never imported

- **Where:** `requirements.txt:2-3`, no matching import anywhere
- **Breaks:** `beautifulsoup4==4.12.3` and `lxml==5.2.2` are pinned but dead — grep for
  `bs4|BeautifulSoup|lxml` across all `.py` returns zero hits. `enricher.py` uses
  `re` only. Every CI run installs and resolves two unnecessary packages (plus their own
  transitive trees, since there is no lockfile).
- **Fix:** delete both lines.

---

## Not a bug, but worth knowing

- **There is no reachable `KeyError` or `TypeError` in the codebase.** I enumerated every
  site; the scorecard is genuinely clean, and it is clean *by accident*, not by design:

  | Site | Expression | Why it doesn't fire |
  |---|---|---|
  | `enricher.py:182` | `r["website"]` | Guarded by `r.get("website")` on the same line |
  | `main.py:146` | `r.get("completeness_score", 0) >= 1` | `enricher.py:289` overwrites the key unconditionally with an `int` **before** line 146 runs. Would be the first thing to break if `enrich()` were ever made conditional or a post-enrich append added. |
  | `enricher.py:253` | `rating < 4.0` | `main.py:42` casts `rating` to float on the CSV path; scrapers emit floats. Note `pitch_recommender._is_low_rating:135` guards the *same field* with `float()` in a `try` — only one of the two is type-safe. |
  | `enricher.py:92` | `lat_min <= lat <= lat_max` | `main.py:42` casts `lat`/`lon`. `infer_region` is nonetheless a public function with an unguarded comparison fed arbitrary `dict`s. |
  | `wikidata.py:62-70` | `row.get("itemLabel", {}).get("value")` | Only fires on an explicit JSON `null`, which well-formed SPARQL results don't emit. Genuinely unguarded. |

  **Conclusion:** the fix priority is *validation*, not `try/except`. Adding exception
  handling would treat the symptom and leave every silent corruption in this report intact.

- **`load_master`'s derived columns are re-derived every run anyway.** `website_live`,
  `completeness_score`, `lead_score`, `industry_priority`, `recommended_service` are all
  unconditionally overwritten by `check_websites`/`enrich`/`main.py:132-137`. Parsing
  them out of the CSV is dead work — except for rows with no website, where
  `website_live` is *not* re-derived, which is exactly what makes S6 reachable.

- **`load_master`'s boolean parser accepts exactly two spellings.** `main.py:69` matches
  only `"True"` / `"False"`. Google Sheets exports booleans as `TRUE`/`FALSE`, and
  `README.md:40` documents the column as lowercase `true`/`false`. Any spelling drift
  silently becomes `None` — indistinguishable from "never checked". Largely self-healing
  (rows with a website get re-fetched by `check_websites`), which is why this is filed
  here rather than as a finding. `strtobool` / accepting `{true, false, 1, 0, yes, no}`
  case-insensitively would close it.

- **`enricher.py` writes 9 keys and `main.py` writes 4 more** (`enricher.py:280-290`,
  `main.py:132-137`) that no constructor ever declared. The record type is only ever a
  *starting* shape; the *final* shape is defined by scattered assignment statements.

- **`osm.py:65` uses `or` to fall through `lat` → `center.lat`**, so a legitimate
  coordinate of `0.0` is discarded. Irrelevant for Lebanon, but it is the wrong operator
  for the job — `if el.get("lat") is None`.

- **`main.py:161` prints `dead` from `with_websites`, and S7 means that number is
  inflated by every WAF-blocked site.** The run summary overstates dead-site opportunity
  by an unknown margin.

---

## The import: `from enricher import infer_region`

### S11 — `scrapers/google_places.py:26` is the only absolute top-level project import inside a package, and it breaks under `src/` or a wheel

- **Where:** `scrapers/google_places.py:26`, immediately followed by the correct
  relative import on line 27
- **Breaks:** absolute imports resolve against `sys.path` entries only. A package's own
  directory is **never** placed on `sys.path`. So a module living inside the package is
  unreachable by its bare top-level name:

  ```
  line  26  from enricher import ...   -> ABSOLUTE (layout-dependent)
  line  27  from .base import ...      -> RELATIVE  (layout-safe)

  flat repo root (today): sys.path[0]=<repo root>   -> 'enricher'  OK,             '.base' OK
  src/leadminer/...     : sys.path[0]=<.../src>     -> 'enricher'  ModuleNotFoundError, '.base' OK
  installed wheel       : sys.path[0]=site-packages -> 'enricher'  ModuleNotFoundError, '.base' OK
  ```

  Line 26 fails **at import time**, before any scraping, so it is an instant, total
  failure of `main.py` — not a degraded run.
- **Trigger:** move the tree to `src/leadminer/` (the near-universal Python layout that
  makes `pip install -e .` work correctly) and `import scrapers.google_places` raises
  `ModuleNotFoundError: No module named 'enricher'`.
- **Fix:** `from ..enricher import infer_region` — one character of `..`, layout-independent,
  and consistent with the three sibling scrapers, which all use `from .base import`.

  Full-tree classification confirms this line is the unique violation of the codebase's
  own convention:

  ```
  scrapers/google_places.py:26   enricher          <- project module, absolute, INSIDE a package
  scrapers/osm.py:6              from .base        <- relative  OK
  scrapers/wikidata.py:6         from .base        <- relative  OK
  scrapers/google_places.py:27   from .base        <- relative  OK
  scrapers/whitelist.py:9        from .whitelist   <- relative  OK (documented in its own docstring)
  ```

### S12 — There is no cycle today, but the dependency direction is inverted and the type lives in the wrong layer

- **Where:** `scrapers/google_places.py:26` reaching up out of the package
- **Breaks:** `enricher.py` imports nothing from `scrapers/` (only `re`, `requests`,
  `concurrent.futures`, `urllib.parse`), so **no import cycle exists today** — worth
  stating plainly rather than inventing one. But the *direction* is wrong: `scrapers/` is
  the lowest layer (it produces rows), yet it reaches sideways-and-up into a module that
  sits above it. And the reason `enricher.py` can't simply be imported by a scraper is
  that it has no identity of its own — `enricher.py` is simultaneously "a top-level
  module" and "part of the leadminer domain", and nothing in the code says which.
- **Why it will bite:** the obvious next step for S2 is to annotate
  `enrich(records: list[BusinessRecord])`. That makes `enricher` import
  `scrapers.base`, producing `scrapers.google_places → enricher → scrapers.base`. It
  happens to still work (because `base.py` imports nothing but `abc`/`typing`, and
  `scrapers/__init__.py` is empty), but it is a real dependency from an upper layer back
  into a lower one, and it will become an actual cycle the moment `enricher` grows an
  import of any scraper module.
- **Fix:** move the contract out of `scrapers/` into a top-level `contracts.py` (or
  `models.py`) that `base.py`, `enricher.py`, `dedup.py` and `main.py` all import. That
  kills S11's root cause, gives `BusinessRecord` a single canonical home, and makes the
  layering honest: `contracts` ← `scrapers`, `contracts` ← `enricher`, `contracts` ← `main`.

### S13 — `main.py`'s imports and `DATA_DIR` are the other half of the same layout coupling

- **Where:** `main.py:24-30` (absolute top-level imports), `main.py:40`
  (`DATA_DIR = pathlib.Path("data")`)
- **Breaks:** `main.py`'s absolute imports are *correct today* — it is a top-level script,
  so `sys.path[0]` is the repo root and `from scrapers.osm import ...` resolves. But they
  break the instant `main.py` moves into the package for the S12 fix, where they must
  become `from .scrapers.osm import ...` / `from .enricher import ...`.
  `DATA_DIR` is CWD-relative: under `python -m leadminer` or any installed entry point,
  the master CSV is read from and written to whatever directory the process happens to
  start in. The GitHub Action happens to set CWD to the checkout root, which is the only
  reason it works.
- **Fix:** derive it from the package — `DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"`, or make it an explicit `--data-dir` argument (there is no CLI at all today).

---

## Recommended order of work

1. **S1** — `utf-8-sig` + header validation in `load_master`. One line each; prevents
   total, silent, irreversible dataset collapse. Do this first.
2. **S11** — `from enricher import` → `from ..enricher import`. One character; unblocks
   any future `src/` move or `pip install`.
3. **S5** — `or "LB"` at the four `country` sites. Stops KSA phone numbers being written
   as `+961966…` and stops region inference being silently disabled.
4. **S8 + S9** — `validate_record()` in `load_master`, and restrict `_merge` to `FIELDS`.
   Makes the next drift visible on the run that causes it.
5. **S3 + S4** — derive `FIELDS` from the contract; fix required-vs-optional with
   `NotRequired`. Then annotate `enrich`/`dedup`/`write_csv` with `BusinessRecord` (S2)
   and add a type checker to CI. Only worth doing once 1–4 stop the data from rotting.
6. **S12** — move `BusinessRecord` to a top-level `contracts.py`; this is the change that
   makes steps 5 and the `main.py` import fix (S13) coherent rather than three separate
   patches.
7. **S6, S7** — scoring correctness: the asymmetric guard, and widening `website_live` so
   Tier 1 pitches stop targeting WAF-blocked sites.
8. **S10** — delete the two dead pins.