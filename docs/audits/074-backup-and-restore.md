# 074 — Backup and Restore Design

## Verdict

The current persistence model treats an unversioned Google Drive directory as both an active operational datastore and a disaster recovery archive, with zero retention controls, no secondary replica, and no point-in-time recovery (PITR) mechanism. Every pipeline execution uploads an uncompressed, unvalidated duplicate of all CSV exports to `gdrive:leads/data/$TIMESTAMP/`, guaranteeing unbounded Drive storage growth and eventual API failure. Crucially, if Google Drive credentials fail, the Google account is suspended, or a corrupted master is uploaded, the entire cumulative business database is permanently lost because `data/` is gitignored and ephemeral CI runners hold no state.

---

## Findings

### S1 — Unbounded timestamped directory uploads cause Drive quota and API exhaustion
- **Where:** `.github/workflows/scrape.yml:48-50`
- **Breaks:** Every run creates a new folder `gdrive:leads/data/$TIMESTAMP/` and uploads all five CSV files uncompressed. Over weekly and manual runs, this results in hundreds of directories containing identical or slightly mutated CSV files. Google Drive imposes hard file count limits (500,000 items per user/Shared Drive), strict API rate limits (10 requests/second per user), and standard storage quotas (15 GB free, 30 GB to 2 TB on Google Workspace). Without pruning, `rclone list` operations degrade exponentially, eventual `rclone copy` commands fail with `quotaExceeded` (HTTP 403), and the automated pipeline crashes.
- **Trigger:** Accumulation of regular cron runs and ad-hoc manual dispatches over several months.
- **Fix:** Implement an automated Grandfather-Father-Son (GFS) retention schedule that prunes historical snapshots to a bounded set (14 daily, 8 weekly, 12 monthly, 3 annual) via an automated post-run cleanup script.

### S1 — Google Drive as a single point of failure (SPOF) risks catastrophic total data loss
- **Where:** `.github/workflows/scrape.yml:35-38,46-50`; `.gitignore:9`
- **Breaks:** The cumulative business database exists solely in `data/` on the ephemeral GitHub Actions runner and inside `gdrive:leads/`. Because `data/` is gitignored, the repository holds zero data history. If the Google account is flagged/suspended, the Google Cloud project disabled, billing lapses, an rclone OAuth token expires, or a malicious/buggy command executes `rclone sync --delete-after` with reversed arguments, all cumulative business records scraped since project inception are permanently destroyed.
- **Trigger:** Cloud account suspension, OAuth refresh token invalidation, or accidental Drive root deletion.
- **Fix:** Establish a 3-2-1 backup topology: replicate compressed master snapshots to an independent S3-compatible object store (e.g., Cloudflare R2 or AWS S3) configured with immutable Object Lock (WORM retention) and an isolated authentication boundary.

### S1 — Swallowed download failure (`|| true`) permits silent master overwrite with partial data
- **Where:** `.github/workflows/scrape.yml:38,49-50`
- **Breaks:** The download step runs `rclone copy "gdrive:leads/" data/ --max-depth 1 --include "*.csv" --drive-chunk-size 8M || true`. If Drive is unreachable, credentials fail, or rate limits are exceeded, the error is swallowed and `data/` remains empty. `main.py` starts with `master = []` (`main.py:80,150`), scrapes only new records, and writes this small batch to `data/`. The upload step then executes `rclone copy data/ "gdrive:leads/"`, overwriting the cumulative master with the partial batch. Months of historical data are silently erased in a single successful workflow run.
- **Trigger:** Transient network timeout or expired OAuth token during initial `rclone copy`.
- **Fix:** Remove `|| true`, fail closed on download errors, and enforce an explicit pre-upload volume invariant: reject publishing if the record count drops by more than 10% without an explicit manual override.

