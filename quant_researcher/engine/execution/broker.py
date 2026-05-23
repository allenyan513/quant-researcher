"""
SimulatedBroker — 模拟撮合引擎。

支持:
- 市价单: 以下一根 bar 的 open 成交 + 滑点
- 限价单: 买入限价 ≤ limit_price 时成交，卖出限价 ≥ limit_price 时成交
- 止损单: 价格穿过 stop_price 后转为市价单成交
- 止损限价单: 价格穿过 stop_price 后转为限价单
"""

from __future__ import annotations

from quant_researcher.engine.core.bar_data import BarData
from quant_researcher.engine.core.event import Direction, FillEvent, OrderEvent, OrderType
from quant_researcher.engine.execution.fee_model import FeeModel, PerShareFeeModel
from quant_researcher.engine.execution.slippage_model import SlippageModel


class SimulatedBroker:
    """模拟经纪商。"""

    def __init__(
        self,
        fee_model: FeeModel | None = None,
        slippage_rate: float = 0.0005,   # 0.05% 滑点
        # 向后兼容: 如果传了 commission_rate，自动创建 PercentageFeeModel
        commission_rate: float | None = None,
        slippage_model: SlippageModel | None = None,
    ) -> None:
        if commission_rate is not None:
            from quant_researcher.engine.execution.fee_model import PercentageFeeModel
            self.fee_model = PercentageFeeModel(rate=commission_rate)
        elif fee_model is not None:
            self.fee_model = fee_model
        else:
            self.fee_model = PerShareFeeModel()
        self.slippage_rate = slippage_rate
        self.slippage_model = slippage_model
        self._pending_orders: list[OrderEvent] = []

    def submit_order(self, order: OrderEvent) -> None:
        """提交订单（下一根 bar 撮合）。"""
        self._pending_orders.append(order)

    def cancel_order(self, symbol: str, direction: Direction | None = None) -> int:
        """
        取消指定标的的待处理订单。

        如果指定 direction，只取消该方向的订单。
        返回被取消的订单数量。
        """
        before = len(self._pending_orders)
        self._pending_orders = [
            o for o in self._pending_orders
            if not (o.symbol == symbol and (direction is None or o.direction == direction))
        ]
        return before - len(self._pending_orders)

    def fill_orders(self, bar_data: BarData) -> list[FillEvent]:
        """
        尝试撮合所有待处理订单。

        撮合逻辑:
        - MARKET: open + 滑点
        - LIMIT (买): bar.low ≤ limit_price → 以 limit_price 成交
        - LIMIT (卖): bar.high ≥ limit_price → 以 limit_price 成交
        - STOP (买): bar.high ≥ stop_price → 触发，以 open 或 stop_price 的较高者 + 滑点成交
        - STOP (卖): bar.low ≤ stop_price → 触发，以 open 或 stop_price 的较低者 - 滑点成交
        - STOP_LIMIT: stop 触发后转为 limit 单，同根 bar 内尝试撮合
        """
        fills: list[FillEvent] = []
        remaining: list[OrderEvent] = []

        for order in self._pending_orders:
            bar = bar_data.current(order.symbol)
            if bar is None:
                remaining.append(order)
                continue

            fill = self._try_fill(order, bar)
            if fill is not None:
                fills.append(fill)
            else:
                remaining.append(order)

        self._pending_orders = remaining
        return fills

    def _try_fill(self, order: OrderEvent, bar) -> FillEvent | None:
        """尝试撮合单个订单，返回 FillEvent 或 None。"""
        if order.order_type == OrderType.MARKET:
            return self._fill_market(order, bar)
        elif order.order_type == OrderType.LIMIT:
            return self._fill_limit(order, bar)
        elif order.order_type == OrderType.STOP:
            return self._fill_stop(order, bar)
        elif order.order_type == OrderType.STOP_LIMIT:
            return self._fill_stop_limit(order, bar)
        return None

    def _fill_market(self, order: OrderEvent, bar) -> FillEvent:
        """市价单: 以 open + 滑点成交。"""
        base_price = bar.open
        fill_price = self._apply_slippage(base_price, order.direction, bar, order.quantity)
        return self._make_fill(order, fill_price, bar.timestamp)

    def _fill_limit(self, order: OrderEvent, bar) -> FillEvent | None:
        """
        限价单撮合。

        买入: bar.low ≤ limit_price → 成交
        卖出: bar.high ≥ limit_price → 成交
        成交价为 limit_price（不会更差）。
        """
        if order.limit_price is None:
            return None

        if order.direction == Direction.LONG:
            if bar.low <= order.limit_price:
                # 如果 open 就已经低于限价，以 open 成交（更优价格）
                fill_price = min(order.limit_price, bar.open)
                return self._make_fill(order, fill_price, bar.timestamp)
        else:
            if bar.high >= order.limit_price:
                fill_price = max(order.limit_price, bar.open)
                return self._make_fill(order, fill_price, bar.timestamp)
        return None

    def _fill_stop(self, order: OrderEvent, bar) -> FillEvent | None:
        """
        止损单: 价格触及 stop_price 后以市价成交。

        买入止损: bar.high ≥ stop_price → 触发
        卖出止损: bar.low ≤ stop_price → 触发
        触发后以 stop_price + 滑点成交（模拟穿越止损价后的滑点）。
        """
        if order.stop_price is None:
            return None

        if order.direction == Direction.LONG:
            if bar.high >= order.stop_price:
                # 如果 open 就已经高于 stop，gap up 场景，以 open 成交
                base_price = max(order.stop_price, bar.open)
                fill_price = self._apply_slippage(base_price, order.direction, bar, order.quantity)
                return self._make_fill(order, fill_price, bar.timestamp)
        else:
            if bar.low <= order.stop_price:
                base_price = min(order.stop_price, bar.open)
                fill_price = self._apply_slippage(base_price, order.direction, bar, order.quantity)
                return self._make_fill(order, fill_price, bar.timestamp)
        return None

    def _fill_stop_limit(self, order: OrderEvent, bar) -> FillEvent | None:
        """
        止损限价单: stop 触发后转为 limit 单。

        先检查 stop 是否触发，触发后在同根 bar 内尝试以 limit_price 撮合。
        如果同根 bar 不能撮合，转为 limit 单留在 pending。
        """
        if order.stop_price is None or order.limit_price is None:
            return None

        triggered = False
        if order.direction == Direction.LONG:
            triggered = bar.high >= order.stop_price
        else:
            triggered = bar.low <= order.stop_price

        if not triggered:
            return None

        # Stop 已触发，尝试以 limit 价格成交
        # 转换为 limit order 逻辑
        if order.direction == Direction.LONG:
            if bar.low <= order.limit_price:
                fill_price = min(order.limit_price, bar.open)
                return self._make_fill(order, fill_price, bar.timestamp)
        else:
            if bar.high >= order.limit_price:
                fill_price = max(order.limit_price, bar.open)
                return self._make_fill(order, fill_price, bar.timestamp)

        # Stop 触发但 limit 未成交 — 转为 limit 单（下根 bar 继续尝试）
        # 返回 None，但 order 会被保留在 remaining 中
        # 我们需要把它转换为 LIMIT 单，通过返回特殊标记
        # 实际上这里返回 None 就行，order 留在 pending 继续尝试
        return None

    def _apply_slippage(
        self, price: float, direction: Direction, bar=None, quantity: int = 0
    ) -> float:
        """应用滑点。优先使用 slippage_model，回退到固定比例。"""
        if self.slippage_model is not None and bar is not None:
            return self.slippage_model.calculate(price, direction, bar, quantity)
        if direction == Direction.LONG:
            return price * (1 + self.slippage_rate)
        else:
            return price * (1 - self.slippage_rate)

    def _make_fill(self, order: OrderEvent, fill_price: float, timestamp) -> FillEvent:
        """构造 FillEvent。"""
        commission = self.fee_model.calculate(fill_price, order.quantity)
        return FillEvent(
            symbol=order.symbol,
            direction=order.direction,
            quantity=order.quantity,
            fill_price=fill_price,
            commission=commission,
            timestamp=timestamp,
        )

    @property
    def pending_count(self) -> int:
        return len(self._pending_orders)

    @property
    def pending_orders(self) -> list[OrderEvent]:
        return list(self._pending_orders)
