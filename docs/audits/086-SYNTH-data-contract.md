# 086 — Sales output contract

## Verdict

The CSVs have a stable *header* in `main.py`, but not yet a dependable sales-facing contract: null website status, contact eligibility, completeness, and priority are implicit in implementation details. Publish a versioned schema and make the three subset predicates below executable tests before reps treat a blank or score as a business fact.

## Findings

### S1 — Resolve the schema count before calling it additive
- **Where:** `main.py:33-40`; `scrapers/base.py:5-28`; `docs/audits/BRIEF.md:57-62`
- **Breaks:** The checked-in CSV writer defines **23 fields total, including `website_live`**. The brief says 22 but enumerates 23 names, and the request's “23 columns plus” wording could mean 24. These interpretations produce incompatible headers; a consumer may map values to the wrong columns or silently lose a field.
- **Trigger:** A downstream import expects 23 legacy columns and gets a 24-column file, or the opposite.
- **Fix:** Treat the code's explicit ordered list below as the current schema (23 total); check the deployed legacy CSV before any migration. If it really has 23 fields without `website_live`, preserve those 23 and version an additive 24-field schema rather than dropping or replacing a column.

### S1 — `website_live` needs a three-state contract, not a boolean gloss
- **Where:** `main.py:102-104,215-223`; `enricher.py:142-153,158-214,217-242`
- **Breaks:** CSV blank currently covers both “no website” and “website not assessed / no answer.” They are different sales situations. `website` must be read alongside `website_live`; blank status alone is not “no website” and is not evidence for a rebuild.
- **Trigger:** A site times out or blocks the crawler. Its website is present, status is blank, and treating it as broken creates a false rebuild pitch.
- **Fix:** Keep the tri-state and document its exact serialization and operational meaning (contract below); never coerce blank to false.

### S2 — The three exported subsets need named, tested predicates
- **Where:** `main.py:137-145,199-206`; `enricher.py:101-117,318-342`
- **Breaks:** Membership currently depends on inline truthiness and a score whose calculation precedes final phone normalization. In particular, `sales_ready` excludes WhatsApp, Facebook, and LinkedIn as contact routes, while `qualified` can use a phone later normalized to empty. “Qualified” therefore does not mean sales-ready or verified.
- **Trigger:** A record with only a WhatsApp number and high priority is excluded from `sales_ready`; a record whose only completeness point was a junk phone can retain a positive completeness score after that phone is normalized to empty.
- **Fix:** Adopt the canonical definitions below, calculate completeness from final normalized fields, and add boundary tests for every predicate and overlap.

## Proposed stable output contract

### Schema and row guarantees

All five files use the same ordered header, even when a subset has zero rows: `main.py:33-40,108-130,208-213`. A missing value is serialized as an empty CSV cell; CSV itself does not preserve Python types. The order in this checkout is:

