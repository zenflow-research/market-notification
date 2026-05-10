"""Unit tests for `poller.cross_exchange`.

Pure-function tests -- no DB. Verifies:
  - Cosine similarity is symmetric, between 0 and 1.
  - find_match honors company_id, opposite-source, time-window, and threshold.
  - assign_role mints group ids and applies the right role.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from market_notification.poller.cross_exchange import (
    DEFAULT_SIMILARITY_THRESHOLD,
    DEFAULT_WINDOW_MINUTES,
    assign_role,
    cosine_similarity,
    find_match,
)


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------

class TestCosine:
    def test_identical(self):
        assert cosine_similarity("hello world", "hello world") == pytest.approx(1.0)

    def test_disjoint(self):
        assert cosine_similarity("hello world", "foo bar") == 0.0

    def test_partial(self):
        s = cosine_similarity("foo bar baz", "foo bar")
        assert 0.0 < s < 1.0

    def test_case_insensitive(self):
        assert cosine_similarity("HELLO World", "hello world") == pytest.approx(1.0)

    def test_punctuation_stripped(self):
        assert cosine_similarity("Hello, World!", "hello world") == pytest.approx(1.0)

    def test_empty(self):
        assert cosine_similarity("", "anything") == 0.0
        assert cosine_similarity("anything", "") == 0.0
        assert cosine_similarity("", "") == 0.0

    def test_real_world_pair(self):
        """Realistic BSE+NSE same-event headlines."""
        bse = "Outcome of Board Meeting held on May 07, 2026"
        nse = "Outcome of Board Meeting held on 07-May-2026"
        assert cosine_similarity(bse, nse) >= DEFAULT_SIMILARITY_THRESHOLD


# ---------------------------------------------------------------------------
# find_match
# ---------------------------------------------------------------------------

@pytest.fixture
def base_time():
    return datetime(2026, 5, 7, 10, 30, 0)


def _row(source, company_id, headline, t, group_id=None):
    return {
        "source": source,
        "company_id": company_id,
        "headline": headline,
        "announced_at": t,
        "cross_exchange_group_id": group_id,
    }


class TestFindMatch:
    def test_no_existing_returns_none(self, base_time):
        candidate = _row("BSE", 11, "Outcome of Board Meeting", base_time)
        assert find_match(candidate, []) is None

    def test_match_within_window(self, base_time):
        candidate = _row("BSE", 11, "Outcome of Board Meeting on May 07", base_time)
        existing = [
            _row("NSE", 11, "Outcome of Board Meeting on May 07",
                 base_time - timedelta(minutes=2))
        ]
        m = find_match(candidate, existing)
        assert m is not None
        assert m["source"] == "NSE"

    def test_outside_window_does_not_match(self, base_time):
        candidate = _row("BSE", 11, "Outcome of Board Meeting on May 07", base_time)
        existing = [
            _row("NSE", 11, "Outcome of Board Meeting on May 07",
                 base_time - timedelta(minutes=DEFAULT_WINDOW_MINUTES + 1))
        ]
        assert find_match(candidate, existing) is None

    def test_same_source_does_not_match(self, base_time):
        candidate = _row("BSE", 11, "Outcome of Board Meeting", base_time)
        existing = [
            _row("BSE", 11, "Outcome of Board Meeting",
                 base_time - timedelta(minutes=2))
        ]
        assert find_match(candidate, existing) is None

    def test_different_company_does_not_match(self, base_time):
        candidate = _row("BSE", 11, "Outcome of Board Meeting", base_time)
        existing = [
            _row("NSE", 22, "Outcome of Board Meeting",
                 base_time - timedelta(minutes=2))
        ]
        assert find_match(candidate, existing) is None

    def test_low_similarity_does_not_match(self, base_time):
        candidate = _row("BSE", 11, "Outcome of Board Meeting", base_time)
        existing = [
            _row("NSE", 11, "Newspaper publication regarding CSR",
                 base_time - timedelta(minutes=2))
        ]
        assert find_match(candidate, existing) is None


# ---------------------------------------------------------------------------
# assign_role
# ---------------------------------------------------------------------------

class TestAssignRole:
    def test_no_match_is_primary(self):
        gid, role = assign_role(None)
        assert role == "primary"
        assert isinstance(gid, str) and len(gid) >= 36  # uuid

    def test_match_with_existing_group(self):
        match = {"cross_exchange_group_id": "abc-123"}
        gid, role = assign_role(match)
        assert role == "duplicate_dropped"
        assert gid == "abc-123"

    def test_match_without_group_mints_one(self):
        match = {"cross_exchange_group_id": None}
        gid, role = assign_role(match)
        assert role == "duplicate_dropped"
        assert isinstance(gid, str) and len(gid) >= 36
