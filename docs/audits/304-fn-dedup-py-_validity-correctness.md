# 304 — `_validity` (dedup.py:120) — correctness lens

## Verdict

`_validity` is correct *as a function* — every regex and numeric check behaves exactly as
written — but it is **structurally unable to do the job its docstring claims**, because
`dedup.py:182` places its return value in the **second** slot of the `_pick` ranking tuple,
behind `_trust_for` (`dedup.py:181`). Trust spans 1–3 and `_validity` only spans 0–3, so
whenever two sources differ in trust the validity check is never consulted. In practice that
means: **every syntactically invalid but non-empty `email` or `website` coming from OSM
(trust 3) beats a perfectly valid one from Wikidata (trust 2) or Google (trust 2)** — and
because `enricher.py:252` only fills `email` when it is falsy, that junk is then permanent
and scores the lead +21 while shipping in `sales_ready.csv`.

The second structural defect: `_validity` is a **type check masquerading as a plausibility
check for `lat`/`lon`** (`dedup.py:128-133`). `float(value)` accepts `nan`, `inf`, `999`,
`0` and `True`, all of which receive the **maximum** score of 3 — while `rating` four lines
below (`:139`) correctly range-checks. Combined with the lexicographic `str(value)` tiebreak
at `dedup.py:184`, `"nan"` **beats** `"33.8938"`, so a junk coordinate actively displaces a
real one.

---

## Findings

### S1 — `_validity` is ranked below `_trust_for`, so it can only break ties it never gets

- **Where:** `dedup.py:182` (the `rank` tuple places `_validity` second), `dedup.py:181`
  (`_trust_for` first), `dedup.py:97-110` (trust table).
- **Breaks:** `_validity` returns `0` for "checked and bad" and `3` for "checked and good",
  but those land in tuple slot #2 while trust occupies slot #1 with a wider range. So for
  `email` (osm 3, wikidata 2, google 1) and `website` (osm 3, google 2, wikidata 2) — the two
  fields that most need syntax validation — an invalid OSM value **always** wins. The winner
  is then unrepairable: `enricher.py:252` is `if not r.get("email") and contacts["email"]:`,
  so a good address scraped off the real website is discarded because a junk string is
  already present.
- **Trigger** (verified, executed against the real module):

  ```python
  osm = {"name":"Cafe des Arts","address":"Rue Verdun, Beirut","lat":33.8938,"lon":35.5018,
         "phone":"+96170123456","email":"info@localhost",           # OSM contact:email, no dot
         "country":"LB","source":"osm","scraped_at":"2026-10-01T00:00:00Z"}
  wd  = {"name":"Cafe des Arts","address":"Rue Verdun, Beirut","lat":33.8940,"lon":35.5020,
         "phone":"+96170123456","email":"contact@cafedesarts.example",   # Wikidata P968, valid
         "country":"LB","source":"wikidata","scraped_at":"2026-10-01T00:00:00Z"}
  ```

  ```
  _validity('email','info@localhost')              = 0     # correctly rejected
  _validity('email','contact@cafedesarts.example') = 3     # correctly accepted
  _trust_for('email', osm) = 3   vs   _trust_for('email', wd) = 2
  dedup([osm, wd]) -> 1 record, email = 'info@localhost'      # <-- the INVALID one
  dedup([wd, osm]) -> 1 record, email = 'info@localhost'      # deterministic, consistently wrong
  completeness_score = 3    (enricher.py:105)
  lead_score        = 55    (enricher.py:288 gives +20 for an undeliverable address)
  main.has_any_contact = True  -> row is written to sales_ready.csv
  ```

  The lead is scored as a **high-value, contact-ready prospect whose only email channel is
  undeliverable**. That is the product's core signal, inverted.
- **Fix:** one line — rank validity first for the format-checked fields, e.g. put
  `int(_validity(field, value) > 0)` in slot #1 of `rank` (`dedup.py:180-185`), or return a
  hard reject (e.g. `-1`) from `dedup.py:125/127` and have `_pick` drop the value outright.

### S2 — `lat`/`lon` are type-checked, not plausibility-checked; NaN scores the maximum

