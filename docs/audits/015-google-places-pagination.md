# 015 — Google Places pagination loop & thread-safety audit

## Verdict

The response shape is correct for the Places API (New) `places:searchText` endpoint, and the
per-thread `requests.Session` plus the lock-guarded shared `seen_ids` set are actually safe. The
two real problems are both in error/termination handling: a `401` (or any auth failure) silently
returns partial records instead of failing the run, which turns a bad API key into *silent data
loss*; and the pagination loop has no termination guard beyond `not page_token`, so a non-advancing
or persistent-token response can spin forever and burn quota (or hang the run) with no local escape.

## Findings

### S1 — 401 handler returns partial records instead of raising; a bad API key becomes silent data loss

- **Where:** `scrapers/google_places.py:210-212` (and the catch-all `:218-220`).
- **Breaks:** An invalid, expired, or unauthorized API key is a hard, global configuration error,
  but the code prints a line and `return records` (normally an empty list), abandoning the query.
  Because this runs per-thread for each of the 68 queries, `scrape()` yields an empty iterator, so
  `main.py:108-110` prints `[GooglePlacesScraper] collected 0 records` and the run continues as if
  Google simply found nothing. The run then writes all five CSVs with Google data silently absent,
  exits 0, and raises no alert. Google is a primary source here, so this is exactly the "silent data
  loss / wrong results" case: downstream `dedup`/`enrich`/`lead_score` can't tell "Google returned
  nothing" from "Google auth failed."
- **Trigger:** `GOOGLE_PLACES_API_KEY` set to a bad key and `python main.py`. Every Google query
  returns 0 records; the CSVs are written as if Google contributed nothing. The cumulative
  `all_businesses.csv` (`main.py:95,124`) masks it further — old data persists, so the hole is not
  even obvious from row counts.
- **Fix:** Raise instead of returning. `raise RuntimeError("Google Places API key invalid")` (or
  re-raise the underlying `requests.HTTPError`) so the failure surfaces at `main.py:112`. A bad key
  should fail the whole run, not degrade it.
