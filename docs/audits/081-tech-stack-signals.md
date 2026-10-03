# 081 — Fingerprinting digital maturity: which signals predict agency purchase intent

## Verdict

The pipeline already captures the two strongest intent signals — *dead website* (`pitch_recommender.py:57-58`) and *no website + social presence* (`pitch_recommender.py:65-66`) — but it treats every **live** website identically, routing all of them to one SEO pitch (`pitch_recommender.py:70-71`). That live branch is where the remaining money is, and the signals that would split it (which CMS, analytics/pixel presence, SEO tags, mobile-friendliness, HTTPS) are already sitting in HTML the enricher downloads and then throws away (`enricher.py:180-214`). The correct ranking is counterintuitive: the generic booleans "CMS present / HTTPS / page speed" are near-useless for predicting *who buys*, because they are either saturated or confounded; what actually predicts intent is **evidence of prior paid investment** (a real multi-page site on a specific CMS vs a free-builder one-pager) and **evidence of active marketing spend** (an ad pixel or tag manager). Both are extractable for $0 from the HTML already in hand. Wappalyzer/BuiltWith are only worth paying for coverage beyond reachable HTML; PSI/CrUX/HTTPArchive are free backfill, not per-lead enrichment.

## Findings

### S1 — The live-website branch discards the highest-predictive signals
- **Where:** `enricher.py:180-214`, consumed by `pitch_recommender.py:70-71`
- **Breaks:** `_fetch_website` reads up to 200 KB of HTML (`enricher.py:180-181`), regexes four contact fields out of it (`enricher.py:191-214`), then `return LIVE, contacts` (`enricher.py:214`) discards the document. CMS generator tag, analytics snippets, `<title>`/`<meta name="description">`, viewport, and `https://` are all in that string and all vanish. Downstream, `recommend_service` has no material to distinguish a 2015 WordPress site, a Wix free-tier one-pager, and a brand-new Shopify store — every live site with `< 20` reviews returns `"SEO audit + visibility upgrade"` (`pitch_recommender.py:70-71`), and every "established" site returns `"Digital marketing retainer"` (`pitch_recommender.py:95-96`).
- **Trigger:** a live site running a Meta Pixel + Google Ads (the strongest buy signal available) scores and pitches identically to a static brochure page.
- **Fix:** in `_fetch_website`, after decoding `html`, extract a `tech` dict (generator meta, analytics/tag-manager/pixel signatures, has-viewport, title/description present, scheme) and return it alongside `contacts`. No new HTTP request.

### S2 — "CMS present" is a saturated boolean; the predictive value is in *which* CMS and *staleness*, not presence
- **Where:** the proposed signal, not yet collected anywhere (no column exists in `main.py:33-40`)
- **Breaks:** "does a CMS exist" barely discriminates — essentially every live site is built on *some* builder or CMS. The decision-relevant distinctions are: WordPress vs Shopify vs Wix vs Squarespace vs hand-rolled, and how stale the build is (old theme, old jQuery, no HTTP/2). A stale WordPress is a **rebuild + security + SEO** pitch; a Wix free-tier site is a **migrate-off-free-plan** pitch; a fresh Shopify is an **e-commerce retainer**. These map to different services and different budgets, so conflating them under one "SEO audit" label loses the sale.
- **Trigger:** the code already knows about `wixpress.com` as a *noise domain* (`enricher.py:137`, `_EMAIL_BLACKLIST`), proving Wix sites are present in the dataset — yet no "builder = wix" signal is recorded.
- **Fix:** detect the generator/CMS from `<meta name="generator">`, `wp-content`, `wp-json`, `cdn.shopify.com`, `static.wixstatic.com`, `squarespace`, etc. Record the **name**, not a boolean.

### S3 — Analytics / pixel presence is the single strongest intent signal, and it is free to detect
- **Where:** proposed addition to `_fetch_website`
- **Breaks:** a Google Tag Manager, GA4 (`gtag.js` / `googletagmanager.com`), Meta Pixel (`connect.facebook.net/.../fbevents.js`), TikTok, or Google Ads (`googleadservices`) snippet is direct evidence the business already **pays for traffic and measures ROI**. That is the highest-conversion prospect for marketing retainers and analytics/SEO services. The absence of any analytics is itself a pitch ("you are flying blind"). Right now neither presence nor absence is captured, so the strongest marketing-lead signal is invisible to `lead_score` and `recommend_service`.
- **Trigger:** two sites, identical in every current field except one has a Meta Pixel and one has nothing; the pipeline cannot tell them apart.
- **Fix:** match a small set of known snippet signatures in `html` and record `has_analytics` / `has_ad_pixel`.

