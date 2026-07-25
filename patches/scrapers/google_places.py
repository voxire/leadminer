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
import time
from typing import Iterator

import requests

from .base import BaseScraper, BusinessRecord
from .whitelist import is_business_category

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

# Lebanon region inference - rough lat/lon boxes
# (Replace with proper polygons later if precision matters)
LEBANON_REGIONS = [
    # (name, min_lat, max_lat, min_lon, max_lon)
    ("Beirut",         33.85, 33.92, 35.45, 35.55),
    ("Mount Lebanon",  33.70, 34.20, 35.40, 35.85),
    ("North Lebanon",  34.20, 34.70, 35.70, 36.30),
    ("Akkar",          34.40, 34.70, 35.90, 36.50),
    ("Bekaa",          33.60, 34.40, 35.60, 36.50),
    ("Baalbek-Hermel", 34.00, 34.65, 36.00, 36.70),
    ("South Lebanon",  33.05, 33.55, 35.10, 35.65),
    ("Nabatieh",       33.20, 33.70, 35.30, 35.75),
]

# KSA region inference
KSA_REGIONS = [
    # (name, min_lat, max_lat, min_lon, max_lon)
    ("Riyadh", 24.40, 25.20, 46.40, 47.20),
    ("Jeddah", 21.30, 21.80, 39.05, 39.45),
    ("Dammam", 26.20, 26.65, 49.85, 50.30),
    ("Mecca",  21.30, 21.55, 39.75, 40.00),
    ("Medina", 24.30, 24.65, 39.45, 39.80),
]


def _infer_region(lat: float | None, lon: float | None, country: str) -> str | None:
    if lat is None or lon is None:
        return None
    boxes = LEBANON_REGIONS if country == "LB" else KSA_REGIONS if country == "SA" else []
    for name, min_lat, max_lat, min_lon, max_lon in boxes:
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            return name
    return None


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


class GooglePlacesScraper(BaseScraper):
    def __init__(self) -> None:
        self._api_key = os.environ.get("GOOGLE_PLACES_API_KEY")

    def scrape(self) -> Iterator[BusinessRecord]:
        if not self._api_key:
            print("[Google] GOOGLE_PLACES_API_KEY not set, skipping.")
            return

        scraped_at = datetime.datetime.utcnow().isoformat() + "Z"
        all_queries = LEBANON_QUERIES + KSA_QUERIES
        print(
            f"[Google] Scraping {len(all_queries)} queries "
            f"({len(LEBANON_QUERIES)} LB, {len(KSA_QUERIES)} SA) via Places API..."
        )

        session = requests.Session()
        session.headers.update({
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": FIELD_MASK,
            "Content-Type": "application/json",
        })

        seen_ids: set[str] = set()
        kept = 0
        skipped = 0

        for query in all_queries:
            country = _country_from_query(query)
            for record in self._scrape_query(session, query, seen_ids, scraped_at, country):
                if not is_business_category(record.category):
                    skipped += 1
                    continue
                kept += 1
                yield record
            time.sleep(0.5)  # polite to the API

        print(f"[Google] Done. Kept {kept}, skipped {skipped} (whitelist).")

    def _scrape_query(
        self,
        session: requests.Session,
        query: str,
        seen_ids: set,
        scraped_at: str,
        country: str,
    ) -> Iterator[BusinessRecord]:
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

                name = (place.get("displayName") or {}).get("text") or ""
                if not name:
                    continue

                location = place.get("location") or {}
                lat = location.get("latitude")
                lon = location.get("longitude")
                region = _infer_region(lat, lon, country)

                # Pick the most specific Google "type" as our category
                types = place.get("types") or []
                category = _pick_category(types)

                yield BusinessRecord(
                    name=name,
                    category=category,
                    region=region,
                    country=country,
                    address=place.get("formattedAddress"),
                    lat=lat,
                    lon=lon,
                    phone=place.get("nationalPhoneNumber"),
                    email=None,  # Places API doesn't return email
                    website=place.get("websiteUri"),
                    website_live=None,
                    facebook=None,
                    instagram=None,
                    rating=place.get("rating"),
                    review_count=place.get("userRatingCount"),
                    industry_priority=None,
                    recommended_service=None,
                    source="google_places",
                    scraped_at=scraped_at,
                    completeness_score=0,
                )

            page_token = data.get("nextPageToken")
            if not page_token:
                return
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
