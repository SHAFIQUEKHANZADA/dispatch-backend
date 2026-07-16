"""Database engine + portable column types.

The production target is Supabase/PostgreSQL and the SQL in supabase/migrations
is the source of truth for the schema.  The SQLAlchemy models mirror it.

The column types below are dialect-portable so the exact same models also run on
SQLite, which is what lets a reviewer clone this repo and see the dispatch board
with real data in about ninety seconds instead of standing up a Postgres first.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, Text, TypeDecorator
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=settings.db_echo,
    future=True,
    # asyncpg + pgbouncer (Supabase pooler) do not get along with prepared
    # statement caching; harmless to set unconditionally for postgres.
    **({"connect_args": {"statement_cache_size": 0}} if "asyncpg" in settings.database_url else {}),
)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session():
    async with SessionLocal() as session:
        yield session


# --------------------------------------------------------------------------- #
# Portable column types                                                        #
# --------------------------------------------------------------------------- #


class UTCDateTime(TypeDecorator):
    """timestamptz that always round-trips as a timezone-aware UTC datetime.

    SQLite has no timezone-aware datetime storage.  Rather than let naive values
    leak into the scoring engine — where a silently naive `promise_at` would
    blow up a comparison against an aware `now` — we normalise on the way in and
    re-attach UTC on the way out.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: Any, dialect):
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            value = value.astimezone(timezone.utc)
            if dialect.name == "sqlite":
                return value.replace(tzinfo=None)  # stored as naive UTC
        return value

    def process_result_value(self, value: Any, dialect):
        if value is None:
            return None
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class StringArray(TypeDecorator):
    """text[] on Postgres, a JSON array on SQLite."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(ARRAY(Text()))
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value, dialect):
        if value is None:
            return []
        return list(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return []
        return list(value)


def JSONBType():
    """jsonb on Postgres, json on SQLite."""
    return JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
