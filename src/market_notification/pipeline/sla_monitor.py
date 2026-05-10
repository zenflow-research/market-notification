"""SLA monitor (Phase 5, FR-CLASSIFY-005).

Watches the ``classify_pending`` queue for rows that have been waiting longer
than ``threshold_minutes`` (default 5 — PLAN.md §6 / SPEC §FR-CLASSIFY-005).
Each detected breach is logged at WARNING and written to ``pipeline_journal``
once per row per breach (de-duped by checking the latest journal entry for
that notification).

Design
------
- Single background daemon-thread; ``start()`` is non-blocking.
- ``check_once()`` is the unit of work, kept synchronous + side-effect-only
  so unit tests can drive it directly without threads.
- The threshold and tick interval come from ``Settings`` but accept overrides
  for tests.
- When a notification graduates out of ``classify_pending`` (success or
  failure), the corresponding row is no longer flagged on subsequent ticks
  — the query filters by current pipeline_status only.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlalchemy import select

from ..db.models import PipelineJournal
from ..db.repositories.journal_repo_sqla import SqlaJournalRepo
from ..db.session import get_session

logger = logging.getLogger(__name__)


SLA_ACTOR = "sla_monitor"
SLA_BREACH_KIND = "sla_breach_classify_pending"


@dataclass(frozen=True)
class SlaCheckResult:
    """Counters returned by ``check_once`` (mainly for tests + Health UI)."""
    found: int
    new_breaches: int
    already_alerted: int


class SlaMonitor:
    """Watches the classify-queue freshness."""

    def __init__(
        self,
        *,
        threshold_minutes: int = 5,
        tick_interval_s: int = 60,
        session_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.threshold_minutes = threshold_minutes
        self.tick_interval_s = tick_interval_s
        self._session_factory = session_factory or get_session

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            logger.warning("SlaMonitor already running")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="SlaMonitor"
        )
        self._thread.start()
        logger.info(
            "SlaMonitor started (threshold=%dmin tick=%ds)",
            self.threshold_minutes, self.tick_interval_s,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        logger.info("SlaMonitor stopped")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception:  # noqa: BLE001
                logger.exception("SlaMonitor tick error")
            for _ in range(self.tick_interval_s):
                if self._stop.is_set():
                    return
                time.sleep(1)

    # ------------------------------------------------------------------
    # One pass — public for direct test use
    # ------------------------------------------------------------------
    def check_once(self) -> SlaCheckResult:
        """Find and log any classify_pending rows older than threshold.

        Returns a counter object for tests; emits exactly one journal entry
        per (notification, breach) so subsequent ticks don't spam the log.
        """
        new_breaches = 0
        already = 0

        with self._session_factory() as sess:
            journal = SqlaJournalRepo(sess)
            stale = journal.find_stale_in_status(
                "classify_pending", self.threshold_minutes
            )
            for row in stale:
                if _has_recent_breach_record(sess, row["id"]):
                    already += 1
                    continue
                journal.append(
                    notification_id=row["id"],
                    from_status="classify_pending",
                    to_status="classify_pending",  # state didn't change; this is an alert
                    actor=SLA_ACTOR,
                    duration_ms=0,
                    error_kind=SLA_BREACH_KIND,
                    error_message=(
                        f"classify_pending exceeded {self.threshold_minutes}min "
                        f"(notif id={row['id']} headline={row['headline'][:80]!r})"
                    ),
                )
                logger.warning(
                    "SLA breach: notif=%d source=%s headline=%r "
                    "in classify_pending > %dmin",
                    row["id"], row["source"], row["headline"][:80],
                    self.threshold_minutes,
                )
                new_breaches += 1

        return SlaCheckResult(
            found=len(stale),
            new_breaches=new_breaches,
            already_alerted=already,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _has_recent_breach_record(sess, notification_id: int) -> bool:
    """Return True if we've already journaled an SLA breach for this row.

    We re-alert only after a status change clears the queue; i.e. once a row
    is journaled with the breach kind, subsequent ticks ignore it until it
    leaves classify_pending. Practically this means at most one warning per
    row per stuck-period.
    """
    stmt = (
        select(PipelineJournal.id)
        .where(PipelineJournal.notification_id == notification_id)
        .where(PipelineJournal.actor == SLA_ACTOR)
        .where(PipelineJournal.error_kind == SLA_BREACH_KIND)
        .order_by(PipelineJournal.id.desc())
        .limit(1)
    )
    return sess.execute(stmt).scalar_one_or_none() is not None


__all__ = ["SlaMonitor", "SlaCheckResult", "SLA_ACTOR", "SLA_BREACH_KIND"]
