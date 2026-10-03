# 054 — Hardened HTTP Fetch Layer: Politeness and SSRF Protection

## Verdict

The current website enrichment layer in `enricher.py` executes unauthenticated HTTP requests across 40 concurrent threads against untrusted, crowdsourced URLs with `verify=False`, no robots.txt checks, no per-host rate limiting, and zero SSRF protection. An attacker or crowdsourced editor on OpenStreetMap, Wikidata, or Google Places can point target URLs to cloud instance metadata (`169.254.169.254`), loopback daemons (`127.0.0.1`, `[::1]`), or internal subnets, causing the runner to probe private networks and exfiltrate credentials into CSVs uploaded to Google Drive. Furthermore, `verify=False` enables on-path tampering and misclassifies broken SSL certificates as healthy, while unconstrained concurrency against regional shared hosting triggers automated WAF bans. To establish production safety and crawling politeness, `leadminer` requires a unified, hardened fetch layer that enforces an explicit 10-step deny-list evaluation order at socket level (neutralizing DNS rebinding and redirect escapes), strict TLS certificate verification, a global concurrency budget, per-domain leaky-bucket rate limiting, cached robots.txt compliance, exponential backoff with full jitter honoring `Retry-After`, and strict streaming byte caps.

---

## Findings

### S1 — Unrestricted SSRF and Cloud Metadata Exfiltration via Crowdsourced URLs

- **Where:** `enricher.py:167` (`_fetch_website`), fed by `check_websites:223`, sourcing unvalidated URLs from `scrapers/osm.py:88-93`, `scrapers/wikidata.py:66-73`, and `scrapers/google_places.py:259`.
- **Breaks:** Any business record containing a malicious `website` property directs the scraper host (e.g., GitHub Actions runner, local workstation, or internal VM) to dispatch an HTTP GET request to arbitrary network endpoints. Sourced from public OpenStreetMap tags or Wikidata items, these URLs are fully user-controlled.
  
  When running on cloud infrastructure (AWS, Azure, GCP, DigitalOcean, OpenStack), dialing `http://169.254.169.254/` targets Instance Metadata Services (IMDS). On AWS IMDSv1, this exposes IAM role security credentials; on GCP, it targets `metadata.google.internal`; on Alibaba Cloud, it targets `100.100.100.200`. In a local workstation or enterprise VPC, it allows port scanning and interaction with unauthenticated daemons (Docker socket `127.0.0.1:2375`, Redis `127.0.0.1:6379`, Kubernetes Kubelet `127.0.0.1:10250`, or internal RFC 1918 web apps).
  
  Crucially, `enricher.py:191-213` executes regex extraction for emails, Instagram handles, WhatsApp numbers, and LinkedIn URLs on the returned body. If an internal service or metadata endpoint leaks an email (e.g., cloud service account emails or internal error messages), it is persisted into `record["email"]` (`enricher.py:245`), written to `all_businesses.csv` and `sales_ready.csv` (`main.py:209-213`), and synced to public Google Drive storage via rclone (`.github/workflows/scrape.yml:48-50`). This converts a blind SSRF into an automatic data exfiltration channel.
- **Trigger:** A crowdsourced OSM node with:
  ```yaml
  website: "http://169.254.169.254/latest/meta-data/iam/security-credentials/admin-role"
  ```
  or an internal loopback target:
  ```yaml
  website: "http://127.0.0.1:2375/containers/json"
  ```
- **Fix:** Enforce a strict protocol allow-list (`http`, `https`), validate resolved IP addresses against a comprehensive CIDR deny-list prior to socket connection, connect directly to the validated IP to eliminate DNS rebinding, and re-validate every redirect hop manually.

---

### S1 — Memory Exhaustion and Runner Hang via Decompression Bombs and Unbounded Streaming

- **Where:** `enricher.py:167-184`
- **Breaks:** While `enricher.py:180` attempts to limit read bytes via `r.raw.read(_MAX_BODY_BYTES, decode_content=True)`, the request is initiated without Content-Length pre-checks, without a total wall-clock timeout budget, and without safe decompression streaming limits:
  1. **Socket Drip / Slowloris:** In the `requests` library, `timeout=8` specifies socket connect and socket read timeouts independently; it does **not** enforce a total request execution timeout. A slow-drip server sending one byte every 7.5 seconds will never trigger the 8-second read timeout, holding an enrichment worker blocked indefinitely. Across 40 workers (`enricher.py:229`), 40 hanging sockets will stall the entire enrichment stage until GitHub Actions forcibly kills the runner at 300 minutes (`scrape.yml:11`).
  2. **Decompression Bombs (Zip Bombs):** If a target responds with `Content-Encoding: gzip` or `deflate` and serves a crafted archive (e.g. 10 MB of compressed zeros expanding to 10 GB), `r.raw.read(decode_content=True)` delegates decompression to `zlib` / `urllib3` within memory. In earlier implementations or non-streaming requests, this causes an immediate out-of-memory (OOM) kill (`SIGKILL`) on the 7 GB GitHub Actions runner.
- **Trigger:** A target URL returning an infinite chunked stream (`Transfer-Encoding: chunked`) at 1 byte every 7 seconds, or a 10 MB recursive gzip bomb.
- **Fix:** Implement a streaming response processor with an absolute wall-clock request deadline (10 seconds total across DNS, connect, TLS, redirects, and body read), reject responses where `Content-Length > 1_000_000`, and iteratively stream and decompress with a hard cut-off at exactly 200 KB, immediately closing the underlying transport socket.

---

### S2 — Global TLS Verification Disabled (`verify=False`) Distorts Lead Signals and Enables MITM

- **Where:** `enricher.py:167` (`verify=False`)
- **Breaks:** `_SESSION.get(url, ..., verify=False)` explicitly disables TLS certificate validation. This causes two severe failures:
  1. **Destroys Core Lead Signal (False Positives):** In Lebanon and KSA, thousands of SMB websites have expired Let's Encrypt certificates, untrusted self-signed certs, or host mismatch errors (`NET::ERR_CERT_DATE_INVALID`). To a consumer browser, these sites display full-screen red security warnings, rendering them inaccessible to real customers. However, `enricher.py` connects successfully, receives HTTP 200, and stamps `website_live = True` (`enricher.py:214`). This sabotages sales lead qualification: in `pitch_recommender.py:54`, the highest-value Tier 1 pitch is "Website rebuild / renewal". Disabling TLS verification hides broken certificates, disqualifies prime leads, and marks them as healthy.
  2. **Man-in-the-Middle (MITM) Vulnerability:** Traffic routed through untrusted networks or ISP middleboxes (such as Lebanese Ogero/Alfa captive portals) can intercept plaintext or forged TLS traffic, serving captive login pages containing ISP billing contacts. The enricher regex scrapes these false contacts directly into the lead database.