- **Where:** `dedup.py:128-133`.
- **Breaks:** `float(value)` succeeds for `nan`, `inf`, `-inf`, `"1e309"`, `"999"`, `"-999"`,
  `0`, `0.0`, `True` and `False`. All of them return **3 — the top score**, the same as a
  real Beirut coordinate. `rating` immediately below (`:139`) correctly does `0 <= v <= 5`, so
  the omission is plainly unintentional; `lat`/`lon` are the only coordinate values checked
  anywhere in the codebase (`enricher.infer_region` at `:89-93` just compares them, and
  `main.load_master` at `main.py:92` happily turns the CSV cells `"nan"`, `"inf"`,
  `"Infinity"`, `"1e999"` into `float('nan')`/`float('inf')`).
  Worse, the `str(value)` tiebreak at `dedup.py:184` is lexicographic, so `"nan"` sorts
  **above** every real coordinate.
- **Trigger** (verified):

  ```python
  _validity('lat', 'nan')   = 3      _validity('lat', '999')  = 3
  _validity('lat', 'inf')   = 3      _validity('lat', 0)     = 3
  _validity('lat', float('nan')) = 3 _validity('lat', True)  = 3
  'nan' > '33.8938' -> True
  _merge({"lat":nan,"lon":nan,"source":"wikidata"}, {"lat":33.8938,"lon":35.5018,"source":"wikidata"})
      -> {'lat': nan, 'lon': nan}        # junk beat the real coordinate
  infer_region(None, nan, nan) -> None   # region inference silently lost
  ```

  Once a NaN enters `all_businesses.csv` it is **self-perpetuating**: `write_csv`
  (`main.py:127`) writes `nan`, and `load_master` (`main.py:92`) reads it back as `float('nan')`
  forever. `lead_score` also loses its pain-point bonus silently, because `nan < 4.0` is
  `False` (`enricher.py:313`).
  Note also that `lat` and `lon` are ranked **independently** (`dedup.py:193` loops keys), so
  a merged record can pair a real latitude from one source with a NaN longitude from another —
  `_merge({"lat":33.8938,"lon":35.5018,"source":"osm"}, {"lat":nan,"lon":35.9,"source":"wikidata"})`
  returns `{'lat': nan, 'lon': 35.9}`, a coordinate pair present in neither input.
- **Reachability (honest):** today's three scrapers all emit finite floats, so this needs a
  non-finite or out-of-range value to enter the input — from a hand-edited master CSV, or from
  the next scraper that does not sanitise. It is a hardening gap that becomes data corruption
  the moment it happens, and the ranking inversion (`"nan"` > `"33.9"`) is unconditional.
- **Fix:** one line — after the `float()` at `dedup.py:130`, require `math.isfinite(v)` and
  the field's legal range (`-90<=v<=90` for `lat`, `-180<=v<=180` for `lon`), else `return 0`.

### S2 — Flat `1` for 19 of 22 columns leaves `str(value)` lexicographic sort deciding

- **Where:** `dedup.py:140` (`return 1`), consumed at `dedup.py:183-184`.
- **Breaks:** `dedup.py:183` forces recency to `""` for non-`_VOLATILE` fields, so for
  `lat`, `lon`, `name`, `address`, `category`, `phone`, `email` and all four social fields the
  **final discriminator is `str(value)`** — a string comparison. For numeric columns that is
  semantically wrong.
- **Trigger** (verified, and live): OSM and Wikidata both have **no** `lat`/`lon` entry in
  `_SOURCE_TRUST` (`dedup.py:102-105`, `:107-109`), so both score `_DEFAULT_TRUST = 1`;
  both coordinates are finite floats so both score validity 3; `lat`/`lon` are not in
  `_VOLATILE` (`dedup.py:114`).

  ```python
  _merge({"lat": 33.89,  "source": "osm"}, {"lat": 33.9, "source": "wikidata"})
      -> lat = 33.9          # because "33.9" > "33.89" as strings
  _merge({"review_count": "9", "scraped_at": "X", "source": "google_places"},
         {"review_count": "120","scraped_at": "X", "source": "google_places"})
      -> review_count = 9    # because "9" > "120" as strings
  ```

  Coordinates that are tens of metres apart are resolved by `str` ordering, and a business
  with 120 reviews is exported with 9.
- **Fix:** one line — make the tiebreak type-aware, e.g. compare
  `float(value)` when the field is in `{"lat","lon","rating","review_count"}` and fall back
  to `str(value)` otherwise (`dedup.py:184`).

### S3 — The `-100` sentinel is unreachable, and would not work if it were reached

