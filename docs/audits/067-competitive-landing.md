# 067 — Competitive & Landing: how MENA agencies actually run outbound, and the gap `leadminer` can own

## Verdict

`leadminer` is **not** competing with Apollo, Clay, Instantly, Smartlead or Lemlist. Those
tools are contact databases and email machines built for the US/EU mid-market and enterprise,
where a buyer has a LinkedIn profile and a company domain. This project's actual substrate —
OSM + Google Places + Wikidata records for the MENA SMB long tail (restaurants, clinics,
salons, retail, workshops) — is precisely the segment those tools cannot see. The project's
current 5-CSV output, however, is framed as a *contact list*, which is the one game Apollo
will always win. The defensible product is not "more contacts": it is **"which local business
has a broken or absent digital presence, what exactly is missing, and which service to sell
them" — with MENA-native Arabic entity resolution and PDPL/WhatsApp compliance built in.**

That is a real, currently unoccupied position. The rest of this report is the evidence and the
concrete shape of it.

---

## The standard MENA outbound pipeline (2026)

An agency running outbound into LB/KSA today wires the same 7 stages. MENA changes stages 3,
4 and 6 materially.

| # | Stage | Typical tooling | MENA reality |
|---|---|---|---|
| 1 | ICP definition | ICP doc, Sales Navigator, TAM spreadsheet | Bilingual EN/AR; "company" often means a physical SMB, not a domain |
| 2 | List build | Apollo 240M+ contacts / 30M companies; Clay TAM sourcing; Google Maps scrapers (Outscraper, Apify, PhantomBuster, Grepsr, Lobstr); LinkedIn Sales Navigator; local directories | Apollo/Clay coverage of the SMB long tail is thin; directories + Maps dominate |
| 3 | Enrich / waterfall + verify | Clay (150+ databases / 200+ providers), Apollo waterfall (20+ vendors), Lusha, Cognism, D&B Hoovers, Pintel.ai, GSDSI; email verify via Instantly/ZeroBounce | Phone-first dedup; Arabic transliteration; mobile vs landline matters |
| 4 | Sequencing | Email: Instantly, Smartlead, Lemlist, Salesforge. LinkedIn: HeyReach, Dripify, Expandi, Waalaxy. WhatsApp: BSPs (Cequens, Unifonic, Infobip, 360dialog), plus LinkedIn+WhatsApp tools like Sbl.so | WhatsApp + LinkedIn are first-class, not email; Arabic copy; RTL |
| 5 | Deliverability infra | Secondary domains, SPF/DKIM/DMARC, warmup, inbox rotation, inbox-placement tests | Many MENA recipient domains are poorly authenticated; sender reputation work is heavier |
| 6 | CRM / automation | HubSpot, Pipedrive (very common in MENA), Salesforce, Airtable/Sheets + Zapier/Make | Pipedrive + Sheets dominate the SMB/agency tier |
| 7 | Measurement | Open/reply/positive-reply, meetings, cost-per-meeting | Reply rates judged on WhatsApp + calls, not just email opens |

### Tool economics (Oct 2026, vendor-published — treat as directional)

- **Apollo** — 240M+ contacts / 30M+ companies, claims 98% email accuracy, 2M+ contributor
  network, waterfall across 20+ vendors, and *"Local business data sourced via Google Maps,
  inside Apollo"* (apollo.io/product/b2b-data). SOC 2 / ISO 27001 / GDPR. Freemium + seat tiers.
- **Clay** — orchestration layer; "150+ databases", "200+ providers", native sequencer,
  Claygents, MCP/API. Raised to a reported **$5B valuation in Jan 2026** (NYT). This is the
  tool agencies build custom waterfalls on.
- **Instantly** — flat-fee, *unlimited sending accounts*, 450M+ contacts bundled (SuperSearch),
  SISR IP sharding, warmup, AI reply agent. Vendor-cited TCO ≈ **$194–$455/mo** at 5k emails/day.
- **Smartlead** — **$94/mo** Pro (150k emails), **+$29/client** for agencies, API-first,
  unlimited sender addresses.
- **Lemlist** — **$69+/user/mo**, per-seat, multichannel (email + LinkedIn + calls), 3 sending
  accounts included / $9 extra. Expensive past ~5 seats.