### S2 — Archiving derived projections multiplies backup storage and network bandwidth by 4×
- **Where:** `main.py:208-213`; `.github/workflows/scrape.yml:48-49`
- **Breaks:** The upload step copies the entire `data/` directory into cold storage. Four of the five files (`qualified_businesses.csv`, `with_websites.csv`, `without_websites.csv`, `sales_ready.csv`) are deterministic, losslessly reconstructible views of `all_businesses.csv`. Storing all four derived files in every point-in-time snapshot quadruples storage consumption and transfer times without providing any disaster recovery benefit.
- **Trigger:** Every run executing lines 48-49 in `.github/workflows/scrape.yml`.
- **Fix:** Archive only the canonical master (`all_businesses.csv.gz` or SQLite `leads.db.gz`), an integrity manifest (`manifest.json`), and run diagnostic logs in timestamped snapshots; deliver the four derived CSVs only to the active root/export location.

### S2 — Absence of a documented Point-In-Time Recovery (PITR) mechanism leads to operator error
- **Where:** `.github/workflows/scrape.yml:46-50`
- **Breaks:** If an enrichment bug marks hundreds of active websites dead (`enricher.py:151-152`), a bad regex truncates phone numbers, or invalid categories pass the whitelist, there is no tool or procedure to roll back to a specific timestamped snapshot. An operator attempting manual recovery via the Drive web UI or ad-hoc rclone commands risks copying derived files over the master, uploading corrupted partials, or triggering concurrency races with scheduled runs.
- **Trigger:** Need to revert a poisoned master file after a flawed scrape or code deployment.
- **Fix:** Implement a dedicated restoration CLI (`tools/restore.py`) and a GitHub Actions workflow (`.github/workflows/restore.yml`) featuring pre-flight SHA-256 verification, schema validation, safety staging, and atomic promotion.

### S3 — Raw uncompressed CSVs lack cryptographic checksum manifests and bit-rot detection
- **Where:** `.github/workflows/scrape.yml:48-50`; `main.py:108-135`
- **Breaks:** CSVs are stored as raw plaintext without compression or checksum manifests (`SHA256SUMS`). Storage is 4× to 5× larger than necessary. Furthermore, there is no way to detect silent file truncation, network packet corruption, or character encoding corruption (e.g., Arabic business name mojibake) in archived snapshots without manually reading each file.
- **Trigger:** Flaky network transfer during multi-megabyte CSV upload or silent storage corruption.
- **Fix:** Compress snapshot masters using `gzip -9` (or `zstandard`) and generate a machine-readable `manifest.json` containing SHA-256 digests, row counts, and schema definitions.

---

## What to Retain: Data Classification and Storage Artifacts

The system processes three distinct tiers of data. Conflating them into a single `data/` folder uploaded indiscriminately to Drive causes the unbounded growth.

```
data/
├── all_businesses.csv          <- Canonical Master (PERSIST & ARCHIVE)
├── qualified_businesses.csv    <- Derived View (LATEST ONLY)
├── with_websites.csv           <- Derived View (LATEST ONLY)
├── without_websites.csv        <- Derived View (LATEST ONLY)
├── sales_ready.csv             <- Derived View (LATEST ONLY)
├── manifest.json               <- Run & Data Contract (ARCHIVE)
└── run_summary.json            <- Execution Telemetry (ARCHIVE)
```

### 1. Canonical Master (Ground Truth)
- **File:** `all_businesses.csv` (or `leads.db` if migrated to SQLite per Audit 033).
- **Classification:** **Mission-Critical State.**
- **Retention:** Must be preserved indefinitely under a Grandfather-Father-Son rotation. Every historical snapshot must retain an exact, compressed, checksummed copy of this file.
- **Storage Format:** Gzip-compressed (`all_businesses.csv.gz`). At 50,000 records (~18 MB raw), gzip achieves ~3.5 MB (~80% compression ratio).

