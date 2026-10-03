# 039 — GitHub Actions workflow hardening

## Verdict

The workflow is not safe for concurrent or unreliable runs: a failed Drive download is explicitly ignored, and a later upload can replace the cumulative master with incomplete data. It also leaves the Drive credential on disk throughout the job, relies on unspecified token permissions, and has no useful failure artifact. Make the workflow fail closed and serialize runs before enabling its promised monthly schedule.

## Findings

### S1 — A failed master download can silently destroy cumulative data
- **Where:** `.github/workflows/scrape.yml:35-38,46-50`
- **Breaks:** `rclone copy` errors are swallowed with `|| true`; the scraper then runs and the upload copies its results over the shared root CSVs. Since `all_businesses.csv` is the cumulative master (audit brief, “Output CSVs”), a transient Drive/auth/network error can result in a smaller or incomplete master replacing the current one without the run failing.
- **Trigger:** Drive is temporarily unavailable or credentials are invalid; `data/` starts empty, the download fails, and the scraper's partial output is uploaded as the new root CSV set.
- **Fix:** Remove `|| true`; fail on download errors and validate that the expected master was downloaded (or explicitly handle a deliberate first run) before scraping or uploading. Upload only after successful scrape and validation.

### S1 — Concurrent manual runs can lose each other's updates
- **Where:** `.github/workflows/scrape.yml:3-10,35-50`
- **Breaks:** There is no workflow/job concurrency group. Runs use separate runner filesystems, but both can download the same Drive snapshot, independently update it, and copy back to the same shared CSV names. The later upload can overwrite additions from the earlier run.
- **Trigger:** Two users click **Run workflow** before either run finishes.
- **Fix:** Add a stable `concurrency.group` for this scraper and set `cancel-in-progress: false` so runs do not overlap. Note GitHub retains at most one pending run per group; if every manual invocation must be preserved, use an external queue/lock rather than relying on Actions concurrency alone.

### S2 — The workflow does not declare least-privilege token permissions
- **Where:** `.github/workflows/scrape.yml:1-10`
- **Breaks:** With no `permissions` declaration, the job inherits the repository/organization default for `GITHUB_TOKEN`, which may be broader than checkout requires. A compromised action, dependency, or script could use any granted write permissions.
- **Trigger:** A dependency or workflow action is compromised while the job has write-capable default token access.
- **Fix:** Declare `permissions: contents: read` at workflow or job scope (checkout needs repository read access; this workflow does not commit changes). Keep any future permission additions narrowly scoped.

### S2 — The rclone credential remains as a plaintext file for the job
- **Where:** `.github/workflows/scrape.yml:28-33`
- **Breaks:** The full secret is written to `~/.config/rclone/rclone.conf` without an explicit restrictive umask, and the file remains available to all later steps, including the scraper and upload step. Hosted runners are ephemeral, but that does not prevent later job processes from reading the credential.
- **Trigger:** A compromised dependency or scraper code reads the runner's home directory after the setup step.
- **Fix:** Avoid a persistent setup step: create a temporary config with `umask 077`/mode `0600` only around each rclone operation, pass its path to rclone, and remove it with a shell `trap` on success or failure. Never print the secret or include the config in artifacts.

### S2 — rclone operations have no bounded duration
- **Where:** `.github/workflows/scrape.yml:35-38,46-50`
- **Breaks:** The download and both upload operations inherit only the 300-minute job timeout. A stalled transfer can consume most of the job window and leave the run without a timely outcome.
- **Trigger:** A Drive connection stalls or repeatedly retries during transfer.
- **Fix:** Set explicit, appropriate `timeout-minutes` on each rclone step and rclone connection/I/O timeouts (for example `--contimeout` and `--timeout`), with bounded retry counts. Keep the job timeout as an outer ceiling.

### S2 — Failed runs do not preserve diagnostic artifacts
- **Where:** `.github/workflows/scrape.yml:13-50` (no artifact step)
- **Breaks:** A failure leaves only transient Actions logs; there is no downloadable run artifact containing diagnostics or partial output for investigation/recovery.
- **Trigger:** A scrape or transfer fails after producing useful logs or partial CSVs.
- **Fix:** Add an `actions/upload-artifact` step guarded by `if: failure()` (or `if: always()` for both outcomes) to preserve a diagnostic log and, if needed, partial CSVs with short retention. Restrict access and do not package secrets/config; business contact data in CSVs should only be retained when necessary.

### S2 — README promises a monthly run that the workflow never schedules
- **Where:** `.github/workflows/scrape.yml:3-6`; `README.md:5,74-77`
- **Breaks:** The workflow has only `workflow_dispatch`; its commented-out cron is weekly Monday, while the README promises the first day of each month at 03:00 UTC. No automatic monthly run occurs.
- **Trigger:** The team relies on the documented monthly refresh without manually starting a run.
- **Fix:** Decide which cadence is intended; if README is correct, enable `schedule: - cron: '0 3 1 * *'` and retain `workflow_dispatch`. Otherwise correct the README and remove the misleading commented cron. Scheduled workflows run from the default branch.

### S3 — Action and bootstrap tool versions are mutable
- **Where:** `.github/workflows/scrape.yml:15,18,24-26`
- **Breaks:** `actions/checkout@v4`, `actions/setup-python@v5`, and `pip install uv` select mutable action tags/latest package versions, so the executed workflow can change without a repository change.
- **Trigger:** An upstream tag or newly released `uv` version changes or is compromised between runs.
- **Fix:** Pin actions to reviewed full commit SHAs (with a version comment) and pin the `uv` version; update these pins deliberately.

## Hardened version outline

1. Set least privilege (`permissions: contents: read`) and serialize runs with a stable concurrency group and `cancel-in-progress: false`.
2. Pin action references to full SHAs; pin the `uv` bootstrap version.
3. Add explicit timeouts to download/upload steps and rclone network timeouts/retry limits.
4. For each rclone operation, create a mode-`0600` temporary config from the secret, trap cleanup, and never leave it available to the scraper step.
5. Make download failure fatal; verify expected input files before running. Make scrape/validation failure fatal and do not update canonical Drive CSVs on failure. Consider uploading to a temporary/versioned destination, then promoting only after validation to avoid partial canonical updates.
6. Add a failure diagnostic artifact step with short retention and restricted contents/access; exclude credentials and avoid retaining lead CSVs unless needed.
7. Align the actual `schedule` with the README's monthly cadence (or correct the README), while keeping manual dispatch.

## Not a bug, but worth knowing

- `timeout-minutes: 300` already bounds the whole job, but is too coarse to provide an operational bound for an individual rclone transfer.
- The timestamped upload at line 49 is useful for recovery, but it happens before the canonical upload and does not make the latter atomic. A failed/partial canonical copy still needs validation and a promotion strategy.

## Recommended order of work

1. Remove the ignored-download failure and add input/output validation so an incomplete run cannot replace the master.
2. Serialize runs and explicitly restrict token permissions.
3. Scope the rclone credential to each transfer; add transfer timeouts and bounded retries.
4. Add safe failure diagnostics, then align and enable the intended monthly schedule.
5. Pin actions and the `uv` bootstrap version.
