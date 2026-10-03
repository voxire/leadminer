# 100 — Run Manifest: the format that makes a run auditable

## Verdict

The pipeline can exit 0, write five CSVs, and upload them while silently
producing wrong or empty results — an invalid Google key, a timed-out SPARQL
query, or a swallowed master-CSV download all collapse output without raising
an exception. The only fix that scales is to emit a machine-readable **run
manifest** after every run and diff it against the previous manifest. This
document specifies that format in full, then lists the concrete code gaps that
currently prevent a truthful manifest from being emitted.

The single most important idea: the manifest must be **self-describing and
diffable** — every number that can drift (record counts, liveness ratios, API
requests, output hashes) is captured alongside the provenance (git SHA + config
hash) that *explains* the drift. Breakage detection is then a rule engine over
two manifests, not a human reading stdout.

---

## The manifest schema

The manifest is a single JSON file, `data/run-manifest.json` (versioned copies
in Drive as `run-manifest-<run_id>.json`). It is written *atomically* after the
CSVs (same `os.replace` pattern already in `write_csv`, `main.py:108-134`).

### Annotated example (valid JSON)

```json
{
  "manifest_version": 1,
  "run": {
    "run_id": "6f1f0c9e-3a2b-4c7d-9e0f-1a2b3c4d5e6f",
    "started_at": "2026-10-03T03:00:01Z",
    "finished_at": "2026-10-03T03:47:12Z",
    "duration_seconds": 2831,
    "status": "success",
    "trigger": "workflow_dispatch",
    "github_run_id": "11435821901",
    "github_attempt": 1,
    "python_version": "3.12.7"
  },

  "provenance": {
    "git_sha": "9c4e1f2d0b3a4c5d6e7f8a9b0c1d2e3f4a5b6c7d",
    "git_ref": "refs/heads/main",
    "git_dirty": false,
    "config_hash": "b2f0d1c3...",
    "config": {
      "requirements_lock_sha256": "…",
      "scraper_config_sha256": "…",
      "runtime_params_sha256": "…",
      "google_cost_rate_usd_per_1k": 32.00,
      "google_cost_sku": "Text Search Pro"
    }
  },

  "pipeline": {
    "stages": [
      {"name": "master_in",        "records": 48211},
      {"name": "scrape_raw",       "records": 2931,  "by_source": {"osm": 2104, "wikidata": 121, "google_places": 706}},
      {"name": "whitelist_filter", "in": 2931, "out": 2744, "dropped": 187, "dropped_by_reason": {"noise": 187, "no_category": 0}},
      {"name": "merge",            "combined": 50955, "in_new": 2744, "in_master": 48211},
      {"name": "dedup",            "in": 50955, "out": 50870, "collapsed": 85},
      {"name": "enrich",           "in": 50870, "out": 50870},
      {"name": "score",            "in": 50870, "out": 50870},
      {"name": "write",            "out": 50870}
    ],
    "totals": {
      "final_records": 50870,
      "net_new": 659,
      "qualified": 17430,
      "with_website": 9312,
      "without_website": 41558,
      "with_social": 12004,
      "sales_ready": 4310
    }
  },

  "sources": {
    "osm": {
      "scraped": 2104,
      "after_filter": 1988,
      "final": 1980,
      "new": 41,
      "websites_supplied": 331
    },
    "wikidata": {
      "scraped": 121,
      "after_filter": 118,
      "final": 116,
      "new": 3,
      "websites_supplied": 74
    },
    "google_places": {
      "scraped": 706,
      "after_filter": 638,
      "final": 630,
      "new": 615,
      "websites_supplied": 512
    }
  },

  "liveness": {
    "live": 6101,
    "dead": 1402,
    "unknown": 1809,
    "no_website": 41558,
    "checked": 9312,
    "new": {"live": 88, "dead": 12, "unknown": 39, "no_website": 520}
  },

  "api": {
    "requests": {
      "osm": 1,
      "osm_retries": 0,
      "wikidata": 1,
      "wikidata_retries": 0,
      "google_places_queries": 67,
      "google_places_pages": 24,
      "google_places_requests": 91,
      "google_places_429s": 0,
      "website_checks": 9312
    },
    "estimated_cost_usd": {
      "osm": 0.0,
      "wikidata": 0.0,
      "google_places": 2.91,
      "total": 2.91
    },
    "cost_note": "google_places = 91 requests × $32.00 / 1000 (Text Search Pro). Rate pinned in provenance.config."
  },

  "outputs": [
    {"file": "all_businesses.csv",        "rows": 50870, "bytes": 9241340, "sha256": "…", "canonical_hash": "…"},
    {"file": "qualified_businesses.csv",  "rows": 17430, "bytes": 3119042, "sha256": "…", "canonical_hash": "…"},
    {"file": "with_websites.csv",         "rows": 9312,  "bytes": 1900117, "sha256": "…", "canonical_hash": "…"},
    {"file": "without_websites.csv",      "rows": 41558, "bytes": 6623905, "sha256": "…", "canonical_hash": "…"},
    {"file": "sales_ready.csv",           "rows": 4310,  "bytes": 870413,  "sha256": "…", "canonical_hash": "…"}
  ],

  "warnings": [
    {"code": "UNKNOWN_RATIO_HIGH", "message": "19.4% of checked websites are UNKNOWN (unreachable), above the 15% threshold."},
    {"code": "WIKIDATA_LOW_YIELD", "message": "wikidata yielded 121 records vs 7-run median 158."}
  ],

  "errors": []
}
```

