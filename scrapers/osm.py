import datetime
import time
import requests
from typing import Iterator
from .base import BaseScraper, BusinessRecord

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

OVERPASS_QUERY = """
[out:json][timeout:180];
area["ISO3166-1"="LB"]->.lb;
(
  node["name"]["shop"](area.lb);
  node["name"]["amenity"](area.lb);
  node["name"]["office"](area.lb);
  node["name"]["tourism"](area.lb);
  node["name"]["craft"](area.lb);
  node["name"]["healthcare"](area.lb);
  way["name"]["shop"](area.lb);
  way["name"]["amenity"](area.lb);
  way["name"]["office"](area.lb);
  way["name"]["tourism"](area.lb);
);
out center tags;
"""


class OSMScraper(BaseScraper):
    def scrape(self) -> Iterator[BusinessRecord]:
        scraped_at = datetime.datetime.utcnow().isoformat() + "Z"
        print("[OSM] Fetching Lebanon businesses from Overpass API...")

        session = requests.Session()
        session.headers["User-Agent"] = "leadminer/1.0 (voxire.tech@gmail.com)"

        for attempt in range(3):
            try:
                resp = session.post(
                    OVERPASS_URL,
                    data={"data": OVERPASS_QUERY},
                    timeout=200,
                )
                resp.raise_for_status()
                break
            except requests.RequestException as e:
                print(f"[OSM] Attempt {attempt + 1} failed: {e}")
                if attempt < 2:
                    time.sleep(30)
                else:
                    print("[OSM] All retries exhausted, skipping.")
                    return

        elements = resp.json().get("elements", [])
        print(f"[OSM] Got {len(elements)} elements.")

        for el in elements:
            tags = el.get("tags", {})
            name = tags.get("name") or tags.get("name:en") or tags.get("name:ar")
            if not name:
                continue

            category = (
                tags.get("shop")
                or tags.get("amenity")
                or tags.get("office")
                or tags.get("tourism")
                or tags.get("craft")
                or tags.get("healthcare")
            )

            addr_parts = [
                tags.get("addr:housenumber"),
                tags.get("addr:street"),
                tags.get("addr:suburb"),
                tags.get("addr:city"),
                tags.get("addr:district"),
            ]
            address = ", ".join(p for p in addr_parts if p) or None

            phone = tags.get("phone") or tags.get("contact:phone")
            email = tags.get("email") or tags.get("contact:email")
            website = tags.get("website") or tags.get("contact:website") or tags.get("url")

            if website and not website.startswith("http"):
                website = "https://" + website

            yield BusinessRecord(
                name=name,
                category=category,
                address=address,
                phone=phone,
                email=email,
                website=website,
                source="osm",
                scraped_at=scraped_at,
            )
