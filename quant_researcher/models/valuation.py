"""Valuation snapshots — one row per (symbol, model_type, run).

Per implementation-plan.md §6 "qr 专属"-section: every research artifact lives
under an immutable snapshot row that captures `assumptions` + `result` + the
code/data versions, so a Claude-driven decision can be replayed later.

`assumptions` and `result` are JSON for v1 schemaless flexibility — each
model emits its own shape (DCF includes WACC + terminal_growth + horizon;
multiples include the per-multiple peer median; etc.). `sensitivity` is
JSON too (typically a 5×5 grid for DCF).

This is the only MC-introduced table; sector medians are computed on
demand from `profiles` + `financial_ratios` rather than persisted in a
separate `sector_betas` table (deferred to MG if signal research needs
historically-stable sector data).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class ValuationSnapshot(Base):
    __tablename__ = "valuation_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    model_type: Mapped[str] = mapped_column(String(32), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)

    fair_value_per_share: Mapped[float | None] = mapped_column(Float)
    current_price: Mapped[float | None] = mapped_column(Float)
    upside_pct: Mapped[float | None] = mapped_column(Float)

    assumptions: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    sensitivity: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    code_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
