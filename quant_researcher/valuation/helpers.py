"""Data accessors used by the valuation models.

All accept a `Session` and a `symbol`, return the most-recent annual value
(or `None` when missing). Keep this module read-only — the models don't
write anything; persistence is the engine's job.
"""

from __future__ import annotations

import statistics

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant_researcher.models.financials import BalanceSheet, CashFlow, IncomeStatement
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile
from quant_researcher.models.ratios import FinancialRatios


def historical_fcf(session: Session, symbol: str, n: int = 5) -> list[float]:
    """Return up to `n` annual free cash flow values, sorted oldest → newest.

    Sources `cash_flow.free_cash_flow` for `period == 'FY'`. Skips rows with
    None FCF.
    """
    rows = session.execute(
        select(CashFlow.fiscal_date, CashFlow.free_cash_flow)
        .where(CashFlow.symbol == symbol, CashFlow.period == "FY")
        .order_by(CashFlow.fiscal_date.desc())
        .limit(n)
    ).all()
    series = [float(fcf) for _date, fcf in rows if fcf is not None]
    return list(reversed(series))


def latest_income_statement(session: Session, symbol: str) -> IncomeStatement | None:
    return session.scalars(
        select(IncomeStatement)
        .where(IncomeStatement.symbol == symbol, IncomeStatement.period == "FY")
        .order_by(IncomeStatement.fiscal_date.desc())
        .limit(1)
    ).first()


def latest_balance_sheet(session: Session, symbol: str) -> BalanceSheet | None:
    return session.scalars(
        select(BalanceSheet)
        .where(BalanceSheet.symbol == symbol, BalanceSheet.period == "FY")
        .order_by(BalanceSheet.fiscal_date.desc())
        .limit(1)
    ).first()


def latest_ratios(session: Session, symbol: str) -> FinancialRatios | None:
    return session.scalars(
        select(FinancialRatios)
        .where(FinancialRatios.symbol == symbol, FinancialRatios.period == "FY")
        .order_by(FinancialRatios.fiscal_date.desc())
        .limit(1)
    ).first()


def shares_outstanding(session: Session, symbol: str) -> float | None:
    """Derive shares outstanding, preferring the freshest source.

    Primary: `profile.raw.marketCap / profile.raw.price` — both come from
    FMP's `/profile` endpoint, refreshed together on each profile pull, so
    the ratio is definitionally the current implied share count.

    Fallback: latest-FY `net_income / eps_diluted` — used only when the
    profile is missing or lacks marketCap/price. Buyback-heavy names drift
    materially against this fallback within a fiscal year because the EPS
    denominator is averaged over the period.

    Returns None when neither path resolves.
    """
    raw = session.scalar(select(Profile.raw).where(Profile.symbol == symbol))
    if raw:
        mcap = raw.get("mktCap") or raw.get("marketCap")
        price = raw.get("price")
        try:
            if mcap is not None and price is not None and float(price) > 0:
                return float(mcap) / float(price)
        except (TypeError, ValueError):
            pass  # fall through to EPS-based fallback

    inc = latest_income_statement(session, symbol)
    if inc is None or inc.net_income is None or inc.eps_diluted in (None, 0):
        return None
    return float(inc.net_income) / float(inc.eps_diluted)


def net_debt(session: Session, symbol: str) -> float | None:
    """`short_term_debt + long_term_debt − cash_and_equivalents` (latest FY)."""
    bal = latest_balance_sheet(session, symbol)
    if bal is None:
        return None
    short = bal.short_term_debt or 0.0
    long_ = bal.long_term_debt or 0.0
    cash = bal.cash_and_equivalents or 0.0
    total_debt = float(short) + float(long_)
    if total_debt == 0 and cash == 0:
        return None
    return total_debt - float(cash)


def latest_close(session: Session, symbol: str) -> float | None:
    return session.scalar(
        select(DailyPrice.close)
        .where(DailyPrice.symbol == symbol)
        .order_by(DailyPrice.trade_date.desc())
        .limit(1)
    )


