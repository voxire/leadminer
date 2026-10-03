# 027 — Whitelist Taxonomy Does Not Match Its Sources

## Verdict

`is_business_category()` treats one bare string as if Google Places types and OSM tag values shared a taxonomy. They do not: Google types like `doctor` and `hair_care` and valid OSM values like `healthcare=doctor`, `shop=car_parts`, and `office=travel_agent` are silently dropped, while an unanchored substring fallback accepts unrelated values. Since `main.py:117` discards failures without a source-aware mapping, these are silent, unrecoverable lead losses and false inclusions.

## Findings

### S1 — Valid business categories are dropped because source vocabularies are conflated

- **Where:** `scrapers/whitelist.py:17-82,105-130`; `scrapers/google_places.py:245-247,281-296`; `scrapers/osm.py:68-75`; `main.py:117-121`
- **Breaks:** Google returns standardized type tokens; OSM supplies the raw value of the first present tag among `shop`, `amenity`, `office`, `tourism`, `craft`, and `healthcare`. The whitelist compares both to the same exact-string sets. It has no source or tag-key context, and an unlisted value is simply dropped before dedup/enrichment/CSV output.
- **Trigger:** Google `doctor` is not the listed `doctors`; `hair_care` is not `beauty` or `hairdresser`; `car_dealer` is not `car_dealership`; `lodging` is not `hotel`; and `veterinary_care` is not `veterinary`. Each fails unless another type happens to be selected. OSM values with the same problem include `healthcare=doctor`, `shop=car_parts`, `shop=locksmith`, `craft=photographer`, and `office=travel_agent` (the list has `travel_agency`). These are real medical practices, auto-parts/locksmith shops, photography businesses, and travel agents—not geographic noise. OSM's `healthcare=doctor` is a documented value; Google documents `doctor` as a Place type. (OSM [`healthcare` key](https://wiki.openstreetmap.org/wiki/Key:healthcare); Google [Place Types](https://developers.google.com/maps/documentation/places/web-service/place-types).)
- **Concrete cross-type trigger:** Google's official Place Types page gives a `Studios Inn` example with types `lodging` and `real_estate_agency` (among generic types). `_pick_category()` chooses the first non-generic type, `lodging`; the whitelist rejects it even though `real_estate_agency` would pass. This is not hypothetical type coverage: selection order directly decides whether a real property business survives.
- **Fix:** Preserve source plus OSM tag key through filtering (or map each source vocabulary to canonical categories first); for Google, test all returned types / primary type against an explicit mapping rather than filtering only `_pick_category()`'s first type.

#### Google type intersection (tested against the documented, common Places business-type set)

These **pass** `is_business_category()` as-is (exact allow-list or substring fallback):

`accounting`, `atm`, `bakery`, `bank`, `bar`, `beauty_salon`, `bicycle_store`, `book_store`, `cafe`, `car_rental`, `car_repair`, `car_wash`, `clothing_store`, `convenience_store`, `dentist`, `drugstore`, `electronics_store`, `florist`, `furniture_store`, `gym`, `hardware_store`, `home_goods_store`, `hospital`, `insurance_agency`, `jewelry_store`, `laundry`, `lawyer`, `liquor_store`, `local_government_office`, `moving_company`, `pet_store`, `pharmacy`, `physiotherapist`, `post_office`, `real_estate_agency`, `restaurant`, `school`, `shoe_store`, `shopping_mall`, `spa`, `store`, `supermarket`, `travel_agency`, `university`.

Examples from that same set that **fail** include:

`car_dealer`, `doctor`, `gas_station`, `hair_care`, `lodging`, `locksmith`, `meal_delivery`, `meal_takeaway`, `movie_theater`, `painter`, `plumber`, `primary_school`, `veterinary_care`, `zoo`.

This is an executable classification of the listed tokens against the current predicate, not a promise that Google's evolving Table A/B is exhaustive. Note also the accidental pass-throughs: `local_government_office` and `moving_company` pass on substrings, not deliberate allow-list entries. `shopping_mall` passes because `shop` occurs inside `shopping`.

#### OSM value intersection (common values from the keys this scraper actually queries)

The predicate accepts common OSM values including:

