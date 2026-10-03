"""
leadminer command line interface.

Replaces `python main.py` with something that can be scripted, scheduled and
debugged:

    leadminer run                    # full pipeline (scrape -> export)
    leadminer scrape --source osm    # one source only
    leadminer stats                  # summarise an existing CSV, no network
    leadminer validate               # data quality checks, no network
    leadminer doctor                 # check credentials and connectivity
    leadminer score                  # re-score existing records, no network

Commands other than `run`/`scrape`/`doctor` never touch the network, so they
are safe to run anywhere and are what you reach for when a run looks wrong.
"""

from __future__ import annotations

import argparse
import collections
import csv
import pathlib
import sys

DATA_DIR = pathlib.Path("data")


def _fmt(n: int | float | None, suffix: str = "") -> str:
    return "n/a" if n is None else f"{n:,}{suffix}"


def cmd_run(args: argparse.Namespace) -> int:
    """Full pipeline. Thin wrapper over main.main so there is one code path."""
    import main as pipeline

    return pipeline.main()


def cmd_scrape(args: argparse.Namespace) -> int:
    """Run one scraper and print what it found, without writing the exports.

    Useful for checking a single source after an API change without paying for
    a full enrichment pass.
    """
    from scrapers.base import BaseScraper  # noqa: F401  (import check)
    from scrapers.google_places import GooglePlacesScraper
    from scrapers.osm import OSMScraper
    from scrapers.wikidata import WikidataScraper

    registry = {
        "osm": OSMScraper,
        "wikidata": WikidataScraper,
        "google_places": GooglePlacesScraper,
    }
    if args.source not in registry:
        print(f"unknown source {args.source!r}; choose from {sorted(registry)}",
              file=sys.stderr)
        return 2

    records = list(registry[args.source]().scrape())
    print(f"[{args.source}] {len(records)} records")
    if not records:
        print("  WARNING: zero records. The source may be broken or blocked.",
              file=sys.stderr)
        return 1

    by_cat = collections.Counter(r.get("category") for r in records)
    with_phone = sum(1 for r in records if r.get("phone"))
    with_site = sum(1 for r in records if r.get("website"))
    print(f"  with phone : {with_phone}")
    print(f"  with site  : {with_site}")
    print("  top categories:")
    for cat, n in by_cat.most_common(10):
        print(f"    {str(cat):<28} {n}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Summarise a CSV. No network. This is the first thing to check."""
    path = pathlib.Path(args.file)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 2

    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print(f"{path} is empty", file=sys.stderr)
        return 1

    total = len(rows)
    print(f"{path}  ({total:,} rows)\n")

    def count(field: str) -> int:
        return sum(1 for r in rows if (r.get(field) or "").strip())

    print(f"  with phone      : {_fmt(count('phone'))}")
    print(f"  with email      : {_fmt(count('email'))}")
    print(f"  with website    : {_fmt(count('website'))}")
    print(f"  with instagram  : {_fmt(count('instagram'))}")
    print(f"  with whatsapp   : {_fmt(count('whatsapp'))}")
    print(f"  with region     : {_fmt(count('region'))}")

    # website_live is tri-state: True live, False server-confirmed dead,
    # blank unreachable. Collapsing blank into False was the original bug.
    live = sum(1 for r in rows if (r.get("website_live") or "").strip() == "True")
    dead = sum(1 for r in rows if (r.get("website_live") or "").strip() == "False")
    unknown = sum(1 for r in rows
                  if r.get("website") and not (r.get("website_live") or "").strip())
    print(f"\n  website live    : {live:,}")
    print(f"  website dead    : {dead:,}")
    print(f"  unreachable     : {unknown:,}  (not the same as dead)")

    print("\n  by priority:")
    for k, v in collections.Counter(r.get("industry_priority") for r in rows).most_common():
        print(f"    {str(k):<10} {v:,}")

    print("\n  by source:")
    for k, v in collections.Counter(r.get("source") for r in rows).most_common(8):
        print(f"    {str(k):<28} {v:,}")

    print("\n  by region:")
    for k, v in collections.Counter(r.get("region") or "Unknown"
                                    for r in rows).most_common(12):
        print(f"    {str(k):<20} {v:,}")

    print("\n  top pitches:")
    for k, v in collections.Counter(r.get("recommended_service")
                                    for r in rows).most_common(8):
        print(f"    {str(k):<52} {v:,}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Cheap data-quality gate. No network. Exits non-zero if checks fail."""
    path = pathlib.Path(args.file)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 2

    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print(f"{path} is empty", file=sys.stderr)
        return 1

    problems: list[str] = []
    total = len(rows)

    if not any((r.get("name") or "").strip() for r in rows):
        problems.append("every row has a blank name")

    bom_name = sum(1 for r in rows if (r.get("name") or "").startswith("\ufeff"))
    if bom_name:
        problems.append(f"{bom_name} names carry a UTF-8 BOM (written without utf-8-sig)")

    # Coordinates must fall inside the two markets we claim to cover.
    def in_box(lat: float, lon: float) -> bool:
        lb = 33.0 <= lat <= 34.8 and 35.0 <= lon <= 36.9
        sa = 16.0 <= lat <= 32.2 and 34.4 <= lon <= 55.7
        return lb or sa

    bad_coords = 0
    for r in rows:
        try:
            lat, lon = float(r["lat"]), float(r["lon"])  # type: ignore[arg-type]
        except (ValueError, TypeError, KeyError):
            continue
        if not in_box(lat, lon):
            bad_coords += 1
    if bad_coords > total * 0.01:
        problems.append(f"{bad_coords} rows have coordinates outside Lebanon/Saudi Arabia")

    bad_rating = 0
    for r in rows:
        raw = (r.get("rating") or "").strip()
        if not raw:
            continue
        try:
            v = float(raw)
        except ValueError:
            bad_rating += 1
            continue
        if not 0 <= v <= 5:
            bad_rating += 1
    if bad_rating:
        problems.append(f"{bad_rating} rows have a rating outside 0-5")

    dupes = total - len({(r.get("name"), r.get("phone")) for r in rows})
    if dupes > total * 0.05:
        problems.append(f"{dupes} duplicate (name, phone) pairs")

    print(f"checked {total:,} rows in {path}")
    if not problems:
        print("  all checks passed")
        return 0
    print("  PROBLEMS:")
    for p in problems:
        print(f"    - {p}")
    return 1


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check credentials and connectivity before blaming the data."""
    import os

    ok = True
    print("credentials:")
    for var in ("GOOGLE_PLACES_API_KEY", "SCRAPER_EMAIL"):
        present = bool(os.environ.get(var))
        print(f"  {'OK ' if present else 'MISSING'}  {var}")
        if var == "GOOGLE_PLACES_API_KEY" and not present:
            ok = False

    print("\ndata directory:")
    if DATA_DIR.exists():
        print(f"  OK   {DATA_DIR} exists")
        for f in sorted(DATA_DIR.glob("*.csv")):
            print(f"       {f.name:<32} {f.stat().st_size:,} bytes")
    else:
        print(f"  MISSING  {DATA_DIR} (no run has produced output yet)")

    print("\nconnectivity (5s timeout each):")
    try:
        import requests
    except ImportError:
        # doctor exists to diagnose a broken environment, so it must never be
        # the thing that tracebacks.
        print("  FAIL  requests is not installed - run: pip install -e '.[dev]'")
        return 1

    for name, url in (
        ("Overpass", "https://overpass-api.de/api/status"),
        ("Wikidata", "https://query.wikidata.org/sparql?query=ASK%7B%7D"),
        ("Google Places", "https://places.googleapis.com/v1/places:searchText"),
    ):
        try:
            r = requests.get(url, timeout=5)
            note = "" if r.status_code < 500 else f"HTTP {r.status_code}"
            print(f"  {'OK ' if r.status_code < 500 else 'WARN'}  {name:<14} {note}")
        except Exception as e:
            print(f"  FAIL  {name:<14} {type(e).__name__}")
    return 0 if ok else 1


def cmd_score(args: argparse.Namespace) -> int:
    """Re-score an existing CSV with current rules, without scraping."""
    import main as pipeline
    from enricher import lead_score
    from pitch_recommender import recommend_service
    from scrapers.whitelist import industry_priority

    path = pathlib.Path(args.file)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 2

    records = pipeline.load_master(path)
    if not records:
        print(f"{path} has no usable rows", file=sys.stderr)
        return 1

    changed = 0
    for r in records:
        r["country"] = pipeline.resolve_country(r)
        r["industry_priority"] = industry_priority(r.get("category"))
        r["recommended_service"] = recommend_service(r)
        new = lead_score(r)
        if new != r.get("lead_score"):
            changed += 1
        r["lead_score"] = new

    out = pathlib.Path(args.out) if args.out else path
    pipeline.write_csv(out, records)
    print(f"re-scored {len(records):,} records, {changed:,} changed -> {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="leadminer", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="full pipeline: scrape, enrich, export").set_defaults(
        func=cmd_run)

    s = sub.add_parser("scrape", help="run one scraper and report, no export")
    s.add_argument("--source", required=True,
                   choices=["osm", "wikidata", "google_places"])
    s.set_defaults(func=cmd_scrape)

    s = sub.add_parser("stats", help="summarise a CSV (no network)")
    s.add_argument("file", nargs="?", default=str(DATA_DIR / "all_businesses.csv"))
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("validate", help="data quality checks (no network)")
    s.add_argument("file", nargs="?", default=str(DATA_DIR / "all_businesses.csv"))
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("doctor", help="check credentials and connectivity")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("score", help="re-score an existing CSV (no network)")
    s.add_argument("file", nargs="?", default=str(DATA_DIR / "all_businesses.csv"))
    s.add_argument("--out", help="write elsewhere instead of in place")
    s.set_defaults(func=cmd_score)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())