### 2. Derived Views (Projections)
- **Files:** `qualified_businesses.csv`, `with_websites.csv`, `without_websites.csv`, `sales_ready.csv`.
- **Classification:** **Ephemeral Product Deliverables.**
- **Retention:** **Latest version only.** Never persist in historical snapshot folders.
- **Rationale:** These files are 100% deterministically generated from `all_businesses.csv` using pure filtering logic (`main.py:199-206`). Storing them in historical snapshots wastes 80% of storage quota. They belong solely in `gdrive:leads/` (or `gdrive:leads/exports/`) for end-user CRM ingest.

### 3. Run Manifest and Operational Telemetry
- **Files:** `manifest.json`, `run_summary.json`.
- **Classification:** **Audit & Governance Metadata.**
- **Retention:** Retained alongside every compressed master snapshot.
- **Contents:** Cryptographic signatures, row counts, delta statistics, scraper yields, schema version, Git commit SHA, and execution timestamp.

#### Manifest Schema (`manifest.json`)
```json
{
  "version": 1,
  "run_id": "github-actions-12345678",
  "commit_sha": "a1b2c3d4e5f67890abcdef1234567890abcdef12",
  "timestamp": "2026-10-01T03:00:00Z",
  "record_counts": {
    "master_total": 48210,
    "net_new": 1420,
    "updated": 320,
    "qualified": 31200,
    "sales_ready": 8450
  },
  "schema": {
    "columns": 23,
    "encoding": "utf-8-sig",
    "header_checksum_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  },
  "files": {
    "all_businesses.csv.gz": {
      "size_bytes": 3670016,
      "sha256": "7f83b1657ff1fc53b92dc18148a1d65dfc2d4b1fa3d677284addd200126d9069",
      "uncompressed_rows": 48210
    }
  }
}
```

---

## Retention Schedule: Grandfather-Father-Son (GFS)

To prevent unbounded Drive growth while guaranteeing granular recovery windows, the system must transition to a tiered Grandfather-Father-Son retention policy.

### Tiered Retention Matrix

| Tier | Window | Frequency | Snapshots Retained | Purpose |
|---|---|---|---|---|
| **Son (Daily / Hot)** | Last 14 days | Every run | Max 14 | Immediate rollback from scraping bugs, IP blocks, or faulty dedup. |
| **Father (Weekly)** | 15 to 60 days | 1 per week (Sunday 00:00Z) | 8 | Medium-term rollback; audit changes across scrape cycles. |
| **Grandfather (Monthly)**| 61 to 365 days | 1 per month (1st of month)| 12 | Long-term historical trend analysis and regulatory compliance. |
| **Archive (Annual)** | 1 to 3 years | 1 per year (Jan 1st) | 3 | Historical business census and multi-year survivability. |
| **Total Steady State** | **3 Years** | — | **~37 snapshots** | **Total storage: ~135 MB** (vs tens of GBs unmanaged). |

### Storage Budget Comparison

```
Current Architecture (Uncompressed 5 CSVs, No Pruning):
- Size per run: 5 files * ~4.5 MB average = 22.5 MB / run
- After 1 year (weekly runs = 52 runs): 52 * 22.5 MB = 1.17 GB (260 files)
- After 3 years (weekly runs = 156 runs): 156 * 22.5 MB = 3.51 GB (780 files)
- With manual test dispatches (e.g., 200 runs): > 4.5 GB (> 1,000 files)
- Threat: Drive folder listing latency spikes; manual navigation becomes impossible.

Hardened Architecture (GFS Policy, Compressed Master + Manifest):
- Size per snapshot: all_businesses.csv.gz (~3.5 MB) + manifest.json (2 KB) = ~3.5 MB
- Active working set: 37 retained snapshots * 3.5 MB = 129.5 MB
- Latest export set (5 CSVs uncompressed in root): ~22.5 MB
- Total steady-state storage: ~152 MB (Fixed upper bound forever)
- Savings: > 96% reduction in storage and bandwidth.
```

