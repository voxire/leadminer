# 033 — Replace the CSV master with SQLite

## Verdict

SQLite is a good fit for this workload: one scheduled Python process, one modest cumulative lead store, and no need for a database server. It replaces the fragile read/merge/rewrite of the whole master with transactional upserts and gives each business/source/enrichment state a durable identity. SQLite does not itself make GitHub Actions state durable, however: the current runner is ephemeral, so the database file still needs a single-writer restore/backup path and overlapping workflows must be prevented.

## Findings

### S1 — The master CSV is both database and export, so every run rewrites the whole world

- **Where:** `main.py:46-71` reads the full `all_businesses.csv`; `main.py:123-129` combines all scraped and historical records, deduplicates them, then calls `enrich()`; `main.py:148-153` overwrites all five CSVs. The workflow downloads and uploads those files at `.github/workflows/scrape.yml:35-50`.
- **Breaks:** Persistence has no row-level transaction or stable primary key. Every run parses and rewrites all history, and the complete historical record set is handed back to enrichment even when nothing about most businesses changed. A partial CSV write can damage the cumulative source of truth; CSV is also being used to encode multi-source provenance as a single pipe-delimited field (`dedup.py:54-56`).
- **Trigger:** A business with an unchanged website remains in the master. On the next run, it is included in `records` and its website is fetched again; `check_websites()` targets every record with a website (`enricher.py:180-189`). As the master grows, repeated HTTP checks and whole-file processing grow with it.
- **Fix:** Import the current master once, then make SQLite authoritative: match/upsert just the newly scraped rows inside a transaction, retain source observations separately, and export the five CSVs as derived snapshots for consumers.

### S2 — Full re-enrichment is not incremental and can be replaced with explicit freshness state

- **Where:** `main.py:123-129`; `enricher.py:180-205`; derived scores are recalculated in `enricher.py:270-290` and again in `main.py:131-137`.
- **Breaks:** `check_websites()` makes a GET for every nonempty website on every run, including unchanged historical rows. There is no persisted checked-at time, source-specific seen time, or enrichment version to distinguish new data from already-processed data.
- **Trigger:** A monthly scheduled run that finds the same 10,000 businesses makes up to 10,000 website requests again, though only a small new/changed subset may need checking.
- **Fix:** Persist `website_checked_at` and `enrichment_version`; enqueue a check only for a new/changed website, a never-checked website, or one older than a chosen refresh interval. Recompute local derived fields only for records whose inputs changed or whose scoring/rules version changed.

### S2 — SQLite removes the service, not the need to persist and serialize the cron state

- **Where:** `.github/workflows/scrape.yml:8-11` runs on a fresh `ubuntu-latest` runner; `.github/workflows/scrape.yml:35-50` currently restores/uploads CSVs and has no concurrency group.
- **Breaks:** A SQLite file left in the runner workspace disappears when the job ends. Copying a live database (especially with WAL sidecars) to Drive, or allowing two runs to restore the same old copy and then upload, can lose committed changes. SQLite permits many readers but only one writer at a time; WAL is not a multi-writer or distributed-lock solution.
- **Trigger:** A manual dispatch overlaps the future schedule, or the workflow is cancelled after database changes but before the database is safely published.
- **Fix:** Set workflow-level concurrency with `cancel-in-progress: false`; restore one database before the run, use it locally as the only writer, checkpoint/close it and validate the backup before publishing, and fail the job if durable publication fails. Never sync the database while the process is using it. If multiple runners or concurrent writers become a requirement, move to a server database rather than treating a Drive file as one.

## Recommended schema

Use the Python standard-library `sqlite3`; no server, daemon, or new runtime package is needed. Enable foreign keys on every connection and use a transaction per scraper batch (or a single transaction for all source batches). This schema keeps the current business projection, stores source provenance and contacts as rows, and records auditable changes. The SQL is SQLite DDL:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE scrape_runs (
    id              INTEGER PRIMARY KEY,
    source_name     TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL CHECK (status IN ('running','succeeded','partial','failed')),
    cursor_before   TEXT,
    cursor_after    TEXT,
    fetched_count   INTEGER NOT NULL DEFAULT 0 CHECK (fetched_count >= 0),
    upserted_count  INTEGER NOT NULL DEFAULT 0 CHECK (upserted_count >= 0),
    error           TEXT
);
CREATE INDEX scrape_runs_source_status ON scrape_runs(source_name, status, finished_at);

CREATE TABLE businesses (
    id                  INTEGER PRIMARY KEY,
    name                TEXT,
    category            TEXT,
    region              TEXT,
    country             TEXT NOT NULL DEFAULT 'LB',
    address             TEXT,
    city_key            TEXT NOT NULL DEFAULT '',
    lat                 REAL,
    lon                 REAL,
    website             TEXT,
    website_live        INTEGER CHECK (website_live IN (0,1) OR website_live IS NULL),
    website_checked_at  TEXT,
    rating              REAL,
    review_count        INTEGER,
    completeness_score  INTEGER NOT NULL DEFAULT 0,
    lead_score          INTEGER NOT NULL DEFAULT 0,
    industry_priority   TEXT,
    recommended_service TEXT,
    normalized_name     TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    enrichment_version  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX businesses_name_city ON businesses(country, normalized_name, city_key);
CREATE INDEX businesses_website_refresh ON businesses(website_checked_at) WHERE website IS NOT NULL;
CREATE INDEX businesses_sales_filter ON businesses(industry_priority, lead_score);

CREATE TABLE sources (
    id              INTEGER PRIMARY KEY,
    business_id     INTEGER NOT NULL REFERENCES businesses(id),
    source_name     TEXT NOT NULL,
    external_id     TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    last_run_id     INTEGER REFERENCES scrape_runs(id),
    raw_json        TEXT,
    UNIQUE(source_name, external_id)
);
CREATE UNIQUE INDEX sources_without_external_id
    ON sources(business_id, source_name) WHERE external_id IS NULL;
CREATE INDEX sources_business ON sources(business_id, source_name);

CREATE TABLE contacts (
    id              INTEGER PRIMARY KEY,
    business_id     INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN
                    ('phone','email','facebook','instagram','whatsapp','linkedin')),
    value           TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    is_current      INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
    source_name     TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    UNIQUE(business_id, kind, normalized_value)
);
CREATE INDEX contacts_match ON contacts(kind, normalized_value, business_id);
CREATE INDEX contacts_current ON contacts(business_id, kind) WHERE is_current = 1;

