"""Warehouse I/O for factor research — the efficiency core.

Loads each universe symbol's full adjusted-close series ONCE into a numpy-backed
`PriceSeries` (sorted, bisectable), so forward-return and momentum lookups are
in-memory array ops rather than thousands of per-(symbol, date) SQL round-trips.
Builds point-in-time factor panels:
- forward returns from prices (`adj_close`, 3-day staleness like the ledger);
- fundamental factor values via the PIT join `FinancialRatios → IncomeStatement`
  filtering `IncomeStatement.known_at` (= FMP acceptedDate, the REAL filing date,
  unlike `FinancialRatios.known_at` which is ingestion time → would leak).
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from quant_researcher.models.financials import IncomeStatement
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.ratios import FinancialRatios

_STALE_DAYS = 3  # weekend/holiday buffer; bigger gap = data issue (mirrors ledger)


@dataclass(frozen=True)
class PriceSeries:
    """One symbol's adjusted-close history as sorted parallel numpy arrays."""

    symbol: str
    dates: np.ndarray  # object array of datetime.date, ascending
    prices: np.ndarray  # float array (adj_close, fallback close), np.nan where missing
    ordinals: np.ndarray  # int64 d.toordinal(), for bisect

    def price_on_or_before(
        self, target: date, *, max_staleness_days: int = _STALE_DAYS
    ) -> float | None:
        """Most recent finite price with trade_date <= target, within staleness."""
        pos = int(np.searchsorted(self.ordinals, target.toordinal(), side="right")) - 1
        while pos >= 0:
            p = self.prices[pos]
            if not np.isnan(p):
                if target.toordinal() - int(self.ordinals[pos]) > max_staleness_days:
                    return None
                return float(p)
            pos -= 1
        return None

    def index_on_or_before(self, target: date) -> int | None:
        """Row index of the most recent bar <= target (for momentum offsets)."""
        pos = int(np.searchsorted(self.ordinals, target.toordinal(), side="right")) - 1
        return pos if pos >= 0 else None

    def price_at_offset(self, anchor_idx: int, trading_days_back: int) -> float | None:
        """Finite price `trading_days_back` rows before `anchor_idx`, else None."""
        i = anchor_idx - trading_days_back
        if i < 0 or i >= len(self.prices):
            return None
        p = self.prices[i]
        return None if np.isnan(p) else float(p)


def load_price_panel(session: Session, symbols: list[str]) -> dict[str, PriceSeries]:
    """One pass over `daily_prices` for all symbols → {symbol: PriceSeries}."""
    rows = session.execute(
        select(
            DailyPrice.symbol,
            DailyPrice.trade_date,
            DailyPrice.adj_close,
            DailyPrice.close,
        )
        .where(DailyPrice.symbol.in_(symbols))
        .order_by(DailyPrice.symbol, DailyPrice.trade_date.asc())
    ).all()

    by_sym: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for sym, trade_date, adj, close in rows:
        px = adj if adj is not None else close
        by_sym[sym].append((trade_date, float(px) if px is not None else np.nan))

    panel: dict[str, PriceSeries] = {}
    for sym, series in by_sym.items():
        ds = [d for d, _ in series]
        panel[sym] = PriceSeries(
            symbol=sym,
            dates=np.array(ds, dtype=object),
            prices=np.array([p for _, p in series], dtype=float),
            ordinals=np.array([d.toordinal() for d in ds], dtype=np.int64),
        )
    return panel


def rebalance_dates(panel: dict[str, PriceSeries], *, freq: str = "monthly") -> list[date]:
    """Calendar rebalance dates over the union span of all price data."""
    if not panel:
        return []
    start = min(s.dates[0] for s in panel.values())
    end = max(s.dates[-1] for s in panel.values())
    if freq == "monthly":
        return _month_ends(start, end)
    if freq == "weekly":
        return _fridays(start, end)
    raise ValueError(f"unknown rebalance freq {freq!r} (valid: monthly, weekly)")


def _month_ends(start: date, end: date) -> list[date]:
    out: list[date] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        d = date(y, m, calendar.monthrange(y, m)[1])
        if start <= d <= end:
            out.append(d)
        m, y = (1, y + 1) if m == 12 else (m + 1, y)
    return out


def _fridays(start: date, end: date) -> list[date]:
    d = start + timedelta(days=(4 - start.weekday()) % 7)
    out: list[date] = []
    while d <= end:
        out.append(d)
        d += timedelta(days=7)
    return out


def forward_return(base: float | None, fwd: float | None) -> float | None:
    """Raw forward return fwd/base − 1 (None if either missing or base == 0)."""
    if base is None or fwd is None or base == 0:
        return None
    return fwd / base - 1


def build_forward_return_panel(
    panel: dict[str, PriceSeries], dates: list[date], horizons: dict[str, int]
) -> dict[date, dict[str, dict[str, float | None]]]:
    """{rebalance_date: {symbol: {horizon: forward_return}}} (calendar-day horizons)."""
    out: dict[date, dict[str, dict[str, float | None]]] = {}
    for anchor in dates:
        per_sym: dict[str, dict[str, float | None]] = {}
        for sym, series in panel.items():
            base = series.price_on_or_before(anchor)
            rets: dict[str, float | None] = {}
            for h, n_days in horizons.items():
                fwd = series.price_on_or_before(anchor + timedelta(days=n_days))
                rets[h] = forward_return(base, fwd)
            per_sym[sym] = rets
        out[anchor] = per_sym
    return out


def build_fundamental_panel(
    session: Session, symbols: list[str], dates: list[date], ratio_col: str
) -> dict[date, dict[str, float | None]]:
    """PIT value of one `financial_ratios` column at each rebalance date.

    Joins FinancialRatios → IncomeStatement on the shared PK, period 'FY', and
    only counts a filing as known once `IncomeStatement.known_at` (acceptedDate)
    <= rebalance_date. Among known filings, picks the greatest fiscal_date.
    """
    col = getattr(FinancialRatios, ratio_col)
    rows = session.execute(
        select(
            FinancialRatios.symbol,
            FinancialRatios.fiscal_date,
            col,
            IncomeStatement.known_at,
        )
        .join(
            IncomeStatement,
            and_(
                FinancialRatios.symbol == IncomeStatement.symbol,
                FinancialRatios.period == IncomeStatement.period,
                FinancialRatios.fiscal_date == IncomeStatement.fiscal_date,
            ),
        )
        .where(FinancialRatios.symbol.in_(symbols), FinancialRatios.period == "FY")
        .order_by(FinancialRatios.symbol, FinancialRatios.fiscal_date.asc())
    ).all()

    # symbol -> ascending-by-fiscal_date list of (known_date, value)
    by_sym: dict[str, list[tuple[date | None, float | None]]] = defaultdict(list)
    for sym, _fiscal_date, value, known_at in rows:
        by_sym[sym].append((known_at.date() if known_at else None, value))

    out: dict[date, dict[str, float | None]] = {}
    for anchor in dates:
        per_sym: dict[str, float | None] = {}
        for sym in symbols:
            chosen: float | None = None
            for known_date, value in by_sym.get(sym, []):  # ascending fiscal_date
                if known_date is not None and known_date <= anchor:
                    chosen = value  # last qualifying = greatest known fiscal_date
            per_sym[sym] = chosen
        out[anchor] = per_sym
    return out
