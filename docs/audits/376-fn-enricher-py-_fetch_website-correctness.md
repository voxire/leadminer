# 376 — `_fetch_website` correctness

**Target:** `enricher.py:161` `_fetch_website(url) -> tuple[str, dict]`
**Caller:** `enricher.py:238` (`check_websites`, itself called from `enricher.py:337` → `main.py:187`)

## Verdict

`_fetch_website` is **not** correct. The tri-state LIVE/DEAD/UNKNOWN contract its
docstring promises (`enricher.py:163-167`) is broken on the HTTP-status path:
`if status >= 400: return DEAD` (`enricher.py:182-184`) files Cloudflare 403s, 429s
and transient 500/503s as *server-confirmed broken sites*, which is precisely the
lead-signal inversion the docstring claims to prevent — measured end-to-end below,
403 → `recommended_service == "Website rebuild + maintenance"`. Separately,
`_EMAIL_RE` (`enricher.py:130`) is **quadratic**: a single 150 KB run of word
characters costs 107 s of uninterruptible CPU, and the 200 KB body cap
(`enricher.py:158`) does not stop it, because the cost is CPU and not I/O and so is
immune to `timeout=8`. And the bare `@` branch of `_INSTAGRAM_RE`
(`enricher.py:131`) manufactures an `instagram` handle out of the local part of every
email address on the page — `mailto:info@acme-lb.com` yields
`instagram == "acme"`.

Everything below was **executed**, not reasoned about. `requests` is not installed
in this environment and installing it is forbidden by `BRIEF.md` rule 3, so I stubbed
only the transport (`requests.Session.get` / `Response.raw.read` /
`Response.encoding`, the last mirroring `requests.utils.get_encoding_from_headers`)
and ran the **real, unmodified** `enricher.py:161-222` source. Findings 1, 2, 3 and
4 overlap with `084-SYNTH-adversarial-fix-review.md` §S1, `006`/`025` and `087`; the
difference is that each is now backed by a measured output rather than a code reading.

## How to reproduce

```
D=<any dir>; mkdir -p $D/requests   # $D/requests/__init__.py = ~60-line stub
python3 -u probe.py $D             # patches enricher.get_session, then calls _fetch_website
```

## Findings

### S1 — WAF blocks, rate limits and transient 5xx are recorded as DEAD → false rebuild pitch

- **Where:** `enricher.py:182-184`
- **Breaks:** `status >= 400` is the entire DEAD test, but 403/406/429/451 mean
  *"we were refused"*, not *"their site is broken"*, and 500/503 mean *"they are
  having a bad minute"*. `check_websites` maps `DEAD → website_live=False`
  (`enricher.py:250`), which buys **+20 lead_score** (`enricher.py:303-304`) and
  hits Tier 1 of the pitch recommender, returning
  `"Website rebuild + maintenance"` (`pitch_recommender.py:57-58`). So the highest
  value a salesperson is told to lead with — "they already paid for a site and it is
  broken" — is generated in bulk for any site that blocks a 40-thread crawler.
  This directly violates the function's own docstring at `enricher.py:163-167`.
- **Trigger:** `_fetch_website("https://walled-shop.test")` where the origin answers
  `HTTP/1.1 403` with a Cloudflare interstitial. Measured, one record with
  `rating=4.5, review_count=100, industry_priority="high"`:

  | HTTP | outcome | lead_score | recommended_service |
  |---|---|---|---|
  | 200 | `live` | 25 | `Lead-gen overhaul (landing pages + Google Ads + WhatsApp capture)` |
  | 403 | `dead` | 35 | `Website rebuild + maintenance` |
  | 406 | `dead` | 35 | `Website rebuild + maintenance` |
  | 429 | `dead` | 35 | `Website rebuild + maintenance` |
  | 451 | `dead` | 35 | `Website rebuild + maintenance` |
  | 500 | `dead` | 35 | `Website rebuild + maintenance` |
  | 503 | `dead` | 35 | `Website rebuild + maintenance` |
  | 404 / 410 | `dead` | 35 | `Website rebuild + maintenance` |

  Only the last two rows are a rebuild pitch the data supports.
- **Fix:** one line at `enricher.py:182` —
  `if status in (404, 410) or 500 <= status < 600 and status not in (502, 503, 504): return DEAD, contacts`
  and route 401/403/406/429/451 plus 502/503/504 to a retry (`httpclient.fetch_with_retry`,
  `httpclient.py:149`, already exists and already treats 429 and 5xx as retryable at
  `httpclient.py:146`) and then `UNKNOWN`.

