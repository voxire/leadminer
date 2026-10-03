# 050 — Run provenance and resumable publication

## Verdict

The current run is neither auditable nor safely restartable: it keeps no run identity, stage checkpoints, source snapshots, or output hashes, and writes the cumulative master in place before writing four derived CSVs. A timeout can leave a truncated master or a mixture of old and new outputs; a rerun repeats paid/network work and cannot distinguish a complete source result from a partial one. Treat each execution as an immutable, manifested run, checkpoint its stages, and publish all five CSVs as one validated generation while retaining the last committed master until commit.

## Findings

### S1 — In-place sequential writes can corrupt the master or expose mixed generations

- **Where:** `main.py:74-79,148-153`; `.github/workflows/scrape.yml:46-50`
- **Breaks:** `write_csv()` opens each destination with mode `"w"`, truncating it immediately, then writes directly to the final path. A process kill during `all_businesses.csv` can leave a corrupt/empty cumulative master. A kill after that write but before the remaining four completes leaves a new master paired with stale or missing subsets. The five outputs therefore have no shared commit boundary. The workflow also copies files individually to Drive root (`scrape.yml:50`), so a failure during that copy can expose a mixed generation there too. The timestamped copy is not a transaction either, and its directory name has only minute precision (`scrape.yml:48`).
- **Trigger:** The Actions 300-minute timeout (`scrape.yml:11`) fires while writing a large master, between any two output writes, or during Drive upload. On the next run, `load_master()` accepts the partial CSV if parseable (`main.py:46-71`), and that damaged state becomes the input to the next cumulative merge.
- **Fix:** Write all outputs to a unique run staging directory, flush/close and validate them, then publish an immutable generation and switch a single `CURRENT` manifest/pointer only after every object is present and hash-verified; never truncate the committed master in place. On Drive, upload under a unique run ID and write the commit marker/pointer last; consumers must read only the generation named by that committed pointer.

### S1 — No checkpoints or completion contract make timeout recovery unsafe and expensive

- **Where:** `main.py:93-129,148-153`; `main.py:105-113`; `scrapers/google_places.py:208-220`; `.github/workflows/scrape.yml:11,40-44`
- **Breaks:** Every invocation reloads the current CSV and restarts all source queries and website enrichment from scratch; there is no run ID, stage state, input fingerprint, or durable intermediate artifact. After timeout, a retry cannot tell whether a source completed, whether its results were already merged, or whether to reuse them. This repeats API requests and website GETs. Worse, `main()` logs and continues when a scraper future raises (`main.py:107-113`), while Google handles request errors by returning whatever records it has so far (`google_places.py:218-220`); the pipeline can therefore label a partial scrape as a successful full run and publish it as the cumulative master.
- **Trigger:** Google returns some pages for a query and then a request fails, or any scraper errors while another source succeeds. The run proceeds through enrichment and writes outputs with no completeness marker. If the job is killed later, a manual rerun repeats the uncheckpointed work; if it was not killed, partial source coverage is indistinguishable from a complete run.
- **Fix:** Persist stage artifacts/checkpoints by immutable `run_id`; make each stage idempotently produce a content-addressed artifact, record `pending/running/complete/failed` plus counts and hashes, and only allow publication when all required stages are complete (or an explicit, visible partial-run override is approved). Resume by verifying and reusing complete checkpoints, not by inferring success from the CSVs.

### S2 — There is no provenance sufficient to reproduce or cost-account for an output

- **Where:** `main.py:93-153`; `scrapers/osm.py:10-26,38-55`; `scrapers/wikidata.py:10-26,40-59`; `scrapers/google_places.py:29-42,45-124,157-188,190-279`; `.github/workflows/scrape.yml:17-26`
- **Breaks:** The CSV rows contain `scraped_at` and a source label, but no run identifier, exact input-master version, git revision, configuration/query snapshot, request accounting, stage counts, or output checksum. The code's queries are constants, but neither their text nor request/page outcomes are recorded; providers can change results between attempts, so a git SHA alone cannot recreate the fetched data. The workflow installs a requirements file without a lockfile or transitive hashes (`scrape.yml:22-26`; `BRIEF.md:41-45`). No API usage/cost is measured. It is impossible to tell whether a changed CSV reflects new source data, a code/config change, partial failure, or file damage.
- **Trigger:** Compare two same-named Drive master files after a rerun: no metadata establishes which code, query set, input master, or successful API responses produced either file, or whether a checksum changed because of content or partial upload.
- **Fix:** Emit a versioned `run_manifest.json` for every run, and retain immutable raw source responses or normalized per-source results as the replay inputs; record and verify hashes throughout.

## Manifest and replay contract

