# 399 — `lead_score`: the six hours of operational risk (ops lens)

**Target:** `enricher.py:283` `lead_score(record) -> int`
**Lens:** ops — production, weekly cron, six-hour budget, stdout-only feedback.
**Method:** every number below was produced by executing `lead_score` offline against
synthetic records (no network, no scrapers run). No repo file was modified.

---

## Verdict

`lead_score` is 37 lines, runs at ~2 µs/row (measured 501k calls/s), is pure, and
cannot be the thing that makes a run slow or unrecoverable. Its operational problem is
the opposite one: **its output is never observed by anything.** No filter, no aggregate,
no log line, no CI gate and no diff reads `lead_score` — `sales_ready.csv` is built from
`has_any_contact()` and `industry_priority` (`main.py:202-205`), `qualified_businesses.csv`
from `completeness_score` (`main.py:206`), the run summary prints counts only
(`main.py:219-242`), `leadminer stats` prints no score line (`cli.py:95-131`), and the
publish gate checks only "non-empty" and "≥ 100 rows" (`scrape.yml:71-83`). A scorer that
returns `0` for every row therefore produces **four byte-identical exports**, a green run,
and a fresh timestamped folder on Drive.

The second operational fact is that its only time-varying input, `website_live`, is
overwritten unconditionally at `enricher.py:250` — including with `None` when the probe
failed. One bad network hour erases a verdict that cost a real HTTP GET to obtain, and
moves 37% of the shipped scores by 10-20 points with a zero exit code.

---

## The six questions

### What does it print?

Nothing. `lead_score` (`enricher.py:283-319`) has no `print`, no `logger`, no counter, no
docstring. Nor does anything downstream print a statistic derived from it: the summary
block at `main.py:219-242` reports totals, region counts and pitch counts, and no score
aggregate exists anywhere in the tree. The only score-derived number ever printed is the
`changed` count in `cli.py:276`, and `cmd_score` is not in the cron path.

### What does it swallow?

Nothing, because it catches nothing — which is worse here than swallowing. Every
malformed-input path in this function is either a silent wrong answer or an unhandled
`TypeError` that kills the run at its last statement. There is no defensive coercion, no
type gate, and no record of how many rows were coerced.

### When the network drops halfway

It cannot tell. `lead_score` reads `website_live` (`enricher.py:300-304`) and cannot
distinguish "the host said 404" from "we never reached the host": both arrive as a value
that happens to be `None`, and `None` silently means "no website signal at all". Measured on
200k synthetic records with a realistic field distribution:

| Scenario | mean | median | p90 | rows scored 0 | rows scored 100 |
|---|---|---|---|---|---|
| Healthy run (70/18/12 live/dead/unreachable) | 33.81 | 33 | 55 | 4,140 | 23 |
| 50% of probes fail | 31.58 | 30 | 53 | 5,362 | 12 |
| Total blackhole (all `None`) | 29.35 | 30 | 50 | 6,621 | 0 |

**74,112 of 200,000 rows (37%) change score, every one by 10-20 points, and the run exits
0.** The rows that had earned the +20 rebuild-pitch credit lose it, so `lead_score` stops
ranking exactly the leads the module's own docstring says it exists to find
(`enricher.py:296-304`).

The mitigation that does exist: `main.py:222-223` prints `(live, dead, unreachable)` and
would read `(0 live, 0 dead, 41832 unreachable)`. So the outage is visible **in the log**;
its effect on the score column is visible nowhere.

### When a dependency returns garbage

