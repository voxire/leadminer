# 367 — `infer_region` through the ops lens: what prints, what swallows, what a human never finds

## Verdict

This function will never be the reason a cron run takes six hours — measured at **0.99 µs
per call** (200,000 calls in 0.197 s), it is pure, allocation-free, performs no I/O, and its
patterns are compiled once at import. **Its entire risk is silent and permanent.** A wrong or
blank `region` is written into the cumulative master `data/all_businesses.csv` and there is no
counter that notices, no column that says how it was derived, and no operator path to recompute
it: `leadminer score` (`cli.py:247-277`) replays `industry_priority`, `recommended_service` and
`lead_score` but never calls `infer_region`. The one failure that is loud — a malformed
coordinate raising `TypeError` at `enricher.py:92` and discarding the whole run — is the one
failure an operator sees within minutes, and it is the least interesting one.

I am not re-litigating the geography. `007-enricher-region-inference.md` and
`094-infer-region-correction.md` own the boxes. What those two audits do not cover is the
operational half: **why a wrong region, once written, is unrecoverable, and why nobody finds
out.**

Everything below was verified by importing `enricher` with a stubbed `requests` (the same
technique as `tests/test_lead_signal.py:23-63`) and calling the function directly. No network
calls were made and no source file was modified.

---

## Findings, ranked by how long a human takes to notice

### S1 — A wrong region is written to the cumulative master and can never be recomputed

- **Where:** `enricher.py:331` (`if not r.get("region"):`), `main.py:209` (master write),
  `main.py:87-89` (blank cell → `None` on read), `cli.py:265-272` (the only replay tool).
- **Breaks:** inference is gated on falsiness, so it fills a blank and **never overwrites an
  existing value**. Once `"Akkar"` lands in `all_businesses.csv` for a Hermel row
  (`enricher.py:60-62` orders the Akkar box ahead of Baalbek-Hermel, so
  `infer_region(None, 34.394, 36.384)` returns `"Akkar"`), that value is authoritative
  forever. Every subsequent weekly cron reloads it (`scrape.yml:56` → `main.py:150`), skips it
  at `enricher.py:331`, and rewrites it unchanged.
  The operator's one offline repair tool, `leadminer score`, loops at `cli.py:265-272` over
  exactly four fields and **does not touch `region`** — so the documented way to re-apply
  current rules to an existing CSV cannot re-apply this one. The only remaining remedy is to
  blank the column by hand and pay for a full re-scrape: ~5 hours of Places API quota
  (`google_places.py:174`, 68 queries) plus a 300-minute Actions budget
  (`scrape.yml:25`). In practice nobody does that, so the error is permanent.
- **Trigger:** run the pipeline once. `infer_region(None, 34.394, 36.384, "LB")` → `"Akkar"`.
  Wait one cron cycle. The row is still `"Akkar"` and `leadminer score` cannot change it.
- **Detection time:** unbounded. Nothing in the repo compares `region` to ground truth;
  discovery requires a human who happens to know Lebanese geography reading a CSV cell.
- **Fix:** add a `region_source` column (`scraped` / `address` / `coords` / `none`) to
  `main.py:33-40` and a `leadminer recheck-region` command that re-derives `region` for every
  row whose `region_source` is `coords` or `none`, writing the old value to
  `region_previous` for one week.

### S1 — One malformed coordinate kills the entire run, after the expensive phase

- **Where:** `enricher.py:92` (unguarded `<=` on caller-supplied values),
  `enricher.py:330-335` (no per-record isolation), `main.py:187` (no try/except),
  `cli.py:314-318` (catches only `KeyboardInterrupt`, and production does not use the CLI).
