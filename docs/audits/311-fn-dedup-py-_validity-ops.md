# 311 — `_validity` (dedup.py:120) — ops lens

## Verdict

`_validity` is the only component in the merge path that is supposed to know the difference
between a value and garbage, and it is the only one that emits no signal at all: no log, no
counter, no throw. Measured, it blesses `nan`/`inf` coordinates as *maximally* plausible while
`rating=nan` is rejected, it cannot fail on `website` at all because both upstream scrapers
pre-pend a scheme before it ever sees a value, and it validates emails with a weaker regex than
`enricher._EMAIL_BLACKLIST` uses to deliberately discard the same addresses three modules later.
The single highest-leverage fix is not in `dedup.py` at all: the repo **already owns** a
detector for exactly these two classes of garbage — `cli.py:135-201` (`cmd_validate`, coordinate
boxes at `:159-173`, rating range at `:175-188`) — and the weekly cron never calls it, so every
misjudgement `_validity` makes is permanent, silent, and shipped.

Every claim below was verified by executing `dedup._validity` / `_pick` / `_merge` offline with
synthetic inputs. No scraper was run and no source was modified.

**Scope note:** sibling lenses on this function and its callers already exist —
`304-…-correctness`, `307-…-concur`, `308-…-contract` (this function), `343-…-ops` (`_pick`),
`300-…-contract` and `303-…-ops` (`_is_missing`). Where I confirm one of their findings I say so
inline and state what the ops lens adds; I do not re-report them as new.

---

## The six operational questions

### What does it print?

**Nothing.** Verified: `inspect.getsource(_validity)` contains no `print`, no `log`, no
`logger`, no `logging`, no counter, no warn. `dedup.py` as a whole contains no `print` and no
`logging` import (verified by grep across the file — the only logging is in
`scrapers/osm.py:7` and `scrapers/wikidata.py:7`, neither of which is on this path).

The entire dedup phase of a six-hour run contributes **exactly one line** to the log:
`main.py:181` `"After merge + dedup: {n} unique businesses"`. That number is invariant under
every failure mode in this report: merge correctly, merge wrongly, poison the master, drop
40,000 values — the count is the same. `docs/audits/072-run-failure-triage.md:35` asks for
`dedup_merged_by_phone` / `dedup_merged_by_name` and `docs/audits/074-backup-and-restore.md:91`
assumes a `net_new` counter. **None of them exist** (verified: no match for any of the three
identifiers anywhere outside `docs/`).

### What does it swallow?

Exactly two exception types, at `dedup.py:131` and `dedup.py:138`:
`except (TypeError, ValueError)`. Both wrap a single `float(value)` call. Measured behaviour:

| Input | `_validity` result |
|---|---|
| `"abc"`, `[]`, `12345` | `0` — caught, downgraded |
| `"33.89"` | `3` — accepted |
| `10**400` (a Python `int`) | **raises `OverflowError`, uncaught** |
| an object whose `__float__` raises `OverflowError` | **raises, uncaught** |

Everything else is swallowed *semantically* rather than by an `except`: `_validity` never
raises for garbage it merely mis-scores, and `_pick` never records that it scored it. Measured
domain of returned values for present values: `{0, 1, 3}` — plus `-100` for missing, which the
docstring at `dedup.py:121` ("0-3 bonus") does not disclose.

### When the network drops halfway

`_validity` never touches the network, so it cannot detect a drop. The ops-relevant fact is
worse: **a partial scrape does not merely produce less data, it permanently upgrades the
surviving thin records.** `_pick` unions `source` at `dedup.py:174`, so after one good week a
master row reads `source="google_places|osm"`; `_trust_for` (`dedup.py:148-152`) takes the
**max over that union**, not per-observation. Measured:

```
merged = {"website": "https://PLACEHOLDER.example",
          "source": "google_places|osm", "scraped_at": "2020-01-01T00:00:00+00:00"}
newg   = {"website": "https://real-live-site.example",
          "source": "google_places",        "scraped_at": "2026-10-01T00:00:00+00:00"}
_merge(merged, newg)["website"] -> 'https://PLACEHOLDER.example'
_trust_for("website", merged) = 3   _trust_for("website", newg) = 2
_validity(both)              = 3 3        <- validity never consulted
```

