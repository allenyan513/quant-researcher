"""Numpy-based technical indicators used by `qr screen run --technical …`.

All indicators take a 1D `np.ndarray` of `float` (closes, or volumes for
`sma`/`ema` on volume) and return an array of the same length. Slots before
the window is full are filled with `np.nan` so callers can drop them.

Conventions:
* SMA = simple moving average (equal weights, window of `n`).
* EMA = exponential moving average with smoothing factor `2 / (n + 1)`,
  seeded by the SMA of the first `n` samples (standard).
* MACD = `ema(close, fast) - ema(close, slow)`, signal = `ema(macd, signal)`,
  histogram = `macd - signal`. Standard parameters (12, 26, 9).
* RSI = Wilder's smoothing on gains/losses, period `n`. Returns 0-100.

These are computed in pure numpy and don't depend on pandas / TA-Lib — MA-3
ships ~7 tickers, so vectorized 252-bar windows are trivially cheap.
"""

from __future__ import annotations

import numpy as np


def sma(arr: np.ndarray, n: int) -> np.ndarray:
    """Simple moving average. First `n - 1` slots are NaN."""
    if n <= 0:
        raise ValueError(f"sma window must be > 0, got {n}")
    a = np.asarray(arr, dtype=float)
    out = np.full(a.shape, np.nan)
    if len(a) < n:
        return out
    csum = np.cumsum(np.insert(a, 0, 0.0))
    out[n - 1 :] = (csum[n:] - csum[:-n]) / n
    return out


def ema(arr: np.ndarray, n: int) -> np.ndarray:
    """Exponential moving average, seeded by the SMA of the first `n` bars."""
    if n <= 0:
        raise ValueError(f"ema window must be > 0, got {n}")
    a = np.asarray(arr, dtype=float)
    out = np.full(a.shape, np.nan)
    if len(a) < n:
        return out
    alpha = 2.0 / (n + 1)
    # Seed: SMA of the first n values, placed at index n - 1.
    out[n - 1] = float(np.mean(a[:n]))
    for i in range(n, len(a)):
        out[i] = alpha * a[i] + (1 - alpha) * out[i - 1]
    return out


def macd(
    arr: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return `(macd_line, signal_line, histogram)`. Standard 12/26/9."""
    if fast >= slow:
        raise ValueError(f"macd fast ({fast}) must be < slow ({slow})")
    a = np.asarray(arr, dtype=float)
    fast_ema = ema(a, fast)
    slow_ema = ema(a, slow)
    macd_line = fast_ema - slow_ema
    # Signal EMA needs to skip the leading NaNs to compute properly.
    valid = ~np.isnan(macd_line)
    sig = np.full(a.shape, np.nan)
    if valid.sum() >= signal:
        first_valid = int(np.argmax(valid))
        sig_input = macd_line[first_valid:]
        sig[first_valid:] = ema(sig_input, signal)
    hist = macd_line - sig
    return macd_line, sig, hist


def rsi(arr: np.ndarray, n: int = 14) -> np.ndarray:
    """Wilder's RSI. First `n` slots are NaN."""
    if n <= 0:
        raise ValueError(f"rsi window must be > 0, got {n}")
    a = np.asarray(arr, dtype=float)
    out = np.full(a.shape, np.nan)
    if len(a) <= n:
        return out
    deltas = np.diff(a)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    # Seed: simple average of the first n gains/losses (positions 1..n in `a`).
    avg_gain = float(np.mean(gains[:n]))
    avg_loss = float(np.mean(losses[:n]))
    out[n] = _rsi_value(avg_gain, avg_loss)
    for i in range(n + 1, len(a)):
        avg_gain = (avg_gain * (n - 1) + gains[i - 1]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i - 1]) / n
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def rolling_max(arr: np.ndarray, n: int) -> np.ndarray:
    """Rolling maximum over a trailing window of `n`. First `n-1` slots NaN."""
    if n <= 0:
        raise ValueError(f"rolling_max window must be > 0, got {n}")
    a = np.asarray(arr, dtype=float)
    out = np.full(a.shape, np.nan)
    for i in range(n - 1, len(a)):
        out[i] = float(np.max(a[i - n + 1 : i + 1]))
    return out


def rolling_min(arr: np.ndarray, n: int) -> np.ndarray:
    """Rolling minimum over a trailing window of `n`. First `n-1` slots NaN."""
    if n <= 0:
        raise ValueError(f"rolling_min window must be > 0, got {n}")
    a = np.asarray(arr, dtype=float)
    out = np.full(a.shape, np.nan)
    for i in range(n - 1, len(a)):
        out[i] = float(np.min(a[i - n + 1 : i + 1]))
    return out