### S1 — `_EMAIL_RE` is quadratic; `timeout=8` cannot stop it and the 200 KB cap is not enough

- **Where:** `enricher.py:130` (compiled) applied at `enricher.py:200`
- **Breaks:** `[a-zA-Z0-9._%+\-]+@…` has an unbounded leading `+` and no anchor, so
  at every start offset in a run of *n* word characters with no following `@` the
  engine matches to the end of the run and backtracks one character at a time:
  Θ(n²) per run. The regex runs at `enricher.py:200`, i.e. **after** the `finally`
  that closes the response (`enricher.py:193-197`), so nothing can interrupt it, and
  the request timeout is irrelevant because no socket operation is in progress —
  this is pure CPU. `check_websites` has no `future.result(timeout=…)`
  (`enricher.py:243`) and `ThreadPoolExecutor.__exit__` does
  `shutdown(wait=True)` (`enricher.py:237`), so `enrich()` blocks until the worst
  page finishes. With 40 workers contending for one GIL this does **not** parallelise.
- **Trigger:** any page containing one long run of `[A-Za-z0-9._%+-]` — a base64
  data-URI image, an inline base64 webfont, a minified-JS identifier blob, a long
  hex/`content=` attribute. All are ordinary on a modern small-business site and all
  fit inside `_MAX_BODY_BYTES = 200_000` (`enricher.py:158`). Measured on this
  machine, single call, `_EMAIL_RE.findall` only:

  | page shape | chars | CPU |
  |---|---|---|
  | `<img src="data:image/png;base64,iVBORw0KGgo…">` | 66,034 | **30.4 s** |
  | `<script>var abcdefghijabcdefghij…=1;</script>` | 60,024 | **17.8 s** |
  | `<style>@font-face{src:url(data:font/woff2;base64,d09GMgAB…)}` | 72,059 | **44.3 s** |
  | `<meta content="00000…0000">` | 150,017 | **140.3 s** |
  | `199,000` word chars | 199,000 | **> 480 s (did not finish)** |

  Confirmed quadratic — each doubling roughly quadruples: L=2,500 → 0.0125 s,
  5,000 → 0.134 s, 10,000 → 0.440 s, 20,000 → 2.32 s, 40,000 → 8.51 s.
  For contrast `_INSTAGRAM_RE` on the same 80,000-char page is **0.0022 s**: only
  the email regex is affected.
  Ten such sites in one run is ~18 minutes of pure stall, against the
  `timeout-minutes: 300` workflow budget, and it produces no diagnostic of any kind.
- **Fix:** bound the quantifier so the cost is linear —
  `_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]{1,64}@[a-zA-Z0-9.\-]{1,255}\.[a-zA-Z]{2,24}")`.
  Measured: L=100,000 goes from **58.4 s → 0.205 s** (285× faster), and
  `info@acme-lb.com`, `a.b+tag@shop-mall-lb.com`, `x@y.ae`,
  `user_name@sub.domain.co.uk` all still match while `not-an-email` and `a@b.c`
  still do not.

### S1 — `_INSTAGRAM_RE`'s `@` branch turns every email address into an Instagram handle

- **Where:** `enricher.py:131` (`(?:instagram\.com/|@)([a-zA-Z0-9_.]{2,30})/?`),
  applied at `enricher.py:206-210`
- **Breaks:** the `@` alternative has no host requirement and no left boundary, so
  *any* `@` in the document yields a handle. Every mailto href, every plain-text
  address, every address inside a JS bundle or JSON-LD blob does this. The value is
  then written to a scored, sales-facing column (`enricher.py:254-255`) with no
  further check.
