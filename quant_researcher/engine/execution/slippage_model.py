"""
SlippageModel — 可插拔的滑点模型。

内置模型:
- FixedRateSlippage: 固定比例滑点（默认，向后兼容）
- VolumeImpactSlippage: 成交量冲击模型，滑点随订单占成交量比例增大
- ZeroSlippage: 零滑点
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from quant_researcher.engine.core.bar_data import Bar
from quant_researcher.engine.core.event import Direction


class SlippageModel(ABC):
    """滑点模型基类。"""

    @abstractmethod
    def calculate(self, price: float, direction: Direction, bar: Bar, quantity: int) -> float:
        """
        计算滑点后的成交价。

        Args:
            price: 基准价格（通常是 bar.open 或 stop/limit price）
            direction: 交易方向
            bar: 当前 bar 数据（可用 volume 等信息）
            quantity: 成交数量

        Returns:
            滑点后的成交价
        """
        ...


class FixedRateSlippage(SlippageModel):
    """
    固定比例滑点。

    买入: price * (1 + rate)
    卖出: price * (1 - rate)

    与原有 broker.slippage_rate 行为一致。
    """

    def __init__(self, rate: float = 0.0005) -> None:
        self.rate = rate

    def calculate(self, price: float, direction: Direction, bar: Bar, quantity: int) -> float:
        if direction == Direction.LONG:
            return price * (1 + self.rate)
        else:
            return price * (1 - self.rate)


class VolumeImpactSlippage(SlippageModel):
    """
    成交量冲击滑点模型 (Square-Root Market Impact)。

    基于经典的 Almgren-Chriss 市场冲击模型简化版:
    slippage = base_rate + impact_factor * sqrt(quantity / volume)

    - base_rate: 最小固定滑点（bid-ask spread 的一半）
    - impact_factor: 冲击系数，控制大单对价格的影响程度
    - quantity / volume: 订单占当日成交量的比例（participation rate）

    参数说明:
    - base_rate=0.0001 (0.01%): 最小 spread 成本
    - impact_factor=0.1 (10%): 典型大盘股的冲击系数
      小盘股/低流动性标的应使用更大的值 (0.2~0.5)

    示例:
    - 订单=1000股, 日成交量=1M: sqrt(0.001) ≈ 0.032 → 0.1 * 0.032 = 0.32%
    - 订单=10000股, 日成交量=1M: sqrt(0.01) ≈ 0.1 → 0.1 * 0.1 = 1.0%
    - 订单=100股, 日成交量=1M: sqrt(0.0001) = 0.01 → 0.1 * 0.01 = 0.1%
    """

    def __init__(
        self,
        base_rate: float = 0.0001,
        impact_factor: float = 0.1,
    ) -> None:
        self.base_rate = base_rate
        self.impact_factor = impact_factor

    def calculate(self, price: float, direction: Direction, bar: Bar, quantity: int) -> float:
        volume = bar.volume if bar.volume and bar.volume > 0 else 1_000_000
        participation = quantity / volume
        impact = self.base_rate + self.impact_factor * (participation ** 0.5)

        if direction == Direction.LONG:
            return price * (1 + impact)
        else:
            return price * (1 - impact)


class ZeroSlippage(SlippageModel):
    """零滑点。"""

    def calculate(self, price: float, direction: Direction, bar: Bar, quantity: int) -> float:
        return price
