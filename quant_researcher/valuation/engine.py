"""Valuation orchestration — runs requested models, persists snapshot rows.

The `value_company` function is the single entry point used by both the
CLI and any future Python orchestration. It returns a dict (one block
per model run) and writes one `ValuationSnapshot` row per model so
individual model results can be diffed / replayed.

For `model='all'` (default), we run DCF + PEG + multiples. EPV and DDM
are deliberately left as future work (deferred from MC v1).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from quant_researcher.contract import code_version
from quant_researcher.models.valuation import ValuationSnapshot
from quant_researcher.valuation.dcf import (
    DCFError,
    dcf_fcff,
    default_growth_from_history,
    sensitivity_5x5,
    smoothed_base_fcf,
)
from quant_researcher.valuation.helpers import (
    forward_eps_growth_rate,
    historical_fcf,
    latest_close,
    net_debt,
    shares_outstanding,
)
from quant_researcher.valuation.multiples import value_via_multiples
from quant_researcher.valuation.peg import value_via_peg
from quant_researcher.valuation.reverse_dcf import implied_growth
from quant_researcher.valuation.scenario import scenario_dcf
from quant_researcher.valuation.wacc import wacc_for_symbol

VALID_MODELS = ("dcf", "peg", "multiples", "scenario", "all")


def value_company(
    session: Session,
    symbol: str,
    *,
    model: str = "all",
    assumptions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the requested valuation model(s) for `symbol`.

    `assumptions` (optional) overrides defaults — supports:
    `growth_rate`, `terminal_growth`, `n_years`, `wacc`, `rf`, `erp`,
    `base_fcf`. Missing keys fall back to sensible defaults / inferred
    values from the warehouse.
    """
    if model not in VALID_MODELS:
        raise ValueError(f"unknown model: {model!r}; valid: {VALID_MODELS}")

    assumptions = dict(assumptions or {})
    models_to_run = (
        ("dcf", "peg", "multiples", "scenario") if model == "all" else (model,)
    )
    out: dict[str, Any] = {
        "symbol": symbol,
        "model": model,
        "as_of": date.today().isoformat(),
        "current_price": latest_close(session, symbol),
        "models": {},
        "snapshot_ids": {},
    }

    for m in models_to_run:
        if m == "dcf":
            res = _run_dcf(session, symbol, assumptions)
        elif m == "peg":
            res = value_via_peg(session, symbol)
        elif m == "multiples":
            res = value_via_multiples(session, symbol)
        elif m == "scenario":
            res = _run_scenario(session, symbol, assumptions)
        else:  # pragma: no cover — covered by VALID_MODELS check
            continue
        out["models"][m] = res
        snap_id = _persist_snapshot(session, symbol, m, res)
        if snap_id is not None:
            out["snapshot_ids"][m] = snap_id

    # Cross-model aggregate: simple mean of available fair values. Scenario is a
    # DCF variant, so it's excluded — blending it would double-count the DCF
    # methodology against the independent peg / multiples reads.
    fair_values = [
        v["fair_value_per_share"]
        for k, v in out["models"].items()
        if k != "scenario" and v.get("fair_value_per_share") is not None
    ]
    if fair_values:
        out["fair_value_per_share_mean"] = sum(fair_values) / len(fair_values)
        if out["current_price"]:
            out["upside_pct_mean"] = (
                out["fair_value_per_share_mean"] / out["current_price"] - 1
            )
        else:
            out["upside_pct_mean"] = None
    else:
        out["fair_value_per_share_mean"] = None
        out["upside_pct_mean"] = None

    return out


