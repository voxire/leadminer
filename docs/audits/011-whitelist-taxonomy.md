# 011 — Whitelist Taxonomy and Cross-Source Vocabulary Mismatch

## Verdict

`whitelist.py` treats Google Places API type tokens, raw OpenStreetMap tag values, and Wikidata English labels as if they shared a single, uniform category vocabulary. They do not: the direct intersection between standard Google Place types and queried OSM tag values is only 32 terms out of hundreds. Over 51% of standard Google Place types (including primary restaurant categories like `lebanese_restaurant`, `seafood_restaurant`, and `fast_food_restaurant`, as well as `lodging`, `doctor`, and `car_dealer`) evaluate to `False` and are permanently discarded on sight in `main.py:117`. Furthermore, valid business types that survive solely via the unanchored substring soft-allow (`clothing_store`, `real_estate_agency`, `beauty_salon`) are assigned `industry_priority = "low"`, which completely excludes them from `sales_ready.csv` and starves them of lead-score points. Meanwhile, the unanchored substring matcher leaks civic, government, religious, and lexical false positives (`government_office`, `intelligence_agency`, `refugee_centre`, `bishop`).

---

## Findings

### S1 — Google Places primary restaurant, lodging, healthcare, and auto types silently dropped
- **Where:** `scrapers/whitelist.py:105-130`, `scrapers/google_places.py:246-248, 283-296`, `main.py:117-121`
- **Breaks:** `scrapers/google_places.py:_pick_category()` selects the most specific non-generic type returned by the Google Places API (New). Google orders types from most specific to least specific (e.g. `["lebanese_restaurant", "middle_eastern_restaurant", "restaurant", "food", "point_of_interest"]`). Because `_pick_category()` selects `lebanese_restaurant`, and `scrapers/whitelist.py` contains only generic `"restaurant"`, `is_business_category("lebanese_restaurant")` evaluates to `False`. In `main.py:117`, all matching records are permanently purged before deduplication, enrichment, scoring, or CSV output. This destroys the primary yield of target queries specifically configured in `google_places.py` (e.g. `"restaurants in Lebanon"`, `"boutique hotels in Lebanon"`, `"car dealerships in Lebanon"`).
- **Trigger:** Any record with Google Places types:
  - F&B: `"lebanese_restaurant"`, `"fast_food_restaurant"`, `"seafood_restaurant"`, `"mediterranean_restaurant"`, `"middle_eastern_restaurant"`, `"italian_restaurant"`, `"pizza_restaurant"`, `"steak_house"`, `"sushi_restaurant"`, `"french_restaurant"`
  - Lodging: `"lodging"`, `"resort_hotel"`, `"extended_stay_hotel"`, `"inn"`
  - Healthcare: `"doctor"` (whitelist has `"doctors"`, missing singular), `"veterinary_care"`
  - Automotive: `"car_dealer"` (whitelist has `"car_dealership"`), `"gas_station"`
  - Personal Care: `"hair_care"` (whitelist has `"hairdresser"`), `"beautician"`
- **Fix:** Normalize Google Place types before filtering by mapping them through an explicit Google-to-canonical taxonomy dictionary or stripping suffixes (e.g., `*_restaurant` -> `restaurant`, `*_hotel` -> `hotel`).

