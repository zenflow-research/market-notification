"""Summarizer prompt v1 — pinned source of truth for FR-SUMM-002 envelope.

Snapshot semantics
------------------
``PROMPT_VERSION`` and the rendered output of ``build_summarize_prompt()`` form
a contract. The unit test snapshots both; if you intentionally change the
wording or schema you MUST bump ``PROMPT_VERSION`` and update the snapshot.

Two modes
---------
We expose two builders that share the same JSON schema:

* ``build_summarize_prompt`` — full-bodied input (headline + body + extracted
  PDF text). Used for the FR-SUMM-001 happy path.
* ``build_deferred_summarize_prompt`` — headline + metadata only; the model is
  told the document body is intentionally NOT supplied because the row is a
  deferred-doc type (earnings / ppt / annual_report / credit_note / large_misc)
  per FR-ATTACH-004 + FR-SUMM-004. The model still produces the full envelope
  but with the corresponding tag pre-set in ``deferred_doc_tags``.

A third builder ``build_strict_retry_prompt`` exists for the FR-SUMM-002 retry
path: it keeps the same schema but adds an emphatic preamble stressing the
single non-negotiable invariant (a non-empty ``summary``) plus the prior
attempt's error list, so the model can self-correct.

Why FR-SUMM-003 lives in the prompt
-----------------------------------
"Figures preserved exactly" is a *behavioral* contract, not something the
parser can enforce after the fact (we have no source-of-truth to diff
against). It must therefore be steered at generation time: a strong
"copy figures verbatim, do NOT round, do NOT convert units" instruction
plus ``temperature=0.1`` give us the determinism FR-SUMM-003 needs.
"""
from __future__ import annotations

PROMPT_VERSION = "summarize_v1.0-2026-05-09"


# ---------------------------------------------------------------------------
# Schema block — embedded verbatim in every prompt variant.
# ---------------------------------------------------------------------------
SCHEMA_BLOCK = """{
  "summary": "<2-4 sentence neutral factual summary of what the company announced>",
  "impact": "<1-2 sentence read on materiality / shareholder impact; empty string if unclear>",
  "key_figures": [
    {"label": "<what the figure represents>", "value": "<verbatim from source>", "unit": "<INR Cr | % | units | tonnes/day | MW | ...>"}
  ],
  "key_people": [
    {"name": "<full name as written>", "role": "<CEO | CFO | director | allottee | ...>"}
  ],
  "key_dates": [
    {"label": "<commissioning | record_date | AGM | ex_date | ...>", "iso_date": "<YYYY-MM-DD>", "certainty": "<announced | expected | tentative>"}
  ],
  "attachments_referenced": ["<filename or descriptive label>"],
  "deferred_doc_tags": [],
  "external_links": [
    {"url": "<full URL>", "referenced_as": "<how the document refers to it>", "target_summary": "<empty string if not followed>"}
  ],
  "confidence": <float between 0 and 1>
}"""


SHARED_RULES = """Rules:
1. Output exactly ONE JSON object — no markdown fences, no commentary, no extra keys.
2. The `summary` field is REQUIRED and MUST be 2-4 sentences. Do not leave it empty.
3. PRESERVE FIGURES VERBATIM. Copy every numeric value, percentage, rupee
   amount, and date EXACTLY as written in the source. Do NOT round, do NOT
   convert units (INR Cr stays INR Cr, do not change to USD), do NOT
   paraphrase. Symbols like ₹, %, "Cr", "Lakh" stay attached to their value.
4. Every field is REQUIRED. For lists with no entries, return `[]`.
5. `deferred_doc_tags` may contain any subset of:
   ['earnings', 'ppt', 'annual_report', 'credit_note', 'large_misc'].
   Use 'earnings' for quarterly/annual results announcements; 'ppt' for
   investor presentations; 'annual_report' for the AR document;
   'credit_note' for credit-rating commentary; 'large_misc' for catch-all
   long-form documents (>20 pages) outside the other tags.
6. `confidence` is your subjective probability that the summary faithfully
   captures the announcement. Use 0.0 only when you cannot summarize at all.
7. Respond with ONLY the JSON object. No explanation."""