- **MENA-specific / regional**
  - **Pintel.ai** — positions on *"proprietary Gulf registry access and 30+ provider waterfall
    enrichment"* for all six GCC markets.
  - **Solvarex** — Apollo **Certified Partner** serving KSA/UAE/Jordan/Egypt; sells
    implementation + *Arabic* sequence design + deliverability, not just seats.
  - **Sulsaly** — markets itself as a "best Apollo alternative for MENA", claiming Apollo's
    *"contact database has very sparse MENA coverage — most contacts are in the US and Europe"*
    (vendor claim, but consistent with practitioner complaints).
  - **Cequens** (Egypt) and **Unifonic** (Saudi) — the WhatsApp BSPs MENA enterprises actually
    onboard; both are official Meta BSPs.
  - **Sbl.so** — LinkedIn + WhatsApp automation; publishes 2026 WhatsApp cold-reply benchmarks
    (see below).

### Direct competitors to `leadminer` (the ones that matter)

Functionally, `leadminer` is closer to **Google Maps lead scrapers** (Outscraper, Apify actors,
PhantomBuster, Grepsr, Lobstr) than to Apollo. Those tools:
- extract name / phone / address / website / rating from Maps,
- charge per record or per run,
- and do **nothing** with the data — no liveness check, no digital-gap score, no service fit.

The scoring + pitch mapping in `pitch_recommender.py` and `enricher.py` is the differentiator
against *that* category. Against Apollo it should not compete at all.

---

## What is genuinely missing — and what `leadminer` can own

There are five real, currently unoccupied gaps. Ranked by defensibility.

1. **"Digital gap" as a sellable signal.** No incumbent sells "this business has a *dead*
   website", "this business has Instagram but no site", "this clinic has 8 reviews and no
   Google presence". Apollo/Clay enrich tech stack and firmographics; they do not score *digital
   deficiency* as a buying trigger. `enricher.py:241-244` already treats a dead website as
   +20 — the strongest single score in the code — but it is buried inside a 0–100 composite and
   never surfaced as a segment. **Own it: a `digital_gap_tier` + `gap_reasons` field.**

2. **Arabic / transliteration entity resolution.** `dedup.normalize_name` (`dedup.py:28-31`)
   does NFKD + lowercase + whitespace collapse. It does **not** normalise Arabic alef/hamza
   forms, taa marbuta, remove tatweel, strip "Al-/El-/ال" prefixes, or reconcile EN↔AR
   transliterations ("Beirut"/"بيروت"/"Bayrut"). Global tools don't do this for MENA either —
   so a correct Arabic-aware resolver is a genuine moat, and it makes every other field more
   trustworthy. Phone-first dedup (`dedup.py:64-97`) is already the right instinct for MENA
   (phone is the universal local key).

3. **A verified-entity / local-registry layer.** **Maroof** (`maroof.sa` / `business.sa`) is the
   Saudi Ministry of Commerce national register of online stores; **Daleeli**, **Connect Saudi**,
   **ListInSaudi** and similar directories cover offline SMBs. None of this is in Apollo/Clay.
   Ingesting it yields: legal entity + category + KSA e-commerce legitimacy signal, plus the
   *lawful basis* trail that PDPL expects. **Own it: `registry_source`, `registry_id`,
   `verified_entity` columns.**

