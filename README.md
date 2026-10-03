# leadminer

Automated business data scraper for Lebanon and KSA (Riyadh, Jeddah, Dammam).

Runs weekly via GitHub Actions (Monday 03:00 UTC) and writes updated CSVs back
to Google Drive.

## Command line

```bash
leadminer run                  # full pipeline: scrape -> enrich -> export
leadminer scrape --source osm  # one source only, no export
leadminer stats                # summarise a CSV (no network)
leadminer validate             # data-quality gate, exits non-zero on failure
leadminer doctor               # check credentials and connectivity
leadminer score                # re-score an existing CSV with current rules
```

`stats`, `validate` and `score` never touch the network. They are what you
reach for when a run looks wrong.

## What it does

Scrapes three sources in parallel:
- **OpenStreetMap** — bulk Overpass API query for Lebanon businesses
- **Wikidata** — SPARQL query for Lebanon entities
- **Google Places** — Text Search API across 67 targeted queries (LB + KSA)

Then enriches the data:
- Checks website liveness, distinguishing **live**, **dead** (the server
  answered 4xx/5xx) and **unreachable** (DNS/TLS/timeout — we know nothing)
- Extracts email, Instagram, WhatsApp, and LinkedIn from live websites
- Infers Lebanese governorate or KSA city from address/coordinates
- Deduplicates across sources by phone number, then by (name, city)
- Tags each business with `industry_priority`, `recommended_service`, and `lead_score`

> The live/dead/unreachable distinction matters: an unreachable site is **not**
> a rebuild opportunity. Scoring it as one makes the crawler look like it is
> finding opportunities by being bad at its job.

## Output files (`data/`)

| File | Contents |
|------|----------|
| `all_businesses.csv` | Every business that passed the category whitelist |
| `qualified_businesses.csv` | Subset with at least one contact signal (completeness_score ≥ 1) |
| `with_websites.csv` | Businesses that have a website |
| `without_websites.csv` | Businesses with no website (new-site pitch targets) |
| `sales_ready.csv` | High/medium-priority industries with at least one contact channel |

All files are written atomically (temp file → `fsync` → `rename`) as UTF-8
with a BOM so Excel and Google Sheets render Arabic names correctly.

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
uv pip install --system -e ".[dev]"
```

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GOOGLE_PLACES_API_KEY` | Yes | Google Places API (New) key |
| `SCRAPER_EMAIL` | Recommended | Contact address sent in the User-Agent, which Overpass and Wikidata both ask for |

Enable **Places API (New)** in [Google Cloud Console](https://console.cloud.google.com/apis/library).

### Run locally

```bash
leadminer doctor      # check keys and connectivity first
leadminer run
```

### Tests

```bash
python3 -m unittest discover -s tests    # stdlib only, no install needed
```

## GitHub Actions

The workflow runs weekly on Monday at 03:00 UTC, and can be triggered manually
from the **Actions** tab → **Run workflow**.

Add `GOOGLE_PLACES_API_KEY` to **Settings → Secrets and variables → Actions**.

The job fails closed: if the master CSV cannot be downloaded from Drive, or the
output is suspiciously small, the run stops rather than uploading an empty or
truncated state over months of accumulated leads.

## Audits

`docs/audits/` contains ~100 independent review reports (correctness,
operations, data sources, cost, compliance) plus a synthesized, deduplicated
backlog in `083-SYNTH-s1-backlog.md`. `scripts/audit_status.py` summarises them.

## Architecture

```
cli.py               leadminer run | scrape | stats | validate | doctor | score
httpclient.py        shared HTTP: thread-local sessions, retry + jitter, body cap
main.py              pipeline orchestration and CSV export
├── scrapers/
│   ├── osm.py          # OpenStreetMap Overpass
│   ├── wikidata.py     # Wikidata SPARQL
│   ├── google_places.py # Google Places API
│   └── whitelist.py    # Category filter
├── dedup.py            # Phone-first deduplication + merge
├── enricher.py         # Site probe, contact extraction, lead scoring
└── pitch_recommender.py # Service recommendation logic
```
