"""SQLAlchemy session/engine factory. WAL mode for SQLite per D-07."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from market_notification.config.settings import get_settings

log = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def _enable_sqlite_wal(dbapi_connection, _connection_record) -> None:  # noqa: ARG001
    """SQLite-specific pragmas: WAL, foreign keys, busy timeout."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.db.url,
            future=True,
            echo=False,
        )
        if settings.db.url.startswith("sqlite"):
            event.listen(_engine, "connect", _enable_sqlite_wal)
            log.info("SQLite engine created with WAL mode at %s", settings.db.url)
        else:
            log.info("Engine created at %s", settings.db.url)
    return _engine


def get_sessionmaker() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(), autoflush=False, autocommit=False, expire_on_commit=False
        )
    return _SessionLocal


@contextmanager
def get_session() -> Iterator[Session]:
    """Yield a session with auto-rollback on exception, commit on success."""
    sess = get_sessionmaker()()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


def dispose_engine() -> None:
    """Dispose engine (e.g. before fork or during shutdown)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
        _engine = None
        _SessionLocal = None
