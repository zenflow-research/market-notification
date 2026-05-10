"""Read-only company + fundamentals + price provider contracts."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class CompanyDTO:
    company_id: int  # = DataCompanyID
    short_id: str  # NSE-friendly tag
    company_name: str
    company_fullname: str
    bse_code: Optional[str]
    nse_code: Optional[str]
    isin: Optional[str]
    sector: Optional[str]
    industry: Optional[str]
    basic_industry: Optional[str]
    macro: Optional[str]
    mcap_crores: Optional[float]


@dataclass(frozen=True)
class FundamentalsDTO:
    company_id: int
    mcap_crores: Optional[float]
    quarterly_sales: Optional[float]
    annual_sales_approx: Optional[float]
    ebitda: Optional[float]
    ebitda_margin: Optional[float]
    pat: Optional[float]
    pat_margin: Optional[float]
    eps: Optional[float]
    roce: Optional[float]
    roe: Optional[float]
    debt_total: Optional[float]
    debt_net: Optional[float]
    fcf_latest: Optional[float]
    fcf_3y: Optional[float]
    capex_latest: Optional[float]
    capex_5y: Optional[float]
    pe: Optional[float]
    dividend_yield: Optional[float]
    promoter_pct: Optional[float]
    fii_pct: Optional[float]
    dii_pct: Optional[float]
    sales_cagr_3y: Optional[float]
    sales_cagr_5y: Optional[float]
    pat_cagr_3y: Optional[float]
    pat_cagr_5y: Optional[float]


@dataclass(frozen=True)
class PriceBar:
    bar_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float
    deliveries: Optional[float]


@dataclass(frozen=True)
class PriceSeriesDTO:
    company_id: int
    bars: list[PriceBar]
    days: int


class CompanyProvider(ABC):
    """Read-only access to company + fundamentals + price data.

    Implementations chain multiple sources (CSV, brain SQLite, Screener_original).
    They MUST never write to the underlying source.
    """

    @abstractmethod
    def get_by_bse_code(self, code: str) -> Optional[CompanyDTO]: ...

    @abstractmethod
    def get_by_nse_symbol(self, symbol: str) -> Optional[CompanyDTO]: ...

    @abstractmethod
    def get_by_isin(self, isin: str) -> Optional[CompanyDTO]: ...

    @abstractmethod
    def get_by_company_id(self, company_id: int) -> Optional[CompanyDTO]: ...

    @abstractmethod
    def get_fundamentals(self, company_id: int) -> Optional[FundamentalsDTO]: ...

    @abstractmethod
    def get_price_series(
        self, company_id: int, days: int = 90
    ) -> Optional[PriceSeriesDTO]: ...
