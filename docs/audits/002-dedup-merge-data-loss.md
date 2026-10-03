# 002 — Merge Data Loss and Whole-Record Field Selection

## Verdict

`_merge()` in `dedup.py` resolves field-level conflicts using a global scalar count (`_field_count`) computed across the whole record rather than evaluating the quality, validity, or authority of the field in question. This architecture provably causes silent data loss: a record with more populated keys overall unconditionally overwrites superior field-level data in another record, including canonical addresses, specific business categories, and rooftop coordinates. When a business has a phone in OSM and a rating in Google Places, it either completely bypasses merging and is emitted twice as two fractured records (if Google omitted the phone), or enters `_merge()` where the surviving metadata is dictated by non-deterministic network race conditions between the scrapers. Furthermore, the merge logic conflates missing keys, explicit `None` values, and non-`None` falsy values (`""`, `0`, `False`), allowing empty strings to inflate record counts and overwrite legitimate data, while default zeros in freshly scraped records erase historical enrichment scores.

---

## Findings

### S1 — Whole-record dominance silently discards higher-quality field data

- **Where:** `dedup.py:41-43, 60`
- **Breaks:** Field-level conflict resolution is governed entirely by `merged[key] = av if _field_count(a) >= _field_count(b) else bv`. `_field_count` is a holistic count of all non-`None` values in the entire dictionary (`sum(1 for v in record.values() if v is not None)`). Consequently, conflict resolution is an "all-or-nothing" takeover: whichever record has more total populated attributes wins **every single conflicting field**, irrespective of field quality, semantic precision, or source authority.
  - If Record A has 14 non-`None` fields (because it possesses generic social links or metadata) and Record B has 13 non-`None` fields, Record A wins every overlapping field.
  - If Record B contains a rooftop-precise geocode (`lat`, `lon`), a fully verified street address (`address`), an HTTPS domain (`website`), and a specific category (`"boutique_hotel"`), while Record A contains an administrative centroid, a comma-separated fragment (`"Hamra"`), an unverified HTTP link, and a generic category (`"lodging"`), Record B's high-fidelity data is **silently discarded** across all overlapping keys simply because Record A had an extra social media handle like `facebook`.
- **Trigger:**
  ```python
  record_a = {
      "name": "Phoenicia Hotel", "category": "hotel", "country": "LB",
      "address": "Minet El Hosn", "lat": 33.90, "lon": 35.49,
      "phone": "+9611369100", "website": "http://phoenicia.com",
      "facebook": "phoeniciabeirut", "instagram": "phoeniciabeirut",
      "lead_score": 0, "completeness_score": 0, "source": "osm", "scraped_at": "2026-10-01T00:00:00Z"
  } # _field_count = 14
  
  record_b = {
      "name": "Phoenicia Hotel Beirut", "category": "luxury_hotel", "country": "LB",
      "address": "Fakhredjine Street, Minet El Hosn, Beirut, Lebanon",
      "lat": 33.901234, "lon": 35.495678, "phone": "+9611369100",
      "website": "https://www.phoeniciabeirut.com", "rating": 4.6, "review_count": 1850,
      "lead_score": 0, "completeness_score": 0, "source": "google_places", "scraped_at": "2026-10-01T00:00:00Z"
  } # _field_count = 13
  
  merged = _merge(record_a, record_b)
  ```
  Result: Record A wins every conflicting field (`14 >= 13`). Record B's specific category (`"luxury_hotel"`), verified street address, HTTPS website, and precision coordinates are permanently dropped.
- **Fix:** Replace whole-record field counts with per-field resolution strategies or a defined source hierarchy (e.g. Google Places for `rating`, `review_count`, `address`, and coordinates; OSM for local social tags; latest non-empty string for contact details).

---

### S1 — Sentinel default zeros (`lead_score=0`, `completeness_score=0`) overwrite enriched master records

- **Where:** `scrapers/osm.py:115, 118`; `scrapers/google_places.py:269, 272`; `scrapers/wikidata.py:95, 98`; `dedup.py:42, 50-53, 60`; `main.py:124-125`
- **Breaks:** Scrapers instantiate new records with integer zeros: `lead_score=0` and `completeness_score=0`. In Python, `0 is not None` evaluates to `True`. In `main.py:124-125`, `combined = raw_filtered + master` merges fresh scrapes with the persistent master database loaded from `all_businesses.csv`. In `master`, rows already have computed enrichment scores (e.g., `completeness_score=4`, `lead_score=75`).
  When `_merge(a, b)` merges a fresh scrape with a master record, both `av` and `bv` are non-`None` (`0` and `4`). Because `0 is None` is `False`, the merge drops into the `else:` branch (`_field_count(a) >= _field_count(b)`). If the freshly scraped record has an equal or greater field count than the historical master row, the hardcoded sentinel `0` **silently overwrites** the previously computed score from the master.
