"""`qr backtest` CLI — single-envelope contract on run / list / show."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from quant_researcher.cli import app
from quant_researcher.db import Base
from quant_researcher.models.prices import DailyPrice

runner = CliRunner()


def _json_lines(output: str) -> list[dict]:
    return [json.loads(ln) for ln in output.strip().split("\n") if ln.strip()]


@pytest.fixture
def memory_db():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    # seed a tradeable price series for AAPL
    with factory() as sess:
        start = date(2023, 1, 2)
        for i in range(160):
            px = 100 + 20 * math.sin(i / 12.0) + i * 0.15
            sess.add(
                DailyPrice(
                    symbol="AAPL",
                    trade_date=start + timedelta(days=i),
                    open=px,
                    high=px * 1.01,
                    low=px * 0.99,
                    close=px,
                    adj_close=px,
                    volume=1_000_000,
                )
            )
        sess.commit()
    with patch("quant_researcher.db.session_factory", return_value=factory):
        yield factory


def _run(*args: str):
    return runner.invoke(app, ["backtest", *args])


def test_run_single_envelope_on_success(memory_db) -> None:
    result = _run(
        "run",
        "--strategy", "sma_crossover",
        "--symbols", "AAPL",
        "--start", "2023-01-02",
        "--end", "2023-07-01",
        "--params", "fast_period=5,slow_period=20",
        "--fee", "zero",
    )
    payloads = _json_lines(result.output)
    assert len(payloads) == 1, f"expected 1 envelope, got {len(payloads)}: {payloads}"
    assert payloads[0]["ok"] is True
    assert payloads[0]["data"]["strategy"] == "sma_crossover"
    assert "run_id" in payloads[0]["data"]
    assert result.exit_code == 0


def test_run_missing_strategy_single_failure_envelope(memory_db) -> None:
    result = _run("run", "--symbols", "AAPL", "--start", "2023-01-02", "--end", "2023-07-01")
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["ok"] is False
    assert payloads[0]["error"]["code"] == "missing_strategy"
    assert result.exit_code == 1


def test_run_bad_date_single_failure_envelope(memory_db) -> None:
    result = _run(
        "run",
        "--strategy", "sma_crossover",
        "--symbols", "AAPL",
        "--start", "01/02/2023",  # wrong format
        "--end", "2023-07-01",
    )
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["ok"] is False
    assert payloads[0]["error"]["code"] == "invalid_date"


def test_run_unknown_strategy_single_failure_envelope(memory_db) -> None:
    result = _run(
        "run", "--strategy", "nope", "--symbols", "AAPL",
        "--start", "2023-01-02", "--end", "2023-07-01",
    )
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["ok"] is False
    assert payloads[0]["error"]["code"] == "invalid_backtest_spec"


def test_list_and_show_roundtrip(memory_db) -> None:
    run_result = _run(
        "run", "--strategy", "sma_crossover", "--symbols", "AAPL",
        "--start", "2023-01-02", "--end", "2023-07-01",
        "--params", "fast_period=5,slow_period=20", "--fee", "zero",
    )
    run_id = _json_lines(run_result.output)[0]["data"]["run_id"]

    list_result = _run("list")
    list_payloads = _json_lines(list_result.output)
    assert len(list_payloads) == 1
    assert list_payloads[0]["ok"] is True
    assert list_payloads[0]["data"]["count"] == 1

    show_result = _run("show", run_id)
    show_payloads = _json_lines(show_result.output)
    assert len(show_payloads) == 1
    assert show_payloads[0]["ok"] is True
    assert show_payloads[0]["data"]["run_id"] == run_id
    assert show_payloads[0]["data"]["equity_curve"]  # full curve present


def test_show_not_found_single_failure_envelope(memory_db) -> None:
    result = _run("show", "no-such-run-id")
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["ok"] is False
    assert payloads[0]["error"]["code"] == "not_found"
    assert result.exit_code == 1
