"""CSV parser + unified holdings importer — boundary cases + DB roundtrip."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from quant_researcher.db import Base
from quant_researcher.holdings.csv import CSVError, parse_holdings_csv
from quant_researcher.holdings.importer import import_holdings, import_trades
from quant_researcher.models.holdings import Holding
from quant_researcher.models.trades import Trade


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as sess:
        yield sess


# ----- CSV parser ---------------------------------------------------------


def test_csv_parses_required_and_optional(tmp_path: Path) -> None:
    f = tmp_path / "h.csv"
    f.write_text(
        "account_id,symbol,quantity,as_of_date,avg_cost,mark_price,currency\n"
        "U1,AAPL,100,2026-05-20,150.0,200.0,USD\n"
        "U1,MSFT,50,2026-05-20,250.5,310.0,USD\n"
    )
    rows = parse_holdings_csv(f)
    assert len(rows) == 2
    assert rows[0]["account_id"] == "U1"
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["quantity"] == 100.0
    assert rows[0]["as_of_date"] == date(2026, 5, 20)
    assert rows[0]["avg_cost"] == 150.0
    assert rows[0]["mark_price"] == 200.0
    assert rows[0]["currency"] == "USD"
    # default category
    assert rows[0]["asset_category"] == "STK"


def test_csv_missing_required_rejected(tmp_path: Path) -> None:
    f = tmp_path / "h.csv"
    f.write_text("symbol,quantity\nAAPL,100\n")
    with pytest.raises(CSVError, match="missing required"):
        parse_holdings_csv(f)


def test_csv_bad_quantity_raises(tmp_path: Path) -> None:
    f = tmp_path / "h.csv"
    f.write_text("account_id,symbol,quantity,as_of_date\nU1,AAPL,xyz,2026-05-20\n")
    with pytest.raises(CSVError, match="bad quantity"):
        parse_holdings_csv(f)


def test_csv_bad_date_raises(tmp_path: Path) -> None:
    f = tmp_path / "h.csv"
    f.write_text("account_id,symbol,quantity,as_of_date\nU1,AAPL,100,not-a-date\n")
    with pytest.raises(CSVError, match="as_of_date"):
        parse_holdings_csv(f)


def test_csv_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(CSVError, match="no such file"):
        parse_holdings_csv(tmp_path / "nope.csv")


def test_csv_empty_numeric_cells_become_none(tmp_path: Path) -> None:
    f = tmp_path / "h.csv"
    f.write_text(
        "account_id,symbol,quantity,as_of_date,avg_cost,mark_price\n"
        "U1,AAPL,100,2026-05-20,,\n"
    )
    rows = parse_holdings_csv(f)
    assert rows[0]["avg_cost"] is None
    assert rows[0]["mark_price"] is None


# ----- importer: flex source ---------------------------------------------


_FLEX_ROW = {
    "accountId": "U16781493",
    "symbol": "AAPL",
    "reportDate": "20260520",
    "assetCategory": "STK",
    "subCategory": "COMMON",
    "position": "100",
    "markPrice": "200.0",
    "positionValue": "20000.0",
    "costBasisPrice": "150.0",
    "costBasisMoney": "15000.0",
    "fifoPnlUnrealized": "5000.0",
    "percentOfNAV": "25.0",
    "side": "Long",
    "currency": "USD",
    "fxRateToBase": "1",
    "conid": "265598",
    "listingExchange": "NASDAQ",
    "description": "APPLE INC",
}


def test_import_flex_writes_row(session: Session) -> None:
    result = import_holdings(session, source="flex", payload=[_FLEX_ROW])
    session.commit()

    assert result.imported == 1
    assert result.account_id == "U16781493"
    assert result.as_of_date == date(2026, 5, 20)
    assert result.symbols == ["AAPL"]
    assert result.skipped == []

    row = session.get(Holding, ("U16781493", "AAPL", date(2026, 5, 20)))
    assert row is not None
    assert row.quantity == 100.0
    assert row.mark_price == 200.0
    assert row.market_value == 20000.0
    assert row.avg_cost == 150.0
    assert row.cost_basis_total == 15000.0
    assert row.unrealized_pnl == 5000.0
    assert row.percent_of_nav == 25.0
    assert row.side == "Long"
    assert row.asset_category == "STK"
    assert row.sub_category == "COMMON"
    assert row.conid == 265598
    assert row.listing_exchange == "NASDAQ"
    assert row.description == "APPLE INC"
    assert row.source == "flex"
    assert row.raw["accountId"] == "U16781493"


def test_import_flex_handles_negative_position(session: Session) -> None:
    short_row = {
        **_FLEX_ROW,
        "symbol": "META  260821P00530000",
        "position": "-1",
        "assetCategory": "OPT",
    }
    result = import_holdings(session, source="flex", payload=[short_row])
    session.commit()

    assert result.imported == 1
    row = session.scalars(
        select(Holding).where(Holding.asset_category == "OPT")
    ).one()
    assert row.quantity == -1.0
    assert row.asset_category == "OPT"


def test_import_flex_merge_overwrites_same_day(session: Session) -> None:
    # First import: mark price 200.
    import_holdings(session, source="flex", payload=[_FLEX_ROW])
    session.commit()
    # Second import same PK with updated mark price.
    row2 = {**_FLEX_ROW, "markPrice": "210.0", "positionValue": "21000.0"}
    import_holdings(session, source="flex", payload=[row2])
    session.commit()

    rows = list(session.scalars(select(Holding)))
    assert len(rows) == 1
    assert rows[0].mark_price == 210.0
    assert rows[0].market_value == 21000.0


def test_import_flex_skips_row_without_pk(session: Session) -> None:
    bad = {**_FLEX_ROW, "symbol": ""}
    result = import_holdings(session, source="flex", payload=[bad])
    assert result.imported == 0
    assert len(result.skipped) == 1


# ----- importer: flex SUMMARY + LOT row de-dup --------------------------


def test_import_flex_keeps_summary_drops_lots_when_both_present(
    session: Session,
) -> None:
    """The actual TSLA bug case: IBKR Flex returns 1 SUMMARY (qty 40) plus
    10 LOT rows summing to 40. The importer must keep the SUMMARY and drop
    the lots — NOT sum everything (which would double-count to 80)."""
    summary = {**_FLEX_ROW, "position": "40", "costBasisPrice": "438.7866",
               "costBasisMoney": "17551.46"}
    lot1 = {**_FLEX_ROW, "position": "10", "costBasisPrice": "479.00",
            "costBasisMoney": "4790.0"}
    lot2 = {**_FLEX_ROW, "position": "30", "costBasisPrice": "425.39",
            "costBasisMoney": "12761.46"}
    # Order shouldn't matter — IBKR can interleave or put summary anywhere.
    result = import_holdings(session, source="flex", payload=[lot1, summary, lot2])
    session.commit()

    assert result.imported == 1
    row = session.get(Holding, ("U16781493", "AAPL", date(2026, 5, 20)))
    assert row is not None
    assert row.quantity == 40.0, "must equal the SUMMARY row, not sum of all three"
    assert row.avg_cost == pytest.approx(438.7866)
    # Trusted SUMMARY → no _lots audit list (it'd be misleading).
    assert "_lots" not in (row.raw or {})


def test_import_flex_uses_level_of_detail_when_provided(session: Session) -> None:
    """When IBKR includes the `levelOfDetail` attribute, the importer
    prefers the row tagged SUMMARY regardless of the position-sum heuristic."""
    summary = {**_FLEX_ROW, "position": "100", "costBasisPrice": "150.0",
               "costBasisMoney": "15000.0", "levelOfDetail": "SUMMARY"}
    lot = {**_FLEX_ROW, "position": "100", "costBasisPrice": "150.0",
           "costBasisMoney": "15000.0", "levelOfDetail": "LOT"}
    # Position-sum heuristic would tie (both 100); the levelOfDetail
    # attribute is the tie-breaker.
    import_holdings(session, source="flex", payload=[lot, summary])
    session.commit()
    row = session.get(Holding, ("U16781493", "AAPL", date(2026, 5, 20)))
    assert row is not None and row.quantity == 100.0
    assert "_lots" not in (row.raw or {})


def test_import_flex_aggregates_when_only_lots_no_summary(
    session: Session,
) -> None:
    """Fallback path: when neither a SUMMARY row nor a sum-equal pattern
    is detectable (e.g. a query configured for LOT-only output), the
    importer aggregates all rows. quantity is summed, avg_cost is the
    Σ(costBasisMoney) / Σ(qty) weighted mean."""
    # Three genuine lots, no row whose qty == sum-of-others, so heuristic
    # finds no summary and falls through to aggregation.
    lot1 = {**_FLEX_ROW, "position": "10", "costBasisPrice": "100.0",
            "costBasisMoney": "1000.0", "positionValue": "1500.0",
            "fifoPnlUnrealized": "500.0", "percentOfNAV": "5.0"}
    lot2 = {**_FLEX_ROW, "position": "20", "costBasisPrice": "200.0",
            "costBasisMoney": "4000.0", "positionValue": "3000.0",
            "fifoPnlUnrealized": "-1000.0", "percentOfNAV": "10.0"}
    lot3 = {**_FLEX_ROW, "position": "25", "costBasisPrice": "150.0",
            "costBasisMoney": "3750.0", "positionValue": "3750.0",
            "fifoPnlUnrealized": "0.0", "percentOfNAV": "12.5"}
    # Sanity: positions 10/20/25/total 55; no row has qty == sum-of-others
    # (10+20=30 ≠ 25, 10+25=35 ≠ 20, 20+25=45 ≠ 10). Heuristic falls through.
    import_holdings(session, source="flex", payload=[lot1, lot2, lot3])
    session.commit()
    row = session.get(Holding, ("U16781493", "AAPL", date(2026, 5, 20)))
    assert row is not None
    assert row.quantity == 55.0  # aggregated
    # weighted avg = (1000 + 4000 + 3750) / 55 = 8750 / 55 ≈ 159.09
    assert row.avg_cost == pytest.approx(8750.0 / 55.0)
    # In the fallback path the audit `_lots` list IS retained.
    assert isinstance(row.raw.get("_lots"), list)
    assert len(row.raw["_lots"]) == 3


def test_import_flex_single_lot_passes_through_unchanged(session: Session) -> None:
    """A position with exactly one row must not gain a `_lots` audit list."""
    import_holdings(session, source="flex", payload=[_FLEX_ROW])
    session.commit()
    row = session.get(Holding, ("U16781493", "AAPL", date(2026, 5, 20)))
    assert row is not None
    assert "_lots" not in (row.raw or {})


def test_import_flex_aggregate_falls_back_to_position_weighted_avg(
    session: Session,
) -> None:
    """When `costBasisMoney` is empty on lots, recover avg_cost from the
    per-lot prices and quantities directly."""
    # Two lots, no summary (10 ≠ 30 and 30 ≠ 10), costBasisMoney missing.
    lot1 = {**_FLEX_ROW, "position": "10", "costBasisPrice": "100.0",
            "costBasisMoney": ""}
    lot2 = {**_FLEX_ROW, "position": "30", "costBasisPrice": "200.0",
            "costBasisMoney": ""}
    # Heuristic check: total = 40. 2*10 = 20 ≠ 40, 2*30 = 60 ≠ 40 → no
    # summary; falls through to aggregation.
    import_holdings(session, source="flex", payload=[lot1, lot2])
    session.commit()
    row = session.get(Holding, ("U16781493", "AAPL", date(2026, 5, 20)))
    # (10*100 + 30*200) / 40 = 7000 / 40 = 175
    assert row is not None
    assert row.avg_cost == pytest.approx(175.0)


def test_import_flex_collapse_preserves_different_symbols(
    session: Session,
) -> None:
    """Rows from different symbols stay separate after collapse."""
    # AAPL has a summary (30 = 10 + 20); TSLA is single-lot.
    aapl_sum = {**_FLEX_ROW, "symbol": "AAPL", "position": "30",
                "costBasisMoney": "3000.0", "costBasisPrice": "100.0"}
    aapl_lot1 = {**_FLEX_ROW, "symbol": "AAPL", "position": "10",
                 "costBasisMoney": "900.0", "costBasisPrice": "90.0"}
    aapl_lot2 = {**_FLEX_ROW, "symbol": "AAPL", "position": "20",
                 "costBasisMoney": "2100.0", "costBasisPrice": "105.0"}
    tsla_lot1 = {**_FLEX_ROW, "symbol": "TSLA", "position": "5",
                 "costBasisMoney": "2000.0", "costBasisPrice": "400.0"}
    import_holdings(
        session, source="flex",
        payload=[aapl_sum, aapl_lot1, aapl_lot2, tsla_lot1],
    )
    session.commit()
    aapl = session.get(Holding, ("U16781493", "AAPL", date(2026, 5, 20)))
    tsla = session.get(Holding, ("U16781493", "TSLA", date(2026, 5, 20)))
    assert aapl is not None and aapl.quantity == 30.0
    assert tsla is not None and tsla.quantity == 5.0


# ----- importer: csv source ----------------------------------------------


def test_import_csv_writes_row(session: Session) -> None:
    payload = [
        {
            "account_id": "U1",
            "symbol": "AAPL",
            "as_of_date": date(2026, 5, 20),
            "quantity": 100.0,
            "avg_cost": 150.0,
            "mark_price": 200.0,
            "currency": "USD",
        }
    ]
    result = import_holdings(session, source="csv", payload=payload)
    session.commit()

    assert result.imported == 1
    row = session.get(Holding, ("U1", "AAPL", date(2026, 5, 20)))
    assert row is not None
    assert row.source == "csv"
    assert row.avg_cost == 150.0
    assert row.side == "Long"  # derived from positive qty


def test_import_csv_uses_overrides(session: Session) -> None:
    # Row missing account_id and as_of_date but overrides provided.
    payload = [{"symbol": "AAPL", "quantity": 50.0}]
    import_holdings(
        session,
        source="csv",
        payload=payload,
        account_id_override="U-OVR",
        as_of_date_override=date(2026, 5, 20),
    )
    session.commit()

    row = session.get(Holding, ("U-OVR", "AAPL", date(2026, 5, 20)))
    assert row is not None


def test_import_csv_negative_qty_marks_short(session: Session) -> None:
    payload = [
        {
            "account_id": "U1",
            "symbol": "X",
            "as_of_date": date(2026, 5, 20),
            "quantity": -10.0,
        }
    ]
    import_holdings(session, source="csv", payload=payload)
    session.commit()
    row = session.get(Holding, ("U1", "X", date(2026, 5, 20)))
    assert row is not None
    assert row.side == "Short"


def test_import_csv_missing_account_no_override_skips(session: Session) -> None:
    payload = [{"symbol": "AAPL", "quantity": 10.0, "as_of_date": date(2026, 5, 20)}]
    result = import_holdings(session, source="csv", payload=payload)
    assert result.imported == 0
    assert len(result.skipped) == 1
    assert "account_id" in result.skipped[0]["error"]


# ----- error paths -------------------------------------------------------


def test_unknown_source_rejected(session: Session) -> None:
    with pytest.raises(ValueError, match="unknown source"):
        import_holdings(session, source="??", payload=[{}])


def test_empty_payload_rejected(session: Session) -> None:
    with pytest.raises(ValueError, match="empty"):
        import_holdings(session, source="flex", payload=[])


# ----- importer: trades ---------------------------------------------------


_FLEX_TRADE = {
    "accountId": "U16781493",
    "symbol": "AAPL",
    "ibExecID": "0000e0d5.000abc12.01.01",
    "tradeID": "7228851234",
    "conid": "265598",
    "assetCategory": "STK",
    "subCategory": "COMMON",
    "description": "APPLE INC",
    "tradeDate": "20260519",
    "dateTime": "20260519;101512",
    "buySell": "BUY",
    "quantity": "100",
    "tradePrice": "200.5",
    "ibCommission": "-1.0",
    "netCash": "-20051.0",
    "proceeds": "-20050.0",
    "fifoPnlRealized": "0",
    "openCloseIndicator": "O",
    "orderReference": "my-order-1",
    "exchange": "NASDAQ",
    "currency": "USD",
    "fxRateToBase": "1",
    "notes": "P",
}


def test_import_trades_writes_row(session: Session) -> None:
    result = import_trades(session, payload=[_FLEX_TRADE])
    session.commit()

    assert result.imported == 1
    assert result.account_id == "U16781493"
    assert result.symbols == ["AAPL"]
    assert result.skipped == []

    row = session.get(Trade, ("U16781493", "0000e0d5.000abc12.01.01"))
    assert row is not None
    assert row.trade_id == "7228851234"
    assert row.symbol == "AAPL"
    assert row.side == "BUY"
    assert row.quantity == 100.0
    assert row.price == 200.5
    assert row.commission == -1.0
    assert row.proceeds == -20050.0
    assert row.trade_date == date(2026, 5, 19)
    assert row.executed_at == "20260519;101512"
    assert row.open_close == "O"
    assert row.conid == 265598
    assert row.source == "flex"
    assert row.raw["ibExecID"] == "0000e0d5.000abc12.01.01"


def test_import_trades_merge_is_idempotent(session: Session) -> None:
    import_trades(session, payload=[_FLEX_TRADE])
    session.commit()
    # Re-pull the same execution (e.g. an IBKR correction) with a new price.
    again = {**_FLEX_TRADE, "tradePrice": "201.0"}
    import_trades(session, payload=[again])
    session.commit()

    rows = list(session.scalars(select(Trade)))
    assert len(rows) == 1
    assert rows[0].price == 201.0


def test_import_trades_negative_quantity_for_sell(session: Session) -> None:
    sell = {
        **_FLEX_TRADE,
        "ibExecID": "0000e0d5.000abc99.01.01",
        "symbol": "META  260821P00530000",
        "assetCategory": "OPT",
        "buySell": "SELL",
        "quantity": "-1",
    }
    import_trades(session, payload=[sell])
    session.commit()

    row = session.scalars(select(Trade).where(Trade.asset_category == "OPT")).one()
    assert row.quantity == -1.0
    assert row.side == "SELL"
    assert row.symbol == "META  260821P00530000"


def test_import_trades_skips_row_without_exec_id(session: Session) -> None:
    bad = {**_FLEX_TRADE, "ibExecID": ""}
    result = import_trades(session, payload=[bad])
    assert result.imported == 0
    assert len(result.skipped) == 1
    assert "ib_exec_id" in result.skipped[0]["error"]


def test_import_trades_empty_payload_is_no_op(session: Session) -> None:
    """A no-trade day must succeed with 0 imports (not raise)."""
    result = import_trades(session, payload=[])
    assert result.imported == 0
    assert result.symbols == []
    assert result.account_id is None
