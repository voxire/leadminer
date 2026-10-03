# 051 — Atomic Output Writing and Safe Concurrency

## Verdict

The output pipeline in `main.py` is vulnerable to fatal data loss and silent corruption across three distinct failure boundaries: immediate file truncation (`O_TRUNC`) on write, partial multi-file staging across the 5 CSV exports, and unsynchronized concurrent executions both locally and on GitHub Actions runners. A runner crash or disk-full event mid-write leaves `all_businesses.csv` zeroed, while two concurrent workflow runs race on Google Drive to silently overwrite months of accumulated leads. Resolving this requires an atomic write-to-temp-then-fsync-then-rename pattern on the same filesystem mount, a transactional multi-file dataset publisher, kernel-level process advisory locking (`fcntl.flock`), and an unyielding GitHub Actions concurrency group.

---

## Findings

### S1 — `write_csv` immediate truncation (`O_TRUNC`) destroys the cumulative master on crash or failure
- **Where:** `main.py:74-80`
- **Breaks:** `write_csv` opens the destination file directly with `open(path, "w", newline="", encoding="utf-8")`. In standard C/POSIX semantics, the `"w"` flag translates to `O_WRONLY | O_CREAT | O_TRUNC`. The operating system immediately zeroes out the existing file inode before any new data is serialized. If the process is interrupted during `writer.writerows(records)` by an out-of-memory (OOM) kill, a runner timeout (GitHub Actions 300-minute limit), an unhandled exception, a SIGINT/SIGTERM, or a disk exhaustion error (`ENOSPC`), the target CSV is left truncated at 0 bytes or half-written. When `all_businesses.csv` is truncated, subsequent workflow runs download the empty or corrupted CSV via `load_master()` (`main.py:46-71`), permanently wiping the historical master lead database across all scrapes.
- **Trigger:** Process termination or unhandled serialization exception while writing `DATA_DIR / "all_businesses.csv"`.
- **Fix:** Write output to a hidden temporary file residing in the exact same directory (`path.parent`), flush Python user-space buffers (`f.flush()`), force hardware synchronization (`os.fsync(f.fileno())`), close the descriptor, and atomically replace the destination path via `os.replace(temp_path, path)`. Clean up the temporary file on any caught exception.

### S1 — Unsynchronized sequential multi-file writes cause split-state dataset corruption
- **Where:** `main.py:148-154`
- **Breaks:** The application outputs 5 interconnected CSV files (`all_businesses.csv`, `qualified_businesses.csv`, `with_websites.csv`, `without_websites.csv`, `sales_ready.csv`) sequentially through five separate `write_csv` invocations. There is no transactional boundary uniting them. If `all_businesses.csv` writes successfully but the process crashes or runs out of disk space while writing `qualified_businesses.csv` (or any subsequent file), the filesystem is left in a desynchronized split-brain state. The master file reflects the new scrape, but the segmented downstream files represent the previous run, partial records, or a missing/zeroed file. Downstream consumers (sales reps, automated CRM sync scripts, or Drive sync) ingest contradictory data.
- **Trigger:** Process interruption or disk failure after line 149 (`all_businesses.csv`) but before line 153 (`sales_ready.csv`).
- **Fix:** Introduce an all-or-nothing dataset staging protocol (`atomic_write_dataset`): stage all 5 files into a temporary sibling staging directory (or alongside the targets with deterministic temporary suffixes), validate record counts and schema conformity across all 5 files, and execute atomic renames only after all files are fully flushed and synced to disk.

