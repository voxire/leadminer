# 200 — `_extract_city` correctness (`dedup.py:72-76`)

## Verdict

`_extract_city` is a 4-line function that returns *whatever token happens to be last
before a comma*, and it is the second half of the fallback identity key
(`dedup.py:214`). In practice it is **anti-informative**: for every Google Places record
it returns the **country name** (`"lebanon"` / `"saudi arabia"`), for OSM records it
returns `addr:district` (a neighbourhood, not a city) because `osm.py:80-87` puts
district last, and for any address with no ASCII comma it returns **the entire address**.
The function has no invariant that makes it correct. It is neither "returns a city" nor
"returns a locality-like label" — it returns the last comma token, and all three of the
scrapers can put a non-city there. Both failure directions are live and I reproduced
each: **two distinct branches of the same chain in different cities merge into one
exported lead whose address, latitude and region come from three different physical
locations**, and **the same single business appears as two rows forever** in the
cumulative master because Google and OSM disagree on where the city token is.

Because `city` is not a column in `FIELDS` (`main.py:33-40`), neither error is visible
in the shipped CSVs. You cannot audit it post-hoc from the product.

**Callers:** exactly one. `dedup.py:213`, inside the `else` branch of `dedup()` that is
taken when `normalize_phone` returns `""` (`dedup.py:205-206`). No other module imports
it. Everything below is therefore scoped to records with **no usable phone** — which is
exactly the population that has no other identity signal.

## Call chain

```
main.main()  main.py:180   records = dedup(combined)
dedup.dedup  dedup.py:211  else:  # normalize_phone returned "" at dedup.py:205
dedup.dedup  dedup.py:213  city = _extract_city(record.get("address"))
dedup.dedup  dedup.py:214  key  = (normalize_name(name), city)
```

Address sources (the only three possible inputs to `_extract_city`):

| Source | Field | Line | Shape |
|---|---|---|---|
| Google Places | `formattedAddress` | `scrapers/google_places.py:286` | `"…, Beirut, Lebanon"` — **country last** |
| OSM | built from tags | `scrapers/osm.py:80-87` | `housenumber, street, suburb, city, **district**` |
| Wikidata | `P6375` | `scrapers/wikidata.py:99` | street literal, `str` or `None` |
| Previous export | `address` column | `main.py:77-105` `load_master` | whatever a human ever typed |

Google's own Text Search docs put the country last in every documented
`formattedAddress` example (`"367 Pitt St, Sydney NSW 2000, Australia"`,
`"350 Bay St #100-178, San Francisco, CA 94118, USA"`) — and warn that the component
set varies by location.

## Findings

### S1 — `_extract_city` returns the country for every Google record, so the name key degenerates to just the name

- **Where:** `dedup.py:76` (`return parts[-1] if parts else ""`), fed by
  `scrapers/google_places.py:286` (`address=place.get("formattedAddress")`).
- **Breaks:** `formattedAddress` always ends with the country name, so for every
  phone-less Google record the key is `(normalized_name, "lebanon")` or
  `(normalized_name, "saudi arabia")`. The city dimension is a **constant** and carries
  zero information. Two same-named businesses in different cities of the same country
  therefore share a key and are fused by `_merge` (`dedup.py:208`). This affects the
  bulk of the dataset: 68 of the queries (`google_places.py:46-125`) are Google, and
  Places records for chain brands, malls and landmarks frequently have no
  `nationalPhoneNumber`, which is precisely the condition that routes them here.
