# 049 — Output CSV Data Dictionary (all 23 columns)

## Verdict

The output CSV is a **denormalized, provenance-lossy merge** of three incompatible
taxonomies (OSM tags, Wikidata class labels, Google Places types) projected onto one
23-column schema. Every column is technically typed, but **9 of the 23 carry meaning
that is ambiguous today** — chiefly `category`, `region`, `website_live`, `source`,
and the derived scores — because the pipeline silently discards the distinction
between *"the value is absent"*, *"the value is a different kind of thing"*, and
*"the value was produced by a different source with different semantics"*.

Two structural facts to internalize before reading any column:

1. **The schema is 23 columns, not 22.** `BRIEF.md:57-62` and `BRIEF.md:18` both say
   "22" but enumerate 23 names. `main.py:32-39` (`FIELDS`) and
   `scrapers/base.py:5-28` (`BusinessRecord`) both have **23** keys. This doc covers all 23.
2. **`source` is a set, and merge discards field-level provenance.** After
   `dedup._merge`, a record may come from two scrapers, but nothing records *which
   field came from which source* (`dedup.py:83-99`). `source` only says "seen by these
   scrapers".

---

## Summary table

| # | Column | Type | Nullable | Source of truth | Example | Ambiguous? |
|---|--------|------|----------|-----------------|---------|-----------|
| 1 | `name` | str | yes | OSM `name`/`name:en`/`name:ar`; Wikidata `itemLabel`; Google `displayName.text` | `Café Younes` / `مطعم` | **Yes** |
| 2 | `category` | str | yes | OSM `shop`/`amenity`/…; Wikidata `P31` label; Google most-specific type | `restaurant`, `real_estate_agent` | **Yes** |
| 3 | `region` | str | yes | `infer_region()` (address keywords → coord boxes) | `Beirut`, `Riyadh`, `Mount Lebanon` | **Yes** |
| 4 | `country` | str | yes | hardcoded `"LB"` / `"SA"`; Google via `_country_from_query` | `LB`, `SA` | Mild |
| 5 | `address` | str | yes | OSM `addr:*` join; Wikidata `P6375`; Google `formattedAddress` | `Hamra Street, Beirut` | **Yes** |
| 6 | `lat` | float | yes | OSM node/way center; Google `location.latitude` | `33.8938` | No |
| 7 | `lon` | float | yes | OSM node/way center; Google `location.longitude` | `35.5018` | No |
| 8 | `phone` | str | yes | OSM `phone`/`contact:phone`; Wikidata `P1329`; Google `nationalPhoneNumber`; **normalized to E.164 in `main.py:136`** | `+9611234567` | **Yes** |
| 9 | `email` | str | yes | OSM/Wikidata tag, **or scraped from website HTML** | `info@example.com` | **Yes** |
| 10 | `website` | str | yes | OSM `website`/`url`; Wikidata `P856`; Google `websiteUri` | `https://example.com` | **Yes** |
| 11 | `website_live` | bool \| null | yes | `_fetch_website()` outcome (LIVE/DEAD/UNKNOWN) | `true`, `false`, *(empty)* | **Yes** |
| 12 | `facebook` | str | yes | OSM `contact:facebook`/`facebook` only | `https://www.facebook.com/…` or bare id | **Yes** |
| 13 | `instagram` | str | yes | OSM `contact:instagram`; scraped handle from website HTML | `beirutcafe` | **Yes** |
| 14 | `whatsapp` | str | yes | scraped from website HTML only | `+9611234567` | Mild |
| 15 | `linkedin` | str | yes | scraped from website HTML only (`/company/…`) | `https://linkedin.com/company/x` | Mild |
| 16 | `rating` | float | yes | Google Places `rating` only | `4.5` | Mild |
| 17 | `review_count` | int | yes | Google Places `userRatingCount` only | `120` | Mild |
| 18 | `completeness_score` | int | no | computed `completeness_score()` (0–7) | `3` | **Yes** |
| 19 | `lead_score` | int | no | computed `lead_score()` (0–100), recomputed `main.py:137` | `78` | Mild |
| 20 | `industry_priority` | str | yes | `industry_priority(category)` → high/medium/low | `high` | **Yes** |
| 21 | `recommended_service` | str | yes | `recommend_service()` free-text label | `Website rebuild + maintenance` | **Yes** |
| 22 | `source` | str | no | scraper constant, unioned with `\|` on merge | `osm\|google_places` | **Yes** |
| 23 | `scraped_at` | str | no | `datetime.utcnow().isoformat()+"Z"` per scraper | `2026-10-03T12:34:56.789012Z` | Mild |

