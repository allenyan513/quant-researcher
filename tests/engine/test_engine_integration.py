"""
集成测试 — 端到端验证整个引擎事件循环。

使用 MockDataFeed + 简单策略，不依赖网络。
"""


import pytest

from quant_researcher.engine.core.event import Direction, FillEvent
from quant_researcher.engine.engine import BacktestEngine
from quant_researcher.engine.strategy.base import BaseStrategy
from tests.engine.helpers import MockDataFeed, make_bars

# ── 测试策略 ──

class BuyAndHoldStrategy(BaseStrategy):
    """第一根 bar 全仓买入，之后不动。"""
    def __init__(self, symbol: str, size: int = 100):
        super().__init__()
        self.symbol = symbol
        self.size = size
        self._bought = False

    def on_bar(self) -> None:
        if not self._bought:
            self.buy(self.symbol, self.size)
            self._bought = True


class BuyThenSellStrategy(BaseStrategy):
    """第一根 bar 买入，第三根 bar 卖出。"""
    def __init__(self, symbol: str, size: int = 100):
        super().__init__()
        self.symbol = symbol
        self.size = size
        self._bar_count = 0

    def on_bar(self) -> None:
        self._bar_count += 1
        if self._bar_count == 1:
            self.buy(self.symbol, self.size)
        elif self._bar_count == 3:
            pos = self.get_position(self.symbol)
            if pos > 0:
                self.sell(self.symbol, pos)


class NeverTradeStrategy(BaseStrategy):
    """什么都不做。"""
    def on_bar(self) -> None:
        pass


class FillTrackingStrategy(BaseStrategy):
    """追踪所有成交回报。"""
    def __init__(self, symbol: str, size: int = 100):
        super().__init__()
        self.symbol = symbol
        self.size = size
        self.fills_received: list[FillEvent] = []
        self._bought = False

    def on_bar(self) -> None:
        if not self._bought:
            self.buy(self.symbol, self.size)
            self._bought = True

    def on_fill(self, fill: FillEvent) -> None:
        self.fills_received.append(fill)


# ── 集成测试 ──

class TestEngineRunsToCompletion:
    def test_basic_run(self):
        """引擎能跑完并返回 Portfolio。"""
        prices = [100.0, 105.0, 110.0, 108.0, 115.0]
        feed = MockDataFeed({"X": make_bars("X", prices)})
        strategy = BuyAndHoldStrategy("X", size=100)

        engine = BacktestEngine(
            strategy=strategy, data_feed=feed,
            symbols=["X"], start="2024-01-01", end="2024-12-31",
            initial_cash=100_000.0, commission_rate=0.0, slippage_rate=0.0,
        )
        portfolio = engine.run()

        assert len(portfolio.equity_curve) == len(prices)
        assert portfolio.equity > 0

    def test_empty_strategy(self):
        """不交易时，净值应等于初始现金。"""
        prices = [100.0, 110.0, 120.0]
        feed = MockDataFeed({"X": make_bars("X", prices)})
        strategy = NeverTradeStrategy()

        engine = BacktestEngine(
            strategy=strategy, data_feed=feed,
            symbols=["X"], start="2024-01-01", end="2024-12-31",
            initial_cash=50_000.0,
        )
        portfolio = engine.run()

        for _, eq in portfolio.equity_curve:
            assert eq == pytest.approx(50_000.0)


class TestEngineBuyAndHold:
    def test_equity_tracks_price(self):
        """买入后净值应跟随股价变动。"""
        prices = [100.0, 110.0, 120.0, 130.0]
        feed = MockDataFeed({"X": make_bars("X", prices)})
        strategy = BuyAndHoldStrategy("X", size=100)

        engine = BacktestEngine(
            strategy=strategy, data_feed=feed,
            symbols=["X"], start="2024-01-01", end="2024-12-31",
            initial_cash=100_000.0, commission_rate=0.0, slippage_rate=0.0,
        )
        portfolio = engine.run()

        # bar 0: 买入订单提交（本 bar 不成交）
        # bar 1: 撮合买入 @ open=109.5, 持仓100股
        #         equity = (100000 - 109.5*100) + 100*110 = 100050
        # bar 2: equity = cash + 100*120
        # bar 3: equity = cash + 100*130
        assert portfolio.get_position_quantity("X") == 100
        final_eq = portfolio.equity
        assert final_eq > 100_000.0  # 价格上涨，应盈利


