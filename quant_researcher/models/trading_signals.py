"""Trading signals — the **TradingSignal** side of the Event/TradingSignal
message contract for the real-time signal service (Issue #53, S3 → S2 → S5).

The *output* of the signal-analysis brain: "buy or sell now, to what target, with
what stop, held how long". This is System B (price/event-driven), distinct from
System A valuation (`valuation_snapshots`). NOTE the table is `trading_signals`,
**not** `signals` — `models/signals.py` already owns `signals` for MG factor
signals; the two are deliberately different things (see `docs/architecture-subsystems.md`
naming disambiguation: Event = input, Trading Signal = output).

Position sizing is intentionally absent — a signal carries direction + target +
stop + horizon only; the operator sizes it manually. `conviction` is a
strength/notification-priority label, not a sizing input.

Defined in Phase 0 to lock the contract; written-to from Phase 1 onward.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class TradingSignal(Base):
    __tablename__ = "trading_signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("raw_events.id", ondelete="SET NULL"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    # The four core elements.
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # buy | sell | hold
    target_price: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    horizon_days: Mapped[int | None] = mapped_column(Integer)

    # Strength / notification-priority label only — NOT used for sizing.
    conviction: Mapped[str | None] = mapped_column(String(16))

    entry_price: Mapped[float | None] = mapped_column(Float)
    fair_value_base: Mapped[float | None] = mapped_column(Float)  # from System A, nullable
    deviation_pct: Mapped[float | None] = mapped_column(Float)

    thesis: Mapped[str | None] = mapped_column(Text)
    generated_by: Mapped[str | None] = mapped_column(String(8))  # llm | algo
    snapshot_id: Mapped[str | None] = mapped_column(String(36))  # System A valuation snapshot

    # open | target_hit | stopped_out | expired | closed
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
