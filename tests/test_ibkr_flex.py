"""IBKR Flex client — XML parsing, polling, error handling. respx-mocked."""

from __future__ import annotations

import httpx
import pytest
import respx

from quant_researcher.holdings.ibkr_flex import FlexClient, FlexError

BASE = "https://gdcdyn.interactivebrokers.com/Universal/servlet"

# A minimal valid FlexQueryResponse covering the fields we extract.
_SAMPLE_QUERY_RESPONSE = """<FlexQueryResponse queryName="Live" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U16781493" fromDate="20260520"
 toDate="20260520" period="X" whenGenerated="20260521;092516">
<OpenPositions>
<OpenPosition accountId="U16781493" symbol="AAPL" description="APPLE INC"
 currency="USD" assetCategory="STK" subCategory="COMMON" position="100"
 markPrice="200.0" positionValue="20000.0" costBasisPrice="150.0"
 costBasisMoney="15000.0" fifoPnlUnrealized="5000.0" percentOfNAV="25.0"
 side="Long" fxRateToBase="1" conid="265598" listingExchange="NASDAQ"
 reportDate="20260520"/>
<OpenPosition accountId="U16781493" symbol="META  260821P00530000"
 description="META PUT" currency="USD" assetCategory="OPT" subCategory="PUT"
 position="-1" markPrice="14.425" positionValue="-1442.5"
 costBasisPrice="15.0" costBasisMoney="-1500.0" fifoPnlUnrealized="57.5"
 percentOfNAV="-1.5" side="Short" fxRateToBase="1" conid="999999"
 listingExchange="CBOE" reportDate="20260520"/>
</OpenPositions>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>"""

_SEND_OK = """<FlexStatementResponse timestamp='21 May, 2026 09:25 AM EDT'>
<Status>Success</Status>
<ReferenceCode>1234567890</ReferenceCode>
<Url>https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement</Url>
</FlexStatementResponse>"""

_SEND_FAIL = """<FlexStatementResponse timestamp='21 May, 2026 09:25 AM EDT'>
<Status>Fail</Status>
<ErrorCode>1012</ErrorCode>
<ErrorMessage>Token has expired.</ErrorMessage>
</FlexStatementResponse>"""

_SEND_TRANSIENT_1001 = """<FlexStatementResponse timestamp='21 May, 2026 09:25 AM EDT'>
<Status>Fail</Status>
<ErrorCode>1001</ErrorCode>
<ErrorMessage>Statement could not be generated at this time.</ErrorMessage>
</FlexStatementResponse>"""

_GET_PENDING = """<FlexStatementResponse timestamp='21 May, 2026 09:25 AM EDT'>
<Status>Warn</Status>
<ErrorCode>1019</ErrorCode>
<ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage>
</FlexStatementResponse>"""


@pytest.fixture
def client() -> FlexClient:
    # poll_delay=0 so polling tests don't burn wall-clock.
    return FlexClient(token="test-token", max_poll_attempts=3, poll_delay=0)


# ----- happy path ----------------------------------------------------------


@respx.mock
def test_fetch_positions_happy_path(client: FlexClient) -> None:
    respx.get(f"{BASE}/FlexStatementService.SendRequest").mock(
        return_value=httpx.Response(200, text=_SEND_OK)
    )
    respx.get(f"{BASE}/FlexStatementService.GetStatement").mock(
        return_value=httpx.Response(200, text=_SAMPLE_QUERY_RESPONSE)
    )
    meta, positions = client.fetch_positions(1440609)

    assert meta.account_id == "U16781493"
    assert meta.from_date == "20260520"
    assert meta.query_name == "Live"
    assert len(positions) == 2
    p1 = positions[0]
    assert p1["symbol"] == "AAPL"
    assert p1["position"] == "100"
    assert p1["markPrice"] == "200.0"
    assert p1["assetCategory"] == "STK"
    # Option position with negative quantity preserved as-is.
    p2 = positions[1]
    assert p2["symbol"] == "META  260821P00530000"
    assert p2["position"] == "-1"
    assert p2["assetCategory"] == "OPT"


@respx.mock
def test_fetch_positions_sends_creds(client: FlexClient) -> None:
    send_route = respx.get(f"{BASE}/FlexStatementService.SendRequest").mock(
        return_value=httpx.Response(200, text=_SEND_OK)
    )
    respx.get(f"{BASE}/FlexStatementService.GetStatement").mock(
        return_value=httpx.Response(200, text=_SAMPLE_QUERY_RESPONSE)
    )
    client.fetch_positions("1440609")
    params = send_route.calls.last.request.url.params
    assert params.get("t") == "test-token"
    assert params.get("q") == "1440609"
    assert params.get("v") == "3"


