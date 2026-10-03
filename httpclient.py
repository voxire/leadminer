"""
Shared HTTP client for every outbound request.

Exists because the same three problems were reimplemented (badly) in each
module:

1. **Thread safety.** enricher.py held a single module-level
   `requests.Session` and fired it at 40 threads. `requests.Session` is
   documented as not thread-safe: its cookie jar, header state and urllib3
   connection pool are all mutated per request. Sessions are now thread-local.

2. **No real retry.** osm.py and wikidata.py each had a hand-rolled loop
   using bare `time.sleep`, which retries at a fixed interval with no jitter,
   so N workers retry in lockstep and hammer the host together. Retries are
   now centralised with exponential backoff and full jitter.

3. **Failures were indistinguishable.** Everything was `print` then a silent
   `return`, so a source that stopped working looked exactly like a source
   that had nothing to return. Callers now get a typed outcome.

Usage:

    with http_session() as session:
        resp = session.get(url, timeout=8)

    # with retries:
    result = fetch_with_retry(session, "GET", url)
    if result.ok:
        ...
"""

from __future__ import annotations

import email.utils
import logging
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 1.0
MAX_BACKOFF = 60.0

# Default cap on how much of a response body we will read. A hostile or
# misconfigured host can otherwise stream indefinitely into memory.
DEFAULT_MAX_BODY_BYTES = 200_000

# One Session per thread. Cheap to create, and this is the documented
# requirement rather than the old shared-and-mutable version.
_local = threading.local()


def get_session() -> requests.Session:
    """Return this thread's Session, creating it on first use."""
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({
            "User-Agent": _user_agent(),
            "Accept-Language": "en,ar;q=0.9",
        })
        _local.session = session
    return session


def _user_agent() -> str:
    # Overpass and Wikidata both ask for a contact address in the UA, and
    # Overpass's policy explicitly says commercial use should self-host. Being
    # identifiable is both the polite thing and the compliant thing.
    try:
        import os

        email = os.environ.get("SCRAPER_EMAIL", "")
    except Exception:
        email = ""
    tail = f" ({email})" if email else ""
    return f"leadminer/0.2 (+https://github.com/voxire/leadminer){tail}"


@contextmanager
def http_session():
    """Context-managed access to the thread-local Session."""
    yield get_session()


@dataclass
class FetchResult:
    """Outcome of a request, including the transport failures.

    `status` of None with an `error` set means we never reached the server.
    Callers must not treat that as the server saying no.
    """

    ok: bool
    status: int | None = None
    body: bytes = b""
    error: str | None = None
    attempts: int = 0
    elapsed: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        import json as _json

        return _json.loads(self.body.decode("utf-8", errors="replace"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def _retry_after(headers: Any) -> float | None:
    """Parse Retry-After, which may be seconds or an HTTP date."""
    raw = headers.get("Retry-After") if headers else None
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    delta = (parsed - now).total_seconds()
    return max(0.0, delta)


def _should_retry(status: int | None) -> bool:
    """429 and 5xx are worth retrying; 4xx generally is not."""
    if status is None:
        return True  # transport failure
    return status == 429 or 500 <= status < 600


def fetch_with_retry(
    method: str,
    url: str,
    *,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
    max_bytes: int | None = None,
    **kwargs: Any,
) -> FetchResult:
    """Perform a request with exponential backoff and full jitter.

    `max_bytes` caps how much of the body is read, so a hostile or
    misconfigured host cannot stream an unbounded response into memory.
    """
    session = session or get_session()
    last_error: str | None = None
    last_status: int | None = None
    started = time.monotonic()

    for attempt in range(1, retries + 1):
        retry_hint: float | None = None
        try:
            resp = session.request(
                method, url, timeout=timeout, stream=max_bytes is not None, **kwargs
            )
        except requests.RequestException as e:
            last_error = f"{type(e).__name__}: {e}"
            log.debug("%s %s attempt %d/%d transport error: %s",
                      method, url, attempt, retries, last_error)
        else:
            with resp:
                if not _should_retry(resp.status_code):
                    body = resp.raw.read(max_bytes) if max_bytes else resp.content
                    return FetchResult(
                        ok=resp.status_code < 400,
                        status=resp.status_code,
                        body=body or b"",
                        attempts=attempt,
                        elapsed=time.monotonic() - started,
                        headers=dict(resp.headers),
                    )
                last_error = f"HTTP {resp.status_code}"
                last_status = resp.status_code
                retry_hint = _retry_after(resp.headers)

        if attempt < retries:
            if retry_hint is None:
                # Full jitter: uniform in [0, backoff * 2^n]. Prevents N
                # workers from retrying in lockstep and hammering the host
                # in a thundering herd.
                retry_hint = random.uniform(
                    0, min(MAX_BACKOFF, backoff * (2 ** (attempt - 1)))
                )
            log.debug("%s %s retrying in %.1fs (attempt %d/%d)",
                      method, url, retry_hint, attempt, retries)
            time.sleep(retry_hint)

    return FetchResult(
        ok=False,
        # Preserve the last observed status so a caller can tell "the server
        # said 500" apart from "we never reached the server". Both are
        # failures, but they need different investigation.
        status=last_status,
        error=last_error,
        attempts=retries,
        elapsed=time.monotonic() - started,
    )


def utc_now_iso() -> str:
    """Timezone-aware UTC timestamp.

    Replaces `datetime.datetime.utcnow()`, deprecated in Python 3.12. The
    naive value it returned also sorted incorrectly against any offset-aware
    timestamp in dedup._merge's string comparison.
    """
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat()