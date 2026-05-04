"""
Google Places API scraper.

Scrapes Lebanon and KSA (Riyadh, Jeddah, Dammam) using the Places Text Search API.
Industry queries are tilted per market:
  - Lebanon: full priority list (F&B, hospitality, healthcare, real estate, services)
  - KSA: e-commerce + hospitality + fintech + real estate weighted (per Sales Playbook)

Region tagging (set on each record):
  - "Beirut" / "Mount Lebanon" / etc. inferred from coordinates for Lebanon
  - "Riyadh" / "Jeddah" / "Dammam" for KSA

Requires GOOGLE_PLACES_API_KEY environment variable.
Set it in GitHub Actions Settings -> Secrets and variables -> Actions.
"""

import datetime
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterator

import requests

from enricher import infer_region
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

# ---- Lebanon queries (full priority + adjacent) ----
LEBANON_QUERIES = [
    "restaurants in Lebanon",
    "cafes in Lebanon",
    "hotels in Lebanon",
    "boutique hotels in Lebanon",
    "boutiques in Lebanon",
    "fashion stores in Lebanon",
    "jewelry stores in Lebanon",
    "beauty salons in Lebanon",
    "cosmetic clinics in Lebanon",
    "dental clinics in Lebanon",
    "fertility clinics in Lebanon",
    "hospitals in Lebanon",
    "pharmacies in Lebanon",
    "real estate offices in Lebanon",
    "real estate developers in Lebanon",
    "law firms in Lebanon",
    "accounting firms in Lebanon",
    "consulting firms in Lebanon",
    "marketing agencies in Lebanon",
    "architecture firms in Lebanon",
    "schools in Lebanon",
    "language schools in Lebanon",
    "gyms in Lebanon",
    "yoga studios in Lebanon",
    "supermarkets in Lebanon",
    "bakeries in Lebanon",
    "car dealerships in Lebanon",
    "insurance companies in Lebanon",
    "travel agencies in Lebanon",
    "wedding venues in Lebanon",
    "photography studios in Lebanon",
    "tech startups in Lebanon",
    "co-working spaces in Lebanon",
]

# ---- KSA queries (e-commerce + hospitality + fintech + real estate weighted) ----
KSA_QUERIES = [
    # E-commerce and DTC (high priority for KSA)
    "fashion brands in Riyadh",
    "fashion brands in Jeddah",
    "perfume brands in Saudi Arabia",
    "beauty brands in Saudi Arabia",
    "jewelry brands in Riyadh",
    "DTC brands in Saudi Arabia",
    "online retail brands in Riyadh",
    # Hospitality and F&B groups (large operators)
    "restaurant groups in Riyadh",
    "restaurant groups in Jeddah",
    "boutique hotels in Riyadh",
    "boutique hotels in Jeddah",
    "luxury hotels in Saudi Arabia",
    "cafe chains in Saudi Arabia",
    # Real estate developers
    "real estate developers in Riyadh",
    "real estate developers in Jeddah",
    "property developers in Saudi Arabia",
    "real estate brokers in Riyadh",
    # Fintech and tech startups
    "fintech startups in Saudi Arabia",
    "fintech companies in Riyadh",
    "insurtech companies in Saudi Arabia",
    "tech startups in Riyadh",
    "SaaS companies in Saudi Arabia",
    # Healthcare networks (large, multi-location)
    "healthcare clinics in Riyadh",
    "dental clinic networks in Saudi Arabia",
    "cosmetic clinics in Riyadh",
    "fertility clinics in Saudi Arabia",
    # Adjacent
    "law firms in Riyadh",
    "consulting firms in Saudi Arabia",
    "marketing agencies in Riyadh",
    "co-working spaces in Riyadh",
    "events venues in Riyadh",
    # Dammam (smaller priority but covered)
    "restaurants in Dammam",
    "real estate developers in Dammam",
    "hotels in Dammam",
]

def _country_from_query(query: str) -> str:
    q = query.lower()
    if "lebanon" in q or "beirut" in q:
        return "LB"
    if (
        "saudi" in q or "riyadh" in q or "jeddah" in q or "dammam" in q
        or "mecca" in q or "medina" in q
    ):
        return "SA"
    return "LB"  # default


