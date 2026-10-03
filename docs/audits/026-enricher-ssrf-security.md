# 026 — Enricher SSRF & resource-exhaustion surface

## Verdict

`enricher._fetch_website()` issues an **unauthenticated, outbound `GET` to an
arbitrary URL that came straight out of untrusted, crowdsourced scraped data**,
with TLS verification disabled and no scheme/host/address/redirect policy. Any
OSM/Wikidata/Google contributor can point the `website` field at loopback,
RFC1918, or the cloud metadata IP and the tool becomes an SSRF proxy; separately,
the `200_000`-byte "cap" is applied *after* `r.text` has already fully downloaded
and decompressed the body, so a gzip bomb OOMs the run. Both are S1. The fix is a
hardened fetch with an explicit allow/deny policy (schemes, ports, and
`ipaddress.is_global` on the *connected* peer, re-checked per redirect hop),
streamed reads with a true byte cap, `verify=True`, and a bounded time budget.

## Where the input comes from (all untrusted)

The `website` field that reaches `_fetch_website` is attacker-influenced at three
upstream points:

| Source | Line | How `website` is set |
|---|---|---|
| OSM tags | `scrapers/osm.py:88` | `website` / `contact:website` / `url` — any OSM editor can write any string |
| Wikidata | `scrapers/wikidata.py:66` | `wdt:P856` "official website" — any editor |
| Google Places | `scrapers/google_places.py:259` | `websiteUri` — returned by Google, but not validated |

Both OSM (`osm.py:92-93`) and Wikidata (`wikidata.py:72-73`) only do
`if not website.startswith("http"): website = "https://" + website`. That is a
*prefix*, not a scheme allow-list: `http://169.254.169.254/…`, `http://127.0.0.1:8000/…`,
`http://10.0.0.5/…` all pass through untouched. `enricher` does **no** further
validation and is the last line of defense.

## Findings

### S1 — Unrestricted SSRF: arbitrary `GET` to loopback / RFC1918 / cloud metadata / internal hosts

- **Where:** `enricher.py:146` (`r = _SESSION.get(url, timeout=8, allow_redirects=True, verify=False)`), fed by the `website` field above.
- **Breaks:** the runner acts as an HTTP proxy that an anonymous data contributor can aim anywhere the runner can reach. Concretely: internal port scanning (`website_live` is a boolean oracle for "TCP connect + HTTP status < 500"), probing internal services on the runner's network, and — if this ever runs on AWS/Azure/GCP or a self-hosted runner — pulling instance metadata / credentials. A limited exfil channel exists: anything an internal response body happens to match (`_EMAIL_RE` at `enricher.py:127`, `_INSTAGRAM_RE`, `_LINKEDIN_RE`) is written to the CSVs and uploaded to Google Drive (`scrape.yml:49-50`).
- **Trigger:** a malicious or merely typosquatting `website` value. Examples that all reach `_fetch_website` unchanged:
  - `http://169.254.169.254/latest/meta-data/` (AWS link-local; also `http://metadata.google.internal/…` on GCP, `http://169.254.169.254/metadata/…` on Azure)
  - `http://127.0.0.1:6379/`, `http://localhost:8545/`, `http://127.0.0.1:9200/`
  - `http://192.168.1.1/`, `http://10.0.0.2:8080/`, `http://172.16.0.1/`
  - `http://[::1]/` (IPv6 loopback — no IPv6 guard exists either)
- **Fix:** an allow/deny policy before any socket is opened (see **Hardened fetch** below) — reject non-http(s) schemes, reject literal non-global IPs, and re-validate the *connected* peer's IP as global.

Attack-surface enumeration requested for this lens:

| Vector | Reachable via `requests` today? | Notes |
|---|---|---|
| `file://` scheme | **No** — `requests` raises `InvalidSchema` (caught by `except Exception` at `enricher.py:151`). Blocked *by coincidence*, not policy | If the code ever switches to `urllib`/`httpx`/`curl` or an adapter is added, `file:///etc/passwd` becomes readable. Not a reason to skip an explicit scheme allow-list. |
| Cloud metadata `169.254.169.254` | **Yes** (if reachable) | `requests` happily dials link-local. IMDSv2 (AWS), `Metadata-Flavor: Google`, and Azure's `Metadata: true` all reject the plain headerless GET here, so hardened endpoints return 4xx — but legacy IMDSv1, misconfigured proxies, and non-cloud link-local services are not. The workflow runs on `ubuntu-latest` (`scrape.yml:10`); GitHub-hosted runners reportedly block IMDS, but that is an environmental accident, not a control in this code, and does not hold for a self-hosted runner or a future VM/VPC deployment. |
| Loopback `127.0.0.0/8`, `::1` | **Yes** | No guard. `_SESSION.get("http://127.0.0.1:…")` connects. |
| RFC1918 `10/8`, `172.16/12`, `192.168/16` | **Yes** | No guard. |
| Link-local/reserved/CGNAT `100.64/10`, `192.0.0.0/24`, multicast | **Yes** | No guard. |
| DNS rebinding | **Yes** | A hostname that resolves to a public IP for the policy check and to `127.0.0.1` for the connect defeats any "resolve once, then fetch" check. Needs re-validation of the actual socket peer (see design). |
| Redirect into internal network | **Yes** | `allow_redirects=True` with requests' default `max_redirects=30`. A benign `http://public.example/` can 302 to `http://169.254.169.254/…` or `http://192.168.1.1/`. Each hop must be re-validated. |
| Decompression bomb | **Yes** | See S1 below. |

### S1 — Illusory 200 KB cap: body is fully downloaded *and decompressed* before slicing

- **Where:** `enricher.py:150` — `html = r.text[:200_000]`
- **Breaks:** `r.text` first calls `r.content`, which (with the default `stream=False`) reads and gzip/deflate-decompresses the **entire** response into memory, *then* the slice keeps 200 KB. The cap constrains nothing about memory; it only limits what gets regex-scanned. A `Content-Encoding: gzip` response that decompresses to hundreds of MB (a "gzip bomb", trivially crafted and cheap to host) costs ~2× that in RAM (bytes + decoded `str`). With `workers=40` (`enricher.py:180`) all fetching concurrently, a handful of hostile URLs OOMs the runner and kills the whole run — no exception, no partial CSV.
- **Trigger:** `website` → `http://attacker.example/bomb` returning a 10 MB gzip file that decompresses to ~10 GB of HTML.
- **Fix:** `stream=True` + `iter_content(...)` with a hard byte cap that **breaks and closes the connection once the cap is reached** (cap is applied to the decompressed stream, so a bomb is truncated at ~200 KB, not expanded). See design.

### S2 — `verify=False` disables TLS verification for every fetch

- **Where:** `enricher.py:146` (`verify=False`) and the cosmetic suppression at `enricher.py:268` (`urllib3.disable_warnings(...)`).
- **Breaks:** the liveness/contact pass now trusts any certificate, so a MITM (or a network that intercepts TLS) can inject arbitrary content that flows into `email`/`instagram`/`whatsapp`/`linkedin` and then into the CSVs/Drive — meaning a tampered website also poisons the sales output. It also makes "website is live" mean "website answered with a self-signed cert", silently upgrading dead/malicious sites to live. This is a GitHub-Actions-authenticated outbound flow with an rclone token in the environment (`scrape.yml:29-33`), so the bar for hygiene is high.
- **Trigger:** any TLS site with a bad/mismatched cert is accepted as live; any on-path attacker rewrites the body.
- **Fix:** delete `verify=False` (default `verify=True`). If some scraped sites genuinely use bad certs, that is a data-quality signal worth *recording*, not a reason to disable verification globally.

### S2 — `timeout=8` is a per-socket-op timeout, not a budget: slow-drip hosts pin the 40-thread pool

