"""Integration-flavored unit tests for PriorityService.

Drives PriorityService end-to-end against an in-memory DB:
  - happy path: writes det_* and ai_priority_* columns
  - status transitions priority_pending -> summarize_pending
  - journals the transition
  - LLM-override path produces ai_priority differing from det_priority
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from market_notification.db.models import Notification, PipelineJournal
from market_notification.priority.base import (
    LlmPriorityOverride,
    PriorityResult,
)
from market_notification.priority.service import PriorityService


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


def _seed(sess, *, headline: str, category: str = "Acquisition") -> int:
    row = Notification(
        company_id=1, source="BSE", headline=headline,
        announced_at=_utc_now_naive(),
        pipeline_status="priority_pending",
        ai_category=category,
        ai_category_group="Deals & Partnerships",
    )
    sess.add(row)
    sess.commit()
    return row.id


def test_happy_path_writes_columns_and_advances_status(session_factory) -> None:
    with session_factory() as sess:
        nid = _seed(sess, headline="Acquires Foo Ltd")

    svc = PriorityService(session_factory=session_factory)
    res = svc.run_for(nid)

    assert res.deterministic.bucket == "medium"
    assert res.final.bucket == "medium"
    assert not res.used_llm_override

    with session_factory() as sess:
        row = sess.get(Notification, nid)
        assert row.det_priority == "medium"
        assert row.det_score == 50
        assert json.loads(row.det_reasons)
        assert row.ai_priority == "medium"
        assert row.ai_priority_score == 50
        assert row.pipeline_status == "summarize_pending"

        journal = sess.execute(
            select(PipelineJournal).where(PipelineJournal.notification_id == nid)
        ).scalars().all()
        assert len(journal) == 1
        assert journal[0].actor == "priority"
        assert journal[0].to_status == "summarize_pending"
        assert "final=medium" in (journal[0].error_message or "")


class _StubOverride(LlmPriorityOverride):
    def override(self, inp, deterministic, gemma_summary="", gemma_impact=""):
        return PriorityResult(
            bucket="important",
            score=85,
            reasons=list(deterministic.reasons) + ["LLM override -> important: bumped"],
            source="llm_override",
        )


def test_llm_override_upgrades_and_persists(session_factory) -> None:
    with session_factory() as sess:
        nid = _seed(sess, headline="Acquires Foo Ltd")

    svc = PriorityService(session_factory=session_factory, llm_override=_StubOverride())
    res = svc.run_for(nid)
    assert res.used_llm_override
    assert res.deterministic.bucket == "medium"
    assert res.final.bucket == "important"

    with session_factory() as sess:
        row = sess.get(Notification, nid)
        # Deterministic columns reflect the engine output
        assert row.det_priority == "medium"
        # ai_* columns reflect the LLM override
        assert row.ai_priority == "important"
        assert row.ai_priority_score == 85
        ai_reasons = json.loads(row.ai_priority_reasons)
        assert any("LLM override" in r for r in ai_reasons)


def test_newspaper_ad_yields_ignored_persisted(session_factory) -> None:
    with session_factory() as sess:
        nid = _seed(
            sess,
            headline="Copy of newspaper publication of Quarterly Results",
            category="Quarterly Results",
        )
    svc = PriorityService(session_factory=session_factory)
    res = svc.run_for(nid)
    assert res.final.bucket == "ignored"
    with session_factory() as sess:
        row = sess.get(Notification, nid)
        assert row.det_priority == "ignored"
        assert row.ai_priority == "ignored"
        assert row.ai_priority_score == 0
