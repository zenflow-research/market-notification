"""Phase 0 verification — schema integrity smoke tests."""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from market_notification.db.models import (
    Base,
    Notification,
    NotificationFilterRule,
    NotificationPollState,
    PipelineJournal,
)


def test_metadata_has_expected_tables(in_memory_engine):
    """Every table from PLAN.md §5 must exist after metadata.create_all."""
    expected = {
        "notifications",
        "notification_filter_rules",
        "notification_poll_state",
        "historical_symbol_map",
        "pipeline_journal",
        "taxonomy_version",
        "prompt_version",
    }
    actual = set(Base.metadata.tables.keys())
    assert expected <= actual, f"Missing tables: {expected - actual}"


def test_insert_one_notification(in_memory_session: Session):
    """Insert a Notification with the minimum required fields, read it back."""
    n = Notification(
        company_id=11,
        source="BSE",
        headline="Test Capacity Expansion announcement",
        announced_at=datetime(2026, 5, 7, 10, 30, 0),
        pipeline_status="ingested",
    )
    in_memory_session.add(n)
    in_memory_session.commit()

    rows = in_memory_session.execute(select(Notification)).scalars().all()
    assert len(rows) == 1
    got = rows[0]
    assert got.company_id == 11
    assert got.source == "BSE"
    assert got.pipeline_status == "ingested"
    assert got.fetched_at is not None  # default _utc_now applied


def test_pipeline_status_transition(in_memory_session: Session):
    """Verify status field accepts the documented states."""
    n = Notification(
        company_id=11,
        source="NSE",
        headline="Quarterly Results",
        announced_at=datetime(2026, 5, 7, 11, 0, 0),
        pipeline_status="ingested",
    )
    in_memory_session.add(n)
    in_memory_session.commit()

    n.pipeline_status = "classify_pending"
    in_memory_session.commit()

    refreshed = in_memory_session.get(Notification, n.id)
    assert refreshed is not None
    assert refreshed.pipeline_status == "classify_pending"


def test_unique_natural_key_blocks_duplicates(in_memory_session: Session):
    """The (source, company_id, announced_at, headline) UNIQUE must reject dupes."""
    payload = dict(
        company_id=11,
        source="BSE",
        headline="Same headline",
        announced_at=datetime(2026, 5, 7, 12, 0, 0),
    )
    in_memory_session.add(Notification(**payload))
    in_memory_session.commit()

    with pytest.raises(IntegrityError):
        in_memory_session.add(Notification(**payload))
        in_memory_session.commit()


def test_journal_appends(in_memory_session: Session):
    """Pipeline journal writes a row per transition."""
    n = Notification(
        company_id=11,
        source="BSE",
        headline="Journal test",
        announced_at=datetime(2026, 5, 7, 13, 0, 0),
        pipeline_status="ingested",
    )
    in_memory_session.add(n)
    in_memory_session.commit()

    j = PipelineJournal(
        notification_id=n.id,
        from_status="ingested",
        to_status="classify_pending",
        actor="poller",
        duration_ms=10,
    )
    in_memory_session.add(j)
    in_memory_session.commit()

    rows = in_memory_session.execute(select(PipelineJournal)).scalars().all()
    assert len(rows) == 1
    assert rows[0].notification_id == n.id


def test_filter_rule_unique_key(in_memory_session: Session):
    """Filter rules with same (rule_type, pattern, source) collide."""
    r1 = NotificationFilterRule(
        rule_type="category",
        pattern="Trading Window",
        source="BSE",
        action="hide",
        created_by="system",
    )
    in_memory_session.add(r1)
    in_memory_session.commit()

    with pytest.raises(IntegrityError):
        in_memory_session.add(
            NotificationFilterRule(
                rule_type="category",
                pattern="Trading Window",
                source="BSE",
                action="hide",
                created_by="user",
            )
        )
        in_memory_session.commit()


def test_poll_state_per_source(in_memory_session: Session):
    """Each source has at most one poll state row."""
    s = NotificationPollState(source="BSE", status="idle")
    in_memory_session.add(s)
    in_memory_session.commit()

    with pytest.raises(IntegrityError):
        in_memory_session.add(NotificationPollState(source="BSE", status="polling"))
        in_memory_session.commit()
