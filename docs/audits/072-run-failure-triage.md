# 072 — Run-failure triage runbook

## Verdict

Diagnosing "a bad week" is a funnel problem: six distinct failure modes all
present as "fewer rows than last week," so they must be isolated stage-by-stage
with the numbers each stage owns. The runbook below is an ordered decision tree
that walks scrape → whitelist → dedup → enrichment → export, using a **run
manifest** as the primary input and the five CSVs as a degraded fallback. The
single blocker today is that **no manifest exists and no per-run metric is
persisted** — every count is an ephemeral `print()` — so the full tree can only
run after the one-line `run_history.csv` write from audit 041 lands; until then,
only the CSV-only degraded path (Step 0) is usable.

## Inputs and baselines

You need two things before starting:

1. **This run's manifest**, with at least the fields in the table below. The
   manifest is specified in audit 040 (`data/manifest_<run_id>.json`,
   `040-observability.md`) and its CSV-friendly subset in audit 041
   (`data/run_history.csv`, `041-anomaly-detection.md`). The runbook only needs
   the funnel counters, not the full lineage.
2. **A baseline** = the **median of the last 3 successful runs** of each metric
   (fallback: the previous run; a first run only bootstraps, never alerts). This
   is 041's convention; the tree's thresholds are all expressed against it.

| Manifest field | Owned by stage | Source in code (when a counter is added) |
|---|---|---|
| `master_before` | merge | `main.py:150-152` (`len(master)`) |
| `osm_raw`, `wikidata_raw`, `google_raw` | scrape | `main.py:165` (`len(batch)` per scraper) |
| `google_requests`, `google_errors_401/403/429` | scrape | `google_places.py:209-220` |
| `raw_total` | scrape | `main.py:170` |
| `whitelist_kept`, `whitelist_dropped` | whitelist | `main.py:172-176` |
| `dedup_merged_by_phone`, `dedup_merged_by_name` | dedup | `dedup.py:112-123` |
| `unique_businesses` | dedup | `main.py:181` (`len(records)`) |
| `net_new` = `unique_businesses − master_before` | dedup | derived |
| `websites_targeted`, `live`, `dead`, `unknown` | enrichment | `enricher.py:223,256-258` |
| `email/instagram/whatsapp/linkedin_count` | enrichment | `enricher.py:259-262` |
| 5 export `row_count` + `sha256` | export | `main.py:209-213` |

CSV-only mode needs the same *baselines*, which today must be hand-recorded from
prior Drive snapshots because nothing stores them.

---

## The decision tree

Run top to bottom. Each step lists **the numbers to check**, the **threshold**,
and **which diagnosis it points to**. Stop at the first match — the first broken
stage upstream explains everything downstream, so do not "fix" the later stages
first.

### Step 0 — Did the run even finish? (always first)

- Check: manifest `status` is `committed`/`complete`, and `finished_at` exists.
  If `status` is `timeout`/`partial`/`failed`, this is a **run problem, not a
  data problem** — go to audit 050's resume protocol, do not interpret numbers.
- Check the export **cross-file invariants** in `all_businesses.csv` (these hold
  regardless of data quality and are the strongest CSV-only signal you have):
  - `with_websites + without_websites == all_businesses` (exact; `main.py:199-200`
    partitions every record by `bool(website)`).
  - `qualified <= all_businesses` (`main.py:206`), `sales_ready <= all_businesses`
    (`main.py:202-205`).
  - All five files have the same 23-column header (`main.py:33-40`) and no
    mid-row trailing/truncated last line.
  - **Any violation → D6 (export truncated / mixed generation).** Do not pass go.

### Step 1 — Did a source break?

- Check: `osm_raw`, `wikidata_raw`, `google_raw` vs baseline.
- Threshold (041 C2): a source at **0** (with a non-zero baseline) or **< 25% of
  baseline** = BLOCK → **D1 (source broke)**. `< 50%` = WARN.
