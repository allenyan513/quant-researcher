"""`qr signal` CLI — single-envelope contract on research / factors / runs / show."""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from quant_researcher.cli import app
from quant_researcher.db import Base
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.universe import UniverseMember

runner = CliRunner()


def _json_lines(output: str) -> list[dict]:
    return [json.loads(ln) for ln in output.strip().split("\n") if ln.strip()]


@pytest.fixture
def memory_db():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as sess:
        d0 = date(2023, 1, 2)
        for k in range(8):
            slope = 0.0002 * (k + 1)
            px = 100.0
            for i in range(400):
                px *= 1 + slope
                sess.add(DailyPrice(symbol=f"S{k}", trade_date=d0 + timedelta(days=i),
                                    close=px, adj_close=px))
            sess.add(UniverseMember(symbol=f"S{k}"))
        sess.commit()
    with patch("quant_researcher.db.session_factory", return_value=factory):
        yield factory


def _run(*args: str):
    return runner.invoke(app, ["signal", *args])


def test_factors_single_envelope(memory_db) -> None:
    result = _run("factors")
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["ok"] is True
    assert payloads[0]["data"]["count"] >= 1
    assert any(f["name"] == "momentum_12_1" for f in payloads[0]["data"]["factors"])


def test_research_single_envelope_on_success(memory_db) -> None:
    result = _run("research", "--factor", "momentum_12_1", "--horizon", "1m")
    payloads = _json_lines(result.output)
    assert len(payloads) == 1, f"expected 1 envelope, got {len(payloads)}"
    assert payloads[0]["ok"] is True
    da = payloads[0]["data"]
    assert da["factor"] == "momentum_12_1"
    assert "run_id" in da and "ic_summary" in da and "coverage" in da
    assert result.exit_code == 0


def test_research_unknown_factor_single_failure(memory_db) -> None:
    result = _run("research", "--factor", "nope")
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["ok"] is False
    assert payloads[0]["error"]["code"] == "invalid_factor"


def test_research_bad_horizon_single_failure(memory_db) -> None:
    result = _run("research", "--factor", "momentum_12_1", "--horizon", "2y")
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["error"]["code"] == "invalid_horizon"


def test_research_bad_quantiles_single_failure(memory_db) -> None:
    result = _run("research", "--factor", "momentum_12_1", "--quantiles", "1")
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["error"]["code"] == "invalid_quantiles"


def test_runs_and_show_roundtrip(memory_db) -> None:
    run_result = _run("research", "--factor", "momentum_12_1", "--name", "mom")
    run_id = _json_lines(run_result.output)[0]["data"]["run_id"]

    runs = _json_lines(_run("runs").output)
    assert len(runs) == 1 and runs[0]["ok"] is True
    assert runs[0]["data"]["count"] == 1

    show = _json_lines(_run("show", run_id).output)
    assert len(show) == 1 and show[0]["ok"] is True
    assert show[0]["data"]["run_id"] == run_id
    assert "ic_summary" in show[0]["data"] and "coverage" in show[0]["data"]


def test_show_not_found_single_failure(memory_db) -> None:
    result = _run("show", "no-such-run")
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["ok"] is False
    assert payloads[0]["error"]["code"] == "not_found"
    assert result.exit_code == 1
