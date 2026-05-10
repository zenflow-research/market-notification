"""Unit tests: pure schema validator (no DB, no Ollama)."""
from __future__ import annotations

from market_notification.summarizer.schema import (
    ALLOWED_DEFERRED_TAGS,
    is_fatal,
    validate,
)


def _envelope(**overrides):
    """Build a fully-valid envelope and override fields per test."""
    base = {
        "summary": "Acquired XYZ Pvt Ltd for INR 100 Cr.",
        "impact": "Materially adds to FY26 revenue.",
        "key_figures": [
            {"label": "deal_size", "value": "100", "unit": "INR Cr"},
            {"label": "stake", "value": "100", "unit": "%"},
        ],
        "key_people": [{"name": "Jane Doe", "role": "CEO"}],
        "key_dates": [
            {"label": "completion", "iso_date": "2026-06-30", "certainty": "expected"}
        ],
        "attachments_referenced": ["acq_announcement.pdf"],
        "deferred_doc_tags": [],
        "external_links": [
            {
                "url": "https://example.com/note",
                "referenced_as": "side letter",
                "target_summary": "",
            }
        ],
        "confidence": 0.85,
    }
    base.update(overrides)
    return base


def test_happy_envelope_passes_clean():
    result, errors = validate(_envelope())
    assert errors == []
    assert result.summary.startswith("Acquired XYZ")
    assert len(result.key_figures) == 2
    assert result.key_figures[0].label == "deal_size"
    assert result.key_figures[0].value == "100"
    assert result.key_figures[0].unit == "INR Cr"
    assert result.confidence == 0.85
    assert not is_fatal(errors)


def test_missing_summary_is_fatal():
    _, errors = validate(_envelope(summary=""))
    assert "empty_summary" in errors
    assert is_fatal(errors)


def test_none_input_is_fatal():
    result, errors = validate(None)
    assert any(e.startswith("not_a_dict") for e in errors)
    assert result.summary == ""
    assert is_fatal(errors)


def test_non_dict_input_is_fatal():
    _, errors = validate(["not", "a", "dict"])
    assert any(e.startswith("not_a_dict") for e in errors)
    assert is_fatal(errors)


def test_key_figures_drops_bad_entries_keeps_good():
    payload = _envelope(key_figures=[
        {"label": "good", "value": "10", "unit": "%"},
        "garbage",
        {"label": "missing-value"},  # missing value -> dropped
        {"label": "ok2", "value": 50, "unit": "Cr"},  # int value coerced to str
    ])
    result, errors = validate(payload)
    assert len(result.key_figures) == 2
    assert {kf.label for kf in result.key_figures} == {"good", "ok2"}
    assert result.key_figures[1].value == "50"  # coerced
    assert any("not_a_dict" in e for e in errors)
    assert not is_fatal(errors)  # not fatal — summary still present


def test_deferred_tags_filtered_to_known_set():
    payload = _envelope(deferred_doc_tags=["earnings", "weird_tag", "ppt"])
    result, errors = validate(payload)
    assert set(result.deferred_doc_tags) <= ALLOWED_DEFERRED_TAGS
    assert "earnings" in result.deferred_doc_tags
    assert "ppt" in result.deferred_doc_tags
    assert "weird_tag" not in result.deferred_doc_tags
    assert any("unknown_tag" in e for e in errors)


def test_confidence_rescaled_from_percent():
    _, errors = validate(_envelope(confidence=85))
    # 85 > 1 but <= 100 → rescaled to 0.85
    assert any("rescaled" in e for e in errors)


def test_confidence_clamped_above_100():
    result, errors = validate(_envelope(confidence=200))
    assert result.confidence == 1.0
    assert any("clamped" in e for e in errors)


def test_confidence_negative_clamped_to_zero():
    result, errors = validate(_envelope(confidence=-0.5))
    assert result.confidence == 0.0
    assert any("bad_confidence" in e for e in errors)


def test_key_dates_bad_certainty_defaults_to_announced():
    payload = _envelope(key_dates=[
        {"label": "agm", "iso_date": "2026-09-01", "certainty": "garbage"}
    ])
    result, errors = validate(payload)
    assert len(result.key_dates) == 1
    assert result.key_dates[0].certainty == "announced"
    assert any("bad certainty" in e for e in errors)


def test_external_links_missing_url_dropped():
    payload = _envelope(external_links=[
        {"url": "", "referenced_as": "x", "target_summary": ""},
        {"url": "https://ok.example", "referenced_as": "ok", "target_summary": ""},
    ])
    result, errors = validate(payload)
    assert len(result.external_links) == 1
    assert result.external_links[0].url == "https://ok.example"
    assert any("missing url" in e for e in errors)


def test_attachments_referenced_non_list_recorded_as_error():
    _, errors = validate(_envelope(attachments_referenced="single.pdf"))
    assert any("non_list_field: attachments_referenced" in e for e in errors)


def test_used_model_and_prompt_version_propagated():
    result, _ = validate(_envelope(), used_model="m1", used_prompt_version="v1")
    assert result.used_model == "m1"
    assert result.used_prompt_version == "v1"


def test_int_summary_coerced_with_error():
    """Defensive: model returns a number where a string is expected."""
    result, errors = validate(_envelope(summary=123))
    # We coerce, but record the type error
    assert any("non_string_field: summary" in e for e in errors)
    assert result.summary == "123"