- **Trigger:** a site whose only contact link is a mailto —
  `<html><head><style>@media(min-width:1px){}</style></head><body><a href="mailto:info@acme-lb.com">Email us</a></body></html>`,
  HTTP 200. Measured result: `{'email': 'info@acme-lb.com', 'instagram': 'media'}`.
  Isolating each source:

  | input | wrong `instagram` |
  |---|---|
  | `mailto:info@acme-lb.com` | `'acme'` |
  | `mailto:sales@restaurant-zahra-lb.com` | `'restaurant'` |
  | `mailto:contact@acme.com` | `'acme.com'` |
  | `mailto:hello@beirutdental.com` | `'beirutdental.com'` |
  | `mailto:a.b+tag@shop-mall-lb.com` | `'shop'` |
  | `@media (max-width:600px)` | `'media'` |
  | `@keyframes slide` | `'keyframes'` |
  | `@font-face{…}` | `'font'` |
  | `@import url("theme.css")` | `'import'` |
  | `@supports (display:grid)` | `'supports'` |
  | `@charset "utf-8"` | `'charset'` |
  | `twitter.com/@acme_lb` | `'acme_lb'` (wrong platform) |
  | `tiktok.com/@acmelb` | `'acmelb'` (wrong platform) |
  | `srcset="a@2x.jpg 2x"` | `'2x.jpg'` |
  | `<text xlink:href="mailto:a@b.png"/>` | `'b.png'` |

  Blast radius, measured on a record with a website, a phone, an address,
  `rating=4.8, review_count=140, category="florist"`, comparing `instagram=None`
  against `instagram="font-face"`:
  `completeness_score` 4 → 5 (`enricher.py:111`), `lead_score` 60 → 70
  (`enricher.py:294-295`), and `recommended_service` flips from
  `'Discovery call - scope the right service'` to
  `'Digital marketing retainer (Meta + Google + content)'`.
  Mitigation that limits the damage: the `not r.get("instagram")` guard at
  `enricher.py:254` means a scraped OSM `contact:instagram` tag wins, so this only
  corrupts rows where nothing was known — which is still every row that had no
  Instagram account to begin with.
- **Fix:** delete the `@` alternative —
  `_INSTAGRAM_RE = re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/([a-zA-Z0-9_.]{1,30})/?(?![a-zA-Z0-9_.])", re.I)`
  and, if handle-only text is genuinely wanted, match it in a second pass restricted
  to `href`/`content` attribute values on known social hosts.

### S1 — the `mailto:` capture is not an address; query strings and recipient lists are kept

- **Where:** `enricher.py:199` (`r'href=["\']mailto:([^"\'>\s]+)'`) → `enricher.py:200-204`
- **Breaks:** the capture has no terminator, so everything up to the closing quote is
  taken, including `?subject=`, `?body=` and comma-separated recipient lists. The
  blacklist check at `enricher.py:201-202` then reads
  `addr.split("@")[-1] == "acme-lb.com?subject=…"` which matches nothing in
  `_EMAIL_BLACKLIST`, so the polluted value is accepted. Because `mailto:` hits are
  searched first, a polluted mailto also *beats* a valid plain-text address
  elsewhere on the page.
- **Trigger / measured output:**

  | HTML | `email` written to the CSV |
  |---|---|
  | `<a href="mailto:info@acme-lb.com?subject=Website%20inquiry">` | `'info@acme-lb.com?subject=website%20inquiry'` |
  | `<a href="mailto:info@acme-lb.com?body=Hello">` | `'info@acme-lb.com?body=hello'` |
  | `<a href="mailto:info@acme-lb.com,sales@acme-lb.com">` | `'info@acme-lb.com,sales@acme-lb.com'` |
  | `<a href="mailto:info%40acme-lb.com">` | `'info%40acme-lb.com'` |

  None of these is pasteable into a mail client. The `?subject=` form is what every
  "Email us" button on a WordPress/Wix/Squarespace site produces.
- **Fix:** cut at the RFC 6068 delimiters —
  `re.findall(r'href=["\']mailto:([^"\'>\s?]+)', html, re.I)`, then validate each hit
  with `dedup._EMAIL_OK`-style anchoring (`^[^@\s,?]+@[^@\s,?]+\.[^@\s,?]+$`) before
  accepting it.

### S2 — `_LINKEDIN_RE` has no end anchor, and matches third-party `og:url` tags

- **Where:** `enricher.py:136` (`linkedin\.com/company/([a-zA-Z0-9\-_.]+)/?`),
  applied at `enricher.py:216-220`
- **Breaks:** the slug character class includes `.` and `-` with nothing following,
  so trailing punctuation is absorbed into the slug and the generated URL is wrong.
  Separately, the pattern has no notion of *whose* page it is on, so a site that
  embeds an aggregator's or a partner's LinkedIn link yields that other company.
- **Trigger / measured output:**
  - `<a href="https://www.linkedin.com/company/acme-lb-">` → `'https://linkedin.com/company/acme-lb-'`
  - `<a href="https://www.linkedin.com/company/acme.lb.">` → `'https://linkedin.com/company/acme.lb.'`
  - `<meta property="og:url" content="https://www.linkedin.com/company/unrelated-hq/">` → `'https://linkedin.com/company/unrelated-hq'`
  - `<a href="https://www.linkedin.com/in/john-doe">` → `None` (`/in/` not matched)
  - `<a href="https://www.linkedin.com/school/acme">` → `None`
