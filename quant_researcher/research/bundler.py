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
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant_researcher.contract import code_version
from quant_researcher.models.estimates import AnalystEstimate
from quant_researcher.models.financials import BalanceSheet, CashFlow, IncomeStatement
from quant_researcher.models.holdings import Holding
from quant_researcher.models.insider import InsiderTransaction
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile
from quant_researcher.models.ratios import FinancialRatios
from quant_researcher.models.research import NewsItem, ResearchBundle
from quant_researcher.models.short_interest import ShortInterest
from quant_researcher.models.transcripts import Transcript
from quant_researcher.models.valuation import ValuationSnapshot
from quant_researcher.research import scores
from quant_researcher.research.sector_classifier import classify_stock_type, net_revenue
from quant_researcher.screen import indicators as ind


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
        "technical": _technical_section(session, symbol),
        "scores": _scores_section(session, symbol),
        "quality": _quality_section(session, symbol),
        "ratio_history": _ratio_history(session, symbol),
        "holdings": _holdings_section(session, symbol),
        "insider": _insider_section(session, symbol),
        "short_interest": _short_interest_section(session, symbol),
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
        # Drives sector-aware report templates downstream (see `scores`,
        # `quality` blocks below + the deep-research SKILL.md fork).
        # Defaults to "general" for non-bank / unknown sector.
        "stock_type": classify_stock_type(p.sector, p.industry),
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
    # Bank-aware: for IncomeStatement on a bank, augment each row with a
    # `revenue_net` field (revenue − interestExpense). The raw `revenue`
    # line stays — it's FMP-gross for banks and downstream readers may
    # need both. For non-banks `revenue_net == revenue`, so we omit it
    # to keep the payload minimal.
    is_income = model is IncomeStatement
    stock_type = _stock_type_for(session, symbol) if is_income else "general"

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
        if is_income and stock_type == "bank":
            d["revenue_net"] = net_revenue(row, stock_type)
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


def _insider_section(
    session: Session, symbol: str, *, lookback_days: int = 180
) -> dict[str, Any] | None:
    """Recent insider (Form 4) activity: open-market buy/sell tallies + notable rows.

    Open-market purchases (code 'P') vs sales ('S') are the discretionary signal;
    grants/exercises/tax (A/M/F/G) are compensation noise, counted only in totals.
    """
    cutoff = date.today() - timedelta(days=lookback_days)
    rows = list(
        session.scalars(
            select(InsiderTransaction)
            .where(InsiderTransaction.symbol == symbol)
            .where(InsiderTransaction.transaction_date >= cutoff)
            .order_by(InsiderTransaction.transaction_date.desc())
        )
    )
    if not rows:
        return None

    def _sum(rs: list[InsiderTransaction], attr: str) -> float:
        return sum(getattr(r, attr) or 0.0 for r in rs)

    buys = [r for r in rows if (r.code or "").upper() == "P"]
    sells = [r for r in rows if (r.code or "").upper() == "S"]
    last_filing = max((r.filing_date for r in rows if r.filing_date), default=None)
    return {
        "lookback_days": lookback_days,
        "transactions": len(rows),
        "open_market_buys": len(buys),
        "open_market_sells": len(sells),
        "buy_shares": _sum(buys, "shares"),
        "sell_shares": _sum(sells, "shares"),
        "net_open_market_value": _sum(buys, "value") - _sum(sells, "value"),
        "last_filing_date": last_filing.isoformat() if last_filing else None,
        "recent": [
            {
                "date": r.transaction_date.isoformat() if r.transaction_date else None,
                "insider": r.insider,
                "position": r.position,
                "type": r.transaction_type,
                "code": r.code,
                "shares": r.shares,
                "price": r.price,
                "value": r.value,
            }
            for r in rows[:8]
        ],
    }


