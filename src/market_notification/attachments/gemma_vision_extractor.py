"""Gemma-MoE multimodal vision fallback (FR-ATTACH-003).

When pdfplumber returns empty / near-empty text (scanned image PDFs), this
extractor renders each PDF page to PNG via PyMuPDF, ships the images to
Ollama's chat endpoint with the configured Gemma vision-capable model, and
concatenates the per-page summaries into ``image_summary``.

The transport is injectable so unit tests can stub Ollama. Production callers
get a default transport that lazy-imports the ``ollama`` Python client and
calls ``chat`` with ``images=[...]``.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .base import ExtractionResult, PdfImageExtractor

logger = logging.getLogger(__name__)


# Per-page render DPI. 150 DPI is enough for OCR-class extraction on
# typical 8.5x11 corporate filings without exploding the payload size.
DEFAULT_RENDER_DPI = 150
DEFAULT_VISION_PROMPT = (
    "You are an OCR + summarization assistant for Indian corporate filings. "
    "The image below is one page of a PDF that has no extractable text "
    "(it's scanned or image-only). Output a faithful Markdown rendering of "
    "ALL visible text, tables, signatures, and headers in order. Preserve "
    "amounts, dates, percentages, and proper nouns verbatim. Do not interpret. "
    "If the page is blank or non-textual, return the single token: BLANK."
)
DEFAULT_VISION_MODEL = "gemma4-zenflow-moe:latest"


@dataclass(frozen=True)
class _RenderedPage:
    page_number: int  # 1-indexed
    png_bytes: bytes


class GemmaVisionExtractor(PdfImageExtractor):
    """Vision fallback. Renders pages -> sends to Gemma -> concats output.

    Args:
        transport: callable(model, prompt, images_b64) -> per-page text. The
            default lazy-imports ``ollama`` and uses its ``chat`` API.
        model: ollama model id (must be vision-capable).
        prompt: per-page instruction; sent with each rendered page.
        render_dpi: PyMuPDF rasterization DPI.
        max_pages_render: cap on pages we render+send. Independent of the
            text extractor's cap so vision can be made stricter (it's
            substantially more expensive).
    """

    def __init__(
        self,
        *,
        transport: Optional[Callable[[str, str, list[str]], str]] = None,
        model: str = DEFAULT_VISION_MODEL,
        prompt: str = DEFAULT_VISION_PROMPT,
        render_dpi: int = DEFAULT_RENDER_DPI,
        max_pages_render: int = 5,
    ) -> None:
        self._transport = transport or _default_ollama_vision_transport
        self.model = model
        self.prompt = prompt
        self.render_dpi = render_dpi
        self.max_pages_render = max_pages_render

    def extract_with_vision(
        self, pdf_path: Path, max_pages: int
    ) -> ExtractionResult:
        if not pdf_path.exists():
            return _error("pdf_missing", f"file not found: {pdf_path}")

        try:
            pages = list(_render_pages(pdf_path, self.render_dpi, max_pages))
        except Exception as exc:  # noqa: BLE001
            return _error("render_failed", f"{exc}")

        if not pages:
            return _error("no_pages", "PDF had zero renderable pages")

        # We render up to `max_pages` (extractor's cap) but only ship up to
        # `max_pages_render` to the model. Two caps: one bounds disk/cpu, the
        # other bounds Ollama traffic.
        ship = pages[: self.max_pages_render]
        per_page_summaries: list[str] = []
        errors: list[str] = []

        for page in ship:
            try:
                b64 = base64.b64encode(page.png_bytes).decode("ascii")
                summary = self._transport(self.model, self.prompt, [b64]).strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "vision page %d failed: %s", page.page_number, exc
                )
                errors.append(f"p{page.page_number}: {exc}")
                continue
            if summary and summary.upper() != "BLANK":
                per_page_summaries.append(
                    f"### Page {page.page_number}\n\n{summary}"
                )

        image_summary = "\n\n".join(per_page_summaries).strip()
        err = None
        if not image_summary and errors:
            err = "vision_failed: " + " | ".join(errors[:3])
        elif not image_summary:
            err = "vision_empty"

        return ExtractionResult(
            text="",  # vision pass populates image_summary, not text
            pages_extracted=len(pages),
            method="gemma_vision",
            image_summary=image_summary,
            deferred_doc_type=None,
            error=err,
        )


def _render_pages(
    pdf_path: Path, dpi: int, max_pages: int
) -> list[_RenderedPage]:
    """Lazy-import PyMuPDF; render up to ``max_pages`` to PNG bytes."""
    import fitz  # type: ignore[import-not-found]  # PyMuPDF, lazy

    out: list[_RenderedPage] = []
    with fitz.open(pdf_path) as doc:
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        # Index-driven loop: PyMuPDF's Document type stub doesn't expose
        # __iter__, even though the runtime object is iterable.
        for idx in range(min(len(doc), max_pages)):
            page = doc.load_page(idx)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            out.append(
                _RenderedPage(page_number=idx + 1, png_bytes=pix.tobytes("png"))
            )
    return out


def _default_ollama_vision_transport(
    model: str, prompt: str, images_b64: list[str]
) -> str:
    """Default Ollama transport. Sends a single chat turn with images.

    Uses ``think=False`` (consistent with Phase 5's reasoning-model fix —
    without this Gemma's hidden chain-of-thought eats the entire token
    budget and leaves the visible response empty).
    """
    import ollama  # local import; only loaded when used

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt, "images": images_b64},
        ],
        "options": {"temperature": 0.1, "num_predict": 2048},
        "think": False,  # see Phase 5 fix; reasoning models burn budget on hidden CoT
    }
    try:
        resp = ollama.chat(**kwargs)
    except TypeError:
        # Older ollama clients don't accept `think`; retry without it.
        kwargs.pop("think", None)
        resp = ollama.chat(**kwargs)

    msg = resp.get("message") or {}
    return msg.get("content") or ""


def _error(kind: str, msg: str) -> ExtractionResult:
    return ExtractionResult(
        text="",
        pages_extracted=0,
        method="gemma_vision",
        image_summary="",
        deferred_doc_type=None,
        error=f"{kind}: {msg}",
    )


__all__ = [
    "GemmaVisionExtractor",
    "DEFAULT_VISION_PROMPT",
    "DEFAULT_VISION_MODEL",
    "DEFAULT_RENDER_DPI",
]
