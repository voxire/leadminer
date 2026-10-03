# 084 — Adversarial Review: Breakdown of Recent Core Fixes

## Verdict

Recent commits (`5222d32` and `8a42a67`) attempted to patch lead-signal inversion, cross-border phone mangling, dead country defaults, and non-atomic writes. Under adversarial scrutiny, several fixes fail on their own terms: `_fetch_website` still classifies WAF-blocked sites (HTTP 403/429) as `DEAD`, re-introducing the exact lead inversion it claimed to eliminate; `normalize_phone` collapses dummy zero-filled numbers into `+961` or `+966`, causing catastrophic deduplication merges across unrelated businesses; `resolve_country` misclassifies all domestic Saudi phone formats and explicit country names as Lebanon (`LB`); and an empty phone string (`""`) now leaks into production records where it corrupts pitch recommendations and completeness scoring.

---

## Findings

### S1 — `_fetch_website` Still Classifies WAF & Bot Protection (HTTP 403 / 429) as Dead Websites
- **Where:** `enricher.py:172-176`
- **Breaks:** Commit `5222d32` documented that conflating transport failures with dead websites inverted the core business proposition (ranking blocked sites above live ones). However, the new implementation checks:
  ```python
  status = r.status_code
  if status >= 400:
      return DEAD, contacts
  ```
  Web Application Firewalls (Cloudflare, AWS WAF, Akamai) and bot-defense layers do not drop connections or raise transport exceptions when challenging `requests`—they respond with HTTP status codes: **HTTP 403 Forbidden** (Cloudflare managed challenge / block) or **HTTP 429 Too Many Requests** (rate limiting). Because `status >= 400` treats every 4xx as `DEAD`, any active, high-traffic enterprise site that challenges our scraper is classified as `DEAD`. In `main.py` and `pitch_recommender.py`, these sites receive `website_live = False`, gain **+20 points** in `lead_score`, and are routed to sales reps with the recommendation `"Website rebuild + maintenance"`.
- **Trigger:** An active clinic or restaurant website protected by Cloudflare WAF returning HTTP 403 Forbidden to the standard Python User-Agent.
- **Fix:** Restrict `DEAD` exclusively to server-confirmed missing/gone resources, while routing challenges and rate limits to `UNKNOWN`:
  ```python
  if status in (404, 410):
      return DEAD, contacts
  if status in (403, 429) or status >= 500:
      return UNKNOWN, contacts
  ```

---

### S1 — `normalize_phone` Collapses Dummy Numbers (`"0000000"`) into `+961`/`+966`, Triggering Mass Dedup Merges
- **Where:** `dedup.py:50-63`
- **Breaks:** Commit `5222d32` aimed to prevent junk strings (like `"---"` or `"..."`) from normalizing to `"+"` and merging unrelated businesses under a shared phone key. However, if a record contains a dummy or placeholder number consisting of 7 or more zeros (frequently found in OSM and Google Places listings, e.g. `"0000000"` or `"000-0000"`), the new logic breaks catastrophically:
  ```python
  if digits.startswith("00"):
      digits = digits[2:]             # Drops first 2 zeros -> "00000"
  ...
  return "+" + cc + digits.lstrip("0") # digits.lstrip("0") becomes "" -> returns "+961"
  ```
  The function returns `"+961"` for Lebanon or `"+966"` for Saudi Arabia. In `dedup.py:110-115`, `key` evaluates to truthy `"+961"`. As a result, **every unrelated business with a placeholder of zeros merges into a single corrupted record** in `phone_index`.
- **Trigger:** Calling `normalize_phone("0000000", "LB")` evaluates `digits.lstrip("0")` to `""` and returns `"+961"`.
- **Fix:** Guard against empty stripped digits before returning the country code prefix:
  ```python
  stripped = digits.lstrip("0")
  if len(stripped) < 4:
      return ""
  return "+" + cc + stripped
  ```

---

