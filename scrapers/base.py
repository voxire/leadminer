from abc import ABC, abstractmethod
from typing import Iterator, TypedDict


class BusinessRecord(TypedDict):
    name: str | None
    category: str | None
    address: str | None
    region: str | None
    country: str | None
    lat: float | None
    lon: float | None
    phone: str | None
    email: str | None
    website: str | None
    website_live: bool | None
    facebook: str | None
    instagram: str | None
    whatsapp: str | None
    linkedin: str | None
    rating: float | None
    review_count: int | None
    industry_priority: str | None
    recommended_service: str | None
    lead_score: int
    source: str
    scraped_at: str
    completeness_score: int


class BaseScraper(ABC):
    @abstractmethod
    def scrape(self) -> Iterator[BusinessRecord]: ...
