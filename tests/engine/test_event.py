"""事件系统测试。"""

from quant_researcher.engine.core.event import (
    Direction,
    EventType,
    FillEvent,
    MarketEvent,
    OrderEvent,
    OrderType,
    SignalEvent,
)


class TestMarketEvent:
    def test_type(self):
        e = MarketEvent()
        assert e.type == EventType.MARKET

    def test_frozen(self):
        e = MarketEvent()
        try:
            e.type = EventType.SIGNAL  # type: ignore
            raise AssertionError("Should be frozen")
        except AttributeError:
            pass


class TestSignalEvent:
    def test_fields(self):
        s = SignalEvent(symbol="AAPL", direction=Direction.LONG, strength=0.8)
        assert s.symbol == "AAPL"
        assert s.direction == Direction.LONG
        assert s.strength == 0.8
        assert s.type == EventType.SIGNAL

    def test_default_strength(self):
        s = SignalEvent(symbol="AAPL", direction=Direction.SHORT)
        assert s.strength == 1.0


class TestOrderEvent:
    def test_market_order(self):
        o = OrderEvent(symbol="AAPL", direction=Direction.LONG, quantity=100)
        assert o.order_type == OrderType.MARKET
        assert o.limit_price is None
        assert o.type == EventType.ORDER

    def test_limit_order(self):
        o = OrderEvent(
            symbol="AAPL", direction=Direction.LONG, quantity=50,
            order_type=OrderType.LIMIT, limit_price=150.0,
        )
        assert o.order_type == OrderType.LIMIT
        assert o.limit_price == 150.0


class TestFillEvent:
    def test_cost(self):
        f = FillEvent(
            symbol="AAPL", direction=Direction.LONG,
            quantity=100, fill_price=150.0, commission=15.0,
        )
        assert f.cost == 150.0 * 100 + 15.0

    def test_cost_no_commission(self):
        f = FillEvent(
            symbol="AAPL", direction=Direction.LONG,
            quantity=50, fill_price=200.0,
        )
        assert f.cost == 200.0 * 50

    def test_type(self):
        f = FillEvent(
            symbol="AAPL", direction=Direction.SHORT,
            quantity=10, fill_price=100.0,
        )
        assert f.type == EventType.FILL


class TestDirection:
    def test_values(self):
        assert Direction.LONG.value == 1
        assert Direction.SHORT.value == -1
