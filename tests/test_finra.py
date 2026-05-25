"""FinraClient — settlement-date probe + CSV parse (respx, no network)."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from quant_researcher.data.finra import (
    FinraClient,
    FinraError,
    _candidate_settlement_dates,
    _prev_weekday,
)

BASE = "https://cdn.finra.org/equity/otcmarket/biweekly"
# FINRA's real file is PIPE-delimited with these columns.
CSV = (
    "accountingYearMonthNumber|symbolCode|issueName|issuerServicesGroupExchangeCode|"
    "marketClassCode|currentShortPositionQuantity|previousShortPositionQuantity|stockSplitFlag|"
    "averageDailyVolumeQuantity|daysToCoverQuantity|revisionFlag|changePercent|"
    "changePreviousNumber|settlementDate\n"
    "20260430|TSLA|Tesla, Inc.|R|NNM|12000000|10000000||5000000|2.4||20.0|2000000|2026-04-30\n"
    "20260430|NVDA|NVIDIA Corp|R|NNM|8000000|9000000||40000000|0.2||-11.1|-1000000|2026-04-30\n"
)


@respx.mock
def test_probes_to_latest_published_and_parses() -> None:
    # 05-15 not published yet → FINRA's CDN 403s it → falls back to 04-30.
    respx.get(f"{BASE}/shrt20260515.csv").mock(return_value=httpx.Response(403))
    respx.get(f"{BASE}/shrt20260430.csv").mock(return_value=httpx.Response(200, text=CSV))
    with FinraClient() as c:
        out = c.get_short_interest(["TSLA", "NVDA"], today=date(2026, 5, 25))
    assert set(out) == {"TSLA", "NVDA"}
    assert out["TSLA"]["short_interest"] == 12000000.0
    assert out["TSLA"]["days_to_cover"] == 2.4
    assert out["TSLA"]["settlement_date"] == date(2026, 4, 30)  # the probed date
    assert out["TSLA"]["security_name"] == "Tesla, Inc."
    assert out["NVDA"]["change_pct"] == -11.1


@respx.mock
def test_filters_to_requested_symbols() -> None:
    respx.get(f"{BASE}/shrt20260515.csv").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/shrt20260430.csv").mock(return_value=httpx.Response(200, text=CSV))
    with FinraClient() as c:
        out = c.get_short_interest(["TSLA"], today=date(2026, 5, 25))
    assert set(out) == {"TSLA"}  # NVDA present in the file but not requested


@respx.mock
def test_strips_whitespace_in_headers_and_values() -> None:
    # FINRA's format is clean today, but harden the parse: a padded header or a
    # padded symbolCode would otherwise silently drop the row (no error).
    padded = (
        " symbolCode | currentShortPositionQuantity | daysToCoverQuantity | issueName \n"
        " TSLA |12000000|2.4| Tesla, Inc. \n"
    )
    respx.get(f"{BASE}/shrt20260515.csv").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/shrt20260430.csv").mock(
        return_value=httpx.Response(200, text=padded)
    )
    with FinraClient() as c:
        out = c.get_short_interest(["TSLA"], today=date(2026, 5, 25))
    assert set(out) == {"TSLA"}  # padded symbol still matches the request
    assert out["TSLA"]["short_interest"] == 12000000.0
    assert out["TSLA"]["days_to_cover"] == 2.4
    assert out["TSLA"]["security_name"] == "Tesla, Inc."  # value stripped


@respx.mock
def test_no_file_in_window_returns_empty() -> None:
    respx.get(url__startswith=f"{BASE}/").mock(return_value=httpx.Response(404))
    with FinraClient(max_lookback_files=2) as c:
        assert c.get_short_interest(["TSLA"], today=date(2026, 5, 25)) == {}


@respx.mock
def test_http_error_raises() -> None:
    respx.get(url__startswith=f"{BASE}/").mock(return_value=httpx.Response(500))
    with FinraClient() as c, pytest.raises(FinraError):
        c.get_short_interest(["TSLA"], today=date(2026, 5, 25))


def test_candidate_dates_newest_first_and_weekend_nudge() -> None:
    assert _prev_weekday(date(2026, 5, 31)) == date(2026, 5, 29)  # Sun → Fri
    cands = _candidate_settlement_dates(date(2026, 5, 25), 2)
    assert cands == sorted(cands, reverse=True)
    assert date(2026, 5, 15) in cands  # mid-May, on/before today
    assert date(2026, 5, 29) not in cands  # month-end is in the future
