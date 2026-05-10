"""Attachment download + extraction contracts."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class DownloadResult:
    notification_id: int
    local_path: Optional[Path]
    bytes_downloaded: int
    content_type: Optional[str]
    md5: Optional[str]
    skipped_reason: Optional[str]  # 'already_downloaded' | 'too_old' | 'no_url'
    error: Optional[str]


@dataclass(frozen=True)
class ExtractionResult:
    text: str
    pages_extracted: int
    method: str  # 'pdfplumber' | 'pymupdf' | 'gemma_vision' | 'unstructured'
    image_summary: str  # populated by vision extractor
    deferred_doc_type: Optional[str]
    error: Optional[str]


@dataclass(frozen=True)
class ExternalLinkSummary:
    url: str
    target_kind: str  # 'pdf' | 'html' | 'unknown' | 'other_doc'
    summary: str
    error: Optional[str]


class AttachmentDownloader(ABC):
    @abstractmethod
    def download(self, notification_id: int) -> DownloadResult: ...


class PdfTextExtractor(ABC):
    @abstractmethod
    def extract(self, pdf_path: Path, max_pages: int) -> ExtractionResult: ...


class PdfImageExtractor(ABC):
    """Vision-LLM-based extractor for scanned/image-only PDFs."""

    @abstractmethod
    def extract_with_vision(
        self, pdf_path: Path, max_pages: int
    ) -> ExtractionResult: ...


class LinkResolver(ABC):
    """Find URLs inside extracted text; optionally fetch + summarize."""

    @abstractmethod
    def resolve(self, pdf_text: str) -> list[ExternalLinkSummary]: ...