- **Breaks:** `infer_region` compares caller-supplied values against floats with no type or
  finiteness check. A non-numeric coordinate raises `TypeError`, which propagates out of the
  `for r in records` loop at `enricher.py:330` and out of `main()` at `main.py:187`.
  **Verified:** a four-record batch whose third row has `lat="33.89"` raises
  `TypeError: '<=' not supported between instances of 'float' and 'str'`, prints *nothing*, and
  returns no records — three good rows are discarded with it. Region inference runs at
  `enricher.py:330`, i.e. **before** `check_websites` at `enricher.py:337`, so what is lost is
  the hours already spent in the three scrapers, and nothing has been written yet (writes begin
  at `main.py:209`), so there is no partial output to inspect.
  The codebase already knows coordinates are untrusted: `dedup._validity` wraps `float(value)`
  for `lat`/`lon` in `try/except` at `dedup.py:128-133`. `infer_region` has no equivalent
  guard. That asymmetry is the defect.
- **Trigger (all verified):**

  | Input | Result |
  |---|---|
  | `infer_region(None, "33.89", 35.50, "LB")` | `TypeError: '<=' not supported between instances of 'float' and 'str'` |
  | `infer_region(None, ["33.89"], 35.50, "LB")` | `TypeError: … 'float' and 'list'` |
  | `infer_region(None, object(), 35.50, "LB")` | `TypeError: … 'float' and 'object'` |
  | `infer_region(None, float("nan"), 35.50, "LB")` | returns `None` — **silent** (see S3) |
  | `infer_region(None, float("inf"), float("inf"), "LB")` | returns `None` — **silent** |

- **Why S1 despite being the fastest failure to notice:** it destroys a multi-hour run of paid
  API work, which is exactly "a run that dies" in the brief's severity table. It is not S1
  *because* it is subtle — it is S1 because of blast radius. Detection is fast (GitHub red X
  and a failure email from `scrape.yml:25` within minutes), which is the only good news here.
- **Fix:** guard the comparison inside `infer_region` —
  `if not isinstance(lat, (int, float)) or not math.isfinite(lat): return None` — and wrap the
  loop body at `enricher.py:331-335` in `try/except Exception` so one poisoned row degrades to
  `region=None` instead of ending the run.

### S2 — `None` is one bucket for at least seven distinct failures, and nothing counts it

- **Where:** `enricher.py:94` (`return None`), `enricher.py:85` (address gate),
  `enricher.py:90` (box table selection); only visible surfaces are `main.py:228-234` and
  `cli.py:123-126`, both of which print a single undifferentiated `Unknown` row.
- **Breaks:** the function returns `None` for all of the following, and the two printouts
  collapse them into one number with no reason breakdown:

  | # | Cause | Anchor | Verified result |
  |---|---|---|---|
  | 1 | No address and no coordinates | `enricher.py:85, 89` | `infer_region(None, None, None)` → `None` |
  | 2 | `country` not exactly `"SA"` → address ignored, LB boxes used | `enricher.py:85, 90` | `infer_region("King Fahd Rd, Olaya, Riyadh", 24.71, 46.67, None)` → `None`; the same call with `"SA"` → `"Riyadh"` |
  | 3 | Saudi address never matched at all (no keyword table for SA) | `enricher.py:85` gated to LB | `infer_region("Khobar, Saudi Arabia", None, None, "SA")` → `None` |
  | 4 | In-country coordinates outside the five KSA boxes | `enricher.py:70-76` | `infer_region(None, 18.216, 42.505, "SA")` (Abha) → `None` |
  | 5 | `nan` / `inf` coordinates — they survive `load_master` | `main.py:90-95`, `enricher.py:92` | `float("nan")` passes `float()` at `main.py:93`; every `<=` at `enricher.py:92` is `False` → `None` |
  | 6 | lat/lon transposed | `enricher.py:92` | `infer_region(None, 35.50, 33.89, "LB")` → `None` |
  | 7 | Coordinates hand-edited into an unparseable form | `main.py:92-95` | `float("33,89")` raises → coerced to `None` → case 1 |

  Cases 5 and 7 are the ones that will actually happen, because the CSVs are the product that
  humans open in Excel and Google Sheets — `main.py:118-119` writes `utf-8-sig` explicitly so
  that they *will* be opened in Excel. A European-locale save rewrites every `lat` as `33,89`,
  `float()` rejects it at `main.py:93`, and the entire Saudi book silently loses coordinate-based
  region inference with the run reporting success.
