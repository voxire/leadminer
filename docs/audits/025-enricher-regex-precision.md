# 025 — Contact-Extraction Regex Precision and Recall Audit

## Verdict

The contact extraction logic in `enricher.py` (`_EMAIL_RE`, `_INSTAGRAM_RE`, `_WHATSAPP_RE`, `_LINKEDIN_RE`) exhibits severe precision and recall defects when run against real small-business (SMB) websites in Lebanon and Saudi Arabia. Because regexes are executed against unparsed, raw HTML, non-contact tokens—including CSS media queries (`@media`), retina image filenames (`logo@2x.jpg`), npm asset references, font URLs, and third-party SaaS/widget scripts—are routinely extracted as business contacts. Furthermore, query parameters in `mailto:` links corrupt email strings, while ubiquitous SMB patterns such as Cloudflare email obfuscation (`data-cfemail`), space/hyphen-separated WhatsApp links, personal LinkedIn profiles, and footer contacts beyond byte 200,000 are completely missed.

## Findings

---

### S1 — Instagram Regex Matches Any `@` Word, Capturing CSS Rules, Retina Assets, and Arbitrary Handles

- **Where:** `enricher.py:128` and `enricher.py:161-165`
  ```python
  _INSTAGRAM_RE = re.compile(r"(?:instagram\.com/|@)([a-zA-Z0-9_.]{2,30})/?", re.IGNORECASE)
  ```
- **Breaks:**
  The regex uses an unanchored alternative `(?:instagram\.com/|@)`. As a result, *every single occurrence* of the `@` character in raw HTML matching `[a-zA-Z0-9_.]{2,30}` is evaluated. The loop in `enricher.py:161-165` accepts the very first match whose slug is not in `_IG_BLACKLIST` (`{"instagram", "p", "explore", "accounts", "stories", "reel", "reels", "tv"}`). On real WordPress, Wix, Squarespace, and Webflow websites, CSS `<style>` blocks and `<head>` tags appear well before any footer social links, guaranteeing false positives:
  1. **CSS at-rules:** `@media`, `@charset`, `@keyframes`, `@font-face`, `@import`, and `@supports` are captured as Instagram handles. `media` is not blacklisted.
  2. **Retina image filenames:** Filenames containing `@2x`, `@3x`, etc., in `srcset`, `src`, or JSON blobs match because the character class permits dots (`.`). A string like `logo@2x.png` yields the handle `2x.png`.
  3. **Twitter/X meta tags and blog handles:** `<meta name="twitter:site" content="@company_tw">` or customer testimonials like `@sarah_k` become the business's Instagram handle.
  4. **Email addresses:** An email in plain text or attributes (e.g., `info@domain.com`) matches `@domain.com`. Since `.` is allowed, `domain.com` becomes the Instagram handle.
- **Trigger:**
  ```html
  <!DOCTYPE html>
  <html>
  <head>
    <style>
      @media only screen and (max-width: 600px) { body { font-size: 14px; } }
    </style>
  </head>
  <body>
    <a href="https://instagram.com/beirut_bakery">Follow Us</a>
  </body>
  </html>
  ```
  `_INSTAGRAM_RE.finditer(html)` encounters `@media` first. `contacts["instagram"]` is populated with `"media"`, and the loop breaks, completely ignoring `beirut_bakery`.
- **False Negative Trigger:**
  Valid Instagram URLs using country subdomains or trailing parameters/anchors are either misparsed or dropped:
  ```html
  <!-- Language / regional subdomains or trailing query params -->
  <a href="https://lb.instagram.com/beirut_bakery?hl=en">Instagram</a>
  ```
  The capture group captures `beirut_bakery?hl=en` up to illegal characters, or misses due to subdomain variations if protocol parsing expects `instagram.com` directly.
- **Fix:**
  Remove the loose `@` alternative entirely from the regex. Require explicit Instagram domains (including optional subdomains) and anchor parsing to HTML link contexts, or validate handles strictly against CSS and known non-handle terms:
  ```python
  _INSTAGRAM_RE = re.compile(
      r"(?:https?://)?(?:[a-zA-Z0-9-]+\.)?instagram\.com/(?!p/|explore/|reels?/|stories/)([a-zA-Z0-9_.]{1,30})",
      re.IGNORECASE,
  )
  ```

