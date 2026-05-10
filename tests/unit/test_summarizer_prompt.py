"""Unit tests: prompt rendering — golden-snapshot markers + invariants."""
from __future__ import annotations

from market_notification.summarizer.prompts.summarize_v1 import (
    PROMPT_VERSION,
    build_deferred_summarize_prompt,
    build_strict_retry_prompt,
    build_summarize_prompt,
    deferred_tag_for,
    metadata,
)


def _kwargs() -> dict:
    return {
        "source": "BSE",
        "headline": "Acquires 100% stake in XYZ Pvt Ltd",
        "category": "Acquisition",
        "subcategory": "-",
        "ai_category": "Acquisition",
        "ai_category_group": "Deals & Partnerships",
        "ai_priority": "important",
        "ai_priority_score": 85,
        "body": "Body of the announcement explaining the deal.",
        "pdf_text": "Detailed terms and conditions of the acquisition...",
    }


def test_version_is_pinned():
    assert PROMPT_VERSION.startswith("summarize_v1.")
    assert metadata()["prompt_version"] == PROMPT_VERSION


def test_full_body_prompt_contains_required_markers():
    system, user = build_summarize_prompt(**_kwargs())
    # Schema marker
    assert "Output schema" in system
    assert '"summary"' in system
    assert '"key_figures"' in system
    assert '"deferred_doc_tags"' in system
    # Behavioral rules
    assert "PRESERVE FIGURES VERBATIM" in system
    # User content present
    assert "Acquires 100% stake" in user
    assert "Acquisition" in user
    assert "important" in user
    assert "Detailed terms and conditions" in user


def test_full_body_truncates_long_inputs():
    big_body = "x" * 5000
    big_pdf = "y" * 50000
    kwargs = _kwargs()
    kwargs["body"] = big_body
    kwargs["pdf_text"] = big_pdf
    _, user = build_summarize_prompt(**kwargs)
    assert "[truncated]" in user


def test_deferred_prompt_pre_sets_tag_and_kind():
    system, user = build_deferred_summarize_prompt(
        source="BSE",
        headline="Quarterly Results — Q1FY26",
        category="Result Q",
        subcategory=None,
        ai_category="Quarterly Results",
        ai_category_group="Financials & Reports",
        ai_priority="medium",
        ai_priority_score=50,
        deferred_doc_type="earnings",
    )
    # Tag baked into the system prompt
    assert "'earnings'" in system or "\"earnings\"" in system
    # Kind label rendered
    assert "quarterly/annual earnings" in system
    # Body intentionally omitted in user
    assert "intentionally withheld" in user
    # Schema still present
    assert '"summary"' in system


def test_deferred_prompt_handles_synonym():
    """'investor_presentation' is synonymous with 'ppt' per FR-SUMM-002 enum."""
    system, _ = build_deferred_summarize_prompt(
        source="NSE",
        headline="Investor Presentation",
        category=None,
        subcategory=None,
        ai_category="Investor Presentation",
        ai_category_group="Financials & Reports",
        ai_priority="medium",
        ai_priority_score=50,
        deferred_doc_type="investor_presentation",
    )
    assert "investor presentation" in system
    assert "'ppt'" in system or "\"ppt\"" in system


def test_strict_retry_preserves_user_payload_and_adds_errors():
    base = "Source: BSE\nHeadline: foo"
    out = build_strict_retry_prompt(
        base_user_prompt=base,
        prior_errors=["empty_summary", "key_dates[0]: missing label or iso_date"],
    )
    assert base in out  # original user payload preserved at the tail
    assert "empty_summary" in out
    assert "MUST be a non-empty 2-4 sentence string" in out
    # Reinforcement preamble comes before the original payload
    assert out.index("MUST be a non-empty") < out.index(base)


def test_deferred_tag_for_helper():
    assert deferred_tag_for(None) is None
    assert deferred_tag_for("earnings") == "earnings"
    assert deferred_tag_for("investor_presentation") == "ppt"
    assert deferred_tag_for("annual_report") == "annual_report"
    # Unknown source value falls back to large_misc
    assert deferred_tag_for("something_new") == "large_misc"
