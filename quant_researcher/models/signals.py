"""Signal/factor-research tables (MG).

Mirrors the Screen/ScreenRun split: a named DEFINITION (`signals`) + immutable
RUN snapshots (`signal_runs`). A run captures the factor + params + the three
analyses (IC summary / quantile spreads / decay) + a `coverage` honesty block
(how many rebalance dates / symbols actually fed each statistic — critical
because fundamental factors are quasi-static on a short history). JSON columns
stay schemaless for v1; numbers are sanitized via `backtest.runner._to_jsonable`
before insert (numpy/inf/nan → JSON-safe).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class Signal(Base):
    __tablename__ = "signals"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    factor: Mapped[str] = mapped_column(String(64), nullable=False)
    params: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SignalRun(Base):
    __tablename__ = "signal_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    signal_name: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("signals.name", ondelete="SET NULL")
    )
    factor: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str | None] = mapped_column(String(16))  # fundamental | price

    params: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    ic_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    quantiles: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    decay: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    coverage: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    universe_size: Mapped[int] = mapped_column(Integer, nullable=False)
    code_version: Mapped[str | None] = mapped_column(String(64))
    ran_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
