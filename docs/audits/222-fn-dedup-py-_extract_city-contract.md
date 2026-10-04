# 200 — `_extract_city` (`dedup.py:72`) type contract vs. practice

## Verdict

The signature `_extract_city(address: str | None) -> str` (`dedup.py:72`) is
**honest**: I could not produce an input where the return type is anything other
than `str`, and no current caller can hand it a non-`str` in a way that is
*silent* — a truthy non-`str` crashes loudly, and nothing in the pipeline ever
produces one. The contract is nonetheless **unenforceable**, because `dedup.py`
imports only `re` and `unicodedata` (`dedup.py:1-2`) and `dedup()` is typed
`list[dict]` (`dedup.py:197`), so `BusinessRecord` (`scrapers/base.py:5`) is
documentation, not a checked boundary.

The real damage is not type-level at all. It is that the return value is used
**only as a `dict` key** (`dedup.py:214-218`) and is never stored on a record or
written to any CSV, so every misfire of this function is invisible except as a
row count at `main.py:181`. That is why the semantics can be wrong by 100% —
verified: for Google records the "city" is the **country**, and for OSM records
it is the **`addr:district`**, because `osm.py:80-86` orders the components
`housenumber, street, suburb, city, district`.

## Contract answers (the four questions, answered directly)

### 1. Actual input type vs. declared `str | None`

Declared: `str | None`. Actual, at the only call site `dedup.py:213`
(`record.get("address")`): **`str | None`, in practice — the declaration holds.**

Every producer of `address` supplies `str | None`:
- `osm.py:87` — `", ".join(p for p in addr_parts if p) or None`
- `google_places.py:286` — `place.get("formattedAddress")`
- `wikidata.py:99,109` — `row.get("address", {}).get("value")`
- `main.py:104` — `load_master` returns `list[dict]` read by `csv.DictReader`;
  a cell is always `str` or (after `main.py:88-89`) `None`.

`main.py:43-44` casts only `lat/lon/rating` and
`review_count/completeness_score/lead_score`. **`address` is never cast**, so no
numeric coercion can sneak in from the master CSV. There is no reachable
non-`str` today.

### 2. Actual output type vs. declared `str`

Declared `str`. Actual: **always `str`**. `str.split(",")` on a `str` yields
`list[str]`; `p.strip().lower()` yields `str`; `parts[-1]` is `str`. Verified
across `[None, '', 'a', ',', ' , ', '  a b, c ,  d']` — observed return types
`{'str'}`. The `parts[-1] if parts else ""` fallback at `dedup.py:76` is
unreachable, because `split` always returns at least one element.

### 3. Can a caller violate the contract silently?

**No, not on the type axis — and that is the only axis the annotation covers.**
Every violation raises:

| `address` value | Result at `dedup.py:75` |
|---|---|
| `123` | `AttributeError: 'int' object has no attribute 'split'` |
| `3.5` | `AttributeError` |
| `['a','b']` | `AttributeError` |
| `{'city':'X'}` | `AttributeError` |
| `b'Beirut, Lebanon'` | `TypeError: a bytes-like object is required, not 'str'` |

So this is a *latent crash*, not a latent corruption — and it is **not reachable
from any current caller** (see §1). I am deliberately not calling it S1.

The silent violations are on the **semantic** axis, and those are live:

- `address` **absent** as a key vs. `address=None` vs. `address=""` vs.
  `address=","` vs. `address="   ,   "` all collapse to the same city `""`.
  Verified: `_extract_city(None) == _extract_city("") == _extract_city(",") == ""`.
  A record whose address is pure punctuation is indistinguishable from a record
  that has no address at all, and lands in the same national mega-bucket.
- `BusinessRecord` is `total=True` (`base.py:5` — no `total=False`), so
  `address` is a *required* key. Nothing enforces it: `dedup()` accepts
  `list[dict]` (`dedup.py:197`) and `main.py:179-180` concatenates typed scraper
  output with the untyped `load_master()` result (`main.py:104`,
  `list[dict]`). A record missing the key entirely is silently accepted and
  silently keys as `""`.

### 4. Fields read/written not in the `TypedDict`

**None — and that is itself the finding.** `_extract_city` reads exactly one
field, `address`, which **is** in the `BusinessRecord` (`base.py:8`), and it
writes nothing. Every field it touches is declared.

The inverse is the actionable list. `_extract_city` ignores four fields that are
**also** in the `BusinessRecord` and that actually determine locality:

