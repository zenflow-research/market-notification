"""Pure threshold + regex helpers used by the deterministic priority engine.

No DB access, no logging side-effects. Faithfully ports brain's regex set
so historical decisions are reproducible.
"""
from __future__ import annotations

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Amount extraction
# ---------------------------------------------------------------------------
_AMOUNT_RE = re.compile(
    r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d+)?)\s*(?:crore|cr\b|Cr\b)",
    re.IGNORECASE,
)


def extract_amount_cr(text: str) -> Optional[float]:
    """Pull the first ``Rs X Cr`` amount from a headline/body. None if absent.

    Brain's pattern is preserved verbatim (with the ``₹`` rupee symbol
    added) so historical scoring stays bit-for-bit reproducible.
    """
    if not text:
        return None
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Stage / language matchers
# ---------------------------------------------------------------------------
_COMMISSIONED_RE = re.compile(
    r"\bcommenc(?:e[sd]?|ement|ing)\b|\bcommission(?:ed|ing)?\b|\boperational\b",
    re.IGNORECASE,
)
_BOARD_APPROVED_RE = re.compile(
    r"\bboard\s+(?:has\s+)?approv\w+\b|\bapproved\s+by\s+(?:the\s+)?board\b",
    re.IGNORECASE,
)
_PROPOSED_RE = re.compile(
    r"\bproposed\b|\bexploring\b|\bplans?\s+to\b", re.IGNORECASE,
)
_SIGNIFICANT_KEYWORDS_RE = re.compile(
    r"\b(?:significant|material|major|largest)\b", re.IGNORECASE,
)
_SPECIAL_DIVIDEND_RE = re.compile(r"\bspecial\b|\bone[\s-]*time\b", re.IGNORECASE)
_DECLARED_AMOUNT_RE = re.compile(r"\bdeclar\w+\b.*\bRs\.?\s*\d", re.IGNORECASE)
_NEWSPAPER_RE = re.compile(
    r"\bnewspaper\b|\badvertis\w+\b|\bpublicat\w+\b", re.IGNORECASE,
)
_RESULTS_BOARD_NOTICE_RE = re.compile(
    r"\bBoard\s+Meeting\s+Intimation\b|"
    r"\bscheduled\b.*\bconsider\b|"
    r"\bto\s+be\s+held\b.*\bconsider\b",
    re.IGNORECASE,
)
_CLARIFICATION_RE = re.compile(
    r"\bClarification\b|\bReply\s+to\s+Clarification\b", re.IGNORECASE,
)
_INTERNAL_TRANSFER_RE = re.compile(
    r"\btransfer\w*\s+(?:of\s+)?(?:its\s+)?(?:100%|wholly|subsidiary)",
    re.IGNORECASE,
)
_USFDA_VAI_RE = re.compile(r"\bVAI\b|\bVoluntary\s+Action\b", re.IGNORECASE)
_USFDA_OAI_RE = re.compile(
    r"\bOAI\b|\bOfficial\s+Action\b|\bimport\s+alert\b|\bwarning\b",
    re.IGNORECASE,
)
_SEBI_BAN_RE = re.compile(r"\bban\b|\bdebar\w+\b|\bprohibit\w+\b", re.IGNORECASE)
_AUDITOR_QUALIFIED_RE = re.compile(
    r"\bqualifi(?:ed|cation)\b|\bdisclaimer\b", re.IGNORECASE,
)
_RATING_UPGRADE_RE = re.compile(r"\bupgrad\w+\b|\benhance\w*\b", re.IGNORECASE)
_RATING_DOWNGRADE_RE = re.compile(
    r"\bdowngrad\w+\b|\brevise.*\bnegativ\w*\b", re.IGNORECASE,
)
_WOS_MERGER_RE = re.compile(
    r"\bwholly[\s-]*owned\b|\bWOS\b|\bsubsidiary\s+into\s+(?:the\s+)?company\b",
    re.IGNORECASE,
)
_BUYBACK_PROCEDURAL_RE = re.compile(
    r"\bclosure\b|\bpost[\s-]*buyback\b|\bpost[\s-]*offer\b|\bextinguish\w*\b",
    re.IGNORECASE,
)
_QIP_PROCEDURAL_RE = re.compile(
    r"\ballot(?:ment|ted)\b|\blisting\s+(?:of|approval)\b", re.IGNORECASE,
)
_QIP_BOARD_LANGUAGE_RE = re.compile(
    r"\bboard\s+approv\w+\b|\bconsider\b", re.IGNORECASE,
)
_PROMOTER_RE = re.compile(r"\bpromoter\b", re.IGNORECASE)
_CEO_CFO_RE = re.compile(r"\b(?:CEO|CFO|MD|Managing\s+Director)\b", re.IGNORECASE)