### S1 — `resolve_country` Misclassifies Domestic Saudi Numbers and Explicit Country Names as Lebanon (`LB`)
- **Where:** `main.py:49-74`
- **Breaks:** Commit `8a42a67` introduced `resolve_country` to prevent `None` country fields from silently breaking region inference. However, its resolution logic is fatally narrow:
  1. **Saudi domestic numbers fail phone detection:** Saudi national mobile numbers begin with `05` (e.g. `050 123 4567`) and landlines begin with `011` / `012` / `013`. Google Places stores these in `nationalPhoneNumber` without international prefixes. `resolve_country` checks only:
     ```python
     if "+966" in phone or "00966" in phone:
         return "SA"
     ```
     Because neither substring is present, `resolve_country` returns `"LB"`.
  2. **Explicit full country names fail code check:** If a record carries `country: "Saudi Arabia"` or `"Kingdom of Saudi Arabia"` (standard in OSM tags and external datasets), `code in ("KSA", "SAU", "SAU-AR")` fails, defaulting the record to `"LB"`.
  3. **Coordinates are completely ignored:** A business located in central Riyadh (`lat: 24.71, lon: 46.68`) is tagged `"LB"`.
  4. **Cascade corruption:**
     - In `main.py:180`, `dedup` runs **before** `resolve_country`, defaulting `record.get("country") or "LB"` to `"LB"`.
     - In `main.py:194`, `normalize_phone("0501234567", country="LB")` prepends `+961`, permanently mutating a Saudi mobile into a corrupted Lebanese number: `+961501234567`.
     - In `enricher.py:326`, coordinate region inference tests Saudi coordinates against `_LB_COORD_REGIONS`, failing to infer any region.
- **Trigger:** `resolve_country({"country": "Saudi Arabia", "phone": "0501234567"})` returns `"LB"`.
- **Fix:** Parse full country strings, evaluate coordinates against geographic bounding boxes, and recognize domestic Saudi dialing patterns:
  ```python
  def resolve_country(record: dict) -> str:
      val = str(record.get("country") or "").strip().upper()
      if "SAUDI" in val or val in ("SA", "KSA", "SAU", "SAU-AR"):
          return "SA"
      if "LEBANON" in val or val in ("LB", "LEB", "LBN"):
          return "LB"
      lat = record.get("lat")
      if lat is not None and 16.0 <= float(lat) <= 32.2:
          return "SA"
      phone = re.sub(r"\D", "", str(record.get("phone") or ""))
      if phone.startswith(("966", "00966", "05", "5")) and len(phone) in (9, 10, 12, 14):
          return "SA"
      return _DEFAULT_COUNTRY
  ```

---

### S1 — Empty Phone String `""` Leaks into Records and Inverts Pipeline Execution Order
- **Where:** `main.py:190-195`, `scrapers/base.py:13`
- **Breaks:**
  1. **Schema violation and leakage:** `scrapers/base.py:13` defines `phone: str | None`. When a raw phone is invalid or short (e.g. `"12345"`, `"N/A"`), `normalize_phone` returns `""`. In `main.py:194`, `r["phone"] = normalize_phone(...)` assigns `""` to `r["phone"]` instead of `None`.
  2. **Pitch Recommender inverted by execution order:** In `main.py:190-195`, `recommend_service(r)` executes at line 191 **before** phone normalization at line 194. At line 191, `r.get("phone")` contains the raw junk string `"12345"`. Inside `pitch_recommender.py:38`, `has_phone = bool(record.get("phone"))` evaluates to `True`. The lead is assigned a phone-centric pitch:
     `"Starter web presence (domain + single-page landing + WhatsApp lead-gen)"` (Tier 6: *"nothing at all - no website, no social, just a phone number. Cold call or WhatsApp is the only route in"*).
     Three lines later (line 194), `r["phone"]` is set to `""`. The final output in `all_businesses.csv` and `qualified_businesses.csv` presents a business whose pitch explicitly recommends calling them, but whose phone column is blank.
  3. **Completeness score distortion:** In `enricher.py:103`, `completeness_score` awards +1 point for `r.get("phone")` because the raw junk string is present during enrichment, even though no valid phone survives into export.
  4. **CLI rescore divergence:** If `leadminer score` (`cli.py:cmd_score`) is run on the exported CSV, `load_master` converts `""` back to `None`. Rescoring the lead now evaluates `has_phone = False`, silently mutating `recommended_service` to a different pitch than what `main.py` originally produced.
- **Trigger:** Ingesting a record with `{"phone": "123", "website": None, "instagram": None}`.
- **Fix:** Normalize phone numbers before computing completeness, enrichment, and pitch recommendations, and coerce empty results to `None`:
  ```python
  r["phone"] = normalize_phone(raw_phone, r.get("country") or _DEFAULT_COUNTRY) or None
  ```

