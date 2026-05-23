"""
RiskManager — 组合级风控。

可插拔的风控模块，插入 engine 下单环节（策略下单 → 风控过滤 → broker 提交），支持:
- 全局最大回撤熔断: 净值回撤超过阈值后拒绝新开仓
- 单标的最大仓位限制: 限制单标的持仓占总资产的比例
- 下单前拦截/调整: 调整订单数量使其符合风控规则

与策略级 StopManager/PositionSizer 的区别:
- StopManager: 管理单笔交易的止损/止盈，触发后平仓
- PositionSizer: 建议单笔交易的仓位大小
- RiskManager: **组合级**，拦截/调整所有订单，确保整体风险可控
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from quant_researcher.engine.core.bar_data import BarData
from quant_researcher.engine.core.event import Direction, OrderEvent
from quant_researcher.engine.portfolio.portfolio import Portfolio


@dataclass
class RiskCheckResult:
    """风控检查结果。"""
    approved: bool
    adjusted_order: OrderEvent | None = None
    reason: str = ""


class RiskManager(ABC):
    """
    风控管理器基类。

    子类实现 check_order() 方法，引擎在提交订单给 broker 之前调用。
    """

    @abstractmethod
    def check_order(
        self,
        order: OrderEvent,
        portfolio: Portfolio,
        bar_data: BarData,
    ) -> RiskCheckResult:
        """
        检查订单是否通过风控。

        Args:
            order: 待提交的订单
            portfolio: 当前组合状态
            bar_data: 当前行情数据

        Returns:
            RiskCheckResult:
            - approved=True, adjusted_order=None: 原样通过
            - approved=True, adjusted_order=...: 调整后通过
            - approved=False: 拒绝，reason 说明原因
        """
        ...

    def on_bar(self, portfolio: Portfolio, bar_data: BarData) -> list[OrderEvent]:
        """
        每 bar 回调，可用于生成风控触发的平仓订单（如回撤熔断清仓）。

        默认不生成任何订单，子类可覆盖。

        Returns:
            需要立即提交的风控订单列表
        """
        return []


class CompositeRiskManager(RiskManager):
    """
    组合风控 — 串联多个 RiskManager，依次检查。

    任何一个 RiskManager 拒绝，则订单被拒。
    如果有调整，后续 RiskManager 看到的是调整后的订单。
    """

    def __init__(self, managers: list[RiskManager] | None = None) -> None:
        self._managers: list[RiskManager] = managers or []

    def add(self, manager: RiskManager) -> None:
        self._managers.append(manager)

    def check_order(
        self,
        order: OrderEvent,
        portfolio: Portfolio,
        bar_data: BarData,
    ) -> RiskCheckResult:
        current = order
        for mgr in self._managers:
            result = mgr.check_order(current, portfolio, bar_data)
            if not result.approved:
                return result
            if result.adjusted_order is not None:
                current = result.adjusted_order
        if current is not order:
            return RiskCheckResult(approved=True, adjusted_order=current)
        return RiskCheckResult(approved=True)

    def on_bar(self, portfolio: Portfolio, bar_data: BarData) -> list[OrderEvent]:
        orders: list[OrderEvent] = []
        for mgr in self._managers:
            orders.extend(mgr.on_bar(portfolio, bar_data))
        return orders


class MaxDrawdownBreaker(RiskManager):
    """
    全局最大回撤熔断。

    当组合净值从峰值回撤超过 max_drawdown 时:
    - 拒绝所有新开仓订单
    - 可选: 触发全部清仓 (liquidate=True)

    平仓订单始终放行。
    """

    def __init__(
        self,
        max_drawdown: float = 0.20,
        liquidate: bool = False,
    ) -> None:
        self.max_drawdown = max_drawdown
        self.liquidate = liquidate
        self._peak_equity: float = 0.0
        self._breaker_triggered: bool = False
        self._liquidation_done: bool = False

    @property
    def is_triggered(self) -> bool:
        return self._breaker_triggered

    def check_order(
        self,
        order: OrderEvent,
        portfolio: Portfolio,
        bar_data: BarData,
    ) -> RiskCheckResult:
        self._update_state(portfolio)

        if not self._breaker_triggered:
            return RiskCheckResult(approved=True)

        # 熔断后: 只允许平仓方向的订单
        pos_qty = portfolio.get_position_quantity(order.symbol)
        is_reducing = (
            (pos_qty > 0 and order.direction == Direction.SHORT) or
            (pos_qty < 0 and order.direction == Direction.LONG)
        )

        if is_reducing:
            return RiskCheckResult(approved=True)

        return RiskCheckResult(
            approved=False,
            reason=f"MaxDrawdownBreaker: drawdown breaker triggered "
                   f"(peak={self._peak_equity:.0f}, "
                   f"current={portfolio.equity:.0f}, "
                   f"limit={self.max_drawdown:.1%})",
        )

    def on_bar(self, portfolio: Portfolio, bar_data: BarData) -> list[OrderEvent]:
        self._update_state(portfolio)

        if not self._breaker_triggered or not self.liquidate or self._liquidation_done:
            return []

        # 生成清仓订单
        orders: list[OrderEvent] = []
        for symbol, pos in portfolio.positions.items():
            if pos.quantity > 0:
                orders.append(OrderEvent(
                    symbol=symbol,
                    direction=Direction.SHORT,
                    quantity=pos.quantity,
                ))
            elif pos.quantity < 0:
                orders.append(OrderEvent(
                    symbol=symbol,
                    direction=Direction.LONG,
                    quantity=abs(pos.quantity),
                ))

        if orders:
            self._liquidation_done = True
        return orders

    def _update_state(self, portfolio: Portfolio) -> None:
        equity = portfolio.equity
        if equity > self._peak_equity:
            self._peak_equity = equity
        if self._peak_equity > 0:
            dd = (self._peak_equity - equity) / self._peak_equity
            if dd >= self.max_drawdown:
                self._breaker_triggered = True


class MaxPositionLimit(RiskManager):
    """
    单标的最大仓位限制。

    限制单标的持仓市值不超过总资产的 max_pct。
    如果订单执行后会超过限制，自动缩减数量。
    """

    def __init__(self, max_pct: float = 0.25) -> None:
        self.max_pct = max_pct

    def check_order(
        self,
        order: OrderEvent,
        portfolio: Portfolio,
        bar_data: BarData,
    ) -> RiskCheckResult:
        bar = bar_data.current(order.symbol)
        if bar is None:
            return RiskCheckResult(approved=True)

        equity = portfolio.equity
        if equity <= 0:
            return RiskCheckResult(approved=False, reason="MaxPositionLimit: zero equity")

        price = bar.close
        current_qty = portfolio.get_position_quantity(order.symbol)
        max_qty = int(equity * self.max_pct / price) if price > 0 else 0

        # 计算订单执行后的持仓
        if order.direction == Direction.LONG:
            new_qty = current_qty + order.quantity
        else:
            new_qty = current_qty - order.quantity

        # 平仓方向始终放行
        if abs(new_qty) <= abs(current_qty):
            return RiskCheckResult(approved=True)

        # 检查是否超过限制
        if abs(new_qty) <= max_qty:
            return RiskCheckResult(approved=True)

        # 缩减数量
        allowed_delta = max(max_qty - abs(current_qty), 0)
        if allowed_delta <= 0:
            return RiskCheckResult(
                approved=False,
                reason=f"MaxPositionLimit: {order.symbol} already at limit "
                       f"({abs(current_qty)} shares, max {max_qty})",
            )

        adjusted = OrderEvent(
            symbol=order.symbol,
            direction=order.direction,
            quantity=allowed_delta,
            order_type=order.order_type,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
        )
        return RiskCheckResult(
            approved=True,
            adjusted_order=adjusted,
            reason=f"MaxPositionLimit: {order.symbol} qty reduced "
                   f"{order.quantity} → {allowed_delta}",
        )
