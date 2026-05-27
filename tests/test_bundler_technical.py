"""Technical snapshot in `qr research bundle` — shape + signal correctness."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from quant_researcher.db import Base
from quant_researcher.models.prices import DailyPrice
from quant_researcher.research.bundler import _technical_section, build_bundle


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        yield sess


def _seed_prices(
    session: Session,
    symbol: str,
    closes: np.ndarray,
    volumes: np.ndarray | None = None,
    *,
    end_date: date | None = None,
) -> None:
    """Insert one DailyPrice per element, business-day-stepped."""
    end_date = end_date or date(2026, 5, 27)
    if volumes is None:
        volumes = np.full(len(closes), 1_000_000, dtype=int)
    n = len(closes)
    for i, (c, v) in enumerate(zip(closes, volumes, strict=True)):
        # Step back day-by-day; close enough for tests (we don't care about
        # actual trading-day spacing — indicators operate on bar counts).
        d = end_date - timedelta(days=n - 1 - i)
        session.add(
            DailyPrice(
                symbol=symbol,
                trade_date=d,
                close=float(c),
                adj_close=float(c),
                volume=int(v),
            )
        )
    session.commit()


# ----- shape / structure -------------------------------------------------


def test_technical_section_no_data_returns_none(session: Session) -> None:
    assert _technical_section(session, "GHOST") is None


def test_technical_section_insufficient_data(session: Session) -> None:
    _seed_prices(session, "THIN", np.linspace(100, 110, 30))
    out = _technical_section(session, "THIN")
    assert out is not None
    assert out["insufficient_data"] is True
    assert out["bars"] == 30


def test_build_bundle_includes_technical_key(session: Session) -> None:
    # Even with no daily_prices the key must exist (value = None).
    payload = build_bundle(session, "GHOST")
    assert "technical" in payload
    assert payload["technical"] is None


# ----- signal correctness ------------------------------------------------


def test_technical_section_uptrend(session: Session) -> None:
    # 250 bars of steady uptrend from 100 → 200. After 200 bars, SMA200
    # exists. The latest close (~200) is above SMA20 > SMA50 > SMA200,
    # which is the textbook up-bias.
    closes = np.linspace(100.0, 200.0, 250)
    _seed_prices(session, "UP", closes)

    out = _technical_section(session, "UP")
    assert out is not None and "insufficient_data" not in out

    pa = out["price_action"]
    assert pa["latest_close"] == pytest.approx(200.0)
    assert pa["return_1y_pct"] == pytest.approx(100.0, rel=1e-3)
    assert pa["high_52w"] == pytest.approx(200.0)

    trend = out["trend"]
    assert trend["sma20"] > trend["sma50"] > trend["sma200"]
    assert out["signal_summary"]["trend_bias"] == "up"
    assert out["signal_summary"]["near_52w_extreme"] == "near_high"

    # In a monotone uptrend MACD line is positive (fast EMA leads slow).
    assert out["macd"]["line"] is not None and out["macd"]["line"] > 0
    assert out["signal_summary"]["macd_bias"] == "bullish"


def test_technical_section_oversold_dip(session: Session) -> None:
    # Long flat run, then a sharp 20% dip in the last 20 bars. RSI should
    # drop below 30 on at least one of those bars.
    flat = np.full(230, 100.0)
    dip = np.linspace(100.0, 80.0, 20)
    closes = np.concatenate([flat, dip])
    _seed_prices(session, "DIP", closes)

    out = _technical_section(session, "DIP")
    assert out is not None
    assert len(out["momentum"]["oversold_days_last_60"]) >= 1
    assert out["signal_summary"]["momentum_bias"] == "oversold"


def test_technical_section_volume_spike(session: Session) -> None:
    closes = np.linspace(100.0, 110.0, 250)
    volumes = np.full(250, 1_000_000, dtype=int)
    # Spike at bar -5 (within the last 30-day window).
    volumes[-5] = 5_000_000
    _seed_prices(session, "VOL", closes, volumes)

    out = _technical_section(session, "VOL")
    spikes = out["volume"]["spike_days_last_30"]
    assert any(s["x"] >= 2.0 for s in spikes)
    assert any(s["volume"] == 5_000_000 for s in spikes)


def test_technical_section_uses_close_when_adj_close_missing(session: Session) -> None:
    """Falls back to `close` when `adj_close` is None (FMP 402 case)."""
    end = date(2026, 5, 27)
    n = 100
    for i in range(n):
        d = end - timedelta(days=n - 1 - i)
        session.add(
            DailyPrice(
                symbol="NOADJ",
                trade_date=d,
                close=100.0 + i,  # uptrend on close only
                adj_close=None,
                volume=1_000_000,
            )
        )
    session.commit()

    out = _technical_section(session, "NOADJ")
    assert out is not None
    assert out["adj_close_used"] is False
    # Fell back to close → still produces an uptrend snapshot
    assert out["price_action"]["latest_close"] == pytest.approx(199.0)


def test_technical_section_sma200_null_when_bars_below_200(session: Session) -> None:
    closes = np.linspace(100.0, 150.0, 120)
    _seed_prices(session, "SHORT", closes)

    out = _technical_section(session, "SHORT")
    assert out is not None and "insufficient_data" not in out
    assert out["trend"]["sma200"] is None
    assert out["trend"]["price_vs_sma200_pct"] is None
    # 50/200 cross unavailable too
    assert out["trend"]["last_golden_cross_50_200"] is None
    # But sma20 / sma50 do exist
    assert out["trend"]["sma20"] is not None
    assert out["trend"]["sma50"] is not None
