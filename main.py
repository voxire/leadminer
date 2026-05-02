import csv
import pathlib
import sys

from scrapers.osm import OSMScraper
from scrapers.wikidata import WikidataScraper
from dedup import dedup

FIELDS = ["name", "category", "address", "phone", "email", "website", "source", "scraped_at"]
DATA_DIR = pathlib.Path("data")


def write_csv(path: pathlib.Path, records: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    print(f"  Written: {path} ({len(records)} records)")


def main() -> None:
    raw: list[dict] = []

    scrapers = [OSMScraper(), WikidataScraper()]

    for scraper in scrapers:
        name = type(scraper).__name__
        try:
            batch = list(scraper.scrape())
            print(f"[{name}] collected {len(batch)} records")
            raw.extend(batch)
        except Exception as e:
            print(f"[{name}] ERROR: {e}", file=sys.stderr)

    print(f"\nTotal raw records: {len(raw)}")
    records = dedup(raw)
    print(f"After dedup: {len(records)} unique businesses\n")

    DATA_DIR.mkdir(exist_ok=True)

    with_websites = [r for r in records if r.get("website")]
    without_websites = [r for r in records if not r.get("website")]

    write_csv(DATA_DIR / "all_businesses.csv", records)
    write_csv(DATA_DIR / "with_websites.csv", with_websites)
    write_csv(DATA_DIR / "without_websites.csv", without_websites)

    print(f"\nSummary:")
    print(f"  Total unique businesses : {len(records)}")
    print(f"  With website            : {len(with_websites)}")
    print(f"  Without website         : {len(without_websites)}")


if __name__ == "__main__":
    main()
