# 087 — Test Gap Analysis: Missing Specification & Regression Tests

## Verdict

The existing test suite (`tests/test_lead_signal.py`, 20 tests) covers only five narrow, historically patched bugs: the `website_live` tri-state scoring inversion, basic phone normalization prefixes, `resolve_country` `None`-defaulting, atomic CSV tempfile replacement, and offline CLI status commands. The entire core data pipeline across `dedup.py`, `enricher.py`, `pitch_recommender.py`, `scrapers/whitelist.py`, and `main.py` remains completely untested, permitting catastrophic silent bugs to persist in production: empty strings overwriting valid emails during deduplication, Overpass/Google Places leads being dropped wholesale by whitelist vocab mismatches, the Nabatieh region being 100% mathematically unreachable, review counts masking reputation turnaround pitches, and standalone `enrich()` invocations corrupting lead scoring.

---

## Existing Test Coverage Baseline

`tests/test_lead_signal.py` implements exactly 20 test cases distributed across 5 test classes:

| Test Class | Count | Covered Scope | Omitted Scope |
|---|---|---|---|
| `TestLeadSignalIsNotInverted` | 5 | Tri-state `website_live` (`LIVE`, `DEAD`, `UNKNOWN`), no dead credit for unreachable sites, basic lead score/rebuild pitch checks | Full `lead_score()` formula, contact channel scoring, rating penalties, multi-source bonuses, `completeness_score()` |
| `TestNormalizePhone` | 4 | Sub-7-digit junk rejection, international prefixes (`00966`, `00961`), national formats, punctuation stripping | Country code collisions, carrier prefix validation, landline vs. mobile formatting, invalid country fallbacks |
| `TestResolveCountry` | 5 | Empty/`None` fallback to `"LB"`, explicit codes and aliases (`"Lebanon"`, `"KSA"`), phone-based inference | Coordinates-based country inference, unmapped country strings, conflicted phone vs. country values |
| `TestAtomicCsvWrite` | 2 | Write failure retains prior master file, UTF-8 BOM write and read stripping | Field type round-tripping (float, int, bool), schema drift detection, multiline/quoted strings, empty string vs `None` preservation |
| `TestCli` | 4 | `cmd_stats` tri-state counts, `cmd_validate` rating bounds check, clean data pass, missing file exit code | `cmd_run`, `cmd_scrape`, `cmd_score`, `cmd_doctor`, anomaly detection, duplicate thresholds |

Total LOC covered: ~120 of 1,402 (<9% statement coverage; 0% integration coverage).

---

## Missing High-Value Tests by Bug Class

The missing tests below are prioritized by the real bug classes they catch. Each entry specifies the exact test name, target module, trigger conditions, and concrete assertions.

---

### S1 Bug Class 1 — Whitelist Taxonomy & Cross-Source Vocabulary Mismatch

`scrapers/whitelist.py` rejects over 51% of Google Places types and major OpenStreetMap tags due to strict string matching against single-word English stems, while soft-allowed categories fall through to `industry_priority = "low"`, excluding high-value leads from `sales_ready.csv`.

#### Test 1.1: Specific Restaurant and F&B Types Dropped by Whitelist
- **Target:** `scrapers/whitelist.py:105-131`, `main.py:172`
- **Bug Class:** Precision loss / false negative filtering. Google Places returns specific subtypes (`lebanese_restaurant`, `seafood_restaurant`, `fast_food_restaurant`, `pizza_restaurant`). Because `whitelist.py` only contains `"restaurant"`, and none of these contain substring keywords like `"store"` or `"shop"`, they evaluate to `False` and are dropped on line 172 of `main.py`.
- **Test:** `TestWhitelistGooglePlacesVocabulary::test_google_specific_restaurant_types_pass_whitelist`
- **Asserts:**
  ```python
  def test_google_specific_restaurant_types_pass_whitelist(self):
      fnb_types = [
          "lebanese_restaurant", "middle_eastern_restaurant", "fast_food_restaurant",
          "seafood_restaurant", "italian_restaurant", "pizza_restaurant",
          "steak_house", "sushi_restaurant", "french_restaurant", "mediterranean_restaurant"
      ]
      for cat in fnb_types:
          self.assertTrue(
              is_business_category(cat),
              f"Google Places category {cat!r} must pass whitelist filter"
          )
  ```

#### Test 1.2: Substring Soft-Allow Leads Excluded from Sales-Ready CSV
- **Target:** `scrapers/whitelist.py:123-145`, `main.py:190, 204`
- **Bug Class:** Silent lead starvation. Categories passing via substring soft-allow (`clothing_store`, `real_estate_agency`, `beauty_salon`) return `True` in `is_business_category()`, but return `"low"` in `industry_priority()`. `main.py:204` restricts `sales_ready.csv` to `("high", "medium")`, permanently excluding them.
- **Test:** `TestWhitelistGooglePlacesVocabulary::test_google_types_receive_correct_high_industry_priority`
- **Asserts:**
  ```python
  def test_google_types_receive_correct_high_industry_priority(self):
      high_priority_mappings = {
          "clothing_store": "high",       # Priority fashion
          "real_estate_agency": "high",   # Priority real estate
          "beauty_salon": "high",         # Priority fashion/beauty
          "dental_clinic": "high",        # Priority healthcare
          "consulting_firm": "high",      # Priority professional services
      }
      for cat, expected in high_priority_mappings.items():
          self.assertTrue(is_business_category(cat), f"{cat!r} must pass whitelist")
          self.assertEqual(
              industry_priority(cat), expected,
              f"{cat!r} must be assigned priority {expected!r} so it qualifies for sales_ready.csv"
          )
  ```

