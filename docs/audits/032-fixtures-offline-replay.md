# 032 — Offline, deterministic testability (fixtures + conftest design)

## Verdict

The pipeline is testable offline **today with zero source changes**, but only by
monkeypatching six different seams scattered across four modules, which makes the
test suite fragile and the "zero network" property unenforced. The single highest
leverage move is an autouse **socket guard** in `conftest.py` that turns "we promise
not to hit the network" into a hard failure, plus a tiny injectable-clock seam
(`now=` param) in the three scrapers so `scraped_at` is deterministic without
depending on `freezegun`'s thread behavior.

The unit-testable surface (dedup, whitelist, `lead_score`, `infer_region`,
`recommend_service`, `load_master`, the three parser loops) is large and pure —
**most of the value is cheap**. Only the four HTTP call sites and the
orchestration order in `main.main()` need stubbing.

---

## Testability blockers — the seams to cut

These are the places the current code prevents (or merely makes awkward) a
zero-network, deterministic test. Severity here means "how much it blocks offline
testing," not "how wrong the output is."

### S1 — No injection point for the clock
- **Where:** `scrapers/osm.py:31`, `scrapers/wikidata.py:31`, `scrapers/google_places.py:162` — each does `datetime.datetime.utcnow().isoformat() + "Z"` inline.
- **Breaks:** `scraped_at` is a microsecond-precision wall-clock value baked into every record. Golden CSVs can never be byte-stable, and `dedup._merge` (`dedup.py:57-58`) does `max(av, bv)` on those strings, so merge order also drifts.
- **Trigger:** any test that asserts on a full record or on a golden CSV.
- **Fix:** add an optional `now` parameter to each scraper's `scrape()` (below). Backward-compatible; `base.py` does not need to change because the default is `None`.

### S1 — HTTP clients are constructed inline; URLs are module constants
- **Where:** `scrapers/osm.py:34` (`session = requests.Session()`), `scrapers/wikidata.py:42` (`requests.get(...)`), `scrapers/google_places.py:148` (`_make_session`), `enricher.py:124` (`_SESSION = requests.Session()` at import time).
- **Breaks:** there is no seam to hand a fake transport to any of them. `enricher._SESSION` is created at *import* time, before any test fixture runs, so you must patch `enricher._SESSION` or `enricher._fetch_website` after import.
- **Trigger:** importing `enricher` or `scrapers.*` then trying to inject a cassette/fake.
- **Fix:** two acceptable paths. (a) Zero-change: patch the module-level `OVERPASS_URL` / `SPARQL_ENDPOINT` / `API_URL` constants plus `enricher._fetch_website`. (b) Recommended seam: let `__init__` accept a `session` / `base_url`, and give `_fetch_website` its session as a parameter. Both are specified below; (a) is what the fixture recipes use so nothing in `scrapers/` changes.

### S1 — `main.main()` is a hardwired procedure
- **Where:** `main.py:93-153` — constructs `scrapers = [OSMScraper(), WikidataScraper(), GooglePlacesScraper()]`, reads the module-global `DATA_DIR`, and runs its own `ThreadPoolExecutor`.
- **Breaks:** end-to-end replay requires monkeypatching `main.OSMScraper`, `main.WikidataScraper`, `main.GooglePlacesScraper`, `main.DATA_DIR`, and `enricher._fetch_website` all at once. It works, but the test encodes implementation details rather than behavior.
- **Trigger:** wanting one integration test that runs scrape→filter→dedup→enrich→score→5 CSVs offline.
- **Fix:** extract `run(scrapers, data_dir, now=None)` from `main()` (the only *recommended* source change in this report) OR patch the five names in a single `fake_scrapers` fixture (shown below). The fixture route keeps `main.py` read-only.

