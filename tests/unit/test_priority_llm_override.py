"""Unit tests: GemmaLlmPriorityOverride with injected fake transport."""
from __future__ import annotations

import json

from market_notification.priority.base import (
    NotificationPriorityInput,
    PriorityResult,
)
from market_notification.priority.llm_override import (
    GemmaLlmPriorityOverride,
    _OverrideCallSpec,
)


def _det(bucket: str = "medium", score: int = 50) -> PriorityResult:
    return PriorityResult(
        bucket=bucket, score=score, reasons=["Default: medium (base 50)"],
        source="deterministic",
    )


def _input(headline: str = "Acquires Foo Ltd", category: str = "Acquisition") -> NotificationPriorityInput:
    return NotificationPriorityInput(
        notification_id=1, headline=headline, body="", pdf_text="",
        ai_category=category, ai_category_group="Deals & Partnerships",
    )


def test_override_upgrades_to_important() -> None:
    def transport(spec: _OverrideCallSpec) -> str:
        return json.dumps({
            "priority": "important",
            "reasoning": "Adds 25% to revenue",
            "confidence": 0.9,
        })

    eng = GemmaLlmPriorityOverride(model="m", transport=transport)
    out = eng.override(_input(), _det())
    assert out.bucket == "important"
    assert out.source == "llm_override"
    assert any("LLM override" in r for r in out.reasons)
    assert out.score >= 70


def test_override_downgrades_to_normal() -> None:
    def transport(spec: _OverrideCallSpec) -> str:
        return json.dumps({
            "priority": "normal",
            "reasoning": "Tiny acquisition, immaterial",
            "confidence": 0.6,
        })

    eng = GemmaLlmPriorityOverride(model="m", transport=transport)
    out = eng.override(_input(), _det("important", 80))
    assert out.bucket == "normal"
    assert out.source == "llm_override"


def test_override_confirms_keeps_det_score() -> None:
    def transport(spec: _OverrideCallSpec) -> str:
        return json.dumps({"priority": "medium", "reasoning": "OK", "confidence": 0.8})

    eng = GemmaLlmPriorityOverride(model="m", transport=transport)
    out = eng.override(_input(), _det("medium", 55))
    assert out.bucket == "medium"
    # When the bucket matches, we keep the det score (richer reasoning)
    assert out.score == 55


def test_override_falls_back_when_invalid_bucket() -> None:
    def transport(spec: _OverrideCallSpec) -> str:
        return json.dumps({"priority": "super-important"})

    eng = GemmaLlmPriorityOverride(model="m", transport=transport)
    det = _det()
    out = eng.override(_input(), det)
    assert out.bucket == det.bucket
    assert out.source == "deterministic"
    assert any("override_invalid_bucket" in r for r in out.reasons)


def test_override_falls_back_on_unparseable_json() -> None:
    def transport(spec: _OverrideCallSpec) -> str:
        return "definitely not json"

    eng = GemmaLlmPriorityOverride(model="m", transport=transport)
    det = _det()
    out = eng.override(_input(), det)
    assert out.source == "deterministic"
    assert any("override_unparseable" in r for r in out.reasons)


def test_override_falls_back_on_transport_error() -> None:
    def transport(spec: _OverrideCallSpec) -> str:
        raise ConnectionError("ollama down")

    eng = GemmaLlmPriorityOverride(model="m", transport=transport)
    det = _det()
    out = eng.override(_input(), det)
    assert out.source == "deterministic"
    assert any("override_skipped" in r for r in out.reasons)
