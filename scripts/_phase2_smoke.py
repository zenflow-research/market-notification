"""Phase 2 verification harness.

Runs live BSE + NSE fetches and writes three artifact files into
verification/phase_2_results/:
  - sample_bse_response.json  -- one raw BSE record (pre-normalization)
  - sample_nse_response.json  -- one raw NSE record (pre-normalization)
  - normalized_sample.json    -- one BSE + one NSE RawNotification

Exit criteria (per VERIFICATION.md §Phase 2):
  - >=10 rows from each exchange during market hours.
  - Each row schema-valid (RawNotification).
  - Date parsing handles all observed formats.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from datetime import datetime
from pathlib import Path

# Make sure this works whether run as a module or as a script.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_notification.exchange.bse_fetcher import BSEFetcher  # noqa: E402
from market_notification.exchange.nse_fetcher import NSEFetcher  # noqa: E402
from market_notification.exchange.normalizer import normalize_bse  # noqa: E402

OUT_DIR = ROOT / "verification" / "phase_2_results"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _to_jsonable(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    return obj


def run_bse():
    print("\n=== BSE ===")
    fetcher = BSEFetcher()
    today = datetime.now().strftime("%Y%m%d")
    raw_rows = fetcher._fetch_raw_for_date(today)
    print(f"BSE raw rows for {today}: {len(raw_rows)}")
    if raw_rows:
        sample = raw_rows[0]
        (OUT_DIR / "sample_bse_response.json").write_text(
            json.dumps(sample, indent=2, default=str), encoding="utf-8"
        )
        print(f"  wrote sample_bse_response.json (one raw record)")
    norm = [normalize_bse(r) for r in raw_rows]
    norm = [n for n in norm if n is not None]
    print(f"BSE normalized: {len(norm)} of {len(raw_rows)}")
    none_dates = [r for r in raw_rows if normalize_bse(r) is None and r.get("DT_TM")]
    print(f"BSE rows where date parse failed: {len(none_dates)}")
    return norm


def run_nse():
    print("\n=== NSE ===")
    fetcher = NSEFetcher(playwright_headless=True)
    rows = fetcher.fetch_latest(50)
    print(f"NSE normalized rows: {len(rows)}")
    # For raw sample, fetch once more keeping the first record raw via _get_json
    raw = fetcher._get_json(
        "/api/NextApi/apiClient",
        params={
            "functionName": "getCorporateInfo",
            "type": "null",
            "noOfRecords": "5",
            "flag": "CAN",
        },
    )
    records = NSEFetcher._extract_records(raw)
    if records:
        (OUT_DIR / "sample_nse_response.json").write_text(
            json.dumps(records[0], indent=2, default=str), encoding="utf-8"
        )
        print(f"  wrote sample_nse_response.json (one raw record)")
    return rows


def main():
    print(f"Phase 2 smoke -- artifacts -> {OUT_DIR}")
    bse_rows = run_bse()
    nse_rows = run_nse()

    samples = {}
    if bse_rows:
        samples["bse"] = _to_jsonable(bse_rows[0])
    if nse_rows:
        samples["nse"] = _to_jsonable(nse_rows[0])
    (OUT_DIR / "normalized_sample.json").write_text(
        json.dumps(samples, indent=2, default=str), encoding="utf-8"
    )
    print(f"\nWrote normalized_sample.json")

    # Phase 2 exit assertions
    print("\n=== Exit criteria ===")
    bse_pass = len(bse_rows) >= 10
    nse_pass = len(nse_rows) >= 10
    print(f"  BSE >=10 rows: {bse_pass} (got {len(bse_rows)})")
    print(f"  NSE >=10 rows: {nse_pass} (got {len(nse_rows)})")
    if not bse_pass:
        print("    NOTE: <10 may be expected outside market hours")
    if not nse_pass:
        print("    NOTE: <10 may be expected outside market hours")


if __name__ == "__main__":
    main()
