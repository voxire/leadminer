# 018 — OSM tag extraction: discarded contact & business signals

## Verdict

The OSM scraper already downloads every tag on every element (`out center tags;`,
`scrapers/osm.py:25`) but maps only 8 keys into `BusinessRecord`. The single most
valuable contact channel for this market — **WhatsApp** — is discarded, as are
LinkedIn/X/TikTok/YouTube/Telegram/Snapchat, the mobile/fax numbers, and a dozen
business-signal tags (brand, operator, opening_hours, cuisine, rooms, …). Because
every tag is already in hand, this is a pure mapping fix in `osm.py`, not a query
change; the only real blocker is that `BusinessRecord` (`scrapers/base.py:5-28`)
and the CSV schema (`main.py:32-39`) have no columns for most of these fields.

## Findings

### S1 — WhatsApp is silently dropped, costing the single highest-value contact channel in Lebanon/Saudi Arabia

- **Where:** `scrapers/osm.py:107-110` (`whatsapp=None`), `scrapers/base.py:19` (field exists)
- **Breaks:** A POI tagged `contact:whatsapp=+961 3 123456` (or legacy `whatsapp=…`)
  yields a record with `whatsapp=None`. That record loses the +15 lead-score points
  (`enricher.py:234-235`), loses a completeness point (`enricher.py:113-114`), and —
  critically — is not counted as sales-ready, because `has_any_contact()`
  (`main.py:82-90`) checks only phone / email / instagram, not whatsapp. In a market
  where WhatsApp is the dominant business channel, this is silent, systematic data
  loss for OSM-sourced leads.
- **Trigger:** any of the thousands of Lebanese POIs carrying `contact:whatsapp=*`
  (the key is "in use" per the OSM contact template).
- **Fix:** `whatsapp = tags.get("contact:whatsapp") or tags.get("whatsapp")` and yield
  it; separately extend `has_any_contact()` in `main.py` to treat whatsapp as a channel.

### S2 — Only 8 tag keys are mapped; every other social and contact key is thrown away

- **Where:** `scrapers/osm.py:86-90` (phone/email/website/facebook/instagram extraction)
- **Breaks:** `linkedin` (`base.py:20`) is never populated from OSM
  (`osm.py:110`), even though `contact:linkedin`/`linkedin` tags exist and the field is
  wired into completeness (`enricher.py:115-116`). X/Twitter, TikTok, YouTube, Telegram,
  Snapchat have **no field at all** in `BusinessRecord`, so they cannot be captured
  without a schema change. `contact:mobile`/`mobile`/`phone:mobile` (secondary numbers)
  and `contact:fax`/`fax` are also dropped.
- **Trigger:** a restaurant with `contact:instagram`, `contact:facebook`, and
  `contact:linkedin` — only the first two survive to CSV.
- **Fix:** add a `_social` mapping in `osm.py` for
  linkedin / twitter|x / tiktok / youtube / telegram / snapchat, and add matching
  columns to `BusinessRecord` and `FIELDS`.

### S3 — Address assembly omits the keys that actually locate a Lebanese business

- **Where:** `scrapers/osm.py:77-84`
- **Breaks:** `addr:place` (village/town name — commonly the only locality present for
  rural Lebanese POIs), `addr:full` (whole-address string used by some mappers),
  `addr:province` (governorate), and `addr:postcode` are all ignored. `infer_region()`
  matches the *address string* (`enricher.py:85-88`), so dropping these keys directly
  degrades region assignment, which drives the `region` column and the per-region
  breakdown (`main.py:166-172`).
- **Trigger:** a Bekaa guest house tagged only `addr:place=Chtaura` (no `addr:city`)
  produces address `None` and region falls back to coordinate boxes.
- **Fix:** append `addr:place`, `addr:neighbourhood`, `addr:postcode`, `addr:province`,
  and fall back to `addr:full` when the assembled parts are empty.

### S3 — Business-signal tags (brand, operator, opening_hours, cuisine, rooms, …) are never surfaced

- **Where:** `scrapers/osm.py:58-119` (the `tags` dict is never re-read beyond the 8 keys)
- **Breaks:** `brand`/`operator` (chain & operator identity — useful for dedup and
  account-based pitch), `opening_hours`, `cuisine`, `rooms`, `wheelchair`,
  `internet_access`, and the entire `payment:*` namespace are silently discarded. These
  are not contact channels, so their omission is lower-severity, but they are the raw
  material for pitch personalization and would cost nothing to keep if columns existed.
- **Trigger:** `brand=Zaatar w Zeit` is reduced to `category=restaurant`, losing the
  single most useful fact about that lead.