- **Trigger:**
  ```python
  fresh_scrape = {
      "name": "Acme Med", "phone": "+9611222333", "category": "clinic", "country": "LB",
      "address": "Hamra St", "lat": 33.89, "lon": 35.48, "email": "info@acme.com",
      "website": "https://acme.com", "facebook": "acme", "instagram": "acme",
      "lead_score": 0, "completeness_score": 0, "source": "osm", "scraped_at": "2026-10-03T12:00:00Z"
  } # _field_count = 14
  
  master_record = {
      "name": "Acme Med", "phone": "+9611222333", "category": "clinic", "country": "LB",
      "address": "Hamra St", "website": "https://acme.com", "website_live": True,
      "lead_score": 85, "completeness_score": 4, "industry_priority": "high",
      "recommended_service": "SEO", "source": "osm", "scraped_at": "2026-09-01T12:00:00Z"
  } # _field_count = 11
  
  merged = _merge(fresh_scrape, master_record)
  ```
  `merged["completeness_score"]` becomes `0` (was `4`), and `merged["lead_score"]` becomes `0` (was `85`). A sales-ready lead is reset to zero.
- **Fix:** Scrapers should emit `lead_score=None` and `completeness_score=None` for uncomputed fields, and `_merge()` must prioritize positive computed integers over `0` or `None`.

---

### S1 — Asymmetric tie-breaking (`>=`) causes arrival-order non-determinism via scraper race conditions

- **Where:** `dedup.py:60`; `main.py:105-113, 124-125`
- **Breaks:** `merged[key] = av if _field_count(a) >= _field_count(b) else bv`. When `_field_count(a) == _field_count(b)`, `a` unconditionally wins every conflicting key.
  In `dedup(records)`:
  ```python
  if key in phone_index:
      phone_index[key] = _merge(phone_index[key], record)
  else:
      phone_index[key] = dict(record)
  ```
  `phone_index[key]` is passed as `a`, and the incoming record is passed as `b`. Therefore, `a` is strictly whichever record appeared earlier in the `combined` list.
  In `main.py:105-113`, the three scrapers run concurrently in a `ThreadPoolExecutor`, and their results are appended to `raw` using `as_completed(futures)`. `as_completed()` yields in arbitrary order based on network timing:
  - If OSM finishes before Google Places: `r_osm` becomes `a`. On field-count ties, OSM's category, address, and coordinates overwrite Google's.
  - If Google Places finishes before OSM: `r_google` becomes `a`. On field-count ties, Google's category, address, and coordinates overwrite OSM's.
  Two identical executions of `main.py` against identical upstream data produce **different merged records** depending entirely on transient HTTP request latency.
  Additionally, in `main.py:124`, `combined = raw_filtered + master`. Fresh raw records precede master records, meaning that in any tie between a fresh scrape and a master record, the unvalidated fresh scrape always overwrites the master.
- **Trigger:** Two matching records from OSM and Google Places each having exactly 14 non-`None` fields. Run 1: OSM finishes in 1.2s, Google Places in 1.8s $\rightarrow$ OSM data wins all conflicts. Run 2: Google Places finishes in 1.1s, OSM in 2.0s $\rightarrow$ Google Places data wins all conflicts.
- **Fix:** Eliminate arrival-order tie-breaking. Break ties using deterministic criteria (such as explicit source priority: `google_places` > `osm` > `wikidata` for venue metadata; or ISO timestamp recency).

---

### S1 — Empty strings (`""`) masquerade as valid data, inflate `_field_count`, and overwrite real values