| Field | Declared | Why it matters |
|---|---|---|
| `region` | `base.py:9` | **Already populated before dedup runs** for Google records — `google_places.py:275` calls `infer_region(None, lat, lon, country=...)` at scrape time and `google_places.py:284` stores it on the record. Verified against the KSA boxes at `enricher.py:71`: `lat=24.71, lon=46.68` → `region="Riyadh"`. That is the correct city, sitting in the record, discarded in favour of `"saudi arabia"`. |
| `country` | `base.py:10` | Distinguishes `("starbucks","lebanon")` from `("starbucks","saudi arabia")`. Not consulted, so a missing address erases the only remaining country signal. |
| `lat` | `base.py:11` | Trivial geospatial binning; `_LB_COORD_REGIONS`/`_KSA_COORD_REGIONS` (`enricher.py:58-76`) already exist. |
| `lon` | `base.py:12` | Same. |

### Places the return value is used in a way that breaks on an unexpected shape

**Exactly one consumer, and it cannot break — because the shape is never
surfaced.** `_extract_city` has a single call site (`dedup.py:213`, confirmed by
grep across the repo). Its result flows into:

```
dedup.py:214  key = (normalize_name(name), city)
dedup.py:215  if key in name_index:            # needs hashable — str always is
dedup.py:218  name_index[key] = dict(record)
```

`name_index: dict[tuple, dict]` (`dedup.py:199`) — the element type is the
**unparameterized `tuple`**, so even a checker would accept a heterogeneous key.
The key is never attached to a record, never read back (`dedup.py:226-230`
iterates `.values()`, not keys), and there is no merge-key column in `FIELDS`
(`main.py:33-40`).

Consequences:
- A *type* regression would raise `TypeError: unhashable type` at `dedup.py:215`
  — a crash, not corruption. Not the problem.
- A *semantic* regression is **completely invisible**. The only observable
  effects are `len(records)` printed at `main.py:181` and the row sets of the
  five CSVs. There is no per-merge provenance to compare against a baseline.
  Audit 072 §D4 already flags the missing `merged_by_name` counter
  (`072-run-failure-triage.md:300-311`); this function is the reason that metric
  matters most.
- `_merge` (`dedup.py:190-194`) builds the output key set as `set(a) | set(b)`,
  and `csv.DictWriter(..., extrasaction="ignore")` (`main.py:125`) silently drops
  anything outside `FIELDS` on write. So a derived city key that is never
  persisted can never be recovered or audited on a later run either.

## Findings

### S1 — A missing address makes `country`/`lat`/`lon` a no-op, so one Saudi record absorbs a Lebanese one and the survivor asserts the wrong country

- **Where:** `dedup.py:72-76` (returns `""` for any falsy address) feeding the
  key at `dedup.py:213-214`; the clobbering happens in `_pick`/`_merge` at
  `dedup.py:167-171, 190-194`. Propagated downstream by `main.py:194`
  (`normalize_phone(raw_phone, r.get("country"))`) and `enricher.py:334`
  (`country=r.get("country", "LB")`).
- **Agreement:** audit 003 S1 flags the `parts[-1]`-is-the-country collapse;
  audit 096 line 37 flags the `(name, "")` collapse for Saudi. **What is new
  here is the consequence, not the cause:** because `""` is a *shared* city
  across all countries, and because `_merge` treats `country`/`lat`/`lon` as
  ordinary competing fields, the merged row does not merely lose a branch — it
  inherits whichever values won the trust/recency/lexicographic tiebreak
  (`dedup.py:179-187`) and now *asserts* them. The `country` field, which is
  `str` on the `BusinessRecord` and used for phone normalization and region
  inference, ends up wrong.
- **Breaks:** A Lebanese business from OSM and a Saudi business from Google
  with the same name, both lacking an address, produce one CSV row whose
  `country` is the one that won. Phone normalization then runs under the wrong
  calling code and region inference runs under the wrong country table.
- **Trigger** (executed):
  ```python
  a = rec("Al Salam", None, country="SA", lat=24.71, lon=46.68, source="google_places")
  b = rec("Al Salam", None, country="LB", lat=33.89, lon=35.50, source="osm")
  dedup([a, b])
  # in=2  out=1
  # -> Al Salam | country= SA | lat= 24.71 lon= 46.68 | source= google_places|osm
  ```
  The Beirut business (km 33.89/35.50) is gone and the row claims `country=SA`.
  Worse with more sources — 4 records (`SA`/`google_places`, `LB`/`osm`,
  `SA`/`google_places`, `LB`/`wikidata`), all `address=None`, all named
  `"Al Salam"` → **out=1**. Wikidata `address` is `wdt:P6375` and is usually
  unbound (`wikidata.py:99`), so the Wikidata stream is the most likely to land
  in this bucket.