# Max parallel workers for Google Places queries.
# The Places API (New) doesn't publish a hard QPS limit but 5 concurrent
# requests is conservative enough to avoid 429s in practice.
_WORKERS = 5


class GooglePlacesScraper(BaseScraper):
    def __init__(self) -> None:
        self._api_key = os.environ.get("GOOGLE_PLACES_API_KEY")

    def _make_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": FIELD_MASK,
            "Content-Type": "application/json",
        })
        return s

    def scrape(self) -> Iterator[BusinessRecord]:
        if not self._api_key:
            print("[Google] GOOGLE_PLACES_API_KEY not set, skipping.")
            return

        scraped_at = datetime.datetime.utcnow().isoformat() + "Z"
        all_queries = LEBANON_QUERIES + KSA_QUERIES
        print(
            f"[Google] Scraping {len(all_queries)} queries "
            f"({len(LEBANON_QUERIES)} LB, {len(KSA_QUERIES)} SA) "
            f"with {_WORKERS} workers..."
        )

        seen_ids: set[str] = set()
        seen_lock = threading.Lock()
        all_records: list[BusinessRecord] = []
        records_lock = threading.Lock()

        def fetch_query(query: str) -> None:
            country = _country_from_query(query)
            # Each thread gets its own session (requests.Session is not thread-safe)
            session = self._make_session()
            results = list(self._scrape_query(session, query, seen_ids, seen_lock, scraped_at, country))
            with records_lock:
                all_records.extend(results)

        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            futures = [pool.submit(fetch_query, q) for q in all_queries]
            for f in as_completed(futures):
                f.result()  # re-raises any exception

        yield from all_records

    def _scrape_query(
        self,
        session: requests.Session,
        query: str,
        seen_ids: set,
        seen_lock: threading.Lock,
        scraped_at: str,
        country: str,
    ) -> list[BusinessRecord]:
        records: list[BusinessRecord] = []
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
                    return records
                if resp.status_code == 429:
                    print(f"[Google] Rate limited on '{query}', waiting 30s...")
                    time.sleep(30)
                    continue
                resp.raise_for_status()
            except requests.RequestException as e:
                print(f"[Google] Request error for '{query}': {e}")
                return records

            data = resp.json()
            places = data.get("places", [])
            page += 1
            print(f"[Google] '{query}' page {page}: {len(places)} results")

            for place in places:
                place_id = place.get("id")
                if not place_id:
                    continue
                with seen_lock:
                    if place_id in seen_ids:
                        continue
                    seen_ids.add(place_id)

                name = (place.get("displayName") or {}).get("text") or ""
                if not name:
                    continue

                location = place.get("location") or {}
                lat = location.get("latitude")
                lon = location.get("longitude")
                region = infer_region(None, lat, lon, country=country)

                # Pick the most specific Google "type" as our category
                types = place.get("types") or []
                category = _pick_category(types)

                records.append(BusinessRecord(
                    name=name,
                    category=category,
                    region=region,
                    country=country,
                    address=place.get("formattedAddress"),
                    lat=lat,
                    lon=lon,
                    phone=place.get("nationalPhoneNumber"),
                    email=None,
                    website=place.get("websiteUri"),
                    website_live=None,
                    facebook=None,
                    instagram=None,
                    whatsapp=None,
                    linkedin=None,
                    rating=place.get("rating"),
                    review_count=place.get("userRatingCount"),
                    industry_priority=None,
                    recommended_service=None,
                    lead_score=0,
                    source="google_places",
                    scraped_at=scraped_at,
                    completeness_score=0,
                ))

            page_token = data.get("nextPageToken")
            if not page_token:
                return records
            time.sleep(2)  # required between paginated requests


# Google Places "types" are ordered most-specific first
# Drop generic ones, prefer concrete categories
_GENERIC_TYPES = {
    "establishment", "point_of_interest", "place", "premise",
    "subpremise", "street_address", "route", "country",
    "administrative_area_level_1", "administrative_area_level_2",
    "administrative_area_level_3", "locality", "sublocality",
    "neighborhood", "postal_code",
}


def _pick_category(types: list[str]) -> str:
    for t in types:
        if t and t not in _GENERIC_TYPES:
            return t
    return types[0] if types else "business"