Google comes back next week with a correct, fresher URL and the two-year-old placeholder wins
again, because `_validity` sits at tuple position 2 (`dedup.py:182`) behind trust at position 1
(`dedup.py:181`). Recovery from the outage does not undo the outage's damage. See S2 #4.

### When a dependency returns garbage

Measured garbage-in / verdict-out table for the values the three scrapers can actually emit:

```
_validity('email', 'n/a')            -> 0      _validity('website', 'n/a')        -> 0
_validity('email', 'unknown')        -> 0      _validity('website', 'x.com')      -> 0
_validity('email', 'not available')  -> 0      _validity('website', 'ftp://x.com')-> 0
_validity('email', 'info@wixpress.com') -> 3    _validity('website', 'https://facebook.com/page') -> 3
_validity('email', 'a@b.c')          -> 3      _validity('website', 'https://t.co/x') -> 3
_validity('email', b'a@b.com')       -> 3      _validity('email', ['a@b.com'])    -> 3
_validity('lat', nan | 'nan' | 'NaN' | inf | 'Infinity' | 999 | -999) -> 3  (all of them)
_validity('rating', nan)             -> 0      _validity('rating', 0)             -> 3
_validity('lat', [])                 -> 0      _validity('rating', False)        -> 3
```

Two of those rows are wrong in a way that matters commercially, and both are S2 findings below:
the email one (`info@wixpress.com` is a platform artefact that `enricher.py:138-141`
deliberately blacklists) and the website one (a Facebook page scores 3 as a "website").

### When it is called with an empty list

`dedup([])` returns `[]` and **`_validity` is never called** (verified: `dedup([]) == []`,
`dedup([{}]) == [{}]`). No crash, no error. `_validity` has no empty-input path of its own
because it takes a scalar `value`, not a list — the degenerate case arrives as `[]` *as a
value*, where `_is_missing([])` is `False` (`dedup.py:86-90` only special-cases `None` and
`str`), so an empty list is scored as **present** and falls to `_validity(...) -> 0`.

The real damage from the empty list is upstream and is a Drive-overwrite scenario, not a crash.
See "Not a bug" #3.

### When it is interrupted

`_validity` itself is pure — no I/O, no locks, no shared state, no partial writes — so killing
the job inside it loses an in-memory dict and nothing on disk. Measured cost, which bounds how
long that window can be:

```
dedup(125,000 records) -> 100,000 unique in 3.30s
~832,000 rank() calls/sec  (x22 fields per merge)
extrapolated to a 500k-row master: ~16.5s
```

Against that, `main.py:209` is the **first and only durable write of the run**, and it happens
after `enricher.check_websites` has done one HTTP GET per website at 40 workers
(`enricher.py:225-276`). A SIGTERM from `timeout-minutes: 300` (`scrape.yml:25`) landing during
`enrich` discards the entire scrape plus all enrichment. **~3.3 seconds of `_validity`
decisions govern 100,000 rows and none of it is observable.** That ratio is the finding.

---

## Findings

### S1 — `float()` blesses `nan`/`inf` as maximally valid, and the CSV round-trip re-legitimises them every generation

- **Where:** `dedup.py:128-133` (`lat`/`lon`), contrast `dedup.py:139` (`rating`, which
  range-checks). Round-trip carrier: `main.py:92-95` (`float(row[field])`) and `main.py:128`
  (`csv` writes `str(value)`).
- **Why S1, and where I disagree with the sibling report:** `343-fn-dedup-py-_pick-ops.md:130`
  rates the same code S2. I am raising it, on three measured grounds. (1) `_validity("lat", nan)`
  is `3` while `_validity("rating", nan)` is `0` — the function handles *identical* garbage two
  different ways inside itself. (2) The tiebreak is unconditional, not incidental: at
  `dedup.py:184`, `str(nan) > "33.8938"` is `True`, so NaN beats every real coordinate
  regardless of arrival order — verified in **both** `_merge` argument orders. (3) It is
  self-perpetuating: measured across five simulated weekly merge + `write_csv`/`load_master`
  round-trips, `lat` stays `nan` at every generation while a genuine Google coordinate is
  re-offered each week and re-loses.
