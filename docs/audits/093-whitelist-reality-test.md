# 093 — Whitelist Reality Test

## Verdict
`scrapers/whitelist.py:is_business_category()` is fatally vocabulary-blind: it was authored against arbitrary singular/plural English stems, while Google Places emits snake_case type strings (e.g. `lebanese_restaurant`, `lodging`, `doctor`, `car_dealer`) and OpenStreetMap emits raw tag values (e.g. `pastry`, `greengrocer`, `fuel`, `electrician`, `caterer`). Over 36% of valid commercial Google Places types and 28% of real commercial OSM tag values are silently dropped on sight at `main.py:172`. Furthermore, categories that survive via keyword fallback (such as `clothing_store`, `real_estate_agency`, and `beauty_salon`) fail exact-set membership in `industry_priority()`, defaulting to `"low"` priority and causing them to be entirely purged from `sales_ready.csv` at `main.py:202`.

---

## Findings

### S1 — Silent Discard of Primary Regional Business Categories
- **Where:** `scrapers/whitelist.py:105-130`, called in `main.py:172`
- **Breaks:** When scrapers fetch data from Google Places (New) or OpenStreetMap Overpass, `is_business_category()` applies an exact membership test against `PRIORITY_INDUSTRIES` and `ADJACENT_BUSINESSES`, followed by a 12-keyword substring match. Google Places returns specific subtypes (e.g. `lebanese_restaurant`, `mediterranean_restaurant`, `pizza_restaurant`, `seafood_restaurant`, `fast_food_restaurant`, `fine_dining_restaurant`, `lodging`, `doctor`, `medical_lab`, `car_dealer`, `night_club`, `event_venue`). OSM returns standard OpenStreetMap taxonomy tags (e.g. `pastry`, `greengrocer`, `fuel`, `it`, `telecommunication`, `electrician`, `plumber`, `caterer`, `chalet`, `photographer`). Because none of these exact strings exist in the whitelist sets and their stems (`restaurant`, `hotel`, `clinic`, `doctor`, `dealer`, etc.) are absent from `business_keywords`, **every single one of these records is discarded instantly**.
- **Trigger:** A Google Places search for "restaurants in Lebanon" yields a place with `types: ["lebanese_restaurant", "restaurant", "food", "point_of_interest"]`. `scrapers/google_places.py:_pick_category` selects the first non-generic type: `"lebanese_restaurant"`. Calling `is_business_category("lebanese_restaurant")` evaluates to `False`. The restaurant is dropped.
- **Fix:** Normalize incoming category strings by stripping known compound suffixes (`_restaurant`, `_hotel`, `_clinic`, `_store`, `_shop`) and add canonical mappings for Google Places and OSM vocabularies into unified canonical keys before running allowlist tests.

### S1 — Priority Downgrade Cascades to Total Exclusion from `sales_ready.csv`
- **Where:** `scrapers/whitelist.py:133-145`, called in `main.py:190` and filtered in `main.py:202-205`
- **Breaks:** `is_business_category()` has a fallback substring check (`any(kw in cat for kw in business_keywords)`). Consequently, Google Places categories like `clothing_store`, `shoe_store`, `jewelry_store`, `real_estate_agency`, `beauty_salon`, `legal_services`, and `tour_agency` return `True` for `is_business_category()` because they contain `"store"`, `"agency"`, `"salon"`, or `"service"`. However, `industry_priority()` in `whitelist.py:133` contains **no fallback logic whatsoever**:
  ```python
  if cat in PRIORITY_INDUSTRIES:
      return "high"
  if cat in ADJACENT_BUSINESSES:
      return "medium"
  return "low"
  ```
  Because `"clothing_store" != "clothes"`, `"real_estate_agency" != "real_estate"`, and `"beauty_salon" != "beauty"`, all these core Tier 1 target businesses are assigned priority `"low"`. Then, `main.py:202` filters:
  ```python
  sales_ready = [
      r for r in records
      if has_any_contact(r) and r.get("industry_priority") in ("high", "medium")
  ]
  ```
  Every single clothing store, beauty salon, jewelry store, and real estate agency scraped from Google Places is discarded from `sales_ready.csv`, even if it has a phone, verified email, live website, and high rating.
- **Trigger:** Record scraped with `category="beauty_salon"` and valid phone and website. `is_business_category` returns `True`. `industry_priority` returns `"low"`. Lead is excluded from `sales_ready.csv`.
- **Fix:** Align `industry_priority()` to resolve the normalized/canonical category before evaluating tier membership, ensuring aliases inherit their true priority tier.

### S2 — Pitch Recommender Incoherence on Surviving Non-Normalized Categories
- **Where:** `pitch_recommender.py:62, 78, 83, 103-134`
- **Breaks:** `pitch_recommender.py` uses hardcoded sets (`_ECOM_FRIENDLY`, `_RTYLR_TARGETS`, `_LEAD_GEN_VERTICALS`) with exact string matching on `category`. Even if a business survives dedup and enrichment, if its category is `clothing_store` (Google), `shoe_store` (Google), `jeweller` (OSM), `pastry` (OSM), or `estate_agent` (OSM), it fails to match `_ECOM_FRIENDLY` or `_LEAD_GEN_VERTICALS`. It falls through to generic fallbacks like `"Full digital launch"` or `"Discovery call - scope the right service"`, completely bypassing specialized high-converting pitches like `"E-commerce launch + Instagram-to-store funnel"` or `"Lead-gen website + Google Ads launch"`.
- **Trigger:** A fashion brand scraped from Google Places with `category="clothing_store"`, no website, and an Instagram handle. `recommend_service()` checks `category in _ECOM_FRIENDLY`. It evaluates to `False`. The pitch assigned is `"Website launch + capture their existing audience"` instead of `"E-commerce launch + Instagram-to-store funnel"`.
- **Fix:** Resolve categories to a canonical enum/token at ingest, or have `pitch_recommender.py` check canonical category groups.

### S2 — Semantic Gaps in Whitelist Fallback Substrings
- **Where:** `scrapers/whitelist.py:123-128`
- **Breaks:** The substring fallback list `business_keywords` comprises only 12 tokens: `("shop", "store", "agency", "firm", "company", "service", "studio", "office", "salon", "centre", "center", "boutique")`. It omits primary commercial anchors: `restaurant`, `hotel`, `motel`, `resort`, `clinic`, `hospital`, `gym`, `bar`, `cafe`, `market`, `dealer`, `lab`, `repair`, `contractor`, `academy`. Any compound category ending in or containing these roots (e.g., `italian_restaurant`, `skin_care_clinic`, `medical_lab`, `car_dealer`, `roofing_contractor`, `sports_complex`) fails the substring test.
- **Trigger:** Category string `"skin_care_clinic"` (Google Places type for dermatology/aesthetic clinics). Exact match fails; substring match fails because `"clinic"` is not in `business_keywords`.
- **Fix:** Expand root business keywords to include institutional and hospitality nouns, or use token decomposition.

---

## Complete Mapping Tables

### Table 1: Google Places API Types (70 Representative Types)
Tested against `scrapers/whitelist.py` (`is_business_category` and `industry_priority`).
*Note: In Google Places API (New), types are snake_case strings from Table A & Table B.*