- **Trigger (executed, real `dedup()` call):**
  ```
  rec(name="Fitness First", address="Hamra Street, Beirut, Lebanon",  lat=33.888,
      rating=4.2, region="Beirut",        website="https://ff-lebanon.com")
  rec(name="Fitness First", address="Central Road, Tripoli, Lebanon", lat=34.436,
      rating=3.9, region="North Lebanon", website="https://ff-tripoli.com")
  → dedup output: 1 row
  ```
  Direct function evidence that the key is constant:
  ```
  _extract_city('Rue Hamra, Beirut, Lebanon')        -> 'lebanon'
  _extract_city('Hamra Street, Hamra, Beirut, Lebanon')-> 'lebanon'
  _extract_city('Clemenceau Street, Beirut, Lebanon')  -> 'lebanon'
  _extract_city('Bliss Street, Beirut, Lebanon')       -> 'lebanon'
  _extract_city('King Fahd Road, Al Olaya, Riyadh, Saudi Arabia') -> 'saudi arabia'
  ```
- **Fix:** stop parsing `formattedAddress` — add `places.addressComponents` to
  `FIELD_MASK` (`google_places.py:32-43`) and take `locality`, falling back to
  `administrativeArea_level_1`, so the city is a structured field rather than a token
  position.

### S1 — an over-merge at `dedup.py:214` produces a lead with an address, a pin and a region from three different countries' worth of geography

- **Where:** `dedup.py:214` (bad key) + `dedup.py:187` / `dedup.py:193`
  (`_pick` ranks and returns **per field**, so a fused pair does not keep one record's
  geography — it keeps the best *field* from each side).
- **Breaks:** `_pick` picks `address`, `lat`, `lon` and `region` independently. Two
  merged branches produce a row that is internally inconsistent: a Beirut street address
  with a Tripoli latitude and a Beirut region label. `region` is *not* recomputed after
  the merge because `enricher.py:330-331` only calls `infer_region` when
  `r.get("region")` is falsy, and both Google inputs already had it
  (`google_places.py:275`). This row goes straight into `sales_ready.csv`
  (`main.py:202-205`) and is handed to a human to pitch — at an address that does not
  match the pin on the map.
- **Trigger:** the `Fitness First` pair above. Actual output row:
  ```
  -> name='Fitness First' addr='Hamra Street, Beirut, Lebanon'
        lat=34.436 region='Beirut' rating=4.2 website='https://ff-tripoli.com'
  ```
  `addr` is Beirut (33.888), `lat` is Tripoli (34.436), `website` is the Tripoli branch.
  One exported lead, two real businesses, address/website say Beirut and the pin is in
  Tripoli.
- **Fix:** when two records fuse, keep a single record's geospatial block
  (`address`, `lat`, `lon`, `region`, `city`) together as one unit in `_pick` rather
  than field-by-field, so a merged row is always internally coherent.

### S1 — `""` is the most attractive key in the dataset: every address-less record of the same name fuses across countries

- **Where:** `dedup.py:73-74` (`if not address: return ""`) and `dedup.py:76`.
- **Breaks:** six distinct inputs all return `""` — verified:
  `_extract_city(None)`, `_extract_city('')`, `_extract_city('   ')`,
  `_extract_city(',')`, `_extract_city(',,')`, and `_extract_city('Hamra Street, ,')`.
  Because `country` is **not part of the key** (`dedup.py:214` keys on name+city only),
  a Lebanese and a Saudi record with no address and the same name collide. `P6375` is
  frequently absent on Wikidata (`wikidata.py:99`) and OSM's address is `None` whenever
  none of the five `addr:*` tags are present (`osm.py:87`), so `""` is a large bucket.
- **Trigger (executed):**
  ```
  rec(name="Al Baik", country="LB", address=None, lat=33.90)   # Beirut
  rec(name="Al Baik", country="SA", address=None, lat=24.71, region="Riyadh")
  → dedup output: 1 row: country='SA' addr=None lat=33.9 region='Riyadh'
  ```
  A row that claims `country=SA` / `region=Riyadh` with a Beirut latitude. `resolve_country`
  runs *after* dedup (`main.py:183-184`), so it cannot catch this.
- **Fix:** include `country` in the fallback key — `key = (normalize_name(name), record.get("country"), city)`
  — and refuse to emit a name-keyed record whose key components are empty.

### S1 — Google and OSM disagree on where the city token is, so a single business is two rows in the cumulative master, permanently

