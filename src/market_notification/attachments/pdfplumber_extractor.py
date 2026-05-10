"""pdfplumber-based PDF text extractor (FR-ATTACH-002).

Reads up to ``max_pages`` (default 20 per ``settings.pipeline.pdf_max_pages_default``).
Returns whatever text it finds; the orchestrator decides whether to fall back
to vision based on the emptiness check below.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .base import ExtractionResult, PdfTextExtractor

logger = logging.getLogger(__name__)

# Empty-text heuristic: scanned PDFs frequently return a few stray ligatures
# even with no real OCR. Below this character count we consider the extraction
# a miss and recommend the vision fallback.
EMPTY_TEXT_THRESHOLD = 40


class PdfPlumberExtractor(PdfTextExtractor):
    """Default text-only extractor.

    Args:
        empty_threshold: chars below which the result is considered "empty"
            (which the orchestrator interprets as "try vision next").
    """

    def __init__(self, *, empty_threshold: int = EMPTY_TEXT_THRESHOLD) -> None:
        self.empty_threshold = empty_threshold

    def extract(self, pdf_path: Path, max_pages: int) -> ExtractionResult:
        try:
            import pdfplumber  # local import keeps test discovery cheap
        except ImportError as exc:  # pragma: no cover -- declared dep
            return _error("pdfplumber_missing", f"{exc}")

        if not pdf_path.exists():
            return _error("pdf_missing", f"file not found: {pdf_path}")

        text_chunks: list[str] = []
        pages_in_file = 0
        try:
            with pdfplumber.open(pdf_path) as pdf:
                pages_in_file = len(pdf.pages)
                for idx, page in enumerate(pdf.pages):
                    if idx >= max_pages:
                        break
                    try:
                        chunk = page.extract_text() or ""
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "pdfplumber page %d failed: %s", idx, exc
                        )
                        chunk = ""
                    if chunk.strip():
                        text_chunks.append(chunk.strip())
        except Exception as exc:  # noqa: BLE001
            return _error("pdfplumber_failed", f"{exc}", pages=pages_in_file)

        text = "\n\n".join(text_chunks).strip()

        # ``pages_extracted`` carries the file's TOTAL page count (not the
        # capped read count). The orchestrator uses it to apply the
        # ">20 pages -> deferred large_misc" rule even when we stopped early.
        return ExtractionResult(
            text=text,
            pages_extracted=pages_in_file,
            method="pdfplumber",
            image_summary="",
            deferred_doc_type=None,
            error=None if text else "empty_text",
        )

    def is_empty(self, result: ExtractionResult) -> bool:
        return len(result.text.strip()) < self.empty_threshold


def _error(kind: str, msg: str, pages: int = 0) -> ExtractionResult:
    return ExtractionResult(
        text="",
        pages_extracted=pages,
        method="pdfplumber",
        image_summary="",
        deferred_doc_type=None,
        error=f"{kind}: {msg}",
    )


__all__ = ["PdfPlumberExtractor", "EMPTY_TEXT_THRESHOLD"]