- Disambiguate *which* source, and *why*:
  - `osm_raw == 0` → Overpass timed out / 5xx after 3 retries
    (`osm.py:38-53`). A single POST, so it's all-or-nothing.
  - `wikidata_raw == 0` → SPARQL timeout / 5xx after 3 retries
    (`wikidata.py:40-56`). Also all-or-nothing, and capped at 5000
    (`wikidata.py:25`).
  - `google_raw == 0` → three sub-causes, split on `google_requests`/errors:
    - `google_requests == 0` → key missing, scraper returned before any call
      (`google_places.py:157-160`).
    - `google_requests > 0` + many 401/403 → bad or Places-disabled key; note a
      bad key surfaces as **400/403**, not 401 (`015-google-places-pagination.md`
      S1).
    - `google_requests > 0`, no errors, 0 places → query set returned nothing
      (unlikely for all 67 at once) or response shape changed (fall through to
      **D2**).
  - **All three** collapse at once → not three independent outages; suspect the
    runner's egress/DNS (check durations in the manifest), a transient network
    partition, or the run never fetched at all.
- If all three ≈ baseline → no source broke. Continue.

### Step 2 — Did Google change its response shape? (only if Google ran)

- This is the subtle one: `google_raw` can look **normal** while every downstream
  field empties out. Check the **per-source field fill rates** on Google-only
  rows (rows whose `source` contains `google_places`):
  - Google `website` fill rate = rows with non-empty `website` / `google_raw`.
  - Google `phone` fill rate; Google `rating` fill rate (rating not NULL).
- Threshold: any of these collapses (e.g. `> 50%` → near 0) while `google_raw`
  and `google_requests` are unchanged → **D2 (shape change)**. The Places API
  renamed/dropped a field, or the field mask (`google_places.py:31-42`) no longer
  returns it, or `displayName`/`id` moved so records are silently skipped
  (`google_places.py:236-238`).
- A second D2 signature: `google_raw` stable but the **category strings change** —
  a spike in *new* distinct `category` values not in `PRIORITY_INDUSTRIES` /
  `ADJACENT_BUSINESSES` (Google renames `types`, or `_pick_category` starts
  returning `"business"` at `google_places.py:296`). This pushes dropped-up in
  Step 3, so rule D2 **in** before blaming the whitelist.

### Step 3 — Did the whitelist drop everything?

- Check: `raw_total` ≈ baseline (healthy scrape) **but** `whitelist_kept`
  collapses; `whitelist_dropped / raw_total` → near 100%.
- Threshold: `whitelist_dropped / raw_total` was ~X% last week and is now > 2×
  that, with `raw_total` unchanged → **D3 (whitelist dropped)**.
- Separate D3 from D2 using the dropped rows' categories: if the dropped set is
  dominated by **new/renamed category strings** → it's D2 (Google renamed types).
  If the dropped set is **categories that used to pass** (e.g. `restaurant`
  vanished from the allowlist) → D3 (a regression in `whitelist.py:17-131`).
  Note the whitelist has a substring fallback (`whitelist.py:122-128`), so it
  rarely drops *everything* by accident unless `category` itself went `None`
  (Wikidata rows frequently have no English `P31` label → `category=None` →
  `is_business_category(None)==False` at `whitelist.py:110-111`).
- Downstream confirm: `all_businesses ≈ master_before` (no net growth) with a
  healthy `raw_total`.

### Step 4 — Did dedup over-merge?

- Check three numbers together:
  1. `net_new` (= `unique_businesses − master_before`) vs `raw_filtered`.
     Absorption `1 − net_new / raw_filtered` (041 C3).
  2. `dedup_merged_by_phone` and `dedup_merged_by_name`.
  3. `unique_businesses` vs baseline for a comparable raw volume.
- Thresholds (041 C3): absorption **> 99.9%** (everything collapsed into the
  master — replay or over-merge) or **< 10%** (identity model broke — nothing
  merged) = BLOCK.
- The over-merge signatures to look for:
  - `dedup_merged_by_name` spikes to ≈ the count of **phone-less** records → the
    name-key over-merge (audit 003 S1): `_extract_city` returns the *country*
    (`dedup.py:72-76`), so every same-named phone-less business nationwide keys
    `(name, "lebanon")` and collapses.
  - `dedup_merged_by_phone` spikes on records that are *distinct* businesses →
    shared-hotline over-merge (003 "Not a bug") or the `+961` default: a KSA
    number in a record whose `country` is `None`/unknown gets prefixed `961`
    (`dedup.py:48`, 055 S2) and collides with real Lebanese numbers.
