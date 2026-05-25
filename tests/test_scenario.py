"""Scenario DCF — probability-weighted bull/base/bear (pure function)."""

from __future__ import annotations

import pytest

from quant_researcher.valuation.dcf import dcf_fcff
from quant_researcher.valuation.scenario import scenario_dcf

COMMON = dict(
    base_fcf=1000.0, terminal_growth=0.025, wacc=0.09, n_years=5, net_debt=0.0, shares=100.0
)


def test_weighted_matches_manual() -> None:
    scenarios = {
        "bear": {"growth": 0.02, "prob": 0.25},
        "base": {"growth": 0.06, "prob": 0.50},
        "bull": {"growth": 0.10, "prob": 0.25},
    }
    res = scenario_dcf(scenarios=scenarios, **COMMON)
    fvs = {
        k: dcf_fcff(growth_rate=v["growth"], **COMMON)["fair_value_per_share"]
        for k, v in scenarios.items()
    }
    expected = 0.25 * fvs["bear"] + 0.50 * fvs["base"] + 0.25 * fvs["bull"]
    assert res["weighted_fair_value_per_share"] == pytest.approx(expected)
    assert res["weight_used"] == pytest.approx(1.0)
    for k in scenarios:
        assert res["scenarios"][k]["fair_value_per_share"] == pytest.approx(fvs[k])


def test_undefined_scenario_dropped_and_renormalized() -> None:
    # bull's wacc (0.02) <= terminal_growth (0.025) → DCFError → excluded.
    scenarios = {
        "base": {"growth": 0.05, "prob": 0.5},
        "bull": {"growth": 0.05, "prob": 0.5, "wacc": 0.02},
    }
    res = scenario_dcf(scenarios=scenarios, **COMMON)
    assert res["scenarios"]["bull"]["fair_value_per_share"] is None
    assert res["weight_used"] == pytest.approx(0.5)  # only base counted
    base_fv = dcf_fcff(growth_rate=0.05, **COMMON)["fair_value_per_share"]
    assert res["weighted_fair_value_per_share"] == pytest.approx(base_fv)
