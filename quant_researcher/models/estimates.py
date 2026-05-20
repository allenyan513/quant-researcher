"""FMP `/analyst-estimates` — forward-looking consensus forecasts.

PK `(symbol, fiscal_date, period)`. Unlike financials these rows are revised
continuously as analysts update their numbers, so the refresh uses
`session.merge` to overwrite the existing row (not append). `known_at` is
ingestion time since the endpoint exposes no publication timestamp.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class AnalystEstimate(Base):
    __tablename__ = "analyst_estimates"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    fiscal_date: Mapped[date] = mapped_column(Date, primary_key=True)
    period: Mapped[str] = mapped_column(String(8), primary_key=True)

    revenue_avg: Mapped[float | None] = mapped_column(Float)
    revenue_low: Mapped[float | None] = mapped_column(Float)
    revenue_high: Mapped[float | None] = mapped_column(Float)
    eps_avg: Mapped[float | None] = mapped_column(Float)
    eps_low: Mapped[float | None] = mapped_column(Float)
    eps_high: Mapped[float | None] = mapped_column(Float)
    ebitda_avg: Mapped[float | None] = mapped_column(Float)
    net_income_avg: Mapped[float | None] = mapped_column(Float)
    num_analysts_revenue: Mapped[int | None] = mapped_column(Integer)
    num_analysts_eps: Mapped[int | None] = mapped_column(Integer)

    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
