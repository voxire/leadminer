# 005 — Enricher HTTP Robustness: Blind Liveness Heuristics, Unchecked TLS, and Network Fragility

## Verdict

The enricher's HTTP client is architecturally unsound: line 147 defines liveness as `r.status_code < 500`, which erroneously classifies HTTP 404, 403, 410, and 429 responses as `website_live = True`. When compounded by blanket `verify=False`, an inactivity-only `timeout=8`, unconstrained redirect following, and zero per-host rate limiting across 40 threads, the pipeline produces systemic false positives and false negatives. These errors corrupt the core product outputs: dead sites are denied the +20 `lead_score` bonus and misdirected away from Tier 1 "Website rebuild + maintenance" pitches, while transiently slow or rate-limited sites are falsely tagged dead and pitched broken-site rebuilds.

---

## Architectural Context & Data Flow

Website enrichment is executed in `enricher.py:180-220` via `check_websites()`:
1. `ThreadPoolExecutor(max_workers=40)` submits every record with a `website` property to `_fetch_website(url)`.
2. A single process-global `requests.Session` (`enricher.py:124`) executes a single GET request:
   `r = _SESSION.get(url, timeout=8, allow_redirects=True, verify=False)` (`enricher.py:146`).
3. Liveness and parsing branching logic is governed by lines 147–152:
   ```python
   live = r.status_code < 500
   if not live or r.status_code >= 400:
       return live, contacts
   html = r.text[:200_000]
   ```
4. Downstream effects of `website_live`:
   - **`enricher.lead_score` (`enricher.py:241-244`):**
     - `website_live is True` -> **+10 points**
     - `website and website_live is False` -> **+20 points** ("dead website = sales opportunity")
   - **`pitch_recommender.recommend_service` (`pitch_recommender.py:31-72`):**
     - `has_dead_website` (`has_website and not website_live`) triggers **Tier 1: "Website rebuild + maintenance"** (`pitch_recommender.py:54-55`).
     - `website_live is True` bypasses Tier 1 and drops into **Tier 3 ("SEO audit + visibility upgrade")** or **Tier 7 ("Digital marketing retainer")**.
   - **Output CSVs (`main.py:149-156`):**
     - Summary metrics `live` and `dead` printed to terminal and reflected in `all_businesses.csv`, `with_websites.csv`, and `sales_ready.csv`.

---

## Detailed Failure Mode Modeling & website_live Correctness

The audit specifically models five network failure modes plus per-host concurrency collapse, tracing their exact mechanics through `enricher.py` and evaluating the resulting effect on `website_live` correctness.

### 1. 3xx Redirect Chains

#### Mechanics & Code Trace
- `allow_redirects=True` is passed to `_SESSION.get()` (`enricher.py:146`) without setting `max_redirects` on the session or request. Requests defaults to a 30-redirect ceiling.
- **Scenario A (Circular / Infinite Loop):** Domain `http://example.com` redirects to `https://example.com/`, which redirects to `https://example.com/en/`, looping back to `/`. After 30 hops, `requests.exceptions.TooManyRedirects` is raised. Caught by `except Exception:` (`enricher.py:151`).
- **Scenario B (Redirect to Social / Third-Party Platform):** Lebanese and Saudi SMBs frequently register domains that 301/302 forward directly to `https://www.instagram.com/<handle>/` or `https://wa.me/<phone>`. Requests follows redirects to the target platform, receiving HTTP 200 from Instagram or WhatsApp.
- **Scenario C (Redirect to Domain Parking Broker):** Expired domain redirects to `https://www.sedo.com/search/details/?domain=...` or `https://dan.com/...`. Requests follows and lands on the parking platform with HTTP 200.

