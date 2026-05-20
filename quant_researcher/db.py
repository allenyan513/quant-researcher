"""Database engine + session factory + declarative Base.

Per I2 / D9 / D10: SQLAlchemy 2.x + Alembic, Postgres dialect, DSN from
`QR_DATABASE_URL` (default = Supabase). Models (added from MA onward) inherit
from `Base`.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from quant_researcher.config import settings


class Base(DeclarativeBase):
    """Shared SQLAlchemy declarative base for all quant-researcher models."""


@lru_cache(maxsize=1)
def engine() -> Engine:
    """Cached SQLAlchemy engine bound to QR_DATABASE_URL.

    `pool_pre_ping=True` defends against stale connections (relevant for
    Supabase / managed Postgres which may drop idle connections).
    """
    return create_engine(
        settings().qr_database_url,
        future=True,
        pool_pre_ping=True,
    )


@lru_cache(maxsize=1)
def session_factory() -> sessionmaker[Session]:
    """Cached session factory bound to the project engine."""
    return sessionmaker(bind=engine(), expire_on_commit=False, future=True)
