"""
BaseStrategy — 所有策略的基类。

用户继承这个类，实现 initialize() 和 on_bar()。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from quant_researcher.engine.core.bar_data import BarData
from quant_researcher.engine.core.event import (
    Direction,
    FillEvent,
    OrderEvent,
    OrderType,
    SignalEvent,
)
from quant_researcher.engine.portfolio.portfolio import Portfolio
from quant_researcher.engine.risk.position_sizer import PositionSizer
from quant_researcher.engine.risk.stop_manager import StopManager


class BaseStrategy(ABC):
    """策略基类。"""

    def __init__(self) -> None:
        self._bar_data: BarData | None = None
        self._portfolio: Portfolio | None = None
        self._signals: list[SignalEvent] = []
        self._pending_orders: list[OrderEvent] = []
        self.position_sizer: PositionSizer | None = None
        self.stop_manager: StopManager = StopManager()

    def _bind(self, bar_data: BarData, portfolio: Portfolio) -> None:
        """引擎调用，绑定数据和组合。"""
        self._bar_data = bar_data
        self._portfolio = portfolio

    @property
    def bar_data(self) -> BarData:
        assert self._bar_data is not None
        return self._bar_data

    @property
    def portfolio(self) -> Portfolio:
        assert self._portfolio is not None
        return self._portfolio

    def initialize(self) -> None:  # noqa: B027 (intentional optional-override hook)
        """策略初始化（可选覆盖）。"""
        pass

    @abstractmethod
    def on_bar(self) -> None:
        """每根 bar 调用一次 — 核心逻辑在这里。"""
        ...

    def on_fill(self, fill: FillEvent) -> None:  # noqa: B027 (optional-override hook)
        """成交回报回调（可选覆盖）。"""
        pass

    # -----------------------------------------------------------------------
    # 市价单便捷方法
    # -----------------------------------------------------------------------

    def buy(self, symbol: str, quantity: int) -> None:
        """市价买入。"""
        self._signals.append(SignalEvent(
            symbol=symbol,
            direction=Direction.LONG,
        ))
        self._pending_orders.append(OrderEvent(
            symbol=symbol,
            direction=Direction.LONG,
            quantity=quantity,
            order_type=OrderType.MARKET,
        ))

    def sell(self, symbol: str, quantity: int) -> None:
        """市价卖出。"""
        self._signals.append(SignalEvent(
            symbol=symbol,
            direction=Direction.SHORT,
        ))
        self._pending_orders.append(OrderEvent(
            symbol=symbol,
            direction=Direction.SHORT,
            quantity=quantity,
            order_type=OrderType.MARKET,
        ))

    # -----------------------------------------------------------------------
    # 限价单便捷方法
    # -----------------------------------------------------------------------

    def buy_limit(self, symbol: str, quantity: int, limit_price: float) -> None:
        """限价买入。"""
        self._signals.append(SignalEvent(
            symbol=symbol,
            direction=Direction.LONG,
        ))
        self._pending_orders.append(OrderEvent(
            symbol=symbol,
            direction=Direction.LONG,
            quantity=quantity,
            order_type=OrderType.LIMIT,
            limit_price=limit_price,
        ))

    def sell_limit(self, symbol: str, quantity: int, limit_price: float) -> None:
        """限价卖出。"""
        self._signals.append(SignalEvent(
            symbol=symbol,
            direction=Direction.SHORT,
        ))
        self._pending_orders.append(OrderEvent(
            symbol=symbol,
            direction=Direction.SHORT,
            quantity=quantity,
            order_type=OrderType.LIMIT,
            limit_price=limit_price,
        ))

    # -----------------------------------------------------------------------
    # 止损便捷方法
    # -----------------------------------------------------------------------

    def set_stop_loss(
        self,
        symbol: str,
        stop_price: float,
        quantity: int | None = None,
    ) -> None:
        """
        为当前持仓设置止损。

        如果不指定 quantity，默认使用全部持仓。
        """
        pos_qty = abs(self.get_position(symbol))
        qty = quantity or pos_qty
        if qty <= 0:
            return

        direction = Direction.LONG if self.get_position(symbol) > 0 else Direction.SHORT
        self.stop_manager.add_fixed_stop(
            symbol=symbol,
            direction=direction,
            quantity=qty,
            stop_loss=stop_price,
        )

    def set_take_profit(
        self,
        symbol: str,
        target_price: float,
        quantity: int | None = None,
    ) -> None:
        """为当前持仓设置止盈。"""
        pos_qty = abs(self.get_position(symbol))
        qty = quantity or pos_qty
        if qty <= 0:
            return

        direction = Direction.LONG if self.get_position(symbol) > 0 else Direction.SHORT
        self.stop_manager.add_fixed_stop(
            symbol=symbol,
            direction=direction,
            quantity=qty,
            take_profit=target_price,
        )

    def set_trailing_stop(
        self,
        symbol: str,
        trail_pct: float | None = None,
        trail_points: float | None = None,
        quantity: int | None = None,
    ) -> None:
        """设置追踪止损。"""
        pos_qty = abs(self.get_position(symbol))
        qty = quantity or pos_qty
        if qty <= 0:
            return

        direction = Direction.LONG if self.get_position(symbol) > 0 else Direction.SHORT

        # 用当前价格作为初始追踪价
        bar = self.bar_data.current(symbol)
        initial_price = bar.close if bar else 0.0

        self.stop_manager.add_trailing_stop(
            symbol=symbol,
            direction=direction,
            quantity=qty,
            trail_pct=trail_pct,
            trail_points=trail_points,
            initial_price=initial_price,
        )

    def cancel_stops(self, symbol: str) -> None:
        """取消指定标的的所有止损。"""
        self.stop_manager.remove_stops(symbol)

    # -----------------------------------------------------------------------
    # 仓位计算便捷方法
    # -----------------------------------------------------------------------

    def calculate_quantity(self, symbol: str) -> int:
        """
        使用 position_sizer 计算建议仓位。

        如果没有设置 position_sizer，返回 0。
        """
        if self.position_sizer is None:
            return 0
        bar = self.bar_data.current(symbol)
        if bar is None:
            return 0
        return self.position_sizer.calculate(
            symbol=symbol,
            equity=self.portfolio.equity,
            price=bar.close,
            bar_data=self.bar_data,
        )

    # -----------------------------------------------------------------------
    # 查询方法
    # -----------------------------------------------------------------------

    def get_position(self, symbol: str) -> int:
        """查询当前持仓。"""
        return self.portfolio.get_position_quantity(symbol)

    def _collect_orders(self) -> list[OrderEvent]:
        """引擎调用，收集本次 bar 产生的订单。"""
        orders = list(self._pending_orders)
        self._pending_orders.clear()
        return orders

    def _collect_stop_orders(self) -> list[OrderEvent]:
        """引擎调用，检查止损管理器产生的订单。"""
        return self.stop_manager.check(self.bar_data)