| # | Google Places Type | Target Domain | Currently Passes? | Should Pass? | Current Priority | Correct Priority | Failure / Root Cause Analysis |
|---|---|---|:---:|:---:|:---:|:---:|---|
| 1 | `restaurant` | F&B / Dining | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 2 | `cafe` | F&B / Cafe | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 3 | `bar` | F&B / Nightlife | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 4 | `bakery` | F&B / Retail | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 5 | `coffee_shop` | F&B / Cafe | **True** | **True** | high | high | Matches substring `"shop"`. |
| 6 | `fast_food_restaurant` | F&B / Quick Service | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Whitelist has `"fast_food"`. Missing `_restaurant` suffix support. |
| 7 | `pizza_restaurant` | F&B / Dining | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Compound cuisine type; `"restaurant"` not in `business_keywords`. |
| 8 | `lebanese_restaurant` | F&B / Dining | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Core regional query in LB; dropped by exact match. |
| 9 | `mediterranean_restaurant`| F&B / Dining | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Core regional query; dropped by exact match. |
| 10 | `middle_eastern_restaurant`| F&B / Dining | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Core regional query in SA & LB; dropped. |
| 11 | `italian_restaurant` | F&B / Dining | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Common restaurant type; dropped. |
| 12 | `seafood_restaurant` | F&B / Dining | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Whitelist has `"seafood"`, but exact match fails. |
| 13 | `fine_dining_restaurant` | F&B / Luxury Dining | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** High-budget pitch target; dropped. |
| 14 | `meal_takeaway` | F&B / Casual | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Google standard takeaway type; dropped. |
| 15 | `meal_delivery` | F&B / Delivery | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Google standard delivery type; dropped. |
| 16 | `ice_cream_shop` | F&B / Sweets | **True** | **True** | low | high | Passes via `"shop"`, but **PRIORITY DOWNGRADED** to `"low"`! Excluded from sales_ready. |
| 17 | `sandwich_shop` | F&B / Casual | **True** | **True** | low | high | Passes via `"shop"`, but **PRIORITY DOWNGRADED** to `"low"`! Excluded from sales_ready. |
| 18 | `hotel` | Hospitality | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 19 | `motel` | Hospitality | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 20 | `lodging` | Hospitality | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Google's top umbrella type for hotels/accommodations. Catastrophic loss. |
| 21 | `resort_hotel` | Hospitality | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Whitelist has `"resort"`, but `"resort_hotel"` fails exact match. |
| 22 | `bed_and_breakfast` | Hospitality | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 23 | `guest_house` | Hospitality | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 24 | `hostel` | Hospitality | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 25 | `extended_stay_hotel` | Hospitality | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Suffix `_hotel` not recognized. |
| 26 | `doctor` | Healthcare | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Whitelist has `"doctors"` (plural). Google returns `"doctor"` (singular). |
| 27 | `dentist` | Healthcare | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 28 | `dental_clinic` | Healthcare | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 29 | `physiotherapist` | Healthcare | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 30 | `skin_care_clinic` | Healthcare / Aesthetics | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Google type for aesthetic/cosmetic clinics; `"clinic"` not a keyword. |
| 31 | `medical_lab` | Healthcare / Diagnostics | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Medical diagnostic testing center; dropped. |
| 32 | `chiropractor` | Healthcare | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** High-margin healthcare practice; dropped. |
| 33 | `veterinary_care` | Healthcare / Vet | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Whitelist has `"veterinary"`; Google returns `"veterinary_care"`. |
| 34 | `hospital` | Healthcare | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 35 | `pharmacy` | Healthcare / Retail | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 36 | `drugstore` | Healthcare / Retail | **True** | **True** | low | medium | Passes via `"store"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 37 | `wellness_center` | Healthcare / Wellness | **True** | **True** | low | high | Passes via `"center"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 38 | `real_estate_agency` | Real Estate | **True** | **True** | low | high | Passes via `"agency"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 39 | `lawyer` | Legal Services | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 40 | `legal_services` | Legal Services | **True** | **True** | low | high | Passes via `"service"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 41 | `accounting` | Professional Services | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 42 | `consultant` | Professional Services | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Whitelist has `"consulting"`; Google returns `"consultant"`. |
| 43 | `advertising_agency` | Marketing / Agency | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 44 | `insurance_agency` | Insurance Brokerage | **True** | **True** | low | medium | Passes via `"agency"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 45 | `corporate_office` | Business Services | **True** | **True** | low | medium | Passes via `"office"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 46 | `travel_agency` | Tourism / Travel | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 47 | `tour_agency` | Tourism / Tours | **True** | **True** | low | high | Passes via `"agency"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 48 | `telecommunications_service_provider` | Tech / Telco | **True** | **True** | low | high | Passes via `"service"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 49 | `clothing_store` | Fashion / Retail | **True** | **True** | low | high | Passes via `"store"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 50 | `shoe_store` | Fashion / Footwear | **True** | **True** | low | high | Passes via `"store"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 51 | `jewelry_store` | Luxury / Jewelry | **True** | **True** | low | high | Passes via `"store"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 52 | `electronics_store` | Consumer Electronics | **True** | **True** | low | medium | Passes via `"store"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 53 | `furniture_store` | Home Furnishings | **True** | **True** | low | medium | Passes via `"store"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 54 | `home_goods_store` | Home Retail | **True** | **True** | low | medium | Passes via `"store"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 55 | `convenience_store` | Retail / Grocery | **True** | **True** | low | medium | Passes via `"store"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 56 | `supermarket` | Retail / Grocery | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 57 | `department_store` | Retail | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 58 | `shopping_mall` | Commercial Retail | **True** | **True** | low | medium | Whitelist has `"mall"`; `"shopping_mall"` downgraded to `"low"`. |
| 59 | `book_store` | Retail / Books | **True** | **True** | low | medium | Passes via `"store"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 60 | `pet_store` | Retail / Pets | **True** | **True** | low | medium | Passes via `"store"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 61 | `florist` | Retail / Flowers | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 62 | `auto_parts_store` | Automotive Retail | **True** | **True** | low | medium | Passes via `"store"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 63 | `hardware_store` | Hardware / DIY | **True** | **True** | low | medium | Passes via `"store"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 64 | `liquor_store` | Beverage Retail | **True** | **True** | low | medium | Passes via `"store"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 65 | `wholesaler` | B2B Trade / Wholesale | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Whitelist has `"trade"`; Google returns `"wholesaler"`. |
| 66 | `market` | Retail / Marketplace | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Whitelist has `"marketplace"`; Google returns `"market"`. |
| 67 | `beauty_salon` | Personal Care / Beauty | **True** | **True** | low | high | Passes via `"salon"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 68 | `hair_salon` | Personal Care / Hair | **True** | **True** | low | medium | Passes via `"salon"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 69 | `hair_care` | Personal Care / Hair | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Google type for barbers/salons; dropped. |
| 70 | `barber_shop` | Personal Care / Barber | **True** | **True** | low | medium | Passes via `"shop"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 71 | `nail_salon` | Personal Care / Nails | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 72 | `spa` | Personal Care / Spa | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 73 | `tattoo` | Personal Care | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 74 | `laundry` | Local Services | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 75 | `tailor` | Personal Services | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 76 | `locksmith` | Trades & Maintenance | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Standard local trade; dropped. |
| 77 | `electrician` | Trades & Maintenance | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Standard local trade; dropped. |
| 78 | `plumber` | Trades & Maintenance | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Standard local trade; dropped. |
| 79 | `roofing_contractor` | Trades & Contracting | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** High-ticket contracting business; dropped. |
| 80 | `painter` | Trades & Contracting | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Whitelist has `"paint"` (retail); contractor dropped. |
| 81 | `moving_company` | Services & Logistics | **True** | **True** | low | medium | Passes via `"company"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 82 | `storage` | Services & Storage | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Commercial self-storage/warehousing; dropped. |
| 83 | `car_dealer` | Automotive Dealership | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Whitelist has `"car_dealership"`; Google returns `"car_dealer"`. |
| 84 | `car_repair` | Automotive Repair | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 85 | `car_rental` | Automotive Rental | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 86 | `car_wash` | Automotive Cleaning | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 87 | `gas_station` | Auto Fueling / Retail | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Major regional commercial hub in LB/SA; dropped. |
| 88 | `gym` | Fitness / Gym | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 89 | `fitness_center` | Fitness Center | **True** | **True** | low | medium | Passes via `"center"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 90 | `sports_complex` | Sports / Fitness | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Whitelist has `"sports_centre"`; Google returns `"sports_complex"`. |
| 91 | `swimming_pool` | Sports / Aquatic | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 92 | `event_venue` | Events / Hospitality | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Whitelist has `"events_venue"` (plural); Google returns `"event_venue"`. |
| 93 | `banquet_hall` | Events / Hospitality | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Core regional wedding/event hall type; dropped. |
| 94 | `wedding_venue` | Events / Weddings | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 95 | `night_club` | Nightlife / Hospitality | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Google returns `"night_club"` (two words); dropped. |
| 96 | `movie_theater` | Entertainment | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Commercial cinema; dropped. |
| 97 | `locality` | Geographic Noise | ❌ **False** | ❌ **False** | low | n/a | Correctly dropped by `GEOGRAPHIC_NOISE`. |
| 98 | `sublocality` | Geographic Noise | ❌ **False** | ❌ **False** | low | n/a | Correctly dropped (not in whitelist). |
| 99 | `church` | Place of Worship | ❌ **False** | ❌ **False** | low | n/a | Correctly dropped by `GEOGRAPHIC_NOISE`. |
| 100| `cemetery` | Civic Noise | ❌ **False** | ❌ **False** | low | n/a | Correctly dropped by `GEOGRAPHIC_NOISE`. |

---

### Table 2: OpenStreetMap Tag Values (70 Representative Tag Values)
Tested against `scrapers/whitelist.py` (`is_business_category` and `industry_priority`).
*Note: In OSM, tags are extracted via `tags.get('shop') or tags.get('amenity') or tags.get('office') or tags.get('tourism') or tags.get('craft') or tags.get('healthcare')`.*

| # | OSM Tag Value | Primary OSM Key | Target Domain | Currently Passes? | Should Pass? | Current Priority | Correct Priority | Failure / Root Cause Analysis |
|---|---|---|---|:---:|:---:|:---:|:---:|---|
| 1 | `supermarket` | `shop` | Retail / Grocery | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 2 | `bakery` | `shop` | Retail / F&B | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 3 | `clothes` | `shop` | Fashion Retail | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 4 | `hairdresser` | `shop` | Personal Care | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 5 | `car_repair` | `shop` / `craft` | Automotive Repair | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 6 | `convenience` | `shop` | Retail / Grocery | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 7 | `butcher` | `shop` | Retail / Meat | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 8 | `electronics` | `shop` | Consumer Tech | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 9 | `shoes` | `shop` | Fashion Footwear | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 10 | `car` | `shop` | Auto Dealership | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 11 | `florist` | `shop` | Retail / Flowers | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 12 | `beauty` | `shop` | Beauty / Cosmetics | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 13 | `jewelry` | `shop` | Jewelry / Luxury | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 14 | `stationery` | `shop` | Office Supplies | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 15 | `kiosk` | `shop` | Retail Kiosk | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 16 | `furniture` | `shop` | Home Furnishings | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 17 | `optician` | `shop` | Healthcare / Eyewear | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 18 | `chemist` | `shop` | Healthcare / Pharmacy | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 19 | `beverages` | `shop` | Beverage Retail | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Standard OSM tag for drink stores; dropped. |
| 20 | `pastry` | `shop` | Bakery / Sweets | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Common in Lebanon (Arabic & French pastries); whitelist only has `"patisserie"`. |
| 21 | `confectionery` | `shop` | Sweets / Candy | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 22 | `tailor` | `shop` / `craft` | Personal Services | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 23 | `hardware` | `shop` | Hardware / Tools | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 24 | `mall` | `shop` | Shopping Mall | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 25 | `greengrocer` | `shop` | Fruit & Veggie Store | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Pervasive commercial shop type across Lebanon; dropped. |
| 26 | `seafood` | `shop` | Fish Market | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 27 | `dairy` | `shop` | Dairy Retail | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 28 | `ice_cream` | `shop` | Sweets / Ice Cream | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 29 | `cosmetics` | `shop` | Beauty Products | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 30 | `kitchen` | `shop` | Kitchenware | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 31 | `lighting` | `shop` | Home Lighting | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 32 | `paint` | `shop` | Paint & Decor | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 33 | `curtain` | `shop` | Drapes / Decor | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 34 | `bed` | `shop` | Bedding & Mattresses | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Standard OSM tag for mattress/bed stores; dropped. |
| 35 | `carpet` | `shop` | Flooring & Rugs | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 36 | `antiques` | `shop` | Antique Retail | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Popular high-ticket boutique retail; dropped. |
| 37 | `second_hand` | `shop` | Thrift / Vintage | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Popular vintage/thrift shop tag; dropped. |
| 38 | `variety_store` | `shop` | Discount Retail | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 39 | `general` | `shop` | General Store | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 40 | `medical_supply` | `shop` | Medical Equipment | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 41 | `massage` | `shop` / `amenity` | Wellness / Therapy | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 42 | `tattoo` | `shop` | Body Art / Tattoo | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 43 | `fabric` | `shop` | Textiles & Fabric | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Pervasive trade in Beirut/Tripoli; dropped. |
| 44 | `haberdashery` | `shop` | Sewing & Notions | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Standard OSM tag; dropped. |
| 45 | `sports` | `shop` | Sporting Goods | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Retail sporting goods shop; dropped. |
| 46 | `toys` | `shop` | Toy Store | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 47 | `motorcycle` | `shop` | Motorcycle Dealer | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 48 | `art` | `shop` | Art & Gallery | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 49 | `musical_instrument` | `shop` | Instruments Retail | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 50 | `tobacco` | `shop` | Tobacco / Vape | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 51 | `photo` | `shop` | Photography Retail | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 52 | `houseware` | `shop` | Home Goods | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Common home retail shop; dropped. |
| 53 | `doityourself` | `shop` | DIY / Hardware | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 54 | `department_store` | `shop` | Department Store | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 55 | `mobile_phone` | `shop` | Telecom Retail | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 56 | `books` | `shop` | Bookstore | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 57 | `alcohol` | `shop` | Liquor Retail | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 58 | `dry_cleaning` | `shop` | Laundry Services | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 59 | `tyres` | `shop` | Tire Sales & Repair | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 60 | `copyshop` | `shop` | Printing & Copying | **True** | **True** | low | medium | Passes via `"shop"`, but **PRIORITY DOWNGRADED** to `"low"`! (Whitelist has `"copy_shop"`). |
| 61 | `pet` | `shop` | Pet Store | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 62 | `caterer` | `craft` / `amenity` | Hospitality / Catering | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** High-ticket catering business; dropped. |
| 63 | `restaurant` | `amenity` | F&B / Dining | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 64 | `cafe` | `amenity` | F&B / Cafe | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 65 | `fast_food` | `amenity` | F&B / Fast Food | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 66 | `bar` | `amenity` | F&B / Bar | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 67 | `pub` | `amenity` | F&B / Pub | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 68 | `pharmacy` | `amenity` | Healthcare Retail | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 69 | `bank` | `amenity` | Financial Services | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 70 | `atm` | `amenity` | Financial | **True** | **True** | medium | low | Passes via `ADJACENT_BUSINESSES`. (Should be low/noise). |
| 71 | `clinic` | `amenity` | Healthcare Clinic | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 72 | `hospital` | `amenity` | Hospital | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 73 | `dentist` | `amenity` | Healthcare / Dental | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 74 | `doctors` | `amenity` | Healthcare / Clinic | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 75 | `veterinary` | `amenity` | Animal Healthcare | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 76 | `school` | `amenity` | Education | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 77 | `kindergarten` | `amenity` | Education | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 78 | `college` | `amenity` | Higher Education | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 79 | `university` | `amenity` | Higher Education | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 80 | `driving_school` | `amenity` | Vocational Training | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 81 | `fuel` | `amenity` | Gas Station & Retail | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Standard OSM tag for gas stations; dropped. |
| 82 | `car_wash` | `amenity` | Automotive Cleaning | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 83 | `car_rental` | `amenity` | Automotive Rental | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 84 | `nightclub` | `amenity` | Nightlife / Hospitality | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Whitelist only has `"bar"`, `"pub"`; dropped. |
| 85 | `cinema` | `amenity` | Commercial Cinema | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Commercial entertainment business; dropped. |
| 86 | `theatre` | `amenity` | Live Performing Arts | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Commercial theatre/hall; dropped. |
| 87 | `marketplace` | `amenity` | Market Facility | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 88 | `events_venue` | `amenity` | Events Venue | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 89 | `coworking_space` | `amenity` | Tech / Shared Workspace | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Standard OSM tag for coworking hubs; dropped. |
| 90 | `food_court` | `amenity` | F&B / Food Court | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 91 | `bistro` | `amenity` | F&B / Bistro | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 92 | `biergarten` | `amenity` | F&B / Beer Garden | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 93 | `company` | `office` | Corporate Office | **True** | **True** | low | medium | Passes via `"company"`, but **PRIORITY DOWNGRADED** to `"low"`! Excluded from sales_ready. |
| 94 | `it` | `office` | Tech / Software House | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Standard OSM tag for IT companies; dropped. |
| 95 | `lawyer` | `office` | Legal Practice | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 96 | `insurance` | `office` | Insurance Broker | **True** | **True** | medium | medium | Exact match in `ADJACENT_BUSINESSES`. |
| 97 | `estate_agent` | `office` | Real Estate Agency | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 98 | `architect` | `office` | Architecture Studio | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 99 | `accountant` | `office` | Accounting Firm | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 100| `consulting` | `office` | Management Consulting | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 101| `advertising_agency` | `office` | Advertising Agency | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 102| `telecommunication` | `office` | Telecom Operator | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Whitelist has `"telecommunications"` (plural); OSM tag is singular. |
| 103| `employment_agency` | `office` | Recruitment Agency | **True** | **True** | low | high | Passes via `"agency"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 104| `financial` | `office` | Financial Services | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Financial advisory/wealth office; dropped. |
| 105| `travel_agent` | `office` | Travel Agency | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Whitelist has `"travel_agency"`; OSM tag is `"travel_agent"`. |
| 106| `coworking` | `office` | Coworking Office | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Standard OSM office tag for coworking; dropped. |
| 107| `carpenter` | `craft` | Carpentry & Woodwork | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Pervasive trade in LB; dropped. |
| 108| `electrician` | `craft` | Electrical Contractor | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Standard trade; dropped. |
| 109| `plumber` | `craft` | Plumbing Contractor | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Standard trade; dropped. |
| 110| `painter` | `craft` | Painting Contractor | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Standard trade; dropped. |
| 111| `photographer` | `craft` | Commercial Photographer | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Whitelist has `"photo"` / `"photography_studio"`; dropped. |
| 112| `shoemaker` | `craft` | Cobbler & Shoe Repair | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Whitelist has `"cobbler"`; OSM craft tag is `"shoemaker"`. |
| 113| `jeweller` | `craft` | Goldsmith & Jewelry | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Whitelist has `"jewelry"` / `"jewellery"`; craft tag is `"jeweller"`. |
| 114| `blacksmith` | `craft` | Metal Fabrication | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Metal shop/fabrication; dropped. |
| 115| `upholsterer` | `craft` | Furniture Upholstery | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Custom furniture craft; dropped. |
| 116| `hotel` | `tourism` | Hotel | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 117| `guest_house` | `tourism` | Guest House | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 118| `hostel` | `tourism` | Hostel | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 119| `motel` | `tourism` | Motel | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 120| `chalet` | `tourism` | Mountain / Beach Resort | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Massive holiday rental industry in Lebanon (Faraya/Batroun); dropped. |
| 121| `camp_site` | `tourism` | Camping / Glamping | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Glamping/ecotourism hospitality; dropped. |
| 122| `theme_park` | `tourism` | Theme / Amusement Park | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Commercial tourist attraction; dropped. |
| 123| `doctor` | `healthcare` | Medical Doctor | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Whitelist has `"doctors"` (plural); healthcare tag is singular. |
| 124| `centre` | `healthcare` | Medical Center | **True** | **True** | low | medium | Passes via `"centre"`, but **PRIORITY DOWNGRADED** to `"low"`. Excluded from sales_ready. |
| 125| `physiotherapist` | `healthcare` | Physical Therapy | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 126| `alternative` | `healthcare` | Naturopathy / Holistic | ❌ **False** | **True** | low | medium | **WRONGLY DROPPED.** Wellness/alternative practice; dropped. |
| 127| `optometrist` | `healthcare` | Eye Care / Optometry | **True** | **True** | high | high | Exact match in `PRIORITY_INDUSTRIES`. |
| 128| `laboratory` | `healthcare` | Diagnostic Laboratory | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Major clinical testing labs in Lebanon; dropped. |
| 129| `audiologist` | `healthcare` | Hearing Care | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Private audiology clinic; dropped. |
| 130| `speech_therapist`| `healthcare` | Speech Therapy Clinic | ❌ **False** | **True** | low | high | **WRONGLY DROPPED.** Private medical clinic; dropped. |
| 131| `place_of_worship` | `amenity` | Religious Noise | ❌ **False** | ❌ **False** | low | n/a | Correctly dropped by `GEOGRAPHIC_NOISE`. |
| 132| `church` | `amenity` | Religious Noise | ❌ **False** | ❌ **False** | low | n/a | Correctly dropped by `GEOGRAPHIC_NOISE`. |
| 133| `grave_yard` | `amenity` | Cemetery Noise | ❌ **False** | ❌ **False** | low | n/a | Correctly dropped by `GEOGRAPHIC_NOISE`. |
| 134| `yes` | `amenity` | Tag Noise (`amenity=yes`)| ❌ **False** | ❌ **False** | low | n/a | Correctly dropped by `GEOGRAPHIC_NOISE`. |
| 135| `bench` | `amenity` | Street Furniture | ❌ **False** | ❌ **False** | low | n/a | Correctly dropped (not in whitelist). |

