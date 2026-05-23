"""Tests for portfolio-level RiskManager."""

from datetime import datetime, timedelta

from quant_researcher.engine.core.bar_data import Bar, BarData
from quant_researcher.engine.core.event import Direction, FillEvent, OrderEvent
from quant_researcher.engine.portfolio.portfolio import Portfolio
from quant_researcher.engine.risk.risk_manager import (
    CompositeRiskManager,
    MaxDrawdownBreaker,
    MaxPositionLimit,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bar_data(symbol: str, closes: list[float]) -> BarData:
    bd = BarData()
    bars = []
    dt = datetime(2024, 1, 1)
    for i, c in enumerate(closes):
        bars.append(Bar(
            symbol=symbol,
            timestamp=dt + timedelta(days=i),
            open=c, high=c + 1, low=c - 1, close=c,
            volume=1_000_000,
        ))
    bd.add_symbol_bars(symbol, bars)
    return bd


def _portfolio_with_equity(
    initial: float, current: float, symbol: str = "AAPL", qty: int = 0
) -> Portfolio:
    """Create a portfolio with specified equity level."""
    p = Portfolio(initial_cash=initial)
    if qty != 0:
        fill_dir = Direction.LONG if qty > 0 else Direction.SHORT
        fill = FillEvent(
            symbol=symbol,
            direction=fill_dir,
            quantity=abs(qty),
            fill_price=100.0,
        )
        p.on_fill(fill)
    # Simulate equity by adjusting cash
    bd = BarData()
    price = (current - p.cash) / qty if qty != 0 else current
    bars = [Bar(symbol=symbol, timestamp=datetime(2024, 1, 1),
                open=price, high=price, low=price, close=price, volume=1000)]
    bd.add_symbol_bars(symbol, bars)
    bd.advance(symbol)
    p.update_equity(bd, datetime(2024, 1, 1))
    return p


def _buy_order(symbol: str = "AAPL", qty: int = 100) -> OrderEvent:
    return OrderEvent(symbol=symbol, direction=Direction.LONG, quantity=qty)


def _sell_order(symbol: str = "AAPL", qty: int = 100) -> OrderEvent:
    return OrderEvent(symbol=symbol, direction=Direction.SHORT, quantity=qty)


# ---------------------------------------------------------------------------
# MaxDrawdownBreaker
# ---------------------------------------------------------------------------

class TestMaxDrawdownBreaker:
    def test_no_trigger_below_threshold(self):
        breaker = MaxDrawdownBreaker(max_drawdown=0.20)
        portfolio = Portfolio(initial_cash=100_000)
        bd = _make_bar_data("AAPL", [150.0])
        bd.advance("AAPL")
        portfolio.update_equity(bd, datetime(2024, 1, 1))

        order = _buy_order()
        result = breaker.check_order(order, portfolio, bd)
        assert result.approved

    def test_trigger_on_drawdown(self):
        breaker = MaxDrawdownBreaker(max_drawdown=0.10)
        portfolio = Portfolio(initial_cash=100_000)

        bd = _make_bar_data("AAPL", [150.0, 150.0])

        # Simulate peak equity = 100k
        bd.advance("AAPL")
        portfolio.update_equity(bd, datetime(2024, 1, 1))

        # Simulate drawdown: buy 100 shares at 150, price drops
        fill = FillEvent(symbol="AAPL", direction=Direction.LONG,
                         quantity=100, fill_price=150.0)
        portfolio.on_fill(fill)

        # First check at peak — breaker learns peak
        result = breaker.check_order(_buy_order(), portfolio, bd)
        assert result.approved

        # Now force equity down by updating with low price
        bd2 = BarData()
        bd2.add_symbol_bars("AAPL", [Bar(
            symbol="AAPL", timestamp=datetime(2024, 1, 2),
            open=40, high=40, low=40, close=40, volume=1000,
        )])
        bd2.advance("AAPL")
        portfolio.update_equity(bd2, datetime(2024, 1, 2))
        # equity = cash (100000-15000=85000) + 100*40 = 89000
        # But peak was 100000, so drawdown ~11%

        result = breaker.check_order(_buy_order(), portfolio, bd2)
        assert not result.approved
        assert "MaxDrawdownBreaker" in result.reason

    def test_allows_closing_orders_when_triggered(self):
        breaker = MaxDrawdownBreaker(max_drawdown=0.05)

        # Force breaker triggered
        breaker._peak_equity = 100_000
        breaker._breaker_triggered = True

        portfolio = Portfolio(initial_cash=90_000)
        # Has long position
        fill = FillEvent(symbol="AAPL", direction=Direction.LONG,
                         quantity=100, fill_price=100.0)
        portfolio.on_fill(fill)

        bd = _make_bar_data("AAPL", [100.0])
        bd.advance("AAPL")

        # Sell (close long) should be allowed
        result = breaker.check_order(_sell_order("AAPL", 100), portfolio, bd)
        assert result.approved

        # Buy (open new) should be rejected
        result = breaker.check_order(_buy_order("AAPL", 50), portfolio, bd)
        assert not result.approved

    def test_liquidation_generates_orders(self):
        breaker = MaxDrawdownBreaker(max_drawdown=0.10, liquidate=True)
        breaker._peak_equity = 100_000
        breaker._breaker_triggered = True

        portfolio = Portfolio(initial_cash=50_000)
        fill = FillEvent(symbol="AAPL", direction=Direction.LONG,
                         quantity=100, fill_price=100.0)
        portfolio.on_fill(fill)
        fill2 = FillEvent(symbol="GOOG", direction=Direction.LONG,
                          quantity=50, fill_price=200.0)
        portfolio.on_fill(fill2)

        bd = _make_bar_data("AAPL", [100.0])
        bd.advance("AAPL")

        orders = breaker.on_bar(portfolio, bd)
        assert len(orders) == 2
        symbols = {o.symbol for o in orders}
        assert "AAPL" in symbols
        assert "GOOG" in symbols
        for o in orders:
            assert o.direction == Direction.SHORT

        # Should not generate again
        orders2 = breaker.on_bar(portfolio, bd)
        assert len(orders2) == 0

    def test_is_triggered_property(self):
        breaker = MaxDrawdownBreaker(max_drawdown=0.10)
        assert not breaker.is_triggered
        breaker._breaker_triggered = True
        assert breaker.is_triggered


# ---------------------------------------------------------------------------
# MaxPositionLimit
# ---------------------------------------------------------------------------

class TestMaxPositionLimit:
    def test_order_within_limit(self):
        limiter = MaxPositionLimit(max_pct=0.25)
        portfolio = Portfolio(initial_cash=100_000)
        bd = _make_bar_data("AAPL", [100.0])
        bd.advance("AAPL")
        portfolio.update_equity(bd, datetime(2024, 1, 1))

        # Buy 200 shares at $100 = $20,000 = 20% < 25%
        order = _buy_order("AAPL", 200)
        result = limiter.check_order(order, portfolio, bd)
        assert result.approved
        assert result.adjusted_order is None

    def test_order_exceeds_limit_adjusted(self):
        limiter = MaxPositionLimit(max_pct=0.10)
        portfolio = Portfolio(initial_cash=100_000)
        bd = _make_bar_data("AAPL", [100.0])
        bd.advance("AAPL")
        portfolio.update_equity(bd, datetime(2024, 1, 1))

        # Buy 200 shares at $100 = $20,000 = 20% > 10%, max = 100 shares
        order = _buy_order("AAPL", 200)
        result = limiter.check_order(order, portfolio, bd)
        assert result.approved
        assert result.adjusted_order is not None
        assert result.adjusted_order.quantity == 100

    def test_closing_order_always_allowed(self):
        limiter = MaxPositionLimit(max_pct=0.10)
        portfolio = Portfolio(initial_cash=50_000)
        # Has existing long 500 shares
        fill = FillEvent(symbol="AAPL", direction=Direction.LONG,
                         quantity=500, fill_price=100.0)
        portfolio.on_fill(fill)
        bd = _make_bar_data("AAPL", [100.0])
        bd.advance("AAPL")
        portfolio.update_equity(bd, datetime(2024, 1, 1))

        # Sell 500 (close position) — always allowed
        order = _sell_order("AAPL", 500)
        result = limiter.check_order(order, portfolio, bd)
        assert result.approved

    def test_already_at_limit_rejected(self):
        limiter = MaxPositionLimit(max_pct=0.10)
        portfolio = Portfolio(initial_cash=90_000)
        fill = FillEvent(symbol="AAPL", direction=Direction.LONG,
                         quantity=100, fill_price=100.0)
        portfolio.on_fill(fill)
        bd = _make_bar_data("AAPL", [100.0])
        bd.advance("AAPL")
        portfolio.update_equity(bd, datetime(2024, 1, 1))
        # equity = 90000 - 10000 + 100*100 = 90000, 10% = 9000 = 90 shares
        # Already have 100 > 90 → no more room

        order = _buy_order("AAPL", 50)
        result = limiter.check_order(order, portfolio, bd)
        assert not result.approved
        assert "already at limit" in result.reason


# ---------------------------------------------------------------------------
# CompositeRiskManager
# ---------------------------------------------------------------------------

class TestCompositeRiskManager:
    def test_all_pass(self):
        mgr = CompositeRiskManager([
            MaxPositionLimit(max_pct=0.50),
            MaxDrawdownBreaker(max_drawdown=0.50),
        ])
        portfolio = Portfolio(initial_cash=100_000)
        bd = _make_bar_data("AAPL", [100.0])
        bd.advance("AAPL")
        portfolio.update_equity(bd, datetime(2024, 1, 1))

        result = mgr.check_order(_buy_order("AAPL", 100), portfolio, bd)
        assert result.approved

    def test_first_rejects(self):
        breaker = MaxDrawdownBreaker(max_drawdown=0.05)
        breaker._breaker_triggered = True
        breaker._peak_equity = 100_000

        mgr = CompositeRiskManager([
            breaker,
            MaxPositionLimit(max_pct=0.50),
        ])
        portfolio = Portfolio(initial_cash=90_000)
        bd = _make_bar_data("AAPL", [100.0])
        bd.advance("AAPL")
        portfolio.update_equity(bd, datetime(2024, 1, 1))

        result = mgr.check_order(_buy_order("AAPL", 100), portfolio, bd)
        assert not result.approved

    def test_adjustment_chained(self):
        """Second manager sees adjusted order from first."""
        mgr = CompositeRiskManager([
            MaxPositionLimit(max_pct=0.10),  # limits to 100 shares at $100
            MaxPositionLimit(max_pct=0.05),  # limits to 50 shares at $100
        ])
        portfolio = Portfolio(initial_cash=100_000)
        bd = _make_bar_data("AAPL", [100.0])
        bd.advance("AAPL")
        portfolio.update_equity(bd, datetime(2024, 1, 1))

        result = mgr.check_order(_buy_order("AAPL", 200), portfolio, bd)
        assert result.approved
        assert result.adjusted_order is not None
        # First limits to 100, second limits to 50
        assert result.adjusted_order.quantity == 50

    def test_add_manager(self):
        mgr = CompositeRiskManager()
        mgr.add(MaxPositionLimit(max_pct=0.50))
        assert len(mgr._managers) == 1

    def test_on_bar_collects_from_all(self):
        b1 = MaxDrawdownBreaker(max_drawdown=0.10, liquidate=True)
        b1._breaker_triggered = True
        b1._peak_equity = 100_000

        mgr = CompositeRiskManager([b1])
        portfolio = Portfolio(initial_cash=50_000)
        fill = FillEvent(symbol="AAPL", direction=Direction.LONG,
                         quantity=100, fill_price=100.0)
        portfolio.on_fill(fill)

        bd = _make_bar_data("AAPL", [100.0])
        bd.advance("AAPL")

        orders = mgr.on_bar(portfolio, bd)
        assert len(orders) == 1
        assert orders[0].symbol == "AAPL"


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------

class TestRiskManagerEngineIntegration:
    """Test that RiskManager is properly called in the engine loop."""

    def test_engine_without_risk_manager(self):
        """Engine works fine without risk_manager (backward compat)."""
        from quant_researcher.engine.core.bar_data import Bar
        from quant_researcher.engine.data.data_feed import DataFeed
        from quant_researcher.engine.engine import BacktestEngine
        from quant_researcher.engine.strategy.base import BaseStrategy

        class DummyFeed(DataFeed):
            def fetch(self, symbol, start, end):
                dt = datetime(2024, 1, 1)
                return [
                    Bar(symbol=symbol, timestamp=dt + timedelta(days=i),
                        open=100+i, high=102+i, low=99+i, close=101+i, volume=1000)
                    for i in range(5)
                ]

        class DummyStrategy(BaseStrategy):
            def on_bar(self):
                pass

        engine = BacktestEngine(
            strategy=DummyStrategy(),
            data_feed=DummyFeed(),
            symbols=["X"],
            start="2024-01-01",
            end="2024-01-10",
            initial_cash=100_000,
        )
        portfolio = engine.run()
        assert portfolio.equity > 0

    def test_engine_with_risk_manager_blocks_orders(self):
        """RiskManager blocks orders when triggered."""
        from quant_researcher.engine.core.bar_data import Bar
        from quant_researcher.engine.data.data_feed import DataFeed
        from quant_researcher.engine.engine import BacktestEngine
        from quant_researcher.engine.strategy.base import BaseStrategy

        class DummyFeed(DataFeed):
            def fetch(self, symbol, start, end):
                dt = datetime(2024, 1, 1)
                return [
                    Bar(symbol=symbol, timestamp=dt + timedelta(days=i),
                        open=100, high=101, low=99, close=100, volume=1000)
                    for i in range(5)
                ]

        class BuyEveryBar(BaseStrategy):
            def on_bar(self):
                if self.get_position("X") == 0:
                    self.buy("X", 100)

        # Use MaxPositionLimit that effectively blocks all buys (0%)
        limiter = MaxPositionLimit(max_pct=0.0001)

        engine = BacktestEngine(
            strategy=BuyEveryBar(),
            data_feed=DummyFeed(),
            symbols=["X"],
            start="2024-01-01",
            end="2024-01-10",
            initial_cash=100_000,
            risk_manager=limiter,
        )
        portfolio = engine.run()
        # Should have no position because risk manager blocked
        assert portfolio.get_position_quantity("X") == 0
