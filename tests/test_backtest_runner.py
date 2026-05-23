"""run_backtest — end-to-end orchestration, persistence, serialization."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from quant_researcher.backtest.runner import _to_jsonable, run_backtest
from quant_researcher.db import Base
from quant_researcher.models.backtest import BacktestRun
from quant_researcher.models.prices import DailyPrice


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        yield sess


def _seed_series(
    session: Session, symbol: str = "AAPL", n: int = 160, start: date = date(2023, 1, 2)
) -> None:
    """Sine + drift so SMA fast/slow actually cross during the window."""
    for i in range(n):
        px = 100 + 20 * math.sin(i / 12.0) + i * 0.15
        session.add(
            DailyPrice(
                symbol=symbol,
                trade_date=start + timedelta(days=i),
                open=px,
                high=px * 1.01,
                low=px * 0.99,
                close=px,
                adj_close=px,
                volume=1_000_000,
            )
        )
    session.commit()


def test_run_executes_and_persists(session: Session) -> None:
    _seed_series(session)
    summary = run_backtest(
        session,
        strategy="sma_crossover",
        symbols=["AAPL"],
        start="2023-01-02",
        end="2023-07-01",
        initial_cash=100_000,
        params={"fast_period": 5, "slow_period": 20},
        fee="zero",
    )
    session.commit()

    assert summary["n_trades"] >= 1
    assert "sharpe_ratio" in summary["metrics"]
    assert summary["n_equity_points"] > 0

    row = session.get(BacktestRun, summary["run_id"])
    assert row is not None
    assert row.strategy == "sma_crossover"
    assert row.symbols == ["AAPL"]
    assert len(row.equity_curve) == summary["n_equity_points"]
    # the whole snapshot must be JSON-serializable (no numpy / inf leaked)
    json.dumps(row.metrics)
    json.dumps(row.equity_curve)
    json.dumps(row.trade_log)


def test_persist_false_skips_db(session: Session) -> None:
    _seed_series(session)
    summary = run_backtest(
        session,
        strategy="sma_crossover",
        symbols=["AAPL"],
        start="2023-01-02",
        end="2023-07-01",
        params={"fast_period": 5, "slow_period": 20},
        fee="zero",
        persist=False,
    )
    session.commit()
    assert session.get(BacktestRun, summary["run_id"]) is None


def test_unknown_strategy_raises(session: Session) -> None:
    with pytest.raises(KeyError):
        run_backtest(
            session,
            strategy="does_not_exist",
            symbols=["AAPL"],
            start="2023-01-02",
            end="2023-02-02",
        )


def test_injects_symbol_for_single_symbol_strategy(session: Session) -> None:
    # SMACrossover.__init__ requires `symbol`; runner injects symbols[0].
    _seed_series(session)
    summary = run_backtest(
        session,
        strategy="sma_crossover",
        symbols=["AAPL"],
        start="2023-01-02",
        end="2023-07-01",
        params={"fast_period": 5, "slow_period": 20},  # no `symbol` here
        fee="zero",
    )
    assert summary["strategy"] == "sma_crossover"


def test_strategy_file_is_loaded(session: Session, tmp_path) -> None:
    _seed_series(session)
    strat = tmp_path / "always_buy.py"
    strat.write_text(
        "from quant_researcher.engine.strategy.base import BaseStrategy\n"
        "class AlwaysBuy(BaseStrategy):\n"
        "    def __init__(self, symbol):\n"
        "        super().__init__()\n"
        "        self.symbol = symbol\n"
        "        self._done = False\n"
        "    def on_bar(self):\n"
        "        if not self._done and self.bar_data.has_enough_bars(self.symbol, 1):\n"
        "            self.buy(self.symbol, 10)\n"
        "            self._done = True\n"
    )
    summary = run_backtest(
        session,
        strategy="",
        symbols=["AAPL"],
        start="2023-01-02",
        end="2023-07-01",
        strategy_file=str(strat),
        fee="zero",
    )
    assert summary["strategy"] == "AlwaysBuy"
    assert summary["n_trades"] >= 0  # it ran without error


def test_benchmark_symbol_adds_relative_metrics(session: Session) -> None:
    _seed_series(session, "AAPL")
    _seed_series(session, "SPY")
    summary = run_backtest(
        session,
        strategy="sma_crossover",
        symbols=["AAPL"],
        start="2023-01-02",
        end="2023-07-01",
        params={"fast_period": 5, "slow_period": 20},
        fee="zero",
        benchmark_symbol="SPY",
    )
    assert summary["benchmark_symbol"] == "SPY"
    assert any(k in summary["metrics"] for k in ("alpha", "beta", "benchmark_return"))


def test_bad_fee_model_raises(session: Session) -> None:
    _seed_series(session)
    with pytest.raises(ValueError, match="fee model"):
        run_backtest(
            session,
            strategy="sma_crossover",
            symbols=["AAPL"],
            start="2023-01-02",
            end="2023-07-01",
            fee="nonsense",
        )


def test_to_jsonable_coerces_numpy_and_non_finite() -> None:
    out = _to_jsonable(
        {
            "a": np.float64(1.5),
            "b": float("inf"),
            "c": float("nan"),
            "d": [np.int64(3), np.float64(2.0)],
            "e": np.array([1.0, 2.0]),
        }
    )
    assert out == {"a": 1.5, "b": None, "c": None, "d": [3, 2.0], "e": [1.0, 2.0]}
    json.dumps(out)  # must not raise
