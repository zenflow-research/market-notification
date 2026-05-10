"""Regex/keyword junk-filter engine (Phase 4, FR-FILTER-001..006).

Design
------
- Rules live in `notification_filter_rules` (DB table) and are mirrored from
  `config/filter_rules.json` (versioned source-of-truth) at first run via
  `scripts/seed_filter_rules.py`. The engine reads from the DB at construction
  time so UI-driven edits are reflected on next `reload_rules()`.
- Four rule types (matches brain semantics, see brain/exchange_util/notification_poller.py):
    `category`        — case-insensitive equality vs raw.category
    `subcategory`     — case-insensitive equality vs raw.subcategory
    `headline_regex`  — `re.search(pattern, headline, IGNORECASE)`
    `keyword`         — case-insensitive substring on headline
- Source-scoped: a rule with `source='BSE'` only applies to BSE rows; `None`
  applies to both.
- Compiled-regex cache is built once per `reload_rules()`; `is_junk()` is
  pure CPU and the perf budget (NFR-PERF-001) is >=1000 rows/sec/core.
- First match wins; FilterMatch carries the rule id so the poller can store
  it in `notifications.junk_rule_id` for audit and UI un-ignore.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from ..exchange.base import RawNotification
from .base import FilterEngineBase, FilterMatch

logger = logging.getLogger(__name__)


# Allowed rule_type values (matches schema check expectations downstream).
_VALID_RULE_TYPES = frozenset({"category", "subcategory", "headline_regex", "keyword"})


@dataclass(frozen=True)
class _CompiledRule:
    rule_id: int
    rule_type: str
    pattern: str
    pattern_lower: str            # for fast equality / substring
    regex: Optional[re.Pattern]   # only for headline_regex
    source: Optional[str]         # 'BSE' | 'NSE' | None
    action: str
    reason: Optional[str]


class RegexFilterEngine(FilterEngineBase):
    """In-memory rule evaluator.

    Args:
        session_factory: callable returning a `Session` context manager. Used
            on each `reload_rules()` to read the active rule set. Decoupling
            from `get_session` keeps the engine testable.
    """

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory
        self._rules: list[_CompiledRule] = []
        self.reload_rules()

    # ------------------------------------------------------------------
    # Public surface (FilterEngineBase)
    # ------------------------------------------------------------------
    def is_junk(self, raw: RawNotification) -> Optional[FilterMatch]:
        """Return the first matching rule, or None if no rule fires."""
        if not self._rules:
            return None

        category = (raw.category or "").strip().lower()
        subcategory = (raw.subcategory or "").strip().lower()
        headline = (raw.headline or "").strip()
        headline_lower = headline.lower()
        source = raw.source

        for rule in self._rules:
            if rule.source is not None and rule.source != source:
                continue
            if rule.rule_type == "category":
                if category and category == rule.pattern_lower:
                    return self._to_match(rule)
            elif rule.rule_type == "subcategory":
                if subcategory and subcategory == rule.pattern_lower:
                    return self._to_match(rule)
            elif rule.rule_type == "keyword":
                if headline_lower and rule.pattern_lower in headline_lower:
                    return self._to_match(rule)
            elif rule.rule_type == "headline_regex":
                if rule.regex is not None and headline and rule.regex.search(headline):
                    return self._to_match(rule)
            # unknown rule_type silently skipped (defensive)
        return None

    def reload_rules(self) -> None:
        """Refresh the compiled rule cache from the DB."""
        from ..db.models import NotificationFilterRule

        compiled: list[_CompiledRule] = []
        with self._session_factory() as sess:
            rows = (
                sess.query(NotificationFilterRule)
                .filter(NotificationFilterRule.is_active == 1)
                .order_by(NotificationFilterRule.id)
                .all()
            )
            for r in rows:
                rule = _compile(
                    rule_id=r.id,
                    rule_type=r.rule_type,
                    pattern=r.pattern,
                    source=r.source,
                    action=r.action or "hide",
                    reason=r.reason,
                )
                if rule is not None:
                    compiled.append(rule)
        self._rules = compiled
        logger.info("FilterEngine loaded %d active rules", len(self._rules))

    # ------------------------------------------------------------------
    # Introspection (used by tests + UI/Health page)
    # ------------------------------------------------------------------
    @property
    def rules(self) -> list[_CompiledRule]:
        return list(self._rules)

    def __len__(self) -> int:
        return len(self._rules)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    @staticmethod
    def _to_match(rule: _CompiledRule) -> FilterMatch:
        return FilterMatch(
            rule_id=rule.rule_id,
            rule_type=rule.rule_type,
            pattern=rule.pattern,
            action=rule.action,
            reason=rule.reason,
        )


def _compile(
    rule_id: int,
    rule_type: str,
    pattern: str,
    source: Optional[str],
    action: str,
    reason: Optional[str],
) -> Optional[_CompiledRule]:
    """Compile a single rule. Returns None if invalid (logged + skipped)."""
    rt = (rule_type or "").strip()
    if rt not in _VALID_RULE_TYPES:
        logger.warning("FilterEngine: skipping rule %d with unknown type %r", rule_id, rule_type)
        return None
    if not pattern:
        logger.warning("FilterEngine: skipping rule %d with empty pattern", rule_id)
        return None

    regex: Optional[re.Pattern] = None
    if rt == "headline_regex":
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            logger.warning("FilterEngine: invalid regex on rule %d (%s): %s", rule_id, pattern, e)
            return None

    return _CompiledRule(
        rule_id=rule_id,
        rule_type=rt,
        pattern=pattern,
        pattern_lower=pattern.strip().lower(),
        regex=regex,
        source=source,
        action=action,
        reason=reason,
    )
