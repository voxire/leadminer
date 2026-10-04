# 377 — `_fetch_website` edge/hostile-input audit (`enricher.py:161`)

## Verdict

**No input crashes this function.** I drove 34 boundary/hostile `url` values and 19 hostile
*responses* through the real `enricher._fetch_website` offline (stubbed transport, no
network) and it returned a valid `(outcome, contacts)` tuple for **100 %** of them. The
two `except Exception` arms at `enricher.py:176` and `enricher.py:190`, plus the second net
at `enricher.py:244`, make it structurally crash-proof, and the regexes at
`enricher.py:199-220` sit outside every `try` yet cannot raise (no nested quantifiers, so
no ReDoS; `html` is hard-capped at `httpclient.py:54` `DEFAULT_MAX_BODY_BYTES = 200_000`).
That part is genuinely fine.

The danger is entirely in **silent misclassification**, and it is one-sided: `enricher.py:182`
(`if status >= 400: return DEAD`) collapses *six distinct HTTP refusals that mean "we were
blocked"* into the same verdict as *"this site is broken"*. This is the exact inversion the
function's own docstring forbids (`enricher.py:164-166`), and it is not a hypothetical — I
measured the downstream effect with the real `lead_score` and `recommend_service`:
a healthy, Cloudflare-protected dental clinic scored through this function lands at
**43 points / "Website rebuild + maintenance"**, while the *same clinic with no website at
all* lands at **23 points / "Lead-gen website + Google Ads launch"**. A blocked site
outscores a website-less business by 20 points and gets pitched a rebuild. The `043` fix
split *transport* failures out into `UNKNOWN` and left *HTTP-level* refusals in `DEAD`.

## Method / reproducibility

`requests` is not installed in this environment (`requirements.txt` pins
`requests==2.32.3`). So:

- I injected a minimal `requests`/`urllib3` stub into `sys.modules`, imported the **real**
  `enricher.py` / `pitch_recommender.py` / `main.py`, and called `_fetch_website` with a
  controllable status code, `r.encoding`, and `r.raw.read()`. All quoted return values and
  scores are real program output.
- For the `url` and `Content-Type` boundaries — where the behaviour lives inside
  `requests` — I re-implemented only the *validation* from the authoritative source at
  tag `v2.32.3` (`requirements.txt:1`): `PreparedRequest.prepare_url` and
  `_get_idna_encoded_host` (`src/requests/models.py`), `utils._parse_content_type_header`
  and `utils.get_encoding_from_headers` (`src/requests/utils.py`),
  `HTTPAdapter.build_response` (`src/requests/adapters.py`), and
  `SessionRedirectMixin.resolve_redirects` (`src/requests/sessions.py`). Sources:
  `https://raw.githubusercontent.com/psf/requests/v2.32.3/src/requests/{models,utils,sessions,adapters}.py`
  (source tree is `src/requests/` at that tag, not `requests/`).
- `urllib3` is **unpinned** (`requirements.txt` has 3 lines, none of them urllib3), so I
  checked `urllib3.util.url.parse_url` at both `1.26.20` and `2.5.0`. Both raise
  `LocationParseError` (→ `requests` `InvalidURL`) for a non-integer or out-of-range port,
  so every conclusion below holds on either.
- Python 3.14.8, `idna` 3.13. Zero network calls.

## Boundary-input enumeration — `url` argument

Classification: **CRASH** = traceback escapes · **WRONG** = returns a confidently incorrect
value · **OK** = correct outcome. Note that every row below produces *some* value; nothing
raises.

