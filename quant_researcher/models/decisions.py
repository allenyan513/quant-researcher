"""Decision ledger + forward-return tracking (MF).

* `Decision` — one row per Claude buy/sell call. Auto-bundle of the
  warehouse state at decision time goes into `research_bundles` and is
  referenced by `bundle_id`. `thesis` / `confidence` / `tags` capture the
  why; `price_at_open` is the close (latest `daily_prices.close`) at open.
* `DecisionTracking` — composite PK `(decision_id, horizon)` where horizon
  ∈ {"1w","1m","3m","6m"}. Filled by `qr ledger track`; rerunning the
  command for the same horizon overwrites (mark prices may shift). Stores
  the symbol's return + SPY return + sector ETF return + alpha (symbol -
  sector benchmark when available, else symbol - SPY).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class Decision(Base):
    __tablename__ = "decisions"

    decision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # buy / sell
    opened_at: Mapped[date] = mapped_column(Date, nullable=False)
    price_at_open: Mapped[float | None] = mapped_column(Float)

    thesis: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[int | None] = mapped_column(Integer)
    tags: Mapped[list[str] | None] = mapped_column(JSON)
    sector_at_open: Mapped[str | None] = mapped_column(String(128))

    bundle_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("research_bundles.bundle_id", ondelete="SET NULL")
    )
    code_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DecisionTracking(Base):
    __tablename__ = "decision_tracking"

    decision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("decisions.decision_id", ondelete="CASCADE"),
        primary_key=True,
    )
    horizon: Mapped[str] = mapped_column(String(4), primary_key=True)  # 1w/1m/3m/6m

    tracked_at: Mapped[date] = mapped_column(Date, nullable=False)
    price: Mapped[float | None] = mapped_column(Float)
    return_pct: Mapped[float | None] = mapped_column(Float)
    spy_return_pct: Mapped[float | None] = mapped_column(Float)
    sector_etf: Mapped[str | None] = mapped_column(String(8))
    sector_return_pct: Mapped[float | None] = mapped_column(Float)
    alpha_pct: Mapped[float | None] = mapped_column(Float)
    extras: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
