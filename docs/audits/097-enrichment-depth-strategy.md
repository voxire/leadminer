# 097 — Enrichment depth strategy: the escalation ladder

## Verdict

The single-GET homepage pass (`enricher.py:158-214`) is the correct *floor* — it
is cheap, bounded at 200 KB, and preserves the LIVE/DEAD/UNKNOWN tri-state that
`043-scoring-upgrade` fixed — but it leaves measurable contact value on the table
for two very common site shapes in Lebanon/KSA: SMBs that put email only on
`/contact` or `/about` (not a `mailto:` on the homepage), and JavaScript-rendered
SPAs that return a 200 shell with zero text. The right answer is **not** "crawl
more pages" but a bounded escalation ladder: a hard **3-request / 600 KB / ~20 s
budget per lead**, gated by a strict "does the marginal contact change the lead's
outcome" predicate, with JS-rendered sites detected cheaply from the HTML already
in hand and deferred to a *separate, budgeted* render stage rather than rendered
inline. This document specifies that ladder; it assumes the hardened fetch,
politeness, retry, and cache layers from `054`, `064`, and `053` already exist and
sits on top of them as a pure "which URLs, in what order, under what budget" policy.

## Findings

### S2 — The homepage-only pass misses emails that live on `/contact`, `/about`, and the Arabic locale

- **Where:** `enricher.py:191-196` (email extraction from the single GET at `:167`), fan-out `check_websites` `:217-268`.
- **Breaks:** A large share of Lebanese/Saudi SMB sites put a contact *form* on the
  homepage (or a bare "اتصل بنا" nav link) and the real `mailto:`/phone/WhatsApp on
  a `/contact` or `/about` page, or only on the Arabic-locale page. The current
  extractor regexes only the 200 KB homepage body (`enricher.py:180-181`), so these
  sites return `LIVE` with `email=None`. That drops `completeness_score` by 1
  (`enricher.py:105-107`) and `lead_score` by 20 (`enricher.py:280-281`), and can
  knock an otherwise-contactable business out of `sales_ready`, whose filter is
  `has_any_contact(r) and priority in (high, medium)` (`main.py:202-205`). Finding
  that one email is a binary flip from "not in the pipeline" to "sales-ready".
- **Trigger:** a dentist's homepage that only renders `<a href="/contact">Contact</a>`
  with the `mailto:` on `/contact.html`; today the run records `email=None`.
- **Fix:** add Stage 2 of the ladder (below) — a budgeted, ordered fetch of
  `sitemap.xml → /contact → /about → /ar/` — fired only by the predicate in Stage 1.

### S2 — JS-rendered SPAs return a 200 shell, are marked "live", and their contacts are unrecoverable

- **Where:** `enricher.py:172-183` (2xx → read 200 KB → `LIVE`), extraction `:191-214`.
- **Breaks:** A React/Vue/Angular SPA (or a Wix/Shopify page that gates content
  behind JS) returns HTTP 200 with a near-empty shell: `<div id="root"></div>` plus a
  bundle script. The enricher reads 200 KB of that shell, finds no email/Instagram/
  WhatsApp/LinkedIn, and returns `LIVE` with empty contacts. `website_live=True`
  routes the lead to the SEO/marketing branches (`pitch_recommender.py:70-71,95-96`)
  even though the actual content — and any contact data — was never seen. The site is
  correctly "live", but the pipeline is blind to it, and there is no marker to
  distinguish "live and empty because it's a shell" from "live and empty because the
  business genuinely has no contact info".
- **Trigger:** a Riyadh startup built as a CRA app; the 200 KB body is one
  `<script src="/static/js/main.abc123.js">` and no text. `email=None`,
  `website_live=True`, no signal that rendering would help.
- **Fix:** detect the shell cheaply in-process (Stage 4 heuristics), record a
  `needs_render` flag, and defer to a bounded render stage rather than crawling it
  harder. Do **not** mark it `UNKNOWN` — the server answered 200 and the site is live.

### S2 — No per-lead or per-run budget: "fetch more pages" naively becomes an unbounded crawl

- **Where:** `enricher.py:217-230` (`workers=40`, no request cap anywhere), 200 KB
  cap only at `:180`; run ceiling is the workflow's 300-min timeout (`scrape.yml:11`).