- **Breaks:**
  ```
  _validity('lat', 33.8938) -> 3    _validity('lat', 999)   -> 3
  _validity('lat', nan)     -> 3    _validity('lat', -999)  -> 3
  _validity('lat', 'inf')   -> 3    _validity('lat', 'NaN') -> 3
  ```
  `write_csv` emits the literal text `nan`; `load_master`'s `float("nan")` accepts it without
  error (measured: `name,lat,lon,rating,source | Cafe,nan,nan,nan,osm`), so the poison is
  re-legitimised at every future run and cannot be aged out.
- **Trigger:** a cell reading `nan`, `NaN`, `inf` or `Infinity` in the `lat`/`lon` column of
  `all_businesses.csv`. Honest scoping: I could not construct a path from the three current
  scrapers — `osm.py:68-69` uses `or` (which would discard `0.0`), `wikidata.py:54-63` parses a
  `Point(...)` substring, and Google returns real numbers. The entry point is the master CSV
  itself, which is the artifact humans open and edit in Drive (`data/` is gitignored, so it is
  Drive-only), and the same omission exists in `load_master`, which never calls
  `math.isfinite`. That entry path is why this is S1 rather than S2: the trust boundary is a
  human, not a scraper.
- **Downstream, silently:** `enricher.py:92` `lat_min <= lat <= lat_max` is `False` for NaN, so
  region inference is skipped, `region` stays `None`, and the row reports under `Unknown` in the
  `main.py:232-234` regional breakdown — a bucket a reader learns to ignore, which is precisely
  how it stays invisible.
- **Fix:** `if not math.isfinite(v): return 0` then `return 3 if -90 <= v <= 90 else 0` for
  `lat`, `-180 <= v <= 180` for `lon`; add the same `isfinite` guard to `main.py:90-95`.

### S1 — The detector that catches S1 #1 exists, and the cron never runs it

- **Where:** `cli.py:135-201` (`cmd_validate`) versus `scrape.yml:65-69`.
- **Breaks:** the repo already contains the exact defence for the finding above and for the
  rating case — `in_box()` at `cli.py:159-162` with the LB/SA boxes, `float(r["lat"])` at
  `cli.py:167`, and the rating range check at `cli.py:185`. A NaN coordinate makes `in_box` false
  and is therefore counted by `cmd_validate`. **The scheduled run never invokes it.**
  `scrape.yml:69` is `run: python main.py`; the only quality gate on the publish path is the
  shell row-count check at `scrape.yml:74-83` (`wc -l`, fails under 100 rows). Grep confirms
  `leadminer validate` / `cmd_validate` appears nowhere in the workflow. `cli.py:10` advertises
  the command as the tool you reach for "when a run looks wrong" — but nothing in the schedule
  ever looks.
- **Amplifier:** the coordinate check is a *ratio* test, `if bad_coords > total * 0.01`
  (`cli.py:172`). So even when run by hand, up to 1% of a 100k-row master (1,000 poisoned rows)
  passes and exits 0.
- **Trigger:** run the schedule for a week in which a master cell is corrupted to `nan`. Nothing
  in the log changes; `scrape.yml:71-83` passes; the poisoned CSV is republished to
  `gdrive:leads/` at `scrape.yml:88-89`.
- **Fix:** add `python -m cli validate data/all_businesses.csv` as a step immediately before
  `scrape.yml:85`, and change `cli.py:172` to fail on any non-zero count of non-finite
  coordinates.

### S2 — The `website` validity check cannot fail: both scrapers pre-pend the scheme it validates

- **Where:** `dedup.py:126-127` and `_URL_OK` at `dedup.py:117`; producers at
  `scrapers/osm.py:95-96` and `scrapers/wikidata.py:103-104`; consumption at `main.py:199-200`.
- **Overlap note:** `304-fn-dedup-py-_validity-correctness.md:144-146` covers the related
  *prefix-match* weakness and notes the `osm.py:95-96` prepend. That report argues junk
  therefore reaches `_validity` intact. Mine is the complementary and stronger claim for this
  lens: because of that same prepend, the branch's **scheme test can essentially never fail**, so
  it is near-vacuous as written — and the gap that does bite is not punctuation, it is the
  unconstrained host.
