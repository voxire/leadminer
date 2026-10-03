# 061 — Outreach & Data-Source Compliance Checklist

## Verdict

The scraper's collection logic has one hard blocker and several conditional ones. The Google Places leg as built — persisting business names, addresses, phone numbers, websites and review counts to CSV and Google Drive — directly violates the Google Maps Platform Terms of Service ("copy and save business names, addresses, or user reviews"; "use … in a listings or directory service or to create or augment an advertising product"), which can get the API key suspended with no warning. The other two sources (OSM, Wikidata) are permissively licensed but carry attribution/share-alike duties the code never performs. On the outreach side there is no sending code today, but the moment the agency cold-emails or cold-messages the scraped contacts, Saudi PDPL and Lebanese Law 81/2018 both require prior consent plus an opt-out mechanism and recordkeeping — and a scraped WhatsApp number is never "opt-in" under WhatsApp's Business Messaging Policy.

## Scope: what this system does, and what it would do

Today `leadminer` is collection-only: three scrapers (`main.py:125-139`) → category filter → dedup → enrichment (which regexes email / Instagram / WhatsApp / LinkedIn out of live websites, `enricher.py:191-213`) → scoring → five CSVs (`main.py:174-178`) → rclone upload to Google Drive (`.github/workflows/scrape.yml:46-50`). There is **no** email, SMS, or WhatsApp sending code anywhere in the repo. Outreach is the stated end goal: `pitch_recommender.py:1-9` says the output is "a starting hypothesis on the first outbound message", and the product sells the pitch to the business (`README.md`). This checklist therefore covers two phases: **(A)** the collection/storage that runs today, and **(B)** the outreach that would run next. Findings are keyed to the phase they break.

---

## Findings

### S1 — Google Places: the pipeline persists and redistributes Google "places data" in breach of the ToS

- **Where:** `scrapers/google_places.py:31-42` (field mask requests `places.nationalPhoneNumber`, `places.websiteUri`, `places.rating`, `places.userRatingCount`); `scrapers/google_places.py:249-273` (builds `BusinessRecord` with `name`, `address`, `phone`, `website`, `rating`, `review_count`, `source="google_places"`); `main.py:174-178` (writes `all_businesses.csv` / `sales_ready.csv`); `.github/workflows/scrape.yml:46-50` (rclone to Google Drive).
- **Breaks:** Google Maps Platform Terms of Service §3.2.3(a) "No Scraping" states: *"Customer will not export, extract, or otherwise scrape Google Maps Content for use outside the Services. For example, Customer will not: … (ii) bulk download … places information …; (iii) copy and save business names, addresses, or user reviews."* The ToS defines "Google Maps Content" to include *"places data (including business listings)"*. §3.2.3(d)(iii) separately bars using the Services *"in a listings or directory service or to create or augment an advertising product"* — a lead list for an ad agency is squarely both. §3.2.3(b) bars caching Google content. This is not a gray area: saving `name`+`address`+`rating`+`review_count` into a cumulative CSV and shipping it to Drive is the exact conduct the clause names.
- **Also:** §3.2.3(c)(iv) bars *"use latitude/longitude values from the Places API as an input for point-in-polygon analysis."* `enricher.py:79-94` does coordinate→region box-matching on the Places coordinates (`google_places.py:243`), which is point-in-polygon under any reasonable reading.
- **Consequence:** Google may suspend the project or terminate the API key for ToS breach (§5.1/§5.2), with zero recourse; the scraped Google-derived rows also contaminate the combined CSVs (which mix in OSM/Wikidata) with data that must not be redistributed.
- **Fix:** Drop the Google Places source for this use, or replace it with a licensed business-data provider whose terms permit lead-generation use. Do not "anonymize" or "cache then delete" — the terms prohibit the *collection and saving* itself, not just redistribution.

### S1 — Saudi PDPL: storing and cold-contacting KSA contacts has no legal basis