### Field reference

| Field | Required | Meaning / how it is produced |
|---|---|---|
| `run.run_id` | yes | UUIDv4 generated at process start; joins manifest → Drive object → logs. |
| `run.status` | yes | `success` \| `partial` (a scraper yielded 0 but run completed) \| `failed`. |
| `provenance.git_sha` | yes | Full 40-char SHA from `git rev-parse HEAD`. |
| `provenance.git_dirty` | yes | `git status --porcelain` non-empty. A dirty tree means the SHA is a lie. |
| `provenance.config_hash` | yes | Compound hash of everything that can change output; see below. |
| `pipeline.stages` | yes | One entry per stage in `main.py`; carries `in`/`out`/delta. |
| `sources.*` | yes | Per-scraper counts, *including pre-filter and post-dedup* views. |
| `liveness` | yes | Four-way split: `live`, `dead`, `unknown`, `no_website`. |
| `api.requests` | yes | Measured, not assumed (see S3). |
| `outputs[].sha256` | yes | Hash of exact bytes on disk (integrity). |
| `outputs[].canonical_hash` | yes | Order-independent hash of record content (cross-run diff). |
| `warnings[]` | yes | Machine-readable, coded warnings with thresholds. |

---

## What each field is for

### `config_hash` — the "why did output change" key

Two runs with the **same** `config_hash` should, on identical upstream data,
produce identical output. Any output change with an unchanged `config_hash` is
therefore *external world* change (new businesses, closed sites) or breakage —
never a code/config change. This is the crux of silent-breakage detection.

`config_hash = sha256(canonical_json(`
- `git_sha` (40-char) and `git_dirty`
- `requirements` resolved to a full lockfile (top-level + transitives). Today
  `requirements.txt:1-3` pins three packages but **no transitives**
  (`requests` pulls `urllib3`, `certifi`, `idna`, `charset-normalizer`;
  `beautifulsoup4` pulls `soupsieve`; `lxml` is a binary wheel). A transitive
  bump that changes TLS behavior or parsing changes output with the same SHA.
- scraper config: the three query lists (`LEBANON_QUERIES`, `KSA_QUERIES`,
  `google_places.py:45-124`), the whitelist sets (`whitelist.py:17-102`), the
  region boxes (`enricher.py:58-76`), the Overpass/SPARQL query strings, and
  the `LIMIT 5000` cap.
- runtime params: `_WORKERS`, enricher `workers=40`, `timeout=8`, retry counts,
  `_MAX_BODY_BYTES`, `_COUNTRY_CODES`.
- the Google cost rate + SKU (see below).
`)`

### `sources.*` — three views of each scraper, and why all three are needed

- **`scraped`** — records yielded by the scraper *before* the whitelist filter
  (`main.py:154-166`). This is the only number that reflects the upstream data
  source, before any of our own filtering can hide a collapse.
- **`after_filter`** — what survives `is_business_category` (`main.py:172`).
  A whitelist regression shows up here as a sudden `dropped` spike.
- **`final` / `new`** — how many final records carry this source (including
  pipe-joined multi-source records) and how many are new this run.

`websites_supplied` is captured **at scrape time**, before merge, because after
merge the website URL may have come from a different source than the name (see
S2). It is the denominator for per-source liveness attribution.

### `liveness` — the four-way split, not the three-way one

`main.py:215-217` prints live/dead/unknown only over `with_websites`, and the
word "unknown" is doing double duty. The manifest splits into four disjoint
buckets:

