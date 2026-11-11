"""Engine / session plumbing.

The engine is cached per URL so tests can spin up an isolated SQLite file
without touching application code.  SQLite gets foreign keys enforced (off by
default) and WAL mode so the APScheduler job and the web worker can share the
file without lock stepping on each other.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Settings, get_settings


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model."""


_ENGINES: dict[str, Engine] = {}


def get_engine(database_url: str | None = None, *, echo: bool = False) -> Engine:
    """Return a cached :class:`~sqlalchemy.engine.Engine` for ``database_url``."""
    settings = get_settings()
    url = database_url or settings.database_url
    key = f"{url}|{echo}"
    if key not in _ENGINES:
        engine = create_engine(
            url,
            echo=echo,
            future=True,
            connect_args=settings.sqlalchemy_connect_args(),
        )
        if url.startswith("sqlite"):
            _attach_sqlite_pragmas(engine)
        _ENGINES[key] = engine
    return _ENGINES[key]


def _attach_sqlite_pragmas(engine: Engine) -> None:
    """Enable FK enforcement + WAL on every SQLite connection."""

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:  # pragma: no cover - DB hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def get_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    """Return a :class:`sessionmaker` bound to the cached engine."""
    return sessionmaker(bind=get_engine(database_url), future=True, expire_on_commit=False)


@contextmanager
def session_scope(database_url: str | None = None) -> Iterator[Session]:
    """Transactional session scope: commits on success, rolls back on error."""
    session = get_session_factory(database_url)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_all() -> None:
    """Dispose every cached engine (used by the test suite and CLI shutdown)."""
    for engine in _ENGINES.values():
        engine.dispose()
    _ENGINES.clear()