- **Secondary observation (same finding):** the `== 401` branch is likely dead code. The Places API
  (New) reports an invalid API key as HTTP `400` with `status: INVALID_ARGUMENT` ("API key not
  valid…"), and a key valid-but-not-enabled for Places as `403` — not `401` ([Google Maps Platform
  error docs](https://developers.google.com/maps/documentation/places/web-service/error-handling)).
  So an invalid key in practice flows into `resp.raise_for_status()` at `:217` and is swallowed by
  the catch-all `:218-220` with the same silent `return records`. Either path yields the same silent
  failure, so the fix is identical: auth errors must raise.

### S2 — Pagination loop has no termination guard beyond `not page_token`; can spin forever / hang

- **Where:** `scrapers/google_places.py:203-278`, the exit condition at `:275-278` and the 429
  retry at `:213-216`.
- **Breaks:** The loop's *only* exit is `if not page_token: return records`. There is:
  - no check for a **repeating token** (the same `nextPageToken` returned again),
  - no `if not places: break` (an empty `places` array does not stop the loop), and
  - no maximum-page cap (the `page` counter at `:201/:224` is log-only).
  If the API returns a non-empty `nextPageToken` that does not advance — or returns
  `{"places": [], "nextPageToken": "..."}` repeatedly — the loop spins forever, each iteration
  issuing a *paid* Text Search request and printing a line that looks like normal progress.
  Separately, the 429 branch retries the same token after a fixed 30s sleep with no backoff and no
  attempt cap (`:213-216`); a sustained rate-limit turns into an unbounded retry loop that never
  gives up, and because `fetch_query` never returns, `scrape()`'s `as_completed`/`f.result()`
  (`:185-186`) blocks indefinitely — the whole run hangs.
- **Trigger:** A sticky `nextPageToken` (identical value across pages) or a repeated
  empty-`places`-with-token response. The 429 path triggers whenever the key is rate-limited for
  longer than the fixed 30s window.
- **What saves it in the common case:** Google caps Text Search at **60 results across all pages**
  ([Text Search (New) docs](https://developers.google.com/maps/documentation/places/web-service/text-search)),
  and with `pageSize` unset the default is 20/page, so in normal operation each query terminates
  after at most 3 pages and the token naturally goes absent. The code is relying entirely on that
  server-side cap with no local safety net.
- **Fix:** Add at least two of: `if page_token == prev_token: break`; `if not places: break`; and a
  page cap (`if page >= 3: break`, given the 60-result ceiling). Add a bounded retry/backoff to the
  429 branch (e.g. exponential backoff with a max-attempts counter, then return the partial records).

## Not a bug, but worth knowing

- **The response shape is correct.** Every field the code reads matches the Places API (New)
  `searchText` response: top-level `places` array and `nextPageToken`; per-place `id`,
  `displayName.text`, `formattedAddress`, `location.latitude/longitude`, `websiteUri`,
  `nationalPhoneNumber`, `types`, `rating`, `userRatingCount`. All of these are valid `X-Goog-FieldMask`
  entries, `nextPageToken` is correctly listed at top level (no `places.` prefix), and the mask is
  comma-joined with no spaces (`google_places.py:31-42`) — exactly as the docs require. The
  pagination request correctly keeps every non-`pageToken` parameter identical across pages, which
  the docs require (`google_places.py:204-206`).
- **The 60-result cap is the real coverage ceiling.** With `pageSize` unset, each of the 68 queries
  yields at most 60 places (3 pages), so Google contributes at most ~4,080 raw places before dedup —
  regardless of how many businesses actually match. Worth knowing when sizing expectations, and it
  means a page cap of 3 in the fix above loses nothing.
- **`types` ordering is assumed, not guaranteed.** `_pick_category` (`:292-296`) returns `types[0]`
  on the assumption that Google orders types most-specific-first (`:281`). That was contractual for
  the *old* Places API; the (New) docs show most-specific-first in examples but do not state the
  ordering as a guarantee. Practical impact is low, but a category that happens to be generic-first
  would fall through to `"business"`.
- **`time.sleep(2)` "required between paginated requests" is a holdover.** The comment at `:278` is
  from the old Places API. The (New) API has no mandated inter-page delay, so this is pure latency
  (~2s × 2 pages × 68 queries, absorbed by 5 workers), not correctness.
- **Requesting `nationalPhoneNumber`, `websiteUri`, `rating`, and `userRatingCount` bills the Text
  Search *Enterprise* SKU on every call.** That is the most expensive SKU and is charged per call
  (including every pagination page), so the silent-loop finding above is also a direct cost risk.

## Thread-safety (asked explicitly): the shared `seen_ids` set with a per-thread session **is safe**

- **`seen_ids`** is a shared `set[str]`, but every read *and* write happens inside `with seen_lock:`
  (`:231-234`), and the check-then-add is atomic under that lock, so cross-thread dedup cannot race.
- **Per-thread sessions** are correct: `fetch_query` calls `_make_session()` once per thread
  (`:178`), and `requests.Session` is not documented as thread-safe, so this avoids the shared-session
  hazard. `_make_session` only reads `self._api_key`, which is set once in `__init__` (`:146`) and
  never mutated, so there is no data race there.
- **`all_records.extend`** is guarded by `records_lock` (`:180-181`); the per-query `records` list is
  thread-local (`:199`). `infer_region` (`:243`) and `_pick_category` (`:247`) only read module-level
  constants (precompiled regexes and the `_GENERIC_TYPES` set), which are read-only and thread-safe.
- The only nit: `scraped_at` uses the deprecated `datetime.utcnow()` (`:162`), which is unrelated to
  thread safety but worth a modern `datetime.now(timezone.utc)` cleanup.

## Recommended order of work

1. Fix the 401/auth path to raise (S1) — this is a one-line change that restores trust in the output
   whenever a key is bad, and is the most likely real-world failure.
2. Add local termination guards to the pagination loop and cap the 429 retry (S2) — cheap, and it
   converts a potential hang/quota-burn into a bounded, diagnosable failure.
3. (Optional) Drop the `time.sleep(2)` holdover and decide whether to stop requesting Enterprise-SKU
   fields (`rating`/`userRatingCount`/`websiteUri`/`nationalPhoneNumber`) if per-call cost matters.