### S2 — Non-deterministic ordering breaks byte-stable goldens
- **Where:** `main.py:105-111` (`as_completed` over 3 scraper futures → `raw.extend`), `scrapers/google_places.py:183-188` (`as_completed` over 5 workers → `all_records`).
- **Breaks:** `raw` order varies run to run, and `dedup.dedup` (`dedup.py:68-97`) builds dicts in insertion order, so output row order varies. Golden-CSV comparison must be order-insensitive, or the executor must be forced serial.
- **Trigger:** `test_pipeline_replay` comparing `data/all_businesses.csv` to a committed golden file.
- **Fix:** compare CSVs as sorted multisets (the `assert_csv_equal` helper below), and/or monkeypatch `concurrent.futures.ThreadPoolExecutor` to run inline for replay. `enricher.check_websites` is already order-stable — it mutates `records[idx]` (`enricher.py:194`), never reorders.

### S2 — Real `time.sleep` backoff would drag every test
- **Where:** `scrapers/osm.py:50` (30s), `scrapers/wikidata.py:53` (15s), `scrapers/google_places.py:215` (30s) and `:278` (2s between pages).
- **Breaks:** exercising the retry/pagination paths costs wall-clock seconds per test.
- **Trigger:** any scraper test with a stub returning 500-then-200, or a Places fixture with `nextPageToken`.
- **Fix:** autouse `_no_sleep` fixture patching `time.sleep` to a no-op (all modules call `time.sleep`, which resolves through the shared `time` module object).

### S3 — Env reads are inline and one is cached at construction
- **Where:** `scrapers/google_places.py:146` (`self._api_key = os.environ.get(...)` in `__init__`), `scrapers/osm.py:35` / `scrapers/wikidata.py:34` (`SCRAPER_EMAIL`).
- **Breaks:** a test must set the env var *before* instantiating `GooglePlacesScraper`; instantiating at module scope, then setting env, silently tests the "skipping" branch.
- **Trigger:** a test that wants to exercise the `if not self._api_key: ... return` guard (`google_places.py:158-160`) versus the real path.
- **Fix:** trivial to control via `monkeypatch.setenv`, but document the construction-order trap; prefer passing the key to `__init__`.

### S3 — `scraped_at` merge depends on a single string format
- **Where:** `dedup.py:57-58` — `merged["scraped_at"] = max(av, bv)` (lexicographic).
- **Breaks:** correct only because all three scrapers emit the identical `...T...Z` format. If one source ever emits a different format (e.g. tz-aware `+00:00`, or no `Z`), chronological order breaks silently.
- **Trigger:** future scrapers emitting non-UTC timestamps.
- **Fix:** freeze the clock in tests (makes it deterministic regardless), and separately recommend parsing timestamps before `max` — belongs to the dedup audit, not this one.

---

## Directory layout

```
leadminer/
├── pyproject.toml                 # [tool.pytest.ini_options] (pythonpath, markers)
├── requirements.txt               # unchanged (runtime only)
├── requirements-dev.txt           # pytest + freezegun + vcrpy (+ optional requests-mock)
├── main.py, dedup.py, enricher.py, pitch_recommender.py, scrapers/   # read-only
├── data/                          # gitignored runtime output; NEVER written by tests
└── tests/
    ├── conftest.py                # THE shared fixture file (full listing below)
    ├── unit/
    │   ├── test_dedup.py
    │   ├── test_whitelist.py
    │   ├── test_enricher_pure.py      # completeness_score, lead_score, infer_region
    │   ├── test_pitch_recommender.py
    │   └── test_main_helpers.py       # load_master, write_csv, has_any_contact
    ├── scrapers/
    │   ├── test_osm.py                # parser vs local Overpass stub
    │   ├── test_wikidata.py           # parser vs local SPARQL stub
    │   └── test_google_places.py      # parser vs fake Places API (incl. pagination)
    ├── integration/
    │   ├── test_enricher_websites.py  # _fetch_website parsing, offline
    │   └── test_pipeline_replay.py    # full main.main() → 5 golden CSVs
    └── fixtures/
        ├── csv/                       # GOLDEN CSV FIXTURES (committed)
        │   ├── master_input.csv               # seeds data/all_businesses.csv
        │   ├── expected_all_businesses.csv
        │   ├── expected_qualified_businesses.csv
        │   ├── expected_with_websites.csv
        │   ├── expected_without_websites.csv
        │   └── expected_sales_ready.csv
        └── http/                      # RECORDED / STUB HTTP FIXTURES (committed)
            ├── cassettes/             # vcrpy .yaml (redacted) — RECORDED ONCE
            │   ├── osm_lebanon.yaml
            │   ├── wikidata_lebanon.yaml
            │   └── google_places_lebanon.yaml
            ├── overpass/
            │   └── lebanon_nodes_ways.json    # golden Overpass response body
            ├── wikidata/
            │   └── lebanon_bindings.json      # golden SPARQL results JSON
            ├── places/
            │   ├── lebanon_page1.json         # fake Places API page 1 (has nextPageToken)
            │   └── lebanon_page2.json         # page 2 (no token)
            └── websites/                       # HTML bodies for _fetch_website
                ├── live_with_email.html
                ├── live_with_socials.html
                └── (…)
```