- **Where:** `dedup.py:42, 50-53, 60`
- **Breaks:** `_merge()` checks only `if av is None:` and `elif bv is None:`. It does not test for empty strings (`""`) or whitespace. In Python:
  - `"" is None` evaluates to `False`.
  - `_field_count` increments for `""` because `"" is not None` is `True`.
  
  This produces two destructive failure modes:
  1. **False Metric Inflation:** A record containing empty string placeholders appears to have a higher `_field_count` than a clean record with fewer fields.
  2. **Silent Overwriting of Valid Data:** If Record A has `email=""` and Record B has `email="sales@agency.com"`, both `av is None` and `bv is None` are `False`. If `_field_count(a) >= _field_count(b)` (which can be caused by Record A having multiple empty strings), Record A enters the `else:` branch and `merged["email"]` is assigned `""`. The legitimate email address is **silently wiped out**.
  3. **Propagation of Empty Strings:** If `bv is None` and `av == ""`, `merged[key]` is set to `""` rather than normalized to `None`.
- **Trigger:**
  ```python
  record_a = {"name": "Biz", "phone": "+9611111111", "email": "", "website": "", "facebook": ""}
  # _field_count = 5 (inflated by empty strings)
  record_b = {"name": "Biz", "phone": "+9611111111", "email": "real@biz.com"}
  # _field_count = 3
  
  merged = _merge(record_a, record_b)
  ```
  `merged["email"]` evaluates to `""`. The valid email `"real@biz.com"` is deleted.
- **Fix:** Treat empty strings and whitespace-only strings as `None` in `_field_count` and at the start of `_merge()`, or normalize `""` to `None` across all scraper outputs before entering deduplication.

---

### S2 — Dictionary schema asymmetry: missing keys vs explicit `None`

- **Where:** `dedup.py:46, 50-53`
- **Breaks:** In `_merge(a, b)`:
  `all_keys = set(a) | set(b)`
  There is a critical structural asymmetry between dictionaries with missing keys and dictionaries with explicit `None` values:
  - If a key is present with value `None` in either dictionary: `key in all_keys` is `True`. `merged` will contain `key: None`.
  - If a key is missing from both dictionaries: `key in all_keys` is `False`. `merged` will **completely omit the key**.
  
  While `load_master()` and full `BusinessRecord` instantiations populate all 23 keys, partial records (produced by unit tests, sub-pipeline transformations, external enrichment scripts, or ad-hoc scrapers) will produce merged dictionaries missing canonical keys.
  If downstream code performs direct key indexing (e.g., `record["website_live"]` or `record["completeness_score"]` rather than `.get()`), Python raises an unhandled `KeyError`. Direct indexing is already used throughout the pipeline (`main.py:69, 132-137`; `enricher.py:182, 195, 272, 289-290`).
- **Trigger:**
  ```python
  a = {"name": "Shop", "phone": "+9611234567"}
  b = {"name": "Shop", "phone": "+9611234567", "category": "retail"}
  m = _merge(a, b)
  # m contains only {'name', 'phone', 'category'}
  # Accessing m["website_live"] raises KeyError
  ```
- **Fix:** Initialize `merged` with the canonical set of 23 schema fields (e.g. from `BusinessRecord.__annotations__` or `FIELDS`) mapped to `None`, ensuring all merged records maintain a uniform dictionary schema.

---

### S3 — Redundant re-computation of `_field_count` inside the field iteration loop

- **Where:** `dedup.py:48, 60`
- **Breaks:** In `_merge(a, b)`:
  ```python
  for key in all_keys:
      ...
      else:
          merged[key] = av if _field_count(a) >= _field_count(b) else bv
  ```
  `_field_count(a)` and `_field_count(b)` do not depend on `key`. Yet for every key where both `av` and `bv` are non-`None` (up to 15–20 times per record pair), both dictionaries are fully scanned to sum their non-`None` values. For a scrape batch with thousands of overlapping records, this performs tens of thousands of redundant dictionary traversals.
- **Trigger:** Merging two records with 18 conflicting fields executes `_field_count()` 36 times instead of 2.
- **Fix:** Compute `count_a = _field_count(a)` and `count_b = _field_count(b)` once before entering the loop.

---

## In-Depth Traces

### Trace 1: Record with phone from OSM and rating from Google Places

Consider a real-world business ("Mayrig Beirut") present in both OpenStreetMap and Google Places:
- In OSM: Contains a phone number, address fragments, category, and social tags. OSM **never** provides customer ratings or review counts (`scrapers/osm.py:111-112` hardcodes `rating=None, review_count=None`).
- In Google Places: Contains an official star rating (`rating=4.5`) and review count (`review_count=420`).

