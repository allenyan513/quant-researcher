"""Reverse DCF — implied-growth solver (pure function)."""

from __future__ import annotations

import pytest

from quant_researcher.valuation.dcf import dcf_fcff
from quant_researcher.valuation.reverse_dcf import implied_growth

COMMON = dict(
    base_fcf=1000.0, terminal_growth=0.025, wacc=0.09, n_years=5, net_debt=0.0, shares=100.0
)


def test_roundtrip_recovers_growth() -> None:
    # Price = the DCF value at g=10% → solver should back out ~10%.
    target = dcf_fcff(growth_rate=0.10, **COMMON)["fair_value_per_share"]
    r = implied_growth(target_price=target, **COMMON)
    assert r["implied_growth"] == pytest.approx(0.10, abs=1e-3)
    assert r["note"] is None


def test_price_above_bracket_returns_none() -> None:
    r = implied_growth(target_price=1e12, **COMMON)
    assert r["implied_growth"] is None
    assert r.get("implied_above") is True


def test_price_below_bracket_returns_none() -> None:
    r = implied_growth(target_price=0.01, **COMMON)
    assert r["implied_growth"] is None
    assert r.get("implied_below") is True


def test_missing_shares_returns_none() -> None:
    r = implied_growth(target_price=100.0, **{**COMMON, "shares": None})
    assert r["implied_growth"] is None
    assert "shares" in r["note"]