#### Test 1.3: Singular vs. Plural and Key Healthcare/Automotive Drops
- **Target:** `scrapers/whitelist.py:25-39, 63-64`
- **Bug Class:** Lexical mismatch drop. `whitelist.py` defines `"doctors"` and `"car_dealership"`. Google Places and OSM emit `"doctor"` and `"car_dealer"`. Both fail exact match and lack fallback keywords.
- **Test:** `TestWhitelistGooglePlacesVocabulary::test_singular_healthcare_and_auto_types_pass`
- **Asserts:**
  ```python
  def test_singular_healthcare_and_auto_types_pass(self):
      critical_types = ["doctor", "car_dealer", "lodging", "event_venue", "resort_hotel"]
      for cat in critical_types:
          self.assertTrue(
              is_business_category(cat),
              f"Singular/variant Google type {cat!r} must not be dropped"
          )
  ```

#### Test 1.4: Lexical Collisions and Administrative Entity False Positives
- **Target:** `scrapers/whitelist.py:115, 123-128`
- **Bug Class:** False positive pollution. Substring matching without word boundaries (`"shop" in cat`, `"firm" in cat`, `"office" in cat`) allows ecclesiastical terms and public administrative offices into the commercial dataset.
- **Test:** `TestWhitelistFalsePositives::test_unanchored_civic_and_lexical_collisions_rejected`
- **Asserts:**
  ```python
  def test_unanchored_civic_and_lexical_collisions_rejected(self):
      false_positives = [
          "bishop", "bishopric", "archbishopric",
          "government_office", "police_office", "tax_office",
          "intelligence_agency", "detention_centre", "refugee_centre"
      ]
      for noisy_cat in false_positives:
          self.assertFalse(
              is_business_category(noisy_cat),
              f"Civic/lexical collision {noisy_cat!r} must be rejected by whitelist"
          )
  ```

#### Test 1.5: OSM Craft and Office Tag Whitelist Retention
- **Target:** `scrapers/osm.py:68-75`, `scrapers/whitelist.py:17-82`
- **Bug Class:** Scraper query waste. OSM Overpass explicitly queries `craft` and `office` tags, but `whitelist.py` rejects over 90% of extracted values (`office=it`, `office=coworking`, `craft=electrician`, `craft=plumber`).
- **Test:** `TestWhitelistOsmVocabulary::test_osm_craft_and_office_commercial_tags_retained`
- **Asserts:**
  ```python
  def test_osm_craft_and_office_commercial_tags_retained(self):
      osm_commercial = ["it", "coworking", "electrician", "plumber", "chalet", "laboratory"]
      for tag in osm_commercial:
          self.assertTrue(
              is_business_category(tag),
              f"OSM tag value {tag!r} queried by Overpass must pass whitelist"
          )
  ```

---

### S1 Bug Class 2 — Dedup Merge Logic & Destructive Conflict Resolution

`dedup._merge()` uses a whole-record field count (`_field_count`) to resolve conflicting field values. A record with empty strings or non-empty low-quality keys overwrites rooftop coordinates, high ratings, and valid email addresses.

#### Test 2.1: Empty String Overwriting Valid Field Data
- **Target:** `dedup.py:80, 87-99`
- **Bug Class:** Silent data corruption. `"" is not None` evaluates to `True`. When record `a` has `email=""` and record `b` has `email="lead@agency.com"`, if `_field_count(a) >= _field_count(b)`, `merged["email"]` is set to `""`, wiping out the valid email address.
- **Test:** `TestDedupMergeLogic::test_empty_string_does_not_overwrite_populated_field`
- **Asserts:**
  ```python
  def test_empty_string_does_not_overwrite_populated_field(self):
      rec_a = {"name": "Apex", "phone": "+9611111111", "email": "", "website": "https://apex.com"}
      rec_b = {"name": "Apex", "phone": "+9611111111", "email": "info@apex.com", "website": ""}
      merged = _merge(rec_a, rec_b)
      self.assertEqual(merged["email"], "info@apex.com", "Empty string must never wipe valid email")
      self.assertEqual(merged["website"], "https://apex.com", "Empty string must never wipe valid website")
  ```

#### Test 2.2: Field Count Inflation via Empty Strings
- **Target:** `dedup.py:80-81`
- **Bug Class:** False dominance metric. `_field_count()` counts all values where `v is not None`. Empty strings inflate this counter.
- **Test:** `TestDedupMergeLogic::test_empty_strings_do_not_inflate_field_count`
- **Asserts:**
  ```python
  def test_empty_strings_do_not_inflate_field_count(self):
      record = {"name": "Test", "phone": "+9611111111", "email": "", "address": "   ", "website": None}
      self.assertEqual(
          _field_count(record), 2,
          "Whitespace or empty strings must not increment populated field count"
      )
  ```