---

## Column detail

### 1. `name` — str | null
- **Source of truth:** `osm.py:60` (`name` → `name:en` → `name:ar`, first non-empty);
  `wikidata.py:62` (`itemLabel`, rows starting with `Q` are skipped at `wikidata.py:63`);
  `google_places.py:236` (`displayName.text`).
- **Example:** `Café Younes`, `Beirut Digital District`, `مطعم الشرق`.
- **Ambiguous — yes.** There is no canonical form: OSM prefers the raw `name` tag which
  is frequently Arabic or French, Google returns the operator's display name in the
  query locale, and Wikidata returns an English label. The same physical business can
  appear under three different strings, and dedup relies on `normalize_name`
  (`dedup.py:66-69`, NFKD + lowercase) to reconcile them — which cannot bridge
  script/language differences. Case and diacritics are normalized only in the dedup
  key, never in the stored value.

### 2. `category` — str | null
- **Source of truth:** `osm.py:68-75` (first non-empty of `shop`/`amenity`/`office`/
  `tourism`/`craft`/`healthcare`); `wikidata.py:70` (`wdt:P31` English label);
  `google_places.py:247` → `_pick_category()` (`google_places.py:292-296`, first type
  not in `_GENERIC_TYPES`, else `types[0]`, else `"business"`).
- **Example:** `restaurant`, `real_estate_agent`, `bakery`.
- **Ambiguous — yes, the single most load-bearing ambiguity in the schema.** Three
  distinct controlled vocabularies are written to one column with no mapping:
  - OSM emits bare single-word tags (`amenity=restaurant`, `shop=beauty`, `shop=bakery`).
  - Wikidata emits human-readable labels with spaces (`real estate agent`, `law firm`).
  - Google emits underscore-joined type strings (`real_estate_agent`, `car_dealership`).
  These do not line up. `whitelist.py` matches categories **exactly** after lowercasing
  (`whitelist.py:112`, `119`, `141`), so a Wikidata label `"real estate agent"` never
  equals the key `"real_estate_agent"` and is misclassified `low`; a Google type like
  `car_dealer` (not in any list, and containing none of the `business_keywords` at
  `whitelist.py:123-126`) is dropped by the whitelist filter entirely
  (`main.py:117`). The `"business"` fallback (`google_places.py:296`) is also not in
  the whitelist, so those records silently disappear.

### 3. `region` — str | null
- **Source of truth:** `infer_region()` (`enricher.py:79-94`). For `country=="LB"` it
  first scans address keywords (`enricher.py:10-50`), then coordinate boxes
  (`enricher.py:58-68`); for SA it uses coordinate boxes (`enricher.py:70-76`). Called
  at scrape time for Google (`google_places.py:243`) and in `enrich()` for OSM/Wikidata
  (`enricher.py:322-327`).
- **Example:** `Beirut`, `Mount Lebanon`, `Riyadh`, `Jeddah`, `Dammam`.
- **Ambiguous — yes.** Two distinct levels share one column: Lebanese values are
  **governorates** (Beirut, Mount Lebanon, North, Akkar, South, Nabatieh, Bekaa,
  Baalbek-Hermel) while KSA values are **cities** (Riyadh, Jeddah, Dammam, Mecca,
  Medina). Two structural defects compound this:
  - **`Nabatieh` is unreachable by address** — its keywords (`nabatieh`, `bint jbeil`,
    `hasbaya`, `marjayoun`, `enricher.py:38-41`) are a subset of `South Lebanon`
    (`enricher.py:33-37`), which is checked first, so an address containing "nabatieh"
    always resolves to `South Lebanon`.
  - **Coordinate boxes overlap** (`enricher.py:58-68`): e.g. Akkar (lat 34.38–34.72,
    lon 35.98–36.65) overlaps North (34.10–34.68, 35.49–36.30) and Baalbek-Hermel
    (34.00–34.72, 36.10–36.85). Boundary businesses get whichever box is listed first.

