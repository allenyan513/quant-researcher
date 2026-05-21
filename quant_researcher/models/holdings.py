"""Holdings — point-in-time positions per (account, symbol, as_of_date).

Sourced from either IBKR Flex Query API ("flex") or a CSV import ("csv"),
with a "manual" path reserved for adjustments. The PK is composite so
nightly snapshots accumulate naturally; `qr holdings history` walks them.

Field mapping from FMP-agnostic Flex XML (see `holdings/ibkr_flex.py`):
* `position` → `quantity` (negative = short)
* `markPrice` → `mark_price`
* `positionValue` → `market_value`
* `costBasisPrice` → `avg_cost`
* `costBasisMoney` → `cost_basis_total`
* `fifoPnlUnrealized` → `unrealized_pnl`
* `percentOfNAV` → `percent_of_nav`
* `assetCategory`/`subCategory`/`side`/`currency`/`conid`/`listingExchange`/
  `description` → eponymous columns
* full attribute dict → `raw` JSON
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Date, DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class Holding(Base):
    __tablename__ = "holdings"

    account_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)

    asset_category: Mapped[str] = mapped_column(String(8), nullable=False)
    sub_category: Mapped[str | None] = mapped_column(String(32))
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    mark_price: Mapped[float | None] = mapped_column(Float)
    market_value: Mapped[float | None] = mapped_column(Float)
    avg_cost: Mapped[float | None] = mapped_column(Float)
    cost_basis_total: Mapped[float | None] = mapped_column(Float)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float)
    percent_of_nav: Mapped[float | None] = mapped_column(Float)
    side: Mapped[str | None] = mapped_column(String(8))
    currency: Mapped[str | None] = mapped_column(String(8))
    fx_rate_to_base: Mapped[float | None] = mapped_column(Float)
    conid: Mapped[int | None] = mapped_column(BigInteger)
    listing_exchange: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(String(256))

    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    known_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
