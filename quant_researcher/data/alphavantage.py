"""Alpha Vantage client — earnings-call transcripts (free-tier source).

FMP gates transcripts behind a premium tier; Alpha Vantage serves them on the
**free** key (≈ 25 requests/day, ~1 request/second). This is a deliberately thin
httpx wrapper — only the one endpoint we need — with a min-interval throttle so
back-to-back calls don't trip the per-second limit.

`EARNINGS_CALL_TRANSCRIPT` requires an explicit `quarter` (`YYYY'Q'N`, e.g.
`2025Q1`); there is no "latest" (omitting `quarter` returns an empty payload).
Callers walk recent quarters and take the first that has data.

Alpha Vantage signals throttling / quota with a plain HTTP 200 carrying an
`Information` or `Note` key instead of data — we retry once after a pause, then
return None (a soft miss), so a rate cap degrades gracefully rather than erroring.
"""

from __future__ import annotations

import time
from typing import Any

import httpx


class AlphaVantageError(RuntimeError):
    """Any non-recoverable Alpha Vantage response (e.g. a non-200 HTTP status)."""


class AlphaVantageClient:
    """Synchronous Alpha Vantage client (transcripts only)."""

    DEFAULT_BASE_URL = "https://www.alphavantage.co"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        min_interval_s: float = 1.1,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise AlphaVantageError(
                "ALPHA_VANTAGE_API_KEY is not set (configure it in .env)."
            )
        self._api_key = api_key
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._min_interval_s = min_interval_s
        self._last_call = 0.0
        self._client = httpx.Client(timeout=timeout, transport=transport)

    # -- public ------------------------------------------------------------

    def get_earnings_transcript(
        self, symbol: str, *, quarter: str
    ) -> dict[str, Any] | None:
        """Return the raw AV transcript payload for `(symbol, quarter)`.

        `quarter` is `YYYY'Q'N` (e.g. `2025Q1`). Returns the full payload dict
        (`{symbol, quarter, transcript: [{speaker, title, content, sentiment}]}`)
        when a non-empty transcript exists, else None (no data for that quarter,
        or a rate-limit reply that survived one retry).
        """
        payload = self._get(
            {
                "function": "EARNINGS_CALL_TRANSCRIPT",
                "symbol": symbol,
                "quarter": quarter,
            }
        )
        transcript = payload.get("transcript") if isinstance(payload, dict) else None
        if isinstance(transcript, list) and transcript:
            return payload
        return None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AlphaVantageClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    def _get(self, params: dict[str, Any], *, _retried: bool = False) -> dict[str, Any]:
        self._throttle()
        resp = self._client.get(
            f"{self._base_url}/query", params={**params, "apikey": self._api_key}
        )
        if resp.status_code != 200:
            raise AlphaVantageError(
                f"GET /query → HTTP {resp.status_code}: {resp.text[:160]}"
            )
        payload = resp.json()
        if not isinstance(payload, dict):
            return {}
        # AV returns a throttle/quota notice (HTTP 200) under Information / Note /
        # Error Message instead of data. Retry once after a pause, then soft-miss.
        if any(k in payload for k in ("Information", "Note", "Error Message")):
            if not _retried:
                time.sleep(self._min_interval_s * 2)
                return self._get(params, _retried=True)
            return {}
        return payload

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval_s:
            time.sleep(self._min_interval_s - elapsed)
        self._last_call = time.monotonic()
