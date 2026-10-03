# 062 — Bulk OSM alternatives to the Overpass API

## Verdict

The public Overpass endpoint is the wrong long-term home for a commercial, monthly,
national-scale OSM pull: it is overloaded, its published usage policy tells commercial
users to self-host or pay, and the current query only covers Lebanon. For a monthly job
the cheapest robust path is the free **Geofabrik daily regional PBF extracts**
(`lebanon-latest` ≈ 50 MB, `gcc-states-latest` ≈ 242 MB) filtered locally with `osmium`,
which costs **$0, is one day fresh, and has no rate limit**. If keeping Overpass QL is a
hard requirement, **paid hosted Overpass is $19–139/mo** (Overspan/Mapsource) or
**EUR 40–550/mo** (Geofabrik) and is a one-line URL swap.

## Scope and method

Read-only evaluation. I did **not** run any scraper, make business-data requests, install
packages, or modify source. All claims are anchored to the repo or to a cited, current
(Oct 2026) public source; source URLs are listed at the end.

"Monthly national-scale job" = one run per month covering all of **Lebanon** plus the
**KSA** cities already targeted by the project (Riyadh, Jeddah, Dammam; README.md:3,
`scrapers/google_places.py:82-124`). That is the scale the comparison is priced against;
the actual result set is tens of thousands of named POIs across the two countries.

## Where the current code stands

- Endpoint is **hardcoded to the main public instance**: `scrapers/osm.py:8`
  `OVERPASS_URL = "https://overpass-api.de/api/interpreter"`.
- One giant query, **Lebanon only**, node+way for shop/amenity/office/tourism/craft/healthcare:
  `scrapers/osm.py:10-26` (`area["ISO3166-1"="LB"]`, `[out:json][timeout:180]`). There is
  no `SA` area and no city-level fallback.
- 3 retries with a fixed 30 s sleep, then **silent `return`** — the OSM source disappears
  from the run with only a print, not an exception: `scrapers/osm.py:38-53`; `main.py:107-113`
  logs it and continues.
- No response cache, no second endpoint, no `maxsize`, no jitter, no backoff header handling.

## The options, compared

Prices are USD/EUR as published, per month, for the volumes above. "Freshness" is the data
update cadence, not request latency.

| Option | Freshness | Cost for a monthly LB+KSA pull | Drop-in? | Notes |
|---|---|---|---|---|
| **overpass-api.de** (current) | minutely | $0 **in theory**, but policy-limited (see S1) | — | overloaded; commercial use disallowed by policy |
| Alt. public Overpass: VK Maps, Private.coffee, NextGIS | minutely | $0, no published rate limit | URL swap | no guarantee, undocumented operators |
| FairwayMapper | minutely | free tier for active OSM mappers; paid tiers | URL swap | contributor-gated |
| Geofabrik Overpass | minutely | EUR 40 (10k req) / 80 (100k) / 160 (1M) / from 550 unlimited | URL swap | paid key; resource-heavy queries renegotiated |
| Overspan | minutely | $19 (50k) / $69 (250k) / $139 (1M) | URL swap | "country-scale extracts are what Business is for" |
| Mapsource | minutely | $19 (50k) / $69 (250k) / $139 (1M) | URL swap | near-identical pricing to Overspan |
| Tracestrack Overpass | minutely | EUR 3.99 personal (non-commercial) / EUR 32 commercial (1M credits ≈ 166k calls) | URL swap | cheapest commercial drop-in |
| **Geofabrik PBF** | **daily** | **$0** (~0.3 GB/mo download) | code change | **recommended primary** |
| BBBike | on-demand from a Planet snapshot (weekly planet) | $0 free web/email (≤512 MB) or EUR 129 pro API | code change | manual; pro adds API |
| osm2pgsql + Planet.osm | weekly planet + replication | data $0, infra $ (89 GB PBF; import "days") | large | overkill for two countries |
| GraphHopper | weekly OSM | EUR 69–479 | no | routing/geocoding, not a POI database |
| HERE | not published on pricing page | ~$0.88/1k after 30k/mo free (Geocoding & Search / POI) | no | enrichment, not bulk seed |
| TomTom | Orbis map data 2×/week | free tiers (Places Search Discover 5k/mo, Suggest 10k/mo) | no | enrichment, volume = contact sales |
| Google Places (already used) | near-real-time | ~$0 at current volume (5k free Text Search Pro events/mo) | already integrated | enrichment; $32/1k beyond free cap |
| "OSMdata" (`osmdata.openstreetmap.de`) | not published | $0 | no | coastline/water/land polygons only — **not POI data** (see S3) |