### Automated Pruning Implementation (`tools/prune_backups.py`)

Pruning must be deterministic, atomic, and safe. It must never delete the active master or the latest snapshot.

```python
"""
tools/prune_backups.py — Enforce GFS retention across remote snapshot directories.
"""

from datetime import datetime, timezone, timedelta
import json
import re
import subprocess
import sys

def list_remote_snapshots(remote_prefix: str) -> list[dict]:
    cmd = ["rclone", "lsf", "--dirs-only", remote_prefix]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    snapshots = []
    for line in res.stdout.splitlines():
        folder = line.strip().rstrip("/")
        # Matches ISO timestamps: YYYY-MM-DDTHH-MM or YYYY-MM-DDTHH-MM-SS
        match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}(?:-\d{2})?)$", folder)
        if match:
            try:
                ts = datetime.strptime(match.group(1)[:16], "%Y-%m-%dT%H-%M").replace(tzinfo=timezone.utc)
                snapshots.append({"folder": folder, "timestamp": ts})
            except ValueError:
                continue
    return sorted(snapshots, key=lambda x: x["timestamp"], reverse=True)

def select_snapshots_to_keep(snapshots: list[dict], now: datetime) -> set[str]:
    keep = set()
    daily_seen = set()
    weekly_seen = set()
    monthly_seen = set()
    yearly_seen = set()

    for s in snapshots:
        folder = s["folder"]
        ts = s["timestamp"]
        age = now - ts

        # 1. Hot tier: Keep all runs for 14 days
        if age <= timedelta(days=14):
            keep.add(folder)
            continue

        # 2. Weekly tier: Keep 1 per calendar week up to 60 days
        if age <= timedelta(days=60):
            year_week = f"{ts.year}-W{ts.isocalendar().week:02d}"
            if year_week not in weekly_seen:
                keep.add(folder)
                weekly_seen.add(year_week)
            continue

        # 3. Monthly tier: Keep 1 per month up to 365 days
        if age <= timedelta(days=365):
            year_month = f"{ts.year}-{ts.month:02d}"
            if year_month not in monthly_seen:
                keep.add(folder)
                monthly_seen.add(year_month)
            continue

        # 4. Annual tier: Keep 1 per year up to 3 years (1095 days)
        if age <= timedelta(days=1095):
            year = f"{ts.year}"
            if year not in yearly_seen:
                keep.add(folder)
                yearly_seen.add(year)
            continue

    # Safety invariant: ALWAYS keep at least the most recent snapshot regardless of age
    if snapshots:
        keep.add(snapshots[0]["folder"])

    return keep

def prune_remote(remote_prefix: str, dry_run: bool = False):
    now = datetime.now(timezone.utc)
    snapshots = list_remote_snapshots(remote_prefix)
    keep = select_snapshots_to_keep(snapshots, now)

    to_delete = [s["folder"] for s in snapshots if s["folder"] not in keep]
    print(f"Total snapshots: {len(snapshots)} | Retaining: {len(keep)} | Pruning: {len(to_delete)}")

    for folder in to_delete:
        target = f"{remote_prefix}{folder}/"
        if dry_run:
            print(f"[DRY RUN] Would purge: {target}")
        else:
            print(f"Purging expired snapshot: {target}")
            subprocess.run(["rclone", "purge", target], check=True)

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    remote = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "gdrive:leads/snapshots/"
    prune_remote(remote, dry_run=dry_run)
```

---

## Point-In-Time Recovery (PITR) Protocol

Point-in-time recovery is the operational procedure for reverting the database to a verified historical state when a data corruption incident occurs.

### Disaster Recovery Runbook