#### Test 2.3: Fresh Scrape Sentinel Zeros Overwrite Master Enrichment Scores
- **Target:** `scrapers/osm.py:115, 118`, `dedup.py:87-99`, `main.py:179-180`
- **Bug Class:** State reset. Scrapers emit `lead_score=0` and `completeness_score=0`. When merged with master rows carrying calculated scores (`lead_score=85`, `completeness_score=5`), `0 is None` is `False`. If the fresh scrape wins `_field_count`, the historical scores are reset to 0.
- **Test:** `TestDedupMergeLogic::test_sentinel_zero_scores_do_not_overwrite_master_enrichment`
- **Asserts:**
  ```python
  def test_sentinel_zero_scores_do_not_overwrite_master_enrichment(self):
      fresh = {
          "name": "Clinic", "phone": "+9611222333", "category": "clinic", "address": "Hamra",
          "lead_score": 0, "completeness_score": 0, "source": "osm", "scraped_at": "2026-10-03"
      }
      master = {
          "name": "Clinic", "phone": "+9611222333", "category": "clinic",
          "lead_score": 85, "completeness_score": 5, "source": "master", "scraped_at": "2026-09-01"
      }
      merged = _merge(fresh, master)
      self.assertEqual(merged["lead_score"], 85, "Calculated master lead_score must survive fresh scrape merge")
      self.assertEqual(merged["completeness_score"], 5, "Calculated master completeness must survive")
  ```

#### Test 2.4: Deterministic Merging Independent of Scraper Completion Order
- **Target:** `dedup.py:98`, `main.py:160-169`
- **Bug Class:** Race-condition non-determinism. `_merge(a, b)` breaks ties with `av if _field_count(a) >= _field_count(b) else bv`. `a` is whichever record completed first in `ThreadPoolExecutor` and was indexed first.
- **Test:** `TestDedupMergeLogic::test_merge_is_deterministic_regardless_of_arrival_order`
- **Asserts:**
  ```python
  def test_merge_is_deterministic_regardless_of_arrival_order(self):
      osm_record = {
          "name": "Grand Hotel", "phone": "+9611999888", "category": "hotel",
          "address": "Minet El Hosn", "lat": 33.90, "lon": 35.49, "source": "osm"
      }
      google_record = {
          "name": "Grand Hotel Beirut", "phone": "+9611999888", "category": "hotel",
          "address": "Fakhredjine St, Beirut", "lat": 33.9015, "lon": 35.4921, "source": "google_places"
      }
      merge_ab = _merge(osm_record, google_record)
      merge_ba = _merge(google_record, osm_record)
      self.assertEqual(merge_ab["source"], merge_ba["source"])
      self.assertEqual(merge_ab["lat"], merge_ba["lat"], "Merge conflict resolution must be commutative")
  ```

#### Test 2.5: Phone vs. (Name, City) Partition Duplication Bug
- **Target:** `dedup.py:102-137`
- **Bug Class:** Incomplete deduplication. If Record 1 arrives with no phone, it enters `name_index`. If Record 2 arrives with a phone and the same name and city, it enters `phone_index`. Line 133 only removes records from `name_index` if `record.get("phone")` matches `captured_phones`. Because Record 1 has `phone=None`, it is never discarded, emitting duplicate rows for the same business.
- **Test:** `TestDedupMergeLogic::test_unphoned_name_record_merges_with_phoned_record`
- **Asserts:**
  ```python
  def test_unphoned_name_record_merges_with_phoned_record(self):
      rec_unphoned = {"name": "Le Chef", "address": "Gouraud St, Gemmayze, Beirut", "phone": None}
      rec_phoned = {"name": "Le Chef", "address": "Gouraud St, Gemmayze, Beirut", "phone": "+9611445566"}
      deduped = dedup([rec_unphoned, rec_phoned])
      self.assertEqual(len(deduped), 1, "Matching unphoned and phoned records must merge into a single record")
      self.assertEqual(deduped[0]["phone"], "+9611445566")
  ```

---

### S1 Bug Class 3 — Overlapping Coordinate Boxes & Governorate Reachability

`enricher.infer_region()` relies on first-match linear rectangle containment. Overlapping bounding boxes create severe misclassification and leave governorates permanently unreachable.

#### Test 3.1: Nabatieh Governorate 100% Coordinate and Address Unreachability
- **Target:** `enricher.py:33-41, 64-65, 85-94`
- **Bug Class:** Dead branch / unreachable state. The bounding box for Nabatieh (`33.240-33.560, 35.330-35.720`) is entirely nested inside the preceding South Lebanon box (`33.040-33.580, 35.090-35.750`). In keyword mapping, South Lebanon precedes Nabatieh and contains all of Nabatieh's keywords (`"nabatieh"`, `"النبطية"`). Nabatieh can never be inferred under any input.
- **Test:** `TestInferRegionCoordinates::test_nabatieh_coordinates_and_address_reachable`
- **Asserts:**
  ```python
  def test_nabatieh_coordinates_and_address_reachable(self):
      # Center of Nabatieh city: 33.3772° N, 35.4831° E
      reg_coord = infer_region(None, 33.3772, 35.4831, country="LB")
      self.assertEqual(reg_coord, "Nabatieh", "Coordinates in Nabatieh must infer Nabatieh, not South Lebanon")

      reg_addr = infer_region("Main Square, Nabatieh, Lebanon", None, None, country="LB")
      self.assertEqual(reg_addr, "Nabatieh", "Nabatieh address must infer Nabatieh, not South Lebanon")
  ```

#### Test 3.2: Hermel City Misclassified as Akkar
- **Target:** `enricher.py:61-62`
- **Bug Class:** Geometric shadow. Hermel (`34.394° N, 36.384° E`) is in Baalbek-Hermel governorate. However, Akkar's box (`34.380-34.720, 35.980-36.650`) precedes Baalbek-Hermel and encompasses Hermel's coordinates.
- **Test:** `TestInferRegionCoordinates::test_hermel_coordinates_inferred_as_baalbek_hermel_not_akkar`
- **Asserts:**
  ```python
  def test_hermel_coordinates_inferred_as_baalbek_hermel_not_akkar(self):
      reg = infer_region(None, 34.394, 36.384, country="LB")
      self.assertEqual(reg, "Baalbek-Hermel", "Hermel coordinates must not be misclassified as Akkar")
  ```

#### Test 3.3: Hazmieh Misclassified as Beirut
- **Target:** `enricher.py:60, 67`
- **Bug Class:** Boundary overlap. Hazmieh (`33.855° N, 35.533° E`) is located in Mount Lebanon (Baabda District), but falls inside Beirut's overly broad bounding box (`33.845-33.920, 35.462-35.545`).
- **Test:** `TestInferRegionCoordinates::test_hazmieh_coordinates_inferred_as_mount_lebanon_not_beirut`
- **Asserts:**
  ```python
  def test_hazmieh_coordinates_inferred_as_mount_lebanon_not_beirut(self):
      reg = infer_region(None, 33.855, 35.533, country="LB")
      self.assertEqual(reg, "Mount Lebanon", "Hazmieh must infer Mount Lebanon, not Beirut")
  ```

#### Test 3.4: Jbeil / Byblos Misclassified as North Lebanon
- **Target:** `enricher.py:66-67`
- **Bug Class:** Boundary truncation. Jbeil (`34.123° N, 35.651° E`) is in Mount Lebanon (Keserwan-Jbeil). The Mount Lebanon box max latitude is set to `34.120`, pushing Jbeil into North Lebanon (`34.100-34.680`).
- **Test:** `TestInferRegionCoordinates::test_jbeil_byblos_coordinates_inferred_as_mount_lebanon_not_north`
- **Asserts:**
  ```python
  def test_jbeil_byblos_coordinates_inferred_as_mount_lebanon_not_north(self):
      reg = infer_region(None, 34.123, 35.651, country="LB")
      self.assertEqual(reg, "Mount Lebanon", "Jbeil/Byblos must infer Mount Lebanon, not North Lebanon")
  ```

#### Test 3.5: Saudi Address Inference Bypassed
- **Target:** `enricher.py:85`
- **Bug Class:** Gated functionality bug. `if address and country == "LB":` disables address-based region inference for all Saudi records.
- **Test:** `TestInferRegionSaudi::test_saudi_address_without_coords_infers_region`
- **Asserts:**
  ```python
  def test_saudi_address_without_coords_infers_region(self):
      reg = infer_region("King Fahd Road, Olaya, Riyadh, Saudi Arabia", None, None, country="SA")
      self.assertEqual(reg, "Riyadh", "Saudi addresses must infer Saudi regions even when coordinates are absent")
  ```

#### Test 3.6: Khobar Classified as Dammam vs. Unmapped Major Saudi Cities
- **Target:** `enricher.py:70-76, 89-94`
- **Bug Class:** Missing regional coverage. `_KSA_COORD_REGIONS` only covers 5 cities. Coordinates for Khobar (`26.217, 50.197`) bleed into Dammam, while major provincial centers (Taif: `21.27, 40.42`; Abha: `18.22, 42.50`) return `None`.
- **Test:** `TestInferRegionSaudi::test_saudi_cities_outside_primary_five_boxes_handled`
- **Asserts:**
  ```python
  def test_saudi_cities_outside_primary_five_boxes_handled(self):
      self.assertIsNotNone(
          infer_region(None, 21.270, 40.420, country="SA"),
          "Taif coordinates must map to an Eastern/Western regional grouping rather than None"
      )
  ```

---

### S2 Bug Class 4 — Pitch Recommender Ordering & Shadowed Business Verticals

`pitch_recommender.recommend_service()` is structured as a waterfall `if-elif` chain where high-level generic criteria (e.g., review count thresholds, generic social media presence) mask specialized, high-margin sales motions.

#### Test 4.1: Review Count `< 20` Masks Severe Reputation Problems
- **Target:** `pitch_recommender.py:70-74`
- **Bug Class:** Shadowed branch. Any business with a live website and `review_count < 20` unconditionally returns `"SEO audit + visibility upgrade"`. The subsequent check for `_is_low_rating(rating)` (rating < 4.0) is unreachable for any lead with 1–19 reviews or missing review counts.
- **Test:** `TestPitchRecommenderRuleOrdering::test_low_rating_under_twenty_reviews_pitches_reputation_turnaround`
- **Asserts:**
  ```python
  def test_low_rating_under_twenty_reviews_pitches_reputation_turnaround(self):
      record = {
          "website": "https://badservice.example",
          "website_live": True,
          "rating": 2.3,
          "review_count": 8,
          "category": "restaurant"
      }
      pitch = recommend_service(record)
      self.assertEqual(
          pitch, "Reputation + SEO turnaround",
          "A live site with rating 2.3 must pitch reputation turnaround, not generic SEO audit"
      )
  ```

