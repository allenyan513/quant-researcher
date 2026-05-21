"""FMP REST client (stable endpoint).

Thin httpx wrapper with two non-functional concerns built in:

* Token-bucket rate limit (default 250 calls / 60s — the stable plan ceiling).
  A `collections.deque[float]` holds wall-clock timestamps of recent calls;
  before each request we evict timestamps older than the window and sleep if
  we'd otherwise burst past the limit.
* Retry with exponential backoff + jitter on 429 / 5xx (transient). Other
  HTTP errors raise `FMPError` immediately.

Public surface (MA-2):
    `get_profile(symbol)` → single dict (or None on empty response)
    `get_historical_prices(symbol, since=None)` → list of OHLCV dicts

The client owns no business logic — callers map FMP payloads onto SQLAlchemy
rows in `quant_researcher.data.refresh`.
"""

from __future__ import annotations

import random
import time
from collections import deque
from datetime import date
from typing import Any

import httpx


class FMPError(RuntimeError):
    """Any non-recoverable FMP response."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class FMPClient:
    """Synchronous FMP /stable client."""

    DEFAULT_BASE_URL = "https://financialmodelingprep.com/stable"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        rate_limit_per_minute: int = 250,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_max: float = 8.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise FMPError("FMP_API_KEY is not set (configure it in .env).")
        self._api_key = api_key
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._rate_limit = rate_limit_per_minute
        self._window_s = 60.0
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._timestamps: deque[float] = deque()
        self._client = httpx.Client(timeout=timeout, transport=transport)

    # -- public ------------------------------------------------------------

    def get_profile(self, symbol: str) -> dict[str, Any] | None:
        """Return the first row from `/profile?symbol=…` (FMP returns a list)."""
        rows = self._get("/profile", {"symbol": symbol})
        if not isinstance(rows, list):
            raise FMPError(f"profile: expected list, got {type(rows).__name__}")
        return rows[0] if rows else None

    def get_historical_prices(
        self, symbol: str, *, since: date | None = None
    ) -> list[dict[str, Any]]:
        """Return OHLCV rows from `/historical-price-eod/full`.

        `since` (inclusive) trims the request range; otherwise FMP returns
        the full history it has. Result is the raw list FMP gives us
        (newest-first); callers map → `DailyPrice` rows.
        """
        params: dict[str, Any] = {"symbol": symbol}
        if since is not None:
            params["from"] = since.isoformat()
        payload = self._get("/historical-price-eod/full", params)
        # FMP sometimes wraps history under `{symbol, historical: [...]}` and
        # sometimes returns the bare list — handle both.
        if isinstance(payload, dict):
            rows = payload.get("historical") or []
        elif isinstance(payload, list):
            rows = payload
        else:
            raise FMPError(
                f"historical-price-eod/full: unexpected payload type {type(payload).__name__}"
            )
        return rows

    def get_income_statement(
        self, symbol: str, *, period: str = "quarter", limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Return `/income-statement` rows (newest-first; FMP default ~40 periods)."""
        return self._get_period_list("/income-statement", symbol, period, limit)

    def get_balance_sheet(
        self, symbol: str, *, period: str = "quarter", limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Return `/balance-sheet-statement` rows."""
        return self._get_period_list("/balance-sheet-statement", symbol, period, limit)

    def get_cash_flow(
        self, symbol: str, *, period: str = "quarter", limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Return `/cash-flow-statement` rows."""
        return self._get_period_list("/cash-flow-statement", symbol, period, limit)

    def get_ratios(
        self, symbol: str, *, period: str = "quarter", limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Return `/ratios` rows (period-keyed financial ratios)."""
        return self._get_period_list("/ratios", symbol, period, limit)

    def get_analyst_estimates(
        self, symbol: str, *, period: str = "quarter", limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Return `/analyst-estimates` rows (forward-looking consensus)."""
        return self._get_period_list("/analyst-estimates", symbol, period, limit)

    def get_news(
        self, symbols: list[str] | str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Recent news for one or more symbols (comma-separated).

        Uses `/news/stock` — `/news/stock-latest` ignores the `symbols`
        param and returns global headlines. Verified against FMP /stable
        2026-05-21.

        Returns `[]` on HTTP 402 (premium-gated on lighter plans) — MD's
        bundler treats missing news as a soft signal, not an error.
        """
        syms = ",".join(symbols) if isinstance(symbols, list) else symbols
        try:
            payload = self._get(
                "/news/stock", {"symbols": syms, "limit": limit}
            )
        except FMPError as exc:
            if exc.status_code == 402:
                return []
            raise
        return payload if isinstance(payload, list) else []

    def get_earnings_transcript(
        self,
        symbol: str,
        *,
        year: int | None = None,
        quarter: int | None = None,
    ) -> list[dict[str, Any]]:
        """Earnings call transcript(s) — defaults to the latest available.

        Returns `[]` on HTTP 402 (premium-gated). Each row typically has
        `date`, `quarter`, `year`, `content` (the actual transcript text).
        """
        params: dict[str, Any] = {"symbol": symbol}
        if year is not None:
            params["year"] = year
        if quarter is not None:
            params["quarter"] = quarter
        try:
            payload = self._get("/earning-call-transcript", params)
        except FMPError as exc:
            if exc.status_code == 402:
                return []
            raise
        return payload if isinstance(payload, list) else []

    def _get_period_list(
        self, path: str, symbol: str, period: str, limit: int | None
    ) -> list[dict[str, Any]]:
        """Shared shape for period-keyed list endpoints (statements/ratios/estimates)."""
        params: dict[str, Any] = {"symbol": symbol, "period": period}
        if limit is not None:
            params["limit"] = limit
        payload = self._get(path, params)
        if not isinstance(payload, list):
            raise FMPError(f"{path}: expected list, got {type(payload).__name__}")
        return payload

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FMPClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{self._base_url}{path}"
        merged = {**params, "apikey": self._api_key}
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            self._wait_for_token()
            try:
                resp = self._client.get(url, params=merged)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self._max_retries:
                    self._sleep_backoff(attempt)
                    continue
                raise FMPError(f"GET {path} transport error: {exc}") from exc

            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self._max_retries:
                self._sleep_backoff(attempt)
                continue
            # Non-retriable, or retries exhausted.
            raise FMPError(
                f"GET {path} → HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        # Unreachable: the loop either returns or raises. Defensive:
        raise FMPError(f"GET {path} exhausted retries: {last_error}")

    def _wait_for_token(self) -> None:
        now = time.monotonic()
        cutoff = now - self._window_s
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        if len(self._timestamps) >= self._rate_limit:
            sleep_for = self._timestamps[0] + self._window_s - now
            if sleep_for > 0:
                time.sleep(sleep_for)
                # Re-evict after sleeping.
                now = time.monotonic()
                cutoff = now - self._window_s
                while self._timestamps and self._timestamps[0] < cutoff:
                    self._timestamps.popleft()
        self._timestamps.append(time.monotonic())

    def _sleep_backoff(self, attempt: int) -> None:
        delay = min(self._backoff_base * (2**attempt), self._backoff_max)
        # Full jitter (AWS pattern) — pick uniformly in [0, delay].
        time.sleep(random.uniform(0.0, delay))
