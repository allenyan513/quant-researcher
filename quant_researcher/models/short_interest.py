"""FINRA Equity Short Interest (free, bi-monthly).

One row per `(symbol, settlement_date)`. FINRA publishes a single CSV per
settlement date covering all securities (mid-month + month-end, ~7 business-day
lag), free and auth-free. PK `(symbol, settlement_date)` dedups via
`session.merge`. Freshness is judged on `MAX(settlement_date)`. `known_at` is
ingestion time.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class ShortInterest(Base):
    __tablename__ = "short_interest"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    settlement_date: Mapped[date] = mapped_column(Date, primary_key=True)

    short_interest: Mapped[float | None] = mapped_column(Float)
    previous_short_interest: Mapped[float | None] = mapped_column(Float)
    change_pct: Mapped[float | None] = mapped_column(Float)
    avg_daily_volume: Mapped[float | None] = mapped_column(Float)
    days_to_cover: Mapped[float | None] = mapped_column(Float)
    security_name: Mapped[str | None] = mapped_column(String(256))

    known_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
