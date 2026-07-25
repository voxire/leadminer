# leadminer patches - apply instructions

This folder contains four files plus a small set of edit instructions to upgrade leadminer from the current state to a sales-ready engine. Apply in the order below. Total time: 30 to 45 minutes.

## What changes

| Change | File | Type |
|---|---|---|
| Category whitelist module | `scrapers/whitelist.py` | NEW |
| Pitch recommender module | `pitch_recommender.py` | NEW |
| Google Places scraper with KSA support | `scrapers/google_places.py` | REPLACE |
| Main entry point with `sales_ready.csv` export | `main.py` | REPLACE |
| Optional: per-scraper inline whitelist | `scrapers/osm.py`, `scrapers/wikidata.py` | TINY EDIT |
| Set the missing GitHub secret | repo settings | CONFIG |

After this, `leadminer` will:
- Drop ~2,700 records of geographic noise (mountains, villages, places of worship, etc.)
- Add ~30+ KSA queries via Google Places API (Riyadh, Jeddah, Dammam)
- Add a 4th CSV `data/sales_ready.csv` with only actionable leads (phone OR email OR Instagram + priority/medium industry)
- Tag every record with a `recommended_service` column so helpers know what to pitch on first touch

## Step 1 - Set the GOOGLE_PLACES_API_KEY secret (5 min)

This is the single highest-leverage fix. Currently the Google Places scraper logs `"GOOGLE_PLACES_API_KEY not set, skipping"` on every run, so the best data source is contributing zero rows.

1. Go to Google Cloud Console: https://console.cloud.google.com
2. Create or select a project (e.g. `voxire-leadminer`)
3. Enable the Places API (New): https://console.cloud.google.com/marketplace/product/google/places.googleapis.com
4. APIs & Services > Credentials > Create credentials > API key
5. Restrict the key: API restrictions > Places API only
6. Set a budget cap: Billing > Budgets & alerts > $50/month with alerts at 50%, 80%, 100%
7. Copy the API key
8. Go to GitHub: https://github.com/voxire/leadminer/settings/secrets/actions
9. New repository secret: name `GOOGLE_PLACES_API_KEY`, value the API key
10. Done

Cost estimate: with the new query list (~65 queries, ~3 pages each, ~20 results per page) running weekly, expect ~$15 to $30/month at current Google Places pricing ($17 per 1,000 Text Search calls in tier 1).

## Step 2 - Copy the new files into leadminer (5 min)

From this folder, copy into the `leadminer` repo:

```bash
# From wherever this leadminer-patches folder lives
cd "/Users/abedamouneh/Development/Voxire Website/leadminer-patches"

# Adjust LEADMINER_PATH to your local clone of the repo
LEADMINER_PATH="$HOME/Development/leadminer"  # change if different

cp scrapers/whitelist.py        "$LEADMINER_PATH/scrapers/whitelist.py"
cp scrapers/google_places.py    "$LEADMINER_PATH/scrapers/google_places.py"
cp pitch_recommender.py         "$LEADMINER_PATH/pitch_recommender.py"
cp main.py                      "$LEADMINER_PATH/main.py"
```

## Step 3 - Optional inline filter on OSM and Wikidata scrapers (10 min)

The new `main.py` filters the combined output centrally, so this is optional. Doing it inside the scrapers saves a bit of memory and makes each scraper standalone-correct.

In `scrapers/osm.py`, find the `yield BusinessRecord(...)` line and wrap it:

```python
from .whitelist import is_business_category   # add this import at the top

# ...

# inside the loop, just before `yield BusinessRecord(...)`:
if not is_business_category(category):
    continue

yield BusinessRecord(
    name=name,
    category=category,
    # ... unchanged
)
```

Same change in `scrapers/wikidata.py`. Add the import at the top, add the 2-line guard before the yield.

## Step 4 - Sanity check locally (10 min)

```bash
cd "$LEADMINER_PATH"

# If GOOGLE_PLACES_API_KEY is set in Step 1, also export it locally:
export GOOGLE_PLACES_API_KEY="paste-the-key-here-or-skip"

python main.py
```

