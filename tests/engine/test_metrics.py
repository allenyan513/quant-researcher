"""回测指标计算测试。"""

from datetime import datetime, timedelta

import pytest

from quant_researcher.engine.analytics.metrics import calculate_metrics
from quant_researcher.engine.portfolio.portfolio import Portfolio


def _build_portfolio_with_curve(equities: list[float]) -> Portfolio:
    """构造带有指定净值曲线的 Portfolio。"""
    p = Portfolio(initial_cash=equities[0])
    start = datetime(2024, 1, 1)
    for i, eq in enumerate(equities):
        p.equity_curve.append((start + timedelta(days=i), eq))
    return p


class TestTotalReturn:
    def test_positive_return(self):
        m = calculate_metrics(_build_portfolio_with_curve([100, 110]))
        assert m["total_return"] == pytest.approx(0.10)

    def test_negative_return(self):
        m = calculate_metrics(_build_portfolio_with_curve([100, 90]))
        assert m["total_return"] == pytest.approx(-0.10)

    def test_flat(self):
        m = calculate_metrics(_build_portfolio_with_curve([100, 100, 100]))
        assert m["total_return"] == pytest.approx(0.0)


class TestMaxDrawdown:
    def test_no_drawdown(self):
        """单调递增 → 回撤为 0。"""
        m = calculate_metrics(_build_portfolio_with_curve([100, 110, 120, 130]))
        assert m["max_drawdown"] == pytest.approx(0.0)

    def test_simple_drawdown(self):
        """100 → 200 → 150 → 回撤 25%。"""
        m = calculate_metrics(_build_portfolio_with_curve([100, 200, 150]))
        assert m["max_drawdown"] == pytest.approx(-0.25)

    def test_multiple_drawdowns(self):
        """取最大的那个。"""
        m = calculate_metrics(_build_portfolio_with_curve([100, 120, 100, 150, 100]))
        # 最大回撤: 150 → 100 = -33.3%
        assert m["max_drawdown"] == pytest.approx(-1 / 3, rel=1e-4)


class TestSharpeRatio:
    def test_constant_equity_zero_sharpe(self):
        m = calculate_metrics(_build_portfolio_with_curve([100, 100, 100, 100]))
        assert m["sharpe_ratio"] == 0.0

    def test_positive_sharpe(self):
        """稳定上涨 → 正 Sharpe。"""
        m = calculate_metrics(_build_portfolio_with_curve([100, 101, 102, 103, 104]))
        assert m["sharpe_ratio"] > 0


class TestWinRate:
    def test_all_up_days(self):
        m = calculate_metrics(_build_portfolio_with_curve([100, 101, 102, 103]))
        assert m["win_rate"] == pytest.approx(1.0)

    def test_all_down_days(self):
        m = calculate_metrics(_build_portfolio_with_curve([100, 99, 98, 97]))
        assert m["win_rate"] == pytest.approx(0.0)

    def test_mixed(self):
        m = calculate_metrics(_build_portfolio_with_curve([100, 101, 99, 102]))
        # 3 天: +1, -2, +3 → 2/3 win
        assert m["win_rate"] == pytest.approx(2 / 3, rel=1e-4)


class TestEdgeCases:
    def test_empty_portfolio(self):
        p = Portfolio()
        m = calculate_metrics(p)
        assert m == {}

    def test_single_point(self):
        p = _build_portfolio_with_curve([100])
        m = calculate_metrics(p)
        assert m == {}  # < 2 points