```
                         [DISASTER DETECTED]
             (Data corruption, scraping bug, or zero-out)
                                  │
                                  ▼
                     Step 1: Discover & Audit
               `python -m tools.restore --list-snapshots`
                                  │
                                  ▼
                      Step 2: Staging Download
              Download snapshot to isolated temporary dir
                                  │
                                  ▼
                     Step 3: Verification Phase
               - Check SHA-256 against manifest.json
               - Decompress & validate CSV columns (23)
               - Validate non-zero record count
                                  │
                      ┌───────────┴───────────┐
                    PASS                     FAIL
                      │                        │
                      ▼                        ▼
          Step 4: Safety Backup          Abort Operation
       Snapshot current live master       Alert on-call
                      │
                      ▼
          Step 5: Atomic Promotion
       Promote restored master to root
                      │
                      ▼
        Step 6: Projection Rebuild
       Regenerate the 4 derived CSVs
                      │
                      ▼
        Step 7: Verify Live Endpoints
```

### Operational Step-by-Step Execution

#### Step 1: Discover Available Snapshots
List available snapshots with record counts and timestamps:
```bash
python tools/restore.py --list
```
*Output:*
```
Available Snapshots (Google Drive & R2):
  TIMESTAMP             RECORDS   SHA256 (PREFIX)   STATUS
  2026-10-01T03-00      48,210    7f83b165...       VERIFIED
  2026-09-24T03-00      47,680    3a90c128...       VERIFIED
  2026-09-17T03-00      46,910    e11b2299...       VERIFIED
  2026-09-10T03-00      45,820    88cc3144...       VERIFIED
```

#### Step 2: Download to Isolated Local Staging
Never restore directly into `data/all_businesses.csv`. Always fetch into an isolated staging directory:
```bash
python tools/restore.py --download 2026-09-24T03-00 --staging-dir /tmp/leadminer_restore
```

#### Step 3: Integrity and Schema Verification
The restoration tool verifies:
1. **Cryptographic Checksum:** Compares `sha256sum(all_businesses.csv.gz)` against `manifest.json`.
2. **Decompression Integrity:** Unpacks the archive and verifies gzip trailer CRC32.
3. **Schema Invariant:** Validates that all 23 expected headers (`name`, `category`, ..., `scraped_at`) are present in exact order with UTF-8-sig encoding.
4. **Volume Invariant:** Asserts uncompressed line count matches `manifest.json["record_counts"]["master_total"]`.

#### Step 4: Safety Snapshot of Current State
Before modifying the live remote master, snapshot whatever currently exists in `gdrive:leads/` to a rescue directory:
```bash
RESCUE_TS=$(date -u +%Y-%m-%dT%H-%M-pre-restore)
rclone copy "gdrive:leads/all_businesses.csv" "gdrive:leads/rescue/$RESCUE_TS/" || true
```

#### Step 5: Atomic Promotion
Upload the verified restored master to replace the active copy:
```bash
python tools/restore.py --promote /tmp/leadminer_restore/all_businesses.csv --target gdrive:leads/
```

#### Step 6: Reconstruct Derived Views
Run the projection builder to re-derive the four sales views from the restored master so downstream users receive consistent data:
```bash
python -m leadminer.export --master data/all_businesses.csv --output-dir data/
rclone copy data/ "gdrive:leads/" --include "*.csv"
```

### GitHub Actions Restore Workflow (`.github/workflows/restore.yml`)

Enable self-service, audited rollbacks directly from GitHub Actions without requiring local developer environments:

```yaml
name: Restore Master Dataset

on:
  workflow_dispatch:
    inputs:
      snapshot_timestamp:
        description: 'Snapshot timestamp to restore (e.g., 2026-09-24T03-00)'
        required: true
        type: string
      target_remote:
        description: 'Storage backend to restore from'
        required: true
        default: 'gdrive'
        type: choice
        options:
          - gdrive
          - r2_secondary
      confirm_restore:
        description: 'Type "RESTORE" to confirm destructive overwrite of active master'
        required: true
        type: string

concurrency:
  group: leadminer-production
  cancel-in-progress: false

jobs:
  restore:
    runs-on: ubuntu-latest
    if: inputs.confirm_restore == 'RESTORE'
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: |
          pip install uv
          uv pip install --system -r requirements.txt
          sudo apt-get install -y rclone -q

      - name: Configure rclone
        env:
          RCLONE_CONFIG_CONTENT: ${{ secrets.RCLONE_CONFIG }}
        run: |
          mkdir -p ~/.config/rclone
          echo "$RCLONE_CONFIG_CONTENT" > ~/.config/rclone/rclone.conf
          chmod 600 ~/.config/rclone/rclone.conf

      - name: Execute Verified Restoration
        run: |
          python tools/restore.py \
            --source "${{ inputs.target_remote }}:leads/snapshots/${{ inputs.snapshot_timestamp }}/" \
            --promote-to "gdrive:leads/" \
            --rebuild-projections
```

---

## Surviving Single-Vendor Failure: The 3-2-1 Backup Strategy

If Google Drive is the sole repository for the gitignored master, the business operates with an existential single point of failure.

### Threat Modeling: Google Drive as Single Point of Failure

| Risk Scenario | Likelihood | Impact | Consequence for leadminer |
|---|---|---|---|
| **OAuth Token Expiry** | High | High | rclone user refresh tokens expire if unrefreshed for 6 months or if OAuth consent app remains in "Testing" status (7-day token lifespan). Pipeline silently halts. |
| **Account Ban / Suspension** | Medium | Fatal | Google Workspace billing lapse, TOS policy flags, or automated abuse triggers lock the entire Google account. All historical data permanently inaccessible. |
| **Human / Script Deletion** | Medium | Fatal | An engineer or workflow script executing `rclone sync data/ gdrive:leads/` with an empty local directory permanently purges the remote master. |
| **Drive API Rate Limiting** | High | Medium | HTTP 403 `User Rate Limit Exceeded` during large scrapes blocks upload, leaving CI runs in a failed, uncheckpointed state. |
| **Silent Bit Rot / Corruption** | Low | High | Drive file updates without version locks can result in partially written files or corrupted byte streams. |

### The LeadMiner 3-2-1 Implementation Architecture

To guarantee survivability, leadminer must implement the standard **3-2-1 Backup Rule**:
- **3 Copies of Data:**
  1. Active working copy on runner / local runtime.
  2. Primary cloud copy on Google Drive (`gdrive:leads/`).
  3. Secondary cold archive on S3-compatible object storage (`r2:leadminer-backups/`).
- **2 Different Media / Providers:**
  1. Google Drive (Google Cloud ecosystem, used for user distribution and Google Sheets integration).
  2. Cloudflare R2 or AWS S3 (Independent cloud infrastructure, different billing, identity, and security realm).
- **1 Immutable / Off-Site Copy:**
  - S3 / R2 Object Lock enabled with **WORM (Write Once, Read Many)** retention for 90 days. Even with compromised rclone credentials, historical snapshots cannot be overwritten or deleted.

```
                   GitHub Actions Production Runner
                                  │
         ┌────────────────────────┴────────────────────────┐
         │ Writes & Verifies: all_businesses.csv.gz        │
         │ Generates: manifest.json + sha256sum            │
         └────────────────────────┬────────────────────────┘
                                  │
                 ┌────────────────┴────────────────┐
                 │                                 │
                 ▼                                 ▼
       Primary Storage Mirror            Secondary Storage Mirror
         [Google Drive]                   [Cloudflare R2 / AWS S3]
  gdrive:leads/                          r2:leadminer-backups/
  ├── all_businesses.csv (active)        ├── snapshots/ (immutable WORM)
  ├── qualified_businesses.csv           │   ├── 2026-10-01T03-00/
  ├── sales_ready.csv                    │   │   ├── all_businesses.csv.gz
  └── snapshots/                         │   │   └── manifest.json
      └── 2026-10-01T03-00/              └── latest/
          ├── all_businesses.csv.gz          └── manifest.json
          └── manifest.json
```

