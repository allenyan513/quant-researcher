"""Broker limit/stop/stop-limit order tests."""

import pytest

from quant_researcher.engine.core.bar_data import BarData
from quant_researcher.engine.core.event import Direction, OrderEvent, OrderType
from quant_researcher.engine.execution.broker import SimulatedBroker


def _bar_data_with_ohlc(symbol, bars_ohlc):
    """
    Create BarData with specific OHLC values.
    bars_ohlc: list of (open, high, low, close)
    """
    from datetime import datetime, timedelta

    from quant_researcher.engine.core.bar_data import Bar

    bd = BarData()
    bars = []
    dt = datetime(2024, 1, 1)
    for i, (o, h, lo, c) in enumerate(bars_ohlc):
        bars.append(Bar(
            symbol=symbol,
            timestamp=dt + timedelta(days=i),
            open=o, high=h, low=lo, close=c,
            volume=1_000_000,
        ))
    bd.add_symbol_bars(symbol, bars)
    return bd


class TestLimitOrderBuy:
    def test_buy_limit_fills_when_low_touches(self):
        """Low ≤ limit_price → 成交。"""
        broker = SimulatedBroker(commission_rate=0.0, slippage_rate=0.0)
        # open=100, high=105, low=95, close=102
        bd = _bar_data_with_ohlc("X", [(100, 105, 95, 102)])

        order = OrderEvent(
            symbol="X", direction=Direction.LONG, quantity=100,
            order_type=OrderType.LIMIT, limit_price=98.0,
        )
        broker.submit_order(order)
        bd.advance("X")
        fills = broker.fill_orders(bd)

        assert len(fills) == 1
        assert fills[0].fill_price == 98.0  # limit price
        assert fills[0].quantity == 100

    def test_buy_limit_fills_at_open_when_cheaper(self):
        """Open < limit_price → 以 open 成交（更优价格）。"""
        broker = SimulatedBroker(commission_rate=0.0, slippage_rate=0.0)
        bd = _bar_data_with_ohlc("X", [(95, 105, 94, 102)])

        order = OrderEvent(
            symbol="X", direction=Direction.LONG, quantity=100,
            order_type=OrderType.LIMIT, limit_price=98.0,
        )
        broker.submit_order(order)
        bd.advance("X")
        fills = broker.fill_orders(bd)

        assert len(fills) == 1
        assert fills[0].fill_price == 95.0  # open is cheaper

    def test_buy_limit_not_filled_when_price_above(self):
        """Low > limit_price → 不成交。"""
        broker = SimulatedBroker(commission_rate=0.0, slippage_rate=0.0)
        bd = _bar_data_with_ohlc("X", [(100, 105, 99, 102)])

        order = OrderEvent(
            symbol="X", direction=Direction.LONG, quantity=100,
            order_type=OrderType.LIMIT, limit_price=98.0,
        )
        broker.submit_order(order)
        bd.advance("X")
        fills = broker.fill_orders(bd)

        assert len(fills) == 0
        assert broker.pending_count == 1


class TestLimitOrderSell:
    def test_sell_limit_fills_when_high_touches(self):
        broker = SimulatedBroker(commission_rate=0.0, slippage_rate=0.0)
        bd = _bar_data_with_ohlc("X", [(100, 105, 95, 102)])

        order = OrderEvent(
            symbol="X", direction=Direction.SHORT, quantity=50,
            order_type=OrderType.LIMIT, limit_price=104.0,
        )
        broker.submit_order(order)
        bd.advance("X")
        fills = broker.fill_orders(bd)

        assert len(fills) == 1
        assert fills[0].fill_price == 104.0

    def test_sell_limit_fills_at_open_when_better(self):
        """Open > limit_price → 以 open 成交。"""
        broker = SimulatedBroker(commission_rate=0.0, slippage_rate=0.0)
        bd = _bar_data_with_ohlc("X", [(106, 108, 95, 102)])

        order = OrderEvent(
            symbol="X", direction=Direction.SHORT, quantity=50,
            order_type=OrderType.LIMIT, limit_price=104.0,
        )
        broker.submit_order(order)
        bd.advance("X")
        fills = broker.fill_orders(bd)

        assert len(fills) == 1
        assert fills[0].fill_price == 106.0

    def test_sell_limit_not_filled(self):
        broker = SimulatedBroker(commission_rate=0.0, slippage_rate=0.0)
        bd = _bar_data_with_ohlc("X", [(100, 103, 95, 102)])

        order = OrderEvent(
            symbol="X", direction=Direction.SHORT, quantity=50,
            order_type=OrderType.LIMIT, limit_price=104.0,
        )
        broker.submit_order(order)
        bd.advance("X")
        fills = broker.fill_orders(bd)

        assert len(fills) == 0


