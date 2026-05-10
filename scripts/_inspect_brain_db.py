"""One-shot helper: inspect brain's screener_essential.db. Read-only.

Used at Phase 1 start to confirm columns we need for fundamentals.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path("G:/brain/screener_essential.db")


def main() -> int:
    if not DB.exists():
        print(f"NOT FOUND: {DB}")
        return 1

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    print(f"-- DB: {DB} ({DB.stat().st_size:,} bytes)")
    print()

    print("-- ALL TABLES --")
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    for t in tables:
        print(f"  {t}")

    target = "companies"
    if target not in tables:
        print(f"\n!! {target} table not found")
        con.close()
        return 1

    print(f"\n-- {target} columns --")
    cols = con.execute(f"PRAGMA table_info({target})").fetchall()
    for c in cols:
        # cid, name, type, notnull, dflt, pk
        print(f"  {c[1]:35s} {c[2]:20s} notnull={c[3]} pk={c[5]}")

    n = con.execute(f"SELECT COUNT(*) FROM {target}").fetchone()[0]
    print(f"\nrow count: {n:,}")

    print("\n-- sample (first 3 rows, key columns) --")
    cols_to_show = [
        "id", "company_name", "nse_code", "bse_code", "isin",
        "mcap", "sector", "industry", "basic_industry"
    ]
    avail = [c[1] for c in cols if c[1] in cols_to_show]
    select = ", ".join(avail)
    for row in con.execute(f"SELECT {select} FROM {target} LIMIT 3"):
        print(" ", row)

    # All other column names (for reference)
    print("\n-- ALL columns (full list) --")
    for c in cols:
        print(f"  {c[1]}")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
