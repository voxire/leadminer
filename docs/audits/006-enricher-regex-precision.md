# 006 — Contact-extraction regexes: high false-positive rate, low recall on obfuscated sites

## Verdict

The four contact regexes in `enricher.py` are precision-first in name only. The Instagram regex matches *any* `@` token, so it records CSS at-rules (`@media`, `@import`), retina asset markers (`@2x.png`), and email domains (`@acme.com`) as Instagram handles; the mailto parser concatenates `?subject=` query strings into the email field; and the email regex treats `@2x.jpg`/`@2x.webp` asset paths as addresses because only `.png` is filtered. On the recall side, Cloudflare-encoded `data-cfemail` emails (the majority of small-business sites), URL-encoded/formatted WhatsApp numbers, and any footer contact past byte 200,000 are silently missed. The `email`, `instagram`, and `whatsapp` columns are not trustworthy as extracted.

## Findings

### S1 — Instagram regex captures CSS at-rules, retina markers, and email domains as handles

- **Where:** `enricher.py:128`, consumed at `enricher.py:161-165`. The pattern is `(?:instagram\.com/|@)([a-zA-Z0-9_.]{2,30})/?`.
- **Breaks:** The `@` alternative is unanchored and matches every `@` character in the page, regardless of context. The capture class `[a-zA-Z0-9_.]{2,30}` then grabs whatever identifier follows. Three distinct false-positive families all resolve to the *first* `finditer` hit and are written to `contacts["instagram"]` because none of them appear in `_IG_BLACKLIST` (`enricher.py:139`):

  1. **CSS at-rules** — `@media`, `@import`, `@charset`, `@font-face`, `@supports`, `@keyframes`. These sit near the top of virtually every WordPress/Wix/Squarespace page inside a `<style>` block, so they are usually the *first* `@` match:
     ```html
     <style>
       @charset "UTF-8";
       @import url('https://fonts.googleapis.com/css2?family=Roboto');
       @media (max-width: 768px) { .nav { display: none; } }
     </style>
     ```
     → `contacts["instagram"] = "charset"` (first hit wins).

  2. **Retina/asset markers** — `@2x`, `@3x` in `srcset`/`src` attributes. The capture class permits `.`, so `2x.png` is one token:
     ```html
     <img src="https://cdn.acme.com/logo@2x.png" srcset=".../logo@2x.png 2x, .../logo@3x.png 3x">
     ```
     → `contacts["instagram"] = "2x.png"`. Note the email loop *does* suppress this same string as `.png`, but the Instagram loop runs independently and does not.

  3. **Email domains and social mentions** — any `@domain` or `@handle` in text, blog comments, or Twitter meta tags:
     ```html
     <meta name="twitter:site" content="@acme">
     <div class="comment">thanks @admin, order placed</div>
     <p>Email us: info@acme.com</p>
     ```
     → `contacts["instagram"] = "acme"`, `"admin"`, or `"acme.com"` respectively (`.` is legal in the capture, so `domain.com` is captured whole). Blog-comment `@mention`s and footer "email us" text are extremely common, so the wrong value is near-certain whenever the CSS case does not fire first.
- **Trigger:** Any page whose `<head>` contains a `<style>` with a media query and no literal `instagram.com/` link before it. `_fetch_website` will fill the Instagram column with `media`, `charset`, or `import`.
- **Fix:** Drop the bare `@` alternative. Match only a real Instagram URL (`(?:https?://)?(?:www\.)?instagram\.com/([a-zA-Z0-9_.]{2,30})`) and reject handles containing a `.` (Instagram disallows it) or any blacklisted token. If `@handle` support is kept, require the preceding character to be whitespace/`>`/`(` and the handle to match `[a-zA-Z0-9_.]*[a-zA-Z0-9_]+[a-zA-Z0-9_.]*` with no dot-run, and still verify it is not a CSS at-rule keyword.

### S1 — mailto links with a `?subject=`/`?body=` corrupt the email field

- **Where:** `enricher.py:154-158`. `href=["\']mailto:([^"\'>\s]+)` captures everything after `mailto:` up to a quote/whitespace, then `domain = addr.split("@")[-1]` and the result is stored verbatim.
- **Breaks:** Contact-form and "email us" links routinely append a subject. The query string is captured into `addr`, survives the blacklist check (the blacklisted tokens are exact domains, so `domain.com?subject=quote` is not `domain.com`), and is stored as the email:
  ```html
  <a href="mailto:info@acme.com?subject=Request%20a%20quote">Request a quote</a>
  ```
  → `contacts["email"] = "info@acme.com?subject=request%20a%20quote"`. This is a malformed address that will not bounce but is useless for outreach, and it silently pollutes the single most important output field. The same failure applies to multi-recipient mailtos:
  ```html
  <a href="mailto:sales@acme.com,info@acme.com">Contact</a>
  ```
  → `contacts["email"] = "sales@acme.com,info@acme.com"` (`split("@")[-1]` yields `acme.com`, passing the check).
