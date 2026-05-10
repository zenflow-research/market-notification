"""Priority-override prompt v1.

Per design-decision F2 / D-11: Gemma fully overrides the deterministic
verdict; the rubric is only a starting hint. The prompt makes that
explicit so the model doesn't anchor too hard on the deterministic
bucket.
"""
from __future__ import annotations

PROMPT_VERSION = "priority_override_v1.0-2026-05-07"


SYSTEM_PROMPT = """You are a financial analyst deciding the user-facing priority of an Indian-market corporate notification.

Bucket definitions:
- "important": material impact on stock price, financials, or ownership
  (acquisitions, large order wins, fundraises, splits/bonus, results,
  CEO/CFO change, regulatory penalty, credit rating action).
- "medium": noteworthy but not immediately price-moving
  (board-meeting dates, dividends, new plants, JVs/MOUs, analyst meets).
- "normal": routine/administrative, no material impact
  (most compliance filings, LODR updates, XBRL, book closure).
- "ignored": junk / pure boilerplate (newspaper-publication copies,
  trading-window closures, postal-ballot scrutinizer reports).

A deterministic engine has already produced a verdict. You may CONFIRM,
UPGRADE, or DOWNGRADE — pick whichever is correct given the full context.
You are NOT required to defer to the deterministic verdict; it is only
a starting hint.

Output: a single JSON object — no markdown, no commentary.
{
  "priority": "<important | medium | normal | ignored>",
  "reasoning": "<one short sentence explaining your decision>",
  "confidence": <float between 0 and 1>
}"""


USER_PROMPT_TEMPLATE = """Source: {source}
Headline: {headline}
ai_category: {ai_category} ({ai_category_group})

Deterministic verdict: {det_bucket} (score {det_score})
Deterministic reasoning:
{det_reasons}

Body excerpt:
{body}

Gemma summary (may be empty if not yet summarized):
{summary}
Gemma impact (may be empty):
{impact}"""


def render(
    *,
    source: str,
    headline: str,
    ai_category: str,
    ai_category_group: str,
    det_bucket: str,
    det_score: int,
    det_reasons: list[str],
    body: str,
    summary: str,
    impact: str,
    body_max_chars: int = 1500,
) -> tuple[str, str]:
    body_text = (body or "").strip()
    if len(body_text) > body_max_chars:
        body_text = body_text[:body_max_chars] + " ...[truncated]"
    if not body_text:
        body_text = "(no body)"
    user = USER_PROMPT_TEMPLATE.format(
        source=source,
        headline=(headline or "").strip(),
        ai_category=ai_category,
        ai_category_group=ai_category_group,
        det_bucket=det_bucket,
        det_score=det_score,
        det_reasons="\n".join(f"  - {r}" for r in det_reasons[:8]) or "  (none)",
        body=body_text,
        summary=(summary or "(none)").strip(),
        impact=(impact or "(none)").strip(),
    )
    return SYSTEM_PROMPT, user


__all__ = ["PROMPT_VERSION", "SYSTEM_PROMPT", "render"]
