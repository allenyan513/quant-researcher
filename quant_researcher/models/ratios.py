"""FMP `/ratios` — period-keyed derived ratios.

PK `(symbol, period, fiscal_date)`. `known_at` falls back to ingestion time
because the `/ratios` endpoint doesn't expose an `acceptedDate`; for strict
point-in-time, callers should join `income_statement` on the same PK and use
that row's `known_at`. This is a v1 pragmatic compromise documented in MA-3.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class FinancialRatios(Base):
    __tablename__ = "financial_ratios"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    period: Mapped[str] = mapped_column(String(8), primary_key=True)
    fiscal_date: Mapped[date] = mapped_column(Date, primary_key=True)

    pe_ratio: Mapped[float | None] = mapped_column(Float)
    peg_ratio: Mapped[float | None] = mapped_column(Float)
    price_to_book: Mapped[float | None] = mapped_column(Float)
    price_to_sales: Mapped[float | None] = mapped_column(Float)
    ev_to_ebitda: Mapped[float | None] = mapped_column(Float)
    ev_to_sales: Mapped[float | None] = mapped_column(Float)
    current_ratio: Mapped[float | None] = mapped_column(Float)
    debt_to_equity: Mapped[float | None] = mapped_column(Float)
    return_on_equity: Mapped[float | None] = mapped_column(Float)
    return_on_assets: Mapped[float | None] = mapped_column(Float)
    gross_margin: Mapped[float | None] = mapped_column(Float)
    operating_margin: Mapped[float | None] = mapped_column(Float)
    net_margin: Mapped[float | None] = mapped_column(Float)
    fcf_yield: Mapped[float | None] = mapped_column(Float)
    payout_ratio: Mapped[float | None] = mapped_column(Float)

    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
