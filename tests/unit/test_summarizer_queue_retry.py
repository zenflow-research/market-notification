"""Unit tests: queue_retry policy (FR-SUMM-006).

Cases
-----
1. Single transient failure recovers — first transport raises, second
   succeeds; row ends up in `deep_dive_pending`; sleep called once.
2. Persistent outage hits retry_max — row ends up in `summarize_dead`;
   sleep called retry_max - 1 times; OllamaUnavailableError re-raised.
3. `record_failure` increments retry_count and stamps next_retry_at.
4. `record_failure` at the cap transitions to summarize_dead and clears
   next_retry_at.
5. `reset_for_retry` returns False for non-failed rows.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session, sessionmaker

from market_notification.db.models import Notification
from market_notification.summarizer.gemma_summarizer import (
    GemmaLlmSummarizer,
    OllamaUnavailableError,
    _LlmCallSpec,
)
from market_notification.summarizer.queue_retry import (
    DEFAULT_RETRY_MAX,
    RetryingSummarizer,
    record_failure,
    reset_for_retry,
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


def _seed(sess: Session, *, status: str = "summarize_pending", retry_count: int = 0) -> int:
    n = Notification(
        company_id=1,
        source="BSE",
        headline="Hdr",
        body="Body",
        announced_at=_utc_now_naive(),
        pipeline_status=status,
        retry_count=retry_count,
        ai_category="Acquisition",
        ai_priority="medium",
        ai_priority_score=50,
    )
    sess.add(n)
    sess.commit()
    return n.id


def _good_response_text() -> str:
    return json.dumps({
        "summary": "Recovered after transient failure.",
        "impact": "",
        "key_figures": [],
        "key_people": [],
        "key_dates": [],
        "attachments_referenced": [],
        "deferred_doc_tags": [],
        "external_links": [],
        "confidence": 0.5,
    })


def test_transient_failure_recovers(session_factory):
    with session_factory() as sess:
        nid = _seed(sess)

    calls = {"n": 0}

    def flaky(_spec: _LlmCallSpec) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OllamaUnavailableError("first time fails")
        return _good_response_text()

    sleeps: list[float] = []

    inner = GemmaLlmSummarizer(
        model="m",
        transport=flaky,
        session_factory=session_factory,
    )
    wrapped = RetryingSummarizer(
        inner,
        retry_max=DEFAULT_RETRY_MAX,
        retry_delay_s=0.0,
        session_factory=session_factory,
        sleep_fn=lambda s: sleeps.append(s),
    )

    run = wrapped.summarize_with_meta(nid)
    assert run.summary.summary == "Recovered after transient failure."
    # One sleep between attempt 1 and attempt 2
    assert len(sleeps) == 1

    with session_factory() as sess:
        row = sess.get(Notification, nid)
        assert row.pipeline_status == "deep_dive_pending"
        # retry_count stamped on the failed first attempt
        assert row.retry_count == 1


def test_persistent_outage_marks_dead(session_factory):
    with session_factory() as sess:
        nid = _seed(sess)

    def boom(_spec: _LlmCallSpec) -> str:
        raise OllamaUnavailableError("ollama down")

    sleeps: list[float] = []
    inner = GemmaLlmSummarizer(
        model="m",
        transport=boom,
        session_factory=session_factory,
    )
    wrapped = RetryingSummarizer(
        inner,
        retry_max=3,
        retry_delay_s=0.0,
        session_factory=session_factory,
        sleep_fn=lambda s: sleeps.append(s),
    )

    with pytest.raises(OllamaUnavailableError):
        wrapped.summarize_with_meta(nid)

    # 3 attempts -> 2 sleeps between attempts (3rd failure flips to dead and stops)
    assert len(sleeps) == 2

    with session_factory() as sess:
        row = sess.get(Notification, nid)
        assert row.pipeline_status == "summarize_dead"
        assert row.retry_count == 3
        assert row.next_retry_at is None
        assert "summarize_ollama_down" in (row.last_error or "")


def test_record_failure_first_failure(session_factory):
    with session_factory() as sess:
        nid = _seed(sess)

    outcome = record_failure(
        session_factory=session_factory,
        notification_id=nid,
        error=OllamaUnavailableError("nope"),
        retry_max=3,
        retry_delay_s=30.0,
    )
    assert outcome.new_status == "summarize_failed"
    assert outcome.retry_count == 1
    assert outcome.next_retry_at is not None

    with session_factory() as sess:
        row = sess.get(Notification, nid)
        assert row.pipeline_status == "summarize_failed"
        assert row.retry_count == 1
        assert row.next_retry_at is not None


def test_record_failure_at_cap_marks_dead(session_factory):
    with session_factory() as sess:
        nid = _seed(sess, retry_count=2)  # already at retry_max-1 with retry_max=3

    outcome = record_failure(
        session_factory=session_factory,
        notification_id=nid,
        error=OllamaUnavailableError("final"),
        retry_max=3,
    )
    assert outcome.new_status == "summarize_dead"
    assert outcome.retry_count == 3
    assert outcome.next_retry_at is None


def test_reset_for_retry_only_works_for_failed_rows(session_factory):
    with session_factory() as sess:
        nid_failed = _seed(sess, status="summarize_failed", retry_count=1)
        nid_pending = _seed(sess, status="summarize_pending")

    assert reset_for_retry(
        session_factory=session_factory, notification_id=nid_failed
    ) is True
    assert reset_for_retry(
        session_factory=session_factory, notification_id=nid_pending
    ) is False

    with session_factory() as sess:
        row = sess.get(Notification, nid_failed)
        assert row.pipeline_status == "summarize_pending"