### S1 — Substring soft-allow demotes valid Google leads to "low" priority, banishing them from `sales_ready.csv`
- **Where:** `scrapers/whitelist.py:123-128, 133-145`, `main.py:132, 144-145`, `enricher.py:245-250`
- **Breaks:** Google Places types that contain keywords like `store`, `agency`, `salon`, or `center` (e.g. `clothing_store`, `shoe_store`, `jewelry_store`, `electronics_store`, `beauty_salon`, `hair_salon`, `real_estate_agency`, `fitness_center`) pass `is_business_category()` via the soft-allow fallback (`any(kw in cat for kw in business_keywords)`). However, `industry_priority()` in `scrapers/whitelist.py:133-145` only checks exact membership in `PRIORITY_INDUSTRIES` (high) and `ADJACENT_BUSINESSES` (medium); it has no soft-allow logic and defaults unmatched strings to `"low"`. In `main.py:144-145`, `sales_ready.csv` strictly requires `r.get("industry_priority") in ("high", "medium")`. Consequently, high-priority target leads scraped from Google Places (e.g. Riyadh fashion brands, Beirut jewelry stores, real estate agencies) are permanently excluded from `sales_ready.csv`, even when they possess complete contact information (phone, email, Instagram). Additionally, `enricher.py:245-250` awards them 0 bonus points instead of +15 on `lead_score`.
- **Trigger:** A record with `category="clothing_store"` or `category="real_estate_agency"` and valid contact channels. `is_business_category()` returns `True`, but `industry_priority()` returns `"low"`, causing `main.py:144` to drop the lead from `sales_ready.csv`.
- **Fix:** Align `industry_priority()` with the category mapping so that Google types like `clothing_store` and `real_estate_agency` map to `"high"` priority rather than falling through to `"low"`.

### S1 — Raw OSM tag extraction drops key context and discards major commercial categories
- **Where:** `scrapers/osm.py:68-75`, `scrapers/whitelist.py:17-82`
- **Breaks:** In `scrapers/osm.py:68-75`, category extraction checks six tag keys in order (`shop`, `amenity`, `office`, `tourism`, `craft`, `healthcare`) and takes the raw *value* of the first present tag, discarding the tag key completely. Overpass explicitly queries `node["name"]["craft"]` and `node["name"]["office"]`, but `whitelist.py` rejects standard OSM values:
  - `office=*`: `it` (tech/SaaS companies), `coworking` (coworking spaces), `telecommunication` (whitelist has plural `telecommunications`), `travel_agent` (whitelist has `travel_agency`), `property_management`, `financial` are all dropped (`is_business_category == False`).
  - `shop=*`: `greengrocer` (ubiquitous local produce retail), `pastry` (whitelist has `patisserie`), `car_parts` (whitelist has `parts`), `bicycle`, `wholesale`, `beverages`, `fabric`, `antiques`, `cheese`, `second_hand` are all dropped.
  - `tourism=*`: `chalet` (standard OSM tag for Lebanese mountain/ski rentals in Faraya, Cedars, Faqra) and `apartment` (serviced vacation rentals) are dropped.
  - `craft=*`: 95% of craft values (`electrician`, `plumber`, `carpenter`, `shoemaker`, `photographer`, `locksmith`, `painter`, `jeweller`, `winery`, `brewery`) are dropped, making the Overpass `craft` query almost entirely wasted network overhead.
  - `healthcare=*`: `doctor` (OSM `healthcare=doctor`) and `laboratory` (commercial medical diagnostic labs) are dropped.
  - `amenity=*`: `bureau_de_change` (money changers; whitelist has `currency_exchange`), `nightclub` (whitelist has nothing), `fuel` (gas stations; whitelist has `fuel_dispenser` under noise) are dropped.
- **Trigger:** An OSM node with `office=it`, `office=coworking`, `tourism=chalet`, `shop=greengrocer`, or `craft=electrician`. All return `is_business_category(...) == False`.
- **Fix:** Retain OSM tag keys during extraction (`f"{key}:{value}"` or structured tuple) and map qualified OSM key-value pairs into canonical business categories.

### S2 — Unanchored substring matching permits civic, government, religious, and lexical false positives
- **Where:** `scrapers/whitelist.py:123-128`
- **Breaks:** The soft-allow check `any(kw in cat for kw in business_keywords)` tests raw substring containment (`kw in cat`) without token boundaries or regex anchors. This creates two distinct failure modes:
  1. *Lexical substring collisions:* `"bishop"`, `"bishopric"`, and `"archbishopric"` pass because they contain `"shop"`. `"affirmation"` and `"confirmation"` pass because they contain `"firm"`. `"accompany"` passes because it contains `"company"`.
  2. *Civic, administrative, and public infrastructure leakage:* Public institutions containing `"office"`, `"agency"`, `"service"`, or `"centre"` pass unfiltered because `GEOGRAPHIC_NOISE` lacks institutional blocks: `"government_office"`, `"police_office"`, `"customs_office"`, `"tax_office"`, `"passport_office"`, `"employment_office"`, `"government_agency"`, `"intelligence_agency"`, `"space_agency"`, `"military_service"`, `"civil_service"`, `"detention_centre"`, `"refugee_centre"`, `"migrant_centre"`, `"community_centre"`, `"crisis_center"`, `"probation_service"`, `"social_services"`. All pass `is_business_category()` as valid "businesses" (assigned priority `"low"`).