def _short_interest_section(session: Session, symbol: str) -> dict[str, Any] | None:
    """Latest FINRA short interest: short shares, days-to-cover, change vs prior."""
    row = session.scalars(
        select(ShortInterest)
        .where(ShortInterest.symbol == symbol)
        .order_by(ShortInterest.settlement_date.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return {
        "settlement_date": row.settlement_date.isoformat() if row.settlement_date else None,
        "short_interest": row.short_interest,
        "previous_short_interest": row.previous_short_interest,
        "change_pct": row.change_pct,
        "avg_daily_volume": row.avg_daily_volume,
        "days_to_cover": row.days_to_cover,
    }


# ----- technical snapshot (1y price-action + trend / momentum / vol) -----


_TECHNICAL_WINDOW = 252  # ~1 trading year
_MIN_BARS = 50  # below this the indicators are too noisy to report
_EVENT_LOOKBACK = 60  # MACD / 20-50 cross / RSI extremes window
_SPIKE_LOOKBACK = 30
_SPIKE_MULT = 2.0  # matches screen.technical.volume_spike default


def _technical_section(
    session: Session, symbol: str, *, lookback_days: int = _TECHNICAL_WINDOW
) -> dict[str, Any] | None:
    """Technical snapshot for SYM over ~1y: price action, SMA trend, RSI,
    MACD, volume + a small signal_summary the report can use directly.

    Returns:
      • None when daily_prices has no rows for SYM.
      • {"insufficient_data": True, "bars": N, ...} when N < 50.
      • Full structured snapshot otherwise (see deep-research SKILL.md §10).

    Uses adj_close (split/div-adjusted) where available so SMAs / RSI / MACD
    don't get bent by corporate actions; falls back to close for any null
    slot. The 50/200 golden/death cross is searched over the full window
    (it's a rare event); other cross / extreme events use a 60-day lookback.
    Indicator math reuses `quant_researcher.screen.indicators` (pure numpy).
    """
    rows = list(
        session.scalars(
            select(DailyPrice)
            .where(DailyPrice.symbol == symbol)
            .order_by(DailyPrice.trade_date.desc())
            .limit(lookback_days)
        )
    )
    if not rows:
        return None
    rows.reverse()  # oldest first for indicator math

    bars = len(rows)
    if bars < _MIN_BARS:
        return {
            "insufficient_data": True,
            "bars": bars,
            "reason": f"<{_MIN_BARS} bars in daily_prices; technical indicators unreliable",
        }

    adj_closes = np.array(
        [(r.adj_close if r.adj_close is not None else r.close) for r in rows],
        dtype=float,
    )
    # Forward-fill any remaining None slots (close also missing). Realistically
    # zero in production; we just don't want a NaN to cascade through.
    if np.isnan(adj_closes).any():
        valid = ~np.isnan(adj_closes)
        if not valid.any():
            return None
        first = int(np.argmax(valid))
        adj_closes[:first] = adj_closes[first]
        for i in range(first + 1, len(adj_closes)):
            if np.isnan(adj_closes[i]):
                adj_closes[i] = adj_closes[i - 1]

    volumes = np.array([(r.volume or 0) for r in rows], dtype=float)
    dates = [r.trade_date for r in rows]
    adj_close_used = any(r.adj_close is not None for r in rows)

    sma20 = ind.sma(adj_closes, 20)
    sma50 = ind.sma(adj_closes, 50)
    sma200 = ind.sma(adj_closes, 200)  # all-NaN when bars < 200; that's fine
    rsi14 = ind.rsi(adj_closes, 14)
    macd_line, macd_sig, macd_hist = ind.macd(adj_closes, 12, 26, 9)
    vol_sma20 = ind.sma(volumes, 20)

    latest_close = float(adj_closes[-1])

    def _last(arr: np.ndarray) -> float | None:
        v = arr[-1]
        return None if np.isnan(v) else float(v)

    def _vs(price: float, level: float | None) -> float | None:
        if level is None or level == 0:
            return None
        return (price / level - 1) * 100

    # ---- price_action ----
    def _ret_pct(periods: int) -> float | None:
        if bars <= periods:
            return None
        base = adj_closes[-1 - periods]
        if base <= 0:
            return None
        return (adj_closes[-1] / base - 1) * 100

    high_52w = float(np.max(adj_closes))
    low_52w = float(np.min(adj_closes))
    high_idx = int(np.argmax(adj_closes))
    low_idx = int(np.argmin(adj_closes))

    running_max = np.maximum.accumulate(adj_closes)
    drawdowns = (adj_closes - running_max) / running_max
    trough_idx = int(np.argmin(drawdowns))
    max_dd_pct = float(drawdowns[trough_idx] * 100)
    if max_dd_pct < -1e-9:
        peak_idx = int(np.argmax(adj_closes[: trough_idx + 1]))
        peak_date: str | None = dates[peak_idx].isoformat()
        trough_date: str | None = dates[trough_idx].isoformat()
    else:
        max_dd_pct = 0.0
        peak_date = None
        trough_date = None

    price_action = {
        "latest_close": latest_close,
        "return_6m_pct": _ret_pct(126),
        "return_1y_pct": _ret_pct(min(252, bars - 1)),
        "high_52w": high_52w,
        "high_52w_date": dates[high_idx].isoformat(),
        "low_52w": low_52w,
        "low_52w_date": dates[low_idx].isoformat(),
        "pct_below_52w_high": (latest_close / high_52w - 1) * 100 if high_52w else None,
        "pct_above_52w_low": (latest_close / low_52w - 1) * 100 if low_52w else None,
        "max_drawdown_pct": max_dd_pct,
        "max_drawdown_peak_date": peak_date,
        "max_drawdown_trough_date": trough_date,
    }

    # ---- trend ----
    sma20_last = _last(sma20)
    sma50_last = _last(sma50)
    sma200_last = _last(sma200)

    def _last_cross_date(
        fast: np.ndarray, slow: np.ndarray, direction: str, lookback: int
    ) -> str | None:
        """direction = 'up' (fast crosses above slow) | 'down' (fast crosses below)."""
        n = len(fast)
        start = max(1, n - lookback)
        last: int | None = None
        for i in range(start, n):
            if (
                np.isnan(fast[i])
                or np.isnan(slow[i])
                or np.isnan(fast[i - 1])
                or np.isnan(slow[i - 1])
            ):
                continue
            if direction == "up" and fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]:
                last = i
            elif direction == "down" and fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]:
                last = i
        return dates[last].isoformat() if last is not None else None

    last_20_50_up = _last_cross_date(sma20, sma50, "up", _EVENT_LOOKBACK)
    last_20_50_down = _last_cross_date(sma20, sma50, "down", _EVENT_LOOKBACK)
    if last_20_50_up and (not last_20_50_down or last_20_50_up >= last_20_50_down):
        last_cross_20_50: dict[str, Any] | None = {
            "date": last_20_50_up,
            "direction": "up",
        }
    elif last_20_50_down:
        last_cross_20_50 = {"date": last_20_50_down, "direction": "down"}
    else:
        last_cross_20_50 = None

    trend = {
        "sma20": sma20_last,
        "price_vs_sma20_pct": _vs(latest_close, sma20_last),
        "sma50": sma50_last,
        "price_vs_sma50_pct": _vs(latest_close, sma50_last),
        "sma200": sma200_last,
        "price_vs_sma200_pct": _vs(latest_close, sma200_last),
        # 50/200 cross uses the full window — a rare, slow event, worth surfacing
        # even when it happened ~6 months ago.
        "last_golden_cross_50_200": _last_cross_date(sma50, sma200, "up", bars),
        "last_death_cross_50_200": _last_cross_date(sma50, sma200, "down", bars),
        "last_cross_20_50": last_cross_20_50,
    }

    # ---- momentum (RSI 14) ----
    rsi_last = _last(rsi14)
    if rsi_last is None:
        rsi_zone = "unknown"
    elif rsi_last < 30:
        rsi_zone = "oversold"
    elif rsi_last > 70:
        rsi_zone = "overbought"
    else:
        rsi_zone = "neutral"

    def _zone_days(arr: np.ndarray, threshold: float, kind: str) -> list[str]:
        n = len(arr)
        start = max(0, n - _EVENT_LOOKBACK)
        out: list[str] = []
        for i in range(start, n):
            v = arr[i]
            if np.isnan(v):
                continue
            if kind == "oversold" and v < threshold:
                out.append(dates[i].isoformat())
            elif kind == "overbought" and v > threshold:
                out.append(dates[i].isoformat())
        return out

    momentum = {
        "rsi14_latest": rsi_last,
        "rsi14_zone": rsi_zone,
        "oversold_days_last_60": _zone_days(rsi14, 30, "oversold"),
        "overbought_days_last_60": _zone_days(rsi14, 70, "overbought"),
    }

    # ---- MACD ----
    macd_block = {
        "line": _last(macd_line),
        "signal": _last(macd_sig),
        "histogram": _last(macd_hist),
        "last_golden_cross_60d": _last_cross_date(macd_line, macd_sig, "up", _EVENT_LOOKBACK),
        "last_death_cross_60d": _last_cross_date(macd_line, macd_sig, "down", _EVENT_LOOKBACK),
    }

    # ---- volume ----
    avg_vol_20 = _last(vol_sma20)
    latest_vol = float(volumes[-1])
    latest_vs_avg = latest_vol / avg_vol_20 if avg_vol_20 else None

    spike_days: list[dict[str, Any]] = []
    spike_start = max(20, len(volumes) - _SPIKE_LOOKBACK)
    for i in range(spike_start, len(volumes)):
        m = vol_sma20[i]
        if np.isnan(m) or m == 0:
            continue
        if volumes[i] > m * _SPIKE_MULT:
            spike_days.append(
                {
                    "date": dates[i].isoformat(),
                    "volume": int(volumes[i]),
                    "x": float(volumes[i] / m),
                }
            )

    volume_block = {
        "avg_volume_20d": avg_vol_20,
        "latest_volume": int(latest_vol),
        "latest_vs_avg_x": latest_vs_avg,
        "spike_days_last_30": spike_days,
    }

    # ---- signal_summary (one-line bias on each axis) ----
    if sma20_last is not None and sma50_last is not None and sma200_last is not None:
        if sma20_last > sma50_last > sma200_last:
            trend_bias = "up"
        elif sma20_last < sma50_last < sma200_last:
            trend_bias = "down"
        else:
            trend_bias = "mixed"
    else:
        trend_bias = "mixed"

    macd_line_v = macd_block["line"]
    macd_sig_v = macd_block["signal"]
    macd_hist_v = macd_block["histogram"] or 0
    if macd_line_v is not None and macd_sig_v is not None:
        if macd_line_v > macd_sig_v and macd_hist_v > 0:
            macd_bias = "bullish"
        elif macd_line_v < macd_sig_v and macd_hist_v < 0:
            macd_bias = "bearish"
        else:
            macd_bias = "neutral"
    else:
        macd_bias = "neutral"

    pct_below_high = price_action["pct_below_52w_high"]
    pct_above_low = price_action["pct_above_52w_low"]
    if pct_below_high is not None and pct_below_high > -5:
        near_extreme = "near_high"
    elif pct_above_low is not None and pct_above_low < 5:
        near_extreme = "near_low"
    else:
        near_extreme = "none"

    signal_summary = {
        "trend_bias": trend_bias,
        "momentum_bias": rsi_zone,
        "macd_bias": macd_bias,
        "near_52w_extreme": near_extreme,
    }

    return {
        "bars_in_window": bars,
        "oldest_trade_date": dates[0].isoformat(),
        "latest_trade_date": dates[-1].isoformat(),
        "adj_close_used": adj_close_used,
        "price_action": price_action,
        "trend": trend,
        "momentum": momentum,
        "macd": macd_block,
        "volume": volume_block,
        "signal_summary": signal_summary,
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
    """Piotroski F (needs 2 FYs) + Altman Z'' (latest FY) from annual statements.

    For banks both metrics are conceptually inapplicable (working capital,
    gross margin, FCF, accruals all break on a deposit-funded balance
    sheet). Return a `template: "bank"` shape that names them as
    `not_applicable` so downstream consumers don't read a misleading
    "distress zone" verdict on a healthy bank.
    """
    if _stock_type_for(session, symbol) == "bank":
        return {
            "template": "bank",
            "not_applicable": ["piotroski_f", "altman_z"],
            "not_applicable_reason": (
                "Piotroski / Altman Z'' assume a non-financial balance sheet "
                "(working capital, gross margin, FCF). Banks structurally "
                "carry high leverage and low working capital — the metrics "
                "lose meaning."
            ),
        }

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
        "template": "general",
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
    """ROIC−WACC, FCF conversion, accruals, multi-year trends — OR, for
    banks (issue #37), the bank-appropriate metric set (ROA / ROE / NIM /
    efficiency ratio / equity-to-assets) plus revenue trend.
    """
    inc = _annual_rows(session, IncomeStatement, symbol, 6)
    if not inc:
        return None
    bal = _annual_rows(session, BalanceSheet, symbol, 6)
    cf = _annual_rows(session, CashFlow, symbol, 6)
    ratios = _annual_ratios(session, symbol, 6)

    if _stock_type_for(session, symbol) == "bank":
        return _quality_section_bank(inc, bal)

    latest_inc = inc[0]
    latest_bal = bal[0] if bal else None
    latest_cf = cf[0] if cf else None
    roic = ratios[0].return_on_invested_capital if ratios else None
    wacc = _safe_wacc(session, symbol)
    inc_asc = list(reversed(inc))
    return {
        "template": "general",
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


def _quality_section_bank(
    inc: list[IncomeStatement], bal: list[BalanceSheet]
) -> dict[str, Any]:
    """Bank-template quality block.

    NIM denominator uses `(total_assets_curr + total_assets_prev) / 2`
    as a proxy for true earning assets (which would exclude goodwill /
    PP&E / non-earning cash). The proxy over-states the denominator
    slightly — documented in `scores.net_interest_margin` and in
    `.claude/skills/deep-research/SKILL.md`. Bank metrics that FMP
    standard endpoints don't expose (Tier-1 capital ratio, NPL ratio)
    are surfaced in `missing_fields` so the report can supplement them
    from filings.
    """
    latest_inc = inc[0]
    latest_bal = bal[0] if bal else None
    prev_bal = bal[1] if len(bal) > 1 else None
    inc_asc = list(reversed(inc))

    total_assets_curr = latest_bal.total_assets if latest_bal else None
    total_assets_prev = prev_bal.total_assets if prev_bal else None
    avg_earning_assets = None
    if total_assets_curr is not None and total_assets_prev is not None:
        avg_earning_assets = (total_assets_curr + total_assets_prev) / 2

    # FMP's bank `/income-statement` payload doesn't expose
    # `nonInterestExpense` / `nonInterestIncome` directly. Derive from
    # what is there:
    #   • non_interest_expense ≈ `operatingExpenses` (the bank operating
    #     cost line, salaries / premises / tech, excludes interest expense)
    #   • non_interest_income  ≈ `revenue − interestIncome`  (revenue is
    #     bank gross = interestIncome + nonInterestIncome on the FMP feed)
    # So the efficiency-ratio denominator simplifies to net revenue =
    # `revenue − interestExpense`. Pull `netInterestIncome` direct.
    nii = _extract_raw(latest_inc, "netInterestIncome")
    interest_income = _extract_raw(latest_inc, "interestIncome")
    non_int_expense = _extract_raw(latest_inc, "operatingExpenses")
    revenue = latest_inc.revenue
    non_int_income = (
        revenue - interest_income
        if revenue is not None and interest_income is not None
        else None
    )

    return {
        "template": "bank",
        "roa": scores.roa(latest_inc.net_income, total_assets_curr),
        "roe": scores.roe(latest_inc.net_income, latest_bal.total_equity if latest_bal else None),
        "net_interest_margin": scores.net_interest_margin(nii, avg_earning_assets),
        "efficiency_ratio": scores.efficiency_ratio(non_int_expense, nii, non_int_income),
        "equity_to_assets": scores.equity_to_assets(
            latest_bal.total_equity if latest_bal else None, total_assets_curr
        ),
        "trends": {
            "revenue": scores.trend([r.revenue for r in inc_asc]),
        },
        "missing_fields": ["tier_1_capital_ratio", "npl_ratio"],
        "not_applicable": [
            "roic_wacc_spread",
            "fcf_conversion",
            "accruals_ratio",
            "gross_margin_trend",
            "operating_margin_trend",
            "net_margin_trend",
        ],
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


def _stock_type_for(session: Session, symbol: str) -> str:
    """Resolve the symbol's stock_type from its Profile row. Used by the
    `_scores_section` / `_quality_section` template branch. Defaults to
    `"general"` when no profile is present."""
    p = session.get(Profile, symbol)
    if p is None:
        return "general"
    return classify_stock_type(p.sector, p.industry)


def _extract_raw(row: Any, *keys: str, default: Any = None) -> Any:
    """Read the first non-None value found in `row.raw` for any of `keys`.

    Helper for bank quality metrics whose inputs (`netInterestIncome`,
    `nonInterestExpense`, ...) live in `income_statement.raw` rather
    than as typed columns. Returns `default` when none of the keys are
    populated.
    """
    raw = getattr(row, "raw", None) or {}
    for k in keys:
        v = raw.get(k)
        if v is not None:
            return v
    return default


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
