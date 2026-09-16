"""Engine/session factory. Reads connection info from the DATABASE_URL env
var so nothing environment-specific is hardcoded here.

Expected format (psycopg 3 driver):
    postgresql+psycopg://user:password@host:5432/well_platform
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DATABASE_URL = "postgresql+psycopg://localhost:5432/well_platform"


def get_engine(database_url: str | None = None):
    url = database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    return create_engine(url, pool_pre_ping=True, future=True)


_SessionFactory: sessionmaker | None = None


def _factory() -> sessionmaker:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Standard unit-of-work context manager: commits on clean exit,
    rolls back on exception.

    Usage:
        with session_scope() as session:
            repo = WellRepository(session)
            repo.upsert_many(wells)
    """
    session = _factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
