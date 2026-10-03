# 028 — CSV Round-Trip Fidelity and Cumulative Master Drift

## Verdict

The flat record dictionary functions as the de facto database schema in `leadminer`, but lacks schema enforcement, type validation, or drift protection. Across an isolated single cycle (`write_csv` → `load_master`), RFC 4180 quoting and UTF-8 preserve floats, ints, embedded punctuation, and Arabic RTL characters; however, empty strings (`""`) are irreversibly mutated to `None`, and booleans survive only if they match exact Python string literals (`"True"` / `"False"`). In production, because `data/all_businesses.csv` is an unversioned cumulative master file merged weekly, three compounding failure modes degrade the dataset: an Excel/Sheets UTF-8 BOM wipes the primary `name` column across the entire dataset (S1), disconnected schema definitions silently purge unlisted columns (S2), and asymmetrical merge weighting permanently locks in stale master values over fresh scrapes (S2).

---

## Findings

### S1-1 — UTF-8 BOM on master CSV silently wipes the entire `name` column
- **Where:** `main.py:51` (`open(path, newline="", encoding="utf-8")`) and `main.py:76` (`csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")`).
- **Breaks:** `all_businesses.csv` is synced to Google Drive via rclone for sales consumption (`main.py:8-12`, `BRIEF.md:37`). If an operator or sales rep opens the file in Excel or Google Sheets and saves it, standard spreadsheet software prepends a UTF-8 Byte Order Mark (`\xef\xbb\xbf` / `\ufeff`). Because `load_master` reads using standard `"utf-8"`, `csv.DictReader` parses the first header key as `"\ufeffname"` instead of `"name"`. Every loaded row contains `{..., '\ufeffname': 'Acme Corp', ...}` while `row.get("name")` evaluates to `None`. On line 76, `write_csv` writes `FIELDS[0]` (`"name"`), finds `None` (written as `""`), and ignores `"\ufeffname"` due to `extrasaction="ignore"`. **The business names of every historical record in the cumulative master are permanently wiped to empty strings in a single run.**
- **Trigger:** Re-saving `all_businesses.csv` in Excel or exporting from Google Sheets:
  ```python
  bom_csv = '\ufeffname,category,region\n"Al Sultan Bakery",bakery,Beirut\n'
  # DictReader yields keys: ['\ufeffname', 'category', 'region']
  # row.get("name") -> None
  # write_csv writes name as "" and discards '\ufeffname'
  ```
- **Fix:** In `main.py:51`, open with `encoding="utf-8-sig"` to transparently strip any leading BOM without altering ASCII or UTF-8 reads.

---

### S2-1 — Dual schema divergence and `extrasaction="ignore"` permanently discard columns
- **Where:** `main.py:76` (`writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")`), `main.py:32-39` (`FIELDS`), and `scrapers/base.py:5-28` (`BusinessRecord`).
- **Breaks:** The pipeline has two uncoupled schema definitions: `BusinessRecord` (23 typed keys in `scrapers/base.py`) and `FIELDS` (23 string keys in `main.py`). `csv.DictWriter` is configured with `extrasaction="ignore"`.
  1. If an engineer adds an enrichment field to `BusinessRecord` (e.g. `opening_hours`, `place_id`, `verified_phone`, or `menu_url`) and emits it from a scraper, but does not update `FIELDS` in `main.py`, the new field is discarded silently on every write.
  2. If an external process or previous release populated extra columns in `all_businesses.csv`, `load_master` reads them into the row dict, `dedup._merge` preserves them via `all_keys = set(a) | set(b)` (`dedup.py:46`), but `write_csv` erases them permanently when overwriting the cumulative master.
- **Trigger:** Adding a field `verified: bool` to scraper outputs without updating `main.py:FIELDS`. The data is collected, passes through dedup and enrichment, but is dropped at `write_csv`.
- **Fix:** Single-source the schema from `BusinessRecord.__annotations__.keys()`, configure `extrasaction="raise"` to fail loudly on unmapped keys, and validate incoming CSV headers against the schema before processing.

