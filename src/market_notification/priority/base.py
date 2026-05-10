"""Priority engine contracts. Two-pass: deterministic then LLM override."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from market_notification.companies.base import CompanyDTO


@dataclass(frozen=True)
class NotificationPriorityInput:
    notification_id: int
    headline: str
    body: str
    pdf_text: str
    ai_category: str
    ai_category_group: str


@dataclass(frozen=True)
class PriorityResult:
    bucket: str  # 'important' | 'medium' | 'normal' | 'ignored'
    score: int  # 0..100
    reasons: list[str] = field(default_factory=list)
    source: str = "deterministic"  # 'deterministic' | 'llm_override'
    extracted_amount_cr: float | None = None


class DeterministicPriority(ABC):
    """Cheap, no-LLM priority. Uses category default + threshold rules."""

    @abstractmethod
    def score(
        self, inp: NotificationPriorityInput, company: CompanyDTO | None
    ) -> PriorityResult: ...


class LlmPriorityOverride(ABC):
    """LLM gets the deterministic verdict + summary; can upgrade or downgrade.

    Per design-decisions.md F2: Gemma fully overrides; rubric is a starting hint.
    """

    @abstractmethod
    def override(
        self,
        inp: NotificationPriorityInput,
        deterministic: PriorityResult,
        gemma_summary: str,
        gemma_impact: str,
    ) -> PriorityResult: ...
