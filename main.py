"""
leadminer entry point.

Scrapes OSM, Wikidata, and Google Places, dedups, enriches, applies a category
whitelist, tags every record with a recommended Voxire service to pitch, and
writes four CSVs:

    data/all_businesses.csv   - everything that survived the whitelist
    data/with_websites.csv    - subset with a website (sell SEO / rebuild / etc.)
    data/without_websites.csv - subset with no website (sell new website)
    data/sales_ready.csv      - actionable subset: at least one contact channel
                                (phone OR email OR Instagram), priority/medium
                                industries only, with recommended_service tag.

Run:
    python main.py
"""

import csv
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from scrapers.osm import OSMScraper
from scrapers.wikidata import WikidataScraper
from scrapers.google_places import GooglePlacesScraper
from scrapers.whitelist import is_business_category, industry_priority
from dedup import dedup
from enricher import enrich
from pitch_recommender import recommend_service

FIELDS = [
    "name", "category", "region", "country", "address", "lat", "lon",
    "phone", "email", "website", "website_live",
    "facebook", "instagram",
    "rating", "review_count", "completeness_score",
    "industry_priority", "recommended_service",
    "source", "scraped_at",
]
DATA_DIR = pathlib.Path("data")


def write_csv(path: pathlib.Path, records: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    print(f"  Written: {path} ({len(records)} records)")


def has_any_contact(record: dict) -> bool:
    """Sales-ready threshold: phone OR email OR Instagram."""
    return bool(
        (record.get("phone") and len(str(record["phone"])) > 3)
        or (record.get("email") and len(str(record["email"])) > 3)
        or (record.get("instagram") and len(str(record["instagram"])) > 3)
    )


def main() -> None:
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
                print(
                    f"[{type(futures[future]).__name__}] ERROR: {e}",
                    file=sys.stderr,
                )

    print(f"\nTotal raw records: {len(raw)}")

    # Apply whitelist BEFORE dedup so the dedup pass operates on a clean set
    raw_filtered = [r for r in raw if is_business_category(r.get("category"))]
    print(
        f"After whitelist filter: {len(raw_filtered)} "
        f"(dropped {len(raw) - len(raw_filtered)})"
    )

    records = dedup(raw_filtered)
    print(f"After dedup: {len(records)} unique businesses")

    print("\nEnriching records (website liveness, completeness)...")
    records = enrich(records)

    # Tag each record with industry_priority and recommended_service.
    # Country is set by each scraper directly (LB for OSM/Wikidata, LB or SA for Google Places).
    for r in records:
        r["industry_priority"] = industry_priority(r.get("category"))
        r["recommended_service"] = recommend_service(r)

    with_websites = [r for r in records if r.get("website")]
    without_websites = [r for r in records if not r.get("website")]
    with_social = [
        r for r in records if r.get("facebook") or r.get("instagram")
    ]

    # Sales-ready: any contact channel + priority or medium industry
    sales_ready = [
        r
        for r in records
        if has_any_contact(r) and r.get("industry_priority") in ("high", "medium")
    ]

    DATA_DIR.mkdir(exist_ok=True)
    write_csv(DATA_DIR / "all_businesses.csv", records)
    write_csv(DATA_DIR / "with_websites.csv", with_websites)
    write_csv(DATA_DIR / "without_websites.csv", without_websites)
    write_csv(DATA_DIR / "sales_ready.csv", sales_ready)

    live = sum(1 for r in with_websites if r.get("website_live"))
    dead = sum(1 for r in with_websites if r.get("website_live") is False)

    print(f"\nSummary:")
    print(f"  Total unique businesses : {len(records)}")
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

    # Recommended-service breakdown for quick pulse on what helpers will pitch
    by_service = {}
    for r in sales_ready:
        svc = r.get("recommended_service") or "Unknown"
        by_service[svc] = by_service.get(svc, 0) + 1
    print(f"\n  Sales-ready by recommended service:")
    for svc, count in sorted(by_service.items(), key=lambda x: -x[1]):
        print(f"    {svc:<55} {count}")


if __name__ == "__main__":
    main()
