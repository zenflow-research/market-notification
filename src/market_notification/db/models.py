"""SQLAlchemy ORM models. Source of truth for the schema; Alembic migrations generated from these.

Schema follows PLAN.md §5. Any column change here MUST be paired with:
  1. an Alembic migration (revision file in db/migrations/versions/)
  2. an update to PLAN.md §5
  3. an entry in design-decisions.md §P (in-build decisions)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utc_now() -> datetime:
    """Tz-naive UTC now for SQLite-friendly DateTime columns.

    SQLAlchemy DateTime(timezone=False) doesn't store tz info; we strip after
    constructing the tz-aware value (which is the recommended pattern post
    Python 3.12 deprecation of datetime.utcnow()).
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Project-wide declarative base."""


# ---------------------------------------------------------------------------
# Notifications (primary table — unified BSE + NSE)
# ---------------------------------------------------------------------------
class Notification(Base):
    __tablename__ = "notifications"

    # Identity
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)  # 'BSE' or 'NSE'

    # Dedup key
    seq_id: Mapped[Optional[str]] = mapped_column(Text)

    # Core normalized fields
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[Optional[str]] = mapped_column(Text)
    subcategory: Mapped[Optional[str]] = mapped_column(Text)
    body: Mapped[Optional[str]] = mapped_column(Text)

    # Times
    announced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    exchange_disseminated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # Attachment metadata (file gets dropped at D:\Notification Dump\{cid}\)
    attachment_name: Mapped[Optional[str]] = mapped_column(Text)
    attachment_url: Mapped[Optional[str]] = mapped_column(Text)
    attachment_size: Mapped[Optional[str]] = mapped_column(Text)

    # Source-specific extras
    is_critical: Mapped[int] = mapped_column(Integer, default=0)  # BSE
    has_xbrl: Mapped[int] = mapped_column(Integer, default=0)  # NSE
    symbol: Mapped[Optional[str]] = mapped_column(Text)
    company_name_raw: Mapped[Optional[str]] = mapped_column(Text)
    isin: Mapped[Optional[str]] = mapped_column(Text)
    industry_raw: Mapped[Optional[str]] = mapped_column(Text)

    # Cross-exchange grouping (NSE+BSE same event)
    cross_exchange_group_id: Mapped[Optional[str]] = mapped_column(Text)
    cross_exchange_role: Mapped[Optional[str]] = mapped_column(Text)
    # 'primary' | 'duplicate_dropped'

    # Pipeline state machine
    pipeline_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="ingested"
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    last_status_change_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # Junk-filter result
    is_useless: Mapped[int] = mapped_column(Integer, default=0)
    junk_rule_id: Mapped[Optional[int]] = mapped_column(Integer)

    # Deterministic priority
    det_priority: Mapped[Optional[str]] = mapped_column(Text)
    det_score: Mapped[Optional[int]] = mapped_column(Integer)
    det_reasons: Mapped[Optional[str]] = mapped_column(Text)  # JSON list[str]

    # LLM classification + override (Gemma)
    ai_category: Mapped[Optional[str]] = mapped_column(Text)
    ai_category_group: Mapped[Optional[str]] = mapped_column(Text)
    ai_category_confidence: Mapped[Optional[float]] = mapped_column(Float)
    ai_category_source: Mapped[Optional[str]] = mapped_column(Text)
    # 'gemma' | 'regex' | 'fallback'
    ai_priority: Mapped[Optional[str]] = mapped_column(Text)
    ai_priority_score: Mapped[Optional[int]] = mapped_column(Integer)
    ai_priority_reasons: Mapped[Optional[str]] = mapped_column(Text)
    ai_classified_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    ai_classified_taxonomy_version: Mapped[Optional[str]] = mapped_column(Text)
    ai_classified_prompt_version: Mapped[Optional[str]] = mapped_column(Text)

    # Attachment processing
    download_status: Mapped[str] = mapped_column(Text, default="pending")
    # pending | downloading | done | failed | skipped
    local_path: Mapped[Optional[str]] = mapped_column(Text)
    pdf_extracted_text: Mapped[Optional[str]] = mapped_column(Text)
    pdf_image_summary: Mapped[Optional[str]] = mapped_column(Text)
    pdf_pages: Mapped[Optional[int]] = mapped_column(Integer)
    pdf_text_md5: Mapped[Optional[str]] = mapped_column(Text)
    deferred_doc_type: Mapped[Optional[str]] = mapped_column(Text)
    # annual_report | investor_presentation | earnings | ppt | credit_note | large_misc

    # Gemma summary (extended schema per H2)
    gemma_summary: Mapped[Optional[str]] = mapped_column(Text)
    gemma_impact: Mapped[Optional[str]] = mapped_column(Text)
    gemma_key_figures: Mapped[Optional[str]] = mapped_column(Text)
    gemma_key_people: Mapped[Optional[str]] = mapped_column(Text)
    gemma_key_dates: Mapped[Optional[str]] = mapped_column(Text)
    gemma_attachments_referenced: Mapped[Optional[str]] = mapped_column(Text)
    gemma_deferred_tags: Mapped[Optional[str]] = mapped_column(Text)
    gemma_external_links: Mapped[Optional[str]] = mapped_column(Text)
    gemma_summarized_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    gemma_model_version: Mapped[Optional[str]] = mapped_column(Text)
    gemma_prompt_version: Mapped[Optional[str]] = mapped_column(Text)
    gemma_confidence: Mapped[Optional[float]] = mapped_column(Float)

    # gemini-rr deep-dive
    gemini_deep_dive_json: Mapped[Optional[str]] = mapped_column(Text)
    gemini_deep_dive_prose: Mapped[Optional[str]] = mapped_column(Text)
    gemini_sector_kpi_findings: Mapped[Optional[str]] = mapped_column(Text)
    gemini_dive_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    gemini_prompt_version: Mapped[Optional[str]] = mapped_column(Text)
    gemini_cache_hit: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    gemini_profile_used: Mapped[Optional[str]] = mapped_column(Text)
    gemini_latency_s: Mapped[Optional[float]] = mapped_column(Float)

    # User workflow (kept simple; multi-user via user_id added in future migration)
    is_read: Mapped[int] = mapped_column(Integer, default=0)
    selected_for_report: Mapped[int] = mapped_column(Integer, default=0)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    user_notes: Mapped[Optional[str]] = mapped_column(Text)
    user_id: Mapped[str] = mapped_column(Text, default="system")

    # Metadata
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now, nullable=False
    )
    raw_json: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "source",
            "company_id",
            "announced_at",
            "headline",
            name="uq_notification_natural_key",
        ),
        CheckConstraint("source IN ('BSE','NSE')", name="ck_notification_source"),
        Index("ix_notif_company", "company_id"),
        Index("ix_notif_source", "source"),
        Index("ix_notif_announced", "announced_at"),
        Index("ix_notif_pipeline", "pipeline_status"),
        Index("ix_notif_priority", "ai_priority"),
        Index("ix_notif_priority_score", "ai_priority_score"),
        Index("ix_notif_company_announced", "company_id", "announced_at"),
        Index("ix_notif_cross_group", "cross_exchange_group_id"),
        Index("ix_notif_dispatcher", "pipeline_status", "ai_priority", "announced_at"),
        Index("ix_notif_seq", "source", "seq_id"),
        Index("ix_notif_symbol", "symbol"),
        Index("ix_notif_isin", "isin"),
        Index("ix_notif_category", "ai_category"),
        Index("ix_notif_cat_group", "ai_category_group"),
        Index("ix_notif_next_retry", "next_retry_at"),
    )


# ---------------------------------------------------------------------------
# Notification filter rules (junk negative-list)
# ---------------------------------------------------------------------------
class NotificationFilterRule(Base):
    __tablename__ = "notification_filter_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_type: Mapped[str] = mapped_column(Text, nullable=False)
    # category | subcategory | headline_regex | keyword
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[Optional[str]] = mapped_column(Text)  # BSE | NSE | NULL=both
    action: Mapped[str] = mapped_column(Text, default="hide")  # hide | block
    created_by: Mapped[str] = mapped_column(Text, default="system")
    # system | user | auto
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("rule_type", "pattern", "source", name="uq_filter_rule_key"),
        Index("ix_filter_active", "is_active"),
    )


# ---------------------------------------------------------------------------
# Poll state (watermarks per source)
# ---------------------------------------------------------------------------
class NotificationPollState(Base):
    __tablename__ = "notification_poll_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    last_poll_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_seq_id: Mapped[Optional[str]] = mapped_column(Text)
    last_date: Mapped[Optional[str]] = mapped_column(Text)  # YYYYMMDD
    records_fetched: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(Text, default="idle")
    # idle | polling | error
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now, nullable=False
    )


# ---------------------------------------------------------------------------
# Historical symbol map (old codes -> successor company)
# ---------------------------------------------------------------------------
class HistoricalSymbolMap(Base):
    __tablename__ = "historical_symbol_map"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    old_symbol: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    successor_company_id: Mapped[int] = mapped_column(Integer, nullable=False)
    mapping_type: Mapped[Optional[str]] = mapped_column(Text)
    # merger | rename | demerger | relisting
    old_company_name: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("old_symbol", "source", name="uq_historical_symbol"),
        Index("ix_hist_symbol_source", "old_symbol", "source"),
    )


# ---------------------------------------------------------------------------
# Pipeline journal (audit log of every state transition)
# ---------------------------------------------------------------------------
class PipelineJournal(Base):
    __tablename__ = "pipeline_journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    notification_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    from_status: Mapped[Optional[str]] = mapped_column(Text)
    to_status: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now, nullable=False
    )
    actor: Mapped[Optional[str]] = mapped_column(Text)
    # poller | dispatcher | classifier | summarizer | deep_dive | sla_monitor | user
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    error_kind: Mapped[Optional[str]] = mapped_column(Text)
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        Index("ix_journal_notif_at", "notification_id", "at"),
        Index("ix_journal_at_actor", "at", "actor"),
        Index("ix_journal_to_status", "to_status"),
    )


# ---------------------------------------------------------------------------
# Taxonomy version (for re-classification audit)
# ---------------------------------------------------------------------------
class TaxonomyVersion(Base):
    __tablename__ = "taxonomy_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_tag: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    taxonomy_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now, nullable=False
    )
    retired_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


# ---------------------------------------------------------------------------
# Prompt version (for re-summarize / re-deep-dive audit)
# ---------------------------------------------------------------------------
class PromptVersion(Base):
    __tablename__ = "prompt_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    # classifier | summarizer | deep_dive_<category>
    version_tag: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now, nullable=False
    )
    retired_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("scope", "version_tag", name="uq_prompt_version"),
    )