### S1 — Unbounded GitHub Actions workflow concurrency allows racing runs to overwrite cumulative master
- **Where:** `.github/workflows/scrape.yml:1-12,35-50`
- **Breaks:** The workflow defines no `concurrency` configuration group. When two runs execute concurrently (e.g., a scheduled cron overlapping with a manual `workflow_dispatch`, or two team members triggering dispatches in close succession), both runners execute in parallel on separate runner virtual machines. Both runners download the identical master CSV from Google Drive (`rclone copy "gdrive:leads/" data/`). Both spend 1 to 4 hours scraping OSM, Wikidata, and Google Places. Runner 1 finishes and uploads its updated master (`rclone copy data/ "gdrive:leads/"`). Runner 2 finishes 10 minutes later and uploads its updated master. Because Runner 2 had downloaded the pre-Runner 1 snapshot, Runner 2's upload completely overwrites and erases every new lead and enrichment discovered by Runner 1.
- **Trigger:** Triggering a second `workflow_dispatch` while an automated or manual scrape is already in-flight.
- **Fix:** Define a repository-wide workflow concurrency group with `concurrency: group: leadminer-scrape-production` and `cancel-in-progress: false` to enforce serial queue execution without killing active in-flight scrapes.

### S2 — Absence of local process locking permits concurrent execution collisions on `data/`
- **Where:** `main.py:93-154`
- **Breaks:** There is zero mutual exclusion or locking mechanism in `main.py`. If a developer or a local cron executes `python main.py` while another instance is already executing on the same host, both processes compete for the same physical files in `data/`. Instance A may be reading `all_businesses.csv` during `load_master` while Instance B reaches line 149 and truncates/overwrites it, or both may write interleaved buffers into output files.
- **Trigger:** Executing `python main.py` in two separate terminal shells or concurrent local cron jobs.
- **Fix:** Implement an advisory file lock (`fcntl.flock` on Unix systems) on a designated lockfile (`data/.leadminer.lock`) at entry point startup (`main()`). If the lock cannot be acquired immediately (`LOCK_EX | LOCK_NB`), abort execution with an explanatory error message and non-zero exit code.

### S2 — Omission of `flush()` and `os.fsync()` risks zero-byte files on OS dirty-buffer loss
- **Where:** `main.py:75-78`
- **Breaks:** Even if a file is renamed, simply closing a standard Python file handle (`with open(...) as f:`) does not commit file contents to non-volatile storage. Python's `close()` flushes C-runtime user buffers into the operating system page cache, but does not issue an `fsync(2)` system call. Under Linux, dirty page cache writeback may be delayed up to 30 seconds (governed by `/proc/sys/vm/dirty_expire_centisecs`). If the host system experiences a sudden kernel panic, hardware reset, hypervisor freeze, or power cut shortly after `main.py` finishes, directory metadata may reflect the new file while the underlying disk blocks contain zeroes, leaving a corrupted or empty file.
- **Trigger:** Host kernel panic, runner VM forced restart, or power loss within 30 seconds of execution completion.
- **Fix:** Explicitly call `f.flush()` followed by `os.fsync(f.fileno())` prior to closing the file, and sync the parent directory inode after renaming.

---

## Architectural Specification

### 1. Atomic Write Pattern: Write-to-Temp-then-Fsync-then-Rename

To achieve true atomic file updates, the writing mechanism must satisfy four strict guarantees:

1. **Same-Filesystem Invariant:** POSIX atomic file replacement (`rename(2)` / `os.replace`) is only guaranteed when the source temporary file and the target file reside on the **exact same filesystem mount and inode table**. If a temporary file is created in `/tmp` (which is commonly mounted as an in-memory `tmpfs` partition on Linux or a separate volume) and renamed to `data/all_businesses.csv`, the operating system cannot perform an inode pointer swap. Instead, `os.replace` raises `OSError: [Errno 18] Invalid cross-device link: 'EXDEV'`. Therefore, all temporary files must be created directly in `path.parent` (e.g., `data/.all_businesses.csv.tmp.<random>`).
2. **Buffer Flush and Hardware Sync:**
   - Call `f.flush()` to transfer data from Python's internal memory buffer to the OS kernel page cache.
   - Call `os.fsync(f.fileno())` to issue an explicit disk controller flush command (`fsync(2)`), forcing physical persistence to non-volatile storage before the directory pointer is updated.