| Garbage from upstream | `lead_score` behaviour | Verified |
|---|---|---|
| `rating` is `"3.2"` (any string) | `TypeError: '<' not supported between instances of 'str' and 'float'` at `enricher.py:313` | yes |
| `rating` is `""` | same `TypeError` | yes |
| `rating` is `[]` (any non-number JSON) | same `TypeError` | yes |
| `rating` is `float("nan")` (from `load_master`'s `float()` at `main.py:93`) | silently scores as if there were no rating | yes |
| `rating` cell is `"3,1"` (European decimal comma — plausible for an operator in Beirut editing the master in Sheets) | `ValueError` → `None` at `main.py:95`; **−10 points, no warning** | yes |
| `rating` cell is `"N/A"` / `"3.1/5"` | same silent −10, and `cmd_validate` misreports it as "rating outside 0-5" | yes |
| `industry_priority` is `"High"` / `"HIGH"` / `" high"` | silently loses the +15/+8 | yes (`"high"`→30, `"High"`→15) |
| `industry_priority` absent | silently loses +15/+8 | yes |
| `website_live` is `"True"` / `"False"` / `1` / `0` (the literal CSV encoding) | silently loses both the +10 and the +20 | yes (all → 30, same as `None`) |
| `source` is a list `["osm","wikidata"]` | `str()` → `"['osm', 'wikidata']"`, no `\|`, silent −5 | yes (by inspection, `enricher.py:316`) |

### When it is called with an empty list

It isn't — it takes one record. `lead_score({})` returns `0` with no error;
`lead_score` over an all-`None` record returns `0`; `check_websites([])` returns `[]`
before its first `print` (`enricher.py:232-233`); `enrich([])` returns `[]`. All clean, and
`scrape.yml:80` rejects a sub-100-row export. **The empty case is the only one of the six
that is handled correctly.**

### When it is interrupted

It cannot be interrupted meaningfully — 500k rows × 2 passes measured at **0.03 s**
(`enricher.py:350` and `main.py:197`). But that is the problem: `lead_score` is the *last*
statement of `enrich()` (`enricher.py:350`) and the last step of the loop before the run's
only durable writes (`main.py:209-213`). Everything upstream of it lives only in memory
(`enricher.py:249-259` mutates dicts; nothing is written until `main.py:209`). A SIGTERM at
minute 299 discards 100% of five hours of scraping plus the Places quota. The master on
Drive survives, because `scrape.yml:85` (the upload) is `success()`-gated — the loss is the
week's refresh, not the data.

---

## Findings

### S1 — `lead_score`'s output is consumed by nothing, so a totally broken scorer is undetectable forever

- **Where:** output written at `enricher.py:283-319`, consumed at `enricher.py:350` and
  `main.py:197` and then dropped into `FIELDS` (`main.py:37`) and `_INT_FIELDS`
  (`main.py:44`). No consumer: `main.py:202-205` (`sales_ready`),
  `main.py:206` (`qualified`), `main.py:219-242` (summary), `cli.py:95-131` (`stats`),
  `cli.py:148-201` (`validate`), `scrape.yml:71-83` (gate).
- **Breaks:** grep the tree for `lead_score` and every hit is a write, a cast, or the
  offline `cmd_score`. Nothing sorts by it, filters on it, bounds it, or prints it.
  Consequently **four of the five exports are mathematically independent of it**:
  `sales_ready` = `has_any_contact()` + priority, `qualified` = `completeness_score >= 1`,
  `with_/without_websites` = the `website` column. A scorer that returns `0` for every row
  yields a `sales_ready.csv` byte-identical to last week's, a green Actions run, an exit
  code of 0, and a new timestamped Drive folder (`scrape.yml:87-89`). The only artifact
  that differs is one column of `all_businesses.csv` that no operator opens and no alert
  reads.
- **Trigger:** rename one input key (`enricher.py:288` `record.get("email")` →
  `record.get("e_mail")`), or ship a refactor that drops the rating term. 200k rows all
  score ~13 points lower; every gate passes; the log is byte-identical. The only way to
  notice is to diff two Drive folders by hand.
- **Fix:** three concrete additions, all in `main.py`: (a) print `min/mean/p50/p90/max`,
  the zero count and the count of rows clamped at 100 in the `main.py:219` block;
  (b) compare that distribution against the master already loaded at `main.py:150` and
  `raise SystemExit(1)` if the mean moves more than 10% or if `max == 0`; (c) emit
  `data/score_deltas.csv` (`name, old, new`) whenever a row's score changes, and add the
  same block to `cmd_stats`.

### S1 — An `UNKNOWN` probe result is written over a known verdict, erasing paid-for data and shifting 37% of scores

- **Where:** `enricher.py:249-250`
  (`r["website_live"] = True if outcome == LIVE else (False if outcome == DEAD else None)`),
  unconditional; upstream `enricher.py:176-178` returns `UNKNOWN` for DNS/TLS/timeout/reset;
  downstream `enricher.py:300-304` pays nothing for `None`. `dedup.py:114` lists
  `website_live` in `_VOLATILE`, so the merge faithfully preserves the previous verdict —
  and then `enricher.py:250` throws it away.
- **Breaks:** a verdict costs one real HTTP GET against a hostile or foreign host. When the
  probe fails, the code does not keep the last known value; it sets the column to `None`.
  A single weekly run in which a host is briefly unreachable permanently deletes the
  `DEAD`/`LIVE` classification for every row behind that host. Because nothing is dated
  (`website_live` carries no `checked_at`), the loss is undetectable afterwards: the CSV
  shows a blank, and a blank is also what a never-probed row looks like. The measured
  score effect is the table above: mean 33.81 → 29.35, 74,112/200,000 rows down by 10-20
  points, and the 23 rows that had reached 100 drop to at most 90.
- **Trigger:** run the pipeline while the runner's egress is blackholed for 10 minutes
  during `check_websites`. The run succeeds. Next week, `leadminer stats` reports a larger
  "unreachable" count than the week before, and every one of those rows has silently lost
  its rebuild-pitch credit. Run it again the following week and the knowledge is gone for
  good — there is no other source for it in the system.
- **Fix:** only overwrite when the probe produced an answer — `if outcome != UNKNOWN:
  r["website_live"] = ...` — and add `website_live_checked_at` so a carried-forward verdict
  is distinguishable from a fresh one. (This is the inverse of the finding in
  `395-fn-enricher-py-lead_score-concur.md` S3, which notes that `dedup` makes a transient
  verdict permanent; here a transient *failure* destroys a permanent one.)

### S2 — `rating` is the only unguarded comparison in the function, and it sits on the run's last line

- **Where:** `enricher.py:312-314`; reached from `enricher.py:350` and `main.py:197`.
- **Breaks:** `rating < 4.0` with no coercion raises `TypeError` for any string, empty
  string, or container. That exception is not caught anywhere on the path, and both call
  sites are terminal: `enricher.py:350` is the final statement of `enrich()`, and
  `main.py:197` is the last step before `main.py:209`'s five writes. A `TypeError` there
  throws away the entire run — every HTTP GET, the Places quota, five hours — and leaves
  `data/` holding only the master downloaded at `scrape.yml:56`.
  **Reachability, stated honestly:** not from today's `main.py` path, because
  `load_master` casts `rating` through `float()` in a `try` (`main.py:90-95`) and all three
  scrapers emit `float | None` (`google_places.py:297`, `osm.py:114`, `wikidata.py:122`).
  It *is* reachable from every caller that does not pre-cast, and those are the callers the
  project tells you to write: `cmd_stats` and `cmd_validate` both read raw `csv.DictReader`
  rows (`cli.py:86`, `cli.py:142`), and `docs/audits/043-scoring-upgrade.md` prescribes an
  offline backtest script over the CSVs. Verified: a raw DictReader row raises `TypeError`.
  The *silent* variants are the ones that are live right now — see the trigger below.
- **Trigger:** an operator corrects a rating in the Drive CSV, typing `3,1` (French decimal
  comma is a natural keystroke in Beirut) or leaving `N/A`. Verified chain: `float("3,1")`
  raises `ValueError` → `main.py:95` sets `None` → the row silently loses 10 points every
  run, forever, and `leadminer validate` reports the misleading `1 rows have a rating
  outside 0-5` and exits 1. Measured: the same record scores 80 with `3.1` and 70 with `3,1`.
  `nan` is worse: `float("nan")` *succeeds* (`main.py:93`), so the column holds `nan`, and
  `nan < 4.0` is `False` — a poisoned cell that `validate` reports as out-of-range forever.
- **Fix:** one coercion helper used by both the scorer and the loader —
  `try: rating = float(record.get("rating")) except (TypeError, ValueError): rating = None`
  — plus a counter of coerced rows printed in the run summary, and a `nan`/`inf` rejection
  in `load_master`.

### S2 — Two different `lead_score` values exist for every record in every run, and only the second is persisted

- **Where:** `enricher.py:350` (inside `enrich`) vs `main.py:197` (inside `main`), with
  `main.py:190` (priority) and `main.py:194` (phone normalization) happening in between.
- **Breaks:** the two passes see different inputs. `enrich()` scores before
  `industry_priority` exists, so every freshly scraped record (all three scrapers set it to
  `None`: `google_places.py:299`, `osm.py:116`, `wikidata.py:124`) forfeits up to 15
  points; verified 45 vs 60 for a dead-site high-priority record. `395-...-concur.md` S2
  covers that half. **The half nobody has flagged is the phone:** `enrich()` scores the raw
  `phone`, and `normalize_phone` (`main.py:194`, `dedup.py:33-63`) runs in between.
  Verified: `phone="0000000"` scores 30 in the `enrich()` pass and 15 in the `main()` pass,
  because `normalize_phone("0000000", "LB")` returns `"+961"` (`dedup.py:45-63`: 7 digits,
  no `00` prefix beyond the two stripped, no known country code matches) which then fails
  the `len(phone) >= 7` test at `enricher.py:292`. Zero-filled numbers are not hypothetical
  — `084-SYNTH-adversarial-fix-review.md` flags `normalize_phone` collapsing them to `+961`.
  So any in-process consumer of `enrich()` — a notebook, a future service, a test — reads a
  score the pipeline itself does not believe, and nothing anywhere compares the two.
- **Trigger:** `enrich([{"phone": "0000000", ...}])[0]["lead_score"]` → 30; the value that
  reaches the CSV → 15.
- **Fix:** delete `r["lead_score"] = lead_score(r)` at `enricher.py:350` and score once, in
  `main.py:189-197`, after classification and normalization — or make `lead_score` raise if
  `industry_priority` is absent, so the "forgot to classify" state cannot be scored silently.

### S2 — Exact-match on `industry_priority` literals couples the scorer to a vocabulary that no test pins

- **Where:** `enricher.py:306-310` compares against the literals `"high"` / `"medium"`;
  the producer is `whitelist.industry_priority()` (`whitelist.py:133-145`), which does
  return those strings.
- **Breaks:** the coupling is invisible in both directions. Any change to the producer's
  vocabulary — `"HIGH"`, `"tier1"`, `"High Priority"` — silently zeroes the priority term
  for 100% of rows, a uniform −15/−8 shift, and no test fails: `tests/test_lead_signal.py:74`
  hardcodes `"industry_priority": "high"` and never calls `whitelist.industry_priority`.
  Verified: `"high"` → 30, `"High"` → 15, `"HIGH"` → 15, `" high"` → 15, `None` → 15.
  By S1's finding, that shift is invisible in every artifact and in the log.
- **Trigger:** rename the tier strings in `whitelist.py:134-145`; the suite stays green and
  every score in the master drops by 8-15 points overnight.
- **Fix:** `priority = str(record.get("industry_priority") or "").strip().lower()`, and add
  a test that scores `industry_priority(c)` for every `c` in `PRIORITY_INDUSTRIES |
  ADJACENT_BUSINESSES` (`whitelist.py:17-82`) instead of a literal.

### S2 — The enrichment deadline is per socket operation, not per URL, so the job can die before `lead_score` ever runs

- **Where:** `enricher.py:175` `get_session().get(url, timeout=8, allow_redirects=True,
  stream=True)` with `r.raw.read(_MAX_BODY_BYTES, ...)` at `enricher.py:188`; workers fixed
  at 40 (`enricher.py:225`, called with defaults from `enricher.py:337`); budget
  `timeout-minutes: 300` (`scrape.yml:25`).
- **Breaks:** `timeout=8` in `requests` is applied to each socket operation, not to the
  request as a whole, and `stream=True` means the body read is a separate operation. A host
  that dribbles one byte every seven seconds holds its worker indefinitely; 40 such hosts
  freeze the enrichment phase for the remainder of the run. `allow_redirects=True` with
  requests' default 30-hop limit means a single URL can consume up to ~30 × 16 s ≈ 8 minutes
  of one worker. There is no per-URL deadline and no phase deadline.
  `200-fn-cli-py-main-ops.md` S1 already flags the linear cost against the 300-minute wall;
  the arithmetic that makes it concrete: with every probe hitting the 8 s timeout at 40
  workers, wall time is `N × 8 / 40 = N/5` seconds, so the budget breaks at
  **N ≈ 90,000 website-bearing rows**. `N` is the whole cumulative master, re-probed every
  run (`main.py:187` → `enricher.py:337` → `enricher.py:231`), so it grows every week.
- **Why it belongs in a report about `lead_score`:** in this failure mode the run is killed
  inside `check_websites` and `lead_score` **never executes**. The operator's log tail shows
  `[Enricher] N/M done...` progress lines and nothing from scoring, so triage points at the
  scorer when the scorer is not at fault. The fix belongs in `check_websites`, not here.
- **Fix:** a monotonic per-URL deadline in `_fetch_website` (pass a `requests` timeout pair
  plus a deadline check between redirect hops) and a run-level `deadline` argument to
  `check_websites` that flips the remainder to `UNKNOWN` and returns cleanly when the
  budget is spent.

### S3 — The score saturates: the raw maximum is 110, so the top of the list is flat

- **Where:** `enricher.py:319` `return min(score, 100)`.
- **Breaks:** the eight terms sum to at most 20+15+15+10+20+15+10+5 = **110**, so the clamp
  is load-bearing and discards up to 10 points of real signal. Verified: five materially
  different evidence profiles — full-contact dead site; the same plus a LinkedIn URL;
  `rating` 2.1 vs 3.9; single-source vs multi-source — all return exactly `100`. The best
  leads in the market are therefore indistinguishable, and any future weight change in that
  band is invisible. Compounding it: `sales_ready.csv` is written in `dedup` order and is
  never sorted (`main.py:202-213`), so the deliverable a salesperson opens has no ordering
  at all, and the one number they might sort by is flat at the top.
- **Fix:** keep the 0-100 contract but emit `score_raw` alongside it (or raise the cap to
  the true maximum and band it), and sort `sales_ready` by score descending at `main.py:213`.

### S3 — The `is True` branch is missing the `website` guard the `is False` branch has, and stale flags are never cleared

- **Where:** `enricher.py:300-304`; `enricher.py:231` (only rows with a website are
  probed); `enricher.py:346` (`r.setdefault("website_live", None)` never overwrites an
  existing key).
- **Breaks:** the dead branch is guarded by `record.get("website") and live is False`; the
  live branch is not. Verified: `{"website_live": True}` with no `website` earns the +10 —
  25 instead of 15. Such a row is reachable whenever the `website` cell is cleared in the
  Drive CSV by a human (a natural way to suppress a lead) while `website_live` survives the
  merge as `True`. `check_websites` skips it forever (`enricher.py:231`) and
  `r.setdefault` never resets it (`enricher.py:346`), so the phantom credit is permanent and
  the row is invisible in every export except `all_businesses.csv`, because
  `with_websites`/`without_websites` filter on `website` (`main.py:199-200`).
- **Fix:** `if record.get("website") and live is True:` — and set `r["website_live"] = None`
  unconditionally in the loop at `enricher.py:339-348` so a cleared URL clears the flag.

### S3 — No docstring, no declared preconditions, no weights in the artifact; and the two headline columns disagree

- **Where:** `enricher.py:283-284` — the function opens with `score = 0`, no docstring (in
  contrast to its neighbours `check_websites` at `enricher.py:226` and `_fetch_website` at
  `enricher.py:162`); `README.md:65` publishes `lead_score` as a "0–100 weighted quality
  score" and lists none of the eight weights.
- **Breaks:** two operational consequences. (a) "Why is this lead 47?" is unanswerable from
  the CSV — the weights live only in the source, and `scored_at` / `score_version` do not
  exist (the versioning gap is raised properly in `043-scoring-upgrade.md` S2; this is the
  support-desk cost of it). (b) `lead_score` never reads `address`, `facebook`, `linkedin`,
  or `review_count`, while `completeness_score` (`enricher.py:101-117`) counts `address`,
  `facebook` and `linkedin`. The two headline columns are computed over different field
  sets and routinely disagree in the direction that misleads a rep: verified,
  `completeness_score=7 → lead_score=68` versus `completeness_score=3 → lead_score=75`.
- **Fix:** a docstring naming the eight weights, the preconditions (`industry_priority`
  must be set, `website_live` must be a bool or `None`), and the fact that the clamp is
  lossy; document `address`/`facebook`/`linkedin` as deliberately excluded in `README.md:64-65`.

---

## Silent-failure inventory, ranked by time-to-detect

Every row verified by execution unless noted. "Log signal" is what a human actually sees
in the Actions log during and after the run.

| # | Failure | Log signal | Time to notice |
|---|---|---|---|
| 1 | Scorer returns constant 0, or every weight term is lost (S1 #1) | **none** — row count unchanged, `scrape.yml:71-83` passes, `sales_ready.csv` byte-identical | **never**; requires diffing two Drive folders by hand |
| 2 | `UNKNOWN` overwrites a known `website_live` (S1 #2) | partial — `main.py:222-223` prints a higher "unreachable" count | one log glance if anyone reads it weekly; realistically the week a rep notices a missing rebuild pitch; the deleted verdict is gone forever |
| 3 | Priority vocabulary changes, uniform −15/−8 (S2) | **none** | weeks-to-a-quarter, when a rep says "the numbers look low"; never if nobody compares runs |
| 4 | Network blackhole shifts 37% of rows by 10-20 pts (S1 #2, measured) | yes, in the `live/dead/unreachable` line, not in the score | self-heals next week if the network does; the log line is the only warning |
| 5 | `rating` cell `"3,1"` / `"N/A"` → silent −10 every run (S2) | **none**; `cmd_validate` misreports it as "rating outside 0-5" and is never run by the cron | never, until a rep asks why that row ranks low |
| 6 | `enrich()`'s stale score (priority and/or raw phone) read by a library consumer (S2) | **none** | whenever that consumer is compared to the CSV — months for a notebook |
| 7 | `website_live=True` with no website earns a permanent phantom +10 (S3) | **none** — the row is absent from all four filtered exports | never |
| 8 | Enrichment stalls on slow-drip hosts; SIGTERM at 300 min before `lead_score` runs (S2) | red X in Actions; progress lines stop | **minutes** — but triage misdirects at the scorer |
| 9 | `TypeError` on a string `rating` (S2) | full traceback, exit 1 | **seconds** |

The shape of that table is the finding: rows 1-7 are invisible in the log *and* invisible
in every artifact, and only row 9 — the one that is currently unreachable from `main.py` —
fails loudly. For a system whose only feedback channel is stdout, that is the wrong way
round.

---

## Not a bug, but worth knowing

- **Do not spend the six hours on this function.** Measured: 200,000 calls in 0.399 s
  (~501k rows/s, 1.99 µs/row); a 500k-row master scored twice per run (`enricher.py:350`
  and `main.py:197`) costs **0.03 s**. The perf and reliability work belongs in
  `check_websites`, which does one HTTP GET per website at 40 workers
  (`enricher.py:225-276`). This is the same conclusion as
  `311-fn-dedup-py-_validity-ops.md` reached for `_validity`.
- **It is pure, deterministic and idempotent — verified.** `lead_score` mutates nothing
  (the input dict is byte-identical after the call), repeated calls return the same value,
  and it never reads the stored `lead_score`, so re-scoring cannot compound and
  `cmd_score` (`cli.py:264-276`) is safe to re-run over the master. `min(score, 100)`
  always returns an `int`.
- **Interruption-safe in isolation, and that is worth nothing operationally.** No I/O, no
  locks, no shared state, no partial writes. The atomic writer at `main.py:121-133`
  (temp file → `fsync` → `os.replace`, `except BaseException` cleanup) means a reader sees
  either the whole previous CSV or the whole new one, and `scrape.yml:85` being
  `success()`-gated means a killed run cannot overwrite the Drive master. **That part is
  correct and should not be changed.** The expensive interruption is a SIGTERM during
  `enrich`, because `main.py:209` is the run's only durable write.
- **`website_live=False` without a website is self-guarding; `True` is not.** That
  asymmetry is the S3 above, and it is worth knowing independently of the fix: the dead
  branch requires `record.get("website")` (`enricher.py:303`) so a stale `False` cannot
  fabricate a rebuild pitch, while a stale `True` can fabricate a live-site bonus.
- **The scorer is completely time-blind.** `rating`, `website_live` and `industry_priority`
  all vary over time and none is dated or decayed; `scraped_at` is the only freshness signal
  in the record and `lead_score` ignores it. Verified: 1,000 rows stamped six months old
  score identically (mean 34.03) to the same rows stamped fresh. So a row last seen by
  Google eight months ago can still be collecting the +10 "low rating = pain" bonus for a
  reputation that was fixed long ago. This overlaps `395-fn-enricher-py-lead_score-concur.md`
  S3 (no timestamp on `website_live`) and is listed here rather than as a finding because
  the remedy is the same one: carry a `checked_at` per volatile signal.
- **`len(phone) >= 7` counts digits, not a number.** `"0000000"` and `"1234567"` both earn
  +15 (verified). `main.py:194` rescues this for the exported value; `enrich()`'s pass does
  not (see S2 above).
- **`source` is stringified before the `|` test.** `str(record.get("source") or "")`
  (`enricher.py:316`) means a list-valued provenance silently loses the +5 instead of
  raising. Cosmetic today — `dedup.py:174` writes a `"a|b"` string — but it is one repr
  change away from a silent 5-point loss across a whole source.

---

## Recommended order of work

1. **Make `lead_score` observable (S1 #1).** Print the score distribution in the
   `main.py:219` block, compare it against the master loaded at `main.py:150`, fail the run
   on a >10% mean shift or an all-zero column, write `data/score_deltas.csv`, and mirror the
   block into `cmd_stats`. Nothing else in this report can be detected until this lands, and
   it is a dozen lines.
2. **Stop erasing verdicts (S1 #2).** `if outcome != UNKNOWN:` before the assignment at
   `enricher.py:250`, plus `website_live_checked_at`. Recovers the data and removes the
   outage-induced score shift at the same time.
3. **Coerce `rating` once (S2).** A shared `to_float()` used by `lead_score` and
   `load_master`, rejecting `nan`/`inf`, with a coercion counter in the summary. Fixes the
   crash, the silent −10, and the misleading `validate` message together.
4. **Score once (S2).** Delete `enricher.py:350`; let `main.py:189-197` be the only scorer.
   Then assert `lead_score` raises on a missing `industry_priority` so the ordering bug
   cannot return quietly.
5. **Normalise the priority vocabulary (S2).** `.strip().lower()` at `enricher.py:306`, and
   a test that scores the real output of `whitelist.industry_priority` for every category in
   `whitelist.py:17-82`.
6. **Give the enrichment phase a deadline (S2).** Per-URL monotonic deadline in
   `_fetch_website` and a run-level budget in `check_websites`, so the job returns cleanly
   with `UNKNOWN` verdicts instead of being SIGTERMed — which also removes the triage
   red-herring that blames the scorer.
7. **Polish (S3).** Emit `score_raw` and sort `sales_ready` by score; add the missing
   `website` guard to the live branch and clear stale flags at `enricher.py:346`; write the
   docstring and document the deliberate field exclusions in `README.md:64-65`.