---

## Inventory of Wrongly Dropped Categories

### 1. Food & Beverage / Hospitality
- **Google Places Types:** `fast_food_restaurant`, `pizza_restaurant`, `seafood_restaurant`, `lebanese_restaurant`, `mediterranean_restaurant`, `middle_eastern_restaurant`, `italian_restaurant`, `fine_dining_restaurant`, `meal_takeaway`, `meal_delivery`, `lodging`, `resort_hotel`, `extended_stay_hotel`, `night_club`.
- **OSM Tag Values:** `pastry`, `caterer`, `nightclub`, `chalet`, `camp_site`, `theme_park`.
- **Business Impact:** Google Places queries for `"restaurants in Lebanon"` and `"restaurant groups in Riyadh"` return places whose primary type is the cuisine (`lebanese_restaurant`, `middle_eastern_restaurant`, `fine_dining_restaurant`). Google's top umbrella category for hotels and resorts is `lodging`. Dropping `lodging` wipes out boutique and luxury hotels that do not explicitly tag `hotel` as their first type. In Lebanon, `chalet` is the primary tag for private mountain and beach resort rentals (Faraya, Faqra, Kfardebian, Batroun), a major high-budget target for web development.

### 2. Healthcare, Medical & Aesthetics
- **Google Places Types:** `doctor`, `medical_lab`, `skin_care_clinic`, `chiropractor`, `veterinary_care`.
- **OSM Tag Values:** `doctor`, `laboratory`, `audiologist`, `speech_therapist`, `alternative`.
- **Business Impact:** Clinics and healthcare networks are Voxire's top margin sector. Google returns `skin_care_clinic` for aesthetic/cosmetic clinics; OSM returns `laboratory` for diagnostic centers (e.g. Rodolphe Mérieux, Saint George). The simple plural mismatch (`doctor` vs `doctors`) drops every solo medical practitioner in both Google Places and OSM.

