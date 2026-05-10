"""Ollama-down retry policy for the summarizer (FR-SUMM-006).

Separation of concerns
----------------------
``GemmaLlmSummarizer`` raises ``OllamaUnavailableError`` on any transport
failure. This module wraps the summarizer with the **queue-level** retry
policy that FR-SUMM-006 mandates:

  attempt 1 fails  -> pipeline_status='summarize_failed', retry_count=1,
                      next_retry_at = now + 30s
  attempt 2 fails  -> retry_count=2, next_retry_at = now + 30s
  attempt 3 fails  -> pipeline_status='summarize_dead', retry_count=3
                      (Health UI surfaces these for manual intervention)

Why a separate module
---------------------
The summarizer itself only knows about ONE row at a time and ONE Ollama call.
The queue policy needs to:

  * persist retry state across worker restarts (``retry_count``,
    ``next_retry_at`` columns already exist on ``Notification``)
  * sleep between attempts without holding a DB lock
  * decide that "Ollama is back" by attempting the call rather than probing
    a separate endpoint (probing /api/tags vs. actually generating can give
    different answers — the model itself might be unloaded even if the
    daemon is up)

Wrapping the summarizer at this layer keeps each layer single-purpose and
lets the dispatcher (Phase 10) treat ``RetryingSummarizer`` as an opaque
``Summarizer`` implementation.

This module exposes both:
  * ``RetryingSummarizer`` — inline retry-with-sleep, suitable for the
    smoke harness and for a long-running worker that wants to drain
    transient blips without involving the dispatcher.
  * ``record_failure`` — pure DB helper that stamps the row's retry state
    and computes the next status. The dispatcher (Phase 10) uses this
    when it prefers async / cron-driven retries over inline sleep.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from ..db.models import Notification
from ..db.repositories.journal_repo_sqla import SqlaJournalRepo
from ..db.session import get_session
from .base import Summarizer, SummaryResult
from .gemma_summarizer import (
    GemmaLlmSummarizer,
    OllamaUnavailableError,
    SummarizeRunResult,
)

logger = logging.getLogger(__name__)


# Defaults follow FR-SUMM-006: 30s retry, retry_max=3.
DEFAULT_RETRY_MAX = 3
DEFAULT_RETRY_DELAY_S = 30.0


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Pure DB helper — used by both inline retry and dispatcher-driven retry.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FailureOutcome:
    """Result of recording an Ollama-down failure on a row."""
    notification_id: int
    new_status: str  # 'summarize_failed' | 'summarize_dead'
    retry_count: int
    next_retry_at: Optional[datetime]


def record_failure(
    *,
    session_factory: Callable[[], Any],
    notification_id: int,
    error: Exception,
    retry_max: int = DEFAULT_RETRY_MAX,
    retry_delay_s: float = DEFAULT_RETRY_DELAY_S,
) -> FailureOutcome:
    """Persist an Ollama-down failure and decide the next status.

    Atomically:
      * increments ``retry_count``
      * if ``retry_count >= retry_max`` -> ``summarize_dead`` (terminal)
        else                            -> ``summarize_failed`` (will retry)
      * sets ``next_retry_at = now + retry_delay_s`` (only when not dead)
      * stamps ``last_error`` with the exception repr (truncated)
      * journals the transition with ``error_kind='ollama_unavailable'``

    The caller drives the actual retry timing — either by sleeping inline
    (``RetryingSummarizer``) or by polling for rows where ``next_retry_at
    <= now`` (dispatcher).
    """
    now = _utc_now_naive()

    with session_factory() as sess:
        row = sess.get(Notification, notification_id)
        if row is None:
            raise ValueError(f"Notification {notification_id} not found")

        prev_status = row.pipeline_status
        new_count = (row.retry_count or 0) + 1
        if new_count >= retry_max:
            new_status = "summarize_dead"
            next_retry: Optional[datetime] = None
        else:
            new_status = "summarize_failed"
            next_retry = now + timedelta(seconds=retry_delay_s)

        row.retry_count = new_count
        row.pipeline_status = new_status
        row.last_status_change_at = now
        row.next_retry_at = next_retry
        row.last_error = (f"summarize_ollama_down: {error!r}")[:1000]

        SqlaJournalRepo(sess).append(
            notification_id=notification_id,
            from_status=prev_status,
            to_status=new_status,
            actor="summarizer",
            duration_ms=0,
            error_kind="ollama_unavailable",
            error_message=(f"attempt={new_count}/{retry_max}: {error!r}")[:1000],
        )

    return FailureOutcome(
        notification_id=notification_id,
        new_status=new_status,
        retry_count=new_count,
        next_retry_at=next_retry,
    )


def reset_for_retry(
    *,
    session_factory: Callable[[], Any],
    notification_id: int,
) -> bool:
    """Move a `summarize_failed` row back to `summarize_pending` for retry.

    Used by the dispatcher-style flow (and the smoke harness) after the
    ``next_retry_at`` deadline passes. Returns True if the row was eligible
    and moved; False if it was no longer in ``summarize_failed`` (e.g.
    something else picked it up first).
    """
    with session_factory() as sess:
        row = sess.get(Notification, notification_id)
        if row is None or row.pipeline_status != "summarize_failed":
            return False
        row.pipeline_status = "summarize_pending"
        row.last_status_change_at = _utc_now_naive()
        SqlaJournalRepo(sess).append(
            notification_id=notification_id,
            from_status="summarize_failed",
            to_status="summarize_pending",
            actor="summarizer",
            duration_ms=0,
            error_kind=None,
            error_message=f"retry_count={row.retry_count or 0}",
        )
    return True


# ---------------------------------------------------------------------------
# Inline retrying summarizer — the high-level wrapper.
# ---------------------------------------------------------------------------
class RetryingSummarizer(Summarizer):
    """Wrap a ``GemmaLlmSummarizer`` with FR-SUMM-006 inline retry.

    Each Ollama-down failure records the failure (via ``record_failure``),
    sleeps for ``retry_delay_s`` (skippable in tests via ``sleep_fn``),
    and re-attempts up to ``retry_max`` total tries. After the final
    failure, the row is left in ``summarize_dead`` and we re-raise the
    last ``OllamaUnavailableError`` so the caller knows the unit-of-work
    didn't produce a summary.

    Notes
    -----
    * Schema-validation failures (model returned junk JSON) are NOT
      retried here — the inner summarizer already handles those at the
      prompt level (FR-SUMM-002 stricter-prompt retries). FR-SUMM-006
      retry is ONLY for transport-level outages.
    * ``next_retry_at`` is stamped on every failure, even though the
      inline path doesn't strictly need it — that way an external
      dispatcher inspecting the DB sees a consistent view.
    """

    def __init__(
        self,
        inner: GemmaLlmSummarizer,
        *,
        retry_max: int = DEFAULT_RETRY_MAX,
        retry_delay_s: float = DEFAULT_RETRY_DELAY_S,
        session_factory: Optional[Callable[[], Any]] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.inner = inner
        self.retry_max = retry_max
        self.retry_delay_s = retry_delay_s
        self._session_factory = session_factory or get_session
        self._sleep_fn = sleep_fn

    # ------------------------------------------------------------------
    # ABC + rich variants
    # ------------------------------------------------------------------
    def summarize(self, notification_id: int) -> SummaryResult:
        return self.summarize_with_meta(notification_id).summary

    def summarize_with_meta(self, notification_id: int) -> SummarizeRunResult:
        last_err: Optional[OllamaUnavailableError] = None
        for attempt in range(1, self.retry_max + 1):
            try:
                return self.inner.summarize_with_meta(notification_id)
            except OllamaUnavailableError as e:
                last_err = e
                outcome = record_failure(
                    session_factory=self._session_factory,
                    notification_id=notification_id,
                    error=e,
                    retry_max=self.retry_max,
                    retry_delay_s=self.retry_delay_s,
                )
                logger.warning(
                    "summarize attempt=%d/%d failed for notif=%d: %s -> %s",
                    attempt, self.retry_max, notification_id, e, outcome.new_status,
                )
                if outcome.new_status == "summarize_dead":
                    break
                self._sleep_fn(self.retry_delay_s)
                # Move the row back to summarize_pending before retrying so
                # the inner summarizer's `from_status` check matches.
                reset_for_retry(
                    session_factory=self._session_factory,
                    notification_id=notification_id,
                )
        # Exhausted retries.
        assert last_err is not None
        raise last_err


__all__ = [
    "DEFAULT_RETRY_DELAY_S",
    "DEFAULT_RETRY_MAX",
    "FailureOutcome",
    "RetryingSummarizer",
    "record_failure",
    "reset_for_retry",
]