Notes on the tree:

- `tests/` must **not** contain `__init__.py` — with `pythonpath = ["."]`
  (pytest ≥ 7.0) the repo root is on `sys.path`, so `import main`, `import dedup`,
  and `from scrapers.osm import OSMScraper` all resolve regardless of the CWD the
  runner starts from.
- `requirements-dev.txt` (do not add these to runtime `requirements.txt`):

  ```
  -r requirements.txt
  pytest==8.2.2
  freezegun==1.4.0
  vcrpy==6.0.1
  requests-mock==1.12.1   # optional: lighter than vcr for targeted single-request tests
  ```

- `pyproject.toml` pytest section:

  ```toml
  [tool.pytest.ini_options]
  testpaths = ["tests"]
  pythonpath = ["."]
  markers = [
      "unit: pure functions, no network, no clock",
      "scraper: parser tests against local stubs",
      "integration: full offline pipeline replay",
  ]
  filterwarnings = [
      "ignore::urllib3.exceptions.InsecureRequestWarning",
  ]
  ```

---

## `tests/conftest.py` — full design

This file is the contract for "zero network, deterministic." Every fixture below
is referenced by the recipes in the next section.

```python
"""
tests/conftest.py — shared fixtures for offline, deterministic leadminer tests.

Guarantees enforced at suite level:
  1. ZERO real network: an autouse socket guard raises on any non-loopback
     connect, so a regression that reintroduces a live `requests.get` fails
     loudly instead of leaking to the internet.
  2. Deterministic clock: `frozen_time` freezes `datetime.utcnow()` so every
     scraper stamps the identical `scraped_at`.
  3. No sleeping: `time.sleep` is neutralised so retry/backoff runs fast.
  4. Local in-process HTTP stubs for Overpass, Wikidata, the Places API, and
     per-URL website handlers.
"""
from __future__ import annotations

import csv
import http.server
import json
import pathlib
import socket
import threading
import time
from typing import Callable

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
CSV_DIR = FIXTURES / "csv"
HTTP_DIR = FIXTURES / "http"
CASSETTE_DIR = HTTP_DIR / "cassettes"

FROZEN_NOW = "2024-01-15T12:00:00"          # whole-second → isoformat has no µs
FROZEN_SCRAPED_AT = FROZEN_NOW + "Z"        # == "...T12:00:00Z" (osm.py:31 format)

# ---------------------------------------------------------------------------
# 1. Zero-network enforcement (autouse)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens a TCP connection to a non-loopback host.

    Loopback stays open so the local stub HTTP server (bound to 127.0.0.1)
    keeps working. This is what turns "we promise no network" into a property
    of the suite rather than a line in a README.
    """
    real_connect = socket.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, (tuple, list)) else str(address)
        if host in ("127.0.0.1", "localhost", "::1"):
            return real_connect(self, address, *args, **kwargs)
        raise RuntimeError(
            f"Blocked external network access to {address!r}. "
            "Use a cassette, a stub fixture, or a monkeypatched session."
        )

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise time.sleep so retry/backoff (osm.py:50, wikidata.py:53,
    google_places.py:215,278) doesn't add 30s to every test."""
    monkeypatch.setattr(time, "sleep", lambda *_: None)


# ---------------------------------------------------------------------------
# 2. Deterministic clock
# ---------------------------------------------------------------------------
@pytest.fixture
def frozen_time():
    """Freeze the clock so every scraper stamps the same scraped_at.

    freeze_time patches `datetime.datetime` at the module/class level, so it is
    visible to the short-lived worker threads spawned inside main.main()'s
    ThreadPoolExecutor. For fine-grained per-call control, prefer the `now=`
    seam on the scrapers (see recommended change).
    """
    from freezegun import freeze_time

    with freeze_time(FROZEN_NOW) as freezer:
        yield freezer


# ---------------------------------------------------------------------------
# 3. Generic in-process HTTP stub
# ---------------------------------------------------------------------------
Route = Callable[[str, bytes], tuple[int, str, bytes]]   # (path, body) -> (status, ctype, body)


class _StubHandler(http.server.BaseHTTPRequestHandler):
    def _dispatch(self, method: str) -> None:
        routes = getattr(self.server, "routes", {})
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        for (m, prefix), handler in routes.items():
            if m == method and self.path.startswith(prefix):
                status, ctype, payload = handler(self.path, body)
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, *args) -> None:  # silence request logging
        pass


@pytest.fixture
def stub_server():
    """Factory. Usage:

        base = stub_server({("POST", "/api/interpreter"): handler})
        # base == "http://127.0.0.1:<port>"

    Each handler is `(path, body_bytes) -> (status, content_type, body_bytes)`.
    """
    servers: list[http.server.ThreadingHTTPServer] = []

    def _make(routes: dict) -> str:
        class Handler(_StubHandler):
            pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        srv.routes = routes
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield _make
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _json_body(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _load_json(rel: str):
    return json.loads((HTTP_DIR / rel).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 4. Fake Places API, Overpass stub, Wikidata stub
# ---------------------------------------------------------------------------
@pytest.fixture
def overpass_payload():
    return _load_json("overpass/lebanon_nodes_ways.json")


@pytest.fixture
def fake_overpass_url(stub_server, overpass_payload):
    """Local Overpass stub. POST /api/interpreter returns the golden elements."""
    def handler(path, body):
        return 200, "application/json", _json_body(overpass_payload)

    return stub_server({("POST", "/api/interpreter"): handler})


@pytest.fixture
def wikidata_payload():
    return _load_json("wikidata/lebanon_bindings.json")


@pytest.fixture
def fake_wikidata_endpoint(stub_server, wikidata_payload):
    """Local SPARQL stub. GET /sparql returns the golden bindings."""
    def handler(path, body):
        return 200, "application/sparql-results+json", _json_body(wikidata_payload)

    return stub_server({("GET", "/sparql"): handler})


@pytest.fixture
def places_pages():
    """Two pages keyed by the nextPageToken the fixture simulates."""
    return {
        "first": _load_json("places/lebanon_page1.json"),
        "tok2": _load_json("places/lebanon_page2.json"),
    }


@pytest.fixture
def fake_places_api(stub_server, places_pages):
    """Fake Places API (New) searchText endpoint, incl. pagination.

    Reads the JSON body's `pageToken`; returns page 2 when it equals the token
    the first page advertises, else page 1.
    """
    def handler(path, body):
        req = json.loads(body or b"{}")
        token = req.get("pageToken")
        page = places_pages["tok2"] if token == "tok2" else places_pages["first"]
        return 200, "application/json", _json_body(page)

    return stub_server({("POST", "/v1/places:searchText"): handler})


# ---------------------------------------------------------------------------
# 5. Enricher website stub (offline, thread-safe)
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200):
        self.text = text
        self.status_code = status_code


@pytest.fixture
def fake_website_fetch(monkeypatch: pytest.MonkeyPatch):
    """Replace enricher._fetch_website with a deterministic mapping.

        mapping = {"https://x.com": (True, {"email": "a@b.c", ...})}
        fake_website_fetch(mapping)

    check_websites looks up `_fetch_website` in module globals at call time
    (enricher.py:189), so patching the name works without touching the source.
    """
    def _apply(mapping: dict) -> dict:
        def fake(url: str):
            return mapping.get(url, (False, {"email": None, "instagram": None,
                                             "whatsapp": None, "linkedin": None}))

        monkeypatch.setattr("enricher._fetch_website", fake)
        return mapping

    return _apply


# ---------------------------------------------------------------------------
# 6. Cassettes (recorded HTTP, replay-only)
# ---------------------------------------------------------------------------
@pytest.fixture
def cassette():
    """vcrpy instance locked to replay mode. Never records in CI.

    To (re)generate a cassette: run once on a trusted machine with
    `record_mode="once"` and network enabled, review + redact, commit.
    """
    import vcr

    return vcr.VCR(
        cassette_library_dir=str(CASSETTE_DIR),
        record_mode="none",                     # hard-fail if a request is missing
        match_on=["method", "scheme", "host", "port", "path", "query"],
        filter_headers=[("X-Goog-Api-Key", "REDACTED")],
        filter_post_data_parameters=["data", "query"],
    )


# ---------------------------------------------------------------------------
# 7. Golden-CSV helpers (order-insensitive)
# ---------------------------------------------------------------------------
def _rows(path: pathlib.Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def assert_csv_equal(actual: pathlib.Path, expected: pathlib.Path) -> None:
    """Compare CSVs as sorted multisets, so scraper/thread ordering can't cause
    a false failure. Reads raw (strings) — do NOT route through load_master,
    which type-casts and would mask empty-string vs None differences."""
    def canonical(path):
        return sorted(
            (json.dumps(r, sort_keys=True, default=str) for r in _rows(path))
        )

    if canonical(actual) != canonical(expected):
        raise AssertionError(f"CSV mismatch: {actual} != {expected}")


# ---------------------------------------------------------------------------
# 8. Full-pipeline replay wiring
# ---------------------------------------------------------------------------
@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Redirect main.DATA_DIR to a temp dir (main.py:40 global)."""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr("main.DATA_DIR", d)
    return d


@pytest.fixture
def seed_master(data_dir):
    """Write a pre-existing master CSV so main() exercises the merge path
    (main.py:95, 124)."""
    def _write(rows: list[dict]) -> pathlib.Path:
        import main

        p = data_dir / "all_businesses.csv"
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=main.FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        return p

    return _write


@pytest.fixture
def fake_scrapers(monkeypatch):
    """Swap main's three scraper classes for canned generators (main.py:100)."""
    def _apply(osm_records, wikidata_records, places_records):
        class Fake:
            def __init__(self, records):
                self._records = records

            def scrape(self):
                yield from self._records

        monkeypatch.setattr("main.OSMScraper", lambda: Fake(osm_records))
        monkeypatch.setattr("main.WikidataScraper", lambda: Fake(wikidata_records))
        monkeypatch.setattr("main.GooglePlacesScraper", lambda: Fake(places_records))

    return _apply
```