Store one manifest with each immutable run generation. Use a unique UUID (or UTC timestamp plus UUID; do not use minute-only timestamps) for `run_id`; include schema/manifest version, start/end UTC, final status, and parent committed `run_id`. At minimum, structure it as:

```json
{
  "manifest_version": 1,
  "run_id": "2026-10-03T12:34:56Z-<uuid>",
  "status": "committed",
  "started_at": "...Z",
  "finished_at": "...Z",
  "git": {"sha": "...", "dirty": false, "diff_sha256": null},
  "config": {"sha256": "...", "effective": {"...": "..."}},
  "parent": {"run_id": "...", "master_sha256": "..."},
  "queries": {
    "osm": {"endpoint": "...", "text": "exact OVERPASS_QUERY"},
    "wikidata": {"endpoint": "...", "text": "exact SPARQL_QUERY"},
    "google_places": {"endpoint": "...", "text": ["each exact query in execution order"]}
  },
  "stages": {
    "scrape": {"status": "complete", "counts": {"osm": 0, "wikidata": 0, "google_places": 0, "raw_total": 0}, "artifact": {"path": "...", "sha256": "..."}},
    "whitelist": {"status": "complete", "input": 0, "kept": 0, "dropped": 0},
    "merge_dedup": {"status": "complete", "master_input": 0, "fresh_input": 0, "output": 0, "new": 0, "updated": 0},
    "enrichment": {"status": "complete", "input": 0, "website_attempted": 0, "live": 0, "dead": 0, "unknown": 0},
    "scoring": {"status": "complete", "input": 0},
    "exports": {"status": "complete", "counts": {"all_businesses.csv": 0, "qualified_businesses.csv": 0, "with_websites.csv": 0, "without_websites.csv": 0, "sales_ready.csv": 0}}
  },
  "api_usage": {"google_places": {"requests": 0, "by_sku": {}, "estimated_cost_usd": 0}, "osm": {"requests": 0}, "wikidata": {"requests": 0}, "websites": {"requests": 0}},
  "outputs": {
    "all_businesses.csv": {"rows": 0, "sha256": "..."},
    "qualified_businesses.csv": {"rows": 0, "sha256": "..."},
    "with_websites.csv": {"rows": 0, "sha256": "..."},
    "without_websites.csv": {"rows": 0, "sha256": "..."},
    "sales_ready.csv": {"rows": 0, "sha256": "..."}
  }
}
```

- **Git and config:** Always record the checked-out git SHA and dirty flag; if a dirty tree is allowed, record a diff/source-tree hash and preserve the diff or reject the run. Build `config.sha256` from canonical JSON (sorted keys, normalized encoding) of the effective non-secret settings: query lists/text, endpoints, filters, field mask, concurrency/page limits, timeouts/retry behavior, output schema, and relevant scoring/enrichment settings. Include credential *presence/provider identity* only if useful; never write API keys, tokens, or secrets into the manifest. Record dependency versions or a lock/environment hash too, since installed package versions affect requests/parsing.
- **Exact queries:** Save the full actual query text and ordered Google text-query list, not only a hash; hash each query as an integrity aid. Record request/page counts and completion status per source/query so a failed page cannot look complete. Do not include page tokens or credentials in publicly exposed logs; tokens can be stored privately with the checkpoint only if needed to resume a provider pagination session.
- **Counts and cost:** Define counts as data rows (exclude CSV headers), and record each scraper's raw yield, whitelist kept/dropped, merge inputs/output and new/updated counts, enrichment attempted/outcome counts, and exact row count for each export. Count API attempts separately from successful responses/retries and record the applicable Google SKU/field-mask class. Report estimated cost using a versioned price table and label it as estimated; reconcile against provider billing data when available. OSM, Wikidata, and website requests still belong in usage counts even if they have no direct metered API charge. Capture costs/counters as requests happen so a kill does not erase accrued usage.
- **Output hashes:** After each staged CSV is closed, compute SHA-256 over its exact bytes and store it with its row count in the manifest. Verify all hashes and cross-file invariants (subsets/counts, CSV schema, master parseability) before commit. The manifest must list the five CSVs; do not attempt to embed its own hash inside itself. If desired, hash the manifest separately in the commit marker.
- **Reproducibility boundary:** A git SHA, query text, and config do not freeze external APIs or live websites. Persist each completed raw/normalized source response as an immutable run artifact and use it as replay input. Fix the run's `scraped_at` timestamp once at run start; use deterministic ordering/merge tie-breaking, since `main.py:124-129` processes fresh data before master and `dedup.py:83-99` selects values based on input order/field count. For an exact output rebuild, replay captured source data and, if website extraction must be reproduced, preserve the fetched body or its appropriately retained artifact. State clearly when a run is an online re-fetch rather than a deterministic replay.

