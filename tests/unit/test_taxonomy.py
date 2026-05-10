"""Unit tests: classifier/taxonomy.py — the lookup contract."""
from __future__ import annotations

from market_notification.classifier.taxonomy import (
    CATEGORY_TO_GROUP,
    GROUP_COLORS,
    TAXONOMY,
    TAXONOMY_VERSION,
    UNCATEGORIZED,
    UNCATEGORIZED_GROUP,
    VALID_CATEGORIES,
    VALID_GROUPS,
    taxonomy_as_text,
    validate_category,
)


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------
def test_ten_groups() -> None:
    assert len(TAXONOMY) == 10
    assert len(VALID_GROUPS) == 10


def test_category_count_in_expected_range() -> None:
    # Brain ships ~53 categories; PLAN.md says "10x50". Allow drift but
    # alert on any wild change.
    assert 45 <= len(VALID_CATEGORIES) <= 60


def test_every_group_has_categories() -> None:
    for g in TAXONOMY:
        assert g["categories"], f"Group {g['group']} has zero categories"
        assert isinstance(g["color"], str) and g["color"].startswith("#")


def test_no_duplicate_categories() -> None:
    seen: set[str] = set()
    dupes: list[str] = []
    for g in TAXONOMY:
        for c in g["categories"]:
            if c in seen:
                dupes.append(c)
            seen.add(c)
    assert not dupes, f"duplicate categories: {dupes}"


def test_category_to_group_consistent() -> None:
    for g in TAXONOMY:
        for c in g["categories"]:
            assert CATEGORY_TO_GROUP[c] == g["group"]


def test_group_colors_cover_groups() -> None:
    assert set(GROUP_COLORS.keys()) == VALID_GROUPS


def test_taxonomy_version_set() -> None:
    assert TAXONOMY_VERSION
    assert TAXONOMY_VERSION.startswith("v")


# ---------------------------------------------------------------------------
# validate_category
# ---------------------------------------------------------------------------
def test_validate_canonical_category() -> None:
    cat, group = validate_category("Acquisition", "Deals & Partnerships")
    assert (cat, group) == ("Acquisition", "Deals & Partnerships")


def test_validate_canonicalizes_group_when_mismatched() -> None:
    # Model picks valid category but wrong group -> we always rewrite the group.
    cat, group = validate_category("Acquisition", "Wrong Group")
    assert cat == "Acquisition"
    assert group == "Deals & Partnerships"


def test_validate_unknown_category_falls_back() -> None:
    cat, group = validate_category("Made-Up Category")
    assert (cat, group) == (UNCATEGORIZED, UNCATEGORIZED_GROUP)


def test_validate_empty_falls_back() -> None:
    assert validate_category("") == (UNCATEGORIZED, UNCATEGORIZED_GROUP)
    assert validate_category(None) == (UNCATEGORIZED, UNCATEGORIZED_GROUP)


def test_validate_legacy_alias_resolves() -> None:
    cat, group = validate_category("USFDA")
    assert cat == "USFDA (Approval/Warning/Import Alert)"
    assert group == "Regulatory & Compliance"


# ---------------------------------------------------------------------------
# taxonomy_as_text
# ---------------------------------------------------------------------------
def test_taxonomy_as_text_includes_every_category_by_default() -> None:
    text = taxonomy_as_text()
    for g in TAXONOMY:
        assert f"Group: {g['group']}" in text
        for c in g["categories"]:
            assert c in text


def test_taxonomy_as_text_skips_requested_groups() -> None:
    text = taxonomy_as_text(skip_groups=frozenset({"Other"}))
    assert "Group: Other" not in text
    # "Other Important" is in Other group -> should be absent
    assert "Other Important" not in text
    # Spot-check a kept group
    assert "Acquisition" in text