- **Breaks:** `osm.py:95-96` rewrites any scheme-less tag to `https://`; `wikidata.py:103-104`
  does the same; Google's `websiteUri` always carries a scheme. `load_master` reads back what
  `write_csv` wrote, i.e. already-prefixed values. So by the time `_validity("website", v)` is
  reached in any normal run, `_URL_OK` (`^https?://[^\s/]+\.[^\s]+`) matches and the branch
  returns `3` unconditionally. A plausibility gate that cannot fail is not a gate — it is three
  lines that read like validation and arbitrate nothing. Measured:
  `_validity('website', 'x.com') -> 0` and `_validity('website', 'n/a') -> 0` are the only
  reachable non-3 outcomes, and neither reaches production.
- **The gap that does bite:** nothing constrains the *host*. Measured
  `_validity('website', 'https://facebook.com/page') -> 3`,
  `_validity('website', 'https://t.co/x') -> 3`. OSM's `website` falls back to the bare `url`
  tag (`osm.py:91`), which in Lebanon is very often a Facebook page, an Instagram handle, or a
  Linktree. Because `main.py:199-200` splits `with_websites.csv` / `without_websites.csv` on
  `r.get("website")` truthiness alone, a business whose only "website" is a Facebook page is
  counted as having a website and **is excluded from `without_websites.csv` — the file the brief
  names as the new-site pitch list.** Those are the leads the product exists to find.
- **Fix:** reject known social/shortlink hosts (`facebook.com`, `instagram.com`, `linktr.ee`,
  `t.co`, `wa.me`, `business.site`, `wixsite.com`, `sites.google.com`) — the repo already owns
  exactly this pattern for email in `enricher._EMAIL_BLACKLIST` (`enricher.py:138-141`) — and
  return `0` so the field loses to a real URL in `_pick`.

### S2 — `_validity` and `enricher` disagree about which emails are real, and `_validity` wins

- **Where:** `dedup.py:116` (`_EMAIL_OK`) versus `enricher.py:138-141` (`_EMAIL_BLACKLIST`),
  applied at `enricher.py:200-204`.
- **Overlap note:** `308-fn-dedup-py-_validity-contract.md:240-245` already records that
  `_validity` never applies `enricher._EMAIL_BLACKLIST` and cites `info@wixpress.com` scoring 3.
  I confirm that and add the two things this lens surfaces: which side *wins* (the merge
  authority, because the OSM path has no competing `email` to outrank it) and what it costs
  (+20 of `lead_score`, plus a `completeness_score` point).
- **Breaks:** two validators, one import apart, and they disagree. `enricher.py:202` computes
  `addr.split("@")[-1].lower()` and *deliberately throws away* any address whose domain is
  `example.com`, `wixpress.com`, `squarespace.com`, `shopify.com`, `sentry.io` and six others —
  platform artefacts, not contacts. `_validity` applies no blacklist:
  `_validity('email', 'info@wixpress.com') -> 3` (verified). So a Wix template footer scraped by
  the enricher is discarded at `enricher.py:202`, and the identical string arriving from an OSM
  `email` tag (`osm.py:90`) is promoted to *maximally valid* by the merge authority and written
  to `all_businesses.csv`. `enricher.completeness_score` (`enricher.py:105-106`) then awards the
  record a real point, and `enricher.lead_score` (`enricher.py:288-289`) awards it **+20** — a
  fifth of the maximum score — for an address that bounces.
- **Trigger:** an OSM node tagged `email=info@wixpress.com`, merged against a Google record for
  the same business. Trust ties (OSM email 3, Google has no `email` in `_SOURCE_TRUST` at
  `dedup.py:102`... falls to `_DEFAULT_TRUST` 1), so validity decides, and validity says the
  Wix footer is perfect.
- **Fix:** import `_EMAIL_BLACKLIST` into `dedup.py` and return `0` for a blacklisted domain;
  better, extract the two regexes and the blacklist into one `validation.py` so the merge and
  the extractor cannot drift again.

### S2 — `scraped_at = max()` stamps stale contact data as fresh, which blinds the run-triage strategy

- **Where:** `dedup.py:176-177` and `dedup.py:183`; trust source-union at `dedup.py:174` and
  `dedup.py:148-152`.