### Technical Implementation of Dual-Mirror Upload

Update the GitHub Actions workflow (`.github/workflows/scrape.yml`) to mirror backups simultaneously to both Google Drive and Cloudflare R2:

```yaml
      - name: Archive and Mirror Master Dataset
        run: |
          TIMESTAMP=$(date -u +%Y-%m-%dT%H-%M)
          STAGING_DIR="staging/$TIMESTAMP"
          mkdir -p "$STAGING_DIR"

          # 1. Compress master and compute checksums
          gzip -9 -c data/all_businesses.csv > "$STAGING_DIR/all_businesses.csv.gz"
          
          # 2. Generate cryptographically sound manifest.json
          python tools/generate_manifest.py \
            --data-dir data/ \
            --staging-dir "$STAGING_DIR" \
            --timestamp "$TIMESTAMP" \
            --run-id "$GITHUB_RUN_ID" \
            --commit "$GITHUB_SHA"

          # 3. Mirror snapshot to Primary (Google Drive)
          rclone copy "$STAGING_DIR" "gdrive:leads/snapshots/$TIMESTAMP/" \
            --drive-chunk-size 16M \
            --retries 3

          # 4. Mirror snapshot to Secondary Immutable Storage (Cloudflare R2)
          rclone copy "$STAGING_DIR" "r2:leadminer-backups/snapshots/$TIMESTAMP/" \
            --retries 3

          # 5. Atomic Active Master Update on Drive (with safety backup-dir)
          rclone copy data/ "gdrive:leads/" \
            --include "*.csv" \
            --backup-dir "gdrive:leads/prev_master/" \
            --drive-chunk-size 16M

          # 6. Enforce GFS retention pruning across both remotes
          python tools/prune_backups.py "gdrive:leads/snapshots/"
          python tools/prune_backups.py "r2:leadminer-backups/snapshots/"
```

### Hardening Google Drive Operations

1. **Replace OAuth Refresh Tokens with Service Account Keys:**
   - User OAuth tokens expire or fail when multi-factor authentication (MFA) changes.
   - Provision a dedicated Google Cloud Service Account with Google Drive API scope.
   - Share the `leads/` folder with the service account email.
   - Configure rclone using `service_account_file` in `rclone.conf`. Service account authentication does not expire and requires no interactive browser renewal.
2. **Leverage Native Drive Versioning:**
   - Google Drive automatically retains previous versions of files for 30 days (or up to 100 revisions) if files are updated rather than deleted.
   - Use `rclone copy` or `rclone copyto` to update `all_businesses.csv` in place instead of deleting and re-creating.
3. **Use `--backup-dir` on Every Sync:**
   - Never perform destructive overwrites on the root CSV files.
   - Supplying `--backup-dir gdrive:leads/prev_master/` forces rclone to move existing files into a timestamped rollback directory before writing new ones, providing zero-cost immediate rollback capabilities.
4. **Pre-Upload Volume Invariant Guard:**
   - Add a Python verification gate before uploading:
   ```python
   # tools/verify_volume.py
   import csv, sys

   def verify(old_master_path, new_master_path):
       with open(old_master_path, encoding="utf-8-sig") as f:
           old_count = sum(1 for _ in csv.DictReader(f))
       with open(new_master_path, encoding="utf-8-sig") as f:
           new_count = sum(1 for _ in csv.DictReader(f))
       
       # Master must never shrink by more than 5% without manual override
       if new_count < (old_count * 0.95):
           print(f"FATAL: Master record count collapsed from {old_count} to {new_count}!")
           sys.exit(1)
   ```

---

## Not a bug, but worth knowing

