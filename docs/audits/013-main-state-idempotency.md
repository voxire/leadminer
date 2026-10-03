# 013 — Master-state fail-open and timestamp ordering

## Verdict

The workflow treats the Drive master as optional state: a failed download is suppressed, and `load_master()` interprets the resulting missing file as a valid empty first-run state. A scrape with no surviving records can therefore write and upload header-only outputs, replacing the cumulative master; a partial scrape instead silently drops all prior leads and enrichments. `scraped_at` is normally ISO-8601 UTC text, whose string ordering works for consistent fixed-width timestamps but is not completely safe with the format the scrapers actually emit.

## Findings

### S1 — A failed master download is indistinguishable from an intentional empty master

- **Where:** `.github/workflows/scrape.yml:35-44`; `main.py:46-50, 93-97, 123-129`; `.gitignore:9`; `.github/workflows/scrape.yml:46-50`
- **Breaks:** `data/` is gitignored and checkout does not supply the master. The workflow creates `data/`, then suppresses every `rclone copy` failure with `|| true`. `load_master()` returns `[]` when `all_businesses.csv` is absent and logs nothing for that state. Consequently, `combined` contains only this run's filtered scrape, so all records and enrichment retained only in the Drive master are discarded. If every scraper returns no records or raises (exceptions are caught in `main.py:105-113`), `enrich([])` does no work and `write_csv()` still emits five header-only files (`main.py:128-153`). The workflow then proceeds to copy those same-named CSVs to the Drive root (`scrape.yml:46-50`), replacing the cumulative master with an empty one. Scraper errors are printed, but there is no failure/empty-output guard, so the job can still complete successfully.
- **Trigger:** In a fresh Actions checkout, make the Drive download fail (bad credentials, inaccessible remote, transient API error, or wrong remote path). If scraping also yields zero filtered records, the run writes and uploads an empty master; if scraping yields some records, the uploaded master contains only those records, losing all historical rows and contacts. A legitimate first run and a failed download take exactly the same path.
- **Fix:** Fail the workflow when the master download fails or the expected master is missing (except for an explicit bootstrap); validate the loaded master and refuse to upload empty/implausibly shrunken outputs without an explicit override.

### S3 — `max()` on `scraped_at` strings has a precision edge case

- **Where:** `dedup.py:57-58`; `scrapers/osm.py:31`; `scrapers/wikidata.py:31`; `scrapers/google_places.py:162`
- **Breaks:** All three scrapers create the value as `datetime.datetime.utcnow().isoformat() + "Z"`. In ordinary runs this looks like `2026-10-03T12:34:56.123456Z`: UTC with a literal `Z`, usually six fractional digits. But `isoformat()` omits the fractional part when microseconds are zero. Thus, within the same second, a later `...56.100000Z` compares *less than* an earlier `...56Z` as text (`.` sorts before `Z`), so `max()` can retain the earlier timestamp. For consistently formatted values at different seconds, or values all carrying the same fractional precision, lexical ordering is chronological; the actual emitted format does not guarantee that consistency.
- **Trigger:** Merge rows stamped `2026-10-03T12:34:56Z` and `2026-10-03T12:34:56.100000Z`. The latter is later in time, but `max()` selects the no-fraction string.
- **Fix:** Parse/normalize timestamps as UTC datetimes before comparing, or enforce one canonical fixed-width representation (including microseconds) for both new and existing values.

## Not a bug, but worth knowing

- `load_master()` correctly casts the score/coordinate columns it knows about, but leaves `scraped_at` as a string (`main.py:51-70`); `_merge()` therefore receives strings for persisted and newly scraped rows. The issue is not an accidental mixed Python type—it is the variable precision of the shared string format.

## Recommended order of work

1. **S1** — Make master acquisition fail closed and add a pre-upload guard for absent/empty or unexpectedly reduced cumulative output; allow empty state only through a deliberate bootstrap path.
2. **S3** — Normalize `scraped_at` to a canonical UTC representation or parse it for comparison, and cover the same-second/no-fraction case.
