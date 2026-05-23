"""BaseStrategy 和 SMA 策略测试。"""


from quant_researcher.engine.core.event import Direction, FillEvent
from quant_researcher.engine.portfolio.portfolio import Portfolio
from quant_researcher.engine.strategy.base import BaseStrategy
from tests.engine.helpers import advance_all, make_bar_data

# ── 用于测试的简单策略 ──

class AlwaysBuyStrategy(BaseStrategy):
    """每根 bar 都买 10 股。"""
    def __init__(self, symbol: str):
        super().__init__()
        self.symbol = symbol

    def on_bar(self) -> None:
        self.buy(self.symbol, 10)


class BuyOnceThenSellStrategy(BaseStrategy):
    """第一根 bar 买入，第二根 bar 卖出。"""
    def __init__(self, symbol: str):
        super().__init__()
        self.symbol = symbol
        self._bar_count = 0

    def on_bar(self) -> None:
        self._bar_count += 1
        if self._bar_count == 1:
            self.buy(self.symbol, 100)
        elif self._bar_count == 2:
            self.sell(self.symbol, 100)


class DoNothingStrategy(BaseStrategy):
    def on_bar(self) -> None:
        pass


# ── 测试 ──

class TestBaseStrategyBind:
    def test_bind_sets_bar_data_and_portfolio(self):
        s = DoNothingStrategy()
        bd = make_bar_data("X")
        p = Portfolio()
        s._bind(bd, p)
        assert s.bar_data is bd
        assert s.portfolio is p

    def test_bar_data_asserts_before_bind(self):
        s = DoNothingStrategy()
        try:
            _ = s.bar_data
            raise AssertionError("Should assert")
        except AssertionError:
            pass


class TestBaseStrategyOrders:
    def test_buy_creates_order(self):
        s = AlwaysBuyStrategy("X")
        s._bind(make_bar_data("X"), Portfolio())
        advance_all(s.bar_data, 1)

        s.on_bar()
        orders = s._collect_orders()

        assert len(orders) == 1
        assert orders[0].direction == Direction.LONG
        assert orders[0].quantity == 10

    def test_collect_clears_pending(self):
        s = AlwaysBuyStrategy("X")
        s._bind(make_bar_data("X"), Portfolio())
        advance_all(s.bar_data, 1)

        s.on_bar()
        s._collect_orders()
        assert s._collect_orders() == []  # 第二次为空

    def test_multiple_orders_per_bar(self):
        """一个 bar 内多次调用 buy/sell 应都保留。"""
        s = DoNothingStrategy()
        s._bind(make_bar_data("X"), Portfolio())
        s.buy("X", 50)
        s.sell("X", 30)
        orders = s._collect_orders()
        assert len(orders) == 2

    def test_sell_creates_short_order(self):
        s = BuyOnceThenSellStrategy("X")
        bd = make_bar_data("X", [100.0, 110.0])
        s._bind(bd, Portfolio())
        bd.advance("X")
        s.on_bar()  # bar 1: buy
        s._collect_orders()
        bd.advance("X")
        s.on_bar()  # bar 2: sell
        orders = s._collect_orders()
        assert len(orders) == 1
        assert orders[0].direction == Direction.SHORT
        assert orders[0].quantity == 100


class TestBaseStrategyGetPosition:
    def test_zero_before_trade(self):
        s = DoNothingStrategy()
        s._bind(make_bar_data("X"), Portfolio())
        assert s.get_position("X") == 0

    def test_reflects_portfolio(self):
        s = DoNothingStrategy()
        p = Portfolio()
        s._bind(make_bar_data("X"), p)
        p.on_fill(FillEvent(symbol="X", direction=Direction.LONG, quantity=100, fill_price=50.0))
        assert s.get_position("X") == 100


class TestBaseStrategyOnFill:
    def test_on_fill_called(self):
        """验证 on_fill 可被覆盖。"""
        fills_received = []

        class TrackingStrategy(BaseStrategy):
            def on_bar(self) -> None:
                pass
            def on_fill(self, fill: FillEvent) -> None:
                fills_received.append(fill)

        s = TrackingStrategy()
        fill = FillEvent(symbol="X", direction=Direction.LONG, quantity=10, fill_price=100.0)
        s.on_fill(fill)
        assert len(fills_received) == 1
        assert fills_received[0].symbol == "X"
