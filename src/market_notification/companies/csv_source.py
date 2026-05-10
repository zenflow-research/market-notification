"""CSV-backed identity provider.

Reads `company_sector_mapping_master.csv` (path from settings) and exposes
fast lookups by BSE code, NSE symbol, ISIN, and DataCompanyID.

The CSV is the single source of truth for: company_id, short_id, name,
exchange codes, ISIN, sector taxonomy, and static mcap.

Loaded once at construction; reload via `reload()`.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from market_notification.companies.base import (
    CompanyDTO,
    CompanyProvider,
    FundamentalsDTO,
    PriceSeriesDTO,
)
from market_notification.config.settings import get_settings

log = logging.getLogger(__name__)


def _safe_int(val) -> Optional[int]:
    if val is None or pd.isna(val):
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _safe_float(val) -> Optional[float]:
    if val is None or pd.isna(val):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _safe_str(val) -> Optional[str]:
    if val is None or pd.isna(val):
        return None
    s = str(val).strip()
    return s or None


class CsvCompanyProvider(CompanyProvider):
    """Identity-only provider. Returns None for fundamentals and price.

    Use `CompositeCompanyProvider` to combine this with
    `ScreenerOriginalCompanyProvider` for prices, etc.
    """

    def __init__(self, csv_path: Optional[Path] = None) -> None:
        self.csv_path = Path(csv_path or get_settings().paths.company_sector_mapping_csv)
        self._df: Optional[pd.DataFrame] = None
        self._by_bse: dict[str, int] = {}
        self._by_nse: dict[str, int] = {}
        self._by_isin: dict[str, int] = {}
        self._by_id: dict[int, dict] = {}
        self.reload()

    def reload(self) -> None:
        if not self.csv_path.exists():
            raise FileNotFoundError(f"Company CSV not found: {self.csv_path}")
        log.info("Loading company CSV: %s", self.csv_path)
        df = pd.read_csv(self.csv_path, dtype=str)
        # numeric columns we want
        for col in ("mcap", "DataCompanyID", "BSE Code"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        self._df = df

        self._by_bse.clear()
        self._by_nse.clear()
        self._by_isin.clear()
        self._by_id.clear()

        for _, row in df.iterrows():
            cid = _safe_int(row.get("DataCompanyID"))
            if cid is None:
                continue
            self._by_id[cid] = self._row_to_dict(row)
            bse = _safe_int(row.get("BSE Code"))
            if bse is not None:
                self._by_bse[str(bse)] = cid
            nse = _safe_str(row.get("NSE Code"))
            if nse:
                self._by_nse[nse.upper()] = cid
            isin = _safe_str(row.get("ISIN"))
            if isin:
                self._by_isin[isin.upper()] = cid

        log.info(
            "Loaded %d companies (bse=%d, nse=%d, isin=%d)",
            len(self._by_id),
            len(self._by_bse),
            len(self._by_nse),
            len(self._by_isin),
        )

    @staticmethod
    def _row_to_dict(row: pd.Series) -> dict:
        # Some rows have a JSON 'tags' column with redundant fields; the flat
        # columns (Macro, Sector, etc.) are authoritative.
        tags_raw = _safe_str(row.get("tags"))
        tags_json: dict = {}
        if tags_raw:
            try:
                tags_json = json.loads(tags_raw)
            except (json.JSONDecodeError, TypeError):
                tags_json = {}
        bse = _safe_int(row.get("BSE Code"))
        return {
            "company_id": _safe_int(row.get("DataCompanyID")),
            "short_id": _safe_str(row.get("Short_id")) or tags_json.get("short_id"),
            "company_name": _safe_str(row.get("Company Name")),
            "company_fullname": _safe_str(row.get("CompanyFullName"))
            or tags_json.get("company_fullname"),
            "bse_code": str(bse) if bse is not None else None,
            "nse_code": _safe_str(row.get("NSE Code")),
            "isin": _safe_str(row.get("ISIN")) or tags_json.get("isin"),
            "sector": _safe_str(row.get("Sector")) or tags_json.get("sector"),
            "industry": _safe_str(row.get("Industry")) or tags_json.get("industry"),
            "basic_industry": _safe_str(row.get("BasicIndustry"))
            or tags_json.get("basic_industry"),
            "macro": _safe_str(row.get("Macro")) or tags_json.get("macro"),
            "mcap_crores": _safe_float(row.get("mcap")),
        }

    def _make_dto(self, d: Optional[dict]) -> Optional[CompanyDTO]:
        if not d or d.get("company_id") is None:
            return None
        return CompanyDTO(
            company_id=d["company_id"],
            short_id=d.get("short_id") or "",
            company_name=d.get("company_name") or "",
            company_fullname=d.get("company_fullname") or d.get("company_name") or "",
            bse_code=d.get("bse_code"),
            nse_code=d.get("nse_code"),
            isin=d.get("isin"),
            sector=d.get("sector"),
            industry=d.get("industry"),
            basic_industry=d.get("basic_industry"),
            macro=d.get("macro"),
            mcap_crores=d.get("mcap_crores"),
        )

    def get_by_bse_code(self, code: str) -> Optional[CompanyDTO]:
        if not code:
            return None
        # Normalise: BSE codes can have trailing .0 or whitespace
        try:
            normalized = str(int(float(str(code).strip())))
        except (ValueError, TypeError):
            normalized = str(code).strip()
        cid = self._by_bse.get(normalized)
        if cid is None:
            return None
        return self._make_dto(self._by_id.get(cid))

    def get_by_nse_symbol(self, symbol: str) -> Optional[CompanyDTO]:
        if not symbol:
            return None
        cid = self._by_nse.get(str(symbol).strip().upper())
        if cid is None:
            return None
        return self._make_dto(self._by_id.get(cid))

    def get_by_isin(self, isin: str) -> Optional[CompanyDTO]:
        if not isin:
            return None
        cid = self._by_isin.get(str(isin).strip().upper())
        if cid is None:
            return None
        return self._make_dto(self._by_id.get(cid))

    def get_by_company_id(self, company_id: int) -> Optional[CompanyDTO]:
        return self._make_dto(self._by_id.get(int(company_id)))

    def get_fundamentals(self, company_id: int) -> Optional[FundamentalsDTO]:
        """CSV provides only static mcap; return a partial DTO."""
        d = self._by_id.get(int(company_id))
        if not d:
            return None
        return FundamentalsDTO(
            company_id=d["company_id"],
            mcap_crores=d.get("mcap_crores"),
            quarterly_sales=None,
            annual_sales_approx=None,
            ebitda=None,
            ebitda_margin=None,
            pat=None,
            pat_margin=None,
            eps=None,
            roce=None,
            roe=None,
            debt_total=None,
            debt_net=None,
            fcf_latest=None,
            fcf_3y=None,
            capex_latest=None,
            capex_5y=None,
            pe=None,
            dividend_yield=None,
            promoter_pct=None,
            fii_pct=None,
            dii_pct=None,
            sales_cagr_3y=None,
            sales_cagr_5y=None,
            pat_cagr_3y=None,
            pat_cagr_5y=None,
        )

    def get_price_series(  # noqa: ARG002
        self, company_id: int, days: int = 90
    ) -> Optional[PriceSeriesDTO]:
        """CSV does not have prices. Use ScreenerOriginalCompanyProvider."""
        return None

    @property
    def total_count(self) -> int:
        return len(self._by_id)
