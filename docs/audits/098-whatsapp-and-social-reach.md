# 098 — WhatsApp and Social Reach Verification Design

## Verdict

The current website pass extracts strings that look like social contacts but does not verify that a WhatsApp account exists, an Instagram profile is real or business-owned, or preserve evidence for either claim. Treat every extracted value as a **candidate**, never as a verified sales channel; separate existence from ownership, normalize LinkedIn company URLs without changing profile type, and keep an auditable observation for every decision. When the available evidence cannot establish the claim, retain `unknown`/`candidate` rather than guessing—the cost of a missing handle is lower than misattributing a person or business.

## Findings

### S1 — A link-shaped value is promoted to a business contact without verification

- **Where:** `enricher.py:128-133, 198-212, 244-251`; `enricher.py:275-287`; `main.py:33-40, 137-145`.
- **Breaks:** `_WHATSAPP_RE` extracts digits from HTML and prefixes `+`; it never contacts WhatsApp or establishes that the number is registered. `_INSTAGRAM_RE` searches raw HTML for either an Instagram URL or any `@word`, then accepts the first non-blacklisted string without checking that a profile exists or represents the lead. LinkedIn company slugs are similarly accepted from a regex alone. These values populate the business row and can count toward completeness, lead score, and sales-ready output. Website presence is extraction provenance, not proof of current ownership.
- **Trigger:** A page has `https://wa.me/96170123456`, an Instagram testimonial `@sarah_k`, or a stale template link to another shop. The pipeline records all three as the business's contacts; the number may not use WhatsApp and the social account may belong to an unrelated person.
- **Fix:** Model extracted values as unverified candidates with evidence; add explicit, independent existence and business-association checks; only expose verified business-owned identities as verified outreach contacts.

### S2 — Verification outcomes and evidence are not represented in the current record/export

- **Where:** `scrapers/base.py:5-29`; `main.py:33-40, 108-133`; `enricher.py:165, 242-251`.
- **Breaks:** The record has one scalar each for Instagram, WhatsApp, and LinkedIn, with no candidate/verified/unknown state, check time, source page, observed raw URL, verification method, or evidence. `main.py:125` writes only `FIELDS` and silently ignores extra keys, so attaching an ad-hoc evidence object to an in-memory row would not make the CSV auditable. A failed/blocked platform check would have nowhere to go and could be mistaken for a negative result or silently erased.
- **Trigger:** Instagram blocks an unauthenticated check or WhatsApp is unavailable during a run. The current flat value cannot distinguish “not checked,” “check blocked,” “profile absent,” and “confirmed business account”; later users cannot reconstruct why it was trusted.
- **Fix:** Persist append-only verification observations separately from selected contact values; export explicit verification status and evidence reference (or an adjacent contacts/evidence export) and define `unknown` as a first-class outcome.

### S2 — LinkedIn URL normalization must not manufacture a company identity

- **Where:** `enricher.py:133, 208-212`.
- **Breaks:** The regex captures one route segment and rebuilds it as a company URL. This loses the raw URL and route context, can accept non-profile paths as slugs, and provides no canonical handling for host aliases, query tracking, fragments, or trailing slashes. A personal `/in/{slug}` or share/post URL must never be rewritten as `/company/{slug}`.
- **Trigger:** A site links to `https://www.linkedin.com/in/founder/?trk=website` or `linkedin.com/sharing/share-offsite/?url=...`; normalization must preserve the first as a person profile (not a company page) and reject the latter as a non-company profile.
- **Fix:** Parse and validate host and path components; canonicalize only recognized `/company/{slug}` URLs to one HTTPS host/path form, retain the raw URL, and keep all other LinkedIn profile types distinct or reject them for the company-URL field.

## Verification design

### 1. Capture candidates; do not label them verified

