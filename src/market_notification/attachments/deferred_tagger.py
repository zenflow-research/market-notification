"""Heuristic deferred-doc-type tagger (FR-ATTACH-004).

Some PDFs are out-of-scope for this pipeline's summarizer:
  - annual_report
  - investor_presentation
  - earnings  (quarterly results PDFs that need a dedicated extractor)
  - ppt       (concall presentations)
  - credit_note (rating-agency reports)
  - large_misc (>20 pages with no specific match)

These get tagged so the summarizer can skip the body (per FR-ATTACH-004 +
design-decisions D-13). Headline + metadata still flow downstream.

The tagger looks at, in priority order:
  1. exchange-supplied category/subcategory (BSE classifies announcements)
  2. classifier-assigned ``ai_category``
  3. headline + body keywords
  4. attachment filename keywords
  5. PDF text first ~2KB keywords
  6. page count (>20 -> ``large_misc`` if nothing else matched)

We deliberately favor false negatives over false positives: a missed tag
just means the summarizer does extra work; a wrong tag silently drops
information the user wanted.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeferredTaggerInput:
    """Subset of a notification row needed by the tagger."""

    headline: str = ""
    body: str = ""
    category: str = ""        # exchange-supplied
    subcategory: str = ""     # exchange-supplied
    ai_category: str = ""     # classifier output
    attachment_name: str = ""
    pdf_text_head: str = ""   # first ~2KB of extracted text
    pdf_pages: int = 0


# ---------------------------------------------------------------------------
# Keyword patterns
# ---------------------------------------------------------------------------
# Each tuple: (tag, regex). Order matters -- earlier matches win.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "annual_report",
        re.compile(
            r"\b(annual\s+report|integrated\s+annual\s+report|"
            r"\bAR\b\s*\d{2,4}|annual\s+\&?\s*sustainability\s+report)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "investor_presentation",
        re.compile(
            r"\b(investor\s+presentation|investor\s+update|"
            r"earnings\s+presentation|"
            r"investor\s+meet|"
            r"results?\s+presentation|"
            r"corporate\s+presentation)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ppt",
        re.compile(
            # No \b around `concall` because it commonly appears inside
            # filenames like ``Q1FY26_concall_transcript.pdf`` where the
            # underscores prevent regex word boundaries from firing.
            r"(concall|"
            r"earnings\s+call|"
            r"transcript\s+of\s+(?:concall|earnings\s+call)|"
            r"audio\s+recording|"
            r"\.pptx?(?:[\s/]|$))",
            re.IGNORECASE,
        ),
    ),
    (
        "credit_note",
        re.compile(
            r"\b(credit\s+rating(?:s)?|"
            r"rating\s+rationale|"
            r"crisil|icra|care\s+ratings|india\s+ratings|brickwork|acuit[ée]"
            r")\b",
            re.IGNORECASE,
        ),
    ),
    (
        "earnings",
        re.compile(
            r"\b(financial\s+results|"
            r"unaudited\s+(?:standalone|consolidated)\s+financial|"
            r"quarterly\s+results|"
            r"results\s+for\s+the\s+(?:quarter|year)\s+ended|"
            r"\bQ[1-4]FY\d{2}\b)\b",
            re.IGNORECASE,
        ),
    ),
)

# Categories from our taxonomy (Phase 5) that hard-route to a deferred tag.
_AI_CATEGORY_TAG_MAP: dict[str, str] = {
    "Annual Report": "annual_report",
    "Investor Presentation": "investor_presentation",
    "Investor Meet / Conference Call": "ppt",
    "Credit Rating Change": "credit_note",
    "Quarterly Results": "earnings",
}

# BSE/NSE-supplied category strings that route to a deferred tag.
_EXCHANGE_CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("annual_report", re.compile(r"annual\s+report", re.IGNORECASE)),
    (
        "investor_presentation",
        re.compile(r"(investor\s+presentation|presentation)", re.IGNORECASE),
    ),
    ("earnings", re.compile(r"financial\s+results?", re.IGNORECASE)),
    ("credit_note", re.compile(r"credit\s+rating", re.IGNORECASE)),
)

LARGE_DOC_PAGE_THRESHOLD = 20


class DeferredDocTagger:
    """Stateless tagger; can also be called as a function via ``tag()``."""

    def tag(self, inp: DeferredTaggerInput) -> Optional[str]:
        # 1. ai_category direct map (highest precedence: classifier verdict
        # has already disambiguated headlines like "Outcome of Board Meeting -
        # Audited Financial Results" that pure regex would mis-route).
        ai_tag = _AI_CATEGORY_TAG_MAP.get((inp.ai_category or "").strip())
        if ai_tag:
            return ai_tag

        # 2. exchange-supplied category/subcategory
        for haystack in (inp.category, inp.subcategory):
            for tag, pat in _EXCHANGE_CATEGORY_PATTERNS:
                if haystack and pat.search(haystack):
                    return tag

        # 3-5. concatenated keyword search across headline/body/filename/pdf-head
        haystack = " | ".join(
            s for s in (
                inp.headline,
                inp.body[:1024] if inp.body else "",
                inp.attachment_name,
                inp.pdf_text_head[:2048] if inp.pdf_text_head else "",
            ) if s
        )
        for tag, pat in _PATTERNS:
            if pat.search(haystack):
                return tag

        # 6. page-count fallback for unclassified large PDFs
        if inp.pdf_pages and inp.pdf_pages > LARGE_DOC_PAGE_THRESHOLD:
            return "large_misc"

        return None


__all__ = [
    "DeferredDocTagger",
    "DeferredTaggerInput",
    "LARGE_DOC_PAGE_THRESHOLD",
]
