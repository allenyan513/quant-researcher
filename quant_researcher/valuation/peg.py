"""PEG / Lynch fair-P/E valuation.

PEG = P/E ÷ (earnings growth %, in whole percent). Peter Lynch's rule of
thumb is that a "fair" P/E equals the earnings growth rate in percent —
so a company growing earnings at 15%/yr is "fair" at a P/E of 15.

Growth rate is sourced in priority order:
  1. Forward analyst-consensus EPS CAGR (`forward_eps_growth_rate`) — what
     the Street expects over the next ~3 FYs from `analyst_estimates`.
  2. Historical 5-year net_income CAGR (`earnings_growth_rate`) — fallback
     when the forward path returns None (no estimates, non-positive).

The choice is surfaced as `growth_source` in the result so downstream
consumers (the deep-research skill, an LLM agent, a human reviewer) can
weigh it appropriately.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from quant_researcher.valuation.helpers import (
    earnings_growth_rate,
    forward_eps_growth_rate,
    latest_close,
    latest_income_statement,
    latest_ratios,
)


def peg_value(
    pe: float | None, growth_rate: float | None, *, eps: float | None = None
) -> dict[str, Any]:
    """PEG ratio + Lynch "fair P/E" implied price.

    `growth_rate` is decimal (0.15 for 15%); we convert to percent for the
    standard PEG formula. Returns None fields gracefully when inputs are
    missing.
    """
    if pe is None or growth_rate is None or growth_rate <= 0:
        return {
            "pe": pe,
            "growth_rate": growth_rate,
            "peg_ratio": None,
            "fair_pe": None,
            "fair_value_per_share": None,
            "note": "missing pe or growth_rate (or non-positive growth)",
        }
    growth_pct = growth_rate * 100
    peg = pe / growth_pct
    fair_pe = growth_pct  # Lynch heuristic
    fair_price = fair_pe * eps if eps is not None else None
    return {
        "pe": pe,
        "growth_rate": growth_rate,
        "growth_pct": growth_pct,
        "peg_ratio": peg,
        "fair_pe": fair_pe,
        "fair_value_per_share": fair_price,
        "interpretation": _interpret_peg(peg),
    }


def _interpret_peg(peg: float) -> str:
    if peg < 0.5:
        return "deeply_undervalued"
    if peg < 1.0:
        return "undervalued"
    if peg < 1.5:
        return "fairly_priced"
    return "overvalued"


def value_via_peg(session: Session, symbol: str) -> dict[str, Any]:
    """Pull pe + growth + eps from the warehouse and call `peg_value`.

    Growth source priority: forward analyst-consensus EPS CAGR → historical
    5-year net_income CAGR. The selected source is recorded in the result
    as `growth_source` so callers can weigh PEG output accordingly.
    """
    ratios = latest_ratios(session, symbol)
    pe = ratios.pe_ratio if ratios else None

    growth = forward_eps_growth_rate(session, symbol)
    growth_source = "forward_consensus" if growth is not None else None
    if growth is None:
        growth = earnings_growth_rate(session, symbol, n=5)
        growth_source = "historical_cagr" if growth is not None else None

    inc = latest_income_statement(session, symbol)
    eps = inc.eps_diluted if inc else None
    out = peg_value(pe, growth, eps=eps)
    out["growth_source"] = growth_source
    out["current_price"] = latest_close(session, symbol)
    if out["fair_value_per_share"] is not None and out["current_price"]:
        out["upside_pct"] = (
            out["fair_value_per_share"] / out["current_price"] - 1
        )
    else:
        out["upside_pct"] = None
    return out
