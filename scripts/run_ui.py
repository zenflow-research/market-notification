"""Entry point for the Streamlit UI process.

Phase 0: launcher stub. Phase 12 wires the real Streamlit app.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
APP_PATH = SRC / "market_notification" / "ui" / "app.py"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from market_notification.config.settings import get_settings  # noqa: E402
from market_notification.ops.logging import configure_logging, get_logger  # noqa: E402


@click.command()
@click.option("--port", type=int, default=None, help="Override UI port.")
def main(port: int | None) -> int:
    configure_logging()
    log = get_logger("scripts.run_ui")
    settings = get_settings()
    chosen_port = port or settings.ui.port

    if not APP_PATH.exists():
        log.warning(
            "UI app not yet implemented (%s missing). Phase 12 builds this. Exiting.",
            APP_PATH,
        )
        return 0

    log.info("Launching Streamlit on port %d", chosen_port)
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(APP_PATH),
        "--server.port",
        str(chosen_port),
        "--server.headless",
        "true",
    ]
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())  # pylint: disable=no-value-for-parameter
