"""Unit tests: priority/rubric.py — taxonomy coverage + bucket mapping."""
from __future__ import annotations

import pytest

from market_notification.classifier.taxonomy import (
    UNCATEGORIZED,
    VALID_CATEGORIES,
)
from market_notification.priority.rubric import (
    BASE_SCORE,
    CATEGORY_PRIORITY,
    assert_rubric_complete,
    bucket_for_score,
    default_for,
)


def test_assert_rubric_complete_passes_at_import() -> None:
    # Calling it again must remain a no-op once the rubric is in sync with
    # the taxonomy.
    assert_rubric_complete()


def test_every_taxonomy_category_has_a_priority() -> None:
    missing = sorted(VALID_CATEGORIES - set(CATEGORY_PRIORITY.keys()))
    assert missing == [], f"taxonomy categories missing from rubric: {missing}"


def test_uncategorized_maps_to_normal() -> None:
    assert CATEGORY_PRIORITY[UNCATEGORIZED] == "normal"


@pytest.mark.parametrize(
    "category, expected_bucket",
    [
        ("Acquisition", "medium"),
        ("Buyback", "important"),
        ("Dividend", "normal"),
        ("USFDA (Approval/Warning/Import Alert)", "important"),
        ("Compliance Filing", "normal"),
        ("Board Meeting Outcome", "normal"),
    ],
)
def test_default_for_known_categories(category: str, expected_bucket: str) -> None:
    bucket, score = default_for(category)
    assert bucket == expected_bucket
    assert score == BASE_SCORE[expected_bucket]


def test_default_for_unknown_falls_back_to_normal() -> None:
    bucket, score = default_for("never-heard-of-it")
    assert bucket == "normal"
    assert score == BASE_SCORE["normal"]


@pytest.mark.parametrize(
    "score, expected_bucket",
    [
        (-5, "ignored"),
        (0, "ignored"),
        (1, "normal"),
        (39, "normal"),
        (40, "medium"),
        (69, "medium"),
        (70, "important"),
        (95, "important"),
    ],
)
def test_bucket_for_score(score: int, expected_bucket: str) -> None:
    assert bucket_for_score(score) == expected_bucket