---

### S2-2 — Cumulative merge logic freezes stale master data over fresh scrapes
- **Where:** `dedup.py:41-42` (`_field_count`), `dedup.py:60` (`_merge`), and `main.py:124` (`combined = raw_filtered + master`).
- **Breaks:** On weekly runs, `main.py:124` concatenates new scrapes with the historical master. In `dedup._merge`:
  ```python
  merged[key] = av if _field_count(a) >= _field_count(b) else bv
  ```
  Master records (`b`) have already undergone enrichment (`region`, `website_live`, `industry_priority`, `recommended_service`, `lead_score`, contacts), typically containing 16–22 non-None fields. Freshly scraped records (`a`) contain only 6–10 raw scraper fields. Because `_field_count(master) > _field_count(scraper)`, `bv` (master) wins for **every single non-null field collision**.
  - Updated ratings (e.g., rating drops from 4.8 to 3.2 on Google) are discarded in favor of stale master ratings.
  - New review counts are discarded.
  - Updated addresses or phone numbers are discarded.
  - Meanwhile, `scraped_at` updates via `max(av, bv)` (`dedup.py:58`), stamping the record with today's timestamp while freezing the actual data in its historical state.
- **Trigger:** A business changes phone number or rating. Google Places returns `{"phone": "+9611999999", "rating": 3.5, ...}`. The master record has `phone: "+9611111111"`, `rating: 4.8`, and 6 enrichment fields. `_field_count(master) > _field_count(scrape)` causes the stale phone and rating to be retained forever.
- **Fix:** Decouple field conflict resolution from crude global `_field_count`. Fresh scrape values should supersede master values for source-level telemetry (`rating`, `review_count`, `address`, `phone`), while derived fields (`website_live`, `lead_score`) are re-evaluated downstream.

---

### S3-1 — `website_live` boolean parsing drops dead-website pitch signals upon spreadsheet export
- **Where:** `main.py:68-69` (`row["website_live"] = True if wl == "True" else (False if wl == "False" else None)`).
- **Breaks:** The boolean parser uses strict string equality against `"True"` and `"False"`. If `all_businesses.csv` is opened and saved by Excel, LibreOffice, Google Sheets, or processed through pandas/databases, boolean columns are frequently formatted as `"TRUE"`, `"FALSE"`, `"true"`, `"false"`, `1`, or `0`.
  - In all such cases, `wl == "True"` and `wl == "False"` both evaluate to `False`, coercing the value to `None`.
  - In `enricher.py:243-244`, dead websites earn +20 lead score points:
    `elif record.get("website") and record.get("website_live") is False: score += 20`
  - When `website_live` becomes `None`, high-value sales leads (businesses with broken websites) lose 20 points, missing the sales-ready threshold (`main.py:144`).
  - Furthermore, `load_master` only casts `website_live`. If another boolean flag is added to `BusinessRecord` in the future, it will remain a string (`"False"`), which evaluates as truthy in Python (`bool("False") is True`).
- **Trigger:** Master CSV containing `"TRUE"` or `"FALSE"` in the `website_live` column.
- **Fix:** Normalize before parsing:
  ```python
  if wl is not None:
      wl_clean = str(wl).strip().lower()
      row["website_live"] = True if wl_clean in ("true", "1", "yes") else (False if wl_clean in ("false", "0", "no") else None)
  ```

---

### S3-2 — Irreversible `""` to `None` coercion erases explicit empty states
- **Where:** `main.py:53-55`:
  ```python
  for k in list(row):
      if row[k] == "":
          row[k] = None
  ```
- **Breaks:** In Python's standard `csv.DictWriter`, `None` is written as `""`, and empty string `""` is written as `""`. On read, `load_master` converts all `""` to `None`. The distinction between *"attribute scraped and verified empty"* (`""`) and *"attribute unprobed / missing"* (`None`) is lost.
  - In `dedup.py:41-42`, `_field_count` counts `v is not None`. An in-memory record with `""` has a higher field count before writing than after reading from CSV.
  - For scrapers that emit empty strings (e.g. `google_places.py:236` setting `name = ... or ""`), the value collapses to `None` upon reload.