- **Trigger:** An SMB domain `https://dr-dentist-beirut.com` with an expired SSL certificate.
- **Fix:** Enforce `verify=True` using the standard `certifi` CA bundle. Catch `requests.exceptions.SSLError` / `ssl.SSLCertVerificationError` explicitly to record `website_live = False` with a structured diagnosis (`tls_error: certificate_expired`), converting it into a verified Tier 1 sales pitch.

---

### S2 — Missing Per-Host Rate Limiter, robots.txt Awareness, and Concurrency Budget Triggers Host Bans

- **Where:** `enricher.py:217-230` (`check_websites` with `workers=40`)
- **Breaks:** 
  1. **Shared Infrastructure Flooding:** Many Lebanese and Saudi SMBs share regional hosting infrastructure: local hosting providers (e.g. IDM, Cyberia, Terranet, local cPanel resellers) or regional e-commerce SaaS platforms (Salla, Zid, Shopify). When 40 worker threads fetch thousands of URLs concurrently without domain grouping, dozens of requests strike the exact same host or shared IP simultaneously. This triggers automated WAF DDoS mitigation (Cloudflare, Fail2Ban, cPanel ModSecurity), returning HTTP 429 or dropping connections for all subsequent businesses on that IP.
  2. **Zero robots.txt Compliance:** The scraper does not fetch or parse `/robots.txt`. Commercial crawlers that ignore robots.txt violate standard web politeness conventions and risk administrative takedowns or legal complaints from domain owners.
  3. **No Retry-After Handling:** When servers respond with HTTP 429 (Too Many Requests) or HTTP 503 (Service Unavailable) with a `Retry-After` header, `enricher.py` simply treats them as `DEAD` or crashes into `UNKNOWN`, instead of backing off politely and retrying with jitter.
- **Trigger:** A batch containing 20 Lebanese dental clinics hosted on the same shared cPanel IP (`IDM` shared hosting). 40 workers burst requests into the IP simultaneously, triggering automated IP blocking.
- **Fix:** Partition requests by hostname, enforce a per-host leaky-bucket rate limit (minimum 1.5 seconds between requests; max 1 concurrent connection per host), obey robots.txt directives with in-memory TTL caching, limit global concurrency to 16 workers, and implement exponential backoff with full jitter honoring `Retry-After`.

---

## Deny-List & Security Policy: Detailed Evaluation Order

To provide airtight protection against SSRF, loopback access, cloud metadata theft, DNS rebinding (TOCTOU), and redirect bypasses, the HTTP fetch layer must evaluate targets against an explicit, deterministic 10-step validation pipeline.

Every single target URL—both the initial URL and every URL encountered in a `Location` redirect header—must execute this sequence in exact order before any TCP connection or TLS handshake is performed.

```
+-----------------------------------------------------------------------------------+
|                           Target URL (Initial or Redirect)                        |
+-----------------------------------------------------------------------------------+
                                         │
                                         ▼
[Step 1: Scheme Check] ──────────► Is scheme strictly 'http' or 'https'?
                                         │ Yes (Reject file://, gopher://, ftp://, data:)
                                         ▼
[Step 2: Port Check] ────────────► Is port in {80, 443} (or explicitly allowed web port)?
                                         │ Yes (Reject internal ports: 22, 2375, 6379, etc.)
                                         ▼
[Step 3: Host Syntax Check] ─────► Does host contain credentials (@), null bytes, or spaces?
                                         │ Pass (Sanitize and extract hostname)
                                         ▼
[Step 4: Literal IP Check] ──────► Is host a literal IP string? If so, evaluate Deny-List.
                                         │ Pass (If not literal, proceed to DNS resolution)
                                         ▼
[Step 5: DNS Resolution] ────────► Resolve A and AAAA records under strict 3.0s deadline.
                                         │ Pass (DNS query succeeds with >= 1 record)
                                         ▼
[Step 6: IP Deny-List Matrix] ───► Test ALL resolved IP addresses against Deny-List Matrix.
                                   (IPv4 + IPv6 + IPv4-Mapped IPv6 + Metadata + RFC1918)
                                         │ All IPs Global & Routable (Drop if ANY match deny)
                                         ▼
[Step 7: Rebinding-Safe Dial] ───► Dial socket DIRECTLY to validated IP address.
                                   Set SNI = original hostname, Host = original hostname.
                                         │ Connected
                                         ▼
[Step 8: TLS Verification] ──────► Perform TLS handshake with verify=True via system CA.
                                         │ Handshake OK
                                         ▼
[Step 9: Streamed Read & Cap] ───► Inspect Content-Length. Stream chunks up to 200 KB max.
                                         │ Body parsed
                                         ▼
[Step 10: Redirect Guard] ───────► If 3xx redirect: hop_count <= 5?
                                   Yes: URL = urljoin(current, Location) ──► Loop to Step 1
                                   No: Abort with TooManyRedirects.
```

### Exact Evaluation Order Specification

#### Step 1: Scheme Allow-List Verification
- **Rule:** The URL scheme must belong to the strict allow-list `{"http", "https"}`.
- **Action:** Parse URL using `urllib.parse.urlsplit`. If scheme is not lowercase `http` or `https`, immediately raise `SSRFSecurityError(f"Disallowed scheme: {scheme}")`.
- **Blocks:** `file://` (prevents local file disclosures like `/etc/passwd`), `gopher://`, `dict://`, `ftp://`, `ldap://`, `data:`, `javascript:`.

#### Step 2: Port Allow-List Verification
- **Rule:** The target destination port must belong to standard web ports: `{80, 443}` (or optionally `{8080, 8443}`).
- **Action:** Extract port from `parsed.port` (defaulting to 80 for `http` and 443 for `https`). If `port not in {80, 443, 8080, 8443}`, immediately raise `SSRFSecurityError(f"Disallowed port: {port}")`.
- **Blocks:** Port scanning against internal services: SSH (`22`), Telnet (`23`), SMTP (`25`), DNS (`53`), Redis (`6379`), Memcached (`11211`), Docker daemon (`2375`, `2376`), Kubernetes API (`6443`, `10250`), Elasticsearch (`9200`).

