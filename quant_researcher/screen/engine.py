"""Screen execution engine.

Three responsibilities:
1. `build_symbol_state(session, symbols)` — assemble per-symbol fundamental
   state from `profiles` + latest annual `financial_ratios` + latest
   `daily_prices`. Returns `dict[symbol, dict[field, value]]` keyed on the
   field names registered in `expression.FIELDS`.
2. `run_screen(session, expr=…, technical=…, …)` — apply the fundamental
   predicate (AST-parsed) and the technical predicates (DSL-parsed) to the
   universe (or a subset), persist a `ScreenRun` row, optionally upsert a
   named `Screen` definition, return a `ScreenRunResult`.
3. `diff_runs(session, from_id, to_id)` — symbol set diff between two runs.

The fundamental and technical steps are independent; if both are supplied,
a symbol must pass BOTH (logical AND). Either alone is allowed. Empty
universe / no predicates raises `ValueError`.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant_researcher.contract import code_version
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile
from quant_researcher.models.ratios import FinancialRatios
from quant_researcher.models.screens import Screen, ScreenRun
from quant_researcher.models.universe import UniverseMember
from quant_researcher.screen.expression import parse_expression
from quant_researcher.screen.technical import parse_technical


@dataclass(frozen=True)
class ScreenRunResult:
    run_id: str
    screen_name: str | None
    expr: str | None
    technical: str | None
    universe_size: int
    result_symbols: list[str]
    expr_hash: str
    ran_at: datetime


def run_screen(
    session: Session,
    *,
    expr: str | None = None,
    technical: str | None = None,
    symbols: list[str] | None = None,
    save_name: str | None = None,
    description: str | None = None,
) -> ScreenRunResult:
    """Execute a screen and persist the result."""
    if not expr and not technical:
        raise ValueError("must supply at least one of `expr` or `technical`")

    universe = (
        sorted(symbols)
        if symbols is not None
        else sorted(session.scalars(select(UniverseMember.symbol)))
    )
    if not universe:
        raise ValueError("empty universe — run `qr universe set` first")
    universe_size = len(universe)
    candidates = list(universe)

    if expr:
        pred = parse_expression(expr)
        state = build_symbol_state(session, candidates)
        candidates = [s for s in candidates if pred(state.get(s, {}))]

    if technical and candidates:
        tech_preds = parse_technical(technical)
        passed: list[str] = []
        for sym in candidates:
            closes, volumes = load_price_series(session, sym)
            if all(p(closes, volumes) for p in tech_preds):
                passed.append(sym)
        candidates = passed

    candidates = sorted(candidates)
    run_id = str(uuid.uuid4())
    expr_hash = _hash_spec(expr, technical)
    now = datetime.now(UTC)

    if save_name:
        session.merge(
            Screen(
                name=save_name,
                expr=expr,
                technical=technical,
                description=description,
            )
        )

    session.add(
        ScreenRun(
            run_id=run_id,
            screen_name=save_name,
            expr=expr,
            technical=technical,
            expr_hash=expr_hash,
            universe_size=universe_size,
            result_symbols=candidates,
            code_version=code_version(),
        )
    )

    return ScreenRunResult(
        run_id=run_id,
        screen_name=save_name,
        expr=expr,
        technical=technical,
        universe_size=universe_size,
        result_symbols=candidates,
        expr_hash=expr_hash,
        ran_at=now,
    )


def diff_runs(
    session: Session, from_run_id: str, to_run_id: str
) -> dict[str, list[str]]:
    from_run = session.get(ScreenRun, from_run_id)
    to_run = session.get(ScreenRun, to_run_id)
    if from_run is None:
        raise ValueError(f"unknown run_id: {from_run_id}")
    if to_run is None:
        raise ValueError(f"unknown run_id: {to_run_id}")
    from_set = set(from_run.result_symbols or [])
    to_set = set(to_run.result_symbols or [])
    return {
        "added": sorted(to_set - from_set),
        "removed": sorted(from_set - to_set),
        "kept": sorted(from_set & to_set),
    }


def build_symbol_state(
    session: Session, symbols: list[str]
) -> dict[str, dict[str, Any]]:
    """One pass per source table → state dict per symbol."""
    state: dict[str, dict[str, Any]] = defaultdict(dict)
    if not symbols:
        return state

    # Profiles — one row per symbol.
    for p in session.scalars(select(Profile).where(Profile.symbol.in_(symbols))):
        state[p.symbol].update(
            {
                "sector": p.sector,
                "industry": p.industry,
                "country": p.country,
                "beta": p.beta,
                "market_cap": _extract_market_cap(p.raw),
            }
        )

    # Latest annual ratios per symbol. Order DESC by fiscal_date, take first.
    seen_ratios: set[str] = set()
    ratio_rows = session.scalars(
        select(FinancialRatios)
        .where(FinancialRatios.symbol.in_(symbols))
        .where(FinancialRatios.period == "FY")
        .order_by(FinancialRatios.symbol, FinancialRatios.fiscal_date.desc())
    )
    for r in ratio_rows:
        if r.symbol in seen_ratios:
            continue
        seen_ratios.add(r.symbol)
        state[r.symbol].update(
            {
                "pe": r.pe_ratio,
                "peg": r.peg_ratio,
                "pb": r.price_to_book,
                "ps": r.price_to_sales,
                "ev_ebitda": r.ev_to_ebitda,
                "current_ratio": r.current_ratio,
                "debt_equity": r.debt_to_equity,
                "roe": r.return_on_equity,
                "roa": r.return_on_assets,
                "roic": r.return_on_invested_capital,
                "gross_margin": r.gross_margin,
                "operating_margin": r.operating_margin,
                "net_margin": r.net_margin,
                "fcf_yield": r.fcf_yield,
                "earnings_yield": r.earnings_yield,
            }
        )

    # Latest close per symbol.
    seen_close: set[str] = set()
    rows = session.execute(
        select(DailyPrice.symbol, DailyPrice.close, DailyPrice.trade_date)
        .where(DailyPrice.symbol.in_(symbols))
        .order_by(DailyPrice.symbol, DailyPrice.trade_date.desc())
    )
    for sym, close, _td in rows:
        if sym in seen_close:
            continue
        seen_close.add(sym)
        state[sym]["close"] = close

    return dict(state)


def load_price_series(
    session: Session, symbol: str
) -> tuple[np.ndarray, np.ndarray]:
    """Closes + volumes sorted ascending by trade_date."""
    rows = session.execute(
        select(DailyPrice.close, DailyPrice.volume)
        .where(DailyPrice.symbol == symbol)
        .order_by(DailyPrice.trade_date.asc())
    ).all()
    if not rows:
        return np.array([], dtype=float), np.array([], dtype=float)
    closes = np.array(
        [float(r[0]) if r[0] is not None else np.nan for r in rows], dtype=float
    )
    volumes = np.array(
        [float(r[1]) if r[1] is not None else 0.0 for r in rows], dtype=float
    )
    return closes, volumes


def _extract_market_cap(raw: dict | None) -> float | None:
    if not raw:
        return None
    for key in ("mktCap", "marketCap", "MktCap"):
        v = raw.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _hash_spec(expr: str | None, technical: str | None) -> str:
    """sha256 of normalized expr+technical for cheap dedup / cache hint."""
    payload = f"{(expr or '').strip()}|{(technical or '').strip()}"
    return hashlib.sha256(payload.encode()).hexdigest()