---

### S1 — Mailto Query Parameters and Multi-Recipient Targets Corrupt the `email` Field

- **Where:** `enricher.py:154-159`
  ```python
  mailto_hits = re.findall(r'href=["\']mailto:([^"\'>\s]+)', html, re.IGNORECASE)
  for addr in mailto_hits + _EMAIL_RE.findall(html):
      domain = addr.split("@")[-1].lower()
      if domain not in _EMAIL_BLACKLIST and not addr.endswith(".png"):
          contacts["email"] = addr.lower()
          break
  ```
- **Breaks:**
  The regex `href=["\']mailto:([^"\'>\s]+)` captures the raw value inside the attribute until closing quotes or whitespace. Real-world SMB websites frequently configure `mailto:` links with default subject lines, CCs, or pre-filled inquiry bodies (`?subject=...&body=...`), or list comma-separated addresses.
  1. The string `sales@company.com?subject=Inquiry` is captured intact into `addr`.
  2. `domain = addr.split("@")[-1].lower()` evaluates to `company.com?subject=inquiry`.
  3. Because `company.com?subject=inquiry` does not match exact strings in `_EMAIL_BLACKLIST` (which checks for `example.com`), it passes through.
  4. The corrupted value `sales@company.com?subject=inquiry` is saved to the CSV and used in downstream lead generation. It fails RFC email validation and breaks outreach scripts.
- **Trigger:**
  ```html
  <a href="mailto:info@riyadh-clinic.sa?subject=Appointment%20Request&body=Hello">Book Now</a>
  ```
  Resulting record: `email = "info@riyadh-clinic.sa?subject=appointment%20request&body=hello"`.
- **Fix:**
  Split `addr` on `?` and `,` before validation, strip URL percent-encoding, and validate the extracted local-part and domain against a strict email regex:
  ```python
  clean_addr = addr.split("?")[0].split(",")[0].strip()
  ```

---

### S2 — Email Regex Captures Asset Filenames, Retina Images, Webpack Bundles, and Tracking Pixels

- **Where:** `enricher.py:127` and `enricher.py:157`
  ```python
  _EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
  ...
  if domain not in _EMAIL_BLACKLIST and not addr.endswith(".png"):
  ```
- **Breaks:**
  1. **Retina & responsive images:** Modern web builders (WordPress/Elementor, Wix, Squarespace, Webflow) generate responsive image tags with `@2x`, `@3x`, `@1.5x`. The pattern matches strings like `banner@2x.jpg`, `logo@2x.webp`, `icon@3x.svg`, and `photo@2x.avif`. The check `not addr.endswith(".png")` was clearly an ad-hoc patch for PNG assets, completely neglecting `.jpg`, `.jpeg`, `.webp`, `.gif`, `.svg`, and `.avif`.
  2. **NPM and Webpack asset paths:** Inline scripts and module loaders embed paths such as `core-js@3.26.1/bundle.min.js` or `swiper@8.4.5/swiper-bundle.min.css`. The regex interprets `core-js@3.26.1` + `.min.js` as an email where the TLD is `js`.
  3. **Third-party scripts and font loaders:** Typekit/Google Fonts and CDNs containing `@` (e.g., `https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js`).
  4. **Tracking pixels and analytics:** Links like `<img src="https://tracker.adroll.com/pixel?e=user@test.com">` capture subscriber parameters rather than company contact emails.
- **Trigger:**
  ```html
  <picture>
    <source srcset="/wp-content/themes/theme/assets/hero@2x.webp" type="image/webp">
    <img src="/wp-content/themes/theme/assets/hero@2x.jpg" alt="Hero">
  </picture>
  <div class="footer">Contact: info@auto-lebanon.com</div>
  ```
  The code processes `_EMAIL_RE.findall(html)`. `hero@2x.webp` or `hero@2x.jpg` is matched before the footer. Since neither ends with `.png`, `contacts["email"]` becomes `"hero@2x.webp"`.
