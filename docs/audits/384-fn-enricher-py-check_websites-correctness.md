# 384 — `check_websites` correctness

**Target:** `enricher.py:225` (`check_websites`) and the extraction helper it drives,
`_fetch_website` at `enricher.py:161`.
**Lens:** correctness.
**Audited revision:** `99493b967f03dabf298866bcbb11618298100f34`.

**How this was verified.** `requests` is not installed and the brief forbids installing
it, so I extracted the tree at HEAD into a scratch directory, added a ~30-line stub
`requests`/`urllib3` whose `Session.get` returns a scripted `Response` (with `.status_code`,
`.encoding`, `.raw.read(n, decode_content=True)`, `.close()`), and drove the real
`check_websites` end-to-end. Every "exact wrong output" below is program output, not
reasoning. Where a claim depends on how real `requests` follows redirects I say so.
Note that `enricher.py` was being edited concurrently while I worked; one intermediate
state introduced a circular import. All numbers come from the pinned HEAD snapshot.

---

## Verdict

The tri-state `LIVE`/`DEAD`/`UNKNOWN` contract that `check_websites` exists to enforce
is **violated in both directions at once**, and the extraction regexes write confidently
wrong values into the CSVs. A CSS at-rule (`@media`) is recorded as an Instagram handle
and lands the lead in `sales_ready.csv`; a Cloudflare `403` is recorded as a dead website
and triggers the "strongest pitch in the database"; and a previously confirmed dead-site
verdict is permanently erased by a single blip because the master CSV is cumulative.
Separately, `_EMAIL_RE` is quadratic: **one 200 KB response body costs 69–209 seconds of
CPU** inside a worker, and the byte cap added to stop exactly this attack does not bound
CPU time at all.

---

## Findings

### S1 — `_EMAIL_RE` is quadratic; one 200 KB body costs 69–209 s of CPU inside a worker

- **Where:** `enricher.py:130` (pattern), consumed at `enricher.py:200`, over the body read
  at `enricher.py:188`.
- **Breaks:** the pattern is `[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}` — both
  leading quantifiers are unbounded and the pattern is unanchored. On a body that is one
  long run of `[A-Za-z0-9._%+-]` with no `@` anywhere, the engine retries the whole greedy
  match from every one of the n start offsets, giving O(n²). Measured on an idle machine,
  `_EMAIL_RE.findall("A" * n)`:

  | n (contiguous chars) | wall time |
  |---|---|
  | 10,000 | 0.296 s |
  | 40,000 | 5.759 s |
  | 80,000 | 19.937 s |
  | 160,000 | 69.6 s |
  | 199,960 | 209.3 s |

  `enricher.py:188` caps the read at `_MAX_BODY_BYTES = 200_000`, so 199,960 is exactly
  the post-cap worst case — **~2–3.5 minutes of CPU for a single HTTP response**. The
  comment at `enricher.py:186-187` says the cap exists because "a hostile or misconfigured
  host can stream forever"; it bounds bytes but not time, so the stated threat is not
  actually mitigated. There is no per-request alarm, and `timeout=8` (`:175`) is a socket
  timeout only — it does not cover regex evaluation.
- **Consequence chain:** 40 such sites (`enricher.py:237`) hold 40 workers for 3.5 minutes
  each with no progress output (the `done % 200` tick at `:261-262` stalls too). The job
  budget is `timeout-minutes: 300` (`.github/workflows/scrape.yml:25`), so the run is
  killed and the whole enrichment pass produces nothing.
- **Trigger:** `records = [{"website": "https://hostile.example", ...}]` where
  `https://hostile.example` returns `200` with a 200,000-byte body of `b"A"`.
- **Benign variant that also triggers it:** an inline SVG data URI. A 17 KB page whose
  base64 payload has no `/` produced a 16,895-char contiguous run and took **1.74 s**.
  That is not routine, but it means ordinary large pages can cost seconds.
- **Aggravating factor:** `enricher.py:200` is
  `for addr in mailto_hits + _EMAIL_RE.findall(html):`. Python evaluates both operands of
  `+` before the loop body runs, so the expensive scan happens **even when `mailto_hits`
  already holds a valid, non-blacklisted address** and the answer is already known.
