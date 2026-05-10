"""Unit tests: priority/thresholds.py — pure helpers, regex + math."""
from __future__ import annotations

import pytest

from market_notification.priority import thresholds as t


# ---------------------------------------------------------------------------
# Amount extraction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text, expected",
    [
        ("Order win of Rs 250 Cr", 250.0),
        ("INR 1,250.50 crore acquisition", 1250.50),
        ("approx ₹3,500 cr capex", 3500.0),
        ("RS. 47.25 Cr issue", 47.25),
        ("no amount here", None),
        ("", None),
        ("Rs 10 lakh order", None),  # only 'crore' / 'cr' counts
    ],
)
def test_extract_amount_cr(text: str, expected: float | None) -> None:
    assert t.extract_amount_cr(text) == expected


# ---------------------------------------------------------------------------
# pct_of
# ---------------------------------------------------------------------------
def test_pct_of_basic() -> None:
    assert t.pct_of(50, 1000) == 5.0
    assert t.pct_of(0, 1000) == 0.0
    assert t.pct_of(None, 1000) is None
    assert t.pct_of(50, None) is None
    assert t.pct_of(50, 0) is None


# ---------------------------------------------------------------------------
# Stage / language matchers — sample the headlines that drive special rules
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "fn, true_text, false_text",
    [
        (t.is_commissioned, "Commissioning of new MTPA plant", "Plans to expand"),
        (t.is_board_approved, "Board has approved the proposal", "Newspaper publication"),
        (t.is_proposed_or_exploring, "Plans to acquire", "Acquired the asset"),
        (t.has_significant_keyword, "Major order win", "Order received"),
        (t.is_special_dividend, "Special interim dividend", "Final dividend"),
        (t.has_declared_amount, "Declared dividend of Rs 5", "Declared dividend"),
        (t.is_newspaper_publication, "Copy of newspaper publication", "Investor presentation"),
        (
            t.is_board_notice_for_results,
            "Board meeting intimation to be held to consider results",
            "Outcome of board meeting",
        ),
        (t.is_clarification, "Clarification on price movement", "Press release"),
        (
            t.is_internal_subsidiary_transfer,
            "Transfer of 100% wholly-owned subsidiary",
            "Demerger of consumer division",
        ),
        (t.is_usfda_vai, "USFDA VAI letter", "USFDA OAI letter"),
        (t.is_usfda_oai_or_warning, "Import alert from USFDA", "USFDA approval"),
        (t.is_sebi_ban, "SEBI debarment order", "SEBI penalty"),
        (
            t.is_auditor_qualification,
            "Auditor qualified opinion / disclaimer",
            "Auditor change",
        ),
        (t.is_rating_upgrade, "CRISIL upgrades rating", "CRISIL revises rating"),
        (t.is_rating_downgrade, "CRISIL downgrades rating", "CRISIL reaffirms rating"),
        (
            t.is_wos_merger,
            "Merger of wholly-owned subsidiary into the company",
            "Merger with peer company",
        ),
        (t.is_buyback_procedural, "Post-buyback public announcement", "Buyback approved"),
        (t.is_promoter_action, "Promoter warrant conversion", "Investor warrant"),
        (t.mentions_ceo_cfo_md, "Appointment of CFO", "Appointment of company secretary"),
    ],
)
def test_text_matchers(fn, true_text: str, false_text: str) -> None:
    assert fn(true_text) is True
    assert fn(false_text) is False
    assert fn("") is False


def test_qip_procedural_requires_no_board_language() -> None:
    # Allotment without board language -> procedural
    assert t.is_qip_procedural("Allotment of equity shares") is True
    # Allotment WITH board language -> NOT procedural (genuine event)
    assert t.is_qip_procedural("Board approved allotment of equity shares") is False
    # Listing approval alone -> procedural
    assert t.is_qip_procedural("Listing approval received") is True