# ----- polling ------------------------------------------------------------


@respx.mock
def test_polling_retries_until_ready(client: FlexClient) -> None:
    respx.get(f"{BASE}/FlexStatementService.SendRequest").mock(
        return_value=httpx.Response(200, text=_SEND_OK)
    )
    respx.get(f"{BASE}/FlexStatementService.GetStatement").mock(
        side_effect=[
            httpx.Response(200, text=_GET_PENDING),
            httpx.Response(200, text=_GET_PENDING),
            httpx.Response(200, text=_SAMPLE_QUERY_RESPONSE),
        ]
    )
    meta, positions = client.fetch_positions(1440609)
    assert len(positions) == 2


@respx.mock
def test_polling_exhausts_raises(client: FlexClient) -> None:
    respx.get(f"{BASE}/FlexStatementService.SendRequest").mock(
        return_value=httpx.Response(200, text=_SEND_OK)
    )
    respx.get(f"{BASE}/FlexStatementService.GetStatement").mock(
        return_value=httpx.Response(200, text=_GET_PENDING)
    )
    with pytest.raises(FlexError, match="still pending"):
        client.fetch_positions(1440609)


# ----- error handling -----------------------------------------------------


@respx.mock
def test_send_request_fail_raises(client: FlexClient) -> None:
    respx.get(f"{BASE}/FlexStatementService.SendRequest").mock(
        return_value=httpx.Response(200, text=_SEND_FAIL)
    )
    with pytest.raises(FlexError, match="Token has expired"):
        client.fetch_positions(1440609)


@respx.mock
def test_send_request_retries_on_1001(client: FlexClient) -> None:
    """ErrorCode 1001 (Statement could not be generated) is transient — retry."""
    respx.get(f"{BASE}/FlexStatementService.SendRequest").mock(
        side_effect=[
            httpx.Response(200, text=_SEND_TRANSIENT_1001),
            httpx.Response(200, text=_SEND_OK),
        ]
    )
    respx.get(f"{BASE}/FlexStatementService.GetStatement").mock(
        return_value=httpx.Response(200, text=_SAMPLE_QUERY_RESPONSE)
    )
    meta, positions = client.fetch_positions(1440609)
    assert len(positions) == 2


@respx.mock
def test_send_request_exhausts_retries(client: FlexClient) -> None:
    respx.get(f"{BASE}/FlexStatementService.SendRequest").mock(
        return_value=httpx.Response(200, text=_SEND_TRANSIENT_1001)
    )
    with pytest.raises(FlexError, match="gave up"):
        client.fetch_positions(1440609)


@respx.mock
def test_get_statement_non_pending_error_raises(client: FlexClient) -> None:
    respx.get(f"{BASE}/FlexStatementService.SendRequest").mock(
        return_value=httpx.Response(200, text=_SEND_OK)
    )
    respx.get(f"{BASE}/FlexStatementService.GetStatement").mock(
        return_value=httpx.Response(
            200,
            text=(
                "<FlexStatementResponse><Status>Fail</Status>"
                "<ErrorCode>1003</ErrorCode><ErrorMessage>Statement is too large.</ErrorMessage>"
                "</FlexStatementResponse>"
            ),
        )
    )
    with pytest.raises(FlexError, match="too large"):
        client.fetch_positions(1440609)


@respx.mock
def test_unparseable_xml_raises(client: FlexClient) -> None:
    respx.get(f"{BASE}/FlexStatementService.SendRequest").mock(
        return_value=httpx.Response(200, text=_SEND_OK)
    )
    respx.get(f"{BASE}/FlexStatementService.GetStatement").mock(
        return_value=httpx.Response(200, text="not<<xml>>")
    )
    with pytest.raises(FlexError):
        client.fetch_positions(1440609)


@respx.mock
def test_empty_open_positions_returns_empty_list(client: FlexClient) -> None:
    respx.get(f"{BASE}/FlexStatementService.SendRequest").mock(
        return_value=httpx.Response(200, text=_SEND_OK)
    )
    respx.get(f"{BASE}/FlexStatementService.GetStatement").mock(
        return_value=httpx.Response(
            200,
            text=(
                '<FlexQueryResponse queryName="Live" type="AF">'
                '<FlexStatements count="1">'
                '<FlexStatement accountId="U1" fromDate="20260520"'
                ' toDate="20260520" period="x" whenGenerated="20260521;000000">'
                "</FlexStatement></FlexStatements></FlexQueryResponse>"
            ),
        )
    )
    meta, positions = client.fetch_positions(1440609)
    assert positions == []
    assert meta.account_id == "U1"


def test_missing_token_rejected() -> None:
    with pytest.raises(FlexError, match="FLEX_TOKEN_KEY"):
        FlexClient(token="")