## Findings

### S1 — The public Overpass instance is not licensed for this use, and its bulk limits are below the job

- **Where:** `scrapers/osm.py:8` (public instance), `scrapers/osm.py:10-26` (country-wide bulk query).
- **Breaks:** The OpenStreetMap Wiki's published usage policy for `overpass-api.de` states
  that for regular (not one-off) use you should assume **fewer than 100 queries and less
  than 10 MB of data per day**, that **no parallel scripts** are allowed, and — directly
  relevant to a B2B lead-gen product — **"Commercial use should use self-hosted or paid
  Overpass servers."** It also notes the server is "overloaded" and recommends extracts
  for large jobs. A single monthly country-wide `out:json` of every named POI in Lebanon
  (and eventually KSA) is both over the regular-use data budget and commercial in purpose.
- **Trigger:** any production monthly run on `overpass-api.de`, and certainly any run that
  adds the KSA area.
- **Fix:** pick one of: (a) Geofabrik PBF (recommended, S2), (b) a paid hosted Overpass
  ($19–139/mo, or Tracestrack EUR 32), or (c) a no-limit community instance as a
  best-effort fallback with an explicit maintainer notification. Stop treating the public
  instance as an SLA.

### S2 — Only Lebanon is queried; the "national-scale" KSA half of the OSM source does not exist

- **Where:** `scrapers/osm.py:12` `area["ISO3166-1"="LB"]`; only one ISO area. The project
  advertises Lebanon **and** KSA (README.md:3; `scrapers/google_places.py:44,82-124`), but
  OSM contributes zero KSA rows.
- **Breaks:** The OSM-derived lead pool silently excludes Riyadh/Jeddah/Dammam. Because
  `main.py:117` filters on category, an entire market's free OSM coverage is missing and
  Google Places (paid, rate-limited) is silently carrying it.
- **Trigger:** any run with KSA as a target market.
- **Fix:** parameterise the scraper by country and run two areas (`LB`, `SA`), or switch to
  two Geofabrik extracts: `asia/lebanon-latest.osm.pbf` and `asia/gcc-states-latest.osm.pbf`
  (Geofabrik has no standalone Saudi extract; **GCC States** covers Bahrain, Kuwait, Oman,
  Qatar, Saudi Arabia, UAE — 242 MB, updated daily). A local `osmium tags-filter` pass then
  reproduces the current tag selection deterministically.

### S2 — Single endpoint + silent failure means OSM can vanish for a month with no alert

- **Where:** `scrapers/osm.py:38-53` (3 attempts, then `return`), `main.py:107-113`
  (catches and logs the empty result as success).
- **Breaks:** On a bad day on the overloaded public server the whole OSM source yields
  zero rows and the pipeline still writes CSVs and reports success. For a monthly cadence,
  the master CSV simply stays stale for 30 days. There is no cache, no alternate endpoint,
  and no "got 0 elements" alarm (`osm.py:55-56` prints a count but nothing acts on it).
- **Trigger:** HTTP 429/504/connection reset from `overpass-api.de`, or a query that trips
  the server's resource guards.
- **Fix:** make the endpoint an environment variable with an ordered fallback list
  (paid key → Private.coffee/VK Maps), cache the raw response, and fail the run loudly if
  zero elements come back. Geofabrik PBF removes the failure mode entirely.

### S2 — The "giant POST" pattern is the wrong shape for country-sized extracts

