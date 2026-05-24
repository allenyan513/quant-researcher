"""Trades — executed fills per (account, ib_exec_id), sourced from IBKR Flex.

One row per **execution (fill)**, not per order: a single order can fill in
several partial executions, each with its own `ibExecID` — the natural,
globally-unique dedup key, so it is the PK. The importer `session.merge`s on
it, which makes a re-pull of the same business day idempotent (and lets a
later IBKR correction carrying the same `ibExecID` overwrite in place).

Field mapping from Flex `<Trade>` attrs (see `holdings/ibkr_flex.py`):
* `ibExecID` → `ib_exec_id` (PK); `tradeID` → `trade_id`
* `buySell` → `side`; `tradePrice` → `price`; `quantity` → `quantity`
* `ibCommission` → `commission`; `netCash` → `net_cash`; `proceeds` → `proceeds`
* `fifoPnlRealized` → `realized_pnl`; `openCloseIndicator` → `open_close`
* `tradeDate` (YYYYMMDD) → `trade_date`; `dateTime` stored verbatim as
  `executed_at` (Flex format varies / carries no tz — don't reparse)
* `notes`/`code` → `notes` (corrections/cancels, e.g. "Ca"/"Co")
* `assetCategory`/`subCategory`/`currency`/`conid`/`exchange`/`description`/
  `orderReference` → eponymous columns; full attribute dict → `raw` JSON
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Date, DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class Trade(Base):
    __tablename__ = "trades"

    account_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ib_exec_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    trade_id: Mapped[str | None] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    conid: Mapped[int | None] = mapped_column(BigInteger)
    asset_category: Mapped[str] = mapped_column(String(8), nullable=False)
    sub_category: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(String(256))

    trade_date: Mapped[date | None] = mapped_column(Date, index=True)
    executed_at: Mapped[str | None] = mapped_column(String(32))

    side: Mapped[str | None] = mapped_column(String(8))  # BUY / SELL
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float | None] = mapped_column(Float)
    proceeds: Mapped[float | None] = mapped_column(Float)
    net_cash: Mapped[float | None] = mapped_column(Float)
    commission: Mapped[float | None] = mapped_column(Float)
    realized_pnl: Mapped[float | None] = mapped_column(Float)
    open_close: Mapped[str | None] = mapped_column(String(8))

    order_reference: Mapped[str | None] = mapped_column(String(64))
    exchange: Mapped[str | None] = mapped_column(String(32))
    currency: Mapped[str | None] = mapped_column(String(8))
    fx_rate_to_base: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(String(64))

    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    known_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
