"""Portfolio morning-briefing builder (Item 1, features §E).

`build_morning_call` assembles a LEAN per-holding view + portfolio-level
aggregates from the warehouse — not N full research bundles. It reuses the
bundler's per-symbol section helpers (`_latest_price`, `_latest_ratios`,
`_recent_valuations`, `_recent_news`) and the ledger's `etf_for_sector`, and
batches the profile/decision lookups. Pure warehouse read; `save_morning_call`
persists a compact `MorningCallSnapshot`.

Honest-data notes accumulate in `payload["notes"]`: cross-currency portfolios,
underivable cash, stale prices, etc. `day_change_pct` is close-to-close (we
only have daily EOD bars, D6 — no true overnight gap).
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from quant_researcher.contract import code_version
from quant_researcher.ledger.sectors import etf_for_sector
from quant_researcher.models.decisions import Decision
from quant_researcher.models.holdings import Holding
from quant_researcher.models.morningcall import MorningCallSnapshot
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile
from quant_researcher.research.bundler import (
    _latest_price,
    _latest_ratios,
    _recent_news,
    _recent_valuations,
)

_STALE_PRICE_DAYS = 3


def build_morning_call(
    session: Session,
    *,
    account: str | None = None,
    as_of: date | None = None,
    news_per_holding: int = 1,
) -> dict[str, Any]:
    """Lean per-holding + portfolio-level morning briefing for `account`.

    `as_of=None` uses the latest snapshot per (account, symbol); an explicit
    date filters to that exact `as_of_date` (no fallback, mirrors holdings list).
    """
    holdings = _latest_holdings(session, account, as_of)
    notes: list[str] = []

    if not holdings:
        return {
            "account": account,
            "as_of": datetime.now(UTC).isoformat(),
            "as_of_date": as_of.isoformat() if as_of else None,
            "holdings_count": 0,
            "portfolio": {},
            "holdings": [],
            "notes": ["no holdings for filter"],
        }

    symbols = [h.symbol for h in holdings]
    profiles = {
        p.symbol: p
        for p in session.scalars(select(Profile).where(Profile.symbol.in_(symbols)))
    }
    decisions = _latest_decisions_by_symbol(session, symbols)

    total_mv = _sum_attr(holdings, "market_value")

    views: list[dict[str, Any]] = []
    for h in holdings:
        prof = profiles.get(h.symbol)
        sector = prof.sector if prof else None
        price = _latest_price(session, h.symbol)
        prev_close = _prev_close(session, h.symbol)
        latest_close = price["close"] if price else None
        price_date = price["trade_date"] if price else None
        ratios = _latest_ratios(session, h.symbol)
        valuation = _pick_valuation(_recent_valuations(session, h.symbol))
        news = _recent_news(session, h.symbol, news_per_holding)
        decision = decisions.get(h.symbol)

        views.append(
            {
                "symbol": h.symbol,
                "account_id": h.account_id,
                "sector": sector,
                "quantity": h.quantity,
                "market_value": h.market_value,
                "weight_pct": _pct(h.market_value, total_mv),
                "avg_cost": h.avg_cost,
                "unrealized_pnl": h.unrealized_pnl,
                "unrealized_pnl_pct": _pct(
                    h.unrealized_pnl, _cost_basis(h)
                ),
                "side": h.side,
                "latest_close": latest_close,
                "prev_close": prev_close,
                "day_change_pct": _pct_change(latest_close, prev_close),
                "price_date": price_date,
                "ratios": _lean_ratios(ratios),
                "valuation": valuation,
                "latest_news": news[0] if news else None,
                "decision": decision,
            }
        )
        if price_date is not None and _is_stale(price_date):
            notes.append(f"{h.symbol}: latest price {price_date} is >{_STALE_PRICE_DAYS}d old")

    portfolio = _portfolio_summary(views, holdings, total_mv, notes)

    return {
        "account": account,
        "as_of": datetime.now(UTC).isoformat(),
        "as_of_date": max(h.as_of_date for h in holdings).isoformat(),
        "holdings_count": len(views),
        "portfolio": portfolio,
        "holdings": views,
        "notes": notes,
    }


def save_morning_call(
    session: Session, payload: dict[str, Any], *, account: str | None
) -> str:
    """Persist a MorningCallSnapshot row; returns the snapshot_id."""
    snapshot_id = str(uuid4())
    as_of_date = payload.get("as_of_date")
    session.add(
        MorningCallSnapshot(
            snapshot_id=snapshot_id,
            account_id=account or "__ALL__",
            as_of_date=date.fromisoformat(as_of_date) if as_of_date else date.today(),
            payload=payload,
            code_version=code_version(),
        )
    )
    return snapshot_id


# ----- helpers --------------------------------------------------------------


def _latest_holdings(
    session: Session, account: str | None, as_of: date | None
) -> list[Holding]:
    """Latest holding per (account, symbol), or an exact `as_of` date."""
    if as_of is not None:
        stmt = select(Holding).where(Holding.as_of_date == as_of)
    else:
        sub = (
            select(
                Holding.account_id,
                Holding.symbol,
                func.max(Holding.as_of_date).label("max_date"),
            )
            .group_by(Holding.account_id, Holding.symbol)
            .subquery()
        )
        stmt = select(Holding).join(
            sub,
            (Holding.account_id == sub.c.account_id)
            & (Holding.symbol == sub.c.symbol)
            & (Holding.as_of_date == sub.c.max_date),
        )
    if account:
        stmt = stmt.where(Holding.account_id == account)
    return list(session.scalars(stmt.order_by(Holding.account_id, Holding.symbol)))


def _latest_decisions_by_symbol(
    session: Session, symbols: list[str]
) -> dict[str, dict[str, Any]]:
    """Newest Decision per symbol → lean dict (for marking decided positions)."""
    if not symbols:
        return {}
    rows = session.scalars(
        select(Decision)
        .where(Decision.symbol.in_(symbols))
        .order_by(Decision.created_at.desc())
    ).all()
    out: dict[str, dict[str, Any]] = {}
    for d in rows:
        if d.symbol in out:
            continue
        out[d.symbol] = {
            "decision_id": d.decision_id,
            "side": d.side,
            "opened_at": d.opened_at.isoformat() if d.opened_at else None,
            "price_at_open": d.price_at_open,
            "confidence": d.confidence,
        }
    return out


def _prev_close(session: Session, symbol: str) -> float | None:
    """Second-newest daily close (for close-to-close day change)."""
    rows = session.scalars(
        select(DailyPrice.close)
        .where(DailyPrice.symbol == symbol)
        .order_by(DailyPrice.trade_date.desc())
        .limit(2)
    ).all()
    return rows[1] if len(rows) >= 2 else None


def _portfolio_summary(
    views: list[dict[str, Any]],
    holdings: list[Holding],
    total_mv: float | None,
    notes: list[str],
) -> dict[str, Any]:
    total_pnl = _sum_attr(holdings, "unrealized_pnl")
    total_cost = sum(c for c in (_cost_basis(h) for h in holdings) if c is not None) or None

    # cash: sum CASH-category positions, else None + note
    cash_vals = [
        h.market_value
        for h in holdings
        if (h.asset_category or "").upper() == "CASH" and h.market_value is not None
    ]
    cash = sum(cash_vals) if cash_vals else None
    if cash is None:
        notes.append("cash not derivable from positions")

    currencies = Counter(h.currency for h in holdings if h.currency)
    currency = currencies.most_common(1)[0][0] if currencies else None
    if len(currencies) > 1:
        notes.append(f"mixed currencies {dict(currencies)} — market values summed raw")

    # sector exposure (signed market value)
    by_sector: dict[str | None, list[dict[str, Any]]] = {}
    for v in views:
        by_sector.setdefault(v["sector"], []).append(v)
    sector_exposure = [
        {
            "sector": sec,
            "etf": etf_for_sector(sec),
            "market_value": _sum_floats(rows, "market_value"),
            "weight_pct": _pct(_sum_floats(rows, "market_value"), total_mv),
            "holdings": len(rows),
        }
        for sec, rows in by_sector.items()
    ]
    sector_exposure.sort(key=lambda s: s["weight_pct"] or 0.0, reverse=True)

    movers = [v for v in views if v["day_change_pct"] is not None]
    movers.sort(key=lambda v: v["day_change_pct"], reverse=True)
    mover_keys = ("symbol", "day_change_pct", "market_value")

    return {
        "total_market_value": total_mv,
        "total_unrealized_pnl": total_pnl,
        "total_unrealized_pnl_pct": _pct(total_pnl, total_cost),
        "cash": cash,
        "currency": currency,
        "sector_exposure": sector_exposure,
        "top_movers": [{k: v[k] for k in mover_keys} for v in movers[:3]],
        "bottom_movers": [{k: v[k] for k in mover_keys} for v in movers[-3:][::-1]],
        "decided_positions_count": sum(1 for v in views if v["decision"] is not None),
    }


def _pick_valuation(valuations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer the DCF snapshot as the headline upside; else first available."""
    if not valuations:
        return None
    for v in valuations:
        if v.get("model_type") == "dcf":
            return v
    return valuations[0]