What happens depends entirely on whether Google Places returns a phone number for this listing.

```
                    ┌─────────────────────────────────────────┐
                    │  Mayrig in OSM & Google Places          │
                    │  OSM: has phone, rating=None            │
                    │  Google: rating=4.5, review_count=420   │
                    └────────────────────┬────────────────────┘
                                         │
                         Does Google listing have a phone?
                                         │
                    ┌────────────────────┴────────────────────┐
                    ▼                                         ▼
            [NO PHONE IN GOOGLE]                      [PHONE IN GOOGLE]
                    │                                         │
     OSM keys to phone_index                   Both normalize to same phone
     Google keys to name_index                 Matches in phone_index
                    │                                         │
     Cross-index loop (dedup.py:91-95)         _merge(osm, google) called
     checks record.get("phone") on                            │
     Google record -> None!                                   │
                    │                                         │
     `continue` NEVER FIRES!                   rating: None vs 4.5 -> 4.5 preserved
                    │                          reviews: None vs 420 -> 420 preserved
                    │                                         │
     Output gets TWO records:                  OVERLAPPING FIELDS (category, address,
     1. OSM: phone, NO rating                  lat, lon, website, phone):
     2. Google: rating, NO phone               Resolved by _field_count(a) >= _field_count(b)
                    │                                         │
             DATA LOSS VIA                     Arrival-order race determines winner!
              PARTITION                        Inferior data overwrites superior data
```

#### Subcase A: Google Places record lacks a phone number (`phone is None`)
Many Google Places entities lack phone numbers in the API payload (or fail to parse).
1. **Scraping:**
   - `OSMScraper` yields `r_osm`:
     `phone = "+961 1 572121"`, `rating = None`, `review_count = None`, `name = "Mayrig"`.
   - `GooglePlacesScraper` yields `r_goog`:
     `phone = None`, `rating = 4.5`, `review_count = 420`, `name = "Mayrig"`.
2. **Indexing (`dedup.py:68-83`):**
   - `r_osm` has `phone`:
     `key = normalize_phone("+961 1 572121", "LB")` $\rightarrow$ `"+9611572121"`.
     Stored in `phone_index["+9611572121"] = dict(r_osm)`.
   - `r_goog` has `phone=None`:
     Enters the `else:` branch (`dedup.py:76`).
     `city = _extract_city(r_goog.get("address"))` $\rightarrow$ `"lebanon"`.
     `key = (normalize_name("Mayrig"), "lebanon")`.
     Stored in `name_index[("mayrig", "lebanon")] = dict(r_goog)`.
3. **Cross-Index Deduplication (`dedup.py:87-96`):**
   ```python
   phone_records = list(phone_index.values())
   captured_phones = set(phone_index.keys())

   name_records = []
   for record in name_index.values():
       raw = record.get("phone")
       if raw and normalize_phone(raw, record.get("country", "LB")) in captured_phones:
           continue
       name_records.append(record)
   ```
   For `r_goog` in `name_index.values()`, `raw = r_goog.get("phone")` is `None`.
   The condition `if raw and ...` evaluates to `False`.
   The `continue` statement **never fires**.
   `r_goog` is appended to `name_records`.
4. **Outcome:**
   `dedup()` returns `phone_records + name_records` containing **both** records unmerged:
   - Record 1 (OSM): Contains the phone number `+9611572121`, but `rating=None` and `review_count=None`.
   - Record 2 (Google): Contains `rating=4.5` and `review_count=420`, but `phone=None`.
   
   **Impact:** `_merge()` is never executed. The business is split into two incomplete duplicate rows. The OSM lead cannot be evaluated on rating, and the Google lead cannot be contacted via phone or WhatsApp.

#### Subcase B: Google Places record contains the phone number
1. **Indexing:**
   Both `r_osm` and `r_goog` have a phone that normalizes to `"+9611572121"`.
   Whichever record arrives second triggers `_merge(phone_index[key], incoming)`.