| # | `url` input | `requests` 2.32.3 does | `_fetch_website` returns | Class |
|---|---|---|---|---|
| 1 | `None` | `str(None)`→`"None"`, `MissingSchema` | `(UNKNOWN, {})` | OK |
| 2 | `""` | `MissingSchema` | `(UNKNOWN, {})` | OK |
| 3 | `"   "` (whitespace) | `lstrip()`→`""`, `MissingSchema` | `(UNKNOWN, {})` | OK for the fetch; **WRONG upstream** (see S3) |
| 4 | `0` / `0.0` / `False` | falsy → filtered at `enricher.py:231` | never called | OK |
| 5 | `42` / `3.14` / `True` | `str()`→`"42"`, `MissingSchema` | `(UNKNOWN, {})` | OK |
| 6 | `{"a": 1}` / `["x"]` | `":"in url and not startswith("http")` short-circuit → `Session.get_adapter` → `InvalidSchema` | `(UNKNOWN, {})` | OK |
| 7 | `b"\xff\xfe"` (invalid UTF-8) | `url.decode("utf8")` → `UnicodeDecodeError` **not** a `RequestException` | `(UNKNOWN, {})` via `enricher.py:176` | OK (caught by luck of `except Exception`) |
| 8 | `b"https://x.com"` | `isinstance(url, bytes)` branch | fetched normally | OK |
| 9 | `"  https://x.com  "` | only `lstrip()`; **trailing spaces survive** → `https://x.com%20%20/` | typically **404 → DEAD** | **WRONG** (S2) |
| 10 | `"https://x.com/a\tb"` | tab percent-encoded → `https://x.com/ab` | fetched | OK |
| 11 | `"HTTPS://EXAMPLE.COM"` | `parse_url` lowercases scheme+host → `https://example.com/` | fetched | OK — **but see S2**: `osm.py:95` breaks this *before* we get here |
| 12 | `"//example.com"` (protocol-relative) | after upstream prefix → `https:////example.com`, `InvalidURL: No host supplied` | `(UNKNOWN, {})` | **WRONG** — a legal, common OSM tag silently lost |
| 13 | bare `"example.com"` | needs the upstream `https://` prefix; given one → fetched | fetched | OK |
| 14 | `"https://"` | `InvalidURL: No host supplied` | `(UNKNOWN, {})` | OK |
| 15 | `"mailto:info@x.com"` | after upstream prefix → `https://mailto:info@x.com`; auth=`mailto:info`, host=**`x.com`** | **we GET an unrelated host** and record its contacts | **WRONG** (S2) |
| 16 | `"ftp://files.example.com"` | after prefix → `https://ftp//files.example.com`, host=`ftp` → DNS fail | `(UNKNOWN, {})` | OK-ish (lost, but the tag was junk) |
| 17 | Arabic IDN host `https://مقهي.لبنان/` | `idna.encode(uts46=True)` → `xn--ehbfhi.xn--mgbb7fjb` | fetched, contacts extracted | **OK** — IDN is genuinely supported |
| 18 | Arabic path `https://x.com/مقهى` | `requote_uri` → `%D9%85%D9%82%D9%87%D9%89` | fetched, contacts extracted | **OK** |
| 19 | French IDN host `https://café.fr/` | → `xn--caf-dma.fr` | fetched | **OK** |
| 20 | Emoji in path `https://x.com/🏪` | → `%F0%9F%8F%AA` | fetched | **OK** |
| 21 | Emoji in host `https://🏪.com/` | `idna.IDNAError` → `InvalidURL("URL has an invalid label.")` | `(UNKNOWN, {})` | OK |
| 22 | 70-char host label | `idna` `Label too long` → `InvalidURL` | `(UNKNOWN, {})` | OK |
| 23 | `https://.x.com/` (leading dot) | `host.startswith(("*", "."))` → `InvalidURL` | `(UNKNOWN, {})` | OK |
| 24 | `http://example.com:abc/`, `http://example.com:70000/` | urllib3 `parse_url` `LocationParseError` → `InvalidURL` | `(UNKNOWN, {})` | OK |
| 25 | 20 000-char host `https://aaaa…` | sent; DNS/connect fails | `(UNKNOWN, {})` | OK |
| 26 | 5 000-char legal URL (`?i=1&…`) | **sent**; most servers answer **414** | **DEAD** | **WRONG** (S2) |
| 27 | `http://169.254.169.254/latest/meta-data/` | **sent, verbatim** | whatever IMDS returns | **WRONG** (S1, see 026) |
| 28 | `http://127.0.0.1:6379/`, `http://[::ffff:169.254.169.254]/` | **sent, verbatim** | live port-scan oracle | **WRONG** (S1, see 026) |

Rows 1–8, 13, 14, 16–25 are all handled. **Every one of them is handled by `requests`
raising, not by a policy in this function** — which is exactly why the docstring at
`enricher.py:164` deserves a defence rather than an assumption.