- **SQLite Migration Synergy:** When `leadminer` migrates from CSV to SQLite (`leads.db`) per Audit 033, the exact same GFS retention, manifest format, and dual-mirror architecture applies. SQLite files should be checkpointed with `PRAGMA wal_checkpoint(TRUNCATE)`, backed up via `sqlite3.Connection.backup()`, compressed with gzip/zstd, and synced using the exact same snapshot layout.
- **GDPR / Privacy Compliance vs Cold Storage:** Under GDPR and Saudi PDPL, businesses or individuals who exercise their "Right to Erasure" must be purged from active lead lists. When maintaining immutable GFS backups (WORM), historical snapshots cannot be mutated. Standard compliance pattern: maintain an encrypted `tombstones.json` file in active storage; when restoring from an old snapshot, automatically replay the tombstones file to re-delete records requested for erasure.
- **Google Drive Trashing Behavior:** `rclone delete` or `rclone purge` by default moves Drive files to the user's "Trash" bin, where they continue to consume storage quota for 30 days. To permanently purge items and instantly reclaim quota, pass the `--drive-use-trash=false` flag to rclone during automated pruning operations.
- **GitHub Actions Runner Disk Constraints:** Standard GitHub Actions runners provide ~14 GB of available disk space on the root filesystem. Storing uncompressed historical runs or staging multi-gigabyte files locally will exhaust the runner's disk. Compression and streaming to remote storage keep local disk utilization under 200 MB.

---

## Recommended order of work

### Phase 1: Critical Safety & Failure Prevention (Immediate)
1. **Remove `|| true` on Drive Download:**
   - In `.github/workflows/scrape.yml:38`, delete `|| true`.
   - Add explicit check: if `data/all_businesses.csv` is missing and this is not a designated initial bootstrap, fail the build immediately before scraping or writing.
2. **Add Concurrency Group:**
   - Add `concurrency: group: leadminer-scrape-prod; cancel-in-progress: false` to `.github/workflows/scrape.yml` to prevent race conditions from overwriting Drive.
3. **Configure `--backup-dir` in rclone:**
   - Update line 50 of `scrape.yml` to include `--backup-dir "gdrive:leads/prev_master/"` to ensure immediate rollback capability if the root master is overwritten.

### Phase 2: Retention Management & Storage Optimization (Days 1–3)
4. **Implement Compressed Snapshot Archiving:**
   - Update `main.py` or pipeline wrapper to compress `all_businesses.csv` to `all_businesses.csv.gz`.
   - Update upload steps to copy only `all_businesses.csv.gz` and `manifest.json` into `gdrive:leads/snapshots/$TIMESTAMP/`.
   - Keep derived CSVs (`qualified_businesses.csv`, `sales_ready.csv`, etc.) strictly in `gdrive:leads/` root.
5. **Implement `tools/generate_manifest.py`:**
   - Generate SHA-256 digests, row counts, schema definitions, and Git metadata on every run.
6. **Implement and Deploy `tools/prune_backups.py`:**
   - Add automated GFS pruning script to `.github/workflows/scrape.yml` running after the upload step with `--drive-use-trash=false`.

### Phase 3: Secondary Mirroring & Point-In-Time Recovery (Week 2)
7. **Deploy Secondary Object Storage (Cloudflare R2 / AWS S3):**
   - Provision an S3/R2 bucket `leadminer-backups` with Object Lock enabled for 90-day WORM retention.
   - Add R2 credentials to GitHub Secrets and configure `r2` remote in rclone.
   - Add secondary snapshot mirror step to `scrape.yml`.
8. **Switch from Google OAuth to Service Account:**
   - Generate a Google Cloud Service Account key, share the Drive folder, and configure rclone headless credentials to eliminate OAuth refresh token expiration risks.
9. **Build and Test `.github/workflows/restore.yml`:**
   - Implement the automated restoration script (`tools/restore.py`) and dispatch workflow.
   - Perform a recovery drill: wipe the active master, trigger the restore workflow, and verify that the master and all derived views are restored with 100% data integrity.
