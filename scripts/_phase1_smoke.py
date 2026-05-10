"""Phase 1 smoke + verification artifact generator.

Loads the live CSV, the live Screener_original PV, exercises the composite
provider, and writes verification artifacts to verification/phase_1_results/.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from market_notification.companies.factory import (  # noqa: E402
    default_company_provider,
    reset_default_company_provider,
)
from market_notification.companies.csv_source import CsvCompanyProvider  # noqa: E402
from market_notification.ops.logging import configure_logging, get_logger  # noqa: E402

VERIFY_DIR = PROJECT_ROOT / "verification" / "phase_1_results"
VERIFY_DIR.mkdir(parents=True, exist_ok=True)

# Known liquid stocks for sanity lookups
SAMPLES = [
    ("RELIANCE", "NSE"),
    ("ITC", "NSE"),
    ("TCS", "NSE"),
    ("HDFCBANK", "NSE"),
    ("INFY", "NSE"),
    ("SBIN", "NSE"),
    ("ICICIBANK", "NSE"),
    ("KOTAKBANK", "NSE"),
    ("BHARTIARTL", "NSE"),
    ("AXISBANK", "NSE"),
    ("20MICRONS", "NSE"),  # the smallcap from our test fixture
]


def main() -> int:
    configure_logging()
    log = get_logger("scripts._phase1_smoke")

    reset_default_company_provider()
    provider = default_company_provider()

    # 1) CSV total
    csv_only = CsvCompanyProvider()
    log.info("CSV total companies: %d", csv_only.total_count)

    # 2) Identity lookups
    rows = []
    for symbol, src in SAMPLES:
        company = provider.get_by_nse_symbol(symbol)
        rows.append(
            {
                "lookup_by": src,
                "key": symbol,
                "company_id": company.company_id if company else None,
                "company_name": company.company_name if company else None,
                "sector": company.sector if company else None,
                "industry": company.industry if company else None,
                "isin": company.isin if company else None,
                "mcap_crores": company.mcap_crores if company else None,
                "found": company is not None,
            }
        )

    found = sum(1 for r in rows if r["found"])
    log.info("Lookups: %d/%d found", found, len(rows))

    (VERIFY_DIR / "lookup_results.csv").write_text(
        "lookup_by,key,company_id,company_name,sector,industry,isin,mcap_crores,found\n"
        + "\n".join(
            f"{r['lookup_by']},{r['key']},{r['company_id']},"
            f"\"{r['company_name'] or ''}\",\"{r['sector'] or ''}\","
            f"\"{r['industry'] or ''}\",{r['isin'] or ''},"
            f"{r['mcap_crores']},{r['found']}"
            for r in rows
        ),
        encoding="utf-8",
    )

    # 3) Cross-source: BSE code lookup
    bse_company = provider.get_by_bse_code("533022")  # 20 Microns
    log.info("BSE 533022 -> %s", bse_company.company_name if bse_company else None)

    # 4) Price series for Reliance (canonical liquid stock)
    rel = provider.get_by_nse_symbol("RELIANCE")
    price_series = (
        provider.get_price_series(rel.company_id, days=60) if rel else None
    )
    if rel and price_series and price_series.bars:
        last = price_series.bars[-1]
        first = price_series.bars[0]
        log.info(
            "RELIANCE price bars: %d, first=%s close=%.2f, last=%s close=%.2f",
            len(price_series.bars),
            first.bar_date,
            first.close,
            last.bar_date,
            last.close,
        )
        price_summary = {
            "company_id": rel.company_id,
            "symbol": "RELIANCE",
            "bars_returned": len(price_series.bars),
            "first_date": str(first.bar_date),
            "first_close": first.close,
            "last_date": str(last.bar_date),
            "last_close": last.close,
            "days_requested": 60,
        }
    else:
        log.warning("Could not load RELIANCE price series")
        price_summary = {"error": "no_price_data"}

    (VERIFY_DIR / "price_smoke.json").write_text(
        json.dumps(price_summary, indent=2), encoding="utf-8"
    )

    # 5) Coverage report
    csv = csv_only
    nse_covered = sum(1 for d in csv._by_id.values() if d.get("nse_code"))  # noqa: SLF001
    bse_covered = sum(1 for d in csv._by_id.values() if d.get("bse_code"))  # noqa: SLF001
    isin_covered = sum(1 for d in csv._by_id.values() if d.get("isin"))  # noqa: SLF001
    mcap_covered = sum(
        1 for d in csv._by_id.values() if d.get("mcap_crores") is not None  # noqa: SLF001
    )
    coverage = {
        "total_companies": csv.total_count,
        "nse_code_present": nse_covered,
        "bse_code_present": bse_covered,
        "isin_present": isin_covered,
        "mcap_present": mcap_covered,
        "nse_code_pct": round(100 * nse_covered / csv.total_count, 1),
        "bse_code_pct": round(100 * bse_covered / csv.total_count, 1),
        "isin_pct": round(100 * isin_covered / csv.total_count, 1),
        "mcap_pct": round(100 * mcap_covered / csv.total_count, 1),
    }
    (VERIFY_DIR / "coverage_report.md").write_text(
        "# Phase 1 — Coverage Report\n\n"
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"- Total companies in CSV: **{coverage['total_companies']:,}**\n"
        f"- NSE code present: {coverage['nse_code_present']:,} "
        f"({coverage['nse_code_pct']}%)\n"
        f"- BSE code present: {coverage['bse_code_present']:,} "
        f"({coverage['bse_code_pct']}%)\n"
        f"- ISIN present: {coverage['isin_present']:,} ({coverage['isin_pct']}%)\n"
        f"- mcap present: {coverage['mcap_present']:,} ({coverage['mcap_pct']}%)\n\n"
        f"Sample lookups: see lookup_results.csv\n"
        f"RELIANCE price smoke: see price_smoke.json\n",
        encoding="utf-8",
    )
    log.info("Wrote artifacts to %s", VERIFY_DIR)
    log.info("Coverage: %s", coverage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
