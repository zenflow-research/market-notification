"""Screener_original-backed price/mcap provider.

Reads `G:\\Screener_original\\stockDirectory\\{data_company_id}\\{id}_PV.csv`
which is the canonical per-company price-volume file. Logic ported from
`G:\\Screener_original\\screener_util\\pv_df_check.py` (no runtime import — D-01).

The CSV layout (verified 2026-05-07):
  Date, open, high, low, close, wap, turnover, shares_traded, deliveries,
  Equity History, Dividend, Split, Merger, mcap, ff_mcap, no_of_shares,
  no_of_ff_shares, split_ratio, open_adj, high_adj, low_adj, close_adj,
  shares_traded_adj, deliveries_adj
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from market_notification.companies.base import (
    CompanyDTO,
    CompanyProvider,
    FundamentalsDTO,
    PriceBar,
    PriceSeriesDTO,
)
from market_notification.config.settings import get_settings

log = logging.getLogger(__name__)


class ScreenerOriginalCompanyProvider(CompanyProvider):
    """Provider that reads price + mcap from per-company stockDirectory CSVs.

    Identity lookups (BSE/NSE/ISIN) return None — those go through
    `CsvCompanyProvider`. Use `CompositeCompanyProvider` to combine.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(
            root or Path(get_settings().paths.screener_original_root) / "stockDirectory"
        )
        if not self.root.exists():
            log.warning(
                "Screener_original stockDirectory missing: %s "
                "(price provider will return None)",
                self.root,
            )

    # Identity not provided by this source
    def get_by_bse_code(self, code: str) -> Optional[CompanyDTO]:  # noqa: ARG002
        return None

    def get_by_nse_symbol(self, symbol: str) -> Optional[CompanyDTO]:  # noqa: ARG002
        return None

    def get_by_isin(self, isin: str) -> Optional[CompanyDTO]:  # noqa: ARG002
        return None

    def get_by_company_id(self, company_id: int) -> Optional[CompanyDTO]:  # noqa: ARG002
        return None

    def _company_dir(self, company_id: int) -> Path:
        return self.root / str(int(company_id))

    def _pv_csv_path(self, company_id: int) -> Path:
        cid = int(company_id)
        return self._company_dir(cid) / f"{cid}_PV.csv"

    def get_fundamentals(self, company_id: int) -> Optional[FundamentalsDTO]:
        """Static fundamentals derived from latest row of PV.csv (mcap only).

        Sales/EBITDA/PAT come from the per-company subdirs (profit-loss,
        balance-sheet, etc.) — implemented at Phase 6/11 entry.
        """
        path = self._pv_csv_path(company_id)
        if not path.exists():
            return None
        try:
            df = pd.read_csv(path)
            if df.empty:
                return None
            latest = df.iloc[-1]
            mcap = (
                float(latest["mcap"])
                if "mcap" in df.columns and pd.notna(latest["mcap"])
                else None
            )
            mcap_crores = (mcap / 1e7) if mcap is not None else None
            return FundamentalsDTO(
                company_id=int(company_id),
                mcap_crores=mcap_crores,
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
        except Exception:  # pragma: no cover
            log.exception("Failed to read PV.csv for company_id=%s", company_id)
            return None

    def get_price_series(
        self, company_id: int, days: int = 90
    ) -> Optional[PriceSeriesDTO]:
        """Return last `days` calendar days of bars from {id}_PV.csv.

        Uses unadjusted OHLCV (close, etc.) per our design — adjusted columns
        (`close_adj`) exist in the file too if needed later.
        """
        path = self._pv_csv_path(company_id)
        if not path.exists():
            log.debug("PV.csv missing for company_id=%s at %s", company_id, path)
            return None

        try:
            df = pd.read_csv(path)
            if df.empty:
                log.debug("PV.csv empty for company_id=%s", company_id)
                return None

            df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
            df = df.dropna(subset=["Date"]).sort_values("Date")
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
                days=days
            )
            mask = df["Date"] >= cutoff
            recent = df.loc[mask]
            if len(recent) == 0:
                # Fall back to last N rows if all data is older than `days`
                recent = df.tail(days)

            has_deliveries = "deliveries" in recent.columns
            # to_dict('records') converts Series rows to plain dicts so static
            # type-checking doesn't need to model pandas indexing.
            bars: list[PriceBar] = []
            for rec in recent.to_dict(orient="records"):
                date_val = rec.get("Date")
                bar_date = pd.Timestamp(date_val).date()  # type: ignore[arg-type]
                deliveries_val: Optional[float] = None
                if has_deliveries:
                    raw_d = rec.get("deliveries")
                    if raw_d is not None and not pd.isna(raw_d):
                        deliveries_val = float(raw_d)
                bars.append(
                    PriceBar(
                        bar_date=bar_date,
                        open=float(rec.get("open") or 0),
                        high=float(rec.get("high") or 0),
                        low=float(rec.get("low") or 0),
                        close=float(rec.get("close") or 0),
                        volume=float(rec.get("shares_traded") or 0),
                        turnover=float(rec.get("turnover") or 0),
                        deliveries=deliveries_val,
                    )
                )
            return PriceSeriesDTO(
                company_id=int(company_id),
                bars=bars,
                days=days,
            )
        except Exception:  # pragma: no cover
            log.exception("Failed to read price series for company_id=%s", company_id)
            return None
