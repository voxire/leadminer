# 042 — CSV exports and master-store design

## Verdict

The five CSVs should be treated as **generated sales/analysis exports**, not as the database: every run reloads and rewrites the complete master, then uploads all five files and an additional timestamped copy to Drive. For the current single scheduled/manual writer, make a local SQLite database the canonical store, keep it out of Git, persist one versioned copy in Drive with bounded backups, and generate the four operational subsets from SQL; generate the full-master CSV only as an explicit, preferably region-split export. The product schema also needs a single documented contract: the brief says 22 columns, but the writer and `BusinessRecord` define 23.

## Findings

### S2 — Full CSV snapshots are the master and multiply on every run

- **Where:** `main.py:46-71` reads the whole master CSV into a list; `main.py:123-153` merges, materializes every subset, and rewrites all five full CSVs. `.github/workflows/scrape.yml:35-50` downloads CSVs, then uploads the five outputs both into a timestamped directory and the Drive root. `.gitignore:9` excludes `data/`; the workflow does not commit CSVs, despite `README.md:5` claiming it does.
- **Breaks:** The master is a full-file interchange format and an in-memory list, so each run must transfer, parse, deduplicate, enrich, and rewrite the cumulative dataset. Five overlapping snapshots are duplicated into each timestamped Drive folder; archive storage grows roughly by the size of five exports per run with no retention policy. Git is not the current persistence layer, but checking in changing master snapshots would also retain every full-file version in history. GitHub warns above 50 MiB, blocks regular Git objects over 100 MiB, and recommends repositories stay ideally below 1 GB ([GitHub large files](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)). Drive allows files up to 5 TB, so its immediate risk is account quota and unbounded duplicate snapshots, not a 100-MB per-file cap ([Drive file limits](https://support.google.com/drive/answer/37603)).
- **Trigger:** As accumulated rows push a CSV toward GitHub's object limit—or repeated dated archives exhaust the Drive account—the master cannot be pushed or the archive copy fails. There are no CSV fixtures checked into this workspace, so the present size/growth rate cannot be measured here.
- **Fix:** Make a transactional database the only working master, keep the database and exports out of Git, transfer one database artifact to/from Drive per run, and apply explicit retention to backups/exports; serialize workflow runs and never operate directly on a Drive-mounted database.

### S2 — The output schema and audience contract are ambiguous

- **Where:** `main.py:32-39` defines 23 CSV fields, independently of `scrapers/base.py:5-28`; the brief at `docs/audits/BRIEF.md:57-62` says 22. `main.py:139-146` defines subset predicates; `main.py:82-90,142-145` defines sales-ready contact; `README.md:21-46` describes the outputs and fields.
- **Breaks:** There is no complete data dictionary documenting types, null meaning, units, allowed values, or predicate semantics. “Qualified” is `completeness_score >= 1` in code, while README describes at least one contact signal. “Sales ready” is described as having a contact channel, but the actual predicate only counts phone, email, or Instagram, excluding populated `whatsapp`, `facebook`, and `linkedin` fields in the schema. `with_websites` means a nonempty URL, not a live site. A consumer cannot reliably infer the intended CSV contract from its name or headers; schema drift risks incompatible imports.
- **Trigger:** A high-priority row with only `whatsapp` populated is excluded from `sales_ready.csv`; separately, a downstream sales import assumes `qualified_businesses.csv` guarantees contactability. A new field added to the TypedDict but not the export schema is also at risk of disappearing.
- **Fix:** Publish one versioned schema/data dictionary as the source of truth (the actual 23 columns, field type/nullability, country/region vocabulary, UTC timestamp convention, and scoring/contact definitions); define named SQL views/predicates from that contract and make all writers validate against it.

### S2 — No region-partitioned delivery for a multi-market lead product

- **Where:** `main.py:139-153` writes country/region-agnostic global CSVs even though `region` and `country` are fields at `main.py:32-38`; workflow uploads the same full files at `.github/workflows/scrape.yml:46-50`.
- **Breaks:** A user targeting one market must download/filter a growing all-countries export, and even the four subsets remain full-size global copies. This increases transfer/storage and makes a large Excel-oriented export increasingly awkward. There is no named `Unknown` partition/manifest policy for records without inferred region.
- **Trigger:** Sales wants only Riyadh or a Lebanese governorate after the master grows; a single global extract becomes too large or hard to navigate.
- **Fix:** Keep market/region filtering in the store and materialize per-country/per-region export partitions, with stable slugs, an explicit `Unknown` bucket, and a small manifest (export timestamp, schema version, row count, and predicate) describing the files.

## Master-store options

| Option | Fit for this master | Tradeoff / verdict |
|---|---|---|
| **Parquet + PyArrow** | Excellent compact, typed, columnar snapshots for analytical consumers; supports compression and selective column reads. Partitioning by `country` and `region` suits analytics. | Parquet is a file format and PyArrow is its writer/reader, **not a transactional database**. Upserting a deduplicated business normally means rewriting affected files/partitions; partition/file lifecycle and schema evolution must be managed. Do not make this the mutable master. Add it later as an optional analytics snapshot if needed. [PyArrow Parquet docs](https://arrow.apache.org/docs/python/parquet.html) |
| **SQLite (`sqlite3`, stdlib)** | Best fit now: one portable local file, typed columns, indexes/constraints, transactions, simple Python integration with no new package. One serialized workflow writer and occasional exports match its embedded model. SQLite permits many readers but one writer at a time; its documented max database size is far beyond this workload. | Not a shared network database: download to runner-local disk, transact there, then upload the closed file. A GitHub Actions concurrency group is still needed to prevent two runs from downloading the same old master and overwriting one another. SQLite is the recommendation. [SQLite appropriate uses](https://sqlite.org/whentouse.html) |
| **DuckDB** | Strong if SQL analytics and direct Parquet/CSV interoperability become the primary need; embedded database, efficient scans/aggregations, and can export query results. | Requires a dependency and is analytics-oriented. In-process read/write supports writer threads within one process; multiple processes cannot concurrently write the native DB (Quack is documented as beta in the current docs). This is acceptable for one action writer, but offers no meaningful advantage over SQLite for the present mostly record-upsert workload. [Concurrency](https://duckdb.org/docs/current/connect/concurrency), [Parquet support](https://duckdb.org/docs/current/data/parquet/overview) |
| **PostgreSQL** | Best when the product becomes a live multi-user service: centralized access, concurrent writers, database roles, and operational backup/monitoring. | Requires a server/service, credentials/networking, migration and ongoing operations for a one-job scraper. Overkill until web/app users or concurrent writers need shared transactional access. Reconsider at that threshold rather than prematurely adding infrastructure. [PostgreSQL `COPY`](https://www.postgresql.org/docs/current/sql-copy.html) |

### Recommended shape

Use **SQLite as the sole canonical current-state master**, initially keeping the existing 23 fields in a `STRICT` `business` table with typed columns, a durable internal `business_id`, uniqueness/index strategy, and schema version. Retain source provenance separately if source-level auditability/history matters; do not serialize the current `source` union into the primary identity. Make the scraper merge/upsert in one transaction and export only after commit. Publish the database artifact to Drive as the recovery/persistence copy, with a bounded dated-backup policy and a single current canonical path. Keep all generated data out of Git; `.gitignore` already excludes `data/`, but ensure future DB/Parquet outputs remain excluded too. A Drive-held database is not a multi-writer service: the workflow should download it to local disk, verify/open/migrate it, write transactionally, close it, and upload the completed artifact. Never open a live SQLite file over a network/cloud-sync filesystem.

Consider DuckDB/Parquet as a read-optimized analytical layer only if row volume, reporting needs, or downstream query tooling justify it. Move the canonical store to PostgreSQL only when users/processes need concurrent shared writes or direct remote query access.

## CSV exports generated from the store

Define the existing products as named store queries/views, rather than independently filtering an in-memory list. Keep predicates centralized and test row counts and membership against the view:

1. **`sales_ready.csv`** — the primary sales list: high/medium `industry_priority` plus the contractually agreed contact predicate. Decide whether WhatsApp/Facebook/LinkedIn qualify before shipping; current `has_any_contact()` ignores those populated fields.
2. **`qualified_businesses.csv`** — `completeness_score >= 1`, if that remains the intended rule (otherwise align it with the README's contact-signal description).
3. **`with_websites.csv`** — a present/nonblank `website`, explicitly not the same as `website_live = true`.
4. **`without_websites.csv`** — missing/blank `website`; define blank and SQL `NULL` consistently.
5. **`all_businesses.csv`** — a full-store snapshot only for explicit compatibility/download or a backup/export event, not the working master and not a redundant timestamped copy every run. If consumers still require routine access, export partitioned files and optionally provide a generated global alias while it remains a manageable size.

For routine delivery, partition each of the four operational views by `(country, region)` (e.g. `exports/sales_ready/country=SA/region=riyadh.csv`), not the all-time full master. Produce a manifest with each batch; avoid uploading duplicate full and partitioned copies indefinitely. Continue to expose a combined export only where a consumer needs it. Since Excel is a real consumer and Arabic names/addresses must remain legible, **write every Excel-facing CSV as UTF-8 with a BOM** (`utf-8-sig` in Python). Microsoft confirms Excel can normally open UTF-8 CSV directly when the file has a BOM ([Microsoft guidance](https://support.microsoft.com/en-au/excel/opening-csv-utf-8-files-correctly-in-excel)). Parsers importing those CSVs must accept/strip the BOM (e.g. `utf-8-sig`) so the first header does not become `\ufeffname`; the internal SQLite data remains ordinary Unicode text.

## Not a bug, but worth knowing

- `data/` is currently gitignored and the workflow has no Git commit step (`.github/workflows/scrape.yml:35-50`), contrary to README's statement that CSVs are committed (`README.md:5`). Keep that separation explicit: code/schema in Git, generated data in durable storage.
- Drive's stated 5-TB maximum is a per-file maximum, not an assurance of available account quota. Measure real row counts, DB size, CSV size, and month-over-month growth before choosing an archival horizon.
- No split-by-region policy exists in code; partition names should use normalized values from a documented controlled vocabulary, not raw user-facing region text.

## Recommended order of work

1. Freeze the 23-column data contract and data dictionary; resolve the 22/23 count and exact qualified/sales-ready predicates.
2. Adopt SQLite as canonical store; design durable IDs, constraints, migrations, and an atomic transactional merge with a single-writer workflow guard.
3. Keep the database out of Git, store one current Drive master plus bounded recoverable backups, and stop archiving five duplicate exports each run.
4. Generate all five compatibility exports from SQL views; routinely deliver the four subsets by country/region and make all-businesses full dumps explicit/optional.
5. Add export validation (schema/header, row counts, predicate membership, region totals) and UTF-8 BOM to Excel-facing CSVs; verify BOM-safe imports.

## References

- GitHub, [About large files on GitHub](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github).
- Google, [Files you can store in Google Drive](https://support.google.com/drive/answer/37603).
- SQLite, [Appropriate Uses For SQLite](https://sqlite.org/whentouse.html).
- Apache Arrow, [Reading and Writing the Apache Parquet Format](https://arrow.apache.org/docs/python/parquet.html).
- DuckDB, [Concurrency](https://duckdb.org/docs/current/connect/concurrency) and [Parquet Files](https://duckdb.org/docs/current/data/parquet/overview).
- Microsoft, [Opening CSV UTF-8 files correctly in Excel](https://support.microsoft.com/en-au/excel/opening-csv-utf-8-files-correctly-in-excel).
