"""Unit tests: SlaMonitor — finds stuck classify_pending rows, journals once."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from market_notification.db.models import Notification, PipelineJournal
from market_notification.pipeline.sla_monitor import (
    SLA_ACTOR,
    SLA_BREACH_KIND,
    SlaMonitor,
)


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def session_factory(in_memory_engine):
    Maker = sessionmaker(bind=in_memory_engine, expire_on_commit=False)

    @contextmanager
    def factory():
        sess = Maker()
        try:
            yield sess
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()

    return factory


def _seed(sess, *, age_min: int, status: str = "classify_pending", **kw) -> int:
    fetched = _utc_now_naive() - timedelta(minutes=age_min)
    n = Notification(
        company_id=1,
        source="BSE",
        headline=f"row-aged-{age_min}m",
        announced_at=fetched,
        fetched_at=fetched,
        last_status_change_at=fetched,
        pipeline_status=status,
        **kw,
    )
    sess.add(n)
    sess.commit()
    return n.id


def test_no_breach_when_all_fresh(session_factory) -> None:
    with session_factory() as sess:
        _seed(sess, age_min=2)

    monitor = SlaMonitor(threshold_minutes=5, session_factory=session_factory)
    res = monitor.check_once()
    assert res.found == 0
    assert res.new_breaches == 0


def test_breach_logged_once_for_stale_row(session_factory) -> None:
    with session_factory() as sess:
        notif_id = _seed(sess, age_min=10)

    monitor = SlaMonitor(threshold_minutes=5, session_factory=session_factory)

    res1 = monitor.check_once()
    assert res1.found == 1
    assert res1.new_breaches == 1
    assert res1.already_alerted == 0

    # Second tick on the still-stuck row should NOT double-log
    res2 = monitor.check_once()
    assert res2.found == 1
    assert res2.new_breaches == 0
    assert res2.already_alerted == 1

    # Exactly one journal entry
    with session_factory() as sess:
        rows = sess.execute(
            select(PipelineJournal).where(PipelineJournal.notification_id == notif_id)
        ).scalars().all()
        assert len(rows) == 1
        entry = rows[0]
        assert entry.actor == SLA_ACTOR
        assert entry.error_kind == SLA_BREACH_KIND
        assert "exceeded 5min" in entry.error_message


def test_only_classify_pending_is_watched(session_factory) -> None:
    with session_factory() as sess:
        _seed(sess, age_min=30, status="summarize_pending")  # different queue
        _seed(sess, age_min=30, status="done")               # terminal
    monitor = SlaMonitor(threshold_minutes=5, session_factory=session_factory)
    res = monitor.check_once()
    assert res.found == 0


def test_threshold_respected(session_factory) -> None:
    with session_factory() as sess:
        # 4 minutes old; 5-min threshold should not trip; 3-min threshold should
        _seed(sess, age_min=4)

    assert SlaMonitor(threshold_minutes=5, session_factory=session_factory).check_once().new_breaches == 0
    assert SlaMonitor(threshold_minutes=3, session_factory=session_factory).check_once().new_breaches == 1
