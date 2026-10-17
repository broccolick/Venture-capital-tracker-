"""Database connection helpers (SQLite by default, PostgreSQL-compatible)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


SessionLocal = sessionmaker(autoflush=False, expire_on_commit=False)
_engine: Engine | None = None


def configure(url: str) -> Engine:
    """Create the engine for `url` and bind the session factory to it."""
    global _engine
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    SessionLocal.configure(bind=engine)
    _engine = engine
    return engine


def get_engine() -> Engine:
    if _engine is None:
        from .config import get_settings

        configure(get_settings().resolved_database_url)
    assert _engine is not None
    return _engine


def init_db() -> None:
    """Create missing tables and apply safe additive migrations to persisted databases."""
    from . import models  # noqa: F401  (registers the tables)

    engine = get_engine()
    Base.metadata.create_all(engine)
    _ensure_additive_columns(engine)


def _ensure_additive_columns(engine: Engine) -> None:
    """Upgrade older persisted databases with nullable provenance links and current-state flags."""
    additions = {
        "citations": {
            "source_observation_id": "INTEGER REFERENCES source_observations(id) ON DELETE SET NULL",
            "is_current": "BOOLEAN NOT NULL DEFAULT TRUE",
        },
        "discovery_signals": {
            "source_observation_id": "INTEGER REFERENCES source_observations(id) ON DELETE SET NULL",
            "is_current": "BOOLEAN NOT NULL DEFAULT TRUE",
        },
    }
    existing_tables = set(inspect(engine).get_table_names())
    for table, columns in additions.items():
        if table not in existing_tables:
            continue
        existing_columns = {column["name"] for column in inspect(engine).get_columns(table)}
        for column, ddl in columns.items():
            if column not in existing_columns:
                with engine.begin() as connection:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                existing_columns.add(column)


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    get_engine()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