---

## Fixture recipes

### A. Golden CSV fixtures

- `fixtures/csv/master_input.csv` seeds `data/all_businesses.csv` before
  `main.main()` runs, exercising the accumulate+merge+dedup path
  (`main.py:95,123-125`).
- The five `expected_*.csv` files are the reference outputs of a single
  verified-good replay. They are captured, not hand-typed:

  1. Author the raw inputs (stub JSON + website mapping + master) so they are
     *synthetic* — no real business PII. Use `scraped_at == 2024-01-15T12:00:00Z`.
  2. Run `test_pipeline_replay` once with `assert_csv_equal` temporarily
     replaced by `write_csv`-style dumps.
  3. Eyeball the 5 outputs (they must reflect *actual* code semantics, including
     any known quirks — see "Not a bug, but worth knowing"), then commit as
     goldens.
  4. Restore `assert_csv_equal`.

- Comparison is a **sorted multiset** (see `assert_csv_equal`), which sidesteps
  the `as_completed` ordering non-determinism in `main.py:107` and
  `google_places.py:185` without touching threading.

### B. Recorded HTTP cassettes (vcrpy)

- Used for the three upstream APIs where the response shape is large and best
  captured verbatim. `record_mode="none"` in CI guarantees offline-only.
- Recording workflow (the only deliberate network touch, off-CI, once):

  ```bash
  # 1. flip conftest's record_mode to "once" on a trusted machine
  # 2. run the scraper tests; vcrpy writes .yaml under fixtures/http/cassettes/
  # 3. REDACT: X-Goog-Api-Key, the SCRAPER_EMAIL User-Agent, and any real
  #    business phone/email that leaked into the bodies
  # 4. set record_mode back to "none" and commit
  ```