- **Where:** `scrapers/osm.py:10-26` selects an entire country's named POIs in one request.
- **Breaks:** Overpass's own documentation says dynamically generated country-sized results
  "typically take longer to generate and download than downloading existing static extracts
  of the same region," and that for country-sized regions "it's better to use planet.osm
  mirrors." So the current design is explicitly the anti-pattern for this job, independent
  of rate limits. It also has no `[maxsize]`, so behaviour is at the server's mercy.
- **Trigger:** any attempt to extend to KSA or to widen the tag list.
- **Fix:** download the PBF and filter locally; keep Overpass only for small ad-hoc queries.

### S3 — Query gap: relations (multipolygon businesses) are never fetched

- **Where:** `scrapers/osm.py:14-24` queries `node` and `way` only; no `relation`.
- **Breaks:** Businesses mapped as multipolygon relations (some malls, campuses, large
  hotels) are missed. Low incidence but non-zero, and it is a one-line addition.
- **Fix:** add `relation["name"]["shop|amenity|office|tourism|craft|healthcare"](area)` with
  `out center tags`, or accept the gap under the PBF approach (osmium includes relations).

### S3 — Retry/timeout hygiene

- **Where:** `scrapers/osm.py:11` `[timeout:180]` vs. `osm.py:43` HTTP `timeout=200`;
  `osm.py:38-50` fixed 30 s sleeps; no `Retry-After` handling.
- **Fix:** centralise into one HTTP helper with exponential backoff + jitter, honour
  `429`/`Retry-After`, and align query timeout with the HTTP timeout. Minor once the source
  is no longer the public instance.

### S3 — "OSMdata" is not a commercial POI provider

- **Where:** N/A (research item).
- The name resolves to **`osmdata.openstreetmap.de`**, a free distribution of *derived
  geometry* — coastlines, land/water polygons, Antarctic icesheet — not a business/POI
  dataset, and not commercial. No commercial OSM POI vendor trading as "OSMdata" surfaced in
  this pass. If the intent was a commercial POI aggregator, the realistically relevant
  vendors are the ones already named (GraphHopper/HERE/TomTom/Google) plus MapTiler,
  Geoapify, etc., none of which is a better bulk-seed source than free Geofabrik PBF.

## Not a bug, but worth knowing

- **ODbL attribution.** Geofabrik PBF, BBBike and Overpass output are all Open Database
  License 1.0. If leadminer sells or publishes derived rows, it must attribute
  OpenStreetMap and not misrepresent the data as proprietary. Free is not obligation-free.
- **Geofabrik cadence and volume.** `download.geofabrik.de` says extracts are "normally
  updated every day"; at the time of writing `lebanon-latest.osm.pbf` was 50 MB and
  `gcc-states-latest.osm.pbf` 242 MB, both "last modified 12 hours ago." Monthly downloads
  are ≈ 0.3 GB — trivial for CI, and `-updates/*.osc.gz` exist if you ever want incremental.
  A Geofabrik `.gpkg.zip` (already feature-classified) is also available (~493 MB for GCC)
  if you would rather not parse raw tags.
- **Alternative instances (zero-cost, no SLA):** VK Maps (`maps.mail.ru`) advertises "no
  requests limitations"; Private.coffee (ex-`overpass.kumi.systems`) advertises no rate
  limit but asks to be notified before large-scale use; NextGIS advertises no request limit
  and issues free API keys. These are reasonable failover targets, not primary dependencies.
- **Paid Overpass is genuinely cheap at this volume.** Geofabrik's 10k-request tier is
  EUR 40/mo (billed a year up front); Tracestrack's commercial Standard is EUR 32/mo for
  ~166k Overpass calls; Overspan/Mapsource are $19–139. A monthly job issues **one** bulk
  request, so even the smallest paid tier has ~1000× headroom — the cost is for the SLA and
  the commercial licence, not volume.
- **GraphHopper is not a candidate for this lens.** Its pricing (EUR 69/199/479) is for
  routing/geocoding/map-matching, and its data updates are "a minimum of one weekly OSM
  update" — worse freshness than free Geofabrik for a non-POI product.
