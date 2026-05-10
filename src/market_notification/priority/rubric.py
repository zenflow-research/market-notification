"""Category → default-priority rubric (data only, no behavior).

Faithfully ported from `G:/brain/exchange_util/notification_priority.py`'s
``CATEGORY_PRIORITY`` dict. The deterministic engine consumes this as the
*starting hint*; per-category adjusters (in `deterministic.py`) move the
score up or down from the base.

Coverage check
--------------
Every key in ``CATEGORY_PRIORITY`` MUST be a member of
``classifier.taxonomy.VALID_CATEGORIES`` — otherwise the rubric and the
classifier disagree on what categories exist. The ``assert_rubric_complete``
helper enforces this and is run at import time so a missed entry surfaces
immediately rather than during a live notification.
"""
from __future__ import annotations

from ..classifier.taxonomy import UNCATEGORIZED, VALID_CATEGORIES

# Bucket → numeric base score. Tuned so threshold adjusters move buckets
# without overflowing 0..100.
BASE_SCORE = {
    "important": 80,
    "medium": 50,
    "normal": 30,
    "ignored": 0,
}


# Default priority bucket per category (brain port).
CATEGORY_PRIORITY: dict[str, str] = {
    # Growth & Expansion
    "Capacity Expansion": "medium",
    "Capex Update": "medium",
    "New Plant / Facility": "normal",
    "New Ventures": "normal",
    "New Product Launch": "normal",
    # Deals & Partnerships
    "Acquisition": "medium",
    "Joint Venture": "medium",
    "Collaboration / MOU": "normal",
    "Order Win": "medium",
    "Contract Award": "medium",
    "Divestiture / Sale": "medium",
    # Corporate Restructuring
    "Merger": "medium",
    "Demerger / Spinoff": "important",
    "Open Offer": "important",
    "Takeover": "important",
    "Name Change": "normal",
    "Scheme of Arrangement": "normal",
    # Capital Actions
    "Stock Split": "normal",
    "Bonus Issue": "normal",
    "Rights Issue": "normal",
    "OFS (Offer for Sale)": "medium",
    "Buyback": "important",
    "Dividend": "normal",
    "ESOP": "normal",
    # Fund Raising
    "Equity Dilution (QIP/FPO/Preferential)": "important",
    "Debt Raise (NCD/Bond/ECB)": "normal",
    "IPO / Listing": "important",
    "Warrant Conversion": "normal",
    # Regulatory & Compliance
    "USFDA (Approval/Warning/Import Alert)": "important",
    "Regulatory Approval": "medium",
    "SEBI Order": "normal",
    "Legal / Litigation": "normal",
    "Tax / GST Order": "normal",
    "Environmental Clearance": "medium",
    "Cyber Incident": "medium",
    "NCD Compliance": "normal",
    # Management & Governance
    "Management Change (CEO/CFO/Director)": "medium",
    "Board Meeting Outcome": "normal",
    "AGM / EGM": "normal",
    "Related Party Transaction": "normal",
    "Auditor Change / Qualification": "normal",
    "Corporate Guarantee": "medium",
    # Investor Communication
    "First Presentation": "medium",
    "First Con Call": "medium",
    "Investor Presentation": "normal",
    "Credit Rating Change": "normal",
    "Guidance Update": "important",
    # Financial Results
    "Quarterly Results": "important",
    "Annual Results": "important",
    "Revenue/Profit Update": "important",
    "Earnings Surprise": "important",
    # Other
    "Other Important": "medium",
    "Compliance Filing": "normal",
    UNCATEGORIZED: "normal",  # safety net
}


def default_for(ai_category: str) -> tuple[str, int]:
    """Return ``(bucket, base_score)`` for a category. Falls back to normal."""
    bucket = CATEGORY_PRIORITY.get(ai_category, "normal")
    return bucket, BASE_SCORE.get(bucket, BASE_SCORE["normal"])


def bucket_for_score(score: int) -> str:
    """Map a raw score to a bucket. Mirrors brain's thresholds exactly.

    - score <= 0 -> ignored
    - 0 <  score < 40 -> normal
    - 40 <= score < 70 -> medium
    - 70 <= score      -> important
    """
    if score <= 0:
        return "ignored"
    if score >= 70:
        return "important"
    if score >= 40:
        return "medium"
    return "normal"


def assert_rubric_complete() -> None:
    """Crash early if a taxonomy category is missing from the rubric.

    Compliance Filing / Other Important / Uncategorized are explicitly added
    to the rubric so this check is total. New categories added to the
    taxonomy MUST also be added here in the same commit.
    """
    missing = sorted(VALID_CATEGORIES - set(CATEGORY_PRIORITY.keys()))
    if missing:
        raise RuntimeError(
            "priority/rubric.py is missing entries for taxonomy categories: "
            + ", ".join(missing)
        )


# Run at import to catch drift loudly.
assert_rubric_complete()


__all__ = [
    "CATEGORY_PRIORITY",
    "BASE_SCORE",
    "default_for",
    "bucket_for_score",
    "assert_rubric_complete",
]
