"""Factory for the default CompanyProvider chain.

Single function to call from the rest of the codebase:
    from market_notification.companies.factory import default_company_provider
    cp = default_company_provider()

Returns a singleton on first call.
"""
from __future__ import annotations

from functools import lru_cache

from market_notification.companies.base import CompanyProvider
from market_notification.companies.composite import CompositeCompanyProvider
from market_notification.companies.csv_source import CsvCompanyProvider
from market_notification.companies.screener_original_source import (
    ScreenerOriginalCompanyProvider,
)


@lru_cache(maxsize=1)
def default_company_provider() -> CompanyProvider:
    """The default chain for v1.0:
    1) CsvCompanyProvider — identity, sector, static mcap.
    2) ScreenerOriginalCompanyProvider — prices, mcap time-series.
    """
    return CompositeCompanyProvider(
        [
            CsvCompanyProvider(),
            ScreenerOriginalCompanyProvider(),
        ]
    )


def reset_default_company_provider() -> None:
    """For tests / config-edit reload."""
    default_company_provider.cache_clear()