def latest_market_cap(session: Session, symbol: str) -> float | None:
    """Pull `mktCap` from `profiles.raw` JSON (FMP /profile field)."""
    raw = session.scalar(select(Profile.raw).where(Profile.symbol == symbol))
    if not raw:
        return None
    for key in ("mktCap", "marketCap"):
        v = raw.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def latest_ebitda(session: Session, symbol: str) -> float | None:
    """Approximated as `operating_income + (capex absolute, as D&A proxy)`.

    True D&A isn't promoted to a column in MA-3; this is a documented v1
    approximation. Returns None if either input is missing.
    """
    inc = latest_income_statement(session, symbol)
    cf = session.scalars(
        select(CashFlow)
        .where(CashFlow.symbol == symbol, CashFlow.period == "FY")
        .order_by(CashFlow.fiscal_date.desc())
        .limit(1)
    ).first()
    if inc is None or inc.operating_income is None:
        return None
    if cf is None or cf.capital_expenditure is None:
        # Without D&A approximation, fall back to operating income (under-states).
        return float(inc.operating_income)
    # capex is typically reported negative; D&A ≈ |capex| as steady-state proxy.
    da_proxy = abs(float(cf.capital_expenditure))
    return float(inc.operating_income) + da_proxy


def latest_revenue(session: Session, symbol: str) -> float | None:
    inc = latest_income_statement(session, symbol)
    return float(inc.revenue) if inc and inc.revenue is not None else None


def sector_for_symbol(session: Session, symbol: str) -> str | None:
    return session.scalar(select(Profile.sector).where(Profile.symbol == symbol))


def sector_peer_median(
    session: Session, sector: str, ratio_attr: str
) -> float | None:
    """Median of `financial_ratios.<ratio_attr>` across all FY rows of
    companies in `sector`. Pulls the latest FY row per peer in Python.
    """
    if sector is None:
        return None
    col = getattr(FinancialRatios, ratio_attr, None)
    if col is None:
        return None
    peer_symbols = list(
        session.scalars(select(Profile.symbol).where(Profile.sector == sector))
    )
    if not peer_symbols:
        return None
    # Latest FY ratio row per peer.
    values: list[float] = []
    rows = session.execute(
        select(FinancialRatios.symbol, col, FinancialRatios.fiscal_date)
        .where(
            FinancialRatios.symbol.in_(peer_symbols),
            FinancialRatios.period == "FY",
        )
        .order_by(FinancialRatios.symbol, FinancialRatios.fiscal_date.desc())
    ).all()
    seen: set[str] = set()
    for sym, value, _date in rows:
        if sym in seen or value is None:
            continue
        seen.add(sym)
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    return statistics.median(values)


def earnings_growth_rate(
    session: Session, symbol: str, n: int = 5
) -> float | None:
    """CAGR of net_income over up to `n` annual FY rows. Returns None if we
    can't compute (zero/negative start, too few points)."""
    rows = session.execute(
        select(IncomeStatement.fiscal_date, IncomeStatement.net_income)
        .where(IncomeStatement.symbol == symbol, IncomeStatement.period == "FY")
        .order_by(IncomeStatement.fiscal_date.desc())
        .limit(n)
    ).all()
    series = [float(ni) for _d, ni in rows if ni is not None]
    if len(series) < 2:
        return None
    newest, oldest = series[0], series[-1]
    # Both endpoints must be positive: a negative `newest` (latest year a loss)
    # makes the ratio negative and `negative ** (1/years)` returns a COMPLEX
    # number silently (no ValueError), which then crashes callers' comparisons.
    if oldest <= 0 or newest <= 0:
        return None
    years = len(series) - 1
    try:
        return (newest / oldest) ** (1 / years) - 1
    except (ValueError, ZeroDivisionError):
        return None
