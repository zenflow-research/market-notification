"""Pure JSON-schema validator for Gemma summary output (FR-SUMM-002).

Why a stand-alone module
------------------------
The validator fires from three places:
  1. ``GemmaLlmSummarizer`` — to decide whether to retry with a stricter prompt.
  2. The Phase 8 unit tests — to assert envelope shape directly.
  3. The Phase 9 backfill — to re-validate brain-history summaries against the
     current schema before rolling them into ``imported_legacy`` rows.

Keeping the validator pure (no I/O, no Ollama client, no DB) means all three
callers share one definition of "valid". Schema drift would otherwise bite
us in the backfill.

Output of ``validate(parsed_json)``: a tuple ``(SummaryResult, list[str])``.
The list is empty on a clean parse; non-empty entries are human-readable
error strings which the caller decides how to surface (retry / persist as
fallback / journal).

Validation strategy
-------------------
We coerce, never reject. A model that returns ``key_figures: null`` is treated
as ``key_figures: []`` and a single error string is recorded. Same goes for
``key_figures: [{...}, "garbage", {...}]`` — bad entries dropped, good ones
kept. This is consistent with the Phase 5 classifier's "downgrade to fallback,
don't crash" principle.

The ONE thing we strictly require is a non-empty ``summary`` string. Without
that, there's no point persisting the row — the model has effectively returned
nothing useful. Empty-summary triggers the stricter-prompt retry in
``gemma_summarizer.py``.
"""
from __future__ import annotations

from typing import Any

from .base import (
    ExternalLink,
    KeyDate,
    KeyFigure,
    KeyPerson,
    SummaryResult,
)

# Allowed deferred-doc tags (FR-SUMM-002 union; matches the deferred_doc_type
# domain in db/models.py minus 'investor_presentation'/'annual_report' aliases
# that we collapse to the FR-SUMM-002 enum).
ALLOWED_DEFERRED_TAGS = frozenset(
    {"earnings", "ppt", "annual_report", "credit_note", "large_misc"}
)

ALLOWED_DATE_CERTAINTY = frozenset({"announced", "expected", "tentative"})


def validate(
    parsed: Any,
    *,
    used_model: str = "",
    used_prompt_version: str = "",
) -> tuple[SummaryResult, list[str]]:
    """Coerce a parsed JSON dict into a ``SummaryResult``.

    Returns ``(result, errors)``. ``result`` is always returned (possibly
    near-empty); ``errors`` is empty iff the envelope was clean.
    """
    errors: list[str] = []

    if parsed is None:
        errors.append("not_a_dict: parsed=None")
        return _empty_result(used_model, used_prompt_version), errors

    if not isinstance(parsed, dict):
        errors.append(f"not_a_dict: type={type(parsed).__name__}")
        return _empty_result(used_model, used_prompt_version), errors

    summary = _coerce_str(parsed.get("summary"), "summary", errors)
    impact = _coerce_str(parsed.get("impact"), "impact", errors, allow_empty=True)

    key_figures = _coerce_key_figures(parsed.get("key_figures"), errors)
    key_people = _coerce_key_people(parsed.get("key_people"), errors)
    key_dates = _coerce_key_dates(parsed.get("key_dates"), errors)

    attachments_referenced = _coerce_str_list(
        parsed.get("attachments_referenced"), "attachments_referenced", errors
    )

    deferred_doc_tags = _coerce_deferred_tags(parsed.get("deferred_doc_tags"), errors)

    external_links = _coerce_external_links(parsed.get("external_links"), errors)

    confidence = _coerce_confidence(parsed.get("confidence"), errors)

    if not summary:
        # The single hard requirement: without a summary the row is useless.
        errors.append("empty_summary")

    return (
        SummaryResult(
            summary=summary,
            impact=impact,
            key_figures=key_figures,
            key_people=key_people,
            key_dates=key_dates,
            attachments_referenced=attachments_referenced,
            deferred_doc_tags=deferred_doc_tags,
            external_links=external_links,
            confidence=confidence,
            used_model=used_model,
            used_prompt_version=used_prompt_version,
        ),
        errors,
    )


def is_fatal(errors: list[str]) -> bool:
    """Decide whether the validator's errors warrant a stricter-prompt retry.

    "Fatal" = ``empty_summary`` or ``not_a_dict``. Other coercions (a single
    bad ``key_figures`` entry, a stray confidence > 1) are tolerable; the
    summary itself is still useful.
    """
    return any(e.startswith(("empty_summary", "not_a_dict")) for e in errors)


# ---------------------------------------------------------------------------
# Coercers
# ---------------------------------------------------------------------------
def _coerce_str(
    value: Any, field: str, errors: list[str], *, allow_empty: bool = False
) -> str:
    if value is None:
        if not allow_empty:
            errors.append(f"missing_field: {field}")
        return ""
    if not isinstance(value, str):
        errors.append(f"non_string_field: {field} type={type(value).__name__}")
        return str(value)
    return value.strip()


