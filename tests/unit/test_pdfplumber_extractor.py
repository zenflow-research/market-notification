"""Unit tests for PdfPlumberExtractor.

Builds a real, tiny PDF on the fly with PyMuPDF (already a project dep) and
extracts text from it. We avoid bundling fixture PDFs to keep the test
package self-contained.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from market_notification.attachments.pdfplumber_extractor import (
    EMPTY_TEXT_THRESHOLD,
    PdfPlumberExtractor,
)


@pytest.fixture()
def fitz_module():  # noqa: PT004 -- module ref, not autouse
    fitz = pytest.importorskip("fitz")
    return fitz


def _make_text_pdf(path: Path, fitz, *, lines: list[str], pages: int = 1) -> None:
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        for i, line in enumerate(lines):
            page.insert_text((72, 72 + 14 * i), line, fontsize=12)
    doc.save(path)
    doc.close()


def _make_blank_pdf(path: Path, fitz, pages: int = 1) -> None:
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(path)
    doc.close()


def test_extracts_text_happy_path(tmp_path: Path, fitz_module) -> None:
    pdf = tmp_path / "filing.pdf"
    _make_text_pdf(pdf, fitz_module, lines=[
        "RELIANCE INDUSTRIES LIMITED",
        "Outcome of Board Meeting held on 23 April 2026",
        "Approved capex of Rs 1500 crore for new petrochemical plant.",
    ])
    ext = PdfPlumberExtractor()
    res = ext.extract(pdf, max_pages=20)
    assert res.error is None
    assert "RELIANCE" in res.text
    assert "1500 crore" in res.text
    assert res.method == "pdfplumber"
    assert res.pages_extracted == 1
    assert not ext.is_empty(res)


def test_blank_pdf_marks_empty(tmp_path: Path, fitz_module) -> None:
    pdf = tmp_path / "blank.pdf"
    _make_blank_pdf(pdf, fitz_module, pages=2)
    ext = PdfPlumberExtractor()
    res = ext.extract(pdf, max_pages=20)
    assert res.text == ""
    assert res.error == "empty_text"
    assert ext.is_empty(res)
    assert res.pages_extracted == 2


def test_max_pages_cap_returns_total_page_count(tmp_path: Path, fitz_module) -> None:
    pdf = tmp_path / "long.pdf"
    _make_text_pdf(pdf, fitz_module, lines=["line"], pages=25)
    ext = PdfPlumberExtractor()
    res = ext.extract(pdf, max_pages=5)
    # pages_extracted is the FILE total, not the read count -- the orchestrator
    # uses this to fire the >20-pages deferred-tag rule.
    assert res.pages_extracted == 25


def test_missing_file_returns_error(tmp_path: Path) -> None:
    res = PdfPlumberExtractor().extract(tmp_path / "nope.pdf", max_pages=20)
    assert res.error is not None
    assert "pdf_missing" in res.error


def test_corrupt_pdf_returns_error(tmp_path: Path) -> None:
    pdf = tmp_path / "corrupt.pdf"
    pdf.write_bytes(b"not a real pdf at all")
    res = PdfPlumberExtractor().extract(pdf, max_pages=20)
    assert res.error is not None
    assert "pdfplumber_failed" in res.error


def test_is_empty_threshold_configurable(tmp_path: Path, fitz_module) -> None:
    pdf = tmp_path / "tiny.pdf"
    _make_text_pdf(pdf, fitz_module, lines=["abc"])
    ext_strict = PdfPlumberExtractor(empty_threshold=100)
    res = ext_strict.extract(pdf, max_pages=20)
    # only 3 chars extracted -- empty under strict threshold
    assert ext_strict.is_empty(res)


def test_default_empty_threshold_constant_exposed() -> None:
    assert EMPTY_TEXT_THRESHOLD > 0