- **Caveat:** vcrpy is not thread-safe across a single cassette for the
  enricher's 40 workers. Do **not** cassette `check_websites`; use the
  `fake_website_fetch` mapping (or the stub server) for websites instead. Keep
  vcrpy for the single-request OSM / Wikidata / Places calls.

### C. Fake Places API

- `fake_places_api` returns a base URL; monkeypatch `scrapers/google_places.API_URL`
  to `base + "/v1/places:searchText"`.
- The handler keys off `pageToken` so `_scrape_query`'s while-loop
  (`google_places.py:203-278`) can be exercised end-to-end, including the
  `time.sleep(2)` between pages (neutralised by `_no_sleep`) and dedup of
  `seen_ids` when page 2 repeats an id.
- `lebanon_page1.json`:

  ```json
  {
    "places": [
      {
        "id": "p1",
        "displayName": {"text": "Sip Coffee"},
        "formattedAddress": "Mar Mikhael, Beirut",
        "location": {"latitude": 33.89, "longitude": 35.52},
        "websiteUri": "https://sip.co",
        "nationalPhoneNumber": "+961 70 111 222",
        "types": ["cafe", "food", "point_of_interest", "establishment"],
        "rating": 4.5,
        "userRatingCount": 120
      }
    ],
    "nextPageToken": "tok2"
  }
  ```
  `lebanon_page2.json` carries `"places":[{... "id":"p2", "types":["restaurant","establishment"], ...}]`
  and no `nextPageToken`. `_pick_category` (`google_places.py:292-296`) drops
  `point_of_interest`/`establishment` and returns `cafe`.

