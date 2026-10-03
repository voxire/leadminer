# 069 — Configuration file format

## Verdict

Use **TOML** as the single config format for `leadminer`, loaded with Python's stdlib
`tomllib` (3.11+, so free on the pinned 3.12 runtime) and validated with a typed
`pydantic-settings` model. The decisive, project-specific reason is that YAML's
implicit boolean coercion would corrupt a literal value already in this codebase —
the string `"yes"` in the `GEOGRAPHIC_NOISE` hard-block set (`scrapers/whitelist.py:100`)
parses as boolean `True` in PyYAML — while TOML has no such coercion and needs no new
runtime dependency to read.

## Why TOML over YAML and JSON

| Criterion | TOML | YAML | JSON | Winner |
|---|---|---|---|---|
| Read dependency | `tomllib` (stdlib) | `pyyaml` (new pin) | `json` (stdlib) | TOML / JSON |
| Comments | yes | yes | **no** | TOML / YAML |
| Trailing commas / human ergonomics | yes | yes | **no** | TOML / YAML |
| Ordered lists/arrays | native | native | native | all |
| Ordered tables (first-match region rules) | native (`[[...]]`) | native | n/a | TOML / YAML |
| Implicit type coercion | **none** | `yes`→`True`, `1.0`→float, etc. | none | TOML / JSON |
| Writer in stdlib | **no** (read-only) | n/a | yes | JSON |

The two real contenders are TOML and YAML. JSON is eliminated first: this config will
be hand-edited by operators, and it must hold ~200 list entries across the query lists,
region keyword lists, and whitelist sets. JSON has no comments and no trailing commas,
so it is the worst format for a human-maintained file of that size. JSON's only edge —
a stdlib writer — is irrelevant because the app only ever *reads* this file; the file is
authored by humans and CI.

YAML is eliminated on a concrete correctness hazard that is not hypothetical for this
project:

- **`scrapers/whitelist.py:100`** puts the literal string `"yes"` in `GEOGRAPHIC_NOISE`
  ("OSM tag noise from amenity=yes records"). PyYAML (YAML 1.1) parses an *unquoted*
  `yes` as the boolean `True`. An operator writing `yes` instead of `"yes"` in the config
  would silently turn the hard-block entry into `True`, changing set membership and
  letting `amenity=yes` OSM noise back through the filter. The same trap applies to any
  future category/keyword that happens to be `no`, `on`, `off`, `true`, `false`, or a
  numeric-looking string. TOML has no implicit booleans from `yes`/`no`; a bare `yes`
  is a syntax error, which fails loudly at load time instead of corrupting a filter.
- YAML would also add `pyyaml` to `requirements.txt` (currently three pins,
  `requirements.txt:1-3`) and require a blanket "always `safe_load`, never `load`"
  discipline. TOML adds nothing for reading.

TOML is also what the Python ecosystem and the rest of this migration already assume:
`pydantic-settings` reads TOML natively, and audit 037 proposes a `pyproject.toml` for
packaging. One format for build metadata and runtime config is less cognitive load.

The one real cost of TOML is that `tomllib` is **read-only**. If we later want
`leadminer --write-default-config`, we need `tomli-w` (a tiny, pinned dev-only
dependency) or a hand-rolled writer. That is a small, deferred cost — see S3.

## The schema

Settings are grouped into nine sections. Every value below has an **exact current
default** that preserves the existing literal verbatim (spelling, case, order,
membership), so adopting the config changes nothing about product behavior until an
operator actually overrides it. Ordered collections (region boxes, region keyword
regions) must be modeled as **arrays of tables** (`[[...]]`), never as maps keyed by
region name, because `enricher.py:86-93` returns the *first* matching box/keyword.

| Section | Key | Type | Default (source) |
|---|---|---|---|
| `credentials` | `google_places_api_key_env` | `str` | `"GOOGLE_PLACES_API_KEY"` (`google_places.py:146`) |
| | `scraper_email_env` | `str` | `"SCRAPER_EMAIL"` (`osm.py:35`, `wikidata.py:34`) |
| | `scraper_email_default` | `str` | `"voxire.tech@gmail.com"` (`osm.py:35`) |
| `scraping` | `orchestration_workers` | `int >= 1` | `3` (`main.py:155-160`) |
| `google_places` | `workers` | `int >= 1` | `5` (`google_places.py:141`) |
| | `lebanon_queries` | `list[str]` | full `LEBANON_QUERIES`, verbatim (`google_places.py:45-79`) |
| | `ksa_queries` | `list[str]` | full `KSA_QUERIES`, verbatim (`google_places.py:82-124`) |
| | `request_timeout_seconds` | `float > 0` | `30` (`google_places.py:209`) |
| | `page_delay_seconds` | `float >= 0` | `2` (`google_places.py:278`) |
| | `rate_limit_wait_seconds` | `float > 0` | `30` (`google_places.py:214`) |
| `osm` | `overpass_query` | `str` | full `OVERPASS_QUERY`, verbatim (`osm.py:10-26`) |
| | `request_timeout_seconds` | `float > 0` | `200` (`osm.py:43`) |
| | `retries` | `int >= 0` | `3` (`osm.py:38`) |
| | `retry_delay_seconds` | `float >= 0` | `30` (`osm.py:50`) |
| `wikidata` | `sparql_query` | `str` | full `SPARQL_QUERY`, verbatim (`wikidata.py:10-26`) |
| | `request_timeout_seconds` | `float > 0` | `90` (`wikidata.py:46`) |
| | `retries` | `int >= 0` | `3` (`wikidata.py:40`) |
| | `retry_delay_seconds` | `float >= 0` | `15` (`wikidata.py:52`) |
| `regions` | `lb_boxes` | `list[RegionBox]` (ordered) | 8 boxes, verbatim (`enricher.py:58-68`) |
| | `sa_boxes` | `list[RegionBox]` (ordered) | 5 boxes, verbatim (`enricher.py:70-76`) |
| | `keywords` | `list[RegionKeywords]` (ordered) | 8 regions, verbatim (`enricher.py:10-50`) |
| `enrichment` | `website_workers` | `int >= 1` | `40` (`enricher.py:217`) |
| | `http_timeout_seconds` | `float > 0` | `8` (`enricher.py:167`) |
| | `max_body_bytes` | `int > 0` | `200000` (`enricher.py:155`) |
| | `user_agent` | `str` | `"Mozilla/5.0 (compatible; leadminer/1.0)"` (`enricher.py:125`) |
| | `email_blacklist` | `set[str]` | 8 domains, verbatim (`enricher.py:135-138`) |
| | `instagram_blacklist` | `set[str]` | 8 handles, verbatim (`enricher.py:139`) |
| `website_policy` | `live` / `dead` / `unknown` | `StatePolicy` | see below (`enricher.py:151-153,292-296`) |
| `scoring` | `completeness_weights` | 7 weights | `1` each (`enricher.py:101-117`) |
| | `lead_weights` | 10 weights + caps | see below (`enricher.py:275-311`) |
| | `phone_min_digits` | `int > 0` | `7` (`enricher.py:284`, `dedup.py:30`) |
| | `low_rating_threshold` | `float` | `4.0` (`enricher.py:305`) |
| | `max_score` | `int > 0` | `100` (`enricher.py:311`) |
| `whitelist` | `priority_industries` | `set[str]` | full set, verbatim (`whitelist.py:17-42`) |
| | `adjacent_businesses` | `set[str]` | full set, verbatim (`whitelist.py:45-82`) |
| | `geographic_noise` | `set[str]` | full set, verbatim (`whitelist.py:85-102`) |
| | `business_keywords` | `list[str]` | 11 keywords, verbatim (`whitelist.py:123-128`) |

### Tri-state website policy

