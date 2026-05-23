"""WarehouseDataFeed — the qr-specific DataFeed reading `daily_prices`.

This is the bridge between the ported quant-engine backtester and the
quant-researcher warehouse. The engine's `DataFeed.fetch(symbol, start, end)`
contract is honored by querying `daily_prices` for the date window and
returning ascending `Bar`s.

**Adjustment (default on):** FMP gives both raw `close` and `adj_close`
(split/dividend-adjusted). A price backtest on raw closes shows phantom gaps
at split dates, so by default we back-adjust the whole OHLC bar by
`factor = adj_close / close` and set `close = adj_close`. Pass
`adjusted=False` for the raw series. Rows with no usable close are skipped;
missing O/H/L fall back to close; missing volume → 0.

Point-in-time (D6 pragmatic): v1 reads by `trade_date` window only — historical
EOD bars are treated as immutable, which is enough to avoid look-ahead in a
price backtest. A stricter `known_at <= as_of` filter can be layered later.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant_researcher.engine.core.bar_data import Bar
from quant_researcher.engine.data.data_feed import DataFeed
from quant_researcher.models.prices import DailyPrice


class WarehouseDataFeed(DataFeed):
    """Read OHLCV bars from the `daily_prices` warehouse table."""

    def __init__(self, session: Session, *, adjusted: bool = True) -> None:
        self._session = session
        self._adjusted = adjusted

    def fetch(self, symbol: str, start: str, end: str) -> list[Bar]:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
        rows = self._session.execute(
            select(
                DailyPrice.trade_date,
                DailyPrice.open,
                DailyPrice.high,
                DailyPrice.low,
                DailyPrice.close,
                DailyPrice.adj_close,
                DailyPrice.volume,
            )
            .where(
                DailyPrice.symbol == symbol,
                DailyPrice.trade_date >= start_d,
                DailyPrice.trade_date <= end_d,
            )
            .order_by(DailyPrice.trade_date.asc())
        ).all()

        bars: list[Bar] = []
        for trade_date, o, h, low, close, adj_close, volume in rows:
            if close is None:
                continue  # can't price a bar without a close
            factor = 1.0
            final_close = float(close)
            if self._adjusted and adj_close is not None and close:
                factor = float(adj_close) / float(close)
                final_close = float(adj_close)
            bars.append(
                Bar(
                    symbol=symbol,
                    timestamp=datetime(
                        trade_date.year, trade_date.month, trade_date.day
                    ),
                    open=float(o) * factor if o is not None else final_close,
                    high=float(h) * factor if h is not None else final_close,
                    low=float(low) * factor if low is not None else final_close,
                    close=final_close,
                    volume=int(volume) if volume is not None else 0,
                )
            )
        return bars
