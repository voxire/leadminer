# 041 — Run-over-run anomaly detection

## Verdict

The system cannot detect its most damaging failure mode because it **persists no
metrics at all**: every count it computes is a `print()` to ephemeral CI stdout
(`main.py:110,115,119,126,158-180`; `enricher.py:209-219`; `osm.py:56`;
`wikidata.py:59`; `google_places.py:225`). A run that silently returns 500 raw
records instead of 50,000 produces five valid-looking CSVs, exits 0, and uploads
them over the master (`scrape.yml:46-50`), with nothing to compare against and no
failure. The single most important change is to **write a per-run metrics record
to a file that flows through the existing rclone `*.csv` path, and fail the job
when a source's raw count collapses below a floor derived from prior runs.**

## Run-health checks (spec)

The premise of run-over-run detection is a **stored baseline**. Define the
baseline for each metric as the **median of the last 3 successful runs** (fallback:
the previous run if fewer than 3 exist; the first run only bootstraps and never
alerts). Compare each metric to its baseline on two tiers — **WARN** (investigate)
and **BLOCK** (fail the job / block upload). The five checks below are the ones the
brief requires.

| # | Metric | Computed from | BLOCK | WARN |
|---|---|---|---|---|
| **C1 — Volume** | Net-new records added this run = `len(records) − len(master)` | `main.py:124-126` | `0` when baseline > 0, or `< 10%` of baseline | `< 25%` of baseline |
| **C2 — Per-source raw count** | `len(batch)` per scraper, before whitelist/dedup | `main.py:103,110` | source yields `0` when baseline > 0, or `< 25%` of baseline | `< 50%` of baseline |
| **C3 — Dedup rate** | New-record absorption = `1 − net_new / len(raw_filtered)` | `main.py:117-126` | `> 99.9%` (stale/replay) **or** `< 10%` (identity model broke) | `> 99%` or `< 25%` |
| **C4 — Contact-extraction rate** | `email_count / sites_fetched` (also instagram/whatsapp/linkedin) | `enricher.py:209-219` | `< 2%` absolute while baseline ≥ 5% | `< 50%` of baseline |
| **C5 — Dead-site rate** | `dead / (live + dead)` over records **with** a website | `enricher.py:209-210` | `> 60%` absolute **and** `> 2×` baseline | `> 2×` baseline |

The brief's 500-vs-50,000 example is a **99% collapse**, which trips **C1 and C2
BLOCK** (both `< 10%` / `0`) — precisely the failure this design exists to catch.

Two coupling caveats, so the thresholds don't false-positive at scale:

- **C1 decays as the market saturates.** Lebanon has ~50k businesses total; once
  the master covers them, net-new falls toward 0 on *healthy* runs. **C2 (raw
  volume) is therefore the primary signal** — raw scrape volume stays roughly flat
  regardless of how much is already in the master — and C1 is a secondary guard.
  Interpret a C1 BLOCK only when C2 is also healthy but nothing new landed.
- **C3 and C5 both read through the fetcher.** C3 `> 99.9%` is a "market fully
  crawled" result only when C2 is healthy; combined with a C2 collapse it means the
  sources are replaying stale data. C5's dead-site rate doubles as a fetch-health
  signal: blocked fetches are recorded as `website_live = False`
  (`enricher.py:151-152`), so a dead-site spike is more likely "we got IP-blocked"
  than "the whole Lebanese web died" — see `073-cost-model.md` S2.

## Findings

### S1 — No metrics are persisted; there is no baseline to compare against

- **Where:** `main.py:110-180` (all counts printed, none stored); the summary block
  `main.py:158-180`; scraper prints `osm.py:56`, `wikidata.py:59`,
  `google_places.py:225`.
- **Breaks:** The entire anomaly-detection premise — "compare this run to last
  month" — has no substrate. CI stdout is discarded when the job ends. There is no
  `run_metrics.json`, no history CSV, no place the previous run's raw volume,
  per-source counts, dedup rate, contact rate, or dead-site rate are written. So
  "500 records vs 50,000 last month" is undetectable by construction, even if a
  human wanted to look.
- **Trigger:** Any run; the comparison is simply never performed.
- **Fix:** At the end of `main()`, write one metrics record to a **`.csv`-named**
  history file (e.g. `data/run_history.csv`) with columns
  `run_id, scraped_at, master_before, osm_raw, wikidata_raw, google_raw,
  raw_filtered, net_new, dedup_rate, sites_fetched, live, dead, email_count,
  instagram_count, whatsapp_count, linkedin_count, qualified, sales_ready`. Name it
  `.csv` so it flows through the existing `rclone ... --include "*.csv"` download
  (`scrape.yml:38`) and upload (`scrape.yml:50`) without touching the workflow
  include filters. Load the prior rows at startup and compute the WARN/BLOCK
  deltas in-process.

### S2 — Scraper failure is indistinguishable from "no results", and the run still exits 0