- **Trigger:** Category string `"government_office"`, `"detention_centre"`, or `"bishopric"`. All evaluate to `True`.
- **Fix:** Anchor keywords to word boundaries (`\b(?:shop|store|agency|...)\b`) and expand `GEOGRAPHIC_NOISE` to block governmental, diplomatic, municipal, military, and civic welfare entities.

### S2 — Downstream pitch recommender fails on Google Places and unmapped OSM category strings
- **Where:** `pitch_recommender.py:59-60, 75-76, 80-84, 99-130`
- **Breaks:** `pitch_recommender.py` evaluates exact category strings against hardcoded sets: `_ECOM_FRIENDLY`, `_RTYLR_TARGETS`, and `_LEAD_GEN_VERTICALS`. These sets were written using legacy OSM single-noun strings (`"clothes"`, `"shoes"`, `"real_estate_agent"`, `"consulting"`). Google Places types never match these sets:
  - A Google fashion lead with `category="clothing_store"` or `"shoe_store"` fails `category in _ECOM_FRIENDLY`. Instead of receiving `"E-commerce launch + Instagram-to-store funnel"`, it falls back to `"Website launch + capture their existing audience"`.
  - A Google real estate lead with `category="real_estate_agency"` fails `category in _LEAD_GEN_VERTICALS`. Instead of receiving `"Lead-gen website + Google Ads launch"`, it falls back to `"Full digital launch"` or `"Discovery call"`.
  - A Google fitness lead with `category="fitness_center"` fails `_LEAD_GEN_VERTICALS` (which lists `"fitness_centre"` and `"gym"`).
- **Trigger:** A business with `category="clothing_store"`, no website, and active Instagram. `recommend_service()` returns `"Website launch + capture their existing audience"` instead of `"E-commerce launch + Instagram-to-store funnel"`.
- **Fix:** Pass a normalized canonical taxonomy to `recommend_service()` rather than raw source-specific category strings.

### S3 — Singular vs. plural and synonym discrepancies across category definitions
- **Where:** `scrapers/whitelist.py:17-82`
- **Breaks:** Arbitrary grammatical differences between Google types, OSM tags, and `whitelist.py` create unexpected drops:
  - Whitelist specifies `"doctors"`, but Google Places and OSM `healthcare` use `"doctor"` (dropped).
  - Whitelist specifies `"events_venue"`, but Google Places uses `"event_venue"` (dropped).
  - Whitelist specifies `"telecommunications"`, but OSM uses `"telecommunication"` (dropped).
  - Whitelist specifies `"copy_shop"`, but OSM uses `"copyshop"` (passes soft-allow, demoted to low priority).
  - Whitelist specifies `"car_dealership"`, but Google Places uses `"car_dealer"` (dropped).
  - Whitelist specifies `"patisserie"`, but OSM uses `"pastry"` (dropped).
  - Whitelist specifies `"currency_exchange"`, but OSM uses `"bureau_de_change"` (dropped).
  - Whitelist specifies `"cobbler"`, but OSM uses `"shoemaker"` (dropped).
- **Trigger:** `"doctor"`, `"event_venue"`, `"telecommunication"`, `"car_dealer"`. All evaluate to `False`.
- **Fix:** Apply lemmatization/stemming or maintain a canonical synonym mapping dictionary.

---

## Detailed Vocabulary Analysis

### 1. The Vocabulary Gap & The 32-Term Intersection

Testing a comprehensive suite of 178 canonical Google Places API (New & Legacy) types against 242 standard OSM tag values across the six queried keys (`shop`, `amenity`, `office`, `tourism`, `craft`, `healthcare`) reveals that the two vocabularies are largely disjoint.