- **Trigger:** Writing a record with `category=""` or `email=""`. On `load_master`, both become `None`.
- **Fix:** Explicitly define field-level nullability in the schema, and distinguish between unpopulated fields and empty string values if field presence telemetry is required.

---

### S3-3 — Address commas survive CSV quoting but break downstream city extraction
- **Where:** `dedup.py:34-38` (`_extract_city`) and `dedup.py:78-79` (`key = (normalize_name(name), city)`).
- **Breaks:** Embedded commas in addresses survive CSV serialization properly because `csv.DictWriter` uses `QUOTE_MINIMAL` to quote fields containing delimiters (e.g., `"Hamra Street, Beirut, Lebanon"`). However, `dedup._extract_city` naively splits on comma and takes the final item:
  ```python
  parts = [p.strip().lower() for p in address.split(",")]
  return parts[-1] if parts else ""
  ```
  - For Google Places addresses (`"..., Beirut, Lebanon"` or `"..., Riyadh 12213, Saudi Arabia"`), `parts[-1]` is `"lebanon"` or `"saudi arabia"`, not the city.
  - For OSM addresses (`addr:suburb, addr:city, addr:district`), `parts[-1]` may be a district.
  - When the same business is scraped from multiple sources or address details change, the deduplication key `(normalize_name(name), city)` differs. The new scrape fails to merge with the master record, appending a duplicate business to `all_businesses.csv` and inflating the cumulative master week after week.
- **Trigger:** Master record has address `"Hamra, Beirut"`; incoming Google Places scrape has address `"Hamra St, Beirut, Lebanon"`. `dedup` generates keys `("cafe", "beirut")` vs `("cafe", "lebanon")`, bypassing deduplication.
- **Fix:** Parse structured address components (city, country, postal code) at scraper ingestion rather than splitting raw formatted address strings on commas.

---

## Value-by-Value Round-Trip Fidelity

Evaluation of a single round-trip cycle: `record` → `write_csv(path, [record])` → `load_master(path)` → `loaded_record`.

