"""SQLAlchemy concrete impl of FilterRuleRepoBase (Phase 4)."""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import NotificationFilterRule
from .base import FilterRuleRepoBase


class SqlaFilterRuleRepo(FilterRuleRepoBase):
    """Manages negative-list rules used by RegexFilterEngine."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_active(self, source: Optional[str] = None) -> list[dict[str, Any]]:
        stmt = select(NotificationFilterRule).where(
            NotificationFilterRule.is_active == 1
        )
        if source is not None:
            # Match this source OR no-source-specified (universal) rules.
            stmt = stmt.where(
                (NotificationFilterRule.source == source)
                | (NotificationFilterRule.source.is_(None))
            )
        stmt = stmt.order_by(NotificationFilterRule.id)
        return [_to_dict(r) for r in self.session.execute(stmt).scalars().all()]

    def add(  # noqa: PLR0913
        self,
        rule_type: str,
        pattern: str,
        source: Optional[str],
        action: str = "hide",
        created_by: str = "user",
        confidence: Optional[float] = None,
        reason: Optional[str] = None,
    ) -> int:
        """Insert a rule. Idempotent on (rule_type, pattern, source).

        SQLite's UNIQUE constraint treats NULL as distinct, which would let
        duplicate `source=None` rules slip through on re-seed. We do an
        explicit pre-check so rules with `source=None` (the common case for
        universal seeds) dedupe correctly.
        """
        existing = self._find(rule_type, pattern, source)
        if existing is not None:
            if not existing.is_active:
                existing.is_active = 1
                self.session.flush()
            return existing.id

        row = NotificationFilterRule(
            rule_type=rule_type,
            pattern=pattern,
            source=source,
            action=action,
            created_by=created_by,
            confidence=confidence,
            reason=reason,
            is_active=1,
        )
        self.session.add(row)
        try:
            self.session.flush()
            return row.id
        except IntegrityError:  # pragma: no cover -- defensive race guard
            self.session.rollback()
            existing = self._find(rule_type, pattern, source)
            if existing is None:
                raise
            return existing.id

    def _find(
        self,
        rule_type: str,
        pattern: str,
        source: Optional[str],
    ) -> Optional[NotificationFilterRule]:
        stmt = select(NotificationFilterRule).where(
            NotificationFilterRule.rule_type == rule_type,
            NotificationFilterRule.pattern == pattern,
        )
        if source is None:
            stmt = stmt.where(NotificationFilterRule.source.is_(None))
        else:
            stmt = stmt.where(NotificationFilterRule.source == source)
        return self.session.execute(stmt).scalar_one_or_none()

    def deactivate(self, rule_id: int) -> bool:
        row = self.session.get(NotificationFilterRule, rule_id)
        if row is None or not row.is_active:
            return False
        row.is_active = 0
        return True


def _to_dict(row: NotificationFilterRule) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}