- **Where:** `enricher.py:146` (`timeout=8`).
- **Breaks:** a single float is applied as both connect and *read* timeout; the read timeout resets on every received byte. A server that dribbles one byte every 7 s holds a worker open indefinitely. With 40 workers (`enricher.py:180`) and a few thousand targets (`enricher.py:186`), a dozen hostile/slow URLs can stall the whole enrichment pass for the 300-minute workflow cap (`scrape.yml:11`) or until the run is killed.
- **Trigger:** `website` → a host that accepts the connection and sends a trickle.
- **Fix:** use an explicit `(connect, read)` tuple and add a **total wall-clock deadline** per fetch (a `threading.Timer`/`signal`-based watchdog, or an HTTP client with a total-timeout primitive). `timeout=8` → `timeout=(4, 10)` plus a ~15 s total budget.

### S3 — No explicit allow/deny policy in the one place that matters; upstream "normalization" is a prefix, not validation

- **Where:** `enricher.py:142-152` (no validation), `scrapers/osm.py:92-93` and `scrapers/wikidata.py:72-73` (prefix-only).
- **Breaks:** the scheme "normalization" is cosmetic. `ftp://x`, `file://x`, `ssh://x` become `https://ftp://x` etc. — garbage URLs that fail and waste a fetch; conversely real threats like `http://169.254.169.254/` sail through because they already start with `http`. There is no single choke point that defines what is fetchable.
- **Fix:** centralize policy in the fetch function (below); leave scrapers doing only light cleanup.

## Hardened fetch function

Policy in one place, and *defense in depth* against rebinding: fail fast on the
URL, then re-validate the **actual connected peer IP** (not a pre-connect
resolution) inside a custom adapter, which urllib3 invokes once per new
connection — i.e. on every redirect to a new host.

```python
import ipaddress
import requests
from requests.adapters import HTTPAdapter
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443}
MAX_RESPONSE_BYTES = 200_000          # the only place "200 KB" is enforced
CONNECT_TIMEOUT, READ_TIMEOUT = 4.0, 10.0
MAX_REDIRECTS = 5


class SSRFGuardAdapter(HTTPAdapter):
    """Re-validate the *connected* socket peer. urllib3 calls `cert_verify`
    after the socket is connected and before the request bytes are written, and
    does so once per new connection — so every redirect to a new host is
    re-checked. Checking `conn.sock.getpeername()` defeats DNS rebinding: we
    inspect the real peer, not the earlier name resolution."""

    def cert_verify(self, conn, url, verify, cert):
        try:
            peer_ip = conn.sock.getpeername()[0]
        except (AttributeError, OSError):
            raise requests.exceptions.ConnectionError("no peer address")
        if not ipaddress.ip_address(peer_ip).is_global:
            raise requests.exceptions.ConnectionError(
                f"refusing non-global peer address {peer_ip}"
            )
        return super().cert_verify(conn, url, verify, cert)


def assert_url_allowed(url: str) -> None:
    """Fast-fail on obviously disallowed targets. Hostnames are still
    re-validated against the connected peer by SSRFGuardAdapter."""
    p = urlparse(url)
    if p.scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"disallowed scheme {p.scheme!r}")
    if not p.hostname:
        raise ValueError("URL has no hostname")
    port = p.port or (443 if p.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise ValueError(f"disallowed port {port}")
    try:
        ip = ipaddress.ip_address(p.hostname)
    except ValueError:
        return                      # hostname — checked at connect time
    if not ip.is_global:
        raise ValueError(f"disallowed literal address {ip}")


def _fetch_website(session: requests.Session, url: str):
    contacts = {"email": None, "instagram": None, "whatsapp": None, "linkedin": None}
    try:
        assert_url_allowed(url)
    except (ValueError, requests.exceptions.RequestException):
        return False, contacts

    try:
        r = session.get(
            url,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            allow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            verify=True,             # never verify=False
            stream=True,             # do NOT let r.text buffer the whole body
        )
    except requests.exceptions.RequestException:
        return False, contacts

    if r.status_code >= 400:         # 4xx/5xx -> not live (see note below)
        r.close()
        return False, contacts

    # Stream with a hard cap on *decompressed* bytes; close early on a bomb.
    data = bytearray()
    try:
        for chunk in r.iter_content(chunk_size=65536):
            data.extend(chunk)
            if len(data) >= MAX_RESPONSE_BYTES:
                break                 # truncate the bomb at the socket, don't expand it
        html = bytes(data[:MAX_RESPONSE_BYTES]).decode(
            r.encoding or "utf-8", errors="replace"
        )
    except requests.exceptions.RequestException:
        return False, contacts
    finally:
        r.close()

    # ... unchanged regex extraction over `html` ...
    return True, contacts


# One-time setup (replaces the module-global _SESSION at enricher.py:124-125):
def build_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (compatible; leadminer/1.0)"
    s.mount("http://", SSRFGuardAdapter())
    s.mount("https://", SSRFGuardAdapter())
    return s
```

