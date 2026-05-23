"""
Stop Manager — 止损/止盈管理。

管理活跃仓位的止损订单，支持:
- FixedStop: 固定价格止损/止盈
- TrailingStop: 追踪止损（随价格移动调整止损价）
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from quant_researcher.engine.core.bar_data import BarData
from quant_researcher.engine.core.event import Direction, OrderEvent, OrderType


class StopType(Enum):
    FIXED = auto()
    TRAILING = auto()


@dataclass
class FixedStop:
    """固定止损/止盈。"""
    symbol: str
    direction: Direction  # 持仓方向
    quantity: int
    stop_loss: float | None = None
    take_profit: float | None = None


@dataclass
class TrailingStop:
    """
    追踪止损。

    trail_pct: 追踪距离（占价格的百分比）
    trail_points: 追踪距离（固定点数）
    二选一，trail_pct 优先。
    """
    symbol: str
    direction: Direction
    quantity: int
    trail_pct: float | None = None
    trail_points: float | None = None
    _highest: float = 0.0  # 多头追踪的最高价
    _lowest: float = float("inf")  # 空头追踪的最低价

    @property
    def current_stop(self) -> float | None:
        """当前的追踪止损价。"""
        if self.direction == Direction.LONG:
            if self._highest <= 0:
                return None
            if self.trail_pct:
                return self._highest * (1 - self.trail_pct)
            elif self.trail_points:
                return self._highest - self.trail_points
        else:
            if self._lowest == float("inf"):
                return None
            if self.trail_pct:
                return self._lowest * (1 + self.trail_pct)
            elif self.trail_points:
                return self._lowest + self.trail_points
        return None


class StopManager:
    """
    止损管理器。

    策略可以注册止损规则，StopManager 在每根 bar 检查是否触发，
    触发时生成止损订单。
    """

    def __init__(self) -> None:
        self._fixed_stops: list[FixedStop] = []
        self._trailing_stops: list[TrailingStop] = []

    def add_fixed_stop(
        self,
        symbol: str,
        direction: Direction,
        quantity: int,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> None:
        """添加固定止损/止盈。"""
        self._fixed_stops.append(FixedStop(
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            stop_loss=stop_loss,
            take_profit=take_profit,
        ))

    def add_trailing_stop(
        self,
        symbol: str,
        direction: Direction,
        quantity: int,
        trail_pct: float | None = None,
        trail_points: float | None = None,
        initial_price: float = 0.0,
    ) -> None:
        """添加追踪止损。"""
        ts = TrailingStop(
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            trail_pct=trail_pct,
            trail_points=trail_points,
        )
        if direction == Direction.LONG:
            ts._highest = initial_price
        else:
            ts._lowest = initial_price if initial_price > 0 else float("inf")
        self._trailing_stops.append(ts)

    def remove_stops(self, symbol: str) -> None:
        """移除指定标的的所有止损。"""
        self._fixed_stops = [s for s in self._fixed_stops if s.symbol != symbol]
        self._trailing_stops = [s for s in self._trailing_stops if s.symbol != symbol]

    def check(self, bar_data: BarData) -> list[OrderEvent]:
        """
        检查所有止损规则，返回需要执行的止损订单。

        在每根 bar 调用一次。
        """
        orders: list[OrderEvent] = []

        # 检查固定止损
        triggered_fixed: list[int] = []
        for i, stop in enumerate(self._fixed_stops):
            bar = bar_data.current(stop.symbol)
            if bar is None:
                continue

            exit_dir = Direction.SHORT if stop.direction == Direction.LONG else Direction.LONG

            # 止损检查
            if stop.stop_loss is not None:
                if stop.direction == Direction.LONG and bar.low <= stop.stop_loss:
                    orders.append(OrderEvent(
                        symbol=stop.symbol,
                        direction=exit_dir,
                        quantity=stop.quantity,
                        order_type=OrderType.STOP,
                        stop_price=stop.stop_loss,
                    ))
                    triggered_fixed.append(i)
                    continue
                elif stop.direction == Direction.SHORT and bar.high >= stop.stop_loss:
                    orders.append(OrderEvent(
                        symbol=stop.symbol,
                        direction=exit_dir,
                        quantity=stop.quantity,
                        order_type=OrderType.STOP,
                        stop_price=stop.stop_loss,
                    ))
                    triggered_fixed.append(i)
                    continue

            # 止盈检查
            if stop.take_profit is not None:
                if stop.direction == Direction.LONG and bar.high >= stop.take_profit:
                    orders.append(OrderEvent(
                        symbol=stop.symbol,
                        direction=exit_dir,
                        quantity=stop.quantity,
                        order_type=OrderType.LIMIT,
                        limit_price=stop.take_profit,
                    ))
                    triggered_fixed.append(i)
                elif stop.direction == Direction.SHORT and bar.low <= stop.take_profit:
                    orders.append(OrderEvent(
                        symbol=stop.symbol,
                        direction=exit_dir,
                        quantity=stop.quantity,
                        order_type=OrderType.LIMIT,
                        limit_price=stop.take_profit,
                    ))
                    triggered_fixed.append(i)

        # 移除已触发的固定止损
        for i in reversed(triggered_fixed):
            self._fixed_stops.pop(i)

        # 检查追踪止损
        triggered_trailing: list[int] = []
        for i, ts in enumerate(self._trailing_stops):
            bar = bar_data.current(ts.symbol)
            if bar is None:
                continue

            # 更新追踪价格
            if ts.direction == Direction.LONG:
                ts._highest = max(ts._highest, bar.high)
            else:
                ts._lowest = min(ts._lowest, bar.low)

            stop_price = ts.current_stop
            if stop_price is None:
                continue

            exit_dir = Direction.SHORT if ts.direction == Direction.LONG else Direction.LONG

            # 检查是否触发
            if ts.direction == Direction.LONG and bar.low <= stop_price:
                orders.append(OrderEvent(
                    symbol=ts.symbol,
                    direction=exit_dir,
                    quantity=ts.quantity,
                    order_type=OrderType.STOP,
                    stop_price=stop_price,
                ))
                triggered_trailing.append(i)
            elif ts.direction == Direction.SHORT and bar.high >= stop_price:
                orders.append(OrderEvent(
                    symbol=ts.symbol,
                    direction=exit_dir,
                    quantity=ts.quantity,
                    order_type=OrderType.STOP,
                    stop_price=stop_price,
                ))
                triggered_trailing.append(i)

        for i in reversed(triggered_trailing):
            self._trailing_stops.pop(i)

        return orders

    @property
    def active_stop_count(self) -> int:
        return len(self._fixed_stops) + len(self._trailing_stops)