- **`live`** — `website_live is True` (server answered 2xx; `enricher.py:214`).
- **`dead`** — `website_live is False` (server answered 4xx/5xx; a rebuild
  pitch; `enricher.py:176`).
- **`unknown`** — had a website but no verdict (DNS/TLS/timeout/blocked;
  `enricher.py:169-170`). Explicitly **not** a pitch signal.
- **`no_website`** — no `website` field at all; never checked.

`dead` and `unknown` are the two buckets that break silently and in opposite
directions, so they must never be summed into one number.

### `api.requests` and `estimated_cost_usd`

Request counts are measured per scraper. Google Places is the only paid API:

- The `FIELD_MASK` (`google_places.py:31-42`) requests `places.displayName`,
  `places.websiteUri`, `places.nationalPhoneNumber`, `places.rating`, etc. —
  **Pro-tier fields**, so it bills at the **Text Search Pro** SKU (≈ **$32.00
  per 1,000 requests**, 2024–2025 pricing; Google reprices frequently).
- Requests = queries + pagination. There are **67 queries** in code
  (33 Lebanon + 34 KSA) — the audit brief's "68" is already stale, which is
  exactly why the manifest must *measure* this rather than assume it.
- Cost is stored as `requests × rate`, with the rate pinned in
  `provenance.config` so historical manifests stay recomputable after a price
  change. OSM and Wikidata are free but their request/retry counts are still
  recorded (rate-limit abuse and silent 0-yield both show up there).

### `outputs[].sha256` vs `outputs[].canonical_hash`

- **`sha256`** — hash of the exact bytes on disk (post-BOM; `write_csv` writes
  `utf-8-sig`, `main.py:124`). Detects truncation, encoding drift, column
  reorder, and any corruption within a run.
- **`canonical_hash`** — records are serialized in a canonical form (sorted
  keys, normalized values), sorted, and hashed. This is **order-independent**,
  which matters because output row order is *not deterministic*: `raw` is
  extended in `as_completed` order (`main.py:162-166`), which feeds `dedup`'s
  dict insertion order (`dedup.py:102-137`). Two identical runs can emit the
  same records in different orders, so the byte hash alone would false-positive
  on every run. Cross-run diffing uses `canonical_hash`; `sha256` is for
  integrity.

---

## Findings — code gaps that block a truthful manifest

### S1 — No stable record identity, so "new" and deltas are arithmetic, not set-based

- **Where:** `dedup.py:102-137` (key = normalized phone, else `(name, city)`),
  and the absence of any persisted ID across the whole pipeline.
- **Breaks:** "per-source **new** LIVE/DEAD/UNKNOWN counts" cannot be computed
  as set differences. The dedup key is recomputed from raw fields each run; if
  `normalize_phone` or `normalize_name` changes, the keys change and every
  record looks "new". Net-new today would be `final - master_in`, which hides
  records that *left* the master (business closed, whitelist dropped it) behind
  records that *joined*. A run that loses 1,000 and gains 1,000 looks identical
  by net count.
- **Trigger:** change `_MIN_DIGITS` in `dedup.py:30` from 7 to 8. Phones of
  7 digits now fall through to name-keyed dedup, merging businesses that were
  previously distinct, and the "new" count silently swings.
- **Fix:** persist a stable record id per row in `all_businesses.csv` (a
  content hash of the dedup key, or a monotonic id assigned on first insert)
  and define `new` as `id ∉ previous manifest id-set`. Ship the prior
  manifest's id-set forward into the diff step.

### S1 — "per-source LIVE/DEAD/UNKNOWN" is ill-defined after merge

- **Where:** `dedup.py:92-94` merges `source` into a sorted pipe-joined set;
  `website_live` is assigned once during enrich (`enricher.py:242`) *after*
  merge.
- **Breaks:** a merged record may have `source: "osm|google_places"` and a
  website URL that came from *one* of them, but there is no record of which
  source supplied the URL. Any per-source liveness bucket you compute from the
  final record is an attribution guess, not a fact.
- **Trigger:** OSM supplies the name+website for "Al Basha", Google supplies
  only the name. Merged, the record is `source: "google_places|osm"` and
  liveness is charged to *both* sources, inflating Google's dead-site count.
- **Fix:** capture `websites_supplied` and the source that supplied each URL
  **at scrape time** (per scraper, pre-merge), and attribute the liveness
  verdict to that source. Keep multi-source records in a separate
  `source: "multi"` bucket for the *count* totals rather than double-counting.

### S2 — Request counts and cost are printed to stdout, never measured into a structure