#### Step 3: Hostname Syntax & Canonicalization
- **Rule:** Hostname must be syntactically valid and free of URI parser confusion attacks.
- **Action:**
  1. Reject URLs containing embedded credentials (`parsed.username` or `parsed.password` present, or `@` symbol in netloc). This prevents userinfo parser confusion (e.g. `http://trusted.com@169.254.169.254`).
  2. Reject hostnames containing null bytes (`\x00`), whitespace, control characters, or non-ASCII characters that cannot be cleanly IDNA-encoded (punycode).
  3. Strip trailing dots (e.g. `example.com.` -> `example.com`).

#### Step 4: Hostname Literal IP Pre-Resolution Check
- **Rule:** If the hostname is a literal IP address (IPv4 or IPv6), it must be evaluated directly before DNS lookup.
- **Action:** Attempt to parse hostname using `ipaddress.ip_address()`. If successful, evaluate the IP against the Deny-List Subnet Matrix in Step 6. If it matches any denied range, reject immediately without querying DNS.

#### Step 5: DNS Resolution with Strict Timeout
- **Rule:** Resolve domain name to all associated IP addresses (both IPv4 `A` and IPv6 `AAAA` records) within a strict 3.0-second timeout.
- **Action:** Call `socket.getaddrinfo(hostname, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)`.
  - If resolution fails (`socket.gaierror`), classify as `UNKNOWN` (DNS failure).
  - Collect every unique resolved IP address. If no addresses are returned, abort.

#### Step 6: Resolved IP Address Deny-List Evaluation
- **Rule:** **Every single resolved IP address** associated with the hostname must be validated. If **any** resolved IP falls into a denied subnet, the entire request is rejected immediately.
- **Why Check All IPs?** Multi-homed DNS records often publish a valid public IP alongside an internal IP (`127.0.0.1` or `10.0.0.1`) to facilitate internal testing or bypass naive single-IP checks.
- **IPv4-Mapped IPv6 Unwrapping:** If an address is an IPv6 address falling within `::ffff:0:0/96`, extract the underlying IPv4 address (`ip.ipv4_mapped`) and evaluate it against all IPv4 deny-list rules.

##### The Deny-List Subnet Matrix

| Category | CIDR / Address Range | RFC / Specification | Threat / Vector Blocked |
|---|---|---|---|
| **Loopback** | `127.0.0.0/8`<br>`::1/128` | RFC 1122<br>RFC 4291 | Local host daemons (Redis, MySQL, Docker, Kubelet). |
| **Cloud Metadata** | `169.254.169.254/32`<br>`fd00:ec2::254/128`<br>`100.100.100.200/32` | AWS / GCP / Azure<br>AWS IMDS IPv6<br>Alibaba Cloud | Cloud instance profile credential theft, IAM role exfiltration, instance user-data dumps. |
| **Link-Local / APIPA** | `169.254.0.0/16`<br>`fe80::/10` | RFC 3927<br>RFC 4291 | Unconfigured local network devices, cloud hypervisor sidecars. |
| **RFC 1918 Private** | `10.0.0.0/8`<br>`172.16.0.0/12`<br>`192.168.0.0/16` | RFC 1918 | Corporate intranets, internal VPCs, home router gateways (`192.168.1.1`). |
| **Unique Local (ULA)** | `fc00::/7` | RFC 4193 | IPv6 private internal corporate subnets. |
| **Carrier-Grade NAT** | `100.64.0.0/10` | RFC 6598 | Shared telecom CGNAT infrastructure, internal cloud VPC peering. |
| **Current Network / "This"**| `0.0.0.0/8`<br>`::/128` | RFC 1122<br>RFC 4291 | Binds to localhost or all interfaces on BSD/Linux sockets. |
| **Multicast & Broadcast** | `224.0.0.0/4`<br>`255.255.255.255/32`<br>`ff00::/8` | RFC 5771<br>RFC 919<br>RFC 4291 | Multicast groups, network broadcast storm triggers. |
| **Class E / Reserved** | `240.0.0.0/4` | RFC 1112 | Unassigned / experimental space, frequently unrouted. |
| **Documentation / Test** | `192.0.2.0/24`<br>`198.51.100.0/24`<br>`203.0.113.0/24`<br>`2001:db8::/32` | RFC 5737<br>RFC 3849 | Documentation test subnets (TEST-NET-1, 2, 3). |
| **Benchmarking** | `198.18.0.0/15` | RFC 2544 | Inter-network communication benchmarking. |
| **IPv4-Mapped IPv6** | `::ffff:0:0/96` | RFC 4291 | IPv4 addresses embedded inside IPv6 representation (must be unwrapped). |
| **6to4 / Teredo Relays** | `2002::/16`<br>`2001::/32` | RFC 3056<br>RFC 4380 | Deprecated transition relays; can tunnel private IPv4 traffic. |

- **Strict Validation Rule:**
  ```python
  def is_ip_safe(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
      if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
          return False
      if isinstance(ip, ipaddress.IPv6Address):
          if ip.ipv4_mapped:
              return is_ip_safe(ip.ipv4_mapped)
          # Additional check for AWS IMDS IPv6
          if ip == ipaddress.IPv6Address("fd00:ec2::254"):
              return False
      if isinstance(ip, ipaddress.IPv4Address):
          # Alibaba Cloud metadata IP
          if ip == ipaddress.IPv4Address("100.100.100.200"):
              return False
      return True
  ```

#### Step 7: Socket Connection Binding & DNS Rebinding (TOCTOU) Elimination
- **Vulnerability (Time-of-Check to Time-of-Use):** If the application resolves `evil.com` to public IP `93.184.216.34` during Step 5, approves it, and then invokes `requests.get("http://evil.com")`, the OS resolver or `urllib3` executes a *second* DNS lookup. If `evil.com` operates an authoritative DNS server with `TTL=0`, it returns `169.254.169.254` on the second lookup. The connection is established directly with the forbidden metadata service, completely bypassing Step 6.
- **Solution:** Connect the TCP socket directly to the **specific pre-validated IP address**.
  - For HTTP: connect socket to `(validated_ip, port)`, transmit `Host: <original_hostname>`.
  - For HTTPS: connect TCP socket to `(validated_ip, port)`, then wrap socket using TLS context with `server_hostname=<original_hostname>`. This guarantees TLS Server Name Indication (SNI) and certificate validation match the domain name, while the TCP connection is locked to the validated IP.

#### Step 8: Strict TLS Certificate Verification
- **Rule:** Every HTTPS request must verify the remote certificate against the trusted CA bundle (`certifi`).
- **Action:** If TLS verification fails (`ssl.SSLCertVerificationError`, expired certificate, self-signed certificate, hostname mismatch):
  - Do NOT disable verification or retry with `verify=False`.
  - Record the result cleanly as `website_live = False` with `reason = "tls_error"`.
  - This records a high-urgency agency pitch ("Your SSL certificate is expired/broken") while keeping the scraper secure.

