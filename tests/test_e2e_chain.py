"""End-to-end orchestration smoke test.

Mirrors how the Claude agent actually drives the tool (CLAUDE.md §0): a multi-step
chain over one seeded warehouse — universe → freshness → screen → value → ledger
round-trip — driven through the real Typer CLI against in-memory SQLite. The point
is the *contract*: every subcommand emits EXACTLY one JSON envelope and the happy
path stays `ok=true` as data flows from one command to the next. This catches
cross-command regressions (schema drift, envelope double-emit, lazy-import breakage)
that per-command unit tests miss.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from quant_researcher.cli import app
from quant_researcher.db import Base

runner = CliRunner()


def _json_lines(output: str) -> list[dict]:
    return [json.loads(ln) for ln in output.strip().split("\n") if ln.strip()]


def _one_ok(result, *, code: int = 0) -> dict:
    """Assert single-envelope + exit code, return the envelope's `data`."""
    assert result.exit_code == code, result.output
    payloads = _json_lines(result.output)
    assert len(payloads) == 1, f"expected exactly 1 envelope, got: {payloads}"
    env = payloads[0]
    assert env["ok"] is (code == 0), env
    return env.get("data") or {}


@pytest.fixture
def memory_db():
    """In-memory SQLite bound into `session_factory` (canonical CLI test pattern)."""
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with patch("quant_researcher.db.session_factory", return_value=factory):
        yield factory


def _seed_warehouse(factory) -> None:
    """A small but complete 3-ticker warehouse so every read/compute command runs."""
    from quant_researcher.models.financials import (
        BalanceSheet,
        CashFlow,
        IncomeStatement,
    )
    from quant_researcher.models.prices import DailyPrice
    from quant_researcher.models.profile import Profile
    from quant_researcher.models.ratios import FinancialRatios
    from quant_researcher.models.universe import UniverseMember

    now = datetime.now(UTC)
    with factory() as sess:
        rows = (("AAPL", 28.0, 3e12), ("MSFT", 30.0, 2e12), ("NVDA", 35.0, 2.5e12))
        for sym, pe, mktcap in rows:
            sess.add(UniverseMember(symbol=sym, source="test"))
            sess.add(
                Profile(
                    symbol=sym,
                    sector="Technology",
                    beta=1.2,
                    raw={"mktCap": mktcap},
                    known_at=now,
                )
            )
            # Five FY income statements + cash flows with steady growth (DCF needs >=2).
            for i in range(5):
                g = 1.08**i
                sess.add(
                    IncomeStatement(
                        symbol=sym,
                        period="FY",
                        fiscal_date=date(2020 + i, 9, 30),
                        net_income=1e10 * g,
                        eps_diluted=5.0 * g,
                        operating_income=1.5e10 * g,
                        revenue=5e10 * g,
                        known_at=now,
                    )
                )
                sess.add(
                    CashFlow(
                        symbol=sym,
                        period="FY",
                        fiscal_date=date(2020 + i, 9, 30),
                        free_cash_flow=8e9 * g,
                        capital_expenditure=-2e9 * g,
                        known_at=now,
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
                    known_at=now,
                )
            )
            sess.add(
                FinancialRatios(
                    symbol=sym,
                    period="FY",
                    fiscal_date=date(2024, 9, 30),
                    pe_ratio=pe,
                    ev_to_ebitda=22.0,
                    price_to_sales=8.0,
                    return_on_equity=0.4,
                    known_at=now,
                )
            )
            sess.add(DailyPrice(symbol=sym, trade_date=date(2024, 9, 30), close=200.0))
        sess.commit()


def test_research_to_ledger_chain(memory_db) -> None:
    _seed_warehouse(memory_db)

    # 1) universe is visible
    data = _one_ok(runner.invoke(app, ["universe", "list"]))
    assert data["count"] == 3
    assert {m["symbol"] for m in data["members"]} == {"AAPL", "MSFT", "NVDA"}

    # 2) freshness report (pure DB read) — one envelope, ok
    _one_ok(runner.invoke(app, ["data", "freshness"]))

    # 3) fundamental screen flows over the seeded universe
    _one_ok(runner.invoke(app, ["screen", "run", "--expr", "pe < 100"]))

    # 4) value the top name — full data so the engine produces a real number
    value = _one_ok(runner.invoke(app, ["value", "AAPL", "--model", "all"]))
    assert value["symbol"] == "AAPL"
    assert set(value["models"]) == {"dcf", "peg", "multiples", "scenario"}

    # 5) record a decision (skip auto-bundle to keep the step warehouse-only)
    added = _one_ok(
        runner.invoke(
            app,
            ["ledger", "add", "aapl", "--side", "buy", "--thesis", "cheap compounder",
             "--confidence", "4", "--no-bundle"],
        )
    )
    assert added["symbol"] == "AAPL"
    assert added["side"] == "buy"

    # 6) the decision round-trips through `ledger list`
    listed = _one_ok(runner.invoke(app, ["ledger", "list"]))
    assert any(d["symbol"] == "AAPL" for d in listed["decisions"])


def test_empty_warehouse_chain_stays_single_envelope(memory_db) -> None:
    """No data seeded: every command must still emit EXACTLY one envelope.

    Read commands stay ok=true with empty collections; `data freshness` correctly
    fails (`empty_universe`) — both must be a single, well-formed envelope.
    """
    assert _one_ok(runner.invoke(app, ["universe", "list"]))["count"] == 0
    _one_ok(runner.invoke(app, ["ledger", "list"]))
    _one_ok(runner.invoke(app, ["research", "list"]))
    _one_ok(runner.invoke(app, ["screen", "list"]))

    # freshness needs a universe → single failure envelope, not a crash/double-emit
    failed = _one_ok(runner.invoke(app, ["data", "freshness"]), code=1)
    assert failed == {}  # failure envelopes carry data=null
