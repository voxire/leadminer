# 375 — `completeness_score` through an operations lens

**Target:** `enricher.py:101` · **Lens:** ops (a six-hour cron run, unattended)

## Verdict

The function is not the problem — it is 17 lines of pure arithmetic with no I/O, no
`print`, no `try`, and a measured cost of **~1.0–1.2 µs/call** (200,000 calls in 0.20–0.23 s across runs), so
it cannot time out, cannot be interrupted meaningfully, and cannot fail on its own. The
problem is the **contract it publishes**: a 0–7 integer whose only consumer is a
single `>= 1` test at `main.py:206`, computed from fields that `main.py:194` mutates
underneath it, using raw truthiness instead of validation. Result: the run publishes a
"qualified leads" file that is ~a copy of the master, a `completeness_score` column that
credits fields the row no longer contains, and **no signal at all** that would tell anyone
enrichment has stopped working. Every one of those failures is invisible to a human who
only reads the run log, and invisible forever to one who only reads the Actions tab.

---

> **Line-number note.** `enricher.py` is being edited concurrently while this
> audit fleet runs — during this review `enricher.py` gained a line at the top and
> then lost it again, shifting everything below line 1 by one in both directions.
> Anchors below are against the committed state, where `completeness_score` is
> defined at `enricher.py:101-117` and called once at `enricher.py:349`. Every
> anchor was re-verified against the working tree at write time. If the numbers
> have drifted again, the function body is reproduced here — 17 lines, unambiguous.
>
> ```python
> def completeness_score(record: dict) -> int:
>     score = 0
>     if record.get("phone"):
>         score += 1
>     if record.get("email"):
>         score += 1
>     if record.get("website"):
>         score += 1
>     if record.get("address"):
>         score += 1
>     if record.get("facebook") or record.get("instagram"):
>         score += 1
>     if record.get("whatsapp"):
>         score += 1
>     if record.get("linkedin"):
>         score += 1
>     return score
> ```

## Answers to the six ops questions, up front

| Question | Answer | Anchor |
|---|---|---|
| **What does it print?** | Nothing. No `print`, no `log`, no metric. Verified by `inspect.getsource`: 0 occurrences of `print`/`log.`/`raise`/`try:`/`except`. | `enricher.py:101-117` |
| **What does it swallow?** | Nothing — it has no handler to swallow with. Its failure mode is the opposite: it *manufactures* a confident number from unvalidated input. | `enricher.py:103-116` |
| **Network drops halfway?** | This function is unaffected (pure). But its **output** degrades silently: `check_websites` turns every transport failure into `UNKNOWN` with all contacts `None`, so every website-backed record loses 1–4 points at `enricher.py:349` with no counter distinguishing "host down" from "network down". The one number the run prints about this score (`main.py:221`) barely moves. | `enricher.py:176-178`, `main.py:221` |
| **Dependency returns garbage?** | This is the live one. Any non-empty, non-`None` value scores. `"yes"`, `"no"`, `"N/A"`, `"-"`, `"  "`, `"غير معروف"` all earn a full point. | `enricher.py:103-116` |
| **Called with an empty list?** | It takes a single `dict`, so `completeness_score([])` raises `AttributeError: 'list' object has no attribute 'get'` — loud, not silent. `completeness_score({})` returns `0`. The real empty-run path is `check_websites`'s early return at `enricher.py:232-233`, which fires *before* the `print` at `enricher.py:235`, so an empty run emits zero `[Enricher]` lines; it is then caught downstream by `scrape.yml:78-83` (`ROWS=0 -lt 100`). | `enricher.py:232-235`, `scrape.yml:78-83` |
| **Interrupted?** | Not a risk *here*. A 2,000,000-row master spends ~2 s total inside `completeness_score`, against a 300-minute job budget. The interrupt risk lives in the 40-worker HTTP pool at `enricher.py:237`. | measured; `scrape.yml:25` |

---

## Findings

### S1 — The score is computed from fields `main.py` then erases, so the shipped CSV contradicts itself

- **Where:** written at `enricher.py:349`, fields read at `enricher.py:103-116`, phone
  overwritten at `main.py:192-194`.
- **Breaks:** `enrich()` scores the **raw** phone, then `main.py` runs
  `r["phone"] = normalize_phone(raw_phone, country)`. `normalize_phone` returns `""` for
  anything with fewer than 7 digits (`dedup.py:45-46`). The row ships with `phone` empty
  and `completeness_score` still counting the phone. This is not conditional on a broken
  upstream — it fires on **every** record whose raw phone is un-normalizable, which is
  exactly the population OSM produces (`phone=yes`, `phone=no`, `phone=N/A`).