- **Fix:** lowest-priority; add a generic `extra`/`osm_tags` JSON column (see below) so
  these can be retained without enumerating every key in `FIELDS`.

## The complete ranked list of ignored OSM tag keys

Legend for **lead-gen value**: **Critical** = direct contact channel, high conversion;
**High** = strong contact/identity signal; **Medium** = meaningful enrichment for
scoring or pitching; **Low** = marginal; **Negligible** = no lead-gen value (listed for
completeness only).

### Contact & social variants

| Rank | Tag key(s) | Signal | Status in `osm.py` | Lead-gen value |
|---|---|---|---|---|
| 1 | `contact:whatsapp`, `whatsapp` | WhatsApp number (dominant MENA channel) | **Ignored** | **Critical** |
| 2 | `contact:mobile`, `mobile`, `phone:mobile` | Secondary / mobile number | **Ignored** | **High** |
| 3 | `contact:linkedin`, `linkedin` | LinkedIn company/profile URL | **Ignored** (`osm.py:110`) | **High** |
| 4 | `contact:twitter`, `twitter`, `x`, `contact:x` | X/Twitter handle or URL | **Ignored** | **High** |
| 5 | `contact:youtube`, `youtube` | YouTube channel URL | **Ignored** | Medium |
| 6 | `contact:tiktok`, `tiktok` | TikTok profile URL | **Ignored** | Medium |
| 7 | `contact:telegram`, `telegram` | Telegram username/number | **Ignored** | Medium |
| 8 | `contact:snapchat`, `snapchat` | Snapchat username | **Ignored** | Low–Medium |
| 9 | `contact:fax`, `fax` | Fax number | **Ignored** | Low |
| 10 | `toll_free`, `tollfree` | Toll-free number | **Ignored** | Low |
| 11 | `contact:skype`, `skype` | Skype ID | **Ignored** | Negligible |
| — | `phone`, `contact:phone` | Landline/primary phone | Extracted (`osm.py:86`) | (covered) |
| — | `email`, `contact:email` | Email | Extracted (`osm.py:87`) | (covered) |
| — | `website`, `contact:website`, `url` | Website | Extracted (`osm.py:88`) | (covered) |
| — | `contact:facebook`, `facebook` | Facebook URL | Extracted (`osm.py:89`) | (covered) |
| — | `contact:instagram`, `instagram` | Instagram URL | Extracted (`osm.py:90`) | (covered) |

> Note on X/Twitter: the OSM standard is still `contact:twitter` / `twitter`
> (`Key:contact:twitter` — "used to define an X (former Twitter) page"). `x` /
> `contact:x` appear only occasionally; treat them as aliases in the same mapping.

### Address variants

| Rank | Tag key(s) | Signal | Status | Lead-gen value |
|---|---|---|---|---|
| 12 | `addr:full` | Whole address in one string (fallback) | **Ignored** | **High** |
| 13 | `addr:place` | Village/town name (often the only locality) | **Ignored** | **High** |
| 14 | `addr:province` | Governorate / province | **Ignored** | Medium |
| 15 | `addr:postcode` | Postal code | **Ignored** | Medium |
| 16 | `addr:neighbourhood` | Neighborhood (Beirut districts) | **Ignored** | Low–Medium |
| 17 | `addr:housename` | Building name | **Ignored** | Low |
| 18 | `addr:state` | State (not used in LB) | **Ignored** | Low |
| 19 | `addr:country` | Country (always `LB`) | **Ignored** | Negligible |
| 20 | `addr:block`, `addr:floor`, `addr:unit`, `addr:flats` | Unit-level detail | **Ignored** | Negligible |
| — | `addr:housenumber`, `addr:street`, `addr:suburb`, `addr:city`, `addr:district` | Core address | Extracted (`osm.py:77-83`) | (covered) |

### Brand / operator / business attributes

| Rank | Tag key(s) | Signal | Status | Lead-gen value |
|---|---|---|---|---|
| 21 | `brand` | Chain/franchise identity | **Ignored** | Medium |
| 22 | `operator` | Operating entity (bank/telecom branch) | **Ignored** | Medium |
| 23 | `cuisine` | Restaurant cuisine (pitch personalization) | **Ignored** | Medium |
| 24 | `opening_hours` | Hours of operation (outreach timing) | **Ignored** | Low–Medium |
| 25 | `rooms` | Hotel room count (segment/upsell) | **Ignored** | Low–Medium |
| 26 | `beds`, `capacity` | Capacity signals | **Ignored** | Low |
| 27 | `description` | Free-text business description | **Ignored** | Low–Medium |
| 28 | `diet:halal`, `diet:vegetarian`, `diet:vegan`, `diet:*` | Dietary flags (halal relevant) | **Ignored** | Low |
| 29 | `brand:wikidata`, `brand:wikipedia` | Brand entity links | **Ignored** | Low |
| 30 | `stars` | Hotel star rating (only rating-like signal OSM has) | **Ignored** | Low |
| 31 | `image` | Feature photo URL | **Ignored** | Low |
| 32 | `smoking`, `outdoor_seating`, `delivery`, `takeaway`, `drive_through` | Operational attributes | **Ignored** | Low |
| 33 | `operator:type` | Operator classification | **Ignored** | Negligible |
| 34 | `opening_hours:covid19`, `opening_hours:*` | Sub-hour tags (mostly obsolete) | **Ignored** | Negligible |

