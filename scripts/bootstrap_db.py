"""Create the SQLite DB (if missing), apply schema via Base.metadata.create_all.

Idempotent: safe to run multiple times. Future schema changes go through
Alembic migrations; this script is for first-time bootstrap and tests.

Usage:
    python -m scripts.bootstrap_db
    python -m scripts.bootstrap_db --drop  (DESTRUCTIVE - wipes existing DB)
"""
from __future__ import annotations

import sys
from pathlib import Path

import click

# Ensure src/ is on path when running as a script
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from market_notification.config.settings import get_settings  # noqa: E402
from market_notification.db.models import Base  # noqa: E402
from market_notification.db.session import dispose_engine, get_engine  # noqa: E402
from market_notification.ops.logging import configure_logging, get_logger  # noqa: E402


@click.command()
@click.option(
    "--drop",
    is_flag=True,
    default=False,
    help="DESTRUCTIVE: drop all tables before recreating.",
)
def main(drop: bool) -> int:
    configure_logging()
    log = get_logger("scripts.bootstrap_db")

    settings = get_settings()
    log.info("DB URL: %s", settings.db.url)

    # Ensure data dir exists
    if settings.db.url.startswith("sqlite:///"):
        db_path = Path(settings.db.url.replace("sqlite:///", ""))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        log.info("Ensured data dir: %s", db_path.parent)

    engine = get_engine()

    try:
        if drop:
            log.warning("--drop set: dropping all tables")
            Base.metadata.drop_all(engine)

        log.info("Creating tables: %s", ", ".join(Base.metadata.tables.keys()))
        Base.metadata.create_all(engine)
        log.info("Bootstrap OK. %d tables present.", len(Base.metadata.tables))
    finally:
        dispose_engine()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())  # pylint: disable=no-value-for-parameter
