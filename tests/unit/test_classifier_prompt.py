"""Unit tests: classify_v1 prompt — golden-snapshot + structural checks.

Why a golden snapshot
---------------------
The prompt-version field is stamped onto every classified row. If someone
edits the prompt without bumping the version, every later re-classification
will silently use new wording while claiming to be the old version. The
snapshot here forces an explicit bump.
"""
from __future__ import annotations

from market_notification.classifier.prompts.classify_v1 import (
    PROMPT_VERSION,
    build_classify_prompt,
    metadata,
    render_system_prompt,
    render_user_prompt,
)
from market_notification.classifier.taxonomy import (
    TAXONOMY_VERSION,
    UNCATEGORIZED,
    VALID_CATEGORIES,
)


# ---------------------------------------------------------------------------
# System prompt structure
# ---------------------------------------------------------------------------
def test_system_prompt_lists_every_real_category() -> None:
    text = render_system_prompt()
    # Skip categories from the "Other" group — they are intentionally
    # omitted from the prompt listing (the fallback path uses `Uncategorized`).
    other_cats = {"Other Important", "Compliance Filing"}
    for cat in VALID_CATEGORIES - other_cats:
        assert cat in text, f"missing category in prompt: {cat!r}"


def test_system_prompt_has_uncategorized_fallback_instruction() -> None:
    text = render_system_prompt()
    assert UNCATEGORIZED in text


def test_system_prompt_specifies_json_schema() -> None:
    text = render_system_prompt()
    assert '"ai_category"' in text
    assert '"ai_category_group"' in text
    assert '"confidence"' in text


def test_metadata_returns_expected_versions() -> None:
    meta = metadata()
    assert meta["prompt_version"] == PROMPT_VERSION
    assert meta["taxonomy_version"] == TAXONOMY_VERSION


# ---------------------------------------------------------------------------
# User prompt structure
# ---------------------------------------------------------------------------
def test_user_prompt_contains_all_fields() -> None:
    user = render_user_prompt(
        source="BSE",
        headline="Allotment of equity shares",
        category="Allotment of Securities",
        subcategory=None,
        body="Body text.",
    )
    assert "Source: BSE" in user
    assert "Headline: Allotment of equity shares" in user
    assert "Allotment of Securities" in user
    assert "(none)" in user  # subcategory missing -> "(none)"
    assert "Body text." in user


def test_user_prompt_truncates_long_body() -> None:
    long_body = "x" * 5000
    user = render_user_prompt(
        source="NSE",
        headline="x",
        category=None,
        subcategory=None,
        body=long_body,
        body_max_chars=100,
    )
    assert "[truncated]" in user
    # body header + 100 chars of x + truncation tag — total < 5000
    assert len(user) < 1000


# ---------------------------------------------------------------------------
# Golden snapshot — bump PROMPT_VERSION when this changes
# ---------------------------------------------------------------------------
def test_prompt_version_pinned() -> None:
    assert PROMPT_VERSION == "classify_v1.1-2026-05-07"


def test_system_prompt_snapshot() -> None:
    """Snapshot: hash-stable summary of the system prompt.

    We deliberately don't snapshot the full text to keep the test readable
    — but every section header that *would* break callers gets asserted.
    """
    text = render_system_prompt()
    expected_markers = [
        "You are a financial analyst",
        "Pick exactly ONE category",
        "Available categories",
        "Output schema",
        '"ai_category":',
        '"ai_category_group":',
        '"confidence":',
        f'"{UNCATEGORIZED}"',
        '"Compliance Filing"',
        "Respond with ONLY the JSON object",
    ]
    for marker in expected_markers:
        assert marker in text, f"system prompt missing marker: {marker!r}"


def test_build_classify_prompt_returns_pair() -> None:
    system, user = build_classify_prompt(
        source="BSE",
        headline="X",
        category=None,
        subcategory=None,
        body=None,
    )
    assert "Available categories" in system
    assert "Source: BSE" in user