### Accessibility / connectivity / payment

| Rank | Tag key(s) | Signal | Status | Lead-gen value |
|---|---|---|---|---|
| 35 | `internet_access` | `yes`/`wlan`/`no` (weak inverse digital-presence signal) | **Ignored** | Low |
| 36 | `internet_access:fee`, `internet_access:ssid` | Connectivity detail | **Ignored** | Negligible |
| 37 | `wheelchair`, `wheelchair:description` | Accessibility | **Ignored** | Negligible |
| 38 | `payment:cash`, `payment:mastercard`, `payment:visa`, `payment:contactless`, `payment:debit_cards`, `payment:apple_pay`, … (all `payment:*`) | Payment methods | **Ignored** | Negligible |
| 39 | `fee`, `charge` | Admission/usage fee | **Ignored** | Negligible |

**Bottom line for the list:** the keys worth adding immediately are
**1–4 (whatsapp, mobile, linkedin, twitter/x)** plus **`addr:full`/`addr:place`**.
Everything else is enrichment, best captured via a generic passthrough column rather
than one field per key.

## Not a bug, but worth knowing

- **`rating` / `review_count` are always `None` from OSM** (`osm.py:111-112`) and this
  is correct — OSM has no review-count or aggregate-rating key. Do not expect these
  columns to fill from this source; the `stars` tag (hotels) is the closest analogue.
- **The Overpass query already returns all tags** (`out center tags;`,
  `osm.py:25`), so fixing extraction requires no network change and no re-scrape risk.
- **The query does not fetch `way` elements for `craft` or `healthcare`**
  (`osm.py:10-24` lists them only for `node`), yet `category` reads `craft` and
  `healthcare` (`osm.py:74-75`). Harmless dead branches today, but a widening gap if
  either category matters.
- **`has_any_contact()` is the real gatekeeper** for `sales_ready.csv`
  (`main.py:142-145`) and it ignores whatsapp/facebook/linkedin. Fixing the OSM mapping
  is necessary but not sufficient: without a `main.py` change, extracted WhatsApp still
  won't surface in `sales_ready`.
- **Schema is the ceiling.** `FIELDS` (`main.py:32-39`) has no columns for X/TikTok/
  YouTube/Telegram/Snapchat/brand/operator/opening_hours/cuisine, and `write_csv` drops
  unknown keys via `extrasaction="ignore"` (`main.py:76`). Any tag added beyond the
  existing 22 columns must also be added to `base.py` and `FIELDS`, or it will be
  silently discarded at write time.

## Recommended order of work

1. Add WhatsApp, mobile, and LinkedIn extraction in `osm.py` (map
   `contact:whatsapp`/`whatsapp`, `contact:mobile`/`mobile`/`phone:mobile`,
   `contact:linkedin`/`linkedin`) — these map to **existing** `BusinessRecord` fields,
   so no schema change is needed. This is the highest ROI and unblocks S1/S2.
2. Extend `has_any_contact()` in `main.py` to count whatsapp (and linkedin) as a
   contact channel so the recovered data actually reaches `sales_ready.csv`.
3. Add `addr:place`, `addr:neighbourhood`, `addr:postcode`, `addr:province`, and an
   `addr:full` fallback to `osm.py:77-84` to improve `infer_region`.
4. Add a generic `osm_tags` (or `extra`) JSON column — populated once in `osm.py` and
   carried through `FIELDS` — to retain brand/operator/opening_hours/cuisine/rooms/
   internet_access/payment and the long-tail social tags (X, TikTok, YouTube, Telegram,
   Snapchat) without enumerating dozens of columns.
5. If X/TikTok/YouTube/Telegram/Snapchat are wanted as first-class columns, extend
   `BusinessRecord` (`base.py:5-28`) and `FIELDS` (`main.py:32-39`) accordingly — do
   this only after step 4, since the generic column makes it optional.