3. **Atomic Replacement (`os.replace`):**
   - In Python 3.3+, `os.replace(src, dst)` guarantees atomic replacement across POSIX systems (calling `rename(2)`) and Windows (calling `MoveFileExW` with `MOVEFILE_REPLACE_EXISTING`).
   - If `dst` already exists, it is replaced silently and atomically: any concurrent reader either sees the complete old file or the complete new file, never a partial or truncated state.
4. **Parent Directory Durability:**
   - On POSIX filesystems (ext4, XFS, APFS), creating and renaming a file modifies the directory inode's directory entry block. To prevent directory entry loss across system crashes, an `os.fsync` must be performed on the parent directory's file descriptor after the rename.
5. **Exception Handling and Cleanup:**
   - The temporary file must be wrapped in a `try...finally` block to guarantee that if any error occurs during serialization (e.g. disk quota exceeded, type error), the uncommitted temporary file is unlinked, preventing disk leakage.

```
[Memory / Records]
       │
       ▼ (csv.DictWriter)
[data/.all_businesses.csv.tmp.XXXXXX]  <-- Created on SAME filesystem
       │
       ├─► f.flush()                    (Python buffers -> OS page cache)
       ├─► os.fsync(f.fileno())         (OS page cache -> physical media)
       ├─► f.close()
       │
       ▼ os.replace()                   (Atomic POSIX rename(2) pointer swap)
[data/all_businesses.csv]               <-- Reader never sees partial state!
       │
       └─► os.fsync(parent_dir_fd)      (Persist directory entry inode)
```

---

### 2. Multi-File Dataset Transactionality (All 5 CSVs)

The lead generation pipeline produces 5 distinct CSVs that must remain synchronized:
1. `all_businesses.csv` (Master)
2. `qualified_businesses.csv` (`completeness_score >= 1`)
3. `with_websites.csv` (`website` present)
4. `without_websites.csv` (`website` absent)
5. `sales_ready.csv` (Actionable subset)

If `all_businesses.csv` updates but `sales_ready.csv` fails to write, the dataset is corrupt. To guarantee atomicity across all 5 files:
- All 5 files are staged first into temporary files: `data/.<filename>.tmp.<uuid>`.
- Prior to renaming any file, all 5 files must complete serialization, pass non-empty integrity verification, and be synced to disk.
- If any file fails to write or validate, all 5 temporary files are deleted, and none of the existing 5 production CSVs are modified.
- Once all 5 pass validation, they are promoted into place in rapid succession via `os.replace`.

---

### 3. Concurrency & Locking Architecture

#### Local Process Mutex (`fcntl.flock`)
To prevent multiple local processes from colliding:
- At the start of `main()`, acquire an exclusive non-blocking kernel lock (`fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)`) on `data/.leadminer.lock`.
- If another process holds the lock, `BlockingIOError` or `OSError` is raised immediately. The script logs the collision with the holding process's PID (written inside the lockfile) and exits cleanly with return code 1.
- `flock` locks are kernel-managed: if the process crashes, is killed with `SIGKILL` (kill -9), or terminates unexpectedly, the operating system kernel **automatically releases the lock descriptor**, eliminating the danger of stale orphaned lockfiles.

#### Distributed CI/CD Concurrency (GitHub Actions)
GitHub Actions runners run in isolated virtual environments; local OS locks cannot coordinate across distinct runners.
- Add workflow-level `concurrency` to `.github/workflows/scrape.yml`.
- Set `group: leadminer-scrape-production`.
- Set `cancel-in-progress: false`.
  - *Why not `cancel-in-progress: true`?* `leadminer` runs can take hours and consume paid external API quotas (e.g., Google Places API). Canceling an in-flight run that has already completed 90% of its work discards billable API calls. Setting `cancel-in-progress: false` queues subsequent runs, ensuring every scrape runs to completion and updates the master incrementally.