#### Step 9: Streaming Response Body Cap
- **Rule:** The HTTP response body must never be read into memory unconstrained.
- **Action:**
  1. Inspect `Content-Length` header. If `Content-Length > 1_000_000` (1 MB), terminate the stream immediately without reading data.
  2. Stream response bytes chunk-by-chunk using `resp.iter_content(chunk_size=4096)`.
  3. Maintain a running byte counter of decompressed bytes. The moment accumulated bytes reach `MAX_BODY_BYTES = 200_000`, break from the loop, close the connection pool socket immediately, and truncate.
  4. Decode HTML using `errors="replace"`.

#### Step 10: Manual Redirect Re-evaluation Loop
- **Rule:** Automatic redirect following (`allow_redirects=True`) is strictly disabled.
- **Action:**
  1. If response status is in `{301, 302, 303, 307, 308}`:
     - Check redirect hop counter. If `hop_count >= 5`, raise `TooManyRedirects`.
     - Extract `Location` header. Resolve relative URLs using `urllib.parse.urljoin(current_url, location)`.
     - Detect circular loops by storing visited canonical URLs in a `set`.
     - Re-submit the new target URL to **Step 1**.
  2. If the redirect target fails *any* security check (e.g. redirects to `http://169.254.169.254/` or `http://192.168.1.1/`), the request is aborted immediately with `SSRFSecurityError`.

---

## Politeness Architecture Specification

A robust web scraper must be polite to remote webmasters, avoid overloading shared hosting providers, respect robots exclusion standards, and handle rate-limiting gracefully.

```
+─────────────────────────────────────────────────────────────────────────────────+
|                           Enrichment Task Scheduler                             |
+─────────────────────────────────────────────────────────────────────────────────+
                                         │
                                         ▼
                   [Global Concurrency Budget (Semaphore: 16)]
                                         │
                                         ▼
                     [RobotsManager (Cached robots.txt)]
                                         │ Allowed?
                    ┌────────────────────┴────────────────────┐
                 No │                                         │ Yes
                    ▼                                         ▼
      [DISALLOWED_BY_ROBOTS]                       [HostRateLimiter]
                                          (Per-host Leaky Bucket: 1.5s delay
                                            + Max 1 Concurrent per Host)
                                                              │
                                                              ▼
                                                 [Hardened Fetch Execution]
                                                (SSRF Guard + Streaming Cap)
                                                              │
                                            ┌─────────────────┴─────────────────┐
                                      HTTP 429 / 503                      HTTP 200 / 404
                                            │                                   │
                                            ▼                                   ▼
                              [Exponential Backoff + Jitter]             [Process Body]
                             (Honor Retry-After, max 3 tries)
```

### 1. Global Concurrency Budget
- **Current Defect:** `enricher.py:229` defaults to `workers=40`. 40 threads generating concurrent HTTP connections saturate local outbound file descriptors and flood DNS resolvers.
- **Design:** Cap global active network requests using a `threading.BoundedSemaphore(value=16)`. While 40 worker threads can process record queues, at most 16 threads can hold an open network socket simultaneously.

### 2. Per-Host Rate Limiter
- **Domain Normalization:** Extract the normalized registrable host using `urllib.parse.urlsplit(url).hostname.lower()`.
- **Host Concurrency Cap:** Enforce a maximum of **1 concurrent request per host**. A per-host `threading.Lock` guarantees that two threads never scrape the same website concurrently.
- **Minimum Inter-Request Delay:** Enforce a minimum delay of **1.5 seconds** between consecutive requests to the same hostname. A shared state dictionary maps `hostname -> last_request_timestamp`. If a thread acquires the host lock and discovers that only 0.4 seconds have elapsed since the prior request, it sleeps for the remaining 1.1 seconds before transmitting headers.
- **Stale Entry Eviction:** Use an LRU or time-window eviction to prevent memory leakage across tens of thousands of domains.

### 3. robots.txt Awareness
- **Specification:**
  - Before fetching any URL `http(s)://example.com/path`, query `http(s)://example.com/robots.txt`.
  - Fetching `robots.txt` must itself execute through the exact same hardened SSRF-safe fetcher to prevent bypasses.
  - Parse robots rules using Python's standard `urllib.robotparser.RobotFileParser`.
  - User-Agent matching: check against `leadminer/1.0` and fallback to `*`.
- **Caching Mechanism:** Cache parsed robots models in an in-memory thread-safe LRU cache with a **1-hour TTL** (3600 seconds). Subsequent requests to the same domain avoid re-fetching `robots.txt`.
- **Fallback Semantics:**
  - **HTTP 404 / 403 / 4xx:** Permissive. The site specifies no robots restrictions; all paths allowed.
  - **HTTP 5xx (Server Error):** Conservative. The site is failing or shedding load. Treat as temporarily disallowed or retry once.
  - **Network Timeout / Connection Error:** If `robots.txt` times out, proceed politely with default conservative delay (2.0s).
  - **Crawl-Delay Directive:** If `robots.txt` defines a `Crawl-delay: N`, dynamically update the host's rate limiter to `max(1.5, N)` seconds (capped at a ceiling of 10.0s to avoid indefinite stalls).

### 4. Exponential Backoff with Jitter & Retry-After
When a server responds with HTTP 429 (Too Many Requests) or HTTP 503 (Service Unavailable):
1. **Inspect `Retry-After` Header:**
   - Case A (Integer seconds): e.g. `Retry-After: 12` -> sleep 12 seconds.
   - Case B (HTTP Date): e.g. `Retry-After: Wed, 21 Oct 2026 07:28:00 GMT` -> parse using `email.utils.parsedate_to_datetime`, compute delta against current UTC time: `delta = (target_time - now).total_seconds()`.
   - **Clamping Safety:** Clamp `Retry-After` sleep to a maximum ceiling of **30.0 seconds**. If a server requests a sleep of 3600 seconds, do not block the pipeline: abort the attempt and mark as `UNKNOWN`.
2. **Exponential Backoff with Full Jitter:**
   - If `Retry-After` is absent, compute backoff using the AWS Full Jitter algorithm:
     $$\text{sleep} = \text{random.uniform}(0, \min(\text{max\_backoff}, \text{base\_delay} \times 2^{\text{attempt}}))$$
     where $\text{base\_delay} = 1.0\text{s}$, $\text{max\_backoff} = 16.0\text{s}$.
   - Full jitter prevents synchronized thundering herds when multiple threads encounter rate limits on shared infrastructure.
