"""FINRA Equity Short Interest client — free, auth-free bi-monthly CSV.

FINRA publishes one CSV per settlement date covering all securities at
`https://cdn.finra.org/equity/otcmarket/biweekly/shrtYYYYMMDD.csv` (no auth, no
key). Settlements land mid-month (~15th) and month-end, published ~7 business
days later. This client resolves the latest *published* file by probing recent
settlement dates newest-first, downloads it once, and returns the rows for the
requested symbols — one download serves every symbol.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Any

import httpx


class FinraError(RuntimeError):
    """Any non-recoverable FINRA download/parse failure."""


class FinraClient:
    DEFAULT_BASE_URL = "https://cdn.finra.org/equity/otcmarket/biweekly"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_lookback_files: int = 6,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._max_lookback = max_lookback_files
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def get_short_interest(
        self, symbols: list[str], *, today: date | None = None
    ) -> dict[str, dict[str, Any]]:
        """Latest published short interest for `symbols` → `{SYMBOL: {...}}`.

        Probes recent settlement dates newest-first; the first file that exists is
        the latest published (this absorbs the ~7-business-day lag). Returns only
        the requested symbols present in that file; empty dict if no file is found
        in the lookback window.
        """
        wanted = {s.upper() for s in symbols}
        if not wanted:
            return {}
        for d in _candidate_settlement_dates(today or date.today(), self._max_lookback):
            text = self._fetch_csv(d)
            if text is not None:
                return _parse(text, wanted, d)
        return {}

    def _fetch_csv(self, d: date) -> str | None:
        url = f"{self._base}/shrt{d.strftime('%Y%m%d')}.csv"
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as exc:
            raise FinraError(f"GET {url}: {exc}") from exc
        # FINRA's CDN returns 403 (not 404) for a settlement date whose file isn't
        # published yet — treat both as "not available, try the prior settlement".
        if resp.status_code in (403, 404):
            return None
        if resp.status_code != 200:
            raise FinraError(f"GET {url} → HTTP {resp.status_code}")
        return resp.text

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FinraClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ----- internals -----------------------------------------------------------


def _candidate_settlement_dates(today: date, months_back: int) -> list[date]:
    """Recent FINRA settlement dates (mid-month + month-end), newest-first.

    Settlements are the 15th and the last day of each month, nudged back to the
    prior weekday on a weekend. (Holidays are rare; a missing file just falls
    through to the next candidate.)
    """
    out: list[date] = []
    year, month = today.year, today.month
    for _ in range(months_back):
        mid = _prev_weekday(date(year, month, 15))
        eom = _prev_weekday(_last_day_of_month(year, month))
        out.extend(d for d in (eom, mid) if d <= today)
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return sorted(set(out), reverse=True)


def _last_day_of_month(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _prev_weekday(d: date) -> date:
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d -= timedelta(days=1)
    return d


def _parse(
    text: str, wanted: set[str], settlement_date: date
) -> dict[str, dict[str, Any]]:
    # FINRA's short-interest file is PIPE-delimited (not comma).
    out: dict[str, dict[str, Any]] = {}
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    if reader.fieldnames:  # strip header padding so row.get(key) can't silently miss
        reader.fieldnames = [f.strip() for f in reader.fieldnames]
    for row in reader:
        sym = (_get(row, "symbolCode", "Symbol") or "").upper()
        if not sym or sym not in wanted:
            continue
        out[sym] = {
            "settlement_date": settlement_date,
            "short_interest": _f(_get(row, "currentShortPositionQuantity")),
            "previous_short_interest": _f(_get(row, "previousShortPositionQuantity")),
            "change_pct": _f(_get(row, "changePercent")),
            "avg_daily_volume": _f(_get(row, "averageDailyVolumeQuantity")),
            "days_to_cover": _f(_get(row, "daysToCoverQuantity")),
            "security_name": _get(row, "issueName", "securityName") or None,
        }
    return out


def _get(row: dict[str, Any], *keys: str) -> str | None:
    # Return the stripped value: a padded symbolCode would otherwise fail the
    # `sym not in wanted` match and silently drop the row.
    for k in keys:
        v = row.get(k)
        if v is not None and (s := str(v).strip()):
            return s
    return None


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
