"""Health watcher: polls the 3 process /health endpoints and alerts on transitions.

Companion to ``scripts/run_all.py`` (the B1 supervisor). The supervisor
restarts processes that crash; this script reports the *invisible* failures
the supervisor can't see -- queue stagnation, SLA breaches, dead-letter
growth, poller errors. Alerts fire on state transitions only, never on
sustained state -- so a long-standing degradation doesn't spam.

Targets
-------
- poller   http://127.0.0.1:8504/health   (full /health snapshot)
- worker   http://127.0.0.1:8505/health   (full /health snapshot)
- ui       http://127.0.0.1:8501/         (HTTP 200 liveness only)

State machine per target: ``UP`` / ``DEGRADED`` / ``DOWN``.

Evaluation rules (worker / poller endpoints):
  - HTTP error or timeout                                   -> DOWN
  - response ``ok`` is False                                -> DEGRADED
  - any ``failed_or_dead`` bucket non-empty                 -> DEGRADED
  - ``sla_breaches_classify`` > 0                           -> DEGRADED
  - ``pending_total`` >  ``--pending-warn`` (default 5000)   -> DEGRADED
  - poller_state status not 'ok'                            -> DEGRADED
  - 24h throughput.done unchanged for ``--stale-hours``     -> DEGRADED
  - otherwise                                               -> UP

Evaluation rules (ui endpoint):
  - HTTP error or status != 200                             -> DOWN
  - HTTP 200                                                -> UP

Alerts go to stdout always, and to a Discord webhook if one is provided via
the ``--discord-webhook`` flag or the ``MN_DISCORD_WEBHOOK`` env var.

State is persisted to ``data/health_watcher_state.json`` so transitions
survive watcher restarts.

Usage
-----

    python scripts/watch_health.py                         # 60s interval, stdout
    python scripts/watch_health.py --interval 30
    python scripts/watch_health.py --discord-webhook https://...
    python scripts/watch_health.py --once                  # single probe + exit

This is the B2 deliverable from the cutover plan (Operational Hardening).
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from market_notification.ops.logging import configure_logging, get_logger  # noqa: E402


UP = "UP"
DEGRADED = "DEGRADED"
DOWN = "DOWN"
STATUSES = (UP, DEGRADED, DOWN)

DEFAULT_STATE_PATH = PROJECT_ROOT / "data" / "health_watcher_state.json"
DEFAULT_INTERVAL_S = 60
DEFAULT_TIMEOUT_S = 10
DEFAULT_PENDING_WARN = 5000
DEFAULT_STALE_HOURS = 2

ALERT_EMOJI = {UP: "[OK]", DEGRADED: "[WARN]", DOWN: "[DOWN]"}


@dataclass
class WatchTarget:
    name: str
    url: str
    kind: str  # "snapshot" or "liveness"


@dataclass
class PersistedState:
    """Per-target state snapshot persisted across watcher restarts."""

    status: str = UP
    last_change_at: str = ""  # ISO 8601 UTC
    last_done_count: int | None = None
    last_done_change_at: str = ""
    last_message: str = ""

    @classmethod
    def empty(cls) -> "PersistedState":
        return cls(status=UP, last_change_at=_now_iso(), last_message="initialized")


@dataclass
class StateStore:
    """Reads / writes ``health_watcher_state.json``. One file, all targets."""

    path: Path
    targets: dict[str, PersistedState] = field(default_factory=dict)

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        for name, blob in raw.get("targets", {}).items():
            try:
                self.targets[name] = PersistedState(**blob)
            except TypeError:
                # Schema drift: ignore unknown keys, keep defaults
                self.targets[name] = PersistedState.empty()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "saved_at": _now_iso(),
            "targets": {name: ps.__dict__ for name, ps in self.targets.items()},
        }
        # Atomic write: temp file + rename.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, name: str) -> PersistedState:
        if name not in self.targets:
            self.targets[name] = PersistedState.empty()
        return self.targets[name]


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _hours_since(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return 0.0
    return (datetime.now(tz=timezone.utc) - then).total_seconds() / 3600.0


def _probe(target: WatchTarget, timeout_s: int) -> tuple[bool, dict[str, Any] | None, str]:
    """Hit ``target.url``. Return ``(ok, snapshot_or_None, reason)``."""
    try:
        resp = requests.get(target.url, timeout=timeout_s)
    except requests.RequestException as exc:
        return False, None, f"transport_error: {type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return False, None, f"http_{resp.status_code}"
    if target.kind == "liveness":
        return True, None, "ok"
    try:
        snap = resp.json()
    except ValueError:
        return False, None, "non_json"
    return True, snap, "ok"


def _evaluate_snapshot(
    snap: dict[str, Any],
    *,
    pending_warn: int,
    stale_hours: int,
    persisted: PersistedState,
) -> tuple[str, list[str]]:
    """Return ``(status, reasons[])`` for a /health snapshot."""
    reasons: list[str] = []
    if not snap.get("ok", False):
        reasons.append(f"ok=False ({snap.get('error', 'unknown')})")
        return DEGRADED, reasons

    failed = snap.get("failed_or_dead") or {}
    dead_keys = [k for k in failed if k.endswith("_dead") and failed[k]]
    failed_keys = [k for k in failed if k.endswith("_failed") and failed[k]]
    if dead_keys:
        reasons.append(f"dead_lane: {','.join(dead_keys)}")
    if failed_keys:
        reasons.append(f"failed_lane: {','.join(failed_keys)}")

    sla = int(snap.get("sla_breaches_classify", 0) or 0)
    if sla > 0:
        reasons.append(f"sla_breach_classify={sla}")

    pending = int(snap.get("pending_total", 0) or 0)
    if pending > pending_warn:
        reasons.append(f"pending_total={pending}>{pending_warn}")

    # Poller state -- only present in the poller snapshot.
    poller_state = snap.get("poller_state") or {}
    for src, info in poller_state.items():
        if not isinstance(info, dict):
            continue
        status = info.get("status")
        if status and status != "ok":
            reasons.append(f"poller[{src}].status={status}")
            err = info.get("error_message")
            if err:
                reasons.append(f"poller[{src}].err={err[:80]}")

    # Throughput stagnation -- only on worker snapshots.
    throughput = snap.get("throughput_24h") or {}
    done_count = throughput.get("done")
    if done_count is not None:
        if persisted.last_done_count is None or done_count != persisted.last_done_count:
            persisted.last_done_count = int(done_count)
            persisted.last_done_change_at = _now_iso()
        else:
            stale_h = _hours_since(persisted.last_done_change_at)
            if stale_h >= stale_hours:
                reasons.append(
                    f"throughput.done stagnant for {stale_h:.1f}h "
                    f"(>= {stale_hours}h threshold)"
                )

    return (DEGRADED if reasons else UP), reasons


def _evaluate_liveness(ok: bool, reason: str) -> tuple[str, list[str]]:
    if ok:
        return UP, []
    return DOWN, [reason]


def _format_alert(
    target: WatchTarget,
    old: str,
    new: str,
    reasons: list[str],
) -> str:
    head = f"{ALERT_EMOJI[new]} {target.name}: {old} -> {new}"
    if reasons:
        body = "; ".join(reasons)
        return f"{head}  |  {body}"
    return head


def _post_discord(webhook_url: str, message: str, log) -> None:
    try:
        r = requests.post(webhook_url, json={"content": message}, timeout=10)
        if r.status_code >= 300:
            log.warning("discord webhook returned %s: %s", r.status_code, r.text[:200])
    except requests.RequestException as exc:
        log.warning("discord webhook failed: %s", exc)


class Watcher:
    def __init__(
        self,
        targets: list[WatchTarget],
        store: StateStore,
        *,
        interval_s: int,
        timeout_s: int,
        pending_warn: int,
        stale_hours: int,
        discord_webhook: str | None,
    ) -> None:
        self.targets = targets
        self.store = store
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self.pending_warn = pending_warn
        self.stale_hours = stale_hours
        self.discord_webhook = discord_webhook
        self.log = get_logger("scripts.watch_health")
        self._stop = threading.Event()

    def request_stop(self, reason: str = "explicit") -> None:
        if not self._stop.is_set():
            self.log.info("stop requested (%s)", reason)
        self._stop.set()

    def _evaluate(self, target: WatchTarget) -> tuple[str, list[str]]:
        ok, snap, reason = _probe(target, self.timeout_s)
        if not ok:
            return DOWN, [reason]
        if target.kind == "liveness":
            return _evaluate_liveness(ok, reason)
        # snap is guaranteed when target.kind == "snapshot" and ok
        assert snap is not None
        return _evaluate_snapshot(
            snap,
            pending_warn=self.pending_warn,
            stale_hours=self.stale_hours,
            persisted=self.store.get(target.name),
        )

    def tick(self) -> list[str]:
        """One probe pass over all targets. Returns alert lines emitted."""
        alerts: list[str] = []
        for target in self.targets:
            persisted = self.store.get(target.name)
            old_status = persisted.status
            try:
                new_status, reasons = self._evaluate(target)
            except Exception as exc:  # noqa: BLE001
                self.log.exception("evaluate raised on %s", target.name)
                new_status, reasons = DOWN, [f"evaluator_error: {exc}"]

            persisted.status = new_status
            persisted.last_message = "; ".join(reasons) if reasons else "ok"
            if new_status != old_status:
                persisted.last_change_at = _now_iso()
                alert = _format_alert(target, old_status, new_status, reasons)
                alerts.append(alert)
                self.log.info("ALERT %s", alert)
                print(alert, flush=True)
                if self.discord_webhook:
                    _post_discord(self.discord_webhook, alert, self.log)
            else:
                self.log.debug(
                    "stable name=%s status=%s reasons=%s",
                    target.name, new_status, reasons,
                )
        self.store.save()
        return alerts

    def run(self) -> int:
        self.log.info(
            "watcher starting; %d target(s); interval=%ds discord=%s",
            len(self.targets), self.interval_s, "yes" if self.discord_webhook else "no",
        )
        while not self._stop.is_set():
            self.tick()
            slept = 0.0
            while slept < self.interval_s and not self._stop.is_set():
                time.sleep(0.5)
                slept += 0.5
        self.log.info("watcher stopped")
        return 0


def _build_targets(
    *, poller_port: int, worker_port: int, ui_port: int, skip: set[str]
) -> list[WatchTarget]:
    out: list[WatchTarget] = []
    if "poller" not in skip:
        out.append(WatchTarget("poller", f"http://127.0.0.1:{poller_port}/health", "snapshot"))
    if "worker" not in skip:
        out.append(WatchTarget("worker", f"http://127.0.0.1:{worker_port}/health", "snapshot"))
    if "ui" not in skip:
        out.append(WatchTarget("ui", f"http://127.0.0.1:{ui_port}/", "liveness"))
    return out


@click.command()
@click.option("--interval", default=DEFAULT_INTERVAL_S, show_default=True, type=int,
              help="Seconds between probe rounds.")
@click.option("--timeout", default=DEFAULT_TIMEOUT_S, show_default=True, type=int,
              help="Per-probe HTTP timeout.")
@click.option("--pending-warn", default=DEFAULT_PENDING_WARN, show_default=True, type=int,
              help="pending_total above this triggers DEGRADED.")
@click.option("--stale-hours", default=DEFAULT_STALE_HOURS, show_default=True, type=int,
              help="Hours of unchanged throughput.done that trigger DEGRADED.")
@click.option("--poller-port", default=8504, show_default=True, type=int)
@click.option("--worker-port", default=8505, show_default=True, type=int)
@click.option("--ui-port", default=8501, show_default=True, type=int)
@click.option("--skip", multiple=True, type=click.Choice(["poller", "worker", "ui"]),
              help="Targets to skip (repeatable).")
@click.option("--discord-webhook", default=None,
              help="Discord webhook URL. Falls back to MN_DISCORD_WEBHOOK env var.")
@click.option("--state-file", default=str(DEFAULT_STATE_PATH), show_default=True,
              type=click.Path(dir_okay=False),
              help="Where to persist watcher state (transitions survive restarts).")
@click.option("--once", is_flag=True, help="Probe once, alert if needed, exit.")
def main(
    interval: int,
    timeout: int,
    pending_warn: int,
    stale_hours: int,
    poller_port: int,
    worker_port: int,
    ui_port: int,
    skip: tuple[str, ...],
    discord_webhook: str | None,
    state_file: str,
    once: bool,
) -> int:
    configure_logging(process_name="health-watcher")
    log = get_logger("scripts.watch_health")

    targets = _build_targets(
        poller_port=poller_port,
        worker_port=worker_port,
        ui_port=ui_port,
        skip=set(skip),
    )
    if not targets:
        log.error("All targets skipped; nothing to do.")
        sys.exit(2)

    webhook = discord_webhook or os.environ.get("MN_DISCORD_WEBHOOK") or None
    store = StateStore(path=Path(state_file))
    store.load()

    watcher = Watcher(
        targets,
        store,
        interval_s=interval,
        timeout_s=timeout,
        pending_warn=pending_warn,
        stale_hours=stale_hours,
        discord_webhook=webhook,
    )

    def _on_sigint(_signum, _frame):  # noqa: ARG001
        watcher.request_stop("SIGINT")

    signal.signal(signal.SIGINT, _on_sigint)
    try:
        signal.signal(signal.SIGTERM, _on_sigint)
    except (ValueError, AttributeError):
        pass

    if once:
        alerts = watcher.tick()
        log.info("single tick produced %d alert(s)", len(alerts))
        return 0

    return watcher.run()


if __name__ == "__main__":
    raise SystemExit(main())  # pylint: disable=no-value-for-parameter