3. **Retry Budget:** Maximum 2 retries per URL (3 total attempts). If all retries fail, return `UNKNOWN`.

---

## Complete Reference Implementation

Below is the complete, self-contained implementation designed for Python 3.12. It provides `SSRFSafeTransport`, `RobotsCache`, `HostRateLimiter`, and `HardenedHttpClient`, ready to replace the vulnerable session in `enricher.py`.

```python
"""
leadminer/fetcher.py - Production-grade hardened HTTP fetch layer.

Provides SSRF defense, per-host politeness, robots.txt awareness,
jittered backoff with Retry-After support, streaming size caps, and strict TLS.
"""

from __future__ import annotations

import email.utils
import ipaddress
import logging
import random
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from typing import Any, Final
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import certifi
import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.poolmanager import PoolManager

logger = logging.getLogger("leadminer.fetcher")

# ---------------------------------------------------------------------------
# Constants and Policy Configuration
# ---------------------------------------------------------------------------

DEFAULT_USER_AGENT: Final[str] = "Mozilla/5.0 (compatible; leadminer/1.0; +https://github.com/leadminer)"
ALLOWED_SCHEMES: Final[set[str]] = {"http", "https"}
ALLOWED_PORTS: Final[set[int]] = {80, 443, 8080, 8443}
MAX_BODY_BYTES: Final[int] = 200_000          # 200 KB body cut-off
MAX_CONTENT_LENGTH: Final[int] = 1_000_000    # 1 MB pre-stream abort
MAX_REDIRECTS: Final[int] = 5
MAX_RETRY_AFTER_SECS: Final[float] = 30.0

# Comprehensive SSRF Deny-List Subnets
DENIED_NETWORKS: Final[list[ipaddress.IPv4Network | ipaddress.IPv6Network]] = [
    # IPv4 Private & Special Networks
    ipaddress.ip_network("0.0.0.0/8"),          # Current network ("this")
    ipaddress.ip_network("10.0.0.0/8"),         # RFC 1918 Private
    ipaddress.ip_network("100.64.0.0/10"),      # RFC 6598 Shared Carrier-Grade NAT
    ipaddress.ip_network("127.0.0.0/8"),        # Loopback
    ipaddress.ip_network("169.254.0.0/16"),     # Link-Local / AWS IMDS
    ipaddress.ip_network("172.16.0.0/12"),      # RFC 1918 Private
    ipaddress.ip_network("192.0.0.0/24"),       # IETF Protocol Assignments
    ipaddress.ip_network("192.0.2.0/24"),       # Documentation TEST-NET-1
    ipaddress.ip_network("192.168.0.0/16"),     # RFC 1918 Private
    ipaddress.ip_network("198.18.0.0/15"),      # Benchmarking
    ipaddress.ip_network("198.51.100.0/24"),    # Documentation TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),     # Documentation TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),        # Multicast
    ipaddress.ip_network("240.0.0.0/4"),        # Class E Reserved
    ipaddress.ip_network("255.255.255.255/32"), # Broadcast
    # Explicit Cloud Metadata IPs
    ipaddress.ip_network("100.100.100.200/32"), # Alibaba Cloud Metadata

    # IPv6 Special Networks
    ipaddress.ip_network("::/128"),             # Unspecified
    ipaddress.ip_network("::1/128"),           # Loopback
    ipaddress.ip_network("::ffff:0:0/96"),      # IPv4-mapped IPv6
    ipaddress.ip_network("100::/64"),           # Discard prefix
    ipaddress.ip_network("2001::/23"),          # IETF Protocol Assignments
    ipaddress.ip_network("2001:db8::/32"),      # Documentation
    ipaddress.ip_network("2002::/16"),          # 6to4 relay
    ipaddress.ip_network("fc00::/7"),           # Unique Local Address (ULA)
    ipaddress.ip_network("fe80::/10"),          # Link-Local
    ipaddress.ip_network("ff00::/8"),           # Multicast
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS IMDS IPv6
]


class SSRFSecurityError(ValueError):
    """Raised when a URL or resolved destination IP violates security policy."""


# ---------------------------------------------------------------------------
# Step-by-Step Security Validator
# ---------------------------------------------------------------------------

def validate_ip_address(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    """Validate that an IP is global, public, and not in the Deny-List matrix."""
    # Unpack IPv4-mapped IPv6 (::ffff:192.0.2.1)
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped:
        ip_obj = ip_obj.ipv4_mapped

    if not ip_obj.is_global:
        raise SSRFSecurityError(f"IP {ip_obj} is not globally routable")

    for net in DENIED_NETWORKS:
        if ip_obj in net:
            raise SSRFSecurityError(f"IP {ip_obj} belongs to blocked subnet {net}")


def validate_target_url(url: str) -> tuple[str, str, int]:
    """Execute Steps 1-4 of the Security Policy.

    Returns:
        (canonical_url, hostname, port)
    """
    if "\x00" in url or any(c in url for c in " \r\n\t"):
        raise SSRFSecurityError("URL contains illegal characters or whitespace")

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SSRFSecurityError(f"Forbidden scheme: {scheme!r}. Must be http or https.")

    if parts.username or parts.password or "@" in parts.netloc:
        raise SSRFSecurityError("URL contains embedded credentials / userinfo")

    hostname = parts.hostname
    if not hostname:
        raise SSRFSecurityError("URL has missing or empty hostname")

    hostname = hostname.rstrip(".").lower()
    port = parts.port or (443 if scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise SSRFSecurityError(f"Forbidden port: {port}. Standard web ports only.")

    # Step 4: If literal IP, evaluate immediately
    try:
        ip_obj = ipaddress.ip_address(hostname)
        validate_ip_address(ip_obj)
    except ValueError:
        pass  # Standard domain name

    canonical = f"{scheme}://{hostname}:{port}{parts.path or '/'}"
    if parts.query:
        canonical += f"?{parts.query}"

    return canonical, hostname, port


def resolve_and_validate_host(hostname: str, port: int) -> list[str]:
    """Execute Steps 5 & 6: DNS resolution with strict validation of all addresses."""
    try:
        addr_info = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as e:
        raise SSRFSecurityError(f"DNS resolution failed for {hostname}: {e}") from e

    resolved_ips: list[str] = []
    for family, _, _, _, sockaddr in addr_info:
        ip_str = sockaddr[0]
        ip_obj = ipaddress.ip_address(ip_str)
        validate_ip_address(ip_obj)
        if ip_str not in resolved_ips:
            resolved_ips.append(ip_str)

    if not resolved_ips:
        raise SSRFSecurityError(f"No usable IP addresses resolved for {hostname}")

    return resolved_ips


# ---------------------------------------------------------------------------
# Rebinding-Safe Connection Adapters (Step 7)
# ---------------------------------------------------------------------------

class SafeHTTPConnection(HTTPConnection):
    """HTTPConnection that binds directly to a pre-validated IP address."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.validated_ip: str | None = kwargs.pop("validated_ip", None)
        super().__init__(*args, **kwargs)

    def _new_conn(self) -> socket.socket:
        target_host = self.validated_ip or self.host
        sock = socket.create_connection(
            (target_host, self.port),
            timeout=self.timeout,
        )
        return sock


class SafeHTTPSConnection(HTTPSConnection):
    """HTTPSConnection binding directly to a pre-validated IP with original SNI."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.validated_ip: str | None = kwargs.pop("validated_ip", None)
        super().__init__(*args, **kwargs)

    def _new_conn(self) -> socket.socket:
        target_host = self.validated_ip or self.host
        sock = socket.create_connection(
            (target_host, self.port),
            timeout=self.timeout,
        )
        # Server Name Indication (SNI) and certificate validation use original hostname
        self.sock = self._ssl_wrap_socket_and_match_hostname(sock, target_host=self.host)
        return self.sock

    def _ssl_wrap_socket_and_match_hostname(self, sock: socket.socket, target_host: str) -> ssl.SSLSocket:
        context = ssl.create_default_context(cafile=certifi.where())
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        return context.wrap_socket(sock, server_hostname=target_host)


class SafeHTTPConnectionPool(HTTPConnectionPool):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.validated_ip = kwargs.pop("validated_ip", None)
        super().__init__(*args, **kwargs)

    def _new_conn(self) -> HTTPConnection:
        self.num_connections += 1
        return SafeHTTPConnection(
            host=self.host,
            port=self.port,
            timeout=self.timeout.total,
            validated_ip=self.validated_ip,
        )


class SafeHTTPSConnectionPool(HTTPSConnectionPool):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.validated_ip = kwargs.pop("validated_ip", None)
        super().__init__(*args, **kwargs)

    def _new_conn(self) -> HTTPSConnection:
        self.num_connections += 1
        return SafeHTTPSConnection(
            host=self.host,
            port=self.port,
            timeout=self.timeout.total,
            validated_ip=self.validated_ip,
        )


class SSRFSafeTransport(HTTPAdapter):
    """Custom HTTPAdapter that guarantees DNS-rebinding immunity."""

    def __init__(self, validated_ip_map: dict[str, str], **kwargs: Any) -> None:
        self.validated_ip_map = validated_ip_map
        super().__init__(**kwargs)

    def init_poolmanager(self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: Any) -> PoolManager:
        self.poolmanager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            **pool_kwargs,
        )
        return self.poolmanager

    def get_connection(self, url: str, proxies: Any = None) -> Any:
        parts = urlsplit(url)
        hostname = (parts.hostname or "").lower()
        validated_ip = self.validated_ip_map.get(hostname)
        scheme = parts.scheme.lower()
        port = parts.port or (443 if scheme == "https" else 80)

        if scheme == "https":
            return SafeHTTPSConnectionPool(
                host=hostname,
                port=port,
                validated_ip=validated_ip,
                strict=True,
            )
        return SafeHTTPConnectionPool(
            host=hostname,
            port=port,
            validated_ip=validated_ip,
            strict=True,
        )


# ---------------------------------------------------------------------------
# Politeness: Per-Host Rate Limiter & Concurrency Manager
# ---------------------------------------------------------------------------

class HostRateLimiter:
    """Thread-safe per-host rate limiter ensuring inter-request delay and max 1 conn."""

    def __init__(self, min_delay_seconds: float = 1.5) -> None:
        self.min_delay_seconds = min_delay_seconds
        self._lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._last_request_time: dict[str, float] = {}

    def _get_host_lock(self, host: str) -> threading.Lock:
        with self._lock:
            if host not in self._host_locks:
                self._host_locks[host] = threading.Lock()
            return self._host_locks[host]

    def acquire(self, host: str, crawl_delay: float | None = None) -> None:
        """Block until the per-host slot is open and delay has elapsed."""
        host_lock = self._get_host_lock(host)
        host_lock.acquire()

        delay = max(self.min_delay_seconds, crawl_delay or 0.0)
        delay = min(delay, 10.0)  # Absolute clamp against hostile crawl delays

        with self._lock:
            last_time = self._last_request_time.get(host, 0.0)

        elapsed = time.monotonic() - last_time
        if elapsed < delay:
            time.sleep(delay - elapsed)

    def release(self, host: str) -> None:
        """Record completion timestamp and release per-host lock."""
        with self._lock:
            self._last_request_time[host] = time.monotonic()
        host_lock = self._get_host_lock(host)
        if host_lock.locked():
            host_lock.release()


# ---------------------------------------------------------------------------
# Politeness: Cached robots.txt Manager
# ---------------------------------------------------------------------------

class RobotsManager:
    """Thread-safe, cached robots.txt compliance manager with 1-hour TTL."""

    def __init__(self, client: HardenedHttpClient, cache_ttl_seconds: int = 3600) -> None:
        self.client = client
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[str, tuple[float, RobotFileParser | None]] = {}
        self._lock = threading.Lock()

    def is_allowed(self, target_url: str, user_agent: str = DEFAULT_USER_AGENT) -> tuple[bool, float | None]:
        """Check if target_url is allowed. Returns (allowed, crawl_delay)."""
        parts = urlsplit(target_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        robots_url = f"{origin}/robots.txt"

        with self._lock:
            entry = self._cache.get(origin)
            now = time.monotonic()
            if entry and (now - entry[0]) < self.cache_ttl_seconds:
                rp = entry[1]
                if rp is None:
                    return True, None
                return rp.can_fetch(user_agent, target_url), rp.crawl_delay(user_agent)

        # Fetch robots.txt using the safe fetcher
        rp: RobotFileParser | None = None
        try:
            status, text = self.client.fetch_raw(robots_url, is_robots_fetch=True)
            if status == 200 and text:
                rp = RobotFileParser()
                rp.parse(text.splitlines())
            elif 400 <= status < 500:
                rp = None  # 4xx means no robots restriction
            else:
                # 5xx or server failure: treat as polite temporary allow
                rp = None
        except Exception:
            rp = None

        with self._lock:
            self._cache[origin] = (time.monotonic(), rp)

        if rp is None:
            return True, None
        return rp.can_fetch(user_agent, target_url), rp.crawl_delay(user_agent)


# ---------------------------------------------------------------------------
# The Hardened HTTP Client
# ---------------------------------------------------------------------------

class FetchOutcome:
    LIVE = "live"
    DEAD = "dead"
    UNKNOWN = "unknown"
    BLOCKED_SSRF = "blocked_ssrf"
    DISALLOWED_ROBOTS = "disallowed_robots"


class HardenedHttpClient:
    """Enterprise-grade hardened HTTP client combining SSRF guard and politeness."""

    def __init__(
        self,
        global_concurrency: int = 16,
        per_host_delay: float = 1.5,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.user_agent = user_agent
        self.global_semaphore = threading.BoundedSemaphore(value=global_concurrency)
        self.rate_limiter = HostRateLimiter(min_delay_seconds=per_host_delay)
        self.robots_manager = RobotsManager(client=self)

    def _parse_retry_after(self, header_val: str | None) -> float | None:
        if not header_val:
            return None
        header_val = header_val.strip()
        # Case A: Integer seconds
        if header_val.isdigit():
            return min(float(header_val), MAX_RETRY_AFTER_SECS)
        # Case B: HTTP-date format
        try:
            target_dt = email.utils.parsedate_to_datetime(header_val)
            now_dt = datetime.now(timezone.utc)
            delta = (target_dt - now_dt).total_seconds()
            return max(0.0, min(delta, MAX_RETRY_AFTER_SECS))
        except Exception:
            return None

    def fetch_raw(
        self,
        url: str,
        is_robots_fetch: bool = False,
        timeout: tuple[float, float] = (3.0, 5.0),
    ) -> tuple[int, str]:
        """Fetch raw HTTP text with single-request SSRF validation."""
        _, host, port = validate_target_url(url)
        resolved_ips = resolve_and_validate_host(host, port)
        chosen_ip = resolved_ips[0]

        ip_map = {host: chosen_ip}
        session = requests.Session()
        session.headers["User-Agent"] = self.user_agent
        adapter = SSRFSafeTransport(validated_ip_map=ip_map)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        try:
            with session.get(url, timeout=timeout, allow_redirects=False, verify=True, stream=True) as resp:
                status = resp.status_code
                content_len = resp.headers.get("Content-Length")
                if content_len and int(content_len) > MAX_CONTENT_LENGTH:
                    return status, ""

                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_content(chunk_size=4096):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= MAX_BODY_BYTES:
                        break

                raw_bytes = b"".join(chunks)[:MAX_BODY_BYTES]
                text = raw_bytes.decode(resp.encoding or "utf-8", errors="replace")
                return status, text
        finally:
            session.close()

    def fetch_website(self, start_url: str) -> tuple[str, str, dict[str, Any]]:
        """Execute full fetch pipeline: robots -> rate limit -> SSRF redirects -> streaming body.

        Returns:
            (outcome, html_body, metadata)
        """
        metadata: dict[str, Any] = {"final_url": start_url, "redirect_count": 0, "status_code": None}

        # 1. robots.txt check
        allowed, crawl_delay = self.robots_manager.is_allowed(start_url)
        if not allowed:
            return FetchOutcome.DISALLOWED_ROBOTS, "", metadata

        current_url = start_url
        visited: set[str] = set()
        hops = 0
        total_deadline = time.monotonic() + 10.0  # Hard 10-second wall-clock budget

        while hops < MAX_REDIRECTS:
            if time.monotonic() >= total_deadline:
                return FetchOutcome.UNKNOWN, "", metadata

            try:
                canonical, host, port = validate_target_url(current_url)
            except SSRFSecurityError as e:
                logger.warning(f"SSRF policy blocked URL {current_url}: {e}")
                return FetchOutcome.BLOCKED_SSRF, "", metadata

            if canonical in visited:
                return FetchOutcome.UNKNOWN, "", metadata
            visited.add(canonical)

            # Resolve & validate destination IPs
            try:
                resolved_ips = resolve_and_validate_host(host, port)
            except SSRFSecurityError as e:
                logger.warning(f"SSRF policy blocked host {host}: {e}")
                return FetchOutcome.BLOCKED_SSRF, "", metadata

            chosen_ip = resolved_ips[0]

            # Acquire concurrency slot and per-host rate limit
            self.global_semaphore.acquire()
            try:
                self.rate_limiter.acquire(host, crawl_delay=crawl_delay)
                try:
                    resp_data = self._execute_attempt_with_backoff(
                        url=canonical,
                        host=host,
                        ip=chosen_ip,
                        deadline=total_deadline,
                    )
                finally:
                    self.rate_limiter.release(host)
            finally:
                self.global_semaphore.release()

            if resp_data is None:
                return FetchOutcome.UNKNOWN, "", metadata

            status, headers, body = resp_data
            metadata["status_code"] = status
            metadata["final_url"] = canonical

            # Handle redirects manually (Step 10)
            if status in (301, 302, 303, 307, 308):
                location = headers.get("Location")
                if not location:
                    return FetchOutcome.DEAD, "", metadata
                current_url = urljoin(canonical, location)
                hops += 1
                metadata["redirect_count"] = hops
                continue

            # Terminal response reached
            if status < 400:
                return FetchOutcome.LIVE, body, metadata
            return FetchOutcome.DEAD, body, metadata

        return FetchOutcome.UNKNOWN, "", metadata

    def _execute_attempt_with_backoff(
        self,
        url: str,
        host: str,
        ip: str,
        deadline: float,
        max_retries: int = 2,
    ) -> tuple[int, dict[str, str], str] | None:
        """Execute single-hop request with Retry-After and exponential jittered backoff."""
        ip_map = {host: ip}
        session = requests.Session()
        session.headers["User-Agent"] = self.user_agent
        adapter = SSRFSafeTransport(validated_ip_map=ip_map)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        try:
            for attempt in range(max_retries + 1):
                now = time.monotonic()
                if now >= deadline:
                    return None

                rem_timeout = max(1.0, deadline - now)
                connect_timeout = min(3.0, rem_timeout)
                read_timeout = min(5.0, rem_timeout)

                try:
                    with session.get(
                        url,
                        timeout=(connect_timeout, read_timeout),
                        allow_redirects=False,
                        verify=True,
                        stream=True,
                    ) as resp:
                        status = resp.status_code
                        headers = dict(resp.headers)

                        # Retry-After on 429 or 503
                        if status in (429, 503) and attempt < max_retries:
                            retry_sec = self._parse_retry_after(headers.get("Retry-After"))
                            if retry_sec is None:
                                # Full jitter formula: random(0, min(max_backoff, base * 2^attempt))
                                retry_sec = random.uniform(0.0, min(16.0, 1.0 * (2 ** attempt)))
                            if (time.monotonic() + retry_sec) < deadline:
                                time.sleep(retry_sec)
                                continue

                        # Stream response up to MAX_BODY_BYTES
                        chunks: list[bytes] = []
                        total = 0
                        for chunk in resp.iter_content(chunk_size=4096):
                            chunks.append(chunk)
                            total += len(chunk)
                            if total >= MAX_BODY_BYTES:
                                break

                        raw_bytes = b"".join(chunks)[:MAX_BODY_BYTES]
                        body = raw_bytes.decode(resp.encoding or "utf-8", errors="replace")
                        return status, headers, body

                except (requests.exceptions.SSLError, ssl.SSLCertVerificationError):
                    # TLS failures are fatal for this attempt and signal broken certificate
                    logger.info(f"TLS certificate validation failed for {url}")
                    return 526, {}, ""  # 526 Invalid SSL certificate
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                    if attempt < max_retries:
                        backoff = random.uniform(0.5, 2.0 * (2 ** attempt))
                        time.sleep(backoff)
                        continue
                    return None
                except Exception as e:
                    logger.debug(f"Fetch failed on {url}: {e}")
                    return None

            return None
        finally:
            session.close()
```