- **Trigger:** Any `mailto:` whose target carries a query string or comma-separated addresses — common on restaurant/retail template sites.
- **Fix:** Strip the query before validating: `addr = addr.split("?")[0]`, then require the whole token to satisfy `_EMAIL_RE` (or split on `,` and take the first valid address) before accepting it.

### S2 — Retina/versioned asset paths produce fake emails; only `.png` is filtered

- **Where:** `enricher.py:127` (`_EMAIL_RE`) and the `.png`-only guard at `enricher.py:157`.
- **Breaks:** `[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}` accepts `logo@2x.jpg` because `2x` is a legal domain label and `jpg` a legal 2+ letter TLD. The only mitigation is `not addr.endswith(".png")`, which misses every other image extension and is case-sensitive (`logo@2x.PNG` slips through and is stored lowercased). Version-pinned asset URLs in inline JS/JSON (`react@18.2.0.min.js` → TLD `js`) match for the same reason:
  ```html
  <img src="https://cdn.acme.com/hero@2x.jpg" srcset=".../hero@2x.jpg 2x">
  <img src="https://cdn.acme.com/logo@2x.webp">
  ```
  → `contacts["email"] = "hero@2x.jpg"` (first hit). Tracking/marketing pixel query params that embed a subscriber address (`.../pixel.gif?e=user@acme.com`) also match and would be recorded as the business email.
- **Trigger:** Any site using `srcset` retina images in `.jpg`/`.webp`/`.avif` — the default for Squarespace/Wix/Webflow image output.
- **Fix:** Validate the TLD against a real-TLD set (or at minimum reject `jpg|jpeg|png|webp|gif|svg|avif|css|js|woff|woff2|ttf|ico` and any label containing a digit), and drop the fragile `endswith` check.

### S2 — Cloudflare `data-cfemail` obfuscation makes email extraction a no-op on most sites

- **Where:** `enricher.py:154-159` (the extraction path) — by design it never sees the real address.
- **Breaks:** Cloudflare's Email Protection rewrites every `mailto:` into `<a href="/cdn-cgi/l/email-protection" data-cfemail="6b1a0a">[email&#160;protected]</a>` where `data-cfemail` is the real address XOR-obfuscated (first byte is the key). The raw HTML contains no `@domain.tld`, so `_EMAIL_RE` and the mailto parser both fail. Given that a large fraction of Lebanese and Saudi SMB sites sit behind Cloudflare, this is a dominant, structural recall loss — the `email` column comes back empty for precisely the pages most likely to list a contact address.
  ```html
  <a href="/cdn-cgi/l/email-protection" class="__cf_email__" data-cfemail="b7d6d3dad">[email&#160;protected]</a>
  ```