def _lean_ratios(ratios: dict[str, Any] | None) -> dict[str, Any] | None:
    if ratios is None:
        return None
    return {k: ratios.get(k) for k in ("pe_ratio", "peg_ratio", "return_on_equity", "net_margin")}


def _cost_basis(h: Holding) -> float | None:
    if h.cost_basis_total is not None:
        return h.cost_basis_total
    if h.market_value is not None and h.unrealized_pnl is not None:
        return h.market_value - h.unrealized_pnl
    return None


def _sum_attr(holdings: list[Holding], attr: str) -> float | None:
    vals = [getattr(h, attr) for h in holdings if getattr(h, attr) is not None]
    return sum(vals) if vals else None


def _sum_floats(items: list[dict[str, Any]], key: str) -> float | None:
    vals = [i.get(key) for i in items if i.get(key) is not None]
    return sum(vals) if vals else None


def _pct(num: float | None, denom: float | None) -> float | None:
    # abs(denom) so the sign tracks the numerator — critical for short positions
    # / net-short books where cost basis (the denominator) is negative: a loss
    # on a short (neg pnl ÷ neg cost) must stay negative, not flip positive.
    if num is None or not denom:
        return None
    return num / abs(denom) * 100


def _pct_change(latest: float | None, prev: float | None) -> float | None:
    if latest is None or not prev:
        return None
    return (latest / prev - 1) * 100


def _is_stale(price_date: str) -> bool:
    try:
        d = date.fromisoformat(price_date)
    except ValueError:
        return False
    return (date.today() - d).days > _STALE_PRICE_DAYS
