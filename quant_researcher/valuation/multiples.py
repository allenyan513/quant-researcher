"""Relative valuation via sector-median multiples.

For each of P/E, EV/EBITDA, EV/Revenue we:
1. Compute the median multiple across the target's sector (peer set drawn
   from `profiles` joined to latest annual `financial_ratios`).
2. Apply it to the target's per-share/EBITDA/Revenue.
3. Return implied per-share value + upside vs current price.

Sector medians are computed at call time from the current warehouse — no
`sector_betas`-style cached table (D6: not needed yet). If the sector has
fewer than 2 peers with the metric, the relevant multiple returns None
(too thin to be meaningful).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from quant_researcher.valuation.helpers import (
    latest_close,
    latest_ebitda,
    latest_income_statement,
    latest_market_cap,
    latest_revenue,
    net_debt,
    sector_for_symbol,
    sector_peer_median,
    shares_outstanding,
)


def _safe_div(num: float | None, denom: float | None) -> float | None:
    if num is None or denom is None or denom == 0:
        return None
    return num / denom


def pe_implied_price(session: Session, symbol: str, sector: str) -> dict[str, Any]:
    peer_median = sector_peer_median(session, sector, "pe_ratio")
    inc = latest_income_statement(session, symbol)
    eps = inc.eps_diluted if inc else None
    implied = peer_median * eps if peer_median is not None and eps is not None else None
    return {
        "peer_median_pe": peer_median,
        "eps_diluted": eps,
        "implied_price": implied,
    }


def ev_ebitda_implied_price(
    session: Session, symbol: str, sector: str
) -> dict[str, Any]:
    peer_median = sector_peer_median(session, sector, "ev_to_ebitda")
    ebitda = latest_ebitda(session, symbol)
    shares = shares_outstanding(session, symbol)
    debt = net_debt(session, symbol) or 0.0
    implied_ev = peer_median * ebitda if peer_median is not None and ebitda is not None else None
    implied_equity = implied_ev - debt if implied_ev is not None else None
    implied_price = _safe_div(implied_equity, shares)
    return {
        "peer_median_ev_ebitda": peer_median,
        "ebitda": ebitda,
        "implied_enterprise_value": implied_ev,
        "implied_equity_value": implied_equity,
        "shares": shares,
        "implied_price": implied_price,
    }


def ev_revenue_implied_price(
    session: Session, symbol: str, sector: str
) -> dict[str, Any]:
    peer_median = sector_peer_median(session, sector, "price_to_sales")
    # `price_to_sales` ≈ market_cap / revenue, so implied market_cap
    # = peer_median × revenue, then per-share = implied_mcap / shares.
    revenue = latest_revenue(session, symbol)
    shares = shares_outstanding(session, symbol)
    if peer_median is not None and revenue is not None:
        implied_mcap = peer_median * revenue
    else:
        implied_mcap = None
    implied_price = _safe_div(implied_mcap, shares)
    return {
        "peer_median_ps": peer_median,
        "revenue": revenue,
        "implied_market_cap": implied_mcap,
        "shares": shares,
        "implied_price": implied_price,
    }


def value_via_multiples(session: Session, symbol: str) -> dict[str, Any]:
    """Run all three peer-median multiples for `symbol` and aggregate."""
    sector = sector_for_symbol(session, symbol)
    if not sector:
        return {
            "symbol": symbol,
            "sector": None,
            "note": "no sector recorded for symbol",
            "models": {},
            "fair_value_per_share": None,
        }

    pe_result = pe_implied_price(session, symbol, sector)
    ev_ebitda_result = ev_ebitda_implied_price(session, symbol, sector)
    ev_rev_result = ev_revenue_implied_price(session, symbol, sector)

    implied_prices = [
        r["implied_price"]
        for r in (pe_result, ev_ebitda_result, ev_rev_result)
        if r["implied_price"] is not None
    ]
    avg_implied = (
        sum(implied_prices) / len(implied_prices) if implied_prices else None
    )

    current = latest_close(session, symbol) or latest_market_cap(session, symbol)
    upside = None
    if avg_implied is not None and current and current > 0:
        upside = avg_implied / current - 1

    return {
        "symbol": symbol,
        "sector": sector,
        "models": {
            "pe": pe_result,
            "ev_ebitda": ev_ebitda_result,
            "ev_revenue": ev_rev_result,
        },
        "fair_value_per_share": avg_implied,
        "current_price": latest_close(session, symbol),
        "upside_pct": upside,
    }
