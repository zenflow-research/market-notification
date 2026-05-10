"""Gemma LLM priority override (Phase 6, FR-PRIORITY-006).

Behavior
--------
- Receives the deterministic verdict + the notification text + (optionally)
  Gemma's summary and impact when available.
- Asks Gemma to confirm/upgrade/downgrade. Per design-decision D-11 the
  override has full authority — it can move the verdict in either
  direction and reasons are journaled for audit.
- If the LLM call fails or returns an invalid bucket, the deterministic
  verdict is returned unchanged with the failure noted in `reasons`.

Design choices that mirror the classifier
-----------------------------------------
- ``think=False`` to defeat reasoning-model token starvation (same gotcha
  as the classifier; Gemma 4 MoE eats the full budget on hidden CoT
  otherwise).
- Injectable transport so unit tests run without Ollama.
- JSON-mode output with a permissive parser tolerant of fences.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .base import (
    LlmPriorityOverride,
    NotificationPriorityInput,
    PriorityResult,
)
from .prompts.override_v1 import PROMPT_VERSION, render
from .rubric import BASE_SCORE

logger = logging.getLogger(__name__)


_VALID_BUCKETS = frozenset(BASE_SCORE.keys())  # important / medium / normal / ignored


# ---------------------------------------------------------------------------
# Transport (matches classifier shape so tests can reuse the pattern)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _OverrideCallSpec:
    model: str
    system: str
    user: str
    temperature: float
    num_predict: int
    request_timeout_s: int
    keep_alive: str
    base_url: str


OverrideTransport = Callable[[_OverrideCallSpec], str]


def _default_ollama_override_transport(spec: _OverrideCallSpec) -> str:
    import ollama  # type: ignore[import-not-found]

    client = ollama.Client(host=spec.base_url, timeout=spec.request_timeout_s)
    chat_kwargs: dict[str, Any] = {
        "model": spec.model,
        "format": "json",
        "keep_alive": spec.keep_alive,
        "options": {
            "temperature": spec.temperature,
            "num_predict": spec.num_predict,
        },
        "messages": [
            {"role": "system", "content": spec.system},
            {"role": "user", "content": spec.user},
        ],
    }
    try:
        resp = client.chat(**chat_kwargs, think=False)
    except TypeError:
        resp = client.chat(**chat_kwargs)
    return resp["message"]["content"]


# ---------------------------------------------------------------------------
# Public override
# ---------------------------------------------------------------------------
class GemmaLlmPriorityOverride(LlmPriorityOverride):
    """Gemma-driven priority override."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://localhost:11434",
        temperature: float = 0.1,
        num_predict: int = 256,
        request_timeout_s: int = 300,
        keep_alive: str = "24h",
        transport: Optional[OverrideTransport] = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.temperature = temperature
        self.num_predict = num_predict
        self.request_timeout_s = request_timeout_s
        self.keep_alive = keep_alive
        self._transport: OverrideTransport = (
            transport or _default_ollama_override_transport
        )

    def override(
        self,
        inp: NotificationPriorityInput,
        deterministic: PriorityResult,
        gemma_summary: str = "",
        gemma_impact: str = "",
    ) -> PriorityResult:
        system, user = render(
            source="?",  # source isn't on the input dataclass; not load-bearing
            headline=inp.headline,
            ai_category=inp.ai_category,
            ai_category_group=inp.ai_category_group,
            det_bucket=deterministic.bucket,
            det_score=deterministic.score,
            det_reasons=deterministic.reasons,
            body=inp.body or inp.pdf_text,
            summary=gemma_summary,
            impact=gemma_impact,
        )
        spec = _OverrideCallSpec(
            model=self.model,
            system=system,
            user=user,
            temperature=self.temperature,
            num_predict=self.num_predict,
            request_timeout_s=self.request_timeout_s,
            keep_alive=self.keep_alive,
            base_url=self.base_url,
        )

        try:
            raw = self._transport(spec)
        except Exception as e:
            logger.warning(
                "LLM override failed for notif=%d: %s -> keeping deterministic",
                inp.notification_id, e,
            )
            return _carry_with_note(deterministic, f"override_skipped: {e!r}")

        parsed = _safe_json_loads(raw)
        if parsed is None:
            return _carry_with_note(deterministic, f"override_unparseable: {raw[:200]!r}")

        bucket = (parsed.get("priority") or "").strip().lower()
        if bucket not in _VALID_BUCKETS:
            return _carry_with_note(
                deterministic, f"override_invalid_bucket: {bucket!r}"
            )

        reasoning = str(parsed.get("reasoning") or "").strip()
        confidence = parsed.get("confidence")
        score = _bucket_to_score(bucket, deterministic.score)
        new_reasons = list(deterministic.reasons) + [
            f"LLM override -> {bucket}: {reasoning or '(no reasoning)'}",
        ]
        if confidence is not None:
            new_reasons.append(f"LLM confidence: {confidence}")
        new_reasons.append(f"override_prompt_version: {PROMPT_VERSION}")
        return PriorityResult(
            bucket=bucket,
            score=score,
            reasons=new_reasons,
            source="llm_override",
            extracted_amount_cr=deterministic.extracted_amount_cr,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _safe_json_loads(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK_RE.search(text)
    if m is None:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _bucket_to_score(bucket: str, det_score: int) -> int:
    """Pick a score consistent with the LLM's bucket.

    If the LLM agrees with the deterministic bucket, keep the deterministic
    score (preserves the rich threshold-based reasoning). Otherwise use
    the bucket's base score.
    """
    base = BASE_SCORE.get(bucket, BASE_SCORE["normal"])
    return det_score if _bucket_for_score_local(det_score) == bucket else base


def _bucket_for_score_local(score: int) -> str:
    if score <= 0:
        return "ignored"
    if score >= 70:
        return "important"
    if score >= 40:
        return "medium"
    return "normal"


def _carry_with_note(deterministic: PriorityResult, note: str) -> PriorityResult:
    """Return the deterministic result, appending an override-skipped note.

    `source` stays `'deterministic'` so callers can tell the override didn't
    fire. The note ends up in the journal + `ai_priority_reasons` JSON.
    """
    return PriorityResult(
        bucket=deterministic.bucket,
        score=deterministic.score,
        reasons=list(deterministic.reasons) + [note],
        source="deterministic",
        extracted_amount_cr=deterministic.extracted_amount_cr,
    )


__all__ = [
    "GemmaLlmPriorityOverride",
    "OverrideTransport",
]