- **Fix:** anchor the slug and prefer visible links over metadata —
  `linkedin\.com/company/([a-zA-Z0-9](?:[a-zA-Z0-9\-_.]*[a-zA-Z0-9])?)/?(?![a-zA-Z0-9\-_./])`.

### S2 — Instagram handles longer than 30 characters are silently truncated to a profile that does not exist

- **Where:** `enricher.py:131` (`[a-zA-Z0-9_.]{2,30}`), applied at `enricher.py:207-209`
- **Breaks:** `{2,30}` has no trailing boundary, so a 36-character handle is captured
  as its first 30 characters. That is a *different, existing-or-not* account, not a
  truncated match — worse than returning nothing, because `has_any_contact`
  (`main.py:144`, `len(instagram) > 3`) counts it as a reachable channel.
- **Trigger:** `<a href="https://instagram.com/a_very_long_handle_name_beyond_30_chars/">`
  → measured `instagram == 'a_very_long_handle_name_beyond'`. Expected: `None`
  (or the full handle).
- **Fix:** add a negative lookahead — `([a-zA-Z0-9_.]{2,30})/?(?![a-zA-Z0-9_.])`.

### S2 — extracted WhatsApp numbers are never normalised

- **Where:** `enricher.py:214` (`"+" + m.group(1).lstrip("+")`) written at `enricher.py:256-257`
- **Breaks:** `wa.me` links are frequently national-format. `main.py:192-194`
  normalises only `r["phone"]`; `whatsapp` keeps whatever the regex produced, and
  `dedup.normalize_phone` — which handles every one of these — is never called.
- **Trigger / measured output, with what `normalize_phone` would have produced:**

  | HTML | written to CSV | `normalize_phone(raw, cc)` |
  |---|---|---|
  | `wa.me/70123456` (Lebanese national) | `'+70123456'` | `'+96170123456'` |
  | `wa.me/0096170123456` | `'+0096170123456'` | `'+96170123456'` |
  | `whatsapp.com/send?phone=501234567` (KSA national) | `'+501234567'` | `'+966501234567'` |
  | `wa.me/9617012345678901` (16 digits, `{7,15}` truncates) | `'+961701234567890'` | `'+96617012345678901'`* |

  `+70123456` and `+0096170123456` are not dialable. `+96170123456` is a complete
  different number from `+70123456`. *last row is a truncation, not a fix.
  Note `_fetch_website` has no access to `country`, so the fix belongs at the
  call site, which does: `enricher.py:257`.
- **Fix:** `r["whatsapp"] = normalize_phone(contacts["whatsapp"], r.get("country") or "LB") or contacts["whatsapp"]` at `enricher.py:257`.

### S2 — `_EMAIL_BLACKLIST` is an exact-domain set and is trivially bypassed

- **Where:** `enricher.py:138-141` checked at `enricher.py:201`
- **Breaks:** `addr.split("@")[-1].lower()` is an exact string compare, so any
  subdomain, ccTLD variant or alternate host of a blocked platform passes. The
  entries exist because those domains put *platform* addresses in the HTML; the
  subdomains do exactly the same thing.
- **Trigger / measured output:**
  - `<a href="mailto:hello@mailer.wixpress.com">` → `email='hello@mailer.wixpress.com'` (should be dropped)
  - `<a href="mailto:abc@cdn.sentry.io">` → `email='abc@cdn.sentry.io'` (should be dropped)
  - `<a href="mailto:hello@wixpress.co.il">` → `email='hello@wixpress.co.il'`
- **Fix:** match on the registrable domain, not the full host —
  `if any(domain == b or domain.endswith("." + b) for b in _EMAIL_BLACKLIST): continue`
  and widen the set with the domains observed in a sample run.

### S2 — one GET, no retry, although `fetch_with_retry` already exists in the codebase

- **Where:** `enricher.py:175`
- **Breaks:** this is the mechanism that turns a transient blip into a permanent
  `website_live=False` row in the cumulative master (`main.py:209`), which is never
  re-verified because `website_live` is in `dedup._VOLATILE` (`dedup.py:114`) but is
  also re-fetched on every run — so one bad afternoon permanently poisons the
  rebuild-pitch list for that day. `httpclient.fetch_with_retry` (`httpclient.py:149`)
  already implements jittered backoff and already classifies 429 and 5xx as
  retryable (`httpclient.py:146`); `_fetch_website` uses none of it.
