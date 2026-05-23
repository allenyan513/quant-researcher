"""
数据源适配器 — 负责把外部数据转成 Bar 列表。
"""

from __future__ import annotations

import csv
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from quant_researcher.engine.core.bar_data import Bar


class DataFeed(ABC):
    """数据源基类。"""

    @abstractmethod
    def fetch(self, symbol: str, start: str, end: str) -> list[Bar]:
        """获取历史数据，返回按时间升序排列的 Bar 列表。"""
        ...


class CSVFeed(DataFeed):
    """
    CSV 数据源。

    期望列: Date,Open,High,Low,Close,Volume
    """

    def __init__(self, data_dir: str | Path = "data_cache") -> None:
        self.data_dir = Path(data_dir)

    def fetch(self, symbol: str, start: str, end: str) -> list[Bar]:
        filepath = self.data_dir / f"{symbol}.csv"
        if not filepath.exists():
            raise FileNotFoundError(f"CSV not found: {filepath}")

        start_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d")

        bars: list[Bar] = []
        with open(filepath) as f:
            reader = csv.DictReader(f)
            for row in reader:
                dt = datetime.strptime(row["Date"], "%Y-%m-%d")
                if dt < start_dt or dt > end_dt:
                    continue
                bars.append(Bar(
                    symbol=symbol,
                    timestamp=dt,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=int(row["Volume"]),
                ))
        bars.sort(key=lambda b: b.timestamp)
        return bars
