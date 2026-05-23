"""Tests for margin models."""

from datetime import datetime, timedelta

import pytest

from quant_researcher.engine.core.bar_data import Bar, BarData
from quant_researcher.engine.core.event import Direction, FillEvent, OrderEvent
from quant_researcher.engine.execution.margin_model import (
    CashAccount,
    PortfolioMargin,
    RegTMargin,
)
from quant_researcher.engine.portfolio.portfolio import Portfolio


def _make_bar_data(symbol: str, price: float = 100.0) -> BarData:
    bd = BarData()
    bd.add_symbol_bars(symbol, [Bar(
        symbol=symbol, timestamp=datetime(2024, 1, 1),
        open=price, high=price + 1, low=price - 1, close=price,
        volume=1_000_000,
    )])
    bd.advance(symbol)
    return bd


def _portfolio_with_position(symbol="AAPL", qty=100, price=100.0, cash=100_000) -> Portfolio:
    p = Portfolio(initial_cash=cash)
    if qty != 0:
        direction = Direction.LONG if qty > 0 else Direction.SHORT
        fill = FillEvent(
            symbol=symbol, direction=direction, quantity=abs(qty),
            fill_price=price, timestamp=datetime(2024, 1, 1),
        )
        p.on_fill(fill)
    return p


def _buy_order(symbol="AAPL", qty=100) -> OrderEvent:
    return OrderEvent(symbol=symbol, direction=Direction.LONG, quantity=qty)


def _sell_order(symbol="AAPL", qty=100) -> OrderEvent:
    return OrderEvent(symbol=symbol, direction=Direction.SHORT, quantity=qty)


# ---------------------------------------------------------------------------
# Reg T Margin
# ---------------------------------------------------------------------------

class TestRegTMargin:
    def test_long_margin_requirement(self):
        margin = RegTMargin()
        req = margin.calculate_requirement("AAPL", 100, 150.0, Direction.LONG)
        # 100 * 150 = 15000, initial = 50% = 7500
        assert req.initial_margin == pytest.approx(7500.0)
        assert req.maintenance_margin == pytest.approx(3750.0)  # 25%

    def test_short_margin_requirement(self):
        margin = RegTMargin()
        req = margin.calculate_requirement("AAPL", 100, 150.0, Direction.SHORT)
        # 100 * 150 = 15000, initial = 50% = 7500
        assert req.initial_margin == pytest.approx(7500.0)
        assert req.maintenance_margin == pytest.approx(4500.0)  # 30%

    def test_margin_status_no_positions(self):
        margin = RegTMargin()
        p = Portfolio(initial_cash=100_000)
        bd = _make_bar_data("AAPL")
        p.update_equity(bd, datetime(2024, 1, 1))

        status = margin.check_margin_status(p, bd)
        assert not status.margin_call
        assert status.total_margin_required == 0
        assert status.margin_ratio == float("inf")

    def test_margin_status_with_position(self):
        margin = RegTMargin()
        p = _portfolio_with_position("AAPL", 100, 100.0, cash=100_000)
        bd = _make_bar_data("AAPL", 100.0)
        p.update_equity(bd, datetime(2024, 1, 1))

        status = margin.check_margin_status(p, bd)
        # equity = 100000 - 10000 + 100*100 = 100000
        # maintenance = 10000 * 25% = 2500
        assert not status.margin_call
        assert status.total_margin_required == pytest.approx(2500.0)

    def test_margin_call_triggered(self):
        margin = RegTMargin()
        # Very leveraged: small cash, big position
        p = _portfolio_with_position("AAPL", 500, 100.0, cash=55_000)
        # cash after buy: 55000 - 50000 = 5000
        # equity at price 100: 5000 + 500*100 = 55000
        # maintenance = 50000 * 25% = 12500
        # margin_excess = 55000 - 12500 = 42500, no call

        # But if price drops to 10:
        bd = _make_bar_data("AAPL", 10.0)
        p.update_equity(bd, datetime(2024, 1, 2))
        # equity = 5000 + 500*10 = 10000
        # maintenance = 5000 * 25% = 1250, still no call actually

        # More extreme: position at 100, price drops to 5
        p2 = _portfolio_with_position("AAPL", 1000, 100.0, cash=100_100)
        # cash after buy: 100100 - 100000 = 100
        bd2 = _make_bar_data("AAPL", 5.0)
        p2.update_equity(bd2, datetime(2024, 1, 2))
        # equity = 100 + 1000*5 = 5100
        # maintenance = 5000 * 25% = 1250
        status = margin.check_margin_status(p2, bd2)
        assert not status.margin_call  # 5100 > 1250

        # Even more extreme
        p3 = _portfolio_with_position("AAPL", 1000, 100.0, cash=100_010)
        # cash = 100010 - 100000 = 10
        bd3 = _make_bar_data("AAPL", 0.005)
        p3.update_equity(bd3, datetime(2024, 1, 2))
        # equity = 10 + 1000*0.005 = 15
        # maintenance = 5 * 25% = 1.25
        status3 = margin.check_margin_status(p3, bd3)
        assert not status3.margin_call  # 15 > 1.25

    def test_short_margin_call(self):
        margin = RegTMargin()
        # Short 100 shares at 100, price rises dramatically
        p = Portfolio(initial_cash=10_000)
        fill = FillEvent(symbol="AAPL", direction=Direction.SHORT,
                         quantity=100, fill_price=100.0,
                         timestamp=datetime(2024, 1, 1))
        p.on_fill(fill)
        # cash = 10000 + 10000 = 20000

        # Price rises to 300
        bd = _make_bar_data("AAPL", 300.0)
        p.update_equity(bd, datetime(2024, 1, 2))
        # equity = 20000 + (-100 * 300) = 20000 - 30000 = -10000
        # maintenance = 30000 * 30% = 9000
        # equity < maintenance → margin call
        status = margin.check_margin_status(p, bd)
        assert status.margin_call

    def test_check_order_approved(self):
        margin = RegTMargin()
        p = Portfolio(initial_cash=100_000)
        bd = _make_bar_data("AAPL", 100.0)
        p.update_equity(bd, datetime(2024, 1, 1))

        # Buy 100 shares, need 50% = $5000 margin, have $100000
        approved, reason = margin.check_order(_buy_order("AAPL", 100), p, bd)
        assert approved

    def test_check_order_rejected(self):
        margin = RegTMargin()
        p = Portfolio(initial_cash=1_000)
        bd = _make_bar_data("AAPL", 100.0)
        p.update_equity(bd, datetime(2024, 1, 1))

        # Buy 100 shares, need $5000, only have $1000
        approved, reason = margin.check_order(_buy_order("AAPL", 100), p, bd)
        assert not approved
        assert "Insufficient margin" in reason


