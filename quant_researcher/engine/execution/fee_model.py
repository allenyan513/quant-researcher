"""
手续费模型 — 可插拔的手续费计算。

内置模型:
- ZeroFeeModel: 零手续费
- PerShareFeeModel: 按股数收费 (IB 模式: $0.005/股, 最低 $1, 最高 0.5%)
- PercentageFeeModel: 按成交金额比例收费
- TieredFeeModel: 按月成交量阶梯费率 (IB Tiered 模式)
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class FeeModel(ABC):
    """手续费模型基类。"""

    @abstractmethod
    def calculate(self, fill_price: float, quantity: int) -> float:
        """
        计算手续费。

        Args:
            fill_price: 成交价格
            quantity: 成交数量

        Returns:
            手续费金额 (≥ 0)
        """
        ...


class ZeroFeeModel(FeeModel):
    """零手续费。"""

    def calculate(self, fill_price: float, quantity: int) -> float:
        return 0.0


class PerShareFeeModel(FeeModel):
    """
    按股数收费 (Interactive Brokers Fixed 模式)。

    默认参数:
    - per_share: $0.005/股
    - min_fee: $1.00 每笔最低
    - max_pct: 0.5% 每笔最高 (占成交金额)
    """

    def __init__(
        self,
        per_share: float = 0.005,
        min_fee: float = 1.0,
        max_pct: float = 0.005,
    ) -> None:
        self.per_share = per_share
        self.min_fee = min_fee
        self.max_pct = max_pct

    def calculate(self, fill_price: float, quantity: int) -> float:
        raw = quantity * self.per_share
        trade_value = fill_price * quantity
        max_fee = trade_value * self.max_pct
        return max(self.min_fee, min(raw, max_fee))


class PercentageFeeModel(FeeModel):
    """按成交金额比例收费。"""

    def __init__(self, rate: float = 0.001) -> None:
        self.rate = rate

    def calculate(self, fill_price: float, quantity: int) -> float:
        return fill_price * quantity * self.rate


class TieredFeeModel(FeeModel):
    """
    按月成交量阶梯费率 (Interactive Brokers Tiered 模式)。

    IB US Equities Tiered 费率 (2024):
    - ≤ 300,000 股/月: $0.0035/股
    - 300,001 ~ 3,000,000: $0.0020/股
    - 3,000,001 ~ 20,000,000: $0.0015/股
    - > 20,000,000: $0.0010/股

    每笔最低 $0.35，最高成交额的 1%。
    含 Exchange + Clearing + 监管费。

    tiers 格式: [(volume_threshold, per_share_rate), ...]
    按 volume_threshold 升序排列，最后一档 threshold 为 inf。
    """

    # IB US Tiered 默认费率
    DEFAULT_TIERS: list[tuple[int, float]] = [
        (300_000, 0.0035),
        (3_000_000, 0.0020),
        (20_000_000, 0.0015),
        (float("inf"), 0.0010),
    ]

    def __init__(
        self,
        tiers: list[tuple[int | float, float]] | None = None,
        min_fee: float = 0.35,
        max_pct: float = 0.01,
    ) -> None:
        self.tiers = tiers or self.DEFAULT_TIERS
        self.min_fee = min_fee
        self.max_pct = max_pct
        self._monthly_volume: int = 0

    def calculate(self, fill_price: float, quantity: int) -> float:
        # 确定当前阶梯费率
        rate = self.tiers[-1][1]  # default to last tier
        for threshold, tier_rate in self.tiers:
            if self._monthly_volume <= threshold:
                rate = tier_rate
                break

        raw = quantity * rate
        trade_value = fill_price * quantity
        max_fee = trade_value * self.max_pct

        fee = max(self.min_fee, min(raw, max_fee))

        # 更新月度成交量
        self._monthly_volume += quantity

        return fee

    def reset_monthly_volume(self) -> None:
        """月初重置成交量计数。"""
        self._monthly_volume = 0

    @property
    def monthly_volume(self) -> int:
        return self._monthly_volume
