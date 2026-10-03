# 047 — Deprecations and dependency rot

## Verdict

The code has exactly three uses of a Python 3.12-deprecated API: `datetime.datetime.utcnow()` in the OSM, Wikidata, and Google Places scrapers. The suspected retry-loop `resp`/`NameError` bug is not present: every path that exhausts all attempts returns before the post-loop `resp` access. All three direct dependency pins are behind the latest PyPI releases checked on 2026-10-03; this is maintenance rot, not by itself evidence that the current run is broken.

## Findings

### S3 — Replace deprecated naive UTC timestamps
- **Where:** `scrapers/osm.py:31`, `scrapers/wikidata.py:31`, `scrapers/google_places.py:162`
- **Breaks:** `datetime.datetime.utcnow()` is deprecated since Python 3.12 and emits `DeprecationWarning` when called. It also returns a naive datetime, while the code appends `Z` to represent UTC. These three call sites are the only Python 3.12-deprecated standard-library API uses found in the project source.
- **Trigger:** Run any of the three scrapers under Python 3.12+ with deprecation warnings enabled; each scraper calls the deprecated API when it starts (Google Places after confirming the API key is present).
- **Fix:** Replace each with `datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")` to produce an aware UTC timestamp while retaining the current serialized `Z` form.
- **Reference:** [Python 3.12 `datetime` documentation](https://docs.python.org/3.12/library/datetime.html#datetime.datetime.utcnow) marks `utcnow()` deprecated since 3.12 and recommends `now(datetime.UTC)`.

### S3 — All direct dependency pins lag current releases
- **Where:** `requirements.txt:1-3`
- **Breaks:** The exact pins hold installs on older releases and miss subsequent bug/security and compatibility fixes. As of 2026-10-03, PyPI lists `requests 2.34.2` (2026-05-14), `beautifulsoup4 4.15.0` (2026-06-07), and `lxml 6.1.3` (2026-09-02), versus the pinned `2.32.3`, `4.12.3`, and `5.2.2`. `beautifulsoup4` and `lxml` are not imported by the project’s Python source, so their pins currently add install surface without serving an evident runtime use.
- **Trigger:** Every clean environment setup using `requirements.txt` installs the old exact versions; e.g. it installs lxml 5.2.2 even though 6.1.3 is available.
- **Fix:** Remove the unused Beautiful Soup/lxml dependencies if they are not needed, and update the remaining pin(s) after reviewing release notes; add a lock/constraints strategy if repeatable transitive installs are required.
- **References:** [requests on PyPI](https://pypi.org/project/requests/), [beautifulsoup4 on PyPI](https://pypi.org/project/beautifulsoup4/), [lxml on PyPI](https://pypi.org/project/lxml/).

## Not a bug, but worth knowing

- The suspected retry fall-through/`resp` error is **refuted**. In `scrapers/osm.py:38-53` and `scrapers/wikidata.py:40-56`, a successful request assigns `resp`, checks status, and `break`s. If any attempt raises `requests.RequestException`, attempts 1–2 sleep and continue; on attempt 3 the `else` branch logs exhaustion and executes `return`. Thus three failed attempts never reach `resp.json()` at `osm.py:55` or `wikidata.py:58`, so there is no unbound-local `NameError`. If an earlier response was assigned and a later request fails, `resp` can remain stale locally, but the final failure still returns; it is not consumed. After one or two failures, a successful later attempt overwrites `resp` before breaking.
- The search for other Python 3.12 deprecations in the project’s Python files found no additional uses. This inventory is about deprecated APIs, not APIs removed in 3.12 or deprecations that might occur inside third-party packages.

## Recommended order of work

1. Replace the three `utcnow()` calls with timezone-aware UTC timestamps.
2. Review whether Beautiful Soup and lxml are needed; remove unused direct dependencies, then update active pins with release-note review.
3. Keep the retry code as-is with respect to the suspected `resp` fall-through; no fix is needed for that claim.
