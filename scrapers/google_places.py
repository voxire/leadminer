import datetime
import os
import time
import requests
from typing import Iterator
from .base import BaseScraper, BusinessRecord

API_URL = "https://places.googleapis.com/v1/places:searchText"

FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.location",
    "places.websiteUri",
    "places.nationalPhoneNumber",
    "places.types",
    "places.rating",
    "places.userRatingCount",
    "nextPageToken",
])

SEARCH_QUERIES = [
    "restaurants in Lebanon",
    "hotels in Lebanon",
    "banks in Lebanon",
    "hospitals in Lebanon",
    "pharmacies in Lebanon",
    "schools in Lebanon",
    "gyms in Lebanon",
    "real estate offices in Lebanon",
    "law firms in Lebanon",
    "clinics in Lebanon",
    "supermarkets in Lebanon",
    "shopping malls in Lebanon",
    "car dealerships in Lebanon",
    "accounting firms in Lebanon",
    "insurance companies in Lebanon",
    "construction companies in Lebanon",
    "travel agencies in Lebanon",
    "beauty salons in Lebanon",
]


class GooglePlacesScraper(BaseScraper):
    def __init__(self):
        self._api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "")

    def scrape(self) -> Iterator[BusinessRecord]:
        if not self._api_key:
            print("[Google] GOOGLE_PLACES_API_KEY not set, skipping.")
            return

        scraped_at = datetime.datetime.utcnow().isoformat() + "Z"
        print(f"[Google] Scraping {len(SEARCH_QUERIES)} categories via Places API...")

        session = requests.Session()
        session.headers.update({
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": FIELD_MASK,
            "Content-Type": "application/json",
        })

        seen_ids: set[str] = set()

        for query in SEARCH_QUERIES:
            yield from self._scrape_query(session, query, seen_ids, scraped_at)
            time.sleep(0.5)

    def _scrape_query(self, session, query: str, seen_ids: set, scraped_at: str) -> Iterator[BusinessRecord]:
        page_token = None
        page = 0

        while True:
            body = {"textQuery": query}
            if page_token:
                body["pageToken"] = page_token

            try:
                resp = session.post(API_URL, json=body, timeout=30)
                if resp.status_code == 401:
                    print("[Google] Invalid API key.")
                    return
                if resp.status_code == 429:
                    print("[Google] Rate limited, waiting 60s...")
                    time.sleep(60)
                    continue
                resp.raise_for_status()
            except requests.RequestException as e:
                print(f"[Google] Request error for '{query}': {e}")
                return

            data = resp.json()
            places = data.get("places", [])
            page += 1
            print(f"[Google] '{query}' page {page}: {len(places)} results")

            for place in places:
                place_id = place.get("id")
                if not place_id or place_id in seen_ids:
                    continue
                seen_ids.add(place_id)

                name = (place.get("displayName") or {}).get("text")
                if not name:
                    continue

                loc = place.get("location") or {}
                lat = loc.get("latitude")
                lon = loc.get("longitude")

                types = place.get("types", [])
                category = types[0].replace("_", " ") if types else None

                website = place.get("websiteUri")
                phone = place.get("nationalPhoneNumber")
                address = place.get("formattedAddress")
                rating = place.get("rating")
                review_count = place.get("userRatingCount")

                yield BusinessRecord(
                    name=name,
                    category=category,
                    address=address,
                    region=None,
                    lat=lat,
                    lon=lon,
                    phone=phone,
                    email=None,
                    website=website,
                    website_live=None,
                    facebook=None,
                    instagram=None,
                    rating=rating,
                    review_count=review_count,
                    source="google_places",
                    scraped_at=scraped_at,
                    completeness_score=0,
                )

            page_token = data.get("nextPageToken")
            if not page_token:
                break
            time.sleep(2)
