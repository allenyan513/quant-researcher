"""Screen engine — end-to-end with in-memory SQLite + seeded warehouse rows."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from quant_researcher.db import Base
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile
from quant_researcher.models.ratios import FinancialRatios
from quant_researcher.models.screens import Screen, ScreenRun
from quant_researcher.models.universe import UniverseMember
from quant_researcher.screen.engine import (
    build_symbol_state,
    diff_runs,
    load_price_series,
    run_screen,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        yield sess


def _seed_universe(session: Session, symbols: list[str]) -> None:
    session.add_all(UniverseMember(symbol=s, source="test") for s in symbols)
    session.commit()


def _seed_profile(
    session: Session, symbol: str, sector: str, mkt_cap: float
) -> None:
    session.add(
        Profile(
            symbol=symbol,
            sector=sector,
            beta=1.0,
            raw={"mktCap": mkt_cap, "companyName": symbol},
            known_at=datetime.now(UTC),
        )
    )
    session.commit()


def _seed_ratio(
    session: Session,
    symbol: str,
    *,
    pe: float,
    peg: float | None = None,
    fcf_yield: float | None = None,
    fiscal_date: date | None = None,
) -> None:
    session.add(
        FinancialRatios(
            symbol=symbol,
            period="FY",
            fiscal_date=fiscal_date or date(2024, 9, 30),
            pe_ratio=pe,
            peg_ratio=peg,
            fcf_yield=fcf_yield,
            known_at=datetime.now(UTC),
        )
    )
    session.commit()


def _seed_price_series(
    session: Session, symbol: str, closes: list[float]
) -> None:
    today = date.today()
    rows = []
    for i, c in enumerate(closes):
        rows.append(
            DailyPrice(
                symbol=symbol,
                trade_date=today - timedelta(days=len(closes) - 1 - i),
                close=c,
                volume=1000,
            )
        )
    session.add_all(rows)
    session.commit()


# ----- build_symbol_state --------------------------------------------------


def test_state_combines_profile_ratios_prices(session: Session) -> None:
    _seed_profile(session, "AAPL", "Technology", 3e12)
    _seed_ratio(session, "AAPL", pe=28.5, peg=2.1, fcf_yield=0.04)
    _seed_price_series(session, "AAPL", [150.0, 152.0, 155.0])

    state = build_symbol_state(session, ["AAPL"])
    assert state["AAPL"]["sector"] == "Technology"
    assert state["AAPL"]["market_cap"] == 3e12
    assert state["AAPL"]["pe"] == 28.5
    assert state["AAPL"]["peg"] == 2.1
    assert state["AAPL"]["fcf_yield"] == 0.04
    assert state["AAPL"]["close"] == 155.0


def test_state_uses_latest_annual_ratio(session: Session) -> None:
    _seed_profile(session, "AAPL", "Technology", 3e12)
    _seed_ratio(session, "AAPL", pe=30.0, fiscal_date=date(2023, 9, 30))
    _seed_ratio(session, "AAPL", pe=28.0, fiscal_date=date(2024, 9, 30))

    state = build_symbol_state(session, ["AAPL"])
    assert state["AAPL"]["pe"] == 28.0


def test_state_handles_missing_data(session: Session) -> None:
    # No profile, no ratios, no prices.
    state = build_symbol_state(session, ["GHOST"])
    assert state.get("GHOST", {}) == {}


# ----- run_screen ----------------------------------------------------------


def test_run_screen_fundamental_filter(session: Session) -> None:
    _seed_universe(session, ["AAPL", "MSFT", "GHOST"])
    _seed_profile(session, "AAPL", "Technology", 3e12)
    _seed_ratio(session, "AAPL", pe=20.0)
    _seed_profile(session, "MSFT", "Technology", 2e12)
    _seed_ratio(session, "MSFT", pe=35.0)

    result = run_screen(session, expr="pe < 30")
    session.commit()

    # AAPL passes (pe=20), MSFT fails (pe=35), GHOST excluded (no data).
    assert result.result_symbols == ["AAPL"]
    assert result.universe_size == 3
    assert result.run_id  # UUID-ish


def test_run_screen_persists_run_row(session: Session) -> None:
    _seed_universe(session, ["AAPL"])
    _seed_profile(session, "AAPL", "Technology", 3e12)
    _seed_ratio(session, "AAPL", pe=20.0)

    result = run_screen(session, expr="pe < 30")
    session.commit()

    row = session.get(ScreenRun, result.run_id)
    assert row is not None
    assert row.expr == "pe < 30"
    assert row.result_symbols == ["AAPL"]
    assert row.universe_size == 1
    assert row.expr_hash  # set


def test_run_screen_save_name_creates_screen(session: Session) -> None:
    _seed_universe(session, ["AAPL"])
    _seed_profile(session, "AAPL", "Technology", 3e12)
    _seed_ratio(session, "AAPL", pe=20.0)

    run_screen(session, expr="pe < 30", save_name="cheap_tech", description="cheap")
    session.commit()

    screen = session.get(Screen, "cheap_tech")
    assert screen is not None
    assert screen.expr == "pe < 30"
    assert screen.description == "cheap"


def test_run_screen_technical_filter(session: Session) -> None:
    _seed_universe(session, ["UP", "DOWN"])
    # 60 bars rising → MACD golden cross within window.
    _seed_price_series(session, "UP", list(np.linspace(100, 200, 80)))
    # 60 bars flat → no cross.
    _seed_price_series(session, "DOWN", [100.0] * 80)

    result = run_screen(session, technical="macd_golden_cross[60]")
    session.commit()

    assert "UP" in result.result_symbols
    assert "DOWN" not in result.result_symbols


def test_run_screen_combined_expr_and_technical(session: Session) -> None:
    _seed_universe(session, ["AAPL", "MSFT"])
    _seed_profile(session, "AAPL", "Technology", 3e12)
    _seed_ratio(session, "AAPL", pe=20.0)
    _seed_profile(session, "MSFT", "Technology", 2e12)
    _seed_ratio(session, "MSFT", pe=20.0)
    # Both pass fundamental; only AAPL passes technical.
    _seed_price_series(session, "AAPL", list(np.linspace(100, 200, 80)))
    _seed_price_series(session, "MSFT", [100.0] * 80)

    result = run_screen(
        session, expr="pe < 30", technical="macd_golden_cross[60]"
    )
    session.commit()

    assert result.result_symbols == ["AAPL"]


def test_run_screen_subset_symbols(session: Session) -> None:
    _seed_universe(session, ["AAPL", "MSFT", "NVDA"])
    for s in ("AAPL", "MSFT", "NVDA"):
        _seed_profile(session, s, "Technology", 1e12)
        _seed_ratio(session, s, pe=20.0)

    result = run_screen(session, expr="pe < 30", symbols=["AAPL", "MSFT"])
    session.commit()

    assert sorted(result.result_symbols) == ["AAPL", "MSFT"]
    assert result.universe_size == 2  # restricted universe


def test_run_screen_requires_some_predicate(session: Session) -> None:
    _seed_universe(session, ["AAPL"])
    with pytest.raises(ValueError, match="at least one"):
        run_screen(session)


def test_run_screen_rejects_empty_universe(session: Session) -> None:
    with pytest.raises(ValueError, match="empty universe"):
        run_screen(session, expr="pe < 30")


# ----- load_price_series ---------------------------------------------------


def test_load_price_series_returns_sorted_arrays(session: Session) -> None:
    _seed_price_series(session, "AAPL", [100.0, 105.0, 110.0])
    closes, volumes = load_price_series(session, "AAPL")
    assert list(closes) == [100.0, 105.0, 110.0]
    assert len(volumes) == 3


def test_load_price_series_empty_for_unknown(session: Session) -> None:
    closes, volumes = load_price_series(session, "NOPE")
    assert len(closes) == 0
    assert len(volumes) == 0


# ----- diff_runs -----------------------------------------------------------


def test_diff_runs(session: Session) -> None:
    _seed_universe(session, ["AAPL", "MSFT", "NVDA", "TSLA"])
    for s in ("AAPL", "MSFT", "NVDA", "TSLA"):
        _seed_profile(session, s, "Technology", 1e12)
    _seed_ratio(session, "AAPL", pe=20.0)
    _seed_ratio(session, "MSFT", pe=20.0)

    r1 = run_screen(session, expr="pe < 30", save_name="cheap")
    session.commit()

    # Add NVDA's ratio so the second run includes it.
    _seed_ratio(session, "NVDA", pe=25.0)
    # Bump MSFT pe above threshold.
    msft = session.scalars(
        select(FinancialRatios).where(FinancialRatios.symbol == "MSFT")
    ).one()
    msft.pe_ratio = 40.0
    session.commit()

    r2 = run_screen(session, expr="pe < 30", save_name="cheap")
    session.commit()

    d = diff_runs(session, r1.run_id, r2.run_id)
    assert d["added"] == ["NVDA"]
    assert d["removed"] == ["MSFT"]
    assert d["kept"] == ["AAPL"]


def test_diff_runs_unknown_id(session: Session) -> None:
    with pytest.raises(ValueError, match="unknown"):
        diff_runs(session, "missing-1", "missing-2")