CREATE TABLE changes (
    id              INTEGER PRIMARY KEY,
    business_id     INTEGER NOT NULL REFERENCES businesses(id),
    run_id          INTEGER REFERENCES scrape_runs(id),
    source_name     TEXT,
    field_name      TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    changed_at      TEXT NOT NULL,
    change_kind     TEXT NOT NULL CHECK (change_kind IN ('insert','update','merge','enrichment'))
);
CREATE INDEX changes_business_time ON changes(business_id, changed_at);
CREATE INDEX changes_run ON changes(run_id);
```

`businesses` holds the one current canonical projection (including website and scoring outputs); `contacts` holds one or more observed contact values and provenance. The existing CSV columns `phone`, `email`, `facebook`, `instagram`, `whatsapp`, and `linkedin` are produced by selecting the current contact value(s) for the export, not duplicated as independently edited columns. `sources` is a business-to-source observation, not just a list of scraper names: keep one row per source identity, update `last_seen_at`/`last_run_id`, and retain the raw source payload when useful. `changes` is append-only history for meaningful field changes; do not write a change event for an identical upsert.

## Matching, migration, and run behavior

1. **Bootstrap once.** Read `data/all_businesses.csv` using the existing casts (`main.py:46-71`), normalize each record, and insert a baseline business/contact/source set. Split the legacy pipe-delimited `source` field into separate source observations and use `scraped_at` as the initial seen time. Preserve a copy of the CSV until row counts and representative records match the exported database view.
2. **Resolve identity in the writer transaction.** Normalize phone using the existing country-aware routine (`dedup.py:11-25`); first look for a matching current phone contact, then fall back to `(country, normalized_name, city_key)` for records without a phone. If neither matches, insert a business. Do not make phone or name/city a blind unique constraint: phones can be shared/reassigned, and the current dedup logic treats phone and phone-less name/city records differently (`dedup.py:64-97`). Resolve ambiguous candidates explicitly, preserve stable `businesses.id`, and emit a `merge` change if a human/policy-approved merge is needed. Serialize the find-or-insert and writes in one `BEGIN IMMEDIATE` transaction so two writers cannot race into duplicates.
3. **Upsert observations, not the whole master.** For each source row, upsert its business fields with the established non-null/quality merge policy; upsert the `(business_id, kind, normalized_value)` contacts; and update source first/last-seen metadata. Record only changed business fields in `changes`. A stable upstream ID should be the strongest source-level identity. Commit each scraper's batch and mark that `scrape_runs` row successful only after its batch is stored; only advance that source's cursor/watermark on success.
4. **Enrich incrementally.** Select businesses with a website and `website_checked_at IS NULL`, a changed website, or a check older than policy (for example 30 days). On successful or failed fetch, persist the check time and liveness; update contacts discovered from that page with their source and timestamps. Infer region locally when absent. Recompute completeness, priority, recommendation, and lead score only for affected records; a changed scoring algorithm increments `enrichment_version` and schedules a deliberate backfill. This removes repeated enrichment of unchanged websites without pretending that the upstream scrape itself is incremental.
5. **Be honest about source deltas.** `scrape_runs.cursor_before/cursor_after` supports per-source watermarks, but a DB does not create incremental APIs. OSM can be adapted to a newer-than query; Wikidata's current fixed `LIMIT 5000` and the Places text searches are not shown as delta queries in this codebase. Keep full source discovery where necessary, but still upsert only changed rows and avoid re-fetching every historical website. Do not advance a cursor after a failed/partial source run.
6. **Keep CSVs as exports.** Generate `all_businesses.csv` from the canonical business/contact/source joins, then derive `qualified_businesses.csv`, `with_websites.csv`, `without_websites.csv`, and `sales_ready.csv` using the current predicates in `main.py:139-146`. Continue publishing these for sales/Drive consumers if needed; they cease to be the persistence layer. Write exports to temporary files and atomically replace them only after successful generation.

For a single cron job, use a local SQLite file, `WAL` for safe local reader behavior, a finite busy timeout, and normal transactions; no ORM or hosted service is warranted. Before backing up a WAL-mode database, use SQLite's backup API (preferred) or close all connections and checkpoint (`PRAGMA wal_checkpoint(TRUNCATE)`), then validate the backup. A GitHub Actions artifact is not a durable database by itself; use a durable, single-writer location and retain versioned backups. SQLite is zero-ops relative to operating a DB server, not zero-responsibility for backups, migrations, and locking.

## Recommended order of work

1. Define and test the migration/import plus a deterministic CSV export; keep the CSV master as rollback reference until counts and sampled rows reconcile.
2. Add transactional identity resolution, source/contact provenance, and change recording; make the DB the only source of truth after reconciliation.
3. Add enrichment freshness/version policy and incremental selection; then run a one-time backfill intentionally.
4. Update the cron workflow to serialize runs and restore/publish validated database backups safely; retain CSV export upload for downstream users.