- **Where:** `dedup.py:76` interacting with `scrapers/osm.py:80-87`, whose
  `addr_parts` order is `housenumber, street, suburb, **city, district**` — district is
  last, so `_extract_city` returns a **neighbourhood**, and it returns a **city** for
  exactly those OSM records whose mapper left `addr:district` empty.
- **Breaks:** the granularity of the key silently alternates between city-level and
  neighbourhood-level for the same source, and Google always says "country". The same
  business is therefore split. Because `all_businesses.csv` is loaded back in on every
  run (`main.py:150`, `main.py:179`) and both halves are re-emitted, the duplicate is
  **permanent and self-perpetuating**: the row count never drops below the wrong value.
  This is the audit-003/028 failure mode, still live.
- **Trigger (executed), plus a 4-run stability loop:**
  ```
  google: name="Chez Ramy" address="Zalka, Beirut, Lebanon" phone="01 999 999"
  osm   : name="Chez Ramy" address="Zalka, Beirut"          phone=None
  → city keys: 'lebanon' vs 'beirut'
  → dedup output: 2 rows
    -> addr='Zalka, Beirut, Lebanon' phone='01 999 999' source='google_places'
    -> addr='Zalka, Beirut'          phone=None         source='osm'
  after run 2: 2 rows   after run 3: 2 rows   after run 4: 2 rows
  ```
  Note the phone-bearing half does **not** rescue this: the `captured_phones` filter at
  `dedup.py:226-229` can only drop a name-keyed record whose **own** phone is already
  captured, and the OSM record has none. Same result with the realistic multi-source
  split (`"Rue Mogher, Beirut, Lebanon"` → `'lebanon'` vs `"Rue Mogher, Beirut"` → `'beirut'`).
- **Fix:** key the fallback on a value both sources agree on — `(normalize_name(name), region_or_country)`
  where `region` comes from `enricher.infer_region`'s coordinate boxes
  (`enricher.py:58-76`) — and stop reading `addr:district` as a city.

### S2 — `،` (U+060C), `;`, `-`, `·`, `\n` are not delimiters, so the whole address including the country becomes the key

- **Where:** `dedup.py:75` — `address.split(",")`, ASCII comma only.
- **Breaks:** for a Lebanon/Saudi scraper this is the expected failure mode on
  Arabic-formatted addresses (`،` is the standard Arabic comma; hand-entered OSM tags
  and Wikidata literals use it, and the Places request sends no `languageCode`, so
  Google may return a localized `formattedAddress`). The function returns the *entire*
  address, which means (a) the country distinction is destroyed, so an LB and an SA
  record with the same name and Arabic-comma addresses fuse, and (b) the key now depends
  on every whitespace and punctuation byte of the address, so any formatting difference
  splits. `.lower()` at `dedup.py:75` is a **no-op for Arabic**, and unlike
  `normalize_name` (`dedup.py:67-68`) no NFKD normalization is applied, so Arabic
  presentation forms and Arabic-Indic vs ASCII digits (`'١٢ شارع الحمراء، بيروت'`) do
  not compare equal to their ASCII twins.
- **Trigger (executed):**
  ```
  _extract_city('شارع الحمراء، بيروت، لبنان') -> 'شارع الحمراء، بيروت، لبنان'   (unsplit)
  _extract_city('Zalka - Beirut')             -> 'zalka - beirut'
  _extract_city('Zalka; Beirut')              -> 'zalka; beirut'
  _extract_city('Zalka · Beirut')             -> 'zalka · beirut'
  _extract_city('Zalka\nBeirut')             -> 'zalka\nbeirut'
  _extract_city('Zalka Beirut')               -> 'zalka beirut'
  dedup([osm "شارع الحمراء، بيروت، لبنان", google "شارع الحمراء، بيروت، لبنان"])
    -> 1 row, source='google_places|osm'    (country lost, both markets fuse)
  ```
