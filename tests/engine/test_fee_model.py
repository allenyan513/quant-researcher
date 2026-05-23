"""FeeModel 测试。"""

import pytest

from quant_researcher.engine.execution.fee_model import (
    PercentageFeeModel,
    PerShareFeeModel,
    ZeroFeeModel,
)


class TestZeroFeeModel:
    def test_always_zero(self):
        model = ZeroFeeModel()
        assert model.calculate(100.0, 1000) == 0.0
        assert model.calculate(0.0, 0) == 0.0


class TestPercentageFeeModel:
    def test_default_rate(self):
        model = PercentageFeeModel()  # 0.1%
        assert model.calculate(100.0, 100) == pytest.approx(10.0)

    def test_custom_rate(self):
        model = PercentageFeeModel(rate=0.002)
        assert model.calculate(50.0, 200) == pytest.approx(20.0)

    def test_zero_rate(self):
        model = PercentageFeeModel(rate=0.0)
        assert model.calculate(100.0, 100) == 0.0


class TestPerShareFeeModel:
    def test_default_ib_params(self):
        model = PerShareFeeModel()  # $0.005/share, min $1, max 0.5%
        # 200 shares: 200 * 0.005 = $1.00 = min_fee, so $1.00
        assert model.calculate(100.0, 200) == pytest.approx(1.0)

    def test_above_minimum(self):
        model = PerShareFeeModel()
        # 1000 shares: 1000 * 0.005 = $5.00 > min $1
        assert model.calculate(100.0, 1000) == pytest.approx(5.0)

    def test_minimum_fee_applies(self):
        model = PerShareFeeModel()
        # 10 shares: 10 * 0.005 = $0.05 < min $1 → $1.00
        assert model.calculate(100.0, 10) == pytest.approx(1.0)

    def test_max_pct_cap(self):
        model = PerShareFeeModel()
        # 10000 shares @ $1: raw = $50, max = $1*10000*0.005 = $50
        # 10000 shares @ $0.50: raw = $50, max = $0.50*10000*0.005 = $25 → capped
        assert model.calculate(0.50, 10000) == pytest.approx(25.0)

    def test_custom_params(self):
        model = PerShareFeeModel(per_share=0.01, min_fee=2.0, max_pct=0.01)
        # 500 shares @ $100: raw = $5.00, max = $500 → $5.00
        assert model.calculate(100.0, 500) == pytest.approx(5.0)
        # 50 shares @ $100: raw = $0.50 < min $2 → $2.00
        assert model.calculate(100.0, 50) == pytest.approx(2.0)

    def test_realistic_ib_scenarios(self):
        """模拟 IB 真实场景。"""
        model = PerShareFeeModel()

        # 买 100 股 AAPL @ $200: 100 * 0.005 = $0.50 < min $1 → $1.00
        assert model.calculate(200.0, 100) == pytest.approx(1.0)

        # 买 5000 股 AAPL @ $200: 5000 * 0.005 = $25 → $25
        assert model.calculate(200.0, 5000) == pytest.approx(25.0)

        # 买 100 股低价股 @ $2: 100 * 0.005 = $0.50 < min $1 → $1.00
        assert model.calculate(2.0, 100) == pytest.approx(1.0)


class TestBrokerWithFeeModel:
    """测试 SimulatedBroker 与 FeeModel 的集成。"""

    def test_broker_with_per_share_model(self):
        from quant_researcher.engine.core.event import Direction, OrderEvent
        from quant_researcher.engine.execution.broker import SimulatedBroker
        from tests.engine.helpers import advance_all, make_bar_data

        broker = SimulatedBroker(
            fee_model=PerShareFeeModel(),
            slippage_rate=0.0,
        )
        bd = make_bar_data("X", [100.0])

        order = OrderEvent(symbol="X", direction=Direction.LONG, quantity=100)
        broker.submit_order(order)

        advance_all(bd)
        fills = broker.fill_orders(bd)

        assert len(fills) == 1
        # 100 shares * $0.005 = $0.50, min $1.00
        assert fills[0].commission == pytest.approx(1.0)

    def test_broker_backward_compat_commission_rate(self):
        from quant_researcher.engine.core.event import Direction, OrderEvent
        from quant_researcher.engine.execution.broker import SimulatedBroker
        from tests.engine.helpers import advance_all, make_bar_data

        broker = SimulatedBroker(
            commission_rate=0.002,
            slippage_rate=0.0,
        )
        bd = make_bar_data("X", [100.0])

        order = OrderEvent(symbol="X", direction=Direction.LONG, quantity=100)
        broker.submit_order(order)

        advance_all(bd)
        fills = broker.fill_orders(bd)

        fill = fills[0]
        expected_comm = fill.fill_price * 100 * 0.002
        assert fill.commission == pytest.approx(expected_comm)

    def test_broker_default_is_per_share(self):
        from quant_researcher.engine.execution.broker import SimulatedBroker

        broker = SimulatedBroker()
        assert isinstance(broker.fee_model, PerShareFeeModel)