def is_commissioned(text: str) -> bool:
    return bool(text and _COMMISSIONED_RE.search(text))


def is_board_approved(text: str) -> bool:
    return bool(text and _BOARD_APPROVED_RE.search(text))


def is_proposed_or_exploring(text: str) -> bool:
    return bool(text and _PROPOSED_RE.search(text))


def has_significant_keyword(text: str) -> bool:
    return bool(text and _SIGNIFICANT_KEYWORDS_RE.search(text))


def is_special_dividend(text: str) -> bool:
    return bool(text and _SPECIAL_DIVIDEND_RE.search(text))


def has_declared_amount(text: str) -> bool:
    return bool(text and _DECLARED_AMOUNT_RE.search(text))


def is_newspaper_publication(text: str) -> bool:
    return bool(text and _NEWSPAPER_RE.search(text))


def is_board_notice_for_results(text: str) -> bool:
    return bool(text and _RESULTS_BOARD_NOTICE_RE.search(text))


def is_clarification(text: str) -> bool:
    return bool(text and _CLARIFICATION_RE.search(text))


def is_internal_subsidiary_transfer(text: str) -> bool:
    return bool(text and _INTERNAL_TRANSFER_RE.search(text))


def is_usfda_vai(text: str) -> bool:
    return bool(text and _USFDA_VAI_RE.search(text))


def is_usfda_oai_or_warning(text: str) -> bool:
    return bool(text and _USFDA_OAI_RE.search(text))


def is_sebi_ban(text: str) -> bool:
    return bool(text and _SEBI_BAN_RE.search(text))


def is_auditor_qualification(text: str) -> bool:
    return bool(text and _AUDITOR_QUALIFIED_RE.search(text))


def is_rating_upgrade(text: str) -> bool:
    return bool(text and _RATING_UPGRADE_RE.search(text))


def is_rating_downgrade(text: str) -> bool:
    return bool(text and _RATING_DOWNGRADE_RE.search(text))


def is_wos_merger(text: str) -> bool:
    return bool(text and _WOS_MERGER_RE.search(text))


def is_buyback_procedural(text: str) -> bool:
    return bool(text and _BUYBACK_PROCEDURAL_RE.search(text))


def is_qip_procedural(text: str) -> bool:
    if not text:
        return False
    if not _QIP_PROCEDURAL_RE.search(text):
        return False
    # Brain's logic: "procedural" means the keyword fires AND the headline
    # does NOT also use board-approval language (which signals genuine event)
    return not _QIP_BOARD_LANGUAGE_RE.search(text)


def is_promoter_action(text: str) -> bool:
    return bool(text and _PROMOTER_RE.search(text))


def mentions_ceo_cfo_md(text: str) -> bool:
    return bool(text and _CEO_CFO_RE.search(text))


# ---------------------------------------------------------------------------
# Arithmetic helpers
# ---------------------------------------------------------------------------
def pct_of(amount: Optional[float], denominator: Optional[float]) -> Optional[float]:
    """``(amount / denominator) * 100`` with None-safe handling.

    Returns None if either operand is missing or the denominator is zero.
    """
    if amount is None or denominator is None:
        return None
    if denominator == 0:
        return None
    return (amount / denominator) * 100


__all__ = [
    "extract_amount_cr",
    "pct_of",
    "is_commissioned",
    "is_board_approved",
    "is_proposed_or_exploring",
    "has_significant_keyword",
    "is_special_dividend",
    "has_declared_amount",
    "is_newspaper_publication",
    "is_board_notice_for_results",
    "is_clarification",
    "is_internal_subsidiary_transfer",
    "is_usfda_vai",
    "is_usfda_oai_or_warning",
    "is_sebi_ban",
    "is_auditor_qualification",
    "is_rating_upgrade",
    "is_rating_downgrade",
    "is_wos_merger",
    "is_buyback_procedural",
    "is_qip_procedural",
    "is_promoter_action",
    "mentions_ceo_cfo_md",
]
