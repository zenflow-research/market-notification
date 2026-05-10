"""Notification Poller -- the always-on Process 1 (D-09).

Per FR-INGEST-001..008:
  - Polls BSE + NSE every `interval_s` (default 60s).
  - Polls 24x7 (no time gating).
  - Maps each filing to a company via `CompanyResolver`.
  - Cross-exchange grouping for BSE+NSE near-duplicates.
  - Dedup on natural key via DB UNIQUE + app-level short-circuit.
  - Watermarks per source in `notification_poll_state`.

Threading: poll runs on a single daemon thread; `start()` is non-blocking.
For tests, `poll_once()` runs one cycle synchronously and is the
canonical entry point for assertions.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from ..companies.base import CompanyProvider
from ..db.models import Notification
from ..db.repositories.notification_repo_sqla import SqlaNotificationRepo
from ..db.repositories.poll_state_repo_sqla import SqlaPollStateRepo
from ..db.session import get_session
from ..exchange.base import ExchangeFetcher, RawNotification
from ..filter.base import FilterEngineBase
from .company_resolver import CompanyResolver
from .cross_exchange import (
    DEFAULT_WINDOW_MINUTES,
    assign_role,
    find_match,
)

logger = logging.getLogger(__name__)


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PollResult:
    """Per-source counters from one poll cycle."""

    __slots__ = ("source", "fetched", "inserted", "duplicates",
                 "cross_dropped", "junk", "errors")

    def __init__(self, source: str) -> None:
        self.source = source
        self.fetched = 0
        self.inserted = 0
        self.duplicates = 0
        self.cross_dropped = 0
        self.junk = 0
        self.errors = 0

    def __repr__(self) -> str:  # pragma: no cover -- debug aid
        return (
            f"PollResult({self.source} fetched={self.fetched} "
            f"inserted={self.inserted} duplicates={self.duplicates} "
            f"cross_dropped={self.cross_dropped} junk={self.junk} "
            f"errors={self.errors})"
        )


class Poller:
    """Orchestrator for the BSE + NSE poll loop."""

    def __init__(
        self,
        bse_fetcher: ExchangeFetcher,
        nse_fetcher: ExchangeFetcher,
        company_provider: CompanyProvider,
        filter_engine: Optional[FilterEngineBase] = None,
        interval_s: int = 60,
        bse_records_per_poll: int = 100,
        nse_records_per_poll: int = 50,
        cross_window_minutes: int = DEFAULT_WINDOW_MINUTES,
    ) -> None:
        self.bse_fetcher = bse_fetcher
        self.nse_fetcher = nse_fetcher
        self.resolver = CompanyResolver(company_provider)
        self.filter_engine = filter_engine
        self.interval_s = interval_s
        self.bse_records_per_poll = bse_records_per_poll
        self.nse_records_per_poll = nse_records_per_poll
        self.cross_window = timedelta(minutes=cross_window_minutes)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Launch the poll loop on a daemon thread. Non-blocking."""
        if self._thread and self._thread.is_alive():
            logger.warning("Poller already running")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="Poller"
        )
        self._thread.start()
        logger.info("Poller started (interval=%ds)", self.interval_s)

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the loop to exit and wait for the thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        logger.info("Poller stopped")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                logger.error("Poller loop error: %s", traceback.format_exc())
            # Sleep responsively
            for _ in range(self.interval_s):
                if self._stop.is_set():
                    return
                time.sleep(1)

    # ------------------------------------------------------------------
    # One cycle
    # ------------------------------------------------------------------
    def poll_once(self) -> tuple[PollResult, PollResult]:
        """Run one full BSE + NSE poll. Returns counters for both sources."""
        nse_res = self._poll_source("NSE", self.nse_fetcher, self.nse_records_per_poll)
        bse_res = self._poll_source("BSE", self.bse_fetcher, self.bse_records_per_poll)
        return (bse_res, nse_res)

    def _poll_source(
        self, source: str, fetcher: ExchangeFetcher, n: int
    ) -> PollResult:
        result = PollResult(source)
        started = _utc_now_naive()

        try:
            rows = fetcher.fetch_latest(n)
        except Exception as e:
            logger.error("Fetch failed for %s: %s", source, e)
            result.errors += 1
            self._update_state(source, "error", started, str(e)[:500], 0)
            return result

        result.fetched = len(rows)
        for raw in rows:
            try:
                if self._ingest_one(raw, result):
                    result.inserted += 1
            except Exception as e:  # noqa: BLE001 -- one bad row shouldn't kill the cycle
                logger.error("Ingest error (%s): %s", source, e)
                result.errors += 1

        last_seq = rows[0].seq_id if rows else None
        last_date = rows[0].announced_at.strftime("%Y%m%d") if rows else None
        self._update_state(source, "idle", started, None, result.inserted,
                           last_seq=last_seq, last_date=last_date)
        logger.info(
            "Polled %s fetched=%d inserted=%d duplicates=%d cross_dropped=%d "
            "junk=%d errors=%d",
            source, result.fetched, result.inserted, result.duplicates,
            result.cross_dropped, result.junk, result.errors,
        )
        return result

    # ------------------------------------------------------------------
    # Ingest one raw row
    # ------------------------------------------------------------------
    def _ingest_one(self, raw: RawNotification, result: PollResult) -> bool:
        """Map -> dedup -> junk-filter -> cross-group -> insert.

        Returns True if a NEW row was inserted (regardless of junk/cross status;
        junk rows are still persisted for audit per FR-FILTER-005).
        """
        with get_session() as sess:
            repo = SqlaNotificationRepo(sess)

            company_id = self.resolver.resolve(raw)
            payload = self._raw_to_payload(raw, company_id)

            # App-level dedup short-circuit (DB UNIQUE is the real safety net)
            if repo.exists_by_natural_key(
                source=payload["source"],
                company_id=payload["company_id"],
                announced_at=payload["announced_at"],
                headline=payload["headline"],
            ):
                result.duplicates += 1
                return False

            # Junk filter (FR-FILTER-001). Run before cross-exchange grouping
            # because junk rows shouldn't anchor a group nor consume LLM later.
            is_junk = False
            if self.filter_engine is not None:
                match = self.filter_engine.is_junk(raw)
                if match is not None:
                    is_junk = True
                    payload["is_useless"] = 1
                    payload["junk_rule_id"] = match.rule_id
                    payload["pipeline_status"] = "ignored"
                    result.junk += 1

            # Cross-exchange grouping (only if company is mapped AND not junk).
            # Junk rows skip grouping: they never enter the pipeline so a
            # "primary/duplicate" pairing has no downstream consumer.
            if company_id != 0 and not is_junk:
                window_rows = self._cross_window_rows(sess, company_id, raw.announced_at)
                match = find_match({
                    "source": raw.source,
                    "company_id": company_id,
                    "announced_at": raw.announced_at,
                    "headline": raw.headline,
                }, window_rows, window=self.cross_window)
                group_id, role = assign_role(match)
                payload["cross_exchange_group_id"] = group_id
                payload["cross_exchange_role"] = role
                if role == "duplicate_dropped":
                    result.cross_dropped += 1
                    # Per FR-INGEST-006: short-circuit pipeline
                    payload["pipeline_status"] = "ignored_cross_exchange"

            repo.insert(payload)
            return True

    def _cross_window_rows(
        self, sess, company_id: int, announced_at: datetime
    ) -> list[dict]:
        """Return rows for the same company within +/- cross_window minutes."""
        lo = announced_at - self.cross_window
        hi = announced_at + self.cross_window
        stmt = (
            select(
                Notification.id,
                Notification.source,
                Notification.company_id,
                Notification.announced_at,
                Notification.headline,
                Notification.cross_exchange_group_id,
            )
            .where(Notification.company_id == company_id)
            .where(Notification.announced_at >= lo)
            .where(Notification.announced_at <= hi)
        )
        return [dict(r._mapping) for r in sess.execute(stmt).all()]

    # ------------------------------------------------------------------
    # Glue
    # ------------------------------------------------------------------
    @staticmethod
    def _raw_to_payload(raw: RawNotification, company_id: int) -> dict:
        d = asdict(raw)
        d["company_id"] = company_id
        # Booleans -> SQLite-compatible 0/1
        d["is_critical"] = 1 if raw.is_critical else 0
        d["has_xbrl"] = 1 if raw.has_xbrl else 0
        return d

    def _update_state(  # noqa: PLR0913
        self,
        source: str,
        status: str,
        polled_at: datetime,
        error: Optional[str],
        inserted: int,
        last_seq: Optional[str] = None,
        last_date: Optional[str] = None,
    ) -> None:
        try:
            with get_session() as sess:
                SqlaPollStateRepo(sess).upsert(
                    source=source,
                    status=status,
                    last_poll_at=polled_at,
                    last_seq_id=last_seq,
                    last_date=last_date,
                    records_fetched=inserted,
                    error_message=error,
                )
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to update poll_state for %s: %s", source, e)