- Caveat (041 S3): as the market saturates, `net_new → 0` is *healthy*; only call
  D4 when raw volume (Step 1) and whitelist (Step 3) are both healthy yet
  `unique_businesses` still barely grows **and** `dedup_merged_by_*` is elevated.

### Step 5 — Did enrichment fail?

- Check: `websites_targeted` vs baseline **first** (it must equal
  `with_websites` count = the input to the fetcher).
  - `websites_targeted` collapsed → the failure is **upstream** (Steps 1–4), not
    enrichment. Do not chase the enricher.
  - `websites_targeted ≈ baseline` → read the three outcome buckets
    `live / dead / unknown` and the four contact counts.
- Thresholds (041 C4/C5):
  - `unknown` spikes while `live + dead` collapses → transport failures (DNS /
    timeout / reset), recorded `website_live = None` (`enricher.py:170,184,239`).
    This is a **connectivity / IP-block at the TCP layer** problem, not "the web
    died."
  - `dead` spikes while `live` collapses → the fetcher got **HTTP error statuses**
    (403 Cloudflare challenge, 5xx), recorded `website_live = False`
    (`enricher.py:174-176`). 073 S2: this is "we got IP-blocked," not real dead
    sites — a dead-site rate `> 60%` and `> 2×` baseline is a block, not a market
    event.
  - `live ≈ baseline` but `email/instagram/whatsapp/linkedin` collapse → the
    **contact regexes / body read** broke (`enricher.py:127-133,191-212`) or the
    sites moved to JS-heavy SPAs past the 200 KB cap (`enricher.py:155,180`).
    → **D5 (enrichment failed)**, in whichever of these three sub-shapes.
- CSV-only cross-check: the `website_live` column (True/False/blank) inside
  `with_websites.csv` carries this same signal one run at a time.

### Step 6 — Did the export truncate?

- Check each CSV's **actual** `row_count` (= lines − 1) and `sha256` against the
  manifest's `artifacts` block (audit 050 §6).
  - Actual `<` manifest rows, or hash mismatch, or `file_size` short → **D6
    (truncated upload)**. Because `write_csv` is atomic (`main.py:121-130`), a
    *locally* truncated file should be impossible; a mismatch means the rclone
    copy (`scrape.yml:46-50`) was interrupted, or the consumer read an
    uncommitted/mixed generation.
  - Internal invariants from Step 0 failing (`with + without ≠ all`) → mixed
    generation, not plain truncation — different files came from different
    `records` lists or different runs.

---

## The six diagnoses, one card each

### D1 — A source broke

**Implicate:** one of `osm_raw / wikidata_raw / google_raw` at 0 or < 25%
baseline while the others are healthy. **Confirm:** the source's error counters
in the manifest (Overpass/Wikidata 5xx timeouts; Google 400/403 key errors, 429
spikes). **Fix:** restart that source; check the API key / quota; if it is
Google 400/403, rotate the key. **Watch out:** a simultaneous all-three collapse
is an environment/egress failure, not three bugs.

### D2 — Google changed its response shape

**Implicate:** `google_raw` and `google_requests` stable, but Google-only rows'
`website` / `phone` / `rating` fill rates collapse, or the category vocabulary
shifts to new/`"business"` values. **Confirm:** diff the `places` payload for one
query against the field mask (`google_places.py:31-42`) — the field the code
reads (`displayName`, `websiteUri`, `nationalPhoneNumber`, `types`) is gone or
renamed. **Fix:** update `FIELD_MASK` and `_pick_category` (see 016 S1: request
`primaryType`, drop `types[0]` position reliance).

### D3 — The whitelist dropped everything

**Implicate:** `raw_total` healthy, `whitelist_dropped / raw_total` spikes, and
the dropped categories are ones that used to pass. **Confirm:** a category that
should be allowed (e.g. `restaurant`) is no longer in `PRIORITY_INDUSTRIES`
(`whitelist.py:17-42`). **Fix:** revert the allowlist change, or fix the
`category=None` path for Wikidata (missing English `P31` label). **Watch out:**
if the dropped categories are all *new* strings, it's D2, not D3.

