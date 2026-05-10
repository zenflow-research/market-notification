"""Composite provider — chains multiple CompanyProviders.

For each method, calls providers in order and returns the first non-None
result. Identity comes from CSV; prices/fundamentals from Screener_original.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from market_notification.companies.base import (
    CompanyDTO,
    CompanyProvider,
    FundamentalsDTO,
    PriceSeriesDTO,
)

log = logging.getLogger(__name__)


class CompositeCompanyProvider(CompanyProvider):
    """Tries each inner provider in order; first non-None result wins.

    Recommended chain (Phase 1):
      [CsvCompanyProvider, ScreenerOriginalCompanyProvider]

    The ABC's full surface is covered because at least one of the inner
    providers returns a non-None for each method (CSV for identity,
    Screener_original for prices).
    """

    def __init__(self, providers: Iterable[CompanyProvider]) -> None:
        self.providers: list[CompanyProvider] = list(providers)
        if not self.providers:
            raise ValueError("CompositeCompanyProvider requires >=1 inner provider")

    def get_by_bse_code(self, code: str) -> Optional[CompanyDTO]:
        for p in self.providers:
            r = p.get_by_bse_code(code)
            if r is not None:
                return r
        return None

    def get_by_nse_symbol(self, symbol: str) -> Optional[CompanyDTO]:
        for p in self.providers:
            r = p.get_by_nse_symbol(symbol)
            if r is not None:
                return r
        return None

    def get_by_isin(self, isin: str) -> Optional[CompanyDTO]:
        for p in self.providers:
            r = p.get_by_isin(isin)
            if r is not None:
                return r
        return None

    def get_by_company_id(self, company_id: int) -> Optional[CompanyDTO]:
        for p in self.providers:
            r = p.get_by_company_id(company_id)
            if r is not None:
                return r
        return None

    def get_fundamentals(self, company_id: int) -> Optional[FundamentalsDTO]:
        # Prefer richer providers later in the chain by merging non-None fields.
        merged: Optional[FundamentalsDTO] = None
        for p in self.providers:
            r = p.get_fundamentals(company_id)
            if r is None:
                continue
            if merged is None:
                merged = r
                continue
            # Field-wise overlay: later non-None wins
            merged_dict = {
                k: (getattr(r, k) if getattr(r, k) is not None else getattr(merged, k))
                for k in merged.__dataclass_fields__  # type: ignore[attr-defined]
            }
            merged = FundamentalsDTO(**merged_dict)
        return merged

    def get_price_series(
        self, company_id: int, days: int = 90
    ) -> Optional[PriceSeriesDTO]:
        for p in self.providers:
            r = p.get_price_series(company_id, days=days)
            if r is not None:
                return r
        return None