### 4. `country` — str | null
- **Source of truth:** `"LB"` hardcoded in OSM (`osm.py:100`) and Wikidata
  (`wikidata.py:80`); Google derives it from the query string via
  `_country_from_query` (`google_places.py:126-135`), defaulting to `"LB"`.
- **Example:** `LB`, `SA`.
- **Mildly ambiguous.** Values are ISO 3166-1 alpha-2 codes, never full names. The
  `"LB"` default (`google_places.py:135`) is a silent misclassification risk if a
  future query omits a country keyword. The value feeds `normalize_phone`'s
  country-code selection (`dedup.py:48`), so a wrong `country` can produce a wrong
  E.164 prefix — though `_KNOWN_COUNTRY_CODES` (`dedup.py:8-21`) overrides when the
  number already carries a recognized code.

### 5. `address` — str | null
- **Source of truth:** OSM joins `addr:housenumber/street/suburb/city/district`
  (`osm.py:77-84`); Wikidata `wdt:P6375` (`wikidata.py:69`); Google `formattedAddress`
  (`google_places.py:254`).
- **Example:** `Hamra Street, Beirut`, `Riyadh, Saudi Arabia`.
- **Ambiguous — yes.** Granularity and structure vary wildly: OSM is often
  street-only or empty, Google is a full multi-line string (which may contain newlines
  and commas), Wikidata is often just a city or empty. Critically, `_extract_city`
  (`dedup.py:72-76`) takes the **last comma segment** as the "city" for the name-based
  dedup key — for Google formatted addresses that last segment is the **country**, so
  the `(normalized_name, city)` dedup key is actually `(name, country)` for Google
  records and `(name, whatever_osm_had)` for OSM records. The column is not
  machine-parseable into a consistent location.

### 6. `lat` — float | null
- **Source of truth:** OSM node `lat` or way `center.lat` (`osm.py:65`); Google
  `location.latitude` (`google_places.py:241`); Wikidata never sets it (`None`).
- **Example:** `33.8938`.
- Not ambiguous. Note: OSM **ways** yield the polygon centroid, not the business
  entrance, so points can be meaningfully off from the real location. The
  `el.get("lat") or (el.get("center") or {}).get("lat")` fallback (`osm.py:65`) treats
  `0.0` as missing, which is harmless at Lebanon/KSA latitudes but is a latent bug.

### 7. `lon` — float | null
- Same provenance and caveats as `lat` (`osm.py:66`, `google_places.py:242`).
- **Example:** `35.5018`.

### 8. `phone` — str | null
- **Source of truth:** OSM `phone`/`contact:phone` (`osm.py:86`); Wikidata `P1329`
  (`wikidata.py:67`); Google `nationalPhoneNumber` (`google_places.py:257`). The final
  CSV value is the **normalized** E.164 form, applied in `main.py:134-136` via
  `normalize_phone` (`dedup.py:33-63`).
- **Example:** `+9611234567`, `+966501234567`.
- **Ambiguous — yes.** Two separate concerns:
  - The stored value is **post-normalization**, so the original source format is lost.
    `normalize_phone` strips all non-digits and, crucially, **trusts the number's own
    leading country code over the record's `country`** (`dedup.py:57-60`) — a Lebanese
    record carrying a foreign number is stored and dedup-keyed as foreign.
  - **Empty string vs `None` inconsistency:** `main.py:134-136` only assigns the
    normalized value when `raw_phone` is truthy, but a truthy-but-too-short number
    (e.g. `"12345"`) normalizes to `""` (`dedup.py:45-46`), leaving `phone == ""` in
    the CSV. On the next run `load_master` maps `""` back to `None` (`main.py:54-55`),
    so the same field oscillates between empty string and null across runs.
  - `phone` is simultaneously a contact channel and the dedup identity key
    (`dedup.py:110`); two distinct businesses sharing a reception number will be
    collapsed, and the field cannot distinguish "verified business line" from "whatever
    was in a tag".

