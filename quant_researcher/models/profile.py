"""Company profile (FMP /profile).

One row per ticker — refresh replaces the row (no history per D6 / v1; if we
ever want point-in-time profile, promote to (symbol, known_at) composite PK).
Slow-moving identity attributes (sector, industry, exchange, currency,
country, beta, ipo_date, …) plus the full raw payload for anything we
haven't promoted to a column yet.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class Profile(Base):
    __tablename__ = "profiles"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)

    company_name: Mapped[str | None] = mapped_column(String(256))
    sector: Mapped[str | None] = mapped_column(String(128))
    industry: Mapped[str | None] = mapped_column(String(256))
    exchange: Mapped[str | None] = mapped_column(String(32))
    currency: Mapped[str | None] = mapped_column(String(8))
    country: Mapped[str | None] = mapped_column(String(8))
    beta: Mapped[float | None] = mapped_column(Float)
    ipo_date: Mapped[date | None] = mapped_column(Date)
    is_etf: Mapped[bool | None] = mapped_column(Boolean)
    is_fund: Mapped[bool | None] = mapped_column(Boolean)
    is_adr: Mapped[bool | None] = mapped_column(Boolean)
    is_actively_trading: Mapped[bool | None] = mapped_column(Boolean)

    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    known_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
