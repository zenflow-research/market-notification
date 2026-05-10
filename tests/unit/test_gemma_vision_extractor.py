"""Unit tests for GemmaVisionExtractor.

Renders a tiny PDF, then asserts the injected transport gets one base64 PNG
per page (up to ``max_pages_render``) and the per-page text gets concatenated.
The transport is fully stubbed -- no Ollama dependency.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from market_notification.attachments.gemma_vision_extractor import (
    GemmaVisionExtractor,
)


@pytest.fixture()
def fitz_module():  # noqa: PT004
    return pytest.importorskip("fitz")


def _make_pdf(path: Path, fitz, *, pages: int = 3) -> None:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Image-only page {i + 1}", fontsize=14)
    doc.save(path)
    doc.close()


def test_renders_pages_and_concatenates(tmp_path: Path, fitz_module) -> None:
    pdf = tmp_path / "scan.pdf"
    _make_pdf(pdf, fitz_module, pages=3)

    captured: list[tuple[str, list[str]]] = []

    def transport(model: str, prompt: str, images: list[str]) -> str:
        # one image per call (we ship one page at a time)
        captured.append((model, images))
        idx = len(captured)
        return f"PAGE_{idx}_TEXT"

    ext = GemmaVisionExtractor(
        transport=transport, model="vision-test", max_pages_render=10
    )
    res = ext.extract_with_vision(pdf, max_pages=10)

    assert res.method == "gemma_vision"
    assert res.error is None
    assert "PAGE_1_TEXT" in res.image_summary
    assert "PAGE_3_TEXT" in res.image_summary
    assert "### Page 1" in res.image_summary
    assert len(captured) == 3
    # each page gets exactly one image attached
    assert all(len(images) == 1 for _, images in captured)
    assert all(model == "vision-test" for model, _ in captured)


def test_max_pages_render_caps_traffic(tmp_path: Path, fitz_module) -> None:
    pdf = tmp_path / "long.pdf"
    _make_pdf(pdf, fitz_module, pages=10)

    calls = {"n": 0}

    def transport(_m: str, _p: str, _i: list[str]) -> str:  # noqa: ARG001
        calls["n"] += 1
        return f"page-{calls['n']}"

    ext = GemmaVisionExtractor(transport=transport, max_pages_render=2)
    res = ext.extract_with_vision(pdf, max_pages=10)
    assert calls["n"] == 2
    assert "page-1" in res.image_summary
    assert "page-2" in res.image_summary


def test_blank_response_filtered_out(tmp_path: Path, fitz_module) -> None:
    pdf = tmp_path / "blanky.pdf"
    _make_pdf(pdf, fitz_module, pages=3)

    seq = iter(["BLANK", "real text", "BLANK"])

    def transport(_m: str, _p: str, _i: list[str]) -> str:  # noqa: ARG001
        return next(seq)

    res = GemmaVisionExtractor(transport=transport).extract_with_vision(
        pdf, max_pages=10
    )
    assert "real text" in res.image_summary
    assert "BLANK" not in res.image_summary
    # only page 2 survived the blank filter
    assert "### Page 2" in res.image_summary
    assert "### Page 1" not in res.image_summary


def test_transport_failure_recorded_in_error(tmp_path: Path, fitz_module) -> None:
    pdf = tmp_path / "bad.pdf"
    _make_pdf(pdf, fitz_module, pages=2)

    def transport(_m: str, _p: str, _i: list[str]) -> str:  # noqa: ARG001
        raise RuntimeError("ollama down")

    res = GemmaVisionExtractor(transport=transport).extract_with_vision(
        pdf, max_pages=2
    )
    assert res.error is not None
    assert "vision_failed" in res.error
    assert "ollama down" in res.error
    assert res.image_summary == ""


def test_missing_pdf_returns_error(tmp_path: Path) -> None:
    res = GemmaVisionExtractor(
        transport=lambda *_a, **_k: ""
    ).extract_with_vision(tmp_path / "nope.pdf", max_pages=10)
    assert res.error is not None
    assert "pdf_missing" in res.error


def test_corrupt_pdf_returns_error(tmp_path: Path) -> None:
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"not a pdf")

    captured: Optional[str] = None

    def transport(_m: str, _p: str, _i: list[str]) -> str:  # noqa: ARG001
        return "should not be called"

    res = GemmaVisionExtractor(transport=transport).extract_with_vision(
        bad, max_pages=10
    )
    assert res.error is not None
    assert "render_failed" in res.error
    # transport must not have been invoked
    assert captured is None
