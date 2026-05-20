"""Refresh pipelines for FMP-sourced warehouse tables.

`refresh_profile` overwrites `profiles` rows per symbol (FMP is the truth).
`refresh_quotes` is append-only on `daily_prices`: it asks for OHLCV since
the latest known trade_date (or `today − lookback_days` if the symbol is new)
and inserts only dates not yet in the table — EOD bars are treated as
immutable in v1.

Per-symbol failures are isolated: one bad ticker yields a `SymbolOutcome`
with `ok=False` and the loop continues. The caller decides what to do with
the `RefreshResult` (emit envelope, retry, etc.) and commits the session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from quant_researcher.data.fmp import FMPClient, FMPError
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile


@dataclass(frozen=True)
class SymbolOutcome:
    symbol: str
    ok: bool
    upserted: int = 0
    skipped: int = 0
    error: str | None = None


@dataclass(frozen=True)
class RefreshResult:
    scope: str
    outcomes: list[SymbolOutcome] = field(default_factory=list)

    @property
    def total_upserted(self) -> int:
        return sum(o.upserted for o in self.outcomes)

    @property
    def total_skipped(self) -> int:
        return sum(o.skipped for o in self.outcomes)

    @property
    def succeeded(self) -> list[str]:
        return [o.symbol for o in self.outcomes if o.ok]

    @property
    def failed(self) -> list[dict[str, str]]:
        return [{"symbol": o.symbol, "error": o.error or ""} for o in self.outcomes if not o.ok]


# ----- profile --------------------------------------------------------------


def refresh_profile(
    session: Session, client: FMPClient, symbols: list[str]
) -> RefreshResult:
    """Refresh `profiles` for each symbol. Latest FMP payload overwrites the row."""
    outcomes: list[SymbolOutcome] = []
    for sym in symbols:
        try:
            payload = client.get_profile(sym)
        except FMPError as exc:
            outcomes.append(SymbolOutcome(sym, ok=False, error=str(exc)))
            continue
        if not payload:
            outcomes.append(SymbolOutcome(sym, ok=False, error="empty profile response"))
            continue
        session.merge(_profile_from_fmp(sym, payload))
        outcomes.append(SymbolOutcome(sym, ok=True, upserted=1))
    return RefreshResult(scope="profile", outcomes=outcomes)


def _profile_from_fmp(symbol: str, payload: dict[str, Any]) -> Profile:
    return Profile(
        symbol=symbol,
        company_name=payload.get("companyName"),
        sector=payload.get("sector"),
        industry=payload.get("industry"),
        exchange=payload.get("exchangeShortName") or payload.get("exchange"),
        currency=payload.get("currency"),
        country=payload.get("country"),
        beta=_as_float(payload.get("beta")),
        ipo_date=_as_date(payload.get("ipoDate")),
        is_etf=payload.get("isEtf"),
        is_fund=payload.get("isFund"),
        is_adr=payload.get("isAdr"),
        is_actively_trading=payload.get("isActivelyTrading"),
        raw=payload,
        known_at=datetime.now(UTC),
    )


# ----- quotes (daily OHLCV) ------------------------------------------------


def refresh_quotes(
    session: Session,
    client: FMPClient,
    symbols: list[str],
    *,
    lookback_days: int = 730,
) -> RefreshResult:
    """Append-only refresh of `daily_prices` per symbol."""
    outcomes: list[SymbolOutcome] = []
    today = date.today()
    for sym in symbols:
        try:
            latest = session.scalar(
                select(DailyPrice.trade_date)
                .where(DailyPrice.symbol == sym)
                .order_by(DailyPrice.trade_date.desc())
                .limit(1)
            )
            since = (
                latest + timedelta(days=1)
                if latest
                else today - timedelta(days=lookback_days)
            )
            rows = client.get_historical_prices(sym, since=since)
        except FMPError as exc:
            outcomes.append(SymbolOutcome(sym, ok=False, error=str(exc)))
            continue
        outcomes.append(_insert_prices(session, sym, rows))
    return RefreshResult(scope="quote", outcomes=outcomes)


def _insert_prices(
    session: Session, symbol: str, rows: list[dict[str, Any]]
) -> SymbolOutcome:
    parsed: list[dict[str, Any]] = []
    for r in rows:
        mapped = _price_from_fmp(symbol, r)
        if mapped["trade_date"] is None:
            continue
        parsed.append(mapped)
    if not parsed:
        return SymbolOutcome(symbol, ok=True, upserted=0)

    incoming_dates = {p["trade_date"] for p in parsed}
    existing = set(
        session.scalars(
            select(DailyPrice.trade_date).where(
                DailyPrice.symbol == symbol,
                DailyPrice.trade_date.in_(incoming_dates),
            )
        )
    )
    new_rows = [p for p in parsed if p["trade_date"] not in existing]
    if new_rows:
        session.execute(insert(DailyPrice), new_rows)
    return SymbolOutcome(
        symbol, ok=True, upserted=len(new_rows), skipped=len(parsed) - len(new_rows)
    )


def _price_from_fmp(symbol: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "trade_date": _as_date(row.get("date")),
        "open": _as_float(row.get("open")),
        "high": _as_float(row.get("high")),
        "low": _as_float(row.get("low")),
        "close": _as_float(row.get("close")),
        "adj_close": _as_float(row.get("adjClose")) or _as_float(row.get("adj_close")),
        "volume": _as_int(row.get("volume")),
    }


# ----- coercion helpers ----------------------------------------------------


def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_date(v: Any) -> date | None:
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None