class TestStopOrderBuy:
    def test_buy_stop_triggers(self):
        """High ≥ stop_price → 触发。"""
        broker = SimulatedBroker(commission_rate=0.0, slippage_rate=0.0)
        bd = _bar_data_with_ohlc("X", [(100, 106, 99, 105)])

        order = OrderEvent(
            symbol="X", direction=Direction.LONG, quantity=100,
            order_type=OrderType.STOP, stop_price=105.0,
        )
        broker.submit_order(order)
        bd.advance("X")
        fills = broker.fill_orders(bd)

        assert len(fills) == 1
        assert fills[0].fill_price == 105.0  # max(stop, open) = 105

    def test_buy_stop_gap_up(self):
        """Open > stop_price → 以 open 成交。"""
        broker = SimulatedBroker(commission_rate=0.0, slippage_rate=0.0)
        bd = _bar_data_with_ohlc("X", [(108, 110, 107, 109)])

        order = OrderEvent(
            symbol="X", direction=Direction.LONG, quantity=100,
            order_type=OrderType.STOP, stop_price=105.0,
        )
        broker.submit_order(order)
        bd.advance("X")
        fills = broker.fill_orders(bd)

        assert len(fills) == 1
        assert fills[0].fill_price == 108.0  # gap up, fill at open

    def test_buy_stop_not_triggered(self):
        broker = SimulatedBroker(commission_rate=0.0, slippage_rate=0.0)
        bd = _bar_data_with_ohlc("X", [(100, 104, 99, 103)])

        order = OrderEvent(
            symbol="X", direction=Direction.LONG, quantity=100,
            order_type=OrderType.STOP, stop_price=105.0,
        )
        broker.submit_order(order)
        bd.advance("X")
        fills = broker.fill_orders(bd)

        assert len(fills) == 0
        assert broker.pending_count == 1


class TestStopOrderSell:
    def test_sell_stop_triggers(self):
        broker = SimulatedBroker(commission_rate=0.0, slippage_rate=0.0)
        bd = _bar_data_with_ohlc("X", [(100, 101, 94, 95)])

        order = OrderEvent(
            symbol="X", direction=Direction.SHORT, quantity=100,
            order_type=OrderType.STOP, stop_price=95.0,
        )
        broker.submit_order(order)
        bd.advance("X")
        fills = broker.fill_orders(bd)

        assert len(fills) == 1
        assert fills[0].fill_price == 95.0

    def test_sell_stop_gap_down(self):
        broker = SimulatedBroker(commission_rate=0.0, slippage_rate=0.0)
        bd = _bar_data_with_ohlc("X", [(92, 93, 90, 91)])

        order = OrderEvent(
            symbol="X", direction=Direction.SHORT, quantity=100,
            order_type=OrderType.STOP, stop_price=95.0,
        )
        broker.submit_order(order)
        bd.advance("X")
        fills = broker.fill_orders(bd)

        assert len(fills) == 1
        assert fills[0].fill_price == 92.0  # gap down


class TestStopLimitOrder:
    def test_stop_limit_triggers_and_fills(self):
        """Stop 触发且 limit 条件满足 → 成交。"""
        broker = SimulatedBroker(commission_rate=0.0, slippage_rate=0.0)
        # 卖出止损限价: stop=95, limit=94
        # bar: open=100, high=101, low=93, close=94
        bd = _bar_data_with_ohlc("X", [(100, 101, 93, 94)])

        order = OrderEvent(
            symbol="X", direction=Direction.SHORT, quantity=100,
            order_type=OrderType.STOP_LIMIT,
            stop_price=95.0, limit_price=94.0,
        )
        broker.submit_order(order)
        bd.advance("X")
        fills = broker.fill_orders(bd)

        assert len(fills) == 1
        assert fills[0].fill_price == 100.0  # max(limit=94, open=100) = 100

    def test_stop_limit_triggered_but_limit_not_met(self):
        """Stop 触发但 limit 条件不满足 → 留在 pending。"""
        broker = SimulatedBroker(commission_rate=0.0, slippage_rate=0.0)
        # 卖出止损限价: stop=95, limit=97 (要卖在97以上)
        # bar: open=100, high=101, low=94, close=95 → stop触发, 但high < 97? No, high=101 ≥ 97
        # 换一个: bar low=94 触发 stop=95, 但我们要limit卖在99以上
        bd = _bar_data_with_ohlc("X", [(96, 98, 94, 95)])

        order = OrderEvent(
            symbol="X", direction=Direction.SHORT, quantity=100,
            order_type=OrderType.STOP_LIMIT,
            stop_price=95.0, limit_price=99.0,
        )
        broker.submit_order(order)
        bd.advance("X")
        fills = broker.fill_orders(bd)

        assert len(fills) == 0
        assert broker.pending_count == 1  # stays pending


class TestCancelOrder:
    def test_cancel_all_for_symbol(self):
        broker = SimulatedBroker()
        broker.submit_order(OrderEvent(symbol="X", direction=Direction.LONG, quantity=100))
        broker.submit_order(OrderEvent(symbol="X", direction=Direction.SHORT, quantity=50))
        broker.submit_order(OrderEvent(symbol="Y", direction=Direction.LONG, quantity=200))

        cancelled = broker.cancel_order("X")
        assert cancelled == 2
        assert broker.pending_count == 1

    def test_cancel_by_direction(self):
        broker = SimulatedBroker()
        broker.submit_order(OrderEvent(symbol="X", direction=Direction.LONG, quantity=100))
        broker.submit_order(OrderEvent(symbol="X", direction=Direction.SHORT, quantity=50))

        cancelled = broker.cancel_order("X", Direction.LONG)
        assert cancelled == 1
        assert broker.pending_count == 1

    def test_cancel_nonexistent(self):
        broker = SimulatedBroker()
        cancelled = broker.cancel_order("Z")
        assert cancelled == 0


class TestLimitOrderCommission:
    def test_limit_order_includes_commission(self):
        broker = SimulatedBroker(commission_rate=0.002, slippage_rate=0.0)
        bd = _bar_data_with_ohlc("X", [(100, 105, 95, 102)])

        order = OrderEvent(
            symbol="X", direction=Direction.LONG, quantity=100,
            order_type=OrderType.LIMIT, limit_price=98.0,
        )
        broker.submit_order(order)
        bd.advance("X")
        fills = broker.fill_orders(bd)

        assert fills[0].commission == pytest.approx(98.0 * 100 * 0.002)