Expected output:
```
[OSMScraper] collected ~10000 records
[WikidataScraper] collected ~3000 records
[GooglePlacesScraper] collected ~1500 records (if key set, else 0)

Total raw records: ~14500
After whitelist filter: ~10000  (dropped ~4500)
After dedup: ~9500 unique businesses

Enriching records...

  Written: data/all_businesses.csv (~9500 records)
  Written: data/with_websites.csv (~2500 records)
  Written: data/without_websites.csv (~7000 records)
  Written: data/sales_ready.csv (~2000 records)

Summary:
  Total unique businesses : 9500
  With website            : 2500 (1900 live, 600 dead)
  Without website         : 7000
  With social media       : 800
  SALES-READY (actionable): 2000

  By region:
    Mount Lebanon        2800
    Beirut               2400
    Riyadh               700
    Jeddah               400
    ...

  Sales-ready by recommended service:
    Website rebuild + maintenance                          580
    Lead-gen overhaul (landing pages + Google Ads ...)     320
    SEO audit + visibility upgrade                         280
    Digital marketing retainer (Meta + Google + ...)       240
    ...
```

If something blows up, the scrapers print specific error lines. Most common: missing import (forgot to copy whitelist.py), or Google Places API key not enabled in the GCP project.

## Step 5 - Commit and push (5 min)

```bash
cd "$LEADMINER_PATH"

git checkout -b sales-ready-engine
git add scrapers/whitelist.py scrapers/google_places.py pitch_recommender.py main.py
# If you applied Step 3 inline filters:
git add scrapers/osm.py scrapers/wikidata.py
git status   # sanity check

git commit -m "Add category whitelist and KSA support"
git commit -m "Add pitch recommender module"   # split if you prefer atomic commits
git commit -m "Add sales_ready CSV export"

git push origin sales-ready-engine
# Open a PR on GitHub, review the diff, merge to main
```

The next scheduled run (next Monday 03:00 UTC) will then produce the new CSVs. To run immediately without waiting: GitHub > Actions > Scrape Lebanon Businesses > Run workflow.

## Step 6 - Wire it into the sales workflow (after RTYLR CRM exists)

For now, helpers pull `data/sales_ready.csv` manually from the repo every Monday. Per your decision: defer the auto-push until the RTYLR CRM module ships and can ingest the CSV directly via API.

In the meantime, the Monday weekly call (per Sales Team Operating Manual Section 06) should include a 2-minute item: "leadminer pulse - new sales-ready leads, breakdown by industry, who claims what."

## Files in this folder

```
leadminer-patches/
├── APPLY.md                          (this file)
├── main.py                           (REPLACE leadminer/main.py)
├── pitch_recommender.py              (NEW - drop in repo root)
└── scrapers/
    ├── google_places.py              (REPLACE leadminer/scrapers/google_places.py)
    └── whitelist.py                  (NEW - drop in leadminer/scrapers/)
```

## What this does NOT do (yet)

- Does not auto-push leads to WhatsApp/Slack. Deferred to RTYLR CRM integration.
- Does not scrape Instagram bios. Worth adding as a 4th source later (huge for Lebanon F&B).
- Does not scrape UAE/Qatar. Add by extending KSA_QUERIES with `Dubai` / `Doha` queries when ready.
- Does not check email deliverability. Add Hunter.io API call in `enrich()` when budget allows.
- Does not run AI categorization on Google `types`. The current `_pick_category` is deterministic. Good enough for v1.

## Quick troubleshooting

| Symptom | Fix |
|---|---|
| `[Google] GOOGLE_PLACES_API_KEY not set, skipping.` | Step 1 - set the secret |
| `ModuleNotFoundError: pitch_recommender` | Make sure you copied `pitch_recommender.py` to repo root, not into `scrapers/` |
| `ImportError: from .whitelist import ...` | Make sure you copied `whitelist.py` into `scrapers/`, not repo root |
| Whitelist drops too many real businesses | Edit `scrapers/whitelist.py` - add the missing categories to `ADJACENT_BUSINESSES` |
| Wrong recommended service for a vertical | Edit `pitch_recommender.py` - tune the rules or add a new tier |
