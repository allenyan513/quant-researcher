"""Research artifacts — news cache + bundle snapshots.

* `NewsItem` — lightweight per-(symbol, published_at, url) news row, used as
  a rolling cache so the bundler can pull recent headlines without re-hitting
  FMP every time.
* `ResearchBundle` — one immutable snapshot per `(symbol, as_of)` call,
  containing the full aggregated dict (profile + latest financials/ratios/
  estimates/valuation/holdings + news + transcript excerpt) that Claude
  consumes for deep-dive narratives.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class NewsItem(Base):
    __tablename__ = "news_items"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    url: Mapped[str] = mapped_column(String(512), primary_key=True)

    headline: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(128))
    summary: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(String(512))
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    known_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ResearchBundle(Base):
    __tablename__ = "research_bundles"

    bundle_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    as_of: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    code_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
