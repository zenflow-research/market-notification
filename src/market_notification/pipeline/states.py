"""Pipeline state machine constants. Source of truth for `pipeline_status`.

Keep terminal vs. transient vs. failed states clearly separated. Any change
here is a schema-touching change — update PLAN.md §6 and migrations as needed.
"""
from __future__ import annotations

from enum import Enum


class PipelineStatus(str, Enum):
    # Initial
    INGESTED = "ingested"

    # Junk-filter result
    IGNORED = "ignored"

    # Classification
    CLASSIFY_PENDING = "classify_pending"
    CLASSIFY_ACTIVE = "classify_active"
    CLASSIFY_FAILED = "classify_failed"
    CLASSIFY_DEAD = "classify_dead"

    # Priority (deterministic + LLM override)
    PRIORITY_PENDING = "priority_pending"
    PRIORITY_ACTIVE = "priority_active"
    PRIORITY_FAILED = "priority_failed"

    # Attachment download + extract
    ATTACHMENT_PENDING = "attachment_pending"
    ATTACHMENT_ACTIVE = "attachment_active"
    ATTACHMENT_FAILED = "attachment_failed"
    ATTACHMENT_DEAD = "attachment_dead"

    # Summarize
    SUMMARIZE_PENDING = "summarize_pending"
    SUMMARIZE_ACTIVE = "summarize_active"
    SUMMARIZE_FAILED = "summarize_failed"
    SUMMARIZE_DEAD = "summarize_dead"

    # Deep-dive (gemini-rr)
    DEEP_DIVE_PENDING = "deep_dive_pending"
    DEEP_DIVE_ACTIVE = "deep_dive_active"
    DEEP_DIVE_FAILED = "deep_dive_failed"
    DEEP_DIVE_DEAD = "deep_dive_dead"

    # Terminal
    DONE = "done"
    DONE_DEFERRED = "done_deferred"  # tagged but not summarized
    IMPORTED_LEGACY = "imported_legacy"  # backfilled from brain, not active


TERMINAL = frozenset(
    {
        PipelineStatus.IGNORED,
        PipelineStatus.DONE,
        PipelineStatus.DONE_DEFERRED,
        PipelineStatus.IMPORTED_LEGACY,
        PipelineStatus.CLASSIFY_DEAD,
        PipelineStatus.ATTACHMENT_DEAD,
        PipelineStatus.SUMMARIZE_DEAD,
        PipelineStatus.DEEP_DIVE_DEAD,
    }
)

ACTIVE = frozenset(
    {
        PipelineStatus.CLASSIFY_ACTIVE,
        PipelineStatus.PRIORITY_ACTIVE,
        PipelineStatus.ATTACHMENT_ACTIVE,
        PipelineStatus.SUMMARIZE_ACTIVE,
        PipelineStatus.DEEP_DIVE_ACTIVE,
    }
)

PENDING = frozenset(
    {
        PipelineStatus.CLASSIFY_PENDING,
        PipelineStatus.PRIORITY_PENDING,
        PipelineStatus.ATTACHMENT_PENDING,
        PipelineStatus.SUMMARIZE_PENDING,
        PipelineStatus.DEEP_DIVE_PENDING,
    }
)

FAILED = frozenset(
    {
        PipelineStatus.CLASSIFY_FAILED,
        PipelineStatus.PRIORITY_FAILED,
        PipelineStatus.ATTACHMENT_FAILED,
        PipelineStatus.SUMMARIZE_FAILED,
        PipelineStatus.DEEP_DIVE_FAILED,
    }
)


# Ordered priority bucket -> integer for SQL ORDER BY
PRIORITY_RANK = {
    "important": 0,
    "medium": 1,
    "normal": 2,
    "ignored": 3,
}
