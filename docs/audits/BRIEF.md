# leadminer — Audit Brief

**Read this first. Then do your assigned lens. Then write your report to the assigned file.**

## What this project is

`leadminer` (dir: `leadminer/`) is a B2B lead-generation scraper for **Lebanon** and
**Saudi Arabia** (Riyadh, Jeddah, Dammam). It exists to find businesses with weak or
absent digital presence, score them, and recommend which agency service to pitch.

Python 3.12. **1,402 LOC.** **Zero tests. Zero packaging. Zero CLI. Zero database.**

### Current architecture

```
main.py                 orchestration: scrape -> filter -> dedup -> enrich -> score -> 5 CSVs
├── scrapers/
│   ├── base.py         BusinessRecord TypedDict (22 keys), BaseScraper ABC
│   ├── osm.py          Overpass API, one giant POST, Lebanon only, node+way tags
│   ├── wikidata.py     SPARQL query for wdt:P17 wd:Q822 (Lebanon), LIMIT 5000
│   ├── google_places.py Places API v1 textSearch, 68 queries, 5 workers
│   └── whitelist.py    category allow-list (PRIORITY_INDUSTRIES / ADJACENT_BUSINESSES)
├── dedup.py            normalize_phone, normalize_name, _merge, dedup
├── enricher.py         infer_region, completeness_score, check_websites (40 threads),
│                       lead_score, enrich
└── pitch_recommender.py  recommend_service — hand-ordered if-chain
```

### Data flow

1. 3 scrapers run in parallel (`ThreadPoolExecutor`, 3 workers)
2. `whitelist.is_business_category()` filters geographic/vague noise
3. `dedup.dedup()` merges on normalized phone, else `(normalized_name, city)`
4. `enricher.enrich()` infers region, then does **1 HTTP GET per website** at 40 workers
   to check liveness and regex out email / instagram / whatsapp / linkedin
5. `industry_priority()` + `recommend_service()` + `lead_score()` tag each row
6. 5 CSVs written to `data/` (gitignored), uploaded to Drive via rclone

### Infrastructure

- `.github/workflows/scrape.yml` — `workflow_dispatch` only, **cron is commented out**,
  rclone to Google Drive, `timeout-minutes: 300`
- `requirements.txt` — 3 pins: `requests==2.32.3`, `beautifulsoup4==4.12.3`, `lxml==5.2.2`
- No lockfile, no hashes, no transitive pins
- `README.md` claims a monthly cron that does not actually exist in the workflow

### Output CSVs (the product)

| File | Contents |
|---|---|
| `all_businesses.csv` | cumulative master, all time |
| `qualified_businesses.csv` | `completeness_score >= 1` |
| `with_websites.csv` | has a website |
| `without_websites.csv` | no website — new-site pitch targets |
| `sales_ready.csv` | has a contact channel AND priority in (high, medium) |

### The 22 columns

`name, category, region, country, address, lat, lon, phone, email, website,
website_live, facebook, instagram, whatsapp, linkedin, rating, review_count,
completeness_score, lead_score, industry_priority, recommended_service, source,
scraped_at`

## Goal

Turn this script into a production-grade system. We are running ~400 agents across
audit → research → implementation → verification. You are one of them.

## Rules — read carefully

1. **Your assignment is your ONLY file.** Write to exactly the path given. Never write,
   edit, move, or delete any other file. Other agents are running concurrently.
2. **Do not run the scrapers.** No network calls to Overpass, Wikidata, Google, or any
   business website. You are auditing, not executing.
3. **Do not install packages.** No `pip install`, no `uv add`.
4. **Do not modify source.** `main.py`, `dedup.py`, `enricher.py`,
   `pitch_recommender.py`, and everything in `scrapers/` are read-only to you.
   Read them, quote them, critique them.
5. Web research is allowed and encouraged where your lens calls for it.

## How to write your report

Write **markdown**, aimed at an engineer who will act on it without re-deriving your work.
Concrete beats general. Every claim anchored to `file:line` or a citation.

```markdown
# <NNN> — <Short title>

## Verdict
Two or three sentences. The single most important thing.

## Findings

### S1 — <Title>
- **Where:** `enricher.py:124`
- **Breaks:** what goes wrong, concretely
- **Trigger:** a specific input that demonstrates it
- **Fix:** one line

### S2 — <Title>
...

## Not a bug, but worth knowing
- ...

## Recommended order of work
1. ...
```

Severity is a judgement call, so defend it:

| Level | Meaning |
|---|---|
| **S1** | Wrong results, silent data loss, or a run that dies. Blocks trust in the output. |
| **S2** | Materially degrades quality, cost, or operability at scale. |
| **S3** | Real but minor: maintainability, clarity, hygiene. |

**Do not pad.** A genuine report of 4 findings beats a forced 15. If your lens finds
the code fine, say so plainly and explain why — that is a valid and useful result.
Do not invent bugs to hit a quota.