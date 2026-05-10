"""Integration tests: seed loader + poller-filter wiring (Phase 4).

Two thin tests:
  1. Loading config/filter_rules.json into an in-memory DB populates the table.
  2. The Poller, given a RegexFilterEngine, marks junk rows with
     pipeline_status='ignored' + is_useless=1 + junk_rule_id=<rule id>,
     and skips cross-exchange grouping for them.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select

# Force in-memory DB BEFORE importing settings/session.
os.environ["MN_DB__URL"] = "sqlite:///:memory:"

from market_notification.exchange.base import ExchangeFetcher, RawNotification  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RULES_JSON = PROJECT_ROOT / "config" / "filter_rules.json"


# ---------------------------------------------------------------------------
# Stubs (mirror tests/integration/test_poller_short_run.py)
# ---------------------------------------------------------------------------
class StubFetcher(ExchangeFetcher):
    def __init__(self, source: str, rows: list[RawNotification]) -> None:
        self.source = source  # type: ignore[misc]
        self._rows = rows

    def fetch_latest(self, n: int = 50) -> list[RawNotification]:
        return list(self._rows[:n])

    def fetch_for_date(self, date_yyyymmdd: str) -> list[RawNotification]:
        return list(self._rows)

    def fetch_attachment(self, url: str) -> bytes:
        return b""


class StubCompanyProvider:
    def __init__(self):
        self._bse = {"500325": type("C", (), {"company_id": 11})()}
        self._nse = {"RELIANCE": type("C", (), {"company_id": 11})()}

    def get_by_bse_code(self, code):
        return self._bse.get(code)

    def get_by_nse_symbol(self, sym):
        return self._nse.get(sym)

    def get_by_isin(self, isin):
        return None

    def get_by_company_id(self, cid):
        return None

    def get_fundamentals(self, cid):
        return None

    def get_price_series(self, cid, days=90):
        return None


def _raw(source, symbol, headline, announced_at, *, category=None, subcategory=None):
    return RawNotification(
        source=source,
        seq_id=None,
        headline=headline,
        category=category,
        subcategory=subcategory,
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
    from sqlalchemy import create_engine
    from market_notification.db import session as session_mod
    from market_notification.db.models import Base

    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    session_mod._engine = eng
    session_mod._SessionLocal = None
    try:
        yield eng
    finally:
        session_mod.dispose_engine()


# ---------------------------------------------------------------------------
# 1. Seed loader: rules file -> DB
# ---------------------------------------------------------------------------
class TestSeedLoader:
    def test_seed_file_loads_into_db(self, fresh_db):
        from market_notification.db.models import NotificationFilterRule
        from market_notification.db.repositories.filter_rule_repo_sqla import (
            SqlaFilterRuleRepo,
        )
        from market_notification.db.session import get_session

        with RULES_JSON.open("r", encoding="utf-8") as f:
            rules = json.load(f)
        assert isinstance(rules, list)
        assert len(rules) >= 10  # we ship 14, sanity floor

        with get_session() as sess:
            repo = SqlaFilterRuleRepo(sess)
            for rule in rules:
                repo.add(
                    rule_type=rule["rule_type"],
                    pattern=rule["pattern"],
                    source=rule.get("source"),
                    action=rule.get("action", "hide"),
                    created_by=rule.get("created_by", "system"),
                    reason=rule.get("reason"),
                )

        with get_session() as sess:
            stored = sess.execute(select(NotificationFilterRule)).scalars().all()
        assert len(stored) == len(rules)
        kinds = {r.rule_type for r in stored}
        assert {"category", "subcategory", "headline_regex", "keyword"}.issubset(kinds)

    def test_seed_file_idempotent(self, fresh_db):
        """Re-seeding should not duplicate rows (UNIQUE on rule_type/pattern/source)."""
        from market_notification.db.models import NotificationFilterRule
        from market_notification.db.repositories.filter_rule_repo_sqla import (
            SqlaFilterRuleRepo,
        )
        from market_notification.db.session import get_session

        with RULES_JSON.open("r", encoding="utf-8") as f:
            rules = json.load(f)

        for _ in range(2):
            with get_session() as sess:
                repo = SqlaFilterRuleRepo(sess)
                for rule in rules:
                    repo.add(
                        rule_type=rule["rule_type"],
                        pattern=rule["pattern"],
                        source=rule.get("source"),
                        action=rule.get("action", "hide"),
                        created_by=rule.get("created_by", "system"),
                        reason=rule.get("reason"),
                    )

        with get_session() as sess:
            stored = sess.execute(select(NotificationFilterRule)).scalars().all()
        assert len(stored) == len(rules)


# ---------------------------------------------------------------------------
# 2. Poller wiring: filter marks rows ignored + populates junk_rule_id
# ---------------------------------------------------------------------------
class TestPollerWithFilter:
    def test_junk_row_marked_ignored_with_rule_id(self, fresh_db):
        from market_notification.db.models import (
            Notification,
            NotificationFilterRule,
        )
        from market_notification.db.session import get_session
        from market_notification.filter.filter_engine import RegexFilterEngine
        from market_notification.poller.poller import Poller

        # Seed one rule directly
        with get_session() as sess:
            sess.add(
                NotificationFilterRule(
                    rule_type="category",
                    pattern="Closure of Trading Window",
                    source=None,
                    action="hide",
                    created_by="system",
                    is_active=1,
                )
            )
            sess.flush()
            rule_id = sess.execute(
                select(NotificationFilterRule.id)
            ).scalar_one()

        engine = RegexFilterEngine(get_session)
        assert len(engine) == 1

        t = datetime(2026, 5, 7, 10, 30, 0)
        bse_rows = [
            _raw("BSE", "500325", "Real headline 1", t,
                 category="Outcome of Board Meeting"),
            _raw("BSE", "500325", "Junk one", t.replace(second=1),
                 category="Closure of Trading Window"),
        ]
        poller = Poller(
            bse_fetcher=StubFetcher("BSE", bse_rows),
            nse_fetcher=StubFetcher("NSE", []),
            company_provider=StubCompanyProvider(),
            filter_engine=engine,
        )
        bse_r, _ = poller.poll_once()

        assert bse_r.fetched == 2
        assert bse_r.inserted == 2
        assert bse_r.junk == 1

        with get_session() as sess:
            rows = sess.execute(select(Notification)).scalars().all()
        assert len(rows) == 2
        junk = [r for r in rows if r.is_useless == 1]
        assert len(junk) == 1
        assert junk[0].pipeline_status == "ignored"
        assert junk[0].junk_rule_id == rule_id
        # Junk rows skip cross-exchange grouping
        assert junk[0].cross_exchange_group_id is None

        active = [r for r in rows if r.is_useless == 0]
        assert len(active) == 1
        assert active[0].pipeline_status == "ingested"

    def test_no_filter_engine_works_unchanged(self, fresh_db):
        """Backward compat: omitting filter_engine keeps Phase 3 behavior."""
        from market_notification.db.models import Notification
        from market_notification.db.session import get_session
        from market_notification.poller.poller import Poller

        t = datetime(2026, 5, 7, 10, 30, 0)
        rows = [_raw("BSE", "500325", "Anything", t,
                     category="Closure of Trading Window")]
        poller = Poller(
            bse_fetcher=StubFetcher("BSE", rows),
            nse_fetcher=StubFetcher("NSE", []),
            company_provider=StubCompanyProvider(),
        )
        bse_r, _ = poller.poll_once()
        assert bse_r.junk == 0
        with get_session() as sess:
            stored = sess.execute(select(Notification)).scalars().all()
        assert len(stored) == 1
        assert stored[0].is_useless == 0
