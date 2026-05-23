"""Tests for ExecutionModel abstraction."""

from datetime import datetime, timedelta

from quant_researcher.engine.core.bar_data import Bar, BarData
from quant_researcher.engine.core.event import Direction, OrderEvent, OrderType
from quant_researcher.engine.execution.execution_model import (
    ImmediateExecution,
    TWAPExecution,
    VWAPExecution,
)
from quant_researcher.engine.portfolio.portfolio import Portfolio


def _make_bar_data(symbol: str = "X") -> BarData:
    bd = BarData()
    bd.add_symbol_bars(symbol, [Bar(
        symbol=symbol, timestamp=datetime(2024, 1, 1),
        open=100, high=101, low=99, close=100, volume=1_000_000,
    )])
    bd.advance(symbol)
    return bd


def _order(symbol="X", qty=100, direction=Direction.LONG) -> OrderEvent:
    return OrderEvent(symbol=symbol, direction=direction, quantity=qty)


class TestImmediateExecution:
    def test_passthrough(self):
        model = ImmediateExecution()
        order = _order(qty=500)
        result = model.execute(order, Portfolio(), _make_bar_data())
        assert len(result) == 1
        assert result[0] is order


class TestTWAPExecution:
    def test_splits_evenly(self):
        model = TWAPExecution(n_slices=5)
        order = _order(qty=100)
        result = model.execute(order, Portfolio(), _make_bar_data())
        assert len(result) == 5
        total = sum(o.quantity for o in result)
        assert total == 100
        assert all(o.quantity == 20 for o in result)

    def test_splits_with_remainder(self):
        model = TWAPExecution(n_slices=3)
        order = _order(qty=10)
        result = model.execute(order, Portfolio(), _make_bar_data())
        assert len(result) == 3
        total = sum(o.quantity for o in result)
        assert total == 10
        # First slice gets extra: 4, 3, 3
        assert result[0].quantity == 4
        assert result[1].quantity == 3
        assert result[2].quantity == 3

    def test_small_order_no_split(self):
        model = TWAPExecution(n_slices=5)
        order = _order(qty=3)
        result = model.execute(order, Portfolio(), _make_bar_data())
        assert len(result) == 1
        assert result[0] is order

    def test_preserves_order_attributes(self):
        model = TWAPExecution(n_slices=2)
        order = OrderEvent(
            symbol="AAPL", direction=Direction.SHORT, quantity=200,
            order_type=OrderType.LIMIT, limit_price=150.0,
        )
        result = model.execute(order, Portfolio(), _make_bar_data("AAPL"))
        for o in result:
            assert o.symbol == "AAPL"
            assert o.direction == Direction.SHORT
            assert o.order_type == OrderType.LIMIT
            assert o.limit_price == 150.0
            assert o.quantity == 100

    def test_n_slices_minimum_one(self):
        model = TWAPExecution(n_slices=0)
        assert model.n_slices == 1


class TestVWAPExecution:
    def test_splits_like_twap_on_daily(self):
        model = VWAPExecution(n_slices=4)
        order = _order(qty=100)
        result = model.execute(order, Portfolio(), _make_bar_data())
        assert len(result) == 4
        assert sum(o.quantity for o in result) == 100

    def test_small_order_passthrough(self):
        model = VWAPExecution(n_slices=5)
        order = _order(qty=2)
        result = model.execute(order, Portfolio(), _make_bar_data())
        assert len(result) == 1


class TestEngineIntegration:
    def test_engine_with_twap(self):
        from quant_researcher.engine.data.data_feed import DataFeed
        from quant_researcher.engine.engine import BacktestEngine
        from quant_researcher.engine.strategy.base import BaseStrategy

        class DummyFeed(DataFeed):
            def fetch(self, symbol, start, end):
                dt = datetime(2024, 1, 1)
                return [
                    Bar(symbol=symbol, timestamp=dt + timedelta(days=i),
                        open=100, high=101, low=99, close=100, volume=1000)
                    for i in range(10)
                ]

        class BuyOnce(BaseStrategy):
            def on_bar(self):
                if self.get_position("X") == 0:
                    self.buy("X", 100)

        engine = BacktestEngine(
            strategy=BuyOnce(),
            data_feed=DummyFeed(),
            symbols=["X"],
            start="2024-01-01",
            end="2024-01-15",
            initial_cash=100_000,
            execution_model=TWAPExecution(n_slices=5),
        )
        portfolio = engine.run()
        # TWAP splits 100 into 5x20, all submitted same bar, filled next bar
        assert portfolio.get_position_quantity("X") == 100