- **Total Google Places types evaluated:** 178
- **Total OSM tag values evaluated:** 242
- **Direct String Intersection:** **32 terms** (18.0% of Google types, 13.2% of OSM values)

The 32 intersecting terms are:
```
atm, bakery, bank, bar, cafe, car_rental, car_repair, car_wash, dentist,
department_store, electrician, food_court, guest_house, hospital, hostel,
hotel, laundry, lawyer, locksmith, motel, museum, painter, pharmacy,
plumber, pub, restaurant, school, supermarket, swimming_pool, tailor,
travel_agency, university
```

Even within this narrow 32-term intersection, `whitelist.py` rejects 5 terms: `electrician`, `locksmith`, `museum`, `painter`, and `plumber`. Only **27 terms** universally pass across Google, OSM, and the whitelist.

---

### 2. Google Places API Types Breakdown

When standard Google Places types are run through `whitelist.py`:

| Status | Count | Percentage | Description |
|---|---|---|---|
| **Tier 1 (Priority, High)** | 18 | 10.1% | Matches `PRIORITY_INDUSTRIES` directly |
| **Tier 2 (Adjacent, Medium)** | 20 | 11.2% | Matches `ADJACENT_BUSINESSES` directly |
| **Soft-Allow (Priority Low)** | 48 | 27.0% | Passes `is_business_category()` but demoted to `"low"` in `industry_priority()`, excluding from `sales_ready.csv` |
| **Failed / Dropped** | 92 | 51.7% | Evaluates to `False` and dropped completely in `main.py:117` |

#### A. Google Types Passing Tier 1 (High Priority — 18 types)
```
accounting, bar, bed_and_breakfast, cafe, coffee_shop, dental_clinic,
dentist, food_court, guest_house, hostel, hotel, lawyer, motel,
physiotherapist, pub, restaurant, spa, tea_house
```

#### B. Google Types Passing Tier 2 (Medium Priority — 20 types)
```
atm, bakery, bank, car_rental, car_repair, car_wash, department_store,
florist, gym, hospital, laundry, nail_salon, pharmacy, school,
supermarket, swimming_pool, tailor, travel_agency, university, wedding_venue
```

#### C. Google Types Passing Soft-Allow (Priority DEMOTED TO LOW — 48 types)
*These leads are filtered OUT of `sales_ready.csv` and get 0 priority points in `enricher.py`:*
```
amusement_center, asian_grocery_store, auto_parts_store, barber_shop,
beauty_salon, bicycle_store, body_art_service, book_store, butcher_shop,
catering_service, cell_phone_store, child_care_agency, clothing_store,
community_center, convenience_store, convention_center, corporate_office,
courier_service, cultural_center, discount_store, drugstore, electronics_store,
fitness_center, food_store, furniture_store, gift_shop, grocery_store,
hair_salon, hardware_store, home_goods_store, home_improvement_store,
ice_cream_shop, insurance_agency, jewelry_store, liquor_store, moving_company,
pest_control_service, pet_store, pet_supply_store, real_estate_agency,
sandwich_shop, shoe_store, shopping_mall, sporting_goods_store, store,
telecommunications_service_provider, tour_agency, warehouse_store
```

