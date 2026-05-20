"""CLI smoke — `qr --help`, `qr db --help`, and single-envelope guarantees."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from quant_researcher.cli import app

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
