"""Tests for enhanced fee models and slippage models."""

from datetime import datetime

import pytest

from quant_researcher.engine.core.bar_data import Bar, BarData
from quant_researcher.engine.core.event import Direction, OrderEvent
from quant_researcher.engine.execution.broker import SimulatedBroker
from quant_researcher.engine.execution.fee_model import TieredFeeModel
from quant_researcher.engine.execution.slippage_model import (
    FixedRateSlippage,
    VolumeImpactSlippage,
    ZeroSlippage,
)


def _bar(price=100.0, volume=1_000_000) -> Bar:
    return Bar(
        symbol="X", timestamp=datetime(2024, 1, 1),
        open=price, high=price + 1, low=price - 1, close=price,
        volume=volume,
    )


# ---------------------------------------------------------------------------
# TieredFeeModel
# ---------------------------------------------------------------------------

class TestTieredFeeModel:
    def test_default_tiers_low_volume(self):
        fee = TieredFeeModel()
        # First trade, 0 monthly volume → tier 1: $0.0035/share
        result = fee.calculate(100.0, 1000)
        expected = max(0.35, 1000 * 0.0035)  # $3.50
        assert result == pytest.approx(expected)

    def test_min_fee(self):
        fee = TieredFeeModel()
        result = fee.calculate(10.0, 10)
        # 10 * 0.0035 = $0.035, below min $0.35
        assert result == pytest.approx(0.35)

    def test_max_pct_cap(self):
        fee = TieredFeeModel(max_pct=0.001)
        # 1000 shares at $1 = $1000, max 0.1% = $1.00
        # raw = 1000 * 0.0035 = $3.50, capped at $1.00
        result = fee.calculate(1.0, 1000)
        assert result == pytest.approx(1.0)

    def test_volume_progression(self):
        fee = TieredFeeModel()
        # Trade 300,001 shares to move past tier 1 threshold
        fee.calculate(100.0, 300_001)
        assert fee.monthly_volume == 300_001

        # Next trade should use tier 2 rate ($0.0020)
        result = fee.calculate(100.0, 1000)
        expected = max(0.35, 1000 * 0.0020)  # $2.00
        assert result == pytest.approx(expected)

    def test_reset_monthly_volume(self):
        fee = TieredFeeModel()
        fee.calculate(100.0, 100_000)
        assert fee.monthly_volume == 100_000
        fee.reset_monthly_volume()
        assert fee.monthly_volume == 0

    def test_custom_tiers(self):
        tiers = [
            (1000, 0.01),
            (float("inf"), 0.005),
        ]
        fee = TieredFeeModel(tiers=tiers, min_fee=0.0)
        # First 1000 shares at $0.01
        result = fee.calculate(100.0, 500)
        assert result == pytest.approx(500 * 0.01)

        # After 1000 threshold, rate drops
        fee._monthly_volume = 1001
        result = fee.calculate(100.0, 500)
        assert result == pytest.approx(500 * 0.005)


# ---------------------------------------------------------------------------
# SlippageModel
# ---------------------------------------------------------------------------

class TestFixedRateSlippage:
    def test_buy_slippage(self):
        model = FixedRateSlippage(rate=0.001)
        bar = _bar(100.0)
        result = model.calculate(100.0, Direction.LONG, bar, 100)
        assert result == pytest.approx(100.1)

    def test_sell_slippage(self):
        model = FixedRateSlippage(rate=0.001)
        bar = _bar(100.0)
        result = model.calculate(100.0, Direction.SHORT, bar, 100)
        assert result == pytest.approx(99.9)


class TestVolumeImpactSlippage:
    def test_small_order_low_impact(self):
        model = VolumeImpactSlippage(base_rate=0.0001, impact_factor=0.1)
        bar = _bar(100.0, volume=1_000_000)
        # 100 shares / 1M = 0.0001, sqrt = 0.01
        # impact = 0.0001 + 0.1 * 0.01 = 0.0011
        result = model.calculate(100.0, Direction.LONG, bar, 100)
        expected = 100.0 * (1 + 0.0001 + 0.1 * (100 / 1_000_000) ** 0.5)
        assert result == pytest.approx(expected)

    def test_large_order_high_impact(self):
        model = VolumeImpactSlippage(base_rate=0.0001, impact_factor=0.1)
        bar = _bar(100.0, volume=1_000_000)

        small = model.calculate(100.0, Direction.LONG, bar, 100)
        large = model.calculate(100.0, Direction.LONG, bar, 100_000)
        # Larger order should have more slippage
        assert large > small

    def test_sell_direction(self):
        model = VolumeImpactSlippage(base_rate=0.0001, impact_factor=0.1)
        bar = _bar(100.0, volume=1_000_000)
        result = model.calculate(100.0, Direction.SHORT, bar, 1000)
        assert result < 100.0

    def test_zero_volume_fallback(self):
        model = VolumeImpactSlippage()
        bar = _bar(100.0, volume=0)
        # Should not crash, uses fallback volume
        result = model.calculate(100.0, Direction.LONG, bar, 100)
        assert result > 100.0


class TestZeroSlippage:
    def test_no_slippage(self):
        model = ZeroSlippage()
        bar = _bar(100.0)
        assert model.calculate(100.0, Direction.LONG, bar, 1000) == 100.0
        assert model.calculate(100.0, Direction.SHORT, bar, 1000) == 100.0


# ---------------------------------------------------------------------------
# Broker integration
# ---------------------------------------------------------------------------

class TestBrokerWithSlippageModel:
    def test_broker_uses_slippage_model(self):
        model = FixedRateSlippage(rate=0.01)  # 1% for easy verification
        broker = SimulatedBroker(slippage_model=model, slippage_rate=0.0)

        bd = BarData()
        bd.add_symbol_bars("X", [
            _bar(100.0),
            _bar(100.0),
        ])
        bd.advance("X")

        order = OrderEvent(symbol="X", direction=Direction.LONG, quantity=100)
        broker.submit_order(order)

        bd.advance("X")
        fills = broker.fill_orders(bd)
        assert len(fills) == 1
        # Should use slippage model (1%) not slippage_rate (0%)
        assert fills[0].fill_price == pytest.approx(101.0)

    def test_broker_backward_compat(self):
        """Without slippage_model, uses slippage_rate as before."""
        broker = SimulatedBroker(slippage_rate=0.01)

        bd = BarData()
        bd.add_symbol_bars("X", [
            _bar(100.0),
            _bar(100.0),
        ])
        bd.advance("X")

        order = OrderEvent(symbol="X", direction=Direction.LONG, quantity=100)
        broker.submit_order(order)

        bd.advance("X")
        fills = broker.fill_orders(bd)
        assert fills[0].fill_price == pytest.approx(101.0)
