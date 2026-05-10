"""Summarizer contract — extended JSON schema per design-decisions.md H2."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class KeyFigure:
    label: str
    value: str  # keep as string so units/notation are preserved
    unit: str  # 'INR Cr' | '%' | 'units' | 'tonnes/day' | 'MW' | etc.


@dataclass(frozen=True)
class KeyPerson:
    name: str
    role: str  # 'CEO' | 'CFO' | 'allottee' | 'director' | etc.


@dataclass(frozen=True)
class KeyDate:
    label: str  # 'commissioning' | 'record_date' | 'AGM' | etc.
    iso_date: str  # ISO 8601
    certainty: str  # 'announced' | 'expected' | 'tentative'


@dataclass(frozen=True)
class ExternalLink:
    url: str
    referenced_as: str  # how the document refers to it
    target_summary: str  # if we followed the link


@dataclass(frozen=True)
class SummaryResult:
    summary: str
    impact: str
    key_figures: list[KeyFigure] = field(default_factory=list)
    key_people: list[KeyPerson] = field(default_factory=list)
    key_dates: list[KeyDate] = field(default_factory=list)
    attachments_referenced: list[str] = field(default_factory=list)
    deferred_doc_tags: list[str] = field(default_factory=list)
    # 'earnings' | 'ppt' | 'annual_report' | 'credit_note' | 'large_misc'
    external_links: list[ExternalLink] = field(default_factory=list)
    confidence: float = 0.0
    used_model: str = ""
    used_prompt_version: str = ""


class Summarizer(ABC):
    """Public contract — any summarizer must produce a SummaryResult."""

    @abstractmethod
    def summarize(self, notification_id: int) -> SummaryResult: ...
