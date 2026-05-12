"""Offline smoke harness for B3 (backup_db), B4 (logs_rollup), B5 (push_important).

Covers the unit-of-work logic of each module without touching the live DB,
Discord, or S3:

  B3 backup_db
    - _timestamped_name() shape (sortable, includes Z)
    - _resolve_db_path() loads from settings
    - End-to-end on a tiny synthetic SQLite DB: SQLite-backup-API copy
      produces an identical row count, gzip preserves bytes, prune keeps N

  B4 logs_rollup
    - _LINE_RE matches a real-format record
    - _parse_log_file() folds traceback continuations into prior record
    - _render_report() produces well-formed Markdown with expected sections

  B5 push_important
    - PushState save+load roundtrip
    - build_discord_embed() includes title / description / fields / color
    - _short() truncation behaviour

Run::

    python scripts/_b35_smoke.py

Exit 0 on success, 1 on any failure.
"""
from __future__ import annotations

import gzip
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"
SRC = PROJECT_ROOT / "src"
for p in (SRC, SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _assert(cond: bool, label: str) -> None:
    if not cond:
        raise AssertionError(label)


# ---- B3 backup_db ---------------------------------------------------------


def test_b3_timestamped_name() -> None:
    import backup_db  # type: ignore

    n1 = backup_db._timestamped_name()
    _assert(n1.startswith("notifications-"), f"prefix: {n1}")
    _assert(n1.endswith(".db"), f"suffix: {n1}")
    _assert("Z" in n1, f"has Z marker: {n1}")
    # sortable -- a 1-sec later call should compare strictly greater
    import time as _time
    _time.sleep(1.1)
    n2 = backup_db._timestamped_name()
    _assert(n2 > n1, f"sort order: {n1!r} vs {n2!r}")


def test_b3_resolve_db_path() -> None:
    import backup_db  # type: ignore

    p = backup_db._resolve_db_path()
    _assert(p.is_absolute(), f"absolute path: {p}")
    _assert(p.name.endswith(".db"), f".db suffix: {p}")


def test_b3_end_to_end() -> None:
    import backup_db  # type: ignore
    import logging
    log = logging.getLogger("test-b3")

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        src = td_path / "src.db"
        dst_dir = td_path / "out"
        dst_dir.mkdir()

        # Build a tiny source DB
        conn = sqlite3.connect(str(src))
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)",
                         [(f"row{i}",) for i in range(100)])
        conn.commit()
        conn.close()

        # Backup via the same API the script uses
        snap_db = dst_dir / "src-snap.db"
        backup_db._checkpoint_wal(src, log)
        backup_db._backup_with_sqlite_api(src, snap_db, log)
        _assert(snap_db.exists(), "snapshot db exists")

        # Verify identical row count
        conn = sqlite3.connect(str(snap_db))
        n = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        conn.close()
        _assert(n == 100, f"row count match: {n}")

        # Gzip
        gz = backup_db._gzip_in_place(snap_db, log)
        _assert(gz.suffix == ".gz", f"gz suffix: {gz}")
        _assert(not snap_db.exists(), "source .db removed after gz")

        # Verify gzip is sound + content is a SQLite db
        with gzip.open(gz, "rb") as f:
            head = f.read(16)
        _assert(head.startswith(b"SQLite format 3"), f"gz contents are SQLite: {head!r}")


def test_b3_prune() -> None:
    import backup_db  # type: ignore
    import logging
    log = logging.getLogger("test-b3-prune")

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        # Create 7 fake snapshots
        for i in range(7):
            (td_path / f"notifications-202605{i:02d}-000000Z.db.gz").write_bytes(b"x")

        pruned = backup_db._prune_old(td_path, keep=3, log=log)
        _assert(pruned == 4, f"pruned 4 of 7 with keep=3, got: {pruned}")

        remaining = sorted(p.name for p in td_path.glob("notifications-*.db.gz"))
        _assert(len(remaining) == 3, f"3 kept, got: {len(remaining)}")
        _assert(remaining[-1].endswith("06-000000Z.db.gz"), f"newest kept: {remaining}")


# ---- B4 logs_rollup -------------------------------------------------------


def test_b4_line_re() -> None:
    import logs_rollup  # type: ignore

    line = "2026-05-12 10:15:23 INFO    poller scripts.run_poller Poller running. Ctrl+C to stop."
    m = logs_rollup._LINE_RE.match(line)
    _assert(m is not None, "regex matched")
    assert m is not None
    _assert(m["level"] == "INFO", f"level: {m['level']}")
    _assert(m["proc"] == "poller", f"proc: {m['proc']}")
    _assert(m["logger"] == "scripts.run_poller", f"logger: {m['logger']}")
    _assert(m["msg"].startswith("Poller running"), f"msg: {m['msg']}")


def test_b4_parse_with_traceback() -> None:
    import logs_rollup  # type: ignore
    from datetime import datetime, timezone

    sample = (
        "2026-05-12 10:00:00 ERROR   worker dispatch attachment download failed\n"
        "Traceback (most recent call last):\n"
        "  File \"a.py\", line 1, in foo\n"
        "    raise ValueError('boom')\n"
        "ValueError: boom\n"
        "2026-05-12 10:00:05 INFO    worker dispatch next step\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False,
                                      encoding="utf-8") as f:
        f.write(sample)
        path = Path(f.name)
    try:
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        recs = logs_rollup._parse_log_file(path, cutoff)
        _assert(len(recs) == 2, f"two records, got: {len(recs)}")
        _assert(recs[0].level == "ERROR", "first is ERROR")
        _assert("Traceback" in recs[0].msg, "traceback folded into ERROR")
        _assert("ValueError: boom" in recs[0].msg, "exception line folded")
        _assert(recs[1].level == "INFO", "second is INFO (separate record)")
    finally:
        path.unlink()


