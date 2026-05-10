"""Attachment pipeline orchestrator (Phase 7).

Pulls together: downloader -> deferred-tagger -> text extractor ->
optional vision fallback -> link resolver. Persists everything via
``SqlaNotificationRepo.update_attachment`` and journals the transition.

Status flow handled here:

  attachment_pending --> attachment_active (claim)
                     --> summarize_pending  (success: text or vision OK,
                                             OR no attachment -> nothing to do)
                     --> done_deferred      (deferred_doc_type set; body NOT
                                             fed to summarizer per FR-ATTACH-004)
                     --> attachment_failed  (download or both extractors errored)

Note: when ``deferred_doc_type`` is set, FR-ATTACH-004 says "MUST NOT have
their body text fed into the summarizer". We still want a summary from
headline + metadata, so the row still moves to ``summarize_pending`` with
``pdf_extracted_text`` cleared. Phase 8 reads ``deferred_doc_type`` and
generates the headline-only summary. We document this behavior here rather
than burying it in Phase 8.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..config.settings import get_settings
from ..db.repositories.journal_repo_sqla import SqlaJournalRepo
from ..db.repositories.notification_repo_sqla import SqlaNotificationRepo
from ..db.session import get_session
from .base import (
    AttachmentDownloader,
    DownloadResult,
    ExternalLinkSummary,
    ExtractionResult,
    LinkResolver,
    PdfImageExtractor,
    PdfTextExtractor,
)
from .deferred_tagger import DeferredDocTagger, DeferredTaggerInput
from .pdfplumber_extractor import PdfPlumberExtractor

logger = logging.getLogger(__name__)


@dataclass
class AttachmentRunResult:
    """What happened in one ``run_for`` call. Returned for tests + smoke harness."""

    notification_id: int
    final_status: str
    download_status: str
    local_path: Optional[Path]
    extraction_method: str  # 'pdfplumber' | 'gemma_vision' | 'none'
    text_chars: int
    image_summary_chars: int
    pdf_pages: int
    deferred_doc_type: Optional[str]
    external_links: list[ExternalLinkSummary] = field(default_factory=list)
    error: Optional[str] = None


class AttachmentService:
    """Orchestrates the full attachment lifecycle for a single notification.

    Args:
        downloader: required; the only network-touching component.
        text_extractor: defaults to ``PdfPlumberExtractor``.
        vision_extractor: optional; if provided + text is empty, the service
            renders + sends the PDF via the vision pass.
        link_resolver: optional; if provided + text is non-empty, the service
            extracts URLs and probes them.
        deferred_tagger: optional; defaults to ``DeferredDocTagger``.
        max_pages_text: cap for the text extractor (default 20).
        max_pages_vision: cap for the vision extractor (defaults to 5 inside
            the extractor itself; this knob isn't surfaced here).
        session_factory: testing seam.
    """

    def __init__(
        self,
        *,
        downloader: AttachmentDownloader,
        text_extractor: Optional[PdfTextExtractor] = None,
        vision_extractor: Optional[PdfImageExtractor] = None,
        link_resolver: Optional[LinkResolver] = None,
        deferred_tagger: Optional[DeferredDocTagger] = None,
        max_pages_text: Optional[int] = None,
        session_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.downloader = downloader
        self.text_extractor: PdfTextExtractor = (
            text_extractor or PdfPlumberExtractor()
        )
        self.vision_extractor = vision_extractor
        self.link_resolver = link_resolver
        self.deferred_tagger = deferred_tagger or DeferredDocTagger()
        self.max_pages_text = (
            max_pages_text
            if max_pages_text is not None
            else get_settings().pipeline.pdf_max_pages_default
        )
        self._session_factory = session_factory or get_session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run_for(self, notification_id: int) -> AttachmentRunResult:
        started_ms = time.monotonic()

        with self._session_factory() as sess:
            repo = SqlaNotificationRepo(sess)
            row = repo.get(notification_id)
            if row is None:
                raise ValueError(f"Notification {notification_id} not found")

        starting_status = row.get("pipeline_status", "attachment_pending")

        # ---- Step 1: download -----------------------------------------
        dl: DownloadResult = self.downloader.download(notification_id)

        # No URL = nothing to do; advance to summarize_pending with metadata only.
        if dl.skipped_reason == "no_url":
            return self._finalize_no_attachment(
                notification_id=notification_id,
                row=row,
                started_ms=started_ms,
                starting_status=starting_status,
            )

        if dl.error:
            return self._finalize_failed(
                notification_id=notification_id,
                started_ms=started_ms,
                starting_status=starting_status,
                error=dl.error,
                download_status="failed",
            )

        local_path = dl.local_path
        if local_path is None:  # defensive: success-path always has a path
            return self._finalize_failed(
                notification_id=notification_id,
                started_ms=started_ms,
                starting_status=starting_status,
                error="downloader_returned_no_path",
                download_status="failed",
            )

        # ---- Step 2: text extraction ----------------------------------
        text_result = self.text_extractor.extract(local_path, self.max_pages_text)

        # ---- Step 3: deferred-doc tagging -----------------------------
        # Tag using whatever signals we have so far. Even if extraction
        # failed, headline + ai_category may suffice to deferred-tag.
        deferred = self.deferred_tagger.tag(
            DeferredTaggerInput(
                headline=row.get("headline") or "",
                body=row.get("body") or "",
                category=row.get("category") or "",
                subcategory=row.get("subcategory") or "",
                ai_category=row.get("ai_category") or "",
                attachment_name=row.get("attachment_name") or "",
                pdf_text_head=text_result.text[:2048] if text_result.text else "",
                pdf_pages=text_result.pages_extracted,
            )
        )

        # ---- Step 4: vision fallback (only if text empty AND not deferred) ---
        # If the doc is deferred we deliberately skip the body; FR-ATTACH-004.
        # No point burning vision compute on annual reports.
        vision_result: Optional[ExtractionResult] = None
        text_is_empty = self._text_is_empty(text_result)
        if (
            text_is_empty
            and deferred is None
            and self.vision_extractor is not None
        ):
            vision_result = self.vision_extractor.extract_with_vision(
                local_path, self.max_pages_text
            )

        # ---- Step 5: link resolution (only on real text, not deferred) ----
        external_links: list[ExternalLinkSummary] = []
        if (
            self.link_resolver is not None
            and text_result.text
            and deferred is None
        ):
            try:
                external_links = self.link_resolver.resolve(text_result.text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "link resolver failed nid=%d err=%s", notification_id, exc
                )

        # ---- Step 6: persist + transition -----------------------------
        return self._finalize_success(
            notification_id=notification_id,
            started_ms=started_ms,
            starting_status=starting_status,
            download=dl,
            text_result=text_result,
            vision_result=vision_result,
            deferred_doc_type=deferred,
            external_links=external_links,
        )

    # ------------------------------------------------------------------
    # Finalize helpers
    # ------------------------------------------------------------------
    def _finalize_no_attachment(
        self,
        *,
        notification_id: int,
        row: dict[str, Any],
        started_ms: float,
        starting_status: str,
    ) -> AttachmentRunResult:
        next_status = "summarize_pending"
        # If the notification is non-junk + already classified, we still
        # might be deferred-tagged from headline alone (e.g. classifier
        # tagged "Annual Report"). Run the tagger to be safe.
        deferred = self.deferred_tagger.tag(
            DeferredTaggerInput(
                headline=row.get("headline") or "",
                body=row.get("body") or "",
                category=row.get("category") or "",
                subcategory=row.get("subcategory") or "",
                ai_category=row.get("ai_category") or "",
            )
        )
        if deferred is not None:
            next_status = "summarize_pending"  # still summarize -- headline only

        payload = {
            "download_status": "skipped",
            "local_path": None,
            "deferred_doc_type": deferred,
            "last_error": None,
        }
        self._persist(notification_id, payload, starting_status, next_status,
                      started_ms, error_kind=None, error_message="no_url")
        return AttachmentRunResult(
            notification_id=notification_id,
            final_status=next_status,
            download_status="skipped",
            local_path=None,
            extraction_method="none",
            text_chars=0,
            image_summary_chars=0,
            pdf_pages=0,
            deferred_doc_type=deferred,
            external_links=[],
        )

    def _finalize_failed(
        self,
        *,
        notification_id: int,
        started_ms: float,
        starting_status: str,
        error: str,
        download_status: str,
    ) -> AttachmentRunResult:
        next_status = "attachment_failed"
        payload = {
            "download_status": download_status,
            "last_error": error[:1000],
        }
        self._persist(notification_id, payload, starting_status, next_status,
                      started_ms, error_kind="attachment", error_message=error)
        return AttachmentRunResult(
            notification_id=notification_id,
            final_status=next_status,
            download_status=download_status,
            local_path=None,
            extraction_method="none",
            text_chars=0,
            image_summary_chars=0,
            pdf_pages=0,
            deferred_doc_type=None,
            error=error,
        )

    def _finalize_success(
        self,
        *,
        notification_id: int,
        started_ms: float,
        starting_status: str,
        download: DownloadResult,
        text_result: ExtractionResult,
        vision_result: Optional[ExtractionResult],
        deferred_doc_type: Optional[str],
        external_links: list[ExternalLinkSummary],
    ) -> AttachmentRunResult:
        text = text_result.text
        image_summary = vision_result.image_summary if vision_result else ""
        pdf_pages = (
            vision_result.pages_extracted
            if vision_result and vision_result.pages_extracted
            else text_result.pages_extracted
        )
        method = (
            "gemma_vision"
            if vision_result and vision_result.image_summary
            else ("pdfplumber" if text else "none")
        )

        # Per FR-ATTACH-004: deferred docs do NOT carry body text downstream.
        # We persist what we extracted (audit trail) but null out the
        # summarizer-bound fields so Phase 8 can't accidentally read them.
        # Decision: keep text in DB for forensics, but mask via deferred tag.
        # Phase 8 filters on `deferred_doc_type IS NOT NULL`.
        if deferred_doc_type is not None:
            persisted_text: Optional[str] = None
            persisted_image_summary: Optional[str] = None
            next_status = "summarize_pending"
        else:
            # Persist whichever we got. Empty strings normalize to None so
            # the column reads as NULL in SQL filters.
            persisted_text = text or None
            persisted_image_summary = image_summary or None
            # If both extractors came back empty AND we haven't deferred,
            # don't fail -- the summarizer can still operate on headline +
            # body + metadata. But surface the absence in last_error so the
            # health UI can flag it.
            next_status = "summarize_pending"

        text_md5 = hashlib.md5(text.encode("utf-8")).hexdigest() if text else None  # noqa: S324

        empty_extraction_note: Optional[str] = None
        if (
            deferred_doc_type is None
            and not text
            and not image_summary
        ):
            empty_extraction_note = (
                f"empty_extraction: text={text_result.error or 'empty'} "
                f"vision={(vision_result.error if vision_result else 'not_run')}"
            )

        payload: dict[str, Any] = {
            "download_status": (
                "done" if download.skipped_reason is None else "done"
            ),
            "local_path": str(download.local_path) if download.local_path else None,
            "pdf_extracted_text": persisted_text,
            "pdf_image_summary": persisted_image_summary,
            "pdf_pages": pdf_pages or None,
            "pdf_text_md5": text_md5,
            "deferred_doc_type": deferred_doc_type,
            "gemma_external_links": (
                json.dumps(
                    [_link_to_dict(l) for l in external_links],
                    ensure_ascii=False,
                )
                if external_links
                else None
            ),
            "last_error": empty_extraction_note,
        }

        message_parts = [
            f"download={download.skipped_reason or 'fresh'}",
            f"text_chars={len(text)}",
            f"image_chars={len(image_summary)}",
            f"pages={pdf_pages}",
            f"deferred={deferred_doc_type or '-'}",
            f"links={len(external_links)}",
        ]
        self._persist(
            notification_id,
            payload,
            starting_status,
            next_status,
            started_ms,
            error_kind=None,
            error_message=" ".join(message_parts),
        )
        return AttachmentRunResult(
            notification_id=notification_id,
            final_status=next_status,
            download_status=payload["download_status"],
            local_path=download.local_path,
            extraction_method=method,
            text_chars=len(text),
            image_summary_chars=len(image_summary),
            pdf_pages=pdf_pages,
            deferred_doc_type=deferred_doc_type,
            external_links=external_links,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _text_is_empty(result: ExtractionResult) -> bool:
        # Reuse the extractor's own threshold if it exposes one.
        if hasattr(result, "text") and isinstance(result.text, str):
            return len(result.text.strip()) < 40
        return True

    def _persist(
        self,
        notification_id: int,
        payload: dict[str, Any],
        starting_status: str,
        next_status: str,
        started_ms: float,
        *,
        error_kind: Optional[str],
        error_message: Optional[str],
    ) -> None:
        elapsed_ms = int((time.monotonic() - started_ms) * 1000)
        with self._session_factory() as sess:
            repo = SqlaNotificationRepo(sess)
            journal = SqlaJournalRepo(sess)
            repo.update_attachment(notification_id, payload)
            repo.update_pipeline_status(
                notification_id,
                from_status=starting_status,
                to_status=next_status,
            )
            journal.append(
                notification_id=notification_id,
                from_status=starting_status,
                to_status=next_status,
                actor="attachments",
                duration_ms=elapsed_ms,
                error_kind=error_kind,
                error_message=error_message,
            )


def _link_to_dict(link: ExternalLinkSummary) -> dict[str, Any]:
    return {
        "url": link.url,
        "target_kind": link.target_kind,
        "summary": link.summary,
        "error": link.error,
    }


__all__ = ["AttachmentService", "AttachmentRunResult"]