## Safe resume and commit protocol

1. **Acquire a single-writer lease.** A workflow concurrency group helps prevent overlapping Actions runs, but use a storage-side lock/conditional generation check as well. Record the parent master hash in the run manifest. Before commit, verify the currently committed parent still matches; if another run committed meanwhile, abort/rebase and rerun the merge rather than overwriting newer cumulative state.
2. **Create a unique staging generation.** Keep the committed master read-only. Write run artifacts under `runs/<run_id>/staging/`, and mark the manifest `running`. Never use a shared temporary filename or let a retry reuse an ID with different inputs.
3. **Checkpoint at safe boundaries.** Persist source responses/results after each atomic unit: OSM and Wikidata response as a whole; Google per query and page (only mark a query complete after its final page); then filtered, deduped, enriched, scored, and export artifacts. Each checkpoint records its input hashes, output hash, counts, status, and implementation/config fingerprint. Write checkpoint files to a temporary name, flush/fsync, then atomic-rename locally. A checkpoint is reusable only if its recorded hash verifies and all dependency fingerprints match.
4. **Resume from the last verified complete checkpoint.** A timeout leaves the committed master unchanged and staging intact. On retry, load the manifest, verify checkpoint hashes and stage dependencies, skip verified complete stages, and resume the first incomplete unit. A corrupt/mismatched artifact is quarantined and recomputed; never assume that a file exists means the stage completed. For non-resumable API pagination, restart that query from page one and replace its query artifact atomically, rather than appending duplicate rows to an uncertain partial artifact.
5. **Do not silently publish partial sources.** Change the implicit error behavior into an explicit policy: required scraper/query failures mark the stage/run failed and block commit. If partial mode is intentionally supported, record exactly which sources/queries/pages failed, mark status `partial`, require an explicit operator choice, and merge against the intact parent master so absence from a failed source is never interpreted as deletion. The current code does not delete unseen rows, but it does rewrite the full table; a source-completeness gate still prevents silently publishing an unrepresentative refreshed generation.
6. **Build and validate all five files before publication.** Generate all CSVs from the verified staged master snapshot; write each temp file then rename into the staging generation. Parse them back, check schemas and subset relationships, count records, and compute SHA-256. The cumulative `all_businesses.csv` must be derived from the unchanged parent plus the fully accepted run data, never from a partially written previous attempt.
7. **Publish one generation atomically.** On a local filesystem, rename a complete generation directory into its immutable final name and atomically replace a small `CURRENT` pointer/manifest (same filesystem). On Drive, upload to a fresh unique generation path, verify remote sizes/checksums (download-and-hash if remote checksums are unsuitable), then upload a commit marker or update the pointer as the final operation. Consumers and the next workflow must resolve the current committed generation, not copy whichever root CSVs happen to be present. Keep at least the previous committed generation for rollback.

The atomic boundary is important on Google Drive: five object copies cannot be made transactionally. Immutable generation paths plus a final commit marker/pointer make uncommitted uploads harmless and make timeout recovery a verification/retry of the same generation rather than a rewrite of the live master.

## Not a bug, but worth knowing

- A rerun against live sources is not expected to produce byte-identical output: providers change, website contents change, and timestamps change. Define idempotency as “retrying/resuming one `run_id` does not double-apply data or replace committed state until all outputs commit,” and define reproducibility as replaying the captured inputs under the recorded code/config.
- `main.py:139-146` computes `with_social` but does not write a corresponding CSV; the manifest's required output list should reflect the five actual exports, not this transient count.
- The current output CSVs don't contain `run_id`. Either add a run identifier column in a deliberate schema migration or keep run provenance in a generation-level manifest and preserve the generation association when files are downloaded; avoid silently changing the 22-column contract as part of a storage-only rollout.

## Recommended order of work

1. **S1 — Protect state first:** acquire a single-writer lock; stage and validate all five outputs; publish immutable generation plus final commit pointer; fail closed when the parent master is missing or its hash changes.
2. **S1 — Add manifests/checkpoints:** record run/stage state and hashes; distinguish partial scraper results from complete results; retain per-source/query artifacts so timeouts can resume without refetching completed work.
3. **S2 — Add provenance and accounting:** git/config/dependency hashes, exact query texts, record counts, API request/SKU cost estimates, and SHA-256 for each output.
4. Add replay/idempotency tests for kill points (during master write, between each CSV, during generation upload, before pointer commit), stale/corrupt checkpoint rejection, duplicate resume, concurrent commit conflict, and all-subsets/master invariants.
