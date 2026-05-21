"""Technical-screen predicate DSL.

Users pass a comma-separated list of named predicates to `qr screen run
--technical`, e.g.::

    above_sma[200],macd_golden_cross[5]
    rsi_oversold[3],near_52w_low[5]
    volume_spike[20,2]

Each token is `name` or `name[arg1, arg2, …]` where args are int / float.
All predicates are joined with AND — v1 doesn't support OR/NOT in the
technical DSL (keep MB scope tight; fundamentals do that via the AST
parser).

Registry below; each factory consumes its args and returns a callable
`(closes: np.ndarray, volumes: np.ndarray) -> bool`. Missing data
(insufficient bars, all-NaN indicator output) is treated as `False` — the
symbol is excluded from the screen.

Predicate vocabulary:
* `above_sma[N]` / `below_sma[N]` — latest close vs SMA(N). N typically 20/50/100/200.
* `rsi_oversold[N=1]` — RSI(14) < 30 anywhere in the last N trading days.
* `rsi_overbought[N=1]` — RSI(14) > 70 anywhere in the last N days.
* `macd_golden_cross[N=5]` — MACD line crossed above signal within last N days.
* `macd_death_cross[N=5]` — MACD line crossed below signal within last N days.
* `near_52w_high[pct=5]` — latest close >= 52w high * (1 - pct/100).
* `near_52w_low[pct=5]` — latest close <= 52w low * (1 + pct/100).
* `volume_spike[N=20, mult=2]` — latest volume > N-day avg * mult.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import numpy as np

from quant_researcher.screen.indicators import (
    macd as macd_fn,
)
from quant_researcher.screen.indicators import (
    rolling_max,
    rolling_min,
    rsi,
    sma,
)


class TechnicalError(ValueError):
    """Raised on malformed predicate syntax or unknown predicate name."""


Predicate = Callable[[np.ndarray, np.ndarray], bool]


def parse_technical(spec: str) -> list[Predicate]:
    """Parse a comma-separated DSL string → list of predicate callables.

    Each predicate's `(closes, volumes)` must all return True for a symbol
    to pass the technical screen. Bracket-grouped args may contain commas
    (e.g. `volume_spike[20,2]`); the parser respects `[…]` depth.
    """
    if not spec or not spec.strip():
        raise TechnicalError("empty technical spec")
    out: list[Predicate] = []
    depth = 0
    buf: list[str] = []
    for ch in spec:
        if ch == "[":
            depth += 1
            buf.append(ch)
        elif ch == "]":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            token = "".join(buf).strip()
            if token:
                out.append(_compile_token(token))
            buf = []
        else:
            buf.append(ch)
    if depth != 0:
        raise TechnicalError(f"unbalanced brackets in spec: {spec!r}")
    tail = "".join(buf).strip()
    if tail:
        out.append(_compile_token(tail))
    return out


def _compile_token(token: str) -> Predicate:
    name, args = _split_token(token)
    if name not in _REGISTRY:
        raise TechnicalError(
            f"unknown predicate: {name!r} (valid: {', '.join(sorted(_REGISTRY))})"
        )
    factory = _REGISTRY[name]
    try:
        return factory(*args)
    except TypeError as exc:
        raise TechnicalError(f"bad args for {name}: {exc}") from exc


_TOKEN_RE = re.compile(r"^([a-z][a-z0-9_]*)(?:\[([^\]]*)\])?$")


def _split_token(token: str) -> tuple[str, list[float | int]]:
    m = _TOKEN_RE.match(token)
    if not m:
        raise TechnicalError(f"malformed predicate: {token!r}")
    name = m.group(1)
    raw_args = m.group(2)
    if not raw_args:
        return name, []
    parsed: list[float | int] = []
    for piece in raw_args.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            parsed.append(int(piece) if "." not in piece else float(piece))
        except ValueError as exc:
            raise TechnicalError(
                f"non-numeric arg in {token!r}: {piece!r}"
            ) from exc
    return name, parsed


# --- Predicate factories ---------------------------------------------------


def _above_sma(n: int) -> Predicate:
    def _check(closes: np.ndarray, _volumes: np.ndarray) -> bool:
        if len(closes) < n:
            return False
        s = sma(closes, n)
        if np.isnan(s[-1]):
            return False
        return bool(closes[-1] > s[-1])

    return _check


def _below_sma(n: int) -> Predicate:
    def _check(closes: np.ndarray, _volumes: np.ndarray) -> bool:
        if len(closes) < n:
            return False
        s = sma(closes, n)
        if np.isnan(s[-1]):
            return False
        return bool(closes[-1] < s[-1])

    return _check


def _rsi_oversold(n_window: int = 1, threshold: float = 30.0) -> Predicate:
    def _check(closes: np.ndarray, _volumes: np.ndarray) -> bool:
        r = rsi(closes, n=14)
        recent = r[-max(n_window, 1) :]
        valid = recent[~np.isnan(recent)]
        if len(valid) == 0:
            return False
        return bool((valid < threshold).any())

    return _check


def _rsi_overbought(n_window: int = 1, threshold: float = 70.0) -> Predicate:
    def _check(closes: np.ndarray, _volumes: np.ndarray) -> bool:
        r = rsi(closes, n=14)
        recent = r[-max(n_window, 1) :]
        valid = recent[~np.isnan(recent)]
        if len(valid) == 0:
            return False
        return bool((valid > threshold).any())

    return _check


def _macd_golden_cross(n_window: int = 5) -> Predicate:
    def _check(closes: np.ndarray, _volumes: np.ndarray) -> bool:
        m, s, _ = macd_fn(closes)
        return _crossed(m, s, n_window, ascending=True)

    return _check


def _macd_death_cross(n_window: int = 5) -> Predicate:
    def _check(closes: np.ndarray, _volumes: np.ndarray) -> bool:
        m, s, _ = macd_fn(closes)
        return _crossed(m, s, n_window, ascending=False)

    return _check


def _crossed(
    a: np.ndarray, b: np.ndarray, n_window: int, *, ascending: bool
) -> bool:
    """True if `a` crossed `b` (ascending=True: a was ≤ b then a > b) within
    the last `n_window` bars."""
    if len(a) < 2 or n_window <= 0:
        return False
    start = max(1, len(a) - n_window)
    for i in range(start, len(a)):
        if np.isnan(a[i]) or np.isnan(b[i]) or np.isnan(a[i - 1]) or np.isnan(b[i - 1]):
            continue
        if ascending and a[i - 1] <= b[i - 1] and a[i] > b[i]:
            return True
        if not ascending and a[i - 1] >= b[i - 1] and a[i] < b[i]:
            return True
    return False


def _near_52w_high(pct: float = 5.0) -> Predicate:
    n = 252  # trading days in ~1 year

    def _check(closes: np.ndarray, _volumes: np.ndarray) -> bool:
        if len(closes) < n:
            return False
        highs = rolling_max(closes, n)
        if np.isnan(highs[-1]):
            return False
        return bool(closes[-1] >= highs[-1] * (1 - pct / 100.0))

    return _check


def _near_52w_low(pct: float = 5.0) -> Predicate:
    n = 252

    def _check(closes: np.ndarray, _volumes: np.ndarray) -> bool:
        if len(closes) < n:
            return False
        lows = rolling_min(closes, n)
        if np.isnan(lows[-1]):
            return False
        return bool(closes[-1] <= lows[-1] * (1 + pct / 100.0))

    return _check


def _volume_spike(n: int = 20, mult: float = 2.0) -> Predicate:
    def _check(_closes: np.ndarray, volumes: np.ndarray) -> bool:
        if len(volumes) < n + 1:
            return False
        avg = sma(volumes.astype(float), n)
        if np.isnan(avg[-1]):
            return False
        return bool(volumes[-1] > avg[-1] * mult)

    return _check


# --- Registry --------------------------------------------------------------


_REGISTRY: dict[str, Callable[..., Predicate]] = {
    "above_sma": _above_sma,
    "below_sma": _below_sma,
    "rsi_oversold": _rsi_oversold,
    "rsi_overbought": _rsi_overbought,
    "macd_golden_cross": _macd_golden_cross,
    "macd_death_cross": _macd_death_cross,
    "near_52w_high": _near_52w_high,
    "near_52w_low": _near_52w_low,
    "volume_spike": _volume_spike,
}


def available_predicates() -> list[str]:
    return sorted(_REGISTRY)