# ---------------------------------------------------------------------------
# Portfolio Margin
# ---------------------------------------------------------------------------

class TestPortfolioMargin:
    def test_lower_requirements_than_regt(self):
        regt = RegTMargin()
        pm = PortfolioMargin()

        regt_req = regt.calculate_requirement("AAPL", 100, 100.0, Direction.LONG)
        pm_req = pm.calculate_requirement("AAPL", 100, 100.0, Direction.LONG)

        assert pm_req.initial_margin < regt_req.initial_margin
        assert pm_req.maintenance_margin < regt_req.maintenance_margin

    def test_long_requirement(self):
        pm = PortfolioMargin()
        req = pm.calculate_requirement("AAPL", 100, 100.0, Direction.LONG)
        assert req.initial_margin == pytest.approx(1500.0)  # 15%
        assert req.maintenance_margin == pytest.approx(1000.0)  # 10%

    def test_short_requirement(self):
        pm = PortfolioMargin()
        req = pm.calculate_requirement("AAPL", 100, 100.0, Direction.SHORT)
        assert req.initial_margin == pytest.approx(1500.0)  # 15%
        assert req.maintenance_margin == pytest.approx(1200.0)  # 12%


# ---------------------------------------------------------------------------
# Cash Account
# ---------------------------------------------------------------------------

class TestCashAccount:
    def test_long_full_cash(self):
        ca = CashAccount()
        req = ca.calculate_requirement("AAPL", 100, 100.0, Direction.LONG)
        assert req.initial_margin == pytest.approx(10000.0)  # 100%
        assert req.maintenance_margin == pytest.approx(10000.0)

    def test_short_blocked(self):
        ca = CashAccount()
        req = ca.calculate_requirement("AAPL", 100, 100.0, Direction.SHORT)
        assert req.initial_margin == float("inf")

    def test_check_order_blocks_short(self):
        ca = CashAccount()
        p = Portfolio(initial_cash=100_000)
        bd = _make_bar_data("AAPL", 100.0)
        p.update_equity(bd, datetime(2024, 1, 1))

        approved, reason = ca.check_order(_sell_order("AAPL", 100), p, bd)
        assert not approved
        assert "short selling" in reason

    def test_check_order_allows_close_short(self):
        """Selling to close a long position should be allowed."""
        ca = CashAccount()
        p = _portfolio_with_position("AAPL", 100, 100.0)
        bd = _make_bar_data("AAPL", 100.0)
        p.update_equity(bd, datetime(2024, 1, 1))

        approved, reason = ca.check_order(_sell_order("AAPL", 100), p, bd)
        assert approved

    def test_check_order_allows_buy_with_cash(self):
        ca = CashAccount()
        p = Portfolio(initial_cash=100_000)
        bd = _make_bar_data("AAPL", 100.0)
        p.update_equity(bd, datetime(2024, 1, 1))

        approved, _ = ca.check_order(_buy_order("AAPL", 500), p, bd)
        assert approved  # 500 * 100 = 50000 < 100000

    def test_check_order_rejects_buy_without_cash(self):
        ca = CashAccount()
        p = Portfolio(initial_cash=1_000)
        bd = _make_bar_data("AAPL", 100.0)
        p.update_equity(bd, datetime(2024, 1, 1))

        approved, reason = ca.check_order(_buy_order("AAPL", 500), p, bd)
        assert not approved


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------

class TestMarginEngineIntegration:
    def test_engine_with_cash_account_blocks_short(self):
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

        class ShortStrategy(BaseStrategy):
            def on_bar(self):
                if self.get_position("X") == 0:
                    self.sell("X", 100)  # try to short

        engine = BacktestEngine(
            strategy=ShortStrategy(),
            data_feed=DummyFeed(),
            symbols=["X"],
            start="2024-01-01",
            end="2024-01-10",
            initial_cash=100_000,
            margin_model=CashAccount(),
        )
        portfolio = engine.run()
        # Short should be blocked by CashAccount
        assert portfolio.get_position_quantity("X") == 0

    def test_engine_with_regt_allows_leveraged_long(self):
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

        class BigBuy(BaseStrategy):
            def on_bar(self):
                if self.get_position("X") == 0:
                    self.buy("X", 500)  # $50,000 at $100

        engine = BacktestEngine(
            strategy=BigBuy(),
            data_feed=DummyFeed(),
            symbols=["X"],
            start="2024-01-01",
            end="2024-01-10",
            initial_cash=100_000,
            margin_model=RegTMargin(),
        )
        portfolio = engine.run()
        # Reg T requires 50% = $25,000 initial, have $100k → approved
        assert portfolio.get_position_quantity("X") == 500