---

### S2 — Tri-State `website_live = None` Silently Disables Pitch Recommender Tiers 3 & 7
- **Where:** `pitch_recommender.py:34-36, 70-75, 95-99`
- **Breaks:** Commit `5222d32` set `website_live` to `None` for unreachable or transport-failed domains. In `pitch_recommender.py`:
  ```python
  website_live = record.get("website_live") is True
  website_dead = record.get("website_live") is False
  has_dead_website = has_website and website_dead
  ```
  When `website_live` is `None`:
  - `has_dead_website` is `False`.
  - `website_live` is `False`.
  - Tier 3 requires `has_website and website_live and review_count < 20`. This condition evaluates to `False`.
  - Tier 7 requires `has_website and website_live and has_social`. This condition evaluates to `False`.
  If the business is not in `_RTYLR_TARGETS` or `_LEAD_GEN_VERTICALS`, the function drops through every single tier and exits at line 99:
  `return "Discovery call - scope the right service"`.
  A mature business with 500 Google reviews, an active Instagram account, and an existing website that experienced a transient crawler timeout loses both its SEO audit and digital marketing retainer pitches, collapsing into an uninformative generic fallback.
- **Trigger:** A business record with `website="https://clinic.com"`, `website_live=None`, `review_count=10`, `instagram="clinic_lb"`.
- **Fix:** Decouple commercial pitch hypotheses from crawler transport certainty by checking `not website_dead` instead of `website_live is True` for SEO and marketing tiers, or introduce an explicit unreachable-site audit pitch.

---

### S2 — Unbounded `.tmp` File Accumulation and Leakage into Google Drive Backup
- **Where:** `main.py:121`, `.github/workflows/scrape.yml:48-50`
- **Breaks:** `write_csv` constructs temporary files alongside target CSVs:
  ```python
  tmp = path.with_suffix(path.suffix + ".tmp") # data/all_businesses.csv.tmp
  ```
  If GitHub Actions times out at 300 minutes, runner memory is exhausted (SIGKILL), or the workflow is cancelled, Python's `except BaseException` cleanup does not execute. The incomplete `.tmp` file remains in `data/`. In `.github/workflows/scrape.yml:49`:
  ```bash
  TIMESTAMP=$(date -u +%Y-%m-%dT%H-%M)
  rclone copy data/ "gdrive:leads/data/$TIMESTAMP/" --drive-chunk-size 8M
  ```
  Unlike line 50, line 49 **omits** `--include "*.csv"`. Any leftover `.tmp` files are synced directly into the timestamped production backup directory on Google Drive, permanently archiving partial, unvalidated data.
- **Trigger:** Runner timeout or OOM cancellation during `write_csv`.
- **Fix:** Place temporary files in a dedicated temporary directory outside `data/` (e.g. `tempfile.NamedTemporaryFile(dir=path.parent, prefix=".tmp_")`) and enforce `--include "*.csv"` on all rclone sync steps.

---

### S2 — `os.replace` Incompatible with FUSE / `rclone mount` / Shared Drive Filesystems
- **Where:** `main.py:130`
- **Breaks:** `write_csv` relies on `os.replace(tmp, path)` for atomic updates. When `data/` is backed by a virtual or remote filesystem (such as `rclone mount`, Google Drive for Desktop, or NFS/SMB shares):
  1. **Duplicate files on Google Drive:** Google Drive does not enforce POSIX unique-inode rename semantics. Renaming over an existing file often generates two files with identical names and distinct file IDs in the same folder.
  2. **Shared Drive permission failures:** In Google Drive Shared Drives, members assigned the standard "Contributor" role are permitted to create and edit files, but are forbidden from moving files to Trash or deleting them. Because `os.replace` unlinks the target file, the underlying filesystem driver attempts a file deletion, triggering `PermissionError: [Errno 13] Permission denied` and crashing the pipeline.
- **Trigger:** Executing `main.py` against a `data/` directory mounted via `rclone mount` on a Google Drive Shared Drive with Contributor permissions.
- **Fix:** Catch `OSError` (specifically `EXDEV` and `EPERM`) on `os.replace` and fall back to in-place truncation with explicit flush, or verify local POSIX filesystem backing.

