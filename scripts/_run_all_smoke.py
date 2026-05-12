"""Offline smoke harness for ``scripts/run_all.py`` and ``scripts/watch_health.py``.

Verifies that the unit-of-work functions of both modules work correctly without
spawning child processes or hitting the network. Targets behaviours that
would silently regress under refactor:

  run_all
    - ``_build_specs`` returns the right children for given skip set + ports
    - ``_backoff_for`` returns the documented [5, 10, 30, 60] schedule
    - ``Supervisor`` exits with code 2 when no children remain after --skip

  watch_health
    - ``_evaluate_snapshot`` returns UP for a clean snapshot
    - ``_evaluate_snapshot`` returns DEGRADED for sla / dead-lane / pending-too-high
    - ``_evaluate_liveness`` maps HTTP outcome to UP / DOWN
    - ``StateStore.save() + load()`` roundtrips
    - ``_format_alert`` renders the expected prefix + reasons

Usage::

    python scripts/_run_all_smoke.py

Exits 0 on success, 1 on failure. Prints a one-line PASS / FAIL summary.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _assert(cond: bool, label: str) -> None:
    if not cond:
        raise AssertionError(label)


def test_run_all_build_specs() -> None:
    import run_all  # type: ignore

    specs = run_all._build_specs(
        poller_port=8504, worker_port=8505, ui_port=8501, skip=set()
    )
    _assert(len(specs) == 3, "expected 3 specs")
    names = [s.name for s in specs]
    _assert(names == ["poller", "worker", "ui"], f"unexpected order: {names}")
    _assert(specs[0].health_port == 8504, "poller port")
    _assert("--health-port" in specs[1].cmd, "worker should pass --health-port")
    _assert("--port" in specs[2].cmd, "ui should pass --port (not --health-port)")

    specs = run_all._build_specs(
        poller_port=8504, worker_port=8505, ui_port=8501, skip={"ui"}
    )
    _assert(len(specs) == 2, "expected 2 specs after --skip ui")
    _assert("ui" not in [s.name for s in specs], "ui should be skipped")


def test_run_all_backoff() -> None:
    import run_all  # type: ignore

    _assert(run_all._backoff_for(0) == 5, "backoff(0)")
    _assert(run_all._backoff_for(1) == 5, "backoff(1)")
    _assert(run_all._backoff_for(2) == 10, "backoff(2)")
    _assert(run_all._backoff_for(3) == 30, "backoff(3)")
    _assert(run_all._backoff_for(4) == 60, "backoff(4)")
    _assert(run_all._backoff_for(99) == 60, "backoff caps at 60")


def test_run_all_no_children_exits_2() -> None:
    """End-to-end CLI smoke: --skip all 3 produces clean exit code 2."""
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "run_all.py"),
            "--skip", "poller",
            "--skip", "worker",
            "--skip", "ui",
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    _assert(proc.returncode == 2, f"expected rc=2, got {proc.returncode}; stderr={proc.stderr[:400]}")


def test_watch_evaluate_snapshot_up() -> None:
    import watch_health  # type: ignore

    clean = {
        "ok": True,
        "queue_depth": {"classify_pending": 0},
        "failed_or_dead": {},
        "sla_breaches_classify": 0,
        "pending_total": 0,
        "throughput_24h": {"done": 100, "done_deferred": 5, "ignored": 50},
        "poller_state": {"BSE": {"status": "ok"}, "NSE": {"status": "ok"}},
    }
    persisted = watch_health.PersistedState.empty()
    status, reasons = watch_health._evaluate_snapshot(
        clean, pending_warn=5000, stale_hours=2, persisted=persisted
    )
    _assert(status == watch_health.UP, f"expected UP for clean snap, got {status}: {reasons}")


def test_watch_evaluate_snapshot_degraded_sla() -> None:
    import watch_health  # type: ignore

    snap = {
        "ok": True,
        "failed_or_dead": {},
        "sla_breaches_classify": 7,
        "pending_total": 0,
        "throughput_24h": {"done": 100},
        "poller_state": {},
    }
    persisted = watch_health.PersistedState.empty()
    status, reasons = watch_health._evaluate_snapshot(
        snap, pending_warn=5000, stale_hours=2, persisted=persisted
    )
    _assert(status == watch_health.DEGRADED, "expected DEGRADED for sla > 0")
    _assert(any("sla_breach_classify=7" in r for r in reasons), f"sla in reasons: {reasons}")


def test_watch_evaluate_snapshot_degraded_dead_lane() -> None:
    import watch_health  # type: ignore

    snap = {
        "ok": True,
        "failed_or_dead": {"classify_dead": 3, "summarize_failed": 1},
        "sla_breaches_classify": 0,
        "pending_total": 0,
        "throughput_24h": {"done": 100},
        "poller_state": {},
    }
    persisted = watch_health.PersistedState.empty()
    status, reasons = watch_health._evaluate_snapshot(
        snap, pending_warn=5000, stale_hours=2, persisted=persisted
    )
    _assert(status == watch_health.DEGRADED, "expected DEGRADED for dead lane")
    _assert(any("classify_dead" in r for r in reasons), f"dead lane in reasons: {reasons}")


def test_watch_evaluate_snapshot_degraded_pending() -> None:
    import watch_health  # type: ignore

    snap = {
        "ok": True,
        "failed_or_dead": {},
        "sla_breaches_classify": 0,
        "pending_total": 7000,
        "throughput_24h": {"done": 100},
        "poller_state": {},
    }
    persisted = watch_health.PersistedState.empty()
    status, reasons = watch_health._evaluate_snapshot(
        snap, pending_warn=5000, stale_hours=2, persisted=persisted
    )
    _assert(status == watch_health.DEGRADED, "expected DEGRADED for pending > 5000")
    _assert(any("pending_total=7000" in r for r in reasons), f"pending in reasons: {reasons}")


def test_watch_evaluate_snapshot_ok_false() -> None:
    import watch_health  # type: ignore

    snap = {"ok": False, "error": "ImportError: boom"}
    persisted = watch_health.PersistedState.empty()
    status, reasons = watch_health._evaluate_snapshot(
        snap, pending_warn=5000, stale_hours=2, persisted=persisted
    )
    _assert(status == watch_health.DEGRADED, "expected DEGRADED for ok=False")
    _assert(any("ok=False" in r for r in reasons), f"ok=False in reasons: {reasons}")


def test_watch_evaluate_liveness() -> None:
    import watch_health  # type: ignore

    status, reasons = watch_health._evaluate_liveness(True, "ok")
    _assert(status == watch_health.UP, "ok -> UP")

    status, reasons = watch_health._evaluate_liveness(False, "http_503")
    _assert(status == watch_health.DOWN, "fail -> DOWN")
    _assert(reasons == ["http_503"], f"reason: {reasons}")


def test_watch_state_roundtrip() -> None:
    import watch_health  # type: ignore

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "state.json"
        store = watch_health.StateStore(path=path)
        store.load()  # empty file -- no-op
        ps = store.get("poller")
        ps.status = watch_health.DEGRADED
        ps.last_message = "sla_breach_classify=3"
        ps.last_change_at = watch_health._now_iso()
        store.save()

        _assert(path.exists(), "state file should exist after save")
        raw = json.loads(path.read_text(encoding="utf-8"))
        _assert("poller" in raw["targets"], "poller persisted")
        _assert(raw["targets"]["poller"]["status"] == "DEGRADED", "status persisted")

        # Reload + verify roundtrip
        store2 = watch_health.StateStore(path=path)
        store2.load()
        ps2 = store2.get("poller")
        _assert(ps2.status == watch_health.DEGRADED, "DEGRADED after reload")
        _assert(ps2.last_message == "sla_breach_classify=3", "message after reload")


def test_watch_format_alert() -> None:
    import watch_health  # type: ignore

    target = watch_health.WatchTarget(
        "worker", "http://127.0.0.1:8505/health", "snapshot"
    )
    msg = watch_health._format_alert(
        target, watch_health.UP, watch_health.DEGRADED, ["sla_breach_classify=5"]
    )
    _assert(msg.startswith("[WARN] worker: UP -> DEGRADED"), f"alert prefix: {msg}")
    _assert("sla_breach_classify=5" in msg, "reasons rendered")

    msg = watch_health._format_alert(target, watch_health.DEGRADED, watch_health.UP, [])
    _assert(msg.startswith("[OK] worker: DEGRADED -> UP"), f"recovery alert: {msg}")


CHECKS = [
    ("run_all._build_specs", test_run_all_build_specs),
    ("run_all._backoff_for", test_run_all_backoff),
    ("run_all CLI rc=2 on empty", test_run_all_no_children_exits_2),
    ("watch._evaluate_snapshot UP", test_watch_evaluate_snapshot_up),
    ("watch._evaluate_snapshot DEGRADED (sla)", test_watch_evaluate_snapshot_degraded_sla),
    ("watch._evaluate_snapshot DEGRADED (dead-lane)", test_watch_evaluate_snapshot_degraded_dead_lane),
    ("watch._evaluate_snapshot DEGRADED (pending)", test_watch_evaluate_snapshot_degraded_pending),
    ("watch._evaluate_snapshot DEGRADED (ok=False)", test_watch_evaluate_snapshot_ok_false),
    ("watch._evaluate_liveness", test_watch_evaluate_liveness),
    ("watch.StateStore roundtrip", test_watch_state_roundtrip),
    ("watch._format_alert", test_watch_format_alert),
]


def main() -> int:
    print(f"running {len(CHECKS)} smoke check(s)...")
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
