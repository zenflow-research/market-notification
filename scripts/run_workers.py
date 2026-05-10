"""Entry point for the always-on worker process (single-threaded dispatcher).

Phase 0: stub. Phase 10 wires the real Dispatcher.
"""
from __future__ import annotations

import sys
from pathlib import Path

import click

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from market_notification.ops.logging import configure_logging, get_logger  # noqa: E402


@click.command()
@click.option("--once", is_flag=True, help="Process one task then exit.")
def main(once: bool) -> int:
    configure_logging()
    log = get_logger("scripts.run_workers")
    log.info("Worker stub starting. once=%s", once)
    log.warning("Phase 0 stub — real implementation arrives in Phase 10.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())  # pylint: disable=no-value-for-parameter
