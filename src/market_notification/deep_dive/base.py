"""gemini-rr deep-dive contract.

Per I2: only certain categories are eligible (auto for important).
Per I4: produces both structured JSON and prose narrative.
Per I6: one big call per notification with full topic prompt + all fundamentals.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DeepDiveResult:
    structured_json: str  # raw JSON returned by the model (validated separately)
    prose: str  # narrative text
    sector_kpi_findings: list[str] = field(default_factory=list)
    used_prompt_version: str = ""
    cache_hit: bool = False
    profile_used: str = ""  # which gemini profile served the call
    latency_s: float = 0.0
    error: str | None = None


class DeepDive(ABC):
    @abstractmethod
    def deep_dive(self, notification_id: int) -> DeepDiveResult: ...


class PromptBuilder(ABC):
    """Constructs the per-category prompt with sector KPIs + full fundamentals."""

    @abstractmethod
    def build(self, notification_id: int, category: str) -> str: ...
