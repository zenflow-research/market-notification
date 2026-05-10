"""Classifier prompt v1 — pinned source of truth.

Snapshot semantics
------------------
The `PROMPT_VERSION` and `build_classify_prompt()` output together form a contract.
The unit test `tests/unit/test_classifier_prompt.py` snapshots the rendered
system+user prompt; if you intentionally change the wording or schema you MUST
bump `PROMPT_VERSION` and update the snapshot.

Why a fixed schema
------------------
The model is asked to emit a single JSON object with three keys: `ai_category`,
`ai_category_group`, `confidence`. We deliberately avoid a "reasoning" field in
the JSON to keep the response short and parseable; reasoning is captured as a
separate plain-text trailer when present, but is optional and does not affect
classification correctness.
"""
from __future__ import annotations

from ..taxonomy import (
    TAXONOMY_VERSION,
    UNCATEGORIZED,
    UNCATEGORIZED_GROUP,
    taxonomy_as_text,
)

PROMPT_VERSION = "classify_v1.1-2026-05-07"


SYSTEM_PROMPT_TEMPLATE = """You are a financial analyst classifying corporate notifications from Indian stock exchanges (BSE / NSE).

Pick exactly ONE category. Return a single JSON object — no markdown fences,
no commentary, no extra keys.

Available categories (group -> categories):
{taxonomy_block}

Output schema (verbatim):
{{
  "ai_category": "<one category from the list>",
  "ai_category_group": "<the group that contains the chosen category>",
  "confidence": <float between 0 and 1>
}}

Rules:
1. The `ai_category` MUST be one of the names listed above, copied exactly.
2. The `ai_category_group` MUST be the group that contains the chosen category.
3. Routine compliance / administrative filings (newspaper publications, share-
   certificate matters, regulation-30/LODR boilerplate, secretarial compliance
   reports, generic "Press Release" / "General Updates" / "Company Update"
   intimations, trading-window closures, book-closure notices, postal ballot
   filings, code-of-conduct disclosures, structural digital database) MUST be
   classified as:
     "ai_category": "Compliance Filing"
     "ai_category_group": "Other"
4. Use:
     "ai_category": "{uncategorized}"
     "ai_category_group": "{uncategorized_group}"
   ONLY when the notification is too garbled to interpret. Do NOT use
   "{uncategorized}" for routine compliance items — those go to "Compliance Filing".
5. `confidence` is your subjective probability that the chosen category is correct.
6. Respond with ONLY the JSON object. No explanation."""


USER_PROMPT_TEMPLATE = """Source: {source}
Headline: {headline}
Exchange category: {category}
Exchange subcategory: {subcategory}
Body excerpt:
{body}"""


# Exclude only the "Other" group's prompt-facing categories — brain found this
# steers the model toward real categories rather than the catch-all bucket.
# `Uncategorized` is reachable via the explicit fallback rule, not the listing.
_PROMPT_SKIP_GROUPS = frozenset({"Other"})


def render_system_prompt() -> str:
    """Render the system prompt with the current taxonomy embedded."""
    return SYSTEM_PROMPT_TEMPLATE.format(
        taxonomy_block=taxonomy_as_text(skip_groups=_PROMPT_SKIP_GROUPS),
        uncategorized=UNCATEGORIZED,
        uncategorized_group=UNCATEGORIZED_GROUP,
    )


def render_user_prompt(
    *,
    source: str,
    headline: str,
    category: str | None,
    subcategory: str | None,
    body: str | None,
    body_max_chars: int = 1500,
) -> str:
    """Render the user message that the model classifies."""
    body_text = (body or "").strip()
    if len(body_text) > body_max_chars:
        body_text = body_text[:body_max_chars] + " ...[truncated]"
    if not body_text:
        body_text = "(no body)"
    return USER_PROMPT_TEMPLATE.format(
        source=source,
        headline=(headline or "").strip(),
        category=(category or "").strip() or "(none)",
        subcategory=(subcategory or "").strip() or "(none)",
        body=body_text,
    )


def build_classify_prompt(
    *,
    source: str,
    headline: str,
    category: str | None,
    subcategory: str | None,
    body: str | None,
    body_max_chars: int = 1500,
) -> tuple[str, str]:
    """Return a (system, user) prompt pair ready for Ollama chat."""
    return (
        render_system_prompt(),
        render_user_prompt(
            source=source,
            headline=headline,
            category=category,
            subcategory=subcategory,
            body=body,
            body_max_chars=body_max_chars,
        ),
    )


def metadata() -> dict[str, str]:
    """Versions stamped into every classification row."""
    return {
        "prompt_version": PROMPT_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
    }


__all__ = [
    "PROMPT_VERSION",
    "build_classify_prompt",
    "render_system_prompt",
    "render_user_prompt",
    "metadata",
]