#### Effect on `website_live` Correctness
- **Scenario A (Loops):** **False Negative (`website_live = False`)**. The domain has an active HTTP server and DNS entry, but a misconfigured redirection rule marks it dead. While technically broken, it is dead by configuration rather than absent hosting.
- **Scenario B (Social Forwarding):** **False Positive (`website_live = True`)**. The business has no website; they have a domain pointing to social media. Leadminer marks `website_live = True`, awards +10 points instead of identifying a missing custom web presence, and parses Instagram's generic HTML login wall for contact info.
- **Scenario C (Parking Forwarding):** **False Positive (`website_live = True`)**. An abandoned domain is registered as live. Suppresses the +20 "dead website" pitch tier.

---

### 2. HTTP 429 (Too Many Requests) & WAF Anti-Bot Challenges

#### Mechanics & Code Trace
- When 40 concurrent workers strike rate-limited hosts, Cloudflare-protected domains, or security proxies, servers return `HTTP 429 Too Many Requests` or `HTTP 403 Forbidden` with a CAPTCHA/challenge page.
- Line 147 evaluates:
  `live = r.status_code < 500` -> `429 < 500` evaluates to **`True`**.
- Line 148 evaluates:
  `if not live or r.status_code >= 400:` -> `not True or 429 >= 400` -> `False or True` -> **`True`**.
- Line 149 executes:
  `return live, contacts` -> returns `(True, {"email": None, "instagram": None, "whatsapp": None, "linkedin": None})`.

#### Effect on `website_live` Correctness
- **Severe False Positive (`website_live = True`)**: The scraper never accessed the site's content. A rate-limited response or blocked request is recorded as a confirmed live site.
- **Contact Extraction Blindness**: Because line 148 returns early, all contact extraction is skipped. The record receives `website_live = True`, but zero emails or social handles.
- **Downstream Pitch Distortion**: The site is marked live, preventing Tier 1 rebuild outreach, while the lack of contact information prevents the lead from qualifying for `sales_ready.csv` (`main.py:144`).

---

### 3. Soft-404s & Parked Domains Returning HTTP 200

#### Mechanics & Code Trace
- Thousands of dormant, expired, or lapsed SMB domains in Lebanon and KSA do not return HTTP 404. Instead:
  1. Registrars (GoDaddy, Sedo, HugeDomains, Namecheap) serve parked landing pages with HTTP 200 containing text like *"Buy this domain"*, *"Renew now"*, or pay-per-click ad links.
  2. Shared web hosts (cPanel, Hostinger, Plesk, Apache) serve default placeholder pages (*"Default Web Site Page"*, *"Account Suspended"*, *"Apache2 Ubuntu Default Page"*).
  3. SPAs and modern CMS platforms return HTTP 200 with an empty shell containing *"404 - Page Not Found"* in the DOM body.
- Line 146 gets HTTP 200. Line 147 sets `live = True`. Line 150 reads the HTML: `html = r.text[:200_000]`.
- Regex extraction scans the parking or default host page.

#### Effect on `website_live` Correctness
- **Critical False Positive (`website_live = True`)**: The business has zero active digital presence, yet `enricher.py` certifies the site as healthy.
- **Sales Funnel Sabotage**: In `pitch_recommender.py:54`, Tier 1 ("Website rebuild + maintenance") explicitly targets businesses with `has_website and not website_live`. A parked domain is the single highest-converting lead for an agency pitch. By misclassifying soft-404s as live, the lead drops to Tier 3 or Tier 7.
- **Data Pollution**: Regexes at lines 154–176 scrape registrar contacts (e.g., `support@godaddy.com`, Sedo privacy emails, registrar WhatsApp brokers) and persist them into `record["email"]` and `record["whatsapp"]`.

---

### 4. A Site That Hangs (Slowloris, Tarpits, and Unbounded Streaming)

