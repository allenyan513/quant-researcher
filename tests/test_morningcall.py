"""qr morningcall — portfolio briefing builder + persistence + CLI envelope."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from quant_researcher.cli import app
from quant_researcher.db import Base
from quant_researcher.models.decisions import Decision
from quant_researcher.models.holdings import Holding
from quant_researcher.models.morningcall import MorningCallSnapshot
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile
from quant_researcher.research.morningcall import build_morning_call, save_morning_call

runner = CliRunner()
_AS_OF = date(2026, 5, 21)


def _seed(session: Session) -> None:
    session.add_all(
        [
            Holding(account_id="U1", symbol="AAPL", as_of_date=_AS_OF,
                    asset_category="STK", quantity=100, mark_price=200.0,
                    market_value=20000.0, avg_cost=150.0, cost_basis_total=15000.0,
                    unrealized_pnl=5000.0, side="LONG", currency="USD", source="csv"),
            Holding(account_id="U1", symbol="XOM", as_of_date=_AS_OF,
                    asset_category="STK", quantity=200, mark_price=100.0,
                    market_value=10000.0, avg_cost=110.0, cost_basis_total=11000.0,
                    unrealized_pnl=-1000.0, side="LONG", currency="USD", source="csv"),
            Profile(symbol="AAPL", sector="Technology", is_etf=False),
            Profile(symbol="XOM", sector="Energy", is_etf=False),
            DailyPrice(symbol="AAPL", trade_date=date(2026, 5, 20), close=198.0),
            DailyPrice(symbol="AAPL", trade_date=date(2026, 5, 21), close=200.0),
            DailyPrice(symbol="XOM", trade_date=date(2026, 5, 20), close=102.0),
            DailyPrice(symbol="XOM", trade_date=date(2026, 5, 21), close=100.0),
            Decision(decision_id="d1", symbol="AAPL", side="buy",
                     opened_at=_AS_OF, confidence=4),
        ]
    )
    session.commit()


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        yield sess


def test_build_morning_call_portfolio_aggregates(session: Session) -> None:
    _seed(session)
    mc = build_morning_call(session)

    assert mc["holdings_count"] == 2
    p = mc["portfolio"]
    assert p["total_market_value"] == 30000.0
    assert p["total_unrealized_pnl"] == 4000.0
    assert p["currency"] == "USD"
    # AAPL is 20000/30000 = 66.7%
    aapl = next(h for h in mc["holdings"] if h["symbol"] == "AAPL")
    assert aapl["weight_pct"] == pytest.approx(66.667, abs=0.01)
    # sector exposure sorted desc → Technology (66.7%) before Energy
    assert [s["sector"] for s in p["sector_exposure"]] == ["Technology", "Energy"]
    assert p["sector_exposure"][0]["etf"] == "XLK"
    # movers: AAPL +1.01% top, XOM -1.96% bottom
    assert p["top_movers"][0]["symbol"] == "AAPL"
    assert p["bottom_movers"][0]["symbol"] == "XOM"
    # decision linkage
    assert p["decided_positions_count"] == 1
    assert aapl["decision"]["side"] == "buy"


def test_day_pnl_attribution(session: Session) -> None:
    _seed(session)
    p = build_morning_call(session)["portfolio"]
    # AAPL 100×(200−198)=+200 ; XOM 200×(100−102)=−400 → net −200
    assert p["day_pnl"] == pytest.approx(-200.0)
    # prior portfolio value 100×198 + 200×102 = 40200 → −200/40200
    assert p["day_pnl_pct"] == pytest.approx(-0.4975, abs=0.01)
    assert p["top_contributors"][0]["symbol"] == "AAPL"
    assert p["top_contributors"][0]["day_pnl"] == pytest.approx(200.0)
    assert p["top_detractors"][0]["symbol"] == "XOM"
    assert p["top_detractors"][0]["day_pnl"] == pytest.approx(-400.0)


def test_option_day_pnl_uses_100x_multiplier(session: Session) -> None:
    session.add_all(
        [
            Holding(account_id="U1", symbol="OPTX", as_of_date=_AS_OF,
                    asset_category="OPT", quantity=2, market_value=300.0,
                    side="LONG", currency="USD", source="csv"),
            DailyPrice(symbol="OPTX", trade_date=date(2026, 5, 20), close=1.0),
            DailyPrice(symbol="OPTX", trade_date=date(2026, 5, 21), close=1.5),
        ]
    )
    session.commit()
    h = build_morning_call(session)["holdings"][0]
    # 2 contracts × (1.5−1.0) × 100 = 100
    assert h["day_pnl"] == pytest.approx(100.0)


def test_unrealized_fallback_from_avg_cost(session: Session) -> None:
    # CSV holding: no unrealized_pnl / cost_basis_total on the row.
    session.add_all(
        [
            Holding(account_id="U1", symbol="ZZZ", as_of_date=_AS_OF,
                    asset_category="STK", quantity=10, avg_cost=150.0,
                    market_value=2000.0, side="LONG", currency="USD", source="csv"),
            DailyPrice(symbol="ZZZ", trade_date=date(2026, 5, 21), close=200.0),
        ]
    )
    session.commit()
    h = build_morning_call(session)["holdings"][0]
    # (200−150) × 10 = 500 ; pct vs avg-cost basis 150×10=1500 → 33.3%
    assert h["unrealized_pnl"] == pytest.approx(500.0)
    assert h["unrealized_pnl_pct"] == pytest.approx(33.333, abs=0.01)


def test_cash_excluded_from_day_pnl(session: Session) -> None:
    session.add(
        Holding(account_id="U1", symbol="USD.CAD", as_of_date=_AS_OF,
                asset_category="CASH", quantity=-4000, market_value=-5500.0,
                side="SHORT", currency="CAD", source="flex")
    )
    session.commit()
    mc = build_morning_call(session)
    assert mc["holdings"][0]["day_pnl"] is None
    assert mc["portfolio"]["day_pnl"] is None
    assert "cash/forex positions excluded from day P&L attribution" in mc["notes"]


def test_pct_uses_abs_denominator_for_shorts() -> None:
    from quant_researcher.research.morningcall import _pct

    assert _pct(50.0, 200.0) == 25.0  # normal long unchanged
    # short: loss (neg pnl) on negative cost basis must stay NEGATIVE
    assert _pct(-100.0, -1000.0) == -10.0
    assert _pct(5.0, 0) is None
    assert _pct(None, 200.0) is None


def test_build_morning_call_empty_holdings(session: Session) -> None:
    mc = build_morning_call(session, account="NOPE")
    assert mc["holdings_count"] == 0
    assert mc["holdings"] == []
    assert "no holdings for filter" in mc["notes"]


def test_save_morning_call_persists(session: Session) -> None:
    _seed(session)
    mc = build_morning_call(session)
    sid = save_morning_call(session, mc, account=None)
    session.commit()
    row = session.get(MorningCallSnapshot, sid)
    assert row is not None
    assert row.account_id == "__ALL__"
    json.dumps(row.payload)  # JSON-serializable


# ----- CLI single-envelope --------------------------------------------------


@pytest.fixture
def memory_db():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as sess:
        _seed(sess)
    with patch("quant_researcher.db.session_factory", return_value=factory):
        yield factory


def _json_lines(output: str) -> list[dict]:
    return [json.loads(ln) for ln in output.strip().split("\n") if ln.strip()]


def test_morningcall_cli_single_envelope(memory_db) -> None:
    result = runner.invoke(app, ["morningcall"])
    payloads = _json_lines(result.output)
    assert len(payloads) == 1, f"expected 1 envelope, got {len(payloads)}"
    assert payloads[0]["ok"] is True
    assert payloads[0]["data"]["holdings_count"] == 2
    assert result.exit_code == 0


def test_morningcall_cli_save_sets_snapshot_id(memory_db) -> None:
    result = runner.invoke(app, ["morningcall", "--save"])
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["ok"] is True
    assert payloads[0]["snapshot_id"] is not None