- **Fix (measured):** bound the quantifiers and stop the scan restarting inside a run —
  `re.compile(r"(?<![a-zA-Z0-9._%+\-])[a-zA-Z0-9._%+\-]{1,64}@[a-zA-Z0-9.\-]{1,190}\.[a-zA-Z]{2,24}")`
  runs in **0.003 s** at 160,000 chars (69.6 s → 0.003 s), and additionally rejects the
  invalid 100-char local part that the current pattern happily matches. Possessive
  quantifiers (`{1,64}+`, Python ≥3.11, and `pyproject.toml` requires ≥3.12) are also
  sufficient (0.049 s). Also move the `_EMAIL_RE.findall` call behind a check for whether
  `mailto_hits` already yielded something usable.

### S1 — `@media` is written to the CSV as the Instagram handle

- **Where:** `enricher.py:131`, applied at `enricher.py:206-210`.
- **Breaks:** `_INSTAGRAM_RE = r"(?:instagram\.com/|@)([a-zA-Z0-9_.]{2,30})/?"`. The `@`
  alternative matches **any at-sign in the document**, so a CSS at-rule is indistinguishable
  from an Instagram mention. `_IG_BLACKLIST` (`enricher.py:142`) contains only
  `{"instagram","p","explore","accounts","stories","reel","reels","tv"}` and does not
  cover CSS/Tailwind keywords. Accepted captures I measured: `media`, `charset`,
  `supports`, `keyframes`, `import`, `namespace`, `tailwind`, `apply`, `layer`, `font`
  (from `@font-face`), `js` (from `@js-twitter`), `3dtransform`.
- **Trigger:** an entirely ordinary page whose only "social" content is a media query:
  ```html
  <html><head><style>@media (max-width:600px){.nav{display:none}}</style></head>
  <body><h1>Bakery Beirut</h1></body></html>
  ```
- **Exact wrong output:** `records[0]["instagram"] == "media"`.
- **Why it is worse than cosmetic:** it is not just a wrong string, it *shadows the real
  one*. `@media` appears in `<head>`, so it always wins the `finditer` scan before any
  genuine handle in `<body>`. Measured on a page containing a working
  `https://www.instagram.com/beirut_bakery/` link in the body: `instagram == "media"`,
  the real handle discarded. Downstream, `enricher.py:111` awards `+1`
  `completeness_score`, `enricher.py:294-295` awards `+10` `lead_score`, and
  `main.py:141-145` counts `len(instagram) > 3` as a contact channel — measured
  `sales_ready == True` for a record with no contact data at all.
- **Fix:** drop the bare `@` alternative and only accept handles that are preceded by
  something handle-shaped, e.g. require `(?:instagram\.com/|(?:^|[\s"'>])@)` plus a
  blacklist containing `media, charset, supports, keyframes, import, namespace,
  tailwind, apply, layer, font`.

### S1 — HTTP 403 / 401 / 429 are recorded as `DEAD`, inverting the core lead signal

- **Where:** `enricher.py:182-184` (`if status >= 400: return DEAD`), contradicting the
  module's own stated invariant at `enricher.py:150-153`, which says explicitly that a
  Cloudflare challenge is `BLOCKED / UNKNOWN` and "is NOT a pitch signal".
- **Breaks:** `enricher.py:150-153` lists "Cloudflare challenge" as a transport-style
  non-answer, and `enricher.py:164-166` repeats "Never infer 'dead' from a transport
  failure". But a Cloudflare interstitial arrives as an ordinary `403`, so it takes the
  `>= 400` branch. Measured: `401, 403, 429 → website_live=False` (alongside the correct
  `404, 410, 500, 502, 503, 504`). `429` is worse still: it is our own fault for hitting
  a rate limit, which is exactly what 40 unsynchronised workers do.
- **Trigger:** `records = [{"website": "https://good.example", "rating": 4.6,
  "phone": "+9611234567", "category": "Bakery"}]`; the host answers
  `403` with `<html>Just a moment...</html>`.
- **Exact wrong output:** `website_live = False`, which gives `+20` at
  `enricher.py:303-304` and hits `pitch_recommender.py:57-58`
  (`if has_dead_website: return "Website rebuild + maintenance"`). Measured end-to-end:
  `lead_score = 43`, `recommended_service = "Website rebuild + maintenance"` — for a
  perfectly healthy business. That is the pitch `pitch_recommender.py:55-56` calls
  "the strongest pitch in the database".