#### Mechanics & Code Trace
- `r = _SESSION.get(url, timeout=8, allow_redirects=True, verify=False)` passes a single scalar `timeout=8`.
- In `requests`, a single scalar sets both the connection timeout and the socket read timeout to 8 seconds. It is **not** a total execution timeout.
  1. **Socket Drip / Tarpit:** If a slow or hostile server sends one byte every 7 seconds, the socket never experiences 8 seconds of silence. The thread remains blocked indefinitely or until server close.
  2. **Unbounded Buffering (`stream=False`):** `_SESSION.get()` runs with default `stream=False`. It downloads the **entire response body into memory** before returning. If a server streams an endless payload (e.g. `/dev/urandom`, video file, massive archive), `requests` attempts to buffer gigabytes until OOM. Line 150 (`html = r.text[:200_000]`) only slices the string *after* the entire download has already consumed memory.
  3. **High Latency in Lebanon / Regional Middle East:** Middle Eastern SMB servers frequently exhibit high time-to-first-byte (TTFB) due to local electrical/telecom infrastructure bottlenecks. An 8.1-second TTFB raises `requests.exceptions.ReadTimeout`. Caught by `except Exception:` (`enricher.py:151`).

#### Effect on `website_live` Correctness
- **Thread Starvation & Run Freeze**: 40 hanging threads stall the `ThreadPoolExecutor`. In GitHub Actions with a 300-minute workflow timeout, the pipeline risks running for hours or timing out.
- **False Negative (`website_live = False`)**: A legitimate, operating SMB website that takes 8.2 seconds to complete TLS handshake and send headers is stamped `website_live = False`.
- **False Sales Pitch Generation**: The site is awarded +20 points for being "dead" and assigned Tier 1 "Website rebuild". When the sales rep contacts the business saying their website is down, the business owner opens the site on their phone, finds it working, and dismisses the agency.

---

### 5. TLS Interception, Expired Certs & Blind `verify=False`

#### Mechanics & Code Trace
- `verify=False` is hardcoded at `enricher.py:146`. At `enricher.py:268`, warnings are globally disabled:
  `urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)`.
- **Scenario A (Expired / Self-Signed / Hostname Mismatch):** A small business let their Let's Encrypt or cPanel AutoSSL cert expire, or uses a shared hosting default cert (`*.cpanelhost.com`).
  - To any real consumer using Chrome, Safari, or mobile browsers, the site displays a full-screen red warning (`NET::ERR_CERT_DATE_INVALID` or `NET::ERR_CERT_COMMON_NAME_INVALID`). Over 95% of visitors bounce.
  - `enricher.py` bypasses validation, connects, receives HTTP 200, and sets `website_live = True`.
- **Scenario B (ISP Captive Portals / Middlebox Interception):** On Lebanese ISPs (e.g., Ogero, Alfa, Touch) and Saudi networks, DNS poisoning or HTTP captive interception redirects unpaid accounts or filtered domains to an internal ISP landing page presenting an invalid or untrusted certificate.
  - `verify=False` allows the connection to proceed. The ISP portal returns HTTP 200.

#### Effect on `website_live` Correctness
- **Major False Positive (`website_live = True`)**:
  - In Scenario A, an objectively broken website (inaccessible to legitimate customers) is marked live. The agency misses an undeniable, high-urgency pitch: *"Your SSL certificate has expired and browsers are blocking your customers."*
  - In Scenario B, an ISP block page or captive portal is validated as the business's active website, marking `website_live = True` and scraping ISP billing emails into the business record.

---

### 6. Absence of Per-Host Politeness and robots.txt

#### Mechanics & Code Trace
- `check_websites()` spawns 40 worker threads with zero rate-limiting, zero domain grouping, and zero per-host delay.
- Many SMBs in Lebanon and Saudi Arabia share regional hosting infrastructure:
  - Local Lebanese hosting providers (e.g., IDM, Cyberia, Terranet, local cPanel resellers).
  - Common SaaS hosts (Shopify, Wix, Squarespace, Salla, Zid).
- 40 concurrent workers fire requests across thousands of URLs. If 15 scraped businesses are hosted on the same shared cPanel IP or Salla/Wix cluster, up to 15 concurrent GET requests strike that single IP simultaneously.
- No `robots.txt` check is performed. No `User-Agent` contact email is provided (`"Mozilla/5.0 (compatible; leadminer/1.0)"`).

