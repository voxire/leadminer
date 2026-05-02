from abc import ABC, abstractmethod
from typing import Iterator, TypedDict


class BusinessRecord(TypedDict):
    name: str | None
    category: str | None
    address: str | None
    phone: str | None
    email: str | None
    website: str | None
    source: str
    scraped_at: str


class BaseScraper(ABC):
    @abstractmethod
    def scrape(self) -> Iterator[BusinessRecord]: ...