- **Overlap note:** `343-fn-dedup-py-_pick-ops.md` reports the trust-outranks-recency half of
  this. I am reporting it because of the ops consequence, which that report does not draw, and
  because it is the mechanism by which a *network outage* — the lens question — does lasting
  harm.
- **Breaks:** `_pick("scraped_at", ...)` takes `max(av, bv)`. So a record whose `website` is a
  two-year-old placeholder (trust 3, from the unioned `source`) merges with a fresh, correct
  Google observation (trust 2) and comes out carrying the placeholder **and today's date**.
  Verified: `_merge(merged, newg)` yields `website='https://PLACEHOLDER.example'` with
  `scraped_at='2026-10-01T00:00:00+00:00'`. `_validity` cannot object — it is compared at
  `dedup.py:182`, after trust at `dedup.py:181`, so a trust win ends the comparison before
  validity is read.
- **Why this is an ops finding and not a data one:** `scraped_at` is the only freshness signal
  the product has. `docs/audits/072-run-failure-triage.md:37` defines `net_new` from row-count
  deltas; `docs/audits/100-run-manifest-design.md:400` proposes reacting to
  `pipeline.dedup.collapsed` and `net_new` jumps. Both are blind to staleness that `max()`
  has relabelled. A row that is two years stale and stamped today is indistinguishable, in every
  metric the run emits, from a row refreshed this morning.
- **Time to notice:** months, and only via a failed outreach to a lead whose details were
  replaced two years ago.
- **Fix:** store per-field `observed_at` (or a `_first_seen`/`_last_seen` pair alongside the
  value) instead of one record-level clock; at minimum, stop using `max()` for `scraped_at` and
  use the timestamp of the record that actually supplied each surviving field.

### S3 — `except (TypeError, ValueError)` misses `OverflowError`, in four places at once

- **Where:** `dedup.py:131`, `dedup.py:138`. Same narrow clause at `main.py:94`, `main.py:100`
  and `cli.py:168`.
- **Breaks:** `float(10**400)` raises `OverflowError: int too large to convert to float`, which
  is not in the tuple. Verified: `_validity('rating', 10**400)` and `_validity('lat', 10**400)`
  both propagate. It escapes `_merge` -> `dedup` -> `main.py:180`, which is **not** wrapped in a
  `try` (contrast `main.py:163-168`, which does guard the scraper futures) — so the entire run
  dies and, per `main.py:209`, nothing is written.
- **Severity defence (S3, not S1):** I could not reach it from any current input path.
  `csv.DictReader` always yields `str`, and `float("1e400")` returns `inf` rather than raising,
  so a hostile CSV cell produces the S1 non-finite case instead, not this one. It requires a
  genuine Python `int` ≥ 10^309 in a record, and neither `osm.py` nor `wikidata.py` nor
  `google_places.py` puts an unbounded integer into a `BusinessRecord`. It is a latent trap for
  the first agent who adds a numeric field (`employees` and `inception` are already fetched at
  `wikidata.py:33-34` and currently discarded).
- **Fix:** `except (TypeError, ValueError, OverflowError)`, or better
  `except Exception` in a function whose entire job is to classify junk.

### S3 — The `-100` sentinel is unreachable, contradicts its own docstring, and is a trap for the next caller

- **Where:** `dedup.py:121` ("0-3 bonus"), `dedup.py:122-123` (`return -100`).
- **Breaks:** `_pick` short-circuits at `dedup.py:168-171` before `rank` is ever built, so
  `_validity` is only ever called with two non-missing values. Verified: `_validity("email",
  None) == -100`, and no `_pick` input reaches that branch. The stated contract (`0-3`) is wrong
  about the real domain (`{-100, 0, 1, 3}`).
- **The trap:** the moment anyone sums `_validity` across fields — a natural next step for a
  "record quality" score, and something `043-scoring-upgrade.md` gestures at — one absent field
  contributes **-100 instead of 0**, so a sparse record scores catastrophically rather than
  merely poorly. It is unreachable *today* only because of an accident of call ordering in a
  different function.
- **Fix:** delete lines 122-123 and correct the docstring to `0-3`, or move the missing-check
  into `_validity` so it is the single entry point (that also closes the gap in
  `300-fn-dedup-py-_is_missing-contract.md`).

