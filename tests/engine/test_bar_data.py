"""BarData 测试 — 重点验证防 look-ahead bias。"""

import numpy as np

from quant_researcher.engine.core.bar_data import BarData
from tests.engine.helpers import advance_all, make_bar_data, make_bars


class TestBar:
    def test_repr(self):
        bars = make_bars("AAPL", [150.0])
        r = repr(bars[0])
        assert "AAPL" in r
        assert "150.00" in r

    def test_frozen(self):
        bar = make_bars("AAPL", [100.0])[0]
        try:
            bar.close = 999.0  # type: ignore
            raise AssertionError("Should be frozen")
        except AttributeError:
            pass


class TestBarDataAdvance:
    def test_advance_returns_bars_in_order(self):
        prices = [10.0, 20.0, 30.0]
        bd = make_bar_data("X", prices)
        results = []
        for _ in range(3):
            b = bd.advance("X")
            assert b is not None
            results.append(b.close)
        assert results == prices

    def test_advance_returns_none_at_end(self):
        bd = make_bar_data("X", [10.0, 20.0])
        bd.advance("X")
        bd.advance("X")
        assert bd.advance("X") is None

    def test_advance_unknown_symbol(self):
        bd = BarData()
        assert bd.advance("UNKNOWN") is None


class TestBarDataCurrent:
    def test_current_before_advance(self):
        bd = make_bar_data("X", [10.0])
        assert bd.current("X") is None

    def test_current_after_advance(self):
        bd = make_bar_data("X", [10.0, 20.0])
        bd.advance("X")
        assert bd.current("X") is not None
        assert bd.current("X").close == 10.0


class TestBarDataHistory:
    def test_history_returns_correct_window(self):
        prices = [10.0, 20.0, 30.0, 40.0, 50.0]
        bd = make_bar_data("X", prices)
        advance_all(bd)  # 推到最后一根
        h = bd.history("X", "close", 3)
        np.testing.assert_array_equal(h, [30.0, 40.0, 50.0])

    def test_history_at_start_returns_fewer_bars(self):
        """开头数据不足时，返回已有的部分。"""
        bd = make_bar_data("X", [10.0, 20.0, 30.0])
        bd.advance("X")  # 只推进 1 步
        h = bd.history("X", "close", 5)  # 请求 5 根但只有 1 根
        assert len(h) == 1
        assert h[0] == 10.0

    def test_history_before_advance_returns_empty(self):
        bd = make_bar_data("X", [10.0])
        h = bd.history("X", "close", 5)
        assert len(h) == 0

    def test_history_different_fields(self):
        bd = make_bar_data("X", [100.0])
        bd.advance("X")
        opens = bd.history("X", "open", 1)
        highs = bd.history("X", "high", 1)
        lows = bd.history("X", "low", 1)
        # open = close - 0.5, high = close + 1.0, low = close - 1.0
        assert opens[0] == 99.5
        assert highs[0] == 101.0
        assert lows[0] == 99.0

    def test_no_look_ahead_bias(self):
        """history() 不应返回还未 advance 到的未来数据。"""
        prices = [10.0, 20.0, 30.0, 40.0, 50.0]
        bd = make_bar_data("X", prices)
        bd.advance("X")  # idx=0, close=10
        bd.advance("X")  # idx=1, close=20
        h = bd.history("X", "close", 100)  # 请求100根
        assert len(h) == 2  # 只返回到 idx=1
        np.testing.assert_array_equal(h, [10.0, 20.0])


class TestBarDataHasEnoughBars:
    def test_not_enough(self):
        bd = make_bar_data("X", [10.0, 20.0])
        bd.advance("X")
        assert bd.has_enough_bars("X", 2) is False  # 只有1根

    def test_exactly_enough(self):
        bd = make_bar_data("X", [10.0, 20.0])
        bd.advance("X")
        bd.advance("X")
        assert bd.has_enough_bars("X", 2) is True

    def test_more_than_enough(self):
        bd = make_bar_data("X", [10.0, 20.0, 30.0])
        advance_all(bd)
        assert bd.has_enough_bars("X", 2) is True


class TestBarDataMultiSymbol:
    def test_two_symbols_independent(self):
        bd = BarData()
        bd.add_symbol_bars("A", make_bars("A", [10.0, 20.0]))
        bd.add_symbol_bars("B", make_bars("B", [100.0, 200.0]))
        assert set(bd.symbols) == {"A", "B"}

        bd.advance("A")
        assert bd.current("A").close == 10.0
        assert bd.current("B") is None  # B 还没推进

        bd.advance("B")
        assert bd.current("B").close == 100.0