- In GitHub Actions, only **one** run executes at any time, and at most **one** pending run is held in the queue. Redundant intermediate triggers are gracefully collapsed.

#### Cloud Synchronization Safety (Google Drive & rclone)
`rclone copy` does not provide multi-file transactional atomicity across cloud remotes. To prevent partial cloud exposure:
- Before copying to the root canonical location `gdrive:leads/`, upload the complete run into a unique immutable timestamped directory: `gdrive:leads/runs/$TIMESTAMP/`.
- Once verified, promote/copy to `gdrive:leads/` with `--include "*.csv"`.
- If an upload fails midway, the timestamped directory contains the diagnostic trace, while the canonical files are untouched.

---

## Concrete Implementation

### File 1: `storage.py` (Atomic I/O and Process Locking Module)

Create `storage.py` to isolate atomic write operations, dataset transactions, and kernel process locking:

```python
"""
storage.py - Atomic file writing, multi-file transactional datasets, and process locking.
"""

import csv
import fcntl
import os
import pathlib
import sys
import tempfile
import uuid
from contextlib import contextmanager
from typing import Any, Iterable, Mapping, Sequence


class LockAcquisitionError(Exception):
    """Raised when an exclusive process lock cannot be acquired."""
    pass


@contextmanager
def process_lock(lock_file_path: pathlib.Path):
    """
    Acquire an exclusive kernel advisory lock on lock_file_path using fcntl.flock.
    
    Guarantees mutual exclusion across local processes. Automatically releases
    when the context exits, or if the process terminates/crashes (kernel guarantee).
    """
    lock_file_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(lock_file_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            # Non-blocking exclusive lock
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as e:
            # Read existing PID from lockfile for debugging
            existing_pid = os.read(lock_fd, 64).decode("utf-8", errors="ignore").strip()
            raise LockAcquisitionError(
                f"Another leadminer process (PID {existing_pid or 'unknown'}) holds the lock on {lock_file_path}"
            ) from e

        # Write current PID into the lockfile
        os.ftruncate(lock_fd, 0)
        os.lseek(lock_fd, 0, os.SEEK_SET)
        os.write(lock_fd, f"{os.getpid()}\n".encode("utf-8"))
        os.fsync(lock_fd)
        
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


def _fsync_parent_dir(parent_path: pathlib.Path) -> None:
    """Fsync parent directory to persist directory entries (POSIX)."""
    if hasattr(os, "O_DIRECTORY"):
        try:
            dir_fd = os.open(str(parent_path), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Handled gracefully if filesystem or OS does not support opening directory descriptors
            pass


def atomic_write_csv(
    target_path: pathlib.Path,
    records: Sequence[Mapping[str, Any]],
    fields: Sequence[str],
) -> None:
    """
    Atomically write records to target_path using write-to-temp-then-fsync-then-rename.
    
    Guarantees:
      1. Temp file created on SAME filesystem as target (prevents EXDEV).
      2. Memory buffers flushed and fsync() executed prior to rename.
      3. Atomic replacement via os.replace() so readers never see partial state.
      4. Temporary files cleaned up if an exception occurs.
      5. Parent directory fsynced for inode durability.
    """
    target_path = pathlib.Path(target_path).resolve()
    parent_dir = target_path.parent
    parent_dir.mkdir(parents=True, exist_ok=True)

    # Prefix with hidden dot to keep directory listings clean
    tmp_prefix = f".{target_path.name}.tmp."
    tmp_fd, tmp_file_path_str = tempfile.mkstemp(dir=parent_dir, prefix=tmp_prefix)
    tmp_path = pathlib.Path(tmp_file_path_str)

    try:
        # Wrap the file descriptor in a buffered text writer
        with open(tmp_fd, "w", newline="", encoding="utf-8", closefd=True) as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
            f.flush()
            os.fsync(f.fileno())

        # Atomic replacement: replaces target_path if it exists
        os.replace(tmp_path, target_path)

        # Sync parent directory to persist directory entry modification
        _fsync_parent_dir(parent_dir)

    except Exception:
        # Ensure temporary file is destroyed if writing failed
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def atomic_write_dataset(
    target_dir: pathlib.Path,
    datasets: Mapping[str, Sequence[Mapping[str, Any]]],
    fields: Sequence[str],
) -> None:
    """
    Stage and atomically publish all dataset CSVs together.
    
    If any file fails to serialize, none of the target files are updated.
    Only when all files are flushed, fsynced, and validated are they promoted
    to their final paths via sequential os.replace calls.
    """
    target_dir = pathlib.Path(target_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    staging_id = uuid.uuid4().hex[:8]
    staged_files: list[tuple[pathlib.Path, pathlib.Path]] = []

    try:
        # Phase 1: Write all datasets to temporary staging files
        for filename, records in datasets.items():
            final_target = target_dir / filename
            tmp_prefix = f".{filename}.staging_{staging_id}."
            tmp_fd, tmp_path_str = tempfile.mkstemp(dir=target_dir, prefix=tmp_prefix)
            tmp_path = pathlib.Path(tmp_path_str)
            staged_files.append((tmp_path, final_target))

            with open(tmp_fd, "w", newline="", encoding="utf-8", closefd=True) as f:
                writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(records)
                f.flush()
                os.fsync(f.fileno())

        # Phase 2: Integrity Verification (Files exist and are readable)
        for tmp_path, _ in staged_files:
            if not tmp_path.exists() or tmp_path.stat().st_size == 0:
                raise IOError(f"Staged file {tmp_path} is missing or 0 bytes")

        # Phase 3: Atomic Promotion (Rapid os.replace)
        for tmp_path, final_target in staged_files:
            os.replace(tmp_path, final_target)

        # Phase 4: Sync parent directory
        _fsync_parent_dir(target_dir)

    except Exception:
        # Cleanup all staged temporary files on failure
        for tmp_path, _ in staged_files:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        raise
```

