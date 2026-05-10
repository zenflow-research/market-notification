"""One-shot helper: dump our notifications.db schema for verification artifact."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "notifications.db"


def main() -> int:
    if not DB.exists():
        print(f"DB NOT FOUND: {DB}")
        return 1
    con = sqlite3.connect(DB)
    print(f"-- DB: {DB} ({DB.stat().st_size} bytes)")
    print(f"-- generated: schema dump\n")

    print("-- TABLES --")
    for name, sql in con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ):
        print(f"\n-- {name}")
        print(sql or "(no SQL)")

    print("\n-- INDEXES --")
    for (name,) in con.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        print(name)

    print("\n-- PRAGMAS --")
    print(f"journal_mode = {con.execute('PRAGMA journal_mode').fetchone()[0]}")
    print(f"foreign_keys = {con.execute('PRAGMA foreign_keys').fetchone()[0]}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
