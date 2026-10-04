# 335 — `_recency` (dedup.py:155) — ops lens

## Verdict

`_recency` is a two-line accessor with no validation, no logging, and no observability, and
in production it is **the weakest of the four rank keys** (`dedup.py:183`) and **only reachable
in the narrow case where two records come from the same source and the same run** — where it
ties anyway, because each scraper stamps `scraped_at` exactly once at scrape start
(`scrapers/osm.py:32`, `scrapers/google_places.py:173`, `scrapers/wikidata.py:67`). Meanwhile
the fold in `dedup()` is **non-associative and fed a nondeterministic arrival order**, so
`_recency`'s caller produces a different answer for the same input set on different runs, and
`_merge` **launders an old value's freshness onto a new timestamp**. The worst part is not the
logic, it is that *nothing anywhere observes `scraped_at`*: not `main.py`, not `cmd_validate`
(`cli.py:135-201`), not `cmd_stats`, not the CI gate (`scrape.yml:71-83` is `wc -l` only), and
there is no `logging.basicConfig` in the repo. **Detection time for every finding below is
therefore bounded by a customer noticing, not by a tool noticing.**

All findings verified by executing `dedup.py` locally (pure, no network, no installs).

---

## Reach: where `_recency` can and cannot decide anything

`_recency` is consulted at exactly one place, `dedup.py:183`, as the **third** element of the
rank tuple, after `_trust_for` and `_validity`, and before the `str(value)` tiebreak:

```python
def rank(rec, value):
    return (
        _trust_for(field, rec),                          # 1st
        _validity(field, value),                         # 2nd
        _recency(rec) if field in _VOLATILE else "",     # 3rd  <- here
        str(value),                                      # 4th
    )
```

It is also short-circuited for two fields entirely: `_pick` handles `source` (`dedup.py:173`)
and `scraped_at` (`:176`) before `rank()` is ever built, so `_recency` never decides the
`scraped_at` column itself. `_VOLATILE` is `{"rating", "review_count", "website", "website_live"}`
(`dedup.py:114`).

Verified reachability:

```
cross-source (trust differs first)     rating winner = 4.8   <- decided at key 1
cross-run same source (recency decides) rating winner = 4.8   <- decided at key 3
same-run same-source website conflict   winner = https://zzz.example  <- decided at key 4
```

That third line is the common production case, and `_recency` is not consulted: both records
carry the identical `scraped_at` because `google_places.py:173` computes it once before the
thread pool, so the recency key ties and the winner is decided by **lexicographic URL order**.

---

## Findings

### S1 — `_merge` launders a new timestamp onto an old value, and the laundered row then wins forever

- **Where:** `dedup.py:155-156` (record-level timestamp used as a field-level freshness claim),
  combined with `dedup.py:176-177` and `dedup.py:168-171`
- **Breaks:** `scraped_at` is a property of the **record** (when this pipeline saw this business),
  but `dedup.py:183` uses it to adjudicate a **single field** (when this particular URL / rating
  was true). Merging a fresh observation that has *no* website with a stale one that does
  produces `scraped_at = fresh` + `website = 2024 URL`, because `_pick` takes the old website
  via the missing-value short-circuit at `dedup.py:168-171` while taking the new timestamp via
  `max()` at `dedup.py:177`. From that moment the row claims to be freshly observed, and it
  outranks genuinely fresh rows on every subsequent merge.
- **Trigger** (executed):
  ```
  A = website "http://cafe-old.example", scraped_at "2024-01-01T00:00:00.000000+00:00", source osm
  B = website None,                     scraped_at "2026-10-03T10:00:00.000000+00:00", source google_places
  _merge(A, B) -> scraped_at = 2026-10-03T10:00:00.000000+00:00
                  website    = http://cafe-old.example
  ```
  Then merging that result with `C = {"website": "https://cafe-new.example",
  "scraped_at": "2026-10-04T..."}` still yields `http://cafe-old.example`.
- **Blast radius:** `_VOLATILE` is the four fields a sales pitch is built on. `website` feeds
  `without_websites.csv` / `with_websites.csv` (`main.py:199-200`), `lead_score`
  (`enricher.py:300-304`), and `recommended_service`. A dead 2024 URL pinned in the row means
  the business is permanently miscategorised as "has a site", is never pitched the rebuild
  service it is actually qualified for, and `scraped_at` actively defends the wrong value.
- **Fix:** carry per-field observation timestamps, or at minimum make `scraped_at` reflect the
  *oldest* contributing observation for fields that were carried over, not `max()`. Better:
  stop collapsing N observations into one dict (`dedup.py:191-194`) — see audit 034.

### S1 — The merge fold is non-associative and the arrival order is nondeterministic, so `all_businesses.csv` is a nondeterministic function of its input

