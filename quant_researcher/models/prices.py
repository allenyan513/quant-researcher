"""Daily OHLCV history (FMP /historical-price-eod).

Composite PK (symbol, trade_date) — one row per symbol per trading day.
`known_at` records when we ingested the row (refresh time); `trade_date` is
the as-of date (the trading session the bar describes). Adjusted close is
captured separately because FMP returns both unadjusted and adjusted streams.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class DailyPrice(Base):
    __tablename__ = "daily_prices"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)

    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    adj_close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[int | None] = mapped_column(BigInteger)

    known_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