### 3. Retail & E-commerce
- **Google Places Types:** `wholesaler`, `market`.
- **OSM Tag Values:** `beverages`, `greengrocer`, `bed`, `antiques`, `second_hand`, `fabric`, `haberdashery`, `sports`, `houseware`.
- **Business Impact:** Lebanon's commercial centers (Hamra, Mar Mikhael, Tripoli Souks) contain dense clusters of textile merchants (`fabric`), mattress/bedding stores (`bed`), sports shops (`sports`), and produce markets (`greengrocer`). In Saudi Arabia, wholesale B2B merchants (`wholesaler`) are prime candidates for digital ordering systems.

### 4. Trades, Construction & Maintenance
- **Google Places Types:** `locksmith`, `electrician`, `plumber`, `roofing_contractor`, `painter`, `storage`, `car_dealer`, `gas_station`.
- **OSM Tag Values:** `fuel`, `carpenter`, `electrician`, `plumber`, `painter`, `photographer`, `shoemaker`, `blacksmith`, `upholsterer`.
- **Business Impact:** Trades and contractors are high-ticket service businesses with high demand for SEO and local landing pages. In both Lebanon and KSA, gas stations (`fuel`, `gas_station`) are substantial commercial facilities featuring retail shops, cafes, and car care bays. In Google Places, car dealerships are returned as `car_dealer` (dropped because whitelist only has `car_dealership`).