- **Why a human never notices:** the publish gate that stands between a bad run and Google
  Drive (`scrape.yml:71-83`) checks only that `all_businesses.csv` is non-empty and has ≥ 100
  rows. There is no region check anywhere. `leadminer validate` exists
  (`cli.py:135-201`) and checks name, BOM, coordinates, rating range and duplicates — **it has
  no region check at all** — and `scrape.yml` never invokes the CLI anyway (it runs
  `python main.py` at `scrape.yml:69`), so the validator is dead code in production.
- **Fix:** return a typed result (`RegionResult(value, source, reason)`), print
  `region: N inferred / M blank — top reasons: ...` next to the existing counters at
  `enricher.py:271-275`, and add a `region` check to `cmd_validate` with a
  `leadminer validate` step in `scrape.yml` before `scrape.yml:85`.

### S2 — `enrich()` silently depends on `resolve_country` having already run, and the only test encodes the coupling instead of removing it

- **Where:** `enricher.py:334` (`country=r.get("country", "LB")`), `main.py:184`
  (`resolve_country` runs first), `cli.py:266` (also runs it), `enricher.py:79-84` (signature
  gives no hint), `main.py:50-58` (the docstring that documents this exact trap for
  `normalize_phone`).
- **Breaks:** `main.py:50-58` explains at length why `.get("country", "LB")` never fires its
  default — `load_master` turns blank cells into `None`, and `None` is a *present* key, so the
  default is dead. `enricher.py:334` reintroduces precisely that expression. Today it is saved
  only by statement ordering in `main.main()`: `resolve_country` at `main.py:184`, `enrich` at
  `main.py:187`. Reorder those two lines, add a second entry point, or call `enrich()` on a
  `load_master`-shaped record from a script, and every Saudi record takes the `enricher.py:85`
  false branch *and* the `enricher.py:90` LB box table.
- **Trigger (verified):** the same Riyadh record,
  `{"address": "King Fahd Rd, Olaya, Riyadh", "lat": 24.71, "lon": 46.67, "country": None}`
  returns `"Riyadh"` when routed through `resolve_country` and **`None`** when routed through
  `enrich()`'s own `r.get("country", "LB")` expression. The whole Saudi book goes blank, no
  error, no warning.
- **Why the test does not catch it:** `tests/test_lead_signal.py:147-153` calls
  `infer_region(r["address"], None, None, resolve_country(r))` — it tests the *correct* usage
  and asserts it. There is no test that calls `enrich()` with a raw record, so the defect can
  never surface. The regression test documents the coupling rather than closing it.
- **Detection time:** unbounded; it would present as "the Saudi numbers went to zero" in a
  `by region` table nobody diffs.
- **Fix:** call `resolve_country` at the top of `enrich()` (or make `country` a validated
  required parameter normalised to `{"LB","SA"}`), and add a test that feeds `enrich()` one
  record shaped like `load_master` output.

### S2 — The region tables are order-dependent, the output carries no provenance, and the proposed table edits will silently re-route leads

- **Where:** `enricher.py:52-55` (one alternation per region), `enricher.py:86-88`
  (first match wins), `enricher.py:33-41` (South Lebanon precedes Nabatieh),
  `enricher.py:17` vs `enricher.py:35`.
