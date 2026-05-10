"""Phase 1 entry investigation: inspect brain table schemas for the
fundamentals + price providers. Read-only.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path("G:/brain/screener_essential.db")


def show(con, table: str) -> None:
    print(f"\n========== {table} ==========")
    cols = con.execute(f"PRAGMA table_info({table})").fetchall()
    if not cols:
        print(f"  (no such table)")
        return
    for c in cols:
        print(f"  {c[1]:35s} {c[2]:20s} notnull={c[3]} pk={c[5]}")
    n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"  rows: {n:,}")
    rows = con.execute(f"SELECT * FROM {table} LIMIT 1").fetchall()
    if rows:
        print(f"  sample: {rows[0]!r}"[:300])


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    targets = [
        "company_info",
        "master",
        "pv",
        "mcap",
        "key_metrics",
        "margins",
        "financials",
        "segments",
        "corporate_actions",
        "stock_margin",
    ]
    for t in targets:
        show(con, t)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
