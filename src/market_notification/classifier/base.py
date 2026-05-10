"""Classifier contract. Maps notification text to a (category, group)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ClassificationResult:
    category: str  # one of TAXONOMY categories
    group: str  # derived from CATEGORY_TO_GROUP
    confidence: float  # 0..1
    reasoning: str  # short rationale (audit + UI)
    used_taxonomy_version: str
    used_prompt_version: str
    source: str  # 'gemma' | 'regex' | 'fallback'


class Classifier(ABC):
    """Public contract for any classifier (LLM, regex, ensemble)."""

    @abstractmethod
    def classify(self, notification_id: int) -> ClassificationResult: ...
