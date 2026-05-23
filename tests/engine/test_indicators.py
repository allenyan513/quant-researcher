"""指标库测试。"""

import numpy as np
import pytest

from quant_researcher.engine.indicators import atr, bollinger, donchian, ema, macd, rsi, sma

# =========================================================================
# SMA
# =========================================================================

class TestSMA:
    def test_basic(self):
        data = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = sma(data, 3)
        assert np.isnan(result[0])
        assert np.isnan(result[1])
        assert result[2] == pytest.approx(2.0)  # (1+2+3)/3
        assert result[3] == pytest.approx(3.0)  # (2+3+4)/3
        assert result[4] == pytest.approx(4.0)  # (3+4+5)/3

    def test_period_equals_length(self):
        data = np.array([10.0, 20.0, 30.0])
        result = sma(data, 3)
        assert result[2] == pytest.approx(20.0)

    def test_period_1(self):
        data = np.array([5.0, 10.0, 15.0])
        result = sma(data, 1)
        np.testing.assert_array_almost_equal(result, data)

    def test_data_shorter_than_period(self):
        data = np.array([1.0, 2.0])
        result = sma(data, 5)
        assert all(np.isnan(result))

    def test_constant_data(self):
        data = np.full(10, 42.0)
        result = sma(data, 5)
        assert result[4] == pytest.approx(42.0)
        assert result[9] == pytest.approx(42.0)


# =========================================================================
# EMA
# =========================================================================

class TestEMA:
    def test_first_value_equals_sma(self):
        data = np.array([2.0, 4.0, 6.0, 8.0, 10.0])
        result = ema(data, 3)
        # EMA 起始值 = 前3个的 SMA = (2+4+6)/3 = 4.0
        assert result[2] == pytest.approx(4.0)

    def test_ema_reacts_faster_than_sma(self):
        """EMA 对最新数据反应更快 — 突然涨价时 EMA > SMA。"""
        data = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 20.0, 20.0, 20.0])
        ema_result = ema(data, 5)
        sma_result = sma(data, 5)
        # 价格跳升后，EMA 应比 SMA 更接近新价格
        assert ema_result[7] > sma_result[7]

    def test_nan_before_period(self):
        data = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = ema(data, 3)
        assert np.isnan(result[0])
        assert np.isnan(result[1])
        assert not np.isnan(result[2])

    def test_constant_data(self):
        data = np.full(10, 50.0)
        result = ema(data, 5)
        assert result[9] == pytest.approx(50.0)

    def test_data_shorter_than_period(self):
        data = np.array([1.0, 2.0])
        result = ema(data, 5)
        assert all(np.isnan(result))


# =========================================================================
# MACD
# =========================================================================

class TestMACD:
    def test_output_shapes(self):
        data = np.random.randn(100) + 100
        result = macd(data, 12, 26, 9)
        assert len(result.macd_line) == 100
        assert len(result.signal_line) == 100
        assert len(result.histogram) == 100

    def test_macd_line_is_fast_minus_slow(self):
        data = np.arange(50, dtype=float)
        result = macd(data, 5, 10, 3)
        fast = ema(data, 5)
        slow = ema(data, 10)
        # 从 slow 有值的位置开始比较
        for i in range(9, 50):
            assert result.macd_line[i] == pytest.approx(fast[i] - slow[i])

    def test_uptrend_positive_macd(self):
        """持续上涨 → MACD 线应为正。"""
        data = np.arange(1, 51, dtype=float)
        result = macd(data, 12, 26, 9)
        # 最后几个值应为正
        assert result.macd_line[-1] > 0

    def test_histogram_is_macd_minus_signal(self):
        data = np.arange(50, dtype=float)
        result = macd(data, 12, 26, 9)
        # 在两者都有值的地方
        for i in range(len(data)):
            if not np.isnan(result.macd_line[i]) and not np.isnan(result.signal_line[i]):
                assert result.histogram[i] == pytest.approx(
                    result.macd_line[i] - result.signal_line[i]
                )


# =========================================================================
# RSI
# =========================================================================

class TestRSI:
    def test_all_gains(self):
        """连续上涨 → RSI 接近 100。"""
        data = np.arange(1, 20, dtype=float)
        result = rsi(data, 14)
        assert result[-1] == pytest.approx(100.0)

    def test_all_losses(self):
        """连续下跌 → RSI 接近 0。"""
        data = np.arange(20, 1, -1, dtype=float)
        result = rsi(data, 14)
        assert result[-1] == pytest.approx(0.0, abs=0.1)

    def test_range(self):
        """RSI 应在 0-100 之间。"""
        np.random.seed(42)
        data = np.cumsum(np.random.randn(100)) + 100
        result = rsi(data, 14)
        valid = result[~np.isnan(result)]
        assert all(0 <= v <= 100 for v in valid)

    def test_nan_before_period(self):
        data = np.arange(1, 20, dtype=float)
        result = rsi(data, 14)
        assert all(np.isnan(result[:14]))
        assert not np.isnan(result[14])

    def test_equal_gains_losses(self):
        """交替涨跌等幅 → RSI ≈ 50。"""
        data = np.array([100.0] + [100 + (1 if i % 2 == 0 else -1) for i in range(30)])
        result = rsi(data, 14)
        valid = result[~np.isnan(result)]
        assert 45.0 < valid[-1] < 55.0

    def test_data_too_short(self):
        data = np.array([1.0, 2.0, 3.0])
        result = rsi(data, 14)
        assert all(np.isnan(result))