#### Effect on `website_live` Correctness
- **Cascading False Negatives (`website_live = False`)**:
  - Shared servers and WAFs detect concurrent request spikes and trigger Fail2Ban, ModSecurity, or IP rate limiting.
  - The runner's IP address (e.g. GitHub Actions runner) is blacklisted for the remainder of the job.
  - Every subsequent website hosted on that provider fails with `ConnectionRefusedError`, `ConnectionResetError`, or `ReadTimeout`.
  - All those sites are recorded as `website_live = False`, creating clusters of false negatives across entire hosting providers.

---

## Findings

### S1 — Line 147 treats HTTP 4xx (404, 403, 410, 429) as `website_live = True`

- **Where:** `enricher.py:147-149`
- **Breaks:**
  ```python
  live = r.status_code < 500
  if not live or r.status_code >= 400:
      return live, contacts
  ```
  For an HTTP 404 (Not Found), 410 (Gone), 403 (Forbidden), or 429 (Too Many Requests), `r.status_code < 500` evaluates to `True`. Then `if not live or r.status_code >= 400:` branches to `return live, contacts`, returning `(True, contacts)`.
  Every 404 dead link in the scrape is marked `website_live = True`. This directly inverts the product's primary value proposition: dead websites are denied the Tier 1 "Website rebuild + maintenance" pitch (`pitch_recommender.py:54`) and lose 10 points in `lead_score` (`enricher.py:241-244`).
- **Trigger:** Any URL returning HTTP 404, HTTP 410, HTTP 403, or HTTP 429:
  ```python
  # Trace:
  r.status_code = 404
  live = 404 < 500          # -> True
  if not True or 404 >= 400: # -> True
      return True, contacts # -> website_live is recorded as True!
  ```
- **Fix:** Redefine liveness to valid successful HTTP responses:
  `live = 200 <= r.status_code < 400`

---

### S1 — Unconditional `verify=False` silences broken SSL and validates captive portals

- **Where:** `enricher.py:146`, `enricher.py:267-268`
- **Breaks:** Disabling TLS certificate validation across the board treats invalid, expired, self-signed, and hostname-mismatched certificates as healthy. In the target markets (Lebanon/KSA), SSL misconfiguration is ubiquitous among SMBs. Real browsers refuse to load these sites without severe interstitial security warnings. Furthermore, network captive portals or ISP interception gateways with untrusted certificates return HTTP 200, which `enricher.py` treats as a verified business website.
- **Trigger:** A domain with an expired Let's Encrypt certificate (e.g., `https://expired.badssl.com/`). Real browsers block traffic. `enricher.py` reports `website_live = True`.
- **Fix:** Attempt verification by default, record TLS validity as an explicit signal (`ssl_valid: bool`), and only fall back to unverified GET if diagnosing certificate failure:
  ```python
  # Distinguish TLS failure from dead site:
  try:
      r = session.get(url, timeout=timeout, verify=True)
      ssl_valid = True
  except requests.exceptions.SSLError:
      r = session.get(url, timeout=timeout, verify=False)
      ssl_valid = False
  ```

---

### S1 — Parked domains and hosting placeholders return HTTP 200, spoofing liveness and scraping bogus contacts

- **Where:** `enricher.py:147-177`
- **Breaks:** Expired domains parked on GoDaddy, Sedo, or Dan, as well as unconfigured cPanel / Apache default pages, return HTTP 200. `_fetch_website` marks `website_live = True` and runs regex extraction on the parking page. This results in:
  1. Abandoned sites escaping the "Website rebuild" pitch.
  2. Registrar sales and privacy emails (e.g., `abuse@godaddy.com`, `dns@sedo.com`) saved as the business's contact info.