- **Breaks:** The cost model (`073`) already shows the site sweep is the one part of
  enrichment that scales with traffic: ~40% of records have a website, and at 500k
  that is 200k fetches *per run*. A second request per site doubles that; an
  unbounded "follow internal links" doubles it again, and every extra request also
  multiplies proxy bandwidth (residential $2–3.50/GB, `073` Component 2) and CI time.
  Without a hard budget, the escalation ladder converts a cheap, predictable sweep
  into a crawl whose cost and duration nobody can state. There is no `max_requests`
  per lead, no run-wide request budget, and no byte budget beyond the per-request
  200 KB.
- **Trigger:** adding "also fetch every `<a href>` on the homepage" to a 500k-record
  run: 200k sites × N links each, CI time blows past 300 min and the job is killed
  mid-write (`main.py:124-131`).
- **Fix:** hard per-lead budget of **3 requests / 600 KB / ~20 s** and a run-wide
  escalation token budget (Stage 3), with priority gating so only high-value leads
  use the full budget.

### S3 — Blind path-guessing is both low-yield and impolite; discover via sitemap/robots first

- **Where:** no path discovery exists today (single GET at `enricher.py:167`).
- **Breaks:** Guessing `/contact`, `/about`, `/ar` for every escalated site will hit
  paths that `robots.txt` disallows (the politeness layer in `054` must then block
  them), and will waste requests on 404s for sites that use non-standard slugs
  (`/تواصل-معنا`, `/contact-us-2`, `/من-نحن`). `sitemap.xml` is the cheap, authoritative
  answer to "where is the contact page": it is meant for crawlers, lists the real
  URLs, and doubles as a free "how big is this site" signal that `081` wants anyway.
- **Trigger:** a WordPress site with `/تواصل-معنا` and a `robots.txt` that disallows
  `/contact`; guessing costs a request and yields nothing.
- **Fix:** make `sitemap.xml` (and its `robots.txt`-advertised location) the *first*
  escalation request, then fetch the single highest-value discovered path.

## Design — the escalation ladder

The ladder is a state machine over a single record's website. It is a *depth*
policy: it decides **which URLs, in what order, under what budget**, and assumes the
URL fetch itself is already SSRF-safe, TLS-verifying, robots-aware, rate-limited,
and cached (`054`, `064`, `053`). Every request in every stage flows through that
same hardened fetch primitive and the same per-host limiter.

### Stage 0 — Homepage GET (unchanged floor)

One GET to the homepage, 200 KB cap, classify `LIVE`/`DEAD`/`UNKNOWN`, extract the
four contact fields **plus the free tech/shell signals** described in `081` and Stage
4. This stays the floor for every record with a `website`. Nothing in this design
removes or weakens it. Cost: 1 request.

### Stage 1 — The "worth it" predicate (gate to Stage 2)

A second request is worth it **only when all** of the following hold:

| # | Condition | Rationale |
|---|---|---|
| 1 | `website_live is True` | A DEAD or UNKNOWN site has no subpage to fetch; escalation is wasted. |
| 2 | `email is None` | Email is the highest-value contact (+1 completeness `enricher.py:105-107`, +20 lead score `:280-281`). If we already have it, stop. |
| 3 | `industry_priority in ("high",)` **or** `has_any_contact(r) is False` | Escalate only when the marginal contact is *decisive*: high-LTV industry, or the lead currently has **zero** contacts so a find flips it into `sales_ready` (`main.py:202-205`). |
| 4 | Per-lead budget not exhausted (< 3 requests) | Hard ceiling (Stage 3). |
| 5 | Run-wide escalation budget has tokens | Global ceiling (Stage 3). |

```python
def should_escalate(r: dict, requests_used: int, run_budget) -> bool:
    return (
        r.get("website_live") is True
        and not r.get("email")
        and (r.get("industry_priority") == "high" or not has_any_contact(r))
        and requests_used < MAX_REQUESTS_PER_LEAD
        and run_budget.take()
    )
```

This predicate deliberately **does not** escalate for medium/low-priority leads that
already have a phone — they are already sales-ready and the marginal email is not
worth another request.

### Stage 2 — Ordered path fetch (at most 2 more requests)

