"""
ExecutionModel — 执行模型抽象。

控制订单如何被拆分和提交给 Broker。默认立即执行（ImmediateExecution），
可扩展为 TWAP/VWAP 等算法执行（需要分钟线支持）。

插入点: engine 收集策略订单后，经过 RiskManager 过滤，再通过 ExecutionModel
转换为最终提交给 Broker 的订单。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from quant_researcher.engine.core.bar_data import BarData
from quant_researcher.engine.core.event import OrderEvent
from quant_researcher.engine.portfolio.portfolio import Portfolio


class ExecutionModel(ABC):
    """
    执行模型基类。

    将策略产生的订单转换为实际提交给 broker 的订单序列。
    """

    @abstractmethod
    def execute(
        self,
        order: OrderEvent,
        portfolio: Portfolio,
        bar_data: BarData,
    ) -> list[OrderEvent]:
        """
        处理一个订单，返回实际要提交的订单列表。

        Args:
            order: 策略产生的原始订单
            portfolio: 当前组合状态
            bar_data: 当前行情数据

        Returns:
            要提交给 broker 的订单列表（可能是拆分后的多个小订单）
        """
        ...


class ImmediateExecution(ExecutionModel):
    """
    立即执行 — 默认执行模型。

    原样传递订单，不做任何拆分或延迟。
    适用于日线回测。
    """

    def execute(
        self,
        order: OrderEvent,
        portfolio: Portfolio,
        bar_data: BarData,
    ) -> list[OrderEvent]:
        return [order]


class TWAPExecution(ExecutionModel):
    """
    TWAP (Time-Weighted Average Price) 执行模型。

    将大订单拆分为 n_slices 个等量小订单。在日线回测中，
    所有切片会在同一 bar 提交（效果等同于 ImmediateExecution）。
    设计为分钟线/实盘场景的占位，届时每个切片会在不同 bar 提交。

    当前实现: 简单等分，所有切片同 bar 提交。
    """

    def __init__(self, n_slices: int = 5) -> None:
        self.n_slices = max(1, n_slices)

    def execute(
        self,
        order: OrderEvent,
        portfolio: Portfolio,
        bar_data: BarData,
    ) -> list[OrderEvent]:
        if order.quantity <= self.n_slices:
            return [order]

        base_qty = order.quantity // self.n_slices
        remainder = order.quantity % self.n_slices

        orders: list[OrderEvent] = []
        for i in range(self.n_slices):
            qty = base_qty + (1 if i < remainder else 0)
            if qty <= 0:
                continue
            orders.append(OrderEvent(
                symbol=order.symbol,
                direction=order.direction,
                quantity=qty,
                order_type=order.order_type,
                limit_price=order.limit_price,
                stop_price=order.stop_price,
            ))
        return orders


class VWAPExecution(ExecutionModel):
    """
    VWAP (Volume-Weighted Average Price) 执行模型。

    按成交量权重拆分订单。需要历史成交量数据来计算各时段的权重。
    在日线回测中退化为 ImmediateExecution。

    当前实现: 占位，等分钟线数据源就绪后实现真正的 VWAP 逻辑。
    """

    def __init__(self, n_slices: int = 5) -> None:
        self.n_slices = max(1, n_slices)

    def execute(
        self,
        order: OrderEvent,
        portfolio: Portfolio,
        bar_data: BarData,
    ) -> list[OrderEvent]:
        # 日线场景下退化为等分（无法获取日内成交量分布）
        if order.quantity <= self.n_slices:
            return [order]

        base_qty = order.quantity // self.n_slices
        remainder = order.quantity % self.n_slices

        orders: list[OrderEvent] = []
        for i in range(self.n_slices):
            qty = base_qty + (1 if i < remainder else 0)
            if qty <= 0:
                continue
            orders.append(OrderEvent(
                symbol=order.symbol,
                direction=order.direction,
                quantity=qty,
                order_type=order.order_type,
                limit_price=order.limit_price,
                stop_price=order.stop_price,
            ))
        return orders
