"""Pre-flight checks before launching shadow mode (A1.x of the cutover plan).

Verifies the runtime preconditions for running market_notification alongside
brain. Splits checks into three severities:

  CRITICAL    must pass to launch -- failure means mn cannot run correctly
  IMPORTANT   should pass -- failure means likely operational issue, fix first
  INFO        nice-to-have -- warning only, won't block

Exit codes:
  0   all CRITICAL + IMPORTANT pass (warnings allowed); ready to launch
  1   any CRITICAL or IMPORTANT failed; do not launch yet
  2   harness itself errored (rare)

Usage::

    python scripts/_shadow_preflight.py

The output is intentionally a single-screen ASCII report so it can be
posted into a notes file or pasted into a runbook PR.
"""
from __future__ import annotations

import socket
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


CRITICAL = "CRITICAL"
IMPORTANT = "IMPORTANT"
INFO = "INFO"
SEVERITIES = (CRITICAL, IMPORTANT, INFO)

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"


@dataclass
class CheckResult:
    severity: str
    label: str
    status: str  # PASS / FAIL / WARN
    detail: str = ""


@dataclass
class Report:
    checks: list[CheckResult] = field(default_factory=list)

    def add(self, severity: str, label: str, status: str, detail: str = "") -> None:
        self.checks.append(CheckResult(severity, label, status, detail))

    @property
    def is_ready(self) -> bool:
        """True if no CRITICAL or IMPORTANT FAIL. WARNs and INFO failures allowed."""
        return not any(
            c.status == FAIL and c.severity in (CRITICAL, IMPORTANT) for c in self.checks
        )

    def render(self) -> str:
        max_label = max((len(c.label) for c in self.checks), default=40)
        lines = []
        for c in self.checks:
            marker = {PASS: "PASS", FAIL: "FAIL", WARN: "WARN"}[c.status]
            head = f"[{c.severity:<9}] {c.label:<{max_label}} {marker}"
            if c.detail:
                head += f"  ({c.detail})"
            lines.append(head)
        return "\n".join(lines)

    def summarize(self) -> str:
        counts = {PASS: 0, FAIL: 0, WARN: 0}
        for c in self.checks:
            counts[c.status] += 1
        by_severity = {sev: {PASS: 0, FAIL: 0, WARN: 0} for sev in SEVERITIES}
        for c in self.checks:
            by_severity[c.severity][c.status] += 1
        ready_str = "READY" if self.is_ready else "NOT READY"
        return (
            f"SUMMARY: {counts[PASS]} PASS, {counts[FAIL]} FAIL, "
            f"{counts[WARN]} WARN. {ready_str} to launch shadow mode."
        )


# ---- individual checks ----------------------------------------------------


def check_ollama(rep: Report) -> None:
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=5)
        if r.status_code != 200:
            rep.add(CRITICAL, "Ollama daemon responsive", FAIL, f"HTTP {r.status_code}")
            return
        tags = r.json().get("models", []) or []
        names = [m.get("name", "") for m in tags]
        rep.add(CRITICAL, "Ollama daemon responsive", PASS, f"{len(names)} model(s) installed")
        gemma_matches = [n for n in names if n.startswith("gemma4-zenflow-moe")]
        if gemma_matches:
            rep.add(CRITICAL, "Gemma 4 MoE model installed", PASS, gemma_matches[0])
        else:
            rep.add(CRITICAL, "Gemma 4 MoE model installed", FAIL,
                    "expected `gemma4-zenflow-moe:*` in tag list")
        qwen_matches = [n for n in names if n.startswith("qwen2.5:14b")]
        if qwen_matches:
            rep.add(INFO, "Qwen 2.5 14B (brain) installed", PASS, qwen_matches[0])
        else:
            rep.add(INFO, "Qwen 2.5 14B (brain) installed", WARN,
                    "brain summarizer uses this; not strictly required for mn")
    except requests.RequestException as exc:
        rep.add(CRITICAL, "Ollama daemon responsive", FAIL,
                f"{type(exc).__name__}: {exc}")


