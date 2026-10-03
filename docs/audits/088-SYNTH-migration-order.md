# 088 — Migration plan: the order to modernise leadminer

## Verdict

Modernise **safety-first, then observability, then behaviour**, and only **after** a
characterization test net exists: every refactor after Step 4 is gated by a golden
CSV/checksum diff that proves the five outputs did not change. The single most
important ordering rule is that **the workflow and its backups are fixed before any
pipeline logic is touched** — today a swallowed Drive download (`scrape.yml:38`) or two
overlapping runs (`scrape.yml:3-10`) can silently replace the cumulative master with a
partial scrape, and that is the one failure that is both unrecoverable and invisible.

The plan is 13 steps across six phases. Steps 5–7 are mutually independent and can be
done in parallel; Steps 8 and 9 can also run in parallel once 6 and 7 are done. Steps 12
and 13 are the only changes to the persistence layer, and they are last on purpose.

> **Baseline note.** The brief (`docs/audits/BRIEF.md`) describes a 1,402-LOC, zero-test,
> zero-CLI codebase, but the tree has already moved past that snapshot: `cli.py`,
> `pyproject.toml`, and `tests/test_lead_signal.py` exist. This plan treats those as
> *present but incomplete* — Steps 4 and 10 are therefore "finish and gate", not "build
> from zero". Everything is anchored to the source as it reads today.

---

## Ordering principles (why this order)

1. **The data outlives the code.** The cumulative master `data/all_businesses.csv` is
   the only revenue input and it is gitignored (`README.md:5` claims otherwise; `042`
   confirms no commit step exists). Any step that risks it must come after the steps
   that make it recoverable.
2. **You cannot refactor safely what you cannot diff.** A refactor's "it still works" is
   only credible if you can byte-compare this run's outputs to the last good run. That
   needs a manifest + checksums (Step 3) and golden tests (Step 4) before any structural
   change (Steps 8–9).
3. **Fix correctness at the seam, not the whole layer.** The two worst latent bugs — the
   `google_places → enricher` inverted import (`google_places.py:26`) and the
   score-before-priority ordering (`main.py:195-197`, `enricher.py:341-342`) — are
   one-line/small fixes. They are cheap and shippable long before the full scraper-layer
   and pipeline refactors that audit `045`/`044` propose.
4. **SQLite is last, not first.** `033`/`042` make a compelling case for SQLite, but it
   is the only step that changes what "the master" *is*, and it must be dual-run against
   the CSV until reconciled. It buys the least *per unit of risk* relative to the
   fail-closed workflow and observability steps, so it goes last.

---

## The sequence

Each step is **independently shippable**: after landing it, `python main.py`
(`leadminer run`) still produces the five CSVs and uploads them. "Verify" is the
specific proof that holds for that step.

---

## Phase 1 — Stabilise the data-bearing run (workflow only, no Python)

### Step 1 — Make the workflow fail-closed and serialise runs

- **What changes:** `.github/workflows/scrape.yml` only.
  - Remove `|| true` from the master download so a failed Drive read blocks the run
    instead of starting from `master=[]` (`main.py:80,150`) and overwriting the master
    (`scrape.yml:38`). Per `039` S1 and `074` S1.
  - Add `concurrency: {group: leadminer-scrape-prod, cancel-in-progress: false}` so two
    manual dispatches cannot download the same snapshot and clobber each other
    (`039` S1, `033` S2).
  - Declare `permissions: contents: read` (`039` S2).
  - Add a pre-upload volume guard: fail if `all_businesses.csv` row count drops >10%
    vs the downloaded master without an explicit override (`074` §4, `verify_volume`).
- **Depends on:** nothing. No Python changes, no new secrets.
- **What could break:** a *legitimate* first run (no master in Drive) now fails the
  download check. Handle with an explicit `BOOTSTRAP=1` dispatch input rather than
  re-swallowing the error. Nothing else changes behaviour.
- **How to verify:** (a) dispatch with a deliberately bad `RCLONE_CONFIG` — job must
  fail *before* the scrape step and must not upload; (b) dispatch two runs back-to-back —
  the second must queue, not overlap; (c) a normal dispatch still produces and uploads
  five CSVs.
- **Time:** half a day.

### Step 2 — Give the master real backups and bounded retention