- **Fix:** `if status in (401, 403, 405, 429) or status >= 500: return UNKNOWN, contacts`
  and keep `404/410/451` as the only `DEAD` verdicts — those are the statuses that mean
  "the site is gone".

### S1 — A previously confirmed `website_live` is destroyed by one transient failure

- **Where:** `enricher.py:250` (unconditional overwrite), interacting with `main.py:150`
  (loads the cumulative master) and `main.py:209` (writes it back cumulatively).
- **Breaks:** `all_businesses.csv` is cumulative, so every record arrives already carrying
  last run's verdict. `enricher.py:250` overwrites it with the current outcome and never
  considers what was there. A single connection reset, DNS blip or 8-second timeout turns
  a previously *confirmed* `False` — a server-verified dead site, the single highest-value
  pitch signal in the product — into `None`, i.e. a blank CSV cell. Blank is
  indistinguishable from "never checked" (`cli.py:105-113`), and
  `lead_score`/`recommend_service` both ignore it. The signal is gone and never comes back
  unless a later run happens to succeed.
- **Trigger:** master CSV rows
  `[{"website": "https://dead.example", "website_live": "False"},
    {"website": "https://live.example", "website_live": "True"}]`
  (as parsed by `main.py:103`); this run both hosts reset the connection.
- **Exact wrong output:** `[(False, None), (True, None)]` — measured, and the run printed
  `[Enricher] 0 live / 0 dead / 2 unreachable`.
- **Fix:** only downgrade when the new verdict is not `UNKNOWN` —
  `if outcome != UNKNOWN or r.get("website_live") is None: r["website_live"] = ...`
  (or record `website_live_checked_at` so a stale verdict is distinguishable).

### S2 — `mailto:` query strings become malformed, unsendable emails

- **Where:** `enricher.py:199` — `re.findall(r'href=["\']mailto:([^"\'>\s]+)', html, ...)`.
- **Breaks:** the character class excludes quotes, `>` and whitespace but **not `?` or `=`**,
  so everything after the address is captured and then `.lower()`d into the value at
  `enricher.py:203`.
- **Trigger:** `<a href="mailto:Info@Bakery-LB.com?subject=Website%20inquiry">mail</a>`
- **Exact wrong output:** `records[0]["email"] == "info@bakery-lb.com?subject=website%20inquiry"`.
  Measured downstream: `completeness_score = 3`, `lead_score = 48`,
  `has_any_contact == True` (`main.py:143`) so it reaches `sales_ready.csv`. It is not
  sendable, and `dedup.py:116` `_EMAIL_OK` rejects it, so it also silently fails to merge.
- **Fix:** `re.findall(r'href=["\']mailto:([^"\'>\s?]+)', html, re.IGNORECASE)`.

### S2 — Non-email strings are accepted as emails; the blacklist misses every subdomain

- **Where:** `enricher.py:202`.
- **Breaks (a), `.png` is a one-off patch for one extension:**
  `if domain not in _EMAIL_BLACKLIST and not addr.endswith(".png")`. `[a-zA-Z]{2,}` happily
  treats `jpg`, `webp`, `woff`, `svg` as TLDs. Measured outputs:
  | input | extracted `email` |
  |---|---|
  | `<img src="logo@2x.jpg">` | `logo@2x.jpg` |
  | `<img src="icon@3x.webp">` | `icon@3x.webp` |
  | `@font-face{src:url(Font@2.20.woff)}` | `font@2.20.woff` |
  | `background:url(bg@2x.png)` | `None` (the sole patched case) |
- **Breaks (b), exact-match on the full domain so every subdomain slips through:**
  `enricher.py:201` computes `addr.split("@")[-1].lower()` and tests membership in
  `_EMAIL_BLACKLIST` (`enricher.py:138-141`), which lists bare roots. Measured:
  `no-reply@static.wixpress.com`, `bounce@cdn.squarespace.com`,
  `notify@email.shopify.com`, `dSN@o12345.ingest.sentry.io` are **all accepted**. The
  blacklist's practical value is close to zero — and `<p>user@localhostx.zz</p>` is
  accepted too.
