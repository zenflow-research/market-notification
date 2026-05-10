"""Notification taxonomy — single source of truth for ai_category / ai_category_group.

Faithfully ported from `G:/brain/exchange_util/notification_classifier.py` (TAXONOMY const).
The brain version has been load-tested over ~80k historical notifications, so we treat
its category list as the contract; the prompt builder, the validator, and the migration
hooks all import from here.

Counts (post-port):
    10 groups
    53 leaf categories
    + 1 sentinel "Uncategorized" (returned only when LLM output cannot be coerced)

Versioning
----------
Bump `TAXONOMY_VERSION` whenever a category is added/renamed/removed. Each Notification
row records the version that classified it (`ai_classified_taxonomy_version`) so future
re-classifications can be filtered on version mismatch.
"""
from __future__ import annotations

from typing import Any

# Version tag: change this whenever TAXONOMY changes shape.
TAXONOMY_VERSION = "v1.0-2026-05-07"

# Sentinel used when the LLM output is invalid OR genuinely unknown.
# Maps onto the "Other" group so downstream priority logic still has a bucket.
UNCATEGORIZED = "Uncategorized"
UNCATEGORIZED_GROUP = "Other"


# 10 groups, 53 categories. Order matters for prompt enumeration only — not behavior.
TAXONOMY: list[dict[str, Any]] = [
    {
        "group": "Growth & Expansion",
        "color": "#4ecdc4",
        "categories": [
            "Capacity Expansion",
            "Capex Update",
            "New Plant / Facility",
            "New Ventures",
            "New Product Launch",
        ],
    },
    {
        "group": "Deals & Partnerships",
        "color": "#ff6b6b",
        "categories": [
            "Acquisition",
            "Joint Venture",
            "Collaboration / MOU",
            "Order Win",
            "Contract Award",
            "Divestiture / Sale",
        ],
    },
    {
        "group": "Corporate Restructuring",
        "color": "#ffd93d",
        "categories": [
            "Merger",
            "Demerger / Spinoff",
            "Open Offer",
            "Takeover",
            "Name Change",
            "Scheme of Arrangement",
        ],
    },
    {
        "group": "Capital Actions",
        "color": "#a78bfa",
        "categories": [
            "Stock Split",
            "Bonus Issue",
            "Rights Issue",
            "OFS (Offer for Sale)",
            "Buyback",
            "Dividend",
            "ESOP",
        ],
    },
    {
        "group": "Fund Raising",
        "color": "#f472b6",
        "categories": [
            "Equity Dilution (QIP/FPO/Preferential)",
            "Debt Raise (NCD/Bond/ECB)",
            "IPO / Listing",
            "Warrant Conversion",
        ],
    },
    {
        "group": "Regulatory & Compliance",
        "color": "#fb923c",
        "categories": [
            "USFDA (Approval/Warning/Import Alert)",
            "Regulatory Approval",
            "SEBI Order",
            "Legal / Litigation",
            "Tax / GST Order",
            "Environmental Clearance",
            "Cyber Incident",
            "NCD Compliance",
        ],
    },
    {
        "group": "Management & Governance",
        "color": "#67e8f9",
        "categories": [
            "Management Change (CEO/CFO/Director)",
            "Board Meeting Outcome",
            "AGM / EGM",
            "Related Party Transaction",
            "Auditor Change / Qualification",
            "Corporate Guarantee",
        ],
    },
    {
        "group": "Investor Communication",
        "color": "#86efac",
        "categories": [
            "First Presentation",
            "First Con Call",
            "Investor Presentation",
            "Credit Rating Change",
            "Guidance Update",
        ],
    },
    {
        "group": "Financial Results",
        "color": "#e879f9",
        "categories": [
            "Quarterly Results",
            "Annual Results",
            "Revenue/Profit Update",
            "Earnings Surprise",
        ],
    },
    {
        "group": "Other",
        "color": "#94a3b8",
        "categories": [
            "Other Important",
            "Compliance Filing",
        ],
    },
]


# ---------------------------------------------------------------------------
# Derived lookups (built once at import)
# ---------------------------------------------------------------------------
def _build_indexes() -> tuple[dict[str, str], dict[str, str], frozenset[str], frozenset[str]]:
    cat_to_group: dict[str, str] = {}
    group_colors: dict[str, str] = {}
    valid_categories: set[str] = set()
    valid_groups: set[str] = set()
    for g in TAXONOMY:
        group_colors[g["group"]] = g["color"]
        valid_groups.add(g["group"])
        for cat in g["categories"]:
            cat_to_group[cat] = g["group"]
            valid_categories.add(cat)
    return cat_to_group, group_colors, frozenset(valid_categories), frozenset(valid_groups)


CATEGORY_TO_GROUP, GROUP_COLORS, VALID_CATEGORIES, VALID_GROUPS = _build_indexes()


def validate_category(category: str | None, group: str | None = None) -> tuple[str, str]:  # noqa: ARG001
    """Coerce LLM output to a known (category, group) pair.

    Returns the canonical group for the given category — even if the LLM
    supplied a mismatching group. If the category isn't in the taxonomy,
    falls back to (Uncategorized, Other). The `group` argument is accepted
    for caller symmetry but ignored; we always use the canonical group lookup.
    """
    if not category:
        return UNCATEGORIZED, UNCATEGORIZED_GROUP
    cat = category.strip()
    if cat in VALID_CATEGORIES:
        return cat, CATEGORY_TO_GROUP[cat]
    # Brain shipped a few legacy aliases; we accept them for backward-compat.
    legacy = _LEGACY_ALIASES.get(cat)
    if legacy is not None:
        return legacy, CATEGORY_TO_GROUP[legacy]
    return UNCATEGORIZED, UNCATEGORIZED_GROUP


# Tolerated legacy or near-miss spellings. Keep small; new aliases should
# graduate to canonical entries.
_LEGACY_ALIASES: dict[str, str] = {
    "USFDA": "USFDA (Approval/Warning/Import Alert)",
    "FDA Approval": "USFDA (Approval/Warning/Import Alert)",
    "Buy Back": "Buyback",
    "Stock Split / Sub-division": "Stock Split",
    "Annual Result": "Annual Results",
    "Quarterly Result": "Quarterly Results",
    "Order Wins": "Order Win",
    "Demerger": "Demerger / Spinoff",
    "QIP": "Equity Dilution (QIP/FPO/Preferential)",
    "FPO": "Equity Dilution (QIP/FPO/Preferential)",
    "Preferential": "Equity Dilution (QIP/FPO/Preferential)",
    "NCD": "Debt Raise (NCD/Bond/ECB)",
}


def taxonomy_as_text(skip_groups: frozenset[str] | None = None) -> str:
    """Render the taxonomy as the prompt-facing block.

    Each group is a header; categories indented two spaces. Skipping the "Other"
    group (default behavior in brain's prompt) helps steer the model away from
    over-using Compliance Filing as a fallback bucket.
    """
    skip = skip_groups or frozenset()
    lines: list[str] = []
    for g in TAXONOMY:
        if g["group"] in skip:
            continue
        lines.append(f"Group: {g['group']}")
        for c in g["categories"]:
            lines.append(f"  - {c}")
    return "\n".join(lines)


__all__ = [
    "TAXONOMY",
    "TAXONOMY_VERSION",
    "CATEGORY_TO_GROUP",
    "GROUP_COLORS",
    "VALID_CATEGORIES",
    "VALID_GROUPS",
    "UNCATEGORIZED",
    "UNCATEGORIZED_GROUP",
    "validate_category",
    "taxonomy_as_text",
]