## Boundary-input enumeration — the *response*

These are the ones that actually matter, because `url` is a low-degree-of-freedom string
and the response is entirely attacker/host controlled. Real `enricher._fetch_website`
output, offline:

| Response | Returns | Should be | Class |
|---|---|---|---|
| 200 + normal page | `(LIVE, {email, ig, wa})` | LIVE | OK |
| **200 + `Content-Type: text/html; charset=iso-8859-1,utf-8`** | `(UNKNOWN, {})` | **LIVE** | **WRONG** (S2) |
| **200 + `Content-Type: text/html; charset`** (no `=`) | `(UNKNOWN, {})` — `AttributeError` inside `build_response` | **LIVE** | **WRONG** (S2) |
| 200 + `charset=utf-16` (valid codec, wrong label) | `(LIVE, {})` — every contact missed, body is mojibake | LIVE + contacts | **WRONG** (S3) |
| 200 + `charset=x-unknown-codec` | `(UNKNOWN, {})` | LIVE | **WRONG** (S2) |
| 200 + body read raises (`ProtocolError`, truncated chunk) | `(UNKNOWN, {})` | LIVE | **WRONG** (S2, see 084) |
| 200 + empty body | `(LIVE, {})` | LIVE | OK |
| 200 + 5 MB body | `(LIVE, …)` from first 200 000 bytes only | LIVE | OK (cap real; contacts past 200 KB lost — known) |
| **403 Cloudflare challenge** | **`(DEAD, {})`** | **UNKNOWN** | **WRONG — most dangerous (S1)** |
| 401 basic-auth wall | `(DEAD, {})` | UNKNOWN | **WRONG (S1)** |
| 429 rate-limited | `(DEAD, {})` | UNKNOWN | **WRONG (S1)** |
| 407 proxy-auth | `(DEAD, {})` | UNKNOWN | **WRONG (S1)** |
| 451 geo-blocked | `(DEAD, {})` | UNKNOWN | **WRONG (S1)** |
| 404 / 410 / 500 / 501 | `(DEAD, {})` | DEAD | OK |
| 414 URI Too Long | `(DEAD, {})` | UNKNOWN (our URL is the problem) | **WRONG (S2)** |
| **200 + parked-domain page** (`Sedo`/registrar "this domain is for sale") | **`(LIVE, {})`** | UNKNOWN/parked | **WRONG (S2)** |
| **200 + soft-404** (`Page not found`) | **`(LIVE, {})`** | UNKNOWN | **WRONG (S2)** |
| 302 → registrar parking page (followed) | `(LIVE, {})` | UNKNOWN | **WRONG (S2)** |

## Findings

### S1 — `status >= 400` classifies six HTTP *refusals* as "dead site", inverting the lead signal the docstring forbids

- **Where:** `enricher.py:182-184`. Consumers: `enricher.py:250`
  (`website_live = False`), `enricher.py:303-304` (`score += 20`),
  `pitch_recommender.py:36,57` (`"Website rebuild + maintenance"`).
- **Contract being violated:** `enricher.py:164-166` — *"Never infer 'dead' from a
  transport failure: a site that blocks our crawler is not a site that needs rebuilding,
  and scoring it as one inverts the core lead signal."* The `043` remediation extracted
  transport failures into `UNKNOWN` but left the HTTP-status branch untouched, so the
  other half of the same hazard — an HTTP-level refusal — still lands in `DEAD`.
- **Breaks:** `401`, `402`, `403`, `407`, `429` and `451` all mean *"we were refused"*,
  not *"they are broken"*. They are written to the CSV as `website_live=False`,
  indistinguishable in the export from a real `404`, and they are the *only* signal that
  earns the +20 rebuild bonus (`enricher.py:304`) and the Tier-1 pitch.
- **Trigger (not hostile — this is a correct URL to a healthy site):**
  `url = "https://<any Cloudflare / Imperva / Wordfence / AWS-WAF protected site>"`.
  Cloudflare's managed challenge answers **HTTP 403** with `cf-mitigated: challenge` for a
  non-browser UA. The workflow runs from GitHub-hosted runners (`scrape.yml`), i.e. Azure
  datacenter IP space, which Cloudflare challenges by default; `httpclient._user_agent()`
  identifies the scraper explicitly (`leadminer/0.2 (+https://github.com/voxire/leadminer)`),
  which most managed-challenge configs treat as automation. `429` is self-inflicted: 40
  concurrent workers (`enricher.py:237`) hitting one host with no per-host politeness.
  `451` is real geo-blocking, increasingly common for LB/SA businesses.