- **Fix:** suffix-match the registrable domain
  (`any(domain == b or domain.endswith("." + b) for b in _EMAIL_BLACKLIST)`), require the
  TLD to be a plausible one (`[a-zA-Z]{2,24}`), and validate against `_EMAIL_OK`
  (`dedup.py:116`) before storing rather than patching one extension.

### S2 — No retry and no per-host politeness on the highest-volume call site in the pipeline

- **Where:** `enricher.py:175` calls `get_session().get(...)` directly instead of
  `httpclient.fetch_with_retry`.
- **Breaks:** the project already built the right helper — `httpclient.py:149` implements
  exponential backoff, full jitter and `Retry-After` parsing precisely for this — and both
  API scrapers use it (`scrapers/osm.py:39`, `scrapers/wikidata.py:70`). `enricher.py` is
  the only caller of `fetch_with_retry` that does not, and it is the one making tens of
  thousands of requests. `grep -rn fetch_with_retry --include=*.py` returns only
  `httpclient.py`, `osm.py` and `wikidata.py`.
- **Amplifier:** `workers=40` is unreachable. `enrich(records)` (`enricher.py:326`) takes
  no `workers` parameter and calls `check_websites(records)` at `enricher.py:337`, so
  `max_workers=40` is hardcoded for the whole pipeline; `main.py` and `cli.py` expose no
  flag. There is also no per-host grouping or delay.
- **Trigger:** 40 records whose `website` values are all on one Wix/Bricks/GoDaddy tenant,
  or one shared franchise domain. The host returns `429`, and per the S1 finding above each
  `429` becomes `website_live=False`.
- **Fix:** route `_fetch_website` through `fetch_with_retry(session, "GET", url,
  max_bytes=_MAX_BODY_BYTES)`, drop `max_workers` to 8–12, and group targets by host with
  a per-host delay.

### S2 — No per-URL dedup: N records on one URL produce N identical GETs

- **Where:** `enricher.py:231-238`.
- **Breaks:** `targets` is built per record, not per distinct URL, so `pool.submit` is
  called once per row. `records = [rec(f"Branch{i}", "https://chain.example") for i in
  range(25)]` produced **25 HTTP GETs for one distinct URL** (measured). Multi-branch
  businesses and franchise domains with a shared `websiteUri` are common in this dataset,
  and this is the direct feed into the 429 cascade above.
- **Fix:** key `targets` by `url`, keep `dict[str, list[int]]` of the indices each URL
  serves, and fan the single result back out.

### S2 — Social-profile "websites" are certified LIVE and disappear from the pitch list

- **Where:** `enricher.py:222` / `enricher.py:250`; upstream at
  `scrapers/google_places.py:291` (`website=place.get("websiteUri")`, verbatim, no host
  filter).
- **Breaks:** Google Places returns `https://www.instagram.com/...`,
  `https://www.facebook.com/...` and `https://linktr.ee/...` in `websiteUri` for businesses
  that have no website. `check_websites` fetches them, gets `200`, and records
  `website_live=True`. `main.py:199-200` routes on the truthiness of `website`, so the
  record goes into `with_websites.csv` and is **excluded from `without_websites.csv`** —
  the new-site pitch list, which per `main.py:10-11` is a primary product output.
- **Trigger:** `records = [{"website": "https://www.instagram.com/beirut.bakery/",
  "category": "Bakery"}]`, host answers `200`.
- **Exact wrong output:** `website_live = True`, plus `+10` at `enricher.py:301-302`, and
  `recommend_service` falls to Tier 3 `pitch_recommender.py:70` instead of
  "Website launch + capture their existing audience" (`pitch_recommender.py:65-66`).
- **Fix:** treat a non-first-party host as no website — in `check_websites`, skip
  `instagram.com|facebook.com|linkedin.com|tiktok.com|linktr.ee|wa.me|youtube.com|x.com`
  and route the handle into the `instagram`/`facebook` column instead.

### S2 — WhatsApp numbers get a fabricated country code

- **Where:** `enricher.py:214` — `contacts["whatsapp"] = "+" + m.group(1).lstrip("+")`.
- **Breaks:** `wa.me` links are routinely published in national form. The code prepends
  `+` unconditionally and applies **no** country-code normalisation, even though
  `dedup.normalize_phone` exists and is applied to `phone` at `main.py:192-194`.
