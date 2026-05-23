"""Backtest run snapshots — one row per `qr backtest run` (MH).

Mirrors the `valuation_snapshots` design: every run captures its full inputs
(strategy + params + symbols + window + execution config) and outputs (metrics
+ equity curve + trade log) plus `code_version`, so a run can be replayed and
compared across versions (features.md §H: "回测运行落盘可横向对比").

JSON columns stay schemaless for v1 — `metrics` shape comes straight from
`engine.analytics.metrics.calculate_metrics`; `equity_curve` is a list of
`[iso_timestamp, equity]`; `trade_log` is a list of closed-trade dicts. The
engine's risk/margin knobs aren't wired into the CLI v1 (risk_manager=None),
so `config` only records fee/slippage/benchmark/adjusted.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    start: Mapped[date] = mapped_column(Date, nullable=False)
    end: Mapped[date] = mapped_column(Date, nullable=False)
    initial_cash: Mapped[float] = mapped_column(Float, nullable=False)
    benchmark_symbol: Mapped[str | None] = mapped_column(String(20))

    symbols: Mapped[list[str] | None] = mapped_column(JSON)
    params: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    equity_curve: Mapped[list[Any] | None] = mapped_column(JSON)
    trade_log: Mapped[list[Any] | None] = mapped_column(JSON)

    code_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
