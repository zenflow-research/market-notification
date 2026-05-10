"""Gemma classifier over Ollama (Phase 5, FR-CLASSIFY-001..005).

Design
------
- ``classify(notification_id)`` reads the row, builds the (system, user) prompt,
  calls Ollama in JSON-mode, parses + validates the response, and persists
  the result via ``SqlaNotificationRepo.update_classification``.
- Validation is taxonomy-aware: any LLM-emitted category not in
  ``VALID_CATEGORIES`` is mapped to ``Uncategorized`` (the model is then
  marked ``ai_category_source='fallback'``). The group is always rewritten
  to the canonical lookup, even if the model picked a valid category but
  named the wrong group.
- The prompt is injected — the classifier doesn't reach into
  ``classifier.prompts`` at runtime; the test suite snapshots whatever
  prompt is passed in. This keeps the unit test independent of the
  network call and lets us swap prompts via versioning without forking
  the executor.
- Ollama failures bubble up; the worker layer (Phase 10 dispatcher) is
  responsible for retry/backoff. The classifier is a single unit of work.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..db.repositories.journal_repo_sqla import SqlaJournalRepo
from ..db.repositories.notification_repo_sqla import SqlaNotificationRepo
from ..db.session import get_session
from .base import ClassificationResult, Classifier
from .prompts.classify_v1 import build_classify_prompt, metadata as prompt_metadata
from .taxonomy import (
    TAXONOMY_VERSION,
    UNCATEGORIZED,
    UNCATEGORIZED_GROUP,
    VALID_CATEGORIES,
    validate_category,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ollama transport — pluggable so tests can inject a fake
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _LlmCallSpec:
    """Inputs to one Ollama chat call."""
    model: str
    system: str
    user: str
    temperature: float
    num_predict: int
    request_timeout_s: int
    keep_alive: str
    base_url: str


# Type for an injectable LLM transport. Returns the raw text response.
LlmTransport = Callable[[_LlmCallSpec], str]


def _default_ollama_transport(spec: _LlmCallSpec) -> str:
    """Real Ollama call. Imported lazily so tests don't need the package.

    `think=False` disables hidden chain-of-thought on reasoning-capable
    Gemma variants (e.g. ``gemma4-zenflow-moe``). Without it those models
    spend the entire ``num_predict`` budget on a hidden trace and return
    an empty response — silently and with no error. Classification doesn't
    benefit from reasoning, so we always disable it here.
    """
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
    # `think` was added in newer ollama-python; older versions reject it.
    try:
        resp = client.chat(**chat_kwargs, think=False)
    except TypeError:
        resp = client.chat(**chat_kwargs)
    return resp["message"]["content"]


# ---------------------------------------------------------------------------
# Public classifier
# ---------------------------------------------------------------------------
class GemmaLlmClassifier(Classifier):
    """Ollama/Gemma classifier — concrete implementation of ``Classifier``.

    Args:
        model: Ollama model tag (e.g. ``gemma4-zenflow-moe:latest``).
        base_url: Ollama base URL.
        temperature, num_predict, request_timeout_s, keep_alive: model knobs.
        transport: optional injected callable for tests; defaults to a
            real Ollama client.
        session_factory: optional session-factory override (defaults to
            ``get_session``); kept injectable so tests can drive the
            classifier with an in-memory engine.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://localhost:11434",
        temperature: float = 0.1,
        num_predict: int = 1024,
        request_timeout_s: int = 300,
        keep_alive: str = "24h",
        transport: Optional[LlmTransport] = None,
        session_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.temperature = temperature
        self.num_predict = num_predict
        self.request_timeout_s = request_timeout_s
        self.keep_alive = keep_alive
        self._transport: LlmTransport = transport or _default_ollama_transport
        self._session_factory = session_factory or get_session

    # ------------------------------------------------------------------
    # Classifier ABC
    # ------------------------------------------------------------------
    def classify(self, notification_id: int) -> ClassificationResult:
        """Read row, call Gemma, validate + persist, return the result."""
        started_ms = time.monotonic()

        with self._session_factory() as sess:
            repo = SqlaNotificationRepo(sess)
            row = repo.get(notification_id)
            if row is None:
                raise ValueError(f"Notification {notification_id} not found")

        system, user = build_classify_prompt(
            source=row["source"],
            headline=row["headline"],
            category=row.get("category"),
            subcategory=row.get("subcategory"),
            body=row.get("body") or row.get("pdf_extracted_text"),
        )

        spec = _LlmCallSpec(
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
            raw_text = self._transport(spec)
        except Exception as e:
            logger.warning(
                "Gemma classify failed for notif=%d: %s -> falling back",
                notification_id, e,
            )
            result = self._fallback_result(reason=f"ollama_error: {e!r}")
        else:
            result = self._parse_and_validate(raw_text)

        elapsed_ms = int((time.monotonic() - started_ms) * 1000)

        # Persist + journal in one transaction.
        with self._session_factory() as sess:
            repo = SqlaNotificationRepo(sess)
            journal = SqlaJournalRepo(sess)
            payload = {
                "category": result.category,
                "group": result.group,
                "confidence": result.confidence,
                "source": result.source,
                "reasoning": result.reasoning,
                "taxonomy_version": result.used_taxonomy_version,
                "prompt_version": result.used_prompt_version,
            }
            repo.update_classification(notification_id, payload)
            # Move the row out of classify_active so the dispatcher (Phase 10)
            # can pick the next stage. Phase 6 will own the priority transition;
            # for now we land in priority_pending.
            current_status = row.get("pipeline_status", "classify_pending")
            from_status = current_status
            to_status = "priority_pending"
            repo.update_pipeline_status(
                notification_id, from_status=from_status, to_status=to_status
            )
            journal.append(
                notification_id=notification_id,
                from_status=from_status,
                to_status=to_status,
                actor="classifier",
                duration_ms=elapsed_ms,
                error_kind=None if result.source == "gemma" else "fallback",
                error_message=result.reasoning if result.source != "gemma" else None,
            )

        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _parse_and_validate(self, raw_text: str) -> ClassificationResult:
        """Parse the JSON envelope and coerce to a valid (category, group) pair."""
        meta = prompt_metadata()
        parsed = _safe_json_loads(raw_text)
        if parsed is None:
            return self._fallback_result(reason=f"unparseable_json: {raw_text[:200]!r}")

        raw_cat = parsed.get("ai_category")
        raw_group = parsed.get("ai_category_group")
        confidence = _coerce_confidence(parsed.get("confidence"))

        category, group = validate_category(raw_cat, raw_group)

        # Distinguish "model gave us a real category" vs. "model gave junk".
        if raw_cat in VALID_CATEGORIES:
            source = "gemma"
            reasoning = ""
        elif category == UNCATEGORIZED:
            source = "fallback"
            reasoning = (
                f"unknown_category: ai_category={raw_cat!r}, ai_group={raw_group!r}"
            )
            # Coerce confidence down on fallback.
            confidence = min(confidence, 0.2)
        else:
            # Legacy alias path.
            source = "gemma"
            reasoning = f"alias_resolved: {raw_cat!r} -> {category!r}"

        return ClassificationResult(
            category=category,
            group=group,
            confidence=confidence,
            reasoning=reasoning,
            used_taxonomy_version=meta["taxonomy_version"],
            used_prompt_version=meta["prompt_version"],
            source=source,
        )

    @staticmethod
    def _fallback_result(*, reason: str) -> ClassificationResult:
        meta = prompt_metadata()
        return ClassificationResult(
            category=UNCATEGORIZED,
            group=UNCATEGORIZED_GROUP,
            confidence=0.0,
            reasoning=reason,
            used_taxonomy_version=meta["taxonomy_version"],
            used_prompt_version=meta["prompt_version"],
            source="fallback",
        )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _safe_json_loads(text: str) -> Optional[dict[str, Any]]:
    """Parse a JSON object, tolerating ```json fences``` or surrounding prose."""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK_RE.search(text)
    if match is None:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _coerce_confidence(value: Any) -> float:
    """Normalize the model's confidence to a float in [0, 1]."""
    if value is None:
        return 0.5
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.5
    if f < 0:
        return 0.0
    if f > 1:
        # Some models return 0..100; rescale.
        return min(1.0, f / 100.0) if f <= 100 else 1.0
    return f


__all__ = [
    "GemmaLlmClassifier",
    "LlmTransport",
    "TAXONOMY_VERSION",
]