---

### S3 — `enricher.py:326` Retains the Unfixed `r.get("country", "LB")` Bug
- **Where:** `enricher.py:326`
- **Breaks:** Commit `8a42a67` rightly noted that `r.get("country", "LB")` fails when `country` is `None` because `None` is an existing dictionary key. While `main.py` was updated to call `resolve_country`, `enricher.py:326` was overlooked:
  ```python
  r["region"] = infer_region(
      r.get("address"), r.get("lat"), r.get("lon"),
      country=r.get("country", "LB"),
  )
  ```
  If `enrich()` is called directly (by test suites, CLI tools, or external scripts) with records loaded from master where `country` is `None`, `country=None` is passed to `infer_region`. In `enricher.py:85`, `country == "LB"` evaluates to `False`, silently bypassing address-based region inference.
- **Trigger:** `enrich([{"address": "Hamra, Beirut", "country": None}])`.
- **Fix:** Update line 326 to use `country=r.get("country") or "LB"`.

---

### S3 — `normalize_phone` Erroneously Rewrites Local Lebanese Numbers Starting with `"20"` to Egypt (`+20`)
- **Where:** `dedup.py:20, 58-60`
- **Breaks:** `_KNOWN_COUNTRY_CODES` contains `"20"` (Egypt). In Lebanon, Greater Beirut (Achrafieh) landlines and commercial extensions without a trunk prefix frequently begin with `20` (e.g. `201-xxxx` or `20-xxxx`). When `normalize_phone("2012345", country="LB")` runs:
  - `digits = "2012345"` (length 7).
  - `digits.startswith("20")` matches Egypt in `_KNOWN_COUNTRY_CODES`.
  - The function returns `"+2012345"`, mislabeling a Lebanese business as Egyptian and corrupting cross-border dedup.
- **Trigger:** `normalize_phone("2012345", "LB")` returns `"+2012345"`.
- **Fix:** Only match known foreign country codes if the digit length is consistent with an international number (e.g. `len(digits) >= 10`), or prioritize the record's stated country calling code.

---

## Not a bug, but worth knowing

1. **`utf-8-sig` BOM Injection vs Non-BOM Aware Tools:** Commit `8a42a67` changed `write_csv` to write UTF-8 with a Byte Order Mark (`utf-8-sig`) to accommodate Excel. While `load_master` now reads with `utf-8-sig`, external downstream consumers using naive `open(..., encoding="utf-8")`, standard Linux utilities (`cut`, `awk`), or legacy pandas versions will read the first header as `\ufeffname`, potentially failing field-mapping lookups.
2. **`r.raw.read` Chunked Transfer Bypassing:** In `enricher.py:177`, `raw = r.raw.read(_MAX_BODY_BYTES, decode_content=True)` reads from urllib3's socket interface. If a web server returns `Transfer-Encoding: chunked` rather than a standard `Content-Length`, calling `.read()` on `r.raw` instead of using `r.iter_content()` can raise `urllib3.exceptions.ProtocolError` on incomplete chunks, forcing the fetcher into `UNKNOWN` even when the body was partially usable.

---

## Recommended order of work

1. **Fix `_fetch_website` status code discrimination (`enricher.py:172`):** Distinguish HTTP 403/429/WAF challenges from server-confirmed 404/410 dead responses.
2. **Fix `normalize_phone` placeholder zero-collapse (`dedup.py:63`):** Return `""` if stripped digits are empty, preventing `"+961"` mass dedup corruption.
3. **Overhaul `resolve_country` and move before dedup (`main.py:49, 180`):** Inspect coordinates and domestic Saudi prefixes (`05`, `011`), and run `resolve_country` before deduplication.
4. **Reorder phone normalization and pitch recommendation (`main.py:190-195`):** Normalize phones into `str | None` prior to computing completeness scores and pitch recommendations.
5. **Update `pitch_recommender.py` for tri-state liveness:** Ensure businesses with unreachable websites still receive appropriate SEO, reputation, or digital marketing pitches.
6. **Harmonize rclone copy filters and clean up temp files (`scrape.yml:49`, `main.py:121`):** Ensure backup copy steps enforce `--include "*.csv"` and isolate temporary write files.
