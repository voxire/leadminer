# 091 — Verify: do `places.rating` / `places.userRatingCount` force Enterprise SKU?

## Verdict

The claim is **half-true and, as actionable advice, wrong**. It is **confirmed** that
`places.rating` and `places.userRatingCount` are *Text Search Enterprise*-tier fields, and it is
**confirmed** that Enterprise carries a smaller free monthly cap (1,000) than Pro (5,000). But the
claim's causal framing — that *these two fields* are what force billing "at a higher SKU than
necessary" and shrink the free tier — is **refuted**. The field mask at `scrapers/google_places.py:31-42`
already contains `places.websiteUri` and `places.nationalPhoneNumber`, which are **also** Enterprise-tier
fields. Removing only `rating`/`userRatingCount` therefore changes **nothing** about the SKU, the per-call
price, or the free tier. This report supersedes the S1 in `073-cost-model.md`; it confirms the already-correct
note in `014-google-places-quota.md:122,234`.

---

## What I verified, and where

Google bills Places API (New) requests "at the highest SKU applicable to your request"
([Usage and Billing](https://developers.google.com/maps/documentation/places/web-service/usage-and-billing)).
Every field in the mask maps to a tier; the request is charged at the *highest* tier touched
([SKU details — "Field mask billing"](https://developers.google.com/maps/billing-and-pricing/sku-details)).

### Field → SKU mapping for the exact mask in `google_places.py:31-42`

From [Place Data Fields (New)](https://developers.google.com/maps/documentation/places/web-service/data-fields)
(Text Search column):

| Field in mask | Text Search SKU | Tier |
|---|---|---|
| `places.id` | Text Search Essentials (IDs Only) | Essentials |
| `nextPageToken` | Text Search Essentials (IDs Only) | Essentials |
| `places.displayName` | Text Search Pro | Pro |
| `places.formattedAddress` | Text Search Pro | Pro |
| `places.location` | Text Search Pro | Pro |
| `places.types` | Text Search Pro | Pro |
| `places.websiteUri` | **Text Search Enterprise** | **Enterprise** |
| `places.nationalPhoneNumber` | **Text Search Enterprise** | **Enterprise** |
| `places.rating` | **Text Search Enterprise** | **Enterprise** |
| `places.userRatingCount` | **Text Search Enterprise** | **Enterprise** |

**Key fact:** four fields — `websiteUri`, `nationalPhoneNumber`, `rating`, `userRatingCount` — are all
Enterprise tier. None of the four is *more* Enterprise than another. `rating`/`userRatingCount` do **not**
trigger "Text Search Enterprise + Atmosphere" (that is reserved for `reviews`, `photos`,
`editorialSummary`, etc.), but that distinction is irrelevant here since the mask is already Enterprise.

### Free caps and first-tier price, from the current price list

From [core services pricing list](https://developers.google.com/maps/billing-and-pricing/pricing),
"Places API (New)" table (Last updated 2026-09-28):

| SKU | Free monthly cap | First paid tier ($/1,000) |
|---|---|---|
| Text Search Essentials (IDs Only) | **Unlimited** | — |
| Text Search Pro | **5,000** | $32.00 |
| Text Search Enterprise | **1,000** | $35.00 |
| Text Search Enterprise + Atmosphere | 1,000 | $40.00 |

So the Enterprise tier *does* have a smaller free cap (1,000) than Pro (5,000), and a ~9% higher
first-tier price ($35 vs $32). These numbers are consistent with what `073` cited.

---

## Findings

### S1 — The "remove `rating`/`userRatingCount` to cut cost" fix is a no-op

- **Where:** the claim in `073-cost-model.md:15-17` and its S1 at `073-cost-model.md:368-380`
  (also `073:134-136`, `073:378`). The actual mask is `scrapers/google_places.py:31-42`.
- **Breaks:** The claim attributes the Enterprise billing to `rating`/`userRatingCount` and
  recommends removing them to "drop to Pro" and enlarge the free tier. That is wrong. `websiteUri`
  and `nationalPhoneNumber` are also in the mask and are *also* Enterprise-tier fields
  ([Place Data Fields (New)](https://developers.google.com/maps/documentation/places/web-service/data-fields)).
  Because billing is at the highest SKU touched
  ([Usage and Billing](https://developers.google.com/maps/documentation/places/web-service/usage-and-billing)),
  the request is already Enterprise with or without `rating`/`userRatingCount`. Deleting those two fields
  leaves the exact same SKU (Text Search Enterprise), the same $35.00/1,000 price, and the same
  1,000-call free cap. The ~9% "overspend" and the "larger free tier" described in `073:S1` would **not**
  materialize.
- **Trigger:** Apply the recommended fix (drop `rating`/`userRatingCount`), observe the billing statement
  is unchanged.
- **Fix:** Either (a) drop the Enterprise fields you can actually do without — `websiteUri` **and**
  `nationalPhoneNumber` **and** `rating` **and** `userRatingCount` — to reach Pro ($32/1k, 5,000 free), which
  requires re-sourcing phone/website/rating in a second Place Details pass (likely a net cost *increase*
  given Place Details Enterprise is $20/1k per call); or (b) keep the mask as-is and accept that every call
  is already Enterprise. `rating`/`userRatingCount` alone are not a cost lever. This is exactly what
  `014-google-places-quota.md:122,234` already correctly stated.

### S2 — The claim as worded is wrong, but the underlying SKU facts are right

- **Where:** `073-cost-model.md:15-17,78,81-82,368-380`.
- **Breaks:** The factual premises are all confirmed (see tables above): `rating`/`userRatingCount` are
  Enterprise fields; Enterprise free cap is 1,000 vs Pro 5,000; highest-SKU-wins billing. The defect is
  purely in the *attribution* — naming `rating`/`userRatingCount` as the cause and prescribing their
  removal as the cure. The correct causal statement is: **the mask is Enterprise-tier, full stop, because it
  requests website, phone, rating, and review count; the rating fields are not the binding constraint.**
- **Trigger:** A reader of `073` acts on its "Recommended order of work" item 1 (`073:434-436`) and removes
  only the rating fields, expecting a cost drop that never arrives.
- **Fix:** Rewrite `073`'s S1 to say the mask is Enterprise-tier due to *four* fields, and that reaching
  Pro requires dropping all four (impractical — see S1). The "free 9% / larger free tier" language should
  be removed or scoped to the hypothetical where *all four* Enterprise fields are dropped.

---

## What I could not verify

- **Absolute dollar impact for this project.** Whether this is a real problem depends on call volume
  (query count × pagination), which is 67–201 calls/sweep per `014` — i.e. currently $0/month under the
  1,000-call Enterprise free cap regardless of the rating fields. I did not (and, per the brief, will not)
  run the scrapers to measure real pagination depth, so I cannot state an actual monthly overage. At the
  stated cadences the rating-field question is financially moot: the run stays free either way.
- **Whether Google's page-token / "highest SKU" rule has hidden exceptions** (e.g. regional SKUs for the
  EEA or India). I reviewed the global price list and the web-service data-fields page only. If the project
  ever moves to an India or EEA billing account, re-check
  [India pricing](https://developers.google.com/maps/billing-and-pricing/pricing-india) and the
  [EEA terms](https://cloud.google.com/terms/maps-platform/eea).
- **Historical claim provenance.** The `$200/month credit` model mentioned in `014:126` is explicitly
  ended "February 28, 2025" on the Usage and Billing page; the current model is per-SKU free caps. I verified
  the *current* model, not the transition history.

---

## Not a bug, but worth knowing

- **The correct optimization lever is the phone/website fields, not the rating fields.** If lead-gen ever
  needs to actually *reduce* Places spend, the only way down from Enterprise is to stop requesting
  `websiteUri` and `nationalPhoneNumber` in the search call and resolve them later (a second Place Details
  Enterprise call at $20/1k, or scraping the website from a cheaper source). That is an architectural trade-off,
  not a one-line field-mask edit. `rating`/`userRatingCount` cost nothing extra beyond what the mask already
  pays.
- **`014` was right; `073` regressed it.** `014-google-places-quota.md:122` and `:234` already contain the
  correct note that "dropping them would not reduce the API cost." `073`'s S1 contradicts this without new
  evidence. When consolidating, keep `014`'s wording and discard `073`'s causal claim.
- **Pricing is current as of the docs' "Last updated 2026-09-28 UTC."** Re-verify before any contracting,
  as Google's Places pricing has changed materially in the past (the March 2025 restructure).

## Recommended order of work

1. **Correct `073-cost-model.md` S1 and its "Recommended order of work" item 1** — remove the claim that
   dropping `rating`/`userRatingCount` saves ~9% or enlarges the free tier. Replace with the accurate
   statement that the mask is Enterprise-tier due to four fields and is effectively free at current cadence.
2. **No code change is warranted for the rating fields specifically.** If cost ever becomes real (daily or
   higher cadence), evaluate dropping `websiteUri` + `nationalPhoneNumber` + `rating` + `userRatingCount`
   together against a second-stage Place Details lookup — as a deliberate architecture decision, not a
   field-mask tweak.
3. **Keep `014`'s correct note** as the canonical statement on this topic.

## Sources

- Place Data Fields (New) — field→SKU tier mapping:
  https://developers.google.com/maps/documentation/places/web-service/data-fields
- Places API Usage and Billing — "billed at the highest SKU applicable":
  https://developers.google.com/maps/documentation/places/web-service/usage-and-billing
- Google Maps Platform core services pricing list (Places API New free caps + prices):
  https://developers.google.com/maps/billing-and-pricing/pricing
- SKU details — "Field mask billing" rule and example:
  https://developers.google.com/maps/billing-and-pricing/sku-details
