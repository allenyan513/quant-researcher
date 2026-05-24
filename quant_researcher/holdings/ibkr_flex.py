"""IBKR Flex Statement client (Python port of the docs example).

Two-step flow:
1. `SendRequest?t=<token>&q=<query_id>&v=3` → XML with `ReferenceCode`.
2. `GetStatement?t=<token>&q=<ref>&v=3` → either the full `FlexQueryResponse`
   (XML with positions) or a `FlexStatementResponse` with ErrorCode `1019`
   meaning "still generating, retry shortly". We poll up to
   `max_poll_attempts` times with `poll_delay` seconds between calls.

The client returns the meta + a list of raw attribute dicts per row
(`<OpenPosition>` for `fetch_positions`, `<Trade>` for `fetch_trades`).
Field mapping → SQLAlchemy columns lives in `holdings/importer.py` — the
client is a thin XML-to-Python translator.

Schema reference (from a real Flex Live-Positions response, 2026-05-21):
* `accountId`, `symbol`, `description`, `currency`, `assetCategory` (STK/OPT),
  `subCategory` (COMMON/CALL/PUT), `position` (qty, can be negative),
  `markPrice`, `positionValue`, `costBasisPrice`, `costBasisMoney`,
  `fifoPnlUnrealized`, `percentOfNAV`, `side` (Long/Short),
  `fxRateToBase`, `conid`, `listingExchange`, `reportDate` (YYYYMMDD).
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import httpx


class FlexError(RuntimeError):
    """Any non-recoverable Flex API failure (bad creds, malformed XML, …)."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


# IBKR error codes that mean "transient, try again shortly":
#   1001 — Statement could not be generated at this time.
#   1004 — Statement is not yet available.
#   1007 — Statement is not yet generated. Statement generation is in progress.
#   1019 — Statement generation in progress. Please try again shortly.
# Everything else is treated as permanent (bad token, bad query id, etc.)
_TRANSIENT_CODES = frozenset({"1001", "1004", "1007", "1019"})


@dataclass(frozen=True)
class FlexStatementMeta:
    """Per-statement header info pulled from the response root."""

    account_id: str
    from_date: str
    to_date: str
    when_generated: str
    query_name: str


class FlexClient:
    """Synchronous IBKR Flex Statement client."""

    DEFAULT_BASE_URL = "https://gdcdyn.interactivebrokers.com/Universal/servlet"

    def __init__(
        self,
        token: str,
        *,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_poll_attempts: int = 6,
        poll_delay: float = 8.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not token:
            raise FlexError("FLEX_TOKEN_KEY is not set (configure it in .env).")
        self._token = token
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._max_polls = max_poll_attempts
        self._poll_delay = poll_delay
        self._client = httpx.Client(timeout=timeout, transport=transport)

    # ---- public ----------------------------------------------------------

    def fetch_positions(
        self, query_id: str | int
    ) -> tuple[FlexStatementMeta, list[dict[str, str]]]:
        """End-to-end: send request, poll until ready, return (meta, positions)."""
        ref = self._send_request(str(query_id))
        xml_text = self._poll_statement(ref)
        return self._parse(xml_text, "OpenPositions", "OpenPosition")

    def fetch_trades(
        self, query_id: str | int
    ) -> tuple[FlexStatementMeta, list[dict[str, str]]]:
        """End-to-end: send request, poll until ready, return (meta, trades).

        Requires the Flex Query to include the **Trades** section (a no-trade
        period yields an empty list, not an error).
        """
        ref = self._send_request(str(query_id))
        xml_text = self._poll_statement(ref)
        return self._parse(xml_text, "Trades", "Trade")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FlexClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---- internals -------------------------------------------------------

    def _send_request(self, query_id: str) -> str:
        last_err: str | None = None
        for attempt in range(1, self._max_polls + 1):
            if attempt > 1:
                time.sleep(self._poll_delay)
            r = self._client.get(
                f"{self._base_url}/FlexStatementService.SendRequest",
                params={"t": self._token, "q": query_id, "v": 3},
            )
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                continue
            try:
                root = ET.fromstring(r.text)
            except ET.ParseError as exc:
                raise FlexError(f"SendRequest malformed XML: {exc}") from exc
            status = root.findtext("Status") or ""
            if status == "Success":
                ref = root.findtext("ReferenceCode")
                if not ref:
                    raise FlexError(
                        "SendRequest succeeded but no ReferenceCode in response"
                    )
                return ref
            code = root.findtext("ErrorCode") or ""
            msg = root.findtext("ErrorMessage") or ""
            if code in _TRANSIENT_CODES:
                last_err = f"transient {code}: {msg}"
                continue
            # Permanent failure (bad token, etc.) — surface immediately.
            raise FlexError(
                f"SendRequest failed: status={status!r} code={code!r} msg={msg!r}",
                code=code,
            )
        raise FlexError(
            f"SendRequest gave up after {self._max_polls} attempts: {last_err}"
        )

    def _poll_statement(self, ref: str) -> str:
        last_err: str | None = None
        for _attempt in range(1, self._max_polls + 1):
            time.sleep(self._poll_delay)
            r = self._client.get(
                f"{self._base_url}/FlexStatementService.GetStatement",
                params={"t": self._token, "q": ref, "v": 3},
            )
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                continue
            text = r.text
            # Pending or error responses use FlexStatementResponse.
            if text.lstrip().startswith("<FlexStatementResponse"):
                try:
                    root = ET.fromstring(text)
                except ET.ParseError as exc:
                    raise FlexError(
                        f"GetStatement malformed FlexStatementResponse: {exc}"
                    ) from exc
                status = root.findtext("Status") or ""
                code = root.findtext("ErrorCode") or ""
                msg = root.findtext("ErrorMessage") or ""
                if code in _TRANSIENT_CODES:
                    last_err = f"pending {code}: {msg}"
                    continue
                raise FlexError(
                    f"GetStatement failed: status={status!r} code={code!r} msg={msg!r}",
                    code=code,
                )
            # Otherwise we got the actual <FlexQueryResponse>.
            return text
        raise FlexError(
            f"GetStatement still pending after {self._max_polls} attempts: {last_err}"
        )

    def _parse(
        self, xml_text: str, container_tag: str, row_tag: str
    ) -> tuple[FlexStatementMeta, list[dict[str, str]]]:
        """Parse the statement header + every `<row_tag>` under `<container_tag>`.

        A missing/empty section yields an empty row list (legitimate for a
        no-trade period), not an error.
        """
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise FlexError(f"unparseable FlexQueryResponse: {exc}") from exc
        if root.tag != "FlexQueryResponse":
            raise FlexError(f"unexpected root element: {root.tag!r}")
        stmts = root.find("FlexStatements")
        if stmts is None or len(stmts) == 0:
            raise FlexError("no FlexStatements in response")
        stmt = stmts[0]
        meta = FlexStatementMeta(
            account_id=stmt.get("accountId", ""),
            from_date=stmt.get("fromDate", ""),
            to_date=stmt.get("toDate", ""),
            when_generated=stmt.get("whenGenerated", ""),
            query_name=root.get("queryName", ""),
        )
        rows: list[dict[str, Any]] = []
        container = stmt.find(container_tag)
        if container is not None:
            for el in container.findall(row_tag):
                rows.append(dict(el.attrib))
        return meta, rows
