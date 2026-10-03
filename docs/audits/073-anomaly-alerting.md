# 073 — Pipeline anomaly alerting

## Verdict

The pipeline has useful log counts, but no health contract: unusually small successful responses, skipped sources, and quality collapses do not fail the run or notify anyone. Treat every run as a measured data product: persist per-run stage/source metrics, compare them to robust same-weekday baselines, and deliver actionable alerts to Slack while making critical data-integrity failures fail the GitHub Actions run.

## Findings

### S1 — A 500-row scrape is accepted exactly like a 50,000-row scrape
- **Where:** `main.py:157-181`, `.github/workflows/scrape.yml:40-50`
- **Breaks:** There is no expected-volume check or comparison with prior runs. A scraper that returns 500 rows successfully is logged at `main.py:164-165`, then processed, written, and uploaded. The existing master is combined with the new batch before deduplication (`main.py:178-181`), so old rows can make the resulting CSV look healthy while this run contributed almost nothing. If the process exits zero, Actions reports success. The workflow's schedule is commented out and only `workflow_dispatch` is enabled (`scrape.yml:3-6`), so there is also no automatic run or missed-run signal today.
- **Trigger:** An API query that previously returns 50,000 results starts returning 500 with HTTP 200. No exception is raised, all later steps complete, and no alert is generated; the cumulative output can still contain the old records.
- **Fix:** Record *fresh-run* counts before mixing in the master, validate them against source/stage baselines, and alert/fail on collapse; add an enabled schedule plus a watchdog for missing expected runs.

### S1 — Source errors are logged, then downgraded to a successful partial run
- **Where:** `main.py:160-168`; `scrapers/osm.py:38-53`; `scrapers/wikidata.py:40-56`; `scrapers/google_places.py:175-188, 208-220`
- **Breaks:** `main()` catches any scraper future exception, prints an error, and continues. OSM and Wikidata return an empty iterator after exhausting retries; Google can skip when the key is absent, return partial query results after request errors, or raise from one query and lose its whole batch. None of these outcomes is represented in a run status or causes the workflow to fail. The upload step still runs after `python main.py` (`scrape.yml:40-50`).
- **Trigger:** Overpass exhausts its retries while Google and Wikidata succeed. The run writes and uploads outputs as if complete, without an explicit degraded status or notification.
- **Fix:** Have each source/query report `success | partial | failed | skipped` and counts; classify required-source failures as critical, publish a run manifest, and make critical partial runs fail the job before replacing canonical outputs.

### S1 — No persistent stage metrics means quality regressions are invisible
- **Where:** `main.py:170-181, 199-217`; `enricher.py:217-268`
- **Breaks:** The program prints raw, post-whitelist, and post-dedup totals but never evaluates them or persists them by run/source. Website enrichment prints live/dead/unknown and total contact-field counts, but not rates or fresh-record-only extraction yield. The master merge makes aggregate CSV size unsuitable as a proxy for this run's health. There is no metric for duplicate fraction, source-specific zero, or duration.
- **Trigger:** A source starts returning mostly duplicate records, or the website fetcher starts finding no contacts. Existing counts may still be nonzero, and no configured alert can identify the change.
- **Fix:** Emit one machine-readable manifest per run with timestamps, source/query outcomes, fresh input and output counts at every stage, website outcomes, extraction denominators/numerators, and duration; never derive run health from cumulative master totals.

### S2 — Alert thresholds must separate real regressions from normal weekly variation
- **Where:** `.github/workflows/scrape.yml:3-6`; current counts are unbaselined logs at `main.py:164-181` and `enricher.py:256-267`.
- **Breaks:** A fixed expected row count will be noisy as source coverage, query mix, holidays, and weekly demand vary. Conversely, a static low floor can miss a major collapse from a high normal level. Current scheduling is manual-only, so there is no stable cadence on which to build a meaningful baseline.
- **Trigger:** A seasonally lower but healthy scrape trips an absolute floor, or a 99% collapse remains above a permissive floor and passes unnoticed.
- **Fix:** Once the intended weekly schedule is enabled, compare each metric to its own source/market/query-family and same-weekday history: use the median of the last 8 successful comparable runs and robust spread `s = 1.4826 × median(|x − median(x)|)` (MAD). Require at least 4 comparable runs for a provisional baseline and 8 for normal alerting. Alert on the relative and robust-deviation rules below, with stated minimum sample/floor guards; if MAD is zero, use the relative rule. Exclude failed/partial runs and tag baselines with query/config version so intentional query changes do not poison comparisons.

## Alert specification

Metrics must describe the *current scrape batch*, before merging with `all_businesses.csv`. Store numerator and denominator, not only percentages. Establish baselines separately for each source and country/market where applicable; evaluate the combined total as well. Thresholds below are initial operating defaults, to be tuned from the first 8–12 successful weekly runs.