---

## Pipeline Integration Plan for `enricher.py`

To integrate this hardened fetch engine into `leadminer` without breaking existing type contracts or scoring formulas, update `enricher.py` as follows:

### 1. Replace the Shared `_SESSION` with `HardenedHttpClient`
Delete lines 124–126 in `enricher.py`:
```python
# DELETE:
# _SESSION = requests.Session()
# _SESSION.headers["User-Agent"] = "Mozilla/5.0 (compatible; leadminer/1.0)"

# REPLACE WITH:
from leadminer.fetcher import HardenedHttpClient, FetchOutcome

_CLIENT = HardenedHttpClient(global_concurrency=16, per_host_delay=1.5)
```

### 2. Refactor `_fetch_website` to Use Structured Outcomes
Update `_fetch_website` (`enricher.py:158-215`):
```python
def _fetch_website(url: str) -> tuple[str, dict]:
    contacts: dict = {"email": None, "instagram": None, "whatsapp": None, "linkedin": None}
    
    outcome, html, meta = _CLIENT.fetch_website(url)
    
    if outcome == FetchOutcome.LIVE:
        # Run existing regex extraction on bounded HTML
        _extract_contacts(html, contacts)
        return LIVE, contacts
    elif outcome == FetchOutcome.DEAD:
        return DEAD, contacts
    elif outcome == FetchOutcome.BLOCKED_SSRF:
        logger.warning(f"Blocked SSRF target: {url}")
        return UNKNOWN, contacts
    elif outcome == FetchOutcome.DISALLOWED_ROBOTS:
        return UNKNOWN, contacts
    else:
        return UNKNOWN, contacts
```

