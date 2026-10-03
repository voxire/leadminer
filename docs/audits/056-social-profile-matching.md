# 056 — Social Profile Matching to Businesses

## Verdict

The pipeline currently treats a string that resembles an Instagram handle or LinkedIn company slug as a business contact, without proving who controls the profile or which business it represents. This can silently attach an unrelated or former owner's account to a lead and send sales staff to the wrong person. Social profiles need canonical identities, evidence-backed match states, and collision handling before they count as verified contact channels.

## Findings

### S1 — Unverified profiles can be attributed to a business and used for sales

- **Where:** `enricher.py:128-133, 198-212, 244-251`; `main.py:82-90, 141-145`; `enricher.py:111-116, 275-287`.
- **Breaks:** `_INSTAGRAM_RE` accepts any `@word` in raw HTML, and extraction saves the first match; LinkedIn saves a slug without checking the profile identity. Existing values from scrapers are retained in preference to website discoveries (`if not r.get(...)`). No source, verification status, or evidence accompanies either value. Nevertheless, any Instagram value increases `lead_score`, Instagram counts toward completeness, and a string longer than three characters can qualify as a sales contact. A false match therefore becomes a seemingly actionable, scored lead rather than an uncertain candidate.
- **Trigger:** A restaurant site contains a testimonial `@sarah_k`, an old owner's Instagram link, or a LinkedIn company URL for a similarly named business. The pipeline persists that profile on the restaurant's row; sales staff may message the unrelated account, expose the lead's data, or claim an association the account owner never made. A former owner/employee's personal LinkedIn profile is especially sensitive: the person may have left the business or never consented to be treated as its sales contact.
- **Fix:** Store social links as candidates with provenance and a verification state; only count a verified business-owned profile in scoring and `sales_ready`. Keep personal LinkedIn profiles separate from company profiles and never treat a person's account as the business's contact by default. For a personal profile to be associated at all, require an explicit public business-to-person link and record the role/evidence; require review or explicit policy before using it for outreach.

### S2 — URL/handle canonicalization is underspecified, so equivalent and malformed inputs have no stable identity

- **Where:** `enricher.py:128, 133, 198-212`; `scrapers/osm.py:89-108`; `scrapers/base.py:17-20`; `main.py:32-39, 74-78`.
- **Breaks:** Instagram is stored as a bare lowercase handle from a loose regex, while LinkedIn is emitted as a URL; OSM may supply raw social tag values. There is no shared parser, canonical URL, strict host/path validation, or distinction between an organization page and a person page. Query strings, fragments, trailing slashes, mobile/www hosts, and copied post/share URLs can lead to inconsistent values or the wrong profile type. Lowercasing every provider's path is also an unsafe generic rule; canonicalization should follow each platform's identifier semantics.
- **Trigger:** The same Instagram profile appears as `@BeirutCafe`, `https://www.instagram.com/BeirutCafe/?igsh=...`, and a URL to `/p/{post_id}`. These are respectively stored differently, or a post ID could be mistaken for a profile. A LinkedIn `/in/founder-name` link must not be rewritten as `/company/founder-name` or silently counted as the business page.
- **Fix:** Parse with `urlparse` after HTML-unescaping and trimming; allow only exact platform hostnames (or documented subdomains), validate path shapes, and reject post/reel/share/login routes. For Instagram, normalize a valid profile handle to a platform key and canonical `https://www.instagram.com/{handle}/`; accept a leading `@` only as an input-format marker, not as free-text evidence. For LinkedIn, preserve the typed route (`/company/{slug}/` versus `/in/{slug}/`) in the canonical URL and profile type; remove query/fragment and normalize host/trailing slash, but retain the raw URL and raw handle. Do not infer a profile URL from an arbitrary slug.

### S2 — Reused handles/slugs can silently connect unrelated businesses

- **Where:** `enricher.py:198-212, 244-251`; `main.py:32-39`; `dedup.py:83-99, 102-137`.
- **Breaks:** Social identifiers are not checked against claims already attached to other businesses, and deduplication does not use a social identity or flag conflicts. Platform handles can be renamed and later reassigned; even current identical claims may be a collision, a shared agency/template link, or a legitimate multi-location brand. Without a registry and temporal evidence, the same profile can appear to verify multiple unrelated businesses, while an old association remains on a lead indefinitely.
- **Trigger:** Two unrelated businesses in Beirut publish the same copied Instagram URL, or a business changes its handle and the old handle is later claimed by another business. Both records would currently retain the same identifier as if each owned it; a recycled handle could also cause outreach to reach the new owner's account.
- **Fix:** Enforce a collision check on `(platform, normalized_profile_key)` across active claims, and block automatic verification/outreach when unrelated canonical businesses claim the same profile. Keep legitimate shared brands/multiple branches explicit (e.g. a brand-to-location relationship), rather than treating collisions as proof of duplicate businesses. Keep dated observations and never transfer historical ownership merely because a handle matches again.

## Matching and storage contract

1. **Candidate capture:** Extract only actual anchor `href` values or structured profile-link fields, not arbitrary text, CSS, scripts, or mentions. Retain the raw value, the page URL where it was found, and the extraction source. A link from the business website is useful evidence, not conclusive ownership by itself (it may be stale, copied, or a template link).
2. **Evidence-based verification:** Confirm the profile is accessible as public data, then compare its displayed business/brand name and any public website/domain, phone, category, and location against the lead. Prefer reciprocal evidence: the business website explicitly links to the profile and the profile links back to the same registrable website domain, with a compatible name. A name-only or handle-name match is weak evidence. When platform access is blocked or fields are unavailable, store `unknown`/`unverified`, not verified or rejected. Do not log into platforms, bypass access controls, or infer owner identity from private data.
3. **State and confidence:** Use at least `candidate`, `verified_business`, `verified_person` (separately typed and governed), `rejected`, `collision`, and `unknown`, with match method, confidence/reason, verification timestamp, and reviewer or algorithm version. Thresholds should be calibrated from reviewed examples; a candidate copied from a website must not silently become verified.
4. **Persist a multi-valued, auditable record:** Store `business_id`, `platform`, `profile_type`, canonical URL, normalized profile key/handle, raw observed value, first/last-seen timestamps, active/validity state, source observation/page URL, evidence summary, verification status/confidence, and any collision/review resolution. Keep previous observations when a handle changes or disappears. A `social_profiles` child table is preferable to the current single-value CSV columns; the CSV can export only a selected verified primary URL/handle and should expose verification status if sales staff use it.
5. **Protect downstream use:** Candidate/unverified/colliding profiles must not increment completeness or lead score and must not make a row `sales_ready`. Exports should visibly label non-verified profiles or omit them from outreach fields. Never represent a personal profile as a company-owned profile.

## Not a bug, but worth knowing

- A social profile may legitimately serve several branches under one brand. This is a relationship-modeling case, not grounds to force one profile to one location or to merge those business records.
- Public profile contents and platform URL behavior can change or be unavailable to an unauthenticated crawler. Preserve `unknown` separately from a confirmed mismatch; do not let fetch failure erase prior evidence or count as proof of ownership.

## Recommended order of work

1. Stop treating raw social strings as verified sales contacts; gate scores and sales exports on explicit verification.
2. Add strict platform-specific URL parsing/canonicalization and retain raw values plus source page provenance.
3. Add typed profiles, verification evidence/state, temporal observations, and active-claim collision detection; review ambiguous and personal-profile matches before outreach.
4. Backfill existing CSV handles only as `unverified` candidates; do not upgrade them based solely on their current presence in the master file.
