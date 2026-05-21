"""Unified holdings importer — accepts Flex / CSV / manual payloads.

Each surface (`flex` / `csv`) is translated into a uniform list of column
dicts that map 1:1 to `Holding` rows. The session writer uses
`session.merge` so re-running for the same `(account_id, symbol,
as_of_date)` overwrites (intra-day refresh is idempotent).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from quant_researcher.models.holdings import Holding

_VALID_SOURCES = ("flex", "csv", "manual")


@dataclass(frozen=True)
class ImportResult:
    source: str
    account_id: str | None
    as_of_date: date | None
    imported: int
    symbols: list[str]
    skipped: list[dict[str, str]]


def import_holdings(
    session: Session,
    *,
    source: str,
    payload: list[dict[str, Any]],
    account_id_override: str | None = None,
    as_of_date_override: date | None = None,
) -> ImportResult:
    """Map `payload` rows to `Holding` instances and `session.merge` them."""
    if source not in _VALID_SOURCES:
        raise ValueError(f"unknown source: {source!r}; valid: {_VALID_SOURCES}")
    if not payload:
        raise ValueError("payload is empty")

    mapped: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for i, raw in enumerate(payload):
        try:
            if source == "flex":
                row = _flex_to_holding(raw)
            elif source == "csv":
                row = _csv_to_holding(raw, account_id_override, as_of_date_override)
            else:
                row = dict(raw)
            # PK fields required.
            for col in ("account_id", "symbol", "as_of_date"):
                if not row.get(col):
                    raise ValueError(f"missing {col}")
            mapped.append(row)
        except (ValueError, KeyError, TypeError) as exc:
            skipped.append({"index": str(i), "error": str(exc)})

    for row in mapped:
        session.merge(Holding(source=source, **row))

    accounts = {r["account_id"] for r in mapped}
    dates = {r["as_of_date"] for r in mapped}
    return ImportResult(
        source=source,
        account_id=next(iter(accounts)) if len(accounts) == 1 else None,
        as_of_date=next(iter(dates)) if len(dates) == 1 else None,
        imported=len(mapped),
        symbols=sorted({r["symbol"] for r in mapped}),
        skipped=skipped,
    )


def _flex_to_holding(row: dict[str, Any]) -> dict[str, Any]:
    """Map raw IBKR Flex `<OpenPosition>` attribute dict → Holding kwargs."""
    return {
        "account_id": row.get("accountId") or "",
        "symbol": row.get("symbol") or "",
        "as_of_date": _parse_flex_date(row.get("reportDate")),
        "asset_category": row.get("assetCategory") or "STK",
        "sub_category": row.get("subCategory") or None,
        "quantity": _as_float(row.get("position")) or 0.0,
        "mark_price": _as_float(row.get("markPrice")),
        "market_value": _as_float(row.get("positionValue")),
        "avg_cost": _as_float(row.get("costBasisPrice")),
        "cost_basis_total": _as_float(row.get("costBasisMoney")),
        "unrealized_pnl": _as_float(row.get("fifoPnlUnrealized")),
        "percent_of_nav": _as_float(row.get("percentOfNAV")),
        "side": row.get("side") or None,
        "currency": row.get("currency") or None,
        "fx_rate_to_base": _as_float(row.get("fxRateToBase")),
        "conid": _as_int(row.get("conid")),
        "listing_exchange": row.get("listingExchange") or None,
        "description": row.get("description") or None,
        "raw": dict(row),
    }


def _csv_to_holding(
    row: dict[str, Any],
    account_override: str | None,
    as_of_override: date | None,
) -> dict[str, Any]:
    account = row.get("account_id") or account_override
    if not account:
        raise ValueError("missing account_id (no row value, no override)")
    as_of = row.get("as_of_date") or as_of_override
    if as_of is None:
        raise ValueError("missing as_of_date (no row value, no override)")
    qty = row.get("quantity")
    if qty is None:
        raise ValueError("missing quantity")
    qty_f = float(qty)
    return {
        "account_id": str(account),
        "symbol": str(row["symbol"]),
        "as_of_date": as_of,
        "asset_category": row.get("asset_category") or "STK",
        "sub_category": row.get("sub_category") or None,
        "quantity": qty_f,
        "mark_price": _as_float(row.get("mark_price")),
        "market_value": _as_float(row.get("market_value")),
        "avg_cost": _as_float(row.get("avg_cost")),
        "cost_basis_total": None,
        "unrealized_pnl": None,
        "percent_of_nav": None,
        "side": row.get("side") or ("Long" if qty_f >= 0 else "Short"),
        "currency": row.get("currency") or None,
        "fx_rate_to_base": None,
        "conid": None,
        "listing_exchange": None,
        "description": row.get("description") or None,
        "raw": {k: v for k, v in row.items() if k not in {"as_of_date"}},
    }


def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _parse_flex_date(s: Any) -> date | None:
    """Flex `reportDate` is `YYYYMMDD`."""
    if not s:
        return None
    s = str(s).strip()
    if len(s) == 8 and s.isdigit():
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None
