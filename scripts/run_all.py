"""Supervisor: launches and restarts the 3 market_notification processes.

Replaces 3 manual PowerShell terminals with one. Each child:

- inherits stdout/stderr (so its log lines appear in this terminal too),
- writes its own rotating log file via ``configure_logging``
  (``logs/poller.log`` / ``logs/worker.log`` / ``logs/ui.log``),
- is restarted automatically on exit with ``[5, 10, 30, 60]``-second backoff,
- receives Ctrl+C via Windows console-group propagation; the supervisor
  catches SIGINT itself and waits up to 10 s for clean shutdown before
  forcing termination.

The supervisor itself writes to ``logs/supervisor.log``.

Usage
-----

    python scripts/run_all.py                            # all 3 children
    python scripts/run_all.py --skip ui                  # poller + worker only
    python scripts/run_all.py --poller-port 9504         # custom port

This is the B1 deliverable from the brain notification cutover plan
(see ``G:\\brain\\docs\\memory\\notifications\\architecture.md`` § 0.18 and
the Operational Hardening track in the conversation that introduced this
file). Pair with ``scripts/watch_health.py`` (B2) for transition alerting.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import click

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from market_notification.ops.logging import configure_logging, get_logger  # noqa: E402


BACKOFF_SCHEDULE_S = (5, 10, 30, 60)
STABLE_THRESHOLD_S = 60
GRACEFUL_SHUTDOWN_TIMEOUT_S = 10
TICK_INTERVAL_S = 2

CHILD_NAMES = ("poller", "worker", "ui")


@dataclass
class ChildSpec:
    """One supervised child process.

    ``cmd`` is the full argv passed to ``subprocess.Popen``. ``health_port`` is
    informational only (we don't call ``/health`` here -- that's B2's job).
    """

    name: str
    cmd: list[str]
    health_port: int | None = None
    consecutive_failures: int = 0
    last_start_ts: float = 0.0
    process: subprocess.Popen | None = field(default=None, repr=False)

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None


def _build_specs(
    *,
    poller_port: int,
    worker_port: int,
    ui_port: int,
    skip: set[str],
) -> list[ChildSpec]:
    py = sys.executable
    scripts = PROJECT_ROOT / "scripts"
    out: list[ChildSpec] = []
    if "poller" not in skip:
        out.append(
            ChildSpec(
                name="poller",
                cmd=[py, str(scripts / "run_poller.py"), "--health-port", str(poller_port)],
                health_port=poller_port,
            )
        )
    if "worker" not in skip:
        out.append(
            ChildSpec(
                name="worker",
                cmd=[py, str(scripts / "run_workers.py"), "--health-port", str(worker_port)],
                health_port=worker_port,
            )
        )
    if "ui" not in skip:
        out.append(
            ChildSpec(
                name="ui",
                cmd=[py, str(scripts / "run_ui.py"), "--port", str(ui_port)],
                health_port=ui_port,
            )
        )
    return out


def _backoff_for(consecutive_failures: int) -> int:
    idx = min(max(consecutive_failures - 1, 0), len(BACKOFF_SCHEDULE_S) - 1)
    return BACKOFF_SCHEDULE_S[idx]


class Supervisor:
    """Owns the lifecycle of the configured ``ChildSpec`` list."""

    def __init__(self, specs: list[ChildSpec]) -> None:
        self.specs = specs
        self.log = get_logger("scripts.run_all")
        self._stop = threading.Event()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def request_stop(self, reason: str = "explicit") -> None:
        if not self._stop.is_set():
            self.log.info("stop requested (%s)", reason)
        self._stop.set()

    def _start_child(self, spec: ChildSpec) -> None:
        if spec.is_alive():
            return
        spec.last_start_ts = time.time()
        try:
            spec.process = subprocess.Popen(
                spec.cmd,
                cwd=str(PROJECT_ROOT),
                # Inherit stdout/stderr -- child also writes its own log file
                # via configure_logging, so this terminal carries everything.
            )
            self.log.info(
                "spawned name=%s pid=%d health_port=%s cmd=%s",
                spec.name,
                spec.process.pid,
                spec.health_port,
                " ".join(spec.cmd[1:]),
            )
        except Exception as exc:  # noqa: BLE001
            self.log.exception("spawn failed name=%s: %s", spec.name, exc)
            spec.consecutive_failures += 1

    def _handle_exit(self, spec: ChildSpec, rc: int) -> None:
        ran_for = time.time() - spec.last_start_ts
        if ran_for >= STABLE_THRESHOLD_S:
            # Treated as a clean run; reset failure streak.
            spec.consecutive_failures = 0
        else:
            spec.consecutive_failures += 1
        backoff = _backoff_for(spec.consecutive_failures)
        self.log.warning(
            "child exited name=%s rc=%s ran_for=%.1fs consecutive_failures=%d "
            "restart_in_s=%d",
            spec.name,
            rc,
            ran_for,
            spec.consecutive_failures,
            backoff,
        )
        # Wait the backoff, but bail out early if a stop is requested.
        waited = 0
        while waited < backoff and not self._stop.is_set():
            time.sleep(1)
            waited += 1
        spec.process = None

    def _tick(self) -> None:
        for spec in self.specs:
            if self._stop.is_set():
                return
            if spec.process is None:
                self._start_child(spec)
                continue
            rc = spec.process.poll()
            if rc is None:
                continue  # still running
            self._handle_exit(spec, rc)
            if not self._stop.is_set():
                self._start_child(spec)

    def run(self) -> int:
        self.log.info(
            "supervisor starting; managing %d child(ren): %s",
            len(self.specs),
            ", ".join(s.name for s in self.specs),
        )
        try:
            while not self._stop.is_set():
                self._tick()
                # Sleep in small chunks so SIGINT lands promptly.
                slept = 0.0
                while slept < TICK_INTERVAL_S and not self._stop.is_set():
                    time.sleep(0.5)
                    slept += 0.5
        except KeyboardInterrupt:
            self.request_stop("KeyboardInterrupt")
        return self._shutdown()

    def _shutdown(self) -> int:
        self.log.info("shutdown: waiting up to %ds for clean child exit", GRACEFUL_SHUTDOWN_TIMEOUT_S)
        deadline = time.time() + GRACEFUL_SHUTDOWN_TIMEOUT_S
        while time.time() < deadline:
            if all(not s.is_alive() for s in self.specs):
                break
            time.sleep(0.5)
        # Anyone still alive gets terminated.
        for spec in self.specs:
            proc = spec.process
            if proc is not None and proc.poll() is None:
                self.log.warning("forcing terminate name=%s pid=%d", spec.name, proc.pid)
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    self.log.exception("terminate failed name=%s", spec.name)
        # Final reap.
        for spec in self.specs:
            if spec.process is None:
                continue
            try:
                spec.process.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    spec.process.kill()
                except Exception:  # noqa: BLE001
                    pass
        self.log.info("supervisor stopped")
        return 0


@click.command()
@click.option(
    "--skip",
    multiple=True,
    type=click.Choice(CHILD_NAMES),
    help="Children to skip (repeatable). e.g. --skip ui",
)
@click.option("--poller-port", default=8504, show_default=True, type=int)
@click.option("--worker-port", default=8505, show_default=True, type=int)
@click.option("--ui-port", default=8501, show_default=True, type=int)
def main(skip: tuple[str, ...], poller_port: int, worker_port: int, ui_port: int) -> int:
    configure_logging(process_name="supervisor")
    log = get_logger("scripts.run_all")
    specs = _build_specs(
        poller_port=poller_port,
        worker_port=worker_port,
        ui_port=ui_port,
        skip=set(skip),
    )
    if not specs:
        log.error("All children skipped; nothing to do.")
        sys.exit(2)

    sv = Supervisor(specs)

    def _on_sigint(_signum, _frame):  # noqa: ARG001
        sv.request_stop("SIGINT")

    signal.signal(signal.SIGINT, _on_sigint)
    try:
        signal.signal(signal.SIGTERM, _on_sigint)
    except (ValueError, AttributeError):
        # SIGTERM isn't available on every Windows Python build; skip silently.
        pass

    return sv.run()


if __name__ == "__main__":
    raise SystemExit(main())  # pylint: disable=no-value-for-parameter
