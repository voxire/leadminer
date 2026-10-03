# 073 — Monthly cost model at 10k / 100k / 500k records

## Verdict

At the current design the system is **effectively free to run** — the 67-query
Google Places scraper stays under the 1,000-call free Enterprise tier even on a
weekly cron, the Overpass/Wikidata scrapers are free, and CI is free on a public
repo. **But the design cannot produce 100k records, let alone 500k, from Google
Places Text Search alone**: 67 query strings × 60 results max = ~4,020 raw rows
per full sweep, and Text Search (New) is capped at 60 results per query
(`scrapers/google_places.py:45-124`, [Text Search (New) docs](https://developers.google.com/maps/documentation/places/web-service/text-search)).
Reaching the larger targets requires many more queries and/or Place Details /
enrichment calls, at which point **Google Places billing switches from $0 to
roughly $3.5k/mo at 100k and $15k–28k/mo at 500k**, and enrichment vendors
dominate. The single most important cost fact: **`google_places.py:31-42` puts
`places.rating` and `places.userRatingCount` in the field mask, which forces every
call up to Text Search *Enterprise* ($35/1k) instead of Pro ($32/1k)** — see S1.

## Scope and method

This lens builds a bottom-up monthly cost model at three record scales: **10k,
100k, 500k unique businesses** in the master CSV. "Records" means rows in
`data/all_businesses.csv`. All numbers are USD, list price, first-tier, no
committed-use or reseller discounts. Sources are cited inline. Cost is split into:

1. Google Places API
2. Proxy need
3. rclone + Google Drive storage
4. CI minutes (GitHub Actions)
5. Email validation
6. Enrichment APIs
7. Infrastructure

I model **two run cadences** because the README and the workflow disagree:

- `README.md:5,76` claims a **monthly** cron.
- `.github/workflows/scrape.yml:4-6` has the cron **commented out** — only
  `workflow_dispatch` fires today.

I use **monthly full refresh** as the headline (matches the README) and give the
cost of a deeper re-scan where relevant.

### Why raw record count is decoupled from API calls

Text Search (New) returns a **maximum of 60 results per query** across pages
([Text Search (New)](https://developers.google.com/maps/documentation/places/web-service/text-search)),
with pagination stepping 20 at a time and a 2 s sleep between pages
(`google_places.py:275-278`). The scraper has **67 queries total** (33 Lebanon,
lines 45-79; 34 KSA, lines 82-124 — the README's "67 targeted queries" at
line 12 matches; the brief's "68" is off by one). Absolute ceiling per sweep:

```
67 queries × 60 results = 4,020 raw places, minus dedup/whitelist losses
```

Order of magnitude: **one full sweep can never exceed ~4k raw rows**. To get 10k
you must add queries and paginate deeply; to get 100k/500k you need *hundreds* of
query permutations or a different API (Place Details, Nearby Search, or purchased
data). This is the dominant cost driver and is invisible in the current code.

---

## Component 1 — Google Places API

### The SKU trap (see S1, repeats here because it drives every number)

Billing is "at the highest SKU applicable to your request"
([Usage and billing](https://developers.google.com/maps/documentation/places/web-service/usage-and-billing)).
The field mask at `google_places.py:31-42` requests:

| Field | SKU it triggers |
|---|---|
| `places.id`, `nextPageToken` | Essentials (IDs only) |
| `places.displayName`, `places.formattedAddress`, `places.location`, `places.types` | **Pro** |
| `places.websiteUri`, `places.nationalPhoneNumber` | **Enterprise** |
| `places.rating`, `places.userRatingCount` | **Enterprise** |

`rating` and `userRatingCount` trigger **Text Search Enterprise** per the
[Place Data Fields (New)](https://developers.google.com/maps/documentation/places/web-service/data-fields)
and [Text Search (New)](https://developers.google.com/maps/documentation/places/web-service/text-search)
docs. Because the highest SKU wins, **every call is billed at Enterprise**, not
Pro, even though the code only needs rating counts for `lead_score`
(`enricher.py:253`).

### Price table (Places API New, per 1,000 calls, first paid tier)

From the [Google Maps Platform core services pricing list](https://developers.google.com/maps/billing-and-pricing/pricing):

| SKU | Free/mo | 0–100k | 100k–500k | 500k–1M | 1M–5M |
|---|---|---|---|---|---|
| Text Search Pro | 5,000 | $32.00 | $25.60 | $19.20 | $9.60 |
| Text Search Enterprise | 1,000 | $35.00 | $28.00 | $21.00 | $10.50 |
| Place Details Pro | 5,000 | $17.00 | $13.60 | $10.20 | $5.10 |
| Place Details Enterprise | 1,000 | $20.00 | $16.00 | $12.00 | $6.00 |

### Current run (67 queries)

| Pages/query | Calls/sweep | Monthly (1 sweep) | Monthly (4 weekly sweeps) |
|---|---|---|---|
| 1 | 67 | **$0** (< 1k free) | $0 (< 1k) |
| 2 | 134 | **$0** | $0 |
| 3 (max) | 201 | **$0** | $0 |

Even at weekly cadence, 201×4 = 804 calls < the 1,000 free Enterprise allowance.
**Current Google Places spend is $0/month.** This is the only genuinely cheap
part of the system today.

### Cost to actually reach each scale

I model two search-call efficiencies, since dedup (`dedup.py:64`) and the
whitelist (`whitelist.py:105`) discard rows and query results overlap heavily:

- **A — 1.0 call/record** (optimistic: each call yields ~20 net-unique, high dedup)
- **B — 2.5 calls/record** (realistic: pagination, overlap, filter loss)
- **C — 5.0 calls/record** (pessimistic)

All billed at **Text Search Enterprise**, first-run volume, no prior usage:

| Scale | A @1.0 | B @2.5 (realistic) | C @5.0 |
|---|---|---|---|
| 10,000 | $315 | **$840** | $1,715 |
| 100,000 | $3,465 | **$7,672** | $14,672 |
| 500,000 | $14,672 | **$27,815** | $40,940 |

For a **monthly ongoing re-scan at 25% of base volume** (refresh ratings, catch
closures):

| Scale | Re-scan calls/mo | Cost/mo |
|---|---|---|
| 10,000 | 6,250 | $184 |
| 100,000 | 62,500 | $2,153 |
| 500,000 | 312,500 | $9,422 |

**If the `rating`/`userRatingCount` fields are dropped**, the same volumes bill at
Pro, saving 8–11%: e.g. 250,000 calls cost $8,960 Pro vs $9,660 Enterprise.
Marginal, but free to do.

**Bigger lever:** the architecture does not need Places at all for scale. Lebanon
has ~50k businesses total ([soopage](https://lbn.soopage.com/) claims 49,495;
[asia-places](https://ja-lebanon.asia-places.com/) lists 26,409) and KSA has
~1.68M active registrations ([IBLead](https://iblead.com/en/blog/saudi-arabia-business-directory)).
Google Places coverage of KSA SMBs is partial — licensed commercial data
(Saudi 62k-company directory at ~$53 one-time,
[saudi-data.site](https://www.saudi-data.site/en)) may be far cheaper per record
than API scraping. **Recommend evaluating a one-time data buy against ongoing API
spend before committing to the 500k Places budget.**

---

## Component 2 — Proxy need

Today the scraper fetches websites **directly**, no proxy
(`enricher.py:142-177`), and sends `verify=False`
(`enricher.py:146`). Direct fetches from GitHub-hosted runners are free (no
bandwidth charge). Proxies become necessary only because:

1. **Rate-limit / IP-block risk.** 40 threads (`enricher.py:180`) hammering
   arbitrary websites from one GitHub datacenter IP will trigger WAFs and
   Cloudflare challenges. The code swallows *all* exceptions as
   `return False` (`enricher.py:151-152`), so blocks are silently recorded as
   "dead website" — corrupting `website_live` (S2).
2. **Geo.** Some KSA/LB sites geo-fence; a local exit IP improves hit rate.

Proxy cost is bandwidth-driven. HTML is truncated at 200 KB
(`enricher.py:150`), but real transfer (headers, redirects, images pulled by
redirect targets) averages higher. Residential proxies run **$2.00–$3.50/GB**
([Decodo from $2/GB](https://decodo.com/), [Webshare from $1.40/GB](https://www.webshare.io/decodo-alternative),
[Oxylabs $4/GB](https://decodo.com/best/best-residential-proxies)). Datacenter
proxies are **$0.077–$0.60/GB** ([Bright Data](https://brightdata.com/pricing/proxy-network/datacenter-proxies))
but are more likely to be blocked.

Assume **40% of records have a website** and each is fetched once per run:

| Scale | Sites (~40%) | @150 KB | @300 KB | @1 MB |
|---|---|---|---|---|
| 10,000 | 4,000 | $1–2 | $2–4 | $8–14 |
| 100,000 | 40,000 | $12–21 | $23–41 | $78–137 |
| 500,000 | 200,000 | $59–103 | $117–205 | $391–684 |

At realistic 300 KB, residential proxies cost **~$2–4 (10k), ~$23–41 (100k),
~$117–205 (500k)/mo**. Datacenter proxies cut this ~90% (~$3 at 100k) but with a
higher block rate. **This is cheap compared to Places — proxies are not the
budget problem; they are the data-quality fix for S2.**

---

## Component 3 — rclone + Google Drive storage

Storage math is trivial. The 5 CSVs carry 23 columns
(`main.py:32-39`). A record with name/address/etc. runs **~350–600 bytes** of
CSV. Take 500 B/record × 1.3 for CSV overhead of the multiple derived files
(master + 4 subsets, though subsets are ~50–70% of master collectively):

| Scale | Master CSV | + 4 subsets | + timestamped copies/run | Total/mo |
|---|---|---|---|---|
| 10,000 | ~5 MB | ~12 MB | ×4 runs = ~68 MB | < 0.1 GB |
| 100,000 | ~50 MB | ~120 MB | ×4 = ~680 MB | < 1 GB |
| 500,000 | ~250 MB | ~600 MB | ×4 = ~3.4 GB | ~3.4 GB |

The workflow uploads **both** a timestamped snapshot and a copy to
`gdrive:leads/` on every run (`scrape.yml:47-50`), so storage grows linearly with
run count — a minor but real accumulation.

Google storage is essentially free at these volumes: **15 GB free** with any
account, or **100 GB for $1.99/mo / 2 TB for $9.99/mo** ([Google One](https://one.google.com/about/plans)).

| Scale | Storage used | Plan | $/mo |
|---|---|---|---|
| 10,000 | < 0.1 GB | Free 15 GB | **$0** |
| 100,000 | < 1 GB | Free 15 GB | **$0** |
| 500,000 | ~3.4 GB/yr-to-date | Free 15 GB | **$0** (drifts to $1.99 plan after ~4 yrs of snapshots) |

**rclone itself is free** (installed via apt, `scrape.yml:26`). Drive API quotas
are generous: free standard use, **750 GB/day upload, 1 TB/day project egress**
([Drive API limits](https://developers.google.com/workspace/drive/api/guides/limits)).
At 3.4 GB/run this is a non-issue. Google warns Drive API overage may become
**billable later in 2026** ([Drive API limits](https://developers.google.com/workspace/drive/api/guides/limits)) —
budget a placeholder of **$0–5/mo** for that eventuality.

**Drive storage does not scale with records — it scales with snapshot retention.
Fix: stop writing timestamped snapshots every run, or add pruning.**

---

## Component 4 — CI minutes (GitHub Actions)

Runner: `ubuntu-latest` (2-core x64) = **$0.006/min**. Free tier: **2,000
min/mo on Free, 3,000 on Team/Pro** ([GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)).
Free and unmetered on public repos.

Job wall time is dominated by enrichment: 40 workers
(`enricher.py:180,188`), each site one GET with 8 s timeout
(`enricher.py:146`). Assume ~3 s average latency per site (mix of fast hits and
timeouts):

| Scale | Sites | Enrich wall time | + install/overhead | $/job | $/mo (monthly) | $/mo (weekly) |
|---|---|---|---|---|---|---|
| 10,000 | 4,000 | ~5 min | ~10 min | $0.06 | **$0.06** | **$0.24** |
| 100,000 | 40,000 | ~50 min | ~55 min | $0.33 | **$0.33** | **$1.32** |
| 500,000 | 200,000 | ~250 min | ~255 min | $1.53 | **$1.53** | **$6.12** |

**CI is the cheapest line item and will not approach the 2,000-minute free tier
until ~500k weekly.** Caveat: the workflow sets `timeout-minutes: 300`
(`scrape.yml:11`), so 500k at ~255 min fits but with almost no headroom; anything
slower and the job is killed mid-write. Also note every job is billed rounded up
to the minute, adding a few cents ([GitHub docs](https://docs.github.com/en/billing/concepts/product-billing/github-actions)).

However, **at 500k the job becomes fragile** — the 300-minute ceiling plus the
scraper's own retry sleeps mean you should shard enrichment across jobs, which
multiplies the install overhead but keeps cost under **~$10/mo**.

---

## Component 5 — Email validation

Emails come from OSM/Wikidata tags and regex extraction
(`enricher.py:154-159`). Assume **15–30% of records yield an email**. Validate at
**ZeroBounce $0.009/credit PAYG** (min 2,000 credits,
[ZeroBounce pricing](https://www.zerobounce.net/pricing)); volume rates drop to
~$0.005 at 500k+ ([ZeroBouncer tier table](https://zerobouncer.com/pricing)).
NeverBounce gives 1,000 free/mo then comparable rates; Hunter verify bundles
into credits from $49/mo ([Hunter pricing](https://hunter.io/pricing)).

| Scale | Emails @15% | @$0.009 | Emails @30% | @$0.009 |
|---|---|---|---|---|
| 10,000 | 1,500 | $14 | 3,000 | $27 |
| 100,000 | 15,000 | $135 | 30,000 | $270 |
| 500,000 | 75,000 | $675 | 150,000 | $1,350 |

At volume (~$0.005/email): **$75 (100k) / $375 (500k)**. **Budget $10–30 (10k),
$135–270 (100k), $675–1,350 (500k)/mo.** Note the code currently does
**zero validation** — it accepts any regex match, including `noreply@`,
`info@` catch-alls, and strings scraped from JS. Validating is a net *quality*
win worth the cost (see S3).

---

## Component 6 — Enrichment APIs (fill missing contacts)

This only applies if you plug in a vendor to fill the ~70% of records lacking an
email. The code has **no enrichment vendor today** — it relies on scraping the
business's own site (`enricher.py:142-177`). Costs:

- **People Data Labs** company data starts at **$0.10/credit**, Pro $100/mo for
  1,000 records ([PDL company pricing](https://www.peopledatalabs.com/pricing/company),
  [ZoomInfo summary](https://pipeline.zoominfo.com/sales/people-data-labs-pricing)).
- **Apollo** people enrichment is **1–9 credits/person** (8 extra if mobile
  returned) ([Apollo API pricing](https://docs.apollo.io/docs/api-pricing)).

Modeling lookups for the ~70% missing an email:

| Scale | Lookups | PDL @$0.10 | Apollo @$0.05–0.15 |
|---|---|---|---|
| 10,000 | 7,000 | $700 | $350–1,050 |
| 100,000 | 70,000 | $7,000 | $3,500–10,500 |
| 500,000 | 350,000 | $35,000 | $17,500–52,500 |

**This is the single largest potential line item and it is entirely optional.**
The current pitch (find businesses with *weak* digital presence) is better served
by the free/cheap website-scrape path than by buying contact data. **Recommendation:
do not add an enrichment vendor at 500k unless revenue per lead justifies
~$0.10 × 350k = $35k/mo.** Cite this to the stakeholder before adding one.

Also consider **email-verification-only** (Component 5) instead of
enrichment — 30× cheaper.

---

## Component 7 — Infrastructure

| Item | Cost/mo | Note |
|---|---|---|
| GitHub repo / Actions | $0 | free tier covers all scales to ~500k monthly |
| GitHub runner overage | $0–6 | only at 500k weekly |
| Drive storage | $0 | free 15 GB; $1.99 if it ever exceeds |
| rclone | $0 | open source |
| Overpass API | $0 | free, but fair-use; retries add CI time |
| Wikidata SPARQL | $0 | free, `LIMIT 5000` (`wikidata.py:25`) |
| API key / GCP project | $0 | pay-as-you-go, no platform fee |
| Compute (if moved off Actions) | $5–40 | 500k enrichment on a small VPS/Spot instance |
| Logging/observability | $0–20 | none today |
| **Subtotal** | **$0–66** | |

Container/database hosting is **$0 because there is no database and no
packaging** (`BRIEF.md:11`; no `pyproject.toml`, no `Dockerfile`). Storage is
local `data/` (gitignored, `.gitignore:9`) shipped to Drive.

---

## Total monthly cost per scale

Headline = **realistic (B) Places efficiency, monthly refresh, no enrichment
vendor, residential proxy @300 KB, ZeroBounce validation @30%**.

| Line item | 10k | 100k | 500k |
|---|---|---|---|
| Google Places (first build, monthly) | $840 | $7,672 | $27,815 |
| Google Places (ongoing 25% re-scan) | $184 | $2,153 | $9,422 |
| Proxies (residential @300 KB) | $3 | $32 | $161 |
| Drive storage + rclone | $0 | $0 | $0–5 |
| CI minutes | $0.06 | $0.33 | $1.53 |
| Email validation (@30%) | $27 | $270 | $1,350 |
| Enrichment APIs (optional) | $0 | $0 | $0 |
| Infrastructure | $0 | $0 | $5–40 |
| **TOTAL — first build month** | **~$870** | **~$7,974** | **~$29,328** |
| **TOTAL — steady-state month** | **~$214** | **~$2,455** | **~$10,940** |
| **TOTAL — with enrichment vendor** | ~$1,570 | ~$10,974 | ~$46,828 |

### Read this before planning a budget

1. **The system as written costs ~$0/month** and produces at most ~4k rows.
   Everything above $0 is the cost of *making it produce enough records*.
2. **Google Places is 95%+ of the model.** Optimize there first: drop the
   Enterprise fields (S1), reduce re-scan frequency, and seriously evaluate a
   one-time KSA/LB data purchase over 500k API calls.
3. **500k via Places is economically dubious.** At ~$28k first-build /
   ~$9.4k ongoing, you must convert a large fraction of 500k records to pay back.
   A $53 one-time Saudi directory ([saudi-data.site](https://www.saudi-data.site/en))
   plus targeted Places runs is ~500× cheaper.
4. **Enrichment APIs would 4× the bill.** Do not add one without a revenue model.
5. **Free tiers absorb CI, storage, and proxies completely.** The failure modes
   here are quality (S2, S3), not spend.

---

## Findings

### S1 — Field mask forces Text Search *Enterprise* billing, not Pro

- **Where:** `scrapers/google_places.py:31-42` (`places.rating`,
  `places.userRatingCount`), consumed by `enricher.py:252-254`.
- **Breaks:** Every Text Search call is billed at the highest SKU touched.
  `rating`/`userRatingCount` are Enterprise fields, so all calls bill at
  **$35/1k instead of $32/1k Pro** — an ~9% overspend — and the 1,000-call free
  tier shrinks (Enterprise free cap) vs Pro's 5,000 free
  ([Place Data Fields (New)](https://developers.google.com/maps/documentation/places/web-service/data-fields)).
- **Trigger:** Any run; the discount is automatic but silently forfeited.
- **Fix:** Remove `places.rating` and `places.userRatingCount` from the field
  mask (and from `lead_score`), or accept that the score needs them and budget
  Enterprise explicitly. At 500k records this is ~$2,000/mo.

### S2 — Silent "dead website" from swallowed fetch failures poisons the data

- **Where:** `enricher.py:151-152` (`except Exception: return False`), and
  `verify=False` at `enricher.py:146`.
- **Breaks:** Any 403/429/Cloudflare block/DNS failure is recorded as
  `website_live = False`. `lead_score` then awards **+20 for "dead website =
  sales opportunity"** (`enricher.py:243-244`), so *the harder a site blocks you,
  the higher the lead score*. This inverts the product's core signal.
- **Trigger:** A protected site (any Shopify/Wix site behind a challenge) fetched
  40-at-a-time from one GitHub IP.
- **Fix:** Distinguish network/HTTP-block errors from real 404s; retry blocked
  sites through a proxy (Component 2) rather than scoring them as dead.

### S3 — No email validation; regex accepts junk addresses

- **Where:** `enricher.py:127-159`.
- **Breaks:** `_EMAIL_RE` matches any `x@y.zz`; blacklist covers only 8 domains
  (`enricher.py:135-138`). `noreply@`, `example.org`, JS minified tokens, and
  `info@` catch-alls all enter the output and get `+20` in `lead_score`
  (`enricher.py:232-233`). Sales-ready lists fill with uncontactable addresses.
- **Trigger:** Any site whose HTML contains a `mailto:` to a placeholder or a
  `@` inside inline JS.
- **Fix:** Add a validation pass (ZeroBounce et al., Component 5) before writing
  `sales_ready.csv`; budget $27–1,350/mo depending on scale.

---

## Not a bug, but worth knowing

- **The cron doesn't exist.** `README.md:5,76` promises a monthly run;
  `scrape.yml:4-6` has it commented out. Cost is $0 today, but the model's
  "monthly refresh" numbers assume you turn it back on.
- **Timestamped snapshots grow storage forever** (`scrape.yml:47-50`). Add
  pruning or switch to a single overwritten master; saves the (already tiny)
  storage line and keeps Drive clean.
- **Overpass/Wikidata are load-bearing but free.** OSM covers Lebanon only
  (`osm.py:12`) and Wikidata is `LIMIT 5000` (`wikidata.py:25`) — together they
  cannot reach 100k. If they ever get rate-limited or asked to stop, the Places
  budget jumps to cover the whole target.
- **No database means recompute cost every run.** `main.py:95` reloads the whole
  master CSV and re-enriches every record with a website
  (`main.py:124-129`). At 500k that's 200k fetches *every run*; a DB + incremental
  refresh would cut both CI time and proxy bandwidth by ~75%.
- **`data/` is gitignored** (`.gitignore:9`), so Drive is the only durable store.
  Losing the Drive account loses the whole dataset, and there is no backup line
  item in this model — worth adding (e.g. a $6/mo B2 bucket) as insurance.
- **Costs are anchored to 2026-09 list prices** (Google page "Last updated
  2026-09-28"; Google warns Drive API overage becomes billable later in 2026).
  Re-verify before contracting.

## Recommended order of work

1. **Drop `places.rating` / `places.userRatingCount` from the field mask**
   (`google_places.py:31-42`) unless the score truly needs them — free ~9% and a
   larger free tier. (S1)
2. **Fix the dead-website/blocked-site conflation** (`enricher.py:151-152`)
   before trusting any `sales_ready.csv`. (S2)
3. **Get a hard number for KSA/LB licensed data** and compare it to the modeled
   $9.4k–28k/mo Places spend at scale. Likely changes the whole architecture.
4. **Cap Places spend per run** with a request budget in code; today nothing
   stops an accidental 6-page deep pagination run.
5. **Add pruning** to the Drive snapshot step (`scrape.yml:47-50`).
6. **Only then** consider an enrichment vendor — and only against a revenue
   model that clears ~$35k/mo at 500k. (Component 6)
