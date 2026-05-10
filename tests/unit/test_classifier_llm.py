"""Unit tests: GemmaLlmClassifier with an injected fake Ollama transport.

We exercise three paths:
  1. Happy path — model returns a clean JSON, row is classified, status moves
     classify_pending -> priority_pending, journal entry recorded.
  2. Validation — model invents a category; we coerce to Uncategorized + fallback.
  3. Transport error — Ollama raises; we still write a fallback row + journal.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from market_notification.classifier.llm_classifier import (
    GemmaLlmClassifier,
    _LlmCallSpec,
)
from market_notification.classifier.taxonomy import UNCATEGORIZED
from market_notification.db.models import Notification, PipelineJournal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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


def _seed_notification(
    sess: Session,
    *,
    headline: str = "Test announcement",
    source: str = "BSE",
    category: str | None = "Acquisition",
    body: str | None = "Body.",
) -> int:
    n = Notification(
        company_id=1,
        source=source,
        headline=headline,
        category=category,
        subcategory=None,
        body=body,
        announced_at=_utc_now_naive(),
        pipeline_status="classify_pending",
    )
    sess.add(n)
    sess.commit()
    return n.id


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------
def test_happy_path_classifies_and_advances_status(session_factory) -> None:
    with session_factory() as sess:
        notif_id = _seed_notification(
            sess,
            headline="Acquires 100% stake in subsidiary",
            category="Acquisition",
        )

    captured: dict = {}

    def fake_transport(spec: _LlmCallSpec) -> str:
        captured["spec"] = spec
        return json.dumps({
            "ai_category": "Acquisition",
            "ai_category_group": "Deals & Partnerships",
            "confidence": 0.92,
        })

    classifier = GemmaLlmClassifier(
        model="test-model",
        transport=fake_transport,
        session_factory=session_factory,
    )
    result = classifier.classify(notif_id)

    assert result.category == "Acquisition"
    assert result.group == "Deals & Partnerships"
    assert result.source == "gemma"
    assert 0.9 < result.confidence <= 1.0

    # Persistence
    with session_factory() as sess:
        row = sess.get(Notification, notif_id)
        assert row.ai_category == "Acquisition"
        assert row.ai_category_group == "Deals & Partnerships"
        assert row.ai_category_source == "gemma"
        assert row.pipeline_status == "priority_pending"
        assert row.ai_classified_taxonomy_version
        assert row.ai_classified_prompt_version

        # Journal entry recorded
        journal = sess.execute(
            select(PipelineJournal).where(PipelineJournal.notification_id == notif_id)
        ).scalars().all()
        assert len(journal) == 1
        assert journal[0].actor == "classifier"
        assert journal[0].to_status == "priority_pending"

    # Prompt was constructed
    assert "Acquisition" in captured["spec"].user
    assert "Available categories" in captured["spec"].system


def test_invalid_category_falls_back_to_uncategorized(session_factory) -> None:
    with session_factory() as sess:
        notif_id = _seed_notification(sess)

    def fake_transport(spec: _LlmCallSpec) -> str:
        return json.dumps({
            "ai_category": "Made-Up Category",
            "ai_category_group": "Whatever",
            "confidence": 0.99,
        })

    classifier = GemmaLlmClassifier(
        model="test-model",
        transport=fake_transport,
        session_factory=session_factory,
    )
    result = classifier.classify(notif_id)

    assert result.category == UNCATEGORIZED
    assert result.source == "fallback"
    assert result.confidence <= 0.2

    with session_factory() as sess:
        row = sess.get(Notification, notif_id)
        assert row.ai_category == UNCATEGORIZED
        assert row.ai_category_source == "fallback"
        assert row.last_error and "fallback" in row.last_error
        assert row.pipeline_status == "priority_pending"


def test_transport_failure_falls_back_and_journals(session_factory) -> None:
    with session_factory() as sess:
        notif_id = _seed_notification(sess)

    def boom_transport(spec: _LlmCallSpec) -> str:
        raise ConnectionError("ollama unreachable")

    classifier = GemmaLlmClassifier(
        model="test-model",
        transport=boom_transport,
        session_factory=session_factory,
    )
    result = classifier.classify(notif_id)

    assert result.source == "fallback"
    assert result.category == UNCATEGORIZED
    assert "ollama_error" in result.reasoning

    with session_factory() as sess:
        row = sess.get(Notification, notif_id)
        assert row.ai_category_source == "fallback"
        assert row.pipeline_status == "priority_pending"
        journal = sess.execute(
            select(PipelineJournal).where(PipelineJournal.notification_id == notif_id)
        ).scalars().all()
        assert len(journal) == 1
        assert journal[0].error_kind == "fallback"
        assert "ollama_error" in (journal[0].error_message or "")


def test_legacy_alias_resolves_to_canonical(session_factory) -> None:
    with session_factory() as sess:
        notif_id = _seed_notification(sess)

    def fake_transport(spec: _LlmCallSpec) -> str:
        return json.dumps({
            "ai_category": "USFDA",  # legacy alias
            "ai_category_group": "Regulatory & Compliance",
            "confidence": 0.7,
        })

    classifier = GemmaLlmClassifier(
        model="test-model",
        transport=fake_transport,
        session_factory=session_factory,
    )
    result = classifier.classify(notif_id)
    assert result.category == "USFDA (Approval/Warning/Import Alert)"
    assert result.source == "gemma"  # alias path stays gemma-sourced
    assert "alias_resolved" in result.reasoning


def test_claim_next_for_classify_picks_newest(session_factory) -> None:
    """FR-CLASSIFY-002: classify newest first."""
    from market_notification.db.repositories.notification_repo_sqla import (
        SqlaNotificationRepo,
    )

    now = _utc_now_naive()
    with session_factory() as sess:
        for i, dt in enumerate([
            now - timedelta(minutes=30),
            now - timedelta(minutes=10),
            now,
        ]):
            sess.add(Notification(
                company_id=1,
                source="BSE",
                headline=f"Row {i}",
                announced_at=dt,
                pipeline_status="classify_pending",
            ))
        sess.commit()

    with session_factory() as sess:
        repo = SqlaNotificationRepo(sess)
        claimed = repo.claim_next_for_classify()
        assert claimed is not None
        assert claimed["headline"] == "Row 2"
        assert claimed["pipeline_status"] == "classify_active"

    # Second claim picks the next-newest
    with session_factory() as sess:
        repo = SqlaNotificationRepo(sess)
        claimed2 = repo.claim_next_for_classify()
        assert claimed2["headline"] == "Row 1"
