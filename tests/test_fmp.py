"""FMPClient — request shape, response unwrapping, retry, rate limiter."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from quant_researcher.data.fmp import FMPClient, FMPError

BASE = "https://financialmodelingprep.com/stable"


@pytest.fixture
def client() -> FMPClient:
    # Zero backoff so retry tests don't add latency.
    return FMPClient(api_key="test-key", max_retries=2, backoff_base=0.0)


# ----- profile --------------------------------------------------------------


@respx.mock
def test_get_profile_unwraps_single_dict(client: FMPClient) -> None:
    respx.get(f"{BASE}/profile").mock(
        return_value=httpx.Response(200, json=[{"symbol": "AAPL", "companyName": "Apple"}])
    )
    assert client.get_profile("AAPL") == {"symbol": "AAPL", "companyName": "Apple"}


@respx.mock
def test_get_profile_returns_none_on_empty_list(client: FMPClient) -> None:
    respx.get(f"{BASE}/profile").mock(return_value=httpx.Response(200, json=[]))
    assert client.get_profile("NONESUCH") is None


@respx.mock
def test_get_profile_sends_apikey_and_symbol(client: FMPClient) -> None:
    route = respx.get(f"{BASE}/profile").mock(
        return_value=httpx.Response(200, json=[{"symbol": "AAPL"}])
    )
    client.get_profile("AAPL")
    params = route.calls.last.request.url.params
    assert params.get("apikey") == "test-key"
    assert params.get("symbol") == "AAPL"


# ----- historical prices ----------------------------------------------------


@respx.mock
def test_historical_prices_handles_bare_list(client: FMPClient) -> None:
    payload = [{"date": "2024-01-02", "close": 1.0}]
    respx.get(f"{BASE}/historical-price-eod/full").mock(
        return_value=httpx.Response(200, json=payload)
    )
    assert client.get_historical_prices("AAPL") == payload


@respx.mock
def test_historical_prices_handles_wrapped_response(client: FMPClient) -> None:
    payload = {"symbol": "AAPL", "historical": [{"date": "2024-01-02", "close": 1.0}]}
    respx.get(f"{BASE}/historical-price-eod/full").mock(
        return_value=httpx.Response(200, json=payload)
    )
    assert client.get_historical_prices("AAPL") == payload["historical"]


@respx.mock
def test_historical_prices_passes_since_as_from_param(client: FMPClient) -> None:
    route = respx.get(f"{BASE}/historical-price-eod/full").mock(
        return_value=httpx.Response(200, json=[])
    )
    client.get_historical_prices("AAPL", since=date(2024, 6, 1))
    assert route.calls.last.request.url.params.get("from") == "2024-06-01"


# ----- retry / error handling ----------------------------------------------


@respx.mock
def test_retries_on_429_then_succeeds(client: FMPClient) -> None:
    route = respx.get(f"{BASE}/profile").mock(
        side_effect=[
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(200, json=[{"symbol": "AAPL"}]),
        ]
    )
    assert client.get_profile("AAPL") == {"symbol": "AAPL"}
    assert route.call_count == 2


@respx.mock
def test_retries_on_502_then_succeeds(client: FMPClient) -> None:
    route = respx.get(f"{BASE}/profile").mock(
        side_effect=[
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, json=[{"symbol": "AAPL"}]),
        ]
    )
    assert client.get_profile("AAPL") == {"symbol": "AAPL"}
    assert route.call_count == 2


@respx.mock
def test_404_raises_without_retry(client: FMPClient) -> None:
    route = respx.get(f"{BASE}/profile").mock(
        return_value=httpx.Response(404, text="not found")
    )
    with pytest.raises(FMPError) as exc_info:
        client.get_profile("NONESUCH")
    assert exc_info.value.status_code == 404
    assert route.call_count == 1


@respx.mock
def test_exhausts_retries_then_raises() -> None:
    client = FMPClient(api_key="x", max_retries=1, backoff_base=0.0)
    route = respx.get(f"{BASE}/profile").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(FMPError) as exc_info:
        client.get_profile("AAPL")
    assert exc_info.value.status_code == 500
    assert route.call_count == 2  # initial + 1 retry


def test_missing_api_key_rejected() -> None:
    with pytest.raises(FMPError):
        FMPClient(api_key="")


# ----- rate limiter ---------------------------------------------------------


def test_rate_limiter_sleeps_when_window_full(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    fake_now = [1000.0]

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        fake_now[0] += s  # advance clock so re-eviction works

    monkeypatch.setattr("quant_researcher.data.fmp.time.sleep", fake_sleep)
    monkeypatch.setattr("quant_researcher.data.fmp.time.monotonic", lambda: fake_now[0])

    client = FMPClient(api_key="x", rate_limit_per_minute=2)
    client._wait_for_token()  # bucket: [1000.0]
    client._wait_for_token()  # bucket: [1000.0, 1000.0]
    fake_now[0] = 1000.5  # only 0.5s elapsed
    client._wait_for_token()  # bucket full → must sleep ~59.5s

    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(59.5, abs=0.01)


def test_rate_limiter_does_not_sleep_when_window_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("quant_researcher.data.fmp.time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr("quant_researcher.data.fmp.time.monotonic", lambda: 1000.0)

    client = FMPClient(api_key="x", rate_limit_per_minute=5)
    for _ in range(5):
        client._wait_for_token()
    assert sleeps == []


# ----- MA-3 period-keyed endpoints -----------------------------------------


# (method-name, FMP path, params it passes besides apikey/period/limit)
_MA3_ENDPOINTS = [
    ("get_income_statement", "/income-statement"),
    ("get_balance_sheet", "/balance-sheet-statement"),
    ("get_cash_flow", "/cash-flow-statement"),
    ("get_ratios", "/ratios"),
    ("get_analyst_estimates", "/analyst-estimates"),
]


@pytest.mark.parametrize(("method", "path"), _MA3_ENDPOINTS)
@respx.mock
def test_period_endpoint_happy_path(client: FMPClient, method: str, path: str) -> None:
    payload = [{"symbol": "AAPL", "period": "Q1", "date": "2024-12-28"}]
    respx.get(f"{BASE}{path}").mock(return_value=httpx.Response(200, json=payload))
    assert getattr(client, method)("AAPL") == payload


@pytest.mark.parametrize(("method", "path"), _MA3_ENDPOINTS)
@respx.mock
def test_period_endpoint_passes_period_param(
    client: FMPClient, method: str, path: str
) -> None:
    route = respx.get(f"{BASE}{path}").mock(return_value=httpx.Response(200, json=[]))
    getattr(client, method)("AAPL", period="annual")
    params = route.calls.last.request.url.params
    assert params.get("symbol") == "AAPL"
    assert params.get("period") == "annual"
    assert params.get("apikey") == "test-key"


@pytest.mark.parametrize(("method", "path"), _MA3_ENDPOINTS)
@respx.mock
def test_period_endpoint_passes_limit_when_set(
    client: FMPClient, method: str, path: str
) -> None:
    route = respx.get(f"{BASE}{path}").mock(return_value=httpx.Response(200, json=[]))
    getattr(client, method)("AAPL", limit=5)
    assert route.calls.last.request.url.params.get("limit") == "5"


@pytest.mark.parametrize(("method", "path"), _MA3_ENDPOINTS)
@respx.mock
def test_period_endpoint_omits_limit_by_default(
    client: FMPClient, method: str, path: str
) -> None:
    route = respx.get(f"{BASE}{path}").mock(return_value=httpx.Response(200, json=[]))
    getattr(client, method)("AAPL")
    assert "limit" not in route.calls.last.request.url.params


@pytest.mark.parametrize(("method", "path"), _MA3_ENDPOINTS)
@respx.mock
def test_period_endpoint_empty_list_returns_empty(
    client: FMPClient, method: str, path: str
) -> None:
    respx.get(f"{BASE}{path}").mock(return_value=httpx.Response(200, json=[]))
    assert getattr(client, method)("AAPL") == []


@respx.mock
def test_period_endpoint_raises_when_payload_not_list(client: FMPClient) -> None:
    respx.get(f"{BASE}/income-statement").mock(
        return_value=httpx.Response(200, json={"error": "wrong shape"})
    )
    with pytest.raises(FMPError):
        client.get_income_statement("AAPL")
