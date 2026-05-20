"""Three statement tables (income / balance sheet / cash flow), FMP-sourced.

Composite PK `(symbol, period, fiscal_date)` — same symbol can have an annual
("FY") row and a Q4 quarterly row both ending on the same date; the `period`
discriminator keeps them distinct.

**`known_at` semantics (D6 strict, MA-3).** Unlike MA-2's tables (where
`known_at` is the row's ingestion timestamp via `server_default=func.now()`),
financial-statement `known_at` is set explicitly by the refresh code from
FMP's `acceptedDate` field — the moment the filing became public. Point-in-
time queries should filter `WHERE known_at <= :as_of` to avoid look-ahead
bias. The refresh code is responsible for populating this; `nullable=False`
guards against accidental DB-default drift.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class _FinancialStatementMixin:
    """Columns shared across IncomeStatement / BalanceSheet / CashFlow."""

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    period: Mapped[str] = mapped_column(String(8), primary_key=True)
    fiscal_date: Mapped[date] = mapped_column(Date, primary_key=True)

    calendar_year: Mapped[int | None] = mapped_column(Integer)
    reported_currency: Mapped[str | None] = mapped_column(String(8))
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IncomeStatement(_FinancialStatementMixin, Base):
    __tablename__ = "income_statement"

    revenue: Mapped[float | None] = mapped_column(Float)
    cost_of_revenue: Mapped[float | None] = mapped_column(Float)
    gross_profit: Mapped[float | None] = mapped_column(Float)
    operating_income: Mapped[float | None] = mapped_column(Float)
    net_income: Mapped[float | None] = mapped_column(Float)
    eps: Mapped[float | None] = mapped_column(Float)
    eps_diluted: Mapped[float | None] = mapped_column(Float)


class BalanceSheet(_FinancialStatementMixin, Base):
    __tablename__ = "balance_sheet"

    total_assets: Mapped[float | None] = mapped_column(Float)
    total_liabilities: Mapped[float | None] = mapped_column(Float)
    total_equity: Mapped[float | None] = mapped_column(Float)
    cash_and_equivalents: Mapped[float | None] = mapped_column(Float)
    short_term_debt: Mapped[float | None] = mapped_column(Float)
    long_term_debt: Mapped[float | None] = mapped_column(Float)


class CashFlow(_FinancialStatementMixin, Base):
    __tablename__ = "cash_flow"

    operating_cash_flow: Mapped[float | None] = mapped_column(Float)
    investing_cash_flow: Mapped[float | None] = mapped_column(Float)
    financing_cash_flow: Mapped[float | None] = mapped_column(Float)
    capital_expenditure: Mapped[float | None] = mapped_column(Float)
    free_cash_flow: Mapped[float | None] = mapped_column(Float)
    dividends_paid: Mapped[float | None] = mapped_column(Float)
