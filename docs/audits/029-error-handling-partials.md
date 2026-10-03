# 029 — Error Handling and Partial Failure

## Verdict

The pipeline's error handling is asymmetric in exactly the wrong direction: the
*cheap* stage (three scraper HTTP calls) is wrapped in `try/except`, while the
*expensive* stage (40-thread website enrichment, run after hours of scraping) has a
naked `future.result()` at `enricher.py:193` that will abort the whole process and
discard every row before the CSVs are written. Because `main.py:129` calls `enrich()`
with no enclosing handler, a single worker exception is fatal. The fix is
defense-in-depth at two points — widen the guard inside `_fetch_website` *and* guard
`future.result()` at the call site — plus a per-stage policy that never lets a
per-site HTTP failure propagate past its own record.

## Enumeration of unhandled exception paths

Each row is a place an exception escapes its intended boundary and what it costs.
"Proven" means a realistic, reachable trigger exists today; "latent" means the guard
boundary is misplaced and any future edit (or rare input) converts it to a crash.

| # | Site | Exception | Reachability | Effect |
|---|---|---|---|---|
| 1 | `enricher.py:193` | any `Exception` escaping `_fetch_website` | Proven (see #2) | **Run death.** `enrich()` at `main.py:129` is unguarded, so the traceback exits `main()`; no CSV is written (`main.py:149`) and hours of scraper work are lost. |
| 2 | `enricher.py:154-177` | `AttributeError`, `TypeError`, etc. from the parse block | Latent (today's regexes are safe) | Escapes `_fetch_website`, re-raised by `future.result()` → #1. |
| 3 | `scrapers/google_places.py:186` | any `Exception` from `fetch_query` | Proven (see #4) | **Silent loss of all 68 queries.** Caught by `main.py:112`, but `all_records` is only yielded at `google_places.py:188`, so 67 good queries are discarded with the 1 bad one. |
| 4 | `scrapers/google_places.py:222` | `requests.exceptions.JSONDecodeError` | Proven | `resp.json()` sits outside the `try/except` (208-220); a 200-with-HTML gateway/proxy error crashes `fetch_query` → #3. |
| 5 | `scrapers/osm.py:55` | `requests.exceptions.JSONDecodeError` | Proven | `resp.json()` outside the retry loop (38-53); Overpass returns HTTP 200 with a text "runtime error" body on query timeout. Whole OSM source dropped, no alert. |
| 6 | `scrapers/wikidata.py:58` | `requests.exceptions.JSONDecodeError` | Proven | Same shape as OSM; SPARQL gateway timeout/maintenance page → 200 HTML → `resp.json()` raises. Whole Wikidata source dropped. |
| 7 | `scrapers/whitelist.py:112`, `dedup.py:12,55,58`, `pitch_recommender.py:50` | `AttributeError`, `TypeError` | Latent | Uncoerced `.strip()/.split()/re.sub()/max()` on non-string fields crash the local stages; no handler at `main.py:117/125/133`. |
| 8 | `main.py:95` (`load_master`) | `_csv.Error`, `OSError` | Latent | NUL-byte / truncated master from a prior interrupted write kills the run before it starts. |

Two more non-exception failure modes belong in the same bucket:

| # | Site | Failure | Effect |
|---|---|---|---|
| 9 | `scrapers/google_places.py:213-216` | unbounded `while True` + `continue` on HTTP 429 | Hang → GitHub Actions `timeout-minutes: 300` → SIGKILL → nothing written. |
| 10 | `main.py:147-148`, `enricher.py:147` | 4xx classified as live | 404/403 sites get `website_live=True`, losing the "dead website = rebuild" signal. |

---

## Findings

### S1 — Naked `future.result()` in `check_websites` kills the run after hours of scraping

- **Where:** `enricher.py:191-195`, consumed at `main.py:129`
- **Breaks:** `check_websites()` submits one `_fetch_website` task per website to a
  40-worker pool and consumes them as `live, contacts = future.result()`. There is no
  `try/except` around `future.result()`, and `enrich()` — which calls `check_websites`
  at `enricher.py:277` — is itself called unguarded at `main.py:129`. If any worker
  raises, the exception unwinds through the `as_completed` loop, out of
  `check_websites`, out of `enrich`, and out of `main()` before the first
  `write_csv` call (`main.py:149`). Because enrichment runs *after* OSM, Wikidata and
  Google scraping, the entire run's output — plus the API spend behind it — is
  discarded.
- **Trigger:** The single realistic escape hatch today is the parse block in
  `_fetch_website` (`enricher.py:154-177`), which sits *outside* the function's
  `try/except` (`enricher.py:145-152`). Any change there (an optional regex group
  that later `.group(1)`s to `None`, a `.get()` on a value that isn't a dict, a
  non-string `r.text` edge) becomes an uncaught worker crash. The defect is not "this
  line raises today" — it is that a function whose *documented contract* is "return
  `(bool, dict)`, never raise" leaves half its body unguarded, and the call site
  doesn't backstop it either.
- **Fix:** Guard at the call site (primary) and widen the worker guard (secondary):
  ```python
  for future in as_completed(futures):
      idx = futures[future]
      try:
          live, contacts = future.result()
      except Exception as exc:
          print(f"[Enricher] worker error idx={idx}: {exc}", file=sys.stderr)
          live, contacts = False, {k: None for k in ("email", "instagram", "whatsapp", "linkedin")}
  ```
  And move lines 154-177 inside the `try` in `_fetch_website`.

### S1 — Google Places: one failed query silently drops all 68

- **Where:** `scrapers/google_places.py:183-188`
- **Breaks:** `f.result()` at line 186 re-raises the first worker exception. Results
  are accumulated in a local `all_records` list and only handed back via
  `yield from all_records` at line 188 — *after* the `as_completed` loop finishes.
  So a single bad query (out of 68) discards the 67 that succeeded. `main.py:112`
  catches it, prints one stderr line, and continues with zero Google rows: silent,
  partial data loss with no salvage of completed work.
- **Trigger:** Google returns a truncated/HTML response on one query's `nextPageToken`
  page; `resp.json()` at line 222 raises, bubbles through `fetch_query` → `f.result()`.
- **Fix:** Isolate each query's future and yield *completed* results incrementally:
  ```python
  for f in as_completed(futures):
      try:
          f.result()
      except Exception as exc:
          print(f"[Google] query worker failed: {exc}", file=sys.stderr)
  ```

### S1 — `resp.json()` outside the retry loop drops OSM and Wikidata wholesale

- **Where:** `scrapers/osm.py:55`; `scrapers/wikidata.py:58`
- **Breaks:** Both scrapers retry only `requests.RequestException` inside the loop
  (`osm.py:38-53`, `wikidata.py:40-56`), then parse the response *after* the loop.
  Overpass and the Wikidata SPARQL endpoint both return **HTTP 200 with a text/HTML
  error body** under query timeout or maintenance ("runtime error: Query timed out",
  SPARQL gateway timeouts). `resp.raise_for_status()` passes, then `resp.json()`
  raises `JSONDecodeError`, which is outside the `except requests.RequestException`.
  The scraper exits; `main.py:112` catches it and continues with **zero** records from
  that source — no retry, no alert, no salvage.
- **Trigger:** Overpass's monolithic Lebanon union query (`osm.py:10-26`, `timeout:180`)
  times out under load, returning a 200 HTML error page.
- **Fix:** Move `resp.json()` inside the retry `try`, or guard it separately:
  ```python
  try:
      data = resp.json()
  except requests.exceptions.JSONDecodeError as e:
      print(f"[OSM] non-JSON response: {e}", file=sys.stderr)
      return
  ```

### S2 — Fail-open: all sources can die and the run still reports success

- **Where:** `main.py:105-129`
- **Breaks:** The scraper pool swallows every exception and prints to stderr, but
  nothing downstream checks whether any data actually arrived. If OSM times out,
  Wikidata 429s, and `GOOGLE_PLACES_API_KEY` is unset (`google_places.py:158-160`
  prints and returns), `raw` is empty. The pipeline still runs filter → dedup →
  enrich on whatever the master holds, then writes and (via the workflow) uploads the
  result. A fully-failed scrape is indistinguishable from a healthy no-op run in CI.
- **Trigger:** Any combined upstream outage; no single exception is raised, so nothing
  fails the job.
- **Fix:** Add a floor check after the scrape stage: if `len(raw) == 0`, log a
  critical error and exit non-zero rather than overwriting the master and uploading.

### S2 — A shared, non-thread-safe `requests.Session` misclassifies live sites as dead (not a crash)

- **Where:** `enricher.py:124-125`, `146`
- **Breaks:** `_SESSION` is a module-level `requests.Session` shared by all 40
  enrichment threads, while `requests.Session` is explicitly not thread-safe (the
  `CookieJar` mutates during `extract_cookies_to_jar`). Under contention this raises
  `RuntimeError`/`KeyError` — **but** that happens inside `_SESSION.get`, which is
  inside the `try/except Exception` at `enricher.py:145-152`, so it is caught and
  converted to `(False, contacts)`. The run does *not* crash; instead a live site is
  intermittently recorded as `website_live=False` — silent wrong results that also
  flip the `lead_score` branch at `enricher.py:243-244` from +10 to +20.
- **Trigger:** Many simultaneous responses that set cookies (any session-cookie site).
- **Fix:** Give each worker its own session via `threading.local()` (the Google
  scraper already does this per-thread at `google_places.py:177`, with the comment
  "requests.Session is not thread-safe").

### S2 — HTTP 4xx is classified as "live", burying the dead-website pitch

- **Where:** `enricher.py:147-148`
- **Breaks:** `live = r.status_code < 500` makes 4xx count as live, then
  `if not live or r.status_code >= 400: return live, contacts` returns `live=True`
  for 404/403/410. A parked domain or deleted site — precisely the "Website rebuild +
  maintenance" pitch (`pitch_recommender.py:54-55`) — is recorded as `website_live=True`
  and inflated into the "live" count at `main.py:155`. Failure semantics are inverted
  for the most commercially valuable class of lead.
- **Trigger:** Any of the 40 threads hits a 404/403/410; the record is marked live.
- **Fix:** `return r.status_code < 400, contacts` (and drop the now-redundant guard).

### S2 — Non-atomic CSV writes put the cumulative master at risk

- **Where:** `main.py:74-79`, called at `main.py:149-153`
- **Breaks:** `write_csv` opens the destination in `"w"` mode directly. If the process
  is killed mid-write (workflow `timeout-minutes: 300`, disk full, or an exception
  during `writer.writerows`), `all_businesses.csv` — the accumulated multi-run master —
  is left truncated or corrupted. The next run's `load_master` (`main.py:95`) then
  either loads a partial history silently or crashes (finding #8), erasing months of
  accumulated leads. This is the one asset whose loss is irreversible.
- **Trigger:** SIGKILL at minute 300 during the `all_businesses.csv` write.
- **Fix:** Write to `path.with_suffix(".tmp")`, `flush` + `os.fsync`, then
  `os.replace(tmp, path)`. Optionally assert `len(records) >= len(master)` before
  publishing as a shrink guard.

---

## Not a bug, but worth knowing

- **`verify=False` swallows TLS failures.** `enricher.py:146` disables certificate
  verification, so an intercepted/broken-TLS host silently becomes
  `website_live=False` instead of surfacing a security-relevant error. Adjacent to
  the SSRF/security lens, but it interacts with error handling: you cannot distinguish
  "site is genuinely down" from "site rejected us" from "TLS is broken".
- **No `timeout` on `future.result()`.** Both `enricher.py:193` and
  `google_places.py:186` block indefinitely on a hung worker; the 429 loop (finding
  #9) is the concrete realization of this.
- **`list(scraper.scrape())` is all-or-nothing.** `main.py:103` materializes each
  generator fully before any row is usable, so there is no point where partial scraper
  output can be salvaged on failure. Streaming + per-source accumulation is the
  enabling step for any real quarantine design.

---

## Per-stage failure policy

| Stage | Policy | Rule |
|---|---|---|
| 0. Load master | **Fail-fast** | A corrupt/truncated master (finding #8) must abort with non-zero exit, never be silently re-scraped over. A *missing* master is a valid bootstrap, not an error. |
| 1. Scrape (OSM, Wikidata, Google) | **Degrade-and-continue, per-source** | Isolate each source and each query (`google_places.py:186`); a failed source is logged and skipped, never fatal. Enforce a **global floor**: if `len(raw) == 0`, fail-fast with non-zero exit and do not overwrite/upload (finding #4). |
| 2. Filter (whitelist) | **Quarantine** | Coerce `category` to `str` (`whitelist.py:112`); a record whose fields cannot be coerced goes to a dead-letter sink rather than crashing the listcomp at `main.py:117`. |
| 3. Dedup | **Fail-fast on internal error, quarantine on bad input** | Dedup is pure/deterministic; a real bug must stop the run loudly. But malformed *data* (non-string phone/name/source/scraped_at, `dedup.py:12,55,58`) is quarantined with defensive coercion, not a crash. |
| 4. Enrich (`check_websites`) | **Degrade-and-continue, per-site** | Guard `future.result()` (finding #1) and the full `_fetch_website` body; one site's failure yields `website_live=None` + an error marker and moves on. Add a **circuit breaker**: if >90% of a 100-request window fails (outbound/DNS/ban), abort remaining fetches and proceed with `website_live=None`. |
| 5. Score / pitch | **Degrade-and-continue** | Pure-Python; wrap numeric coercion with defaults (`pitch_recommender.py:42-49` already does this) and default the pitch to `"Discovery call"`. |
| 6. Write CSVs | **Fail-fast + atomic** | `os.replace` after `fsync` (finding #7); a write failure must not leave a truncated master; add a shrink guard so a run that loses >20% of master records refuses to publish. |

**One-liner summary:** external I/O degrades-and-continues per *item*; local/pure
stages fail fast on real bugs but quarantine bad *data*; the cumulative master is
fail-fast and atomic because it is the only non-recoverable asset.

---

## Recommended order of work

1. **`enricher.py:193` (S1)** — wrap `future.result()` in `try/except`; this alone
   turns "hours of work lost" into "one record marked unknown".
2. **`google_places.py:186` (S1)** — isolate per-query futures so 67/68 queries survive.
3. **`osm.py:55` / `wikidata.py:58` / `google_places.py:222` (S1)** — guard every
   `resp.json()` against non-JSON 200s.
4. **`main.py` floor check (S2)** — exit non-zero when a scrape yields zero raw records.
5. **`enricher.py:147-148` (S2)** — fix the 4xx-as-live classification.
6. **`enricher.py:124` (S2)** — per-thread `Session` via `threading.local()`.
7. **`main.py:74-79` (S2)** — atomic `os.replace` writes + master shrink guard.
8. **`google_places.py:213-216` (S2)** — bound the 429 retry loop.