| # | Column | Contract / may be empty? |
|---:|---|---|
| 1 | `name` | Required business display name; should be non-empty. Do not use as a unique key. |
| 2 | `category` | Source category/tag; may be empty in legacy or imperfect master rows. Values are not a controlled taxonomy (`whitelist.py:105-130`). |
| 3 | `region` | Normalized region when inferable; may be empty/unknown. Lebanon and Saudi region inference is partial (`enricher.py:10-94`). |
| 4 | `country` | ISO-like code `LB` or `SA` after `resolve_country`; expected non-empty (`main.py:50-74,183-185`). |
| 5 | `address` | Source address; may be empty. |
| 6 | `lat` | Latitude as numeric text; may be empty. |
| 7 | `lon` | Longitude as numeric text; may be empty. |
| 8 | `phone` | Normalized phone when usable; may be empty. Do not assume it is dialable solely because a source supplied it (`dedup.py:33-63`; `main.py:192-194`). |
| 9 | `email` | Extracted/source email; may be empty; presence is not deliverability verification. |
| 10 | `website` | Website URL as supplied/normalized by source; may be empty. A non-empty URL is not evidence the site works. |
| 11 | `website_live` | Nullable tri-state defined below. Serialized as `True`, `False`, or empty. |
| 12 | `facebook` | Facebook value/link if known; may be empty. |
| 13 | `instagram` | Instagram handle/link if known; may be empty. |
| 14 | `whatsapp` | WhatsApp contact if known; may be empty. |
| 15 | `linkedin` | LinkedIn value/link if known; may be empty. |
| 16 | `rating` | Numeric rating if supplied; may be empty. Not a verified current customer rating. |
| 17 | `review_count` | Integer review count if supplied; may be empty. |
| 18 | `completeness_score` | Derived integer 0–7; expected non-empty after enrichment. Definition below. |
| 19 | `lead_score` | Derived integer 0–100; expected non-empty after enrichment. A heuristic, not a conversion probability. |
| 20 | `industry_priority` | Derived enum `high`, `medium`, `low`; expected non-empty. `high` means exact category in priority set, `medium` exact adjacent-set member, else `low` (`whitelist.py:133-145`). |
| 21 | `recommended_service` | Human-readable pitch hypothesis; expected non-empty, not a confirmed need (`pitch_recommender.py:25-99`). |
| 22 | `source` | Source name(s), `|`-joined on merge; expected non-empty for scraper-created rows, but legacy master rows may be incomplete (`dedup.py:83-99`). |
| 23 | `scraped_at` | ISO-like UTC scrape timestamp; on merge the later string is retained; may be absent in malformed/legacy rows (`dedup.py:83-99`). |

“Guaranteed” means the **column exists in every CSV**, not that each imported master row is complete. `write_csv` writes the fixed header and fills absent dictionary keys as empty cells (`main.py:108-130`). For strict row-level guarantees, validate required fields before writing; do not silently treat bad master rows as fully conforming. All source/observed fields above may legitimately be empty; the four derived fields (`completeness_score`, `lead_score`, `industry_priority`, `recommended_service`) should not be empty in a valid post-pipeline row.

**Count note:** This checkout's exact header is 23 columns **including** `website_live`; the separate `BusinessRecord` type also declares 23 keys. The brief's prose count is inconsistent with its enumeration. This report does not add a duplicate `website_live` column.

### `website_live` operational meaning

| CSV value | Meaning | Sales action |
|---|---|---|
| `True` | A website URL was checked and the request returned a readable response below the implementation's `>=400` error threshold (normally a successful final response). It establishes reachability at check time, not quality, ownership, or uptime. | Do not pitch a rebuild on liveness alone. Assess reviews, conversion, and digital gaps. |
| `False` | A website URL was checked and the server returned HTTP status 400 or greater. This is a server-confirmed error response, not proof the business permanently has a broken site. | Recheck manually before claiming it is down; if confirmed, a rebuild/repair is a plausible opening. |
| empty / null | No verdict. For a non-empty `website`, the request failed or the response body could not be read (e.g. timeout, DNS/TLS failure, block). For an empty `website`, no URL was available to test. | For a listed site, retry/check manually. Never infer “dead.” With no website URL, use the no-website route. |

The fetch follows redirects and classifies status `>=400` as `DEAD`; exceptions and body-read failures are `UNKNOWN` (`enricher.py:158-214`). The comments describe `LIVE` as 2xx, but the implementation accepts any response below 400; either align the implementation with a strict 2xx definition or retain the implementation's `<400` definition as above. Blank is valid only when `website` is absent or status is unknown; the master loader currently accepts only exact `True`/`False` strings and maps other values to null (`main.py:102-104`).

### What a rep can rely on to prioritize