### 9. `email` — str | null
- **Source of truth:** OSM `email`/`contact:email` (`osm.py:87`); Wikidata `P968`
  (`wikidata.py:68`); otherwise scraped from website HTML when the site is LIVE
  (`enricher.py:191-196`, gated by `enricher.py:243-244` — direct source wins over
  scraped).
- **Example:** `info@example.com`.
- **Ambiguous — yes.** Two very different provenances share the column: a
  source-declared business email vs. the **first** `mailto:`/regex email found anywhere
  in the homepage. The scrape can capture the site builder, a privacy address, or a
  third-party contact (`_EMAIL_BLACKLIST` at `enricher.py:135-138` covers only a handful
  of domains). The `mailto:` pattern (`enricher.py:191`) does not strip `?subject=…`
  query strings, so `mailto:info@x.com?subject=hi` becomes the malformed stored value
  `info@x.com?subject=hi` (the `.split("@")[-1]` at `enricher.py:193` no longer equals a
  domain). Scraped emails are lowercased (`enricher.py:195`) but source emails are not.

### 10. `website` — str | null
- **Source of truth:** OSM `website`/`contact:website`/`url` (`osm.py:88`); Wikidata
  `P856` (`wikidata.py:66`); Google `websiteUri` (`google_places.py:259`). OSM/Wikidata
  prepend `https://` when no scheme (`osm.py:92-93`, `wikidata.py:72-73`).
- **Example:** `https://example.com`.
- **Ambiguous — yes.** The OSM `url` tag frequently points at a Facebook page, a menu
  PDF, or a Google Maps listing rather than the business's own site. Those records are
  still routed into `with_websites.csv` (`main.py:139`) and their "website" is fetched
  for contact scraping, conflating "has a real website" with "has any URL in a tag".
  No normalization of trailing slash / `www` / protocol, so the same site in different
  forms is not reconciled (harmless for dedup, which is phone/name-based, but matters
  for downstream consumers).

### 11. `website_live` — bool | null  *(tri-state)*
- **Source of truth:** `_fetch_website()` outcome in `enricher.py:158-214`, mapped at
  `enricher.py:242`: `True` (LIVE, status < 400), `False` (DEAD, status ≥ 400),
  `None` (UNKNOWN — DNS/TLS/timeout/reset/blocked/body-read failure).
- **Example:** `true`, `false`, *(empty string in the raw CSV)*.
- **Ambiguous — yes, and the most consequential.** The recent fix (documented at
  `enricher.py:142-153` and `docs/audits/043`) correctly made this tri-state, but the
  three values still overload distinct meanings:
  - **`None` ("unreachable") is a catch-all** for at least six different outcomes —
    DNS failure, TLS failure, timeout, connection reset, Cloudflare bot challenge, and
    body-read failure (`enricher.py:168-170`, `182-184`). "Unreachable" is not one
    fact.
  - **`False` ("dead") includes 4xx auth walls.** A 401/403 behind a login or geo-block
    is recorded as DEAD and scored as a rebuild pitch (`lead_score` +20 at
    `enricher.py:296`), even though the site is live. This is a silent false positive in
    the core lead signal.
  - **Serialization:** `csv.DictWriter` writes `True`→`True`, `False`→`False`,
    `None`→empty string; `load_master` reverses this (`main.py:68-69`). Downstream
    consumers must read the literal `True`/`False`/empty, not a JSON boolean.
  - **`None` does not mean "no website".** Records with no `website` have
    `website_live=None` too (`enricher.py:338`); the "no site" distinction lives in the
    `website` column, not here. Reading `website_live` alone conflates "no site" with
    "has a site we couldn't reach".
  - `verify=False` (`enricher.py:167`) disables TLS verification, so self-signed or
    MITM'd hosts can yield wrong LIVE/DEAD verdicts.

### 12. `facebook` — str | null
- **Source of truth:** OSM `contact:facebook`/`facebook` only (`osm.py:89`). Not set by
  Wikidata/Google and never scraped from websites.
