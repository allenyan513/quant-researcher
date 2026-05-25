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
* `transcript` — latest persisted earnings-call transcript: year / quarter /
  call_date + a ~2000-char excerpt (ingested by `qr data refresh --scope transcript`)

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
from quant_researcher.models.transcripts import Transcript
from quant_researcher.models.valuation import ValuationSnapshot
from quant_researcher.research import scores


def build_bundle(
    session: Session,
    symbol: str,
    *,
    news_limit: int = 10,
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
        "scores": _scores_section(session, symbol),
        "quality": _quality_section(session, symbol),
        "ratio_history": _ratio_history(session, symbol),
        "holdings": _holdings_section(session, symbol),
        "news": _recent_news(session, symbol, news_limit),
        "transcript": _transcript_section(session, symbol),
    }
    return payload


def bundle(
    session: Session,
    symbol: str,
    *,
    news_limit: int = 10,
    save: bool = True,
) -> tuple[str | None, dict[str, Any]]:
    """Build + (optionally) persist a research bundle.

    Returns `(bundle_id, payload)`. `bundle_id` is None when `save=False`.
    """
    payload = build_bundle(
        session,
        symbol,
        news_limit=news_limit,
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
        "roic": r.return_on_invested_capital,
        "earnings_yield": r.earnings_yield,
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


def _transcript_section(session: Session, symbol: str) -> dict[str, Any] | None:
    """Latest persisted earnings-call transcript: metadata + ~2000-char excerpt."""
    row = session.scalars(
        select(Transcript)
        .where(Transcript.symbol == symbol)
        .order_by(Transcript.year.desc(), Transcript.quarter.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return {
        "year": row.year,
        "quarter": row.quarter,
        "call_date": row.call_date.isoformat() if row.call_date else None,
        "excerpt": (row.content or "")[:2000] or None,
    }


# ----- quality / quant sections (Phase 1) --------------------------------


def _annual_rows(session: Session, model: type, symbol: str, n: int) -> list[Any]:
    """Most-recent `n` annual (period='FY') rows for `model`, newest-first."""
    return list(
        session.scalars(
            select(model)
            .where(model.symbol == symbol, model.period == "FY")  # type: ignore[attr-defined]
            .order_by(model.fiscal_date.desc())  # type: ignore[attr-defined]
            .limit(n)
        )
    )


def _annual_ratios(session: Session, symbol: str, n: int) -> list[FinancialRatios]:
    """Most-recent `n` annual FinancialRatios rows, newest-first."""
    return list(
        session.scalars(
            select(FinancialRatios)
            .where(FinancialRatios.symbol == symbol, FinancialRatios.period == "FY")
            .order_by(FinancialRatios.fiscal_date.desc())
            .limit(n)
        )
    )


def _combine_year(inc: Any, bal: Any, cf: Any) -> dict[str, Any]:
    """Merge one fiscal year's income/balance/cashflow rows → flat dict for scores.

    `shares` is derived from net_income / eps_diluted (same approach as
    `valuation.helpers.shares_outstanding`) — there's no promoted share-count col.
    """
    d: dict[str, Any] = {}
    if inc is not None:
        d.update(
            net_income=inc.net_income,
            revenue=inc.revenue,
            gross_profit=inc.gross_profit,
            operating_income=inc.operating_income,
            eps_diluted=inc.eps_diluted,
        )
    if bal is not None:
        d.update(
            total_assets=bal.total_assets,
            total_liabilities=bal.total_liabilities,
            total_equity=bal.total_equity,
            long_term_debt=bal.long_term_debt,
            retained_earnings=bal.retained_earnings,
            current_assets=bal.current_assets,
            current_liabilities=bal.current_liabilities,
        )
    if cf is not None:
        d.update(
            operating_cash_flow=cf.operating_cash_flow,
            free_cash_flow=cf.free_cash_flow,
        )
    ni, eps = d.get("net_income"), d.get("eps_diluted")
    d["shares"] = ni / eps if (ni is not None and eps) else None
    return d


def _scores_section(session: Session, symbol: str) -> dict[str, Any] | None:
    """Piotroski F (needs 2 FYs) + Altman Z'' (latest FY) from annual statements."""
    inc = {r.fiscal_date.year: r for r in _annual_rows(session, IncomeStatement, symbol, 2)}
    bal = {r.fiscal_date.year: r for r in _annual_rows(session, BalanceSheet, symbol, 2)}
    cf = {r.fiscal_date.year: r for r in _annual_rows(session, CashFlow, symbol, 2)}
    years = sorted(set(inc) | set(bal) | set(cf), reverse=True)
    if not years:
        return None
    combined = {y: _combine_year(inc.get(y), bal.get(y), cf.get(y)) for y in years}
    curr_year = years[0]
    prev_year = years[1] if len(years) > 1 else None
    curr = combined[curr_year]
    return {
        "fiscal_year": curr_year,
        "prior_fiscal_year": prev_year,
        "piotroski_f": scores.piotroski_f(curr, combined[prev_year]) if prev_year else None,
        "altman_z": scores.altman_z(
            current_assets=curr.get("current_assets"),
            current_liabilities=curr.get("current_liabilities"),
            total_assets=curr.get("total_assets"),
            retained_earnings=curr.get("retained_earnings"),
            operating_income=curr.get("operating_income"),
            total_equity=curr.get("total_equity"),
            total_liabilities=curr.get("total_liabilities"),
        ),
    }


def _quality_section(session: Session, symbol: str) -> dict[str, Any] | None:
    """ROIC−WACC, FCF conversion, accruals, and multi-year margin/ROIC/revenue trends."""
    inc = _annual_rows(session, IncomeStatement, symbol, 6)
    if not inc:
        return None
    bal = _annual_rows(session, BalanceSheet, symbol, 6)
    cf = _annual_rows(session, CashFlow, symbol, 6)
    ratios = _annual_ratios(session, symbol, 6)

    latest_inc = inc[0]
    latest_bal = bal[0] if bal else None
    latest_cf = cf[0] if cf else None
    roic = ratios[0].return_on_invested_capital if ratios else None
    wacc = _safe_wacc(session, symbol)
    inc_asc = list(reversed(inc))
    return {
        "roic": roic,
        "wacc": wacc,
        "roic_wacc_spread": scores.roic_wacc_spread(roic, wacc),
        "fcf_conversion": scores.fcf_conversion(
            latest_cf.free_cash_flow if latest_cf else None, latest_inc.net_income
        ),
        "accruals_ratio": scores.accruals_ratio(
            latest_inc.net_income,
            latest_cf.operating_cash_flow if latest_cf else None,
            latest_bal.total_assets if latest_bal else None,
        ),
        "trends": {
            "revenue": scores.trend([r.revenue for r in inc_asc]),
            "gross_margin": scores.trend([_safe_div(r.gross_profit, r.revenue) for r in inc_asc]),
            "operating_margin": scores.trend(
                [_safe_div(r.operating_income, r.revenue) for r in inc_asc]
            ),
            "net_margin": scores.trend([_safe_div(r.net_income, r.revenue) for r in inc_asc]),
            "roic": scores.trend([r.return_on_invested_capital for r in reversed(ratios)]),
        },
    }


def _ratio_history(session: Session, symbol: str, n: int = 10) -> dict[str, Any] | None:
    """Multi-year FY multiples + where the latest ranks vs the symbol's own history."""
    rows = _annual_ratios(session, symbol, n)
    if not rows:
        return None
    asc = list(reversed(rows))
    multiples: dict[str, Any] = {"fiscal_dates": [r.fiscal_date.isoformat() for r in asc]}
    for k in ("pe_ratio", "ev_to_ebitda", "price_to_sales", "price_to_book", "fcf_yield"):
        multiples[k] = [getattr(r, k) for r in asc]
    pct = {
        k: _percentile_rank(multiples[k])
        for k in ("pe_ratio", "ev_to_ebitda", "price_to_sales", "price_to_book")
    }
    return {"multiples": multiples, "latest_percentile_vs_history": pct}


def _safe_div(num: float | None, den: float | None) -> float | None:
    if num is None or not den:
        return None
    return num / den


def _safe_wacc(session: Session, symbol: str) -> float | None:
    """Best-effort CAPM WACC (default rf/erp); None if it can't be computed.

    Uses the same default rf/erp as the DCF, so this spread is an
    approximate, default-assumption signal — not a tuned cost of capital.
    """
    try:
        from quant_researcher.valuation.wacc import wacc_for_symbol

        wacc, _ = wacc_for_symbol(session, symbol, rf=0.045, erp=0.055)
        return wacc
    except Exception:
        return None


def _percentile_rank(series: list[float | None]) -> float | None:
    """Where the latest non-None value sits within its own prior history (0–100).

    100 = richer than every prior year (for a P/E, historically expensive);
    0 = cheaper than all of them. Needs ≥3 points, else None.
    """
    pts = [v for v in series if v is not None]
    if len(pts) < 3:
        return None
    latest, hist = pts[-1], pts[:-1]
    return sum(1 for v in hist if v < latest) / len(hist) * 100
