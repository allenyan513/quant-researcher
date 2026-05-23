"""
回测结果分析 — 核心指标 + 交易日志。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np

from quant_researcher.engine.core.event import Direction, FillEvent
from quant_researcher.engine.portfolio.portfolio import Portfolio

# ---------------------------------------------------------------------------
# Trade log
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    """一笔完整的交易（从开仓到平仓）。"""
    symbol: str
    direction: Direction
    entry_time: datetime
    entry_price: float
    exit_time: datetime | None = None
    exit_price: float | None = None
    quantity: int = 0
    pnl: float = 0.0
    commission: float = 0.0

    @property
    def net_pnl(self) -> float:
        return self.pnl - self.commission

    @property
    def return_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        if self.direction == Direction.LONG:
            return ((self.exit_price or self.entry_price) / self.entry_price) - 1
        else:
            return (self.entry_price / (self.exit_price or self.entry_price)) - 1

    @property
    def holding_days(self) -> int:
        if self.exit_time is None:
            return 0
        return (self.exit_time - self.entry_time).days


class TradeLog:
    """
    交易日志 — 跟踪每笔交易的开仓/平仓。

    通过 on_fill() 接收成交回报，自动配对为完整交易。
    """

    def __init__(self) -> None:
        self.trades: list[Trade] = []
        self._open_trades: dict[str, Trade] = {}  # symbol → 当前未平仓交易

    def on_fill(self, fill: FillEvent) -> None:
        """处理成交事件，更新交易日志。"""
        symbol = fill.symbol

        if symbol not in self._open_trades:
            # 新开仓
            self._open_trades[symbol] = Trade(
                symbol=symbol,
                direction=fill.direction,
                entry_time=fill.timestamp,
                entry_price=fill.fill_price,
                quantity=fill.quantity,
                commission=fill.commission,
            )
        else:
            open_trade = self._open_trades[symbol]
            if fill.direction == open_trade.direction:
                # 加仓 — 更新均价
                total_cost = (open_trade.entry_price * open_trade.quantity
                              + fill.fill_price * fill.quantity)
                open_trade.quantity += fill.quantity
                open_trade.entry_price = total_cost / open_trade.quantity
                open_trade.commission += fill.commission
            else:
                # 平仓（全部或部分）
                close_qty = min(fill.quantity, open_trade.quantity)
                if open_trade.direction == Direction.LONG:
                    pnl = close_qty * (fill.fill_price - open_trade.entry_price)
                else:
                    pnl = close_qty * (open_trade.entry_price - fill.fill_price)

                open_trade.exit_time = fill.timestamp
                open_trade.exit_price = fill.fill_price
                open_trade.pnl = pnl
                open_trade.commission += fill.commission
                self.trades.append(open_trade)

                remaining = open_trade.quantity - close_qty
                if remaining > 0:
                    # 部分平仓后仍有持仓
                    self._open_trades[symbol] = Trade(
                        symbol=symbol,
                        direction=open_trade.direction,
                        entry_time=open_trade.entry_time,
                        entry_price=open_trade.entry_price,
                        quantity=remaining,
                    )
                elif fill.quantity > close_qty:
                    # 反向开仓
                    self._open_trades[symbol] = Trade(
                        symbol=symbol,
                        direction=fill.direction,
                        entry_time=fill.timestamp,
                        entry_price=fill.fill_price,
                        quantity=fill.quantity - close_qty,
                        commission=0.0,
                    )
                else:
                    del self._open_trades[symbol]

    def summary(self) -> dict:
        """交易统计摘要。"""
        if not self.trades:
            return {"total_trades": 0}

        pnls = [t.net_pnl for t in self.trades]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p <= 0]

        return {
            "total_trades": len(self.trades),
            "winning_trades": len(winners),
            "losing_trades": len(losers),
            "win_rate": len(winners) / len(self.trades),
            "avg_win": np.mean(winners) if winners else 0.0,
            "avg_loss": np.mean(losers) if losers else 0.0,
            "profit_factor": (
                sum(winners) / abs(sum(losers))
                if losers and sum(losers) != 0
                else float("inf")
            ),
            "largest_win": max(pnls),
            "largest_loss": min(pnls),
            "avg_holding_days": np.mean([t.holding_days for t in self.trades]),
            "total_pnl": sum(pnls),
        }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def calculate_metrics(
    portfolio: Portfolio,
    benchmark_curve: list[tuple[datetime, float]] | None = None,
    risk_free_rate: float = 0.0,
) -> dict:
    """
    计算核心回测指标。

    Args:
        portfolio: 回测完成的 Portfolio 对象
        benchmark_curve: 可选的基准净值曲线 [(timestamp, value), ...]
        risk_free_rate: 年化无风险利率（默认 0）
    """
    if len(portfolio.equity_curve) < 2:
        return {}

    equities = np.array([e for _, e in portfolio.equity_curve])
    timestamps = [t for t, _ in portfolio.equity_curve]

    # 收益率序列
    returns = np.diff(equities) / equities[:-1]

    # 总收益
    total_return = (equities[-1] / equities[0]) - 1

    # 年化收益 (假设252个交易日)
    n_days = (timestamps[-1] - timestamps[0]).days
    if n_days > 0:
        cagr = (equities[-1] / equities[0]) ** (365 / n_days) - 1
    else:
        cagr = 0.0

    # 最大回撤
    peak = np.maximum.accumulate(equities)
    drawdown = (equities - peak) / peak
    max_drawdown = drawdown.min()

    # 日无风险收益
    daily_rf = (1 + risk_free_rate) ** (1 / 252) - 1
    excess_returns = returns - daily_rf

    # Sharpe Ratio
    if returns.std() > 0:
        sharpe = excess_returns.mean() / returns.std() * np.sqrt(252)
    else:
        sharpe = 0.0

    # Sortino Ratio（只用下行波动率）
    downside = returns[returns < daily_rf] - daily_rf
    if len(downside) > 0 and downside.std() > 0:
        sortino = excess_returns.mean() / downside.std() * np.sqrt(252)
    else:
        sortino = 0.0

    # Calmar Ratio (CAGR / |max_drawdown|)
    calmar = cagr / abs(max_drawdown) if max_drawdown != 0 else 0.0

    # 胜率（日级别）
    winning_days = (returns > 0).sum()
    total_days = len(returns)
    win_rate = winning_days / total_days if total_days > 0 else 0.0

    # Probabilistic Sharpe Ratio (PSR)
    # PSR = Φ((Sharpe - 0) * sqrt(n-1) / sqrt(1 - skew*Sharpe + (kurt-1)/4 * Sharpe^2))
    from scipy.stats import kurtosis as _kurt_fn
    from scipy.stats import norm as _norm
    from scipy.stats import skew as _skew_fn
    n = len(returns)
    if n > 2 and returns.std() > 0:
        skewness = float(_skew_fn(returns))
        excess_kurt = float(_kurt_fn(returns, fisher=True))
        denom = max(1e-10, (1 - skewness * sharpe + (excess_kurt) / 4 * sharpe ** 2)) ** 0.5
        psr = float(_norm.cdf(sharpe * ((n - 1) ** 0.5) / denom))
    else:
        psr = 0.0

    # Expectancy = Win Rate * Avg Win - Loss Rate * |Avg Loss|  (per trade)
    # Computed at portfolio level from daily returns
    winning_returns = returns[returns > 0]
    losing_returns = returns[returns < 0]
    if len(winning_returns) > 0 and len(losing_returns) > 0:
        expectancy = (win_rate * winning_returns.mean()
                      - (1 - win_rate) * abs(losing_returns.mean()))
    else:
        expectancy = 0.0

    metrics = {
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": max_drawdown,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "calmar_ratio": calmar,
        "psr": psr,
        "expectancy": expectancy,
        "win_rate": win_rate,
        "total_trades_days": total_days,
        "initial_equity": equities[0],
        "final_equity": equities[-1],
        "realized_pnl": portfolio.realized_pnl,
        "volatility": returns.std() * np.sqrt(252),
    }

    # Benchmark comparison
    if benchmark_curve and len(benchmark_curve) >= 2:
        bm_values = np.array([v for _, v in benchmark_curve])
        bm_returns = np.diff(bm_values) / bm_values[:-1]

        # 对齐长度
        min_len = min(len(returns), len(bm_returns))
        aligned_ret = returns[:min_len]
        aligned_bm = bm_returns[:min_len]

        bm_total = (bm_values[-1] / bm_values[0]) - 1
        metrics["benchmark_return"] = bm_total
        metrics["alpha"] = total_return - bm_total

        # Beta
        cov = np.cov(aligned_ret, aligned_bm)
        if cov.shape == (2, 2) and cov[1, 1] > 0:
            metrics["beta"] = cov[0, 1] / cov[1, 1]
        else:
            metrics["beta"] = 0.0

        # Information Ratio
        tracking = aligned_ret - aligned_bm
        if tracking.std() > 0:
            metrics["information_ratio"] = tracking.mean() / tracking.std() * np.sqrt(252)
        else:
            metrics["information_ratio"] = 0.0

        # Tracking Error (annualized)
        metrics["tracking_error"] = tracking.std() * np.sqrt(252) if tracking.std() > 0 else 0.0

        # Treynor Ratio = (Portfolio Return - Rf) / Beta
        if metrics.get("beta", 0) != 0:
            metrics["treynor_ratio"] = (cagr - risk_free_rate) / metrics["beta"]
        else:
            metrics["treynor_ratio"] = 0.0

    return metrics


def get_environment_info(engine=None) -> dict:
    """
    收集回测环境信息: Python 版本、依赖版本、引擎配置。

    Args:
        engine: 可选的 BacktestEngine 实例，用于提取配置参数

    Returns:
        包含环境信息的字典
    """
    import platform

    info: dict = {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
    }

    # 核心依赖版本
    for pkg in ("numpy", "scipy"):
        try:
            mod = __import__(pkg)
            info[f"{pkg}_version"] = getattr(mod, "__version__", "unknown")
        except ImportError:
            info[f"{pkg}_version"] = "not installed"

    # 引擎配置
    if engine is not None:
        info["symbols"] = engine.symbols
        info["period"] = f"{engine.start} ~ {engine.end}"
        info["initial_cash"] = engine.portfolio.initial_cash
        info["slippage_rate"] = engine.broker.slippage_rate
        info["fee_model"] = type(engine.broker.fee_model).__name__
        info["strategy"] = type(engine.strategy).__name__
        info["data_feed"] = type(engine.data_feed).__name__

    return info


def print_environment(engine=None) -> None:
    """打印环境信息到控制台。"""
    info = get_environment_info(engine)

    print("\n" + "=" * 55)
    print("            ENVIRONMENT INFO")
    print("=" * 55)
    print(f"  Python:           {info['python_version']}")
    print(f"  Platform:         {info['platform']}")
    print(f"  NumPy:            {info['numpy_version']}")
    print(f"  SciPy:            {info['scipy_version']}")

    if engine is not None:
        print("-" * 55)
        print(f"  Strategy:         {info['strategy']}")
        print(f"  Data Feed:        {info['data_feed']}")
        print(f"  Symbols:          {', '.join(info['symbols'])}")
        print(f"  Period:           {info['period']}")
        print(f"  Initial Cash:     ${info['initial_cash']:>12,.2f}")
        print(f"  Fee Model:        {info['fee_model']}")
        print(f"  Slippage Rate:    {info['slippage_rate']:.4%}")

    print("=" * 55)


def print_report(
    portfolio: Portfolio,
    trade_log: TradeLog | None = None,
    benchmark_curve: list[tuple[datetime, float]] | None = None,
    engine=None,
    show_environment: bool = False,
) -> None:
    """
    打印回测报告。

    如果传入 engine，自动使用 engine.benchmark_curve (SPY)。
    也可以手动传 benchmark_curve 覆盖。

    Args:
        show_environment: 是否在报告末尾打印环境信息
    """
    if benchmark_curve is None and engine is not None:
        benchmark_curve = getattr(engine, "benchmark_curve", None)
    metrics = calculate_metrics(portfolio, benchmark_curve)
    if not metrics:
        print("No data to report.")
        return

    print("\n" + "=" * 55)
    print("              BACKTEST REPORT")
    print("=" * 55)
    print(f"  Initial Equity:   ${metrics['initial_equity']:>12,.2f}")
    print(f"  Final Equity:     ${metrics['final_equity']:>12,.2f}")
    print(f"  Total Return:     {metrics['total_return']:>12.2%}")
    print(f"  CAGR:             {metrics['cagr']:>12.2%}")
    print(f"  Volatility:       {metrics['volatility']:>12.2%}")
    print(f"  Max Drawdown:     {metrics['max_drawdown']:>12.2%}")
    print("-" * 55)
    print(f"  Sharpe Ratio:     {metrics['sharpe_ratio']:>12.2f}")
    print(f"  Sortino Ratio:    {metrics['sortino_ratio']:>12.2f}")
    print(f"  Calmar Ratio:     {metrics['calmar_ratio']:>12.2f}")
    print(f"  PSR:              {metrics['psr']:>12.2%}")
    print(f"  Expectancy:       {metrics['expectancy']:>12.6f}")
    print(f"  Win Rate (daily): {metrics['win_rate']:>12.2%}")
    print(f"  Realized PnL:     ${metrics['realized_pnl']:>12,.2f}")

    # Benchmark
    if "benchmark_return" in metrics:
        print("-" * 55)
        print(f"  Benchmark Return: {metrics['benchmark_return']:>12.2%}")
        print(f"  Alpha:            {metrics['alpha']:>12.2%}")
        print(f"  Beta:             {metrics['beta']:>12.2f}")
        print(f"  Information Ratio:{metrics['information_ratio']:>12.2f}")
        if "tracking_error" in metrics:
            print(f"  Tracking Error:   {metrics['tracking_error']:>12.4f}")
        if "treynor_ratio" in metrics:
            print(f"  Treynor Ratio:    {metrics['treynor_ratio']:>12.2f}")

    # Trade log
    if trade_log:
        ts = trade_log.summary()
        if ts.get("total_trades", 0) > 0:
            print("-" * 55)
            print(f"  Total Trades:     {ts['total_trades']:>12d}")
            print(f"  Win / Loss:       {ts['winning_trades']:>5d} / {ts['losing_trades']:<5d}")
            print(f"  Trade Win Rate:   {ts['win_rate']:>12.2%}")
            print(f"  Avg Win:          ${ts['avg_win']:>12,.2f}")
            print(f"  Avg Loss:         ${ts['avg_loss']:>12,.2f}")
            print(f"  Profit Factor:    {ts['profit_factor']:>12.2f}")
            print(f"  Largest Win:      ${ts['largest_win']:>12,.2f}")
            print(f"  Largest Loss:     ${ts['largest_loss']:>12,.2f}")
            print(f"  Avg Holding Days: {ts['avg_holding_days']:>12.1f}")

    print("=" * 55)

    if show_environment:
        print_environment(engine)