- **Trigger:** a site answering `HTTP 503` for 30 s during a maintenance window at
  03:00 UTC. Measured: one call, no retry, permanent `dead`.
- **Fix:** `result = fetch_with_retry(get_session(), "GET", url, timeout=8, max_bytes=_MAX_BODY_BYTES)` and map `result.status`/`result.error` per finding S1.

### S3 — the 200 KB cap is applied before decoding, so the footer contact is lost

- **Where:** `enricher.py:188-189`
- **Breaks:** `_MAX_BODY_BYTES = 200_000` (`enricher.py:158`, from
  `httpclient.py:54`) truncates at a byte offset that has nothing to do with where
  the document's contact block is. Contact details are almost always in the footer —
  i.e. past the cap on any page over 200 KB.
- **Trigger / measured output** (footer `<a href="mailto:footer@acme-lb.com">`,
  filler is space-separated so the S1 quadratic does not confound this):
  - body 105,605 B → `email='footer@acme-lb.com'`
  - body 200,605 B → `email=None`
  - body 210,105 B → `email=None`
- **Fix:** raise the cap to ~1 MB for this call site (`max_bytes=1_000_000`) — a 40-worker, one-GIL-bound, one-GET-per-site pipeline can afford it — or keep 200 KB for `live` detection and do a second bounded GET of the footer region only for `LIVE` sites.

### S3 — `timeout=8` is a per-socket-operation budget, and nothing bounds the total fetch

- **Where:** `enricher.py:175` (`timeout=8`), `enricher.py:237` (`with ThreadPoolExecutor`), `enricher.py:243` (`future.result()`)
- **Breaks:** a scalar `timeout` in `requests` applies to connect *and to each
  individual read*, not to the transfer. A server that trickles one byte per second
  keeps `r.raw.read(200_000)` alive for as long as it likes. Because
  `ThreadPoolExecutor.__exit__` runs `shutdown(wait=True)` and `future.result()` has
  no `timeout=`, one such host stalls `enrich()` → `main.py:187` blocks → the CSV
  writes at `main.py:209-213` never run and hours of scraper output are lost.
- **Fix:** `future.result(timeout=240)` in the `as_completed` loop at `enricher.py:243`
  plus a `(connect=8, read=8)` tuple; treat `concurrent.futures.TimeoutError` with the
  existing `except Exception` at `enricher.py:244`, which already maps to `UNKNOWN`.

### S3 — a non-ASCII local part yields a different, undialable address

- **Where:** `enricher.py:130`
- **Breaks:** the local-part class is ASCII-only with no left boundary, so the engine
  simply restarts one character later and emits the remainder.
- **Trigger:** `_EMAIL_RE.findall("joão@acme.com.br")` → measured `['o@acme.com.br']`.
- **Fix:** add `(?<![a-zA-Z0-9._%+\-])` in front of the pattern (also removes the
  mid-address restart that makes the S1 quadratic possible).

### S3 — leftovers that contradict the code's own comments

- `enricher.py:4` imports `urljoin, urlparse`; neither is used anywhere in the file.
- `enricher.py:327-328` still calls
  `urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)`. Nothing in
  the repository sets `verify=False` any more — the only occurrence of the string
  `verify` in `enricher.py` is inside the comment at `enricher.py:170` explaining
  that it was removed. The call is dead and implies an insecure mode that no longer
  exists.
- `enricher.py:202`'s `not addr.endswith(".png")` reads as "skip image filenames",
  but `addr` is already a full address, so it can only ever fire for a domain
  literally ending in `.png` (measured: `mailto:a@b.png`). Either drop it or state
  what it is for.

### S3 — `check_websites` issues one GET per record, not per URL

- **Where:** `enricher.py:231`, `enricher.py:238`
- **Breaks:** `targets` is built per record with no URL-level dedup. A row of shops
  whose OSM `website` tag points at a shared Facebook page (very common — and
  `osm.py:91` also accepts a bare `contact:website`, which is frequently a social
  page) produces N identical GETs. `dedup` merges on phone or `(name, city)`, so
  these rows survive.
- **Fix:** fetch each distinct URL once and fan the result back out —
  `{url: [idx, ...]}` instead of a per-record target list.

## Not a bug, but worth knowing