- **Fix:** when `address` yields no locality, fall back to
  `region`/`country`/`(lat, lon)` bin before constructing the key; and never let
  `_merge` pick `country` by ordinary field rank — derive it from the surviving
  coords (`main.py:184` already has `resolve_country` for exactly this).

### S2 — The two halves of the identity key are normalised by different rules, so the key can never match across transliteration

- **Where:** `dedup.py:75` (`p.strip().lower()`) vs. `dedup.py:66-69`
  (`normalize_name` does `NFKD` → strip combining marks → collapse whitespace →
  lower).
- **Breaks:** `normalize_name` strips diacritics; `_extract_city` does not. The
  key at `dedup.py:214` is therefore `(accent-stripped name, accent-*bearing*
  city)`. A Google record and an OSM record for the same Beirut shop whose
  addresses are spelled in Latin vs. French transliteration produce
  `'liban'` vs `'leban'` and `'beyrouth'` vs `'beirut'` — a guaranteed miss.
  The same asymmetry applies to Arabic: no NFKD means hamza (`أ`/`ا`), madda
  (`آ`) and tashkeel survive in the city half, so `الزهراء` != `الزهرا`.
- **Trigger** (executed):
  ```
  _extract_city("Rue Hamra, Beirut, Lebanon")     -> 'lebanon'
  _extract_city("Rue Hamra, Beyrouth, Liban")     -> 'liban'
  normalize_name("Café Français")                 -> 'cafe francais'
  _extract_city("Rue Hamra, Béirouth, Liban")     -> 'liban'
  ```
- **Fix:** reuse one normaliser for both halves — have `_extract_city` return
  `normalize_name(parts[-1])`, or extract the raw segment and run
  `normalize_name` at `dedup.py:214` over the whole tuple.

### S2 — `region` is populated before `dedup()` runs and is never read, so the function discards the one correct answer it is handed

- **Where:** `google_places.py:275` and `google_places.py:284` set `region` at
  scrape time via `infer_region`; `dedup.py:213` reads only `address`.
- **Breaks:** For every Google record with coordinates inside a known box, the
  correct locality is already in `record["region"]` when `_extract_city` is
  called. Verified against `enricher.py:71`: `lat=24.71, lon=46.68` falls inside
  the Riyadh box → `region == "Riyadh"`. `_extract_city` nevertheless returns
  `"saudi arabia"` for that record, because `google_places.py:286` supplies a
  full `formattedAddress` whose last comma segment is the country. The two
  fields disagree, and the wrong one wins because it is the only one consulted.
- **Trigger:** KSA chain branches with no phone, all named `"Starbucks"`, all
  with in-box coordinates (so `region` ∈ {Riyadh, Jeddah, Dammam}), addresses
  `"<street>, <district>, <city>, Saudi Arabia"` (executed):
  ```
  in=5  out=1
  kept address: 'Tahlia St, Riyadh, Saudi Arabia'
  ```
  Five real leads in three cities → one row. The Jeddah and Dammam branches, and
  any phone/website they carried, are silently discarded with no trace.
- **Fix:** order the key derivation `region` → `(lat, lon)` box → address
  segment, and stop treating the last comma segment as authoritative.

### S3 — The `BusinessRecord` contract is documented on a boundary the checker never sees

- **Where:** `dedup.py:1-2` (imports only `re`, `unicodedata`);
  `dedup.py:197` (`records: list[dict]`); `base.py:5` (`total=True`);
  `main.py:104` (`load_master -> list[dict]`); `main.py:179-180`
  (`combined = raw_filtered + master; records = dedup(combined)`).
- **Breaks:** `_extract_city`'s `str | None` annotation cannot be checked
  against `base.py:8`'s `address: str | None`, because `scrapers.base` is never
  imported into `dedup`. The `total=True` guarantee that `address` is present is
  likewise unenforced: `dedup` accepts any `dict`, and `main.py:179` mixes
  typed scraper output with untyped CSV dicts. `name_index: dict[tuple, dict]`
  (`dedup.py:199`) then erases the key type, so even the one remaining
  annotation provides no protection.
- **Trigger:** a future record source (or a hand-edited master CSV) that sets
  `address` to a number, or omits the key. Today this crashes loudly; there is no
  test (the project has zero) pinning the behaviour, so it would only be
  discovered in a multi-hour production run.
