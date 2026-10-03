# 062 — Erasure, suppression, and export controls

## Verdict

There is no suppression, erasure, retention, or export-audit control in the current pipeline: a record loaded from the cumulative master is merged back into every run and written into five overlapping CSVs. Most importantly, deleting a row from the current master is not an erasure while timestamped Drive snapshots still contain it; future exports also need a durable suppression check so a fresh scrape cannot restore an opted-out contact.

## Findings

### S1 — No suppression gate protects any of the five exports

- **Where:** `main.py:93-97` loads the all-time master; `main.py:123-153` merges old and newly scraped records, builds subsets, then writes all five CSVs. `scrapers/base.py:5-28` and `main.py:32-39` define contact-bearing records/exports but no suppression status or store.
- **Breaks:** There is no way to record and enforce “do not use/contact/export this business/contact.” A previously suppressed business can be reintroduced by any scraper or remain in the master, then appear in `all_businesses.csv` and one or more derived exports. Checking only the sales-ready subset would still disclose it through the other four files.
- **Trigger:** A contact requests removal, is removed manually from `sales_ready.csv`, then appears again on the next run from `all_businesses.csv` or a fresh Google/OSM/Wikidata result and is written to the other exports.
- **Fix:** Keep a protected suppression/tombstone store outside the CSV exports; normalize and match stable identifiers (at least phone and email, plus durable internal business identity where available), apply it after every ingest/merge and again at the single shared export boundary, and test that no suppressed match appears in any of the five outputs. Retain only the minimum protected match token needed to prevent re-ingestion; do not export suppression reasons or tokens.

### S1 — Deleting current files does not erase Drive snapshots

- **Where:** `.github/workflows/scrape.yml:35-38` downloads only root-level CSVs (`--max-depth 1`); `:46-50` uploads each run's CSVs to a timestamped `gdrive:leads/data/$TIMESTAMP/` directory and also overwrites root copies. `main.py:148-153` regenerates local current CSVs but has no deletion path.
- **Breaks:** Removing a row from the local master and current Drive root leaves every prior timestamped copy intact. Those snapshots are not downloaded into the next run, so ordinary master cleanup neither discovers nor deletes them. A row can therefore remain retrievable in Drive after a purported deletion, even if it no longer appears in the current exports. The five overlapping exports multiply the number of copies to locate.
- **Trigger:** Delete a contact from `all_businesses.csv` and rerun; the new root export omits it, while a prior `data/<timestamp>/sales_ready.csv` (and potentially the other four files) still contains it.
- **Fix:** Implement an erasure workflow that identifies and purges matching rows/objects from the canonical store, local master and all derived files, every timestamped Drive snapshot, and any recoverable Drive trash/version copies under your control; verify completion across the full inventory. Keep the suppression tombstone so a later scrape cannot recreate the record. Record a non-identifying deletion receipt (case/reference ID, locations checked, counts, timestamps, and failures), and explicitly handle copies already downloaded by recipients, which Drive cleanup cannot recall.

### S2 — No defined retention window or expiry for dead leads

- **Where:** `main.py:46-71, 93-97, 123-153` loads and accumulates the master without age-based removal; the output schema has `scraped_at` (`main.py:32-39`) but no last-verified, expiry, or deletion state. `enricher.py:145-150, 172-177` defines `DEAD` as a website responding with HTTP 4xx/5xx, not as an expired/unusable lead.
- **Breaks:** Records can persist in the all-time master indefinitely, including stale contact details. The website liveness flag is not a lead-retention signal: a dead website may still have valid contact details, and a reachable website does not prove a contact is current. `scraped_at` alone does not specify when a lead expires or how suppression/deletion differs from ordinary expiry.
- **Trigger:** A contact address found years ago remains in the cumulative master and all applicable exports because no record-expiry policy or cleanup runs.
- **Fix:** Define separate lifecycle states and owner-approved windows for stale/unresponsive leads versus opt-outs/erasure requests; persist `last_verified_at`/`expires_at`, refresh only on meaningful verification, and purge expired rows from the store, derivatives, and Drive snapshots. Choose and document the period with privacy/legal review for Lebanon and Saudi Arabia rather than treating `website_live` as the policy.

### S2 — Exports have no accountable “who / what / when” record

- **Where:** `.github/workflows/scrape.yml:3-6, 40-50` runs manually and uploads exports without recording the initiating actor or export metadata. `main.py:74-79, 148-153` writes named files and prints only path and row count.
- **Breaks:** There is no durable record tying a person or service identity to a specific export, its contents/version, filters, destination, or success. The row-count print is not an audit trail and cannot establish which snapshot was sent where or whether an export was subsequently regenerated/deleted.
- **Trigger:** Asked “who exported `sales_ready.csv`, when, with what scope, and which exact file did Drive receive?”, the current files and logs cannot answer reliably.
- **Fix:** Emit one access-controlled, append-only audit event per export with UTC time, actor/service identity (for Actions, capture the workflow actor and run ID), export name/schema version, scope/filter, row count, destination, content digest, and outcome; log deletion events similarly. Keep contact values out of the event and restrict access to the audit store.

### S2 — Exception output has no PII-safe contract

- **Where:** `main.py:112-113` prints an arbitrary caught exception; `scrapers/osm.py:47-48`, `scrapers/wikidata.py:50-51`, and `scrapers/google_places.py:218-220` print request exception text. There is no centralized redaction or logging policy. The record fields being processed include direct contact identifiers at `scrapers/base.py:5-19`.
- **Breaks:** Exception strings are uncontrolled external text and may contain request URLs, parameters, or response details; formatting `{e}` makes it impossible to guarantee that contact data or credentials are not emitted. The current explicit progress prints mostly contain counts, and this review did not find a normal-path print of a record's phone/email, but that is not an enforceable protection for future errors or added logging.
- **Trigger:** A future request/error wrapper includes a contact URL or response payload in its exception, and the existing `{e}` output sends it to CI logs retained outside the CSV storage controls.
- **Fix:** Define a denylist/redaction invariant for every logger and exception boundary: never log record dumps or raw request/response bodies, and never include `name`, `address`, precise `lat`/`lon`, `phone`, `email`, `website` URL, `facebook`, `instagram`, `whatsapp`, or `linkedin` values (nor API keys/Drive credentials). Emit only a stable error code, source/operation, safe status code, and correlation/run ID; sanitize before chaining/reporting exceptions and test representative failures for leakage.

## Not a bug, but worth knowing

- “Business contact” is not automatically non-personal: a sole trader's name, direct phone/email, address, or social account may identify an individual. Treat these fields as sensitive personal data by default; have privacy counsel set applicable notice, legal-basis, access, and retention requirements for both markets. This code review does not determine those legal questions.
- A suppression token is itself potentially linkable data. Keep it access-controlled, purpose-limited to suppression, out of exports/logs, and retain it only under an approved policy; deleting the ordinary record while retaining a narrowly scoped tombstone is not the same as retaining a usable sales profile.

## Recommended order of work

1. Add the protected suppression mechanism and fail-closed export gate across every CSV; test re-ingestion and all subset outputs.
2. Inventory all current and timestamped Drive copies, implement verifiable cross-copy erasure, and stop claiming deletion until archived copies are handled.
3. Set owner/legal-approved lifecycle and retention rules, distinguish dead websites from expired leads, and apply cleanup to archives as well as current state.
4. Add per-export/deletion audit events and enforce the PII-safe logging contract before expanding data collection or access.