### D. Local Overpass stub

- `fake_overpass_url` → monkeypatch `scrapers/osm.OVERPASS_URL`.
- `lebanon_nodes_ways.json` includes: a `node` (lat/lon directly), a `way`
  (coords via `center`, exercising `osm.py:65-66`), and a `natural=valley` node
  with no business tag (category becomes `None`, filtered later by the
  whitelist — so the *parser* yields 3 records while the *pipeline* keeps 2).
- Optional failure-path test: a stateful handler returning 500 twice then 200,
  verifying the retry loop (`osm.py:38-53`) without a 60s wait.

### E. Deterministic clock and `scraped_at` injection

- **Zero-change path:** `frozen_time` (freezegun) freezes `datetime.utcnow()`;
  because `FROZEN_NOW` is whole-second, `.isoformat()` yields
  `"2024-01-15T12:00:00"` and every record's `scraped_at` is exactly
  `"2024-01-15T12:00:00Z"`. freezegun's patch is at the `datetime.datetime`
  class level, so worker threads spawned inside the test also see it.
- **Recommended seam** (the one real change worth making, backward-compatible):

  ```python
  # scrapers/osm.py, wikidata.py — identical shape
  def scrape(self, now: datetime.datetime | None = None) -> Iterator[BusinessRecord]:
      scraped_at = (now or datetime.datetime.utcnow()).isoformat() + "Z"

  # scrapers/google_places.py — compute once, thread the string down as today
  def scrape(self, now: datetime.datetime | None = None) -> Iterator[BusinessRecord]:
      scraped_at = (now or datetime.datetime.utcnow()).isoformat() + "Z"
      # ... pass `scraped_at` to _scrape_query exactly as it already does
  ```

  This lets tests call `OSMScraper().scrape(now=datetime.datetime(2024,1,15,12,0,0))`
  with no freezegun dependency and no thread caveat. `base.py:33` needs no
  change (an extra optional kwarg on a subclass method is legal).

### F. Example tests (wiring the fixtures)