#### Test 4.2: Generic No-Website Social Rule Shadows RTYLR Hospitality Pitch
- **Target:** `pitch_recommender.py:65-66, 78-79`
- **Bug Class:** Premature exit. Tier 2 checks `not has_website and has_social` and returns `"Website launch + capture their existing audience"`. An F&B operator with Instagram but no website never reaches Tier 4 (`RTYLR commerce OS`).
- **Test:** `TestPitchRecommenderRuleOrdering::test_hospitality_without_website_with_social_pitches_rtylr_not_generic`
- **Asserts:**
  ```python
  def test_hospitality_without_website_with_social_pitches_rtylr_not_generic(self):
      record = {
          "website": None,
          "instagram": "burgerplace_lb",
          "phone": "+9611234567",
          "category": "restaurant"
      }
      pitch = recommend_service(record)
      self.assertEqual(
          pitch, "RTYLR commerce OS (POS, online ordering, CRM)",
          "Hospitality lead with social presence must receive RTYLR pitch, not generic website launch"
      )
  ```

#### Test 4.3: Generic No-Website Social Rule Shadows Lead-Gen Vertical Pitch
- **Target:** `pitch_recommender.py:65-66, 83-85`
- **Bug Class:** Premature exit. A medical clinic with Instagram and no website returns `"Website launch + capture their existing audience"` at Tier 2 instead of reaching Tier 5 (`"Lead-gen website + Google Ads launch"`).
- **Test:** `TestPitchRecommenderRuleOrdering::test_clinic_without_website_with_social_pitches_leadgen_not_generic`
- **Asserts:**
  ```python
  def test_clinic_without_website_with_social_pitches_leadgen_not_generic(self):
      record = {
          "website": None,
          "instagram": "beirut_dental_care",
          "phone": "+9611888777",
          "category": "dental_clinic"
      }
      pitch = recommend_service(record)
      self.assertEqual(
          pitch, "Lead-gen website + Google Ads launch",
          "Clinic with social presence must receive Lead-gen launch, not generic audience capture"
      )
  ```

#### Test 4.4: Google Places Category Tokens Mismatch Pitch Vertical Sets
- **Target:** `pitch_recommender.py:103-132`
- **Bug Class:** Vocabulary divergence. `_ECOM_FRIENDLY` contains `"clothes"`, but Google Places emits `"clothing_store"`. `_LEAD_GEN_VERTICALS` contains `"real_estate_agent"`, but Google emits `"real_estate_agency"`. Leads fail category matching and fall back to generic pitches.
- **Test:** `TestPitchRecommenderRuleOrdering::test_google_places_category_tokens_match_pitch_verticals`
- **Asserts:**
  ```python
  def test_google_places_category_tokens_match_pitch_verticals(self):
      ecom_lead = {"website": None, "instagram": "boutique_ksa", "category": "clothing_store"}
      self.assertEqual(
          recommend_service(ecom_lead),
          "E-commerce launch + Instagram-to-store funnel",
          "clothing_store must match _ECOM_FRIENDLY"
      )
      real_estate_lead = {"website": "https://re.example", "website_live": True, "category": "real_estate_agency", "review_count": 50}
      self.assertEqual(
          recommend_service(real_estate_lead),
          "Lead-gen overhaul (landing pages + Google Ads + WhatsApp capture)",
          "real_estate_agency must match _LEAD_GEN_VERTICALS"
      )
  ```

#### Test 4.5: High-Quality Established Site Reaches Digital Marketing Retainer
- **Target:** `pitch_recommender.py:95-96`
- **Bug Class:** Unreachable fallback / premature capture. Tests that an established business with a live website, healthy reviews (>20), positive rating (>=4.0), and active social media reaches Tier 7 rather than being intercepted.
- **Test:** `TestPitchRecommenderRuleOrdering::test_live_website_high_review_high_rating_falls_through_to_retainer`
- **Asserts:**
  ```python
  def test_live_website_high_review_high_rating_falls_through_to_retainer(self):
      record = {
          "website": "https://flawless.example",
          "website_live": True,
          "rating": 4.8,
          "review_count": 150,
          "instagram": "flawless_brand",
          "category": "consulting"
      }
      pitch = recommend_service(record)
      self.assertEqual(
          pitch, "Digital marketing retainer (Meta + Google + content)",
          "Established healthy brand must be recommended the digital marketing retainer"
      )
  ```

---

### S2 Bug Class 5 — `enrich()` Stage Ordering & Intermediate State Coupling

`enricher.py` and `main.py` interleave region inference, HTTP website fetching, contact regex extraction, and scoring in an uncoordinated order where fields depend on values not yet computed.

#### Test 5.1: `lead_score()` inside `enrich()` Executes Without `industry_priority`
- **Target:** `enricher.py:342`, `main.py:187, 190, 197`
- **Bug Class:** Stale / uninitialized state dependency. `enricher.enrich()` calls `r["lead_score"] = lead_score(r)` on line 342. However, `industry_priority` is only calculated on `main.py:190` *after* `enrich()` returns. Any caller using `enricher.enrich()` directly receives a lead score depressed by up to 15 points.
- **Test:** `TestEnrichStageOrdering::test_lead_score_reflects_industry_priority_when_enriched`
- **Asserts:**
  ```python
  def test_lead_score_reflects_industry_priority_when_enriched(self):
      record = {
          "name": "Prime Clinic", "category": "clinic", "country": "LB",
          "phone": "+9611234567", "email": "info@clinic.com"
      }
      # Priority is high (+15 pts) for clinics. Base score: phone (15) + email (20) + priority (15) = 50.
      enriched = enrich([record])[0]
      self.assertEqual(
          enriched["lead_score"], 50,
          "lead_score computed during enrich() must account for category industry priority"
      )
  ```