- **Where:** `dedup.py:208` / `:216` (fold over `phone_index` / `name_index`) fed by
  `main.py:160-166` (`as_completed`) and `main.py:179` (`combined = raw_filtered + master`)
- **Breaks:** `_pick` is pairwise commutative (verified: `_merge(a,b) == _merge(b,a)` is
  `True`), but the **fold is not associative**, because `_merge` collapses two records into one
  dict and destroys per-field provenance. `_merge(_merge(X,Y),Z) != _merge(X,_merge(Y,Z))`.
  `as_completed` yields in completion order, so `raw` order changes every run. The output
  therefore churns on identical inputs.
- **Trigger** (executed) — 4 observations of one business, all 24 permutations:
  ```
  distinct results from 24 orderings of identical input: 3
    http://cafe-old.example  rating=5.0  scraped_at=2026-10-03  <- 14/24 orderings
    http://cafe-old.example  rating=1.0  scraped_at=2026-10-03  <-  6/24 orderings
    http://cafe-old.example  rating=3.5  scraped_at=2026-10-03  <-  4/24 orderings
  ```
  Note the `website` is the laundered 2024 URL in **all 24** — S1 #1 and S1 #2 compound.
- **Why this is worse than a code smell for ops:** the master is cumulative and is the only
  state the system has (`scrape.yml:50-63`). If row content churns between runs for reasons
  unrelated to the world, then no diff between two masters means anything, no regression can be
  attributed, and "what changed since last week" is unanswerable. A human comparing two runs
  sees churn they cannot explain and has no way to distinguish churn from real signal.
- **False confidence:** `tests/test_lead_signal.py:292-295` (`test_merge_is_order_independent`)
  only asserts pairwise commutativity of a single `_merge`. It passes, and it does not cover
  the fold. That is the test that makes this look solved.