```python
# tests/scrapers/test_osm.py
from scrapers.osm import OSMScraper

def test_osm_parses_nodes_ways_and_center(monkeypatch, fake_overpass_url, frozen_time):
    monkeypatch.setattr("scrapers.osm.OVERPASS_URL", fake_overpass_url)
    records = list(OSMScraper().scrape())

    names = [r["name"] for r in records]
    assert "Tawlet" in names and "Zaatar w Zeit" in names

    zz = next(r for r in records if r["name"] == "Zaatar w Zeit")
    assert (zz["lat"], zz["lon"]) == (33.9, 35.51)          # from way.center
    assert zz["scraped_at"] == "2024-01-15T12:00:00Z"
    assert all(r["country"] == "LB" for r in records)
```

```python
# tests/integration/test_pipeline_replay.py
import main

def test_full_pipeline_replay(frozen_time, data_dir, seed_master,
                              fake_scrapers, fake_website_fetch):
    seed_master([{"name": "Existing Co", "phone": "+961 1 000000",
                  "country": "LB", "source": "osm",
                  "scraped_at": "2024-01-15T12:00:00Z"}])
    fake_scrapers(osm_raw, wikidata_raw, places_raw)          # canned records
    fake_website_fetch({"https://sip.co": (True, {"email": "hi@sip.co"})})

    main.main()

    assert_csv_equal(data_dir / "all_businesses.csv", CSV_DIR / "expected_all_businesses.csv")
    assert_csv_equal(data_dir / "sales_ready.csv",  CSV_DIR / "expected_sales_ready.csv")
```

---

## Not a bug, but worth knowing

- **`_fetch_website` marks 4xx as live.** `enricher.py:147-148` sets
  `live = r.status_code < 500`, then returns early with `(live, {})` when
  `status_code >= 400` — so a 404 site is recorded `website_live=True`. Golden
  fixtures for the enricher/pitch pipeline must encode this *actual* behavior,
  not the intent, or the replay test will "fail" against a fixture that assumed
  404 ⇒ dead. The fix is out of scope here (it belongs to the HTTP-robustness
  audit, `005`), but the fixture-author must know it.
- **Fixtures are PII.** The product's output is real Lebanese/Saudi business
  names, phones, emails, and Instagram handles. Golden CSVs and cassettes must be
  **synthetic** (or redacted) before commit — never record live business contact
  data into the repo.
- **`main.main()` prints a lot** (`main.py:97-180`). Replay tests should wrap the
  call in `capsys` or just ignore stdout; the CSVs are the assertion surface.
- **Import-time side effects.** `enricher._SESSION` is created at import
  (`enricher.py:124`) and `enrich()` calls `urllib3.disable_warnings`
  (`enricher.py:267-268`). Both are harmless offline once the socket guard is up,
  but they mean the module must be imported *after* fixtures patch the transport,
  which the `monkeypatch.setattr("enricher._fetch_website", ...)` pattern already
  handles.

## Recommended order of work

1. Add `requirements-dev.txt` + `pyproject.toml` `[tool.pytest.ini_options]`
   (`pythonpath = ["."]`), then `tests/conftest.py` as listed — the socket guard
   and `_no_sleep` alone make "zero network" enforceable immediately.
2. Write `tests/unit/*` for the pure functions (dedup, whitelist, lead_score,
   infer_region, recommend_service, load_master/write_csv/has_any_contact). This
   is the bulk of the value and needs no fixtures beyond inline dicts.
3. Add the scraper parser tests against the local stubs (`test_osm`,
   `test_wikidata`, `test_google_places`) with the `now=` clock seam.
4. Add `test_enricher_websites` via `fake_website_fetch`; capture + commit the
   website HTML fixtures (redacted).
5. Build `test_pipeline_replay` with `seed_master` + `fake_scrapers` +
   `fake_website_fetch`, and generate the five golden CSVs (review, then commit).
6. Optional: record OSM/Wikidata/Places vcrpy cassettes once for a
   high-fidelity replay of the real response shapes.
7. (Deferred, needs a separate PR) extract `run(scrapers, data_dir, now=None)`
   from `main.main()` so the replay test stops monkeypatching five names and
   instead passes three args.
