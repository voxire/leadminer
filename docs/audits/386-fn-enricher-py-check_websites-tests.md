# 386 — `enricher.check_websites`: the tests it still needs

**Target:** `check_websites` — `enricher.py:225-276`
**Lens:** tests
**Anchored to:** git `99493b9`, `enricher.py` md5 `4c6f1e6040a884cd2fde8951592b9041`.
`enricher.py` was being edited by a concurrent agent while this audit ran, so every
line reference below is against that commit, verified with
`git archive 99493b9 | tar -x` into a sandbox. All empirical claims were produced by
running probes against that sandbox with `requests`/`urllib3` stubbed — no network.

---

## Verdict

`check_websites` is the **only producer** of `website_live`, the single most
load-bearing field in the product, and all 28 tests in `tests/test_lead_signal.py`
exercise only its *consumers*. `tests/test_lead_signal.py:71-99` asserts that
`lead_score` and `recommend_service` behave correctly for hand-written
`website_live` values; nothing asserts that `check_websites` ever produces those
values correctly. A generator bug is invisible to every test in the repo.

Two concrete consequences, both reproduced offline:

1. **HTTP 403 is scored DEAD.** `enricher.py:182-184` treats any status `>= 400`
   as `DEAD`, so a Cloudflare challenge — the exact scenario the module's own
   contract names as `UNKNOWN` at `enricher.py:151-153` and `enricher.py:164-167` —
   yields `website_live=False`, `lead_score=50`, and the pitch
   `"Website rebuild + maintenance"` for a site we never actually loaded.
2. **A 200 KB body with no `@` costs ~5 minutes of CPU in one worker thread**
   because `_EMAIL_RE` (`enricher.py:130`, applied at `enricher.py:200`) is
   quadratic on long unbroken tokens. Forty such pages and `check_websites`
   never returns.

Both are one-line tests. Neither exists.

---

## Findings

### S1 — HTTP 403/429/451 are classified DEAD, inverting the core lead signal

- **Where:** `enricher.py:182-184`, consumed at `enricher.py:250`, `enricher.py:303-304`, `pitch_recommender.recommend_service`
- **Breaks:** `_fetch_website` returns `DEAD` for any status `>= 400`. `DEAD` is
  defined at `enricher.py:155` as "the site exists and is broken: a real rebuild
  pitch". But 403 is how Cloudflare, Akamai and AWS WAF serve a bot challenge;
  429 means "not right now"; 451 is a legal block. None of these is evidence the
  site is broken. `enricher.py:164-167` states the rule explicitly and
  `enricher.py:151-153` even lists "Cloudflare challenge" as an `UNKNOWN` case —
  the comment is right and the code contradicts it. `_fetch_website` also bypasses
  `httpclient.fetch_with_retry` entirely (it calls `session.get` directly at
  `enricher.py:175`), so it gets neither the retry that `httpclient._should_retry`
  at `httpclient.py:142-146` already knows 429 deserves, nor the distinction
  `FetchResult.status is None` was designed to preserve (`httpclient.py:98-99`).
- **Trigger (verified end-to-end):** one record,
  `{"name":"Cafe","category":"restaurant","industry_priority":"high","phone":"70123456","scraped_at":"2026-09-01T00:00:00+00:00","website":"https://hardened.example"}`,
  against a host that answers `403` with body `<html>Just a moment...</html>`:
  ```
  HTTP 200 -> website_live=True   lead_score=40  pitch='SEO audit + visibility upgrade'
  HTTP 403 -> website_live=False  lead_score=50  pitch='Website rebuild + maintenance'
  HTTP 429 -> website_live=False  lead_score=50  pitch='Website rebuild + maintenance'
  HTTP 451 -> website_live=False  lead_score=50  pitch='Website rebuild + maintenance'
  ```
  The site we could not read outranks the site we read.
- **Pins:** the lead-signal inversion fixed in commit `5222d32`
  ("fix: invert-risk lead signal"), analysed in `docs/audits/043-scoring-upgrade.md`
  and `docs/audits/029-error-handling-partials.md`. That fix closed the *consumer*
  side; this is the *producer* side of the same bug, still open.
- **Fix:** one line — classify `401/403/405/429/451` and `5xx` as `UNKNOWN`,
  keep `400/404/410` as `DEAD`, or route through `fetch_with_retry` and map
  `FetchResult.status`.

#### Test cases

**`TestWebsiteOutcomeClassification::test_403_is_unknown_not_dead`**
Patch `enricher.get_session` (it is imported by name at `enricher.py:128`, so
`enricher.get_session` is the correct target, **not** `httpclient.get_session`)
to return a fake response with `status_code=403`. Literal input HTML:
`b"<html>Just a moment...</html>"`.
Assert `outcome == UNKNOWN`, i.e. `check_websites([r])[0]["website_live"] is None`.
Pins: the inversion above. **Fails today** (yields `DEAD`).

**`TestWebsiteOutcomeClassification::test_429_is_unknown_not_dead`**
Same, `status_code=429`. Assert `outcome == UNKNOWN`. Pins: 429 is retryable per
`httpclient.py:146` and must not be a pitch signal. **Fails today.**

