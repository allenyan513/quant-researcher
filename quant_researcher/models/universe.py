"""Watchlist membership — the v1 ticker universe (D3, ~200-300 symbols).

Rows are owned by `qr universe set` (replace semantics, per-source). Other
warehouse refresh jobs (MA-2+) iterate this table to decide what to fetch.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, func
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class UniverseMember(Base):
    __tablename__ = "universe"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    added_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