### D4 — Dedup over-merged

**Implicate:** absorption `> 99.9%` with healthy raw+whitelist, and either
`dedup_merged_by_name` ≈ phone-less count (country-as-city collapse, 003 S1) or
`dedup_merged_by_phone` elevated on distinct businesses (`+961` defaulting of KSA
numbers, 055 S2). **Confirm:** count distinct `(name, city)` keys; if `city`
is uniformly `lebanon`/`saudi arabia`, the name key is a no-op. **Fix:** fix
`_extract_city` (`dedup.py:72-76`) and stop defaulting unknown country to 961
(`dedup.py:48`).

### D5 — Enrichment failed

**Implicate:** `websites_targeted ≈ baseline` but `live/dead/unknown` or the
contact counts collapse. **Confirm:** bucket the collapse — transport (`unknown`
spike), HTTP block (`dead` spike), or regex/body-read (`live` OK, contacts
collapse). **Fix:** proxy for the IP-block shape (073 S2); re-read the regexes /
`_MAX_BODY_BYTES` for the contact shape. **Watch out:** if `websites_targeted`
itself collapsed, the cause is upstream (Steps 1–4) — the enricher only fetches
what it is given (`enricher.py:223`).

### D6 — The export truncated

**Implicate:** actual CSV rows / sha256 / size disagree with the manifest, or the
Step-0 invariants fail. **Confirm:** `file_size` and `sha256` (`050` §6). **Fix:**
re-run the rclone upload; if invariants fail, discard the mixed generation and
re-commit from the staged run (050 §7). **Watch out:** `write_csv` is atomic
locally (`main.py:121-130`), so a clean local write + short Drive file points at
the upload, not the writer.

---

## CSV-only degraded mode (no manifest yet)

If only the five CSVs exist (today's reality), you can still check, in order:

1. **Step-0 invariants** (fully available): `with + without == all`,
   `qualified <= all`, `sales_ready <= all`, 23-column headers, no truncated last
   row. This catches D6 and mixed generations but nothing else.
2. **`scraped_at` as a net-new proxy.** `_merge` keeps `max(scraped_at)` per
   record (`dedup.py:95-96`), so "rows whose `scraped_at ≥` this week's run start"
   is a weak but real proxy for net-new (041 S3). A week where this count is ~0
   while the cumulative `all_businesses` count is unchanged points at Steps 1–4,
   but cannot tell them apart.
3. **`source` distribution shift.** A source that broke won't *remove* its old
   rows (the master is cumulative), but its share of `source` on new rows drops;
   with only the cumulative file you cannot isolate the marginal change, so this
   is indicative, not diagnostic.
4. **`website_live` distribution in `with_websites.csv`** (True/False/blank) and
   the contact-column fill rates → enrichment health proxy (D5), one run at a
   time.

**Bottom line:** CSV-only triage can reliably catch D6 (export) and, crudely,
D5 (enrichment) and "something collapsed upstream." It **cannot** distinguish
D1/D2/D3/D4 from each other because the per-source raw counts, whitelist
kept/dropped, and dedup merge counts are never written down (041 S1/S3).

---

## Findings

### S1 — The runbook's primary input does not exist; no run manifest or metric history is written

- **Where:** every count is a `print()` to ephemeral CI stdout — `main.py:165,170,
  174-175,181,219-242`; `osm.py:56`; `wikidata.py:59`; `google_places.py:225`;
  `enricher.py:227,263-267`.
- **Breaks:** Steps 1–6 need `osm_raw/wikidata_raw/google_raw`, `whitelist_kept/
  dropped`, `dedup_merged_by_*`, `net_new`, and `live/dead/unknown` compared
  against a baseline. None of these are persisted; CI stdout is discarded at job
  end. The full tree cannot execute, and "last week looked better" is unrecoverable
  by construction (041 S1).
- **Trigger:** any run — the comparison simply has no substrate to read.
- **Fix:** at the end of `main()` write one `data/run_history.csv` row per 041
  S1, named `.csv` so it rides the existing rclone `*.csv` include
  (`scrape.yml:38,50`), then load prior rows and compute the Step 1–5 deltas
  in-process.

### S2 — The per-stage numbers the tree keys on are never computed as first-class values

- **Where:** `main.py:179-181` collapses raw → filtered → dedup in place; the
  intermediate `raw_filtered`, `dropped`, `merged_by_phone`, `merged_by_name`,
  and `net_new` are transient locals or don't exist (`dedup.py:102-137` returns
  only the merged list).