- **Fix:** split on `[,\u060C;·\n]` after collapsing whitespace, or better, match
  against a known-locality list and fall back to `""` (an unknown locality is safer
  than a whole address in the key).

### S2 — the last token may be a postal code, making an entire postcode one "city"

- **Where:** `dedup.py:76`. Reachable from `scrapers/google_places.py:286` (Google
  varies the component order by location) and, more reliably, from the
  `load_master` path (`main.py:77-105`) where `address` is free text carried over from
  a previous export.
- **Breaks:** every business sharing a postcode gets the same key component. Saudi
  postal codes are 5-digit district identifiers, so a single value covers hundreds of
  businesses.
- **Trigger (executed):**
  ```
  _extract_city('Al Olaya, Riyadh, 12271')    -> '12271'
  _extract_city('Al Malaz, Riyadh, 11564')   -> '11564'
  _extract_city('Al Andalus, Jeddah, 23431')  -> '23431'
  ```
- **Fix:** drop trailing all-digit tokens (and `len(p) <= 3` abbreviations) before
  taking the last component.

### S2 — the whole-address fallback makes the street name the city, so one street's branches fuse

- **Where:** `dedup.py:76`. Reachable from `scrapers/osm.py:80-87` whenever only
  `addr:housenumber` and `addr:street` are present — the common case for a tagged shop.
- **Breaks:** two businesses with the same name on the same street share a key. Two
  spellings of the same street (or a street-level vs city-level record for one shop)
  do not. The key is therefore simultaneously too coarse and too fine, depending on
  which tags the mapper filled.
- **Trigger (executed):**
  ```
  _extract_city('12, Rue Verdun')             -> 'rue verdun'
  _extract_city('1234 Beirut')                -> '1234 beirut'
  _extract_city('12 Rue Verdun, Hamra, Beirut, Achrafieh') -> 'achrafieh'   # a district
  _extract_city('12, Rue Verdun, Hamra, Beirut')           -> 'beirut'      # a city
  ```
- **Fix:** return `""` (unknown) instead of the whole address when there is exactly one
  component, so the caller falls back to the next available identity signal rather than
  keying on a street name.

### S3 — the `if parts else ""` guard is dead code, and the failure it was meant to catch is unguarded

- **Where:** `dedup.py:76`.
- **Breaks:** when `address` is truthy, `str.split` always returns at least one
  element, so `else ""` is unreachable. Meanwhile the realistic "empty last component"
  case is not handled at all: an address with a trailing or doubled comma silently
  loses its city instead of falling back to the previous component. Verified:
  `'x'.split(',') == ['x']`, `','.split(',') == ['', '']` — `parts` is never empty.
- **Trigger (executed):** `_extract_city('Hamra Street, Beirut,')` → `''`,
  `_extract_city('Hamra Street, ,')` → `''`, `_extract_city('Beirut,,Lebanon')` →
  `'lebanon'`. A one-character difference in the input flips the key between the real
  city and empty.
- **Fix:** `parts = [p.strip().lower() for p in re.split(r"[,;·\n]", address) if p.strip()]; return parts[-1] if parts else ""`.

### S3 — no type guard at the call site; a non-`str` address is an unhandled `AttributeError`

- **Where:** `dedup.py:213` calls `_extract_city(record.get("address"))` with no
  `isinstance` check, while `BusinessRecord` declares `address: str | None`
  (`scrapers/base.py:8`) — a TypedDict, so not enforced at runtime. Note the
  neighbouring function *is* defensive (`normalize_phone` does `str(phone)` at
  `dedup.py:42`), so the two halves of the key disagree about how much to trust the
  input.
- **Breaks:** confirmed by execution: `_extract_city(123)` →
  `AttributeError: 'int' object has no attribute 'split'`,
  `_extract_city({'a': 1})` → `AttributeError`. This aborts the whole run from inside
  `dedup()` (called at `main.py:180`, outside the per-scraper `try` at
  `main.py:163-168`), so one bad row kills all five CSV exports.
