"""Built-in strategy registry — each strategy runs end-to-end via run_backtest."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from quant_researcher.backtest.runner import run_backtest
from quant_researcher.backtest.strategies import REGISTRY
from quant_researcher.db import Base
from quant_researcher.models.prices import DailyPrice


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        # 200 bars of sine + drift so crossovers/breakouts/reversions all trigger
        d0 = date(2023, 1, 2)
        for i in range(200):
            px = 100 + 20 * math.sin(i / 12.0) + i * 0.12
            sess.add(
                DailyPrice(
                    symbol="AAPL",
                    trade_date=d0 + timedelta(days=i),
                    open=px,
                    high=px * 1.02,
                    low=px * 0.98,
                    close=px,
                    adj_close=px,
                    volume=1_000_000,
                )
            )
        sess.commit()
        yield sess


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_registry_strategy_runs(session: Session, name: str) -> None:
    summary = run_backtest(
        session,
        strategy=name,
        symbols=["AAPL"],
        start="2023-01-02",
        end="2023-12-31",
        fee="zero",
    )
    session.commit()
    assert summary["strategy"] == name
    assert summary["n_equity_points"] > 0
    assert "sharpe_ratio" in summary["metrics"]
    assert summary["n_trades"] >= 0
