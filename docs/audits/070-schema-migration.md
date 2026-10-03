# 070 — Schema evolution and historical scoring

## Verdict

The CSV header is an implicit public contract, but the pipeline has no schema version or migration boundary. A new field can be silently discarded, become a blank column for all historical rows, or alter deduplication behavior; a scoring change can overwrite historical scores without leaving a way to identify or reproduce the model that produced them. Treat the cumulative master as versioned data, migrate it explicitly, and version scoring independently from record shape.

## Findings

### S1 — The CSV schema can change or lose data without a migration
- **Where:** `main.py:33-40, 77-105, 108-130`; `dedup.py:79-99`
- **Breaks:** `FIELDS` is the actual output schema, but the record dictionaries can have other keys. `write_csv()` uses fixed `FIELDS` with `extrasaction="ignore"`, so a newly populated field not added to `FIELDS` disappears silently. If it is added to `FIELDS`, every output CSV gets a new header; old master rows have no value for it. Conversely, removing a field from `FIELDS` drops it on the next write. There is no version marker or controlled conversion. Before writing, `_merge()` unions arbitrary keys, and `_field_count()` counts every non-null value, including unknown columns, so an added column can also change which source record wins for otherwise conflicting fields.
- **Trigger:** Add `business_hours` to scraper output and start populating it without updating `FIELDS`: the value is discarded. Update `FIELDS` instead: historical rows get a blank cell, downstream CSV consumers now see a different schema, and an old CSV with an unrelated extra column can affect dedup merge preference before that column is dropped.
- **Fix:** Establish a registered `schema_version` column (for example `1` for the existing header and `2` for the next contract) and change schema only through a deliberate migration that reads the old version, transforms records, validates them, and atomically writes the new version.

### S2 — The reader is permissive by accident, not forward-compatible
- **Where:** `main.py:77-105, 124-127`; `scrapers/base.py:5-28`
- **Breaks:** `csv.DictReader` does read columns by name and does not fail merely because a header contains extra or missing names, but the returned rows are not normalized against a schema. Missing keys stay absent, removed fields are indistinguishable from absent/blank values, unknown fields remain in dictionaries until the fixed-field writer silently strips them, and known values are cast only for the current hard-coded field sets. The `BusinessRecord` TypedDict is another unversioned field list, separate from `main.FIELDS`. This is not a safe compatibility policy for a cumulative master.
- **Trigger:** A master produced by a newer run adds a column or drops an optional column. The current run accepts it without a compatibility decision, carries unknown data into dedup, then writes only `FIELDS`; removed values cannot be distinguished from intentionally empty data, and added values are lost.
- **Fix:** Put a version-aware normalization layer between CSV parsing and pipeline logic: resolve the version/header, map aliases and supported legacy forms to canonical keys, cast and default missing optional fields, tolerate/ignore unknown fields for computation, and preserve unknown values when rewriting the cumulative master (or explicitly archive them). Reject malformed headers and missing required identity fields with a clear error rather than silently treating them as blank. Keep scraper types and CSV fields generated from or checked against the same schema registry.

### S2 — Score values have no model lineage or historical re-score contract
- **Where:** `main.py:183-197`; `enricher.py:101-117, 275-311, 318-344`
- **Breaks:** The master stores `completeness_score` and `lead_score` as bare integers; there is no scoring-model version or scoring timestamp. `enrich()` recalculates both for every loaded and newly scraped record, and `main()` recalculates `lead_score` again after assigning industry priority. Thus a rule change silently replaces scores for every historical row on the next run, while the CSV gives downstream users no way to distinguish score versions or audit why a record moved. A schema version alone does not identify scoring semantics.
- **Trigger:** Change the lead-score weights or completeness rules. On the next run, all master rows are rescored in place and the five exported CSVs reflect the new values, but consumers cannot identify which rows were scored under the previous rule or compare results on a like-for-like basis.
- **Fix:** Add a separate `scoring_version` (for example `lead-v1`) and `scored_at` to score-bearing records. On a model change, run an explicit, repeatable backfill against the cumulative master, update the version and timestamp for every rescored row, validate score ranges/counts and a before/after diff, then publish atomically; retain a pre-backfill snapshot or score history if prior scores must remain auditable. Document that exact historical recomputation requires retaining the feature inputs used at scoring time (including website-check outcomes), since live enrichment can change independently of the model.

## Not a bug, but worth knowing

- `write_csv()` already writes via a same-directory temporary file and `os.replace()` (`main.py:108-133`), which is a useful publication primitive for a migration. It does not replace migration validation, a backup/snapshot, or a version check.
- The schema version and scoring version must be independent: a new contact field can change the record schema without changing score rules, and a changed score formula can require rescoring without changing the record layout.
- The audit brief calls the CSV a 22-column schema (`docs/audits/BRIEF.md:57-62`), while the listed names and `main.FIELDS` contain 23 columns. Reconcile the documented count as part of the initial schema registry, not by silently dropping a field.

## Recommended order of work

1. Declare the current 23-column header as the legacy schema version; define canonical names, types, required/optional fields, null/default semantics, and an explicit unknown-column policy in one registry.
2. Implement the version-aware reader/normalizer and migration path. For old files with no version column, recognize the known legacy header as v1; do not guess for an unrecognized header. Make schema additions/removals a reviewed migration with preservation or explicit archival of removed/unknown data.
3. Add `schema_version` to every emitted record and ensure all five CSV outputs use the same declared contract. Validate header, row shape, types, and record counts before atomic publication; exercise old/new/unknown-column cases in tests.
4. Add `scoring_version` and `scored_at` independently. Define a repeatable historical re-score command/procedure, snapshot the pre-change master, report the expected score distribution/diff, and publish only after validation.
5. Require downstream consumers to ignore unrecognized columns and use named-column access; announce breaking removals through a versioned migration rather than relying on CSV position or an implicit header change.
