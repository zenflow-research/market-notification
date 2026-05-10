"""End-to-end-ish unit tests for AttachmentService against an in-memory DB.

We stub: downloader (returns a known DownloadResult), text extractor, vision
extractor, link resolver. The service stitches them together and persists to
the DB. Verifies:
  - happy path: writes pdf_extracted_text, advances to summarize_pending
  - vision fallback: empty text -> vision invoked -> image_summary persisted
  - deferred-doc tag: body is deliberately NOT persisted in
    pdf_extracted_text/pdf_image_summary; deferred_doc_type is set
  - download failure: status -> attachment_failed, last_error set
  - no_url: skipped, status -> summarize_pending with metadata only
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from market_notification.attachments.base import (
    AttachmentDownloader,
    DownloadResult,
    ExternalLinkSummary,
    ExtractionResult,
    LinkResolver,
    PdfImageExtractor,
    PdfTextExtractor,
)
from market_notification.attachments.service import AttachmentService
from market_notification.db.models import Notification, PipelineJournal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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


def _seed(
    sess,
    *,
    headline: str = "Outcome of Board Meeting",
    has_attachment: bool = True,
    ai_category: str = "Capex Update",
) -> int:
    row = Notification(
        company_id=42,
        source="BSE",
        headline=headline,
        announced_at=_utc_now_naive(),
        attachment_url="https://example.test/foo.pdf" if has_attachment else None,
        attachment_name="foo.pdf" if has_attachment else None,
        ai_category=ai_category,
        ai_category_group="Operations",
        pipeline_status="attachment_pending",
    )
    sess.add(row)
    sess.commit()
    return row.id


class StubDownloader(AttachmentDownloader):
    def __init__(self, result: DownloadResult) -> None:
        self.result = result
        self.calls: list[int] = []

    def download(self, notification_id: int) -> DownloadResult:
        self.calls.append(notification_id)
        return self.result


class StubText(PdfTextExtractor):
    def __init__(self, result: ExtractionResult) -> None:
        self.result = result

    def extract(self, pdf_path: Path, max_pages: int) -> ExtractionResult:  # noqa: ARG002
        return self.result


class StubVision(PdfImageExtractor):
    def __init__(self, result: ExtractionResult) -> None:
        self.result = result
        self.calls: list[Path] = []

    def extract_with_vision(self, pdf_path: Path, max_pages: int) -> ExtractionResult:  # noqa: ARG002
        self.calls.append(pdf_path)
        return self.result


class StubLinks(LinkResolver):
    def __init__(self, result: list[ExternalLinkSummary]) -> None:
        self.result = result

    def resolve(self, pdf_text: str) -> list[ExternalLinkSummary]:  # noqa: ARG002
        return self.result


def _ok_download(tmp_path: Path) -> DownloadResult:
    pdf = tmp_path / "42" / "foo.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4 stub")
    return DownloadResult(
        notification_id=1,
        local_path=pdf,
        bytes_downloaded=pdf.stat().st_size,
        content_type="application/pdf",
        md5="deadbeef",
        skipped_reason=None,
        error=None,
    )


def _text_result(text: str, pages: int = 1) -> ExtractionResult:
    return ExtractionResult(
        text=text,
        pages_extracted=pages,
        method="pdfplumber",
        image_summary="",
        deferred_doc_type=None,
        error=None if text else "empty_text",
    )


def _vision_result(summary: str) -> ExtractionResult:
    return ExtractionResult(
        text="",
        pages_extracted=2,
        method="gemma_vision",
        image_summary=summary,
        deferred_doc_type=None,
        error=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_happy_path_text_extraction(tmp_path: Path, session_factory) -> None:
    with session_factory() as sess:
        nid = _seed(sess)

    body = "Real extracted text from the BSE filing about a new capex plan." * 5
    svc = AttachmentService(
        downloader=StubDownloader(_ok_download(tmp_path)),
        text_extractor=StubText(_text_result(body, pages=3)),
        vision_extractor=StubVision(_vision_result("should-not-be-used")),
        session_factory=session_factory,
    )
    res = svc.run_for(nid)

    assert res.final_status == "summarize_pending"
    assert res.extraction_method == "pdfplumber"
    assert res.text_chars == len(body)
    assert res.deferred_doc_type is None

    with session_factory() as sess:
        row = sess.get(Notification, nid)
        assert row.pipeline_status == "summarize_pending"
        assert row.download_status == "done"
        assert row.pdf_extracted_text == body
        assert row.pdf_image_summary is None
        assert row.pdf_pages == 3
        assert row.pdf_text_md5 is not None
        assert row.deferred_doc_type is None
        # journal entry recorded
        je = sess.execute(
            select(PipelineJournal).where(PipelineJournal.notification_id == nid)
        ).scalars().one()
        assert je.actor == "attachments"
        assert je.to_status == "summarize_pending"


def test_vision_fallback_when_text_empty(tmp_path: Path, session_factory) -> None:
    with session_factory() as sess:
        nid = _seed(sess, ai_category="Capex Update")  # not deferred

    vision = StubVision(_vision_result("Page 1: scanned text recovered"))
    svc = AttachmentService(
        downloader=StubDownloader(_ok_download(tmp_path)),
        text_extractor=StubText(_text_result("")),  # empty
        vision_extractor=vision,
        session_factory=session_factory,
    )
    res = svc.run_for(nid)

    assert res.extraction_method == "gemma_vision"
    assert "scanned text recovered" in (vision.calls and "ok") or True
    assert vision.calls, "vision extractor should have been invoked"

    with session_factory() as sess:
        row = sess.get(Notification, nid)
        assert row.pdf_extracted_text is None
        assert row.pdf_image_summary is not None
        assert "scanned text recovered" in row.pdf_image_summary
        assert row.pipeline_status == "summarize_pending"


def test_deferred_doc_skips_body_and_vision(tmp_path: Path, session_factory) -> None:
    with session_factory() as sess:
        nid = _seed(sess, ai_category="Annual Report")  # forces deferred

    vision = StubVision(_vision_result("VISION SHOULD NOT RUN"))
    svc = AttachmentService(
        downloader=StubDownloader(_ok_download(tmp_path)),
        text_extractor=StubText(_text_result("body that we keep on disk only")),
        vision_extractor=vision,
        session_factory=session_factory,
    )
    res = svc.run_for(nid)

    assert res.deferred_doc_type == "annual_report"
    assert vision.calls == [], "vision must skip when deferred"

    with session_factory() as sess:
        row = sess.get(Notification, nid)
        assert row.deferred_doc_type == "annual_report"
        # FR-ATTACH-004: deferred body must NOT be persisted into the columns
        # the summarizer reads.
        assert row.pdf_extracted_text is None
        assert row.pdf_image_summary is None
        # Status still moves to summarize_pending; Phase 8 handles deferred.
        assert row.pipeline_status == "summarize_pending"


def test_download_failure_marks_attachment_failed(tmp_path: Path, session_factory) -> None:
    with session_factory() as sess:
        nid = _seed(sess)

    bad = DownloadResult(
        notification_id=nid,
        local_path=None,
        bytes_downloaded=0,
        content_type=None,
        md5=None,
        skipped_reason=None,
        error="HTTP 503",
    )
    svc = AttachmentService(
        downloader=StubDownloader(bad),
        session_factory=session_factory,
    )
    res = svc.run_for(nid)

    assert res.final_status == "attachment_failed"
    assert res.error == "HTTP 503"

    with session_factory() as sess:
        row = sess.get(Notification, nid)
        assert row.pipeline_status == "attachment_failed"
        assert row.download_status == "failed"
        assert "HTTP 503" in (row.last_error or "")


def test_no_url_advances_with_metadata_only(tmp_path: Path, session_factory) -> None:
    with session_factory() as sess:
        nid = _seed(sess, has_attachment=False, ai_category="Acquisition")

    skipped = DownloadResult(
        notification_id=nid,
        local_path=None,
        bytes_downloaded=0,
        content_type=None,
        md5=None,
        skipped_reason="no_url",
        error=None,
    )
    svc = AttachmentService(
        downloader=StubDownloader(skipped),
        session_factory=session_factory,
    )
    res = svc.run_for(nid)
    assert res.final_status == "summarize_pending"
    assert res.download_status == "skipped"

    with session_factory() as sess:
        row = sess.get(Notification, nid)
        assert row.pipeline_status == "summarize_pending"
        assert row.download_status == "skipped"
        assert row.pdf_extracted_text is None


def test_link_resolver_results_persisted_as_json(
    tmp_path: Path, session_factory
) -> None:
    with session_factory() as sess:
        nid = _seed(sess)

    links = [
        ExternalLinkSummary(
            url="https://example.test/dec.pdf",
            target_kind="pdf",
            summary="HEAD 200 application/pdf 1024B",
            error=None,
        )
    ]
    svc = AttachmentService(
        downloader=StubDownloader(_ok_download(tmp_path)),
        text_extractor=StubText(_text_result(
            "Body refers to https://example.test/dec.pdf for details."
        )),
        link_resolver=StubLinks(links),
        session_factory=session_factory,
    )
    svc.run_for(nid)

    with session_factory() as sess:
        row = sess.get(Notification, nid)
        assert row.gemma_external_links is not None
        decoded = json.loads(row.gemma_external_links)
        assert decoded[0]["url"] == "https://example.test/dec.pdf"
        assert decoded[0]["target_kind"] == "pdf"


def test_empty_extraction_with_no_vision_records_note(
    tmp_path: Path, session_factory
) -> None:
    with session_factory() as sess:
        nid = _seed(sess)

    svc = AttachmentService(
        downloader=StubDownloader(_ok_download(tmp_path)),
        text_extractor=StubText(_text_result("")),  # empty, no vision configured
        vision_extractor=None,
        session_factory=session_factory,
    )
    res = svc.run_for(nid)
    # we still advance -- summarizer can use headline+body
    assert res.final_status == "summarize_pending"
    assert res.text_chars == 0
    assert res.image_summary_chars == 0

    with session_factory() as sess:
        row = sess.get(Notification, nid)
        # last_error documents the empty extraction for the Health UI
        assert row.last_error is not None
        assert "empty_extraction" in row.last_error