### S4 — Do not pay per-lookup Wappalyzer/BuiltWith at leadminer's scale; the free tier covers the live-site subset
- **Where:** enrichment architecture decision, not a code defect
- **Breaks:** leadminer enriches thousands of sites per run (one GET per website, 40 workers, `enricher.py:217`). Real costs of the per-lookup vendors make full-corpus enrichment uneconomical:

| Tool | Free tier | Paid (per month) | Cost at leadminer scale |
|---|---|---|---|
| **Wappalyzer** | 50 lookups/mo + 50 email verifications/mo (account); unlimited manual lookups via browser extension | Pro $250 (5,000 API credits, 1 user); Business $450 (unlimited UI lookups, 20,000 API credits); Enterprise $850+ (200,000+ credits) | ~1 credit = 1 lookup; a single full-country run of the live subset can exceed 20,000 lookups, i.e. burn a full Business month in one scrape |
| **BuiltWith** | Individual site lookups free **forever** (web UI) | Basic $295 (2 techs); Pro $495 (unlimited); Team $995 (unlimited + logins). API (`api.builtwith.com`) is a separate sales-quoted product | Same problem: per-lookup API access is not on the $295 tier; bulk domain lookups are a separate data fee |
| **HTTPArchive** | Free — public BigQuery dataset, monthly Lighthouse crawl of the CrUX popular-URL list | Only BigQuery query cost beyond the 1 TB/mo free tier (these queries are tiny, MBs) | Free, but coverage-limited (see S5) |
| **PageSpeed Insights API** | Free — API key required for automation; default ~25,000 req/day/project | No paid tier (quota increase by request) | Free; one call returns Lighthouse lab (perf/a11y/best-practices/SEO) + CrUX field data |
| **CrUX API** | Free — 150 req/min/project, **not purchasable** | None | Free; one origin *or* URL per call, field data only |

- **Trigger:** a naive plan to "enrich every record with Wappalyzer" lands at $450/mo and still throttles mid-run; the same signals are free from HTML already fetched.
- **Fix:** extract CMS/analytics/SEO/HTTPS/mobile from the HTML in-process (S1–S3). Use the **free PSI API** (or CrUX API) only for the `with_websites.csv` live subset to get mobile-friendliness + page-speed + SEO-lab scores in one call. Reserve paid Wappalyzer/BuiltWith solely for records whose HTML you could not reach (`website_live is None`), and even then, Wappalyzer's open-source detection rules (the `apps.json` fingerprints it licenses) can be run locally.

### S5 — HTTPArchive is free but only covers the popular-web long tail; most Lebanon/KSA SMB leads will not be in it
- **Where:** sourcing decision
- **Breaks:** HTTPArchive crawls the URLs in the Chrome UX Report — the *popular* millions of sites — monthly, via WebPageTest + Lighthouse, and detects 1,000+ technologies using Wappalyzer itself (its "lens" feature). It is a public BigQuery dataset, effectively free to query. But a small Beirut restaurant, a Riyadh clinic, or a Dammam contractor's three-page site is not in the CrUX list, so `httparchive` lookups will return nothing for exactly the long-tail leads this project exists to find. Treat HTTPArchive as a **benchmark/baseline source** (e.g., "median WordPress site in the region is X KB and fails CWV"), not a per-lead enrichment source.
- **Trigger:** querying HTTPArchive for a typical `without_websites.csv` business returns "no such URL."
- **Fix:** use HTTPArchive only for aggregate baselines to calibrate scoring thresholds; use PSI/CrUX per-lead for the live subset.

## Signal ranking by predictive value (not by ease of collection)

Defined here as: does the signal separate "will buy a Voxire service" from "won't", or correctly route the lead to the *right* service. Ranked high → low.

