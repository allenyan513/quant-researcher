"""Factor-research orchestration + IC / quantile / decay math (MG).

`run_signal` is the single entry point (CLI + Python). It loads the price panel
once, builds point-in-time factor + forward-return panels, then computes:
- IC: per-date Spearman rank corr (factor vs forward return), summarized
  (mean / std / IR / t-stat / hit-rate). NaN pairs stripped before scipy.
- Quantiles: equal-count buckets by factor, mean forward return per bucket,
  long-short spread (raw + direction-aligned) + monotonicity.
- Decay: mean IC at each horizon.
Plus a `coverage` honesty block (esp. fundamental quasi-static thinness). All
numbers run through `_to_jsonable` before persist/return (numpy/inf/nan-safe).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import uuid4

import numpy as np
from scipy.stats import spearmanr
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant_researcher.backtest.runner import _to_jsonable
from quant_researcher.contract import code_version
from quant_researcher.ledger.engine import HORIZON_DAYS
from quant_researcher.models.signals import Signal, SignalRun
from quant_researcher.models.universe import UniverseMember
from quant_researcher.signals.factors import FactorSpec, get_factor
from quant_researcher.signals.panel import (
    build_forward_return_panel,
    build_fundamental_panel,
    load_price_panel,
    rebalance_dates,
)

_QUASI_STATIC_DISTINCT = 3  # avg distinct fundamental values/symbol below this = quasi-static

FactorPanel = dict[date, dict[str, float | None]]


@dataclass(frozen=True)
class SignalRunResult:
    run_id: str
    signal_name: str | None
    factor: str
    kind: str
    horizon: str
    quantiles: int
    rebalance: str
    universe_size: int
    ic_summary: dict[str, Any]
    quantiles_result: dict[str, Any]
    decay: dict[str, Any]
    coverage: dict[str, Any]


def run_signal(
    session: Session,
    *,
    factor: str,
    horizon: str = "1m",
    quantiles: int = 5,
    rebalance: str = "monthly",
    symbols: list[str] | None = None,
    min_symbols: int = 5,
    save_name: str | None = None,
    description: str | None = None,
    persist: bool = True,
) -> SignalRunResult:
    """Run factor research end-to-end; add a SignalRun to `session` (caller commits)."""
    spec = get_factor(factor)
    if quantiles < 2:
        raise ValueError("quantiles must be >= 2")
    if horizon not in HORIZON_DAYS:
        raise ValueError(f"unknown horizon {horizon!r} (valid: {', '.join(HORIZON_DAYS)})")

    universe = symbols or sorted(session.scalars(select(UniverseMember.symbol)))
    if not universe:
        raise ValueError("empty universe — `qr universe set` first")

    price_panel = load_price_panel(session, universe)
    dates = rebalance_dates(price_panel, freq=rebalance)
    factor_panel = _build_factor_panel(session, spec, universe, dates, price_panel)
    fwd_panel = build_forward_return_panel(price_panel, dates, HORIZON_DAYS)

    fwd_primary = _fwd_at_horizon(fwd_panel, dates, horizon)
    ics = [ic for _d, ic in _ic_series(factor_panel, fwd_primary, dates, min_symbols)]
    ic_summary = _summarize_ic(ics)
    quantiles_result = _quantile_buckets(
        factor_panel, fwd_primary, dates, quantiles, spec.direction
    )
    decay = _decay_table(factor_panel, fwd_panel, dates, HORIZON_DAYS, min_symbols)
    coverage = _coverage(spec, universe, dates, factor_panel, fwd_primary, ic_summary)

    ic_summary = _to_jsonable(ic_summary)
    quantiles_result = _to_jsonable(quantiles_result)
    decay = _to_jsonable(decay)
    coverage = _to_jsonable(coverage)

    run_id = str(uuid4())
    params = {
        "horizon": horizon,
        "quantiles": quantiles,
        "rebalance": rebalance,
        "min_symbols": min_symbols,
    }
    if persist:
        if save_name:
            session.merge(
                Signal(name=save_name, factor=factor, params=params, description=description)
            )
        session.add(
            SignalRun(
                run_id=run_id,
                signal_name=save_name,
                factor=factor,
                kind=spec.kind,
                params=params,
                ic_summary=ic_summary,
                quantiles=quantiles_result,
                decay=decay,
                coverage=coverage,
                universe_size=len(universe),
                code_version=code_version(),
            )
        )

    return SignalRunResult(
        run_id=run_id,
        signal_name=save_name,
        factor=factor,
        kind=spec.kind,
        horizon=horizon,
        quantiles=quantiles,
        rebalance=rebalance,
        universe_size=len(universe),
        ic_summary=ic_summary,
        quantiles_result=quantiles_result,
        decay=decay,
        coverage=coverage,
    )


# ----- panels ---------------------------------------------------------------


def _build_factor_panel(
    session: Session,
    spec: FactorSpec,
    symbols: list[str],
    dates: list[date],
    price_panel: dict[str, Any],
) -> FactorPanel:
    if spec.kind == "fundamental":
        assert spec.ratio_col is not None
        return build_fundamental_panel(session, symbols, dates, spec.ratio_col)
    assert spec.price_fn is not None
    out: FactorPanel = {}
    for d in dates:
        out[d] = {
            sym: (spec.price_fn(series, d) if (series := price_panel.get(sym)) else None)
            for sym in symbols
        }
    return out


def _fwd_at_horizon(
    fwd_panel: dict[date, dict[str, dict[str, float | None]]],
    dates: list[date],
    horizon: str,
) -> FactorPanel:
    return {d: {sym: rets.get(horizon) for sym, rets in fwd_panel[d].items()} for d in dates}


# ----- metrics --------------------------------------------------------------


def _aligned(
    factor_d: dict[str, float | None], fwd_d: dict[str, float | None]
) -> tuple[np.ndarray, np.ndarray]:
    """Factor + forward-return arrays over symbols with both values finite."""
    fs: list[float] = []
    rs: list[float] = []
    for sym, f in factor_d.items():
        r = fwd_d.get(sym)
        if f is None or r is None:
            continue
        if not (np.isfinite(f) and np.isfinite(r)):
            continue
        fs.append(float(f))
        rs.append(float(r))
    return np.array(fs, dtype=float), np.array(rs, dtype=float)


def _ic_for_date(
    factor_d: dict[str, float | None], fwd_d: dict[str, float | None], min_n: int
) -> float | None:
    f, r = _aligned(factor_d, fwd_d)
    if len(f) < min_n:
        return None
    ic = spearmanr(f, r).statistic  # nan if constant input
    return float(ic) if np.isfinite(ic) else None


def _ic_series(
    factor_panel: FactorPanel, fwd_h: FactorPanel, dates: list[date], min_n: int
) -> list[tuple[date, float]]:
    out: list[tuple[date, float]] = []
    for d in dates:
        ic = _ic_for_date(factor_panel.get(d, {}), fwd_h.get(d, {}), min_n)
        if ic is not None:
            out.append((d, ic))
    return out


def _summarize_ic(ics: list[float]) -> dict[str, Any]:
    k = len(ics)
    if k == 0:
        return {
            "mean_ic": None, "ic_std": None, "ic_ir": None,
            "t_stat": None, "hit_rate": None, "n_dates": 0,
        }
    arr = np.array(ics, dtype=float)
    mean_ic = float(arr.mean())
    std = float(arr.std(ddof=1)) if k >= 2 else None
    ir = (mean_ic / std) if std else None
    t_stat = (ir * np.sqrt(k)) if ir is not None else None
    return {
        "mean_ic": mean_ic,
        "ic_std": std,
        "ic_ir": ir,
        "t_stat": t_stat,
        "hit_rate": float((arr > 0).mean()),
        "n_dates": k,
    }


def _quantile_buckets(
    factor_panel: FactorPanel,
    fwd_h: FactorPanel,
    dates: list[date],
    quantiles: int,
    direction: int,
) -> dict[str, Any]:
    per_bucket: list[list[float]] = [[] for _ in range(quantiles)]
    counts = [0] * quantiles
    for d in dates:
        f, r = _aligned(factor_panel.get(d, {}), fwd_h.get(d, {}))
        if len(f) < quantiles:
            continue
        order = np.argsort(f)  # ascending factor; bucket 0 = lowest
        for qi, idxs in enumerate(np.array_split(order, quantiles)):
            if len(idxs) == 0:
                continue
            per_bucket[qi].append(float(r[idxs].mean()))
            counts[qi] += len(idxs)
    q_mean: list[float | None] = [
        (float(np.mean(v)) if v else None) for v in per_bucket
    ]
    top, bot = q_mean[-1], q_mean[0]
    ls = (top - bot) if (top is not None and bot is not None) else None
    ls_aligned = None
    if ls is not None and direction != 0:
        ls_aligned = ls * (1 if direction >= 0 else -1)
    mono = _monotonicity(q_mean)
    return {
        "quantiles": quantiles,
        "bucket_mean_return": q_mean,
        "long_short_spread": ls,
        "long_short_spread_aligned": ls_aligned,
        "monotonicity": mono,
        "bucket_counts": counts,
    }


def _monotonicity(q_mean: list[float | None]) -> float | None:
    valid = [(i, m) for i, m in enumerate(q_mean) if m is not None]
    if len(valid) < 2:
        return None
    mc = spearmanr([i for i, _ in valid], [m for _, m in valid]).statistic
    return float(mc) if np.isfinite(mc) else None


def _decay_table(
    factor_panel: FactorPanel,
    fwd_panel: dict[date, dict[str, dict[str, float | None]]],
    dates: list[date],
    horizons: dict[str, int],
    min_n: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for h in horizons:
        fwd_h = _fwd_at_horizon(fwd_panel, dates, h)
        ics = [ic for _d, ic in _ic_series(factor_panel, fwd_h, dates, min_n)]
        s = _summarize_ic(ics)
        out[h] = {"mean_ic": s["mean_ic"], "ic_ir": s["ic_ir"], "n_dates": s["n_dates"]}
    return out


def _coverage(
    spec: FactorSpec,
    symbols: list[str],
    dates: list[date],
    factor_panel: FactorPanel,
    fwd_primary: FactorPanel,
    ic_summary: dict[str, Any],
) -> dict[str, Any]:
    per_date_counts = [
        len(_aligned(factor_panel.get(d, {}), fwd_primary.get(d, {}))[0]) for d in dates
    ]
    avg = float(np.mean(per_date_counts)) if per_date_counts else 0.0

    # distinct fundamental observations ~ sum of per-symbol distinct non-None values
    distinct = 0
    for sym in symbols:
        vals = {factor_panel[d].get(sym) for d in dates}
        vals.discard(None)
        distinct += len(vals)
    quasi_static = (
        spec.kind == "fundamental"
        and bool(symbols)
        and (distinct / len(symbols)) < _QUASI_STATIC_DISTINCT
    )

    warnings: list[str] = []
    if quasi_static:
        warnings.append(
            "fundamental factor is quasi-static (few distinct filings in the window): "
            "IC reflects very few effective cross-sections, is autocorrelated, and the "
            "t-stat is overstated — directional only, do not over-interpret."
        )
    if ic_summary["n_dates"] < 6:
        warnings.append(f"only {ic_summary['n_dates']} usable rebalance dates — thin sample.")
    if avg < 10:
        warnings.append(f"avg {avg:.1f} symbols ranked per date — thin cross-section.")

    return {
        "kind": spec.kind,
        "n_rebalance_dates": len(dates),
        "n_dates_with_usable_ic": ic_summary["n_dates"],
        "avg_symbols_ranked_per_date": avg,
        "min_symbols_ranked": min(per_date_counts) if per_date_counts else 0,
        "max_symbols_ranked": max(per_date_counts) if per_date_counts else 0,
        "universe_size": len(symbols),
        "distinct_fundamental_observations": distinct if spec.kind == "fundamental" else None,
        "fundamental_is_quasi_static": quasi_static,
        "warnings": warnings,
    }
