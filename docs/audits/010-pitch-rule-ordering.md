# 010 — Pitch rule ordering

## Verdict

`recommend_service()` is a first-match-wins chain, but its generic website/social rules run before vertical-specific pitches. As a result, dead-site hospitality leads never receive the intended RTYLR pitch, and several other branches only work for a narrower set of inputs than their comments imply. The review-count gate also masks the low-rating rule for every live site with fewer than 20 reviews.

## Findings

### S1 — Dead websites bypass the hospitality pitch
- **Where:** `pitch_recommender.py:52-55`, before RTYLR at `:73-76`.
- **Breaks:** Any record with a website and `website_live` other than the literal boolean `True` returns `Website rebuild + maintenance` immediately. Category and available contact channels are never considered, so hospitality businesses with dead sites cannot reach their intended RTYLR recommendation.
- **Trigger:** `category="restaurant"`, `website="https://example.test"`, `website_live=False`, `phone="..."` returns the rebuild pitch, not `RTYLR commerce OS (POS, online ordering, CRM)`. The same ordering preempts lead-gen verticals too.
- **Fix:** Resolve vertical-specific priorities before the generic dead-site return, or make dead-site status a modifier to the category-specific pitch.

### S2 — No-website social rules shadow vertical-specific pitches
- **Where:** `pitch_recommender.py:57-63`, before RTYLR and lead-gen at `:73-83`.
- **Breaks:** Every no-website record with Instagram or Facebook returns at Tier 2. RTYLR hospitality leads therefore cannot reach RTYLR in this state; lead-gen verticals cannot reach either lead-gen branch when they have social presence but no website. Only the e-commerce set gets its specialized Tier 2 label.
- **Trigger:** A clinic with Instagram and no website gets `Website launch + capture their existing audience`, rather than the lead-gen launch; a restaurant with Instagram and no website gets the same generic website pitch rather than RTYLR.
- **Fix:** Check eligible vertical-specific rules before the generic no-website/social returns, or explicitly encode which verticals should be exceptions.

### S2 — Review-count gate masks low ratings and preempts later pitches
- **Where:** `pitch_recommender.py:65-71`, ahead of RTYLR (`:75`) and lead-gen (`:80`).
- **Breaks:** A live-site record with `review_count < 20` always returns the SEO pitch, even when its rating is poor or its category has a more specific RTYLR/lead-gen pitch. Missing or invalid review counts are normalized to `0` at `:40-44`, so they also take this branch. The following reputation condition is reachable only for live sites with at least 20 reviews and a truthy rating below 4.0—not for the “nearly always” case of fewer reviews. Also, `completeness` is parsed at `:45-49` but never used, despite the Tier 3 comment citing weak completeness.
- **Trigger:** A live restaurant website with rating `2.5` and 8 reviews gets `SEO audit + visibility upgrade`, not reputation turnaround or RTYLR. A live clinic with 8 reviews gets the same SEO pitch instead of lead-gen overhaul.
- **Fix:** Make the review-count condition a signal within a priority decision rather than an unconditional return; evaluate low rating and intended vertical rules explicitly, and either use or remove the completeness input/comment.

## Branch reachability and set overlap

- `_ECOM_FRIENDLY` has 22 entries, `_RTYLR_TARGETS` 17, and `_LEAD_GEN_VERTICALS` 39 (`pitch_recommender.py:99-129`). Their exact intersections are: e-commerce ∩ RTYLR = `{ice_cream}` (1); e-commerce ∩ lead-gen = empty (0); RTYLR ∩ lead-gen = empty (0); the three-way intersection is empty (0). Thus the only category that can enter the e-commerce-specific branch while also being an RTYLR target is `ice_cream`; with no website and social presence, Tier 2 gives it the e-commerce pitch before Tier 4.
- Tier 4 is not wholly unreachable: with no website and no social it can fire for an RTYLR category with a phone; with a live website it can fire only after Tier 3 does not match and a phone or social channel exists. Its dead-website and no-website/social cases are shadowed by Tiers 1 and 2 respectively.
- Tier 5 is not wholly unreachable either: its no-website path is available only without social presence (otherwise Tier 2 returns), and its live-website path only after Tier 3 does not match. Dead websites are intercepted by Tier 1. For a lead-gen category with no website and no social, Tier 5 intentionally precedes and replaces Tier 6's generic launch pitch.
- Tier 7 is reachable for an established live site with social presence only if earlier review/rating checks do not match and the category is in neither vertical set. The fallback is also reachable (for example, a live unrelated-category site with no social, at least 20 reviews, and no low rating); neither is globally dead code.

## Not a bug, but worth knowing

- The rating predicate is not strictly unreachable: a live site with at least 20 reviews and a truthy rating below 4.0 reaches it. The ordering makes it unreachable specifically when the earlier `< 20` condition is true.
- The low-rating helper treats unparsable values as not low (`:132-137`); this means malformed ratings fall through rather than producing a reputation pitch.

## Recommended order of work

1. Decide and encode precedence for dead websites and no-website/social leads versus RTYLR and lead-gen verticals.
2. Restructure Tier 3 so review count, rating, and completeness are signals rather than a broad early return; preserve a deliberate path for the reputation recommendation.
3. Add table-driven tests covering dead/live/no website × social/contact state × hospitality/lead-gen/other category, including review counts below and above 20.
