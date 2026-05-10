"""Entry point for the always-on poller process (D-09 Process 1).

Usage:
    python -m scripts.run_poller          # always-on loop (Ctrl+C to stop)
    python -m scripts.run_poller --once   # one cycle then exit (smoke)
"""
from __future__ import annotations

import signal
import sys
import time
from pathlib import Path

import click

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from market_notification.companies.factory import default_company_provider  # noqa: E402
from market_notification.config.settings import get_settings  # noqa: E402
from market_notification.db.session import get_session  # noqa: E402
from market_notification.exchange.bse_fetcher import BSEFetcher  # noqa: E402
from market_notification.exchange.nse_fetcher import NSEFetcher  # noqa: E402
from market_notification.filter.filter_engine import RegexFilterEngine  # noqa: E402
from market_notification.ops.logging import configure_logging, get_logger  # noqa: E402
from market_notification.poller.poller import Poller  # noqa: E402


def _build_poller() -> Poller:
    settings = get_settings()
    return Poller(
        bse_fetcher=BSEFetcher(),
        nse_fetcher=NSEFetcher(playwright_headless=True),
        company_provider=default_company_provider(),
        filter_engine=RegexFilterEngine(get_session),
        interval_s=settings.poller.interval_s,
        bse_records_per_poll=settings.poller.bse_records_per_poll,
        nse_records_per_poll=settings.poller.nse_records_per_poll,
    )


@click.command()
@click.option("--once", is_flag=True, help="Poll once then exit (smoke test).")
def main(once: bool) -> int:
    configure_logging()
    log = get_logger("scripts.run_poller")
    log.info("Building poller...")
    poller = _build_poller()

    if once:
        log.info("Running poll_once()...")
        bse_r, nse_r = poller.poll_once()
        log.info("BSE: %s", bse_r)
        log.info("NSE: %s", nse_r)
        return 0

    # Always-on mode -- handle Ctrl+C gracefully
    def _handle_sigint(_signum, _frame):  # noqa: ARG001
        log.info("SIGINT received -- stopping")
        poller.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_sigint)
    poller.start()
    log.info("Poller running. Ctrl+C to stop.")
    while poller.is_running:
        time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())  # pylint: disable=no-value-for-parameter
