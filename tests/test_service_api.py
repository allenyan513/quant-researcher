"""qr-service Phase 0 skeleton — /healthz, /value, and the new-table round-trip.

The endpoints are sync `def`; FastAPI's TestClient drives them through the
threadpool exactly as uvicorn would. We patch `quant_researcher.db.engine` /
`session_factory` (and the tool wrapper) onto in-memory SQLite, mirroring the
CLI tests' `memory_db` pattern, so no real DB / FMP is touched.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from quant_researcher.db import Base
from quant_researcher.models import RawEvent, TradingSignal
from quant_researcher.service.api import app

client = TestClient(app)


# ----- /healthz -------------------------------------------------------------


def test_healthz_ok() -> None:
    eng = create_engine("sqlite://", future=True)
    with patch("quant_researcher.db.engine", return_value=eng):
        resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["db"] == "ok"


def test_healthz_db_unreachable() -> None:
    with patch("quant_researcher.db.engine", side_effect=RuntimeError("boom")):
        resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "db_unreachable"
    assert body["error"]["message"] == "boom"


# ----- /value/{symbol} ------------------------------------------------------


def test_value_success_wraps_tool_in_envelope() -> None:
    fake = {"symbol": "NVDA", "fair_value_per_share_mean": 123.4}
    with patch(
        "quant_researcher.service.tools.value_company_tool", return_value=fake
    ) as tool:
        resp = client.get("/value/nvda")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == fake
    # endpoint forwards the raw symbol; the tool upper-cases it internally
    tool.assert_called_once_with("nvda", model="all")


def test_value_invalid_model_is_400() -> None:
    resp = client.get("/value/NVDA", params={"model": "bogus"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_model"


def test_value_tool_failure_is_500() -> None:
    with patch(
        "quant_researcher.service.tools.value_company_tool",
        side_effect=RuntimeError("no data for ZZZZ"),
    ):
        resp = client.get("/value/ZZZZ")
    assert resp.status_code == 500
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "valuation_failed"
    assert "no data for ZZZZ" in body["error"]["message"]


# ----- new tables (Event / TradingSignal message contract) ------------------


def test_event_and_signal_tables_roundtrip() -> None:
    eng = create_engine("sqlite://", future=True)
    Base.metadata.create_all(eng)  # builds raw_events + trading_signals among others
    factory = sessionmaker(bind=eng, expire_on_commit=False, future=True)

    with factory() as session, session.begin():
        session.add(
            RawEvent(
                id="evt-1",
                source="fmp",
                external_id="grades:NVDA:2026-05-27",
                symbol="NVDA",
                event_type="grade_change",
                raw={"newGrade": "Strong Buy", "action": "maintain"},
            )
        )
        session.add(
            TradingSignal(
                id="sig-1",
                event_id="evt-1",
                symbol="NVDA",
                direction="buy",
                target_price=425.0,
                stop_loss=300.0,
                horizon_days=90,
                conviction="medium",
                generated_by="llm",
                status="open",
            )
        )

    with factory() as session:
        event = session.scalars(select(RawEvent)).one()
        signal = session.scalars(select(TradingSignal)).one()
    assert event.event_type == "grade_change"
    assert event.raw["action"] == "maintain"
    assert signal.event_id == event.id
    assert (signal.direction, signal.status, signal.horizon_days) == ("buy", "open", 90)