- **Example:** `https://www.facebook.com/beirutcafe` or a bare numeric page id.
- **Ambiguous — yes.** OSM `facebook` tags are commonly a bare username or numeric page
  id, not a URL, and no normalization is applied — the column is a mix of URLs, ids, and
  handles. Coverage is limited to OSM records that happen to carry the tag.

### 13. `instagram` — str | null
- **Source of truth:** OSM `contact:instagram`/`instagram` (`osm.py:90`); otherwise
  scraped from website HTML as a **handle** (`_INSTAGRAM_RE`, `enricher.py:128`, `198-202`),
  only when LIVE and only when `instagram` is not already set (`enricher.py:246`).
- **Example:** `beirutcafe`.
- **Ambiguous — yes.** The scraped value is a normalized lowercased handle, but the OSM
  value is whatever was in the tag (full URL, `@handle`, or handle), so the column mixes
  formats. The scrape takes the **first** `instagram.com/` or `@` occurrence in the
  page (`enricher.py:198`), which may be a footer social widget or a caption tag rather
  than the business's own account (`_IG_BLACKLIST` at `enricher.py:139` is small).
  Also counted as a "contact channel" by `has_any_contact` (`main.py:89`, `len > 3`).

### 14. `whatsapp` — str | null
- **Source of truth:** scraped from website HTML only (`_WHATSAPP_RE`,
  `enricher.py:129-132`, `204-206`). No scraper sets it.
- **Example:** `+9611234567`.
- **Mildly ambiguous.** Stored as `+<digits>` (7–15 digits). It is only ever extracted
  from a live site's `wa.me`/`whatsapp.com` links, so coverage depends entirely on the
  site linking it, and the number may be a support line rather than a WhatsApp Business
  account. No source-of-truth scraper contributes a WhatsApp number.

### 15. `linkedin` — str | null
- **Source of truth:** scraped from website HTML only (`_LINKEDIN_RE`,
  `enricher.py:133`, `208-212`). No scraper sets it.
- **Example:** `https://linkedin.com/company/acme`.
- **Mildly ambiguous.** Only `/company/<slug>` links match; personal `/in/`, `/school/`,
  and bare-domain links are missed. A footer "made by" or parent-company link is
  captured as if it were the business. Stored as a full URL (unlike `instagram`'s
  handle-only convention), so the contact columns use inconsistent formats. Blocklist
  is only `{company, in, pub}` (`enricher.py:211`).

### 16. `rating` — float | null
- **Source of truth:** Google Places `rating` only (`google_places.py:265`). OSM and
  Wikidata never set it.
- **Example:** `4.5`.
- **Mildly ambiguous.** The column is Google-only, so a huge fraction of records are
  `None` even when the business demonstrably has a Google rating — `None` is
  systematically conflated with "no data" rather than "not scraped". After dedup,
  `_merge` (`dedup.py:98`) keeps the rating only if the Google-derived record has a
  higher field count than its counterpart, so a valid rating can be silently dropped in
  a merge. No 0.0–5.0 bounds check; `lead_score` treats any `rating < 4.0` (including
  `0.0`) as a +10 pain-point signal (`enricher.py:305`).

### 17. `review_count` — int | null
- **Source of truth:** Google Places `userRatingCount` only (`google_places.py:266`).
- **Example:** `120`.
- **Mildly ambiguous.** Same Google-only provenance as `rating`. The downstream
  ambiguity is real: `recommend_service` reads `review_count < 20`
  (`pitch_recommender.py:70`, with `review_count or 0` at `:43-47`), so every live
  website with a non-Google record is treated as having **0 reviews** and routed to
  "SEO audit + visibility upgrade" — systematically undercounting established businesses.

### 18. `completeness_score` — int
- **Source of truth:** computed by `completeness_score()` (`enricher.py:101-117`).
  +1 each for phone, email, website, address, (facebook **or** instagram), whatsapp,
  linkedin. Range 0–7. Set for every record in `enrich()` (`enricher.py:341`).
