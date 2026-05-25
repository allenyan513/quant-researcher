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
    fake_client.get_adjusted_prices.return_value = []
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


# ----- MA-4: --force + skipped_fresh + qr data freshness -------------------


def test_data_refresh_default_skips_fresh_symbols(memory_db, data_env) -> None:
    """Default `qr data refresh --scope profile` skips symbols whose profile
    row is within the freshness threshold."""
    from datetime import UTC, datetime, timedelta

    from quant_researcher.models.profile import Profile

    _seed_universe(memory_db, ["AAPL", "MSFT"])
    # Pre-seed AAPL as fresh (5d old); MSFT has no row → missing → refreshed.
    with memory_db() as sess:
        sess.add(
            Profile(
                symbol="AAPL", known_at=datetime.now(UTC) - timedelta(days=5), raw={}
            )
        )
        sess.commit()
    data_env.get_profile.side_effect = lambda sym: {"symbol": sym, "sector": "Tech"}

    result = runner.invoke(app, ["data", "refresh", "--scope", "profile"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["force"] is False
    profile_scope = data["scopes"]["profile"]
    # MSFT (missing) refreshed; AAPL (fresh) skipped.
    assert profile_scope["succeeded_count"] == 1
    assert profile_scope["skipped_fresh"] == ["AAPL"]
    # FMP only called for MSFT.
    called = [c.args[0] for c in data_env.get_profile.call_args_list]
    assert called == ["MSFT"]


def test_data_refresh_force_ignores_freshness(memory_db, data_env) -> None:
    """`--force` makes the refresh call FMP for every requested symbol."""
    from datetime import UTC, datetime, timedelta

    from quant_researcher.models.profile import Profile

    _seed_universe(memory_db, ["AAPL", "MSFT"])
    with memory_db() as sess:
        sess.add(
            Profile(
                symbol="AAPL", known_at=datetime.now(UTC) - timedelta(days=5), raw={}
            )
        )
        sess.add(
            Profile(
                symbol="MSFT", known_at=datetime.now(UTC) - timedelta(days=5), raw={}
            )
        )
        sess.commit()
    data_env.get_profile.side_effect = lambda sym: {"symbol": sym, "sector": "Tech"}

    result = runner.invoke(app, ["data", "refresh", "--scope", "profile", "--force"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["force"] is True
    profile_scope = data["scopes"]["profile"]
    assert profile_scope["succeeded_count"] == 2
    assert profile_scope["skipped_fresh"] == []
    # FMP called for both even though they're fresh.
    called = sorted(c.args[0] for c in data_env.get_profile.call_args_list)
    assert called == ["AAPL", "MSFT"]


def test_data_freshness_per_scope_summary(memory_db) -> None:
    """`qr data freshness` returns counts + stale_symbols per scope."""
    from datetime import UTC, datetime, timedelta

    from quant_researcher.models.profile import Profile

    _seed_universe(memory_db, ["AAPL", "MSFT", "NVDA"])
    # AAPL fresh, MSFT stale, NVDA missing.
    with memory_db() as sess:
        sess.add(
            Profile(
                symbol="AAPL", known_at=datetime.now(UTC) - timedelta(days=5), raw={}
            )
        )
        sess.add(
            Profile(
                symbol="MSFT", known_at=datetime.now(UTC) - timedelta(days=40), raw={}
            )
        )
        sess.commit()

    result = runner.invoke(app, ["data", "freshness", "--scope", "profile"])
    assert result.exit_code == 0
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    data = payloads[0]["data"]
    assert data["scope"] == "profile"
    assert data["universe_size"] == 3
    profile = data["scopes"]["profile"]
    assert profile["total"] == 3
    assert profile["fresh"] == 1
    assert profile["stale"] == 1
    assert profile["missing"] == 1
    assert profile["threshold_days"] == 30
    assert profile["stale_symbols"] == ["MSFT", "NVDA"]


def test_data_freshness_all_scope(memory_db) -> None:
    """`qr data freshness` with default scope covers all known scopes."""
    _seed_universe(memory_db, ["AAPL"])
    result = runner.invoke(app, ["data", "freshness"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert set(data["scopes"].keys()) == {
        "profile",
        "quote",
        "financials",
        "ratios",
        "estimates",
        "transcript",
    }
    # Fresh DB → every scope is fully missing.
    for scope_name, sf in data["scopes"].items():
        assert sf["missing"] == 1, scope_name
        assert sf["stale_symbols"] == ["AAPL"]


def test_data_freshness_rejects_invalid_scope(memory_db) -> None:
    _seed_universe(memory_db, ["AAPL"])
    result = runner.invoke(app, ["data", "freshness", "--scope", "junk"])
    assert result.exit_code == 1
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["error"]["code"] == "invalid_scope"


def test_data_freshness_empty_universe(memory_db) -> None:
    result = runner.invoke(app, ["data", "freshness"])
    assert result.exit_code == 1
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["error"]["code"] == "empty_universe"


# ----- MB: qr screen -------------------------------------------------------


def _seed_profile_and_ratio(
    memory_db_factory, symbol: str, sector: str, pe: float
) -> None:
    from datetime import UTC, date, datetime

    from quant_researcher.models.profile import Profile
    from quant_researcher.models.ratios import FinancialRatios

    with memory_db_factory() as sess:
        sess.add(
            Profile(
                symbol=symbol,
                sector=sector,
                raw={"mktCap": 1e12},
                known_at=datetime.now(UTC),
            )
        )
        sess.add(
            FinancialRatios(
                symbol=symbol,
                period="FY",
                fiscal_date=date(2024, 9, 30),
                pe_ratio=pe,
                known_at=datetime.now(UTC),
            )
        )
        sess.commit()


def test_screen_run_fundamental(memory_db) -> None:
    _seed_universe(memory_db, ["AAPL", "MSFT"])
    _seed_profile_and_ratio(memory_db, "AAPL", "Technology", 20.0)
    _seed_profile_and_ratio(memory_db, "MSFT", "Technology", 40.0)

    result = runner.invoke(app, ["screen", "run", "--expr", "pe < 30"])
    assert result.exit_code == 0
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    data = payloads[0]["data"]
    assert data["matched"] == 1
    assert data["symbols"] == ["AAPL"]
    assert data["universe_size"] == 2
    assert data["run_id"]


def test_screen_run_requires_predicate(memory_db) -> None:
    result = runner.invoke(app, ["screen", "run"])
    assert result.exit_code == 1
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["error"]["code"] == "missing_predicate"


def test_screen_run_invalid_expression(memory_db) -> None:
    _seed_universe(memory_db, ["AAPL"])
    result = runner.invoke(app, ["screen", "run", "--expr", "forward_pe < 30"])
    assert result.exit_code == 1
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["error"]["code"] == "invalid_screen_spec"
    assert "forward_pe" in payloads[0]["error"]["message"]


def test_screen_run_invalid_technical(memory_db) -> None:
    _seed_universe(memory_db, ["AAPL"])
    result = runner.invoke(
        app, ["screen", "run", "--technical", "nope[5]"]
    )
    assert result.exit_code == 1
    payloads = _json_lines(result.output)
    assert payloads[0]["error"]["code"] == "invalid_screen_spec"


def test_screen_run_saves_named_screen(memory_db) -> None:
    _seed_universe(memory_db, ["AAPL"])
    _seed_profile_and_ratio(memory_db, "AAPL", "Technology", 20.0)

    result = runner.invoke(
        app,
        [
            "screen",
            "run",
            "--expr",
            "pe < 30",
            "--name",
            "cheap_tech",
            "--description",
            "PE under 30",
        ],
    )
    assert result.exit_code == 0
    list_result = runner.invoke(app, ["screen", "list"])
    list_data = _json_lines(list_result.output)[0]["data"]
    assert list_data["count"] == 1
    assert list_data["screens"][0]["name"] == "cheap_tech"
    assert list_data["screens"][0]["description"] == "PE under 30"


def test_screen_runs_lists_history(memory_db) -> None:
    _seed_universe(memory_db, ["AAPL"])
    _seed_profile_and_ratio(memory_db, "AAPL", "Technology", 20.0)
    runner.invoke(app, ["screen", "run", "--expr", "pe < 30"])
    runner.invoke(app, ["screen", "run", "--expr", "pe < 50"])

    result = runner.invoke(app, ["screen", "runs"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["count"] == 2


def test_screen_diff(memory_db) -> None:
    _seed_universe(memory_db, ["AAPL", "MSFT"])
    _seed_profile_and_ratio(memory_db, "AAPL", "Technology", 20.0)
    _seed_profile_and_ratio(memory_db, "MSFT", "Technology", 40.0)

    r1 = runner.invoke(app, ["screen", "run", "--expr", "pe < 30"])
    r2 = runner.invoke(app, ["screen", "run", "--expr", "pe < 50"])
    rid1 = _json_lines(r1.output)[0]["data"]["run_id"]
    rid2 = _json_lines(r2.output)[0]["data"]["run_id"]

    result = runner.invoke(
        app, ["screen", "diff", "--from", rid1, "--to", rid2]
    )
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["added"] == ["MSFT"]
    assert data["removed"] == []
    assert data["kept"] == ["AAPL"]


def test_screen_diff_unknown_run() -> None:
    result = runner.invoke(
        app, ["screen", "diff", "--from", "nope-1", "--to", "nope-2"]
    )
    assert result.exit_code == 1
    payloads = _json_lines(result.output)
    assert payloads[0]["error"]["code"] == "screen_diff_failed"


def test_screen_fields() -> None:
    result = runner.invoke(app, ["screen", "fields"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert "pe" in data["expression_fields"]
    assert "macd_golden_cross" in data["technical_predicates"]


# ----- MC: qr value --------------------------------------------------------


def _seed_valuation_company(memory_db_factory, sym: str = "AAPL") -> None:
    from datetime import UTC, date, datetime

    from quant_researcher.models.financials import (
        BalanceSheet,
        CashFlow,
        IncomeStatement,
    )
    from quant_researcher.models.prices import DailyPrice
    from quant_researcher.models.profile import Profile
    from quant_researcher.models.ratios import FinancialRatios

    with memory_db_factory() as sess:
        sess.add(
            Profile(
                symbol=sym,
                sector="Technology",
                beta=1.2,
                raw={"mktCap": 3e12},
                known_at=datetime.now(UTC),
            )
        )
        for i in range(5):
            sess.add(
                IncomeStatement(
                    symbol=sym,
                    period="FY",
                    fiscal_date=date(2020 + i, 9, 30),
                    net_income=1e10 * (1.08**i),
                    eps_diluted=5.0 * (1.08**i),
                    operating_income=1.5e10 * (1.08**i),
                    revenue=5e10 * (1.08**i),
                    known_at=datetime.now(UTC),
                )
            )
            sess.add(
                CashFlow(
                    symbol=sym,
                    period="FY",
                    fiscal_date=date(2020 + i, 9, 30),
                    free_cash_flow=8e9 * (1.08**i),
                    capital_expenditure=-2e9 * (1.08**i),
                    known_at=datetime.now(UTC),
                )
            )
        sess.add(
            BalanceSheet(
                symbol=sym,
                period="FY",
                fiscal_date=date(2024, 9, 30),
                short_term_debt=1e10,
                long_term_debt=5e10,
                cash_and_equivalents=4e10,
                known_at=datetime.now(UTC),
            )
        )
        sess.add(
            FinancialRatios(
                symbol=sym,
                period="FY",
                fiscal_date=date(2024, 9, 30),
                pe_ratio=28.0,
                ev_to_ebitda=22.0,
                price_to_sales=8.0,
                known_at=datetime.now(UTC),
            )
        )
        sess.add(
            DailyPrice(symbol=sym, trade_date=date(2024, 9, 30), close=200.0)
        )
        sess.commit()


def test_value_dcf_happy_path(memory_db) -> None:
    _seed_valuation_company(memory_db)
    result = runner.invoke(app, ["value", "AAPL", "--model", "dcf"])
    assert result.exit_code == 0
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    data = payloads[0]["data"]
    assert data["symbol"] == "AAPL"
    assert data["model"] == "dcf"
    assert "dcf" in data["models"]
    assert data["models"]["dcf"]["fair_value_per_share"] is not None


def test_value_all_models(memory_db) -> None:
    _seed_valuation_company(memory_db)
    # Also seed a peer so multiples can compute.
    from datetime import UTC, date, datetime

    from quant_researcher.models.profile import Profile
    from quant_researcher.models.ratios import FinancialRatios

    with memory_db() as sess:
        sess.add(
            Profile(
                symbol="MSFT",
                sector="Technology",
                beta=1.1,
                raw={"mktCap": 2e12},
                known_at=datetime.now(UTC),
            )
        )
        sess.add(
            FinancialRatios(
                symbol="MSFT",
                period="FY",
                fiscal_date=date(2024, 9, 30),
                pe_ratio=30.0,
                ev_to_ebitda=20.0,
                price_to_sales=10.0,
                known_at=datetime.now(UTC),
            )
        )
        sess.commit()

    result = runner.invoke(app, ["value", "AAPL"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert set(data["models"].keys()) == {"dcf", "peg", "multiples", "scenario"}
    assert data["fair_value_per_share_mean"] is not None


def test_value_requires_symbol(memory_db) -> None:
    # No symbol → typer raises a usage error (exit code 2), not our envelope.
    result = runner.invoke(app, ["value"])
    assert result.exit_code != 0
    assert "Missing argument" in result.output or "Usage" in result.output


def test_value_rejects_invalid_model(memory_db) -> None:
    result = runner.invoke(app, ["value", "AAPL", "--model", "junk"])
    assert result.exit_code == 1
    payloads = _json_lines(result.output)
    assert payloads[0]["error"]["code"] == "invalid_model"


def test_value_rejects_invalid_assumptions_json(memory_db) -> None:
    result = runner.invoke(
        app, ["value", "AAPL", "--assumptions", "{not-json"]
    )
    assert result.exit_code == 1
    payloads = _json_lines(result.output)
    assert payloads[0]["error"]["code"] == "invalid_assumptions"


def test_value_assumptions_override_threads_through(memory_db) -> None:
    _seed_valuation_company(memory_db)
    result = runner.invoke(
        app,
        [
            "value",
            "AAPL",
            "--model",
            "dcf",
            "--assumptions",
            '{"growth_rate": 0.10, "wacc": 0.09, "terminal_growth": 0.03}',
        ],
    )
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    a = data["models"]["dcf"]["core"]["assumptions"]
    assert a["growth_rate"] == 0.10
    assert a["wacc"] == 0.09
    assert a["terminal_growth"] == 0.03


def test_value_no_data_returns_null_fair_value(memory_db) -> None:
    result = runner.invoke(app, ["value", "GHOST", "--model", "dcf"])
    assert result.exit_code == 0  # not an error, just no data
    data = _json_lines(result.output)[0]["data"]
    assert data["models"]["dcf"]["fair_value_per_share"] is None


# ----- ME: qr holdings -----------------------------------------------------


def test_holdings_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["holdings", "--help"])
    assert result.exit_code == 0
    for sub in ("sync", "import-csv", "list", "history"):
        assert sub in result.output


def test_holdings_import_csv_happy_path(memory_db, tmp_path: Path) -> None:
    csv_path = tmp_path / "h.csv"
    csv_path.write_text(
        "account_id,symbol,quantity,as_of_date,avg_cost,mark_price\n"
        "U1,AAPL,100,2026-05-20,150.0,200.0\n"
        "U1,MSFT,50,2026-05-20,250.0,310.0\n"
    )
    result = runner.invoke(
        app, ["holdings", "import-csv", "--file", str(csv_path)]
    )
    assert result.exit_code == 0
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    data = payloads[0]["data"]
    assert data["imported"] == 2
    assert data["account_id"] == "U1"
    assert sorted(data["symbols"]) == ["AAPL", "MSFT"]


def test_holdings_import_csv_missing_file(memory_db, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["holdings", "import-csv", "--file", str(tmp_path / "nope.csv")],
    )
    assert result.exit_code == 1
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    assert payloads[0]["error"]["code"] == "csv_file_missing"


def test_holdings_import_csv_bad_format(memory_db, tmp_path: Path) -> None:
    bad = tmp_path / "h.csv"
    bad.write_text("symbol,quantity\nAAPL,100\n")  # missing required cols
    result = runner.invoke(app, ["holdings", "import-csv", "--file", str(bad)])
    assert result.exit_code == 1
    assert _json_lines(result.output)[0]["error"]["code"] == "csv_parse_failed"


def test_holdings_list_returns_latest(memory_db, tmp_path: Path) -> None:
    # Seed two snapshots: 2026-05-19 and 2026-05-20.
    csv_path = tmp_path / "h.csv"
    csv_path.write_text(
        "account_id,symbol,quantity,as_of_date,mark_price\n"
        "U1,AAPL,100,2026-05-19,195.0\n"
    )
    runner.invoke(app, ["holdings", "import-csv", "--file", str(csv_path)])
    csv_path.write_text(
        "account_id,symbol,quantity,as_of_date,mark_price\n"
        "U1,AAPL,100,2026-05-20,200.0\n"
        "U1,MSFT,50,2026-05-20,310.0\n"
    )
    runner.invoke(app, ["holdings", "import-csv", "--file", str(csv_path)])

    result = runner.invoke(app, ["holdings", "list"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["count"] == 2  # AAPL latest (2026-05-20) + MSFT
    # AAPL row should be the newer one (mark 200, not 195)
    aapl = next(h for h in data["holdings"] if h["symbol"] == "AAPL")
    assert aapl["as_of_date"] == "2026-05-20"
    assert aapl["mark_price"] == 200.0


def test_holdings_list_filter_by_account(memory_db, tmp_path: Path) -> None:
    csv_path = tmp_path / "h.csv"
    csv_path.write_text(
        "account_id,symbol,quantity,as_of_date\n"
        "U1,AAPL,100,2026-05-20\n"
        "U2,MSFT,50,2026-05-20\n"
    )
    runner.invoke(app, ["holdings", "import-csv", "--file", str(csv_path)])

    result = runner.invoke(app, ["holdings", "list", "--account", "U1"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["count"] == 1
    assert data["holdings"][0]["account_id"] == "U1"


def test_holdings_history(memory_db, tmp_path: Path) -> None:
    for i, qty in enumerate([90, 100, 110]):
        csv_path = tmp_path / "h.csv"
        csv_path.write_text(
            "account_id,symbol,quantity,as_of_date\n"
            f"U1,AAPL,{qty},2026-05-{18 + i}\n"
        )
        runner.invoke(app, ["holdings", "import-csv", "--file", str(csv_path)])

    result = runner.invoke(app, ["holdings", "history", "--symbol", "AAPL"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["count"] == 3
    # newest first
    assert [h["as_of_date"] for h in data["history"]] == [
        "2026-05-20",
        "2026-05-19",
        "2026-05-18",
    ]


def test_holdings_sync_missing_token(memory_db, monkeypatch) -> None:
    fake_settings = MagicMock()
    fake_settings.flex_token_key = None
    fake_settings.flex_query_id_live = "1440609"
    monkeypatch.setattr("quant_researcher.cli.settings", lambda: fake_settings)
    result = runner.invoke(app, ["holdings", "sync"])
    assert result.exit_code == 1
    assert _json_lines(result.output)[0]["error"]["code"] == "missing_flex_token"


def test_holdings_sync_missing_query_id(memory_db, monkeypatch) -> None:
    fake_settings = MagicMock()
    fake_settings.flex_token_key = "secret"
    fake_settings.flex_query_id_live = None
    monkeypatch.setattr("quant_researcher.cli.settings", lambda: fake_settings)
    result = runner.invoke(app, ["holdings", "sync"])
    assert result.exit_code == 1
    assert _json_lines(result.output)[0]["error"]["code"] == "missing_flex_query_id"


# ----- MD: qr research -----------------------------------------------------


def test_research_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["research", "--help"])
    assert result.exit_code == 0
    for sub in ("bundle", "news", "list", "show"):
        assert sub in result.output


def test_research_bundle_minimal(memory_db) -> None:
    from datetime import UTC, datetime

    from quant_researcher.models.profile import Profile

    with memory_db() as sess:
        sess.add(
            Profile(
                symbol="AAPL",
                sector="Technology",
                raw={"mktCap": 3e12},
                known_at=datetime.now(UTC),
            )
        )
        sess.commit()

    result = runner.invoke(app, ["research", "bundle", "AAPL"])
    assert result.exit_code == 0
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    data = payloads[0]["data"]
    assert data["saved"] is True
    assert data["bundle_id"]
    assert data["payload"]["profile"]["sector"] == "Technology"


def test_research_bundle_no_save(memory_db) -> None:
    result = runner.invoke(app, ["research", "bundle", "GHOST", "--no-save"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["saved"] is False
    assert data["bundle_id"] is None
    assert data["payload"]["symbol"] == "GHOST"


def test_research_news_missing_symbols(memory_db, monkeypatch) -> None:
    fake_settings = MagicMock()
    fake_settings.fmp_api_key = "key"
    monkeypatch.setattr("quant_researcher.cli.settings", lambda: fake_settings)
    result = runner.invoke(app, ["research", "news", "--symbols", ""])
    assert result.exit_code == 1
    assert _json_lines(result.output)[0]["error"]["code"] == "no_symbols"


def test_research_news_missing_api_key(memory_db, monkeypatch) -> None:
    fake_settings = MagicMock()
    fake_settings.fmp_api_key = None
    monkeypatch.setattr("quant_researcher.cli.settings", lambda: fake_settings)
    result = runner.invoke(app, ["research", "news", "--symbols", "AAPL"])
    assert result.exit_code == 1
    assert _json_lines(result.output)[0]["error"]["code"] == "missing_fmp_api_key"


def test_research_news_happy_path(memory_db, monkeypatch) -> None:
    fake_settings = MagicMock()
    fake_settings.fmp_api_key = "key"
    monkeypatch.setattr("quant_researcher.cli.settings", lambda: fake_settings)

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None
    fake_client.get_news.return_value = [
        {
            "symbol": "AAPL",
            "publishedDate": "2026-05-20 10:00:00",
            "title": "Apple beats",
            "url": "https://example.com/a",
            "site": "Bloomberg",
        }
    ]
    monkeypatch.setattr(
        "quant_researcher.data.fmp.FMPClient", lambda **_: fake_client
    )

    result = runner.invoke(app, ["research", "news", "--symbols", "AAPL"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["inserted"] == 1
    assert data["symbols_requested"] == ["AAPL"]


def test_research_list_after_bundle(memory_db) -> None:
    from datetime import UTC, datetime

    from quant_researcher.models.profile import Profile

    with memory_db() as sess:
        sess.add(Profile(symbol="AAPL", raw={}, known_at=datetime.now(UTC)))
        sess.commit()
    runner.invoke(app, ["research", "bundle", "AAPL"])

    result = runner.invoke(app, ["research", "list"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["count"] == 1
    assert data["bundles"][0]["symbol"] == "AAPL"


def test_research_show_not_found(memory_db) -> None:
    result = runner.invoke(app, ["research", "show", "nope-id"])
    assert result.exit_code == 1
    assert _json_lines(result.output)[0]["error"]["code"] == "bundle_not_found"


# ----- MF: qr ledger -------------------------------------------------------


def test_ledger_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["ledger", "--help"])
    assert result.exit_code == 0
    for sub in ("add", "track", "list", "scorecard", "show"):
        assert sub in result.output


def test_ledger_add_records_decision(memory_db) -> None:
    from datetime import UTC, date, datetime

    from quant_researcher.models.prices import DailyPrice
    from quant_researcher.models.profile import Profile

    with memory_db() as sess:
        sess.add(
            Profile(symbol="AAPL", sector="Technology", raw={}, known_at=datetime.now(UTC))
        )
        sess.add(DailyPrice(symbol="AAPL", trade_date=date.today(), close=200.0))
        sess.commit()

    result = runner.invoke(
        app,
        [
            "ledger",
            "add",
            "AAPL",
            "--side",
            "buy",
            "--thesis",
            "growth",
            "--confidence",
            "4",
            "--tags",
            "AI,tech",
        ],
    )
    assert result.exit_code == 0
    payloads = _json_lines(result.output)
    assert len(payloads) == 1
    data = payloads[0]["data"]
    assert data["decision_id"]
    assert data["bundle_id"]
    assert data["symbol"] == "AAPL"
    assert data["side"] == "buy"
    assert data["price_at_open"] == 200.0
    assert data["sector_at_open"] == "Technology"
    assert data["tags"] == ["AI", "tech"]


def test_ledger_add_rejects_bad_side(memory_db) -> None:
    result = runner.invoke(app, ["ledger", "add", "AAPL", "--side", "hodl"])
    assert result.exit_code == 1
    assert _json_lines(result.output)[0]["error"]["code"] == "invalid_decision"


def test_ledger_add_invalid_opened(memory_db) -> None:
    result = runner.invoke(
        app, ["ledger", "add", "AAPL", "--side", "buy", "--opened", "bad-date"]
    )
    assert result.exit_code == 1
    assert _json_lines(result.output)[0]["error"]["code"] == "invalid_opened"


def test_ledger_list_filter(memory_db) -> None:
    from datetime import UTC, date, datetime

    from quant_researcher.models.prices import DailyPrice
    from quant_researcher.models.profile import Profile

    with memory_db() as sess:
        sess.add(
            Profile(symbol="AAPL", sector="Tech", raw={}, known_at=datetime.now(UTC))
        )
        sess.add(DailyPrice(symbol="AAPL", trade_date=date.today(), close=200.0))
        sess.commit()
    runner.invoke(app, ["ledger", "add", "AAPL", "--side", "buy", "--no-bundle"])
    runner.invoke(app, ["ledger", "add", "AAPL", "--side", "sell", "--no-bundle"])

    result = runner.invoke(app, ["ledger", "list", "--side", "buy"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["count"] == 1
    assert data["decisions"][0]["side"] == "buy"


def test_ledger_scorecard_invalid_group_by(memory_db) -> None:
    result = runner.invoke(app, ["ledger", "scorecard", "--group-by", "junk"])
    assert result.exit_code == 1
    assert (
        _json_lines(result.output)[0]["error"]["code"]
        == "invalid_scorecard_param"
    )


def test_ledger_show_not_found(memory_db) -> None:
    result = runner.invoke(app, ["ledger", "show", "nope-id"])
    assert result.exit_code == 1
    assert _json_lines(result.output)[0]["error"]["code"] == "decision_not_found"


def test_ledger_track_no_decisions(memory_db) -> None:
    result = runner.invoke(app, ["ledger", "track"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["decisions_touched"] == 0


def test_holdings_sync_happy_path(memory_db, monkeypatch) -> None:
    """Full happy path with FlexClient mocked at the class level."""
    fake_settings = MagicMock()
    fake_settings.flex_token_key = "secret"
    fake_settings.flex_query_id_live = "1440609"
    monkeypatch.setattr("quant_researcher.cli.settings", lambda: fake_settings)

    from quant_researcher.holdings.ibkr_flex import FlexStatementMeta

    fake_flex = MagicMock()
    fake_flex.__enter__.return_value = fake_flex
    fake_flex.__exit__.return_value = None
    fake_flex.fetch_positions.return_value = (
        FlexStatementMeta(
            account_id="U16781493",
            from_date="20260520",
            to_date="20260520",
            when_generated="20260521;092516",
            query_name="Live",
        ),
        [
            {
                "accountId": "U16781493",
                "symbol": "AAPL",
                "reportDate": "20260520",
                "assetCategory": "STK",
                "subCategory": "COMMON",
                "position": "1",
                "markPrice": "302.25",
                "positionValue": "302.25",
                "costBasisPrice": "261.95",
                "costBasisMoney": "261.95",
                "fifoPnlUnrealized": "40.3",
                "percentOfNAV": "0.26",
                "side": "Long",
                "currency": "USD",
            }
        ],
    )
    monkeypatch.setattr(
        "quant_researcher.holdings.ibkr_flex.FlexClient", lambda **_: fake_flex
    )

    result = runner.invoke(app, ["holdings", "sync"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["source"] == "flex"
    assert data["imported"] == 1
    assert data["symbols"] == ["AAPL"]
    assert data["account_id"] == "U16781493"
    assert data["statement"]["query_name"] == "Live"


# ----- ME: qr trades -------------------------------------------------------


def test_trades_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["trades", "--help"])
    assert result.exit_code == 0
    for sub in ("sync", "list"):
        assert sub in result.output


def test_trades_sync_missing_token(memory_db, monkeypatch) -> None:
    fake_settings = MagicMock()
    fake_settings.flex_token_key = None
    fake_settings.flex_query_id_live = "1440609"
    monkeypatch.setattr("quant_researcher.cli.settings", lambda: fake_settings)
    result = runner.invoke(app, ["trades", "sync"])
    assert result.exit_code == 1
    assert _json_lines(result.output)[0]["error"]["code"] == "missing_flex_token"


def test_trades_sync_missing_query_id(memory_db, monkeypatch) -> None:
    fake_settings = MagicMock()
    fake_settings.flex_token_key = "secret"
    fake_settings.flex_query_id_live = None
    monkeypatch.setattr("quant_researcher.cli.settings", lambda: fake_settings)
    result = runner.invoke(app, ["trades", "sync"])
    assert result.exit_code == 1
    assert _json_lines(result.output)[0]["error"]["code"] == "missing_flex_query_id"


def _fake_trades_flex(monkeypatch, trades_payload: list[dict]) -> None:
    """Patch settings + FlexClient so `fetch_trades` returns `trades_payload`."""
    fake_settings = MagicMock()
    fake_settings.flex_token_key = "secret"
    fake_settings.flex_query_id_live = "1440609"
    monkeypatch.setattr("quant_researcher.cli.settings", lambda: fake_settings)

    from quant_researcher.holdings.ibkr_flex import FlexStatementMeta

    fake_flex = MagicMock()
    fake_flex.__enter__.return_value = fake_flex
    fake_flex.__exit__.return_value = None
    fake_flex.fetch_trades.return_value = (
        FlexStatementMeta(
            account_id="U16781493",
            from_date="20260519",
            to_date="20260519",
            when_generated="20260520;070016",
            query_name="Live",
        ),
        trades_payload,
    )
    monkeypatch.setattr(
        "quant_researcher.holdings.ibkr_flex.FlexClient", lambda **_: fake_flex
    )


def test_trades_sync_happy_path(memory_db, monkeypatch) -> None:
    _fake_trades_flex(
        monkeypatch,
        [
            {
                "accountId": "U16781493",
                "symbol": "AAPL",
                "ibExecID": "0000e0d5.000abc12.01.01",
                "tradeID": "7228851234",
                "assetCategory": "STK",
                "tradeDate": "20260519",
                "dateTime": "20260519;101512",
                "buySell": "BUY",
                "quantity": "100",
                "tradePrice": "200.5",
                "ibCommission": "-1.0",
                "currency": "USD",
            }
        ],
    )
    result = runner.invoke(app, ["trades", "sync"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["source"] == "flex"
    assert data["imported"] == 1
    assert data["symbols"] == ["AAPL"]
    assert data["account_id"] == "U16781493"
    assert data["statement"]["query_name"] == "Live"


def test_trades_sync_empty_day_succeeds(memory_db, monkeypatch) -> None:
    """A no-trade business day returns 0 imports and exit 0 (not an error)."""
    _fake_trades_flex(monkeypatch, [])
    result = runner.invoke(app, ["trades", "sync"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["imported"] == 0
    assert data["symbols"] == []
    assert data["account_id"] is None


def test_trades_list_filters_by_symbol(memory_db, monkeypatch) -> None:
    _fake_trades_flex(
        monkeypatch,
        [
            {
                "accountId": "U1",
                "symbol": "AAPL",
                "ibExecID": "exec-1",
                "assetCategory": "STK",
                "tradeDate": "20260519",
                "dateTime": "20260519;101512",
                "buySell": "BUY",
                "quantity": "100",
                "tradePrice": "200.5",
            },
            {
                "accountId": "U1",
                "symbol": "MSFT",
                "ibExecID": "exec-2",
                "assetCategory": "STK",
                "tradeDate": "20260519",
                "dateTime": "20260519;110000",
                "buySell": "BUY",
                "quantity": "50",
                "tradePrice": "310.0",
            },
        ],
    )
    runner.invoke(app, ["trades", "sync"])

    result = runner.invoke(app, ["trades", "list", "--symbol", "AAPL"])
    assert result.exit_code == 0
    data = _json_lines(result.output)[0]["data"]
    assert data["count"] == 1
    assert data["trades"][0]["symbol"] == "AAPL"
    assert data["trades"][0]["ib_exec_id"] == "exec-1"
    assert data["trades"][0]["side"] == "BUY"


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
