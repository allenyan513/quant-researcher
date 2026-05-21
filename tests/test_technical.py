"""Technical predicate DSL — parser + each predicate's boundary behavior."""

from __future__ import annotations

import numpy as np
import pytest

from quant_researcher.screen.technical import (
    TechnicalError,
    available_predicates,
    parse_technical,
)

# ----- Parser --------------------------------------------------------------


def test_parse_single_no_args() -> None:
    preds = parse_technical("above_sma[50]")
    assert len(preds) == 1


def test_parse_multiple_comma_separated() -> None:
    preds = parse_technical("above_sma[50],macd_golden_cross[5]")
    assert len(preds) == 2


def test_parse_unknown_predicate_rejected() -> None:
    with pytest.raises(TechnicalError, match="unknown predicate"):
        parse_technical("nope[5]")


def test_parse_malformed_token_rejected() -> None:
    with pytest.raises(TechnicalError):
        parse_technical("above_sma[")


def test_parse_non_numeric_arg_rejected() -> None:
    with pytest.raises(TechnicalError, match="non-numeric"):
        parse_technical("above_sma[abc]")


def test_parse_empty_spec_rejected() -> None:
    with pytest.raises(TechnicalError, match="empty"):
        parse_technical("")
    with pytest.raises(TechnicalError, match="empty"):
        parse_technical("   ")


def test_available_predicates_list() -> None:
    names = available_predicates()
    assert "above_sma" in names
    assert "macd_golden_cross" in names
    assert "rsi_oversold" in names
    assert "near_52w_high" in names
    assert "volume_spike" in names


# ----- Helper to invoke a single predicate ---------------------------------


def _run(spec: str, closes: np.ndarray, volumes: np.ndarray | None = None) -> bool:
    preds = parse_technical(spec)
    if volumes is None:
        volumes = np.ones_like(closes)
    return all(p(closes, volumes) for p in preds)


# ----- above_sma / below_sma -----------------------------------------------


def test_above_sma_true_when_close_above_average() -> None:
    closes = np.concatenate([np.full(49, 100.0), np.array([150.0])])
    assert _run("above_sma[50]", closes) is True


def test_above_sma_false_when_close_below_average() -> None:
    closes = np.concatenate([np.full(49, 100.0), np.array([50.0])])
    assert _run("above_sma[50]", closes) is False


def test_above_sma_false_when_insufficient_bars() -> None:
    closes = np.linspace(100, 200, 30)
    assert _run("above_sma[50]", closes) is False


def test_below_sma_true_when_close_below_average() -> None:
    closes = np.concatenate([np.full(49, 100.0), np.array([50.0])])
    assert _run("below_sma[50]", closes) is True


# ----- rsi_oversold / overbought -------------------------------------------


def test_rsi_oversold_true_for_declining_series() -> None:
    # Monotonically falling → RSI → 0 → oversold.
    closes = np.linspace(200, 100, 30)
    assert _run("rsi_oversold[1]", closes) is True


def test_rsi_overbought_true_for_rising_series() -> None:
    closes = np.linspace(100, 200, 30)
    assert _run("rsi_overbought[1]", closes) is True


def test_rsi_oversold_window_arg() -> None:
    # Even if today's RSI is back above 30, a recent dip within window counts.
    closes = np.concatenate(
        [
            np.linspace(200, 100, 30),  # forces oversold
            np.linspace(100, 110, 3),  # mild recovery
        ]
    )
    assert _run("rsi_oversold[5]", closes) is True


# ----- macd crosses --------------------------------------------------------


def test_macd_golden_cross_after_step_up() -> None:
    # Flat at 100 for 40 bars, then step to 200 for 40 bars + some bumps to
    # create a MACD crossover.
    closes = np.concatenate([np.full(40, 100.0), np.full(40, 200.0)])
    # MACD on this pattern eventually has a golden cross; check within last 80.
    assert _run("macd_golden_cross[80]", closes) is True


def test_macd_death_cross_after_step_down() -> None:
    closes = np.concatenate([np.full(40, 200.0), np.full(40, 100.0)])
    assert _run("macd_death_cross[80]", closes) is True


def test_macd_golden_cross_false_for_flat_series() -> None:
    closes = np.full(80, 100.0)
    assert _run("macd_golden_cross[5]", closes) is False


# ----- 52w high / low ------------------------------------------------------


def test_near_52w_high_true_when_close_at_high() -> None:
    closes = np.concatenate([np.linspace(50, 100, 251), np.array([100.0])])
    assert _run("near_52w_high[3]", closes) is True


def test_near_52w_high_false_when_close_far_below() -> None:
    closes = np.concatenate([np.linspace(50, 100, 251), np.array([60.0])])
    assert _run("near_52w_high[5]", closes) is False


def test_near_52w_low_true_when_close_at_low() -> None:
    closes = np.concatenate([np.linspace(100, 50, 251), np.array([50.0])])
    assert _run("near_52w_low[3]", closes) is True


# ----- volume spike --------------------------------------------------------


def test_volume_spike_true_when_burst() -> None:
    volumes = np.concatenate([np.full(20, 1000.0), np.array([5000.0])])
    closes = np.full(21, 100.0)
    assert _run("volume_spike[20,2]", closes, volumes) is True


def test_volume_spike_false_when_quiet() -> None:
    volumes = np.full(25, 1000.0)
    closes = np.full(25, 100.0)
    assert _run("volume_spike[20,2]", closes, volumes) is False


# ----- AND combination -----------------------------------------------------


def test_and_combination_all_must_match() -> None:
    # Set up so above_sma[50] is true but rsi_oversold[1] is false.
    closes = np.concatenate([np.full(50, 100.0), np.linspace(100, 200, 20)])
    assert _run("above_sma[50]", closes) is True
    assert _run("rsi_oversold[1]", closes) is False
    # AND requires both → False.
    assert _run("above_sma[50],rsi_oversold[1]", closes) is False