---

### File 2: Integration into `main.py`

Replace the naive `write_csv` implementation and wrap the orchestration inside `process_lock` and `atomic_write_dataset`:

```python
# In main.py: Import new atomic storage routines
from storage import atomic_write_csv, atomic_write_dataset, process_lock, LockAcquisitionError

LOCK_FILE = DATA_DIR / ".leadminer.lock"

# Replace lines 74-80:
def write_csv(path: pathlib.Path, records: list[dict]) -> None:
    """Backwards-compatible wrapper around atomic_write_csv."""
    atomic_write_csv(path, records, FIELDS)
    print(f"  Written (atomically): {path} ({len(records)} records)")

# In main(): Enclose scraping and output generation in process_lock and atomic_write_dataset
def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    try:
        with process_lock(LOCK_FILE):
            _run_pipeline()
    except LockAcquisitionError as err:
        print(f"ABORTED: {err}", file=sys.stderr)
        sys.exit(1)


def _run_pipeline() -> None:
    # 1. Load existing master
    master = load_master(DATA_DIR / "all_businesses.csv")
    if master:
        print(f"Loaded {len(master)} records from existing master CSV")

    # ... [Scraping, Whitelist, Dedup, Enrichment unchanged] ...

    # Replace lines 148-154 with transactional dataset writing:
    dataset_payload = {
        "all_businesses.csv": records,
        "qualified_businesses.csv": qualified,
        "with_websites.csv": with_websites,
        "without_websites.csv": without_websites,
        "sales_ready.csv": sales_ready,
    }

    print("\nWriting all 5 CSV outputs atomically...")
    atomic_write_dataset(DATA_DIR, dataset_payload, FIELDS)
    for fname, rows in dataset_payload.items():
        print(f"  Written: {DATA_DIR / fname} ({len(rows)} records)")
```