- **Trigger:** `phone="yes"` — a mistagging pattern OSM documents explicitly under
  "Possible tagging mistakes" on <https://wiki.openstreetmap.org/wiki/Key:phone>
  ("Perhaps telephone=yes is meant to denote that a facility has a public telephone
  available"). Verified end to end:

  ```
  completeness_score({"phone": "yes", ...})  -> 1      # enricher.py:103
  normalize_phone("yes", "LB")               -> ""     # dedup.py:45-46
  -> CSV row: phone="", completeness_score=1          # main.py:194 after enricher.py:349
  ```

  Same result for `no`, `none`, `N/A`, `12345`, `abc`, `tel:`, `0` (all verified).
- **Why S1:** the product CSV is internally inconsistent — a reader sees a blank `phone`
  beside a nonzero "completeness" figure and has no way to know the two disagree. The
  column that is supposed to certify data quality is itself untrustworthy, with nothing
  in the log to say so.
- **Fix:** move `r["phone"] = normalize_phone(...)` (and the `country` resolution at
  `main.py:184`) to **before** `enrich(records)` at `main.py:187`, or recompute
  `completeness_score` in the same loop that recomputes `lead_score` at `main.py:197`.
  Note the author already did the latter for `lead_score` and left a comment saying so
  (`main.py:195-196`) — the omission is specific to this score.
- **Already reported:** `083-SYNTH-s1-backlog.md:105` item 48 (via 084, 086) flags the
  ordering bug. This report adds the ops consequence (detection latency) and the
  `phone=yes` citation.

### S1 — A `>= 1` threshold on a 0–7 score is a vacuous filter, so `qualified_businesses.csv` is the master and cannot detect degradation

- **Where:** range is 0–7 (`enricher.py:103-116`, documented at `README.md:64`); threshold
  is `main.py:206` `r.get("completeness_score", 0) >= 1`; documented as "Subset with at
  least one contact signal" at `README.md:46`.
- **Breaks:** one point out of seven passes the filter. `address` alone supplies that
  point, and `address` is *always* populated for a Google record
  (`google_places.py:286` → `address=place.get("formattedAddress")`). So a Google record
  with no phone, no email, no website and no social scores **1** and lands in
  "qualified" — verified:

  ```
  Google record, address only, zero contacts -> completeness_score=1 -> qualified=True
  ```

  For OSM, `osm.py:87` sets `address=None` only when no `addr:*` tag exists, and those
  same nodes nearly always carry a `website`/`url`/`phone` tag (`osm.py:89-91`), which is
  itself a scored point. The practical result is that `qualified_businesses.csv` differs
  from `all_businesses.csv` by a few percent of rows, not by a meaningful filter. Two of
  the five paid deliverables are the same file.
- **Why S1 and not S2:** a deliverable named "qualified" that does not qualify is a wrong
  result in the product, and it is 100% reproducible on the very first run.
- **The ops consequence is worse than the mislabel:** because the filter saturates, it has
  **zero power to detect enrichment failure**. Suppose the network drops at hour 2 of a
  six-hour run. Every one of 40 workers' fetches returns `UNKNOWN`
  (`enricher.py:176-178`), so email / instagram / whatsapp / linkedin are never populated,
  and thousands of rows lose 1–4 points each. `main.py:221` prints
  `Qualified (score >= 1): 4,610` against a baseline of 4,800 — a 4% move on a number
  nobody diffs week over week, on a field whose threshold is already saturated. (The
  specific figures are illustrative: `data/` is gitignored and no run output exists in
  the repo, so no measured distribution is available. The *mechanism* — one address-only
  point satisfying the filter — is verified, and it bounds the movement from below at
  "everything Google returns", which is the majority of the corpus.) The
  visible casualty is only `SALES-READY (actionable)` at `main.py:226`, because
  `has_any_contact` (`main.py:137-145`) demands a *usable* channel rather than mere
  presence. In other words: **the one real detector of enrichment health is `lead_score`'s
  neighbourhood, not the score built specifically to measure enrichment.**
- **Trigger:** the run log from any week in which the enricher HTTP pass had a bad day.
  Nothing in `scrape.yml:71-83` checks the score, its distribution, or the
  live/dead/unreachable ratio — the validation step only asserts the file is non-empty
  and has ≥ 100 rows.
- **Fix:** raise the threshold to `>= 3` at `main.py:206` and print the full 0–7
  histogram (`collections.Counter(r["completeness_score"] for r in records)`) next to
  `main.py:221`, plus run-over-run deltas.

### S1 — Presence-only truthiness: sentinel and whitespace values score as real contacts

- **Where:** `enricher.py:103-116` — seven bare `if record.get(...):` tests.
- **Breaks:** the function asks "is this value truthy", never "is this value a phone
  number / an email / a URL". Verified: every one of `"no"`, `"none"`, `"null"`, `"N/A"`,
  `"unknown"`, `"-"`, `"0"`, `"false"`, `"غير معروف"`, `"not available"`, `"0.0"`,
  `"undefined"` scores **1**. Whitespace-only strings `"  "`, `"\t"`, `"\n"` score
  **1**. Seven junk fields compose a record that scores a **perfect 7/7** and passes
  `qualified` — verified:

  ```
  {'phone':'no','email':'none','website':'null','address':'N/A','facebook':'-',
   'instagram':'0','whatsapp':'false','linkedin':'unknown'}
  -> completeness_score = 7 / 7,  qualified = True
  ```

  The codebase already owns the correct helper: `dedup._is_missing` (`dedup.py:79-90`)
  treats whitespace as missing, and `_validity` (`dedup.py:120-140`) pattern-matches email
  and URL. `completeness_score` uses neither. (`dedup` imports nothing from `enricher`, so
  importing it here creates no cycle.)
- **Amplifier — `osm.py:95-96`:** `if website and not website.startswith("http"):
  website = "https://" + website` converts *any* junk website tag into a
  syntactically-plausible URL. Verified: `website=no` → stored as `https://no`
  (hostname `no`) → `enricher.py:107-108` awards the website point, `check_websites`
  (`enricher.py:231,238`) then spends a real HTTP request on it, DNS fails, and the
  record ends up with a URL in `with_websites.csv` that has never existed. This also
  pollutes the `with_websites` / `without_websites` split at `main.py:199-200`.
- **Why S1:** wrong number, in the shipped product, with no log line. The mechanism is
  unconditional (proven for arbitrary strings); the frequency depends on upstream junk,
  which nobody measures.
- **Fix:** gate each point on a validator —
  `if normalize_phone(record.get("phone"), country) for the phone point`,
  `dedup._validity("email", v) > 0`, `dedup._validity("website", v) > 0` — and reject the
  OSM prefixer at `osm.py:95-96` unless the value already contains a dot and a TLD.

### S2 — One corrupt `completeness_score` cell in the Drive master bricks every future run, and the `.get(key, 0)` guard is illusory

- **Where:** `main.py:206`; the guard is undone by `main.py:87-88` and `main.py:96-101`.
- **Breaks:** `load_master` maps every blank cell to `None` (`main.py:87-88`), then maps
  every non-numeric int cell to `None` (`main.py:96-101`). `main.py:206` then writes
  `r.get("completeness_score", 0) >= 1` — but `.get`'s default **never fires**, because
  `load_master` guarantees the key is *present* with value `None`. Verified:

  ```
  cell=""     -> loaded=None -> TypeError: '>=' not supported between 'NoneType' and 'int'
  cell="N/A"  -> loaded=None -> TypeError
  cell="high" -> loaded=None -> TypeError
  {'completeness_score': None}.get('completeness_score', 0) -> None
  ```

  `main()` dies, the `Run scrapers` step (`scrape.yml:65-69`) exits non-zero, the upload
  steps (`scrape.yml:85-89`, which lack `if: always()`) are skipped, and **no new leads
  are published at all**. Every subsequent weekly run fails identically until a human
  hand-edits `gdrive:leads/all_businesses.csv` — and the run log points at `main.py`, not
  at the offending cell, so diagnosis means bisecting a multi-hundred-thousand-row CSV.
- **Why S2 and not S1:** detection is fast (red X on the Actions tab, minutes) and the
  pipeline *fails closed* rather than publishing corruption. But the blast radius is
  total and the recovery is manual. Nothing in this codebase can produce such a cell —
  `enricher.py:349` writes an `int` for every row unconditionally — so the exposure comes
  from the file being a shared, human-consumed Drive artifact (`README.md:41-52`), which
  is exactly how a Sheets sort, a paste, or a trimmed trailing column introduces it.
- **This column is uniquely exposed:** `completeness_score` is the only int column
  compared with `>=` against a raw loaded value. `lead_score` is recomputed
  (`main.py:197`) and `review_count` is coerced inside a `try/except`
  (`pitch_recommender.py:44-47`).
- **Fix:** `cs = r.get("completeness_score"); cs = cs if isinstance(cs, int) else 0`, or
  drop the column from `_INT_FIELDS` at `main.py:44` and coerce at the single point of
  use. Add a `leadminer validate` gate that rejects a master containing any
  non-numeric or blank `completeness_score`.

### S2 — The score is not comparable across sources and rises with *our* scraping effort, not the lead's quality

- **Where:** `enricher.py:111` (facebook/instagram share one point),
  `enricher.py:252-259` (extraction never produces `facebook`),
  `google_places.py:294-295` (Google records always arrive with both set to `None`).
- **Breaks:** three asymmetries compound:
  1. `facebook` earns a point (`enricher.py:111`) but `_fetch_website` extracts only
     email / instagram / whatsapp / linkedin (`enricher.py:252-259`) — facebook credit is
     reachable *only* from OSM tags (`osm.py:92-93`). (Already noted at
     `025-enricher-regex-precision.md:279`; restated here for its scoring effect.)
  2. Google records start with all four social/contact fields `None` and can only earn
     those points **if they have a website we successfully fetched** — a network outcome.
  3. OSM records earn up to 6 points from tags alone, with no HTTP involved.
  Verified: an OSM-style record carrying 6 tag signals scores **5/7** with zero HTTP
  work; the equivalent Google record with nothing scraped scores **2/7**.
- **Why S2:** the column is not a property of the business, it is a property of how much
  *we* happened to scrape. When the HTTP pass silently degrades, scores fall across the
  board by 2–3 points and every cross-week comparison of this column becomes meaningless —
  silently.
- **Fix:** separate the two concepts the column conflates: a static `data_completeness`
  (tag-sourced fields only) and a dynamic `enrichment_yield` (HTTP-sourced fields only),
  each with its own source-fair weighting.

### S3 — The score is vestigial: one consumer reads and discards it, the weights are undocumented, and the range is never asserted

- **Where:** `enricher.py:101-117`; `pitch_recommender.py:48-52`; `main.py:206`;
  `README.md:64`.
- **Breaks:**
  - `pitch_recommender.py:48-52` reads `completeness_score`, coerces it through a
    `try/except`, assigns it to `completeness` — and **never uses it again**. The
    if-chain at `pitch_recommender.py:55-99` contains no reference to it, despite the
    comment at `pitch_recommender.py:68-69` claiming the SEO branch fires on "weak
    completeness". Confirmed by grep: the only occurrences of `completeness` in the file
    are lines 48, 50, 52 and that comment. So the column drives no pitch decision.
  - The seven equal weights are implicit magic numbers with no comment explaining why
    `address` (a field no one calls) is worth the same point as `whatsapp` (the dominant
    B2B channel in Lebanon and KSA — see `017-osm-overpass-query.md:55`).
  - The 0–7 range is documented at `README.md:64` but enforced nowhere. `048-data-quality-validation.md:134`
    already flags the missing invariant; nothing in `main.py` or `enrich()` asserts it.
  - No run output reports the distribution — only the saturated `>= 1` count at
    `main.py:221`.
- **Fix:** delete the dead read in `pitch_recommender.py:48-52` (or wire `completeness`
  into the tier-3 branch it was written for), name the weights as module constants, and
  print the histogram.

### S3 — `website_live` is excluded from the score, so the single strongest pitch does not move it and is routed to the wrong file

- **Where:** `enricher.py:101-117` (no `website_live` term); `enricher.py:300-304`
  (`lead_score` does weight it: +20 for a DEAD site); `main.py:199-200`;
  `README.md:48`.
- **Breaks:** a server-confirmed dead site is the pipeline's best opportunity
  (`pitch_recommender.py:57-58` → "Website rebuild + maintenance"), yet it scores
  identically on `completeness_score` to a healthy site with the same fields, because
  only `website_live` distinguishes them and the score ignores it. Worse, `README.md:48`
  describes `without_websites.csv` as the "new-site pitch targets" file, but the split is
  on `website` presence alone (`main.py:199-200`), so every dead-site rebuild target —
  the highest-value leads in the database — is exported to `with_websites.csv`.
- **Fix:** add a `website_live is False` term to the score, and split
  `without_websites.csv` into "no website" and "dead website" — or at minimum route DEAD
  records into the pitch file.

---

## Ranked: every silent failure, by how long a human takes to notice

"Notice" = a human who reads the Actions log each week and glances at the summary block.
Nobody diffs `completeness_score` values across weeks, because nothing prints them.

| # | Silent failure | In the log? | In the CSV? | Time to notice |
|---|---|---|---|---|
| 1 | Saturated `>= 1` filter — enrichment fails wholesale, `qualified` moves ~4% | no | no | **never** |
| 2 | No score distribution logged — mean falls 3.1 → 1.2 across a week | no | only by hand-diffing two runs | **never** |
| 3 | Score is vestigial: it drives no pitch and only one saturated filter | n/a | no | **never** |
| 4 | Score not source-comparable / effort-dependent — cross-week comparisons meaningless | no | no | **never** |
| 5 | `website_live` excluded — dead sites score like live ones, land in the wrong file | no | only on manual triage of the wrong file | **never** |
| 6 | Junk/sentinel strings score as contacts (`phone=yes`, whitespace, `"N/A"`) | no | yes, but only by reading the *value* columns | **months**, and only if someone audits the CSV |
| 7 | Score credits a phone `main.py:194` erased — row self-contradicts | no | yes, visible as blank phone + nonzero score | **weeks**, on a manual spot-check |
| 8 | `osm.py:95-96` mints `https://no` — phantom websites in `with_websites.csv` | no | yes, in the `website` column | **weeks**, if anyone looks at that column |
| 9 | `None >= 1` `TypeError` kills the run | **yes, immediately** | n/a | **minutes** — but unrecoverable without hand-editing Drive |

The inversion at row 9 is the finding: **the only failure mode that is loud is the only one
that cannot be caused by the code itself.** Every failure the code *can* cause is silent.

---

## Not a bug, but worth knowing

- **The function is genuinely clean as a unit.** No `print`, no `log`, no bare `except`,
  no global reads, no I/O, no state. It is the one function in `enricher.py` that could be
  lifted into a test module unchanged — `032-fixtures-offline-replay.md:84` already
  allocates `tests/test_enricher_pure.py` for exactly this. Every finding above is a
  finding about the *contract*, not the code.
- **It cannot time out or be usefully interrupted.** ~1.0–1.2 µs/call measured; ~2 s for a
  2M-row master against `scrape.yml:25`'s 300-minute budget. Do not spend remediation
  effort here; spend it on `check_websites` (`enricher.py:237`), which is where the
  six-hour run actually lives.
- **The score itself never goes stale.** `enrich()` recomputes it for every record every
  run (`enricher.py:349`). What *can* go stale is the fields it reads — see the dedup
  staleness ratchet at `028-csv-roundtrip-fidelity.md:144`, which is a different report's
  finding and not re-litigated here.
- **`.get()` makes key presence irrelevant.** Verified: a record with `phone` absent, a
  record with `phone` deleted, and a record with `phone=None` all score identically. So a
  key-set migration cannot shift the score; only value changes can. This is mildly
  reassuring and worth stating so nobody adds a key-presence bug later.
- **`enrich([])` is caught, but by luck.** `check_websites` returns before its first
  `print` (`enricher.py:232-233` vs `:235`), so an empty run is silent at the enricher
  layer; the rescue is entirely `scrape.yml:78-83`'s row count. If that guard is ever
  relaxed, an empty run publishes five header-only files with no complaint from Python.

## Cross-references (already covered; not re-litigated here)

- S1 ordering bug: `083-SYNTH-s1-backlog.md:105` item 48 (084, 086).
- Facebook scored but never extracted: `025-enricher-regex-precision.md:279`,
  `056-social-profile-matching.md:12`.
- Missing range invariant: `048-data-quality-validation.md:134`.
- Score double-counted by `lead_score`: `009-lead-score-model.md:37`,
  `043-scoring-upgrade.md:34`.
- Facebook/WhatsApp tag coverage from OSM: `017-osm-overpass-query.md:55`,
  `018-osm-tag-extraction.md:21`.

## Recommended order of work

1. Move `resolve_country` (`main.py:184`) and `normalize_phone` (`main.py:192-194`)
   **before** `enrich(records)` (`main.py:187`), and recompute `completeness_score` in
   the same loop that already recomputes `lead_score` (`main.py:197`). One reorder, fixes
   S1 #1, and matches the comment the author already wrote at `main.py:195-196`.
2. Harden `main.py:206`: `isinstance` check instead of the illusory `.get(..., 0)`.
   One line, removes a total-outage failure mode.
3. Replace truthiness with validation inside `completeness_score`, reusing
   `dedup._is_missing` and `dedup._validity`; reject the `osm.py:95-96` prefixer unless
   the value has a dot and a plausible TLD.
4. Raise the threshold to `>= 3` and log the 0–7 histogram plus week-over-week deltas.
   This is what converts findings 1–5 from "never noticed" into "noticed in one run".
5. Delete the dead `completeness` read in `pitch_recommender.py:48-52`, or wire it into
   the tier-3 branch it was written for.
6. Add `website_live is False` to the score and route DEAD records to the pitch file.

Steps 1–3 are small, local, and independently shippable. Step 4 is the one that changes
what the pipeline is *for*, and should be decided alongside
`043-scoring-upgrade.md` rather than alone.