2. **Field-by-Field Execution in `_merge()`:**
   - `key = "rating"`:
     `av = r_osm.get("rating")` (`None`), `bv = r_goog.get("rating")` (`4.5`).
     `if av is None:` is `True`.
     `merged["rating"] = 4.5`.
     **The Google rating is preserved.**
   - `key = "review_count"`:
     `av = None`, `bv = 420`.
     `merged["review_count"] = 420`.
     **The Google review count is preserved.**
   - `key = "region"`:
     `av = None` (OSM has `region=None`), `bv = "Beirut"` (Google infers region via coordinates).
     `merged["region"] = "Beirut"`.
     **The Google region is preserved.**
   - `key = "facebook"` / `key = "instagram"`:
     `av` is populated (OSM tags), `bv = None` (Google Places does not extract social tags).
     `elif bv is None:` is `True`.
     **OSM social tags are preserved.**
   - `key = "source"`:
     `merged["source"] = "google_places|osm"`.
   - `key = "scraped_at"`:
     `merged["scraped_at"] = max(...)`.
3. **The Conflict Failure on Overlapping Fields:**
   For `name`, `category`, `address`, `lat`, `lon`, `website`, and `phone`:
   Both `av is not None` and `bv is not None`.
   The code executes:
   `merged[key] = av if _field_count(a) >= _field_count(b) else bv`
   
   Let us compute the exact `_field_count`:
   - OSM record (`r_osm`):
     Populated: `name`, `category`, `address`, `country`, `lat`, `lon`, `phone`, `email`, `website`, `facebook`, `instagram`, `lead_score` (0), `source`, `scraped_at`, `completeness_score` (0).
     Total non-`None` = **15 fields**.
   - Google Places record (`r_goog`):
     Populated: `name`, `category`, `region`, `country`, `address`, `lat`, `lon`, `phone`, `website`, `rating`, `review_count`, `lead_score` (0), `source`, `scraped_at`, `completeness_score` (0).
     Total non-`None` = **15 fields**.
   
   The counts are exactly tied at 15.
   Because `15 >= 15` evaluates to `True`, **`a` wins every single conflicting field**.
   
   - If OSM finished scraping first (`a = r_osm`):
     OSM wins all ties. Google's formatted address (`"Pasteur Street, Achrafieh, Beirut, Lebanon"`) is **dropped** in favor of OSM's fragment (`"Pasteur"`). Google's specific category (`"armenian_restaurant"`) is **dropped** in favor of OSM's generic tag (`"restaurant"`).
   - If Google finished scraping first (`a = r_goog`):
     Google wins all ties. Google's unformatted national phone number (`"01 572 121"`) overwrites OSM's international format (`"+961 1 572121"`).

---

### Trace 2: The None-versus-missing-key asymmetry

The table below contrasts how Python and `_merge()` evaluate different states of a key `k` in record `a` against a valid value `bv = "Valid Data"` in record `b`:

| State of Key in Record A | `k in a` | `a.get(k)` | `av is None` | Count in `_field_count(a)` | Outcome in `_merge(a, b)` for key `k` | Integrity Impact |
|---|---|---|---|---|---|---|
| **Missing Key** (`{}`) | `False` | `None` | `True` | 0 | `merged[k] = bv` (`"Valid Data"`) | Correct value preserved |
| **Explicit `None`** (`{k: None}`) | `True` | `None` | `True` | 0 | `merged[k] = bv` (`"Valid Data"`) | Correct value preserved |
| **Empty String** (`{k: ""}`) | `True` | `""` | **`False`** | **+1** | **`av` if `_field_count(a) >= _field_count(b)` else `bv`** | **Real data silently destroyed if `a` dominates** |
| **Integer Zero** (`{k: 0}`) | `True` | `0` | **`False`** | **+1** | **`av` if `_field_count(a) >= _field_count(b)` else `bv`** | **Enriched scores reset to 0 if `a` dominates** |
| **Boolean `False`** (`{k: False}`) | `True` | `False` | **`False`** | **+1** | **`av` if `_field_count(a) >= _field_count(b)` else `bv`** | **Status overwritten based on whole record** |

#### Schema Shape Divergence
When keys are missing rather than set to `None`:
1. Let $A = \{ \text{name}: \text{"Alpha"}, \text{phone}: \text{"123"} \}$ (missing 21 keys).
2. Let $B = \{ \text{name}: \text{"Alpha"}, \text{phone}: \text{"123"}, \text{rating}: 4.0 \}$ (missing 20 keys).
3. `all_keys = set(A) | set(B) = {"name", "phone", "rating"}`.
4. `merged` will contain **only 3 keys**.
5. When `enrich(records)` runs:
   `enricher.py:182` attempts `targets = [(i, r["website"]) for i, r in enumerate(records) if r.get("website")]`.
   If `r.get("website")` is falsy, it does not throw; but at `main.py:69`:
   `row["website_live"] = ...` assigns into `row`.
   However, any code accessing keys via direct indexing (e.g. `r["email"]` or `r["completeness_score"]`) crashes with `KeyError`.

