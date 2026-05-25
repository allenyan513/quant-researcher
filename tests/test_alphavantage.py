"""AlphaVantageClient — HTTP-layer behavior (respx, no real sleeps)."""

from __future__ import annotations

import httpx
import pytest
import respx

from quant_researcher.data.alphavantage import AlphaVantageClient, AlphaVantageError

URL = "https://www.alphavantage.co/query"


def _client() -> AlphaVantageClient:
    # min_interval_s=0 → no real throttling/retry sleeps in tests.
    return AlphaVantageClient(api_key="demo", min_interval_s=0.0)


def _payload(quarter: str = "2025Q1") -> dict:
    return {
        "symbol": "AAPL",
        "quarter": quarter,
        "transcript": [
            {"speaker": "Op", "title": "Operator", "content": "hi", "sentiment": "0.1"}
        ],
    }


@respx.mock
def test_get_transcript_happy() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=_payload()))
    with _client() as c:
        out = c.get_earnings_transcript("AAPL", quarter="2025Q1")
    assert out is not None
    assert out["transcript"][0]["content"] == "hi"


def test_missing_key_raises() -> None:
    with pytest.raises(AlphaVantageError):
        AlphaVantageClient(api_key="")


@respx.mock
def test_empty_transcript_returns_none() -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(
            200, json={"symbol": "AAPL", "quarter": "", "transcript": []}
        )
    )
    with _client() as c:
        assert c.get_earnings_transcript("AAPL", quarter="2025Q1") is None


@respx.mock
def test_rate_limit_note_retried_then_succeeds() -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json={"Information": "spread out your requests, 1/sec"}),
            httpx.Response(200, json=_payload()),
        ]
    )
    with _client() as c:
        out = c.get_earnings_transcript("AAPL", quarter="2025Q1")
    assert out is not None
    assert route.call_count == 2  # retried once after the throttle notice


@respx.mock
def test_rate_limit_note_twice_returns_none() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"Note": "daily cap hit"}))
    with _client() as c:
        assert c.get_earnings_transcript("AAPL", quarter="2025Q1") is None


@respx.mock
def test_non_200_raises() -> None:
    respx.get(URL).mock(return_value=httpx.Response(500, text="boom"))
    with _client() as c, pytest.raises(AlphaVantageError):
        c.get_earnings_transcript("AAPL", quarter="2025Q1")