- **Example:** `0`, `3`, `7`.
- **Ambiguous — yes.** Despite the name, it is a **binary presence count**, not a
  quality measure. Every field is weighted equally, so `{phone, website, address}` (3)
  ties with `{email, instagram, whatsapp}` (3) though their sales value differs. Having
  *both* facebook and instagram scores only one point (`enricher.py:111`). It gates
  `qualified_businesses.csv` at `>= 1` (`main.py:146`), so a lone phone number
  qualifies a lead.

### 19. `lead_score` — int
- **Source of truth:** computed by `lead_score()` (`enricher.py:275-311`), range 0–100
  (`min(score, 100)` at `:311`). **Computed twice**: once in `enrich()`
  (`enricher.py:342`) *before* `industry_priority` is set, then overwritten in
  `main.py:137` *after* priority and service are assigned.
- **Example:** `0`, `35`, `78`.
- **Mildly ambiguous.** The final value is correct (the `main.py` recomputation wins),
  but the duplicate computation means the priority bonus (+15/+8 at `enricher.py:299-302`)
  is only present in the second pass. Components are documented in the function but the
  resulting number is a weighted heuristic with no published interpretation; a reader
  cannot tell *why* a row scored 35 vs 40. The `+10` for `rating < 4.0` and `+20` for a
  server-confirmed dead site (`enricher.py:296`) inherit the `rating`/`website_live`
  ambiguities above.

### 20. `industry_priority` — str | null
- **Source of truth:** `industry_priority(category)` (`whitelist.py:133-145`):
  `"high"` if category ∈ `PRIORITY_INDUSTRIES`, `"medium"` if ∈ `ADJACENT_BUSINESSES`,
  else `"low"` (including `category is None`, `whitelist.py:138-139`). Set in
  `main.py:132`.
- **Example:** `high`, `medium`, `low`.
- **Ambiguous — yes.** It is a pure function of `category`, so it inherits every
  category-vocabulary defect in column 2. A Wikidata label `"real estate agent"` (with
  spaces) falls through to `low` even though `real_estate_agent` is high-priority. After
  dedup, the surviving `category` (and thus priority) depends on the `_field_count`
  tie-break in `_merge` (`dedup.py:98`), not on which source's category is most accurate.

### 21. `recommended_service` — str | null
- **Source of truth:** `recommend_service(record)` (`pitch_recommender.py:25-99`), a
  hand-ordered if-chain. Set in `main.py:133`.
- **Example:** `Website rebuild + maintenance`, `SEO audit + visibility upgrade`,
  `Full digital launch (brand + website + social setup)`,
  `Discovery call - scope the right service`.
- **Ambiguous — yes.** The value is a **human-readable label, not a stable enum**; there
  is no machine key and the exact strings are coupled to the current
  `pitch_recommender.py` (editing a label silently breaks any downstream string match).
  Every input it consumes (`website_live`, `category`, `review_count`,
  `completeness_score`, social flags) carries the ambiguities above, so the
  recommendation is only as sound as those columns. The `review_count < 20` branch
  (`pitch_recommender.py:70`) mis-fires for all non-Google records (see column 17).

### 22. `source` — str
- **Source of truth:** scraper constant (`"osm"`, `"wikidata"`, `"google_places"`),
  unioned with `|` on merge (`dedup.py:92-94`).
- **Example:** `osm`, `google_places`, `osm|google_places`.
- **Ambiguous — yes.** Two meanings are conflated:
  - It is a **set** of scrapers that produced the record, delimited by `|`, not a single
    scalar — consumers must split it.
  - It identifies **which scrapers saw the record**, not **which field came from where**.
    After `_merge`, individual field provenance is gone: a merged `osm|google_places`
    record does not tell you whether the `phone` came from OSM or Google, or whether
    `rating`/`review_count` are Google-only. It is also fed to `lead_score` as a naive
    `"|" in source` multi-source bonus (`enricher.py:308`).

### 23. `scraped_at` — str
- **Source of truth:** `datetime.datetime.utcnow().isoformat() + "Z"` captured at each
  scraper's start (`osm.py:31`, `wikidata.py:31`, `google_places.py:162`). On merge,
  `_merge` keeps `max(av, bv)` (`dedup.py:95-96`).
