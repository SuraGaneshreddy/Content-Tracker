"""
Database engine / session management (SQLAlchemy 2.0 style).

SQLite is the default backend because this is a single-user personal app, but
any SQLAlchemy URL can be supplied through DATABASE_URL.
"""

from __future__ import annotations

from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


def _make_engine():
    url = settings.database_url
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        # Needed so a single connection can be shared across threads (FastAPI
        # runs sync route handlers in a threadpool).
        kwargs["connect_args"] = {"check_same_thread": False}
        # Durability + concurrency: WAL lets readers proceed during writes.
        kwargs["isolation_level"] = None
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
            cur = dbapi_conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA synchronous=NORMAL")
            finally:
                cur.close()

    return engine


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a scoped database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables and seed reference data (categories, statuses)."""
    from . import models  # noqa: F401  (registers mappers)

    Base.metadata.create_all(bind=engine)

    from .services.seed import seed_reference_data

    with SessionLocal() as db:
        seed_reference_data(db)