- **What changes:** workflow + a small `tools/` script. Adopt `074` Phase 1–2: upload
  `all_businesses.csv.gz` (not five uncompressed files) into `gdrive:leads/snapshots/
  <timestamp>/`, write a `manifest.json` (SHA-256 + row count + schema + git SHA), add
  `--backup-dir gdrive:leads/prev_master/` on the root upload, and run a GFS prune
  (14 daily / 8 weekly / 12 monthly / 3 annual) after upload. The four derived CSVs
  stay in the root only (`074` S2).
- **Depends on:** Step 1 (fail-closed download, so a snapshot always exists to restore).
- **What could break:** the prune script is destructive; a bug deletes snapshots. Ship it
  with `--dry-run` default and require `--apply`. rclone `--backup-dir` changes the
  upload from "overwrite" to "move-old-away" — verify it does not break the root copy.
- **How to verify:** run once, confirm (a) root has the 5 CSVs, (b) `snapshots/<ts>/`
  holds `all_businesses.csv.gz` + `manifest.json`, (c) `prev_master/` holds the prior
  root, (d) `prune --dry-run` lists and keeps the expected set.
- **Time:** 1–2 days.

---

## Phase 2 — Observability and a characterization net (the safety net)

### Step 3 — Persist a run manifest + `run_history.csv`, fail on collapse

- **What changes:** `main.py` (end of `main()`) writes one `data/run_history.csv` row
  with the funnel counters that are currently `print()`-and-discarded: per-source raw
  counts, `raw_filtered`, `whitelist_dropped`, `net_new`, `dedup` merge split,
  `sites_fetched/live/dead/unknown`, contact counts, `qualified`, `sales_ready`
  (`041` S1, `040` §6). Add a `scripts/check_run_health.py` post-step that compares
  against the median-of-last-3 and BLOCKs on C1/C2 (`041` table). Name it `.csv` so it
  rides the existing rclone `*.csv` include (`scrape.yml:38,50`) — no workflow filter
  change needed.