- **Commercial map APIs are enrichment, not seeding.** HERE Geocoding & Search is ~$0.88/1k
  after 30k/mo free; TomTom Places Search gives 5k Discover + 10k Suggest/mo free; Google's
  Text Search Pro is $32/1k after 5,000 free. At the current ~75-query × ≤3-page volume,
  Google stays inside its 5,000-event free cap (≈ $0/mo), so it is already the cheapest
  enrichment source and should stay as-is. TomTom Orbis map data is released twice a week.
  HERE's per-country map update cadence was not published on the pages reviewed.
- **Planet + osm2pgsql is the wrong tool here.** `planet.osm.pbf` is 89 GB (weekly; XML
  166 GB). The standard tutorial warns that importing the full planet "might take many
  hours, days or weeks depending on the hardware" and requires tuning `-C` to available
  RAM. For two countries, country-level PBFs (50 MB / 242 MB) import in seconds-to-minutes
  and update daily; there is no reason to run planet or replication for a monthly batch.

## Recommended order of work

1. **Add KSA coverage immediately** — at minimum a second `area["ISO3166-1"="SA"]` query
   looped per country. This is the largest correctness gap in the OSM source.
2. **Replace the OSM source with Geofabrik PBF + local filtering** (LB + GCC States daily
   extracts, `osmium tags-filter` to the existing tag set). This makes the job free, daily
   fresh, KSA-capable and ToS-clean, and removes the overloaded-server failure mode.
3. **If Overpass QL must be retained**, make the endpoint configurable and add a paid key
   (Tracestrack EUR 32 or Overspan $69) as primary, with Private.coffee/VK Maps as fallback;
   cache the response and fail loudly on zero elements.
4. **Stop pointing production at `overpass-api.de`** for commercial use; treat it as a
   development convenience only.
5. Only revisit osm2pgsql/planet if the requirement changes from "monthly bulk export" to
   "arbitrary interactive queries."

## Sources

- OpenStreetMap Wiki, *Overpass API* — public instances, rate limits, commercial-use
  policy, bulk-data limitation: https://wiki.openstreetmap.org/wiki/Overpass_API
- Geofabrik download server (daily cadence): https://download.geofabrik.de/
- Geofabrik Asia sub-regions (Lebanon 50 MB, GCC States 242 MB):
  https://download.geofabrik.de/asia.html ; https://download.geofabrik.de/asia/gcc-states.html
- Geofabrik commercial Overpass pricing (EUR 40/80/160/550):
  https://www.geofabrik.de/data/overpass-api.html
- Overspan hosted Overpass pricing ($19/$69/$139):
  https://overspan.dev/
- Mapsource hosted Overpass pricing ($19/$69/$139):
  https://mapsource.io/pricing
- Tracestrack Overpass pricing (EUR 3.99 personal / EUR 32 commercial):
  https://tracestrack.com/pricing
- FairwayMapper Overpass access: https://www.fairwaymapper.com/api-access
- BBBike Extract Service (free custom extracts, 512 MB cap) and pro service (EUR 129/mo):
  https://extract.bbbike.org/ ; https://extract.bbbike.org/support.html ;
  https://extract.bbbike.org/extract-dialog/en/about.html
- Planet OSM sizes and weekly cadence (PBF 89 GB, XML 166 GB): https://planet.openstreetmap.org/
- switch2osm manual tile server (planet import cost, `-C` memory): 
  https://switch2osm.org/serving-tiles/manually-building-a-tile-server-ubuntu-22-04-lts/
- OSM-derived datasets (coastline/land/water polygons), "OSMData":
  https://osmdata.openstreetmap.de/ ; https://osmdata.openstreetmap.de/data/
- GraphHopper pricing and weekly OSM updates: https://www.graphhopper.com/pricing/
- HERE Geocoding & Search pricing (via Gold Partner listing, Aug 2026):
  https://placematic.com/here-location-services/here-pricing/
- TomTom API pricing and Orbis release cadence:
  https://developer.tomtom.com/pricing ;
  https://docs.tomtom.com/map-display-api/documentation/tomtom-orbis-maps/v2/product-information/release-notes
- Google Maps Platform core services price list (Places API (New), Text Search Pro $32/1k,
  5,000 free): https://developers.google.com/maps/billing-and-pricing/pricing