- **Fix:** sort `combined` by a total, content-derived key before folding (e.g.
  `sorted(combined, key=lambda r: (normalize_phone(...) or normalize_name(...), str(r.get("source")), str(r))))`,
  or switch `dedup` to a group-then-reduce that sorts each group before reducing — associative
  by construction. Add a test that permutes a 4-record group and asserts one result.

### S1 — Nothing in the system observes `scraped_at`, so a fully dead pipeline goes green

- **Where:** absent from `main.py` (only mention is the column list at `main.py:39`),
  `cli.py:135-201` (`cmd_validate`), `cli.py:79-132` (`cmd_stats`), `scrape.yml:71-83`
- **Breaks:** `cmd_validate` checks exactly five things — blank names (`:151`), BOM (`:154`),
  coordinates (`:164`), rating range (`:175`), duplicate `(name, phone)` (`:190`). It never
  looks at `scraped_at`. `cmd_stats` reports no age range, no newest/oldest row, no run id.
  `main.py`'s summary (`:219-242`) prints counts only. The workflow's "Validate output before
  publishing" step is a `wc -l` comparison against 100 (`scrape.yml:78-83`) and **never calls
  `leadminer validate`** — the gate exists as a subcommand but is not wired into CI. There is
  no `logging.basicConfig` anywhere in the repo, so even `httpclient.py:178` / `:204`
  `log.debug` calls are invisible at default level. There is no `run_id`, no run manifest, and
  no state file (grepped: zero hits for `run_id|manifest|last_run|state.json`).
- **Trigger** (executed): all three scrapers fail — `osm.py:56`, `wikidata.py:85` and
  `google_places.py:171` each `print` and `return` an empty batch. `raw_filtered` is `[]`, so
  `combined == master` (main.py:179) and:
  ```
  dedup(master) rows: 2   identical to master? True
  scraped_at values : ['2025-10-06T03:00:00.000000+00:00']
  ```
  The run exits 0. `scrape.yml:80`'s `ROWS < 100` gate passes because the *master* is large.
  The summary prints normal-looking numbers. The CSVs on Drive are republished. The only
  evidence anything is wrong is that `scraped_at` did not move — and nothing prints it.
- **Fix:** three lines in `main.py` after dedup (`min`/`max` `scraped_at`, count of rows older
  than 8 days, count of rows with blank `scraped_at`, printed to stderr and echoed with
  `::error::` when stale); a `scraped_at` parse check in `cmd_validate`; and call
  `leadminer validate` in `scrape.yml` before the upload step.

### S2 — `_recency` collapses falsy junk to `""`, which the rank treats as "infinitely old" rather than "unknown"

- **Where:** `dedup.py:155-156`
- **Breaks:** `str(record.get("scraped_at") or "")` maps `None`, `""`, `0`, `0.0`, `False`,
  `[]`, `{}` all to `""`. `""` sorts below every real timestamp, so such a record loses every
  volatile-field tiebreak to any record that has *any* timestamp — silently and permanently.
  `load_master` turns blank CSV cells into `None` (`main.py:87-89`) and never normalises the
  column otherwise (`main.py:86-104`), so `""` is a value the pipeline itself manufactures.
- **Trigger** (executed):
  ```
  None -> ''   '' -> ''   0 -> ''   0.0 -> ''   False -> ''   [] -> ''   {} -> ''
  '  ' -> '  '                 <- truthy, survives
  'N/A' -> 'N/A'  'null' -> 'null'  'None' -> 'None'   <- arbitrary, treated as a real key
  1780000000 -> '1780000000'  b'2026-10-03T00:00:00+00:00' -> "b'2026-10-03T00:00:00+00:00'"
  ```
  `'N/A'` and `'null'` sort *above* `''` and are treated as timestamps; the `bytes` case
  produces the literal repr `"b'...'"`, which compares as text.
- **Fix:** parse and validate — `try: datetime.fromisoformat(raw) except ValueError: return ""`
  plus a counter surfaced through the S1 #3 observability fix, so "N rows had an unparseable
  scraped_at" is a visible number instead of a silent tiebreak loss.

### S2 — `website_live` is listed in `_VOLATILE` but `_recency` can never decide it

- **Where:** `dedup.py:114` vs `dedup.py:168-171`, combined with `main.py:180` (dedup) running
  **before** `main.py:187` (enrich)
- **Breaks:** dedup runs before `enrich()`, so a freshly scraped record always has
  `website_live = None`, which `_is_missing` (`dedup.py:86-87`) reports missing, so `_pick`
  short-circuits at `dedup.py:170-171` and returns the stale master value. `rank()` — and
  therefore `_recency` — is never built for that field.
- **Trigger** (executed):
  ```
  _pick("website_live", stale=True/2020, fresh=None/2026) -> True   (stale wins)
  ```
  This one is **not** a wrong-output bug: `enricher.py:250` overwrites `website_live` for every
  row with a website right after. It is dead configuration that reads as if recency governs
  liveness. The real cost is that a maintainer editing `_VOLATILE` will assume
  `website_live` is recency-governed and will not look for why it never moves.
- **Fix:** drop `website_live` from `_VOLATILE` and add a comment stating dedup precedes
  enrichment, so liveness is exclusively an enrich-stage field.

---

## Silent-failure ranking — how long until a human notices

Ordered by time-to-detection, longest first. "Detected by" is what actually fires.

| # | Failure | Detected by | Time to notice |
|---|---|---|---|
| 1 | All sources fail / network drops → run exits 0, republishes stale master (S1 #3) | nothing; a salesperson dialling a number that stopped working | **indefinite** |
| 2 | Freshness laundering pins a 2024 URL as "fresh" (S1 #1) | a customer reporting we pitched a dead site; `without_websites.csv` quietly under-counts targets | weeks–months, cause invisible |
| 3 | Nondeterministic fold churns the master every run (S1 #2) | an operator diffing two masters and giving up on the diff | months |
| 4 | Junk `scraped_at` → `""` → loses every volatile tiebreak (S2) | nothing, ever | **indefinite** |
| 5 | `website_live` volatility unreachable (S2) | nothing (masked by enrich) | **indefinite**, benign |
| 6 | `_recency` unreachable because trust decides first (reach section) | nothing; function looks correct and is tested (`test_lead_signal.py:303`) | **indefinite**, benign |

**Explicitly not on this list — interruption.** `_recency` is pure: no I/O, no locks, no shared
mutable state, no writes. `KeyboardInterrupt` inside `dedup()` cannot corrupt anything, and
`write_csv` is atomic (`main.py:121-133`, temp file + `fsync` + `os.replace`, with the temp
cleaned up on `BaseException`). The real interruption risk is upstream of this function:
`scrape.yml:25` sets `timeout-minutes: 300`, so **a six-hour job is killed by design at five
hours**. On a hard job timeout the `Validate output` and `Upload to Google Drive` steps never
run, so Drive keeps the previous master and `scraped_at` ages a full week per consecutive
timeout. The `Upload run artifacts` step is `if: always()` (`scrape.yml:93`) but does not
execute after a job-level timeout. Related: `dedup()` holds the whole combined list plus three
index dicts in memory at `main.py:180`, so an OOM kill is the other silent-loss path (see
audit 052). Neither is `_recency`'s fault, but both make `scraped_at` the only surviving
evidence of a lost run — and per S1 #3 nothing reads it.

**Explicitly not on this list — empty list.** `dedup([])` returns `[]` (executed); `dedup([{}])`
returns `[{}]` (executed). `_recency` is never called, no exception, no log. This is correct
behaviour. It is only a risk via the interaction in S1 #3, where an empty scrape is
indistinguishable from a successful one.

---

## Checked and dismissed — the `Z` vs `+00:00` timestamp claim

Audits 013, 021, 032 and 065 all assert that the `Z`-to-`+00:00` suffix change breaks
lexicographic `max()`. I tested it and **this does not reach production.** `utc_now_iso()`
(`httpclient.py:220-229`) did change the suffix in commit `607e729` (2026-10-03 19:17) from
`datetime.utcnow().isoformat() + "Z"` to `datetime.now(timezone.utc).isoformat()`, and the
cumulative master on Drive does contain both. But:

```
'2025-10-01T09:00:00.000000Z' vs '2026-10-05T03:00:00.000000+00:00'
  max -> 2026-10-05T03:00:00.000000+00:00     decided at the YEAR digit, not the suffix
```

The suffix is only reached when the date-time prefixes are **byte-identical**, which requires
two observations stamped at the same microsecond across a format migration — unreachable. The
suffix bug is real in isolation and unreachable in this pipeline; do not spend a fix cycle on
it. The four prior reports overstate it.

**One genuine (but minor) consequence of the same change, S3:** `datetime.isoformat()` omits
microseconds when `microsecond == 0`, so the emitted string is **variable width** — 32 chars
normally, 25 chars on an exact second (executed). Verified inversion:

```
'2026-10-05T03:00:00+00:00'        (25)  vs  '2026-10-05T02:59:59.999999+00:00' (32)
max() picks the 25-char one; the correct answer is the 32-char one ('+'=43 < '.'=46)
```

Probability is ~1e-6 per source per run (only 3 calls to `utc_now_iso()` per run —
`osm.py:32`, `google_places.py:173`, `wikidata.py:67`), so impact is negligible. Fix when
convenient: `datetime.now(timezone.utc).isoformat(timespec="microseconds")`, or better, parse
before comparing as S1 #1 recommends.

---

## Not a bug, but worth knowing

- **`scraped_at` is stamped once per source, at scrape start** — `osm.py:32`, `wikidata.py:67`,
  `google_places.py:173`, the last one before its thread pool is created. So within a source
  every record ties on `_recency`, and cross-source `_recency` never runs because trust decides
  first. `_recency`'s only real job is master-vs-fresh discrimination.
- **It measures when the scraper reached a record, not when the data was true.** Across a
  5-hour run, records emitted at minute 250 outrank records emitted at minute 5 by up to 245
  minutes. Combined with the trust table already preferring Google 3:2 for `rating`
  (`dedup.py:99`) over OSM's volunteer-surveyed data of unbounded age, recency double-counts
  the same prior instead of adding information.
- **`set(a) | set(b)` iteration at `dedup.py:192` is harmless.** I checked: it varies dict key
  order (and `PYTHONHASHSEED` varies it per process), but each key's value is chosen
  independently and `csv.DictWriter` uses a fixed `fieldnames=FIELDS` (`main.py:125`), so
  output bytes are unaffected. Not a finding.
- **The genuine strengths of this function**, so they are not "fixed" away: `_recency` returns
  `str` rather than a parsed object, so it can never raise `TypeError` on mixed types — which
  is precisely the crash three prior reports predicted at `dedup.py:57-58` (audits 020, 031,
  083). It never sees unhashable or unorderable input. `_pick` is genuinely pairwise
  commutative, and `_is_missing` (`dedup.py:86-90`) correctly treats `""` as missing. The
  problem is the *absence of validation*, not the presence of coercion.

---

## Recommended order of work

1. **Make the fold deterministic** (S1 #2) — sort `combined` by a content-derived key before
   `dedup`, or group-then-sort-then-reduce. One line at `main.py:179` plus a permutation test.
   Until this lands, no other fix to `_pick` can be verified, because you cannot tell whether
   a change helped.
2. **Add the `scraped_at` observability block** (S1 #3) — min/max/blank/stale counts in
   `main.py` after `dedup`, a `scraped_at` check in `cmd_validate`, and call
   `leadminer validate` from `scrape.yml:71`. This is what converts findings 4, 5 and 6 from
   "indefinite" to "one run". Cheapest fix in the report and it gates the diagnosis of all the
   others.
3. **Stop laundering freshness** (S1 #1) — the honest fix is per-field observation timestamps,
   which means not collapsing observations into one dict (`dedup.py:191-194`, see audit 034).
   A stopgap: track, per merged row, the `scraped_at` of the record that supplied each
   volatile field, and use that instead of the record-level `_recency`.
4. **Validate the timestamp** (S2) — `datetime.fromisoformat` with a rejection counter fed into
   finding 2's output. Fix the `timespec="microseconds"` width variance at the same time.
5. **Delete `website_live` from `_VOLATILE`** (S2) and note that dedup precedes enrich.
6. **Correct audits 013 / 021 / 032 / 065** on the `Z` vs `+00:00` claim — they currently
   prioritise a fix for an unreachable ordering bug over the two S1s above.