- **Fix:**
  Reject known media/asset extensions (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg`, `.avif`, `.ico`, `.css`, `.js`, `.woff`, `.woff2`, `.ttf`, `.map`), validate the TLD against valid TLD lengths/formats, and prioritize text inside visible tags and `mailto:` anchors rather than searching script/style tags.

---

### S2 — Cloudflare Email Obfuscation Causes Universal False Negatives

- **Where:** `enricher.py:127`, `enricher.py:154-159`
- **Breaks:**
  A large percentage of commercial SMB sites in Lebanon and Saudi Arabia sit behind Cloudflare (either directly or via hosting platforms like Wix, Shopify, or regional hosting providers with Cloudflare proxy enabled). Cloudflare's Email Address Obfuscation intercepts email patterns in server responses and replaces them with:
  ```html
  <a href="/cdn-cgi/l/email-protection#127b7c747d5273767f737c3c717d7f">
    <span class="__cf_email__" data-cfemail="127b7c747d5273767f737c3c717d7f">[email&#160;protected]</span>
  </a>
  ```
  Neither `_EMAIL_RE` nor the `mailto:` regex matches `[email protected]`. The actual contact address is lost, returning `None`.
- **Trigger:**
  ```html
  <footer>
    <p>Get in touch: <a href="/cdn-cgi/l/email-protection#7e171018113e1f1a131f10501d1113" class="__cf_email__" data-cfemail="7e171018113e1f1a131f10501d1113">[email&#160;protected]</a></p>
  </footer>
  ```
  `mailto_hits` is empty. `_EMAIL_RE.findall(html)` finds nothing. Email extraction silently yields `None`.
- **Fix:**
  Add a decoder for Cloudflare `data-cfemail` attributes. The algorithm is a simple XOR cipher against the first byte key:
  ```python
  def _deobfuscate_cf_email(cf_hex: str) -> str | None:
      try:
          data = bytes.fromhex(cf_hex)
          key = data[0]
          return "".join(chr(b ^ key) for b in data[1:])
      except Exception:
          return None
  ```

---

### S2 — WhatsApp Regex Misses Formatted Numbers, URL Encoded Spaces, and Query Variations

- **Where:** `enricher.py:129-132`
  ```python
  _WHATSAPP_RE = re.compile(
      r"(?:wa\.me/|whatsapp\.com/send\?phone=|api\.whatsapp\.com/send\?phone=)(\d{7,15})",
      re.IGNORECASE,
  )
  ```
- **Breaks:**
  Small businesses in MENA heavily depend on WhatsApp floating widgets (e.g., Joinchat, WP Social Chat, Elfsight, GetButton). The regex requires 7–15 contiguous digits immediately after the domain/path. It fails to match common real-world link structures:
  1. **Formatted phone numbers:** Numbers written with country prefixes containing `+`, spaces, dashes, or parentheses (e.g., `wa.me/+9613123456`, `wa.me/961-1-234567`). The `\d{7,15}` condition fails because `-` or `+` is encountered immediately after the slash.
  2. **URL-encoded separators:** Links using `%20` or `%2B` (e.g., `whatsapp.com/send?phone=%2B966501234567`).
  3. **Alternative URL routes:** Many WordPress plugins use `whatsapp.com/send/?phone=...` (with a trailing slash before the query string) or `api.whatsapp.com/send?text=...&phone=...` (query parameters in different order).
  4. **Dead code in prefix cleanup:** `contacts["whatsapp"] = "+" + m.group(1).lstrip("+")` (`enricher.py:169`) demonstrates the author intended to handle numbers starting with `+`, but `_WHATSAPP_RE` only captures `\d`, making `lstrip("+")` dead code while rejecting all `wa.me/+...` links.
- **Trigger:**
  ```html
  <!-- Common WordPress plugin formats in Lebanon / KSA -->
  <a href="https://wa.me/+9613123456">Chat with Us</a>
  <a href="https://api.whatsapp.com/send/?phone=966501234567">WhatsApp KSA</a>
  <a href="https://wa.me/961%2070%20123456">WhatsApp Support</a>
  ```
  All three patterns are rejected by `_WHATSAPP_RE.search(html)`.
- **False Positive Risk:**
  Third-party shared chat widget scripts embedded in the page that link to generic documentation or platform support (e.g., `https://wa.me/` with vendor test numbers) can be captured if no business link exists.
- **Fix:**
  Permit optional `+`, percent-encoding, spaces, hyphens, and flexible query parameter ordering:
  ```python
  _WHATSAPP_RE = re.compile(
      r"(?:wa\.me/|api\.whatsapp\.com/send\?|whatsapp\.com/send/\?)(?:[^\"'>\s]*[?&]phone=|\+?)([\d\s+\-%]{7,25})",
      re.IGNORECASE,
  )
  ```
  Normalize by stripping `\D` to extract the canonical 7–15 digit string.

---

### S2 — LinkedIn Regex Rejects Personal Profiles and Company Sub-Pages

- **Where:** `enricher.py:133` and `enricher.py:171-175`
  ```python
  _LINKEDIN_RE = re.compile(r"linkedin\.com/company/([a-zA-Z0-9\-_.]+)/?", re.IGNORECASE)
  ...
  slug = m.group(1).strip("/").lower()
  if slug not in {"company", "in", "pub"}:
      contacts["linkedin"] = f"https://linkedin.com/company/{slug}"
  ```
- **Breaks:**
  1. **Exclusion of SMB personal founder/owner profiles:** In Lebanon and Saudi Arabia, micro-businesses, consultancies, architectural studios, clinics, and sole proprietorships rarely maintain formal LinkedIn Company pages (`linkedin.com/company/...`). Instead, their websites link directly to the founder's profile (`linkedin.com/in/founder-name`). `_LINKEDIN_RE` explicitly ignores all `linkedin.com/in/` links, dropping valuable B2B outreach targets.
  2. **Dead blacklist logic:** The check `if slug not in {"company", "in", "pub"}` is largely dead code because `_LINKEDIN_RE` requires `company/` immediately before the slug.
  3. **Third-party widget / share buttons (False Positive):** Many blog themes include standard LinkedIn share buttons:
     `<a href="https://www.linkedin.com/sharing/share-offsite/?url=...">` or follow widgets pointing to the theme developer or platform company page.
- **Trigger:**
  ```html
  <div class="team-social">
    <a href="https://www.linkedin.com/in/dr-farid-khoury-5912a">LinkedIn Profile</a>
  </div>
  ```
  `_LINKEDIN_RE.search(html)` yields `None`, leaving `contacts["linkedin"] = None`.
- **Fix:**
  Capture both `/company/` and `/in/` profile paths while excluding share URLs (`sharing`, `share-offsite`):
  ```python
  _LINKEDIN_RE = re.compile(
      r"linkedin\.com/(company|in)/([a-zA-Z0-9\-_.]+)/?",
      re.IGNORECASE,
  )
  ```

---

### S3 — Boilerplate, Privacy Disclaimers, and Third-Party Widgets Cause Contact Attribution Errors

- **Where:** `enricher.py:135-138` and `enricher.py:155`
  ```python
  _EMAIL_BLACKLIST = {
      "example.com", "domain.com", "yourdomain.com", "email.com",
      "sentry.io", "wixpress.com", "squarespace.com", "shopify.com",
  }
  ```
- **Breaks:**
  Raw regex matching across the entire HTML string scans footer copyright disclaimers, cookie banners, privacy policies, template boilerplate, and third-party SaaS widget embeds:
  1. **Template & Theme boilerplate:** Unconfigured WordPress/Shopify templates contain placeholder emails like `info@yourbusiness.com`, `support@company.com`, `admin@sitename.com`.
  2. **Platform & CMS vendors:** Vendor support links in footers like `support@wordpress.org`, `contact@woocommerce.com`, or web agency credits like `designed-by@beirutagency.com`.
  3. **Privacy / GDPR / Cookie banners:** Privacy notices often cite third-party data protection officers (e.g., `dpo@cookiebot.com`, `privacy@termly.io`, `gdpr@google.com`).
  4. **Chat widgets & customer support:** Scripts for Zendesk, Tawk.to, Crisp, or Intercom often contain internal support or report addresses in inline JSON configuration.
  Because `mailto_hits + _EMAIL_RE.findall(html)` takes the first hit that is not in `_EMAIL_BLACKLIST`, any boilerplate or agency email positioned earlier in the document than the company's own contact details gets incorrectly attributed to the business.
- **Trigger:**
  ```html
  <head>
    <!-- Privacy Policy banner script -->
    <script>
      var config = { privacyContact: "compliance@cookie-consent-provider.com" };
    </script>
  </head>
  <body>
    ...
    <footer>Contact: info@riyadh-bakery.com</footer>
  </body>
  ```
  `_EMAIL_RE` matches `compliance@cookie-consent-provider.com` before reaching the footer. `info@riyadh-bakery.com` is lost.
- **Fix:**
  Filter script/style/svg tags prior to regex evaluation using an HTML parser (such as BeautifulSoup or lxml, which are already pinned in `requirements.txt`), expand the blacklist to include privacy/vendor domains, and prioritize `mailto:` links found inside `<footer>` or `<header>` elements.

---

### S3 — Hard 200 KB HTML Truncation Silently Discards Footer Contacts

- **Where:** `enricher.py:150`
  ```python
  html = r.text[:200_000]
  ```
- **Breaks:**
  Modern CMS platforms (WordPress with page builders like Elementor/Divi, Shopify, Wix) routinely emit HTML documents between 250 KB and 1.5 MB due to inlined SVG sprites, heavy JSON state trees (`__NEXT_DATA__`, `window.__INITIAL_STATE__`), and inlined CSS. Footers and contact blocks reside at the very bottom of the DOM. Truncating at character index 200,000 cuts off the contact section entirely, causing false negatives across all four channels.
- **Trigger:**
  A typical WordPress + Elementor restaurant site in Beirut with 180 KB of `<head>` styles, inline SVG icons, and hero sections. The contact details sit at character 245,000:
  ```html
  <!-- bytes 0 to 200,000: inline CSS, Elementor JSON, SVG icons -->
  ... [truncated] ...
  <footer>
    <a href="mailto:contact@beirut-bistro.com">contact@beirut-bistro.com</a>
    <a href="https://wa.me/9613000000">WhatsApp</a>
  </footer>
  ```
  `html = r.text[:200_000]` cuts before the footer. `contacts` returns all `None`.
- **Fix:**
  Strip `<style>`, `<script>`, `<svg>`, and base64 image data before applying character limits, or parse the DOM directly with `lxml`/`BeautifulSoup` over the complete response body.

---

## Not a bug, but worth knowing

1. **Client-Side Rendered (CSR) Single Page Applications:**
   Sites built with React, Vue, or Angular without SSR return minimal HTML shells (e.g., `<div id="root"></div>`). Regex extraction against `r.text` will yield zero contacts regardless of regex precision. Resolving this requires headless rendering (Playwright/Puppeteer), which is heavy, or querying linked social metadata (`og:see_also`, schema.org JSON-LD).
2. **Missing Facebook Extraction in Enricher:**
   `completeness_score` (`enricher.py:111`) scores `record.get("facebook") or record.get("instagram")`, but `_fetch_website()` does not extract Facebook URLs at all, only checking Instagram, WhatsApp, and LinkedIn.
3. **HTML Entity Encoding:**
   `mailto:` addresses in HTML attributes are sometimes entity-encoded by CMS plugins (e.g., `&#109;&#97;&#105;&#108;&#116;&#111;&#58;`). Unescaping HTML entities (`html.unescape(html)`) prior to regex extraction is necessary to prevent false negatives.

---

## Recommended order of work

1. **Fix the Instagram regex immediately (`enricher.py:128`):** Eliminate the bare `@` alternative to stop polluting the database with CSS keywords (`media`, `charset`) and image filenames.
2. **Clean Mailto parsing (`enricher.py:154-159`):** Split on `?` and `,` to prevent query string pollution in email fields.
3. **Add Cloudflare `data-cfemail` deobfuscation (`enricher.py:154`):** Recover emails from Cloudflare-protected sites, which constitute a large fraction of the Lebanese and Saudi SMB target web.
4. **Harden WhatsApp regex and normalization (`enricher.py:129-132`):** Handle leading `+`, URL-encoded spaces (`%20`), and alternate query forms (`send/?phone=`).
5. **Add extension exclusions to `_EMAIL_RE` (`enricher.py:127, 157`):** Ban common media extensions (`.jpg`, `.webp`, `.svg`, `.gif`, `.js`, `.css`) beyond just `.png`.
6. **Support LinkedIn personal profiles (`enricher.py:133`):** Add `linkedin.com/in/` support for founder-led SMBs.
7. **DOM stripping before truncation (`enricher.py:150`):** Remove `<style>`, `<script>`, and `<svg>` nodes before slicing text to ensure footer contact blocks are preserved.
