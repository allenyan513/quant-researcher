"""TradeLog and enhanced metrics tests."""

from datetime import datetime, timedelta

import pytest

from quant_researcher.engine.analytics.metrics import TradeLog, calculate_metrics
from quant_researcher.engine.core.event import Direction, FillEvent
from quant_researcher.engine.portfolio.portfolio import Portfolio


def _fill(symbol, direction, qty, price, commission=0.0, day_offset=0):
    return FillEvent(
        symbol=symbol,
        direction=direction,
        quantity=qty,
        fill_price=price,
        commission=commission,
        timestamp=datetime(2024, 1, 1) + timedelta(days=day_offset),
    )


class TestTradeLogBasic:
    def test_open_and_close(self):
        tl = TradeLog()
        tl.on_fill(_fill("X", Direction.LONG, 100, 50.0, day_offset=0))
        tl.on_fill(_fill("X", Direction.SHORT, 100, 55.0, day_offset=5))

        assert len(tl.trades) == 1
        trade = tl.trades[0]
        assert trade.symbol == "X"
        assert trade.direction == Direction.LONG
        assert trade.entry_price == 50.0
        assert trade.exit_price == 55.0
        assert trade.pnl == pytest.approx(500.0)  # 100 * (55 - 50)
        assert trade.holding_days == 5

    def test_losing_trade(self):
        tl = TradeLog()
        tl.on_fill(_fill("X", Direction.LONG, 100, 50.0))
        tl.on_fill(_fill("X", Direction.SHORT, 100, 45.0, day_offset=3))

        assert tl.trades[0].pnl == pytest.approx(-500.0)

    def test_add_to_position(self):
        tl = TradeLog()
        tl.on_fill(_fill("X", Direction.LONG, 100, 50.0))
        tl.on_fill(_fill("X", Direction.LONG, 100, 60.0))  # add

        assert len(tl.trades) == 0  # still open
        assert tl._open_trades["X"].quantity == 200
        assert tl._open_trades["X"].entry_price == pytest.approx(55.0)  # avg

    def test_partial_close(self):
        tl = TradeLog()
        tl.on_fill(_fill("X", Direction.LONG, 200, 50.0))
        tl.on_fill(_fill("X", Direction.SHORT, 100, 55.0, day_offset=5))

        assert len(tl.trades) == 1
        assert tl.trades[0].pnl == pytest.approx(500.0)  # 100 * 5
        # Remaining position
        assert "X" in tl._open_trades
        assert tl._open_trades["X"].quantity == 100

    def test_reverse_position(self):
        tl = TradeLog()
        tl.on_fill(_fill("X", Direction.LONG, 100, 50.0))
        tl.on_fill(_fill("X", Direction.SHORT, 150, 55.0, day_offset=3))

        # Close 100 long, open 50 short
        assert len(tl.trades) == 1
        assert tl.trades[0].pnl == pytest.approx(500.0)
        assert tl._open_trades["X"].direction == Direction.SHORT
        assert tl._open_trades["X"].quantity == 50


class TestTradeLogSummary:
    def test_summary_with_trades(self):
        tl = TradeLog()
        # Winner: +500
        tl.on_fill(_fill("X", Direction.LONG, 100, 50.0))
        tl.on_fill(_fill("X", Direction.SHORT, 100, 55.0, day_offset=5))
        # Loser: -300
        tl.on_fill(_fill("X", Direction.LONG, 100, 60.0, day_offset=6))
        tl.on_fill(_fill("X", Direction.SHORT, 100, 57.0, day_offset=10))

        s = tl.summary()
        assert s["total_trades"] == 2
        assert s["winning_trades"] == 1
        assert s["losing_trades"] == 1
        assert s["win_rate"] == pytest.approx(0.5)
        assert s["total_pnl"] == pytest.approx(200.0)

    def test_empty_summary(self):
        tl = TradeLog()
        s = tl.summary()
        assert s["total_trades"] == 0


class TestTradeReturnPct:
    def test_long_return(self):
        tl = TradeLog()
        tl.on_fill(_fill("X", Direction.LONG, 100, 100.0))
        tl.on_fill(_fill("X", Direction.SHORT, 100, 110.0, day_offset=5))
        assert tl.trades[0].return_pct == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# Enhanced metrics tests
# ---------------------------------------------------------------------------

def _build_portfolio_with_curve(equities):
    p = Portfolio(initial_cash=equities[0])
    start = datetime(2024, 1, 1)
    for i, eq in enumerate(equities):
        p.equity_curve.append((start + timedelta(days=i), eq))
    return p


class TestSortinoRatio:
    def test_positive_sortino(self):
        # Need some down days for downside deviation to be non-zero
        m = calculate_metrics(_build_portfolio_with_curve([100, 102, 99, 104, 101, 106]))
        assert m["sortino_ratio"] > 0

    def test_sortino_with_drawdown(self):
        m = calculate_metrics(_build_portfolio_with_curve([100, 105, 95, 110, 108]))
        assert "sortino_ratio" in m


class TestCalmarRatio:
    def test_calmar_ratio(self):
        m = calculate_metrics(_build_portfolio_with_curve([100, 110, 105, 120]))
        assert m["calmar_ratio"] > 0

    def test_calmar_no_drawdown(self):
        m = calculate_metrics(_build_portfolio_with_curve([100, 110, 120, 130]))
        assert m["calmar_ratio"] == 0.0  # max_dd = 0


class TestBenchmarkComparison:
    def test_with_benchmark(self):
        start = datetime(2024, 1, 1)
        portfolio = _build_portfolio_with_curve([100, 105, 110, 115])
        benchmark = [(start + timedelta(days=i), v) for i, v in enumerate([100, 102, 104, 106])]

        m = calculate_metrics(portfolio, benchmark_curve=benchmark)

        assert "benchmark_return" in m
        assert "alpha" in m
        assert "beta" in m
        assert "information_ratio" in m
        assert m["alpha"] > 0  # strategy beat benchmark

    def test_without_benchmark(self):
        m = calculate_metrics(_build_portfolio_with_curve([100, 105, 110]))
        assert "benchmark_return" not in m


class TestVolatility:
    def test_volatility_present(self):
        m = calculate_metrics(_build_portfolio_with_curve([100, 101, 99, 102, 98]))
        assert "volatility" in m
        assert m["volatility"] > 0