def _coerce_str_list(value: Any, field: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"non_list_field: {field} type={type(value).__name__}")
        return []
    out: list[str] = []
    for i, item in enumerate(value):
        if isinstance(item, str):
            s = item.strip()
            if s:
                out.append(s)
        else:
            errors.append(f"non_string_item: {field}[{i}] type={type(item).__name__}")
    return out


def _coerce_key_figures(value: Any, errors: list[str]) -> list[KeyFigure]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"non_list_field: key_figures type={type(value).__name__}")
        return []
    out: list[KeyFigure] = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"key_figures[{i}]: not_a_dict type={type(item).__name__}")
            continue
        label = _safe_strip(item.get("label"))
        # FR-SUMM-003: figures preserved verbatim. We keep numeric values
        # as their string repr to avoid float-formatting drift.
        raw_value = item.get("value")
        unit = _safe_strip(item.get("unit"))
        if raw_value is None or label == "":
            errors.append(f"key_figures[{i}]: missing label or value")
            continue
        out.append(
            KeyFigure(label=label, value=str(raw_value).strip(), unit=unit)
        )
    return out


def _coerce_key_people(value: Any, errors: list[str]) -> list[KeyPerson]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"non_list_field: key_people type={type(value).__name__}")
        return []
    out: list[KeyPerson] = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"key_people[{i}]: not_a_dict type={type(item).__name__}")
            continue
        name = _safe_strip(item.get("name"))
        role = _safe_strip(item.get("role"))
        if not name:
            errors.append(f"key_people[{i}]: missing name")
            continue
        out.append(KeyPerson(name=name, role=role))
    return out


def _coerce_key_dates(value: Any, errors: list[str]) -> list[KeyDate]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"non_list_field: key_dates type={type(value).__name__}")
        return []
    out: list[KeyDate] = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"key_dates[{i}]: not_a_dict type={type(item).__name__}")
            continue
        label = _safe_strip(item.get("label"))
        iso_date = _safe_strip(item.get("iso_date"))
        certainty = _safe_strip(item.get("certainty")) or "announced"
        if certainty not in ALLOWED_DATE_CERTAINTY:
            errors.append(
                f"key_dates[{i}]: bad certainty {certainty!r} -> 'announced'"
            )
            certainty = "announced"
        if not label or not iso_date:
            errors.append(f"key_dates[{i}]: missing label or iso_date")
            continue
        out.append(KeyDate(label=label, iso_date=iso_date, certainty=certainty))
    return out


def _coerce_deferred_tags(value: Any, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"non_list_field: deferred_doc_tags type={type(value).__name__}")
        return []
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            errors.append(f"deferred_doc_tags[{i}]: non_string {type(item).__name__}")
            continue
        tag = item.strip()
        if tag in ALLOWED_DEFERRED_TAGS and tag not in out:
            out.append(tag)
        elif tag:
            errors.append(f"deferred_doc_tags[{i}]: unknown_tag {tag!r}")
    return out


def _coerce_external_links(value: Any, errors: list[str]) -> list[ExternalLink]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"non_list_field: external_links type={type(value).__name__}")
        return []
    out: list[ExternalLink] = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"external_links[{i}]: not_a_dict type={type(item).__name__}")
            continue
        url = _safe_strip(item.get("url"))
        if not url:
            errors.append(f"external_links[{i}]: missing url")
            continue
        referenced_as = _safe_strip(item.get("referenced_as"))
        target_summary = _safe_strip(item.get("target_summary"))
        out.append(
            ExternalLink(
                url=url,
                referenced_as=referenced_as,
                target_summary=target_summary,
            )
        )
    return out


def _coerce_confidence(value: Any, errors: list[str]) -> float:
    if value is None:
        return 0.5
    try:
        f = float(value)
    except (TypeError, ValueError):
        errors.append(f"bad_confidence: {value!r}")
        return 0.5
    if f < 0:
        errors.append(f"bad_confidence: {f} < 0")
        return 0.0
    if f > 1:
        if f <= 100:
            errors.append(f"rescaled_confidence: {f} -> {f/100}")
            return min(1.0, f / 100.0)
        errors.append(f"clamped_confidence: {f} -> 1.0")
        return 1.0
    return f


def _safe_strip(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _empty_result(used_model: str, used_prompt_version: str) -> SummaryResult:
    return SummaryResult(
        summary="",
        impact="",
        key_figures=[],
        key_people=[],
        key_dates=[],
        attachments_referenced=[],
        deferred_doc_tags=[],
        external_links=[],
        confidence=0.0,
        used_model=used_model,
        used_prompt_version=used_prompt_version,
    )


__all__ = [
    "ALLOWED_DATE_CERTAINTY",
    "ALLOWED_DEFERRED_TAGS",
    "is_fatal",
    "validate",
]
