# 095 — Deduplication: Evidence-Based Entity Resolution

## Verdict

Replace key-based deduplication with a candidate-generation, pair-scoring, and constrained-clustering pipeline. The current exact-phone/one-name-key split misses cross-key duplicates, while `_merge` lets the globally more-populated row decide every conflicting field; neither preserves the evidence needed to audit a merge. Treat each scraper row as an immutable observation, resolve likely observations into entities using multiple independent signals, and retain field-level provenance and a calibrated confidence for every accepted link.

## Findings

### S1 — A single key both misses duplicates and can silently combine unrelated businesses
- **Where:** `dedup.py:102-123` and `dedup.py:125-135`
- **Breaks:** A record with any usable phone is considered only in `phone_index`; it is not compared against the name/address index. Conversely, all no-phone records with the same normalized name and final comma-separated address segment collapse together, regardless of address, category, or distance. A shared/landline phone is treated as conclusive identity. The result depends on which identifiers happen to be present, rather than the combined evidence.
- **Trigger:** A Google Places row with phone `+961...` and an OSM row with the same business name and city but no phone are left as separate outputs. Two no-phone branches named “ABC Pharmacy” whose addresses both end in “Beirut” can be merged despite different streets and coordinates. The final-segment key is especially weak because OSM builds address components in house/street/suburb/city/district order (`scrapers/osm.py:77-84`), while Google supplies a formatted address (`scrapers/google_places.py:249-260`); the last comma component is not a consistently defined city.
- **Fix:** Generate candidates through several selective blocking keys, score each candidate pair with positive and negative evidence, then cluster only high-confidence, internally consistent matches.

### S1 — Whole-record completeness is not a field-level source policy
- **Where:** `dedup.py:79-99`
- **Breaks:** For every conflicting non-null field, `_merge` selects the value from whichever entire row has more non-`None` values. Completeness says nothing about which source is more reliable for that field, whether the value is stale, or whether the two values are a real conflict. It discards the losing observation and cannot explain why the surviving value won. `source` is reduced to a sorted string set and `scraped_at` to the maximum timestamp (`dedup.py:92-96`), which is not enough to recover field provenance.
- **Trigger:** A stale, richly populated OSM row beats a newer Google Places phone/address because the OSM row happens to contain more populated columns; the losing phone and its source disappear. In the opposite case, a sparse row with an incorrect coordinate can replace a better coordinate simply because that row wins the global count comparison.
- **Fix:** Keep per-field observations and provenance; select a display value using a field-specific trust/validation/recency policy, and preserve unresolved conflicts instead of silently overwriting them.

## Proposed design

### 1. Separate raw observations, candidate pairs, and canonical entities

Do not make the merged business dictionary the only surviving representation. Introduce these internal records (dataclasses or TypedDicts are sufficient; no database is required for an initial implementation):

```text
Observation {
  observation_id, source, source_record_id?, observed_at,
  raw: BusinessRecord,
  normalized: {phone[], email[], website_domain, name_forms[], address_parts?, country, lat, lon}
}

FieldValue {
  value, source, observation_id, observed_at,
  source_trust, value_confidence, validation_status
}

Entity {
  entity_id, observation_ids[], fields: {field_name: FieldValue[]},
  selected: {field_name: value},
  match_confidence, match_evidence[], conflict_flags[]
}

MatchDecision {
  left_observation_id, right_observation_id, score, confidence,
  evidence[], contradictions[], decision
}
```

Every input row becomes an `Observation` before normalization or merge. Preserve raw spelling and phone formatting. The current record shape includes `source` and `scraped_at` but no stable upstream identity (`scrapers/base.py:5-28`); add `source_record_id` where available (OSM element type+ID, Wikidata QID, Google Place ID) and keep the scrape timestamp as observation time. Google provides its place fields and coordinates (`scrapers/google_places.py:240-272`), and OSM has element IDs and coordinates available in its input objects (`scrapers/osm.py:58-66`), so stable source IDs can be populated at extraction time. When no upstream ID exists, use a deterministic observation ID derived from source plus a hash of the raw row and run timestamp; do not mistake that hash for entity identity.

Keep the existing `BusinessRecord` values as the selected, flattened compatibility view for enrichment and the current CSV consumers. `main.py:33-40` explicitly defines CSV columns and `main.py:124-129` ignores extra keys, so add output fields deliberately if confidence/provenance must be visible. At minimum add `entity_id`, `match_confidence`, and a machine-readable `match_evidence`/`conflict_flags` representation to the master export; store per-field provenance in an adjacent JSONL sidecar or a dedicated export rather than forcing nested structures into CSV cells. Continue exposing `source` as the union of contributing source names for compatibility, but derive it from observation membership rather than using it as provenance.

### 2. Normalize conservatively and use multiple blocking keys

Normalization should create comparison forms, never overwrite raw input. Reuse `normalize_phone` for the principal phone form, while retaining multiple phone values and the country-context decision; its current minimum length check is useful protection against short junk (`dedup.py:28-63`). For names, use Unicode normalization, `casefold`, whitespace/punctuation cleanup, and conservative Arabic variants as separate forms. Do not make transliteration or aggressive token removal a hard identity key. Parse addresses into components when possible; keep a normalized full-address form and city/region/country separately instead of assuming the last comma component is a city (`dedup.py:72-77`). Normalize website hostnames and emails, stripping URL scheme, `www`, and tracking/path details only for comparison.

Build an inverted index from each blocking key to observation IDs; a pair is a candidate if it shares **any** qualifying block. Avoid a single composite key that requires every field to be present. Suggested initial blocks:

| Block key | Candidate use / guard |
|---|---|
| `(country, normalized_phone)` | Strong block; still score shared phone rather than making it unquestionable, since phones can be shared/reused. |
| `(country, normalized_email)` and `(country, website_domain)` | High-value business contact blocks; discount generic/free email domains and directory/social domains. |
| `(country, normalized_name, city_or_region)` | Fuzzy name comparison inside the block; city/region is a guard, not proof. |
| `(country, rare_name_token, city_or_region)` | Handles spelling/token-order variants without quadratic same-city comparisons; compute token frequency and skip common tokens. |
| `(country, normalized_street_or_address, name_token)` | Finds address/name matches across address-format differences. |
| Neighboring spatial cells at a configured precision | Candidate only when coordinates exist; search adjacent cells so cell boundaries do not split nearby points. Pair scoring must also consider name/category and geographic distance. |

Country should be normalized before blocking, with unknown country kept as unknown rather than silently asserted to be Lebanon. `main.py:50-74` currently applies the Lebanon default, and `main.py:183-184` resolves country only after deduplication; move country resolution/normalization ahead of candidate generation so cross-border phone/name collisions are not compared under inconsistent context. Candidate generation should de-duplicate observation pairs that appear in several blocks and cap overly common blocks (e.g., generic names/domains) to avoid a large all-pairs expansion.

### 3. Score pairs from independent evidence and explicit contradictions

For each candidate pair, compute a feature vector, not a boolean key equality. A practical first scoring policy is an interpretable weighted log-odds model or a documented weighted score trained/calibrated later against labeled pairs. Keep features and reason codes with every decision. Example evidence groups:

- **Strong positive:** exact verified/stable source ID; exact normalized phone; exact non-generic email or website domain.
- **Supporting positive:** name similarity (token/Jaro-Winkler or edit similarity on conservative normalized forms), address/street similarity, compatible category, close coordinates using haversine distance, same normalized social handle.
- **Negative/contradictory:** different valid country; materially distant coordinates for records that otherwise claim the same branch; incompatible specific categories; two distinct valid phones/websites where the candidate otherwise has weak evidence; common/generic name; a shared contact known to be a chain-wide brand contact.

Count correlated evidence only once: name-token and name-edit similarity are one evidence family, and a shared website domain plus shared email domain may be one contact family. Coordinates alone must never establish identity (branches cluster geographically), nor should a common name plus city. Distinguish same legal/business entity from same branch if the product needs both: this lead list should normally make a physical location/branch the entity, while a separate `brand_id` can group chain locations. That prevents a chain-wide name or phone from fusing locations.

Return confidence as a calibrated probability for the pair decision (range 0–1) once labeled examples exist. Until then, label it a provisional score and use conservative thresholds: auto-link only when there is a strong identifier or multiple independent supporting families and no hard contradiction; leave the middle band unmatched/ambiguous for review or a later pass; reject weak-only matches. Store `decision`, threshold/model version, raw score, evidence, and contradictions. A score without its evidence is not auditable, and a probability claim without calibration is misleading.

### 4. Use proximity clustering without transitive chain merges