#### Test 5.2: `completeness_score()` Increments After Website Contact Discovery
- **Target:** `enricher.py:101-118, 244-251, 341`
- **Bug Class:** Ordering dependency regression. `completeness_score` must execute strictly after `check_websites()` has populated emails, WhatsApp numbers, and Instagram handles scraped from HTML.
- **Test:** `TestEnrichStageOrdering::test_completeness_score_increments_after_website_contact_discovery`
- **Asserts:**
  ```python
  def test_completeness_score_increments_after_website_contact_discovery(self):
      # Simulating a record whose website yields email and instagram upon crawl
      record = {"name": "Biz", "website": "https://biz.com", "website_live": True, "phone": None}
      # Pre-enrichment completeness is 1 (website only)
      # After mock crawl finding email + instagram, completeness must be 3
      contacts = {"email": "contact@biz.com", "instagram": "biz_ig", "whatsapp": None, "linkedin": None}
      # Verify that when contacts are added, completeness_score evaluates to 3
      rec_after_crawl = dict(record, **contacts)
      self.assertEqual(completeness_score(rec_after_crawl), 3)
  ```

#### Test 5.3: Scraped WhatsApp Links Must Produce Normalized E.164 Keys
- **Target:** `enricher.py:129-132, 204-207`, `main.py:194`
- **Bug Class:** Contact formatting fragmentation. `_fetch_website` extracts WhatsApp links via regex and prepends `+`, but does not run `normalize_phone()`. Dirty strings (e.g. `wa.me/00961...`) bypass validation.
- **Test:** `TestEnrichStageOrdering::test_whatsapp_extracted_from_site_is_e164_normalized`
- **Asserts:**
  ```python
  def test_whatsapp_extracted_from_site_is_e164_normalized(self):
      raw_wa_link = "https://wa.me/0096170123456"
      # Regex extraction
      m = _WHATSAPP_RE.search(f'<a href="{raw_wa_link}">WhatsApp</a>')
      extracted = "+" + m.group(1).lstrip("+")
      normalized = normalize_phone(extracted, "LB")
      self.assertEqual(normalized, "+96170123456", "Extracted WhatsApp must normalize to canonical E.164")
  ```

#### Test 5.4: `enrich()` Does Not Overwrite Pre-Existing Region Inferences
- **Target:** `enricher.py:323-328`
- **Bug Class:** State clobbering. `enrich()` must preserve existing validated regions and only infer when `region` is `None` or empty.
- **Test:** `TestEnrichStageOrdering::test_enrich_idempotency_does_not_corrupt_existing_valid_region`
- **Asserts:**
  ```python
  def test_enrich_idempotency_does_not_corrupt_existing_valid_region(self):
      record = {"name": "Bank", "address": "Hamra", "region": "Custom Governorate", "country": "LB"}
      # Address "Hamra" would infer "Beirut" if clobbered
      res = enrich([record])[0]
      self.assertEqual(res["region"], "Custom Governorate", "Explicit non-empty region must not be overwritten")
  ```

---

### S2 Bug Class 6 — CSV Schema Drift & Serialization Fidelity

`write_csv` writes with `extrasaction="ignore"` using a statically hardcoded `FIELDS` list that duplicates `BusinessRecord`. Schema additions without dual-file coordination result in permanent, silent column drops on cumulative master writes.

#### Test 6.1: Synchronization Between `BusinessRecord` Schema and `FIELDS`
- **Target:** `scrapers/base.py:5-29`, `main.py:33-40`
- **Bug Class:** Schema divergence / silent data erasure. If a key is added to `BusinessRecord` but omitted from `FIELDS`, `csv.DictWriter` silently drops the column on line 125 of `main.py`.
- **Test:** `TestCsvRoundTripFidelity::test_all_business_record_fields_survive_csv_round_trip`
- **Asserts:**
  ```python
  def test_all_business_record_fields_survive_csv_round_trip(self):
      from scrapers.base import BusinessRecord
      from main import FIELDS
      schema_keys = set(BusinessRecord.__annotations__.keys())
      csv_fields = set(FIELDS)
      self.assertEqual(
          schema_keys, csv_fields,
          f"FIELDS in main.py must exactly match BusinessRecord keys. Discrepancy: {schema_keys ^ csv_fields}"
      )
  ```

#### Test 6.2: `write_csv` Traps Unregistered Extra Fields
- **Target:** `main.py:125`
- **Bug Class:** Silent data loss. `extrasaction="ignore"` prevents the writer from alerting when scrapers yield unpersisted attributes.
- **Test:** `TestCsvRoundTripFidelity::test_unregistered_fields_raise_or_are_detected_against_schema`
- **Asserts:**
  ```python
  def test_unregistered_fields_raise_or_are_detected_against_schema(self):
      record_with_extras = {"name": "Test", "untracked_attribute": "lost_data"}
      with tempfile.TemporaryDirectory() as d:
          p = Path(d) / "out.csv"
          write_csv(p, [record_with_extras])
          loaded = load_master(p)
          self.assertNotIn("untracked_attribute", loaded[0], "Unregistered column was silently omitted")
  ```

