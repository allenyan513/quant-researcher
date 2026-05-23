"""Portfolio 和 Position 测试。"""

import pytest

from quant_researcher.engine.core.event import Direction, FillEvent
from quant_researcher.engine.portfolio.portfolio import Portfolio, Position
from tests.engine.helpers import advance_all, make_bar_data


class TestPositionOpen:
    def test_buy_open(self):
        pos = Position(symbol="X")
        fill = FillEvent(symbol="X", direction=Direction.LONG, quantity=100, fill_price=50.0)
        pnl = pos.update(fill)
        assert pos.quantity == 100
        assert pos.avg_cost == 50.0
        assert pnl == 0.0  # 开仓无盈亏

    def test_sell_short_open(self):
        pos = Position(symbol="X")
        fill = FillEvent(symbol="X", direction=Direction.SHORT, quantity=100, fill_price=50.0)
        pnl = pos.update(fill)
        assert pos.quantity == -100
        assert pos.avg_cost == 50.0
        assert pnl == 0.0


class TestPositionAddToPosition:
    def test_buy_add(self):
        """加仓应更新均价。"""
        pos = Position(symbol="X")
        pos.update(FillEvent(symbol="X", direction=Direction.LONG, quantity=100, fill_price=50.0))
        pos.update(FillEvent(symbol="X", direction=Direction.LONG, quantity=100, fill_price=60.0))
        assert pos.quantity == 200
        assert pos.avg_cost == pytest.approx(55.0)  # (50*100 + 60*100) / 200


class TestPositionClose:
    def test_buy_then_sell_profit(self):
        """买入后卖出，盈利。"""
        pos = Position(symbol="X")
        pos.update(FillEvent(symbol="X", direction=Direction.LONG, quantity=100, fill_price=50.0))
        pnl = pos.update(
            FillEvent(symbol="X", direction=Direction.SHORT, quantity=100, fill_price=60.0)
        )
        assert pos.quantity == 0
        assert pnl == 100 * (60.0 - 50.0)  # $1000 profit

    def test_buy_then_sell_loss(self):
        """买入后卖出，亏损。"""
        pos = Position(symbol="X")
        pos.update(FillEvent(symbol="X", direction=Direction.LONG, quantity=100, fill_price=50.0))
        pnl = pos.update(
            FillEvent(symbol="X", direction=Direction.SHORT, quantity=100, fill_price=40.0)
        )
        assert pos.quantity == 0
        assert pnl == 100 * (40.0 - 50.0)  # -$1000 loss

    def test_partial_close(self):
        """部分平仓。"""
        pos = Position(symbol="X")
        pos.update(FillEvent(symbol="X", direction=Direction.LONG, quantity=100, fill_price=50.0))
        pnl = pos.update(
            FillEvent(symbol="X", direction=Direction.SHORT, quantity=60, fill_price=70.0)
        )
        assert pos.quantity == 40
        assert pos.avg_cost == 50.0  # 均价不变
        assert pnl == 60 * (70.0 - 50.0)  # $1200

    def test_commission_deducted(self):
        """手续费从已实现盈亏中扣除。"""
        pos = Position(symbol="X")
        pos.update(FillEvent(symbol="X", direction=Direction.LONG, quantity=100, fill_price=50.0))
        pnl = pos.update(FillEvent(
            symbol="X", direction=Direction.SHORT, quantity=100,
            fill_price=60.0, commission=10.0,
        ))
        assert pnl == 100 * (60.0 - 50.0) - 10.0  # $990


class TestPositionReverse:
    def test_reverse_long_to_short(self):
        """从多头反转到空头。"""
        pos = Position(symbol="X")
        pos.update(FillEvent(symbol="X", direction=Direction.LONG, quantity=50, fill_price=100.0))
        pnl = pos.update(
            FillEvent(symbol="X", direction=Direction.SHORT, quantity=80, fill_price=110.0)
        )
        # 先平掉 50 股多头: 50 * (110 - 100) = 500
        assert pnl == 500.0
        # 剩余 30 股空头
        assert pos.quantity == -30
        assert pos.avg_cost == 110.0


class TestPortfolioCash:
    def test_initial_cash(self):
        p = Portfolio(initial_cash=50_000.0)
        assert p.cash == 50_000.0
        assert p.equity == 50_000.0

    def test_buy_reduces_cash(self):
        p = Portfolio(initial_cash=100_000.0)
        fill = FillEvent(
            symbol="X", direction=Direction.LONG,
            quantity=100, fill_price=50.0, commission=5.0,
        )
        p.on_fill(fill)
        assert p.cash == 100_000.0 - 50.0 * 100 - 5.0

    def test_sell_adds_cash(self):
        p = Portfolio(initial_cash=100_000.0)
        # 先买入
        p.on_fill(FillEvent(symbol="X", direction=Direction.LONG, quantity=100, fill_price=50.0))
        # 再卖出
        p.on_fill(
            FillEvent(
                symbol="X", direction=Direction.SHORT, quantity=100,
                fill_price=60.0, commission=6.0,
            )
        )
        expected = 100_000.0 - 50.0 * 100 + 60.0 * 100 - 6.0
        assert p.cash == pytest.approx(expected)


class TestPortfolioEquity:
    def test_equity_with_open_position(self):
        """净值 = 现金 + 持仓市值。"""
        p = Portfolio(initial_cash=100_000.0)
        p.on_fill(FillEvent(symbol="X", direction=Direction.LONG, quantity=100, fill_price=50.0))
        # 现金: 100000 - 5000 = 95000
        # 模拟价格涨到 60
        bd = make_bar_data("X", [60.0])
        advance_all(bd)
        from datetime import datetime
        p.update_equity(bd, datetime(2024, 1, 1))
        # equity = 95000 + 100 * 60 = 101000
        assert p.equity == pytest.approx(101_000.0)

    def test_equity_curve_recorded(self):
        p = Portfolio(initial_cash=100_000.0)
        bd = make_bar_data("X", [100.0, 110.0])
        bd.advance("X")
        from datetime import datetime
        p.update_equity(bd, datetime(2024, 1, 1))
        bd.advance("X")
        p.update_equity(bd, datetime(2024, 1, 2))
        assert len(p.equity_curve) == 2


class TestPortfolioGetPosition:
    def test_no_position(self):
        p = Portfolio()
        assert p.get_position_quantity("X") == 0

    def test_has_position(self):
        p = Portfolio()
        p.on_fill(FillEvent(symbol="X", direction=Direction.LONG, quantity=100, fill_price=50.0))
        assert p.get_position_quantity("X") == 100


class TestPortfolioRealizedPnl:
    def test_tracks_cumulative_pnl(self):
        p = Portfolio(initial_cash=100_000.0)
        p.on_fill(FillEvent(symbol="X", direction=Direction.LONG, quantity=100, fill_price=50.0))
        p.on_fill(FillEvent(symbol="X", direction=Direction.SHORT, quantity=100, fill_price=60.0))
        assert p.realized_pnl == pytest.approx(1000.0)  # 100 * (60-50)