| Signal | Measure | Warning | Critical / action |
|---|---|---|---|
| **Volume collapse** | Fresh raw results and post-whitelist records, overall and per source/market | Below 70% of the same-weekday 8-run median **or** below median − 3 robust spreads; only compare where baseline median is at least 100 records. | Below 40% of median **or** below median − 5 robust spreads; zero is always critical. A 500 vs 50,000 result is 1% of baseline and therefore critical. Pause canonical-output promotion pending investigation. |
| **Per-source zero** | Expected source's returned records and completed query count | None: zero is not a normal fluctuation for an enabled, required source. | Any required source returns zero, is skipped (including missing credentials), or has zero successful queries. Mark the run partial and fail the job. For Google, retain per-query status/counts so one failed query is visible even when the rest return records. |
| **Dedup-rate spike** | On this run's post-whitelist rows only: `1 − (new unique / input rows)`; measure before adding historical master. | At `n ≥ 100`, dedup rate exceeds its baseline by 10 percentage points **and** is over 1.5× baseline. | At `n ≥ 100`, exceeds baseline by 20 points or is over 2× baseline; inspect source/query composition and source IDs. Do not count the expected overlap with the existing master as this signal. |
| **Dead-site-rate spike** | `DEAD / (LIVE + DEAD)` over fresh records with websites; track `UNKNOWN` separately | At least 100 determinate website checks, and dead rate is ≥10 percentage points above baseline **and** >1.5× baseline. | ≥20 points above baseline or >2× baseline. Also alert on a simultaneous `UNKNOWN` spike as a likely network/blocking incident, not as evidence that businesses' sites are dead. Below 30 determinate checks, show counts but suppress rate alerts; at 30–99, alert only on an extreme increase (≥30 points). |
| **Extraction-rate collapse** | Fresh live websites yielding at least one newly extracted email/Instagram/WhatsApp/LinkedIn, divided by fresh `LIVE` sites checked; also retain per-field yields | With at least 100 live sites, yield is below 70% of baseline **and** at least 10 points lower. | Below 50% of baseline or at least 20 points lower. Track pre-existing contacts separately: counting them as newly extracted would mask extractor failures. |
| **Run-duration spike** | End-to-end job duration and stage durations | >1.5× same-weekday median or > median + 3 robust spreads. | >2× median, >240 minutes (leaves less than an hour before the configured 300-minute timeout), or timeout/failure. Report the slowest stage so the alert is actionable. |
| **Partial failure / missed run** | Required source/query status, run final status, expected-run heartbeat | Any optional query/source partial result is a warning with affected count and query names. | Any required source failure/skip, an incomplete output/upload, nonzero job exit, or missed scheduled run is critical. If a scheduled run is expected weekly, alert when no successful run has completed within 8 days of the prior expected window. |

For rate thresholds with a nonzero baseline, apply the ratio and percentage-point rules shown; for a zero/near-zero baseline, use an absolute percentage-point increase and a minimum denominator rather than divide by zero. Volume uses both a relative floor and MAD because the first detects large proportional collapses and the second catches unusually low values against a stable baseline. If the baseline has fewer than 4 comparable successes, use only the hard checks (zero required source, job failure/timeout, and a conservative absolute floor); label the baseline “warming up” rather than claiming statistical confidence.

## Delivery and response

- **Primary channel:** Send one deduplicated Slack message to `#pipeline-alerts` from a GitHub Actions notification step using a repository secret such as `SLACK_WEBHOOK_URL`. Include severity, run link, source/query affected, actual vs baseline, numerator/denominator, and the recommended response. Send after metrics are available and also on job failure (`if: always()`), so a crashed pipeline does not suppress its alert.
- **System of record:** Add the same compact metric table and manifest location to the GitHub Actions run summary; retain the manifest as an artifact for trend analysis. Critical alerts fail the workflow and block promotion/upload of the canonical CSVs; warnings keep the run visible without paging. A failed webhook should itself fail or visibly warn in the Actions summary, not silently disappear.
- **Ownership:** Route critical messages to the on-call/maintainer group with a named responder and acknowledgement expectation. Slack is the operational notification; GitHub's native failed-workflow notification is useful backup, but is insufficient by itself because source failures currently do not fail the workflow.

## Not a bug, but worth knowing

- A successful 200 response with an empty or drastically shortened result set is not an HTTP/request failure. The scraper logs whatever count it receives (`osm.py:55-56`, `wikidata.py:58-59`, `google_places.py:222-225`); only data-volume expectations or historical comparisons can detect a semantically bad success.
- `enricher.py:174-176` classifies all HTTP 4xx/5xx responses as dead websites, while transport errors become unknown (`enricher.py:167-170`). Keep `UNKNOWN` separate and annotate a dead-rate alert with status-class counts so a widespread remote outage or 5xx wave is not misread as a sudden business-quality change.
- The workflow has a 300-minute timeout (`scrape.yml:11`), but without duration telemetry the timeout is only a final failure, not an early warning.

## Recommended order of work

1. Define and enable the expected cadence; add a missed-run heartbeat because the current workflow is manual-only (`scrape.yml:3-6`).
2. Add per-source/per-query status and fresh-batch counters, stage rates, and run/stage durations; persist a manifest before any master merge.
3. Enforce hard checks first: required-source zero/skip, partial failure, output validation, and the 300-minute budget. Prevent critical partial runs from replacing canonical outputs.
4. Collect 8–12 successful weekly manifests, then enable robust same-weekday baselines and the relative/MAD thresholds in the table; keep minimum denominators and configuration-version boundaries.
5. Deliver warning/critical alerts to Slack and summarize metrics in Actions; verify notifications with a deliberate test fixture, never by running production scrapers against external sources.
