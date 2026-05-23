"""Backtest orchestration — the single entry point CLI and Python both use.

`run_backtest` resolves a strategy (built-in registry or `--strategy-file`),
wires a `WarehouseDataFeed` over the engine, runs it, computes metrics, and
persists a `BacktestRun` snapshot. It returns an envelope-friendly summary
(metrics + counts + run_id) — the bulky `equity_curve` / `trade_log` go to the
DB and come back via `qr backtest show`.

Two serialization hazards are handled here so the JSON columns stay portable:
- `calculate_metrics` emits numpy scalars → coerced to native floats.
- metrics like `profit_factor` can be `inf`/`nan` → coerced to None (Postgres
  JSONB rejects Infinity).
"""

from __future__ import annotations

import inspect
import math
from datetime import date
from typing import Any
from uuid import uuid4

import numpy as np
from sqlalchemy.orm import Session

from quant_researcher.backtest.loader import load_strategy_from_file
from quant_researcher.backtest.strategies import get_strategy
from quant_researcher.contract import code_version
from quant_researcher.engine.analytics.metrics import calculate_metrics
from quant_researcher.engine.data import WarehouseDataFeed
from quant_researcher.engine.engine import BacktestEngine
from quant_researcher.engine.execution import (
    PercentageFeeModel,
    PerShareFeeModel,
    ZeroFeeModel,
)
from quant_researcher.engine.strategy.base import BaseStrategy
from quant_researcher.models.backtest import BacktestRun

_FEE_MODELS = {
    "zero": ZeroFeeModel,
    "per-share": PerShareFeeModel,
    "percentage": PercentageFeeModel,
}


def run_backtest(
    session: Session,
    *,
    strategy: str,
    symbols: list[str],
    start: str,
    end: str,
    initial_cash: float = 100_000.0,
    params: dict[str, Any] | None = None,
    fee: str = "per-share",
    slippage_rate: float = 0.0005,
    benchmark_symbol: str | None = None,
    adjusted: bool = True,
    strategy_file: str | None = None,
    strategy_class: str | None = None,
    risk_free_rate: float = 0.0,
    persist: bool = True,
) -> dict[str, Any]:
    """Run one backtest end-to-end. Adds a `BacktestRun` to `session` (caller
    commits). Returns a summary dict (no bulky curves)."""
    params = dict(params or {})

    # 1. resolve strategy class (file takes precedence over registry name)
    if strategy_file is not None:
        cls = load_strategy_from_file(strategy_file, class_name=strategy_class)
        strategy_label = cls.__name__
    else:
        cls = get_strategy(strategy)
        strategy_label = strategy

    # 2. instantiate — auto-inject the first symbol for single-symbol strategies
    instance = _build_strategy(cls, params, symbols)

    # 3. wire feed + fee model + engine (risk/margin not exposed in CLI v1)
    fee_model = _build_fee_model(fee)
    feed = WarehouseDataFeed(session, adjusted=adjusted)
    engine = BacktestEngine(
        strategy=instance,
        data_feed=feed,
        symbols=symbols,
        start=start,
        end=end,
        initial_cash=initial_cash,
        fee_model=fee_model,
        slippage_rate=slippage_rate,
        benchmark_symbol=benchmark_symbol,
        verbose=False,
    )
    portfolio = engine.run()

    # 4. metrics + serializable curves
    metrics = _to_jsonable(
        calculate_metrics(portfolio, engine.benchmark_curve, risk_free_rate)
    )
    equity_curve = [[ts.isoformat(), float(v)] for ts, v in portfolio.equity_curve]
    trades = [_trade_to_dict(t) for t in engine.trade_log.trades]
    trade_summary = _to_jsonable(engine.trade_log.summary())

    run_id = str(uuid4())
    config = {
        "fee": fee,
        "slippage_rate": slippage_rate,
        "adjusted": adjusted,
        "strategy_file": strategy_file,
    }

    if persist:
        session.add(
            BacktestRun(
                run_id=run_id,
                strategy=strategy_label,
                start=date.fromisoformat(start),
                end=date.fromisoformat(end),
                initial_cash=initial_cash,
                benchmark_symbol=benchmark_symbol,
                symbols=symbols,
                params=params,
                config=config,
                metrics=metrics,
                equity_curve=equity_curve,
                trade_log=trades,
                code_version=code_version(),
            )
        )

    return {
        "run_id": run_id,
        "strategy": strategy_label,
        "symbols": symbols,
        "start": start,
        "end": end,
        "initial_cash": initial_cash,
        "final_equity": float(portfolio.equity),
        "params": params,
        "config": config,
        "benchmark_symbol": benchmark_symbol,
        "metrics": metrics,
        "trade_summary": trade_summary,
        "n_trades": len(trades),
        "n_equity_points": len(equity_curve),
        "code_version": code_version(),
    }


def _build_strategy(
    cls: type[BaseStrategy], params: dict[str, Any], symbols: list[str]
) -> BaseStrategy:
    """Instantiate a strategy, injecting `symbol=symbols[0]` for single-symbol
    strategies that declare it but didn't get it via --params."""
    kwargs = dict(params)
    sig = inspect.signature(cls.__init__)
    if "symbol" in sig.parameters and "symbol" not in kwargs and symbols:
        kwargs["symbol"] = symbols[0]
    return cls(**kwargs)


def _build_fee_model(fee: str):
    try:
        return _FEE_MODELS[fee]()
    except KeyError:
        valid = ", ".join(sorted(_FEE_MODELS))
        raise ValueError(f"unknown fee model '{fee}' (valid: {valid})") from None


def _trade_to_dict(t: Any) -> dict[str, Any]:
    return {
        "symbol": t.symbol,
        "direction": t.direction.name,
        "entry_time": t.entry_time.isoformat() if t.entry_time else None,
        "entry_price": _num(t.entry_price),
        "exit_time": t.exit_time.isoformat() if t.exit_time else None,
        "exit_price": _num(t.exit_price) if t.exit_price is not None else None,
        "quantity": int(t.quantity),
        "pnl": _num(t.pnl),
        "commission": _num(t.commission),
        "net_pnl": _num(t.net_pnl),
        "return_pct": _num(t.return_pct),
        "holding_days": int(t.holding_days),
    }


def _num(v: Any) -> float | None:
    """One scalar → JSON-safe float (inf/nan → None)."""
    if isinstance(v, np.generic):
        v = v.item()
    f = float(v)
    return None if (math.isnan(f) or math.isinf(f)) else f


def _to_jsonable(obj: Any) -> Any:
    """Recursively coerce numpy scalars/arrays and inf/nan for JSON storage."""
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [_to_jsonable(v) for v in obj.tolist()]
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    return obj
