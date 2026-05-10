"""Unit tests for natural-key dedup in SqlaNotificationRepo.

Uses the in-memory SQLite engine from conftest.py. Verifies:
  - Inserting the same (source, company_id, announced_at, headline) twice
    returns the same id and only one row exists.
  - The DB UNIQUE constraint is the safety net (covered indirectly).
  - exists_by_natural_key returns the right answer.
"""
from __future__ import annotations

from datetime import datetime

from market_notification.db.repositories.notification_repo_sqla import (
    SqlaNotificationRepo,
)


def _payload(**overrides):
    base = {
        "company_id": 11,
        "source": "BSE",
        "headline": "Outcome of Board Meeting",
        "announced_at": datetime(2026, 5, 7, 10, 30, 0),
        "category": "Result",
        "raw_json": "{}",
    }
    base.update(overrides)
    return base


class TestNaturalKeyDedup:
    def test_first_insert_returns_new_id(self, in_memory_session):
        repo = SqlaNotificationRepo(in_memory_session)
        nid = repo.insert(_payload())
        in_memory_session.commit()
        assert nid > 0

    def test_duplicate_insert_returns_same_id(self, in_memory_session):
        repo = SqlaNotificationRepo(in_memory_session)
        first = repo.insert(_payload())
        in_memory_session.commit()
        second = repo.insert(_payload())
        in_memory_session.commit()
        assert second == first

    def test_duplicate_does_not_create_second_row(self, in_memory_session):
        repo = SqlaNotificationRepo(in_memory_session)
        repo.insert(_payload())
        in_memory_session.commit()
        repo.insert(_payload())
        in_memory_session.commit()
        from market_notification.db.models import Notification
        n = in_memory_session.query(Notification).count()
        assert n == 1

    def test_different_source_is_a_new_row(self, in_memory_session):
        repo = SqlaNotificationRepo(in_memory_session)
        a = repo.insert(_payload(source="BSE"))
        in_memory_session.commit()
        b = repo.insert(_payload(source="NSE"))
        in_memory_session.commit()
        assert a != b

    def test_different_company_is_new_row(self, in_memory_session):
        repo = SqlaNotificationRepo(in_memory_session)
        a = repo.insert(_payload(company_id=11))
        in_memory_session.commit()
        b = repo.insert(_payload(company_id=22))
        in_memory_session.commit()
        assert a != b

    def test_different_announced_at_is_new_row(self, in_memory_session):
        repo = SqlaNotificationRepo(in_memory_session)
        a = repo.insert(_payload(announced_at=datetime(2026, 5, 7, 10, 30, 0)))
        in_memory_session.commit()
        b = repo.insert(_payload(announced_at=datetime(2026, 5, 7, 10, 31, 0)))
        in_memory_session.commit()
        assert a != b

    def test_exists_by_natural_key_true(self, in_memory_session):
        repo = SqlaNotificationRepo(in_memory_session)
        repo.insert(_payload())
        in_memory_session.commit()
        assert repo.exists_by_natural_key(
            source="BSE", company_id=11,
            announced_at=datetime(2026, 5, 7, 10, 30, 0),
            headline="Outcome of Board Meeting",
        )

    def test_exists_by_natural_key_false(self, in_memory_session):
        repo = SqlaNotificationRepo(in_memory_session)
        assert not repo.exists_by_natural_key(
            source="BSE", company_id=99,
            announced_at=datetime(2026, 1, 1, 0, 0, 0),
            headline="nope",
        )