- **Not currently reachable** from the three scrapers — all pass `str | None` — so this
  is latent, not live. It becomes live the moment a JSON/CSV/API field feeds `address`
  without casting, which is the shape of every future scraper.
- **Fix:** `if not isinstance(address, str): return ""` as the first line.

### S3 — compounding with `dedup.py:212`: whitespace-only names make the empty key catastrophic

- **Where:** `dedup.py:212` (`name = record.get("name") or ""`) + `dedup.py:76`
  (`_extract_city(None) == ""`).
- **Breaks:** `osm.py:63` only tests `if not name`, so an OSM `name="   "` tag passes
  and `normalize_name` (`dedup.py:69`) strips it to `""`. Key `("", "")` then matches
  every other nameless, address-less record in **both** markets.
- **Trigger (executed):** three records with `name` of `None`, `"   "`, and `"   "`
  with `address` of `None` and `","` → **1 output row**, two coordinates discarded.
- **Fix:** skip records whose normalized name is empty instead of indexing them
  (`if not normalize_name(name): continue` before the key lookup at `dedup.py:214`).

## Not a bug, but worth knowing

- **`_extract_city` has no second consumer and no test.** `grep` over the repo finds
  exactly one call site (`dedup.py:213`) and zero references in `tests/`, which is why
  a function that returns the country string has survived since the initial commit
  (`963da9c`) through three rewrite passes (`99493b9`, `5222d32`). The only test that
  touches this area asserts `normalize_phone`/`_merge` behaviour
  (`tests/test_lead_signal.py:65`).
- **The one thing it gets right:** it never returns a value containing a comma, so the
  key tuple at `dedup.py:214` is never ambiguous, and it is deterministic — same input,
  same output, no locale or filesystem dependence. It is also cheap: one `split`, one
  `strip`, one `lower` per record, no measurable cost at 100k+ rows.
- **It is the right place to fix, cheaply.** `region` is already computed and stored on
  Google records (`google_places.py:275` → `enricher.infer_region`) and derivable from
  coordinates for OSM/Wikidata (`enricher.py:58-76`), so a correct, cross-source city
  signal already exists in the pipeline. The address parser is the only thing routing
  around it.
- **`country` is a free discriminator that the key ignores.** `dedup.py:214` omits it
  even though `dedup` already reads `record.get("country")` twice at `dedup.py:205` and
  `dedup.py:228`. Adding it to the key fixes the cross-market collision (S1 #3) in one
  token.
- **`address` is used raw and unsanitised in the CSV, so this function's inputs are
  permanently fragile.** `main.py:121-129` writes `address` verbatim into
  `all_businesses.csv` and `main.py:86-104` reads it back with no normalisation, so any
  key derived from it inherits whatever formatting the upstream source chose — today
  and on every future run.

## Recommended order of work

1. **Stop parsing `formattedAddress` for the city.** Add `places.addressComponents`
   to `FIELD_MASK` (`google_places.py:32-43`), store `locality` in a new `city` field
   (`scrapers/base.py`), and key on it (`dedup.py:214`). Kills S1 #1 and S2 postcode.
2. **Add `country` to the fallback key and refuse all-empty keys** at `dedup.py:212-214`.
   Kills S1 #3 and S3 whitespace-name compounding in two lines.
3. **Make the geospatial fields move as one unit in `_pick`** (`dedup.py:159-187`), so a
   merge can never emit a row whose `address` and `lat` disagree. Kills S1 #2 and makes
   any residual over-merge survivable.
4. **Harden the parser** against `،;·\n`, trailing/doubled commas, all-digit trailing
   tokens, and non-`str` input — and return `""` rather than a whole address when
   nothing splits. Kills S2 #1 and #2 and S3 #1 and #2.
5. **Add `tests/test_dedup_city.py`** with the eight inputs above as a table, asserting
   the *semantics* (`city != "lebanon"`, `city` is never all digits, `city != address`),
   not the current `split(",")[-1]` behaviour, so the fix cannot silently regress.