### S3 — Field dispatch is bare string matching, so a renamed CSV column silently removes the gate

- **Where:** `dedup.py:124`, `:126`, `:128`, `:134`, `:140`; `_merge` iterates
  `set(a) | set(b)` at `dedup.py:192`; `main.py:86` performs no header validation;
  `main.py:125` uses `extrasaction="ignore"`.
- **Breaks:** `_validity` recognises exactly five field names. Any other column — including one
  added by a human in the Drive CSV, or renamed by a future migration — falls to `return 1` at
  `dedup.py:140`, and its value is then arbitrated by trust -> recency -> `str(value)` with **no
  plausibility check at all**. Measured: 18 of the 23 columns return a constant `1` from
  `_validity`, including `phone` — the field that decides whether two records are the same
  business (`dedup.py:206-218`). Because `write_csv` silently drops unrecognised columns
  (`main.py:125`), a typo like `web_site` both loses the value and loses its validation in the
  same run, with no warning at any point.
- **Fix:** validate the master's header against `main.FIELDS` in `load_master` and fail loudly on
  unknown or missing columns.

---

## Silent-failure inventory, ranked by time-to-detect

All rows verified by execution. "Log signal" is what a human actually sees in the Actions log
during and after the run.

| # | Failure | Log signal | Time to notice |
|---|---|---|---|
| 1 | `_validity` blesses `nan`/`inf` coordinates; poison is permanent and beats every real coordinate (S1 #1) | none — row count unchanged, `scrape.yml:71-83` passes | **never**; `cmd_validate` would catch it but the cron never runs it (`scrape.yml:69`) |
| 2 | `_validity` scores a Wix/Shopify template footer a perfect 3 and `lead_score` pays it +20 (S2 #3) | none — score distribution shifts imperceptibly | months, via a bounced outreach |
| 3 | Facebook/Linktree counted as a "website", removing the lead from `without_websites.csv` — the pitch list (S2 #2) | none — the row looks *better* than it is | **never**; the missing leads are invisible because absence is not printed |
| 4 | Partial scrape permanently upgrades thin rows to max trust; recovery does not undo it (S2 #4) | none — `scraped_at` is `max()`, so the row reads as freshly scraped | months, via wrong outreach on stale data |
| 5 | `rating=nan` scores 0 while `lat=nan` scores 3 — same garbage, two verdicts, inside one function | none | never; measured `lead_score` 30 vs 40, a 10-point loss with no error |
| 6 | Placeholder junk (`"n/a"`, `"unknown"`) adopted unconditionally whenever the peer field is blank, earning a completeness point (confirmed in `300-fn-dedup-py-_is_missing-contract.md`) | none | months, via a dead phone/email on a sales-ready lead |
| 7 | A renamed/extra master column drops its field to the constant `1` and loses all validation (S3) | none — `extrasaction="ignore"` hides it | never; silent value loss |
| 8 | `OverflowError` from `float()` escapes and kills the run (S3) | full traceback, exit 1 | **seconds** |

The asymmetry in that table is the report. Everything ranked 1-7 is invisible and, for 1-3,
effectively permanent; the only loud failure is the one that cannot currently be reached.
This is the correct ordering for a system whose only feedback channel is stdout.

---

## Not a bug, but worth knowing

- **Do not spend the six hours optimising this function.** Measured: `dedup(125,000 records)`
  completes in **3.30s** (~832k `rank()` calls/sec across 22 fields); extrapolated to a 500k-row
  master, ~16.5s. `_validity`'s regexes are pre-compiled at module scope (`dedup.py:116-117`),
  so there is no per-call compile cost. The perf work belongs in `enricher.check_websites`,
  which does one HTTP GET per website at 40 workers (`enricher.py:225-276`).
- **`_validity` is interruption-safe in isolation.** No I/O, no locks, no shared mutable state,
  no partial writes. Killing the job inside it loses in-memory dicts and nothing on disk. The
  atomic writer at `main.py:121-133` (temp file -> `fsync` -> `os.replace`) means a reader sees
  either the whole previous CSV or the whole new one. This is correct and should not be
  changed. The interruption that *does* cost six hours is a SIGTERM during `enrich`, because
  `main.py:209` is the run's only durable write.
- **Empty input is clean; the risk is one layer up.** `dedup([])` returns `[]` without touching
  `_validity` — no crash. The danger is `main.py:180` propagating `[]` into five `write_csv`
  calls at `main.py:209-213`. In CI, `scrape.yml:74-83` is the only thing standing between a
  header-only master and an overwrite of months of leads on Drive. **`leadminer run`
  (`cli.py:33-37`, wired at `pyproject.toml:33`) calls `main.main()` directly and has no such
  gate**, and because `data/` is gitignored, `git checkout` cannot restore the file. Running
  `leadminer run` locally while the network is down, with a stale master, is the realistic way to
  lose the dataset.
- **The `website`/`website_live` incoherence is NOT a bug — checked and cleared.** A merged
  record can briefly pair a `website` from one input with a `website_live` verdict from the
  other, which looked like it would hand `lead_score` (`enricher.py:303-304`) a free +20
  "server-confirmed DEAD site = rebuild pitch". Verified it does not survive: `enricher.py:231`
  targets *every* record with a website and `enricher.py:250` overwrites `website_live`
  unconditionally with a verdict from a fetch made in the same run. Do not spend time here.
- **`_validity("rating", 0)` returns 3** — maximum validity for a literal zero, because
  `0 <= 0 <= 5` at `dedup.py:139`. Already reported in
  `300-fn-dedup-py-_is_missing-contract.md:157`; noting only the downstream coupling, since
  `enricher.py:312-314` reads the same `0` as a strong pain signal and pays +10 for it. A value
  is simultaneously "the most plausible rating in the system" and "proof the business is
  failing".
- **`_validity`'s strip/validate/store mismatch** is minor but real: it validates
  `str(value).strip()` (`dedup.py:125`, `dedup.py:127`) while `_pick` stores the **unstripped**
  value (`dedup.py:187`). A master cell of `" https://a.example "` is scored 3 and written back
  with its padding intact, so the junk survives the round-trip. Covered as S3 in
  `300-fn-dedup-py-_is_missing-contract.md:214`.

---

## Recommended order of work

1. **Wire `cmd_validate` into the schedule** (`scrape.yml`, before line 85) and change
   `cli.py:172` from a 1% ratio to an exact count of non-finite coordinates. One YAML change
   converts rows 1 and 5 of the inventory from *never* to *minutes*, and it is worth more than
   every `_validity` fix below combined.
2. **Add `math.isfinite` and real bounds to the `lat`/`lon` branch** (`dedup.py:128-133`), and
   the same guard to `load_master` (`main.py:90-95`) so the poison cannot be re-read. Closes S1 #1
   at the source *and* at the door.
3. **Add a host blacklist to the `website` branch** (`dedup.py:126-127`) covering
   facebook/instagram/linktr.ee/t.co/wa.me/wixsite/business.site, then derive
   `with_websites`/`without_websites` (`main.py:199-200`) from that verdict rather than from
   truthiness. This is the only finding on this list that **removes leads from the sales list**,
   so it has direct revenue impact.
4. **Unify email validation.** Move `_EMAIL_OK` (`dedup.py:116`) and `_EMAIL_BLACKLIST`
   (`enricher.py:138-141`) into one `validation.py`, import it from both `dedup` and `enricher`,
   and add a test asserting `_validity` rejects every address `_fetch_website` discards. Stops
   the two validators from drifting further apart.
5. **Give `dedup` a stats dict** — merges by index, per-field conflicts, per-field
   value-dropped-without-replacement, plus a count of non-finite coordinates seen — and print it
   at `main.py:181` next to the row count that currently proves nothing. Every other row in the
   inventory becomes visible with this one change; `docs/audits/072-run-failure-triage.md:35` has
   already specified the counters.
6. **Per-field `observed_at` instead of record-level `max(scraped_at)`** (`dedup.py:176-177`).
   Larger, but until this lands, freshness-based monitoring is measuring the wrong thing.
7. **Delete the `-100` sentinel** (`dedup.py:122-123`), correct the docstring, and widen the
   `except` clauses to include `OverflowError` (`dedup.py:131`, `dedup.py:138`). Five minutes of
   work that removes a trap for whoever writes the quality-score function next.