class TestEngineBuyThenSell:
    def test_round_trip(self):
        """完整买卖一轮后，应有已实现盈亏。"""
        # 价格: 100 → 110 → 120 → 130 → 140
        prices = [100.0, 110.0, 120.0, 130.0, 140.0]
        feed = MockDataFeed({"X": make_bars("X", prices)})
        strategy = BuyThenSellStrategy("X", size=100)

        engine = BacktestEngine(
            strategy=strategy, data_feed=feed,
            symbols=["X"], start="2024-01-01", end="2024-12-31",
            initial_cash=100_000.0, commission_rate=0.0, slippage_rate=0.0,
        )
        portfolio = engine.run()

        # bar 0: 策略发出买入
        # bar 1: 撮合买入 @ open=109.5
        # bar 2: 策略发出卖出 (bar_count=3)
        # bar 3: 撮合卖出 @ open=129.5
        # pnl = 100 * (129.5 - 109.5) = 2000
        assert portfolio.realized_pnl == pytest.approx(2000.0)
        assert portfolio.get_position_quantity("X") == 0


class TestEngineOnFillCallback:
    def test_strategy_receives_fills(self):
        """策略的 on_fill 应被调用。"""
        prices = [100.0, 110.0, 120.0]
        feed = MockDataFeed({"X": make_bars("X", prices)})
        strategy = FillTrackingStrategy("X", size=50)

        engine = BacktestEngine(
            strategy=strategy, data_feed=feed,
            symbols=["X"], start="2024-01-01", end="2024-12-31",
            initial_cash=100_000.0, commission_rate=0.0, slippage_rate=0.0,
        )
        engine.run()

        assert len(strategy.fills_received) == 1
        assert strategy.fills_received[0].quantity == 50
        assert strategy.fills_received[0].direction == Direction.LONG


class TestEngineCommissionAndSlippage:
    def test_commission_reduces_pnl(self):
        """手续费应减少利润。"""
        prices = [100.0, 110.0, 120.0, 130.0, 140.0]
        feed = MockDataFeed({"X": make_bars("X", prices)})

        # 无手续费
        s1 = BuyThenSellStrategy("X", size=100)
        e1 = BacktestEngine(
            strategy=s1, data_feed=feed,
            symbols=["X"], start="2024-01-01", end="2024-12-31",
            initial_cash=100_000.0, commission_rate=0.0, slippage_rate=0.0,
        )
        p1 = e1.run()

        # 有手续费
        feed2 = MockDataFeed({"X": make_bars("X", prices)})
        s2 = BuyThenSellStrategy("X", size=100)
        e2 = BacktestEngine(
            strategy=s2, data_feed=feed2,
            symbols=["X"], start="2024-01-01", end="2024-12-31",
            initial_cash=100_000.0, commission_rate=0.01, slippage_rate=0.0,
        )
        p2 = e2.run()

        assert p2.realized_pnl < p1.realized_pnl

    def test_slippage_worsens_fill_price(self):
        """滑点应使买入价更高。"""
        prices = [100.0, 110.0, 120.0]

        feed1 = MockDataFeed({"X": make_bars("X", prices)})
        s1 = FillTrackingStrategy("X", size=100)
        e1 = BacktestEngine(
            strategy=s1, data_feed=feed1,
            symbols=["X"], start="2024-01-01", end="2024-12-31",
            initial_cash=100_000.0, commission_rate=0.0, slippage_rate=0.0,
        )
        e1.run()

        feed2 = MockDataFeed({"X": make_bars("X", prices)})
        s2 = FillTrackingStrategy("X", size=100)
        e2 = BacktestEngine(
            strategy=s2, data_feed=feed2,
            symbols=["X"], start="2024-01-01", end="2024-12-31",
            initial_cash=100_000.0, commission_rate=0.0, slippage_rate=0.05,
        )
        e2.run()

        # 有滑点时买入价更高
        assert s2.fills_received[0].fill_price > s1.fills_received[0].fill_price


class TestEngineSMACrossover:
    def test_sma_crossover_golden_cross(self):
        """构造金叉数据，验证 SMA 策略买入。"""
        from quant_researcher.backtest.strategies import SMACrossover

        # 先跌后涨 → 制造金叉
        prices = (
            [50.0 - i * 0.5 for i in range(30)]  # 前30根：下跌 50→35.5
            + [35.0 + i * 1.0 for i in range(20)]  # 后20根：上涨 35→54
        )
        feed = MockDataFeed({"X": make_bars("X", prices)})
        strategy = SMACrossover(symbol="X", fast_period=5, slow_period=15, size=100)

        engine = BacktestEngine(
            strategy=strategy, data_feed=feed,
            symbols=["X"], start="2024-01-01", end="2024-12-31",
            initial_cash=100_000.0, commission_rate=0.0, slippage_rate=0.0,
        )
        portfolio = engine.run()

        # 上涨阶段应触发金叉买入
        assert portfolio.get_position_quantity("X") > 0
