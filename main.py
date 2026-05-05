"""
leadminer entry point.

Scrapes OSM, Wikidata, and Google Places, merges with the existing master CSV,
dedups, enriches, applies a category whitelist, tags every record with a
recommended Voxire service to pitch, and writes five CSVs:

    data/all_businesses.csv     - cumulative master (all time)
    data/qualified_businesses.csv - completeness_score >= 1
    data/with_websites.csv      - subset with a website
    data/without_websites.csv   - subset with no website
    data/sales_ready.csv        - actionable subset

Run:
    python main.py
"""

import csv
import pathlib
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from scrapers.osm import OSMScraper
from scrapers.wikidata import WikidataScraper
from scrapers.google_places import GooglePlacesScraper
from scrapers.whitelist import is_business_category, industry_priority
from dedup import dedup, normalize_phone
from enricher import enrich, lead_score as _lead_score
from pitch_recommender import recommend_service

FIELDS = [
    "name", "category", "region", "country", "address", "lat", "lon",
    "phone", "email", "website", "website_live",
    "facebook", "instagram", "whatsapp", "linkedin",
    "rating", "review_count", "completeness_score", "lead_score",
    "industry_priority", "recommended_service",
    "source", "scraped_at",
]
DATA_DIR = pathlib.Path("data")

_FLOAT_FIELDS = {"lat", "lon", "rating"}
_INT_FIELDS = {"review_count", "completeness_score", "lead_score"}


def load_master(path: pathlib.Path) -> list[dict]:
    """Load an existing master CSV with proper type casting."""
    if not path.exists():
        return []
    records = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for k in list(row):
                if row[k] == "":
                    row[k] = None
            for field in _FLOAT_FIELDS:
                if row.get(field) is not None:
                    try:
                        row[field] = float(row[field])
                    except (ValueError, TypeError):
                        row[field] = None
            for field in _INT_FIELDS:
                if row.get(field) is not None:
                    try:
                        row[field] = int(float(row[field]))
                    except (ValueError, TypeError):
                        row[field] = None
            wl = row.get("website_live")
            row["website_live"] = True if wl == "True" else (False if wl == "False" else None)
            records.append(row)
    return records


def write_csv(path: pathlib.Path, records: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    print(f"  Written: {path} ({len(records)} records)")


def has_any_contact(record: dict) -> bool:
    phone = re.sub(r"\D", "", str(record.get("phone") or ""))
    email = str(record.get("email") or "").strip()
    instagram = str(record.get("instagram") or "").strip()
    return bool(
        len(phone) >= 7
        or ("@" in email and len(email) > 5)
        or len(instagram) > 3
    )


def main() -> None:
    # Load existing master to accumulate across runs
    master = load_master(DATA_DIR / "all_businesses.csv")
    if master:
        print(f"Loaded {len(master)} records from existing master CSV")

    raw: list[dict] = []
    scrapers = [OSMScraper(), WikidataScraper(), GooglePlacesScraper()]

    def run(scraper):
        return type(scraper).__name__, list(scraper.scrape())

    with ThreadPoolExecutor(max_workers=len(scrapers)) as pool:
        futures = {pool.submit(run, s): s for s in scrapers}
        for future in as_completed(futures):
            try:
                name, batch = future.result()
                print(f"[{name}] collected {len(batch)} records")
                raw.extend(batch)
            except Exception as e:
                print(f"[{type(futures[future]).__name__}] ERROR: {e}", file=sys.stderr)

    print(f"\nTotal raw records from scrapers: {len(raw)}")

    raw_filtered = [r for r in raw if is_business_category(r.get("category"))]
    print(
        f"After whitelist filter: {len(raw_filtered)} "
        f"(dropped {len(raw) - len(raw_filtered)})"
    )

    # Merge new records with existing master, then dedup the combined set
    combined = raw_filtered + master
    records = dedup(combined)
    print(f"After merge + dedup: {len(records)} unique businesses")

    print("\nEnriching records (website liveness + contacts)...")
    records = enrich(records)

    for r in records:
        r["industry_priority"] = industry_priority(r.get("category"))
        r["recommended_service"] = recommend_service(r)
        raw_phone = r.get("phone")
        if raw_phone:
            r["phone"] = normalize_phone(raw_phone, r.get("country", "LB"))
        r["lead_score"] = _lead_score(r)

    with_websites = [r for r in records if r.get("website")]
    without_websites = [r for r in records if not r.get("website")]
    with_social = [r for r in records if r.get("facebook") or r.get("instagram")]
    sales_ready = [
        r for r in records
        if has_any_contact(r) and r.get("industry_priority") in ("high", "medium")
    ]
    qualified = [r for r in records if r.get("completeness_score", 0) >= 1]

    DATA_DIR.mkdir(exist_ok=True)
    write_csv(DATA_DIR / "all_businesses.csv", records)
    write_csv(DATA_DIR / "qualified_businesses.csv", qualified)
    write_csv(DATA_DIR / "with_websites.csv", with_websites)
    write_csv(DATA_DIR / "without_websites.csv", without_websites)
    write_csv(DATA_DIR / "sales_ready.csv", sales_ready)

    live = sum(1 for r in with_websites if r.get("website_live"))
    dead = sum(1 for r in with_websites if r.get("website_live") is False)

    print(f"\nSummary:")
    print(f"  Total unique businesses : {len(records)}")
    print(f"  Qualified (score >= 1)  : {len(qualified)}")
    print(f"  With website            : {len(with_websites)} ({live} live, {dead} dead)")
    print(f"  Without website         : {len(without_websites)}")
    print(f"  With social media       : {len(with_social)}")
    print(f"  SALES-READY (actionable): {len(sales_ready)}")

    by_region = {}
    for r in records:
        reg = r.get("region") or "Unknown"
        by_region[reg] = by_region.get(reg, 0) + 1
    print(f"\n  By region:")
    for reg, count in sorted(by_region.items(), key=lambda x: -x[1]):
        print(f"    {reg:<20} {count}")

    by_service = {}
    for r in sales_ready:
        svc = r.get("recommended_service") or "Unknown"
        by_service[svc] = by_service.get(svc, 0) + 1
    print(f"\n  Sales-ready by recommended service:")
    for svc, count in sorted(by_service.items(), key=lambda x: -x[1]):
        print(f"    {svc:<55} {count}")


if __name__ == "__main__":
    main()
