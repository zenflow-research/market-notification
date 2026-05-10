"""Abstract base classes and DTOs for exchange (BSE/NSE) fetchers.

Implementations live in `bse_fetcher.py`, `nse_fetcher.py`. Each is a cleaned
copy of brain's fetcher with only the methods we need at runtime.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Optional


@dataclass(frozen=True)
class RawNotification:
    """Normalized representation of one corporate filing.

    Both BSE and NSE responses are mapped into this DTO before any pipeline
    work happens. New fields go here; per-source quirks stay in the fetcher.
    """

    source: str  # 'BSE' or 'NSE'
    seq_id: Optional[str]
    headline: str
    category: Optional[str]
    subcategory: Optional[str]
    body: Optional[str]
    announced_at: datetime
    exchange_disseminated_at: Optional[datetime]
    attachment_url: Optional[str]
    attachment_name: Optional[str]
    attachment_size: Optional[str]
    is_critical: bool
    has_xbrl: bool
    symbol: Optional[str]
    company_name_raw: Optional[str]
    isin: Optional[str]
    industry_raw: Optional[str]
    raw_json: str  # original API response, JSON-serialized for audit


class ExchangeFetcher(ABC):
    """Public contract every exchange fetcher must satisfy."""

    source: ClassVar[str]  # 'BSE' or 'NSE'

    @abstractmethod
    def fetch_latest(self, n: int = 50) -> list[RawNotification]:
        """Return the N most recent filings. May be slightly stale."""

    @abstractmethod
    def fetch_for_date(self, date_yyyymmdd: str) -> list[RawNotification]:
        """Return all filings for a specific calendar date (IST)."""

    @abstractmethod
    def fetch_attachment(self, url: str) -> bytes:
        """Download a single attachment by URL. Returns file bytes."""