Escalation fetches paths in this order, **stopping as soon as an email is found**
(the hunt is for the missing contact, not for completeness of the crawl):

1. **`sitemap.xml`** (and `sitemap_index.xml`, and any `Sitemap:` line advertised in
   `robots.txt`). One request. Parse `<loc>` entries. From them pick the single
   best candidate path in priority `contact → about → /ar locale`. This also yields
   the free "site size" signal (number of `<url>` entries) that `081` wants.
2. **The best discovered/guessed contact path.** One request. Candidate set, tried
   in priority order (first non-404 wins):

   ```
   contact: /contact  /contact-us  /contactus  /contact.html  /تواصل-معنا  /اتصل-بنا
   about:   /about    /about-us    /aboutus    /about.html    /من-نحن      /نبذة-عنّا
   locale:  /ar  /ar/  /en  /en/  /ar/home  (opposite of the <html lang> detected in Stage 0)
   ```

3. **Locale fallback.** If Stage 0's homepage HTML has `<html lang="en">` (or
   English-dominant text) and no email was found, fetch the `/ar/` (Arabic) variant —
   Lebanese and Saudi sites frequently keep contact info only on the Arabic page.
   Conversely, if the homepage is Arabic (`lang="ar"`), try `/en/`. The `<html lang>`
   value is already in the 200 KB we hold; this is a free signal, no extra request.
   For Lebanon only, a `/fr/` (French) variant is a cheap third candidate given the
   trilingual market.

Total Stage 2 cost: **≤ 2 requests** (sitemap + one path), for a per-lead ceiling of
**3 requests**. Sitemap-first replaces blind guessing, so a 404 on `/contact` does not
burn the second request on another guess unless a sitemap candidate exists.

Every Stage 2 request respects `robots.txt` for its exact path (`054`); a disallowed
path is skipped and the next candidate tried.

### Stage 3 — Budgets: capping cost per lead and per run

Hard per-lead budget:

| Resource | Cap | Notes |
|---|---|---|
| Requests | **3** | 1 homepage + ≤ 2 escalation. |
| Bytes | **600 KB** | 3 × the existing 200 KB cap (`enricher.py:155,180`). |
| Wall-clock | **~20 s total** | ~8 s homepage + 2 × ~6 s escalation, matching the per-call deadline in `054` (10 s wall-clock/call). |

Cost per lead, using `073` Component 2 numbers (residential $2.00–3.50/GB, datacenter
$0.077–0.60/GB, ~300 KB real transfer per request):

| Scenario | Requests/lead | Residential $/lead | Datacenter $/lead |
|---|---|---|---|
| Baseline (today) | 1 | ~0.0006–0.001 | ~0.00002–0.0002 |
| Worst case (3 requests) | 3 | ~0.0018–0.0032 | ~0.00007–0.0005 |
| Expected (~40% escalate × 2 extra) | ~1.8 | ~0.0011–0.0019 | ~0.00004–0.0003 |

Headline: **cap cost per lead at ~$0.003 residential / ~$0.0005 datacenter** as the
absolute ceiling, with ~60% of that as the realistic average. The ceiling is the
contract; the average is the forecast.

Run-wide budget (mirrors the token-bucket idea in `064`): a single process-shared
`EscalationBudget` with capacity **`MAX_EXTRA_REQUESTS_PER_RUN`** (default e.g.
`0.8 × len(websites)`, tuned to keep the job under the 300-min timeout at the
`073`-modeled ~3 s/request latency). Each Stage 2 request consumes one token; when
the bucket is empty, remaining leads stay at Stage 0. This is the only thing that
keeps a 500k-record run predictable.

### Stage 4 — Cheap JS-rendered detection and the fallback render stage

Detect shells **in-process, from the 200 KB already held** — no headless browser in
the main sweep. Compute over the decoded `html`:

```python
def js_rendered(html: str) -> bool:
    # True only when the shell has no extractable content. Beware SSR hydration:
    # __NEXT_DATA__ / __NUXT__ pages have full text in HTML already -> NOT a shell.
    if "__NEXT_DATA__" in html or "__NUXT__" in html:
        return False
    mount = any(m in html for m in ('id="root"', 'id="app"', 'data-reactroot',
                                    'createRoot(', 'Vue.createApp', 'app-root'))
    if not mount:
        return False
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return len(text) < 300  # visible-text threshold; tune once on real fixtures
```

