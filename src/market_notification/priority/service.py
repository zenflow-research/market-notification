"""Priority orchestrator — runs deterministic + (optional) LLM override and persists.

Layer above the engines: pulls the row from the repo, builds the
``NotificationPriorityInput`` + (optional) ``CompanyDTO``/``FundamentalsDTO``,
calls the deterministic scorer, then optionally the LLM override,
persists via `SqlaNotificationRepo.update_priority`, advances pipeline_status,
and journals the transition.

Designed to be reusable from the dispatcher (Phase 10) and the smoke
harness (Phase 6 verification).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..companies.base import CompanyDTO, CompanyProvider, FundamentalsDTO
from ..db.repositories.journal_repo_sqla import SqlaJournalRepo
from ..db.repositories.notification_repo_sqla import SqlaNotificationRepo
from ..db.session import get_session
from .base import (
    DeterministicPriority,
    LlmPriorityOverride,
    NotificationPriorityInput,
    PriorityResult,
)
from .deterministic import DeterministicScorer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriorityRunResult:
    """Returned by `PriorityService.run_for` so callers can verify outcomes."""
    notification_id: int
    deterministic: PriorityResult
    final: PriorityResult
    used_llm_override: bool


class PriorityService:
    """Orchestrates priority scoring + persistence.

    Args:
        company_provider: optional read-only company/fundamentals source.
            When provided, deterministic threshold rules that depend on
            mcap or annual sales fire correctly. When None, those rules
            silently no-op (which is fine for backfill scenarios).
        deterministic: optional engine override (defaults to
            ``DeterministicScorer``).
        llm_override: optional LLM override engine. When None, only the
            deterministic verdict is persisted and `ai_priority` mirrors
            `det_priority`.
        next_status: pipeline_status to set after a successful run.
            Defaults to "summarize_pending" since attachment processing
            is owned by the dispatcher's status machine in Phase 10.
        session_factory: optional override (defaults to ``get_session``).
    """

    def __init__(
        self,
        *,
        company_provider: Optional[CompanyProvider] = None,
        deterministic: Optional[DeterministicPriority] = None,
        llm_override: Optional[LlmPriorityOverride] = None,
        next_status: str = "summarize_pending",
        session_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.company_provider = company_provider
        self.deterministic: DeterministicPriority = deterministic or DeterministicScorer()
        self.llm_override = llm_override
        self.next_status = next_status
        self._session_factory = session_factory or get_session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run_for(self, notification_id: int) -> PriorityRunResult:
        started_ms = time.monotonic()

        with self._session_factory() as sess:
            repo = SqlaNotificationRepo(sess)
            row = repo.get(notification_id)
            if row is None:
                raise ValueError(f"Notification {notification_id} not found")

        company, fundamentals = self._lookup_company(row.get("company_id"))
        inp = NotificationPriorityInput(
            notification_id=notification_id,
            headline=row.get("headline") or "",
            body=row.get("body") or "",
            pdf_text=row.get("pdf_extracted_text") or "",
            ai_category=row.get("ai_category") or "",
            ai_category_group=row.get("ai_category_group") or "",
        )

        det_result = self.deterministic.score(inp, company, fundamentals)  # type: ignore[arg-type]

        used_llm = False
        if self.llm_override is not None:
            llm_result = self.llm_override.override(
                inp,
                det_result,
                gemma_summary=row.get("gemma_summary") or "",
                gemma_impact=row.get("gemma_impact") or "",
            )
            # The override returns deterministic when it failed; only count it
            # as "used" when source flipped to llm_override.
            used_llm = llm_result.source == "llm_override"
            final = llm_result
        else:
            final = det_result

        elapsed_ms = int((time.monotonic() - started_ms) * 1000)

        with self._session_factory() as sess:
            repo = SqlaNotificationRepo(sess)
            journal = SqlaJournalRepo(sess)
            repo.update_priority(
                notification_id,
                det=_priority_to_payload(det_result),
                llm=_priority_to_payload(final) if final is not det_result else None,
            )
            current_status = row.get("pipeline_status", "priority_pending")
            repo.update_pipeline_status(
                notification_id,
                from_status=current_status,
                to_status=self.next_status,
            )
            journal.append(
                notification_id=notification_id,
                from_status=current_status,
                to_status=self.next_status,
                actor="priority",
                duration_ms=elapsed_ms,
                error_kind=None,
                error_message=(
                    f"final={final.bucket} score={final.score} "
                    f"source={final.source} llm_used={used_llm}"
                ),
            )

        return PriorityRunResult(
            notification_id=notification_id,
            deterministic=det_result,
            final=final,
            used_llm_override=used_llm,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _lookup_company(
        self, company_id: Optional[int]
    ) -> tuple[Optional[CompanyDTO], Optional[FundamentalsDTO]]:
        if not company_id or self.company_provider is None:
            return None, None
        try:
            company = self.company_provider.get_by_company_id(company_id)
        except Exception:  # noqa: BLE001
            logger.exception("company lookup failed for id=%d", company_id)
            return None, None
        if company is None:
            return None, None
        try:
            fundamentals = self.company_provider.get_fundamentals(company_id)
        except Exception:  # noqa: BLE001
            logger.exception("fundamentals lookup failed for id=%d", company_id)
            fundamentals = None
        return company, fundamentals


def _priority_to_payload(result: PriorityResult) -> dict[str, Any]:
    return {
        "bucket": result.bucket,
        "score": result.score,
        "reasons": list(result.reasons),
        "source": result.source,
    }


__all__ = ["PriorityService", "PriorityRunResult"]
