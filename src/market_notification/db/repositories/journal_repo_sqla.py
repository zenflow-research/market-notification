"""SQLAlchemy concrete impl of JournalRepoBase.

The pipeline_journal table is the audit log for every state transition.
It is also the queryable surface the SLA monitor uses to find rows that
have lingered in a status too long.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Notification, PipelineJournal
from .base import JournalRepoBase


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SqlaJournalRepo(JournalRepoBase):
    """SQLAlchemy-backed pipeline journal repository."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def append(  # noqa: PLR0913
        self,
        notification_id: int,
        from_status: str,
        to_status: str,
        actor: str,
        duration_ms: int,
        error_kind: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        row = PipelineJournal(
            notification_id=notification_id,
            from_status=from_status,
            to_status=to_status,
            at=_utc_now_naive(),
            actor=actor,
            duration_ms=duration_ms,
            error_kind=error_kind,
            error_message=(error_message or "")[:1000] or None,
        )
        self.session.add(row)
        self.session.flush()

    def find_stale_in_status(
        self, status: str, older_than_minutes: int
    ) -> list[dict[str, Any]]:
        """Return notifications stuck in ``status`` for more than N minutes.

        Uses ``last_status_change_at`` (set every time we transition) as the
        freshness clock. Falls back to ``fetched_at`` when the row has never
        transitioned (e.g. a row created directly in classify_pending).
        """
        cutoff = _utc_now_naive() - timedelta(minutes=older_than_minutes)
        anchor = func_coalesce(
            Notification.last_status_change_at, Notification.fetched_at
        )
        stmt = (
            select(
                Notification.id,
                Notification.source,
                Notification.headline,
                Notification.announced_at,
                Notification.pipeline_status,
                Notification.last_status_change_at,
                Notification.fetched_at,
            )
            .where(Notification.pipeline_status == status)
            .where(anchor <= cutoff)
            .order_by(Notification.announced_at.asc())
        )
        rows = self.session.execute(stmt).all()
        return [dict(r._mapping) for r in rows]


# Local import-aware wrapper avoids polluting the module top with a `func`
# alias that would shadow other usages.
def func_coalesce(a, b):  # noqa: ANN001
    from sqlalchemy import func as _f
    return _f.coalesce(a, b)
