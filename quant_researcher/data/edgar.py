"""SEC EDGAR client — insider transactions (Form 4) via edgartools.

Thin wrapper: set the SEC-required identity ("Name email") once, then pull a
company's recent Form 4 filings and flatten each filing's transaction table into
normalized rows. edgartools handles CIK lookup, the mandatory User-Agent, and
SEC's ≤10 req/s rate limit. FMP gates insider data behind a premium tier; SEC
EDGAR is free.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


class EdgarError(RuntimeError):
    """Any non-recoverable SEC EDGAR / edgartools failure."""


class EdgarClient:
    """edgartools-backed client (insider Form 4 only, for now)."""

    def __init__(self, identity: str) -> None:
        if not identity:
            raise EdgarError("SEC_EDGAR_IDENTITY is not set (configure it in .env).")
        import edgar  # lazy — heavy optional dep

        edgar.set_identity(identity)
        self._edgar = edgar

    def get_insider_transactions(
        self, symbol: str, *, since: date | None = None, max_filings: int = 60
    ) -> list[dict[str, Any]]:
        """Recent Form 4 transaction rows for `symbol`, newest filings first.

        Each row carries `accession_no` + `line_no` (the dedup key) plus
        filing_date / transaction_date / insider / position / transaction_type /
        code / shares / price / value / remaining_shares / security. Returns an
        empty list when the company has no Form 4 filings in the window.
        """
        try:
            company = self._edgar.Company(symbol)
        except Exception as exc:  # noqa: BLE001 — edgartools raises bare Exceptions
            raise EdgarError(f"{symbol}: EDGAR company lookup failed: {exc}") from exc
        if company is None:
            return []
        filings = company.get_filings(form="4")
        if filings is None or len(filings) == 0:
            return []

        out: list[dict[str, Any]] = []
        for n, filing in enumerate(filings):
            if n >= max_filings:
                break
            fdate = _as_date(getattr(filing, "filing_date", None))
            # Filings are newest-first, so once we pass the window we can stop.
            if since is not None and fdate is not None and fdate < since:
                break
            accession = str(
                getattr(filing, "accession_no", None)
                or getattr(filing, "accession_number", "")
            )
            try:
                df = filing.obj().to_dataframe()
            except Exception:  # noqa: BLE001 — a single unparseable filing is skipped
                continue
            for i, (_, row) in enumerate(df.iterrows()):
                out.append(
                    {
                        "accession_no": accession,
                        "line_no": i,
                        "filing_date": fdate,
                        "transaction_date": _as_date(row.get("Date")),
                        "insider": _s(row.get("Insider")),
                        "position": _s(row.get("Position")),
                        "transaction_type": _s(row.get("Transaction Type")),
                        "code": _s(row.get("Code")),
                        "shares": _f(row.get("Shares")),
                        "price": _f(row.get("Price")),
                        "value": _f(row.get("Value")),
                        "remaining_shares": _f(row.get("Remaining Shares")),
                        "security": _s(row.get("Description")),
                    }
                )
        return out


# ----- coercion helpers (pandas-tolerant) ----------------------------------


def _as_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if hasattr(v, "date"):  # pandas Timestamp
        try:
            return v.date()
        except Exception:  # noqa: BLE001
            pass
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _s(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    return s