- **Trigger:** Any expired SMB domain pointing to nameservers like `ns1.godaddy.com` or displaying *"This domain is parked with Namecheap"*.
- **Fix:** Implement lightweight heuristic fingerprinting on the response body to detect parking and placeholder pages before confirming liveness:
  ```python
  PARKED_SIGNATURES = (
      "buy this domain", "domain is parked", "parked free", "godaddy",
      "dan.com", "sedo", "cpanel default page", "account suspended",
      "default website page", "under construction"
  )
  is_parked = any(sig in html.lower() for sig in PARKED_SIGNATURES)
  if is_parked:
      live = False
  ```

---

### S1 — Unbounded body downloads (`stream=False`) and single scalar timeout permit tarpits to freeze worker threads

- **Where:** `enricher.py:146`, `enricher.py:150`
- **Breaks:**
  1. `timeout=8` is an inactivity socket timeout. A server streaming 1 byte every 7 seconds bypasses the timeout indefinitely, locking a worker thread.
  2. `_SESSION.get()` is invoked with `stream=False` (default). It buffers the entire HTTP body into memory before `r.text[:200_000]` can slice it. A rogue server or massive binary file (e.g. 1 GB disk image mistakenly linked as a homepage) triggers massive memory bloat and pipeline lockup.
- **Trigger:** A server returning an infinite chunked response or a slowloris drip:
  ```python
  # A server dripping 1 byte every 5s never exceeds timeout=8
  # ThreadPoolExecutor thread is blocked permanently.
  ```
- **Fix:** Split timeout into `(connect, read)` tuple, stream responses, and cap total downloaded bytes directly at the socket level:
  ```python
  r = _SESSION.get(url, timeout=(3.0, 5.0), stream=True, allow_redirects=True)
  content = b""
  for chunk in r.iter_content(chunk_size=8192):
      content += chunk
      if len(content) >= 200_000:
          break
  r.close()
  ```

---

### S2 — Uncapped redirect following (`allow_redirects=True`) with no domain guard poisons liveness and social contacts

- **Where:** `enricher.py:146`
- **Breaks:**
  1. `allow_redirects=True` follows up to 30 redirects indiscriminately. Circular redirect chains raise `TooManyRedirects`, which line 151 catches as an exception and marks `website_live = False`.
  2. Domains configured to forward to third-party social profiles (`someshop.com` -> `instagram.com/someshop`) follow through to Instagram, get HTTP 200, mark `website_live = True`, and run regexes on Instagram's login/meta HTML. The system fails to realize the business has no functional standalone website.
- **Trigger:** A domain that 301 redirects to `https://www.instagram.com/lebanon_bakery/`.
- **Fix:** Limit redirects to a maximum of 5 hops and verify the final redirected domain matches or canonically relates to the original target:
  ```python
  # Configure session redirect ceiling:
  _SESSION.max_redirects = 5
  # In _fetch_website:
  final_host = urlparse(r.url).netloc.lower()
  if any(social in final_host for social in ("instagram.com", "facebook.com", "wa.me")):
      # Domain is an alias/redirect to social, not an independent live website
      return False, extract_social_from_redirect(r.url)
  ```

---

### S2 — Zero-retry, zero-backoff policy turns transient network jitter into permanent `website_live = False`

- **Where:** `enricher.py:145-152`
- **Breaks:** Network requests fail transiently due to TCP reset, temporary DNS blip, TLS handshake stall, or transient 503 Service Unavailable. Without retries or backoff:
  - 1 transient glitch immediately triggers line 151 `except Exception: return False, contacts`.
  - A healthy, high-value business is marked `website_live = False`.
  - Downstream, the lead receives +20 points and is queued for a "Website rebuild" pitch. The sales team pitches a working business, destroying agency credibility.
- **Trigger:** A remote server resetting the TCP connection on the first handshake (`ConnectionResetError`), but succeeding 500ms later.
- **Fix:** Mount an `HTTPAdapter` configured with `urllib3.util.Retry` for idempotent GET requests:
  ```python
  from requests.adapters import HTTPAdapter
  from urllib3.util import Retry

  retries = Retry(total=2, backoff_factor=0.5, status_forcelist=[502, 503, 504])
  adapter = HTTPAdapter(max_retries=retries, pool_connections=40, pool_maxsize=40)
  _SESSION.mount("http://", adapter)
  _SESSION.mount("https://", adapter)
  ```

