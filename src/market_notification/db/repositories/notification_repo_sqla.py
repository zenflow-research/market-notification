"""SQLAlchemy concrete impl of NotificationRepoBase.

Phase 3 only needs: insert (idempotent on natural key), get, count_by_*,
and update_pipeline_status. Methods owned by later phases are stubbed with
NotImplementedError so the ABC is satisfied without reaching ahead.

Phase 5 adds: update_classification (write ai_* fields for the LLM result),
and claim_next_for_classify (latest-first claim of `classify_pending` rows
per FR-CLASSIFY-002). The generic `claim_next` remains stubbed for Phase 10.

Phase 6 adds: update_priority (write det_* and ai_priority_* columns;
both deterministic and LLM-override outputs land here).

Phase 7 adds: update_attachment (write download/extraction outputs --
download_status, local_path, pdf_extracted_text, pdf_image_summary,
pdf_pages, pdf_text_md5, deferred_doc_type, gemma_external_links).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Notification
from .base import NotificationRepoBase

logger = logging.getLogger(__name__)


class SqlaNotificationRepo(NotificationRepoBase):
    """SQLAlchemy-backed notification repository."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Phase 3 surface
    # ------------------------------------------------------------------
    def insert(self, payload: dict[str, Any]) -> int:
        """Insert a row. Idempotent on the natural key.

        On UNIQUE constraint violation (`uq_notification_natural_key`), we
        roll back and look up the existing id -- callers get back the same
        id whether the row is new or pre-existing.
        """
        row = Notification(**payload)
        self.session.add(row)
        try:
            self.session.flush()
            return row.id
        except IntegrityError:
            self.session.rollback()
            existing = self._find_by_natural_key(
                source=payload["source"],
                company_id=payload["company_id"],
                announced_at=payload["announced_at"],
                headline=payload["headline"],
            )
            if existing is None:
                raise  # pragma: no cover -- shouldn't happen
            return existing

    def _find_by_natural_key(
        self,
        source: str,
        company_id: int,
        announced_at: datetime,
        headline: str,
    ) -> Optional[int]:
        stmt = select(Notification.id).where(
            Notification.source == source,
            Notification.company_id == company_id,
            Notification.announced_at == announced_at,
            Notification.headline == headline,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def exists_by_natural_key(
        self,
        source: str,
        company_id: int,
        announced_at: datetime,
        headline: str,
    ) -> bool:
        return self._find_by_natural_key(
            source, company_id, announced_at, headline
        ) is not None

    def get(self, notification_id: int) -> Optional[dict[str, Any]]:
        row = self.session.get(Notification, notification_id)
        return _row_to_dict(row) if row else None

    def update_pipeline_status(
        self,
        notification_id: int,
        from_status: str,
        to_status: str,
        error: Optional[str] = None,
        actor: Optional[str] = None,  # noqa: ARG002 -- accepted for ABC compat
    ) -> bool:
        """Conditional state transition. Returns True if updated."""
        row = self.session.get(Notification, notification_id)
        if row is None or row.pipeline_status != from_status:
            return False
        row.pipeline_status = to_status
        row.last_status_change_at = _utc_now_naive()
        if error is not None:
            row.last_error = error[:1000]
        return True

    def count_by_status(self) -> dict[str, int]:
        stmt = select(
            Notification.pipeline_status, func.count(Notification.id)
        ).group_by(Notification.pipeline_status)
        return {status: cnt for status, cnt in self.session.execute(stmt).all()}

    def count_by_priority(self) -> dict[str, int]:
        stmt = select(
            Notification.ai_priority, func.count(Notification.id)
        ).group_by(Notification.ai_priority)
        return {p or "unknown": cnt for p, cnt in self.session.execute(stmt).all()}

    # ------------------------------------------------------------------
    # Phase 5 surface (classifier)
    # ------------------------------------------------------------------
    def claim_next_for_classify(
        self, active_status: str = "classify_active"
    ) -> Optional[dict[str, Any]]:
        """Atomically claim the newest `classify_pending` row.

        FR-CLASSIFY-002 mandates latest-first ordering — the user values the
        freshest notifications most. Picks by ``announced_at DESC, id DESC``,
        flips the row to `active_status` under the same transaction, and
        returns it as a dict. Returns None if nothing is pending.
        """
        stmt = (
            select(Notification)
            .where(Notification.pipeline_status == "classify_pending")
            .order_by(Notification.announced_at.desc(), Notification.id.desc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        try:
            row = self.session.execute(stmt).scalar_one_or_none()
        except Exception:  # noqa: BLE001
            # SQLite doesn't support FOR UPDATE / SKIP LOCKED. Retry without.
            stmt_no_lock = (
                select(Notification)
                .where(Notification.pipeline_status == "classify_pending")
                .order_by(Notification.announced_at.desc(), Notification.id.desc())
                .limit(1)
            )
            row = self.session.execute(stmt_no_lock).scalar_one_or_none()
        if row is None:
            return None
        row.pipeline_status = active_status
        row.last_status_change_at = _utc_now_naive()
        self.session.flush()
        return _row_to_dict(row)

    def update_classification(
        self, notification_id: int, result: dict[str, Any]
    ) -> None:
        """Persist Gemma classification output.

        Writes:
          - ai_category, ai_category_group, ai_category_confidence
          - ai_category_source ('gemma' | 'fallback')
          - ai_classified_at / taxonomy_version / prompt_version
          - last_error cleared on success

        ``result`` keys (caller's responsibility):
          category (str), group (str), confidence (float),
          source (str), reasoning (str), taxonomy_version (str),
          prompt_version (str). The reasoning, if provided, is stored
          inline in ``last_error`` only on fallback so the UI Health
          tab can surface it; otherwise it is dropped from the row.
        """
        row = self.session.get(Notification, notification_id)
        if row is None:
            raise ValueError(f"Notification {notification_id} not found")

        row.ai_category = result.get("category")
        row.ai_category_group = result.get("group")
        conf = result.get("confidence")
        row.ai_category_confidence = float(conf) if conf is not None else None
        row.ai_category_source = result.get("source") or "gemma"
        row.ai_classified_at = _utc_now_naive()
        row.ai_classified_taxonomy_version = result.get("taxonomy_version")
        row.ai_classified_prompt_version = result.get("prompt_version")
        # Successful classification clears any prior error; failed-and-fellback
        # carries reasoning forward for debugging.
        if row.ai_category_source == "fallback":
            reasoning = result.get("reasoning") or ""
            row.last_error = (f"classify_fallback: {reasoning}")[:1000] or None
        else:
            row.last_error = None
        self.session.flush()

    # ------------------------------------------------------------------
    # Phase 6 surface (priority engine)
    # ------------------------------------------------------------------
    def update_priority(
        self,
        notification_id: int,
        det: dict[str, Any],
        llm: Optional[dict[str, Any]],
    ) -> None:
        """Persist priority engine outputs.

        Writes both the deterministic verdict (`det_*`) and the final
        verdict (`ai_priority*`). When ``llm`` is None (or the override
        carried the deterministic result through), the final verdict
        equals the deterministic one. Reasons are JSON-encoded lists.
        """
        row = self.session.get(Notification, notification_id)
        if row is None:
            raise ValueError(f"Notification {notification_id} not found")

        det_bucket = det.get("bucket")
        det_score = int(det.get("score", 0))
        det_reasons = det.get("reasons") or []

        row.det_priority = det_bucket
        row.det_score = det_score
        row.det_reasons = json.dumps(det_reasons, ensure_ascii=False)

        if llm is not None:
            row.ai_priority = llm.get("bucket") or det_bucket
            row.ai_priority_score = int(llm.get("score", det_score))
            row.ai_priority_reasons = json.dumps(
                llm.get("reasons") or det_reasons, ensure_ascii=False
            )
        else:
            row.ai_priority = det_bucket
            row.ai_priority_score = det_score
            row.ai_priority_reasons = json.dumps(det_reasons, ensure_ascii=False)

        self.session.flush()

    # ------------------------------------------------------------------
    # Phase 7 surface (attachment processing)
    # ------------------------------------------------------------------
    def update_attachment(
        self, notification_id: int, payload: dict[str, Any]
    ) -> None:
        """Persist downloader + extractor outputs in a single round-trip.

        ``payload`` keys (all optional; missing keys leave the column
        untouched -- the caller is responsible for clearing fields they
        deliberately want blank by passing the empty value):

          download_status        str
          local_path             Optional[str]
          pdf_extracted_text     Optional[str]
          pdf_image_summary      Optional[str]
          pdf_pages              Optional[int]
          pdf_text_md5           Optional[str]
          deferred_doc_type      Optional[str]
          gemma_external_links   Optional[str]  (already JSON-encoded)
          last_error             Optional[str]  (cleared on success)
        """
        row = self.session.get(Notification, notification_id)
        if row is None:
            raise ValueError(f"Notification {notification_id} not found")

        for col in (
            "download_status",
            "local_path",
            "pdf_extracted_text",
            "pdf_image_summary",
            "pdf_pages",
            "pdf_text_md5",
            "deferred_doc_type",
            "gemma_external_links",
            "last_error",
        ):
            if col in payload:
                setattr(row, col, payload[col])

        self.session.flush()

    # ------------------------------------------------------------------
    # Phase 8 surface (summarizer)
    # ------------------------------------------------------------------
    def update_summary(
        self, notification_id: int, result: dict[str, Any]
    ) -> None:
        """Persist the FR-SUMM-002 envelope onto a notification row.

        ``result`` keys (caller-side responsibility — already JSON-encoded
        for the list/dict columns; kept that way so the caller controls
        encoding once and the repo stays a thin pass-through):

          summary                  Optional[str]
          impact                   Optional[str]
          key_figures              Optional[str]   JSON
          key_people               Optional[str]   JSON
          key_dates                Optional[str]   JSON
          attachments_referenced   Optional[str]   JSON
          deferred_tags            Optional[str]   JSON
          external_links           Optional[str]   JSON
          confidence               Optional[float]
          model_version            Optional[str]
          prompt_version           Optional[str]
          last_error               Optional[str]   (cleared on success)

        ``gemma_summarized_at`` is stamped server-side by the repo on every
        call so the caller doesn't need to manage the clock.

        Note on terminology: the row column for the deferred-tag list is
        ``gemma_deferred_tags`` (named after the model output); the
        result-dict key is ``deferred_tags`` to match the orchestrator's
        local naming. We bridge the two here.
        """
        row = self.session.get(Notification, notification_id)
        if row is None:
            raise ValueError(f"Notification {notification_id} not found")

        column_map: dict[str, str] = {
            "summary": "gemma_summary",
            "impact": "gemma_impact",
            "key_figures": "gemma_key_figures",
            "key_people": "gemma_key_people",
            "key_dates": "gemma_key_dates",
            "attachments_referenced": "gemma_attachments_referenced",
            "deferred_tags": "gemma_deferred_tags",
            "external_links": "gemma_external_links",
            "confidence": "gemma_confidence",
            "model_version": "gemma_model_version",
            "prompt_version": "gemma_prompt_version",
        }
        for key, col in column_map.items():
            if key in result:
                setattr(row, col, result[key])

        if "last_error" in result:
            row.last_error = result["last_error"]

        row.gemma_summarized_at = _utc_now_naive()
        self.session.flush()

    # ------------------------------------------------------------------
    # Methods owned by later phases -- stubbed for now
    # ------------------------------------------------------------------
    def claim_next(
        self,
        from_status: str,  # noqa: ARG002
        active_status: str,  # noqa: ARG002
        priority_filter: Optional[list[str]] = None,  # noqa: ARG002
    ) -> Optional[dict[str, Any]]:
        raise NotImplementedError("claim_next is implemented in Phase 10")

    def update_deep_dive(
        self, notification_id: int, result: dict[str, Any]  # noqa: ARG002
    ) -> None:
        raise NotImplementedError("update_deep_dive is implemented in Phase 11")

    def list_for_ui(  # noqa: PLR0913
        self,
        from_date: Optional[datetime] = None,  # noqa: ARG002
        to_date: Optional[datetime] = None,  # noqa: ARG002
        priority_in: Optional[list[str]] = None,  # noqa: ARG002
        category_in: Optional[list[str]] = None,  # noqa: ARG002
        source_in: Optional[list[str]] = None,  # noqa: ARG002
        company_id: Optional[int] = None,  # noqa: ARG002
        include_ignored: bool = False,  # noqa: ARG002
        include_imported_legacy: bool = False,  # noqa: ARG002
        limit: int = 200,  # noqa: ARG002
        offset: int = 0,  # noqa: ARG002
        search: Optional[str] = None,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        raise NotImplementedError("list_for_ui is implemented in Phase 12")


def _row_to_dict(row: Notification) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}