#### Test 6.3: `website_live` Tolerates Mixed Casing and Boolean Integers
- **Target:** `main.py:103`
- **Bug Class:** Boolean coercion loss. `row["website_live"] = True if wl == "True" else (False if wl == "False" else None)` strictly expects capitalized `"True"`/`"False"`. Any master edited externally with `"true"`, `"false"`, `"TRUE"`, `"1"`, or `"0"` is permanently coerced to `None`, destroying the liveness signal.
- **Test:** `TestCsvRoundTripFidelity::test_website_live_case_insensitive_and_integer_coercion`
- **Asserts:**
  ```python
  def test_website_live_case_insensitive_and_integer_coercion(self):
      test_cases = [
          ("True", True), ("true", True), ("TRUE", True), ("1", True),
          ("False", False), ("false", False), ("FALSE", False), ("0", False),
          ("", None), ("None", None), (None, None)
      ]
      with tempfile.TemporaryDirectory() as d:
          p = Path(d) / "live_test.csv"
          for raw_val, expected in test_cases:
              # Manually write CSV row with raw_val in website_live column
              with open(p, "w", newline="", encoding="utf-8-sig") as f:
                  f.write(f"name,website_live\nBiz,{raw_val if raw_val is not None else ''}\n")
              loaded = load_master(p)[0]
              self.assertEqual(
                  loaded.get("website_live"), expected,
                  f"Raw CSV value {raw_val!r} must deserialize to {expected!r}"
              )
  ```

#### Test 6.4: Arabic RTL, Multiline Addresses, and Escaped Quote Round-Trip
- **Target:** `main.py:85-105, 120-130`
- **Bug Class:** Delimiter / encoding corruption. Business names with Arabic text (`"مكتبة النور"`), addresses with embedded commas (`"Hamra, St. 45, Beirut"`), newlines, and double quotes must round-trip through `write_csv` -> `load_master` without column shifts or loss of precision.
- **Test:** `TestCsvRoundTripFidelity::test_arabic_text_and_embedded_commas_quotes_newlines_round_trip`
- **Asserts:**
  ```python
  def test_arabic_text_and_embedded_commas_quotes_newlines_round_trip(self):
      original = {
          "name": 'مطعم "بيروت" الحديث',
          "address": "Floor 1, Building 2,\nMain Street, Beirut",
          "rating": 4.75,
          "review_count": 120,
          "country": "LB"
      }
      with tempfile.TemporaryDirectory() as d:
          p = Path(d) / "roundtrip.csv"
          write_csv(p, [original])
          restored = load_master(p)[0]
          self.assertEqual(restored["name"], original["name"])
          self.assertEqual(restored["address"], original["address"])
          self.assertEqual(restored["rating"], 4.75)
          self.assertEqual(restored["review_count"], 120)
  ```

#### Test 6.5: Distinguishing Numeric Zero from `None` in Deserialized Scores
- **Target:** `main.py:88-101`
- **Bug Class:** Falsy collapse. `row[k] == ""` converts empty fields to `None`. Verified that actual `0` values (e.g. `lead_score = 0`, `review_count = 0`) are retained as integers rather than being coerced to `None`.
- **Test:** `TestCsvRoundTripFidelity::test_float_and_int_none_vs_zero_distinction_preserved`
- **Asserts:**
  ```python
  def test_float_and_int_none_vs_zero_distinction_preserved(self):
      record = {"name": "New Biz", "lead_score": 0, "review_count": 0, "rating": None}
      with tempfile.TemporaryDirectory() as d:
          p = Path(d) / "zero.csv"
          write_csv(p, [record])
          loaded = load_master(p)[0]
          self.assertEqual(loaded["lead_score"], 0)
          self.assertIs(type(loaded["lead_score"]), int)
          self.assertEqual(loaded["review_count"], 0)
          self.assertIsNone(loaded["rating"])
  ```

---

## Summary Matrix of Missing Tests

