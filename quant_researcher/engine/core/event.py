"""
事件系统 — 整个引擎的通信骨架。

事件流:
  DataFeed → MarketEvent
  Strategy → SignalEvent
  OrderManager → OrderEvent
  Broker → FillEvent
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto


class EventType(Enum):
    MARKET = auto()
    SIGNAL = auto()
    ORDER = auto()
    FILL = auto()


class Direction(Enum):
    LONG = 1
    SHORT = -1


class OrderType(Enum):
    MARKET = auto()
    LIMIT = auto()
    STOP = auto()
    STOP_LIMIT = auto()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketEvent:
    """DataFeed 吐出的新 bar 事件。"""
    type: EventType = field(default=EventType.MARKET, init=False)
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class SignalEvent:
    """策略产生的交易信号。"""
    type: EventType = field(default=EventType.SIGNAL, init=False)
    symbol: str
    direction: Direction
    strength: float = 1.0  # 信号强度 0-1，可用于仓位管理
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class OrderEvent:
    """订单管理器生成的订单。"""
    type: EventType = field(default=EventType.ORDER, init=False)
    symbol: str
    direction: Direction
    quantity: int
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class FillEvent:
    """经纪商撮合后的成交回报。"""
    type: EventType = field(default=EventType.FILL, init=False)
    symbol: str
    direction: Direction
    quantity: int
    fill_price: float
    commission: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def cost(self) -> float:
        """总成本（含手续费）。"""
        return self.fill_price * self.quantity + self.commission
