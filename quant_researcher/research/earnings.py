"""Earnings actual-vs-estimate + thesis surfacing (Item 2, features §D).

`read_earnings` matches recent `IncomeStatement` actuals to `AnalystEstimate`
rows on the shared composite PK `(symbol, fiscal_date, period)`, computes EPS /
revenue surprise where an estimate is present, and surfaces any recorded
`Decision` thesis for the symbol (NOT auto-graded — Claude judges). Pure
warehouse read, no FMP, no writes; an optional transcript excerpt is injected
by the caller (the CLI does the online fetch), mirroring `bundler.build_bundle`.

DATA CAVEAT: `AnalystEstimate` is forward-looking and merge-overwritten, so a
PAST period has an estimate only if it was captured pre-report. Historical
surprise is therefore SPARSE; each period row carries `estimate_available` and
`estimates_matched` makes coverage explicit — we never imply a beat/miss
without an estimate.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant_researcher.models.decisions import Decision
from quant_researcher.models.estimates import AnalystEstimate
from quant_researcher.models.financials import IncomeStatement
from quant_researcher.models.profile import Profile
from quant_researcher.research.sector_classifier import classify_stock_type, net_revenue


def read_earnings(
    session: Session,
    symbol: str,
    *,
    limit: int = 4,
    transcript_excerpt: str | None = None,
    decision_limit: int = 5,
) -> dict[str, Any]:
    """Recent earnings actual vs estimate + recorded thesis(es) for `symbol`."""
    actuals = session.scalars(
        select(IncomeStatement)
        .where(IncomeStatement.symbol == symbol)
        .order_by(IncomeStatement.fiscal_date.desc())
        .limit(limit)
    ).all()

    # Bank-aware revenue normalization (issue #36): FMP's `revenue` for
    # financials is gross (interestIncome + non-interest), while analyst
    # consensus is net. Compare net-vs-net so the surprise number isn't
    # the +111% / +144% nonsense it was before the fix.
    p = session.get(Profile, symbol)
    stock_type = classify_stock_type(
        p.sector if p else None, p.industry if p else None
    )

    notes: list[str] = []
    if not actuals:
        notes.append(
            f"no financial statements for {symbol} — run "
            "`qr data refresh --scope financials`"
        )

    periods: list[dict[str, Any]] = []
    matched = 0
    for a in actuals:
        est = session.get(AnalystEstimate, (symbol, a.fiscal_date, a.period))
        actual_eps = a.eps_diluted if a.eps_diluted is not None else a.eps
        # `revenue` on the actual block stays raw (gross for banks); the
        # bank-aware net revenue is what gets compared to the estimate.
        actual_rev_for_surprise = net_revenue(a, stock_type)
        if est is not None:
            matched += 1
            surprise = _surprise(
                actual_eps, est.eps_avg, actual_rev_for_surprise, est.revenue_avg
            )
            estimate = {
                "revenue_avg": est.revenue_avg,
                "eps_avg": est.eps_avg,
                "ebitda_avg": est.ebitda_avg,
                "net_income_avg": est.net_income_avg,
                "num_analysts_eps": est.num_analysts_eps,
                "num_analysts_revenue": est.num_analysts_revenue,
            }
            note = None
        else:
            surprise = None
            estimate = None
            note = "estimate unavailable — not captured before this period reported"
        actual_block: dict[str, Any] = {
            "revenue": a.revenue,
            "net_income": a.net_income,
            "eps": a.eps,
            "eps_diluted": a.eps_diluted,
            "gross_profit": a.gross_profit,
            "operating_income": a.operating_income,
        }
        # For banks, surface net revenue alongside the raw (gross) line so
        # downstream consumers see both. For non-financials this would be
        # redundant (gross == net), so omit.
        if stock_type == "bank":
            actual_block["revenue_net"] = actual_rev_for_surprise
        periods.append(
            {
                "period": a.period,
                "fiscal_date": a.fiscal_date.isoformat(),
                "filed_at": a.known_at.isoformat() if a.known_at else None,
                "reported_currency": a.reported_currency,
                "actual": actual_block,
                "estimate_available": est is not None,
                "estimate": estimate,
                "surprise": surprise,
                "note": note,
            }
        )

    decisions = session.scalars(
        select(Decision)
        .where(Decision.symbol == symbol)
        .order_by(Decision.created_at.desc())
        .limit(decision_limit)
    ).all()

    return {
        "symbol": symbol,
        "as_of": date.today().isoformat(),
        "limit": limit,
        "periods_found": len(periods),
        "estimates_matched": matched,
        "periods": periods,
        "thesis": {
            "count": len(decisions),
            "decisions": [
                {
                    "decision_id": d.decision_id,
                    "side": d.side,
                    "opened_at": d.opened_at.isoformat() if d.opened_at else None,
                    "price_at_open": d.price_at_open,
                    "confidence": d.confidence,
                    "thesis": d.thesis,
                    "sector_at_open": d.sector_at_open,
                    "tags": d.tags,
                    "bundle_id": d.bundle_id,
                }
                for d in decisions
            ],
        },
        "transcript": (
            {"available": True, "excerpt": transcript_excerpt}
            if transcript_excerpt
            else None
        ),
        "notes": notes,
    }


def _surprise(
    actual_eps: float | None,
    est_eps: float | None,
    actual_rev: float | None,
    est_rev: float | None,
) -> dict[str, Any]:
    return {
        "eps_beat": _beat(actual_eps, est_eps),
        "eps_surprise_pct": _surprise_pct(actual_eps, est_eps),
        "revenue_beat": _beat(actual_rev, est_rev),
        "revenue_surprise_pct": _surprise_pct(actual_rev, est_rev),
    }


def _beat(actual: float | None, est: float | None) -> float | None:
    if actual is None or est is None:
        return None
    return actual - est


def _surprise_pct(actual: float | None, est: float | None) -> float | None:
    # abs() denom keeps the sign correct even when the estimate is negative.
    if actual is None or not est:
        return None
    return (actual - est) / abs(est) * 100