- **Depends on:** Step 1 (so a BLOCK actually gates the upload) and Step 2 (so a
  blocked run doesn't corrupt the master).
- **What could break:** the manifest columns change the shape of `data/` on Drive.
  Mitigate: append-only, never rewrite history, and ignore unknown columns on read.
  A false-positive BLOCK on a legitimately saturating market (net-new → 0) must not
  kill the run — C2 (raw volume) is the primary signal, C1 secondary (`041` caveats).
- **How to verify:** two real runs; diff the two `run_history.csv` rows and confirm the
  manifest checksums match `sha256sum` of the CSVs. Simulate a collapsed Google source
  (empty key) and confirm the job BLOCKs instead of uploading.
- **Time:** ~1 day.

### Step 4 — Lock the dependencies and pin current behaviour with tests

- **What changes:** generate and commit a hash-pinned lockfile (from `pyproject.toml`
  deps; `075` F1 — the reproducibility fix is the lock, not Docker). Expand the test
  net from the existing `tests/test_lead_signal.py` to golden/characterization tests
  that assert current *outputs*: `normalize_phone`/`dedup` invariants, `lead_score`
  weights, `recommend_service` decision table, `load_master`/`write_csv` round-trips,
  and an offline end-to-end `main()` with faked scrapers/checker that asserts the five
  output sets (`031` catalogue). Add a minimal `ci.yml` running lint + unit on pure
  modules (no `mypy --strict` yet — that gate is Step 10, after typing).
- **Depends on:** Step 3 (golden baselines to assert against).
- **What could break:** golden tests that over-fit a bug will fight the *intentional*
  corrections in Steps 5–7. Mark the two known-wrong behaviours (dead-site scoring,
  phone cross-country prefix) as `@pytest.mark.xfail` with a comment pointing at the
  fixing step, so the suite is green on current code but flags regressions.
- **How to verify:** `pytest` green with zero network; `python -m unittest discover -s
  tests` (the stdlib path in `tests/test_lead_signal.py:6`) green; `uv pip install
  --require-hashes -r <lock>` installs reproducibly.
- **Time:** 2–3 days.

---

## Phase 3 — Low-risk correctness fixes (mutually independent, parallelisable)

### Step 5 — Python 3.12 hygiene, SSRF/TLS, and bounded concurrency

- **What changes:** replace `datetime.datetime.utcnow()` with
  `datetime.now(datetime.UTC).isoformat().replace("+00:00","Z")` in `osm.py:31`,
  `wikidata.py:31`, `google_places.py:162` (preserves lexicographic `max()` in
  `dedup.py:96`; `065` S2). Drop the unused `from urllib.parse import urljoin, urlparse`
  (`enricher.py:4`; `065` S3). Remove `verify=False` and
  `urllib3.disable_warnings(...)` (`enricher.py:167,319`) — the SSRF/TLS fix from
  `038` S3 / `008`. Bound in-flight futures with `itertools.batched` in
  `enricher.py:229-234` and `google_places.py:183-186` (`065` S2).
- **Depends on:** Step 4 (tests to prove `verify=True` and chunking don't change liveness
  outcomes).
- **What could break:** `verify=True` can turn some previously-"live" sites into
  TLS-error "unknown" if they have broken certs — this is *correct* but changes
  `website_live` distribution; flag it in the run manifest and expect a one-off
  dead/unknown shift. `itertools.batched` reorders completion logging only, not output.
- **How to verify:** golden test asserts `normalize_phone`/`lead_score` unchanged; a
  fixture `_fetch_website` test with a recorded HTTPS 200 confirms no behavioural
  regression; manual `leadminer scrape --source osm` smoke.
- **Time:** ~1 day.

### Step 6 — Sever the inverted import and fix score ownership

- **What changes:** delete `from enricher import infer_region` in `google_places.py:26`
  and stop calling it in `_scrape_query` (`google_places.py:243`); let `enricher.enrich`
  (`enricher.py:322-327`) infer region for Google rows too, so every source funnels
  through one path (`045` S1-2). Then make scoring single-owner and correctly ordered:
  `enrich()` stops assigning `lead_score` (`enricher.py:341-342`), and `main()` assigns
  `industry_priority` → `recommended_service` → `lead_score` exactly once
  (`044` S1; `main.py:189-197`).
- **Depends on:** Step 4 (a test that pins the corrected `lead_score` for a
  high-priority record), Step 5 (region inference is now consistent).
- **What could break:** Google records currently get `region` at ingest; after this they
  get it in enrich. The region *value* is identical (`infer_region` is the same
  function), but if `enrich` runs after a whitelist/dedup that depended on region,
  ordering matters — confirm nothing downstream of ingest reads `region` (it does not;
  `dedup` keys on phone/name-city, `dedup.py:102-137`). The score-ownership change
  *intentionally* changes `lead_score` for records whose priority was unset at enrich
  time — capture that delta in the golden diff and expect exactly that delta, nothing
  more.
- **How to verify:** `python -c "import scrapers.google_places"` under an installed
  wheel (no `enricher` import) succeeds; `cli.py` `leadminer score` on an existing CSV
  shows only the expected score moves; golden end-to-end diff shows the same five set
  sizes.
- **Time:** ~1 day.

### Step 7 — Harden the pure modules against malformed input

- **What changes:** type/None-safety in the pure functions that crash on non-string or
  absent fields: `normalize_phone`/`normalize_name`/`_merge` (`dedup.py:33-99`),
  `is_business_category`/`industry_priority` (`whitelist.py:105-145`),
  `recommend_service`/`_is_low_rating` (`pitch_recommender.py:25-140`). Targets the S1
  catalogue in `031` §2–3 (§5–§12 of the "top 30").
- **Depends on:** Step 4 (property tests via `hypothesis`, already in dev deps
  `pyproject.toml:26`).
- **What could break:** nothing in the happy path — these are guards for inputs that
  today crash the run. The one risk is making a guard *too* permissive and letting junk
  through to dedup keys; keep the `normalize_phone` ""-return invariant
  (`dedup.py:33-47`, `tests/test_lead_signal.py:102-108`).
- **How to verify:** `hypothesis` property tests green (idempotency, non-expansion,
  source-conservation from `031` §properties); existing stdlib tests still green.
- **Time:** 1–2 days.

---

## Phase 4 — Structural refactor (behaviour-preserving, behind golden tests)

### Step 8 — Refactor `main()` into a typed, timed pipeline

- **What changes:** extract the `main()` body into explicit stages with typed
  boundaries and a timed runner, per `044`: Acquire → Whitelist → Dedup → Enrich →
  Classify/Score → Partition → Export → Report. `main()` becomes a thin composition
  root. Keep `dedup.py`, `enricher.py`, `pitch_recommender.py`, and `scrapers/` as
  domain modules called through narrow adapters — this step does **not** rewrite their
  internals.
- **Depends on:** Step 6 (score ownership is already correct so the stage split doesn't
  re-introduce double-scoring) and Step 7 (typed boundaries survive malformed input).
- **What could break:** the highest-risk refactor in the plan. Mitigate by (a) keeping
  CSV names/columns/filter predicates byte-identical, (b) running the golden end-to-end
  test from Step 4 against the refactored code and requiring zero diff, (c) landing it
  behind the `run_history.csv` manifest so any regression trips C1/C2.
- **How to verify:** golden fixture diff is empty except the (already landed) score
  correction; a `scripts/check_run_health.py` BLOCK fires on a deliberately broken
  stage; stage timings appear in the manifest.
- **Time:** 2–3 days.

### Step 9 — Refactor the scraper layer: registry, shared HTTP, validated records

- **What changes:** implement `045`'s design — a `ScraperRegistry` (`@register_scraper`),
  a `ScraperHttpClient` with per-host token-bucket + jittered backoff + `Retry-After`,
  and a validated `RawBusinessRecord` that strips downstream fields (`lead_score`,
  `completeness_score`, …) out of ingestion. Migrate the three scrapers to it and switch
  `main.py` (now the Step 8 runner) from the hardcoded list to `auto_discover()`.
- **Depends on:** Step 6 (inverted import already severed) and Step 8 (the typed
  boundary the raw records feed into). Can run in parallel with Step 8 only if the
  record-contract change is coordinated first; sequential-after-8 is lower-risk.
- **What could break:** the scraper rewrite is the other high-risk step. The retry/HTTP
  changes alter *timing*, not records, but a validation too strict (e.g. rejecting a
  real Overpass row) silently drops records. Mitigate: `record_mode=none` cassette tests
  against recorded Overpass/Wikidata/Places payloads (`031` §4, `038` §5), and assert
  raw per-source counts in `run_history.csv` stay within C2 tolerances.
- **How to verify:** `leadminer scrape --source <osm|wikidata|google_places>` smoke on
  each; cassette integration tests green with `--block-network`; a full run's raw
  counts match baseline within WARN.
- **Time:** 3–5 days.

---

## Phase 5 — Packaging, gates, and ops maturity

### Step 10 — Turn on the full CI/CD quality gates

- **What changes:** finish `038`'s `ci.yml`: `mypy --strict` (now passes because Steps
  8–9 produced typed boundaries), `ruff` lint/format, `pytest --cov` with the 90%/80%
  thresholds, `pip-audit` on the lockfile, and a `uv build` + wheel-import gate. Add
  Dependabot (`038` §not-a-bug). This is the moment `pyproject.toml`'s `[tool.mypy]
  strict = true` (`pyproject.toml:64-68`) actually runs in CI.
- **Depends on:** Step 4 (lockfile + tests), Step 8 and 9 (typing).
- **What could break:** only PRs. The gate is additive to the repo, not to the scrape
  job — `scrape.yml` is untouched, so the revenue pipeline is unaffected by a red gate.
- **How to verify:** a clean PR passes all jobs; a deliberately untyped change fails
  `mypy --strict`; `uv build` emits a wheel that imports.
- **Time:** ~2 days.

### Step 11 — 3-2-1 backup, restore workflow, and (optional) container

- **What changes:** complete `074` Phase 3 — mirror `snapshots/` to an S3/R2 bucket with
  Object Lock (WORM), switch Drive auth to a service account, and ship
  `.github/workflows/restore.yml` with pre-flight checksum/schema verification
  (`074` §restore). Separately, and only if local-dev/CI parity is wanted, add the
  `075` Dockerfile (consuming the Step 4 lockfile) — `075` itself concludes
  containerising the cron is *not* worth it, so this is explicitly optional.
- **Depends on:** Step 2 (snapshot layout exists) and Step 3 (manifest checksums to
  verify against).
- **What could break:** the restore workflow is the first destructive tool; require a
  typed `confirm_restore` gate (`074` restore.yml). The WORM copy means a GDPR/PDPL
  erasure cannot mutate history — record tombstones and replay them on restore
  (`074` §not-a-bug, `062-right-to-erasure`).
- **How to verify:** a live recovery drill — wipe the root master, run `restore.yml`,
  confirm row count and `sha256` match the manifest and the four derived CSVs rebuild.
- **Time:** 2–3 days (container optional: +1 day).

---

## Phase 6 — Persistence and scale

### Step 12 — Migrate the master to SQLite with dual-run reconciliation

- **What changes:** implement `033`/`042`: `sqlite3` (stdlib) as canonical store with
  `businesses`/`sources`/`contacts`/`changes` tables, transactional upsert, and the five
  CSVs generated as SQL views/derived exports. Run **dual** for a period: write both the
  DB and the CSVs, reconcile row counts and sampled rows, keep the CSV as rollback until
  counts match, then make the DB the only source of truth.
- **Depends on:** Step 8 (typed boundaries + a `MasterStore` port), Step 3 (manifest
  row counts to reconcile against), Step 2 (backup of the DB artifact), Step 1
  (concurrency guard — SQLite is single-writer).
- **What could break:** the biggest data-layer risk. A botched identity resolution
  merges or splits businesses, silently changing lead counts. Mitigate by (a) the
  dual-run reconciliation gate, (b) preserving the pipe-delimited `source` provenance as
  `sources` rows during import (`033` §1), (c) WAL + `BEGIN IMMEDIATE` upserts to avoid
  writer races (`033` §2), (d) never syncing a live DB to Drive.
- **How to verify:** `SELECT count(*)` equals the CSV `all_businesses` row count; a
  sampled 100 rows reconcile field-by-field; `website_checked_at`/`enrichment_version`
  columns present; the five export CSVs are byte-identical to the pre-migration run.
- **Time:** ~1 week.

### Step 13 — Incremental enrichment and versioned schema/scoring

- **What changes:** use SQLite freshness to stop re-fetching unchanged websites —
  enqueue only new/changed/never-checked/expired sites (`033` §4, `035`); add
  `schema_version` and independent `scoring_version`/`scored_at` columns with a
  repeatable backfill (`070`); expose country/region-partitioned exports
  (`042` §3).
- **Depends on:** Step 12 (the freshness columns live in SQLite).
- **What could break:** a wrong freshness key can skip a website that actually changed
  (stale `website_live`). Gate on `website` string equality, not the presence of a URL;
  increment `enrichment_version` on any scoring-rule change and run an explicit backfill
  rather than rescoring silently (`070` S2).
- **How to verify:** `website_checked_at` populated and HTTP call count drops on a
  no-op run; a scoring backfill diff shows only intended score moves; partitioned
  exports validate against the schema registry.
- **Time:** 3–5 days.

---

## Dependency graph and parallelisation

```
Step 1 ──▶ Step 2 ──▶ Step 3 ──▶ Step 4
                                   │
              ┌────────────────────┼─────────────────────┐
              ▼                    ▼                     ▼
           Step 5              Step 6 ──────────────▶ Step 7
              │                    │                     │
              └────────┬───────────┴──────────┬──────────┘
                       ▼                      ▼
                    Step 8 ──────────────▶ Step 9
                       │                      │
                       └──────────┬───────────┘
                                  ▼
                               Step 10
                                  │
             ┌────────────────────┼────────────────────┐
             ▼                                         ▼
          Step 11                                   Step 12
             (ops)        ┌───────────────────────────┘
                          ▼
                       Step 13
