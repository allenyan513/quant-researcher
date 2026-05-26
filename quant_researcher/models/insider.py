"""SEC Form 3/4/5 insider transactions (free, via edgartools / SEC EDGAR).

One row per transaction line within a filing. PK `(symbol, accession_no,
line_no)` — `accession_no` identifies the immutable Form 4 filing, `line_no` the
row within its transaction table — so re-ingesting the same filing dedups via
`session.merge`. `filing_date` is the SEC filing date (Form 4 is due within 2
business days of the trade, so it's effectively point-in-time); freshness is
judged on it. `known_at` is ingestion time. No `raw` column — the flat fields
already capture the full transaction.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class InsiderTransaction(Base):
    __tablename__ = "insider_transactions"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    accession_no: Mapped[str] = mapped_column(String(32), primary_key=True)
    line_no: Mapped[int] = mapped_column(Integer, primary_key=True)

    filing_date: Mapped[date | None] = mapped_column(Date)
    transaction_date: Mapped[date | None] = mapped_column(Date)
    # SEC Form 4 reporting-owner names can be a chain of related legal
    # entities concatenated with " / " (e.g. a parent + ten subsidiary funds
    # in one joint filing). 512 was the first fix; 335 chars at Goldman was
    # the trigger, but private-equity / family-office joint filings can run
    # much longer. Use Text — varchar(N) for a free-text concatenation
    # field is just a future bug waiting to happen.
    insider: Mapped[str | None] = mapped_column(Text)
    # SEC Form 4 officer titles can exceed varchar(256) at large issuers
    # (compound roles like "EVP, Global Head of X, Member of the Management
    # Committee"). Use Text to avoid arbitrary cliff cuts.
    position: Mapped[str | None] = mapped_column(Text)
    transaction_type: Mapped[str | None] = mapped_column(String(64))
    code: Mapped[str | None] = mapped_column(String(8))
    shares: Mapped[float | None] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float)
    value: Mapped[float | None] = mapped_column(Float)
    remaining_shares: Mapped[float | None] = mapped_column(Float)

    known_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
