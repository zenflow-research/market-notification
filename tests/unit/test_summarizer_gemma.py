"""Unit tests: GemmaLlmSummarizer with an injected fake Ollama transport.

Cases
-----
1. Happy path — model returns a clean envelope; row advances
   summarize_pending → deep_dive_pending; gemma_* columns populated; journal entry.
2. Stricter-prompt retry — model returns junk JSON on attempt 1, clean on
   attempt 2; we observe two transport calls and the second `user` message
   carries the strict-retry preamble.
3. Persistent junk — model returns junk on every attempt; after
   MAX_STRICT_RETRIES we persist a degraded envelope, mark fallback,
   and `last_error` carries the validator errors.
4. Deferred-doc path — row has deferred_doc_type='earnings'; the deferred
   prompt is used (no body in user); status moves to done_deferred;
   `gemma_deferred_tags` JSON contains 'earnings'.
5. Transport failure — Ollama raises; OllamaUnavailableError propagates
   (NOT swallowed); row state untouched (queue_retry handles persistence).
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from market_notification.db.models import Notification, PipelineJournal
from market_notification.summarizer.gemma_summarizer import (
    GemmaLlmSummarizer,
    MAX_STRICT_RETRIES,
    OllamaUnavailableError,
    _LlmCallSpec,
)


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


def _seed(
    sess: Session,
    *,
    headline: str = "Acquisition of XYZ Pvt Ltd",
    pipeline_status: str = "summarize_pending",
    body: str = "Long body content with INR 100 Cr deal size.",
    pdf_text: str | None = None,
    deferred_doc_type: str | None = None,
    ai_category: str = "Acquisition",
    ai_priority: str = "important",
) -> int:
    n = Notification(
        company_id=1,
        source="BSE",
        headline=headline,
        category="Acquisition",
        subcategory=None,
        body=body,
        pdf_extracted_text=pdf_text,
        announced_at=_utc_now_naive(),
        pipeline_status=pipeline_status,
        ai_category=ai_category,
        ai_category_group="Deals & Partnerships",
        ai_priority=ai_priority,
        ai_priority_score=85,
        deferred_doc_type=deferred_doc_type,
    )
    sess.add(n)
    sess.commit()
    return n.id


def _good_response(
    summary: str = "Acquired XYZ for INR 100 Cr.",
    confidence: float = 0.9,
    deferred_tags: list[str] | None = None,
) -> str:
    return json.dumps({
        "summary": summary,
        "impact": "Adds materially to FY26 revenue.",
        "key_figures": [
            {"label": "deal_size", "value": "100", "unit": "INR Cr"},
        ],
        "key_people": [],
        "key_dates": [],
        "attachments_referenced": [],
        "deferred_doc_tags": deferred_tags or [],
        "external_links": [],
        "confidence": confidence,
    })


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------
def test_happy_path(session_factory) -> None:
    with session_factory() as sess:
        nid = _seed(sess)

    captured_specs: list[_LlmCallSpec] = []

    def fake_transport(spec: _LlmCallSpec) -> str:
        captured_specs.append(spec)
        return _good_response()

    summarizer = GemmaLlmSummarizer(
        model="test-model",
        transport=fake_transport,
        session_factory=session_factory,
    )
    run = summarizer.summarize_with_meta(nid)

    assert run.attempts == 1
    assert run.fallback is False
    assert run.summary.summary.startswith("Acquired XYZ")
    assert run.summary.confidence == 0.9
    assert run.summary.used_model == "test-model"
    assert run.summary.used_prompt_version.startswith("summarize_v1.")

    with session_factory() as sess:
        row = sess.get(Notification, nid)
        assert row.pipeline_status == "deep_dive_pending"
        assert row.gemma_summary.startswith("Acquired XYZ")
        assert row.gemma_impact
        kfs = json.loads(row.gemma_key_figures)
        assert kfs[0]["label"] == "deal_size"
        assert kfs[0]["value"] == "100"  # FR-SUMM-003 verbatim
        assert kfs[0]["unit"] == "INR Cr"
        assert json.loads(row.gemma_deferred_tags) == []
        assert row.gemma_model_version == "test-model"
        assert row.gemma_prompt_version.startswith("summarize_v1.")
        assert row.gemma_summarized_at is not None
        assert row.last_error is None

        journal = sess.execute(
            select(PipelineJournal).where(PipelineJournal.notification_id == nid)
        ).scalars().all()
        assert len(journal) == 1
        assert journal[0].actor == "summarizer"
        assert journal[0].to_status == "deep_dive_pending"

    # System prompt embedded the schema; user prompt embedded the row data
    assert "Output schema" in captured_specs[0].system
    assert "PRESERVE FIGURES VERBATIM" in captured_specs[0].system
    assert "INR 100 Cr" in captured_specs[0].user


def test_stricter_prompt_retry_recovers(session_factory) -> None:
    with session_factory() as sess:
        nid = _seed(sess)

    call_log: list[str] = []

    def flaky_transport(spec: _LlmCallSpec) -> str:
        call_log.append(spec.user)
        if len(call_log) == 1:
            # First attempt: empty summary triggers stricter-prompt retry
            return json.dumps({
                "summary": "",
                "impact": "",
                "key_figures": [],
                "key_people": [],
                "key_dates": [],
                "attachments_referenced": [],
                "deferred_doc_tags": [],
                "external_links": [],
                "confidence": 0.0,
            })
        return _good_response()

    summarizer = GemmaLlmSummarizer(
        model="m",
        transport=flaky_transport,
        session_factory=session_factory,
    )
    run = summarizer.summarize_with_meta(nid)

    assert run.attempts == 2
    assert run.fallback is False
    # Second attempt's user prompt contains the strict-retry preamble
    assert "previous attempt produced output that failed validation" in call_log[1]
    assert "empty_summary" in call_log[1]
    # The original payload is still appended at the tail
    assert "INR 100 Cr" in call_log[1]

    with session_factory() as sess:
        row = sess.get(Notification, nid)
        assert row.pipeline_status == "deep_dive_pending"
        assert row.gemma_summary.startswith("Acquired XYZ")


def test_persistent_junk_falls_back(session_factory) -> None:
    with session_factory() as sess:
        nid = _seed(sess)

    def bad_transport(_spec: _LlmCallSpec) -> str:
        return "not even json {{{"

    summarizer = GemmaLlmSummarizer(
        model="m",
        transport=bad_transport,
        session_factory=session_factory,
    )
    run = summarizer.summarize_with_meta(nid)

    # 1 initial + MAX_STRICT_RETRIES retries
    assert run.attempts == 1 + MAX_STRICT_RETRIES
    assert run.fallback is True
    assert run.summary.summary == ""

    with session_factory() as sess:
        row = sess.get(Notification, nid)
        # Even on fallback we advance — losing the row entirely would be worse.
        assert row.pipeline_status == "deep_dive_pending"
        assert row.last_error and row.last_error.startswith("summarize_fallback:")
        journal = sess.execute(
            select(PipelineJournal).where(PipelineJournal.notification_id == nid)
        ).scalars().all()
        assert journal[0].error_kind == "fallback"


def test_deferred_doc_path(session_factory) -> None:
    with session_factory() as sess:
        nid = _seed(
            sess,
            headline="Quarterly Results — Q1FY26",
            deferred_doc_type="earnings",
            body="should not be in prompt",  # we will assert it isn't
            ai_category="Quarterly Results",
        )

    captured: list[_LlmCallSpec] = []

    def fake_transport(spec: _LlmCallSpec) -> str:
        captured.append(spec)
        # Note: model "forgot" to add the deferred tag — we test the
        # post-processing safety net.
        return _good_response(
            summary="Quarterly results intimation for Q1FY26.",
            deferred_tags=[],
        )

    summarizer = GemmaLlmSummarizer(
        model="m",
        transport=fake_transport,
        session_factory=session_factory,
    )
    run = summarizer.summarize_with_meta(nid)

    assert run.used_deferred_prompt is True
    assert "earnings" in run.summary.deferred_doc_tags

    # Body is intentionally withheld from the user prompt
    assert "should not be in prompt" not in captured[0].user
    assert "intentionally withheld" in captured[0].user

    with session_factory() as sess:
        row = sess.get(Notification, nid)
        assert row.pipeline_status == "done_deferred"
        tags = json.loads(row.gemma_deferred_tags)
        assert "earnings" in tags


def test_transport_failure_propagates(session_factory) -> None:
    with session_factory() as sess:
        nid = _seed(sess)

    def boom(_spec: _LlmCallSpec) -> str:
        raise OllamaUnavailableError("connection refused")

    summarizer = GemmaLlmSummarizer(
        model="m",
        transport=boom,
        session_factory=session_factory,
    )
    with pytest.raises(OllamaUnavailableError):
        summarizer.summarize_with_meta(nid)

    # Row state untouched — the queue_retry layer is responsible for
    # marking summarize_failed.
    with session_factory() as sess:
        row = sess.get(Notification, nid)
        assert row.pipeline_status == "summarize_pending"
        assert row.gemma_summary is None