- **Measured downstream effect** (real `lead_score` + `recommend_service`, `category="dentist"`, `review_count=0`, `industry_priority="medium"`):

  | record | `website_live` | `lead_score` | `recommended_service` |
  |---|---|---|---|
  | healthy site, answers **403** to our IP | `False` | **43** | **`Website rebuild + maintenance`** |
  | same site, answers 200 | `True` | 33 | `SEO audit + visibility upgrade` |
  | genuinely broken (404) | `False` | 43 | `Website rebuild + maintenance` |
  | **no website at all** | `None` | **23** | **`Lead-gen website + Google Ads launch`** |

  A blocked site scores **20 points higher than a business that has no website**, and the
  pitch is a rebuild for a site that works. That is precisely the inversion the docstring
  was written to prevent, and it is the whole product: the CSVs are what sales works from.
- **Fix:** make the status branch an explicit allow-list instead of a threshold, and
  record the reason:
  ```python
  if status in (404, 410, 501):
      return DEAD, contacts
  if status in (401, 402, 403, 407, 429, 451) or \
     str(r.headers.get("cf-mitigated", "")).lower() == "challenge":
      return UNKNOWN, contacts   # we were refused; the site learned nothing
  ```
  Ideally carry a `reason` alongside the outcome (see S3) so `072:163-167` can tell
  "IP-blocked" from "404 cluster" without guessing.
- **Cross-refs:** `043-scoring-upgrade.md` §"Crawler-Block Vulnerability" and
  `072-run-failure-triage.md:163-167` identify the *symptom* (a `dead` spike with a
  collapsed `live` is "we got IP-blocked"). Neither notes that the code path they blame
  (`enricher.py:151-153` in the old numbering) no longer exists — the fix landed on the
  transport arm only. This report pins the residual set at six status codes.

### S2 — `r.encoding` is an unvalidated, host-controlled string fed straight into `bytes.decode()` inside a catch-all — a 200 OK site is recorded as unreachable

- **Where:** `enricher.py:189` (`html = raw.decode(r.encoding or "utf-8", errors="replace")`),
  swallowed by `enricher.py:190-192`.
- **Breaks:** `requests` 2.32.3 `utils.get_encoding_from_headers` returns
  `params["charset"].strip("'\"")` **with no `codecs.lookup` validation**, and
  `HTTPAdapter.build_response` assigns it to `response.encoding` *inside* `adapter.send`,
  i.e. inside the `get()` at `enricher.py:175`. `bytes.decode` then raises `LookupError`
  for an unknown codec. `LookupError ⊂ Exception`, so `enricher.py:190` maps it to
  `UNKNOWN` — for a response whose `status_code` was **200**.
- **Trigger (two, both verified end-to-end):**
  1. `Content-Type: text/html; charset=iso-8859-1,utf-8` → `r.encoding ==
     "iso-8859-1,utf-8"` → `LookupError` at `enricher.py:189` → `(UNKNOWN, {})`.
     A doubled `charset` is a legal-shaped SMTP-style header and appears in the wild.
  2. `Content-Type: text/html; charset` (parameter with no `=`) →
     `_parse_content_type_header` stores `params_dict["charset"] = True`, then
     `params["charset"].strip("'\"")` raises
     `AttributeError: 'bool' object has no attribute 'strip'` **inside `build_response`**,
     which propagates out of `get()` and is caught by `enricher.py:176` → `(UNKNOWN, {})`.