Design notes for the implementer:

1. **`ipaddress.is_global` is the single source of truth** for "public". It rejects loopback, RFC1918, link-local (`169.254.0.0/16`), CGNAT (`100.64.0.0/10`), multicast, reserved, and unspecified — including IPv6 equivalents — without hand-maintaining a CIDR list. If a legitimately public but unusual range must be allowed, maintain an explicit `EXTRA_ALLOWED` allow-list instead of weakening the deny side.
2. **Rebinding vs. this design.** `assert_url_allowed` rejects literal non-global IPs up front. The `SSRFGuardAdapter` re-checks `conn.sock.getpeername()` for *every* new connection, so a hostname that re-resolves to `127.0.0.1` between check and connect is still caught at the socket. The one thing to **verify against the pinned `urllib3`** is that `cert_verify` runs post-connect in the exact version in use (it does in the urllib3 that `requests==2.32.3` pins); if not, override `send()` instead, or connect to a pre-validated IP while pinning `Host`/SNI.
3. **Redirects** are bounded to 5 hops and every hop to a new host is re-validated by the adapter. Same-host redirects reuse the already-validated connection.
4. **`max_redirects`** is a valid `requests.get` kwarg (forwarded to `HTTPAdapter.send`); the adapter re-check covers each hop.
5. **Total time budget** is not expressible with `timeout` alone. Add a watchdog (threading timer raising into the fetch, or an `httpx`-style total timeout) so a slow-drip peer can't hold a worker beyond ~15 s total even while dribbling within the 10 s read timeout.

## Not a bug, but worth knowing

- **`live = r.status_code < 500` treats 4xx as live** (`enricher.py:147`). A 404/410/401 site is recorded `website_live=True`, which flows into `lead_score` (`enricher.py:241-244`) and `pitch_recommender`. This is a correctness question outside the SSRF lens, but note the hardened function above *changes* it to `status_code >= 400 → not live`; confirm that semantic shift is intended before adopting, or preserve the original "4xx is live" policy if that is deliberate.
- **`except Exception` swallows everything** (`enricher.py:151`), so a policy rejection and a network error are indistinguishable and silent. Consider logging the specific failure class at debug level so misconfigured/scraped-garbage URLs are visible.

## Recommended order of work

1. **S1 — Decompression bomb / real cap** (smallest, always-triggerable, kills runs): switch to `stream=True` + capped `iter_content` with early close.
2. **S1 — SSRF allow/deny** (`assert_url_allowed` + `SSRFGuardAdapter`), since it is the core data-integrity/abuse risk and cheap to add once the streaming change is in.
3. **S2 — drop `verify=False`** (and the `disable_warnings` call), defaulting to `verify=True`.
4. **S2 — bounded time budget** (explicit `(connect, read)` + total-deadline watchdog).
5. **S3 — centralize** the scheme/host policy in the one fetch function and stop relying on the prefix-based normalization in `osm.py`/`wikidata.py`.