#### D. Google Types WRONGLY DROPPED (Failed / Evaluates to False — 92 types)
*Real commercial businesses dropped on sight:*
- **Restaurants & Food Service:** `lebanese_restaurant`, `fast_food_restaurant`, `seafood_restaurant`, `mediterranean_restaurant`, `middle_eastern_restaurant`, `italian_restaurant`, `pizza_restaurant`, `steak_house`, `sushi_restaurant`, `french_restaurant`, `chinese_restaurant`, `greek_restaurant`, `japanese_restaurant`, `korean_restaurant`, `spanish_restaurant`, `turkish_restaurant`, `thai_restaurant`, `vietnamese_restaurant`, `brazilian_restaurant`, `american_restaurant`, `barbecue_restaurant`, `breakfast_restaurant`, `brunch_restaurant`, `fine_dining_restaurant`, `hamburger_restaurant`, `ramen_restaurant`, `cafeteria`, `meal_delivery`, `meal_takeaway`
- **Hotels & Lodging:** `lodging`, `resort_hotel`, `extended_stay_hotel`, `inn`, `japanese_inn`, `campground`, `camping_cabin`, `cottage`, `farmstay`, `resort_village`
- **Healthcare & Wellness:** `doctor`, `veterinary_care`, `medical_lab`, `foot_care`, `masseuse`
- **Automotive:** `car_dealer`, `gas_station`, `electric_vehicle_charging_station`
- **Personal Care & Services:** `hair_care`, `beautician`, `consultant`, `event_venue`, `locksmith`, `electrician`, `plumber`, `painter`, `painter_decorator`, `roofing_contractor`, `funeral_home`
- **Entertainment & Leisure:** `night_club`, `art_gallery`, `movie_theater`, `movie_rental`, `sports_club`, `sports_complex`, `stadium`, `golf_course`, `bowling_alley`, `casino`, `performing_arts_theater`, `amusement_park`, `aquarium`, `zoo`
- **Commercial & Trade:** `wholesaler`, `market`, `storage`

---

### 3. OpenStreetMap Tag Values Breakdown

When standard OSM values for queried keys (`shop`, `amenity`, `office`, `tourism`, `craft`, `healthcare`) are tested:

| Status | Count | Percentage | Description |
|---|---|---|---|
| **Tier 1 (Priority, High)** | 40 | 16.5% | Matches `PRIORITY_INDUSTRIES` directly |
| **Tier 2 (Adjacent, Medium)** | 72 | 29.8% | Matches `ADJACENT_BUSINESSES` directly |
| **Soft-Allow (Priority Low)** | 3 | 1.2% | `company`, `copyshop`, `employment_agency` |
| **Failed / Dropped** | 127 | 52.5% | Evaluates to `False` and dropped |

#### Key OSM Commercial Categories Wrongly Dropped (127 values total):
- **`office=*`:** `it` (tech startups), `coworking` (coworking spaces), `telecommunication` (dropped due to singular vs plural), `travel_agent` (dropped), `property_management`, `financial`, `therapist`, `newspaper`, `association`
- **`shop=*`:** `greengrocer`, `pastry`, `car_parts`, `bicycle`, `wholesale`, `beverages`, `cheese`, `fabric`, `antiques`, `second_hand`, `health_food`, `medical_supply`, `hearing_aids`, `hifi`, `sports`, `tiles`, `doors`, `flooring`, `kitchen`
- **`tourism=*`:** `chalet` (critical for Lebanese mountain resorts), `apartment` (vacant/serviced vacation apartments), `camp_site`, `theme_park`, `gallery`, `museum`
- **`craft=*`:** `electrician`, `plumber`, `carpenter`, `shoemaker`, `photographer`, `locksmith`, `painter`, `jeweller`, `winery`, `brewery`, `pottery`, `blacksmith`, `dressmaker`, `upholsterer`, `glaziery`
- **`healthcare=*`:** `doctor`, `laboratory`, `audiologist`, `counselling`, `midwife`, `nurse`, `occupational_therapist`, `podiatrist`, `psychotherapist`, `rehabilitation`, `speech_therapist`
- **`amenity=*`:** `bureau_de_change`, `nightclub`, `fuel`, `charging_station`, `music_school`

---

### 4. Unanchored Substring Soft-Allow Leakage

`scrapers/whitelist.py:123-128`:
```python
business_keywords = (
    "shop", "store", "agency", "firm", "company", "service",
    "studio", "office", "salon", "centre", "center", "boutique",
)
if any(kw in cat for kw in business_keywords):
    return True
```

Because `kw in cat` checks substring inclusion without token boundaries or morphological analysis, it permits significant non-business contamination:

#### A. Lexical Collisions (False Positives)
| Input Category | Matching Keyword | Why It Passes | Reality |
|---|---|---|---|
| `bishop` | `"shop"` | Substring in `"bi[shop]"` | Clergy / religious title |
| `bishopric` | `"shop"` | Substring in `"bi[shop]ric"` | Diocese / ecclesiastic office |
| `archbishopric` | `"shop"` | Substring in `"archbi[shop]ric"` | Religious jurisdiction |
| `affirmation` | `"firm"` | Substring in `"af[firm]ation"` | Abstract legal/religious declaration |
| `confirmation` | `"firm"` | Substring in `"con[firm]ation"` | Religious sacrament |
| `accompany` | `"company"` | Substring in `"ac[company]"` | Grammatical verb / Wikidata label |

#### B. Government, Military, and Civic Institutions (False Positives)
None of these are in `GEOGRAPHIC_NOISE`, so they pass through as valid leads:
- **Government & Diplomatic:** `government_office`, `local_government_office`, `government_agency`, `passport_office`, `customs_office`, `tax_office`, `police_office`, `space_agency`, `intelligence_agency`
- **Military & Emergency:** `military_service`, `civil_service`, `secret_service`, `fire_service`, `ambulance_service`, `emergency_service`
- **Civic & Welfare Institutions:** `detention_centre`, `refugee_centre`, `migrant_centre`, `community_centre`, `cultural_centre`, `civic_centre`, `crisis_center`, `social_services`, `probation_service`, `prison_service`, `day_centre`, `recycling_centre`
- **Agricultural / Non-Retail Storage:** `cold_store`, `grain_store`

---

## Not a bug, but worth knowing

1. **Wikidata instance-of (P31) labels frequently differ from OSM/Google:** Wikidata returns human-readable labels such as `"commercial enterprise"`, `"privately held company"`, or `"business"`. `"privately held company"` passes via `"company"`, but `"business"`, `"enterprise"`, and `"commercial enterprise"` evaluate to `False` and are dropped.
2. **Double filtering in `main.py` vs scraper generators:** `whitelist.py:is_business_category` is imported and described in docstrings as if scrapers call it per record (`if not is_business_category(category): continue`), but none of the scrapers (`osm.py`, `google_places.py`, `wikidata.py`) actually import or call it. It is called exclusively in `main.py:117`.
3. **`GEOGRAPHIC_NOISE` contains non-category strings:** Values like `"village/town/city in Lebanon"` and `"human settlement"` are specific artifacts of Wikidata P31 labels, whereas `"yes"` is an OSM artifact from `amenity=yes`. This confirms the file was patched ad-hoc in response to specific scraper leaks rather than designed around a structured schema.

---

## Recommended order of work

1. **Implement a Canonical Category Normalizer (S1):**
   Create a single normalization layer (`normalize_category(raw_category, source)`) that translates Google Place types (e.g. `lebanese_restaurant` -> `restaurant`, `clothing_store` -> `clothes`, `real_estate_agency` -> `real_estate`) and OSM tag values (e.g. `greengrocer` -> `grocery`, `it` -> `software`) into an internal canonical taxonomy.
2. **Unify Category Validation and Priority Tiers (S1):**
   Merge `is_business_category()` and `industry_priority()` so that every accepted canonical category has an explicit priority (`high`, `medium`, or `low`). Ensure categories like `clothing_store` and `real_estate_agency` map to `"high"` priority so they appear in `sales_ready.csv`.
3. **Preserve OSM Tag Namespaces in `scrapers/osm.py` (S1):**
   Update `OSMScraper` to preserve the tag key context (e.g. `f"{key}:{value}"`), allowing the normalizer to distinguish `shop=pastry` from `amenity=pharmacy` or `craft=electrician`.
4. **Anchor Substring Matches & Expand Institutional Noise Blocklist (S2):**
   Replace unanchored `kw in cat` with word-boundary token matching (`re.search(r'\b(?:shop|store|agency|...)\b', cat)`). Add civic, municipal, police, military, and diplomatic entities (`government_office`, `detention_centre`, `military_service`, etc.) to `GEOGRAPHIC_NOISE`.
5. **Update Downstream Sets in `pitch_recommender.py` (S2):**
   Update `_ECOM_FRIENDLY`, `_RTYLR_TARGETS`, and `_LEAD_GEN_VERTICALS` to operate on the canonical normalized categories, ensuring Google Places leads receive appropriate service recommendations.
