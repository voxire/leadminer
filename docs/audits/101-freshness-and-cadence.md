# 101 — Freshness and source cadence

## Verdict

There is no scheduled run today: the workflow exposes only manual dispatch, while the README promises monthly automation. Even after enabling a cron, one cadence for all three sources is the wrong operating model for an actionable lead list: Places and website status need frequent refresh, while OSM and Wikidata are better treated as lower-frequency discovery feeds. Add lifecycle state and outreach suppression before treating the cumulative CSV as a current sales queue.

## Findings

### S1 — The advertised monthly refresh never runs
- **Where:** `.github/workflows/scrape.yml:3-6`; `README.md:5,74-77`
- **Breaks:** `schedule` is commented out and only `workflow_dispatch` is active. README says monthly on the 1st, while the commented workflow example says every Monday. Unless a person dispatches it, records and website checks age indefinitely; consumers are told a freshness guarantee that does not exist.
- **Trigger:** Leave the repository untouched for a month: GitHub Actions performs no scrape, even though the README says it ran.
- **Fix:** Choose and enable the actual schedule below, then make README state the per-source schedule and what a successful refresh means.

### S2 — A single full scrape is wasteful for slow feeds and insufficient for lead freshness
- **Where:** `main.py:154-168,178-187`; `scrapers/google_places.py:44-124,157-188`; `scrapers/osm.py:10-26,38-56`; `scrapers/wikidata.py:10-25,40-59`
- **Breaks:** Every invocation runs every source, then re-enriches the full cumulative master. Places fans out across 68 text queries, OSM asks Overpass for a large Lebanon-wide extract, and Wikidata executes a broad Lebanon query capped at 5,000 results. A monthly all-source run under-polls the fast-changing lead signals but needlessly repeats lower-change discovery. `main.py:183-187` also checks every stored website on each run, regardless of when it was last verified.
- **Trigger:** A business repairs its previously dead site or is contacted the day after the monthly run. The lead can remain attractive in the export until the next run; conversely, all three APIs and every stored website are still hit again just to discover slowly changing entity facts.
- **Fix:** Decouple scheduled jobs and persist source-specific observation/verification timestamps. Recommended starting cadence (tune from observed yield, API spend, and source reliability):
  - **Google Places — weekly discovery; refresh active, unsuppressed leads at least every 30 days.** Its listing, contact, rating, review-count, and website signals are the most operationally volatile and are directly useful for prioritizing outreach. Weekly discovery gives a reasonable chance to find recent listings without paying for a daily full sweep; split queries into priority batches if quota or cost requires it. Do not interpret a missing text-search result as proof the business closed.
  - **OSM — monthly discovery/diff.** Business tags and contact details are community-maintained and change less quickly than the sales state; a monthly regional pull is a proportionate baseline for new/changed candidates. Do not poll the public Overpass endpoint weekly with the current giant query; it is a shared service, and this extract does not currently preserve OSM element IDs or edit timestamps for reliable incremental polling.
  - **Wikidata — quarterly discovery/refresh.** These curated entity facts are comparatively slow-moving and supplementary for local prospecting. Quarterly is adequate for discovery; use a monthly run only if measured new, in-scope lead yield justifies the query and its 5,000-row cap is addressed. Wikidata currently returns Lebanon only, so this cadence does not fill the KSA coverage gap.
  - **Website verification — before each sales handoff, and every 14 days for active, not-yet-contacted leads.** This is a separate freshness clock, not a reason to re-run every directory query. Recheck sooner after a contact/reply or if a website/contact field changes. Distinguish `unknown` transport failures from `dead`; `enricher.py:142-164` already documents why a failed fetch is not evidence of a broken site.

### S2 — Contacted prospects have no suppression lifecycle
- **Where:** `main.py:33-40,199-213`; `scrapers/base.py:5-28`
- **Breaks:** The row schema has source and scrape time but no stable lead lifecycle, contacted timestamp, attempt count, response, or do-not-contact state. `sales_ready.csv` is recomputed from contact availability and industry priority alone, so an already-pitched business can be exported again on every run. A cumulative master CSV is not a contact-management system.
- **Trigger:** A salesperson pitches a lead, then a later weekly Places refresh still finds its phone and high-priority category; it remains in `sales_ready.csv` with no indication it was already contacted.
- **Fix:** Add stable lead identity and an outreach ledger (`status`, `last_contacted_at`, `attempt_count`, outcome, and explicit opt-out/do-not-contact). Apply suppression when materializing all prospecting exports, not by deleting the record. Default: suppress from new-lead queues immediately after any recorded outreach; keep positive reply, active conversation, customer, and opt-out suppressed until an explicit human status change (opt-out is durable); optionally recycle a no-response lead only after a deliberate cooldown (e.g. 90 days), a fresh verification, and a capped attempt policy. Preserve suppression across dedup/source merges.

### S2 — Scores and active-list membership lack freshness semantics
- **Where:** `main.py:178-206`; `dedup.py:83-99`; `enricher.py:217-268,275-311`; `main.py:208-213`
- **Breaks:** Scores are recomputed after each invocation, but there is no score version/as-of field or trigger tied to a material change, and the same CSV master is also the all-time archive. `scraped_at` is merged as the latest row timestamp (`dedup.py:95-99`); it does not say when an individual source last confirmed the business. Records are never removed from the cumulative master when absent from a later result, so an old lead can look current in active exports.
- **Trigger:** A website changes from dead to live, a phone changes, or rating/review count changes; a previously contacted lead becomes eligible again if its status is not separately persisted; a stale lead remains in `without_websites.csv` indefinitely.
- **Fix:** Store per-source `first_seen_at` / `last_seen_at`, `last_verified_at`, score inputs/version and lifecycle status. Re-score immediately on a material verified change (website URL/live transition, phone/email/social change, location/category/priority change, rating/review-count update, or new corroborating source), on recorded outreach outcomes, and whenever the scoring model version changes. Recompute the active export from current state after each trigger; include `score_as_of` and an explicit active/suppressed/stale state.

## Not a bug, but worth knowing

- Do not infer closure from a record missing from one OSM, Wikidata, or Places response: query coverage and API failures can make absence noisy. Record whether that source's poll completed successfully and only advance absence-based retirement after successful observations.
- Keep the all-time master as an archive, but separate it from the current prospecting queue. A practical starting policy is mark unverified leads stale (exclude from sales-ready/new-lead exports) after 90 days without a successful business/site verification, then retire them from active use after 180 days without positive confirmation. Keep archived rows and history; re-activate only on a fresh observation. For source-specific evidence, require at least two successful scheduled misses before classifying an OSM/Wikidata sighting as absent, and treat Places text-search absence as weak evidence rather than closure.
- Track run-level completion and per-source successful poll time. A failed or partial source run must not advance freshness/absence clocks or overwrite a good prior snapshot as though it were current.

## Recommended order of work

1. Enable one real schedule and align README claims with it; alert on failed or partial runs.
2. Add stable lead identity, source-level observation timestamps, website verification time, and persisted outreach suppression before generating sales queues.
3. Separate weekly Places discovery, monthly OSM, quarterly Wikidata, and targeted website revalidation so each source has an explicit cost and freshness budget.
4. Re-score on verified input/outreach/model changes; age stale leads out of active exports without deleting historical records.
5. Review actual new-lead yield, stale/closed rate, outreach conversion, and API cost after 6–8 weeks; adjust cadences using those measures rather than assuming monthly means fresh.