1. Parse HTML and extract links only from actual `href` attributes (including supported structured profile-link fields), not arbitrary `@mentions`, visible prose, CSS, scripts, or substrings. Keep all distinct candidates rather than only the first. HTML-unescape and trim values before parsing.
2. Retain the raw observed value, source website URL, extraction timestamp, and extraction method. A first-party business website linking to a profile/WhatsApp URL is useful evidence of association, but is not by itself proof that the target is reachable, current, or owned by the business.
3. Parse each provider with its own URL rules. Never construct a social profile from a business name, `@mention`, phone number, or guessed slug.

### 2. WhatsApp number: valid link, registration, and business association are separate claims

- Parse only recognized click-to-chat routes (`wa.me/{number}` and documented WhatsApp send routes). URL-decode once, strip allowed formatting, require a 7–15 digit international number, and store a canonical `+` plus digits form. A `wa.me` path is already an international number; do not prepend Lebanon/Saudi country code based on lead location. Reject malformed, extension-bearing, overlong, or ambiguous inputs instead of repairing them heuristically.
- Record three independent facts: `number_syntax` (`valid`/`invalid`), `whatsapp_registration` (`confirmed`/`not_confirmed`/`unknown`), and `business_association` (`verified`/`candidate`/`rejected`/`unknown`). The link alone establishes only that the website published a WhatsApp-style target. A successful HTTP response, redirect, app-opening prompt, or valid `wa.me` URL does **not** prove the number is registered on WhatsApp.
- Confirm registration only through a documented, currently supported WhatsApp mechanism that is authorized for this use and returns an explicit registered-account result. Use the provider's required authorization, consent, and policy controls; do not bulk-enumerate arbitrary scraped numbers or treat an unofficial endpoint/client trick as authoritative. If no compliant supported check is available for a candidate, leave registration `unknown`. A human may record a public, user-initiated confirmation with its method and date; do not send a message, call, or OTP just to test a number.
- A `confirmed` WhatsApp registration does not identify the account owner and does not prove the account belongs to the lead. For business association, require credible first-party evidence tying the exact number to the business (for example, the official website's WhatsApp contact link) and corroboration appropriate to the workflow. Without adequate attribution evidence, keep it `candidate`; a registered account can be a private person, former employee, agency, or another business.

### 3. Instagram handle: verify profile existence separately from business ownership

- Accept a profile candidate only from a validated Instagram profile URL in an actual link or structured first-party field; accept a bare `@handle` only when it is a clearly typed social-link value, not free text. Parse the URL and reject posts, reels, stories, explore, login, share, and other non-profile routes. Store original URL/value and a normalized handle key. Do not use a guessed handle as a fallback.
- Check existence only via a permitted public or official platform interface. A successful profile response can establish that a profile was reachable at a point in time, not who owns it. Login walls, rate limits, robots/anti-bot responses, transient errors, and unavailable fields mean `unknown`, not absent or rejected. Do not log in, evade controls, scrape private content, or infer from follower data.
- Mark `verified_business` only with explicit, attributable evidence linking the specific profile to this business. Strong evidence is reciprocal: the business's controlled website links to that exact profile and the public profile identifies the matching business and links to the same official domain, with compatible name and business context. Record which exact observations support the decision. A one-way website link, a matching name/handle/logo/category/location, or a profile's existence alone remains a candidate and may require human review. Reciprocal links can still be stale or copied; conflicts, shared-brand ambiguity, or a personal account require review rather than an automatic pass.
- Keep `verified_person` (if ever supported) distinct from `verified_business`; an employee/founder's personal account must never silently become the company's account. Do not attribute personal ownership or employment from name similarity.

### 4. Normalize LinkedIn company URLs without asserting reach or ownership

- Parse URLs with a standards-compliant URL parser after HTML unescaping. Allow only `linkedin.com` and explicitly accepted first-party LinkedIn host aliases; reject lookalike domains and non-HTTPS schemes after normalization. Require the exact `/company/{slug}` route shape for the company URL field. Reject `/in/`, `/pub/`, jobs, posts, feed, sharing, and other routes as company pages; never rewrite route types.
- Canonical form: `https://www.linkedin.com/company/{slug}/`. Remove query and fragment tracking data and normalize host, scheme, and trailing slash; preserve the original URL and exact path slug as observed. A separately normalized comparison key may case-fold the slug, but must not replace the retained raw slug. Do not infer redirects or slug replacements as ownership without recording and validating the redirect target.
- Canonicalization is not verification. Record company-page existence as `confirmed`/`not_confirmed`/`unknown` independently from its business association; require explicit evidence comparable to the Instagram matching standard before setting `verified_business`.

### 5. Evidence record and safe downstream contract

Persist one append-only verification observation per candidate/check, with at least:

| Field | Purpose |
|---|---|
| `business_id`, `platform`, `profile_type` | Target entity and distinction between company and person profiles |
| `raw_value`, `canonical_value` | Original link/number and platform-normalized identity |
| `source_page_url`, `source_page_domain`, `observed_at` | Where/when the candidate was found |
| `check_type`, `check_method`, `checked_at`, `checker_version` | Reproducible description of the verification performed |
| `existence_status`, `association_status`, `reason_code` | Separate reachable/registered facts from business ownership decision |
| `evidence_refs` | Minimal source URL, relevant link target, response outcome, and permitted short excerpt or content hash; avoid storing whole pages or unrelated personal data |
| `reviewer`, `reviewed_at`, `supersedes_observation_id` | Human accountability and temporal correction/history |

Use statuses such as `candidate`, `verified_business`, `verified_person`, `rejected`, `collision`, and `unknown`, with provider-specific existence results. `unknown` is not a failure and must not erase a previous dated observation. Keep conflict/collision review explicit where unrelated leads claim the same profile. Candidate and unknown values must not increment completeness/lead score or qualify as verified outreach channels. If legacy CSV compatibility requires scalar columns, export only the selected verified business-owned value (and expose its status/evidence reference); provide a contacts/evidence export for the full history. Existing values backfill as `candidate`/`unknown`, never `verified` solely because they were previously stored.

## What must never be inferred

- A `wa.me` link or syntactically valid number means that the number is registered on WhatsApp, that the account is a business account, or that the lead owns or controls it.
- WhatsApp registration means the account belongs to the business. Never infer ownership from a matching country code, proximity, a matching business phone field, page placement, or a click-to-chat redirect.
- An Instagram handle exists because `@name` appears in HTML, a URL resembles a profile, or the handle resembles the business name. Existence does not mean business ownership.
- A matching name, transliteration, logo, category, city, followers, or visual resemblance alone proves that a profile belongs to a lead. Do not infer business association from identity similarity or unverified third-party directory listings.
- A founder/employee's personal Instagram or LinkedIn profile is a company asset or an authorized company contact. Never turn `/in/{person}` into `/company/{person}`.
- A LinkedIn URL is a company page merely because its slug follows `linkedin.com/`; normalization must not invent a company route or certify that its slug belongs to the business.
- A blocked, redirected, unavailable, private, rate-limited, or inconclusive check proves nonexistence or ownership. Use `unknown` until there is positive evidence.
- A profile/number that was once associated remains current forever. Keep time-bounded observations; do not transfer an old association to a renamed, recycled, or same-named account.

## Recommended order of work

1. Demote current extracted WhatsApp, Instagram, and LinkedIn values to unverified candidates; stop using them as proof of verified contact in scoring/sales exports.
2. Implement DOM-link-only candidate extraction, strict provider-specific parsers, and safe LinkedIn company URL canonicalization while retaining raw values.
3. Add the evidence/verification-observation contract and explicit separate states for number/profile existence and business association; implement only compliant supported WhatsApp checks and permitted public Instagram/LinkedIn checks. Make inconclusive checks `unknown`.
4. Define positive-evidence and human-review rules, collision handling, and verified-only downstream predicates; backfill historical CSV values as candidates.
5. Add offline fixtures for valid/malformed wa.me routes, valid/unregistered/inconclusive number checks, Instagram URL vs `@mention`/post routes, reciprocal and conflicting identity evidence, LinkedIn aliases/query parameters/person/share paths, and failure/unknown handling. Assert raw evidence retention, canonical outputs, and no score/sales qualification for unverified candidates.