- **Fix:** type `dedup(records: Sequence[BusinessRecord]) -> list[BusinessRecord]`
  and annotate `name_index: dict[tuple[str, str], BusinessRecord]`; move
  `BusinessRecord` into a dependency-free module so `dedup.py` can import it
  without the `scrapers` package (see audit 020 §type-contract-import-cycle).

### S3 — `parts[-1] if parts else ""` is dead code

- **Where:** `dedup.py:76`.
- **Breaks:** nothing. `address.split(",")` always returns ≥ 1 element, and the
  falsy-`address` case is already handled by the guard at `dedup.py:73`. The
  `else ""` arm is unreachable and implies a defensive intent the function does
  not have (the real non-`str` crash path is unguarded).
- **Trigger:** `_extract_city(",")` → `''` via the `parts[-1]` arm, not the
  `else` arm.
- **Fix:** delete the `else` branch; it reads as protection that does not exist.

## Not a bug, but worth knowing

- **The declared return type is the one part of this function that is entirely
  accurate.** No input reachable from any current caller produces a non-`str`
  return, and no caller can corrupt the pipeline through a type mismatch. The
  audit-worthy problems here are semantic, and they are invisible because of
  *where* the value goes (a dict key), not because of *what* it is.
- **The `(name, "")` mega-bucket and the "city is actually the country" collapse
  are already documented** — audit 003 S1 (`003-dedup-identity-model.md:19-40`),
  049 §5 (`049-data-dictionary.md:120-131`), 096 line 37, 028 line 92. I am not
  re-reporting them as new. What S1 above adds is the *downstream* consequence
  (wrong `country` asserted on the survivor), and what S2 adds is that OSM
  yields `addr:district` as the "city" (`osm.py:80-86` orders components
  `housenumber, street, suburb, city, district`, so `parts[-1]` is the district):
  verified `'Rue Hamra, Ras Beirut, Beirut, Geitawi'` → `'geitawi'` vs.
  `'Rue Verdun, Verdun, Beirut'` → `'beirut'`. The same shop therefore keys
  differently depending on which optional OSM tag the mapper filled in — verified:
  two `Cafe Argan` records differing only by the presence of `addr:district`,
  both phone-less, emit **two rows**.
- **Postcode formatting fragments even the country bucket.**
  `_extract_city("Abdulaziz Rd, Al Olaya, Riyadh, Saudi Arabia 12345")` →
  `'saudi arabia 12345'`, not `'saudi arabia'`. Google includes a postcode for
  some regions and not others, so this single string field produces two
  different keys for the same country.
- **No address at all is the common case, not the edge case.** `main.py:88-89`
  rewrites every blank master-CSV cell to `None`, so on the second run and
  beyond the master is full of `address=None` rows feeding `""`. Wikidata
  (`wikidata.py:99`) and OSM-without-addr-tags (`osm.py:87`) both feed `None`.
- **There is no merge provenance.** Audit 072 (lines 300-311) already asks for
  `dedup()` to return `(records, merged_by_phone, merged_by_name)`. Until it
  does, neither the S1 nor the S2 failure mode above can be distinguished from a
  quiet week — `main.py:181` prints one integer and nothing baselines it.

## Recommended order of work

1. **Add the counters first** (audit 072): return
   `(records, merged_by_phone, merged_by_name)` from `dedup()` and log them at
   `main.py:181`. Without this you cannot prove the S1/S2 fixes worked — the
   failure mode is *fewer rows*, which is indistinguishable from a good week.
2. **Fix the key derivation** (S1 + S2): replace `_extract_city`'s
   last-comma-segment heuristic with a precedence chain — `region` →
   `(lat, lon)` box (`enricher.py:58-76`) → parsed address segment — and run the
   same `normalize_name` over the locality string as over the name.
3. **Make `country` non-mergeable** (S1): derive `country` from the surviving
   `(lat, lon)` in `main.py:184` (already running `resolve_country`) *after*
   `dedup`, and stop letting `_pick` choose it by field rank.
4. **Close the type boundary** (S3): `list[BusinessRecord]` in and out of
   `dedup()`, `dict[tuple[str, str], BusinessRecord]` for `name_index`, and move
   `BusinessRecord` to a module `dedup.py` can import without pulling in
   `scrapers` (audit 020).
5. **Add the regression tests** that would have caught all of the above — audit
   031 already specifies `test_extract_city_comma_separated` and
   `test_normalize_name_non_string_type_error`; add
   `test_extract_city_keys_are_country_independent` (assert the key differs for
   Riyadh vs Jeddah) and `test_dedup_does_not_merge_across_countries`.