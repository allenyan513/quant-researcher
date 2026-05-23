"""Regression locks for the MH PR-review fixes (#3/#4/#5/#6).

These guard qr-side corrections to the verbatim-ported engine, so a future
re-sync from upstream that re-introduces the bugs trips a test here.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest
from scipy.stats import kurtosis, norm, skew

from quant_researcher.engine.analytics.metrics import calculate_metrics
from quant_researcher.engine.core.bar_data import Bar
from quant_researcher.engine.data.data_feed import DataFeed
from quant_researcher.engine.engine import BacktestEngine
from quant_researcher.engine.portfolio.portfolio import Portfolio
from quant_researcher.engine.strategy.base import BaseStrategy


def _portfolio_from_returns(rets: np.ndarray, initial: float = 100_000.0) -> Portfolio:
    p = Portfolio(initial_cash=initial)
    eq = initial
    t = datetime(2023, 1, 1)
    curve = [(t, eq)]
    for i, r in enumerate(rets, start=1):
        eq *= 1 + r
        curve.append((t + timedelta(days=i), eq))
    p.equity_curve = curve
    return p


# ----- #5: PSR must use Pearson kurtosis (excess_kurt + 2), not excess --------


def test_psr_uses_pearson_kurtosis_not_excess() -> None:
    # Deterministic, modest-Sharpe, fat-tailed series so PSR lands in the
    # CDF's sensitive zone (a high-Sharpe series saturates PSR→1.0 under both
    # formulas and wouldn't distinguish the fix).
    rets = np.array(
        [0.004, -0.004, 0.005, -0.005, 0.004, -0.005,
         0.004, -0.004, 0.05, -0.052, 0.003, -0.003]
    )
    p = _portfolio_from_returns(rets)
    m = calculate_metrics(p)

    eq = np.array([e for _, e in p.equity_curve])
    r = np.diff(eq) / eq[:-1]
    sharpe = m["sharpe_ratio"]
    n = len(r)
    sk = float(skew(r))
    ek = float(kurtosis(r, fisher=True))  # excess
    denom_correct = max(1e-10, (1 - sk * sharpe + (ek + 2) / 4 * sharpe**2)) ** 0.5
    expected = float(norm.cdf(sharpe * ((n - 1) ** 0.5) / denom_correct))
    denom_buggy = max(1e-10, (1 - sk * sharpe + ek / 4 * sharpe**2)) ** 0.5
    buggy = float(norm.cdf(sharpe * ((n - 1) ** 0.5) / denom_buggy))

    # this data must actually distinguish the two formulas (else the test is moot)
    assert abs(expected - buggy) > 1e-6
    assert m["psr"] == pytest.approx(expected, rel=1e-9)
    assert 0.0 <= m["psr"] <= 1.0


# ----- #3/#4: total_return + CAGR guards on degenerate equity ------------------


def test_cagr_handles_negative_equity_without_crash() -> None:
    p = Portfolio(initial_cash=100.0)
    p.equity_curve = [
        (datetime(2023, 1, 1), 100.0),
        (datetime(2023, 6, 1), -50.0),  # wiped out + flipped negative
    ]
    m = calculate_metrics(p)
    assert m["total_return"] == pytest.approx(-1.5)
    assert m["cagr"] == -1.0  # not a complex number / not a crash


# ----- #6: engine loop tolerates an empty symbol list -------------------------


class _NoopStrategy(BaseStrategy):
    def on_bar(self) -> None:
        pass


class _EmptyFeed(DataFeed):
    def fetch(self, symbol: str, start: str, end: str) -> list[Bar]:
        return []


def test_engine_run_handles_empty_symbols() -> None:
    engine = BacktestEngine(
        strategy=_NoopStrategy(),
        data_feed=_EmptyFeed(),
        symbols=[],
        start="2023-01-01",
        end="2023-02-01",
        verbose=False,
    )
    portfolio = engine.run()  # must not raise ValueError from max([])
    assert portfolio is not None
    assert portfolio.equity == 100_000.0
