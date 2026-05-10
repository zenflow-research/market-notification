"""Junk-removal (filter) engine contract."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from market_notification.exchange.base import RawNotification


@dataclass(frozen=True)
class FilterMatch:
    rule_id: int
    rule_type: str  # 'category' | 'subcategory' | 'headline_regex' | 'keyword'
    pattern: str
    action: str  # 'hide' | 'block'
    reason: Optional[str]


class FilterEngineBase(ABC):
    """Decides whether a raw notification is junk before any LLM cost."""

    @abstractmethod
    def is_junk(self, raw: RawNotification) -> Optional[FilterMatch]:
        """Return the first matching rule, or None if not junk."""

    @abstractmethod
    def reload_rules(self) -> None:
        """Reload rule set from storage. Called on UI rule edits."""