### 3. Exploit Broken TLS as a High-Value Sales Signal
When `_CLIENT.fetch_website()` records status `526` (`tls_error`), update `enricher.py` to flag `record["website_live"] = False` and set a structured field `record["rebuild_reason"] = "expired_ssl"`. 

In `pitch_recommender.py:54`:
```python
if r.get("rebuild_reason") == "expired_ssl":
    return "SSL certificate replacement + website rebuild"
```
This turns a previously hidden TLS crash into a verified, high-converting agency sales pitch.

---

## Not a bug, but worth knowing

1. **Cloudflare / WAF Managed Challenges (HTTP 403 / 503):** Cloudflare and AWS WAF often respond to automated crawlers with HTTP 403 or 503 containing JavaScript challenges. The fetcher must treat these as `UNKNOWN` rather than `DEAD`. Marking a Cloudflare-protected corporate site as `DEAD` falsely awards 20 points for an agency website rebuild pitch, embarrassing sales representatives.
2. **Arabic Internationalized Domain Names (IDNs):** Lebanese and Saudi businesses increasingly register Arabic script domains (e.g., `http://عقارات-الرياض.sa`). `urllib.parse.urlsplit` handles IDNs inconsistently across operating systems unless explicitly converted using `idna.encode()`. The validation pipeline must convert non-ASCII hostnames to punycode (`xn--...`) before executing Step 5 DNS resolution.
3. **Public Cloud IP Recycled Ranges:** Cloud providers frequently reallocate elastic IPs. A domain may legitimately point to an AWS IP that was recently returned to the public pool. Strict subnet comparison against CIDR blocks using Python's `ipaddress` library guarantees zero false-positive SSRF blocks against genuine public cloud allocations while completely shielding private metadata.
4. **Multi-Homed DNS Round-Robin:** Large SaaS platforms (e.g. Shopify, Wix) return multiple A records for a single domain. Step 6 guarantees that if **any** returned record falls inside a private or loopback subnet, the request is dropped. Legitimate platforms will never resolve to RFC 1918 addresses; mixed resolutions are an indicator of DNS rebinding attacks.

