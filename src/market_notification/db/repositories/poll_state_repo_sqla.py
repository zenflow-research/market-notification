"""SQLAlchemy concrete impl of PollStateRepoBase."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import NotificationPollState
from .base import PollStateRepoBase


class SqlaPollStateRepo(PollStateRepoBase):
    """SQLAlchemy-backed poll-state repository (one row per source)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, source: str) -> Optional[dict[str, Any]]:
        stmt = select(NotificationPollState).where(
            NotificationPollState.source == source
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return {c.name: getattr(row, c.name) for c in row.__table__.columns}

    def upsert(  # noqa: PLR0913
        self,
        source: str,
        status: str,
        last_poll_at: datetime,
        last_seq_id: Optional[str] = None,
        last_date: Optional[str] = None,
        records_fetched: int = 0,
        error_message: Optional[str] = None,
    ) -> None:
        stmt = select(NotificationPollState).where(
            NotificationPollState.source == source
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        if row is None:
            row = NotificationPollState(source=source)
            self.session.add(row)

        row.status = status
        row.last_poll_at = last_poll_at
        if last_seq_id is not None:
            row.last_seq_id = last_seq_id
        if last_date is not None:
            row.last_date = last_date
        row.records_fetched = records_fetched
        row.error_message = error_message
        row.updated_at = _utc_now_naive()
