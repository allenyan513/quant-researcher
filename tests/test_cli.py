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
    assert payloads[0]["ok"] is False
    assert payloads[0]["error"]["code"] == "universe_file_missing"


def test_universe_set_rejects_empty_file(memory_db, tmp_path: Path) -> None:
    f = tmp_path / "wl.txt"
    f.write_text("# only comments\n\n")
    result = runner.invoke(app, ["universe", "set", "--file", str(f)])
    assert result.exit_code == 1
    payloads = _json_lines(result.output)
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
