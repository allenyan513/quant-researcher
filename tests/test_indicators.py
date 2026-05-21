"""Indicators — boundary cases + known reference values."""

from __future__ import annotations

import numpy as np
import pytest

from quant_researcher.screen.indicators import (
    ema,
    macd,
    rolling_max,
    rolling_min,
    rsi,
    sma,
)

# ----- SMA -----------------------------------------------------------------


def test_sma_basic() -> None:
    out = sma(np.array([1, 2, 3, 4, 5], dtype=float), n=3)
    # First two NaN, then averages of windows.
    assert np.isnan(out[:2]).all()
    np.testing.assert_allclose(out[2:], [2.0, 3.0, 4.0])


def test_sma_window_larger_than_input_all_nan() -> None:
    out = sma(np.array([1.0, 2.0]), n=5)
    assert np.isnan(out).all()


def test_sma_rejects_nonpositive_window() -> None:
    with pytest.raises(ValueError):
        sma(np.array([1.0]), n=0)


# ----- EMA -----------------------------------------------------------------


def test_ema_seeded_with_sma() -> None:
    arr = np.array([1, 2, 3, 4, 5, 6], dtype=float)
    out = ema(arr, n=3)
    # First two slots NaN; index 2 = SMA(first 3) = 2.0.
    assert np.isnan(out[:2]).all()
    assert out[2] == pytest.approx(2.0)
    # alpha = 2/(3+1) = 0.5
    # out[3] = 0.5 * 4 + 0.5 * 2 = 3.0
    assert out[3] == pytest.approx(3.0)
    # out[4] = 0.5 * 5 + 0.5 * 3 = 4.0
    assert out[4] == pytest.approx(4.0)


def test_ema_constant_series_converges_to_value() -> None:
    out = ema(np.full(20, 5.0), n=5)
    assert out[-1] == pytest.approx(5.0)


# ----- MACD ----------------------------------------------------------------


def test_macd_returns_three_arrays_of_same_length() -> None:
    arr = np.linspace(100, 200, 80)
    m, s, h = macd(arr)
    assert m.shape == s.shape == h.shape == arr.shape


def test_macd_rejects_fast_ge_slow() -> None:
    with pytest.raises(ValueError):
        macd(np.zeros(50), fast=26, slow=12)


def test_macd_step_up_yields_positive_macd_line() -> None:
    # Flat at 100 for 40 bars, then step to 200 — fast EMA catches up faster
    # than slow, so MACD line goes positive after the step.
    arr = np.concatenate([np.full(40, 100.0), np.full(40, 200.0)])
    m, _, _ = macd(arr)
    # A few bars after the step, MACD should be clearly positive.
    assert m[50] > 5.0


# ----- RSI -----------------------------------------------------------------


def test_rsi_all_gains_is_100() -> None:
    arr = np.arange(30, dtype=float)
    out = rsi(arr, n=14)
    # First 14 slots NaN; thereafter all gains → RSI = 100.
    assert np.isnan(out[:14]).all()
    assert out[-1] == pytest.approx(100.0)


def test_rsi_oscillating_in_range() -> None:
    rng = np.random.default_rng(42)
    arr = 100 + np.cumsum(rng.normal(0, 1, 100))
    out = rsi(arr, n=14)
    valid = out[~np.isnan(out)]
    assert valid.min() >= 0.0
    assert valid.max() <= 100.0


def test_rsi_window_larger_than_input() -> None:
    out = rsi(np.array([1.0, 2.0, 3.0]), n=14)
    assert np.isnan(out).all()


# ----- rolling max/min -----------------------------------------------------


def test_rolling_max_basic() -> None:
    out = rolling_max(np.array([1, 3, 2, 5, 4], dtype=float), n=3)
    assert np.isnan(out[:2]).all()
    np.testing.assert_allclose(out[2:], [3.0, 5.0, 5.0])


def test_rolling_min_basic() -> None:
    out = rolling_min(np.array([5, 3, 4, 1, 2], dtype=float), n=3)
    assert np.isnan(out[:2]).all()
    np.testing.assert_allclose(out[2:], [3.0, 1.0, 1.0])
