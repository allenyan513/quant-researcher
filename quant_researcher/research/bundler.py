"""Research bundle aggregator — turns the warehouse into a JSON payload.

`build_bundle(session, symbol)` walks every relevant table and assembles a
self-contained dict Claude can consume for deep-dive narratives. Missing
data falls to `None` / empty list gracefully — the bundle still ships and
notes what's absent.

Sections:
* `profile` — Profile row (full + sector/industry/exchange/beta/raw)
* `financials` — latest 4 quarterly + latest FY for each of income / balance
  / cash flow (compact view)
* `ratios` — latest annual FinancialRatios row
* `estimates` — latest forward analyst_estimates rows (up to 4 future periods)
* `valuation_snapshots` — most recent ValuationSnapshot per model_type
* `holdings` — latest Holding row per account (so you see "what I own + cost basis + unrealized")
* `news` — last N NewsItem rows (default 10)
* `transcript_excerpt` — first ~2000 chars of latest earnings_call transcript
  if one is fetched and cached (caller usually passes None for v1)

`bundle(session, symbol, *, save=True)` persists the result to
`research_bundles` and returns `(bundle_id, payload)` for the CLI.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant_researcher.contract import code_version
from quant_researcher.models.estimates import AnalystEstimate
from quant_researcher.models.financials import BalanceSheet, CashFlow, IncomeStatement
from quant_researcher.models.holdings import Holding
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile
from quant_researcher.models.ratios import FinancialRatios
from quant_researcher.models.research import NewsItem, ResearchBundle
from quant_researcher.models.valuation import ValuationSnapshot


def build_bundle(
    session: Session,
    symbol: str,
    *,
    news_limit: int = 10,
    transcript_excerpt: str | None = None,
) -> dict[str, Any]:
    """Aggregate everything the warehouse knows about `symbol` → dict."""
    payload: dict[str, Any] = {
        "symbol": symbol,
        "as_of": datetime.now(UTC).isoformat(),
        "profile": _profile_section(session, symbol),
        "latest_price": _latest_price(session, symbol),
        "ratios_latest_annual": _latest_ratios(session, symbol),
        "income_statement_recent": _recent_statements(session, IncomeStatement, symbol),
        "balance_sheet_recent": _recent_statements(session, BalanceSheet, symbol),
        "cash_flow_recent": _recent_statements(session, CashFlow, symbol),
        "estimates_forward": _forward_estimates(session, symbol),
        "valuation_snapshots": _recent_valuations(session, symbol),
        "holdings": _holdings_section(session, symbol),
        "news": _recent_news(session, symbol, news_limit),
        "transcript_excerpt": (transcript_excerpt or "")[:2000] or None,
    }
    return payload


def bundle(
    session: Session,
    symbol: str,
    *,
    news_limit: int = 10,
    transcript_excerpt: str | None = None,
    save: bool = True,
) -> tuple[str | None, dict[str, Any]]:
    """Build + (optionally) persist a research bundle.

    Returns `(bundle_id, payload)`. `bundle_id` is None when `save=False`.
    """
    payload = build_bundle(
        session,
        symbol,
        news_limit=news_limit,
        transcript_excerpt=transcript_excerpt,
    )
    if not save:
        return None, payload
    bundle_id = str(uuid.uuid4())
    session.add(
        ResearchBundle(
            bundle_id=bundle_id,
            symbol=symbol,
            as_of=datetime.now(UTC),
            payload=payload,
            code_version=code_version(),
        )
    )
    return bundle_id, payload


# ----- per-section helpers -----------------------------------------------


def _profile_section(session: Session, symbol: str) -> dict[str, Any] | None:
    p = session.get(Profile, symbol)
    if p is None:
        return None
    raw = p.raw or {}
    # FMP /profile uses `marketCap` in /stable; older docs say `mktCap`.
    # Check both for resilience.
    market_cap = raw.get("marketCap") or raw.get("mktCap") or raw.get("MktCap")
    return {
        "company_name": p.company_name,
        "sector": p.sector,
        "industry": p.industry,
        "exchange": p.exchange,
        "currency": p.currency,
        "country": p.country,
        "beta": p.beta,
        "ipo_date": p.ipo_date.isoformat() if p.ipo_date else None,
        "is_etf": p.is_etf,
        "is_fund": p.is_fund,
        "is_adr": p.is_adr,
        "is_actively_trading": p.is_actively_trading,
        "market_cap": market_cap,
    }


def _latest_price(session: Session, symbol: str) -> dict[str, Any] | None:
    row = session.scalars(
        select(DailyPrice)
        .where(DailyPrice.symbol == symbol)
        .order_by(DailyPrice.trade_date.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return {
        "trade_date": row.trade_date.isoformat(),
        "open": row.open,
        "high": row.high,
        "low": row.low,
        "close": row.close,
        "adj_close": row.adj_close,
        "volume": row.volume,
    }


def _latest_ratios(session: Session, symbol: str) -> dict[str, Any] | None:
    r = session.scalars(
        select(FinancialRatios)
        .where(FinancialRatios.symbol == symbol, FinancialRatios.period == "FY")
        .order_by(FinancialRatios.fiscal_date.desc())
        .limit(1)
    ).first()
    if r is None:
        return None
    return {
        "fiscal_date": r.fiscal_date.isoformat(),
        "pe_ratio": r.pe_ratio,
        "peg_ratio": r.peg_ratio,
        "price_to_book": r.price_to_book,
        "price_to_sales": r.price_to_sales,
        "ev_to_ebitda": r.ev_to_ebitda,
        "current_ratio": r.current_ratio,
        "debt_to_equity": r.debt_to_equity,
        "return_on_equity": r.return_on_equity,
        "return_on_assets": r.return_on_assets,
        "gross_margin": r.gross_margin,
        "operating_margin": r.operating_margin,
        "net_margin": r.net_margin,
        "fcf_yield": r.fcf_yield,
    }


def _recent_statements(
    session: Session, model: type, symbol: str, n: int = 5
) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(model)
        .where(model.symbol == symbol)  # type: ignore[attr-defined]
        .order_by(model.fiscal_date.desc())  # type: ignore[attr-defined]
        .limit(n)
    ).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = {
            "period": row.period,
            "fiscal_date": row.fiscal_date.isoformat(),
            "reported_currency": row.reported_currency,
        }
        # promoted typed columns vary per model — sweep all non-PK / non-meta cols
        for col in row.__table__.columns:
            if col.name in {
                "symbol",
                "period",
                "fiscal_date",
                "calendar_year",
                "reported_currency",
                "raw",
                "known_at",
            }:
                continue
            d[col.name] = getattr(row, col.name)
        out.append(d)
    return out


def _forward_estimates(session: Session, symbol: str) -> list[dict[str, Any]]:
    today = datetime.now(UTC).date()
    rows = session.scalars(
        select(AnalystEstimate)
        .where(
            AnalystEstimate.symbol == symbol,
            AnalystEstimate.fiscal_date >= today,
        )
        .order_by(AnalystEstimate.fiscal_date.asc())
        .limit(4)
    ).all()
    return [
        {
            "fiscal_date": r.fiscal_date.isoformat(),
            "period": r.period,
            "revenue_avg": r.revenue_avg,
            "eps_avg": r.eps_avg,
            "ebitda_avg": r.ebitda_avg,
            "net_income_avg": r.net_income_avg,
            "num_analysts_revenue": r.num_analysts_revenue,
            "num_analysts_eps": r.num_analysts_eps,
        }
        for r in rows
    ]


def _recent_valuations(session: Session, symbol: str) -> list[dict[str, Any]]:
    # Latest snapshot per (symbol, model_type) — Python-side group by.
    rows = session.scalars(
        select(ValuationSnapshot)
        .where(ValuationSnapshot.symbol == symbol)
        .order_by(ValuationSnapshot.created_at.desc())
    ).all()
    latest_per_model: dict[str, ValuationSnapshot] = {}
    for r in rows:
        latest_per_model.setdefault(r.model_type, r)
    return [
        {
            "snapshot_id": r.snapshot_id,
            "model_type": r.model_type,
            "as_of": r.as_of.isoformat(),
            "fair_value_per_share": r.fair_value_per_share,
            "current_price": r.current_price,
            "upside_pct": r.upside_pct,
        }
        for r in latest_per_model.values()
    ]


def _holdings_section(session: Session, symbol: str) -> list[dict[str, Any]]:
    # Latest snapshot per account.
    rows = session.scalars(
        select(Holding)
        .where(Holding.symbol == symbol)
        .order_by(Holding.account_id, Holding.as_of_date.desc())
    ).all()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        if r.account_id in seen:
            continue
        seen.add(r.account_id)
        out.append(
            {
                "account_id": r.account_id,
                "as_of_date": r.as_of_date.isoformat(),
                "quantity": r.quantity,
                "mark_price": r.mark_price,
                "market_value": r.market_value,
                "avg_cost": r.avg_cost,
                "unrealized_pnl": r.unrealized_pnl,
                "percent_of_nav": r.percent_of_nav,
                "side": r.side,
            }
        )
    return out


def _recent_news(session: Session, symbol: str, limit: int) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(NewsItem)
        .where(NewsItem.symbol == symbol)
        .order_by(NewsItem.published_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "published_at": r.published_at.isoformat() if r.published_at else None,
            "headline": r.headline,
            "url": r.url,
            "source": r.source,
            "summary": r.summary,
        }
        for r in rows
    ]
