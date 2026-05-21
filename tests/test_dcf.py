"""DCF-FCFF math — closed-form sanity, sensitivity monotonicity, edge errors."""

from __future__ import annotations

import pytest

from quant_researcher.valuation.dcf import (
    DCFError,
    dcf_fcff,
    default_growth_from_history,
    sensitivity_5x5,
    smoothed_base_fcf,
)

# ----- Single scenario ----------------------------------------------------


def test_zero_growth_perpetuity_matches_closed_form() -> None:
    # With growth_rate=0 and terminal_growth=0, the DCF reduces to a flat
    # perpetuity: EV = base_fcf / wacc.
    result = dcf_fcff(
        base_fcf=100.0,
        growth_rate=0.0,
        terminal_growth=0.0,
        wacc=0.10,
        n_years=5,
        net_debt=0.0,
        shares=10.0,
    )
    # Sum of finite PVs (years 1..5) + terminal PV (perpetuity at year 5)
    # equals exactly 100/0.10 = 1000 (algebraic identity).
    assert result["enterprise_value"] == pytest.approx(1000.0, rel=1e-9)
    assert result["fair_value_per_share"] == pytest.approx(100.0)


def test_gordon_terminal_uses_year_n_fcf() -> None:
    r = dcf_fcff(
        base_fcf=100.0,
        growth_rate=0.05,
        terminal_growth=0.02,
        wacc=0.10,
        n_years=5,
    )
    # FCF_5 = 100 * 1.05^5 ≈ 127.628
    assert r["projected_fcf"][-1] == pytest.approx(100 * 1.05**5, rel=1e-6)
    # TV = 127.628 * 1.02 / (0.10 - 0.02) = 1627.252
    assert r["terminal_value"] == pytest.approx(
        100 * 1.05**5 * 1.02 / (0.10 - 0.02), rel=1e-6
    )


def test_subtracts_net_debt() -> None:
    r_no_debt = dcf_fcff(
        base_fcf=100.0,
        growth_rate=0.0,
        terminal_growth=0.0,
        wacc=0.10,
        n_years=5,
        net_debt=0.0,
        shares=10.0,
    )
    r_with_debt = dcf_fcff(
        base_fcf=100.0,
        growth_rate=0.0,
        terminal_growth=0.0,
        wacc=0.10,
        n_years=5,
        net_debt=200.0,
        shares=10.0,
    )
    assert r_with_debt["equity_value"] == r_no_debt["equity_value"] - 200.0
    assert (
        r_with_debt["fair_value_per_share"]
        == pytest.approx((r_no_debt["equity_value"] - 200.0) / 10.0)
    )


def test_shares_none_returns_none_per_share() -> None:
    r = dcf_fcff(
        base_fcf=100.0,
        growth_rate=0.0,
        terminal_growth=0.0,
        wacc=0.10,
        shares=None,
    )
    assert r["fair_value_per_share"] is None


def test_wacc_le_terminal_growth_raises() -> None:
    with pytest.raises(DCFError, match="wacc"):
        dcf_fcff(
            base_fcf=100.0,
            growth_rate=0.05,
            terminal_growth=0.10,  # = wacc → undefined
            wacc=0.10,
        )
    with pytest.raises(DCFError):
        dcf_fcff(
            base_fcf=100.0,
            growth_rate=0.05,
            terminal_growth=0.12,  # > wacc → undefined
            wacc=0.10,
        )


def test_n_years_rejects_zero() -> None:
    with pytest.raises(DCFError):
        dcf_fcff(
            base_fcf=100.0,
            growth_rate=0.05,
            terminal_growth=0.02,
            wacc=0.10,
            n_years=0,
        )


# ----- Sensitivity --------------------------------------------------------


def test_sensitivity_grid_shape() -> None:
    s = sensitivity_5x5(
        base_fcf=100.0,
        growth_rates=[0.0, 0.02, 0.04, 0.06, 0.08],
        waccs=[0.08, 0.09, 0.10, 0.11, 0.12],
        terminal_growth=0.025,
        shares=10.0,
    )
    assert len(s["grid"]) == 5
    assert all(len(row) == 5 for row in s["grid"])


def test_sensitivity_higher_growth_increases_value() -> None:
    s = sensitivity_5x5(
        base_fcf=100.0,
        growth_rates=[0.0, 0.05],
        waccs=[0.10],
        terminal_growth=0.02,
        shares=10.0,
    )
    # Higher growth at same WACC → higher per-share value.
    assert s["grid"][1][0] > s["grid"][0][0]


def test_sensitivity_higher_wacc_decreases_value() -> None:
    s = sensitivity_5x5(
        base_fcf=100.0,
        growth_rates=[0.05],
        waccs=[0.08, 0.12],
        terminal_growth=0.02,
        shares=10.0,
    )
    # Higher WACC at same growth → lower per-share value.
    assert s["grid"][0][1] < s["grid"][0][0]


def test_sensitivity_marks_undefined_cells_none() -> None:
    s = sensitivity_5x5(
        base_fcf=100.0,
        growth_rates=[0.05],
        waccs=[0.02, 0.10],  # 0.02 < terminal_growth 0.025 → None
        terminal_growth=0.025,
        shares=10.0,
    )
    assert s["grid"][0][0] is None
    assert s["grid"][0][1] is not None


# ----- helpers -----------------------------------------------------------


def test_smoothed_base_fcf_median_of_tail() -> None:
    # Median of last 3 = median([100, 200, 150]) = 150
    assert smoothed_base_fcf([10, 20, 100, 200, 150], window=3) == 150.0


def test_smoothed_base_fcf_none_when_empty() -> None:
    assert smoothed_base_fcf([]) is None


def test_default_growth_from_history_clipped() -> None:
    # 100 → 250 over 4 years: CAGR ≈ 25.7% → clipped to 0.20.
    g = default_growth_from_history([100, 150, 200, 225, 250])
    assert g == pytest.approx(0.20)


def test_default_growth_floor_clips_decline() -> None:
    g = default_growth_from_history([100, 80, 70, 60, 30])
    assert g == pytest.approx(-0.10)


def test_default_growth_none_for_negative_start() -> None:
    assert default_growth_from_history([-10, 5, 10]) is None