On a positive detection: record `website_live=True` (the server answered 200 — this
is a live site, never `UNKNOWN`), set `needs_render=True`, and **do not** crawl it
further in Stage 2 (the subpages are the same shell).

Fallback render stage — a **separate, budgeted** job, not part of the main sweep:

- Input: records where `needs_render is True` **and** `email is None` **and**
  `industry_priority in ("high", "medium")` (priority-gate again: rendering is the
  expensive tier).
- Method: headless Chromium (Playwright) render + same contact extractors, or —
  cheaper, try first — fetch the app's JSON API endpoint referenced by the bundle if
  it is same-origin and GET-friendly. Budget renders the same way: 1 render per lead,
  a run-wide render cap, and a time limit per render (~10 s).
- Rationale: rendering the full corpus is uneconomic at scale (`081` S4 makes the same
  argument for Wappalyzer); rendering only the shell-detected, contact-less,
  high/medium-priority subset keeps it bounded to a small fraction of records.

### Politeness integration (what the ladder must not violate)

The ladder adds requests to the *same host*, so it inherits every rule from `054`/`064`:

- **One concurrent connection per host, ≥ 1.5 s between requests** to a host — the
  escalation requests for a site are serialized behind the same host limiter, never
  fired from 40 threads at once (`054` §2).
- **robots.txt per path**: `/contact`, `/about`, `/ar/` are checked individually; a
  disallowed path is skipped. `sitemap.xml` and `/robots.txt` are always allowed.
- **`Crawl-delay` and `Retry-After`** honored, with the `054`/`064` backoff caps.
- **Cache escalation results too** (`053`): a cache hit on `/contact` does not re-hit
  the host next run. Cache keys are per-URL, so escalation pages cache independently.
- **Identity stays honest**: `User-Agent` already declares `leadminer/1.0`
  (`enricher.py:125`); keep the contact point in it (the OSM scraper does,
  `osm.py:35-36`) so a webmaster can reach us instead of blocking the datacenter IP.

## Not a bug, but worth knowing

- **Schema change required.** `needs_render`, `js_rendered`, and any "site size"
  signal need new columns, and they must round-trip through `FIELDS` + `load_master`
  type-casting (`main.py:33-40,90-104`) — the same friction `081` flags. Budget for it.
- **Escalation value is capped by the contact model.** Escalation only pays off while
  `email` (and secondarily WhatsApp/Instagram) drive `lead_score`/`completeness`
  (`enricher.py:101-117,275-311`). If scoring changes, re-check the Stage 1 predicate.
- **SSR frameworks already work today.** Next.js/Nuxt/Gatsby prerender their text, so
  their emails are found by the existing homepage regex; the `__NEXT_DATA__`/`__NUXT__`
  guard in Stage 4 prevents a false "needs render" deferral of exactly the sites that
  need it least.
- **The `ar` locale is not the same as a language subdirectory.** Some sites serve
  Arabic at the root and English under `/en`; the `<html lang>` check in Stage 0
  disambiguates which direction to go, so always read `lang` before guessing.

## Recommended order of work

1. **Add the Stage 4 shell detector + `needs_render` flag** in `_fetch_website`
   (pure function over existing HTML, no new requests). This is the highest value per
   line and immediately separates "live and empty because shell" from "live and empty
   because no contacts" — fixing S2's blindness.
2. **Add the Stage 1 predicate + Stage 3 budgets** (per-lead 3-request/600 KB/20 s
   cap and the run-wide `EscalationBudget`). Ship this *before* any second request, so
   escalation is born bounded. Fixes S2 (budget).
3. **Implement Stage 2 sitemap-first discovery + ordered `/contact`/`/about`/`/ar/`
   fetch**, routed through the `054`/`064`/`053` fetch/retry/cache primitives. Fixes
   S2 (missed emails) and S3.
4. **Add the new columns and round-trip them** (`FIELDS`, `load_master`, `_BOOL_FIELDS`)
   so `needs_render` and site-size survive CSV reload.
5. **Build the separate render stage last**, gated on `needs_render` + `email is None`
   + high/medium priority, with its own render budget. Only after stages 1–4 prove the
   static escalation yield is worth chasing the JS-only tail.
