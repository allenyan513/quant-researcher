"""IC / quantile / decay math on synthetic panels (deterministic) + integration."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from quant_researcher.db import Base
from quant_researcher.ledger.engine import HORIZON_DAYS
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.universe import UniverseMember
from quant_researcher.signals.engine import (
    _ic_for_date,
    _ic_series,
    _quantile_buckets,
    _summarize_ic,
    run_signal,
)

_DATES = [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31)]


def _panel(values_by_sym: dict[str, float | None]) -> dict:
    return {d: dict(values_by_sym) for d in _DATES}


def test_ic_plus_one_for_monotonic_factor() -> None:
    factor = _panel({f"S{i}": float(i) for i in range(10)})
    fwd = _panel({f"S{i}": float(i) for i in range(10)})  # same order
    ics = [ic for _d, ic in _ic_series(factor, fwd, _DATES, 5)]
    assert len(ics) == 3
    assert all(ic == pytest.approx(1.0) for ic in ics)
    s = _summarize_ic(ics)
    assert s["mean_ic"] == pytest.approx(1.0)
    assert s["hit_rate"] == 1.0
    assert s["n_dates"] == 3


def test_ic_minus_one_for_inverted_factor() -> None:
    factor = _panel({f"S{i}": float(i) for i in range(10)})
    fwd = _panel({f"S{i}": float(-i) for i in range(10)})
    ics = [ic for _d, ic in _ic_series(factor, fwd, _DATES, 5)]
    assert _summarize_ic(ics)["mean_ic"] == pytest.approx(-1.0)


@pytest.mark.filterwarnings("ignore:An input array is constant")
def test_constant_factor_yields_no_usable_ic() -> None:
    factor = _panel({f"S{i}": 5.0 for i in range(10)})  # zero variance → spearman nan
    fwd = _panel({f"S{i}": float(i) for i in range(10)})
    assert _ic_for_date(factor[_DATES[0]], fwd[_DATES[0]], 5) is None
    assert _summarize_ic([ic for _d, ic in _ic_series(factor, fwd, _DATES, 5)])["n_dates"] == 0


def test_summarize_ic_guards() -> None:
    assert _summarize_ic([])["n_dates"] == 0
    one = _summarize_ic([0.1])
    assert one["mean_ic"] == pytest.approx(0.1)
    assert one["ic_std"] is None and one["ic_ir"] is None and one["t_stat"] is None


def test_min_symbols_drops_thin_dates() -> None:
    factor = _panel({f"S{i}": float(i) for i in range(3)})  # only 3 symbols
    fwd = _panel({f"S{i}": float(i) for i in range(3)})
    assert _summarize_ic([ic for _d, ic in _ic_series(factor, fwd, _DATES, 5)])["n_dates"] == 0


def test_nan_pairs_dropped_before_spearman() -> None:
    vals = {f"S{i}": float(i) for i in range(10)}
    vals["S0"] = None  # drop one
    factor = _panel(vals)
    fwd = _panel({f"S{i}": float(i) for i in range(10)})
    # 9 finite pairs, still monotonic → IC 1.0 (not nan)
    assert _ic_for_date(factor[_DATES[0]], fwd[_DATES[0]], 5) == pytest.approx(1.0)


def test_quantile_spread_sign_and_monotonicity() -> None:
    factor = _panel({f"S{i}": float(i) for i in range(10)})
    fwd = _panel({f"S{i}": float(i) for i in range(10)})
    q = _quantile_buckets(factor, fwd, _DATES, 5, direction=1)
    means = q["bucket_mean_return"]
    assert means == sorted(means)  # increasing
    assert q["long_short_spread"] > 0
    assert q["monotonicity"] == pytest.approx(1.0)


def test_quantile_spread_aligned_uses_direction() -> None:
    factor = _panel({f"S{i}": float(i) for i in range(10)})
    fwd = _panel({f"S{i}": float(i) for i in range(10)})
    q = _quantile_buckets(factor, fwd, _DATES, 5, direction=-1)
    assert q["long_short_spread_aligned"] == pytest.approx(-q["long_short_spread"])


# ----- integration: run_signal on seeded prices -----------------------------


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        d0 = date(2023, 1, 2)
        for k in range(10):
            slope = 0.0002 * (k + 1)
            px = 100.0
            for i in range(400):
                px *= 1 + slope
                sess.add(DailyPrice(symbol=f"S{k}", trade_date=d0 + timedelta(days=i),
                                    close=px, adj_close=px))
            sess.add(UniverseMember(symbol=f"S{k}"))
        sess.commit()
        yield sess


def test_run_signal_integration_json_safe_and_persists(session: Session) -> None:
    from quant_researcher.models.signals import SignalRun

    r = run_signal(session, factor="momentum_12_1", horizon="1m", quantiles=5)
    session.commit()

    assert r.kind == "price"
    assert "mean_ic" in r.ic_summary
    assert set(r.decay) == set(HORIZON_DAYS)
    assert "warnings" in r.coverage
    # whole snapshot JSON-safe (no numpy / inf / nan leaked)
    for blob in (r.ic_summary, r.quantiles_result, r.decay, r.coverage):
        json.dumps(blob)
    row = session.get(SignalRun, r.run_id)
    assert row is not None and row.factor == "momentum_12_1"


def test_run_signal_unknown_factor_raises(session: Session) -> None:
    from quant_researcher.signals.factors import FactorError

    with pytest.raises(FactorError):
        run_signal(session, factor="nope")


def test_run_signal_empty_universe_raises() -> None:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        with pytest.raises(ValueError, match="empty universe"):
            run_signal(sess, factor="momentum_12_1")


def test_momentum_window_needs_history() -> None:
    # sanity: with <252 days, momentum_12_1 has no usable dates → honest coverage
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        d0 = date(2024, 1, 2)
        for k in range(6):
            for i in range(60):  # only 60 days
                sess.add(DailyPrice(symbol=f"S{k}", trade_date=d0 + timedelta(days=i),
                                    close=100.0 + i + k, adj_close=100.0 + i + k))
            sess.add(UniverseMember(symbol=f"S{k}"))
        sess.commit()
        r = run_signal(sess, factor="momentum_12_1", horizon="1m")
        assert r.ic_summary["n_dates"] == 0
        assert any("usable rebalance dates" in w for w in r.coverage["warnings"])
        assert math.isclose(r.coverage["avg_symbols_ranked_per_date"], 0.0)
