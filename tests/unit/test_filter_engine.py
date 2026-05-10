"""Unit tests for RegexFilterEngine (Phase 4 / FR-FILTER-001..006).

Covers:
  - All four rule types: category, subcategory, headline_regex, keyword.
  - Source scoping (BSE-only rule does not match NSE row, etc.).
  - Misses return None.
  - Active-flag respected: deactivated rules don't fire.
  - Invalid regex skipped (warning logged, engine still loads).
  - Reload picks up new rules.
  - Performance: >=1000 rows/sec with the seeded rule set (NFR-PERF-001).
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime

import pytest

from market_notification.db.models import NotificationFilterRule
from market_notification.exchange.base import RawNotification
from market_notification.filter.filter_engine import RegexFilterEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _raw(
    *,
    source: str = "BSE",
    headline: str = "Some announcement",
    category: str | None = None,
    subcategory: str | None = None,
) -> RawNotification:
    return RawNotification(
        source=source,
        seq_id=None,
        headline=headline,
        category=category,
        subcategory=subcategory,
        body=None,
        announced_at=datetime(2026, 5, 7, 10, 30, 0),
        exchange_disseminated_at=None,
        attachment_url=None,
        attachment_name=None,
        attachment_size=None,
        is_critical=False,
        has_xbrl=False,
        symbol=None,
        company_name_raw=None,
        isin=None,
        industry_raw=None,
        raw_json="{}",
    )


def _add(session, rule_type, pattern, *, source=None, action="hide",
         created_by="system", reason=None, is_active=1):
    row = NotificationFilterRule(
        rule_type=rule_type,
        pattern=pattern,
        source=source,
        action=action,
        created_by=created_by,
        reason=reason,
        is_active=is_active,
    )
    session.add(row)
    session.flush()
    return row.id


@pytest.fixture()
def engine_factory(in_memory_session):
    """Returns a callable that builds a RegexFilterEngine bound to the test session."""

    @contextmanager
    def _sf():
        yield in_memory_session

    def _build():
        return RegexFilterEngine(_sf)

    return _build, in_memory_session


# ---------------------------------------------------------------------------
# Per-rule-type matching
# ---------------------------------------------------------------------------
class TestRuleTypes:
    def test_category_exact_match(self, engine_factory):
        build, sess = engine_factory
        rid = _add(sess, "category", "Closure of Trading Window")
        eng = build()
        m = eng.is_junk(_raw(category="Closure of Trading Window"))
        assert m is not None
        assert m.rule_id == rid
        assert m.rule_type == "category"
        assert m.action == "hide"

    def test_category_match_is_case_insensitive(self, engine_factory):
        build, sess = engine_factory
        _add(sess, "category", "Closure of Trading Window")
        eng = build()
        m = eng.is_junk(_raw(category="closure of TRADING window"))
        assert m is not None

    def test_subcategory_match(self, engine_factory):
        build, sess = engine_factory
        rid = _add(sess, "subcategory", "Closure of Trading Window")
        eng = build()
        m = eng.is_junk(_raw(subcategory="Closure of Trading Window"))
        assert m is not None
        assert m.rule_id == rid

    def test_keyword_substring_on_headline(self, engine_factory):
        build, sess = engine_factory
        rid = _add(sess, "keyword", "Trading Window closure")
        eng = build()
        m = eng.is_junk(_raw(headline="Intimation regarding Trading Window closure for Q4"))
        assert m is not None
        assert m.rule_id == rid

    def test_headline_regex_match(self, engine_factory):
        build, sess = engine_factory
        _add(sess, "headline_regex", r"newspaper\s+(advertisement|publication)")
        eng = build()
        assert eng.is_junk(_raw(headline="Newspaper publication of unaudited results")) is not None
        assert eng.is_junk(_raw(headline="Newspaper Advertisement")) is not None

    def test_no_match_returns_none(self, engine_factory):
        build, sess = engine_factory
        _add(sess, "category", "Closure of Trading Window")
        eng = build()
        assert eng.is_junk(_raw(category="Outcome of Board Meeting")) is None

    def test_empty_field_does_not_match_empty_pattern(self, engine_factory):
        """Defensive: a row with no category should not accidentally match
        an exact-match category rule with a non-empty pattern."""
        build, sess = engine_factory
        _add(sess, "category", "Closure of Trading Window")
        eng = build()
        assert eng.is_junk(_raw(category=None)) is None
        assert eng.is_junk(_raw(category="")) is None


# ---------------------------------------------------------------------------
# Source scoping
# ---------------------------------------------------------------------------
class TestSourceScope:
    def test_universal_rule_matches_both_sources(self, engine_factory):
        build, sess = engine_factory
        _add(sess, "category", "Closure of Trading Window", source=None)
        eng = build()
        assert eng.is_junk(_raw(source="BSE", category="Closure of Trading Window")) is not None
        assert eng.is_junk(_raw(source="NSE", category="Closure of Trading Window")) is not None

    def test_nse_only_rule_skips_bse(self, engine_factory):
        build, sess = engine_factory
        _add(sess, "category", "Trading Window", source="NSE")
        eng = build()
        assert eng.is_junk(_raw(source="BSE", category="Trading Window")) is None
        assert eng.is_junk(_raw(source="NSE", category="Trading Window")) is not None


# ---------------------------------------------------------------------------
# Active-flag and reload
# ---------------------------------------------------------------------------
class TestLifecycle:
    def test_inactive_rules_are_not_loaded(self, engine_factory):
        build, sess = engine_factory
        _add(sess, "category", "Closure of Trading Window", is_active=0)
        eng = build()
        assert len(eng) == 0
        assert eng.is_junk(_raw(category="Closure of Trading Window")) is None

    def test_reload_picks_up_new_rule(self, engine_factory):
        build, sess = engine_factory
        eng = build()
        assert len(eng) == 0

        _add(sess, "category", "Newspaper Publication")
        eng.reload_rules()
        assert len(eng) == 1
        assert eng.is_junk(_raw(category="Newspaper Publication")) is not None

    def test_invalid_regex_skipped_not_raised(self, engine_factory):
        build, sess = engine_factory
        _add(sess, "headline_regex", "(unclosed")  # bad pattern
        _add(sess, "category", "Newspaper Publication")
        eng = build()
        # Engine still loaded the valid rule, dropped the broken one
        assert len(eng) == 1
        assert eng.is_junk(_raw(category="Newspaper Publication")) is not None


# ---------------------------------------------------------------------------
# Performance (NFR-PERF-001)
# ---------------------------------------------------------------------------
class TestPerformance:
    @pytest.mark.perf
    def test_processes_1000_rows_under_one_second(self, engine_factory):
        """Benchmark: with a realistic seed rule set (~14 rules), the engine
        must classify >=1000 notifications in <1.0s on a single thread."""
        build, sess = engine_factory
        # Mirror production seed set, mix of all 4 rule types
        _add(sess, "category", "Closure of Trading Window")
        _add(sess, "category", "Opening of Trading Window")
        _add(sess, "category", "Trading Window", source="NSE")
        _add(sess, "subcategory", "Closure of Trading Window")
        _add(sess, "category", "Newspaper Publication")
        _add(sess, "category", "Loss of Share Certificate")
        _add(sess, "category", "Duplicate Certificate")
        _add(sess, "category", "AGM/EGM")
        _add(sess, "keyword", "Trading Window closure")
        _add(sess, "headline_regex", r"newspaper\s+(advertisement|publication|notice)")
        _add(sess, "headline_regex", r"loss\s+of\s+share\s+certificate")
        _add(sess, "headline_regex", r"issue\s+of\s+duplicate\s+share\s+certificate")
        eng = build()

        # Mix: half match, half don't, varied sources
        rows = []
        for i in range(500):
            rows.append(_raw(source="BSE", category="Outcome of Board Meeting",
                             headline=f"Quarterly result FY26 batch {i}"))
            rows.append(_raw(source="NSE", category="Trading Window",
                             headline=f"Trading Window closure intimation #{i}"))

        start = time.perf_counter()
        flagged = sum(1 for r in rows if eng.is_junk(r) is not None)
        elapsed = time.perf_counter() - start

        # Sanity: half should match
        assert flagged == 500
        rate = len(rows) / elapsed
        assert elapsed < 1.0, f"Filter took {elapsed:.3f}s for 1000 rows ({rate:.0f}/s)"
