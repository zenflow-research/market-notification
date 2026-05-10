"""Gemma summarizer over Ollama (Phase 8, FR-SUMM-001..005).

Orchestrates one summarize unit-of-work:

    read row
      └─ branch on deferred_doc_type:
           - if set: deferred prompt (FR-SUMM-004; body NOT fed)
           - else  : full-body prompt (headline + body + pdf_extracted_text)
      └─ Ollama chat (think=False; FR-SUMM-001)
      └─ schema.validate
           └─ if fatal errors AND retries_left > 0:
               rebuild user prompt with strict-retry preamble + prior errors
               re-call Ollama (FR-SUMM-002 "up to 2 times with stricter prompts")
      └─ persist via repo.update_summary
      └─ advance pipeline_status summarize_pending -> deep_dive_pending|done
      └─ journal entry

Intentionally does NOT handle:
  - Ollama-down retry/backoff at the queue level (that's queue_retry.py
    feeding off pipeline_status='summarize_failed').
  - Claiming the next summarize_pending row (that's the dispatcher,
    Phase 10). This module exposes ``summarize(notification_id)`` directly.

Failure semantics
-----------------
* **Transport error / Ollama unreachable.** Re-raised as
  ``OllamaUnavailableError``. The caller (queue_retry / dispatcher) is
  responsible for transitioning the row to ``summarize_failed`` and
  scheduling the 30-second retry per FR-SUMM-006. We deliberately do NOT
  swallow this here — burying it would prevent the queue layer from
  applying the retry policy.
* **Model returned junk after MAX_STRICT_RETRIES.** We persist a degraded
  envelope (with whatever the validator could salvage), advance the row,
  and write the validator errors into ``last_error`` so the Health UI can
  flag it. Failing the row in this case loses information — the row
  cannot be summarized in this prompt regime, retrying won't help, so we
  capture-what-we-can and move on.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..db.repositories.journal_repo_sqla import SqlaJournalRepo
from ..db.repositories.notification_repo_sqla import SqlaNotificationRepo
from ..db.session import get_session
from .base import SummaryResult, Summarizer
from .prompts.summarize_v1 import (
    PROMPT_VERSION,
    build_deferred_summarize_prompt,
    build_strict_retry_prompt,
    build_summarize_prompt,
    deferred_tag_for,
    metadata as prompt_metadata,
)
from .schema import is_fatal, validate

logger = logging.getLogger(__name__)


# Maximum number of stricter-prompt retries when the validator flags the
# output as fatal (FR-SUMM-002 "up to 2 times with stricter prompts").
MAX_STRICT_RETRIES = 2


class OllamaUnavailableError(RuntimeError):
    """Raised when the Ollama transport itself fails (network/timeout).

    Distinct from "model returned bad JSON" so the queue layer can apply
    FR-SUMM-006 backoff to transport failures only.
    """


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

    ``think=False`` disables hidden chain-of-thought on reasoning-capable
    Gemma variants; without it the entire ``num_predict`` budget gets
    consumed by a hidden trace and the visible content is empty. See the
    Phase 5 lesson in `classifier/llm_classifier.py`.
    """
    try:
        import ollama  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover -- not exercised in unit tests
        raise OllamaUnavailableError(f"ollama package missing: {e}") from e

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
        try:
            resp = client.chat(**chat_kwargs, think=False)  # type: ignore[call-arg]
        except TypeError:
            resp = client.chat(**chat_kwargs)
    except Exception as e:  # noqa: BLE001 -- broad on purpose; promote to typed exc
        raise OllamaUnavailableError(f"ollama transport failure: {e!r}") from e
    return resp["message"]["content"]


# ---------------------------------------------------------------------------
# Run record (returned by `summarize` for callers that care)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SummarizeRunResult:
    notification_id: int
    summary: SummaryResult
    attempts: int
    fallback: bool
    used_deferred_prompt: bool
    duration_ms: int
    validator_errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public summarizer