SYSTEM_PROMPT_TEMPLATE = """You are a financial analyst summarizing corporate notifications from Indian stock exchanges (BSE / NSE) for a buy-side research workflow.

Output schema (verbatim — every field required):
{schema_block}

{shared_rules}"""


USER_PROMPT_TEMPLATE = """Source: {source}
Headline: {headline}
Exchange category: {category}
Exchange subcategory: {subcategory}
AI category: {ai_category} ({ai_category_group})
Priority: {ai_priority} (score={ai_priority_score})

Body:
{body}

Extracted PDF text (may be empty):
{pdf_text}"""


DEFERRED_SYSTEM_PROMPT_TEMPLATE = """You are a financial analyst summarizing corporate notifications from Indian stock exchanges (BSE / NSE).

This notification refers to a {deferred_kind} document. The document body has been INTENTIONALLY withheld; a separate downstream pipeline handles deferred-doc bodies. Produce the envelope below from the headline + exchange metadata only.

Output schema (verbatim — every field required):
{schema_block}

Additional rules for deferred-doc notifications:
- `deferred_doc_tags` MUST contain at least the tag {deferred_tag!r}.
- `summary` describes WHAT the announcement is (e.g. "Quarterly results
  intimation for Q3FY26") rather than the document contents.
- `key_figures` may be empty if no figures appear in the headline itself.
- Do NOT speculate about content you have not been shown.

{shared_rules}"""


DEFERRED_USER_PROMPT_TEMPLATE = """Source: {source}
Headline: {headline}
Exchange category: {category}
Exchange subcategory: {subcategory}
AI category: {ai_category} ({ai_category_group})
Priority: {ai_priority} (score={ai_priority_score})
Deferred document type: {deferred_kind}

(Document body intentionally withheld — see system prompt.)"""


STRICT_RETRY_PREFIX = """The previous attempt produced output that failed validation. Do NOT repeat the mistake.

Validator errors from the previous attempt:
{error_block}

Critical reminder:
- The `summary` field MUST be a non-empty 2-4 sentence string. This is the single most important constraint.
- Return ONLY the JSON object. No prose, no fences, no extra keys.

Now retry the task below.

---

"""


# Map the orchestrator's deferred_doc_type values onto the FR-SUMM-002 tag enum
# and a human-readable kind label for the prompt.
_DEFERRED_KIND_BY_TYPE: dict[str, tuple[str, str]] = {
    "earnings": ("earnings", "quarterly/annual earnings"),
    "ppt": ("ppt", "investor presentation"),
    "investor_presentation": ("ppt", "investor presentation"),
    "annual_report": ("annual_report", "annual report"),
    "credit_note": ("credit_note", "credit rating note"),
    "large_misc": ("large_misc", "large miscellaneous"),
}


def render_system_prompt() -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        schema_block=SCHEMA_BLOCK,
        shared_rules=SHARED_RULES,
    )


def render_user_prompt(
    *,
    source: str,
    headline: str,
    category: str | None,
    subcategory: str | None,
    ai_category: str | None,
    ai_category_group: str | None,
    ai_priority: str | None,
    ai_priority_score: int | None,
    body: str | None,
    pdf_text: str | None,
    body_max_chars: int = 1500,
    pdf_text_max_chars: int = 6000,
) -> str:
    body_text = _truncate(body, body_max_chars) or "(no body)"
    pdf_text_text = _truncate(pdf_text, pdf_text_max_chars) or "(none)"
    return USER_PROMPT_TEMPLATE.format(
        source=source,
        headline=(headline or "").strip(),
        category=(category or "").strip() or "(none)",
        subcategory=(subcategory or "").strip() or "(none)",
        ai_category=(ai_category or "").strip() or "(none)",
        ai_category_group=(ai_category_group or "").strip() or "(none)",
        ai_priority=(ai_priority or "").strip() or "(none)",
        ai_priority_score=(
            str(ai_priority_score) if ai_priority_score is not None else "n/a"
        ),
        body=body_text,
        pdf_text=pdf_text_text,
    )