- **Triggers and exact wrong outputs** (all measured), on a record whose own `phone` is
  `+96170123456` and `country` is `LB`:

  | HTML | extracted `whatsapp` |
  |---|---|
  | `https://wa.me/70123456` | `+70123456` |
  | `https://api.whatsapp.com/send?phone=70123456` | `+70123456` |
  | `https://wa.me/966501234567` | `+966501234567` (correct) |

  `+70` is not a country code; the number is undialable. It still earns `+15` at
  `enricher.py:290-291`.
- **Fix:** `contacts["whatsapp"] = normalize_phone(m.group(1), country)` — the function is
  already there, it just is not called.

### S2 — Orphan liveness survives: `website=None` with `website_live=True/False`

- **Where:** `enricher.py:231` filters targets on `r.get("website")`, so records without a
  website are never touched; nothing downstream clears the field either.
- **Breaks:** a master row can carry `website=None` together with a `website_live` from a
  previous run (or from a merge at `dedup.py:114`, which treats `website` and
  `website_live` as independently volatile). `check_websites` leaves the contradiction in
  place. This is exactly the invariant `docs/audits/048-data-quality-validation.md:188-189`
  specifies (`website is None ==> website_live is None`) and `:311` raises
  `ERR_REF_ORPHAN_LIVENESS` for — but `cli.py:135` `cmd_validate` does not implement that
  check, so nothing catches it.
- **Trigger:** `records = [{"website": None, "website_live": True}]`
- **Exact wrong output (measured, unchanged after the call):**
  `{"website": None, "website_live": True}`.
- **Fix:** `for r in records: if not r.get("website"): r["website_live"] = None` in the
  same pass, and add the invariant to `cmd_validate`.

### S2 — The printed liveness summary over-reports, and mis-reports exactly when you need it

- **Where:** `enricher.py:264-266`.
- **Breaks:** `unknown_count` correctly filters on `r.get("website")` but `live_count` and
  `dead_count` do not, so they count stale/orphan `website_live` values on records that
  were never fetched. The three numbers therefore do not sum to the number of targets, and
  they are not comparable to the totals `main.py:215-217` prints from the same data.
- **Trigger:** `records = [LiveSite(website set, 200), NoSite(website=None,
  website_live=True), NoSite2(website=None, website_live=False), RealDead(website set, 404)]`.
- **Exact wrong output:** 2 sites fetched, but the program prints
  `[Enricher] 2 live / 2 dead / 0 unreachable`. Reality is 1 live, 1 dead, plus 2 orphans.
- **Fix:** add the same `r.get("website")` guard to `live_count` and `dead_count`.

### S3 — `_LINKEDIN_RE` slug guard misses LinkedIn's own reserved paths

- **Where:** `enricher.py:219` — `if slug not in {"company", "in", "pub"}`.
- **Breaks:** measured as accepted: `feed`, `admin`, `learning`, `jobs`, `blog`,
  `settings`, `share`. Any site embedding LinkedIn's company-feed or admin widget gets a
  fabricated `https://linkedin.com/company/<that word>` in the CSV. The slug is also taken
  from the *first* match only, with no fall-through (unlike the email and Instagram loops
  at `:200-210`, which do fall through).
- **Fix:** require the match to sit in an `href` and extend the reserved set with
  `feed, admin, learning, jobs, blog, settings, share, sales, marketing`.

### S3 — A bare 3xx with no `Location` is recorded as a healthy site

- **Where:** `enricher.py:182` — the `DEAD` threshold is `>= 400`.
- **Breaks:** measured `300, 301, 302, 304 → website_live=True`. Real `requests` only
  surfaces a 3xx to us when it cannot follow it (no `Location`, or the hop limit reached),
  which is a misconfigured redirect — recorded as a working site. Narrow, because
  `TooManyRedirects` raises and correctly lands in `UNKNOWN` at `:176-178`.
- **Fix:** treat any un-followed `3xx` as `UNKNOWN` rather than `LIVE`.

### S3 — Dead code and hygiene

- `enricher.py:214` — `m.group(1).lstrip("+")` can never strip anything: the capture group
  at `:133` is `(\d{7,15})`.