After pair scoring, order accepted edges strongest-first and build clusters with a constrained union/agglomeration step. Do **not** take connected components of every above-threshold edge: one false bridge can transitively join two distinct businesses. Before admitting an observation to an entity, compare it to the cluster's representative/medoid and check cluster-level invariants: no hard country or branch-distance conflict, no incompatible strong identifiers, and at least one sufficiently strong direct link to the cluster (or strong links to two existing members). Treat a cluster as spatially coherent only when its coordinate spread is within a branch radius; derive an initial radius from observed city-level data and make it configurable, not a global hardcoded truth.

Add chain detection as an explicit validation pass. Flag a candidate merge when A–B and B–C pass but A–C has a strong contradiction, or when the proposed cluster's diameter/conflict set exceeds policy. Do not accept A–B–C solely because of the bridge B. Preserve that flag and split/review the weak edge; never hide this pattern by reporting only one confidence for the component. For each entity, report the minimum or otherwise conservative confidence of the accepted links and retain the individual edge confidences. A cluster-level confidence can be defined as the weakest accepted merge edge, with a separate summary score only if its semantics are documented.

### 5. Merge fields independently and preserve provenance

For each entity and each field, collect all non-empty `FieldValue` observations. Rank candidate values by a **field-specific** source policy, then value validation and recency; do not rank whole records. For example, Google Places rating/review count is the direct source for those metrics, while OSM may provide useful coordinates, address components, and social tags; Wikidata can contribute stable identifiers and curated website data. Treat these as starting priors, not universal truths: source rankings should be configurable and field-specific, and updateable from measured error rates. The current scrapers explicitly tag source as `osm`, `wikidata`, and `google_places` (`scrapers/osm.py:116`, `scrapers/wikidata.py:96`, `scrapers/google_places.py:270`).

Apply field-specific conflict rules:

- For phone, email, website, and social handles, keep a set of distinct valid values with each value's provenance; select a preferred primary value without deleting alternatives. Normalize for comparison but preserve the source string too.
- For lat/lon, treat coordinates as a paired value from one observation, validate coordinate range, and select by geospatial/source quality; never choose latitude and longitude independently from different rows. Retain alternatives and use their spread as a branch-conflict signal.
- For name, address, and category, select a validated/high-trust value but preserve variants; compare conflicting address/name values as merge evidence, not as columns to silently overwrite.
- For `rating`, `review_count`, `website_live`, `completeness_score`, `lead_score`, `industry_priority`, and `recommended_service`, do not apply ordinary source-winner merging. These are source-specific measurements or pipeline-derived fields and should be recomputed after entity resolution/enrichment, or carry the source and measurement time that gives them meaning. `main.py:186-197` already computes/enriches several of these downstream.
- Treat `scraped_at` as observation time, not entity freshness; retain it per observation. Produce any entity `last_seen_at` as a separate aggregate.

When two high-trust values conflict, record a conflict and either leave the selected value unset or choose the configured best value while exposing the conflict. Do not resolve disagreement based on row field count. This makes the chosen flattened row usable while keeping the evidence reversible.

## Recommended order of work

1. Add observation IDs/source record IDs and a provenance representation; preserve current `dedup()` output shape through a compatibility view.
2. Implement normalization and the multi-block candidate index, including country normalization before blocking and pair de-duplication.
3. Add interpretable pair features/reason codes, contradiction guards, conservative provisional thresholds, and match-decision output.
4. Add constrained clustering and chain detection; verify merges against adversarial examples (same-name branches, shared phones, cell-boundary coordinates, missing-phone duplicates, three-node bridge chains).
5. Replace `_merge` with field-specific selection and conflict retention; make CSV/sidecar schema changes explicit and versioned.
6. Calibrate scores and source trust with a small manually labeled set of match/non-match pairs, then monitor false-merge and missed-match rates when thresholds or source policies change.

## Not a bug, but worth knowing

- The input currently has no universal stable source ID in `BusinessRecord` (`scrapers/base.py:5-28`) and the cumulative master re-enters the same pipeline on every run (`main.py:148-181`). Provenance/source IDs and idempotent observation identity are prerequisites for reliable incremental resolution, not optional audit decoration.
- A high-confidence identity match and a high-confidence field value are different claims. Preserve both independently: one says the observations describe the same business/branch, the other says a particular source's phone/address is trustworthy.