def check_gemini_rr(rep: Report) -> None:
    try:
        from market_notification.config.settings import get_settings
        settings = get_settings()
        path = Path(settings.gemini_rr.binary)
    except Exception as exc:  # noqa: BLE001
        rep.add(CRITICAL, "gemini-rr binary", FAIL, f"settings load: {exc}")
        return

    if not path.exists():
        rep.add(CRITICAL, "gemini-rr binary", FAIL, f"missing: {path}")
        return
    try:
        proc = subprocess.run(
            [str(path), "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 0 or "usage" in (proc.stdout + proc.stderr).lower():
            rep.add(CRITICAL, "gemini-rr binary", PASS, f"{path} responds")
        else:
            rep.add(CRITICAL, "gemini-rr binary", FAIL,
                    f"rc={proc.returncode}, stderr={proc.stderr[:80]}")
    except subprocess.TimeoutExpired:
        rep.add(CRITICAL, "gemini-rr binary", FAIL, "--help timed out (15s)")
    except Exception as exc:  # noqa: BLE001
        rep.add(CRITICAL, "gemini-rr binary", FAIL,
                f"{type(exc).__name__}: {exc}")


def _sqlite_max_id(db_path: Path, table: str = "notifications") -> int | None:
    """Cheap proxy for row count -- O(log n) via the PRIMARY KEY index.

    Avoids ``SELECT COUNT(*)`` which is O(n) on SQLite and can take minutes
    on a multi-million-row cold-cache file. ``MAX(id)`` lower-bounds the
    row count and is good enough for the readiness checks here.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            cur = conn.execute(f"SELECT MAX(id) FROM {table}")
            row = cur.fetchone()
        finally:
            conn.close()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:  # noqa: BLE001
        return None


def _sqlite_exists_recent(db_path: Path, days: int = 1) -> bool | None:
    """Returns True if any row in the last ``days`` exists, False if not.

    Uses ``LIMIT 1`` so the query short-circuits on the first hit.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            cur = conn.execute(
                "SELECT 1 FROM notifications "
                f"WHERE fetched_at >= datetime('now', '-{days} day') LIMIT 1"
            )
            row = cur.fetchone()
        finally:
            conn.close()
        return row is not None
    except Exception:  # noqa: BLE001
        return None


def check_mn_backfill(rep: Report) -> None:
    db = PROJECT_ROOT / "data" / "notifications.db"
    if not db.exists():
        rep.add(CRITICAL, "mn notifications.db", FAIL, f"missing: {db}")
        return
    max_id = _sqlite_max_id(db)
    if max_id is None:
        rep.add(CRITICAL, "mn notifications.db readable", FAIL,
                "could not query notifications table")
        return
    if max_id >= 4_000_000:
        rep.add(CRITICAL, "mn notifications.db backfilled", PASS,
                f"max_id={max_id:,} (row count >= this)")
    elif max_id > 0:
        rep.add(CRITICAL, "mn notifications.db backfilled", FAIL,
                f"only max_id={max_id:,}; expected >= 4,000,000 "
                "(run scripts/backfill_brain_history.py)")
    else:
        rep.add(CRITICAL, "mn notifications.db backfilled", FAIL,
                "empty; run scripts/bootstrap_db.py + scripts/backfill_brain_history.py")


def check_brain_db(rep: Report) -> None:
    """File-stat-only check; the live brain process may hold a write lock
    that blocks even read-only SQLite opens for long stretches. We just
    verify the file exists, is big enough to be a real DB, and was
    touched recently (mtime within last 24h => brain is ingesting).
    """
    import datetime as dt
    db = Path("G:/brain/data/notifications.db")
    if not db.exists():
        rep.add(IMPORTANT, "brain notifications.db", FAIL, f"missing: {db}")
        return
    try:
        st = db.stat()
    except OSError as exc:
        rep.add(IMPORTANT, "brain notifications.db readable", FAIL,
                f"stat failed: {exc}")
        return

    size_gb = st.st_size / (1024 ** 3)
    if size_gb < 0.1:
        rep.add(IMPORTANT, "brain notifications.db size", FAIL,
                f"only {size_gb:.2f} GB; expected several GB for 4.7M rows")
    else:
        rep.add(IMPORTANT, "brain notifications.db size", PASS,
                f"{size_gb:.1f} GB on disk")

    mtime = dt.datetime.fromtimestamp(st.st_mtime, tz=dt.timezone.utc)
    age_h = (dt.datetime.now(tz=dt.timezone.utc) - mtime).total_seconds() / 3600
    if age_h < 24:
        rep.add(INFO, "brain DB mtime (24h)", PASS,
                f"touched {age_h:.1f}h ago -- brain looks active")
    else:
        rep.add(INFO, "brain DB mtime (24h)", WARN,
                f"last modified {age_h:.1f}h ago; brain may be offline")


def check_screener_essential(rep: Report) -> None:
    try:
        from market_notification.config.settings import get_settings
        settings = get_settings()
        path = Path(settings.paths.brain_screener_essential_db)
    except Exception as exc:  # noqa: BLE001
        rep.add(CRITICAL, "screener_essential.db (fundamentals)", FAIL,
                f"settings load: {exc}")
        return
    if not path.exists():
        rep.add(CRITICAL, "screener_essential.db (fundamentals)", FAIL,
                f"missing: {path}; deep-dive cannot inject FundamentalsDTO")
        return
    rep.add(CRITICAL, "screener_essential.db (fundamentals)", PASS, str(path))


def _port_listener_pid(port: int) -> int | None:
    """Return a PID listening on ``port`` if any, else None.

    Uses a TCP-connect probe -- if connection succeeds something is listening.
    The PID is best-effort via ``netstat`` (Windows) for the human-readable
    detail field; absence of a PID doesn't mean the port is free.
    """
    # Connect-probe first; if it refuses, the port is free.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(("127.0.0.1", port))
    except OSError:
        return None
    finally:
        s.close()
    # Something is listening; try to identify it with netstat.
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            timeout=8,
        ).stdout
        for line in out.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    try:
                        return int(parts[-1])
                    except ValueError:
                        pass
    except Exception:  # noqa: BLE001
        pass
    return -1  # listening but unknown PID


def check_port_free(rep: Report, label: str, port: int) -> None:
    pid = _port_listener_pid(port)
    if pid is None:
        rep.add(IMPORTANT, f"Port {port} ({label}) free", PASS, "")
    elif pid == -1:
        rep.add(IMPORTANT, f"Port {port} ({label}) free", FAIL,
                "in use, PID unknown (run netstat manually)")
    else:
        rep.add(IMPORTANT, f"Port {port} ({label}) free", FAIL,
                f"in use by PID {pid}; stop the holder before launch")


def check_brain_flask(rep: Report) -> None:
    """Check if brain Flask is listening on its conventional port 5000."""
    pid = _port_listener_pid(5000)
    if pid is None:
        rep.add(INFO, "brain Flask listening on 5000", WARN,
                "not running; OK for shadow phase A1.1 if intentional")
    else:
        detail = f"PID {pid}" if pid > 0 else "running"
        rep.add(INFO, "brain Flask listening on 5000", PASS, detail)


def check_disk_space(rep: Report) -> None:
    try:
        import shutil
        _total, _used, free = shutil.disk_usage(str(PROJECT_ROOT))
        free_gb = free / (1024 ** 3)
        if free_gb >= 5.0:
            rep.add(IMPORTANT, "Free disk on mn drive", PASS, f"{free_gb:.1f} GB free")
        else:
            rep.add(IMPORTANT, "Free disk on mn drive", FAIL,
                    f"only {free_gb:.1f} GB; logs + DB churn need >= 5 GB")
    except Exception as exc:  # noqa: BLE001
        rep.add(INFO, "Free disk on mn drive", WARN, f"could not measure: {exc}")


def check_optional_sources(rep: Report) -> None:
    sources = [
        ("sector KPI doc",
         "D:/Annual_report_extract/docs/sector_metrics_kpis.md"),
        ("concall taxonomy fallback",
         "D:/claude-codex-gemini/docs/concall_extraction_taxonomy.md"),
        ("screener_original CSV (companies)",
         "G:/Screener_original/screener_util/company_sector_mapping_master.csv"),
    ]
    for label, path_str in sources:
        if Path(path_str).exists():
            rep.add(INFO, label, PASS, path_str)
        else:
            rep.add(INFO, label, WARN, f"missing: {path_str} (mn degrades silently)")


def check_supervisor_smoke(rep: Report) -> None:
    """Verify run_all.py is at least syntactically importable."""
    try:
        proc = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "_run_all_smoke.py")],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(PROJECT_ROOT),
        )
        if proc.returncode == 0:
            rep.add(IMPORTANT, "supervisor + watcher smoke", PASS,
                    "scripts/_run_all_smoke.py all checks PASS")
        else:
            rep.add(IMPORTANT, "supervisor + watcher smoke", FAIL,
                    f"_run_all_smoke.py rc={proc.returncode}; "
                    f"first stderr line: {proc.stderr.splitlines()[:1]}")
    except Exception as exc:  # noqa: BLE001
        rep.add(IMPORTANT, "supervisor + watcher smoke", FAIL,
                f"{type(exc).__name__}: {exc}")