| Value Type | Sample Input | Saved in CSV | Loaded Value | Survives? | Exact Mechanism & Failure Risk |
|---|---|---|---|:---:|---|
| **`None`** | `{"email": None}` | `email,` (empty cell `""`) | `{"email": None}` | **YES** | `DictWriter` writes `None` as `""`; `load_master:54-55` maps `""` back to `None`. Stable across cycles. |
| **Empty string `""`** | `{"category": ""}` | `category,` (empty cell `""`) | `{"category": None}` | **NO** | `load_master:54-55` converts `""` to `None`. Irreversible. Collapses empty values into missing values. |
| **Boolean `True`** | `{"website_live": True}` | `True` | `{"website_live": True}` | **YES** | `str(True)` writes `"True"`; `load_master:69` matches literal string `"True"`. |
| **Boolean `False`** | `{"website_live": False}` | `False` | `{"website_live": False}` | **YES** | `str(False)` writes `"False"`; `load_master:69` matches literal string `"False"`. |
| **Boolean string `"TRUE"` / `"FALSE"`** | `{"website_live": "TRUE"}` | `TRUE` | `{"website_live": None}` | **NO** | Strict equality check (`wl == "True"`) fails. Excel/Sheets exports convert booleans to `None`. |
| **Boolean numeric `1` / `0`** | `{"website_live": 1}` | `1` | `{"website_live": None}` | **NO** | Fails literal `"True"`/`"False"` check. Coerced to `None`. |
| **Future Boolean fields** | `{"verified": False}` | `False` | `{"verified": "False"}` | **NO** | Only `website_live` has a boolean cast in `load_master`. Other boolean fields remain strings; `"False"` evaluates truthy in Python. |
| **Float (`lat`, `lon`)** | `{"lat": 33.8937913}` | `33.8937913` | `{"lat": 33.8937913}` | **YES** | Python 3 shortest-representation IEEE 754 float formatting round-trips without loss. |
| **Float (`rating`)** | `{"rating": 4.5}` | `4.5` | `{"rating": 4.5}` | **YES** | Exact float preserved via `float(row["rating"])` (`main.py:59`). |
| **Integer `rating`** | `{"rating": 4}` (Google int) | `4` | `{"rating": 4.0}` | **PARTIAL** | Value is numerically equivalent, but type mutates from `int` to `float` (`main.py:59`). |
| **Float with comma / invalid** | `{"lat": "33,8938"}` | `"33,8938"` | `{"lat": None}` | **NO** | `float("33,8938")` raises `ValueError`; caught by `load_master:60` and silently wiped to `None`. |
| **Integer (`review_count`)** | `{"review_count": 120}` | `120` | `{"review_count": 120}` | **YES** | Cast via `int(float(x))` (`main.py:65`). Integer type preserved. |
| **Integer zero `0`** | `{"lead_score": 0}` | `0` | `{"lead_score": 0}` | **YES** | `"0"` is non-empty, so `row[k] == ""` does not trigger. `int(float("0"))` returns `0`. |
| **Float string integer** | `{"review_count": "120.0"}` | `120.0` | `{"review_count": 120}` | **YES** | `int(float("120.0"))` correctly casts formatted floats to integers. |
| **Formatted integer** | `{"review_count": "1,200"}` | `"1,200"` | `{"review_count": None}` | **NO** | `float("1,200")` raises `ValueError`; silently wiped to `None`. |
| **Derived integers (`completeness_score`, `lead_score`)** | `{"lead_score": 85}` | `85` | `{"lead_score": 85}` | **OVERWRITTEN** | Preserved on CSV read, but discarded during pipeline execution because `enrich()` recomputes both scores from scratch (`main.py:137`, `enricher.py:289-290`). |
| **Embedded commas** | `{"name": "Beirut, Lebanon"}` | `"Beirut, Lebanon"` | `{"name": "Beirut, Lebanon"}` | **YES** | CSV `QUOTE_MINIMAL` encloses cell in double quotes; parsed back without column drift. |
| **Embedded newlines** | `{"address": "Floor 2\nBeirut"}` | `"Floor 2\nBeirut"` | `{"address": "Floor 2\nBeirut"}` | **YES** | Both read and write open files with `newline=""`, preserving `\n` and `\r\n` within quoted cells. Breaks non-RFC4180 external tools. |
| **Embedded double quotes** | `{"name": 'Café "Al-Sultan"'}` | `"Café ""Al-Sultan"""` | `{"name": 'Café "Al-Sultan"'}` | **YES** | `DictWriter` doubles inner quotes (`""`); `DictReader` restores original `"` cleanly. |
| **Embedded single quotes** | `{"name": "O'Connell Pub"}` | `O'Connell Pub` | `{"name": "O'Connell Pub"}` | **YES** | Standard CSV does not quote single quotes; string preserved exactly. |
| **Arabic RTL text** | `{"name": "مكتبة ومطبعة بيروت"}` | `مكتبة ومطبعة بيروت` | `{"name": "مكتبة ومطبعة بيروت"}` | **YES** | `encoding="utf-8"` preserves Arabic characters (U+0600–U+06FF) and RTL markers cleanly. |
| **UTF-8 with BOM** | `\ufeffname,...` | N/A | `{"name": None}` | **FATAL** | Reader fails to recognize `\ufeffname` as `name`. Wipes `name` column on subsequent write (S1-1). |
| **Unlisted schema fields** | `{"place_id": "ChIJ..."}` | (Dropped) | `{"place_id": None}` | **NO** | `extrasaction="ignore"` silently strips any key not in `main.py:FIELDS` (S2-1). |

---

## Compounding Drift Dynamics in Cumulative Master

Because `data/all_businesses.csv` is cumulative across weekly runs, round-trip anomalies do not remain isolated; they compound over time:

```
[Weekly Run N]
Scrapers (raw) ──┐
                 ├──> combined = raw + master ──> dedup._merge ──> enrich ──> write_csv
Master (Run N-1) ┘                                      │
   ▲                                                    │ (Master field count > raw)
   └────────────────── load_master ◄────────────────────┘ (Fresh ratings/phones rejected)
```

1. **Staleness Ratchet:** As master records accumulate enrichment fields (`completeness_score`, `lead_score`, `website_live`, `recommended_service`), their `_field_count` rises to ~20. Incoming scraper records have ~8 fields. In `dedup._merge`, any conflicting field between master and scrape resolves to the master record. As a result, once a business enters the master, **fresh phone numbers, addresses, ratings, and review counts from subsequent scrapes are permanently rejected**. The master grows increasingly stale while `scraped_at` is updated to the current timestamp.
2. **Duplicate Accumulation:** Commas in address strings trigger divergent city inferences in `dedup._extract_city` (`"beirut"` vs `"lebanon"`). When the same business is re-scraped with slight address formatting differences, the `(normalize_name(name), city)` key mismatches. The scraper record is treated as a new entity and appended to the master. Over months, duplicate entries for the same establishment accumulate in `all_businesses.csv`.
3. **Weekly Re-scraping of Master Records:** Even though `all_businesses.csv` stores `website_live` and contact details from prior runs, `main.py:129-130` passes all records back through `enricher.enrich()`. Every website in the master is re-fetched across 40 threads on every run (`enricher.py:182`). Transient DNS or network failures on scrape day overwrite verified `website_live: True` entries with `False`.

---

## Not a bug, but worth knowing

- **Column count discrepancy in project documentation:** `BRIEF.md:57-62` states *"The 22 columns"*, but enumerates 23 keys (`name` through `scraped_at`). Both `main.py:FIELDS` and `scrapers/base.py:BusinessRecord` define exactly 23 columns. They match today, but the brief's typo illustrates how easily manual count checks drift without programmatic schema assertions.
- **Pipe-delimited source merging:** `source` values are merged via `"|".join(sorted(set(av.split("|")) | set(bv.split("|"))))` (`dedup.py:55-56`). If any future scraper name contains a pipe character (`|`), the delimiter split will corrupt the source name.
- **Microsecond timestamp sorting:** `dedup.py:58` resolves `scraped_at` conflicts via `max(av, bv)`. This relies on ISO-8601 string lexicographical comparison (`YYYY-MM-DDTHH:MM:SS.mmmmmm`). If any scraper emits a timezone offset (`+00:00` or `Z`) or omits microseconds, lexicographical comparison will misorder timestamps.
- **Numeric rating boundary comparisons:** A whole-number rating from Google Places (`5`) is serialized as `"5"` and parsed back as `float(5.0)`. While harmless for numeric comparisons (`5.0 >= 4.0`), exact type checks (`type(r["rating"]) is int`) fail after a single round trip.

---

## Recommended order of work

1. **Fix BOM wiping vector (S1-1):** Change `main.py:51` to `open(path, newline="", encoding="utf-8-sig")`. One-line change that eliminates catastrophic data loss when files are edited in Excel or Google Sheets.
2. **Unify schema definition (S2-1):** Derive `FIELDS` directly from `BusinessRecord.__annotations__.keys()` and switch `csv.DictWriter` to `extrasaction="raise"`. Ensures adding a field to `BusinessRecord` without updating writer parameters fails immediately in development rather than silently dropping data in production.
3. **Repair cumulative merge precedence (S2-2):** Refactor `dedup._merge` to prioritize incoming scrape data for live business telemetry (`rating`, `review_count`, `phone`, `address`) rather than letting stale master records win based on total field count.
4. **Harden boolean parsing (S3-1):** In `main.py:68-69`, strip and lowercase `website_live` values, parsing `"true"`, `"false"`, `"1"`, and `"0"` so external spreadsheet edits do not nullify lead score pitch signals.
5. **Structured address normalization (S3-3):** Replace `address.split(",")[-1]` in `dedup.py:34-38` with structured city extraction to prevent duplicate accumulation in the cumulative master.