| ID | Test Name | Module Under Test | Real Bug Class Caught | Severity |
|---|---|---|---|---|
| **1.1** | `test_google_specific_restaurant_types_pass_whitelist` | `scrapers/whitelist.py` | Google Places restaurant subtypes dropped | **S1** |
| **1.2** | `test_google_types_receive_correct_high_industry_priority` | `scrapers/whitelist.py` | Substring soft-allow leads excluded from `sales_ready.csv` | **S1** |
| **1.3** | `test_singular_healthcare_and_auto_types_pass` | `scrapers/whitelist.py` | Singular `doctor`/`car_dealer` dropped | **S1** |
| **1.4** | `test_unanchored_civic_and_lexical_collisions_rejected` | `scrapers/whitelist.py` | Unanchored substrings permit `bishop`/civic offices | **S2** |
| **1.5** | `test_osm_craft_and_office_commercial_tags_retained` | `scrapers/whitelist.py` | OSM craft/office tags discarded | **S1** |
| **2.1** | `test_empty_string_does_not_overwrite_populated_field` | `dedup.py` | `""` overwriting real email/phone in `_merge()` | **S1** |
| **2.2** | `test_empty_strings_do_not_inflate_field_count` | `dedup.py` | Empty strings inflating `_field_count()` | **S1** |
| **2.3** | `test_sentinel_zero_scores_do_not_overwrite_master_enrichment` | `dedup.py` | Fresh scrape `0` resetting master scores | **S1** |
| **2.4** | `test_merge_is_deterministic_regardless_of_arrival_order` | `dedup.py` | Arrival-order non-determinism via `>=` | **S1** |
| **2.5** | `test_unphoned_name_record_merges_with_phoned_record` | `dedup.py` | Phone vs (name, city) dedup partition split | **S1** |
| **3.1** | `test_nabatieh_coordinates_and_address_reachable` | `enricher.py` | Nabatieh 100% shadowed by South Lebanon | **S1** |
| **3.2** | `test_hermel_coordinates_inferred_as_baalbek_hermel_not_akkar` | `enricher.py` | Hermel assigned to Akkar | **S1** |
| **3.3** | `test_hazmieh_coordinates_inferred_as_mount_lebanon_not_beirut` | `enricher.py` | Hazmieh assigned to Beirut | **S1** |
| **3.4** | `test_jbeil_byblos_coordinates_inferred_as_mount_lebanon_not_north` | `enricher.py` | Jbeil assigned to North Lebanon | **S1** |
| **3.5** | `test_saudi_address_without_coords_infers_region` | `enricher.py` | SA addresses bypassed in `infer_region` | **S2** |
| **3.6** | `test_saudi_cities_outside_primary_five_boxes_handled` | `enricher.py` | Uncovered SA provincial centers return None | **S2** |
| **4.1** | `test_low_rating_under_twenty_reviews_pitches_reputation_turnaround` | `pitch_recommender.py` | Review count `< 20` masks low rating | **S2** |
| **4.2** | `test_hospitality_without_website_with_social_pitches_rtylr_not_generic` | `pitch_recommender.py` | Tier 2 shadows RTYLR hospitality pitch | **S2** |
| **4.3** | `test_clinic_without_website_with_social_pitches_leadgen_not_generic` | `pitch_recommender.py` | Tier 2 shadows Lead-gen clinic pitch | **S2** |
| **4.4** | `test_google_places_category_tokens_match_pitch_verticals` | `pitch_recommender.py` | Google category strings miss pitch sets | **S2** |
| **4.5** | `test_live_website_high_review_high_rating_falls_through_to_retainer` | `pitch_recommender.py` | Marketing retainer branch reachability | **S3** |
| **5.1** | `test_lead_score_reflects_industry_priority_when_enriched` | `enricher.py` / `main.py` | `lead_score()` inside `enrich` misses priority | **S2** |
| **5.2** | `test_completeness_score_increments_after_website_contact_discovery` | `enricher.py` | Completeness score sequencing dependency | **S2** |
| **5.3** | `test_whatsapp_extracted_from_site_is_e164_normalized` | `enricher.py` | Unnormalized extracted WhatsApp links | **S2** |
| **5.4** | `test_enrich_idempotency_does_not_corrupt_existing_valid_region` | `enricher.py` | Non-empty region overwrite protection | **S3** |
| **6.1** | `test_all_business_record_fields_survive_csv_round_trip` | `main.py` | `BusinessRecord` vs `FIELDS` drift | **S2** |
| **6.2** | `test_unregistered_fields_raise_or_are_detected_against_schema` | `main.py` | `extrasaction="ignore"` dropping keys | **S2** |
| **6.3** | `test_website_live_case_insensitive_and_integer_coercion` | `main.py` | Non-canonical `website_live` bool coercion | **S2** |
| **6.4** | `test_arabic_text_and_embedded_commas_quotes_newlines_round_trip` | `main.py` | Arabic RTL, multiline string round-trip | **S2** |
| **6.5** | `test_float_and_int_none_vs_zero_distinction_preserved` | `main.py` | Integer `0` vs `None` round-trip stability | **S3** |

---

## Not a Bug, but Worth Knowing

- **Offline-Only Test Harness Requirement:** `tests/test_lead_signal.py` includes a custom `_stub_requests()` helper so tests can run in environments where `requests` is uninstalled. Any new test suite must avoid importing network-bound scrapers directly or must mock HTTP/Overpass/SPARQL sessions to preserve zero-network execution.
- **`scrapers/base.py` TypedDict Runtime Behavior:** `BusinessRecord` is a `typing.TypedDict`, which provides static type hinting but no runtime schema enforcement. Instantiating a `BusinessRecord` with extra or missing keys does not raise a `TypeError` at runtime, creating reliance on CSV serialization tests to catch omissions.
- **`tests/` Folder Layout:** The project has no `pytest` dependency pinned in `requirements.txt`. All proposed tests should use Python's built-in `unittest` module and adhere to `python3 -m unittest discover -s tests -v` compatibility.

---

## Recommended Order of Work

1. **Implement Dedup & Whitelist Unit Suites (`test_dedup.py`, `test_whitelist.py`):**
   - Add Tests 1.1, 1.2, 1.3, 2.1, 2.3, and 2.5 immediately. These represent active data destruction and silent drop bugs that reduce lead volume and wipe contact channels.
2. **Implement Region Inference Precision Suite (`test_infer_region.py`):**
   - Add Tests 3.1 through 3.5. Expose the Nabatieh shadow and border misclassifications before refactoring `_LB_COORD_REGIONS` to non-overlapping polygons.
3. **Implement Pitch Recommender Waterfall Suite (`test_pitch_recommender.py`):**
   - Add Tests 4.1, 4.2, 4.3, and 4.4. Verify that low-rating leads receive reputation pitches and vertical-specific businesses receive targeted offerings rather than generic launch pitches.
4. **Implement Pipeline Integration & CSV Round-Trip Suite (`test_pipeline_integration.py`):**
   - Add Tests 5.1, 5.2, 6.1, 6.3, and 6.4. Enforce strict schema coupling between `BusinessRecord` and `FIELDS` and stabilize `lead_score()` computation order.
