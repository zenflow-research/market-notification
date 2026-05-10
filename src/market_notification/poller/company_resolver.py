"""Resolve a RawNotification to a `company_id` (FR-INGEST-003).

Tries, in order:
  1. BSE code -> company (when source='BSE')
  2. NSE symbol -> company (when source='NSE')
  3. ISIN -> company (fallback for either source)

Returns 0 (sentinel) when no match is found. Failed-to-map rows are still
inserted -- they appear in the `unmapped` UI bucket per FR-INGEST-003.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..companies.base import CompanyProvider
from ..exchange.base import RawNotification

logger = logging.getLogger(__name__)

UNMAPPED_COMPANY_ID = 0


class CompanyResolver:
    """Wraps a CompanyProvider with the BSE/NSE/ISIN fallback chain."""

    def __init__(self, provider: CompanyProvider) -> None:
        self.provider = provider

    def resolve(self, raw: RawNotification) -> int:
        """Return company_id, or UNMAPPED_COMPANY_ID (0) if nothing matched."""
        cid = self._try_primary(raw)
        if cid is not None:
            return cid
        if raw.isin:
            via_isin = self._safe_call("get_by_isin", raw.isin)
            if via_isin is not None:
                return via_isin
        logger.debug(
            "Unmapped notification: source=%s symbol=%s isin=%s",
            raw.source, raw.symbol, raw.isin,
        )
        return UNMAPPED_COMPANY_ID

    def _try_primary(self, raw: RawNotification) -> Optional[int]:
        if not raw.symbol:
            return None
        if raw.source == "BSE":
            return self._safe_call("get_by_bse_code", raw.symbol)
        if raw.source == "NSE":
            return self._safe_call("get_by_nse_symbol", raw.symbol)
        return None

    def _safe_call(self, method_name: str, key: str) -> Optional[int]:
        method = getattr(self.provider, method_name, None)
        if method is None:
            return None
        try:
            company = method(key)
        except Exception as e:  # noqa: BLE001 -- provider may raise on missing data
            logger.warning("CompanyProvider.%s(%r) raised: %s", method_name, key, e)
            return None
        if company is None:
            return None
        # CompanyDTO has either company_id (int) or id field
        cid = getattr(company, "company_id", None) or getattr(company, "id", None)
        try:
            return int(cid) if cid is not None else None
        except (TypeError, ValueError):
            return None