- **Where:** `main.py:163-226` prints counts; no counter exists for HTTP
  requests anywhere. `google_places.py:203-278` paginates but does not count
  requests or pages; `osm.py:38-53` and `wikidata.py:40-56` retry but do not
  count attempts.
- **Breaks:** the manifest's `api` block (and the entire cost model) has no
  source of truth. Worse, cost *spikes* and *silent-0* are both invisible:
  Google 429s loop forever within one query (`google_places.py:213-216`
  `continue`s the `while` loop with the same body and no page-token advance),
  which can burn unbounded requests on a single query with no error.
- **Trigger:** a regional 429 storm makes one query retry 40× in 30s sleeps;
  duration balloons, cost balloons, and nothing in the output marks it.
- **Fix:** each scraper returns a small `(records, stats)` pair —
  `{requests, retries, pages, http_429, bytes}` — accumulated in `main.py` and
  written to `api.requests`. Multiply by the pinned rate for `estimated_cost_usd`.

### S2 — No content hash per output file

- **Where:** `write_csv` (`main.py:108-134`) flushes and fsyncs but computes no
  hash; the workflow uploads CSVs with no checksum (`scrape.yml:46-50`).
- **Breaks:** two runs with identical row counts but different bytes — a
  truncated final line, a BOM dropped, a column reordered, a partial overwrite
  that `os.replace` did not cover — are indistinguishable by count alone. Row
  count is the *only* signal the current system emits about the product.
- **Trigger:** a future edit reorders `FIELDS` (`main.py:33-40`) without
  changing record content; every downstream consumer silently reads columns in
  the wrong order while all five row counts stay identical.
- **Fix:** in `write_csv`, hash the exact bytes written and also compute the
  order-independent `canonical_hash`; emit both into `outputs[]`.

### S2 — `config_hash` has no lockfile to hash against

- **Where:** `requirements.txt:1-3` (three top-level pins, no hashes, no
  transitives); `scrape.yml:23-25` installs with `uv pip install --system -r
  requirements.txt` (no lockfile).
- **Breaks:** dependency resolution is non-reproducible across time. A
  transitive bump (e.g. `urllib3`) that changes TLS behavior changes liveness
  results with the same `git_sha`, and the manifest cannot tell "the code
  changed" from "the world changed".
- **Trigger:** `requests 2.32.3` stays pinned but `urllib3` releases a breaking
  fix; `website_live` flips a cohort of sites from UNKNOWN to DEAD and the
  manifest has no `config_hash` delta to explain it.
- **Fix:** commit a lockfile (`uv lock` / `pip freeze --all` with hashes) and
  hash it into `config_hash`. Pin `python-version` to a patch, not `3.12`.

### S3 — `no_website` vs `unknown` are conflated in the printed summary

- **Where:** `main.py:215-217` computes `live/dead/unknown` only over
  `with_websites`; `enricher.py:258` counts `unknown` only where `website` is
  present. There is no explicit "no website" bucket in any output.
- **Breaks:** the product's two pitch audiences — "rebuild" (dead) and
  "build-new" (no website) — are the two most important counts, and today
  neither is emitted as a distinct, machine-readable number.
- **Fix:** emit the four-way `liveness` split above; it is a superset of the
  current summary and costs nothing.

### S3 — The workflow neither downloads the prior manifest nor uploads the new one; download failure is swallowed

- **Where:** `scrape.yml:35-38` (`rclone copy … || true`) and `scrape.yml:46-50`
  (uploads CSVs only).
- **Breaks:** (a) the cross-run diff step needs the *previous* manifest, which
  is never fetched; (b) `|| true` masks a failed master download, so a clean
  runner silently starts from `master_in = 0` and **recreates the master from
  one run**, resetting years of accumulated data with no error.
- **Trigger:** the runner's ephemeral disk is empty on a fresh machine; the
  `rclone copy` fails (auth transient), `|| true` swallows it, `load_master`
  returns `[]` (`main.py:79-80`), and `all_businesses.csv` is rewritten from
  ~48k rows to ~2.7k rows.
- **Fix:** download `run-manifest.json` alongside the CSVs; if the prior
  manifest reports `final_records > 0` but `master_in == 0`, raise `status:
  failed` with a `MASTER_RESET` warning. Upload `run-manifest-<run_id>.json`
  with the CSVs.

---

## Cross-run comparison — the only reliable breakage detector

