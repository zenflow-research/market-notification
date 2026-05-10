"""Unit tests for DeferredDocTagger.

Asserts the precedence chain: ai_category > exchange category > headline/body
keyword > filename keyword > pdf-text-head keyword > page-count fallback.
"""
from __future__ import annotations

import pytest

from market_notification.attachments.deferred_tagger import (
    LARGE_DOC_PAGE_THRESHOLD,
    DeferredDocTagger,
    DeferredTaggerInput,
)


@pytest.fixture()
def tagger() -> DeferredDocTagger:
    return DeferredDocTagger()


@pytest.mark.parametrize(
    ("ai_cat", "expected"),
    [
        ("Annual Report", "annual_report"),
        ("Investor Presentation", "investor_presentation"),
        ("Investor Meet / Conference Call", "ppt"),
        ("Credit Rating Change", "credit_note"),
        ("Quarterly Results", "earnings"),
    ],
)
def test_ai_category_direct_map(tagger, ai_cat: str, expected: str) -> None:
    out = tagger.tag(DeferredTaggerInput(ai_category=ai_cat))
    assert out == expected


def test_ai_category_wins_over_keywords(tagger) -> None:
    """When ai_category disagrees with the headline keyword, ai_category wins."""
    out = tagger.tag(
        DeferredTaggerInput(
            ai_category="Acquisition",  # not in deferred map
            headline="Annual report of FY26 published herewith",
        )
    )
    # Acquisition isn't in deferred map -> falls through to keyword search ->
    # the headline keyword fires next.
    assert out == "annual_report"


def test_ai_category_acquisition_with_no_keywords_returns_none(tagger) -> None:
    out = tagger.tag(DeferredTaggerInput(ai_category="Acquisition"))
    assert out is None


def test_exchange_category_routes_quarterly(tagger) -> None:
    out = tagger.tag(DeferredTaggerInput(category="Financial Results"))
    assert out == "earnings"


def test_headline_keyword_investor_presentation(tagger) -> None:
    out = tagger.tag(
        DeferredTaggerInput(
            headline="Submission of Investor Presentation Q3FY26"
        )
    )
    assert out == "investor_presentation"


def test_filename_keyword_concall(tagger) -> None:
    out = tagger.tag(
        DeferredTaggerInput(
            attachment_name="Q1FY26_concall_transcript.pdf"
        )
    )
    assert out == "ppt"


def test_credit_rating_agency_in_pdf_head(tagger) -> None:
    head = "CRISIL has reaffirmed the long-term rating at AA+ stable."
    out = tagger.tag(DeferredTaggerInput(pdf_text_head=head))
    assert out == "credit_note"


def test_large_misc_fallback_for_long_unknown_pdf(tagger) -> None:
    out = tagger.tag(
        DeferredTaggerInput(
            headline="Disclosure under SEBI LODR",
            pdf_pages=LARGE_DOC_PAGE_THRESHOLD + 1,
        )
    )
    assert out == "large_misc"


def test_short_pdf_with_no_keywords_is_not_deferred(tagger) -> None:
    out = tagger.tag(
        DeferredTaggerInput(
            headline="Disclosure of related-party transaction details",
            pdf_pages=2,
        )
    )
    assert out is None


def test_threshold_boundary(tagger) -> None:
    # exactly at the threshold -> NOT deferred (strict >)
    out = tagger.tag(
        DeferredTaggerInput(headline="X", pdf_pages=LARGE_DOC_PAGE_THRESHOLD)
    )
    assert out is None
    out = tagger.tag(
        DeferredTaggerInput(headline="X", pdf_pages=LARGE_DOC_PAGE_THRESHOLD + 1)
    )
    assert out == "large_misc"


def test_subcategory_routes_credit_rating(tagger) -> None:
    out = tagger.tag(
        DeferredTaggerInput(subcategory="Credit Rating Notification")
    )
    assert out == "credit_note"


def test_ar_acronym_pattern(tagger) -> None:
    out = tagger.tag(
        DeferredTaggerInput(headline="Submitting AR 2025-26 to shareholders")
    )
    assert out == "annual_report"


def test_q1fy_pattern_routes_earnings(tagger) -> None:
    out = tagger.tag(
        DeferredTaggerInput(headline="Q3FY26 results -- standalone unaudited")
    )
    assert out == "earnings"
