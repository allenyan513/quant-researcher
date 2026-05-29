"""Raw ingested market events — the **Event** side of the Event/TradingSignal
message contract for the real-time signal service (Issue #53, S1 → S2).

One row per external catalyst (rating change / M&A / insider / earnings / …),
normalized + deduped by `(source, external_id)`. `id` is a surrogate UUID PK so
the `TradingSignal` an event produces can FK to it cleanly; the natural dedup
key is the unique `(source, external_id)` constraint that the S1 poller/webhook
UPSERTs against (so the same event arriving from multiple sources / re-pushes
triggers analysis only once).

Defined in Phase 0 to lock the contract; written-to from Phase 1 onward.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class RawEvent(Base):
    __tablename__ = "raw_events"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_raw_events_source_external"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)

    symbol: Mapped[str | None] = mapped_column(String(20), index=True)
    # grade_change | m&a | insider | earnings | … (None until classified)
    event_type: Mapped[str | None] = mapped_column(String(32))

    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
