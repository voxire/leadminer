import datetime
import os
import time
import requests
from typing import Iterator
from .base import BaseScraper, BusinessRecord

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

SPARQL_QUERY = """
SELECT ?item ?itemLabel ?websiteLabel ?phoneLabel ?emailLabel ?addressLabel ?categoryLabel WHERE {
  ?item wdt:P17 wd:Q822.
  OPTIONAL { ?item wdt:P856 ?website. }
  OPTIONAL { ?item wdt:P1329 ?phone. }
  OPTIONAL { ?item wdt:P968 ?email. }
  OPTIONAL { ?item wdt:P6375 ?address. }
  OPTIONAL {
    ?item wdt:P31 ?category.
    ?category rdfs:label ?categoryLabel.
    FILTER(LANG(?categoryLabel) = "en")
  }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,ar". }
  FILTER(?item != wd:Q822)
}
LIMIT 5000
"""


class WikidataScraper(BaseScraper):
    def scrape(self) -> Iterator[BusinessRecord]:
        scraped_at = datetime.datetime.utcnow().isoformat() + "Z"
        print("[Wikidata] Fetching Lebanon businesses...")

        email = os.environ.get("SCRAPER_EMAIL", "voxire.tech@gmail.com")
        headers = {
            "User-Agent": f"leadminer/1.0 ({email})",
            "Accept": "application/sparql-results+json",
        }

        for attempt in range(3):
            try:
                resp = requests.get(
                    SPARQL_ENDPOINT,
                    params={"query": SPARQL_QUERY, "format": "json"},
                    headers=headers,
                    timeout=90,
                )
                resp.raise_for_status()
                break
            except requests.RequestException as e:
                print(f"[Wikidata] Attempt {attempt + 1} failed: {e}")
                if attempt < 2:
                    time.sleep(15)
                else:
                    print("[Wikidata] All retries exhausted, skipping.")
                    return

        results = resp.json().get("results", {}).get("bindings", [])
        print(f"[Wikidata] Got {len(results)} results.")

        for row in results:
            name = row.get("itemLabel", {}).get("value")
            if not name or name.startswith("Q"):
                continue

            website = row.get("websiteLabel", {}).get("value")
            phone = row.get("phoneLabel", {}).get("value")
            email = row.get("emailLabel", {}).get("value")
            address = row.get("addressLabel", {}).get("value")
            category = row.get("categoryLabel", {}).get("value")

            if website and not website.startswith("http"):
                website = "https://" + website

            yield BusinessRecord(
                name=name,
                category=category,
                address=address,
                region=None,
                country="LB",
                lat=None,
                lon=None,
                phone=phone,
                email=email,
                website=website,
                website_live=None,
                facebook=None,
                instagram=None,
                whatsapp=None,
                linkedin=None,
                rating=None,
                review_count=None,
                industry_priority=None,
                recommended_service=None,
                lead_score=0,
                source="wikidata",
                scraped_at=scraped_at,
                completeness_score=0,
            )
