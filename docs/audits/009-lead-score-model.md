# 009 — lead_score() audit

## Verdict

`lead_score()` does not rank what the product needs it to rank. Sixty of its 100
points are four contact fields (`email`/`whatsapp`/`phone`/`instagram`) that are
already captured by `completeness_score`, so the score is ~60% a restatement of
"how reachable is this lead" and only ~40% "is there an opportunity to pitch."
Worse, the one signal that *should* drive the ranking — weak digital presence —
is non-monotonic (`live +10`, `dead +20`, `absent +0`), which pushes the
project's own flagship pitch target (no-website businesses, `BRIEF.md:54`) to the
*bottom* of the ranking. The dead-vs-live asymmetry the author built in is
defensible in direction but is (a) washed out by the contact block and (b) erased
by a `min(score, 100)` clamp that effectively never binds.

## Findings

### S2 — Website signal is non-monotonic; no-website leads are under-scored
- **Where:** `enricher.py:241-244`
- **Breaks:** The website dimension is `live +10`, `dead +20`, `absent +0`. If
  "dead > live" is justified because a dead site is a stronger rebuild
  opportunity (`pitch_recommender.py:52-54`: "dead website is the strongest pitch
  in the database"), then "no website" — the greenfield build, the project's #1
  new-site pitch target (`BRIEF.md:54`, `main.py:11`) — should score at least as
  high as a dead site, yet it scores **0**. The result is a ranking inversion: a
  fully built-out clinic (live site, full contacts, 4.7★) scores **85**, while a
  dead-website café that actually needs a rebuild scores **60**, and a
  no-website restaurant scores **40** (see worked examples below). The leads most
  worth selling to are ranked lowest.
- **Trigger:** compare a no-website restaurant (phone + instagram) against a
  live-website clinic (full contacts). The restaurant is a pitch target; the
  clinic is not. The clinic scores 85, the restaurant 40.
- **Fix:** make the website signal monotonic. One line, e.g. add a third branch:
  `elif not record.get("website"): score += 20  # greenfield build`, keeping
  `dead` ≥ `live`. (Decide the exact weight, but "absent" must not be 0.)

### S2 — lead_score double-counts four fields already in completeness_score
- **Where:** `enricher.py:232-239` vs `enricher.py:101-117`
- **Breaks:** `email`, `whatsapp`, `phone`, `instagram` each add points in
  *both* `completeness_score` and `lead_score`. Those four fields total **60 of
  100** points in `lead_score`. There is no place where the two columns are
  literally summed, so this is not a hard "add twice into one number" bug — but it
  means the two "quality" columns are highly correlated and `lead_score` adds
  almost no independent signal about digital-weakness (the product's actual
  goal). A downstream consumer who sorts or filters on *both* columns is
  effectively double-weighting the same four fields, and a lead is pushed to the
  top of `lead_score` merely for being contactable, not for being a good pitch.
- **Trigger:** a lead with `email`+`whatsapp`+`phone`+`instagram` but a healthy
  live website and 4.8★ rating scores **85**; a dead-website business with only a
  phone scores **60**. The contact block, not the opportunity, decides ordering.
- **Fix:** reduce the four contact weights and/or restructure `lead_score` as a
  product of "reachability × opportunity" rather than an additive sum of the same
  completeness fields.

### S3 — `min(score, 100)` effectively never binds
- **Where:** `enricher.py:259`
- **Breaks:** The theoretical maximum with a *live* website is exactly 100
  (`20+15+15+10+10+15+10+5`), so the clamp is a no-op for every live-website
  lead. The only configuration that exceeds 100 is a *dead* website plus all
  seven other signals (`20+15+15+10+20+15+10+5 = 110`). That requires email **and**
  whatsapp **and** phone **and** instagram **and** a dead site **and** high
  priority **and** rating < 4.0 **and** multi-source, all simultaneously — an
  effectively empty set in real data. So the clamp binds ≈ never; and in the one
  case where it does, it clamps 110→100 and erases precisely the dead(+20) vs
  live(+10) distinction the author intended (a perfect dead-website lead ties a
  perfect live-website lead at 100).
- **Trigger:** enumerate the max score (table below); live-max = 100 (inert
  clamp), dead-max = 110 (clamp active only in the all-signals case).
- **Fix:** rebalance the weights so the distribution actually uses 0–100 (and the
  top is meaningful), then either keep or drop the clamp — as written it is dead
  code. Do not rely on the clamp to enforce a ceiling the weights already miss.

### S3 — lead_score is computed twice; the `enrich()` pass ignores industry_priority
- **Where:** `enricher.py:290` vs `main.py:132,137`
- **Breaks:** `enrich()` calls `lead_score()` at `enricher.py:290` **before**
  `industry_priority` is ever set (`main.py:132`), so that first pass always
  contributes `+0` for priority and returns a systematically low score.
  `main.py:137` then recomputes and overwrites it, so the final CSV is correct —
  but any other caller of `enrich()` in isolation gets `lead_score` values that
  are 0–15 points too low, and the first computation is pure waste.
- **Trigger:** `from enricher import enrich; enrich(records)` and read
  `record["lead_score"]` — the priority points are missing.
- **Fix:** delete `enricher.py:290` (let `main.py` own `lead_score`), or compute
  `industry_priority` inside `enrich()` before scoring.

### S3 — "dead website" = 5xx/network-error only; 404s are scored as live (upstream)
- **Where:** `enricher.py:147-148` (root cause), consumed at `enricher.py:243-244`
- **Breaks:** `live = r.status_code < 500`, then the early return
  `if not live or r.status_code >= 400: return live, contacts` returns
  `live=True` for any 4xx. So a site that returns **404** (the canonical "dead"
  signal) is marked `website_live=True`, gets the **+10** live bonus, and yields
  no contacts. The `+20` dead-website branch fires only for 5xx errors and
  timeouts/connection failures — never for the most common way a site dies. This
  directly undermines the `+20` rule's premise.
- **Trigger:** a business whose domain now returns HTTP 404 → `website_live=True`
  → `lead_score` adds +10, not +20.
- **Fix:** in `_fetch_website`, treat `status_code >= 400` as not live (return
  `False, contacts`). This is the foundational fix that makes the dead-website
  rule actually target dead sites.

## The asymmetry, argued explicitly

**Direction (dead +20 > live +10): defensible.** It matches the recommender's
Tier 1 (`pitch_recommender.py:52-54`) and the product thesis: a business that
already paid for a website that is now broken has demonstrated intent and budget
precedent, so the rebuild is a high-probability sale. A live website is a weaker
signal (they may not be in-market).

**Magnitude: mostly decorative.** The 10-point gap is (1) swamped by the 60
contact points, and (2) erased by the clamp at the top of the scale. It rarely
changes relative ordering.

**The real defect is the *absent* case, not the dead-vs-live gap.** The website
dimension is `none = 0 < live = 10 < dead = 20`, which is non-monotonic in
opportunity. If "dead beats live because dead = stronger opportunity," then
"absent" (greenfield = cleanest possible sale) should be at or above "dead," not
zero. The asymmetry should be refuted not because dead should not exceed live,
but because the full ordering `0 < live < dead` is wrong at its left edge.

## Scoring table

Max component weights (for the clamp analysis):

| Signal | Points |
|---|---|
| email | +20 |
| whatsapp | +15 |
| phone (≥7 digits) | +15 |
| instagram | +10 |
| website live | +10 |
| website dead | +20 |
| industry high / medium | +15 / +8 |
| rating < 4.0 | +10 |
| multi-source | +5 |

Theoretical maxima: **live = 100**, **dead = 110 → clamped 100**.

Worked examples (six realistic leads):

| # | Lead | Website | Contacts | Priority | Rating | Multi | Breakdown | lead_score | completeness |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Café, dead site, phone only | dead | phone | high | 3.6★ | no | 15 + 20 + 15 + 10 | **60** | 2 |
| 2 | Restaurant, no site, phone + IG | none | phone, ig | high | — | no | 15 + 10 + 15 | **40** | 2 |
| 3 | Clinic, established, full stack | live | email, wa, phone, ig, li | high | 4.7★ | no | 20+15+15+10 + 10 + 15 | **85** | 7 |
| 4 | "Perfect" dead-site lead (clamp) | dead | email, wa, phone, ig | high | 3.5★ | yes | 60 + 20 + 15 + 10 + 5 = 110 | **100** | 7 |
| 5 | Salon, live site, email + phone | live | email, phone | medium | — | no | 20+15 + 10 + 8 | **53** | 3 |
| 6 | Bare storefront, low priority | none | phone | low | — | no | 15 | **15** | 1 |

Read the table top-to-bottom and the inversion is obvious: the established clinic
(#3, no pitch needed) outranks every lead that actually needs a website (#1, #2,
#5, #6). The clamp case (#4) is the only row that would exceed 100, and it ties
#3 at the cap — the dead-vs-live asymmetry is invisible exactly where it should
be loudest.

## Not a bug, but worth knowing

- `len(phone) >= 7` (`enricher.py:236`) excludes short fixed lines (some Lebanese
  landlines are 6 digits); those leads get 0 phone points regardless of validity.
- The `+5` multi-source bonus (`enricher.py:256`) rewards records that survived a
  dedup merge, and merged records also tend to be more complete (because
  `dedup._merge`, `dedup.py:59-60`, keeps the richer record). So it is another
  completeness proxy stacked on top of the four contact fields, not an
  independent quality signal.
- `rating < 4.0` (`enricher.py:253`) assumes a numeric rating. This is guaranteed
  within the current pipeline (`load_master` casts to float, `main.py:57-61`), but
  `enrich()` called standalone against string ratings (e.g. a future CSV source)
  would raise `TypeError`. A defensive `float(...)` cast would harden it.

## Recommended order of work

1. Fix `_fetch_website` so 4xx is "not live" (`enricher.py:147-148`) — this makes
   the dead-website rule target real dead sites (S3, foundational).
2. Make the website signal monotonic: give "no website" a non-zero weight ≥ dead
   (`enricher.py:241-244`) (S2).
3. De-couple `lead_score` from `completeness_score` — cut the four contact weights
   and let opportunity dominate (`enricher.py:232-239`) (S2).
4. Rebalance weights so 0–100 is actually used and the clamp is either meaningful
   or removed (`enricher.py:259`) (S3).
5. Delete the redundant `lead_score()` call in `enrich()` (`enricher.py:290`) (S3).