def test_b4_render_report() -> None:
    import logs_rollup  # type: ignore
    from datetime import datetime, timedelta, timezone

    now = datetime.now(tz=timezone.utc)
    earlier = now - timedelta(hours=1)
    recs = [
        logs_rollup.LogRecord(now - timedelta(minutes=10), "ERROR", "worker",
                              "dispatch", "Ollama timeout id=123", "worker.log"),
        logs_rollup.LogRecord(now - timedelta(minutes=8), "ERROR", "worker",
                              "dispatch", "Ollama timeout id=456", "worker.log"),
        logs_rollup.LogRecord(now - timedelta(minutes=5), "WARNING", "poller",
                              "exchange", "BSE rate-limited", "poller.log"),
        logs_rollup.LogRecord(now - timedelta(minutes=1), "INFO", "poller",
                              "exchange", "ok", "poller.log"),
    ]
    md = logs_rollup._render_report(
        files_scanned=[Path("worker.log"), Path("poller.log")],
        files_skipped=[],
        records=recs,
        window_start=earlier,
        window_end=now,
    )
    _assert("# mn logs rollup" in md, "has title")
    _assert("Per-process level counts" in md, "has level table section")
    _assert("Top error patterns" in md, "has error patterns section")
    # The two Ollama-timeout records have different ids but should fold
    # to the SAME canonical pattern (id=<N> rule).
    _assert("Ollama timeout id=<N>" in md or "Ollama timeout id=notif_id=<N>" in md,
            f"canonicalised pattern present in:\n{md}")


# ---- B5 push_important ----------------------------------------------------


def test_b5_state_roundtrip() -> None:
    import push_important  # type: ignore

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "state.json"
        s = push_important.PushState(last_pushed_id=42, total_pushed=42,
                                      last_run_at="2026-05-12T00:00:00+00:00")
        s.save(path)
        _assert(path.exists(), "state saved")
        s2 = push_important.PushState.load(path)
        _assert(s2.last_pushed_id == 42, f"id roundtrip: {s2.last_pushed_id}")
        _assert(s2.total_pushed == 42, f"total roundtrip: {s2.total_pushed}")


def test_b5_build_embed() -> None:
    import push_important  # type: ignore

    row = push_important.NotificationRow(
        id=7, source="BSE", company_id=3373, company_name_raw="TATA STEEL",
        headline="Acquisition of XYZ Ltd announced -- consideration Rs 850 Cr",
        ai_category="Acquisition", ai_priority="important",
        gemma_impact="Acquires XYZ to expand auto-grade product portfolio.",
        gemma_summary="full summary text...",
        announced_at="2026-05-12T10:15:00+00:00",
        gemini_dive_at="2026-05-12T10:20:00+00:00",
        gemini_cache_hit=0, gemini_profile_used="research",
    )
    embed = push_important.build_discord_embed(row)
    _assert(isinstance(embed, dict), "embed is a dict")
    _assert("TATA STEEL" in embed["title"], f"title has company: {embed['title']}")
    _assert("Acquires XYZ" in embed["description"],
            f"description has impact: {embed['description']}")
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    _assert(fields.get("Source") == "BSE", f"Source field: {fields}")
    _assert(fields.get("Category") == "Acquisition", f"Category field: {fields}")
    _assert(fields.get("id") == "7", f"id field: {fields}")
    _assert("Deep-dive" in fields, f"deep-dive field present: {list(fields)}")
    _assert(embed["color"] == push_important._PRIORITY_COLOR["important"],
            f"color is important-red: {embed['color']}")


def test_b5_short_truncation() -> None:
    import push_important  # type: ignore

    _assert(push_important._short(None, 10) == "", "None -> ''")
    _assert(push_important._short("hello", 10) == "hello", "short stays short")
    _assert(push_important._short("a" * 50, 10) == "aaaaaaaaa...", "long is truncated")
    _assert(push_important._short("a  b\n c", 10) == "a b c", "whitespace collapsed")


CHECKS = [
    ("B3 timestamped_name", test_b3_timestamped_name),
    ("B3 _resolve_db_path", test_b3_resolve_db_path),
    ("B3 end-to-end backup", test_b3_end_to_end),
    ("B3 prune retention", test_b3_prune),
    ("B4 line regex", test_b4_line_re),
    ("B4 parse + traceback fold", test_b4_parse_with_traceback),
    ("B4 render report", test_b4_render_report),
    ("B5 state roundtrip", test_b5_state_roundtrip),
    ("B5 build embed", test_b5_build_embed),
    ("B5 short truncation", test_b5_short_truncation),
]


def main() -> int:
    print(f"running {len(CHECKS)} B3/B4/B5 smoke check(s)...")
    failures: list[tuple[str, str]] = []
    for label, fn in CHECKS:
        try:
            fn()
            print(f"  PASS  {label}")
        except AssertionError as exc:
            failures.append((label, str(exc)))
            print(f"  FAIL  {label} -- {exc}")
        except Exception as exc:  # noqa: BLE001
            failures.append((label, f"{type(exc).__name__}: {exc}"))
            print(f"  FAIL  {label} -- {type(exc).__name__}: {exc}")
    print()
    if failures:
        print(f"SUMMARY: {len(failures)} of {len(CHECKS)} failed.")
        return 1
    print(f"SUMMARY: all {len(CHECKS)} checks PASS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
