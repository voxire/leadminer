import datetime
import random
import re
import time
import requests
from bs4 import BeautifulSoup
from typing import Iterator
from .base import BaseScraper, BusinessRecord

BASE_URL = "https://www.yellowpages.com.lb"

CATEGORIES = [
    "restaurants",
    "hotels",
    "banks",
    "hospitals",
    "real-estate",
    "lawyers",
    "doctors",
    "supermarkets",
    "gyms",
    "pharmacies",
    "schools",
    "clinics",
    "engineers",
    "architects",
    "construction",
    "insurance",
    "accounting",
    "travel-agencies",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

MAX_PAGES = 50


def _clean_website(url: str | None) -> str | None:
    if not url:
        return None
    # filter out yellowpages own tracking/redirect URLs
    if "yellowpages.com.lb" in url and "/redirect" in url:
        return None
    if url.startswith("/"):
        return None
    if not url.startswith("http"):
        url = "https://" + url
    return url


def _extract_text(tag) -> str | None:
    if tag is None:
        return None
    text = tag.get_text(separator=" ", strip=True)
    return text if text else None


class YellowPagesScraper(BaseScraper):
    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        })

    def _rotate_ua(self):
        self._session.headers["User-Agent"] = random.choice(USER_AGENTS)

    def _get_page(self, url: str, retries: int = 3) -> BeautifulSoup | None:
        self._rotate_ua()
        for attempt in range(retries):
            try:
                resp = self._session.get(url, timeout=30)
                if resp.status_code == 429:
                    wait = 30 + attempt * 30
                    print(f"[YP] Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                    continue
                if resp.status_code != 200:
                    print(f"[YP] HTTP {resp.status_code} for {url}")
                    return None
                return BeautifulSoup(resp.text, "lxml")
            except requests.RequestException as e:
                print(f"[YP] Request error (attempt {attempt + 1}): {e}")
                time.sleep(10)
        return None

    def _parse_listing(self, card, category: str, scraped_at: str) -> BusinessRecord | None:
        # Try multiple possible selectors for name
        name_tag = (
            card.select_one("h2.listing-name a")
            or card.select_one("h3.listing-name a")
            or card.select_one(".company-name a")
            or card.select_one("h2 a")
            or card.select_one("h3 a")
            or card.select_one(".title a")
        )
        name = _extract_text(name_tag)
        if not name:
            return None

        phone_tag = (
            card.select_one("a[href^='tel:']")
            or card.select_one(".phone")
            or card.select_one(".tel")
        )
        phone = None
        if phone_tag:
            phone = phone_tag.get("href", "").replace("tel:", "").strip() or _extract_text(phone_tag)

        website_tag = (
            card.select_one("a.website")
            or card.select_one("a[class*='website']")
            or card.select_one("a[href*='http']:not([href*='yellowpages'])")
        )
        website = _clean_website(website_tag.get("href") if website_tag else None)

        email_tag = card.select_one("a[href^='mailto:']")
        email = email_tag.get("href", "").replace("mailto:", "").strip() if email_tag else None

        address_tag = (
            card.select_one(".address")
            or card.select_one(".location")
            or card.select_one("[class*='address']")
        )
        address = _extract_text(address_tag)

        return BusinessRecord(
            name=name,
            category=category,
            address=address,
            phone=phone or None,
            email=email or None,
            website=website,
            source="yellowpages_lb",
            scraped_at=scraped_at,
        )

    def _scrape_category(self, category: str, scraped_at: str) -> Iterator[BusinessRecord]:
        print(f"[YP] Scraping category: {category}")
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE_URL}/search?keyword={category}&location=Lebanon&page={page}"
            soup = self._get_page(url)
            if soup is None:
                break

            cards = (
                soup.select("div.listing-item")
                or soup.select("article.listing")
                or soup.select(".business-listing")
                or soup.select("[class*='listing-item']")
                or soup.select("[class*='business-card']")
            )

            if not cards:
                print(f"[YP] No cards found on page {page} for '{category}', stopping.")
                break

            for card in cards:
                record = self._parse_listing(card, category, scraped_at)
                if record:
                    yield record

            print(f"[YP] {category} page {page}: {len(cards)} listings")
            time.sleep(random.uniform(3, 7))

    def scrape(self) -> Iterator[BusinessRecord]:
        scraped_at = datetime.datetime.utcnow().isoformat() + "Z"
        for category in CATEGORIES:
            yield from self._scrape_category(category, scraped_at)
            time.sleep(random.uniform(5, 10))