### 5. Professional Services, Tech & Office
- **Google Places Types:** `consultant`, `event_venue`, `banquet_hall`, `movie_theater`, `sports_complex`.
- **OSM Tag Values:** `it`, `telecommunication`, `financial`, `travel_agent`, `coworking`, `coworking_space`, `cinema`, `theatre`.
- **Business Impact:** Software and IT agencies are tagged `office=it` in OSM; coworking spaces are tagged `amenity=coworking_space` or `office=coworking`. Management consultants in Google Places are returned as `consultant`. Event venues are returned as `event_venue` (singular) and `banquet_hall`. All are dropped.

---

## Corrected Sets and Normalization Engine Specification

To fix this cleanly, `whitelist.py` must decouple **vocabulary normalization** from **priority assignment**:
1. **Stem & Suffix Stripper:** Automatically strip standard Google compound suffixes (`_restaurant`, `_hotel`, `_store`, `_shop`, `_agency`, `_clinic`, `_service`, `_dealer`, `_center`, `_centre`).
2. **Canonical Mapping Dictionary:** Explicitly resolve vocabulary quirks (`lodging` -> `hotel`, `fuel` -> `gas_station`, `doctor` -> `doctors`, `pastry` -> `patisserie`).
3. **Comprehensive Priority Sets:** Expanded sets covering both OSM tags and Google Places type strings.
4. **Coherent Priority Lookup:** `industry_priority()` must evaluate the canonicalized category, preventing priority downgrades.

### Full Corrected Python Code (`scrapers/whitelist.py`)