- **Where:** `osm.py:52-53` (all retries exhausted → bare `return`),
  `wikidata.py:55-56` (same), `google_places.py:159-160` (missing key → `return`),
  `google_places.py:210-220` (401/429/error → empty `records` or silent `continue`),
  and `main.py:108-113` (any scraper exception caught, printed to stderr, ignored).
- **Breaks:** Every failure mode that produces the "500 records" scenario returns
  an empty or partial iterator with **no status flag and no non-zero exit**:
  Overpass timeout → 0, Wikidata SPARQL timeout → 0, missing/invalid Google key →
  0, Google 429 on a query → that query contributes 0. The workflow then uploads
  the (empty or shrunken) CSVs and reports success (`scrape.yml:40-50`). The
  cumulative master from `load_master` (`main.py:95`) is replaced on Drive by
  whatever little survived, compounding the loss (see `013-main-state-idempotency.md`).
- **Trigger:** An expired `GOOGLE_PLACES_API_KEY`, a transient Overpass outage, or
  a network partition during the run.
- **Fix:** Give each scraper a tri-state result — `ok` / `empty` / `error` — and a
  raw count. In `main()`, treat `empty`-or-`error` on a source whose baseline was
  non-zero as a BLOCK: exit non-zero (or skip the Drive upload step) so the job
  fails and GitHub notifies, rather than silently shrinking the master.

### S3 — The cumulative master hides run-over-run volume

- **Where:** `main.py:95` (load cumulative master), `main.py:124`
  (`combined = raw_filtered + master`), `main.py:125` (`records = dedup(combined)`).
- **Breaks:** `all_businesses.csv` is monotonically cumulative, so its row count
  *cannot* reveal a collapsed scrape: 50,000 last month plus 500 raw this month
  still reads ~50,400 rows and looks healthy. The only quantities that are
  run-over-run comparable — per-source raw counts and net-new — are never computed
  as first-class values, and cannot be reconstructed from the persisted CSVs
  because `dedup` collapses raw rows into master rows and keeps only
  `max(scraped_at)` per merged record (`dedup.py:57-58,64-97`). Retroactive
  recovery of "how many records did Google return in October" is impossible from
  today's artifacts.
- **Trigger:** Any reduced-but-non-zero scrape; invisible in the cumulative count.
- **Fix:** Compute and store `net_new = len(records) − len(master)` and the three
  per-source raw counts on every run (the same `run_history.csv` from S1), and key
  C1/C2 on those, not on the cumulative row count.

## Not a bug, but worth knowing

- **No alerting channel exists.** Even with metrics stored, nothing reads them. The
  minimal mechanism is a `scripts/check_run_health.py` step run after `main.py` in
  `scrape.yml` that exits non-zero on any BLOCK; GitHub Actions failure
  notifications then become the alert. A Slack/Drive webhook is a later nicety.
- **Google Places is the only paid and only KSA source, so it deserves finer
  granularity.** The scraper prints per-query results (`google_places.py:225`) and
  splits LB vs KSA queries (`google_places.py:163-168`), but a single-market or
  single-query collapse (e.g. "fintech startups in Saudi Arabia" returning 0 after
  an API change) is lost in the aggregate `len(batch)`. Record the LB/SA split and
  the per-query mean as sub-metrics under C2.
- **Google has a hard ceiling, so its baseline is bounded.** Text Search (New) is
  capped at 60 results/query (see `073-cost-model.md`), so a healthy Google raw
  count can never exceed ~4,000 no matter what; C2 baselines must be per-source,
  not a single shared threshold.
- **The derived files are free early-warning signals.** `qualified_businesses.csv`
  and `sales_ready.csv` counts (`main.py:139-146`) move *downstream* of every other
  stage, so a drop there that is not explained by C1/C2 points at the whitelist,
  enrichment, or scoring, not the scrape. Worth logging them in the history file
  even though they are not one of the five primary checks.
- **First-run bootstrap has no baseline.** The very first `run_history.csv` row
  can only seed the baseline; alerts are possible from run 2 onward. This is fine,
  but it means the system must be allowed to run twice before it can protect itself.

## Recommended order of work

1. **S1** — Add the `data/run_history.csv` metrics record to `main()` (`.csv` name
   so it rides the existing rclone `*.csv` path at `scrape.yml:38,50`). This is the
   prerequisite for everything else.
2. **S2** — Make scrapers return `ok`/`empty`/`error` + raw count, and make `main()`
   exit non-zero (or skip upload) when a previously-nonzero source yields 0.
3. **S3** — Compute and store `net_new` and per-source raw counts, and implement the
   C1/C2 BLOCK/WARN comparison against the baseline median.
4. Add `scripts/check_run_health.py` as a post-scrape workflow step that fails the
   job on any BLOCK, giving a notification path with zero new infrastructure.
5. Add the C3/C4/C5 checks (dedup absorption, contact-extraction rate, dead-site
   rate) with the thresholds above, plus the Google LB/SA split and per-query mean
   as sub-metrics.