| # | Signal | What it actually predicts | Predictive value | Why it is (not) informative |
|---|---|---|---|---|
| 1 | **Analytics / ad-pixel present** (GTM, GA4, Meta Pixel, Google Ads) | The business already spends on traffic and measures ROI → highest conversion for marketing retainers, SEO, analytics setup | **High** | Presence is rare enough among SMBs to be discriminating; it proves *marketing spend*, not just a website. Absence is also a pitch. |
| 2 | **Prior paid investment footprint** — a real multi-page site on a paid CMS/hosting vs a free-builder one-pager vs no site | Willingness/ability to pay *again* (the intent gate); determines "rebuild/upgrade" vs "launch from scratch" | **High** | This is the single-vs-real-site question, and it is the strongest predictor of budget. Note: the pipeline already approximates the *bottom* of this ladder (no site / dead site). What is missing is the *top* of the ladder (which paid CMS). |
| 3 | **Which CMS + staleness** (WordPress vs Wix vs Shopify vs hand-rolled; old theme/jQuery) | The concrete pitch and the technical-debt dollar figure; routes to rebuild vs migrate vs e-commerce retainer | **High** | "CMS present" as a boolean is saturated and useless; the *identity* and *age* carry the signal. |
| 4 | **SEO tag quality** (missing/duplicate title, no meta description, no OG, no sitemap) | A named "SEO audit / visibility" service, and moderate intent (the owner has a site but it is invisible → unsolved need) | **Medium-High** | Concrete and provable on a sales call; weakly predictive on its own because it correlates with low spend, but maps cleanly to a service. |
| 5 | **Mobile-unfriendly** (no viewport / non-responsive) | A rebuild/refresh pitch in a mobile-first market (LB/KSA traffic is majority mobile); correlates with old CMS | **Medium** | Visible to the owner on their own phone, so a strong *emotional* hook; but as a discriminator it mostly re-states "old CMS" from #3. |
| 6 | **HTTPS absence** | The site is old/abandoned/DIY → corroborates the existing "rebuild + maintenance" pitch | **Medium (narrow, corroborating)** | Presence is saturated (Let's Encrypt made it free and near-universal), so `https=true` is noise. Absence is rare and meaningful, but it is a *corroborating* flag, not a primary one. |
| 7 | **Page speed (lab)** | Little standalone; it is a low-ticket optimization and is confounded by geography, device, hosting | **Low** | Most SMB sites are slow; "slow" does not separate buyers. Highest value is as a *conversation opener* (a concrete, quantified pain), not a scoring input. |

**Meta-point that should govern the design:** intent signals are mostly **deficiency/absence** signals, not presence signals. "Has CMS / has HTTPS / has analytics" is near-universal or near-zero and thus non-discriminating; what separates buyers is the *gap* (no analytics, no SEO, dead site, single page, old CMS). The current design already reflects this — `pitch_recommender.py` ranks "dead website" and "no website + social" first (`pitch_recommender.py:57-66`). Tech-stack fingerprinting should extend that logic into the live branch, not replace it with a checklist of presence booleans.

## Not a bug, but worth knowing

- **Adding any of these signals requires a schema change.** There is no CMS/analytics/HTTPS/mobile/SEO column today (`main.py:33-40`), and `load_master`/`write_csv` round-trip a fixed `FIELDS` list with `extrasaction="ignore"` (`main.py:125`). New fields silently survive writes but are dropped on reload unless `FIELDS` is updated and type-cast in `load_master` (`main.py:90-104`). Budget for this; it is the same class of issue as the existing type-casting block.
- **Detection does not need a vendor.** Wappalyzer's detection rules are open-source (`apps.json`, thousands of fingerprints); the enricher could run them locally against the HTML it already holds. BuiltWith/Wappalyzer's paid value is historical/aggregate coverage and CRM enrichment, not raw detection of a page you can fetch yourself.
- **PSI field data is being deprecated.** The PSI docs state CrUX field data in PSI "will be discontinued," pointing to the CrUX API / CrUX History API as the replacement. If page-speed field data is wanted, plan to call the CrUX API (150 req/min free) directly rather than depend on PSI's `loadingExperience` long-term.
- **CrUX/PSI return nothing for low-traffic sites.** Both require enough real-user samples to be anonymized; a long-tail SMB site will frequently return `404 chrome ux report data not found`. This is the same coverage limitation as S5 and means lab data (Lighthouse) — not field data — is the reliable per-lead signal.

## Recommended order of work

1. **Extract the free tech signals in `_fetch_website`** (S1–S3): return a `tech` dict alongside `contacts` — generator/CMS identity, analytics/pixel presence, `has_viewport`, `has_meta_description`, `scheme`. This is the highest value per line of code and costs $0.
2. **Add the new columns and round-trip them** (`main.py:33-40` `FIELDS`, `load_master` type-casting, `_INT_FIELDS`/`_FLOAT_FIELDS`/new `_BOOL_FIELDS`), so the signals survive CSV reload.
3. **Split the live-website branch in `recommend_service`** on CMS identity + analytics presence + staleness (replace the catch-all `pitch_recommender.py:70-71` / `95-96` with: stale WordPress → rebuild; Wix free → migrate; Shopify → e-commerce retainer; has pixel → marketing retainer; no analytics → analytics/SEO setup).
4. **Add free PSI/CrUX enrichment for the live subset only**, to obtain mobile-friendliness and lab page-speed as *conversation openers* (ranked #5/#7 — lowest scoring weight, highest talk-track value). Skip field-data expectations for low-traffic sites.
5. **Do not procure Wappalyzer/BuiltWith now.** Revisit only if reachable-HTML coverage proves insufficient for `website_live is None` records, and even then evaluate running Wappalyzer's open rules locally first. Use HTTPArchive only for scoring-threshold baselines.