1. Start with `sales_ready.csv` as the current high/medium-priority, contactable queue—not as a guarantee that contact details are valid or that the business is qualified.
2. Treat `industry_priority` as a category-fit tier only: `high` before `medium`. It is an exact category allow-list mapping, not a researched account score (`whitelist.py:133-145`).
3. Use website presence and status as separate evidence: no URL suggests a website-launch conversation; `website_live=False` is a possible repair/rebuild lead but needs human confirmation; `website_live=True` directs attention to other gaps; blank status on an existing URL is unknown, not an opportunity signal.
4. Use `recommended_service` as a first-message hypothesis. The recommender is a hand-ordered ruleset and explicitly expects discovery to refine it (`pitch_recommender.py:4-9,25-99`).
5. Use `lead_score` only as a within-dataset sorting aid, not a probability or a claim of urgency. It awards points for contact, website state, industry priority, rating, and multi-source appearance (`enricher.py:275-311`); validate actual outcomes before making it a quota or rank threshold. `rating` and `review_count` are optional evidence, not universal coverage.

## Canonical subset definitions

Evaluate subsets on the final post-dedup, post-enrichment, post-normalization rows, after `industry_priority` is assigned (`main.py:178-206`). They may overlap; no subset is a disjoint partition. For consistency, normalize text by trimming whitespace before testing presence.

### `sales_ready.csv`

**Canonical predicate:**

```text
has_contact(r) AND r.industry_priority IN {"high", "medium"}

has_contact(r) :=
    digit_count(r.phone) >= 7
    OR ("@" in trim(r.email) AND len(trim(r.email)) > 5)
    OR len(trim(r.instagram)) > 3
```

Phone digit count means count characters matching `\d`, ignoring punctuation and spaces. This captures the current predicate in `main.py:137-145,202-205` and its post-normalization call site. **Current behavior excludes** Facebook, WhatsApp, LinkedIn, and website-only contact; do not silently describe it as “any contact channel.” Product decision: either keep this narrow rule and label/document it as phone/email/Instagram eligibility, or expand `has_contact` to approved channels (including explicit validation rules) and version/test that behavior.

### `qualified_businesses.csv`

**Canonical predicate:** `completeness_score >= 1`, where the score is the count of present normalized fields/groups:

```text
present(phone) + present(email) + present(website) + present(address)
+ present(facebook OR instagram) + present(whatsapp) + present(linkedin)
```

The maximum is 7. Trim strings before presence checks. Each social group contributes at most one point. Category, rating, review count, coordinates, region, website liveness, source, and recommended service do not contribute. This names a **data-completeness** threshold, not a sales qualification, contactability check, or truth/quality score. The current scorer uses truthiness and computes inside `enrich` before `main.py` normalizes phone; recompute after normalization to make this predicate true of the exported row (`enricher.py:101-117,318-342`; `main.py:187-206`).

### `without_websites.csv`

**Canonical predicate:** `trim(r.website)` is empty or null. The complement `with_websites.csv` is a non-empty trimmed `website`. A bad URL, a dead website, and an unreachable website are still **with a website**; `website_live` does not affect membership. This gives a meaningful partition and avoids treating whitespace as a URL. The current implementation instead uses raw Python truthiness (`main.py:199-200`), so a whitespace-only value is currently misclassified as having a website; normalize before filtering.

## Not a bug, but worth knowing

- `sales_ready` uses `industry_priority`, not `lead_score`, as its qualification gate; `lead_score` has no membership effect (`main.py:202-206`).
- A valid business may be omitted from all three subsets for different reasons; the cumulative `all_businesses.csv` remains the full post-filter master.
- As written, `website_live` is already in the current CSV schema. “New” should mean the meaning is newly contracted, unless the deployed legacy artifact proves the field is absent.

## Recommended order of work

1. Confirm the actual sales-consumed header and publish one schema version/count (23 current fields here; 24 only if preserving a separate 23-field legacy schema while adding the status field).
2. Encode the three predicates and tri-state parse/serialization as named functions; add tests for blank values, whitespace, invalid/normalized phone, each contact channel, and all website statuses.
3. Recompute completeness after final normalization; align the live-status HTTP definition with code and docs.
4. Train reps to use `industry_priority` for category fit, `website_live` only as qualified evidence, and score/recommendation as heuristics pending measured sales outcomes.