4. **A compliant MENA channel router.** MENA outbound is WhatsApp-first, but the official
   WhatsApp Business API **requires opt-in** (Meta's November 2024 policy update) and moved to
   **per-message pricing in July 2025**; marketing templates are capped (US `+1` numbers cannot
   receive marketing templates). True-cold WhatsApp reply rates are only **1–5%**; semi-warm
   (after email/LinkedIn) **8–15%**; warm **20–50%+**, and block rates must stay under ~1%.
   So the winning motion is *email/LinkedIn first touch → WhatsApp warm follow-up*. Nothing in
   the tooling stack tells an agency **which channel is compliantly available for this lead**.
   `leadminer` extracts `wa.me` links (`enricher.py:129-132`) but does not distinguish mobile
   vs landline, nor whether a number is WhatsApp-capable. **Own it: `contact_channels`,
   `phone_type`, `whatsapp_ready` with an explicit lawful-basis note.**

5. **Freshness + compliance as a product, not an afterthought.** This is where the incumbents
   are legally exposed and a MENA-native system can win pricing power. KSA **PDPL** has been in
   full effect since **14 Sep 2024** under SDAIA, with fines up to **SAR 5,000,000** per
   violation (doubled for repeat) and criminal liability for sensitive data; UAE **Federal
   Decree-Law 45/2021** is consent-centric (executive regulations were still awaited in early
   2026); Lebanon **Law 81/2018** prohibits unsolicited marketing emails. Separately, Google's
   Places terms forbid storing/caching content beyond `place_id`, with lat/lng cacheable only
   **up to 30 consecutive days**. A lead graph that is *provably* sourced, lawfully based, and
   Google-ToS-clean is a differentiator the scramble-and-blast vendors cannot claim.

---

## Findings

### S1 — The output competes with Apollo on the one field Apollo always wins

- **Where:** `main.py:139-153` writes `with_websites.csv` / `without_websites.csv` / `sales_ready.csv`; `enricher.py:227-259` composites every signal into one `lead_score`.
- **Breaks:** the CSVs read as "a contact list", which invites comparison to Apollo/Instantly
  data (where the project's 1,402-LOC scraper loses on volume, freshness, and email accuracy).
  The buyer inside an agency already has Apollo; what they don't have is *which of these
  un-indexed local SMBs is worth a pitch and why*.
- **Trigger:** open `sales_ready.csv` and ask "why this lead?" — the only answer is an opaque
  0–100 `lead_score`. The differentiators (`website_live=False`, `instagram` present,
  `review_count<20`) are columns, not decisions.
- **Fix:** stop shipping a contact list; ship a **ranked "digital gap" worklist** with explicit
  `gap_reasons` and `pitch_hook`, and demote `email` from hero field to one channel among
  several.

### S1 — The one signal no incumbent sells (dead/absent website) is unreachable

- **Where:** `enricher.py:241-244` (+20 dead site), `enricher.py:54-55` and `:62` in `pitch_recommender.py`.
- **Breaks:** `website_live` is only ever set for records that *have* a website
  (`enricher.py:182`), so "no website" and "unknown" are indistinguishable downstream; and the
  five output files have no "digital gap" tier, so a dead-site lead sits next to a healthy
  multi-channel lead. Worse, a single `requests.get` (`enricher.py:142-152`) can't tell a real
  site from a **parked/placeholder/expired** page, so some of the strongest "rebuild" leads are
  misclassified as healthy.
- **Trigger:** a bakery whose GoDaddy placeholder returns HTTP 200 → `website_live=True`,
  `recommended_service="SEO audit..."` instead of "Website rebuild".
- **Fix:** add `digital_gap_tier` (none / weak / dead / no_site / no_web_at_all), populate
  `website_live` even when there is no website (`False`), and detect parked/placeholder
  (body length, registrar boilerplate, "coming soon", parking DNS).

### S2 — Arabic-aware entity resolution is missing — and it is the moat

- **Where:** `dedup.normalize_name` (`dedup.py:28-31`), `_extract_city` (`dedup.py:34-38`).
- **Breaks:** duplicates across sources/scripts survive dedup, inflating the master
  (`main.py:124-126`), and every downstream score is computed on split records. Arabic is the
  primary language of the actual data (`scrapers/osm.py:60` reads `name:ar`;
  `enricher.py:10-50` matches Arabic region keywords) yet names are never Arabic-normalised.
- **Trigger:** "مطعم النور" vs "Al Nour Restaurant" vs "Nour Rest." in one city → three records;
  `_extract_city` also takes `address.split(",")[-1]`, which for an Arabic address is often a
  country or postcode, not a city.
- **Fix:** implement Arabic normalisation (alef/hamza unification, taa marbuta, tatweel
  removal, definite-article stripping) + a transliteration key and a city gazetteer before
  name-based dedup; keep phone-first as the primary key.

### S2 — WhatsApp is the channel, but the pipeline is email-shaped and compliance-blind

- **Where:** only `enricher.py:129-132` (`_WHATSAPP_RE`) and `main.py:82-90` (`has_any_contact`)
  touch WhatsApp; `dedup.normalize_phone` (`dedup.py:11-25`) never classifies line type.
- **Breaks:** the product cannot answer the only routing question a MENA SDR asks — *"can I
  legally WhatsApp this lead, or is it email-only?"*. Meta requires opt-in and prices per
  message since July 2025; cold WhatsApp yields 1–5% replies and >2–3% block rates risk account
  restriction. An agency blasting `wa.me` links from a CSV will burn numbers.
- **Trigger:** `sales_ready.csv` → SDR imports to an unofficial bulk WhatsApp tool → blocks.
- **Fix:** add `phone_type` (mobile/landline/unknown) and `whatsapp_ready`
  (`has_wa_link` / `unknown`), plus a `lawful_basis` field and a channel recommendation
  (email-first, WhatsApp-on-reply).

### S2 — No local-registry ingestion → no verified entity, no KSA e-commerce signal

- **Where:** sources are only `osm`, `wikidata`, `google_places` (`scrapers/`).
- **Breaks:** the KSA pitch specifically targets DTC / e-commerce brands
  (`scrapers/google_places.py:82-90`), but the project never checks the **Maroof** national
  register of online stores, the strongest available legitimacy + category signal for exactly
  that segment. It also has no CR / VAT field for qualification or invoicing.
- **Fix:** add a `maroof` scraper/connector (store name, category, status, URL) and a
  `registry_source`/`registry_id`/`verified_entity` column set; cross-check Maps names against
  it.

### S2 — The pipeline as built is not compliant, so it cannot be sold as the compliant option

- **Where:** `main.py:74-79` + `:148-153` persist and re-write Places-derived fields
  indefinitely; `.github/workflows/scrape.yml:46-50` pushes them to Drive; `README.md:5` claims
  CSVs are committed.
- **Breaks:** Google Maps Platform terms: no pre-fetch/cache/store of Places content except
  `place_id`, and lat/lng cacheable only up to **30 consecutive days**. The project stores
  `displayName`, address, phone, website, rating and review count forever. Separately, records
  carry no source-provenance/opt-out/lawful-basis trail for KSA/UAE PDPL or Lebanon Law 81.
  The compliance gap is the exact moat argued in Gap #5 — but the current build forfeits it.
- **Trigger:** an audit or a client's PDPL review of `all_businesses.csv`.
- **Fix:** treat Places as ephemeral (store `place_id` + 30-day cache, re-hydrate on demand);
  keep compliance-relevant fields on OSM/Wikidata/registry sources; add
  `source_url`, `collected_at`, `lawful_basis`, `opt_out` per record.

### S3 — Freshness is claimed, not delivered

- **Where:** `.github/workflows/scrape.yml:4-5` (cron commented out), `README.md:5` ("Runs
  monthly"), `main.py:95-97` (master accumulates, never re-validates).
- **Breaks:** incumbents refresh continuously; local SMBs churn fast (closure, phone change,
  new site). Stale `website_live`/phone fields quietly poison the gap score.
- **Fix:** re-check staleness per record (e.g., `last_verified_at`, re-fetch sites older than
  N days), and fix the workflow/README mismatch — otherwise "fresh MENA data" is a claim the
  product can't make.

---

## The product shape this points to

Instead of five overlapping contact CSVs, one **MENA Digital-Gap Lead Graph** with:

| Field | Why / source |
|---|---|
| `entity_key` | Arabic-aware canonical key (`dedup.py` rework) |
| `verified_entity`, `registry_source`, `registry_id` | Maroof / CR / directory trust layer |
| `digital_gap_tier` + `gap_reasons` | the sellable signal (dead site, no site, IG-only, few reviews) |
| `recommended_service` + `pitch_hook` | already exists (`pitch_recommender.py`), make it the headline |
| `contact_channels`, `phone_type`, `whatsapp_ready` | compliant routing for MENA |
| `lawful_basis`, `source_url`, `collected_at`, `last_verified_at` | PDPL / Law 81 / freshness |

Positioning: *"We find the MENA local businesses your competitors' Apollo list can't see, prove
their digital gap, and tell you which service to pitch — compliantly."* The scoring and service
mapping already exist; the gaps above are what turn them from a script into a product.

---

## Not a bug, but worth knowing

- **Apollo is moving toward local SMB data** ("Local business data sourced via Google Maps,
  inside Apollo"). This validates the substrate and raises the urgency of shipping the
  gap/scoring layer before Apollo's Maps coverage localises.
- **Vendor pricing is self-reported.** Instantly's ROI comparisons are marketing; use them as
  ranges, not benchmarks.
- **Sulsaly's "sparse MENA coverage" claim is a competitor's claim** — directionally supported
  by practitioner complaints, not independently audited.
- **WhatsApp is not a cold channel by policy.** 3B+ MAU, 90–98% open, MENA-dominant — but
  Meta's opt-in rule and per-message pricing (July 2025) mean the money is in warm follow-up,
  so the product should optimise the *first touch → WhatsApp* handoff, not bulk WhatsApp.
- **Two regulatory clocks are running:** KSA PDPL enforcement (fines to SAR 5M) and UAE PDPL
  executive regulations. A compliance story has a limited window to be a differentiator before
  it becomes table stakes.

---

## Recommended order of work

1. **Reframe output** (cheap, high leverage): add `digital_gap_tier` + `gap_reasons`; populate
   `website_live=False` for no-website records; make a single ranked worklist the product.
   (`enricher.py`, `main.py`)
2. **Arabic-aware entity resolution** before name-dedup; fix `_extract_city`. (`dedup.py`)
3. **Compliance pass**: Places caching/`place_id` policy; add `source_url`, `lawful_basis`,
   `collected_at`; address the README/workflow freshness mismatch. (`main.py`, workflow, README)
4. **Channel router**: `phone_type`, `whatsapp_ready`, channel recommendation. (`enricher.py`,
   `dedup.py`)
5. **Maroof / local-registry connector** for the KSA DTC segment. (new `scrapers/maroof.py`,
   `scrapers/base.py`)
6. **Parked/placeholder website detection** so "dead" and "healthy" are real. (`enricher.py`)

---

## Sources

- Apollo, *B2B Data* product page (240M+/30M+, 98% accuracy, 20+ waterfall vendors, "Local
  business data sourced via Google Maps"): https://www.apollo.io/product/b2b-data
- Clay, *Waterfall Enrichment* (150+ databases, 200+ providers): https://www.clay.com/waterfall-enrichment
- NYT DealBook, Clay tender offer at a reported $5B valuation (Jan 2026): https://www.nytimes.com/2026/01/28/business/dealbook/clay-start-up-tender-offers.html
- Instantly, *Instantly vs Smartlead vs Lemlist ROI* (pricing, SISR, 450M+ contacts): https://instantly.ai/blog/instantly-vs-smartlead-lemlist-2026/
- Smartlead vs Lemlist (pricing): https://www.smartlead.ai/comparison/smartlead-vs-lemlist
- Lemlist vs Instantly: https://www.lemlist.com/versus/lemlist-vs-instantly
- Pintel.ai (GCC registry + 30+ provider waterfall): https://pintel.ai/blogs/gcc-company-data-providers/
- Solvarex (Apollo Certified Partner, MENA, Arabic sequences): https://solvarex.com/en/blog/apollo-io-b2b-sales-platform-mena/
- Sulsaly (MENA Apollo alternative; coverage claim): https://sulsaly.com/vs/apollo
- Cequens WhatsApp BSP: https://www.cequens.com/products/whatsapp-business-api ; Unifonic: https://www.unifonic.com/en/channels/whatsapp
- Sbl.so, *WhatsApp Cold Outreach Reply Rates: 2026 Benchmarks* (cold 1–5%, semi-warm 8–15%, warm 20–50%+, block <1%, July 2025 per-message pricing): https://sbl.so/whatsapp/whatsapp-cold-outreach-reply-rates/
- Meta, WhatsApp opt-in requirement (Nov 2024 policy): https://developers.facebook.com/documentation/business-messaging/whatsapp/getting-opt-in
- WhatsApp state of business messaging / 73.3% prefer messaging: https://whatsappbusiness.com/resources/resource-library/state-of-business-messaging/
- Saudi PDPL — full effect 14 Sep 2024, SDAIA, fines to SAR 5M: https://www.ampcuscyber.com/knowledge-hub/what-is-pdpl/ and https://www.clydeco.com/en/insights/2025/02/data-protection-privacy-landscape-in-me
- UAE PDPL, Federal Decree-Law 45/2021: https://www.dlapiperdataprotection.com/countries/uae-general/law.html
- Lebanon Law 81/2018 (unsolicited marketing prohibited): https://www.dlapiperdataprotection.com/?t=law&c=LB
- Google Maps Platform Service Specific Terms (30-day lat/lng cache) and Places API policies (no pre-fetch/cache/store except place_id): https://cloud.google.com/maps-platform/terms/maps-service-terms and https://developers.google.com/maps/documentation/places/web-service/policies
- Maroof — KSA Ministry of Commerce national register of online stores: https://www.mc.gov.sa/en/mediacenter/News/Pages/29-03-23-02.aspx and https://business.sa/
- Lebanon digital agencies (market context): https://techbehemoths.com/companies/digital-marketing/lebanon and https://clutch.co/lb/agencies/digital-marketing