- `enricher.py:327-328` — `enrich()` still calls
  `urllib3.disable_warnings(InsecureRequestWarning)`, but `_fetch_website` now uses
  `verify=True` (`:170-175`), so nothing is insecure; it is a leftover that also globally
  suppresses a security warning.
- `enricher.py:232-233` — the `if not targets: return records` early return skips the whole
  summary block, so a run where every source yielded no website produces a **completely
  silent** enrichment pass (measured: stdout is `''`). Given how often that is the actual
  failure mode, it should print the zero line.
- No URL scheme normalisation. `osm.py:95-96` and `wikidata.py:103-104` prefix `https://`,
  but `check_websites` is the natural chokepoint and does not, so a master row with
  `website="www.example.com"` raises `MissingSchema` inside `_fetch_website` and is
  `UNKNOWN` forever.

---

## What is actually correct — the invariants that do hold

The scaffolding around the extraction is sound, and the fix should keep all of it:

1. **Transport failure never becomes `DEAD`.** `_fetch_website` returns `UNKNOWN` from both
   the request `except` (`enricher.py:176-178`) and the body-read `except`
   (`:190-192`), and `enricher.py:250` maps `UNKNOWN → None`. `lead_score`
   (`:300-304`) and `recommend_service` (`pitch_recommender.py:34-36`) both use identity
   checks (`is True` / `is False`), so `None` is never coerced into "dead" anywhere
   downstream. This is the invariant that the 4xx handling in the S1 above breaks.
2. **No data race.** Workers receive only the URL string (`enricher.py:238`); every
   mutation of `records[idx]` happens on the main thread (`:249-259`). `enumerate` at
   `:231` yields unique indices, and `as_completed` order therefore cannot change any
   result — each record is independent. (Caveat: if a caller ever passes the same dict
   object twice with different `website` values, the two writes race and the winner is
   nondeterministic. `dedup` copies records via `dict(record)` at `dedup.py:210`/`:218`, so
   this is not reachable in the pipeline.)
3. **`_fetch_website` cannot raise.** Every path is wrapped, so `check_websites`'s
   `except Exception` at `:244-248` is genuinely belt-and-braces rather than load-bearing.
4. **Sessions are thread-local** (`httpclient.py:58-71`), which genuinely fixes the old
   shared-`Session` race the comment at `enricher.py:124-127` describes.
5. **The email and Instagram loops fall through to later matches** (`:200-204`,
   `:206-210`) rather than accepting the first hit, so a blacklisted candidate does not
   suppress a valid one found later.
6. **Contact extraction is correctly gated on `LIVE` only** (`:251`), and existing
   non-empty values are never overwritten (`:252-259`).
7. **`write_csv` is atomic** (`main.py:120-133`), so a run killed by the 300-minute timeout
   loses the new output but not the previous master.

---

## Recommended order of work

1. **`enricher.py:130`** — bound the `_EMAIL_RE` quantifiers and add the lookbehind.
   One line, no behaviour change, removes a measured 69–209 s stall per site. Also make
   `:200` lazy so `mailto_hits` short-circuits the scan.
2. **`enricher.py:131`** — remove the bare `@` alternative and extend `_IG_BLACKLIST`.
   This is currently injecting false contacts into `sales_ready.csv`.
3. **`enricher.py:182`** — split the status handling so `401/403/429/5xx` are `UNKNOWN`.
   Restores the module's own documented invariant and stops healthy sites being pitched a
   rebuild.
4. **`enricher.py:250`** — do not overwrite a non-`None` prior verdict with `UNKNOWN`.
5. **`enricher.py:199` and `:201-202`** — stop `?` in the `mailto` capture; suffix-match
   the blacklist; validate with `dedup._EMAIL_OK` instead of the `.png` patch.
6. **`enricher.py:175` and `:231`** — route through `fetch_with_retry` and key `targets`
   by URL so one URL is one request. Then lower the hardcoded 40 and expose it.
7. **`enricher.py:214`** — normalise the WhatsApp number with `dedup.normalize_phone`.
8. **`enricher.py:231`** — clear `website_live` when there is no website, and add the
   invariant to `cmd_validate`; fix the `live_count`/`dead_count` guards at `:264-265`.
9. **S3s**, opportunistically.