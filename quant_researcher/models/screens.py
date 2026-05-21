"""Screens (definitions) + screen_runs (result snapshots).

Per implementation-plan.md §6:
* `Screen` = a named, reusable filter (fundamental `expr` and/or `technical`).
* `ScreenRun` = one execution record — full result `result_symbols` is stored
  as JSON so `qr screen diff` can compare two runs without re-querying. Ad-
  hoc runs (no saved screen) have `screen_name=None`.

`expr_hash` is sha256 over the normalized expr+technical strings — lets us
cheaply detect identical re-runs (cache hint, not enforced uniquely).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class Screen(Base):
    __tablename__ = "screens"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    expr: Mapped[str | None] = mapped_column(Text)
    technical: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ScreenRun(Base):
    __tablename__ = "screen_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    screen_name: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("screens.name", ondelete="SET NULL")
    )
    expr: Mapped[str | None] = mapped_column(Text)
    technical: Mapped[str | None] = mapped_column(Text)
    expr_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ran_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    universe_size: Mapped[int] = mapped_column(Integer, nullable=False)
    result_symbols: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    code_version: Mapped[str | None] = mapped_column(String(64))