#### Mathematical Non-Commutativity and Non-Associativity
A robust deduplication merge must form a join-semilattice: the merge operation $\oplus$ must be **commutative** ($A \oplus B = B \oplus A$) and **associative** ($(A \oplus B) \oplus C = A \oplus (B \oplus C)$).

`_merge` violates both properties:
1. **Non-Commutative:**
   Let $A = \{ \text{"name"}: \text{"Cafe A"}, \text{"phone"}: \text{"123"} \}$ and $B = \{ \text{"name"}: \text{"Cafe B"}, \text{"phone"}: \text{"123"} \}$.
   `_field_count(A) = 2`, `_field_count(B) = 2`.
   - `_merge(A, B)`: `_field_count(A) >= _field_count(B)` is `True` $\rightarrow$ `name` is `"Cafe A"`.
   - `_merge(B, A)`: `_field_count(B) >= _field_count(A)` is `True` $\rightarrow$ `name` is `"Cafe B"`.
   $$A \oplus B \neq B \oplus A$$

2. **Non-Associative:**
   Let $A$ have 10 fields (name = "A"), $B$ have 10 fields (name = "B"), and $C$ have 12 fields (name = "C").
   - $(A \oplus B) \oplus C$:
     $M_1 = A \oplus B \rightarrow$ name is "A" (`_field_count(M_1) = 10$).
     $M_1 \oplus C$: `_field_count(C) = 12 > 10` $\rightarrow$ name is "C".
   - $A \oplus (B \oplus C)$:
     $M_2 = B \oplus C \rightarrow$ name is "C" (`_field_count(C) = 12 > 10`).
     $A \oplus M_2$: `_field_count(M_2) \ge 12 > 10` $\rightarrow$ name is "C".
   Now let $C$ have 5 disjoint fields that add to $A$ without conflicting on name:
   Accumulating records in different orders shifts `_field_count` thresholds dynamically, causing intermediate merges to select different winners for identical records.

---

## Not a bug, but worth knowing

- `_merge` handles `source` by splitting on `"|"` and deduplicating (`"google_places|osm"`), which correctly tracks provenance across multi-source merges (`dedup.py:54-56`).
- `scraped_at` uses `max(av, bv)` (`dedup.py:57-58`), which correctly preserves the most recent ISO-8601 timestamp assuming identical timestamp formatting and UTC alignment.
- The `phone_index` normalizes phones with `normalize_phone()` before keying, but stores the raw unnormalized `record` dictionary into `phone_index[key] = dict(record)` (`dedup.py:75`). The stored phone is only normalized much later at `main.py:134-136`.

---

## Recommended order of work

1. **S1 (Empty String & Sentinel Zero Sanitization):**
   - In `dedup.py`, update `_field_count` to ignore empty strings and whitespace: `v is not None and str(v).strip() != ""` and `v != 0`.
   - In `scrapers/osm.py`, `google_places.py`, and `wikidata.py`, initialize `lead_score=None` and `completeness_score=None` rather than `0`.
2. **S1 (Field-Level Resolution Rules):**
   - Replace whole-record `_field_count` competition in `_merge()` with an explicit per-field strategy:
     - `rating`, `review_count`: Authoritative source is `google_places`.
     - `address`, `lat`, `lon`: Prefer `google_places` (geocoded/formatted) > `osm` > `wikidata`.
     - `category`: Prefer specific whitelist matches over generic categories (`establishment`, `point_of_interest`).
     - `email`, `website`, `phone`, social links: Prefer non-empty values; if both exist, prefer newer `scraped_at` or verified live domains.
3. **S1 (Deterministic Tie-Breaking):**
   - Enforce strict, deterministic source priority (`google_places` > `osm` > `wikidata`) when field confidence is otherwise equal, removing dependency on thread completion order.
4. **S2 (Canonical Schema Normalization):**
   - In `_merge()`, initialize `merged` using all 23 keys from `BusinessRecord` initialized to `None`, guaranteeing consistent dictionary shapes across all code paths.
5. **S3 (Precompute Field Counts):**
   - Precompute `_field_count` once outside the `all_keys` loop in `_merge()`.