The "new" tri-state policy is the fix for the worst bug in the pipeline: a site the
crawler merely *failed to reach* must never be treated as a rebuild pitch. The current
code encodes this as three verdicts (`enricher.py:151-153`) with asymmetric lead-score
treatment (`enricher.py:292-296`): live +10, dead +20, unknown +0. Model it as three
tables with two knobs each — the `lead_weight` contributed to `lead_score`, and
`rebuild_pitch` (whether `recommend_service` treats the state as a rebuild opportunity,
mirroring `pitch_recommender.py:57`'s `has_dead_website` gate):

| State | Meaning (`enricher.py`) | `lead_weight` | `rebuild_pitch` |
|---|---|---|---|
| `live` | server answered 2xx; site works | `10` | `false` |
| `dead` | server answered 4xx/5xx; confirmed broken | `20` | `true` |
| `unknown` | no answer: DNS/TLS/timeout/WAF; nothing learned | `0` | `false` |

`unknown` must stay pinned at `lead_weight = 0` and `rebuild_pitch = false` in the
schema docs — making it configurable is what enables the inversion bug to return. It is
exposed only so the *naming* and the live/dead weights can be tuned, not so unknown can
be re-scored as an opportunity.

## Complete example config (`config.toml`)

```toml
# leadminer runtime configuration.
# Secrets are NEVER stored here. The `credentials` section only names the
# environment variable (or secret-store key) that holds each value.

[credentials]
google_places_api_key_env = "GOOGLE_PLACES_API_KEY"
scraper_email_env         = "SCRAPER_EMAIL"
scraper_email_default     = "voxire.tech@gmail.com"

[scraping]
orchestration_workers = 3

# ---------------------------------------------------------------------------
# Google Places (Text Search API, New)
# ---------------------------------------------------------------------------
[google_places]
workers                = 5
request_timeout_seconds = 30
page_delay_seconds      = 2     # required pause between paginated requests
rate_limit_wait_seconds = 30    # sleep on HTTP 429

[google_places]
lebanon_queries = [
  "restaurants in Lebanon", "cafes in Lebanon", "hotels in Lebanon",
  "boutique hotels in Lebanon", "boutiques in Lebanon", "fashion stores in Lebanon",
  "jewelry stores in Lebanon", "beauty salons in Lebanon", "cosmetic clinics in Lebanon",
  "dental clinics in Lebanon", "fertility clinics in Lebanon", "hospitals in Lebanon",
  "pharmacies in Lebanon", "real estate offices in Lebanon", "real estate developers in Lebanon",
  "law firms in Lebanon", "accounting firms in Lebanon", "consulting firms in Lebanon",
  "marketing agencies in Lebanon", "architecture firms in Lebanon", "schools in Lebanon",
  "language schools in Lebanon", "gyms in Lebanon", "yoga studios in Lebanon",
  "supermarkets in Lebanon", "bakeries in Lebanon", "car dealerships in Lebanon",
  "insurance companies in Lebanon", "travel agencies in Lebanon", "wedding venues in Lebanon",
  "photography studios in Lebanon", "tech startups in Lebanon", "co-working spaces in Lebanon",
]
ksa_queries = [
  "fashion brands in Riyadh", "fashion brands in Jeddah", "perfume brands in Saudi Arabia",
  "beauty brands in Saudi Arabia", "jewelry brands in Riyadh", "DTC brands in Saudi Arabia",
  "online retail brands in Riyadh", "restaurant groups in Riyadh", "restaurant groups in Jeddah",
  "boutique hotels in Riyadh", "boutique hotels in Jeddah", "luxury hotels in Saudi Arabia",
  "cafe chains in Saudi Arabia", "real estate developers in Riyadh", "real estate developers in Jeddah",
  "property developers in Saudi Arabia", "real estate brokers in Riyadh",
  "fintech startups in Saudi Arabia", "fintech companies in Riyadh", "insurtech companies in Saudi Arabia",
  "tech startups in Riyadh", "SaaS companies in Saudi Arabia", "healthcare clinics in Riyadh",
  "dental clinic networks in Saudi Arabia", "cosmetic clinics in Riyadh", "fertility clinics in Saudi Arabia",
  "law firms in Riyadh", "consulting firms in Saudi Arabia", "marketing agencies in Riyadh",
  "co-working spaces in Riyadh", "events venues in Riyadh", "restaurants in Dammam",
  "real estate developers in Dammam", "hotels in Dammam",
]

# ---------------------------------------------------------------------------
# Overpass (OSM) — Lebanon-only query, verbatim
# ---------------------------------------------------------------------------
[osm]
request_timeout_seconds = 200
retries                 = 3
retry_delay_seconds     = 30
overpass_query = """
[out:json][timeout:180];
area["ISO3166-1"="LB"]->.lb;
(
  node["name"]["shop"](area.lb);
  node["name"]["amenity"](area.lb);
  node["name"]["office"](area.lb);
  node["name"]["tourism"](area.lb);
  node["name"]["craft"](area.lb);
  node["name"]["healthcare"](area.lb);
  way["name"]["shop"](area.lb);
  way["name"]["amenity"](area.lb);
  way["name"]["office"](area.lb);
  way["name"]["tourism"](area.lb);
);
out center tags;
"""

# ---------------------------------------------------------------------------
# Wikidata SPARQL — Lebanon-only query, verbatim (includes LIMIT 5000)
# ---------------------------------------------------------------------------
[wikidata]
request_timeout_seconds = 90
retries                 = 3
retry_delay_seconds     = 15
sparql_query = """
SELECT ?item ?itemLabel ?websiteLabel ?phoneLabel ?emailLabel ?addressLabel ?categoryLabel WHERE {
  ?item wdt:P17 wd:Q822.
  OPTIONAL { ?item wdt:P856 ?website. }
  OPTIONAL { ?item wdt:P1329 ?phone. }
  OPTIONAL { ?item wdt:P968 ?email. }
  OPTIONAL { ?item wdt:P6375 ?address. }
  OPTIONAL {
    ?item wdt:P31 ?category.
    ?category rdfs:label ?categoryLabel.
    FILTER(LANG(?categoryLabel) = "en")
  }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,ar". }
  FILTER(?item != wd:Q822)
}
LIMIT 5000
"""

# ---------------------------------------------------------------------------
# Region inference — ORDER MATTERS: first matching box/keyword wins.
# ---------------------------------------------------------------------------
[[regions.lb_boxes]]
name = "Beirut";          lat_min = 33.845; lat_max = 33.920; lon_min = 35.462; lon_max = 35.545
[[regions.lb_boxes]]
name = "Akkar";           lat_min = 34.380; lat_max = 34.720; lon_min = 35.980; lon_max = 36.650
[[regions.lb_boxes]]
name = "Baalbek-Hermel";  lat_min = 34.000; lat_max = 34.720; lon_min = 36.100; lon_max = 36.850
[[regions.lb_boxes]]
name = "Bekaa";           lat_min = 33.380; lat_max = 34.200; lon_min = 35.750; lon_max = 36.650
[[regions.lb_boxes]]
name = "South Lebanon";   lat_min = 33.040; lat_max = 33.580; lon_min = 35.090; lon_max = 35.750
[[regions.lb_boxes]]
name = "Nabatieh";        lat_min = 33.240; lat_max = 33.560; lon_min = 35.330; lon_max = 35.720
[[regions.lb_boxes]]
name = "North Lebanon";   lat_min = 34.100; lat_max = 34.680; lon_min = 35.490; lon_max = 36.300
[[regions.lb_boxes]]
name = "Mount Lebanon";   lat_min = 33.540; lat_max = 34.120; lon_min = 35.370; lon_max = 35.950

[[regions.sa_boxes]]
name = "Riyadh";  lat_min = 24.40; lat_max = 25.20; lon_min = 46.40; lon_max = 47.20
[[regions.sa_boxes]]
name = "Jeddah";  lat_min = 21.30; lat_max = 21.80; lon_min = 39.05; lon_max = 39.45
[[regions.sa_boxes]]
name = "Dammam";  lat_min = 26.20; lat_max = 26.65; lon_min = 49.85; lon_max = 50.30
[[regions.sa_boxes]]
name = "Mecca";   lat_min = 21.30; lat_max = 21.55; lon_min = 39.75; lon_max = 40.00
[[regions.sa_boxes]]
name = "Medina";  lat_min = 24.30; lat_max = 24.65; lon_min = 39.45; lon_max = 39.80

[[regions.keywords]]
name = "Beirut"
keywords = ["beirut", "بيروت", "hamra", "achrafieh", "verdun", "ras beirut", "sodeco",
            "badaro", "gemmayze", "mar mikhael", "corniche", "bliss", "sanayeh",
            "zkak el blat", "tallet el khayat", "raouche"]
[[regions.keywords]]
name = "Mount Lebanon"
keywords = ["jounieh", "jbeil", "byblos", "baabda", "aley", "chouf", "metn", "antelias",
            "jdeideh", "bikfaya", "broummana", "beit mery", "dbayeh", "naccache",
            "sin el fil", "dekwaneh", "hazmieh", "aramoun", "khalde", "damour", "jiyeh",
            "bchamoun", "bhamdoun", "aaley", "deir el qamar", "beit ed dine",
            "zahle el metn", "kaslik", "zouk", "ghazir", "keserwan", "fanar",
            "mansourieh", "mtayleb", "rabweh"]
[[regions.keywords]]
name = "North Lebanon"
keywords = ["tripoli", "طرابلس", "zgharta", "batroun", "bcharre", "koura", "amioun",
            "chekka", "enfeh", "qalamoun", "minyeh", "danniyeh", "bsharri", "ehden",
            "kousba", "zghorta"]
[[regions.keywords]]
name = "Akkar"
keywords = ["akkar", "عكار", "halba", "حلبا", "andqet", "bebnine", "kobayat", "qoubaiyat"]
[[regions.keywords]]
name = "South Lebanon"
keywords = ["sidon", "saida", "صيدا", "tyre", "sur", "صور", "jezzine", "nabatieh",
            "النبطية", "bint jbeil", "marjayoun", "khiam", "ibl el saqi", "hasbaya"]
[[regions.keywords]]
name = "Nabatieh"
keywords = ["nabatieh", "النبطية", "bint jbeil", "بنت جبيل", "hasbaya", "marjayoun", "merjeyoun"]
[[regions.keywords]]
name = "Bekaa"
keywords = ["zahle", "زحلة", "chtaura", "anjar", "rashaya", "west bekaa", "saghbine",
            "yohmor", "taanayel", "bar elias"]
[[regions.keywords]]
name = "Baalbek-Hermel"
keywords = ["baalbek", "بعلبك", "hermel", "الهرمل", "yammouneh", "ras baalbek", "qaa",
            "deir el ahmar"]

# ---------------------------------------------------------------------------
# Website liveness + contact extraction
# ---------------------------------------------------------------------------
[enrichment]
website_workers        = 40
http_timeout_seconds   = 8
max_body_bytes         = 200000
user_agent             = "Mozilla/5.0 (compatible; leadminer/1.0)"
email_blacklist        = ["example.com", "domain.com", "yourdomain.com", "email.com",
                          "sentry.io", "wixpress.com", "squarespace.com", "shopify.com"]
instagram_blacklist    = ["instagram", "p", "explore", "accounts", "stories", "reel", "reels", "tv"]

# ---------------------------------------------------------------------------
# Tri-state website policy — see docs/audits/043-scoring-upgrade.md.
# `unknown` must stay weight 0 / rebuild_pitch false.
# ---------------------------------------------------------------------------
[website_policy.live]
lead_weight = 10
rebuild_pitch = false

[website_policy.dead]
lead_weight = 20
rebuild_pitch = true

[website_policy.unknown]
lead_weight = 0
rebuild_pitch = false

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
[scoring.completeness_weights]
phone    = 1
email    = 1
website  = 1
address  = 1
social   = 1   # facebook OR instagram
whatsapp = 1
linkedin = 1

[scoring.lead_weights]
email                 = 20
whatsapp              = 15
phone                 = 15
instagram             = 10
live_website          = 10
dead_website          = 20
high_priority         = 15
medium_priority       = 8
rating_below_threshold = 10
multi_source          = 5

[scoring]
phone_min_digits     = 7
low_rating_threshold = 4.0
max_score            = 100

# ---------------------------------------------------------------------------
# Category whitelist
# ---------------------------------------------------------------------------
[whitelist]
priority_industries = [
  # Restaurants and hospitality
  "restaurant", "cafe", "fast_food", "bar", "pub", "biergarten", "food_court",
  "ice_cream", "bistro", "patisserie", "tea_house", "coffee_shop",
  # Hotels and tourism
  "hotel", "guest_house", "hostel", "motel", "resort", "boutique_hotel",
  "bed_and_breakfast", "tourism", "tour_operator",
  # Healthcare clinics (high-margin)
  "clinic", "clinics", "doctors", "dentist", "dental_clinic", "veterinary",
  "vet", "physiotherapist", "physio", "optician", "optometrist",
  "cosmetic_surgery", "cosmetic_clinic", "dermatology", "dermatologist",
  "fertility_clinic", "medical_clinic", "aesthetic_clinic",
  "spa", "wellness", "medical_centre", "medical_center",
  # Real estate
  "real_estate_agent", "real_estate", "estate_agent", "property",
  # Professional services (high LTV)
  "lawyer", "law_firm", "attorney", "notary", "accountant", "accounting",
  "tax_advisor", "consulting", "marketing_agency", "advertising_agency",
  "design_studio", "architect", "engineering_firm",
  # Fashion and e-commerce
  "clothes", "boutique", "shoes", "jewelry", "jewellery", "bag",
  "watches", "fashion_accessories", "perfumery", "cosmetics",
  "beauty", "lingerie",
  # Tech and SaaS
  "software", "internet_cafe", "computer", "telecommunications",
]
adjacent_businesses = [
  "supermarket", "convenience", "grocery", "bakery", "butcher", "deli", "wine",
  "alcohol", "tobacco", "newsagent", "kiosk", "department_store", "mall",
  "shopping_centre", "marketplace", "variety_store", "general", "trade",
  "hairdresser", "barber", "nail_salon", "massage", "tattoo", "tanning",
  "laundry", "dry_cleaning", "tailor", "cobbler", "pharmacy", "chemist",
  "hospital", "medical_supply", "fitness_centre", "fitness", "gym", "yoga",
  "pilates", "sports_centre", "swimming_pool", "school", "kindergarten",
  "language_school", "music_school", "driving_school", "training_centre",
  "tutoring", "university", "college", "academy", "car", "car_dealership",
  "car_repair", "car_wash", "car_rental", "motorcycle", "tyres", "parts",
  "furniture", "interior_decoration", "florist", "garden_centre", "hardware",
  "doityourself", "paint", "lighting", "carpet", "curtain", "kitchen",
  "bathroom_furnishing", "appliance", "books", "art", "music",
  "musical_instrument", "stationery", "gift", "toys", "pet", "pet_grooming",
  "photo", "video", "electronics", "mobile_phone", "computer_repair",
  "confectionery", "dairy", "seafood", "spices", "coffee", "tea", "chocolate",
  "bank", "atm", "money_lender", "money_transfer", "insurance",
  "currency_exchange", "post_office", "courier", "logistics", "travel_agency",
  "events_venue", "wedding_venue", "photography_studio", "printing",
  "copy_shop", "advertising",
]
geographic_noise = [
  "village/town/city in Lebanon", "village", "town", "city", "hamlet", "suburb",
  "neighbourhood", "quarter", "borough", "municipality", "human settlement",
  "place", "locality", "mountain", "peak", "hill", "ridge", "valley", "plateau",
  "watercourse", "river", "stream", "lake", "spring", "waterfall", "wadi", "bay",
  "cape", "island", "beach", "coast", "place_of_worship", "church", "mosque",
  "temple", "shrine", "cemetery", "grave_yard", "monastery", "synagogue",
  "monument", "memorial", "archaeological_site", "ruins", "castle", "fort",
  "tower", "obelisk", "statue", "park", "garden", "playground", "nature_reserve",
  "protected_area", "forest", "wood", "grassland", "meadow", "wetland", "highway",
  "road", "path", "track", "junction", "roundabout", "bus_stop", "parking",
  "fuel_dispenser", "yes", "metaorganization", "organization",
]
business_keywords = ["shop", "store", "agency", "firm", "company", "service",
                     "studio", "office", "salon", "centre", "center", "boutique"]
```

> Note: the `[google_places]` table is opened twice (before and after the query
> lists) purely to keep the long arrays near the scalar knobs; TOML allows reopening a
> table. The loader treats them as one table. An operator can also hoist the arrays
> into the first `[google_places]` block if they prefer.

## Layering: defaults → file → env → CLI

Resolution order (highest wins), consistent with audit 036's recommended precedence:

```
CLI flags  >  LEADMINER__ env vars  >  config.toml  >  code defaults
```

1. **Code defaults** are the typed field defaults in the `pydantic-settings` model. They
   are initialized to the exact literals above so a missing config still reproduces
   today's behavior byte-for-byte.
2. **`config.toml`** (or the path in `LEADMINER_CONFIG`) supplies non-secret overrides,
   committed to the repo and edited by operators.
3. **Environment variables** override the file per-run, used by GitHub Actions and local
   one-offs. Naming: `LEADMINER_` + section path with `__` as the nesting delimiter.
   Scalar values are plain strings; **list/set values are JSON-encoded** (audit 036
   already specifies JSON arrays for structured overrides).
4. **CLI flags** are the highest layer. They do not cover every key — audit 037's
   subcommands expose `--workers`, `--timeout`, `--max-bytes`, `--user-agent`, `--source`,
   `--region`, and `--data-dir`. Settings with no CLI flag (query lists, region boxes,
   scoring weights, whitelist sets, rate-limit constants) are overridden via file or env.

### Environment variable examples

```bash
# scalar: 3 workers instead of the TOML value of 5
LEADMINER__GOOGLE_PLACES__WORKERS=3

# list: replace the entire Lebanon query list (JSON-encoded, no trailing commas)
LEADMINER__GOOGLE_PLACES__LEBANON_QUERIES='["restaurants in Lebanon","cafes in Lebanon"]'

# scalar float: lower the low-rating threshold
LEADMINER__SCORING__LOW_RATING_THRESHOLD=3.8

# point at a non-default config file
LEADMINER_CONFIG=config/ksa.toml

# secrets — these stay env-only and are read by name from the [credentials] section
GOOGLE_PLACES_API_KEY=...   # referenced, never stored, in config.toml
SCRAPER_EMAIL=ops@voxire.com
```

### CLI mapping (overrides config.toml, then env for those keys)

| CLI flag (audit 037) | Config key it overrides |
|---|---|
| `scrape --workers N` | `google_places.workers` |
| `enrich --workers N` | `enrichment.website_workers` |
| `enrich --timeout T` | `enrichment.http_timeout_seconds` |
| `enrich --max-bytes N` | `enrichment.max_body_bytes` |
| `enrich --user-agent S` | `enrichment.user_agent` |
| `scrape --region LB|SA|...` | selects `google_places.lebanon_queries` / `ksa_queries` |
| `scrape --source ...` | enables/disables a scraper section |

A CLI value beats the env var for the same key, which beats the file, which beats the
default. A single key is always resolved by the highest layer that sets it; lower layers
are never merged for that key (list/set overrides are wholesale replacement, not
concatenation — document this so operators don't expect `LEADMINER__..._QUERIES` to
*append* to the file list).

### Worked example

With the defaults and the `config.toml` above, then:

```bash
export LEADMINER__GOOGLE_PLACES__WORKERS=3
leadminer scrape --source google_places --workers 2
```

the effective `google_places.workers` for this run is `2` (CLI `2` beats env `3` beats
file `5` beats default `5`). `enrichment.website_workers` remains `40` from the file,
since no layer overrides it.

## Findings

### S2 — Secrets are one paste away from being committed
- **Where:** `google_places.py:146` reads `os.environ.get("GOOGLE_PLACES_API_KEY")`;
  the config is a new, git-tracked file.
- **Breaks:** If the key itself (not its env-var name) lands in `config.toml`, it is
  committed to the repo and leaked. The `[credentials]` section above deliberately holds
  only *references*, but nothing enforces that at the schema level.
- **Trigger:** An operator pastes the key into `[credentials]` to "make it work locally"
  and commits.
- **Fix:** Model credential fields as `SecretStr` in `pydantic-settings` with the TOML
  source *excluded*, so they resolve only from env/secrets; add a `doctor` check
  (audit 037) that fails if a secret-looking value appears in the file.

### S2 — List/set overrides via env must be JSON, or they silently mis-parse
- **Where:** `google_places.py:45-124`, `whitelist.py:17-102` become `list[str]` /
  `set[str]` settings.
- **Breaks:** Env vars are strings. A bare `LEADMINER__WHITELIST__PRIORITY_INDUSTRIES=restaurant,cafe`
  yields one string `"restaurant,cafe"`, not two members, silently emptying the allow-list
  and dropping every record.
- **Trigger:** An operator sets a comma-separated env override expecting a split.
- **Fix:** Enforce JSON encoding for structured overrides (as audit 036 specifies) and
  validate at load: reject a list/set field that resolves to a non-JSON scalar instead of
  coercing it.

### S2 — Ordered region rules must survive the loader as arrays, not dicts
- **Where:** `enricher.py:86-93` returns the *first* matching box/keyword region.
- **Breaks:** If `regions.lb_boxes` / `regions.keywords` are modeled as a dict keyed by
  region name, order is at the mercy of the TOML table's key iteration and any sorting the
  loader does. Reordering boxes silently changes region labels on records.
- **Trigger:** A "Beirut" box is moved after "Mount Lebanon" during a cleanup; businesses
  in the overlap (`33.85-33.92`) flip regions.
- **Fix:** Model both as `list[RegionBox]` / `list[RegionKeywords]` (the `[[...]]` form
  above), which tomllib and pydantic both preserve in order. Add a validator that warns
  on overlapping boxes that are not ordered most-specific-first.

### S3 — `tomllib` is read-only; a default-config writer needs a small dependency
- **Where:** Python stdlib `tomllib` (3.11+) can only *parse*.
- **Breaks:** `leadminer --write-default-config` (a nice-to-have for onboarding) cannot
  be implemented with stdlib alone.
- **Fix:** Defer it. If ever needed, add `tomli-w` as a pinned dev dependency; it is
  small and does not affect the runtime read path.

## Not a bug, but worth knowing

- **`website_policy.unknown` is the one knob that must not be "made configurable".** The
  entire point of the tri-state policy is that `unknown ≠ dead`. Exposing `unknown` in the
  schema is for naming/clarity, not for re-scoring. Leave a comment at the call site
  (the successor to `enricher.py:292-296`) pointing at audit 043 so no one re-adds the
  inversion by turning up the unknown weight.
- **Pitch-target category sets are out of scope but adjacent.** `pitch_recommender.py`
  hardcodes `_ECOM_FRIENDLY` (`:103-109`), `_RTYLR_TARGETS` (`:112-117`), and
  `_LEAD_GEN_VERTICALS` (`:120-132`). These are not whitelist sets, so they are not in the
  schema above, but they are the same "tunable category collection" pattern and should be
  promoted to the config in the same pass if pitch tuning becomes an operator task.
- **Whitelist sets have no TOML native `set` type.** Declare them `set[str]` in the
  pydantic model; pydantic converts the TOML array to a set, and duplicates (none exist
  today) would collapse silently — acceptable, but worth a lint that flags exact
  duplicates in `priority_industries` vs `adjacent_businesses` vs `geographic_noise`.

## Recommended order of work

1. Add `pydantic-settings` (pin it) and write the typed model with every default set to
   the exact literal in the table above; keep `credentials` as env-only `SecretStr`.
2. Add TOML loading via stdlib `tomllib` with `LEADMINER_CONFIG` for the path, and the
   `LEADMINER__` env namespace with JSON-encoded structured values.
3. Thread the resolved settings object from `main()` (or the audit-037 CLI entry point)
   into the scrapers, `infer_region`, whitelist, `check_websites`, and scoring; delete the
   module-level constant copies only after all consumers read from the model.
4. Wire the CLI flags in the mapping table to their config keys, confirming precedence
   with a handful of settings-only unit tests (no network).
5. Add load-time validation (positive worker counts, ordered non-degenerate boxes,
   JSON-parseable lists, non-empty query lists, `unknown` weight pinned to 0) and a
   startup summary of effective non-secret settings.