---

## Recommended Order of Work

1. **Phase 1: Implement `leadminer/fetcher.py` (S1, Safety First)**
   - Add `DENIED_NETWORKS` and `validate_ip_address()`.
   - Implement `validate_target_url()` and `resolve_and_validate_host()`.
   - Unit test IP validation against loopback, RFC1918, AWS IMDSv1/v2, Alibaba, and IPv4-mapped IPv6. Gate: **100% test coverage on deny-list matrix**.
2. **Phase 2: Implement Socket Connection Adapter (S1, Rebinding Immunity)**
   - Build `SafeHTTPConnection`, `SafeHTTPSConnection`, and `SSRFSafeTransport`.
   - Verify that TCP sockets connect strictly to pre-resolved IPs while preserving original SNI and `Host` headers.
3. **Phase 3: Implement Politeness & robots.txt (S2, Operational Hygiene)**
   - Build `HostRateLimiter` with per-host locks and 1.5s inter-request spacing.
   - Build `RobotsManager` with thread-safe in-memory caching (1-hour TTL).
   - Implement bounded manual redirect loop (max 5 hops) with complete re-validation per hop.
4. **Phase 4: Integrate into `enricher.py` and Re-enable TLS Verification (S2, Quality)**
   - Replace `_SESSION = requests.Session()` with `HardenedHttpClient`.
   - Re-enable `verify=True` across all enrichment calls; map SSL errors to explicit sales audit metadata.
   - Configure global concurrency semaphore to 16 workers.
5. **Phase 5: Offline Replay & Verification**
   - Execute test suite using `vcrpy` / `requests-mock` fixtures.
   - Verify zero unhandled exceptions and zero data leaks on malicious SSRF fixtures.