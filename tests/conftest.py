"""Shared pytest fixtures.

In-memory SQLite for unit tests; tmp_path for file-system tests.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Iterator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture(scope="session", autouse=True)
def _set_test_env() -> None:  # noqa: PT004 - autouse fixture, name referenced by pytest
    os.environ.setdefault("MN_DEBUG", "1")
    os.environ.setdefault("MN_DB__URL", "sqlite:///:memory:")


@pytest.fixture()
def in_memory_engine() -> Iterator[Engine]:
    engine = create_engine("sqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _connection_record):  # noqa: ARG001 - SQLAlchemy event signature
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    _ = _pragmas  # silence "not accessed" - referenced by SQLAlchemy event registry

    from market_notification.db.models import Base

    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def in_memory_session(in_memory_engine: Engine) -> Iterator[Session]:
    Maker = sessionmaker(bind=in_memory_engine, expire_on_commit=False)
    sess = Maker()
    try:
        yield sess
    finally:
        sess.close()


def now_utc_naive() -> datetime:
    """UTC now, tz-naive (matches our DB convention)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def utc_now() -> datetime:
    return now_utc_naive()
