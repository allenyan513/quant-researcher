"""
Portfolio — 持仓管理 + 净值追踪。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from quant_researcher.engine.core.bar_data import BarData
from quant_researcher.engine.core.event import Direction, FillEvent


@dataclass
class Position:
    """单个标的的持仓。"""
    symbol: str
    quantity: int = 0
    avg_cost: float = 0.0

    @property
    def market_value(self) -> float:
        """需要外部传入最新价格来计算，这里只返回成本基础。"""
        return self.quantity * self.avg_cost

    def update(self, fill: FillEvent) -> float:
        """
        根据成交更新持仓，返回已实现盈亏。
        """
        realized_pnl = 0.0
        signed_qty = fill.quantity if fill.direction == Direction.LONG else -fill.quantity

        if self.quantity == 0:
            # 新建仓位
            self.quantity = signed_qty
            self.avg_cost = fill.fill_price
        elif (self.quantity > 0 and signed_qty > 0) or (self.quantity < 0 and signed_qty < 0):
            # 加仓 — 更新均价
            total_cost = self.avg_cost * abs(self.quantity) + fill.fill_price * abs(signed_qty)
            self.quantity += signed_qty
            self.avg_cost = total_cost / abs(self.quantity)
        else:
            # 减仓或反向
            close_qty = min(abs(signed_qty), abs(self.quantity))
            realized_pnl = (
                close_qty
                * (fill.fill_price - self.avg_cost)
                * (1 if self.quantity > 0 else -1)
            )
            self.quantity += signed_qty
            if self.quantity != 0 and abs(signed_qty) > close_qty:
                # 反向开仓
                self.avg_cost = fill.fill_price

        realized_pnl -= fill.commission
        return realized_pnl


class Portfolio:
    """
    账户级别的组合管理。

    追踪: 现金、持仓、净值曲线。
    """

    def __init__(self, initial_cash: float = 100_000.0) -> None:
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions: dict[str, Position] = {}
        self.realized_pnl = 0.0

        # 净值曲线: (timestamp, equity)
        self.equity_curve: list[tuple[datetime, float]] = []

    def on_fill(self, fill: FillEvent) -> None:
        """处理成交回报。"""
        if fill.symbol not in self.positions:
            self.positions[fill.symbol] = Position(symbol=fill.symbol)

        pos = self.positions[fill.symbol]
        realized = pos.update(fill)
        self.realized_pnl += realized

        # 更新现金
        if fill.direction == Direction.LONG:
            self.cash -= fill.fill_price * fill.quantity + fill.commission
        else:
            self.cash += fill.fill_price * fill.quantity - fill.commission

    def update_equity(self, bar_data: BarData, timestamp: datetime) -> None:
        """根据最新行情更新净值。"""
        equity = self.cash
        for symbol, pos in self.positions.items():
            if pos.quantity != 0:
                bar = bar_data.current(symbol)
                if bar:
                    equity += pos.quantity * bar.close
        self.equity_curve.append((timestamp, equity))

    @property
    def equity(self) -> float:
        """最新净值。"""
        if self.equity_curve:
            return self.equity_curve[-1][1]
        return self.cash

    def get_position_quantity(self, symbol: str) -> int:
        """获取某标的当前持仓数量。"""
        pos = self.positions.get(symbol)
        return pos.quantity if pos else 0
