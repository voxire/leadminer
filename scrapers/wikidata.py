import logging
from typing import Iterator

from httpclient import fetch_with_retry, get_session, utc_now_iso
from .base import BaseScraper, BusinessRecord

log = logging.getLogger(__name__)

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
        scraped_at = utc_now_iso()
        print("[Wikidata] Fetching Lebanon businesses...")

        result = fetch_with_retry(
            "GET",
            SPARQL_ENDPOINT,
            session=get_session(),
            params={"query": SPARQL_QUERY, "format": "json"},
            timeout=90,
            retries=3,
            headers={"Accept": "application/sparql-results+json"},
        )
        if not result.ok:
            print(
                f"[Wikidata] FAILED after {result.attempts} attempts: {result.error}. "
                "Continuing without Wikidata data.",
                flush=True,
            )
            return

        results = result.json().get("results", {}).get("bindings", [])
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