**`TestWebsiteOutcomeClassification::test_404_and_410_remain_dead`**
`status_code=404` and `410`. Assert `outcome == DEAD` and
`records[0]["website_live"] is False`. Pins: the fix above must not over-correct
and turn real rebuild targets into `UNKNOWN`. Passes today; must keep passing.

**`TestWebsiteOutcomeClassification::test_status_boundary_sweep_is_a_total_function`**
Property over the integer status space: for every `status` in
`range(100, 600)`, the outcome is one of `{LIVE, DEAD, UNKNOWN}`, and
`LIVE if status < 400 else DEAD` — the current one-liner. Table-driven, 500 cases,
no network. Pins: no status code can escape the classification, which is what
makes the 403 fix safe to reason about. Passes today (characterization test for
the current rule).

---

### S1 — Nothing pins that `check_websites` overwrites a stale `website_live` from `dedup._merge`

- **Where:** `enricher.py:250`; the stale value is produced by `dedup.py:114` (`_VOLATILE = {"rating","review_count","website_live","website"}`) and `dedup._pick` (`dedup.py:159-187`)
- **Breaks:** `dedup._merge` picks `website` and `website_live` **independently**,
  both by recency but from potentially different source records. So a merged row
  can carry `website` from observation A and `website_live` from observation B.
  `check_websites` is the only code in the repo that repairs this, by
  unconditionally assigning at `enricher.py:250`. There is no test asserting that.
  Verified concrete case:
  ```
  master: website="https://old-dead.example", website_live=False, scraped_at=2025-01-01
  fresh:  website="https://new-live.example",                        scraped_at=2026-09-01
  dedup([master, fresh]) -> website="https://new-live.example", website_live=False
  ```
  If the fetch of `https://new-live.example` returns `UNKNOWN` and that repair
  ever regresses — and three separate audits (`docs/audits/033:13`,
  `docs/audits/035:27`, `docs/audits/053:12`) all recommend "stop re-fetching
  every row every run", i.e. exactly the change that would introduce the
  regression — the pipeline ships a `website_live=False` describing a *different,
  dead* site:
  ```
  skipped repair -> website_live=False  lead_score=50  pitch='Website rebuild + maintenance'
  check_websites -> website_live=None   lead_score=30
  ```
  We fabricate a rebuild pitch against a business whose site we never reached.
  This is the `043-scoring-upgrade.md` inversion, reintroduced through the cache.
- **Fix:** the code is already correct; it needs the test *before* the caching
  work lands, so the caching change has to confront it.

#### Test case

**`TestCheckWebsitesContract::test_fetch_verdict_always_overwrites_incoming_website_live`**
Property over the incoming value. For each `incoming in (True, False, None)`, and
each `outcome in (LIVE, DEAD, UNKNOWN)`, stub `_fetch_website` to return
`(outcome, NO_CONTACTS)` and call:
```python
rec = {"name": "Cafe", "website": "https://new-live.example", "website_live": incoming}
check_websites([rec], workers=1)
# assert rec["website_live"] == {LIVE: True, DEAD: False, UNKNOWN: None}[outcome]
```
Assert the mapping is a function of `outcome` alone and the incoming value never
survives. 9 cases. Pins: the `dedup._merge` `_VOLATILE` desync and, transitively,
any future "skip already-checked" optimisation. Passes today; must pass after
caching is added.

---

### S1 — A worker exception aborts the whole run

- **Where:** `enricher.py:242-248`
- **Breaks:** (already fixed — the guard is present). `future.result()` used to be
  naked; one raising worker re-raised on the main thread, discarded the rest of
  the pool, and returned from `main()` before `write_csv` at `main.py:209-213`,
  destroying hours of paid Google Places quota. Flagged as S1 in
  `docs/audits/029-error-handling-partials.md:43` and
  `docs/audits/020-error-handling-partials.md:35`, and ranked catalog item #01 in
  `docs/audits/031-test-suite-design.md:138`. **No test was ever written for it.**
- **Trigger:** stub raising for one URL only.
- **Fix:** none needed — this is the test that was owed.

#### Test case

**`TestCheckWebsitesResilience::test_worker_exception_yields_unknown_and_does_not_abort_batch`**
```python
def worker(url):
    if url.endswith("/boom"):
        raise ValueError("simulated worker bug")
    return LIVE, contacts(email="hello@good.example")

records = [{"name": "a", "website": "https://x.example/good"},
           {"name": "b", "website": "https://x.example/boom"},
           {"name": "c", "website": "https://x.example/good2"}]
out = check_websites(records, workers=4)          # must not raise
```
Assert: `len(out) == 3`; `[r["name"] for r in out] == ["a","b","c"]`;
`out[0]["website_live"] is True and out[0]["email"] == "hello@good.example"`;
`out[1]["website_live"] is None` (the `enricher.py:247-248` fallback);
`out[2]["website_live"] is True`. Pins: the S1 above.

---

### S1 — Index correctness under `as_completed` is load-bearing and unpinned