# ---------------------------------------------------------------------------
class GemmaLlmSummarizer(Summarizer):
    """Concrete ``Summarizer`` backed by Gemma over Ollama.

    Args:
        model: Ollama model tag (e.g. ``gemma4-zenflow-moe:latest``).
        base_url: Ollama base URL.
        temperature: 0.1 per FR-SUMM-003 for figure-preserving determinism.
        num_predict: 4096 default; the extended schema can run long.
        request_timeout_s, keep_alive: model knobs.
        transport: optional injected callable for tests.
        session_factory: optional override for tests.
        next_status_done: ``pipeline_status`` set after a deferred-doc
            summarize. Defaults to ``done_deferred`` because deferred docs
            are end-of-line per design-decisions.md H2 — no deep-dive.
        next_status_active: ``pipeline_status`` set after a normal
            summarize. Defaults to ``deep_dive_pending``; the dispatcher
            will downgrade to ``done`` for rows whose category isn't
            deep-dive eligible.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://localhost:11434",
        temperature: float = 0.1,
        num_predict: int = 4096,
        request_timeout_s: int = 300,
        keep_alive: str = "24h",
        transport: Optional[LlmTransport] = None,
        session_factory: Optional[Callable[[], Any]] = None,
        next_status_done: str = "done_deferred",
        next_status_active: str = "deep_dive_pending",
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.temperature = temperature
        self.num_predict = num_predict
        self.request_timeout_s = request_timeout_s
        self.keep_alive = keep_alive
        self._transport: LlmTransport = transport or _default_ollama_transport
        self._session_factory = session_factory or get_session
        self.next_status_done = next_status_done
        self.next_status_active = next_status_active

    # ------------------------------------------------------------------
    # Public API: Summarizer ABC
    # ------------------------------------------------------------------
    def summarize(self, notification_id: int) -> SummaryResult:
        """ABC-compliant entrypoint. Use ``summarize_with_meta`` for diagnostics."""
        return self.summarize_with_meta(notification_id).summary

    def summarize_with_meta(self, notification_id: int) -> SummarizeRunResult:
        """Run the unit of work and return the rich record."""
        started_ms = time.monotonic()

        with self._session_factory() as sess:
            repo = SqlaNotificationRepo(sess)
            row = repo.get(notification_id)
            if row is None:
                raise ValueError(f"Notification {notification_id} not found")

        deferred_doc_type = row.get("deferred_doc_type")
        used_deferred = bool(deferred_doc_type)

        if used_deferred:
            system, user = build_deferred_summarize_prompt(
                source=row.get("source") or "",
                headline=row.get("headline") or "",
                category=row.get("category"),
                subcategory=row.get("subcategory"),
                ai_category=row.get("ai_category"),
                ai_category_group=row.get("ai_category_group"),
                ai_priority=row.get("ai_priority"),
                ai_priority_score=row.get("ai_priority_score"),
                deferred_doc_type=deferred_doc_type,
            )
        else:
            system, user = build_summarize_prompt(
                source=row.get("source") or "",
                headline=row.get("headline") or "",
                category=row.get("category"),
                subcategory=row.get("subcategory"),
                ai_category=row.get("ai_category"),
                ai_category_group=row.get("ai_category_group"),
                ai_priority=row.get("ai_priority"),
                ai_priority_score=row.get("ai_priority_score"),
                body=row.get("body"),
                pdf_text=row.get("pdf_extracted_text"),
            )

        # Run model with bounded stricter-prompt retries (FR-SUMM-002).
        attempts = 0
        attempt_user = user
        result: SummaryResult
        errors: list[str]
        while True:
            attempts += 1
            spec = self._build_spec(system=system, user=attempt_user)
            raw_text = self._transport(spec)  # raises OllamaUnavailableError
            parsed = _safe_json_loads(raw_text)
            result, errors = validate(
                parsed,
                used_model=self.model,
                used_prompt_version=PROMPT_VERSION,
            )
            if not is_fatal(errors) or attempts > MAX_STRICT_RETRIES:
                break
            attempt_user = build_strict_retry_prompt(
                base_user_prompt=user, prior_errors=errors
            )

        # FR-SUMM-004: ensure the deferred tag is present on deferred rows
        # even if the model omitted it.
        if used_deferred:
            tag = deferred_tag_for(deferred_doc_type)
            if tag and tag not in result.deferred_doc_tags:
                result = _with_deferred_tag(result, tag)

        elapsed_ms = int((time.monotonic() - started_ms) * 1000)
        is_fallback = is_fatal(errors) and attempts > MAX_STRICT_RETRIES

        # Persist + journal in one transaction.
        with self._session_factory() as sess:
            repo = SqlaNotificationRepo(sess)
            journal = SqlaJournalRepo(sess)

            payload = _result_to_payload(result, errors=errors if is_fallback else None)
            payload["model_version"] = self.model
            payload["prompt_version"] = PROMPT_VERSION
            repo.update_summary(notification_id, payload)

            from_status = row.get("pipeline_status", "summarize_pending")
            to_status = (
                self.next_status_done if used_deferred else self.next_status_active
            )
            repo.update_pipeline_status(
                notification_id, from_status=from_status, to_status=to_status
            )
            journal.append(
                notification_id=notification_id,
                from_status=from_status,
                to_status=to_status,
                actor="summarizer",
                duration_ms=elapsed_ms,
                error_kind=("fallback" if is_fallback else None),
                error_message=(
                    "; ".join(errors)[:1000] if is_fallback else (
                        f"attempts={attempts} deferred={used_deferred}"
                    )
                ),
            )

        return SummarizeRunResult(
            notification_id=notification_id,
            summary=result,
            attempts=attempts,
            fallback=is_fallback,
            used_deferred_prompt=used_deferred,
            duration_ms=elapsed_ms,
            validator_errors=errors,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _build_spec(self, *, system: str, user: str) -> _LlmCallSpec:
        return _LlmCallSpec(
            model=self.model,
            system=system,
            user=user,
            temperature=self.temperature,
            num_predict=self.num_predict,
            request_timeout_s=self.request_timeout_s,
            keep_alive=self.keep_alive,
            base_url=self.base_url,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _safe_json_loads(text: str) -> Optional[dict[str, Any]]:
    """Parse a JSON object, tolerating ```json fences``` or surrounding prose."""
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(text)
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _with_deferred_tag(result: SummaryResult, tag: str) -> SummaryResult:
    """Return a copy of ``result`` with ``tag`` appended to ``deferred_doc_tags``."""
    from dataclasses import replace

    return replace(result, deferred_doc_tags=[*result.deferred_doc_tags, tag])


def _result_to_payload(
    result: SummaryResult, *, errors: Optional[list[str]] = None
) -> dict[str, Any]:
    """Convert the dataclass into the JSON-encoded payload the repo writes."""
    payload: dict[str, Any] = {
        "summary": result.summary or None,
        "impact": result.impact or None,
        "key_figures": json.dumps(
            [_kf_to_dict(kf) for kf in result.key_figures], ensure_ascii=False
        ),
        "key_people": json.dumps(
            [_kp_to_dict(kp) for kp in result.key_people], ensure_ascii=False
        ),
        "key_dates": json.dumps(
            [_kd_to_dict(kd) for kd in result.key_dates], ensure_ascii=False
        ),
        "attachments_referenced": json.dumps(
            list(result.attachments_referenced), ensure_ascii=False
        ),
        "deferred_tags": json.dumps(
            list(result.deferred_doc_tags), ensure_ascii=False
        ),
        "external_links": json.dumps(
            [_el_to_dict(el) for el in result.external_links], ensure_ascii=False
        ),
        "confidence": float(result.confidence),
    }
    if errors is not None:
        payload["last_error"] = ("summarize_fallback: " + "; ".join(errors))[:1000]
    else:
        payload["last_error"] = None
    return payload


def _kf_to_dict(kf) -> dict[str, str]:  # noqa: ANN001
    return {"label": kf.label, "value": kf.value, "unit": kf.unit}


def _kp_to_dict(kp) -> dict[str, str]:  # noqa: ANN001
    return {"name": kp.name, "role": kp.role}


def _kd_to_dict(kd) -> dict[str, str]:  # noqa: ANN001
    return {"label": kd.label, "iso_date": kd.iso_date, "certainty": kd.certainty}


def _el_to_dict(el) -> dict[str, str]:  # noqa: ANN001
    return {
        "url": el.url,
        "referenced_as": el.referenced_as,
        "target_summary": el.target_summary,
    }


__all__ = [
    "GemmaLlmSummarizer",
    "LlmTransport",
    "MAX_STRICT_RETRIES",
    "OllamaUnavailableError",
    "PROMPT_VERSION",
    "SummarizeRunResult",
    "_LlmCallSpec",
]


# Re-export prompt_metadata for the smoke harness convenience.
metadata = prompt_metadata