- **Where:** `dedup.py:122-123`.
- **Breaks:** `_pick` returns early for any missing operand at `dedup.py:168-171`, so
  `_validity` is **only ever called when both values are present**. The `-100` branch is dead
  code from the caller's perspective (the only caller is `dedup.py:182`). It is also
  misplaced: even if reached, it would sit in slot #2 of `rank`, below `_trust_for` ≥ 1, so it
  could never outrank a present value. Verified: `_pick("email", {"email": None}, {"email":
  "real@b.com"})` returns `'real@b.com'` via the early return, `rank()` never called.
- **Fix:** delete `dedup.py:122-123` (it is `_pick`'s job), or move the rejection into `_pick`.

### S3 — `_URL_OK` has no `$` anchor, so multi-value and spaced URLs score as valid

- **Where:** `dedup.py:117`.
- **Breaks:** `^https?://[^\s/]+\.[^\s]+` matches a **prefix**. OSM `website` tags routinely
  carry several values or a human-readable suffix, and `scrapers/osm.py:95-96` only prepends
  `https://` when the tag lacks an `http` prefix — so the junk reaches `_validity` intact.
- **Trigger** (verified): `_validity("website", "https://x.com junk here")` → `3`;
  `_validity("website", "http://a.b/c d/e")` → `3`. Realistic input:
  OSM tag `website=www.a.example www.b.example` becomes `https://www.a.example www.b.example`
  → validity 3 → it survives `_merge`, is written verbatim to the CSV, and
  `enricher._fetch_website` issues a GET against a URL containing a literal space.
  `_EMAIL_OK` (`:116`) has the mirror-image problem: `$` matches *before* a trailing newline,
  so `_validity("email", "a@b.com\n")` is 3 — currently masked only because `:125` calls
  `.strip()` first.
- **Fix:** one line — anchor `dedup.py:117` with a full-string pattern, e.g.
  `re.compile(r"^https?://[^\s/]+\.[^\s]*$", re.IGNORECASE)`, and change the `$` in `:116`
  to `\Z`. Verified against all six cases above: it rejects both spaced inputs and still
  accepts `https://example.com/`, `https://example.com/path?q=1`,
  `https://sub.domain.example.com`, and still rejects `https://none`.

### S3 — `_validity` scores the stripped value but `_pick` returns the raw one

- **Where:** `dedup.py:125` and `dedup.py:127` call `str(value).strip()`; `dedup.py:187`
  returns the untouched `av`/`bv`.
- **Breaks:** a padded value is *judged* on its clean form and then *emitted* dirty.
- **Trigger** (verified): `_pick("website", {"website": "  https://zzz.example  ", "source":
  "wikidata"}, {"website": "aaa", "source": "wikidata"})` returns
  `'  https://zzz.example  '`. `main.write_csv` (`main.py:127`) writes that verbatim; every
  later run re-reads it through `load_master`, and `enricher.check_websites`
  (`enricher.py:231,238`) fetches the padded string.
- **Fix:** one line — normalise once in `_pick`, e.g. return `av.strip()` when
  `isinstance(av, str)` at `dedup.py:187`.

### S3 — `_is_missing` treats non-string falsy values as present, so `False` is a valid latitude

- **Where:** `dedup.py:86-90` (inspected by `_validity` at `dedup.py:122`).
- **Breaks:** `_is_missing` special-cases only `None` and `str`, so `0`, `0.0`, `False`, `[]`
  and `{}` are all "present".
- **Trigger** (verified): `_validity("lat", False)` → `3` (`float(False)` is `0.0`);
  `_validity("rating", False)` → `3`; `_validity("rating", True)` → `3`. A `False` in `lat`
  also passes `_pick`'s early return at `dedup.py:168`, so it is a value that "wins".
- **Fix:** one line — change `dedup.py:88` to `if isinstance(value, str): ...` →
  `if not isinstance(value, (int, float, str)) or value == "" or (isinstance(value, str) and not value.strip())`,
  or simply `return value is None or (isinstance(value, str) and not value.strip()) or
  (isinstance(value, (list, dict, tuple, set)) and not value)`.

---

## Not a bug, but worth knowing

- **`_validity` never rejects anything; it only orders.** It is called exclusively on
  *conflicts*, so a record with no competitor is never scored at all. Verified:
  `dedup([{"name":"Random Shop","lat":999.0,"lon":float("nan"),"rating":99.0,
  "email":"N/A","website":"https://none","review_count":0,"phone":"+96170999999",
  "address":"Somewhere, Beirut","category":"retail","country":"LB","source":"osm",
  "scraped_at":"X"}])` returns that record **untouched** — `completeness_score = 4`,
  `lead_score = 70`, and `r.get("website")` is truthy so it is written to `with_websites.csv`
  (`main.py:199`) with `lat=999`. If you want a quality gate, `_validity` is the wrong place;
  it needs a per-record validation pass before `dedup` (see audit 048).
- **`_trust_for` reads the *merged* `source` string, so trust compounds and never decays.**
  `_pick("source", ...)` unions the sources (`dedup.py:173-174`), and `_trust_for` takes the
  max over all of them (`dedup.py:148-152`). Verified: after one OSM+Wikidata merge,
  `_trust_for("website", merged) == 3` — the record now claims OSM-grade website trust
  **forever**, including on subsequent runs, because `main.py:179` reloads that
  `"osm|wikidata"` string from the master CSV. A fresh Wikidata website (trust 2) can never
  win against it again. This is why S1 gets worse every run rather than staying bounded.
- **`_pick`'s order-independence holds only for equal-typed values.** The `str(value)`
  tiebreak at `dedup.py:184` makes the rank tuple total, and `tests/test_lead_signal.py:292`
  verifies order-independence — but when `str(a) == str(b)` while the values differ in type,
  the `>=` at `dedup.py:187` keeps whichever side was passed as `a`. Verified:
  `_pick("lat", {"lat":33.9,...}, {"lat":"33.9",...})` returns the `float`, and swapping the
  arguments returns the `str`. Latent only because `main.load_master` (`main.py:92`) casts
  coordinates to `float`.
- **`_validity`'s `return 1` at `dedup.py:140` covers `source` and `scraped_at` but is never
  reached for them** — `_pick` short-circuits those two fields at `dedup.py:173` and `:176`.
- **`website` is in `_VOLATILE` (`dedup.py:114`) but "fresher wins" almost never fires**,
  because trust and validity occupy the two higher slots and tie rarely. It only applies
  between two records of the *same* source with equal validity — which, given
  `scrapers/google_places.py:263-266` de-duplicates on `place_id` and each scraper stamps a
  single batch-wide `scraped_at` (`osm.py:32`, `wikidata.py:67`, `google_places.py:173`),
  is a narrow window. The volatile-field logic is effectively dead for its stated purpose.

---

## What `_validity` gets right (stated precisely)

- `_EMAIL_OK` (`:116`) correctly rejects embedded whitespace, leading/trailing `@`, doubled
  `@`, and comma-joined address lists; it accepts the same strings `enricher._EMAIL_RE`
  (`enricher.py:130`) produces, so it will not fight the enricher.
- The `rating` branch (`:134-139`) is genuinely correct: it rejects `nan`, `inf`, `99`, `-1`
  and non-numeric input, and `tests/test_lead_signal.py:311` pins it. It is the model the
  `lat`/`lon` branch should have followed.
- The `try/except (TypeError, ValueError)` around every `float()` (`:129-131`, `:135-137`)
  means `_validity` cannot raise, so `_merge` cannot die here.
- `_pick`'s `str(value)` final tiebreak (`:184`) does make merges order-independent for
  same-typed values, which is the property the docstring at `:162-165` promises and which the
  test suite checks.

---

## Recommended order of work

1. **S1** — one-line change at `dedup.py:180-185`: make the format verdict outrank trust for
   `email`/`website`, or reject outright. Highest value per line changed in the file.
2. **S2 (lat/lon)** — add `math.isfinite` + range check at `dedup.py:130`. Same shape as the
   already-correct `rating` branch, so it is a copy-paste plus three conditions.
3. **S2 (tiebreak)** — make `dedup.py:184` type-aware so numeric fields compare numerically.
   Consider dropping `lat`/`lon` from tiebreak competition entirely and preferring the
   source with the finer precision.
4. **S3s** — the four regex/sentinel/strip/`_is_missing` items are independent one-liners;
   batch them with whatever touches this file next.
5. **Compounding trust** — record field-level provenance instead of a single unioned `source`
   string, otherwise the S1 fix is capped by whatever trust the master CSV has accumulated.