- **The DEAD/UNKNOWN split is sound in principle and I could not break it from the
  exception path.** Measured: `200 → live`; `403 → dead`; socket timeout → `unknown`;
  TLS failure → `unknown`; a body read that raises mid-stream → `unknown`
  (`enricher.py:176-178`, `enricher.py:190-192`). The invariant is: *only a status
  line the server actually sent can produce `DEAD`, and any transport-layer failure
  produces `UNKNOWN`.* Finding S1 is the sole violation of it.
- **`verify=True` and the exception path are correct.** A host with an invalid
  certificate raises and is reported `UNKNOWN` (`enricher.py:170-178`), which is
  right: an unverifiable site is not evidence of a broken one.
- **Capping the body before decoding is the right order** (`enricher.py:186-189`),
  and `or b""` at `enricher.py:188` correctly turns an empty body into `LIVE` with no
  contacts rather than an exception.
- **Discarding contacts when `outcome != LIVE` is correct** (`enricher.py:251`):
  a 403 interstitial's HTML must not be mined for addresses. Verified —
  `403` returns `('dead', {'email': None, 'instagram': None, 'whatsapp': None, 'linkedin': None})`.
- **The `not r.get(...)` guards at `enricher.py:252-259` are the right precedence**:
  values scraped from OSM/Google beat values regexed out of a page, so every
  extraction bug above only lands where nothing was already known.
- **Per-thread sessions are correct.** `get_session()` (`httpclient.py:61-71`) is
  thread-local, so the 40 workers in `check_websites` no longer share a cookie jar
  or a urllib3 pool. `_fetch_website`'s only session dependency is that one call.
- **`_INSTAGRAM_RE` correctly requires the trailing slash** (`instagram.com/acme`
  without one does not match the `instagram\.com/` branch), and correctly rejects
  `instagram.com/p/CxYzAbc` and `instagram.com/reel/…` — the 1-character `p` cannot
  satisfy `{2,30}`, and `reel` is in `_IG_BLACKLIST` (`enricher.py:142`).
  `instagram.com/ab/` → `'ab'` is correct.
- **`_WHATSAPP_RE` correctly ignores a 6-digit value** (`{7,15}`), so shortcodes and
  extensions are not mistaken for phone numbers.
- **Python-level exception safety of the whole body is fine.** The regex block at
  `enricher.py:199-222` is outside the `try`, but every operation on it is
  `str.findall` / `str.split` / `str.lower` on a `str`, none of which raises for
  these patterns; and `check_websites:244` catches anything that does escape.
  `_fetch_website` never propagates an exception in any of my ~90 probes.
- **Encoding choice at `enricher.py:189` (`r.encoding or "utf-8"`) does not affect
  the extracted fields, but it is still wrong for the body.** Measured: a UTF-8 page
  served as `Content-Type: text/html` (no charset) is decoded as `ISO-8859-1` —
  `مطعم` becomes `'Ù\x85Ø·Ø¹Ù\x85'` — because `requests` follows RFC 2616 for
  `text/*`. Served as `charset=windows-1256` the Arabic is worse (`'ظ…ط·ط¹'`). Only
  ASCII survives, and all four patterns are ASCII-only, so no extracted value is
  affected today. It becomes a live bug the moment audit 081's proposed `tech` dict
  starts reading `<title>` or `<meta name="description">`, which are Arabic on
  essentially every site in this dataset.

## Recommended order of work

1. **Bound the email regex** (`enricher.py:130`) — one character-class change, 285×
   measured speedup, removes the only unbounded-CPU path in the pipeline. Do this
   first because it is a one-line change and it currently threatens the 300-minute
   workflow budget on its own.
2. **Split HTTP status into DEAD / BLOCKED / TRANSIENT** (`enricher.py:182-184`) and
   route it through `fetch_with_retry`. This is the finding that inverts the product's
   core signal, and it is ~5 lines plus the call already written at `httpclient.py:149`.
3. **Delete the `@` branch of `_INSTAGRAM_RE`** (`enricher.py:131`) and add the
   trailing lookahead. Removes the two highest-volume wrong values in the export
   (`instagram` from mailto local parts, `instagram` from CSS at-rules).
4. **Terminate the `mailto:` capture** (`enricher.py:199`) and validate against
   `_EMAIL_OK` before accepting. Without this, `email` — the field sales acts on
   first — is frequently un-pasteable.
5. **Normalise `whatsapp` at the call site** (`enricher.py:257`) using the existing
   `dedup.normalize_phone`, and give `_LINKEDIN_RE` an end anchor.
6. Everything in S3, as cleanup.