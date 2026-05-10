"""Repository ABCs. The single change-point for SQLite -> Postgres migration.

Repositories return plain DTOs / dicts, never SQLAlchemy ORM objects.
This isolates persistence concerns from the rest of the codebase.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional


class NotificationRepoBase(ABC):
    @abstractmethod
    def insert(self, payload: dict[str, Any]) -> int:
        """Insert a new notification row. Returns the new id.

        On uniqueness violation, returns the existing id (idempotent insert).
        """

    @abstractmethod
    def get(self, notification_id: int) -> Optional[dict[str, Any]]: ...

    @abstractmethod
    def update_pipeline_status(
        self,
        notification_id: int,
        from_status: str,
        to_status: str,
        error: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> bool:
        """Conditional state transition. Returns True if updated, False if from_status mismatched."""

    @abstractmethod
    def claim_next(
        self,
        from_status: str,
        active_status: str,
        priority_filter: Optional[list[str]] = None,
    ) -> Optional[dict[str, Any]]:
        """Atomically pick + lock the next row in `from_status`, set to `active_status`."""

    @abstractmethod
    def update_classification(
        self, notification_id: int, result: dict[str, Any]
    ) -> None: ...

    @abstractmethod
    def update_priority(
        self, notification_id: int, det: dict[str, Any], llm: Optional[dict[str, Any]]
    ) -> None: ...

    @abstractmethod
    def update_summary(self, notification_id: int, result: dict[str, Any]) -> None: ...

    @abstractmethod
    def update_deep_dive(self, notification_id: int, result: dict[str, Any]) -> None: ...

    @abstractmethod
    def list_for_ui(
        self,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        priority_in: Optional[list[str]] = None,
        category_in: Optional[list[str]] = None,
        source_in: Optional[list[str]] = None,
        company_id: Optional[int] = None,
        include_ignored: bool = False,
        include_imported_legacy: bool = False,
        limit: int = 200,
        offset: int = 0,
        search: Optional[str] = None,
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def count_by_status(self) -> dict[str, int]: ...

    @abstractmethod
    def count_by_priority(self) -> dict[str, int]: ...


class FilterRuleRepoBase(ABC):
    @abstractmethod
    def list_active(self, source: Optional[str] = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def add(
        self,
        rule_type: str,
        pattern: str,
        source: Optional[str],
        action: str = "hide",
        created_by: str = "user",
        confidence: Optional[float] = None,
        reason: Optional[str] = None,
    ) -> int: ...

    @abstractmethod
    def deactivate(self, rule_id: int) -> bool: ...


class PollStateRepoBase(ABC):
    @abstractmethod
    def get(self, source: str) -> Optional[dict[str, Any]]: ...

    @abstractmethod
    def upsert(
        self,
        source: str,
        status: str,
        last_poll_at: datetime,
        last_seq_id: Optional[str] = None,
        last_date: Optional[str] = None,
        records_fetched: int = 0,
        error_message: Optional[str] = None,
    ) -> None: ...


class JournalRepoBase(ABC):
    @abstractmethod
    def append(
        self,
        notification_id: int,
        from_status: str,
        to_status: str,
        actor: str,
        duration_ms: int,
        error_kind: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None: ...

    @abstractmethod
    def find_stale_in_status(
        self, status: str, older_than_minutes: int
    ) -> list[dict[str, Any]]: ...
