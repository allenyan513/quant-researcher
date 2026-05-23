"""SimulatedBroker 测试。"""

import pytest

from quant_researcher.engine.core.event import Direction, OrderEvent, OrderType
from quant_researcher.engine.execution.broker import SimulatedBroker
from tests.engine.helpers import advance_all, make_bar_data


class TestBrokerMarketOrder:
    def test_fill_buy_with_slippage(self):
        broker = SimulatedBroker(commission_rate=0.001, slippage_rate=0.01)
        bd = make_bar_data("X", [100.0])

        order = OrderEvent(symbol="X", direction=Direction.LONG, quantity=100)
        broker.submit_order(order)

        advance_all(bd)
        fills = broker.fill_orders(bd)

        assert len(fills) == 1
        fill = fills[0]
        # open = close - 0.5 = 99.5, fill_price = 99.5 * 1.01
        expected_price = 99.5 * 1.01
        assert fill.fill_price == pytest.approx(expected_price)
        assert fill.direction == Direction.LONG
        assert fill.quantity == 100

    def test_fill_sell_with_slippage(self):
        broker = SimulatedBroker(commission_rate=0.0, slippage_rate=0.01)
        bd = make_bar_data("X", [100.0])

        order = OrderEvent(symbol="X", direction=Direction.SHORT, quantity=50)
        broker.submit_order(order)

        advance_all(bd)
        fills = broker.fill_orders(bd)

        assert len(fills) == 1
        # sell 滑点向下: open * (1 - 0.01)
        expected_price = 99.5 * 0.99
        assert fills[0].fill_price == pytest.approx(expected_price)

    def test_commission_calculated(self):
        broker = SimulatedBroker(commission_rate=0.002, slippage_rate=0.0)
        bd = make_bar_data("X", [100.0])

        order = OrderEvent(symbol="X", direction=Direction.LONG, quantity=100)
        broker.submit_order(order)

        advance_all(bd)
        fills = broker.fill_orders(bd)

        fill = fills[0]
        expected_comm = fill.fill_price * 100 * 0.002
        assert fill.commission == pytest.approx(expected_comm)

    def test_zero_slippage_zero_commission(self):
        broker = SimulatedBroker(commission_rate=0.0, slippage_rate=0.0)
        bd = make_bar_data("X", [100.0])

        order = OrderEvent(symbol="X", direction=Direction.LONG, quantity=10)
        broker.submit_order(order)

        advance_all(bd)
        fills = broker.fill_orders(bd)

        assert fills[0].fill_price == pytest.approx(99.5)  # open = close - 0.5
        assert fills[0].commission == 0.0


class TestBrokerLimitOrder:
    def test_limit_order_stays_pending(self):
        """Phase 1 不撮合限价单，应留在 pending。"""
        broker = SimulatedBroker()
        bd = make_bar_data("X", [100.0])

        order = OrderEvent(
            symbol="X", direction=Direction.LONG, quantity=100,
            order_type=OrderType.LIMIT, limit_price=95.0,
        )
        broker.submit_order(order)

        advance_all(bd)
        fills = broker.fill_orders(bd)

        assert len(fills) == 0
        assert broker.pending_count == 1


class TestBrokerNoData:
    def test_order_stays_pending_when_no_bar(self):
        broker = SimulatedBroker()
        bd = make_bar_data("X", [100.0])
        # 不 advance，没有当前 bar

        order = OrderEvent(symbol="X", direction=Direction.LONG, quantity=100)
        broker.submit_order(order)

        fills = broker.fill_orders(bd)
        assert len(fills) == 0
        assert broker.pending_count == 1


class TestBrokerMultipleOrders:
    def test_fill_multiple_orders(self):
        broker = SimulatedBroker(commission_rate=0.0, slippage_rate=0.0)
        bd = make_bar_data("X", [100.0])

        broker.submit_order(OrderEvent(symbol="X", direction=Direction.LONG, quantity=50))
        broker.submit_order(OrderEvent(symbol="X", direction=Direction.LONG, quantity=30))

        advance_all(bd)
        fills = broker.fill_orders(bd)

        assert len(fills) == 2
        assert fills[0].quantity == 50
        assert fills[1].quantity == 30
        assert broker.pending_count == 0