- **Trigger:** Any Cloudflare-fronted site (the default for many Wix/WordPress/Shopify deployments).
- **Fix:** Decode `data-cfemail` when present: hex-decode the value, XOR every byte with the first byte, and emit the decoded `user@domain`. This is a ~5-line, deterministic transform (Cloudflare's encoding is public and stable).

### S2 — WhatsApp regex misses formatted/URL-encoded numbers and `send/?phone=`

- **Where:** `enricher.py:129-132`, consumed at `enricher.py:167-169`.
- **Breaks:** `(?:wa\.me/|whatsapp\.com/send\?phone=|api\.whatsapp\.com/send\?phone=)(\d{7,15})` requires 7–15 *consecutive* digits immediately after the trigger and requires the literal `send?phone=` (no slash). Real-world markup violates all three:
  ```html
  <a href="https://wa.me/961%2031%20234%2056">Chat</a>          <!-- %20 breaks the digit run -->
  <a href="https://wa.me/961-3-123456">Chat</a>                 <!-- max run = 6 digits -->
  <a href="https://whatsapp.com/send/?phone=9613123456">Chat</a> <!-- slash before ? -->
  ```
  → all three are missed. A `+961...` is handled (the `+` is outside the group), but `+961 3 123456` encoded as `%20` or `%2B` is not. The `lstrip("+")` at `enricher.py:169` is dead code: `group(1)` is `\d{7,15}`, so it can never contain `+`.
- **Trigger:** Any click-to-chat button whose number is space/dash-formatted or whose link uses the `send/?phone=` form (both common in Middle-East WhatsApp float buttons).
- **Fix:** Normalize first — decode `%XX`/`+`, strip non-digits `\D`, then run `(\d{7,15})` against the digits-only string and also accept `send/?phone=`.

### S2 — HTML truncated to 200 KB drops footer-based contact info

- **Where:** `enricher.py:150` (`html = r.text[:200_000]`).
- **Breaks:** Footers are where SMB sites concentrate their `mailto:`, `wa.me/`, and social links, and footers are the last bytes of the document. Any page whose serialized HTML exceeds 200 KB (common with inlined theme CSS/JS, Webflow, or React hydration payloads) has its contact block cut off, so email/instagram/whatsapp/linkedin all come back empty even though they are present.
  ```html
  <html>… <!-- 199,900 bytes of theme CSS/JS ... -->
  <footer><a href="mailto:info@acme.com">info@acme.com</a> …</footer>  <!-- never parsed -->
  ```
- **Trigger:** Any page larger than ~200 KB whose only contact links live in the footer.
- **Fix:** Raise the cap (or, better, extract with an HTML parser on the full body) and/or scan the tail of the document explicitly for footer contact patterns before truncating.

### S3 — Email blacklist is 8 exact domains and misses common template placeholders

- **Where:** `enricher.py:135-138`.
- **Breaks:** `_EMAIL_BLACKLIST` blocks `example.com`, `domain.com`, `yourdomain.com`, `email.com`, and four platform domains, but not the standard Wix/WordPress/theme demo placeholders that ship in starter templates:
  ```html
  <p>Email us at <a href="mailto:you@yourcompany.com">you@yourcompany.com</a></p>
  <p>Reach our DPO at <a href="mailto:dpo@yourbusiness.com">dpo@yourbusiness.com</a></p>
  <p>info@site-name.com</p>
  ```
  → `you@yourcompany.com`, `dpo@yourbusiness.com`, and `info@site-name.com` are stored as real contacts. `sentry.io` is blacklisted but `sentry.io` addresses are not typical of the pages being scraped; the list is more reactive than designed.
- **Trigger:** A site still running its builder's default "contact" section.
- **Fix:** Add `yourcompany.com`, `yourbusiness.com`, `yourwebsite.com`, `mydomain.com`, `test.com`, `weebly.com`, `wordpress.com`, and the `site-name.com` pattern; better, flag free-provider/local-part signals (`info@site-…`, `you@…`, `your@…`, `dpo@…`) rather than maintaining an unbounded domain list.

## Not a bug, but worth knowing

- **LinkedIn** (`enricher.py:133, 171-175`) is the one well-scoped regex: it only matches `linkedin.com/company/<slug>` and the slug blacklist is a no-op (the slug can never be `company`/`in`/`pub` given the required `company/` prefix). Its real limitations are recall, not precision: it misses `linkedin.com/school/…` and personal `linkedin.com/in/…`, and `.search()` keeps the *first* `/company/` link even if a later one is the canonical company page. For B2B SMB lead gen this is acceptable; note it and move on.
- Email extraction over raw HTML cannot see addresses injected by client-side rendering. Next.js/React hydration and JS-decoded contact strings return nothing to `_EMAIL_RE`. This is an architecture limit, not a regex bug, but it compounds the Cloudflare/truncation losses above.
- `verify=False` (`enricher.py:146`) and the fixed `leadminer/1.0` user agent are out of scope for this lens (covered by the HTTP-robustness/security audits), but they interact with extraction: a bot-blocking page returns challenge HTML, and all four regexes then yield empty or junk values that are indistinguishable from a genuinely contact-less site.
- `_fetch_website` returns `live=True` for any `status_code < 500` (`enricher.py:147`), so a 404/403 soft-block page is treated as live and its (empty or wrong) extracted contacts are committed. Same symptom class as the above.

## Recommended order of work

1. Fix the Instagram `@` match (`enricher.py:128`) — it is the single highest-volume wrong-result producer and pollutes the `instagram` column on nearly every site.
2. Strip `?query` and decode/validate `mailto:` targets (`enricher.py:154-159`) — protects the primary `email` field.
3. Add `data-cfemail` decoding — the cheapest, highest-recall win for the email column.
4. Normalize then match WhatsApp numbers, and accept `send/?phone=` (`enricher.py:129-132`).
5. Replace the `.png`-only guard with real-TLD validation for `_EMAIL_RE`, and raise/rework the 200 KB truncation.
6. Harden `_EMAIL_BLACKLIST` against builder placeholder domains, then add table-driven tests (one snippet per finding above) to lock the behavior in.