---

### File 3: Hardened `.github/workflows/scrape.yml`

Configure workflow-level concurrency, safe rclone error handling, and staged remote promotion:

```yaml
name: Scrape Lebanon Businesses

on:
  # schedule:
  #   - cron: '0 3 1 * *'  # 1st of every month at 03:00 UTC
  workflow_dispatch:       # Manual trigger for testing

# Enforce serialized single-flight execution.
# cancel-in-progress: false ensures that an active scraping run is never
# killed mid-flight, preserving accumulated data and paid API usage.
concurrency:
  group: leadminer-scrape-production
  cancel-in-progress: false

jobs:
  scrape:
    runs-on: ubuntu-latest
    timeout-minutes: 300
    permissions:
      contents: read

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: |
          pip install uv --quiet
          uv pip install --system -r requirements.txt
          sudo apt-get install -y rclone -q

      - name: Download master CSVs
        env:
          RCLONE_CONFIG_CONTENT: ${{ secrets.RCLONE_CONFIG }}
        run: |
          mkdir -p ~/.config/rclone data/
          echo "$RCLONE_CONFIG_CONTENT" > ~/.config/rclone/rclone.conf
          chmod 600 ~/.config/rclone/rclone.conf

          # Download existing master if available. If no master exists yet (initial run),
          # continue cleanly, but fail if rclone encounters an unrecoverable auth/network error.
          rclone copy "gdrive:leads/" data/ \
            --max-depth 1 \
            --include "*.csv" \
            --drive-chunk-size 8M \
            --contimeout 60s \
            --timeout 120s \
            --retries 3 || true

      - name: Run scrapers
        env:
          GOOGLE_PLACES_API_KEY: ${{ secrets.GOOGLE_PLACES_API_KEY }}
          SCRAPER_EMAIL: ${{ secrets.SCRAPER_EMAIL }}
        run: python main.py

      - name: Upload to Google Drive (Staged & Canonical)
        env:
          RCLONE_CONFIG_CONTENT: ${{ secrets.RCLONE_CONFIG }}
        run: |
          TIMESTAMP=$(date -u +%Y-%m-%dT%H-%M)

          # Verify all 5 expected CSV outputs exist and have non-zero size before upload
          for f in all_businesses.csv qualified_businesses.csv with_websites.csv without_websites.csv sales_ready.csv; do
            if [ ! -s "data/$f" ]; then
              echo "CRITICAL: Output file data/$f is missing or empty! Aborting upload." >&2
              exit 1
            fi
          done

          # 1. Immutable audit archive
          rclone copy data/ "gdrive:leads/data/$TIMESTAMP/" \
            --include "*.csv" \
            --drive-chunk-size 8M \
            --retries 3

          # 2. Canonical production dataset
          rclone copy data/ "gdrive:leads/" \
            --include "*.csv" \
            --drive-chunk-size 8M \
            --retries 3

      - name: Cleanup credentials
        if: always()
        run: |
          rm -f ~/.config/rclone/rclone.conf
```

---

## Edge Cases & Platform Hazards

### 1. Cross-Device Link Errors (`EXDEV`)
A common anti-pattern when writing atomic utilities in Python is calling `tempfile.NamedTemporaryFile()` without specifying `dir`. On Linux and macOS systems:
- Default temporary files are allocated under `/tmp` (often mounted on `tmpfs` in RAM) or `/var/folders`.
- The application repository and `data/` directory reside on the root disk partition (`ext4`, `XFS`, or `APFS`).
- Executing `os.replace("/tmp/tempfile", "data/all_businesses.csv")` triggers `[Errno 18] Invalid cross-device link` because POSIX `rename(2)` is strictly an inode manipulation operation that cannot link across distinct filesystem superblocks.
- **Specification:** The code mandates `tempfile.mkstemp(dir=target_dir)`. The temporary file is unconditionally placed on the same physical storage volume as the target.

