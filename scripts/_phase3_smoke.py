"""Phase 3 verification harness.

Runs the Poller's `poll_once()` against the live DB at data/notifications.db,
captures counts before/after, then runs it AGAIN to verify dedup
(second pass should insert 0 new rows).

Artifacts in verification/phase_3_results/:
  - poll_run.txt         -- per-source PollResult counters (both passes)
  - poll_state_after.csv -- contents of notification_poll_state
  - dedup_proof.txt      -- N rows after pass 1 vs after pass 2
  - cross_exchange_groups.csv -- any group_ids with >1 member (may be empty
                                   on a single-poll run)
"""
from __future__ import annotations

import csv
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import select, func  # noqa: E402

from market_notification.companies.factory import default_company_provider  # noqa: E402
from market_notification.db.models import (  # noqa: E402
    Notification,
    NotificationPollState,
)
from market_notification.db.session import get_session  # noqa: E402
from market_notification.exchange.bse_fetcher import BSEFetcher  # noqa: E402
from market_notification.exchange.nse_fetcher import NSEFetcher  # noqa: E402
from market_notification.poller.poller import Poller  # noqa: E402

OUT_DIR = ROOT / "verification" / "phase_3_results"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def count_notifications():
    with get_session() as sess:
        return sess.execute(select(func.count(Notification.id))).scalar_one()


def dump_poll_state():
    out = OUT_DIR / "poll_state_after.csv"
    with get_session() as sess:
        rows = sess.execute(select(NotificationPollState)).scalars().all()
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "source", "status", "last_poll_at", "last_seq_id",
            "last_date", "records_fetched", "error_message", "updated_at",
        ])
        for r in rows:
            w.writerow([
                r.source, r.status, r.last_poll_at, r.last_seq_id,
                r.last_date, r.records_fetched, r.error_message, r.updated_at,
            ])
    print(f"  wrote {out.name} ({len(rows)} rows)")


def dump_cross_exchange_groups():
    out = OUT_DIR / "cross_exchange_groups.csv"
    with get_session() as sess:
        # Find groups with >1 member
        sub = (
            select(
                Notification.cross_exchange_group_id,
                func.count(Notification.id).label("cnt"),
            )
            .where(Notification.cross_exchange_group_id.isnot(None))
            .group_by(Notification.cross_exchange_group_id)
            .having(func.count(Notification.id) > 1)
        )
        groups = list(sess.execute(sub).all())
        rows = []
        for gid, _cnt in groups:
            members = sess.execute(
                select(
                    Notification.id, Notification.source,
                    Notification.symbol, Notification.headline,
                    Notification.cross_exchange_role,
                ).where(Notification.cross_exchange_group_id == gid)
            ).all()
            for m in members:
                rows.append((gid,) + tuple(m))
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "group_id", "id", "source", "symbol", "headline", "role",
        ])
        for row in rows:
            w.writerow(row)
    print(f"  wrote {out.name} ({len(groups)} groups, {len(rows)} members)")


def main():
    print(f"Phase 3 smoke -- artifacts -> {OUT_DIR}")

    print("\n=== Setup ===")
    company_provider = default_company_provider()
    bse = BSEFetcher()
    nse = NSEFetcher(playwright_headless=True)
    poller = Poller(
        bse_fetcher=bse,
        nse_fetcher=nse,
        company_provider=company_provider,
        bse_records_per_poll=100,
        nse_records_per_poll=50,
    )

    before = count_notifications()
    print(f"Notifications before: {before}")

    print("\n=== Pass 1 ===")
    t0 = time.monotonic()
    bse_r1, nse_r1 = poller.poll_once()
    dt1 = time.monotonic() - t0
    after1 = count_notifications()
    delta1 = after1 - before
    print(f"  BSE: {bse_r1}")
    print(f"  NSE: {nse_r1}")
    print(f"  rows added pass 1: {delta1} (took {dt1:.1f}s)")

    print("\n=== Pass 2 (dedup verification) ===")
    t0 = time.monotonic()
    bse_r2, nse_r2 = poller.poll_once()
    dt2 = time.monotonic() - t0
    after2 = count_notifications()
    delta2 = after2 - after1
    print(f"  BSE: {bse_r2}")
    print(f"  NSE: {nse_r2}")
    print(f"  rows added pass 2: {delta2} (should be 0 if dedup works) "
          f"(took {dt2:.1f}s)")

    # Artifacts
    print("\n=== Artifacts ===")
    (OUT_DIR / "poll_run.txt").write_text(
        f"Phase 3 smoke run @ {datetime.now().isoformat()}\n\n"
        f"Pass 1\n"
        f"  BSE: {bse_r1}\n"
        f"  NSE: {nse_r1}\n"
        f"  rows added: {delta1}\n"
        f"  duration: {dt1:.2f}s\n\n"
        f"Pass 2 (dedup)\n"
        f"  BSE: {bse_r2}\n"
        f"  NSE: {nse_r2}\n"
        f"  rows added: {delta2}\n"
        f"  duration: {dt2:.2f}s\n",
        encoding="utf-8",
    )
    (OUT_DIR / "dedup_proof.txt").write_text(
        f"Notifications before run: {before}\n"
        f"After pass 1:             {after1}  (delta=+{delta1})\n"
        f"After pass 2:             {after2}  (delta=+{delta2})\n"
        f"\nDedup verified: {delta2 == 0}\n",
        encoding="utf-8",
    )
    dump_poll_state()
    dump_cross_exchange_groups()

    # Exit criteria
    print("\n=== Exit criteria ===")
    print(f"  >=5 rows inserted pass 1: {delta1 >= 5}")
    print(f"  Dedup pass 2 == 0:         {delta2 == 0}")
    print(f"  Poll state has BSE+NSE rows: see {OUT_DIR / 'poll_state_after.csv'}")


if __name__ == "__main__":
    main()