- `shop=*`: `supermarket`, `convenience`, `bakery`, `butcher`, `deli`, `alcohol`, `tobacco`, `newsagent`, `kiosk`, `department_store`, `variety_store`, `hairdresser`, `beauty`, `massage`, `tattoo`, `laundry`, `dry_cleaning`, `tailor`, `car`, `car_repair`, `furniture`, `florist`, `garden_centre`, `hardware`, `paint`, `books`, `gift`, `toys`, `pet`, `photo`, `electronics`, `mobile_phone`.
- `amenity=*`: `restaurant`, `cafe`, `fast_food`, `bar`, `pub`, `clinic`, `doctors`, `dentist`, `veterinary`, `pharmacy`, `hospital`, `school`, `university`, `bank`, `atm`, `post_office`, `marketplace`.
- `office=*`: `lawyer`, `accountant`, `estate_agent`, `architect`, `insurance`, plus arbitrary values containing `company` (for example `company`).
- `tourism=*`: `hotel`, `guest_house`, `hostel`, `motel`.
- `healthcare=*`: `clinic`, `dentist`, `physiotherapist`, `optometrist`.

That is a practical intersection, not a closed OSM vocabulary: the substring fallback makes the accepted set open-ended. These are accepted examples; many other source-valid values are absent from the allow-list and rejected. OSM's own docs confirm the different conventions—for example `amenity=doctors` versus `healthcare=doctor`, and singular office values such as `lawyer` and `accountant` ([healthcare](https://wiki.openstreetmap.org/wiki/Key:healthcare), [office](https://wiki.openstreetmap.org/wiki/Key:office)).

### S2 — The “soft-allow” is a substring rule, not a business-category rule

- **Where:** `scrapers/whitelist.py:122-128`
- **Breaks:** Any occurrence of `shop`, `store`, `agency`, `firm`, `company`, `service`, `studio`, `office`, `salon`, `centre`, `center`, or `boutique` anywhere in the category returns `True`. It is not anchored to a category boundary and does not verify the source, tag key, or that the whole type describes a commercial business.
- **Trigger:** `shopping_mall` is accepted via embedded `shop`; `local_government_office` via `office`; `moving_company` via `company`; `government_office` via `office`; and a generic `workshop` value via embedded `shop`. The first three are in Google's documented type vocabulary; a generic workshop is a plausible raw OSM value. These pass even though none is an exact intended category token (and a local government office is not a private sales lead). Conversely, similarly legitimate terms without one of those fragments—`doctor`, `car_dealer`, `hair_care`—fail.
- **Fix:** Remove substring matching; explicitly map known Google types and OSM `(key,value)` pairs, with unknown categories quarantined/logged rather than implicitly admitted or silently discarded.

### S2 — OSM key precedence can hide the business category before the whitelist sees it

- **Where:** `scrapers/osm.py:68-75`; `scrapers/whitelist.py:105-120`
- **Breaks:** OSM emits only the first truthy value in `shop → amenity → office → tourism → craft → healthcare`, then the whitelist sees that value without its key or the other tags. A broad or irrelevant higher-precedence tag can therefore mask a valid category on the same feature.
- **Trigger:** An OSM feature tagged `shop=yes` and `amenity=restaurant` becomes category `yes`; the whitelist explicitly hard-blocks `yes`, so the named restaurant is lost. Similarly, a feature with a non-business `shop` value and valid `healthcare=doctor` never reaches the healthcare classification.
- **Fix:** Keep the `(key,value)` pair(s), reject only explicit noise after evaluating all business-bearing tags, and derive the canonical category from the strongest mapped business tag.

## Not a bug, but worth knowing

- The hard-block list runs before both allow-lists and soft-allow (`whitelist.py:114-127`), so exact blocked values such as `parking` and `place` do not get revived by substring matching.
- Google `_pick_category()` intentionally skips generic address/establishment tokens (`google_places.py:283-296`), but selecting just one non-generic type remains a lossy policy: whether a record passes currently depends on type ordering.

## Recommended order of work

1. Introduce source-aware canonicalization (Google type → canonical category; OSM `(key,value)` → canonical category), and filter after mapping.
2. Inspect all Google `types` or `primaryType` plus alternatives rather than deciding from the first selected type; add explicit aliases such as `doctor`, `hair_care`, `car_dealer`, `lodging`, and `veterinary_care` where in scope.
3. Remove unanchored keyword acceptance; add a review/quarantine path for unknown values and metrics for source, raw category, and drop reason.
4. Retain OSM tag key and evaluate multiple candidate tags so a `shop=yes` / valid-amenity combination does not erase the actual business type.