- **Breaks:** because `_REGION_MAP` is an ordered list and the loop returns on the first hit,
  **list order — not geography — decides ties.** Verified keyword cross-contamination within
  the current tables: `jbeil` (Mount Lebanon, `enricher.py:17`) is a substring of `bint jbeil`
  (South Lebanon, `enricher.py:35`), and Mount Lebanon is checked first, so
  `infer_region("Bint Jbeil, South Lebanon", None, None, "LB")` returns **`"Mount Lebanon"`**.
  Likewise `zahle` (Bekaa, `enricher.py:43`) is a substring of `zahle el metn`
  (Mount Lebanon, `enricher.py:21`). Separately, the alternations have no word boundaries
  (`enricher.py:53` joins with `|` and no `\b`), so `sur`, `qaa`, `tyre`, `metn`, `koura`,
  `bliss`, `zouk` and `fanar` all match as bare substrings — confirmed.
  I am deliberately *not* claiming a specific wrong city from the substring issue; `094 §E`
  flags it and the geographic tables are that audit's business. The **operational** point is
  this: audits 007 and 094 both propose reordering and replacing these tables, and under
  first-match-wins every one of those edits silently re-routes addresses between governorates —
  in the cumulative master, with no test and, today, no column recording which rows were
  affected. There is no way to answer "what did this table change break?" after the fact.
- **Detection time:** unbounded. A `region_source` column would at least make the question
  answerable.
- **Fix:** add `region_source` (see S1), and land a table-driven test — one real Google
  `formattedAddress` and one real coordinate per governorate, asserting the expected label —
  before any table edit. `tests/test_lead_signal.py` is stdlib-only and already runs under
  `python3 -m unittest discover -s tests` (`tests/test_lead_signal.py:4-7`), so there is no
  excuse.

### S3 — An empty batch prints nothing at all, and for Lebanon an address silently outranks a precise coordinate

- **Where:** `enricher.py:232-233` (early return *before* the print at `enricher.py:235`),
  `enricher.py:85-88` (address branch returns before coordinates are consulted).
- **Breaks (empty batch):** `check_websites` returns at `enricher.py:232-233` before reaching
  the progress print at `enricher.py:235`. **Verified:** `enrich([])` returns `[]` and writes
  `''` to stdout — not one byte. An empty run is indistinguishable from a run that died before
  reaching enrichment. `scrape.yml:74-76` catches an empty *master*, but not an enrichment
  pass that did nothing.
- **Breaks (precedence):** for `country == "LB"` a keyword hit in the address returns at
  `enricher.py:88` before `enricher.py:89` ever looks at the coordinates, and nothing records
  which one won. **Verified:** `infer_region("Bliss Street, Beirut, Lebanon", 34.44, 35.85,
  "LB")` → `"Beirut"`, while the same coordinates with no address → `"North Lebanon"` (the
  coordinates are Tripoli). A stale or forwarded Google address therefore overrides a precise
  coordinate, silently and untraceably.
- **Detection time:** days for the empty case if anyone reads logs; unbounded for the
  precedence case.
- **Fix:** move the print at `enricher.py:235` above the early return at `enricher.py:232`, and
  record the winning branch in `region_source`.

---

## Not a bug, but worth knowing

- **There is no runtime, memory, network or concurrency risk here at all.** Measured 0.99 µs per
  call (200,000 calls in 0.197 s, `infer_region("Bliss Street, Gemmayze, Beirut, Lebanon",
  33.89, 35.50, "LB")`). One million records would cost ~1 second of a 300-minute budget
  (`scrape.yml:25`) — about 0.006 % of the run. Patterns are compiled once at module import
  (`enricher.py:52-55`); there is no per-call `re.compile`, no allocation in the hot loop, and
  nothing to leak. **This function is not why a run takes six hours**, and an engineer
  optimising the cron budget should not spend time here.
- **It is thread-safe, and correctly so.** It is called concurrently from 5 scraper workers
  (`google_places.py:194`, `google_places.py:275`) and sequentially from `enricher.py:332`. It
  only reads module-level compiled `re.Pattern` objects, which the `re` module documents as
  safe for concurrent use. No lock is present and none is needed.
- **"What happens when the network drops halfway" does not apply to this function, and that is
  its main operational virtue.** It is pure: no I/O, no network, no partial state. A network
  failure cannot produce a half-inferred record set from here. Every failure mode below is
  logical, which is exactly why none of them are loud.
- **Blank regions self-heal; only wrong regions are permanent.** `None` is written as an empty
  cell (`main.py:127`) and read back as `None` (`main.py:87-89`), which is falsy, so
  `enricher.py:331` re-infers it next week. That is why S1 is framed around *wrongness* rather
  than around blanks — a blank self-repairs, a wrong answer is entombed.