```python
"""
Category whitelist and normalization engine for leadminer.

Handles Google Places API (New) snake_case types, OpenStreetMap raw tag values,
and Wikidata P31 entity labels. Normalizes incoming strings, drops geographic/civic
noise, and tags priority for sales outreach.
"""

from typing import Optional

# ---------------------------------------------------------------------------
# Tier 1: High Priority (High margin, core Voxire digital pitch verticals)
# ---------------------------------------------------------------------------
PRIORITY_INDUSTRIES = {
    # Hospitality & F&B
    "restaurant", "cafe", "fast_food", "bar", "pub", "bistro", "biergarten",
    "food_court", "ice_cream", "patisserie", "pastry", "tea_house", "coffee_shop",
    "sandwich_shop", "caterer", "catering_service", "fine_dining_restaurant",
    "meal_takeaway", "meal_delivery", "night_club", "nightclub",
    # Lodging & Accommodation
    "hotel", "guest_house", "hostel", "motel", "resort", "resort_hotel",
    "boutique_hotel", "bed_and_breakfast", "extended_stay_hotel", "lodging",
    "chalet", "tourism", "tour_operator", "tour_agency",
    # Healthcare & Clinics
    "clinic", "clinics", "doctor", "doctors", "dentist", "dental_clinic",
    "veterinary", "veterinary_care", "vet", "physiotherapist", "physio",
    "optician", "optometrist", "cosmetic_surgery", "cosmetic_clinic",
    "dermatology", "dermatologist", "fertility_clinic", "medical_clinic",
    "aesthetic_clinic", "skin_care_clinic", "spa", "wellness", "wellness_center",
    "medical_centre", "medical_center", "medical_lab", "laboratory",
    "chiropractor", "audiologist", "speech_therapist",
    # Real Estate & Property
    "real_estate", "real_estate_agent", "real_estate_agency", "estate_agent",
    "property", "property_developer",
    # Professional Services & Agencies
    "lawyer", "law_firm", "legal_services", "attorney", "notary",
    "accountant", "accounting", "tax_advisor", "consulting", "consultant",
    "marketing_agency", "advertising_agency", "design_studio", "architect",
    "engineering_firm", "employment_agency",
    # Fashion, Luxury & High-AOV Retail
    "clothes", "clothing_store", "boutique", "shoes", "shoe_store",
    "jewelry", "jewellery", "jeweller", "jewelry_store", "bag", "watches",
    "fashion_accessories", "perfumery", "cosmetics", "beauty", "beauty_salon",
    "lingerie",
    # Technology, Telecom & Modern Workspaces
    "software", "it", "coworking", "coworking_space", "internet_cafe",
    "computer", "telecommunications", "telecommunication",
    "telecommunications_service_provider",
}

# ---------------------------------------------------------------------------
# Tier 2: Adjacent Businesses (Real commercial entities, secondary priority)
# ---------------------------------------------------------------------------
ADJACENT_BUSINESSES = {
    # Retail & Grocery
    "supermarket", "convenience", "convenience_store", "grocery", "grocery_store",
    "bakery", "butcher", "butcher_shop", "deli", "wine", "alcohol", "liquor_store",
    "tobacco", "newsagent", "kiosk", "department_store", "mall", "shopping_mall",
    "shopping_centre", "marketplace", "market", "variety_store", "general",
    "general_store", "trade", "wholesaler", "beverages", "greengrocer", "dairy",
    "seafood", "confectionery", "chocolate", "spices", "antiques", "second_hand",
    # Personal Care & Local Services
    "hairdresser", "hair_salon", "hair_care", "barber", "barber_shop",
    "nail_salon", "massage", "tattoo", "tanning", "laundry", "dry_cleaning",
    "tailor", "cobbler", "shoemaker", "locksmith",
    # Trades, Crafts & Construction
    "carpenter", "electrician", "plumber", "painter", "blacksmith",
    "upholsterer", "roofing_contractor", "general_contractor", "moving_company",
    "storage", "warehouse",
    # Healthcare Adjacent & Institutional
    "pharmacy", "chemist", "drugstore", "hospital", "medical_supply", "alternative",
    # Fitness & Sports
    "fitness", "fitness_centre", "fitness_center", "gym", "yoga", "pilates",
    "sports_centre", "sports_complex", "swimming_pool",
    # Education & Childcare
    "school", "kindergarten", "preschool", "language_school", "music_school",
    "driving_school", "training_centre", "tutoring", "university", "college",
    "academy", "child_care_agency",
    # Automotive & Fuel
    "car", "car_dealer", "car_dealership", "car_repair", "car_wash", "car_rental",
    "motorcycle", "tyres", "auto_parts_store", "parts", "fuel", "gas_station",
    # Home & Lifestyle
    "furniture", "furniture_store", "interior_decoration", "florist",
    "garden_centre", "hardware", "hardware_store", "doityourself", "paint",
    "lighting", "carpet", "curtain", "kitchen", "bathroom_furnishing", "appliance",
    "home_goods_store", "bed", "houseware", "fabric", "haberdashery",
    # Specialty Retail & Hobbies
    "books", "book_store", "art", "music", "musical_instrument", "stationery",
    "gift", "gift_shop", "toys", "pet", "pet_store", "pet_grooming",
    "photo", "photography_studio", "photographer", "video", "electronics",
    "electronics_store", "mobile_phone", "computer_repair", "sports",
    # Financial & Admin Services
    "bank", "atm", "money_lender", "money_transfer", "insurance",
    "insurance_agency", "currency_exchange", "post_office", "courier",
    "courier_service", "logistics", "corporate_office", "financial",
    # Events & Entertainment
    "travel_agency", "travel_agent", "events_venue", "event_venue",
    "wedding_venue", "banquet_hall", "printing", "copyshop", "copy_shop",
    "advertising", "camp_site", "theme_park", "cinema", "movie_theater",
    "theatre",
}

# ---------------------------------------------------------------------------
# Hard-block Noise: Non-commercial, geographic, infrastructure, or civic tags
# ---------------------------------------------------------------------------
GEOGRAPHIC_NOISE = {
    # Geographic entities & settlements
    "village/town/city in lebanon", "village", "town", "city", "hamlet",
    "suburb", "neighbourhood", "neighborhood", "quarter", "borough",
    "municipality", "human settlement", "place", "locality", "sublocality",
    "administrative_area_level_1", "administrative_area_level_2",
    "administrative_area_level_3", "country", "postal_code",
    # Topography & Nature
    "mountain", "peak", "hill", "ridge", "valley", "plateau",
    "watercourse", "river", "stream", "lake", "spring", "waterfall",
    "wadi", "bay", "cape", "island", "beach", "coast",
    "park", "garden", "playground", "nature_reserve", "protected_area",
    "forest", "wood", "grassland", "meadow", "wetland",
    # Places of Worship & Cemeteries
    "place_of_worship", "church", "mosque", "temple", "shrine",
    "cemetery", "grave_yard", "monastery", "synagogue",
    # Heritage, Civic & Monuments
    "monument", "memorial", "archaeological_site", "ruins", "castle",
    "fort", "tower", "obelisk", "statue", "city_hall", "courthouse",
    "embassy", "fire_station", "police", "local_government_office",
    # Infrastructure & Street Elements
    "highway", "road", "street_address", "route", "path", "track",
    "junction", "intersection", "roundabout", "bus_stop", "parking",
    "fuel_dispenser", "bench", "waste_basket", "fountain",
    # Generic Tag Noise
    "yes", "establishment", "point_of_interest", "premise", "subpremise",
    "metaorganization", "organization", "meta",
}

# ---------------------------------------------------------------------------
# Direct Vocabulary Cross-walk (Google Places / OSM / Wikidata -> Canonical)
# ---------------------------------------------------------------------------
CANONICAL_ALIASES = {
    # Google Places aliases
    "lodging": "hotel",
    "resort_hotel": "resort",
    "car_dealer": "car_dealership",
    "event_venue": "events_venue",
    "night_club": "nightclub",
    "movie_theater": "cinema",
    "theatre": "theatre",
    "gas_station": "fuel",
    "beauty_salon": "beauty",
    "skin_care_clinic": "aesthetic_clinic",
    "medical_lab": "laboratory",
    "veterinary_care": "veterinary",
    "legal_services": "law_firm",
    "lawyer": "lawyer",
    "tour_agency": "tour_operator",
    "corporate_office": "office",
    "wholesaler": "trade",
    "market": "marketplace",
    "fitness_center": "fitness_centre",
    "sports_complex": "sports_centre",
    "barber_shop": "barber",
    "hair_salon": "hairdresser",
    "hair_care": "hairdresser",
    # OSM aliases
    "pastry": "patisserie",
    "greengrocer": "grocery",
    "beverages": "alcohol",
    "it": "software",
    "coworking_space": "coworking",
    "telecommunication": "telecommunications",
    "travel_agent": "travel_agency",
    "shoemaker": "cobbler",
    "jeweller": "jewelry",
    "copyshop": "copy_shop",
    "bed": "furniture",
    "houseware": "interior_decoration",
    "fabric": "clothes",
    "haberdashery": "clothes",
    "doctor": "doctors",
    "caterer": "caterer",
    "chalet": "resort",
}

# Compound suffixes to strip to reach canonical base
COMPOUND_SUFFIXES = [
    "_restaurant", "_hotel", "_clinic", "_store", "_shop",
    "_agency", "_firm", "_service", "_dealer", "_centre", "_center",
]

# Business-identifying root substrings
BUSINESS_KEYWORDS = (
    "shop", "store", "agency", "firm", "company", "service",
    "studio", "office", "salon", "centre", "center", "boutique",
    "restaurant", "hotel", "clinic", "hospital", "gym", "bar",
    "cafe", "market", "repair", "contractor", "academy",
)


def normalize_category(category: Optional[str]) -> str:
    """
    Cleans and canonicalizes category strings from Google Places and OSM.
    Converts compound strings like 'lebanese_restaurant' into 'restaurant'.
    """
    if not category:
        return ""
    cat = category.strip().lower()

    # Check direct alias dictionary first
    if cat in CANONICAL_ALIASES:
        return CANONICAL_ALIASES[cat]

    # Handle compound suffixes (e.g. 'italian_restaurant' -> 'restaurant')
    for suffix in COMPOUND_SUFFIXES:
        if cat.endswith(suffix):
            base = suffix.lstrip("_")
            # If the base itself is an industry (like 'restaurant', 'hotel', 'clinic'), map to it
            if base in PRIORITY_INDUSTRIES or base in ADJACENT_BUSINESSES:
                return base

    return cat


def is_business_category(category: Optional[str]) -> bool:
    """
    True if the category represents a commercial business worth keeping.
    False if it's geographic/civic noise or too vague to act on.
    """
    if not category:
        return False
    cat = category.strip().lower()

    # Hard-block noise immediately
    if cat in GEOGRAPHIC_NOISE:
        return False

    # Check raw category first
    if cat in PRIORITY_INDUSTRIES or cat in ADJACENT_BUSINESSES:
        return True

    # Check normalized/canonical category
    norm = normalize_category(cat)
    if norm in PRIORITY_INDUSTRIES or norm in ADJACENT_BUSINESSES:
        return True

    # Substring root check
    if any(kw in cat for kw in BUSINESS_KEYWORDS):
        return True

    return False


def industry_priority(category: Optional[str]) -> str:
    """
    Returns 'high', 'medium', or 'low' priority for a category.
    Correctly resolves canonical forms so that Google Places subtypes
    (e.g. 'clothing_store', 'lebanese_restaurant') receive 'high' priority.
    """
    if not category:
        return "low"
    cat = category.strip().lower()

    # Direct match on raw category
    if cat in PRIORITY_INDUSTRIES:
        return "high"
    if cat in ADJACENT_BUSINESSES:
        return "medium"

    # Match on normalized canonical category
    norm = normalize_category(cat)
    if norm in PRIORITY_INDUSTRIES:
        return "high"
    if norm in ADJACENT_BUSINESSES:
        return "medium"

    return "low"
```

