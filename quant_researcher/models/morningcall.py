"""Morning-call snapshots — one row per `qr morningcall --save` (Item 1).

A compact portfolio briefing snapshot keyed by a uuid (re-running appends an
immutable row, like ResearchBundle / BacktestRun). `account_id` uses the
sentinel `"__ALL__"` when no `--account` filter is given. The `(account_id,
as_of_date)` index supports a future "vs prior snapshot" delta. `payload` is
the lean briefing dict from `research.morningcall.build_morning_call` — NOT a
pile of full research bundles.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class MorningCallSnapshot(Base):
    __tablename__ = "morning_call_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    code_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