- **Google Places records never reach `enrich`'s inference pass.** `google_places.py:275`
  already sets `region`, so the guard at `enricher.py:331` skips them. The `region` column
  therefore mixes values produced by two different call sites with two different country
  sources — `_country_from_query` (`google_places.py:127-136`, which defaults to `"LB"` at
  `google_places.py:136`) versus `resolve_country` (`main.py:50-74`) — with nothing in the CSV
  to tell you which produced a given row.
- **Nothing downstream consumes `region`, which is why a total failure is invisible.**
  `completeness_score` (`enricher.py:101-117`), `lead_score` (`enricher.py:283-319`) and
  `recommend_service` never read it; the only readers in the whole repo are `enricher.py:331`
  and the two by-region printouts (`main.py:228-234`, `cli.py:123-126`). Delete `infer_region`
  entirely and every score, every CSV row count and every publish-gate check at
  `scrape.yml:71-83` would be unchanged, and the run would print a completely normal-looking
  summary. There is no canary. The real harm is mis-targeted humans reading the CSV, which is
  precisely the failure mode with no automated detector.
- **`nan` and `inf` survive `load_master`.** Verified: `float("nan")`, `float("NaN")` and
  `float("inf")` all pass the cast at `main.py:93` and are kept. Every comparison at
  `enricher.py:92` is `False` for `nan`, so the record silently falls out of coordinate
  inference. For `country == "LB"` a matching address rescues it; for `country == "SA"` the
  address branch is gated off at `enricher.py:85`, so a `nan` coordinate means no region at all.
  And because `str(float("nan")) == "nan"`, `write_csv` (`main.py:127`) writes it straight back
  to the master, making it self-perpetuating.
- **Interruptions are safe but opaque.** `KeyboardInterrupt` inside the loop at
  `enricher.py:330-335` propagates through `enrich` and `main` — neither has a handler — and
  nothing has been written yet (writes start at `main.py:209`), so the Drive master downloaded
  at `scrape.yml:56` is untouched. Result: exit 130 with a raw traceback, no partial output,
  no resume point. `cli.py:316-318` *does* handle this cleanly (`print("interrupted")`, return
  130) — but production invokes `python main.py` at `scrape.yml:69`, not `leadminer run`, so
  the clean path is never taken. Same reason `cmd_validate` never gates a publish.

---

## Recommended order of work

1. **Stop losing runs to one poisoned row (S1).** Add an `isinstance` + `math.isfinite` guard on
   `lat`/`lon` inside `infer_region` and wrap `enricher.py:331-335` in per-record isolation.
   ~10 lines, no behaviour change on valid data, removes the only way this function can end a
   300-minute run.
2. **Make wrong regions repairable (S1).** Add `region_source` to `main.py:33-40` and a
   `leadminer recheck-region` command, or extend the loop at `cli.py:265-272`, so the master can
   be re-derived without a paid re-scrape. Do this *before* landing any table fix from 007/094,
   otherwise the corrections only apply to new rows.
3. **Make silence loud (S2).** Return a reason with the value, print a region fill-rate and
   top-reasons line beside the existing counters at `enricher.py:271-275`, add a region check
   to `cmd_validate` (`cli.py:135-201`), and insert a `leadminer validate` step in `scrape.yml`
   ahead of `scrape.yml:85`.
4. **Remove the `resolve_country` ordering landmine (S2).** Normalise the country inside
   `enrich()` and add a test that calls `enrich()` with a `load_master`-shaped record, not just
   `infer_region(resolve_country(r))` as `tests/test_lead_signal.py:152` does today.
5. **Pin the tables with tests before editing them (S2/S3).** One real address plus one real
   coordinate per governorate, table-driven, in the existing stdlib-only test file. Without this,
   the table rewrites proposed in 094 cannot be applied safely — list order decides the outcome
   and nothing records which rows a change affected.