---

## Not a Bug, but Worth Knowing

1. **Google Places Query vs Type Picking:**
   In `scrapers/google_places.py:247`, `category = _pick_category(types)` chooses the first non-generic type from `places.types`. Google orders `types` most-specific first. When querying `"restaurants in Lebanon"`, Google returns `["lebanese_restaurant", "restaurant", "food", "point_of_interest"]`. Because `_pick_category()` picks index 0, it ALWAYS selects the specific cuisine subtype (`lebanese_restaurant`) rather than `"restaurant"`. Without suffix decomposition, Google Places category extraction will fail for every cuisine query.

2. **OSM Key Hierarchy Fallthrough:**
   In `scrapers/osm.py:68-75`, the category assignment is:
   ```python
   category = (
       tags.get("shop")
       or tags.get("amenity")
       or tags.get("office")
       or tags.get("tourism")
       or tags.get("craft")
       or tags.get("healthcare")
   )
   ```
   If an entity is a gas station with a convenience mart (`shop=convenience`, `amenity=fuel`), `category` becomes `"convenience"`. If a private clinic is tagged `amenity=clinic` and `healthcare=clinic`, `amenity` wins. Both tags are valid, but the extraction flattens the multi-tag OSM object into a single scalar string, losing secondary classification.

3. **Wikidata P31 Output Formats:**
   In `scrapers/wikidata.py:18-20`, `categoryLabel` is fetched from `?item wdt:P31 ?category`. Wikidata P31 returns natural language English phrases such as `"business enterprise"`, `"commercial organization"`, `"privately held company"`, `"joint-stock company"`, or `"chain store"`. Because `"organization"` is listed in `GEOGRAPHIC_NOISE`, any entity tagged as a `"commercial organization"` is instantly dropped by `is_business_category()`.

---

## Recommended Order of Work

1. **Replace `scrapers/whitelist.py` with the Unified Normalization Engine (Immediate):**
   Deploy the corrected sets, `normalize_category()`, `COMPOUND_SUFFIXES`, and updated `industry_priority()`. This immediately restores dropped Google Places and OSM records and fixes the `sales_ready.csv` exclusion bug.
2. **Synchronize `pitch_recommender.py` (High Priority):**
   Update `pitch_recommender.py` to call `normalize_category(record.get("category"))` before evaluating against `_ECOM_FRIENDLY`, `_RTYLR_TARGETS`, and `_LEAD_GEN_VERTICALS`. This ensures businesses receive tailored pitches instead of falling back to `"Discovery call"`.
3. **Add Unit Tests for Whitelist and Priorities (High Priority):**
   Create `tests/test_whitelist.py` asserting that all 70 Google Places types and 70 OSM tag values classify into their expected pass/fail status and priority tiers.
4. **Refine Wikidata P31 Classification (Medium Priority):**
   Remove `"organization"` from `GEOGRAPHIC_NOISE` and add an explicit Wikidata label normalizer so that entities like `"commercial organization"` or `"business enterprise"` map to `"company"` instead of being dropped.
