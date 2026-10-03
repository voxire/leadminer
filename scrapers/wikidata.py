import logging
from typing import Iterator

from httpclient import fetch_with_retry, get_session, utc_now_iso
from .base import BaseScraper, BusinessRecord

log = logging.getLogger(__name__)

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

# Select the variables the OPTIONALs actually bind.
#
# This previously projected ?websiteLabel/?phoneLabel/?emailLabel/?addressLabel
# while binding ?website/?phone/?email/?address. Wikidata's label service only
# synthesises *Label bindings for *entity-valued* variables (Q-items) -- never
# for URLs or plain literals. Every one of those four fields was therefore
# permanently unbound and the scraper had never once extracted a website, phone,
# email or address. Labels are now reserved for genuinely entity-valued
# properties (?item, ?category).
#
# Value formats that must be unwrapped:
#   P1329 phone number -> RFC3966, e.g. "tel:+961-3-123456"
#   P968  email        -> "mailto:someone@example.com"
SPARQL_QUERY = """
SELECT ?item ?itemLabel ?website ?phone ?email ?address ?categoryLabel
       ?coord ?lat ?lon ?employees ?inception WHERE {
  ?item wdt:P17 wd:Q822.
  OPTIONAL { ?item wdt:P856 ?website. }
  OPTIONAL { ?item wdt:P1329 ?phone. }
  OPTIONAL { ?item wdt:P968 ?email. }
  OPTIONAL { ?item wdt:P6375 ?address. }
  OPTIONAL { ?item wdt:P625 ?coord. }
  OPTIONAL { ?item wdt:P1128 ?employees. }
  OPTIONAL { ?item wdt:P571 ?inception. }
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


def _strip_prefix(value: str | None, prefix: str) -> str | None:
    if not value:
        return None
    v = value.strip()
    return v[len(prefix):] if v.lower().startswith(prefix) else v


def _parse_point(coord: str | None) -> tuple[float | None, float | None]:
    """Wikidata point values look like 'Point(35.50 33.89)' (lon lat)."""
    if not coord or not coord.startswith("Point("):
        return None, None
    try:
        lon_s, lat_s = coord[6:].rstrip(")").split()
        return float(lat_s), float(lon_s)
    except (ValueError, IndexError):
        return None, None


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

            # Read the variables the query actually binds. See SPARQL_QUERY.
            website = row.get("website", {}).get("value")
            phone = _strip_prefix(row.get("phone", {}).get("value"), "tel:")
            email = _strip_prefix(row.get("email", {}).get("value"), "mailto:")
            address = row.get("address", {}).get("value")
            category = row.get("categoryLabel", {}).get("value")
            lat, lon = _parse_point(row.get("coord", {}).get("value"))

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