def _run_dcf(
    session: Session, symbol: str, assumptions: dict[str, Any]
) -> dict[str, Any]:
    history = historical_fcf(session, symbol, n=5)
    base_fcf = assumptions.get("base_fcf") or smoothed_base_fcf(history)
    if base_fcf is None or base_fcf <= 0:
        return {
            "fair_value_per_share": None,
            "note": "no positive historical FCF",
            "history": history,
        }

    wacc = assumptions.get("wacc")
    if wacc is None:
        wacc, wacc_breakdown = wacc_for_symbol(
            session,
            symbol,
            rf=assumptions.get("rf", 0.045),
            erp=assumptions.get("erp", 0.055),
        )
    else:
        wacc_breakdown = {"wacc": wacc, "source": "user_override"}

    growth_rate = assumptions.get("growth_rate")
    growth_source = "user_override" if growth_rate is not None else None
    if growth_rate is None:
        forward = forward_eps_growth_rate(session, symbol)
        if forward is not None:
            growth_rate = forward
            growth_source = "forward_consensus"
        else:
            growth_rate = default_growth_from_history(history) or 0.04
            growth_source = "historical_fcf_cagr"
    terminal_growth = assumptions.get("terminal_growth", 0.025)
    n_years = int(assumptions.get("n_years", 5))

    shares = assumptions.get("shares") or shares_outstanding(session, symbol)
    debt = assumptions.get("net_debt")
    if debt is None:
        debt = net_debt(session, symbol) or 0.0

    try:
        core = dcf_fcff(
            base_fcf=base_fcf,
            growth_rate=growth_rate,
            terminal_growth=terminal_growth,
            wacc=wacc,
            n_years=n_years,
            net_debt=debt,
            shares=shares,
        )
    except DCFError as exc:
        return {
            "fair_value_per_share": None,
            "note": f"DCF undefined: {exc}",
            "history": history,
        }

    sens = sensitivity_5x5(
        base_fcf=base_fcf,
        growth_rates=[
            max(growth_rate - 0.04, -0.10),
            max(growth_rate - 0.02, -0.10),
            growth_rate,
            growth_rate + 0.02,
            growth_rate + 0.04,
        ],
        waccs=[
            max(wacc - 0.02, 0.02),
            max(wacc - 0.01, 0.02),
            wacc,
            wacc + 0.01,
            wacc + 0.02,
        ],
        terminal_growth=terminal_growth,
        n_years=n_years,
        net_debt=debt,
        shares=shares,
    )

    current = latest_close(session, symbol)
    upside = (
        core["fair_value_per_share"] / current - 1
        if current and core["fair_value_per_share"]
        else None
    )

    reverse = _reverse_dcf_block(
        base_fcf=base_fcf,
        terminal_growth=terminal_growth,
        wacc=wacc,
        n_years=n_years,
        net_debt=debt,
        shares=shares,
        current=current,
        assumed_growth=growth_rate,
        history=history,
    )

    return {
        "fair_value_per_share": core["fair_value_per_share"],
        "current_price": current,
        "upside_pct": upside,
        "history": history,
        "growth_source": growth_source,
        "wacc_breakdown": wacc_breakdown,
        "core": {
            "enterprise_value": core["enterprise_value"],
            "equity_value": core["equity_value"],
            "projected_fcf": core["projected_fcf"],
            "pv_per_year": core["pv_per_year"],
            "terminal_value": core["terminal_value"],
            "terminal_pv": core["terminal_pv"],
            "assumptions": core["assumptions"],
        },
        "sensitivity": sens,
        "reverse": reverse,
    }


def _reverse_dcf_block(
    *,
    base_fcf: float,
    terminal_growth: float,
    wacc: float,
    n_years: int,
    net_debt: float,
    shares: float | None,
    current: float | None,
    assumed_growth: float,
    history: list[float],
) -> dict[str, Any] | None:
    """Implied stage-1 growth at the current price + the expectations gap.

    The gap (implied − assumed, implied − historical FCF CAGR) is the
    value-investor signal: how much of the price rests on growth above what the
    forward DCF assumed / what history delivered.
    """
    if not current or current <= 0:
        return None
    rev = implied_growth(
        base_fcf=base_fcf,
        terminal_growth=terminal_growth,
        wacc=wacc,
        target_price=current,
        n_years=n_years,
        net_debt=net_debt,
        shares=shares,
    )
    hist_g = default_growth_from_history(history)
    rev["assumed_growth"] = assumed_growth
    rev["history_growth"] = hist_g
    ig = rev.get("implied_growth")
    if ig is not None:
        rev["gap_vs_assumed"] = ig - assumed_growth
        rev["gap_vs_history"] = (ig - hist_g) if hist_g is not None else None
    return rev