# ---- driver ---------------------------------------------------------------


CHECKS = [
    ("Ollama + Gemma", check_ollama),
    ("gemini-rr", check_gemini_rr),
    ("mn DB backfill", check_mn_backfill),
    ("brain DB sanity", check_brain_db),
    ("screener_essential.db", check_screener_essential),
    ("Disk space", check_disk_space),
    ("Port 8504 (poller /health)", lambda r: check_port_free(r, "poller /health", 8504)),
    ("Port 8505 (worker /health)", lambda r: check_port_free(r, "worker /health", 8505)),
    ("Port 8501 (Streamlit UI)", lambda r: check_port_free(r, "Streamlit UI", 8501)),
    ("brain Flask presence", check_brain_flask),
    ("Optional source docs", check_optional_sources),
    ("supervisor + watcher smoke", check_supervisor_smoke),
]


def main() -> int:
    print(f"Running {len(CHECKS)} shadow-mode pre-flight check(s)...", flush=True)
    print(flush=True)
    rep = Report()
    for i, (label, fn) in enumerate(CHECKS, start=1):
        print(f"  [{i:>2}/{len(CHECKS)}] {label}...", end="", flush=True)
        try:
            fn(rep)
            print(" done", flush=True)
        except Exception as exc:  # noqa: BLE001
            rep.add(IMPORTANT, label, FAIL,
                    f"check itself errored: {type(exc).__name__}: {exc}")
            print(f" errored: {exc}", flush=True)
    print(flush=True)
    print(rep.render())
    print()
    print(rep.summarize())
    if not rep.is_ready:
        print()
        print("Next steps:")
        print("  1. Read the FAIL lines above and resolve each one.")
        print("  2. Re-run this script until SUMMARY says READY.")
        print("  3. Then follow docs/SHADOW_MODE.md to start phase A1.1.")
        return 1
    print()
    print("Next: see docs/SHADOW_MODE.md -- Phase A1.1 (poller-only shadow).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