---

### S2 — 40-worker unthrottled concurrency with no per-host politeness or `robots.txt` triggers IP blacklisting

- **Where:** `enricher.py:180-189`
- **Breaks:**
  1. Firing 40 concurrent threads without per-host rate limiting or queuing hammers shared regional web hosts (e.g. Lebanese hosters IDM, Cyberia; cPanel shared IPs; Salla/Zid in KSA).
  2. Triggers WAF rate limits and firewall IP bans (Cloudflare, Fail2Ban). Once the scraper's IP is banned, subsequent requests to different businesses on the same host/WAF fail with connection drops.
  3. Ignores `robots.txt` directives and `Crawl-delay`, violating standard crawling hygiene.
- **Trigger:** A scrape containing 20 local restaurants hosted on the same Lebanese cPanel IP (`IDM` shared hosting). 40 workers burst requests into the IP simultaneously, triggering automated DDoS protection.
- **Fix:** Partition requests by target domain/host, enforce a minimum per-host delay (e.g. 1.0s), and cap per-host concurrency at 1 worker:
  ```python
  # Group targets by netloc before dispatch, or use a token-bucket per domain
  from collections import defaultdict
  # Cap concurrent requests to the same hostname to 1
  ```

---

### S2 — Process-global mutable `requests.Session` across 40 threads causes connection pool thrashing

- **Where:** `enricher.py:124`, `enricher.py:188-189`
- **Breaks:**
  1. `requests.Session()` is initialized globally. By default, `requests.adapters.HTTPAdapter` sets `pool_connections=10` and `pool_maxsize=10`.
  2. With 40 threads hitting `_SESSION` simultaneously, the urllib3 connection pool is constantly exhausted (`Connection pool is full, discarding connection`). New TCP connections must be opened from scratch, creating socket churn.
  3. `_SESSION.cookies` (`RequestsCookieJar`) is accessed and mutated concurrently by 40 threads without locking, causing cross-domain cookie bleeding and race conditions.
- **Trigger:** 40 threads executing `_SESSION.get()` simultaneously against multiple sites that set cookies (`Set-Cookie`).
- **Fix:** Configure `HTTPAdapter(pool_connections=50, pool_maxsize=50)` and disable cookie persistence across domains:
  ```python
  adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50)
  _SESSION.mount("http://", adapter)
  _SESSION.mount("https://", adapter)
  # Or use thread-local sessions / pass cookies=None
  ```

---

### S3 — Deceptive User-Agent string invites anti-bot filtering and lacks operator contact info

- **Where:** `enricher.py:125`
- **Breaks:**
  `_SESSION.headers["User-Agent"] = "Mozilla/5.0 (compatible; leadminer/1.0)"`
  This User-Agent string is contradictory: it borrows Mozilla browser syntax while appending `compatible; leadminer/1.0`. Modern bot detection engines (Cloudflare, Akamai, AWS WAF) flag hybrid user agents as low-reputation scraping signatures and immediately return 403/429. It also violates crawler politeness conventions by failing to provide an operator email or website URL for webmasters to reach out.
- **Trigger:** Visiting any Cloudflare-fronted SMB website in KSA.
- **Fix:** Provide a legitimate, transparent crawler identity or a realistic browser UA with proper headers:
  `_SESSION.headers["User-Agent"] = "leadminer-bot/1.0 (+https://voxire.com/bot; bot@voxire.com)"`

---

### S3 — Blanket `except Exception:` swallows programming errors and discards failure diagnostics

- **Where:** `enricher.py:151-152`
- **Breaks:**
  ```python
  except Exception:
      return False, contacts
  ```
  Catching `Exception` indiscriminately hides underlying causes: `requests.exceptions.ConnectTimeout`, `ReadTimeout`, `SSLError`, `TooManyRedirects`, `ConnectionError`, or internal Python exceptions (e.g. `MemoryError`, regex bugs). All failures collapse into `website_live = False` with zero logging, making it impossible to audit *why* sites failed.