- **Breaks:** `dedup()` returns only the final records (`dedup.py:137`), so the
  split between phone-merge and name-merge — the exact discriminator between D4
  over-merge and D1 source loss — is unrecoverable even with a manifest that only
  records `len(records)`. Same for whitelist `dropped` (`main.py:174-175` prints
  it, stores nothing).
- **Trigger:** any over-merge or whitelist collapse; you can see "fewer rows"
  but not *where* rows vanished.
- **Fix:** have `dedup()` also return `(records, merged_by_phone, merged_by_name)`
  and store them; store `raw_total/whitelist_kept/whitelist_dropped` alongside.

### S3 — A bad week is indistinguishable from a *finished* week unless the run is checked first

- **Where:** `osm.py:52-53`, `wikidata.py:55-56` (retries exhausted → bare
  `return`), `google_places.py:157-160,210-220` (missing key / 401 / errors →
  empty), all swallowed by `main.py:167-168` (`except Exception: print`).
- **Breaks:** a source that fails hard returns an empty iterator with no status
  flag and no non-zero exit; the run writes five valid CSVs, exits 0, and uploads
  over the master (`scrape.yml:46-50`). A triager who skips Step 0 and starts
  comparing row counts will chase D2/D3/D4 when the real answer is "the run never
  completed" (041 S2).
- **Trigger:** expired `GOOGLE_PLACES_API_KEY`, Overpass outage, network
  partition — all exit 0.
- **Fix:** give each scraper a tri-state `ok/empty/error` + raw count and fail
  the job (or skip upload) when a previously-non-zero source yields 0 (041 S2).

---

## Not a bug, but worth knowing

- **Google's ceiling makes D1 easy to miss.** A healthy Google sweep is capped at
  ~4,020 raw rows (67 queries × 60 max, 073 §"Why raw record count is decoupled"),
  so a Google collapse to 0 is the *entire* KSA source disappearing — treat any
  `google_raw == 0` as critical even if `all_businesses.csv` barely moves (the
  cumulative master masks it, 041 S3).
- **`net_new → 0` is eventually healthy.** Lebanon has ~50k businesses; once
  covered, a zero-net-new run with healthy raw volume is saturation, not a bug
  (041 S3). Key D4 only on the dedup merge counters, not on `net_new` alone.
- **The whitelist's substring fallback (`whitelist.py:122-128`) means D3 is
  usually partial, not total** — a whole-run wipe to near-zero `whitelist_kept`
  is more likely `category=None` (Wikidata's missing English `P31` label) than a
  single allowlist typo.
- **`website_live` tri-state is your best CSV-only enrichment signal.** Because
  the fetcher now keeps LIVE/DEAD/UNKNOWN distinct (`enricher.py:151-153`), a
  `dead`-spike reads as "IP block," not "market died" — but only if you actually
  bucket the column; a naive "percent with website" hides it.

## Recommended order of work

1. **S1** — Add the `data/run_history.csv` write to `main()` (`.csv` name so it
   rides the rclone path at `scrape.yml:38,50`). This is the one prerequisite
   that turns the CSV-only degraded path into the full tree.
2. **S3** — Make scrapers return `ok/empty/error` + raw count and exit non-zero
   (or skip upload) on a previously-non-zero source yielding 0, so Step 0 has a
   real completion signal to check.
3. **S2** — Persist `whitelist_kept/dropped`, `dedup_merged_by_phone/name`, and
   `net_new`; these are the discriminators for D3 and D4 and are currently
   computed-but-discarded.
4. Wire the Step 1–5 thresholds into a `scripts/check_run_health.py` post-step
   (041) so a BLOCK fails the job and pages someone, instead of waiting for a
   human to open the tree.
5. Only after 1–4, add the D2 field-fill-rate checks (Google `website`/`phone`/
   `rating` fill rates + category-vocabulary drift), which need the per-source
   row subsets the manifest does not yet capture.