def _run_scenario(
    session: Session, symbol: str, assumptions: dict[str, Any]
) -> dict[str, Any]:
    """Probability-weighted bull/base/bear DCF.

    Scenarios auto-derive from the base-case growth (base ± `scenario_delta`,
    probs 25/50/25) but every input — `scenarios`, `scenario_delta`, growth,
    wacc, terminal_growth, n_years — is overridable via `assumptions`, mirroring
    `_run_dcf`'s resolution order.
    """
    history = historical_fcf(session, symbol, n=5)
    base_fcf = assumptions.get("base_fcf") or smoothed_base_fcf(history)
    if base_fcf is None or base_fcf <= 0:
        return {
            "fair_value_per_share": None,
            "note": "no positive historical FCF",
            "history": history,
        }

    wacc = assumptions.get("wacc")
    if wacc is None:
        wacc, _ = wacc_for_symbol(
            session,
            symbol,
            rf=assumptions.get("rf", 0.045),
            erp=assumptions.get("erp", 0.055),
        )

    base_growth = assumptions.get("growth_rate")
    growth_source = "user_override" if base_growth is not None else None
    if base_growth is None:
        forward = forward_eps_growth_rate(session, symbol)
        if forward is not None:
            base_growth = forward
            growth_source = "forward_consensus"
        else:
            base_growth = default_growth_from_history(history) or 0.04
            growth_source = "historical_fcf_cagr"
    terminal_growth = assumptions.get("terminal_growth", 0.025)
    n_years = int(assumptions.get("n_years", 5))
    shares = assumptions.get("shares") or shares_outstanding(session, symbol)
    debt = assumptions.get("net_debt")
    if debt is None:
        debt = net_debt(session, symbol) or 0.0

    delta = assumptions.get("scenario_delta", 0.04)
    scenarios = assumptions.get("scenarios") or {
        "bear": {"growth": base_growth - delta, "prob": 0.25},
        "base": {"growth": base_growth, "prob": 0.50},
        "bull": {"growth": base_growth + delta, "prob": 0.25},
    }

    res = scenario_dcf(
        base_fcf=base_fcf,
        scenarios=scenarios,
        terminal_growth=terminal_growth,
        wacc=wacc,
        n_years=n_years,
        net_debt=debt,
        shares=shares,
    )
    current = latest_close(session, symbol)
    weighted = res["weighted_fair_value_per_share"]
    upside = (weighted / current - 1) if (current and weighted) else None
    return {
        "fair_value_per_share": weighted,
        "current_price": current,
        "upside_pct": upside,
        "history": history,
        "growth_source": growth_source,
        "scenarios": res["scenarios"],
        "weight_used": res["weight_used"],
    }


def _persist_snapshot(
    session: Session,
    symbol: str,
    model_type: str,
    result: dict[str, Any],
) -> str | None:
    snap_id = str(uuid.uuid4())
    fair = result.get("fair_value_per_share")
    current = result.get("current_price")
    upside = result.get("upside_pct")
    assumptions = result.get("core", {}).get("assumptions") if model_type == "dcf" else None
    sensitivity = result.get("sensitivity") if model_type == "dcf" else None
    session.add(
        ValuationSnapshot(
            snapshot_id=snap_id,
            symbol=symbol,
            model_type=model_type,
            as_of=date.today(),
            fair_value_per_share=fair,
            current_price=current,
            upside_pct=upside,
            assumptions=assumptions,
            result=_jsonable(result),
            sensitivity=sensitivity,
            code_version=code_version(),
        )
    )
    return snap_id


def _jsonable(obj: Any) -> Any:
    """Best-effort JSON-friendly copy (the snapshot column is sqlalchemy.JSON)."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "isoformat"):  # date / datetime
        return obj.isoformat()
    return obj