- **Where:** `enricher.py:238` (`futures = {pool.submit(...): idx ...}`),
  `enricher.py:241` (`idx = futures[future]`), `enricher.py:249` (`r = records[idx]`)
- **Breaks:** the design deliberately snapshots `(index, url)` rather than the
  record, so workers never touch a dict (`docs/audits/004-enricher-thread-safety.md:26`)
  and completion order is irrelevant. The obvious refactor —
  `for (idx, url), future in zip(targets, as_completed(futures))` — is wrong,
  because `as_completed` yields in completion order, not submission order.
  `docs/audits/032-fixtures-offline-replay.md:48` explicitly builds CSV-replay
  comparisons on this invariant ("already order-stable — it mutates
  `records[idx]`, never reorders"). Nothing tests it.
- **Trigger:** reversed completion order plus a website-less record interleaved
  to shift every later index by one.
- **Fix:** none needed — this is the test that was owed.

#### Test case

**`TestCheckWebsitesContract::test_result_lands_on_the_record_that_owns_the_url`**
```python
def worker(url):
    n = int(url.rsplit("/", 1)[1])
    time.sleep(0.02 * (3 - n))              # r0 finishes LAST, r2 first
    return [LIVE, DEAD, UNKNOWN][n], contacts(email=f"e{n}@x.example")

records  = [{"name": f"r{n}", "website": f"https://x.example/{n}"} for n in range(3)]
records.insert(1, {"name": "noweb"})        # shifts indices 1,2,3
out = check_websites(records, workers=4)
```
Assert `[r["name"] for r in out] == ["r0", "noweb", "r1", "r2"]`;
`out[0]["website_live"] is True and out[0]["email"] == "e0@x.example"`;
`out[2]["website_live"] is False`; `out[3]["website_live"] is None`;
`"website_live" not in out[1]`. Pins: the `dict[Future, idx]` mapping and the
never-reorder promise `docs/audits/032:48` depends on.

---

### S2 — The "Contacts" summary counts inventory, not extractions

- **Where:** `enricher.py:267-270`, printed at `enricher.py:272-275`
- **Breaks:** `found_email` and friends sum over **all** records, including rows
  that were never fetched and rows whose value was already present before this
  run. Unlike `unknown_count` at `enricher.py:266`, which is correctly gated on
  `r.get("website")`, these four have no gate at all — an inconsistency inside
  four adjacent lines. Verified: with **one** website fetched (LIVE, zero contacts
  extracted) plus two website-less records that already carried an email,
  instagram and whatsapp, the log prints:
  ```
  [Enricher] 1 live / 0 dead / 0 unreachable
  [Enricher] Contacts — email:1 instagram:1 whatsapp:1 linkedin:0
  ```
  Zero extractions reported as three. Because `main.py:150` reloads the whole
  cumulative master and `main.py:179` merges this run's scrapes into it, these
  four numbers are dominated by historical rows and rise monotonically every run.
  An operator has no way to tell from them that extraction has silently stopped
  working — and `cli.py:96-103` (`cmd_stats`, documented at `cli.py:79-80` as
  "the first thing to check") exists precisely to answer that question and
  contradicts this line.
- **Trigger:** the input above.
- **Fix:** count only values written by this pass, or rename to
  `"Contacts in master"` and add a separate `"extracted this run: N"` counter.

#### Test cases

**`TestCheckWebsitesSummary::test_summary_contact_counts_report_extractions_not_inventory`**
Input: `[{"name":"withsite","website":"https://x.example/1"},          # LIVE, NO_CONTACTS
{"name":"nosite","email":"pre@real.example","instagram":"preig"},
{"name":"nosite2","whatsapp":"+96170123456"}]` with `_fetch_website` stubbed to
`(LIVE, NO_CONTACTS)`. Assert the printed line reads
`Contacts — email:0 instagram:0 whatsapp:0 linkedin:0`.
**Fails today** (`email:1 instagram:1 whatsapp:1`). Ship with `xfail` first, then
flip.

**`TestCheckWebsitesSummary::test_summary_tri_state_matches_the_records`**
Property. Parse the triple from the `"N live / N dead / N unreachable"` line and
assert it equals, recomputed from the returned records:
`sum(r["website_live"] is True)`, `sum(r["website_live"] is False)`, and
`sum(1 for r in records if r.get("website") and r.get("website_live") is None)`.
Input: a five-record mix — one LIVE, one DEAD, one UNKNOWN, two website-less.
Assert the three numbers are `1 / 1 / 1` and that the two website-less rows are
absorbed correctly. Pins: `enricher.py:264-266`. Passes today.

---

### S2 — The S1 guard covers only `future.result()`, not the merge below it

- **Where:** `enricher.py:242-248` guards the call; `enricher.py:249-259`
  (the unpacking and `contacts["email"]` lookups) is **outside** any try
- **Breaks:** a worker returning a contacts dict missing a key raises `KeyError`
  on the main thread, after some records have already been mutated. Verified:
  stub returns `(LIVE, {"email": None})` for one URL →
  `KeyError: 'instagram'` escapes `check_websites` from `enricher.py:254`, and
  the list is left half-written (`records[0]["website_live"] is True`,
  `records[1]` untouched). No CSV is written. Unreachable through the real
  `_fetch_website` today (all three early returns at `enricher.py:178`, `:184`,
  `:192` hand back the zeroed dict built at `enricher.py:168`), so the correct
  test is preventive: assert the **shape contract** on `_fetch_website` itself.
- **Fix:** `contacts.get("email")` instead of `contacts["email"]`, or a
  `TypedDict` on the return type (`enricher.py:161`).

#### Test cases

**`TestFetchWebsiteContract::test_fetch_website_always_returns_all_four_contact_keys`**
Patch `enricher.get_session` with a fake response
(`.status_code`, `.encoding`, `.raw.read(n, decode_content=False)`, `.close()`).
For `status_code` in `(200, 404, 500)` and for a session that raises `OSError`:
assert `outcome in {LIVE, DEAD, UNKNOWN}` and
`set(contacts) == {"email", "instagram", "whatsapp", "linkedin"}`. Verified all
four paths return all four keys today. Pins: the invariant that makes
`enricher.py:252-259` safe. Passes today.

**`TestCheckWebsitesResilience::test_partial_contacts_dict_does_not_kill_the_run`**
Stub `_fetch_website` to return `(LIVE, {"email": None})`. Assert
`check_websites` does not raise and that both records get
`website_live is True`. **Fails today** (`KeyError` at `enricher.py:254`). Ship
with the `.get()` fix from 064/`enricher.py:252-259`.

---

### S2 — Known contacts are never overwritten, but the guard is untested and truthiness-only

- **Where:** `enricher.py:252-259`
- **Breaks / edge:** the four `if not r.get(x) and contacts[x]` guards are
  correct in intent and completely unpinned. Separately, they use bare truthiness,
  so a whitespace-only value wins: verified that a record arriving with
  `email="   "` keeps it and the genuinely extracted address is discarded. That is
  a real input — `main.load_master` at `main.py:88-89` nulls `""` but not
  `"   "`, so an externally edited master row produces exactly this — and
  `dedup._is_missing` at `dedup.py:79-90` already handles it correctly with
  `.strip()`. The two modules disagree on what "missing" means.
- **Fix:** reuse `dedup._is_missing` for the four guards.

#### Test cases

**`TestContactMergePolicy::test_known_contact_survives_a_live_fetch`**
Property over all four channels. Input record with
`email="owner@real.example", instagram="ownerig", whatsapp="+96170123456",
linkedin="https://linkedin.com/company/ownerco"`, worker returns
`(LIVE, contacts(email="new@wixpress.com", instagram="newig", whatsapp="+19999999999",
linkedin="https://linkedin.com/company/newco"))`. Assert all four values are
unchanged. Passes today; pins `enricher.py:252-259`.

**`TestContactMergePolicy::test_missing_contact_is_filled_from_a_live_fetch`**
Same worker; input record with `email="owner2@real.example"` only. Assert
`email` unchanged, `instagram == "newig"`, `whatsapp == "+19999999999"`,
`linkedin == "https://linkedin.com/company/newco"`. Passes today.

**`TestContactMergePolicy::test_contacts_are_written_only_on_live`**
For `outcome in (DEAD, UNKNOWN)`, stub returns full contacts
(`email="ghost@x.example", instagram="ghostig", whatsapp="+10000000000",
linkedin="https://linkedin.com/company/ghostco"`) and assert **none** of the four
keys appear in the record. Verified passes today. Pins `enricher.py:251`, which is
currently unreachable through the real `_fetch_website` (all early returns hand
back a zeroed dict) — pin it anyway, because it is the exact line that must
survive any refactor that moves extraction earlier.

**`TestContactMergePolicy::test_whitespace_only_contact_does_not_block_extraction`**
Input `{"website": "https://x.example/1", "email": "   "}`, worker returns
`(LIVE, contacts(email="real@x.example"))`. Assert
`records[0]["email"] == "real@x.example"`. **Fails today** — `"   "` is truthy,
so `enricher.py:252` skips. Pins the `dedup._is_missing` inconsistency above.

---

### S2 — Records without a website are never targeted, never mutated, never gain a `website_live` key

- **Where:** `enricher.py:231` (`if r.get("website")`), `enricher.py:232-233`
- **Breaks:** nothing today. But `cli.cmd_stats` at `cli.py:108` counts `dead` as
  `sum(1 for r in rows if (r.get("website_live") or "").strip() == "False")`
  over **all** rows with no `website` gate, while its `unknown` counter at
  `cli.py:109-110` *is* gated. If `check_websites` ever starts writing a value onto
  a website-less row — a one-line "safety" change such as hoisting a
  `r.setdefault("website_live", None)` into the loop — `leadminer stats` reports
  phantom dead sites. `tests/test_lead_signal.py:211-226` tests `cmd_stats` but
  with a hand-built CSV, so it cannot catch this.
- **Fix:** none needed — this is the test that was owed.

#### Test cases

**`TestCheckWebsitesContract::test_records_without_a_website_are_never_fetched_or_touched`**
```python
fetched = []
records = [{"name":"a","website":"https://x.example/1"},
           {"name":"b","website":None},
           {"name":"c","website":""},
           {"name":"d"}]
check_websites(records, workers=2)   # worker records its arg in `fetched`
```
Assert `fetched == ["https://x.example/1"]`; `out[1] is records[1]` (same dict
object, not a copy); `out[1] == {"name":"b","website":None}` (deep-equal, so not
even a `website_live: None` added);
`"website_live" not in out[1] and "website_live" not in out[2] and "website_live" not in out[3]`.
Verified passes today. Pins `enricher.py:231` and the `cli.py:108` hazard.

**`TestCheckWebsitesContract::test_no_targets_means_no_pool_no_fetch_no_output`**
Input `[]`, and `{"name":"a","website":None}`.
Assert the return value **is** the input list object (`out is records`), and that
`_fetch_website` was called 0 times, and that stdout is `""`.
Verified passes today. Pins `enricher.py:232-233` — the early return that keeps a
no-website run from spinning up 40 threads.

---

### S3 — `workers` is honoured, unvalidated, and never exercised

- **Where:** `enricher.py:237`; the sole caller `enricher.py:337` passes no
  `workers`, so the default `40` at `enricher.py:225` is the only value ever used
- **Breaks:** verified — `workers=0` → `ValueError: max_workers must be greater
  than 0`; `workers=-1` → same; `workers="3"` → `TypeError`. All three fire
  **after** `enricher.py:235` has already printed
  `[Enricher] Fetching N websites — liveness + contacts (0 workers)...`, so a
  misconfigured caller gets a log line claiming work has started and then a
  traceback. Low severity: unreachable from `main.py` or `cli.py` today.
- **Fix:** `if workers < 1: raise ValueError("workers must be >= 1")` at the top,
  before the print.

#### Test cases

**`TestCheckWebsitesConcurrency::test_workers_bounds_in_flight_requests`**
Stub `_fetch_website` to increment a counter under a `threading.Lock`, `sleep(0.03)`,
decrement, and return `(LIVE, NO_CONTACTS)`. Input: 24 records, `workers=4`.
Assert peak in-flight `<= 4`. Verified: peak is exactly 4. Then repeat with
`workers=1` and assert peak is exactly 1. Pins `enricher.py:237` actually consuming
the parameter — otherwise a future `max_workers=min(workers, len(targets))` or a
hardcoded constant would silently pass.

**`TestCheckWebsitesConcurrency::test_results_are_independent_of_worker_count`**
Property. Base input: 30 records at `https://x.example/{0..29}`, worker returns
`[LIVE, DEAD, UNKNOWN][n % 3]` after `sleep(0.001 * (n % 5))` so completion order
is scrambled. Assert `check_websites(deepcopy(base), workers=1) ==
check_websites(deepcopy(base), workers=32)`. Verified passes today. Pins every
concurrency bug in the function at once, cheaply.

**`TestCheckWebsitesConcurrency::test_invalid_workers_is_rejected_before_the_log_line`**
For `workers in (0, -1)`: assert `ValueError` is raised **and** stdout is `""`.
**Fails today** (the `enricher.py:235` print precedes the pool).

---

### S3 — One GET per record, no URL coalescing

- **Where:** `enricher.py:231`, `enricher.py:238`
- **Breaks:** verified — two records sharing `https://dup.example` produce **two**
  concurrent GETs to the same host. Four audits flag this as the linear cost of the
  pipeline (`docs/audits/033:13`, `docs/audits/035:27`, `docs/audits/053:12`,
  `docs/audits/008:118`), so coalescing or a per-host rate limiter is a
  near-certain future change. That change must be pinned by a test that *asserts
  the coalescing*, not discovered in production.
- **Fix:** coalesce `targets` by URL before submitting, then fan results back to
  every owning index.

#### Test case

**`TestCheckWebsitesContract::test_duplicate_urls_are_fetched_once_per_record`**
Characterization test of current behaviour. Input: `[{"name":"a","website":"https://dup.example"},
{"name":"b","website":"https://dup.example"}]`; worker appends its arg to a list.
Assert `len(fetched) == 2` and `out[0]["website_live"] is out[1]["website_live"] is True`.
Verified passes today. Update to `assertEqual(len(fetched), 1)` when coalescing
lands — leaving it at 2 means the optimisation is invisible to CI.

---

### S3 — Progress reporting and stdout discipline

- **Where:** `enricher.py:235`, `enricher.py:260-262`, `enricher.py:271-275`
- **Breaks:** `done % 200 == 0` at `enricher.py:261` fires after `done += 1`, so
  verified behaviour on 450 targets is exactly two lines
  (`200/450`, `400/450`). An off-by-one on the modulo silently drops the final
  line and, on a 300-minute run, the progress output is the only signal an
  operator has. Separately, `check_websites` uses bare `print` — three lines for
  a single-record run (verified) — interleaved with the whole pipeline's output,
  while `httpclient.py:45` already establishes
  `log = logging.getLogger(__name__)` and `enricher.py:327-328` already mutes
  urllib3 warnings.
- **Fix:** use `log.info` / `log.warning`; keep the modulo but test it.

#### Test cases

**`TestCheckWebsitesSummary::test_progress_line_fires_every_200_targets`**
Input: 450 records, `workers=8`. Assert exactly 2 lines contain `"done..."` and
they are `200/450` and `400/450`. Verified passes today. Add a 199- and
200-target case to pin the boundary.

**`TestCheckWebsitesSummary::test_summary_is_logged_not_printed`**
Assert nothing reaches stdout (capture both `stdout` and, if `enricher` gains a
logger, `caplog`). **Fails today.**

---

## What genuinely cannot be tested here, and why

These are not omissions. They are limits of an offline test, and writing "tests"
for them would produce assertions about the test double rather than about the code.

1. **Whether `DEAD`/`LIVE`/`UNKNOWN` is the *correct* verdict for a given real
   site.** The entire point of the tri-state is that we often do not know
   (`enricher.py:151-153`). No test converts an epistemic limit into a fact. What
   *is* testable is the code-level rule — "a transport failure is not `DEAD`",
   "a 403 is not `DEAD`" — and that is what the first four cases above pin.
   Everything past that needs a live server, and the brief forbids network calls.

2. **The 8 s timeout and the 200 KB body cap as *resource* guarantees.**
   `enricher.py:175` (`timeout=8`) and `enricher.py:188`
   (`r.raw.read(_MAX_BODY_BYTES, decode_content=True)`, `200_000` from
   `httpclient.py:54`) bound bytes read, but `stream=True` means a slow-loris
   still holds a worker for the full 8 s. A test asserting "returns within 9 s"
   is a flake generator on a loaded CI runner; a test asserting "`raw.read` was
   called with `200_000`" requires a fake and therefore proves only that the
   constant was passed. I verified the literal call shape
   (`timeout=8, allow_redirects=True, stream=True`) and `_MAX_BODY_BYTES == 200000`,
   and that is as far as an offline test honestly goes.

3. **Politeness / rate limiting.** 40 concurrent GETs to one host
   (`docs/audits/005:125`, `docs/audits/008:118`) is a property of the network and
   of the *victim's* server, not of our code. No offline test distinguishes "we
   sent 40 requests to example.com" from "we sent 4" — both are 40 calls into a
   stub. A call-counting test proves bookkeeping, not restraint.

4. **DNS, TLS, proxy behaviour, and whether a host is genuinely reachable.** By
   definition untestable offline. The nearest honest substitute is asserting the
   *exception taxonomy* — that `SSLError`, `ConnectionError` and `Timeout` all
   land in the same `UNKNOWN` bucket at `enricher.py:176-178` — which is a
   statement about our code, and is worth having.

5. **Wall-clock cost of the pool at 40 workers.** Whether `workers=40` saturates a
   7 GB GitHub runner is a property of the runner. `TestCheckWebsitesConcurrency::
   test_workers_bounds_in_flight_requests` pins the *only* part that is ours: that
   we do not exceed what we asked for.

6. **The quadratic `_EMAIL_RE` blowup — and the test for it must be bounded.**
   `_EMAIL_RE` (`enricher.py:130`) applied at `enricher.py:200` is O(n²) on a long
   token with no `@`. Measured on a body of `"x" * n`:

   | n | `_EMAIL_RE.findall` |
   |---|---|
   | 4,000 | 35 ms |
   | 8,000 | 612 ms |
   | 16,000 | 2.68 s |
   | 32,000 | 10.2 s |
   | 65,536 | 33.9 s |

   Extrapolating quadratically to the 200 KB cap at `enricher.py:158` gives
   **~5 minutes of CPU in a single worker**, from one page — and a 200 KB base64
   blob or minified JS bundle with no `@` is completely ordinary. Forty such pages
   wedge all 40 workers and `check_websites` never returns, which is the hang
   predicted in `docs/audits/008-enricher-ssrf-security.md:87`. **The natural test
   — "feed a big body and assert extraction completes" — hangs CI instead of
   failing**, which is why it does not exist. The testable version must be
   time-bounded, e.g.:
   ```python
   def test_contact_extraction_is_bounded_on_a_hostile_body(self):
       # 200 KB of one unbroken token, no '@'. _EMAIL_RE is O(n^2) here.
       faulthandler.dump_traceback_later(10, exit=True)   # hard ceiling
       start = time.perf_counter()
       _fetch_website("https://x.example/1")              # via enricher.get_session fake
       self.assertLess(time.perf_counter() - start, 1.0)
   ```
   `signal.alarm` on POSIX, `faulthandler.dump_traceback_later` as the belt-and-braces
   version. This belongs in the `_fetch_website` report (`docs/audits/380-*`), but
   `check_websites` is where the consequence lands: it is the only caller, it owns
   the 40-way pool, and nothing today bounds one worker's runtime.

7. **Whether the Instagram over-extraction is real in the wild.** Verified that
   `_INSTAGRAM_RE` (`enricher.py:131`) matched `real.example` out of
   `<a href="mailto:hi@real.example">` — the `@` alternation grabbed the email —
   so `check_websites` writes `instagram="real.example"` into the CSV via
   `enricher.py:254`. `_IG_BLACKLIST` (`enricher.py:142`) filters handles but there
   is no domain guard at all. Whether real sites produce this often enough to
   matter needs corpus data we do not have; the unit test is deterministic and
   belongs to the `_fetch_website` lens.

---

## Not a bug, but worth knowing

- **The tri-state mapping uses `==`, not `is`** (`enricher.py:250`). Verified
  behaviour for out-of-contract worker returns: `"live"` → `True`, `True` → `None`,
  `False` → `None`, `None` → `None`, `"weird"` → `None`. The fallback direction is
  correct (anything unrecognised degrades to `UNKNOWN`, never to `DEAD`) — but a
  refactor changing `LIVE`/`DEAD`/`UNKNOWN` from bare strings
  (`enricher.py:154-156`) to an `Enum` or to booleans silently makes
  `Enum == "live"` false and turns **every** website into `UNKNOWN`, which reads
  as "our network is down" and costs a full re-run. `tests/test_lead_signal.py:98`
  asserts only that the three constants are *distinct* — not their values or
  types. Add
  `TestLeadSignalIsNotInverted::test_outcome_constants_are_the_strings_csv_expects`
  asserting `(LIVE, DEAD, UNKNOWN) == ("live", "dead", "unknown")` and
  `all(isinstance(c, str) for c in (LIVE, DEAD, UNKNOWN))`. The CSV layer depends
  on the string form: `main.write_csv` at `main.py:125` stringifies with
  `str(True)`/`str(None)`, and `main.load_master` at `main.py:103` parses the
  literal strings `"True"`/`"False"`.
- **`check_websites` returns the input list by identity** (`enricher.py:233`,
  `enricher.py:276`) — verified `out is records` on both paths. It mutates in
  place, never copies, never reorders, never inserts. Every caller relies on this
  silently: `enricher.py:337` reassigns the result, `main.py:187` reassigns again.
- **`live_count` and `dead_count` (`enricher.py:264-265`) are not gated on
  `r.get("website")` while `unknown_count` (`enricher.py:266`) is.** Harmless
  today, because only targeted records ever get `website_live` written. It becomes
  wrong the moment a record arrives carrying a pre-set `website_live` and no
  website — which `main.load_master:103` will happily produce from an
  externally edited CSV, and which `dedup._merge` can produce since `website` and
  `website_live` are both in `_VOLATILE` (`dedup.py:114`) and are picked
  independently.
- **`enricher.py:346` `r.setdefault("website_live", None)` masks omissions.** Any
  path in `check_websites` that forgets to write the key is silently filled with
  `None` two lines later, and `None` is indistinguishable from a real `UNKNOWN`.
  This is why `test_records_without_a_website_are_never_fetched_or_touched` must
  assert **key absence**, not `is None` — `setdefault` makes the latter vacuous.
- **`enrich` computes `lead_score` before `industry_priority` exists.**
  Verified: after `enrich`, `"industry_priority" not in records[0]` and
  `lead_score == 45` (email 20 + phone 15 + live 10, no priority bonus). This is
  by design — `main.py:195-197` recomputes — but it means `lead_score` is only
  correct after `main.main()` line 197. A caller that uses `enrich()` directly
  (nothing does today) ships an understated score. Worth one test on `main`'s
  recompute, not on `check_websites`.

---

## Test catalogue

All in a new `tests/test_check_websites.py`. Stdlib `unittest`, matching
`tests/test_lead_signal.py:17`. Note two harness prerequisites:

- **The worker is patchable with no seam.** `enricher.py:238` resolves
  `_fetch_website` from module globals at submit time, so
  `mock.patch.object(enricher, "_fetch_website", stub)` works and
  `check_websites` needs no injection parameter.
- **`get_session` must be patched at `enricher.get_session`**, not
  `httpclient.get_session` — `enricher.py:128` does
  `from httpclient import DEFAULT_MAX_BODY_BYTES, get_session`, binding the name
  into `enricher`'s globals at import.
- `_stub_requests()` (`tests/test_lead_signal.py:23-60`) must run before importing
  `enricher`, or the new file needs a real `requests` in the test env. Extracting
  it to `tests/_stub.py` and importing from both files is the clean fix;
  `pyproject.toml` already declares `pytest>=8.0` and `hypothesis>=6.100` in the
  `dev` extra, so either runner is available.

| # | Sev | Test method | Status | Pins |
|---|---|---|---|---|
| 1 | S1 | `TestWebsiteOutcomeClassification::test_403_is_unknown_not_dead` | fails | 403/Cloudflare scored DEAD |
| 2 | S1 | `TestWebsiteOutcomeClassification::test_429_is_unknown_not_dead` | fails | 429 scored DEAD |
| 3 | S1 | `TestWebsiteOutcomeClassification::test_404_and_410_remain_dead` | passes | over-correction guard |
| 4 | S1 | `TestWebsiteOutcomeClassification::test_status_boundary_sweep_is_a_total_function` | passes | classification totality |
| 5 | S1 | `TestCheckWebsitesContract::test_fetch_verdict_always_overwrites_incoming_website_live` | passes | `dedup._VOLATILE` desync; future caching |
| 6 | S1 | `TestCheckWebsitesResilience::test_worker_exception_yields_unknown_and_does_not_abort_batch` | passes | naked `future.result()` S1 (audit 029:43) |
| 7 | S1 | `TestCheckWebsitesContract::test_result_lands_on_the_record_that_owns_the_url` | passes | index map under `as_completed` (audit 032:48) |
| 8 | S2 | `TestCheckWebsitesSummary::test_summary_contact_counts_report_extractions_not_inventory` | fails | `enricher.py:267-275` counts inventory |
| 9 | S2 | `TestCheckWebsitesSummary::test_summary_tri_state_matches_the_records` | passes | `enricher.py:264-266` |
| 10 | S2 | `TestFetchWebsiteContract::test_fetch_website_always_returns_all_four_contact_keys` | passes | shape contract for `enricher.py:252-259` |
| 11 | S2 | `TestCheckWebsitesResilience::test_partial_contacts_dict_does_not_kill_the_run` | fails | `KeyError` outside the guard |
| 12 | S2 | `TestContactMergePolicy::test_known_contact_survives_a_live_fetch` | passes | `enricher.py:252-259` |
| 13 | S2 | `TestContactMergePolicy::test_missing_contact_is_filled_from_a_live_fetch` | passes | `enricher.py:252-259` |
| 14 | S2 | `TestContactMergePolicy::test_contacts_are_written_only_on_live` | passes | `enricher.py:251` guard |
| 15 | S2 | `TestContactMergePolicy::test_whitespace_only_contact_does_not_block_extraction` | fails | truthiness vs `dedup._is_missing` |
| 16 | S2 | `TestCheckWebsitesContract::test_records_without_a_website_are_never_fetched_or_touched` | passes | `enricher.py:231`; `cli.py:108` |
| 17 | S2 | `TestCheckWebsitesContract::test_no_targets_means_no_pool_no_fetch_no_output` | passes | `enricher.py:232-233` |
| 18 | S3 | `TestCheckWebsitesConcurrency::test_workers_bounds_in_flight_requests` | passes | `enricher.py:237` |
| 19 | S3 | `TestCheckWebsitesConcurrency::test_results_are_independent_of_worker_count` | passes | all concurrency at once |
| 20 | S3 | `TestCheckWebsitesConcurrency::test_invalid_workers_is_rejected_before_the_log_line` | fails | `enricher.py:235` precedes `:237` |
| 21 | S3 | `TestCheckWebsitesContract::test_duplicate_urls_are_fetched_once_per_record` | passes | `enricher.py:231/238` (flip when coalescing lands) |
| 22 | S3 | `TestCheckWebsitesSummary::test_progress_line_fires_every_200_targets` | passes | `enricher.py:261` modulo |
| 23 | S3 | `TestCheckWebsitesSummary::test_summary_is_logged_not_printed` | fails | bare `print` at `:235/:271/:272` |
| 24 | S3 | `TestLeadSignalIsNotInverted::test_outcome_constants_are_the_strings_csv_expects` | passes | `enricher.py:154-156` ↔ `main.py:103/125` |
| 25 | — | `TestContactExtractionBound::test_contact_extraction_is_bounded_on_a_hostile_body` | hangs today | quadratic `_EMAIL_RE`; **must be time-bounded** |

25 cases. 8 fail against `99493b9` — 5 of them (1, 2, 8, 11, 15) are defects in
the target function, 3 (20, 23, 25) are defects it should not have. The rest
characterise behaviour that is correct today and must stay correct.

---

## Recommended order of work

1. **#6, #7, #5** — write first. They are pure characterization of code that is
   already correct, they cost nothing, and #5 in particular must land *before* the
   caching/incremental work proposed in `docs/audits/033`, `035` and `053`, or
   that work will ship a fabricated `DEAD` verdict against a live site.
2. **#1, #2, #3, #4** — the 403/429 reclassification, plus the boundary sweep that
   proves the fix is not over-broad. Highest user-visible harm in this file: today
   every Cloudflare-protected site in the master is a top-priority "rebuild" lead.
3. **#8** — the summary counters. Cheapest fix in the report (two lines of
   arithmetic) and the only number an operator currently has on whether enrichment
   is working. Keep it as `xfail` for one commit so CI records the gap.
4. **#12, #13, #14, #16, #17** — the merge-policy and targeting contract. Cheap,
   and they are the tests that make #15's fix (use `dedup._is_missing`) safe.
5. **#25** — the time-bounded hostile-body test. Write it with `faulthandler`
   *before* touching `_EMAIL_RE`, so the current quadratic behaviour shows up as a
   red test rather than a hung runner. Fix belongs in
   `docs/audits/380-fn-enricher-py-_fetch_website-contract.md`.
6. **#10, #11** — the contacts-dict shape contract, then the `.get()` fix at
   `enricher.py:252-259`.
7. **#18, #19, #21, #22** — concurrency and progress characterization, to be in
   place before any politeness/rate-limiter change (audit 005, 008, 054).
8. **#9, #24** — cheap invariant pins.
9. **#15, #20, #23** — hygiene, batch them.