- **Where:** KSA queries in `scrapers/google_places.py:82-124`; `main.py:174-178` persistence; `.github/workflows/scrape.yml:46-50` upload to Google Drive; hypothetical send from `sales_ready.csv`.
- **Breaks (collection phase, today):** The Personal Data Protection Law (Royal Decree M/19, 16 Sep 2021; enforced 14 Sep 2023, grace ended 14 Sep 2024) defines "Personal Data" to include *"contact numbers"* and *"addresses"* and applies **extraterritorially** to anyone processing personal data of individuals residing in KSA (ICLG §3.1). Many scraped KSA records are sole traders or named individuals (a mobile number or a `firstname.lastname@` email identifies a natural person). Collecting and storing those on Google Drive (servers almost certainly outside KSA) is a **cross-border transfer** requiring minimum-data discipline, an adequacy/safeguards basis (SDAIA SCCs/Binding Common Rules) and a transfer risk assessment (ICLG §12). The code performs none of this and stores indefinitely (`all_businesses.csv` is a cumulative "all time" master, `main.py:1-12`).
- **Breaks (outreach phase, would-do):** PDPL direct-marketing controls require **consent** and an opt-out mechanism *"as easy as to give consent"*, a clearly stated sender identity, immediate stop on withdrawal, and preservation of consent records (ICLG §10.1). Critically, §10.2 states *"The PDPL does not draw any differentiation, in the context of business-to-consumer or business-to-business marketing"* — the common "B2B is exempt" assumption does **not** hold here, and §10.4 confirms the controls apply to marketing sent from outside KSA. Cold emailing the `sales_ready.csv` list with no consent is therefore non-compliant.
- **Consequence:** fines up to SAR 5,000,000, doubled on repeat (SAR 3,000,000 or up to 2 years' imprisonment where sensitive data is involved) (ICLG §10.7, §16.4). Registration with SDAIA's national register is also mandatory if the controller's *"main activity is based on personal data processing"* (ICLG §7.3), which describes a lead-gen business.
- **Fix:** For KSA, establish a lawful basis (consent, or rely on legitimate-interest only with a defensible balancing test), record it per recipient, and gate every send on it. Treat `country="SA"` rows as personal data by default until proven otherwise.

### S2 — OSM/Overpass: ODbL share-alike + attribution, and bulk-extract etiquette

- **Where:** `scrapers/osm.py:10-26` (single country-wide Overpass POST for Lebanon); `scrapers/osm.py:86-90` (extracts `phone`/`email`/`website`/`facebook`/`instagram` tags); no attribution anywhere in output or `README.md`.
- **Breaks:** OSM data is licensed ODbL: free to copy/adapt **provided** you credit "OpenStreetMap and its contributors" and distribute derived data under the same (share-alike) license (openstreetmap.org/copyright). The CSVs are a derivative database of OSM and are redistributed to Google Drive for sales use — attribution is required and share-alike attaches to anything further distributed. Separately, the Overpass main instance's published usage policy permits roughly <10k queries / <1 GB per day for one-off use, but tells **commercial** users to self-host or use a paid instance and, for country-sized extracts, to prefer Geofabrik/planet dumps rather than a live country-wide query (wiki.openstreetmap.org/wiki/Overpass_API, "Public Overpass API instances").
- **Consequence:** Not a legality risk to *scraping* (ODbL permits it), but an unaddressed **distribution** obligation and a ToS-etiquette risk on a shared public server. The worst practical outcome is a polite block from the Overpass operator and an unfulfilled attribution duty.
- **Fix:** Add "© OpenStreetMap contributors, ODbL" attribution to every distributed artifact; keep OSM-derived rows separate from Google-derived rows (the latter must not be redistributed, which conflicts with OSM share-alike if mixed); switch to Geofabrik country extracts for bulk.

### S2 — Lebanese Law 81/2018: consent and anti-spam provisions are real but dormant

- **Where:** Lebanon is the primary market (`scrapers/osm.py:10-26`, `scrapers/wikidata.py:12`, Google Lebanon queries); email/phone extraction in `enricher.py:191-213`.
- **Breaks:** Law No. 81/2018 (Electronic Transactions and Personal Data) requires that personal data be collected *"faithfully and for legitimate, specific, and explicit purposes"*, that controllers inform data subjects of identity/purposes/recipients/rights, and that any processing entity file a declaration and obtain a permit from the Ministry of Economy and Trade **unless** an exemption applies (notably: prior consent, or processing for one's *own* clients/customers within their legal activity — which does not cover strangers scraped off the web). On marketing specifically: *"It is forbidden to communicate unsolicited marketing and advertising emails (SPAM) using a real person's name and address, unless that person has consented … except … through a previous engagement."* Individuals also have a right to object to marketing use (DLA Piper, Lebanon chapter).
- **Consequence:** **Low enforcement today** — Lebanon has no operational data-protection authority and no administrative enforcement; remedies are private actions before the courts (DLA Piper). This is a real legal exposure with limited near-term enforcement risk.
- **Fix:** Still obtain consent or rely on the "previous engagement" exception and honor opt-outs; file the MOET declaration if processing personal data of identifiable individuals. Cheap to do, and it future-proofs against the authority eventually being stood up.

### S2 — WhatsApp: a scraped number is never opt-in under the Business Messaging Policy

- **Where:** `enricher.py:129-132` (WhatsApp regex over `wa.me` / `api.whatsapp.com` links) and `enricher.py:204-206` (saves the number); `main.py:167-170` promotes such records to `sales_ready`.
- **Breaks:** WhatsApp Business Messaging Policy: *"You may only contact people on WhatsApp if: (a) they have given you their mobile phone number or username; and (b) you have received opt-in permission from the recipient confirming that they wish to receive subsequent messages or calls from you."* A number scraped from a public website satisfies neither. The policy further reserves the right to *"limit or remove your access"* for *"messaging people at scale in an unauthorized manner"*, and prohibits automating bulk sends through the consumer app (outside the approved-template Business Platform flow).
- **Consequence:** Using scraped WhatsApp numbers for cold outreach gets the sender's number/account banned, and the underlying conduct is, in substance, unsolicited marketing that independently trips the Saudi and Lebanese consent rules above.
- **Fix:** Treat WhatsApp as an **opt-in-only** channel: only message numbers whose owners have affirmatively opted in, capture that opt-in with a timestamp and channel, and honor "STOP"-style opt-outs. Do not load `enricher.py`-scraped WhatsApp numbers into a cold-send list.

### S3 — Wikidata: no license burden, but attribution etiquette and the personal-data angle remain

- **Where:** `scrapers/wikidata.py:10-26` (SPARQL) and `scrapers/wikidata.py:34-38` (User-Agent).
- **Breaks (low):** Wikidata structured data is CC0 — no attribution requirement and no share-alike, so there is no license problem. The code already sets a proper User-Agent (`wikidata.py:34-38`), which is the main etiquette requirement (Wikidata:Data access). Two minor notes: the query returns `phone`/`email` for entities (some of which are people, not businesses, since it selects all `wdt:P17 wd:Q822` entities, not just companies), so the same personal-data considerations as above apply to those rows; and a "Powered by Wikidata" attribution is *appreciated* (not required) but currently absent.
- **Fix:** None required for license compliance. Optionally add "Powered by Wikidata" and filter the entity selection to organizations if the intent is business leads only.

---

## The compliance checklist

Use this as a gate. Items marked **now** must be true before the next scrape is shipped; items marked **pre-send** must be true before the first outbound email/WhatsApp/SMS.

### A. Data-source licensing & terms

- [ ] **now — Google Places** Remove `GooglePlacesScraper` and its queries (`scrapers/google_places.py`), or swap to a licensed business-data provider whose terms permit lead-gen persistence and redistribution. Do not re-add Places until written confirmation of permitted use.
- [ ] **now — Google Places** Confirm no Google-derived rows remain in `all_businesses.csv` / `sales_ready.csv`; purge and re-scrape from permitted sources if contaminated.
- [ ] **now — OSM attribution** Add "© OpenStreetMap contributors — ODbL" to every distributed artifact (CSV headers/README, and the Drive folder) and to any downstream product.
- [ ] **now — OSM share-alike** Keep OSM-derived rows attributable and distributed under ODbL; if you redistribute combined data, ensure the OSM share-alike obligation is not diluted by mixing in non-redistributable sources.
- [ ] **now — OSM etiquette** Move bulk Lebanon extraction from the live Overpass instance to Geofabrik country extracts (or a paid/self-hosted Overpass); keep the `User-Agent` with a real contact (`osm.py:35-36`).
- [ ] **now — Wikidata** Keep the identifying User-Agent (`wikidata.py:34-38`); add "Powered by Wikidata" if you publish results; restrict the query to organizations if leads must be businesses.

### B. Saudi PDPL — KSA data

- [ ] **now — classification** Decide and record, per KSA record, whether it is personal data (sole trader or named individual = personal; general corporate `info@` / landline may not be). Default to "personal" when unsure.
- [ ] **now — legal basis** Establish and record a lawful basis for *collecting/storing* KSA personal data (consent, or a documented legitimate-interest balancing test). No basis → do not store.
- [ ] **now — registration** Determine whether the operator is required to register as a Controller with SDAIA ("main activity based on personal data processing") and register on the national data-governance platform if so (ICLG §7).
- [ ] **now — privacy notice** Publish a privacy notice naming the controller, contact/DPO, purpose, retention period, data-subject rights, and consent-withdrawal procedure (ICLG §5.1).
- [ ] **now — retention** Replace the "all time" master accumulation with a defined retention window and a deletion process (PDPL retention principle; ICLG §4).
- [ ] **now — cross-border** For KSA personal data stored on Google Drive (non-KSA): document minimum-data discipline, an adequacy/safeguards basis (SDAIA SCCs or Binding Common Rules), and a transfer risk assessment (ICLG §12).
- [ ] **now — breach** Define a breach response: notify SDAIA within 72 hours and affected individuals without undue delay (ICLG §16.2–16.3).
- [ ] **now — security** Apply the National Cybersecurity Authority controls (or recognized best practice) to the stored data; the current model — CSVs in Drive with no access control, no encryption, no audit log — is insufficient (ICLG §16.1).
- [ ] **pre-send — consent** Obtain and record opt-in consent before any KSA marketing contact; do not rely on a B2B carve-out (none exists — ICLG §10.2).
- [ ] **pre-send — opt-out** Provide an opt-out that is *as easy as consent*; state sender identity clearly; stop on withdrawal with no fee (ICLG §10.1).
- [ ] **pre-send — records** Preserve consent and opt-out records; be able to evidence the legal basis per recipient (ICLG §10.1).

### C. Lebanese law

- [ ] **now — purpose & notice** Ensure collection is for "legitimate, specific and explicit purposes" and the controller's identity/purposes/recipients/rights are disclosed where applicable (DLA Piper, Lebanon).
- [ ] **now — permit/declaration** Confirm whether the operator must file a declaration and obtain a Ministry of Economy and Trade permit; the "clients and customers" exemption does not cover strangers scraped from public sources.
- [ ] **pre-send — anti-spam** For email to individuals: obtain consent, or rely only on a documented "previous engagement" exception; never send to a scraped personal address without one of the two (Law 81/2018).
- [ ] **pre-send — objection** Honor any request to stop marketing processing promptly (right to object).

### D. Pre-send gating — email and WhatsApp (both markets)

- [ ] **pre-send — suppression list** Maintain a single cross-channel suppression list (email + phone + WhatsApp handle). De-duplicate every outbound list against it before every send; never re-add an opted-out contact via a later scrape (this is the single most common real-world violation, since `dedup.py` re-merges new scrapes into the master).
- [ ] **pre-send — opt-out mechanism** Email: working one-click unsubscribe link (honored ≤10 business days, ideally immediately). WhatsApp/SMS: accept a "STOP"/"unsubscribe" keyword and honor it immediately. Make opt-out free and at least as easy as opt-in.
- [ ] **pre-send — WhatsApp opt-in** Only message numbers with affirmative, timestamped opt-in. Scraped `wa.me` numbers from `enricher.py:204-206` are **not** opt-in; exclude them from cold lists (WhatsApp Business Messaging Policy).
- [ ] **pre-send — channel rule** Use WhatsApp Business Platform approved templates for any out-of-window message; do not automate bulk sends through the consumer WhatsApp app.
- [ ] **pre-send — recordkeeping** Log, per message: recipient identity, channel, content/template, timestamp, the legal basis relied on, and the consent/opt-out state. Retain opt-out evidence and a data-subject-request log.
- [ ] **pre-send — sender identity** Identify the sender (business name + valid contact) in every message; no misleading or anonymized sender (Saudi PDPL §10.1).
- [ ] **pre-send — residual jurisdictions** If any scraped contact turns out to be EU/UK/Swiss (GDPR) or US (CAN-SPAM), apply their consent/opt-out requirements too; email domains do not reliably map to geography, so route by the recipient's actual location where known.

### E. Cross-cutting

- [ ] **now — data inventory** Document what personal data is collected, why, where it is stored, who can access it, and how long it is kept (prerequisite for B and C).
- [ ] **now — access control** Restrict the Google Drive destination and any runtime secrets to named individuals; the current model shares an entire `data/` tree broadly (`.github/workflows/scrape.yml:46-50`).
- [ ] **now — legal review** Have the two-market plan reviewed by counsel qualified in Saudi and Lebanese data law before the first outbound send; this checklist is operational, not legal advice.

---

## Recommended order of work

1. **Kill the Google Places source** (S1-A) and purge Google-derived rows — this is the only item that can get the product killed by a vendor overnight, and it is unambiguous.
2. **Add OSM/Wikidata attribution** and switch to Geofabrik extracts (S2-A) — cheap, mechanical, removes the remaining source-side exposure.
3. **Stand up the Saudi PDPL baseline** for stored KSA data: classification, legal basis, retention, cross-border basis, privacy notice (B).
4. **Build the pre-send gate** before any outreach: suppression list, opt-out mechanism, consent capture, per-message recordkeeping, WhatsApp opt-in-only rule (D).
5. **File the Lebanese declaration/permits and enforce the anti-spam rule** (C) — lowest urgency (dormant enforcement) but needed for completeness.
6. **Legal review** (E) as a final sign-off before the first campaign.

---

## Sources

- Google Maps Platform Terms of Service, §§3.2.3 (No Scraping / No Caching / No Creating Content From Google Maps Content), 3.2.3(d)(iii), 5.1–5.2, and "Google Maps Content" definition — https://cloud.google.com/maps-platform/terms (fetched 2026-10-03).
- OpenStreetMap Copyright and License (ODbL, attribution, share-alike) — https://www.openstreetmap.org/copyright (fetched 2026-10-03).
- Overpass API — "Public Overpass API instances" usage policy (query/data limits, commercial-use guidance, prefer dumps for country-sized extracts) — https://wiki.openstreetmap.org/wiki/Overpass_API (fetched 2026-10-03).
- Wikidata:Data access (CC0, User-Agent policy, "Powered by Wikidata" attribution) — https://www.wikidata.org/wiki/Wikidata:Data_access (fetched 2026-10-03).
- ICLG, *Data Protection Laws and Regulations 2026 — Saudi Arabia* (PDPL M/19 of 16 Sep 2021; enforcement 14 Sep 2023 / grace to 14 Sep 2024; definitions; §3.1 extraterritorial scope; §7 registration; §10 marketing incl. §10.2 B2B parity, §10.4 extra-territorial marketing, §10.7 penalties; §12 cross-border transfers; §16 breach) — https://iclg.com/practice-areas/data-protection-laws-and-regulations/saudi-arabia (fetched 2026-10-03).
- DLA Piper, *Data Protection Laws of the World — Lebanon* (Law No. 81/2018; no national authority; MOET permits; anti-spam rule; right to object to marketing; court-based enforcement; last reviewed 2022) — https://www.dlapiperdataprotection.com/index.html?t=law&c=LB (fetched 2026-10-03).
- WhatsApp Business Messaging Policy (opt-in requirement; "messaging people at scale in an unauthorized manner"; best practices for opt-in/opt-out; updated 23 Sep 2026) — https://business.whatsapp.com/policy (fetched 2026-10-03).

> Note on currency: the Lebanese authority/summary was last reviewed 2022 and Law 81/2018's implementing framework remains partially unbuilt as of this writing; the Saudi PDPL implementing regulations and the "Regulation on Personal Data Transfer Outside the Kingdom" are in force. Re-verify both before relying on them for a live campaign.
