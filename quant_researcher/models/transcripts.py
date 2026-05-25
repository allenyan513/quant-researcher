"""FMP `/earning-call-transcript` — latest persisted earnings call per symbol.

PK `(symbol, year, quarter)`. Phase 3 stores only the LATEST transcript per
symbol (one FMP call per refresh), so in practice there's one row per symbol —
but the `(year, quarter)` PK lets a later phase backfill historical quarters
with no schema change. The refresh uses `session.merge` (like analyst_estimates),
so re-pulling the same quarter overwrites in place rather than appending; a
corrected transcript wins.

`known_at` is ingestion time (the endpoint exposes no publication timestamp) and
carries a `server_default` so rows created outside the refresh path still stamp.
Freshness is judged on `call_date` (the call's own date), not `known_at` — see
`data/freshness.py`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class Transcript(Base):
    __tablename__ = "transcripts"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    quarter: Mapped[int] = mapped_column(Integer, primary_key=True)

    call_date: Mapped[date | None] = mapped_column(Date)
    content: Mapped[str | None] = mapped_column(Text)

    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    known_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
