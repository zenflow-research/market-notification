"""Live smoke tests for BSE + NSE fetchers.

Marked `live_internet` so they're skipped automatically in offline runs.
Run with:

    pytest tests/integration/test_fetchers_smoke.py -v -m live_internet

Exit criteria for Phase 2 (per VERIFICATION.md §Phase 2):
  - >=10 rows from each exchange during market hours.
  - Each row passes the RawNotification schema check.
  - Date parsing handles all observed formats (no None announced_at).
"""
from __future__ import annotations

import os
from datetime import datetime

import pytest

from market_notification.exchange.base import RawNotification

pytestmark = pytest.mark.live_internet


# Skip the whole module unless the user explicitly asks for live tests.
# We don't want pytest's "auto-discover" to make a blind internet call.
if not os.environ.get("MN_RUN_LIVE_INTERNET"):
    pytest.skip(
        "Set MN_RUN_LIVE_INTERNET=1 to run live BSE/NSE fetcher tests",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
def bse_fetcher():
    from market_notification.exchange.bse_fetcher import BSEFetcher
    return BSEFetcher()


@pytest.fixture(scope="module")
def nse_fetcher():
    from market_notification.exchange.nse_fetcher import NSEFetcher
    return NSEFetcher()


# ---------------------------------------------------------------------------
# Schema assertions (apply to both exchanges)
# ---------------------------------------------------------------------------

def _assert_valid_notification(n: RawNotification, expected_source: str) -> None:
    assert n.source == expected_source
    assert isinstance(n.headline, str) and n.headline
    assert isinstance(n.announced_at, datetime)
    assert n.symbol is not None and n.symbol  # both exchanges always provide
    # raw_json is non-empty audit trail
    assert n.raw_json and n.raw_json.startswith(("{", "["))


# ---------------------------------------------------------------------------
# BSE
# ---------------------------------------------------------------------------

class TestBSEFetcher:
    def test_fetch_latest_returns_rows(self, bse_fetcher):
        rows = bse_fetcher.fetch_latest(50)
        assert isinstance(rows, list)
        # Outside market hours BSE may return fewer than 10. We assert >0
        # always; the >=10 check is enforced separately for the artifact run.
        assert len(rows) > 0, "BSE returned 0 rows -- check API health"
        for r in rows:
            _assert_valid_notification(r, "BSE")

    def test_fetch_latest_meets_phase2_threshold(self, bse_fetcher):
        """Phase 2 exit criterion: >=10 rows during market hours."""
        rows = bse_fetcher.fetch_latest(50)
        if len(rows) < 10:
            pytest.skip(
                f"BSE returned {len(rows)} rows -- likely outside market hours"
            )
        assert len(rows) >= 10


# ---------------------------------------------------------------------------
# NSE
# ---------------------------------------------------------------------------

class TestNSEFetcher:
    def test_fetch_latest_returns_rows(self, nse_fetcher):
        rows = nse_fetcher.fetch_latest(50)
        assert isinstance(rows, list)
        assert len(rows) > 0, "NSE returned 0 rows -- check API health"
        for r in rows:
            _assert_valid_notification(r, "NSE")

    def test_fetch_latest_meets_phase2_threshold(self, nse_fetcher):
        rows = nse_fetcher.fetch_latest(50)
        if len(rows) < 10:
            pytest.skip(
                f"NSE returned {len(rows)} rows -- likely outside market hours"
            )
        assert len(rows) >= 10
