"""Risk management module tests: PositionSizer + StopManager."""

from datetime import datetime, timedelta

from quant_researcher.engine.core.bar_data import Bar, BarData
from quant_researcher.engine.core.event import Direction, OrderType
from quant_researcher.engine.risk.position_sizer import ATRSizer, FixedFractionSizer
from quant_researcher.engine.risk.stop_manager import StopManager
from tests.engine.helpers import advance_all, make_bar_data

# ---------------------------------------------------------------------------
# PositionSizer tests
# ---------------------------------------------------------------------------

class TestFixedFractionSizer:
    def test_simple_fraction(self):
        sizer = FixedFractionSizer(fraction=0.1)
        bd = make_bar_data("X", [100.0])
        qty = sizer.calculate("X", equity=100_000, price=50.0, bar_data=bd)
        # 10% of 100k = 10k, 10k / 50 = 200
        assert qty == 200

    def test_max_position_cap(self):
        sizer = FixedFractionSizer(fraction=0.5, max_position_pct=0.25)
        bd = make_bar_data("X", [100.0])
        qty = sizer.calculate("X", equity=100_000, price=50.0, bar_data=bd)
        # 50% = 1000 shares, but max 25% = 500 shares
        assert qty == 500

    def test_with_stop_distance(self):
        sizer = FixedFractionSizer(fraction=0.02, stop_distance=0.05, max_position_pct=0.5)
        bd = make_bar_data("X", [100.0])
        qty = sizer.calculate("X", equity=100_000, price=100.0, bar_data=bd)
        # risk = 2% of 100k = 2000, per_share_risk = 100 * 0.05 = 5
        # qty = 2000 / 5 = 400, max = 50% of 100k / 100 = 500
        assert qty == 400

    def test_zero_price(self):
        sizer = FixedFractionSizer(fraction=0.1)
        bd = make_bar_data("X", [100.0])
        assert sizer.calculate("X", equity=100_000, price=0.0, bar_data=bd) == 0

    def test_fractional_shares_truncated(self):
        sizer = FixedFractionSizer(fraction=0.1)
        bd = make_bar_data("X", [100.0])
        qty = sizer.calculate("X", equity=100_000, price=33.33, bar_data=bd)
        assert isinstance(qty, int)


class TestATRSizer:
    def _make_volatile_bar_data(self, symbol="X", n=30):
        """Create bar data with enough bars for ATR calculation."""
        bd = BarData()
        bars = []
        dt = datetime(2024, 1, 1)
        for i in range(n):
            price = 100.0 + (i % 5) * 2  # oscillate
            bars.append(Bar(
                symbol=symbol,
                timestamp=dt + timedelta(days=i),
                open=price - 1,
                high=price + 3,
                low=price - 3,
                close=price,
                volume=1_000_000,
            ))
        bd.add_symbol_bars(symbol, bars)
        # advance all bars
        for _ in range(n):
            bd.advance(symbol)
        return bd

    def test_atr_sizer_produces_positive_qty(self):
        sizer = ATRSizer(risk_pct=0.01, atr_period=14)
        bd = self._make_volatile_bar_data(n=30)
        qty = sizer.calculate("X", equity=100_000, price=100.0, bar_data=bd)
        assert qty > 0
        assert isinstance(qty, int)

    def test_atr_sizer_respects_max_position(self):
        sizer = ATRSizer(risk_pct=0.5, atr_period=14, max_position_pct=0.1)
        bd = self._make_volatile_bar_data(n=30)
        qty = sizer.calculate("X", equity=100_000, price=100.0, bar_data=bd)
        max_qty = int(100_000 * 0.1 / 100.0)
        assert qty <= max_qty

    def test_atr_sizer_fallback_insufficient_data(self):
        sizer = ATRSizer(risk_pct=0.01, atr_period=20)
        bd = make_bar_data("X", [100.0, 101.0, 102.0])  # only 3 bars
        advance_all(bd)
        qty = sizer.calculate("X", equity=100_000, price=100.0, bar_data=bd)
        # Fallback: equity * risk_pct / price = 100000 * 0.01 / 100 = 10
        assert qty == 10


# ---------------------------------------------------------------------------
# StopManager tests
# ---------------------------------------------------------------------------

def _bar_data_with_ohlc(symbol, bars_ohlc):
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