- **Trigger:** A malformed URL causing an unhandled library exception or a DNS timeout.
- **Fix:** Catch specific request exceptions and record the discrete failure reason (`error_reason: str`):
  ```python
  except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.RequestException) as e:
      return False, contacts, type(e).__name__
  ```

---

## Not a bug, but worth knowing

- **Single-pass architecture creates conflicting optimization goals:** `enricher.py` attempts to combine liveness probing with contact scraping in a single HTTP GET. A true liveness probe should be an ultra-fast, lightweight `HEAD` request (or TLS probe), whereas contact scraping requires fetching and parsing the full DOM. Forcing both into one 8-second GET results in downloading 200 KB HTML payloads for sites that only need reachability verification.
- **No DNS resolution caching:** Python's standard `socket.getaddrinfo()` is invoked on every single request across all 40 threads. Operating systems without local caching resolvers (like GitHub Actions Linux runners) repeatedly hammer upstream DNS resolvers, leading to transient DNS timeouts on large runs.
- **`urllib3.disable_warnings` in `enrich()` is process-global:** Invoking `urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)` at `enricher.py:268` suppresses security warnings for the entire Python runtime. If another component or scraper in the future performs secure API transactions, security warnings on genuine configuration errors will be silenced.
- **Asymmetry with `pitch_recommender.py`:** `pitch_recommender.py:33` computes `has_dead_website = has_website and not website_live`. Notice that if a business never had a website (`website` is empty or None), `website_live` defaults to `None`. `lead_score()` at `enricher.py:241-244` correctly checks `elif record.get("website") and record.get("website_live") is False:`, but because `website_live` is so easily corrupted by HTTP failure modes, the scoring distribution is skewed towards dead websites.

---

## Recommended Order of Work

1. **Fix the HTTP status code liveness logic (S1 — `enricher.py:147-149`):**
   Immediately change `live = r.status_code < 500` to `live = 200 <= r.status_code < 400`. This stops 404, 403, 410, and 429 error pages from being certified as live websites.
2. **Implement safe body streaming and request limits (S1 — `enricher.py:146, 150`):**
   Set `(connect, read)` timeouts to `(3.0, 5.0)` and use `stream=True` to read only up to 200 KB directly from the socket, preventing hanging tarpits and unbounded RAM consumption.
3. **Add parked domain and placeholder detection (S1 — `enricher.py:150`):**
   Add basic string matching for common registrar parking and default hosting signatures (GoDaddy, Sedo, Dan, cPanel default page) to mark dormant domains as dead.
4. **Distinguish TLS failures instead of blind `verify=False` (S1 — `enricher.py:146`):**
   Verify SSL certificates by default. Catch `SSLError` and record `ssl_valid = False`, allowing the pitch recommender to target broken SSL certificates as a dedicated sales opportunity.
5. **Configure retry, backoff, and connection pooling (S2 — `enricher.py:124`):**
   Mount an `HTTPAdapter` on `_SESSION` with `urllib3.util.Retry(total=2, backoff_factor=0.5)` and set `pool_connections=50, pool_maxsize=50`.
6. **Constrain redirects and detect social forwards (S2 — `enricher.py:146`):**
   Cap redirects to 5 hops (`max_redirects=5`) and detect when a domain redirects to Instagram, Facebook, or WhatsApp.
7. **Introduce per-host rate limiting and politeness (S2 — `enricher.py:180-189`):**
   Group targets by domain and enforce a 1-request concurrency limit per host to avoid triggering WAF rate limits and runner IP blacklisting.
8. **Refine User-Agent and exception logging (S3 — `enricher.py:125, 151`):**
   Adopt a clean, identifiable User-Agent and replace blanket `except Exception:` with granular error tracking (`status_code`, `error_type`).