- **Why it is worse than a lost contact:** `UNKNOWN` is the bucket the runbook reads as a
  connectivity/IP-block symptom (`072:159-163`: *"`unknown` spikes while `live + dead`
  collapses → transport failures … this is a connectivity / IP-block at the TCP layer
  problem"*). A misconfigured CMS header therefore manufactures a fake "D5 enrichment
  failed" signal and simultaneously hides genuinely dead sites. Both buckets are
  irrecoverably conflated in the output.
- **Fix (one line, before the `decode`):**
  ```python
  enc = r.encoding
  try:
      codecs.lookup(enc or "utf-8")
  except (LookupError, TypeError, AttributeError):
      enc = "utf-8"
  html = raw.decode(enc, errors="replace")
  ```
  Better still: `enc = r.apparent_encoding` when `r.encoding` is absent or unrecognised,
  and keep `status`-based LIVE regardless of decode success — a 200 is a 200. Move the
  `return LIVE, contacts` for the 2xx case *outside* the decode `try`.
- **Trap in the existing remediation:** `065-python-312-modernisation.md:511-517` proposes
  narrowing this handler to `except (requests.RequestException, OSError)`. That would be a
  **regression**. `r.raw` is urllib3's `HTTPResponse`, not a `requests` object, so it
  raises `urllib3.exceptions.ProtocolError` / `DecodeError` / `SSLError` — subclasses of
  `urllib3.exceptions.HTTPError`, which is neither a `requests.RequestException` nor an
  `OSError` (`ReadTimeoutError` *is* an `OSError` in urllib3 2.x; the others are not).
  Those would escape `_fetch_website`, be caught only by the blanket net at
  `enricher.py:244`, and lose all logging. Keep `except Exception` here and `log.warning`
  the exception type.

### S2 — Malformed, padded, oversized or mis-schemed `website` values silently manufacture DEAD verdicts, or fetch a host nobody intended

- **Where:** `enricher.py:175` (no validation), `enricher.py:182`; contributing upstream
  `osm.py:95-96`, `wikidata.py:103-104`, `google_places.py:291`.
- **Breaks:** four distinct silent-wrong paths, all verified:
  1. **Trailing whitespace → 404 → DEAD.** `requests` 2.32.3 `prepare_url` does only
     `url.lstrip()`; there is no `rstrip()`. `"https://example.com "` is sent as
     `https://example.com%20%20/`. Most servers 404 that path → `status >= 400` → `DEAD`
     → +20 points and a rebuild pitch for a perfectly healthy site. Trailing spaces in
     OSM `website` tags are common (the OSM wiki asks editors to trim; many do not).
  2. **Oversized URL → 414 → DEAD.** A 5 000-char URL with a tracking-heavy query string
     is sent verbatim; `414 URI Too Long` is `>= 400`, so a limit on *our* request becomes
     evidence that *their* site is broken.
  3. **`mailto:` in a `website` tag → we GET an unrelated host.** `osm.py:95` prefixes
     `https://` because the value does not start with `http`, producing
     `https://mailto:info@x.com`, which `parse_url` reads as userinfo `mailto:info` and
     **host `x.com`**. We then scrape `x.com`'s homepage and write its email/Instagram/
     LinkedIn into a business record. Verified:
     `prepare_url("https://mailto:info@x.com")` → `https://x.com/`.
  4. **Case-sensitive `startswith("http")`.** `"HTTPS://EXAMPLE.COM"` →
     `"https://HTTPS://EXAMPLE.COM"` → host `https` → DNS failure → `UNKNOWN`;
     `"//example.com"` → `"https:////example.com"` → `InvalidURL: No host supplied` →
     `UNKNOWN`. Both are legal, valid, fetchable URLs destroyed by the normalizer. This
     is primarily a scraper-layer bug (`018`/`019` lens), but the loss lands here as
     `unknown`.
- **Compounding:** `main.py:179-180` merges the loaded master CSV into the working set and
  `main.py:187` calls `enrich()` on the **union** every run, so every junk `website` value
  ever collected costs a GET on every future run, forever.
- **Fix:** validate and normalise in `_fetch_website` before line 175 — that is the last
  line of defence and it currently has none:
  ```python
  if not isinstance(url, str) or not url.strip():
      return UNKNOWN, contacts            # input garbage, not a network fact
  url = url.strip()
  p = urlparse(url if "://" in url else "https://" + url)
  if p.scheme not in ("http", "https") or not p.hostname or "." not in p.hostname:
      return UNKNOWN, contacts
  ```
  Separately, make `osm.py:95` / `wikidata.py:103` case-insensitive and strip.

### S2 — 200 on a parked or soft-404 domain is recorded as LIVE, deleting the highest-value pitch for that record

- **Where:** `enricher.py:181-222` (only `>= 400` is non-live); consumer
  `pitch_recommender.py:70-71`, `main.py:199-205`.
- **Breaks:** parked domains answer **200**. An expired/parked `.com.lb`/`.lb` — the
  single most common web-presence failure mode in Lebanon, where the ccTLD ecosystem is
  young and domains lapse — is indistinguishable from a real business site. The record
  then goes to `with_websites.csv`, `website_live=True`, +10 points, and
  `pitch_recommender.py:70` returns `"SEO audit + visibility upgrade"`. The pitch that
  would actually win the deal is `"E-commerce launch + Instagram-to-store funnel"`
  (`pitch_recommender.py:62-63`) or `"Lead-gen website + Google Ads launch"`
  (`:84-85`) — both gated on `not has_website`, which is now false. Measured on a
  `jewelry` record: parked-domain → `"SEO audit + visibility upgrade"`; the same shop with
  no website and a real Instagram → `"E-commerce launch + Instagram-to-store funnel"`.
  A soft-404 (`200` + "Page not found") has the same effect.
- **Note:** this is the *opposite* failure direction from S1 and is not covered by `043`'s
  remediation framing, which only worried about dead/blocked.
- **Fix:** cheap and effective — in the body, detect parking/soft-404 signatures and
  return a distinct outcome, e.g. `if len(html) < 3_000 and re.search(r"(for sale|sedo|parking|dan\.com|afternic|buy this domain)", html, re.I): return UNKNOWN` (or a new `PARKED` outcome). Also require the final `r.url` host to be a subdomain of the requested host, or at least record it.

### S2 — Body-derived contacts: the `@` alternative, cross-field bleed, digit-run truncation, and blacklist bypass all write confident garbage into the sales CSVs

- **Where:** `enricher.py:131-135` (regexes), `enricher.py:199-220` (use),
  `enricher.py:138-142` (blacklists). Impacted by `main.py:142-144` and `main.py:202-205`.
- **Breaks / measured:**
  - `_INSTAGRAM_RE`'s bare `@` alternative captures the *first* `@`-prefixed token in the
    document, which in any page with an inline `<style>` is a CSS at-rule. Exact captured
    values, all run through the real code: `@charset`→`"charset"`, `@media`→`"media"`,
    `@import`→`"import"`, `@supports`→`"supports"`, `@keyframes`→`"keyframes"`,
    `@font-face`→`"font"` (the class stops at `-`). **None of those six words is in
    `_IG_BLACKLIST`** (`enricher.py:142`). On a realistic page whose footer carries the
    genuine `instagram.com/beirut_bakery` link, the loop `break`s at `enricher.py:210`
    and the record gets `instagram = "charset"`. Every one of those junk values is
    `len(...) > 3`, so `main.py:144` `has_any_contact` returns `True` and the row lands in
    `sales_ready.csv` claiming a contact channel it does not have.
  - **Cross-field bleed (new).** `sales@domain.com` written in plain body text, no
    `mailto:`. `_EMAIL_BLACKLIST` contains `"domain.com"`, so the email is correctly
    rejected at `enricher.py:202` — but `_INSTAGRAM_RE` independently matches
    `@domain.com` and writes **`instagram = "domain.com"`**. The blacklist protects one
    column and hands the value to another.
  - **Digit-run truncation (new).** `_WHATSAPP_RE` is `(\d{7,15})` — bounded, not
    anchored. `https://wa.me/1234567890123456789012345678900` yields
    `contacts["whatsapp"] = "+123456789012345"`: a 15-digit number that is not a real
    number, written to the record, worth +15 points (`enricher.py:290-291`) and
    `has_any_contact` → `sales_ready.csv`. A truncated number is worse than a missing one.
  - **Blacklist bypass.** `enricher.py:202` is an exact-domain set membership test, so
    `a@www.example.com`, `a@mail.sentry.io` and `a@sentry.io.attacker.tld` all pass.
    `mailto:` captures also swallow the query string: `enricher.py:199` has no `?`/`&`
    exclusion, so `href="mailto:info@riyadh-clinic.sa?subject=Hi&body=Yo"` is stored as
    `info@riyadh-clinic.sa?subject=hi&body=yo` — lowercased whole at `enricher.py:203`.
- **Fix:** delete the bare `@` alternative from `_INSTAGRAM_RE` (require an
  `instagram.com/` domain, as `025` already proposes); match the blacklist against the
  registrable domain with `==` **or** `.endswith("." + blocked)`; strip `?...` and `,...`
  from `mailto:` captures before validating with `_EMAIL_RE.fullmatch`; change
  `_WHATSAPP_RE` to `(\d{7,15})(?!\d)` so a longer run is *rejected*, not truncated.
- **Cross-ref:** the `@` class of bug is already S1 in `006` and `025`. This report
  contributes the six exact captured strings, the blacklist→Instagram bleed, the WhatsApp
  truncation, and the `sales_ready.csv` impact chain, none of which those audits cover.

### S3 — No URL validation at the boundary; `unknown` conflates "our IP is blocked" with "the input was garbage"

- **Where:** `enricher.py:161` (`url: str` is unvalidated), `enricher.py:231`
  (`if r.get("website")` is the only filter), `enricher.py:168` (fixed `contacts` keys,
  re-listed by hand at `enricher.py:247-248`).
- **Breaks:** the type contract is unenforced and the function cannot distinguish its two
  very different failure families. Rows 1–8 of my table are *caller bugs*; rows 9, 12, 15,
  26 are *data* bugs; the transport arm (`enricher.py:176`) is an *environment* problem.
  All three land in the same `UNKNOWN` bucket, so `072:159-163` reads every one of them
  as "TCP-level IP block". Two cheap fixes with real diagnostic value: coerce/validate
  `url` at the top of the function, and return a `reason` string with the outcome.
- **Also:** `check_websites:231` submits **one task per record**, not per unique URL
  (`enricher.py:238`). A chain with 40 branches produces 40 identical GETs to one host,
  which both wastes the 40-worker budget and *causes* the `429 → DEAD` self-inflicted case
  in S1. Fix: dedupe `targets` on the normalised URL, fetch once, fan the result out.
- **Fix:** `urls = {url for ...}`, `future -> [idx, ...]`.

### S3 — `r.raw.read()` bypasses `requests` exception translation and `iter_content`'s incremental decode; redirect-hop bodies are uncapped

- **Where:** `enricher.py:188`, `enricher.py:195`.
- **Breaks:** two residuals of the `026` decompression-bomb fix, which was otherwise
  correctly implemented (the 200 000 cap does apply to *decompressed* bytes):
  1. `r.raw` is urllib3's object, so its exceptions are raw
     `urllib3.exceptions.*`, not `requests.*`. Currently harmless (caught by the
     `except Exception` at `:190`) but it is why the `065` narrowing above is a trap.
  2. **`resolve_redirects` consumes redirect bodies with `resp.content`** — fully, with no
     cap — before honouring `Location`
     (`https://raw.githubusercontent.com/psf/requests/v2.32.3/src/requests/sessions.py`:
     `try: resp.content  # Consume socket so it can be released`), and its fallback branch
     is `resp.raw.read(decode_content=False)` with no `amt`. So
     `http://host/` → `302` → a 2 GB chunked body is read into memory in full, entirely
     outside the `_MAX_BODY_BYTES` budget. One such URL among 40 concurrent workers can
     OOM a 7 GB runner.
  3. `prepared_request.url = to_native_string(url)` in `resolve_redirects` sets the
     redirect target **without** calling `prepare_url`. So **no** validation — scheme, host,
     IDN, port — is applied to any redirect hop. This is why the SSRF surface in
     `026`/`008` cannot be closed by validating `url` at `enricher.py:161` alone.
- **Fix:** `allow_redirects=False` and follow up to N hops manually, validating each hop;
  or use `iter_content(chunk_size=8192)` accumulating to `_MAX_BODY_BYTES` with an early
  `r.close()`.

## Not a bug, but worth knowing

- **SSRF is still fully open after the TLS/cap fix.** `026`/`008` are not stale: rows
  27–28 of my table are sent verbatim, and redirect hops are unvalidated. The
  `verify=True` change (`enricher.py:170-174`) and the streaming cap (`enricher.py:186-188`)
  *did* correctly resolve 026's other two S1s — worth crediting explicitly. The remaining
  fix needs `ipaddress.ip_address(peer).is_global` on the connected socket, re-checked per
  hop, plus a scheme/port allow-list. Do not implement S2's URL validator as the SSRF fix.
- **IDN and Arabic are handled correctly.** `requests` 2.32.3 calls
  `idna.encode(host, uts46=True)`, so `https://مقهي.لبنان/` → `xn--ehbfhi.xn--mgbb7fjb` and
  `https://café.fr/` → `xn--caf-dma.fr` both fetch. Emoji in the *path* is
  percent-encoded; emoji in the *host* is a clean `InvalidURL`. `wikidata.py` P856 values
  and Arabic OSM tags do not need special handling here.
- **Latin-1 mojibake on Arabic pages is harmless.** `get_encoding_from_headers` defaults
  `text/*` with no charset to `ISO-8859-1`, so UTF-8 Arabic renders as `Ù…Ù‚Ù‡Ù‰`.
  All four contact regexes are ASCII-only classes, and ASCII bytes survive latin-1
  round-trip identically, so contacts survive. Only `charset=utf-16` (verified) destroys
  them, and that is rare enough to leave as S3.
- **`enricher.py:214` `.lstrip("+")` is dead code** — `m.group(1)` is `\d{7,15}` and cannot
  contain `+`. Harmless; delete it.
- **The regexes cannot raise.** Lines 199–220 sit outside every `try`, but no pattern has a
  nested quantifier or an ambiguous alternation, so worst case is linear scanning. There
  is no ReDoS exposure and `html` is capped at 200 000 chars.
- **`finally: r.close()` at `enricher.py:193-197` is correct** — it covers both the `DEAD`
  early return and the body-read path, and it runs before the regex extraction, so no
  socket leaks.
- **`main.py:199-200` treats `website="   "` as "has a website."** A whitespace-only value
  survives `main.py:87-89` (which only maps `""` → `None`), lands in `with_websites.csv`,
  gets `+1` at `enricher.py:107`, and is permanently denied the
  `not has_website` pitch branches. `dedup._is_missing` (`dedup.py:79-90`) already handles
  this correctly — the fix is to reuse it, not to add a fourth truthiness check.

## Recommended order of work

1. **`enricher.py:182` — split the status branch.** One allow-list; move
   `401/402/403/407/429/451` and `cf-mitigated: challenge` to `UNKNOWN`. Highest ratio of
   corrected sales decisions to lines changed; it is the difference between pitching
   rebuilds to healthy businesses or not.
2. **`enricher.py:189` — validate `r.encoding` with `codecs.lookup` before `decode`, and
   decide `LIVE`/`DEAD` from `status` alone**, outside the decode `try`. Do **not** apply
   the `065` narrowing to this handler.
3. **Validate/normalise `url` at `enricher.py:161`** (type, `strip()`, scheme allow-list,
   plausible hostname) and fix the case-sensitive `startswith("http")` in
   `osm.py:95` / `wikidata.py:103`. Kills rows 9, 12, 15, 26 above.
4. **Dedupe `targets` by URL in `enricher.py:231-238`** and fan results out to all record
   indices. Removes duplicate GETs and the self-inflicted `429 → DEAD`.
5. **Contact extraction:** drop the `@` alternative (`enricher.py:131`), suffix-match the
   email blacklist, strip `mailto:` query strings, and make `_WHATSAPP_RE` reject rather
   than truncate. This is the largest wrong-value surface in the CSVs.
6. **Parked/soft-404 detection** so a 200 on a lapsed `.com.lb` stops deleting the
   new-site pitch.
7. **Manual redirect following with per-hop validation** (`allow_redirects=False`) plus
   `ipaddress.is_global` on the connected peer — the only real fix for the residual SSRF,
   and it also removes the uncapped `resp.content` read in `resolve_redirects`.
8. **Return a `reason` alongside the outcome** so `072`'s triage stops guessing which
   bucket it is looking at, and derive the `contacts` key set once instead of
   duplicating it at `enricher.py:168` and `enricher.py:247-248`.