class TestFixedStopLoss:
    def test_long_stop_loss_triggers(self):
        sm = StopManager()
        sm.add_fixed_stop("X", Direction.LONG, quantity=100, stop_loss=95.0)
        bd = _bar_data_with_ohlc("X", [(100, 101, 94, 95)])
        bd.advance("X")

        orders = sm.check(bd)
        assert len(orders) == 1
        assert orders[0].direction == Direction.SHORT  # exit long
        assert orders[0].quantity == 100
        assert orders[0].stop_price == 95.0
        assert sm.active_stop_count == 0  # removed after trigger

    def test_long_stop_loss_not_triggered(self):
        sm = StopManager()
        sm.add_fixed_stop("X", Direction.LONG, quantity=100, stop_loss=95.0)
        bd = _bar_data_with_ohlc("X", [(100, 105, 96, 102)])
        bd.advance("X")

        orders = sm.check(bd)
        assert len(orders) == 0
        assert sm.active_stop_count == 1

    def test_short_stop_loss_triggers(self):
        sm = StopManager()
        sm.add_fixed_stop("X", Direction.SHORT, quantity=50, stop_loss=105.0)
        bd = _bar_data_with_ohlc("X", [(100, 106, 99, 104)])
        bd.advance("X")

        orders = sm.check(bd)
        assert len(orders) == 1
        assert orders[0].direction == Direction.LONG  # exit short


class TestFixedTakeProfit:
    def test_long_take_profit_triggers(self):
        sm = StopManager()
        sm.add_fixed_stop("X", Direction.LONG, quantity=100, take_profit=110.0)
        bd = _bar_data_with_ohlc("X", [(100, 112, 99, 111)])
        bd.advance("X")

        orders = sm.check(bd)
        assert len(orders) == 1
        assert orders[0].direction == Direction.SHORT
        assert orders[0].order_type == OrderType.LIMIT
        assert orders[0].limit_price == 110.0

    def test_take_profit_not_triggered(self):
        sm = StopManager()
        sm.add_fixed_stop("X", Direction.LONG, quantity=100, take_profit=110.0)
        bd = _bar_data_with_ohlc("X", [(100, 109, 99, 108)])
        bd.advance("X")

        orders = sm.check(bd)
        assert len(orders) == 0


class TestStopLossPriority:
    def test_stop_loss_before_take_profit(self):
        """If both trigger on same bar, stop loss takes priority."""
        sm = StopManager()
        sm.add_fixed_stop("X", Direction.LONG, quantity=100,
                          stop_loss=95.0, take_profit=110.0)
        # Both would trigger: low=94 < 95 (SL), high=111 > 110 (TP)
        bd = _bar_data_with_ohlc("X", [(100, 111, 94, 100)])
        bd.advance("X")

        orders = sm.check(bd)
        # SL triggers first in the code
        assert len(orders) == 1
        assert orders[0].stop_price == 95.0


class TestTrailingStop:
    def test_trailing_stop_triggers_on_wide_bar(self):
        """High updates trailing stop, then low triggers it in the same bar."""
        sm = StopManager()
        sm.add_trailing_stop(
            "X", Direction.LONG, quantity=100,
            trail_pct=0.05, initial_price=100.0,
        )

        # Bar: high=110 → stop moves to 104.5, low=99 < 104.5 → triggers
        bd = _bar_data_with_ohlc("X", [(100, 110, 99, 108)])
        bd.advance("X")
        orders = sm.check(bd)
        assert len(orders) == 1  # correctly triggers

    def test_trailing_stop_follows_price_up(self):
        sm = StopManager()
        sm.add_trailing_stop(
            "X", Direction.LONG, quantity=100,
            trail_points=5.0, initial_price=100.0,
        )

        # Bar 1: high=105, stop at 100 (105-5)
        bd = _bar_data_with_ohlc("X", [
            (100, 105, 101, 104),
            (104, 110, 103, 109),
        ])
        bd.advance("X")
        orders = sm.check(bd)
        assert len(orders) == 0  # low=101 > stop=100

        # Bar 2: high=110, new stop at 105, low=103 > 105? No, 103 < 105 → triggers
        bd.advance("X")
        orders = sm.check(bd)
        assert len(orders) == 1

    def test_trailing_stop_short(self):
        sm = StopManager()
        sm.add_trailing_stop(
            "X", Direction.SHORT, quantity=100,
            trail_pct=0.05, initial_price=100.0,
        )

        # Bar: low=95, new lowest=95, stop = 95 * 1.05 = 99.75, high=98 < 99.75 → no trigger
        bd = _bar_data_with_ohlc("X", [(97, 98, 95, 96)])
        bd.advance("X")
        orders = sm.check(bd)
        assert len(orders) == 0

    def test_remove_stops(self):
        sm = StopManager()
        sm.add_fixed_stop("X", Direction.LONG, 100, stop_loss=95.0)
        sm.add_trailing_stop("X", Direction.LONG, 100, trail_pct=0.05, initial_price=100.0)
        assert sm.active_stop_count == 2

        sm.remove_stops("X")
        assert sm.active_stop_count == 0
