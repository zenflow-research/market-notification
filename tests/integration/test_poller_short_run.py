"""Poller integration tests.

Two flavors:
  - **Mocked fetchers** -- deterministic; runs every CI cycle. Verifies the
    full ingest -> dedup -> cross-exchange flow on a known fixture.
  - **Live** -- gated by `MN_RUN_LIVE_INTERNET=1`. Hits BSE+NSE for real.

The Poller talks to the DB via `get_session()`, so these tests redirect the
project DB to an in-memory SQLite by setting MN_DB__URL before importing
the session module.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

# Force in-memory DB BEFORE importing settings/session (cached on first access).
os.environ["MN_DB__URL"] = "sqlite:///:memory:"

from market_notification.exchange.base import ExchangeFetcher, RawNotification  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class StubFetcher(ExchangeFetcher):
    """Deterministic in-memory fetcher for tests."""

    def __init__(self, source: str, rows: list[RawNotification]) -> None:
        self.source = source  # type: ignore[misc]
        self._rows = rows
        self.fetch_count = 0

    def fetch_latest(self, n: int = 50) -> list[RawNotification]:
        self.fetch_count += 1
        return list(self._rows[:n])

    def fetch_for_date(self, date_yyyymmdd: str) -> list[RawNotification]:
        return list(self._rows)

    def fetch_attachment(self, url: str) -> bytes:
        return b""


class StubCompanyProvider:
    """Maps test BSE 500325 + NSE RELIANCE both to company_id=11."""

    def __init__(self):
        self._bse = {"500325": type("C", (), {"company_id": 11})()}
        self._nse = {"RELIANCE": type("C", (), {"company_id": 11})()}
        self._isin = {}

    def get_by_bse_code(self, code):
        return self._bse.get(code)

    def get_by_nse_symbol(self, sym):
        return self._nse.get(sym)

    def get_by_isin(self, isin):
        return self._isin.get(isin)

    def get_by_company_id(self, cid):
        return None

    def get_fundamentals(self, cid):
        return None

    def get_price_series(self, cid, days=90):
        return None


def _raw(source, symbol, headline, announced_at) -> RawNotification:
    return RawNotification(
        source=source,
        seq_id=None,
        headline=headline,
        category=None,
        subcategory=None,
        body=None,
        announced_at=announced_at,
        exchange_disseminated_at=None,
        attachment_url=None,
        attachment_name=None,
        attachment_size=None,
        is_critical=False,
        has_xbrl=False,
        symbol=symbol,
        company_name_raw=None,
        isin=None,
        industry_raw=None,
        raw_json="{}",
    )


@pytest.fixture()
def fresh_db():
    """Inject a fresh in-memory engine into the session module.

    `get_settings()` is cached and TOML beats env vars, so monkey-injecting
    the engine is the cleanest way to redirect the project DB at test time.
    """
    from sqlalchemy import create_engine
    from market_notification.db import session as session_mod
    from market_notification.db.models import Base

    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    # Inject as the cached engine, force sessionmaker to rebuild
    session_mod._engine = eng
    session_mod._SessionLocal = None
    try:
        yield eng
    finally:
        session_mod.dispose_engine()


# ---------------------------------------------------------------------------
# Mocked-fetcher tests (always run)
# ---------------------------------------------------------------------------

class TestPollerWithMockedFetchers:
    def test_basic_insert(self, fresh_db):
        from market_notification.db.models import Notification
        from market_notification.db.session import get_session
        from market_notification.poller.poller import Poller

        t = datetime(2026, 5, 7, 10, 30, 0)
        bse_rows = [
            _raw("BSE", "500325", f"BSE filing #{i}", t + timedelta(seconds=i))
            for i in range(3)
        ]
        nse_rows = [
            _raw("NSE", "RELIANCE", f"NSE filing #{i}", t + timedelta(seconds=i))
            for i in range(3)
        ]
        poller = Poller(
            bse_fetcher=StubFetcher("BSE", bse_rows),
            nse_fetcher=StubFetcher("NSE", nse_rows),
            company_provider=StubCompanyProvider(),
        )
        bse_r, nse_r = poller.poll_once()

        assert bse_r.fetched == 3
        assert nse_r.fetched == 3
        assert bse_r.inserted == 3
        assert nse_r.inserted == 3

        with get_session() as s:
            n = s.execute(select(Notification)).scalars().all()
            assert len(n) == 6
            for row in n:
                assert row.company_id == 11

    def test_second_poll_dedups(self, fresh_db):
        from market_notification.db.models import Notification
        from market_notification.db.session import get_session
        from market_notification.poller.poller import Poller

        t = datetime(2026, 5, 7, 10, 30, 0)
        rows = [_raw("BSE", "500325", "Same headline", t)]
        poller = Poller(
            bse_fetcher=StubFetcher("BSE", rows),
            nse_fetcher=StubFetcher("NSE", []),
            company_provider=StubCompanyProvider(),
        )
        poller.poll_once()
        poller.poll_once()
        with get_session() as s:
            n = s.execute(select(Notification)).scalars().all()
        assert len(n) == 1

    def test_cross_exchange_pair_marks_second_as_duplicate_dropped(self, fresh_db):
        """The fixture exit-criterion test for FR-INGEST-005/006."""
        from market_notification.db.models import Notification
        from market_notification.db.session import get_session
        from market_notification.poller.poller import Poller

        t = datetime(2026, 5, 7, 10, 30, 0)
        # Same company_id (11), same headline within window
        bse_rows = [_raw("BSE", "500325",
                         "Outcome of Board Meeting held on May 07, 2026", t)]
        nse_rows = [_raw("NSE", "RELIANCE",
                         "Outcome of Board Meeting held on May 07, 2026",
                         t + timedelta(minutes=2))]

        poller = Poller(
            bse_fetcher=StubFetcher("BSE", bse_rows),
            nse_fetcher=StubFetcher("NSE", nse_rows),
            company_provider=StubCompanyProvider(),
        )
        # Poller polls NSE first, then BSE. So BSE will be "second arriver"
        # for the matching announced_at -- and BSE has the EARLIER announced_at.
        # But find_match looks at any +/-10min, so order should still match.
        poller.poll_once()

        with get_session() as s:
            rows = s.execute(select(Notification)).scalars().all()
        assert len(rows) == 2
        groups = {r.cross_exchange_group_id for r in rows}
        assert len(groups) == 1, "Both rows should share one group"
        roles = sorted(r.cross_exchange_role or "" for r in rows)
        assert roles == ["duplicate_dropped", "primary"]
        # The duplicate-dropped row gets short-circuited
        dropped = [r for r in rows if r.cross_exchange_role == "duplicate_dropped"][0]
        assert dropped.pipeline_status == "ignored_cross_exchange"

    def test_poll_state_persisted_for_both_sources(self, fresh_db):
        from market_notification.db.models import NotificationPollState
        from market_notification.db.session import get_session
        from market_notification.poller.poller import Poller

        t = datetime(2026, 5, 7, 10, 30, 0)
        poller = Poller(
            bse_fetcher=StubFetcher("BSE", [_raw("BSE", "500325", "x", t)]),
            nse_fetcher=StubFetcher("NSE", [_raw("NSE", "RELIANCE", "y", t)]),
            company_provider=StubCompanyProvider(),
        )
        poller.poll_once()

        with get_session() as s:
            states = s.execute(select(NotificationPollState)).scalars().all()
        sources = {st.source for st in states}
        assert sources == {"BSE", "NSE"}
        for st in states:
            assert st.status == "idle"
            assert st.last_poll_at is not None


# ---------------------------------------------------------------------------
# Live test (gated)
# ---------------------------------------------------------------------------

@pytest.mark.live_internet
@pytest.mark.skipif(
    not os.environ.get("MN_RUN_LIVE_INTERNET"),
    reason="Set MN_RUN_LIVE_INTERNET=1 to run live poller tests",
)
def test_live_short_run(fresh_db):
    """One real poll, then a second to verify dedup. >=5 rows expected."""
    from market_notification.companies.factory import default_company_provider
    from market_notification.db.models import Notification
    from market_notification.db.session import get_session
    from market_notification.exchange.bse_fetcher import BSEFetcher
    from market_notification.exchange.nse_fetcher import NSEFetcher
    from market_notification.poller.poller import Poller

    poller = Poller(
        bse_fetcher=BSEFetcher(),
        nse_fetcher=NSEFetcher(playwright_headless=True),
        company_provider=default_company_provider(),
        bse_records_per_poll=20,
        nse_records_per_poll=20,
    )
    poller.poll_once()
    with get_session() as s:
        after_pass1 = s.execute(select(Notification)).scalars().all()
    assert len(after_pass1) >= 5

    poller.poll_once()
    with get_session() as s:
        after_pass2 = s.execute(select(Notification)).scalars().all()
    assert len(after_pass2) == len(after_pass1), "dedup should prevent re-insertion"
