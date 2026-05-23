"""
MarginModel — 保证金模型。

模拟真实券商的保证金要求:
- Reg T: 初始保证金 50%, 维持保证金 25% (FINRA 标准)
- Portfolio Margin: 基于风险的保证金 (更低要求, 通常 15-20%)
- 零保证金 (CashAccount): 不允许做空，买入需全额现金

保证金计算:
- 做多: initial_margin = position_value * initial_rate
- 做空: initial_margin = position_value * initial_rate + position_value (借入保证金)
- 维持保证金: position_value * maintenance_rate

Margin Call:
- 当账户权益 < 维持保证金时触发
- 需要追加资金或强制平仓
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from quant_researcher.engine.core.bar_data import BarData
from quant_researcher.engine.core.event import Direction, OrderEvent
from quant_researcher.engine.portfolio.portfolio import Portfolio


@dataclass
class MarginRequirement:
    """保证金要求。"""
    initial_margin: float       # 初始保证金 (开仓时需要)
    maintenance_margin: float   # 维持保证金 (持仓期间)


@dataclass
class MarginStatus:
    """账户保证金状态。"""
    equity: float                   # 账户权益 (净资产)
    total_margin_required: float    # 总维持保证金
    margin_excess: float            # 剩余保证金 (equity - margin_required)
    margin_call: bool               # 是否触发 margin call
    margin_ratio: float             # 保证金比率 (equity / margin_required)


class MarginModel(ABC):
    """保证金模型基类。"""

    @abstractmethod
    def calculate_requirement(
        self,
        symbol: str,
        quantity: int,
        price: float,
        direction: Direction,
    ) -> MarginRequirement:
        """
        计算单笔交易的保证金要求。

        Args:
            symbol: 标的代码
            quantity: 数量 (正数)
            price: 当前价格
            direction: 交易方向

        Returns:
            MarginRequirement
        """
        ...

    def check_margin_status(
        self,
        portfolio: Portfolio,
        bar_data: BarData,
    ) -> MarginStatus:
        """
        检查账户保证金状态。

        Returns:
            MarginStatus — 包含是否触发 margin call
        """
        equity = portfolio.equity
        total_maintenance = 0.0

        for symbol, pos in portfolio.positions.items():
            if pos.quantity == 0:
                continue
            bar = bar_data.current(symbol)
            if bar is None:
                continue

            price = bar.close
            direction = Direction.LONG if pos.quantity > 0 else Direction.SHORT
            req = self.calculate_requirement(symbol, abs(pos.quantity), price, direction)
            total_maintenance += req.maintenance_margin

        margin_excess = equity - total_maintenance
        margin_ratio = equity / total_maintenance if total_maintenance > 0 else float("inf")
        margin_call = equity < total_maintenance and total_maintenance > 0

        return MarginStatus(
            equity=equity,
            total_margin_required=total_maintenance,
            margin_excess=margin_excess,
            margin_call=margin_call,
            margin_ratio=margin_ratio,
        )

    def check_order(
        self,
        order: OrderEvent,
        portfolio: Portfolio,
        bar_data: BarData,
    ) -> tuple[bool, str]:
        """
        检查订单是否满足保证金要求。

        Returns:
            (approved, reason) — 是否通过, 拒绝原因
        """
        bar = bar_data.current(order.symbol)
        if bar is None:
            return True, ""

        price = bar.close
        req = self.calculate_requirement(
            order.symbol, order.quantity, price, order.direction,
        )

        # 当前可用保证金 = equity - 已占用维持保证金
        status = self.check_margin_status(portfolio, bar_data)
        available = status.margin_excess

        if req.initial_margin > available:
            return False, (
                f"Insufficient margin: need ${req.initial_margin:,.0f}, "
                f"available ${available:,.0f}"
            )

        return True, ""


class RegTMargin(MarginModel):
    """
    Regulation T 保证金 (FINRA 标准, 适用于大部分美国券商)。

    规则:
    - 做多初始保证金: 50% (即需要至少 50% 自有资金)
    - 做空初始保证金: 50% + 借入金额
    - 维持保证金: 做多 25%, 做空 30%
    - 日内交易 (Pattern Day Trader): 需要 $25,000 最低权益

    做空保证金计算:
    - 卖空 100 股 @ $50 = $5,000
    - 初始保证金 = $5,000 * 50% = $2,500 (冻结) + $5,000 (卖空所得, 存券商) = $7,500
    - 维持保证金 = $5,000 * 30% = $1,500

    简化: 这里只计算额外需要冻结的保证金金额，不模拟卖空所得的存管。
    """

    def __init__(
        self,
        initial_long: float = 0.50,
        initial_short: float = 0.50,
        maintenance_long: float = 0.25,
        maintenance_short: float = 0.30,
    ) -> None:
        self.initial_long = initial_long
        self.initial_short = initial_short
        self.maintenance_long = maintenance_long
        self.maintenance_short = maintenance_short

    def calculate_requirement(
        self,
        symbol: str,
        quantity: int,
        price: float,
        direction: Direction,
    ) -> MarginRequirement:
        position_value = abs(quantity * price)

        if direction == Direction.LONG:
            return MarginRequirement(
                initial_margin=position_value * self.initial_long,
                maintenance_margin=position_value * self.maintenance_long,
            )
        else:
            # 做空: 需要更高保证金
            return MarginRequirement(
                initial_margin=position_value * self.initial_short,
                maintenance_margin=position_value * self.maintenance_short,
            )


class PortfolioMargin(MarginModel):
    """
    Portfolio Margin (基于风险的保证金, 适用于高净值账户)。

    比 Reg T 更低的保证金要求:
    - 做多: 15% (vs Reg T 50%)
    - 做空: 15% (vs Reg T 50%)
    - 维持: 做多 10%, 做空 12%

    实际 Portfolio Margin 基于 OCC TIMS 或 SPAN 系统计算，
    考虑组合对冲效果。这里用固定比例简化。
    """

    def __init__(
        self,
        initial_rate: float = 0.15,
        maintenance_long: float = 0.10,
        maintenance_short: float = 0.12,
    ) -> None:
        self.initial_rate = initial_rate
        self.maintenance_long = maintenance_long
        self.maintenance_short = maintenance_short

    def calculate_requirement(
        self,
        symbol: str,
        quantity: int,
        price: float,
        direction: Direction,
    ) -> MarginRequirement:
        position_value = abs(quantity * price)

        if direction == Direction.LONG:
            return MarginRequirement(
                initial_margin=position_value * self.initial_rate,
                maintenance_margin=position_value * self.maintenance_long,
            )
        else:
            return MarginRequirement(
                initial_margin=position_value * self.initial_rate,
                maintenance_margin=position_value * self.maintenance_short,
            )


class CashAccount(MarginModel):
    """
    现金账户 — 不允许做空，买入需全额现金。

    规则:
    - 做多: 初始保证金 = 100% (全额), 维持 = 100%
    - 做空: 拒绝 (初始保证金 = inf)
    """

    def calculate_requirement(
        self,
        symbol: str,
        quantity: int,
        price: float,
        direction: Direction,
    ) -> MarginRequirement:
        position_value = abs(quantity * price)

        if direction == Direction.LONG:
            return MarginRequirement(
                initial_margin=position_value,
                maintenance_margin=position_value,
            )
        else:
            return MarginRequirement(
                initial_margin=float("inf"),
                maintenance_margin=float("inf"),
            )

    def check_order(
        self,
        order: OrderEvent,
        portfolio: Portfolio,
        bar_data: BarData,
    ) -> tuple[bool, str]:
        if order.direction == Direction.SHORT:
            # 允许平仓 (如果持有多头)
            pos_qty = portfolio.get_position_quantity(order.symbol)
            if pos_qty > 0:
                return True, ""
            return False, "CashAccount: short selling not allowed"

        return super().check_order(order, portfolio, bar_data)