def build_summarize_prompt(
    *,
    source: str,
    headline: str,
    category: str | None,
    subcategory: str | None,
    ai_category: str | None,
    ai_category_group: str | None,
    ai_priority: str | None,
    ai_priority_score: int | None,
    body: str | None,
    pdf_text: str | None,
    body_max_chars: int = 1500,
    pdf_text_max_chars: int = 6000,
) -> tuple[str, str]:
    """Return ``(system, user)`` for the full-body summarize path."""
    return (
        render_system_prompt(),
        render_user_prompt(
            source=source,
            headline=headline,
            category=category,
            subcategory=subcategory,
            ai_category=ai_category,
            ai_category_group=ai_category_group,
            ai_priority=ai_priority,
            ai_priority_score=ai_priority_score,
            body=body,
            pdf_text=pdf_text,
            body_max_chars=body_max_chars,
            pdf_text_max_chars=pdf_text_max_chars,
        ),
    )


def build_deferred_summarize_prompt(
    *,
    source: str,
    headline: str,
    category: str | None,
    subcategory: str | None,
    ai_category: str | None,
    ai_category_group: str | None,
    ai_priority: str | None,
    ai_priority_score: int | None,
    deferred_doc_type: str,
) -> tuple[str, str]:
    """Return ``(system, user)`` for a deferred-doc notification.

    ``deferred_doc_type`` comes from ``Notification.deferred_doc_type`` set
    in Phase 7. We collapse synonyms ('investor_presentation' -> 'ppt') so
    the FR-SUMM-002 enum is honored.
    """
    tag, kind_label = _DEFERRED_KIND_BY_TYPE.get(
        deferred_doc_type, ("large_misc", deferred_doc_type or "deferred document")
    )
    system = DEFERRED_SYSTEM_PROMPT_TEMPLATE.format(
        schema_block=SCHEMA_BLOCK,
        shared_rules=SHARED_RULES,
        deferred_kind=kind_label,
        deferred_tag=tag,
    )
    user = DEFERRED_USER_PROMPT_TEMPLATE.format(
        source=source,
        headline=(headline or "").strip(),
        category=(category or "").strip() or "(none)",
        subcategory=(subcategory or "").strip() or "(none)",
        ai_category=(ai_category or "").strip() or "(none)",
        ai_category_group=(ai_category_group or "").strip() or "(none)",
        ai_priority=(ai_priority or "").strip() or "(none)",
        ai_priority_score=(
            str(ai_priority_score) if ai_priority_score is not None else "n/a"
        ),
        deferred_kind=kind_label,
    )
    return system, user


def build_strict_retry_prompt(
    *,
    base_user_prompt: str,
    prior_errors: list[str],
) -> str:
    """Return a stricter user prompt with the prior attempt's errors prepended.

    The system prompt is left unchanged — schema rules don't change between
    attempts, but the *user* message gets a reinforcement preamble so the
    model sees its own mistakes inside the immediate context window.
    """
    error_block = "\n".join(f"  - {e}" for e in prior_errors[:10]) or "  (none)"
    prefix = STRICT_RETRY_PREFIX.format(error_block=error_block)
    return prefix + base_user_prompt


def metadata() -> dict[str, str]:
    return {"prompt_version": PROMPT_VERSION}


def deferred_tag_for(deferred_doc_type: str | None) -> str | None:
    """Public helper: map ``Notification.deferred_doc_type`` -> FR-SUMM-002 tag."""
    if not deferred_doc_type:
        return None
    pair = _DEFERRED_KIND_BY_TYPE.get(deferred_doc_type)
    return pair[0] if pair else "large_misc"


def _truncate(text: str | None, limit: int) -> str:
    if not text:
        return ""
    s = text.strip()
    if len(s) > limit:
        return s[:limit] + " ...[truncated]"
    return s


__all__ = [
    "PROMPT_VERSION",
    "build_deferred_summarize_prompt",
    "build_strict_retry_prompt",
    "build_summarize_prompt",
    "deferred_tag_for",
    "metadata",
    "render_system_prompt",
    "render_user_prompt",
]
