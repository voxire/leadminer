# leadminer

Automated business data scraper for Lebanon and KSA (Riyadh, Jeddah, Dammam).

Runs monthly via GitHub Actions and commits updated CSVs back to the repo.

## What it does

Scrapes three sources in parallel:
- **OpenStreetMap** — bulk Overpass API query for Lebanon businesses
- **Wikidata** — SPARQL query for Lebanon entities
- **Google Places** — Text Search API across 67 targeted queries (LB + KSA)

Then enriches the data:
- Checks website liveness
- Extracts email, Instagram, WhatsApp, and LinkedIn from live websites
- Infers Lebanese governorate or KSA city from address/coordinates
- Deduplicates across sources by phone number, then by (name, city)
- Tags each business with `industry_priority`, `recommended_service`, and `lead_score`

## Output files (`data/`)

| File | Contents |
|------|----------|
| `all_businesses.csv` | Every business that passed the category whitelist |
| `qualified_businesses.csv` | Subset with at least one contact signal (completeness_score ≥ 1) |
| `with_websites.csv` | Businesses that have a website |
| `without_websites.csv` | Businesses with no website (new-site pitch targets) |
| `sales_ready.csv` | High/medium-priority industries with at least one contact channel |

## Key fields

| Field | Description |
|-------|-------------|
| `phone` | Normalized to E.164 (+961 Lebanon, +966 KSA) |
| `email` | From scraper tags or extracted from website |
| `instagram` | Handle extracted from website or OSM tags |
| `whatsapp` | Extracted from `wa.me` links on the website |
| `linkedin` | Company page URL extracted from website |
| `website_live` | `true` if reachable, `false` if dead |
| `completeness_score` | 0–7 count of filled contact fields |
| `lead_score` | 0–100 weighted quality score |
| `industry_priority` | `high` / `medium` / `low` |
| `recommended_service` | Which Voxire service to pitch |
| `region` | Lebanese governorate or KSA city |
| `source` | `osm`, `wikidata`, `google_places`, or `osm\|google_places` (multi-source) |

## Setup

### Requirements

- Python 3.12+
- `uv` (or `pip`)

```bash
pip install uv
uv pip install --system -r requirements.txt
```

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GOOGLE_PLACES_API_KEY` | Yes | Google Places API (New) key |

Enable **Places API (New)** in [Google Cloud Console](https://console.cloud.google.com/apis/library).

### Run locally

```bash
GOOGLE_PLACES_API_KEY=your_key python main.py
```

## GitHub Actions

The workflow runs automatically on the 1st of every month at 03:00 UTC.  
You can also trigger it manually from the **Actions** tab → **Run workflow**.

Add `GOOGLE_PLACES_API_KEY` to **Settings → Secrets and variables → Actions**.

## Architecture

```
main.py
├── scrapers/
│   ├── osm.py          # OpenStreetMap Overpass
│   ├── wikidata.py     # Wikidata SPARQL
│   └── google_places.py # Google Places API
├── dedup.py            # Phone-first deduplication + merge
├── enricher.py         # Liveness check, web scraping, lead scoring
├── pitch_recommender.py # Service recommendation logic
└── scrapers/whitelist.py # Category filter
```
