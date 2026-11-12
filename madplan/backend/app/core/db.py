from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from app.core.config import settings


def _build_engine():
    url = settings.sqlalchemy_url
    if str(url).startswith("sqlite"):
        # The web server and the background jobs share one file, so a writer waits for another
        # rather than failing, and readers do not block writers.
        kwargs: dict = {"connect_args": {"check_same_thread": False, "timeout": 30}}
        if ":memory:" in str(url):
            kwargs["poolclass"] = StaticPool
        built = create_engine(url, **kwargs)

        @event.listens_for(built, "connect")
        def _sqlite_pragmas(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

        return built
    # Postgres stays possible for development and CI.
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Always aware and in UTC, including on SQLite which drops the offset."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to a UTCDateTime column")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


def local_today() -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


def local_hour(moment: datetime) -> int:
    """The hour on the household's clocks. A container's own clock normally runs on UTC."""
    return moment.astimezone(ZoneInfo(settings.timezone)).hour


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