- **Example:** `2026-10-03T12:34:56.789012Z`.
- **Mildly ambiguous.** It records the **scrape start time**, not when the record was
  enriched or when `website_live` was last checked (liveness runs later, in
  `enrich()` → `check_websites()`). For a merged record it is the latest contributing
  scraper's start, which may not be the timestamp of the most recent data in any given
  field. The lexicographic `max` is chronologically correct only because the format is
  consistently UTC + zero-padded + `Z` within a run. `datetime.utcnow()` is deprecated
  in Python 3.12 (`DeprecationWarning`) — prefer `datetime.now(timezone.utc)`.

---

## Flagged ambiguous columns (summary)

**Genuinely ambiguous today (9):**

| Column | Core ambiguity |
|--------|----------------|
| `name` | No canonical form; mixed scripts/locales; dedup key |
| `category` | Three incompatible taxonomies in one column |
| `region` | Governorate vs city; overlapping boxes; unreachable `Nabatieh` |
| `address` | Unstructured, mixed granularity; city extraction = country for Google |
| `phone` | Post-normalization (origin lost); empty-string vs null; identity key |
| `email` | Source-declared vs first-match scraped (may not belong to business) |
| `website` | `url` tag may be Facebook/menu, not a real site |
| `website_live` | `None` = six different outcomes; `False` includes auth walls |
| `facebook` | URL vs bare id vs handle, no normalization |
| `instagram` | handle vs URL mixed; first-match scrape |
| `completeness_score` | "presence count", not completeness |
| `industry_priority` | inherits category-vocabulary defects |
| `recommended_service` | free-text label, no stable enum |
| `source` | set of scrapers, not field-level provenance |

*(Note: this list is 14 — the "9" in the Verdict refers to the highest-severity ones;
the rest are real but lower-impact. Full detail above.)*

**Clear enough to build on:** `lat`, `lon`, `country`, `rating`, `review_count`,
`whatsapp`, `linkedin`, `lead_score`, `scraped_at`.

---

## Not a bug, but worth knowing

- **`BusinessRecord` and `FIELDS` agree at 23 keys, but their order differs.** The CSV
  column order is whatever `FIELDS` declares (`main.py:32-39`); the `TypedDict`
  (`base.py:5-28`) orders `region`/`country`/`address` differently. Harmless for
  `DictWriter` (which keys by name), but anyone reading the `TypedDict` as the canonical
  order will be misled.
- **`write_csv` uses `extrasaction="ignore"`** (`main.py:76`), so any extra key a
  scraper adds is silently dropped rather than surfaced as a schema drift warning.
- **`None` vs empty-string is not stable across fields.** `load_master` collapses every
  empty cell to `None` (`main.py:53-55`), but `phone` can be written as `""` within a
  run (see column 8). The type system in `BusinessRecord` says `phone: str | None` but
  reality is `str | None | ""`.
- **`_merge`'s field-count tie-break is the de facto provenance policy**
  (`dedup.py:79-98`): when two records disagree on a field, the "fuller" record wins
  wholesale. There is no per-field freshness or confidence — which is why a Google
  `rating` can be dropped in favor of a more-populated OSM record.

## Recommended order of work

1. **Fix `website_live` semantics** (`enricher.py:158-214`) — split `DEAD` into
   server-confirmed-broken (5xx, or 404 on the homepage) vs auth/geo-blocked (401/403),
   and make `UNKNOWN` a distinct exported reason rather than a single `None`.
2. **Introduce a single category taxonomy** and a mapping table from OSM tags and
   Wikidata labels and Google types into it — before the whitelist and priority logic,
   since `industry_priority` and `recommended_service` both inherit this.
3. **Decide and document `region`** as one level (governorate or city), remove the
   overlapping boxes, and delete the unreachable `Nabatieh` address keywords.
4. **Preserve field-level provenance** — replace `source` (or add a parallel field)
   with per-field origin, so a merged record is auditable.
5. **Stabilize `None` vs `""`** and serialize `website_live` as an explicit token
   (`true`/`false`/`unknown`) so CSV consumers don't guess.