# =========================================================================
# ATR
# =========================================================================

class TestATR:
    def test_basic(self):
        """简单情况: 无跳空时 ATR ≈ high - low。"""
        n = 20
        high = np.full(n, 110.0)
        low = np.full(n, 90.0)
        close = np.full(n, 100.0)
        result = atr(high, low, close, period=14)
        # TR = high - low = 20 for all bars, so ATR = 20
        assert result[-1] == pytest.approx(20.0)

    def test_gap_up_increases_atr(self):
        """跳空高开 → ATR 应增大。"""
        n = 20
        high = np.full(n, 110.0)
        low = np.full(n, 90.0)
        close = np.full(n, 100.0)
        result_no_gap = atr(high, low, close, 14)

        # 制造跳空: 最后一根 bar 的 high 远高于前一根 close
        high2 = high.copy()
        high2[-1] = 150.0
        close2 = close.copy()
        close2[-2] = 100.0
        result_gap = atr(high2, low, close2, 14)

        assert result_gap[-1] > result_no_gap[-1]

    def test_nan_before_period(self):
        n = 20
        high = np.full(n, 110.0)
        low = np.full(n, 90.0)
        close = np.full(n, 100.0)
        result = atr(high, low, close, period=14)
        assert all(np.isnan(result[:13]))
        assert not np.isnan(result[13])

    def test_single_bar(self):
        result = atr(np.array([110.0]), np.array([90.0]), np.array([100.0]), 14)
        assert all(np.isnan(result))


# =========================================================================
# Bollinger Bands
# =========================================================================

class TestBollinger:
    def test_middle_equals_sma(self):
        data = np.arange(1, 31, dtype=float)
        result = bollinger(data, period=20, num_std=2.0)
        sma_result = sma(data, 20)
        for i in range(19, 30):
            assert result.middle[i] == pytest.approx(sma_result[i])

    def test_upper_above_lower(self):
        np.random.seed(42)
        data = np.cumsum(np.random.randn(50)) + 100
        result = bollinger(data, 20, 2.0)
        for i in range(19, 50):
            assert result.upper[i] > result.lower[i]

    def test_constant_data_bands_collapse(self):
        """常数数据 → 标准差为0 → 上轨=中轨=下轨。"""
        data = np.full(30, 100.0)
        result = bollinger(data, 20, 2.0)
        assert result.upper[29] == pytest.approx(100.0)
        assert result.lower[29] == pytest.approx(100.0)

    def test_wider_std_wider_bands(self):
        np.random.seed(42)
        data = np.cumsum(np.random.randn(50)) + 100
        r2 = bollinger(data, 20, 2.0)
        r3 = bollinger(data, 20, 3.0)
        # 3 倍标准差的带宽应更宽
        width2 = r2.upper[29] - r2.lower[29]
        width3 = r3.upper[29] - r3.lower[29]
        assert width3 > width2

    def test_nan_before_period(self):
        data = np.arange(1, 31, dtype=float)
        result = bollinger(data, 20)
        assert np.isnan(result.upper[18])
        assert not np.isnan(result.upper[19])


# =========================================================================
# Donchian Channel
# =========================================================================

class TestDonchian:
    def test_upper_is_max_high(self):
        high = np.array([10.0, 12.0, 8.0, 15.0, 11.0])
        low = np.array([8.0, 9.0, 6.0, 12.0, 9.0])
        result = donchian(high, low, period=3)
        # idx=2: max(10,12,8) = 12
        assert result.upper[2] == pytest.approx(12.0)
        # idx=3: max(12,8,15) = 15
        assert result.upper[3] == pytest.approx(15.0)

    def test_lower_is_min_low(self):
        high = np.array([10.0, 12.0, 8.0, 15.0, 11.0])
        low = np.array([8.0, 9.0, 6.0, 12.0, 9.0])
        result = donchian(high, low, period=3)
        # idx=2: min(8,9,6) = 6
        assert result.lower[2] == pytest.approx(6.0)
        # idx=4: min(6,12,9) = 6
        assert result.lower[4] == pytest.approx(6.0)

    def test_middle_is_average(self):
        high = np.array([10.0, 12.0, 8.0, 15.0, 11.0])
        low = np.array([8.0, 9.0, 6.0, 12.0, 9.0])
        result = donchian(high, low, period=3)
        for i in range(2, 5):
            assert result.middle[i] == pytest.approx(
                (result.upper[i] + result.lower[i]) / 2
            )

    def test_nan_before_period(self):
        high = np.arange(1, 11, dtype=float)
        low = np.arange(0, 10, dtype=float)
        result = donchian(high, low, period=5)
        assert np.isnan(result.upper[3])
        assert not np.isnan(result.upper[4])

    def test_constant_data(self):
        high = np.full(10, 100.0)
        low = np.full(10, 90.0)
        result = donchian(high, low, period=5)
        assert result.upper[9] == pytest.approx(100.0)
        assert result.lower[9] == pytest.approx(90.0)
