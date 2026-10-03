import logging
from typing import Iterator

from httpclient import fetch_with_retry, get_session, utc_now_iso
from .base import BaseScraper, BusinessRecord

log = logging.getLogger(__name__)

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
        scraped_at = utc_now_iso()
        print("[OSM] Fetching Lebanon businesses from Overpass API...")

        session = get_session()

        # Centralised retry with exponential backoff and full jitter replaces
        # the previous fixed 30s sleep loop, which retried in lockstep.
        result = fetch_with_retry(
            "POST",
            OVERPASS_URL,
            session=session,
            data={"data": OVERPASS_QUERY},
            timeout=200,
            retries=3,
        )
        if not result.ok:
            # Previously this was `print` then `return`, indistinguishable from
            # "OSM legitimately had nothing to return". A broken source must
            # not look like an empty one.
            print(
                f"[OSM] FAILED after {result.attempts} attempts: {result.error}. "
                "Continuing without OSM data.",
                flush=True,
            )
            return

        elements = result.json().get("elements", [])
        print(f"[OSM] Got {len(elements)} elements.")

        for el in elements:
            tags = el.get("tags", {})
            name = tags.get("name") or tags.get("name:en") or tags.get("name:ar")
            if not name:
                continue

            # coordinates — nodes have lat/lon directly, ways have a center object
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")

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
            facebook = tags.get("contact:facebook") or tags.get("facebook")
            instagram = tags.get("contact:instagram") or tags.get("instagram")

            if website and not website.startswith("http"):
                website = "https://" + website

            yield BusinessRecord(
                name=name,
                category=category,
                address=address,
                region=None,
                country="LB",
                lat=lat,
                lon=lon,
                phone=phone,
                email=email,
                website=website,
                website_live=None,
                facebook=facebook,
                instagram=instagram,
                whatsapp=None,
                linkedin=None,
                rating=None,
                review_count=None,
                industry_priority=None,
                recommended_service=None,
                lead_score=0,
                source="osm",
                scraped_at=scraped_at,
                completeness_score=0,
            )