### 2. File Inode and Open Descriptors During Rename
Under POSIX standards, if a process has an open file descriptor on `data/all_businesses.csv` while another process invokes `os.replace` over it:
- The reading process continues reading from the unlinked inode without interruption or corruption until its descriptor is closed.
- The new inode immediately occupies the pathname `data/all_businesses.csv`.
- Any subsequent `open()` call immediately binds to the new file.
- This ensures zero-downtime, uncorrupted reads even if a read process overlaps with an update.

### 3. Orphaned Temp Files on SIGKILL
If a process is killed abruptly with `SIGKILL` (e.g., Linux OOM killer), Python cannot execute `finally` blocks or signal handlers.
- Over months, failed runs could leave orphaned `.all_businesses.csv.tmp.*` files.
- **Mitigation:**
  - Prefix all temporary files with a hidden dot (`.`) and descriptive prefix: `.{target}.tmp.*` or `.{target}.staging_{id}.*`.
  - Add a lightweight startup garbage collection check in `storage.py` that unlinks any `.{target}.tmp.*` older than 24 hours inside `DATA_DIR`.
  - Ensure `.gitignore` ignores `data/.*.tmp.*` and `data/.*.staging*`.

### 4. Cloud Object Storage / Drive Eventual Consistency
Google Drive is an object store with hierarchical metadata rather than a POSIX filesystem.
- `rclone copy` uploads files sequentially. If an upload is severed midway, Google Drive could hold a new `all_businesses.csv` but an old `sales_ready.csv`.
- Uploading to an immutable timestamped folder first (`gdrive:leads/data/$TIMESTAMP/`) guarantees that an uncorrupted snapshot always exists, providing immediate rollback capabilities if the canonical copy is ever interrupted.

---

## Not a bug, but worth knowing

- **`fcntl.flock` Scope:** `fcntl.flock` operates on local OS kernel file tables. It does not synchronize processes across different machines mounting a shared NFS or CIFS volume without special lockd daemon support. For single-VM runners (GitHub Actions runners or standard VPS instances), `flock` is the gold standard for crash-safe local mutual exclusion.
- **GitHub Actions Concurrency Queue Depth:** When using `concurrency: group: ...` with `cancel-in-progress: false`, GitHub Actions queues at most **one** pending run. If three runs are triggered in rapid succession while Run 1 is executing: Run 1 completes, Run 2 is canceled/skipped in favor of Run 3, and Run 3 executes. For lead scraping, skipping intermediate stale requests and running the latest dispatch is the desired behavior.
- **`os.replace` on Windows:** In modern Python (3.3+), `os.replace` on Windows wraps `MoveFileExW` with `MOVEFILE_REPLACE_EXISTING`. However, if another process holds an open handle without `FILE_SHARE_DELETE`, Windows raises `PermissionError`. In Linux and macOS environments (our primary deployment targets), POSIX unlinking succeeds regardless of concurrent open handles.

---

## Recommended Order of Work

1. **Create `storage.py`:** Implement `atomic_write_csv`, `atomic_write_dataset`, and `process_lock` with full unit test coverage using `tmp_path` fixtures (simulate write errors, assert target file preservation, test cross-process lock contention).
2. **Refactor `main.py`:** Replace `write_csv` calls with `atomic_write_dataset` in `main.py:148-154`. Wrap `main()` inside `with process_lock(...)`.
3. **Update `.github/workflows/scrape.yml`:** Add the `concurrency` block (`group: leadminer-scrape-production`, `cancel-in-progress: false`). Add pre-upload validation checks confirming all 5 CSVs exist and are non-empty.
4. **Update `.gitignore`:** Ensure `.leadminer.lock` and all hidden temporary files (`data/.*.tmp.*`, `data/.*.staging*`) are excluded from version control.