```

- **Strict serial prefix:** 1 → 2 → 3 → 4. Nothing else should start before the
  workflow is fail-closed, the master is backed up, and there is a manifest + tests to
  diff against.
- **Parallel fan-out:** Steps 5, 6, 7 are independent of each other and can be worked
  concurrently by different agents (the ~400-agent setup makes this the natural
  high-throughput point).
- **Step 8 before Step 9** is recommended (9's validated records slot into 8's typed
  boundary); 8 and 9 can overlap only if the `RawBusinessRecord` contract is agreed
  up front.
- **Step 10** is the natural merge point that proves the structural work is sound.
- **Steps 11 and 12 are independent** of each other (both depend on the prefix, not on
  each other) and can proceed in parallel.
- **Step 13** is the only true tail and depends on Step 12.

## Critical path and time-to-value

- **Critical path:** 1 → 2 → 3 → 4 → 6 → 8 → 9 → 10 → 12 → 13 ≈ **4–6 engineer-weeks**
  on the longest chain (SQLite reconciliation dominates).
- **Highest-value-per-day, do-first:** Steps 1–2 (half-day to two days) close the only
  *silent data-loss* failure in the system before any other work is scheduled.
- **What must not be deferred:** Step 1 is the one change where shipping late means
  every subsequent step is running against a master that could still be silently
  overwritten.

## The one invariant that governs everything

After **every** step, `leadminer run` against the real Drive master must still emit the
same five CSVs with the same column headers, and every verification above reduces to
"diff this run against the Step-3 manifest/checksums and the Step-4 golden fixtures."
Any step whose diff is anything other than the one *intentional* correction it declares
(scores in Step 6, liveness distribution in Step 5) is not shippable, regardless of how
green its own tests are.
