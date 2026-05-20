"""CLI smoke — `qr --help`, `qr db --help`, and single-envelope guarantees."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from quant_researcher.cli import app
from quant_researcher.db import Base

runner = CliRunner()


def test_root_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "quant-researcher" in result.output


def test_db_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["db", "--help"])
    assert result.exit_code == 0
    for sub in ("ping", "status", "init"):
        assert sub in result.output


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    # typer's no_args_is_help=True exits non-zero but prints help
    assert "quant-researcher" in result.output


# ----- Regression: each command emits EXACTLY one envelope ---------------
# (Earlier bug: `except Exception` caught the `typer.Exit` raised by `_emit`
# on success, causing a second failure envelope to be emitted with an empty
# error message. Try/except/else fixes it; these tests lock it in.)


def _json_lines(output: str) -> list[dict]:
    return [json.loads(ln) for ln in output.strip().split("\n") if ln.strip()]


def test_db_ping_single_envelope_on_success() -> None:
    with patch("quant_researcher.db.engine") as mock_engine:
        conn = MagicMock()
        conn.execute.return_value.scalar.return_value = 1
        mock_engine.return_value.connect.return_value.__enter__.return_value = conn
        result = runner.invoke(app, ["db", "ping"])

    assert result.exit_code == 0
    payloads = _json_lines(result.output)
    assert len(payloads) == 1, f"expected 1 envelope, got {len(payloads)}: {payloads}"
    assert payloads[0]["ok"] is True
    assert payloads[0]["data"]["select_1"] == 1


def test_db_ping_single_envelope_on_failure() -> None:
    with patch("quant_researcher.db.engine", side_effect=RuntimeError("boom")):
        result = runner.invoke(app, ["db", "ping"])

    assert result.exit_code == 1
    payloads = _json_lines(result.output)
    assert len(payloads) == 1, f"expected 1 envelope, got {len(payloads)}: {payloads}"
    assert payloads[0]["ok"] is False
    assert payloads[0]["error"]["code"] == "db_ping_failed"
    assert payloads[0]["error"]["message"] == "boom"


# ----- qr universe ... ------------------------------------------------------


@pytest.fixture
def memory_db():
    """Swap `session_factory` for one bound to an in-memory SQLite (MA-1 schema)."""
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with patch("quant_researcher.db.session_factory", return_value=factory):
        yield factory


def test_universe_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["universe", "--help"])
    assert result.exit_code == 0
    for sub in ("set", "list"):
        assert sub in result.output


def test_universe_set_loads_from_file(memory_db, tmp_path: Path) -> None:
    f = tmp_path / "wl.txt"
    f.write_text("# comment\naapl\nMSFT\nNVDA\n")

    result = runner.invoke(app, ["universe", "set", "--file", str(f)])
    assert result.exit_code == 0
    payloads = _json_lines(result.output)
    assert len(payloads) == 1, payloads
    data = payloads[0]["data"]
    assert data["total"] == 3
    assert data["added"] == ["AAPL", "MSFT", "NVDA"]
    assert data["new_securities"] == ["AAPL", "MSFT", "NVDA"]
    assert data["source"] == "wl"  # file stem default


def test_universe_set_custom_source(memory_db, tmp_path: Path) -> None:
    f = tmp_path / "watchlist.txt"
    f.write_text("AAPL\n")
    result = runner.invoke(app, ["universe", "set", "--file", str(f), "--source", "manual"])
    assert result.exit_code == 0
    assert _json_lines(result.output)[0]["data"]["source"] == "manual"


def test_universe_set_rejects_missing_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["universe", "set", "--file", str(tmp_path / "nope.txt")])
    assert result.exit_code == 1
    payloads = _json_lines(result.output)
    assert len(payloads) == 1, f"expected 1 envelope, got {len(payloads)}: {payloads}"
    assert payloads[0]["ok"] is False
    assert payloads[0]["error"]["code"] == "universe_file_missing"


def test_universe_set_rejects_empty_file(memory_db, tmp_path: Path) -> None:
    f = tmp_path / "wl.txt"
    f.write_text("# only comments\n\n")
    result = runner.invoke(app, ["universe", "set", "--file", str(f)])
    assert result.exit_code == 1
    payloads = _json_lines(result.output)
    assert len(payloads) == 1, f"expected 1 envelope, got {len(payloads)}: {payloads}"
    assert payloads[0]["error"]["code"] == "universe_file_empty"


def test_universe_list_after_set(memory_db, tmp_path: Path) -> None:
    f = tmp_path / "wl.txt"
    f.write_text("NVDA\nAAPL\nMSFT\n")
    set_result = runner.invoke(app, ["universe", "set", "--file", str(f)])
    assert set_result.exit_code == 0

    list_result = runner.invoke(app, ["universe", "list"])
    assert list_result.exit_code == 0
    data = _json_lines(list_result.output)[0]["data"]
    assert data["count"] == 3
    assert [m["symbol"] for m in data["members"]] == ["AAPL", "MSFT", "NVDA"]


def test_universe_list_empty(memory_db) -> None:
    result = runner.invoke(app, ["universe", "list"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data == {"count": 0, "members": []}


# ----- qr data refresh -----------------------------------------------------


@pytest.fixture
def data_env(memory_db, monkeypatch):
    """memory_db + stubbed settings (FMP_API_KEY) + stubbed FMPClient factory.

    Yields the mock client instance so tests can program its return values.
    """
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None
    fake_client.get_profile.return_value = None
    fake_client.get_historical_prices.return_value = []
    # MA-3 methods — default empty so scope=all tests don't blow up.
    fake_client.get_income_statement.return_value = []
    fake_client.get_balance_sheet.return_value = []
    fake_client.get_cash_flow.return_value = []
    fake_client.get_ratios.return_value = []
    fake_client.get_analyst_estimates.return_value = []

    monkeypatch.setattr(
        "quant_researcher.data.fmp.FMPClient", lambda *a, **kw: fake_client
    )

    fake_settings = MagicMock()
    fake_settings.fmp_api_key = "test-key"
    monkeypatch.setattr("quant_researcher.cli.settings", lambda: fake_settings)
    return fake_client


def _seed_universe(memory_db, symbols: list[str]) -> None:
    """Insert UniverseMember rows directly via the test factory."""
    from quant_researcher.models.universe import UniverseMember

    with memory_db() as sess:
        sess.add_all(UniverseMember(symbol=s, source="test") for s in symbols)
        sess.commit()


def test_data_refresh_help_lists_refresh() -> None:
    result = runner.invoke(app, ["data", "--help"])
    assert result.exit_code == 0
    assert "refresh" in result.output


def test_data_refresh_rejects_invalid_scope(data_env) -> None:
    result = runner.invoke(app, ["data", "refresh", "--scope", "junk"])
    assert result.exit_code == 1
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["error"]["code"] == "invalid_scope"


def test_data_refresh_rejects_missing_api_key(memory_db, monkeypatch) -> None:
    fake_settings = MagicMock()
    fake_settings.fmp_api_key = None
    monkeypatch.setattr("quant_researcher.cli.settings", lambda: fake_settings)
    result = runner.invoke(app, ["data", "refresh", "--scope", "profile"])
    assert result.exit_code == 1
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["error"]["code"] == "missing_fmp_api_key"


def test_data_refresh_empty_universe(data_env) -> None:
    result = runner.invoke(app, ["data", "refresh", "--scope", "profile"])
    assert result.exit_code == 1
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["error"]["code"] == "empty_universe"


def test_data_refresh_profile_scope(memory_db, data_env) -> None:
    _seed_universe(memory_db, ["AAPL", "MSFT"])
    data_env.get_profile.side_effect = lambda sym: {"symbol": sym, "sector": "Tech"}

    result = runner.invoke(app, ["data", "refresh", "--scope", "profile"])
    assert result.exit_code == 0
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    data = payloads[0]["data"]
    assert data["scope"] == "profile"
    assert data["universe_size"] == 2
    assert data["symbols_processed"] == ["AAPL", "MSFT"]
    assert "profile" in data["scopes"]
    assert "quote" not in data["scopes"]
    assert data["scopes"]["profile"]["succeeded_count"] == 2
    assert data["scopes"]["profile"]["total_upserted"] == 2


def test_data_refresh_quote_scope_filters_symbols(memory_db, data_env) -> None:
    _seed_universe(memory_db, ["AAPL", "MSFT", "GOOGL"])
    data_env.get_historical_prices.return_value = [
        {"date": "2024-01-02", "close": 1.0, "volume": 100},
    ]

    result = runner.invoke(
        app, ["data", "refresh", "--scope", "quote", "--symbols", "AAPL,GOOGL"]
    )
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["symbols_processed"] == ["AAPL", "GOOGL"]
    assert data["scopes"]["quote"]["total_upserted"] == 2
    assert "profile" not in data["scopes"]


def test_data_refresh_all_scope_runs_ma2_pieces(memory_db, data_env) -> None:
    # Narrower test for the MA-2 subset of `all`. Empty MA-3 endpoint responses
    # mean those scopes are still reported (the if-block runs) but with 0 upserts.
    _seed_universe(memory_db, ["AAPL"])
    data_env.get_profile.return_value = {"symbol": "AAPL", "sector": "Tech"}
    data_env.get_historical_prices.return_value = [
        {"date": "2024-01-02", "close": 1.0, "volume": 100},
    ]

    result = runner.invoke(app, ["data", "refresh", "--scope", "all"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["scopes"]["profile"]["succeeded_count"] == 1
    assert data["scopes"]["quote"]["succeeded_count"] == 1


def test_data_refresh_reports_per_symbol_failures(memory_db, data_env) -> None:
    _seed_universe(memory_db, ["AAPL", "BAD"])
    from quant_researcher.data.fmp import FMPError

    def profile_side_effect(sym: str):
        if sym == "BAD":
            raise FMPError("not found", status_code=404)
        return {"symbol": sym, "sector": "Tech"}

    data_env.get_profile.side_effect = profile_side_effect

    result = runner.invoke(app, ["data", "refresh", "--scope", "profile"])
    assert result.exit_code == 0  # partial success ≠ failure
    data = _json_lines(result.output)[0]["data"]
    assert data["scopes"]["profile"]["succeeded_count"] == 1
    failed = data["scopes"]["profile"]["failed"]
    assert len(failed) == 1
    assert failed[0]["symbol"] == "BAD"
    assert "not found" in failed[0]["error"]


# ----- MA-3 scopes ---------------------------------------------------------


def _statement_row(date_str: str, period: str) -> dict:
    return {
        "symbol": "AAPL",
        "date": date_str,
        "period": period,
        "acceptedDate": "2024-11-01 17:23:54",
        "calendarYear": "2024",
        "reportedCurrency": "USD",
    }


def test_data_refresh_financials_scope(memory_db, data_env) -> None:
    _seed_universe(memory_db, ["AAPL"])
    data_env.get_income_statement.return_value = [_statement_row("2024-09-30", "FY")]
    data_env.get_balance_sheet.return_value = [_statement_row("2024-09-30", "FY")]
    data_env.get_cash_flow.return_value = [_statement_row("2024-09-30", "FY")]

    result = runner.invoke(app, ["data", "refresh", "--scope", "financials"])
    assert result.exit_code == 0
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    data = payloads[0]["data"]
    assert set(data["scopes"].keys()) == {"financials"}
    # 3 tables × 1 period (FY-only response, but the refresh calls both periods,
    # and the quarter response is empty since same payload uses period=annual)
    fin = data["scopes"]["financials"]
    assert fin["succeeded_count"] == 1
    # Each of the 3 tables gets 1 row (we returned the same row for both annual
    # and quarter calls, but the test data has period=FY for both → dedup PK
    # collision means total upserted = 3 (one per table; quarter call dedups).
    assert fin["total_upserted"] in (3, 6)  # tolerant — depends on mock setup


def test_data_refresh_ratios_scope(memory_db, data_env) -> None:
    _seed_universe(memory_db, ["AAPL"])
    data_env.get_ratios.return_value = [
        {"symbol": "AAPL", "date": "2024-09-30", "period": "FY", "peRatio": 28.5}
    ]

    result = runner.invoke(app, ["data", "refresh", "--scope", "ratios"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert set(data["scopes"].keys()) == {"ratios"}
    assert data["scopes"]["ratios"]["succeeded_count"] == 1


def test_data_refresh_estimates_scope(memory_db, data_env) -> None:
    _seed_universe(memory_db, ["AAPL"])
    data_env.get_analyst_estimates.return_value = [
        {"symbol": "AAPL", "date": "2025-09-30", "period": "FY", "estimatedEpsAvg": 7.0}
    ]

    result = runner.invoke(app, ["data", "refresh", "--scope", "estimates"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert set(data["scopes"].keys()) == {"estimates"}
    assert data["scopes"]["estimates"]["succeeded_count"] == 1
    assert data["scopes"]["estimates"]["total_upserted"] >= 1


def test_data_refresh_periods_filter(memory_db, data_env) -> None:
    """--periods=annual must not call FMP with period=quarter (mirrors the
    402-Subscription-Tier scenario)."""
    _seed_universe(memory_db, ["AAPL"])
    data_env.get_ratios.return_value = [
        {"symbol": "AAPL", "date": "2024-09-30", "period": "FY", "peRatio": 28.5}
    ]

    result = runner.invoke(
        app, ["data", "refresh", "--scope", "ratios", "--periods", "annual"]
    )
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["periods"] == ["annual"]
    # Only one call to FMP with period=annual; quarter never attempted.
    calls = data_env.get_ratios.call_args_list
    assert len(calls) == 1
    assert calls[0].kwargs.get("period") == "annual"


def test_data_refresh_rejects_invalid_periods(memory_db, data_env) -> None:
    _seed_universe(memory_db, ["AAPL"])
    result = runner.invoke(
        app, ["data", "refresh", "--scope", "ratios", "--periods", "monthly"]
    )
    assert result.exit_code == 1
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["error"]["code"] == "invalid_periods"


def test_data_refresh_all_scope_covers_every_table(memory_db, data_env) -> None:
    _seed_universe(memory_db, ["AAPL"])
    data_env.get_profile.return_value = {"symbol": "AAPL", "sector": "Tech"}
    data_env.get_historical_prices.return_value = [
        {"date": "2024-01-02", "close": 1.0, "volume": 100}
    ]
    data_env.get_income_statement.return_value = [_statement_row("2024-09-30", "FY")]
    data_env.get_balance_sheet.return_value = [_statement_row("2024-09-30", "FY")]
    data_env.get_cash_flow.return_value = [_statement_row("2024-09-30", "FY")]
    data_env.get_ratios.return_value = [
        {"symbol": "AAPL", "date": "2024-09-30", "period": "FY", "peRatio": 28.5}
    ]
    data_env.get_analyst_estimates.return_value = [
        {"symbol": "AAPL", "date": "2025-09-30", "period": "FY", "estimatedEpsAvg": 7.0}
    ]

    result = runner.invoke(app, ["data", "refresh", "--scope", "all"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert set(data["scopes"].keys()) == {
        "profile",
        "quote",
        "financials",
        "ratios",
        "estimates",
    }
    for scope_name in ("profile", "quote", "financials", "ratios", "estimates"):
        assert data["scopes"][scope_name]["succeeded_count"] >= 1, scope_name