Silent breakage means: **exit code 0, CSVs written, no exception**, yet the
output is wrong. The scrapers are specifically built to not throw — a bad API
key prints and returns empty (`google_places.py:211-212`), a missing key just
skips (`google_places.py:158-160`), retries exhaust and return
(`osm.py:52-53`, `wikidata.py:55-56`), and `main.py:167-168` catches exceptions
per scraper and carries on. None of these produce a non-zero exit.

Two facts make row counts an unreliable alarm on their own: (1) scrapers hit
live data, so counts *should* drift every run; (2) a broken run can look like a
quiet run — zero Google records is also a "Monday with no new Google results"
unless you compare against the 7-run history. The manifest turns "is this drift
or breakage" into a mechanical question.

### The silent-failure catalog

| Failure | Where it happens | Manifest signal |
|---|---|---|
| Google API key invalid | `google_places.py:211` returns empty | `sources.google_places.scraped` collapses to 0, `config_hash` unchanged |
| Google API key unset | `google_places.py:159` skips | `sources.google_places` block absent / 0, no warning raised |
| Google 429 storm (infinite retry) | `google_places.py:213-216` `continue`s without page-token advance | `api.google_places_requests` and `duration` spike vs prior run |
| Wikidata SPARQL timeout | `wikidata.py:40-56` retries then returns | `sources.wikidata.scraped` collapses below 7-run band |
| Overpass tag/query change | `osm.py:10-26` | `sources.osm.scraped` drops, `websites_supplied` drops |
| Whitelist regression | `whitelist.py:105-130` | `pipeline.whitelist_filter.dropped` spikes, `config_hash` *changed* (explained) |
| Crawler blocked (Cloudflare everywhere) | `enricher.py:169-170` | `liveness.unknown` → ~100%, `liveness.dead` → ~0% (not "all sites died" — the crawler went blind) |
| Per-record enrich crash | `enricher.py:236-240` swallows → UNKNOWN | same UNKNOWN spike, no `errors[]` entry |
| Master download failed | `scrape.yml:38` `|| true` | `pipeline.master_in == 0` while prior manifest `final_records > 0` |
| Output corruption / column drift | `write_csv` / `FIELDS` reorder | `outputs[].sha256` differs while `canonical_hash` matches (reorder) or both differ (corruption) |
| Dedup/normalization change | `dedup.py:33-63` | `pipeline.dedup.collapsed` and `net_new` jump, `config_hash` *changed* (explained) |

### Invariants to check on every pair of manifests

1. **`config_hash` unchanged + `sources.*.scraped` collapsed → breakage.** The
   highest-signal rule: a source silently dying without a code change.
2. **`liveness.unknown` ratio > threshold (e.g. 30%) → crawler health alarm,**
   independent of record counts. Blocked-crawler is the most common silent
   failure and the most damaging to the lead signal.
3. **`pipeline.master_in == 0` while prior `final_records > 0` → MASTER_RESET.**
4. **`sources.*.scraped` outside the rolling 7-run band → warn.** Cheap drift
   detection for upstream API changes.
5. **`outputs[].canonical_hash` unchanged but `totals.final_records` changed →
   data replaced, not added** (same identities, new content). `sha256` changed
   but `canonical_hash` unchanged → row-order-only difference (expected, ignore).
6. **`estimated_cost_usd` vs prior run:** a cost spike with unchanged
   `config_hash` and unchanged query count means pagination/429 runaway.

Each rule is a pure function of two JSON files. That is the entire point: the
manifest is the only artifact that makes "did this run silently break" a
decidable question, because the code, by design, will not tell you itself.

---

## Recommended order of work

1. Add a `manifest.py` that owns the schema, `config_hash` computation, and the
   atomic write (reuse the `write_csv` temp+`os.replace` pattern).
2. Instrument the three scrapers to return `(records, request_stats)` so
   `api.requests` is measured (S2). This is the smallest change that unlocks
   cost and runaway detection.
3. Add `sha256` + `canonical_hash` to `write_csv` (S2) and emit `outputs[]`.
4. Capture `websites_supplied` and URL-supplier at scrape time; define
   `liveness` as the four-way split (S1/S3).
5. Persist a stable record id and define `new` as a set difference against the
   prior manifest id-set (S1).
6. Pin a lockfile and hash it into `config_hash` (S2).
7. Update `scrape.yml`: download the prior manifest, upload the new one, and
   hard-fail on `MASTER_RESET` instead of `|| true` (S3).
8. Add a `diff_manifests.py` rule engine implementing the six invariants above,
   run at the end of the workflow and failing or warning accordingly.
