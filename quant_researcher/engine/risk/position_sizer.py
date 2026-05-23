"""
Position Sizing — 仓位管理。

提供多种仓位计算策略:
- FixedFractionSizer: 固定比例（每次交易风险固定百分比的总资金）
- ATRSizer: 基于 ATR 的仓位计算（类似海龟交易法）
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from quant_researcher.engine.core.bar_data import BarData
from quant_researcher.engine.indicators.volatility import atr


class PositionSizer(ABC):
    """仓位计算基类。"""

    @abstractmethod
    def calculate(
        self,
        symbol: str,
        equity: float,
        price: float,
        bar_data: BarData,
    ) -> int:
        """
        计算应该交易的股数。

        Args:
            symbol: 标的代码
            equity: 当前总资产
            price: 当前价格
            bar_data: 行情数据（用于计算波动率等）

        Returns:
            建议交易的股数（整数）
        """
        ...


class FixedFractionSizer(PositionSizer):
    """
    固定比例仓位管理。

    每次交易使用总资产的固定比例。
    例如 fraction=0.1 表示每次用 10% 的资产建仓。

    如果提供了 stop_distance（止损距离占价格的比例），
    则按照风险金额计算: position = risk_amount / (price * stop_distance)
    """

    def __init__(
        self,
        fraction: float = 0.1,
        stop_distance: float | None = None,
        max_position_pct: float = 0.25,
    ) -> None:
        self.fraction = fraction
        self.stop_distance = stop_distance
        self.max_position_pct = max_position_pct

    def calculate(
        self,
        symbol: str,
        equity: float,
        price: float,
        bar_data: BarData,
    ) -> int:
        if price <= 0:
            return 0

        if self.stop_distance and self.stop_distance > 0:
            # 基于风险的仓位: risk_amount / 每股风险
            risk_amount = equity * self.fraction
            per_share_risk = price * self.stop_distance
            qty = int(risk_amount / per_share_risk)
        else:
            # 简单固定比例
            position_value = equity * self.fraction
            qty = int(position_value / price)

        # 不超过最大仓位限制
        max_qty = int(equity * self.max_position_pct / price)
        return min(qty, max_qty)


class ATRSizer(PositionSizer):
    """
    基于 ATR 的仓位计算（海龟交易法风格）。

    核心思想: 每次交易承担固定风险（总资产的 risk_pct），
    用 ATR 作为每股风险的度量。

    position = (equity * risk_pct) / (atr_value * atr_multiplier)
    """

    def __init__(
        self,
        risk_pct: float = 0.01,
        atr_period: int = 20,
        atr_multiplier: float = 2.0,
        max_position_pct: float = 0.25,
    ) -> None:
        self.risk_pct = risk_pct
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        self.max_position_pct = max_position_pct

    def calculate(
        self,
        symbol: str,
        equity: float,
        price: float,
        bar_data: BarData,
    ) -> int:
        if price <= 0:
            return 0

        # 获取历史数据计算 ATR
        highs = bar_data.history(symbol, "high", self.atr_period + 1)
        lows = bar_data.history(symbol, "low", self.atr_period + 1)
        closes = bar_data.history(symbol, "close", self.atr_period + 1)

        if highs is None or len(highs) < self.atr_period + 1:
            # 数据不够，回退到简单计算
            return int(equity * self.risk_pct / price)

        atr_values = atr(highs, lows, closes, self.atr_period)
        current_atr = atr_values[-1]

        if current_atr <= 0 or current_atr != current_atr:  # NaN check
            return int(equity * self.risk_pct / price)

        risk_amount = equity * self.risk_pct
        per_share_risk = current_atr * self.atr_multiplier
        qty = int(risk_amount / per_share_risk)

        max_qty = int(equity * self.max_position_pct / price)
        return max(min(qty, max_qty), 0)
