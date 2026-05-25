"""Scenario (bull / base / bear) probability-weighted DCF — pure-Python.

Each named scenario overrides the stage-1 growth (and optionally WACC); we run
the forward DCF per scenario and weight the per-share fair values by their
probabilities. A scenario whose DCF is undefined (`wacc <= terminal_growth`)
contributes a None value and is dropped from the weighting (weights renormalize
over the scenarios that produced a value).
"""

from __future__ import annotations

from typing import Any

from quant_researcher.valuation.dcf import DCFError, dcf_fcff


def scenario_dcf(
    *,
    base_fcf: float,
    scenarios: dict[str, dict[str, float]],
    terminal_growth: float,
    wacc: float,
    n_years: int = 5,
    net_debt: float = 0.0,
    shares: float | None = None,
) -> dict[str, Any]:
    """Probability-weighted DCF across named scenarios.

    `scenarios` maps name → `{"growth": g, "prob": p[, "wacc": w]}`. Returns each
    scenario's `fair_value_per_share` plus the probability-weighted mean over the
    scenarios that produced a value (weights renormalized via `weight_used`).
    """
    per_scenario: dict[str, Any] = {}
    weighted_num = 0.0
    weight_den = 0.0
    for name, spec in scenarios.items():
        g = spec["growth"]
        w = spec.get("wacc", wacc)
        prob = spec.get("prob", 0.0)
        try:
            fv = dcf_fcff(
                base_fcf=base_fcf,
                growth_rate=g,
                terminal_growth=terminal_growth,
                wacc=w,
                n_years=n_years,
                net_debt=net_debt,
                shares=shares,
            )["fair_value_per_share"]
        except DCFError:
            fv = None
        per_scenario[name] = {"growth": g, "wacc": w, "prob": prob, "fair_value_per_share": fv}
        if fv is not None:
            weighted_num += prob * fv
            weight_den += prob
    weighted = weighted_num / weight_den if weight_den > 0 else None
    return {
        "scenarios": per_scenario,
        "weighted_fair_value_per_share": weighted,
        "weight_used": weight_den,
    }
