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
from quant_researcher.models.trades import Trade

_VALID_SOURCES = ("flex", "csv", "manual")


@dataclass(frozen=True)
class ImportResult:
    source: str
    account_id: str | None
    as_of_date: date | None
    imported: int
    symbols: list[str]
    skipped: list[dict[str, str]]


@dataclass(frozen=True)
class TradeImportResult:
    source: str
    account_id: str | None
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

    # Flex returns one `<OpenPosition>` per tax lot — a position split across N
    # lots arrives as N rows with the same (accountId, symbol, reportDate). The
    # warehouse stores one row per (account, symbol, as_of_date), so we collapse
    # lots into a single position row before mapping. Skipping this step makes
    # `session.merge` overwrite N times and only the last lot survives — a real
    # bug that silently understated positions like TSLA (11 lots → 80 shares
    # collapsed to "10 shares" because the last lot happened to be 10).
    if source == "flex":
        payload = _collapse_flex_lots(payload)

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


def _collapse_flex_lots(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce IBKR Flex `<OpenPosition>` rows to one row per position key.

    Per position key `(accountId, symbol, reportDate)`, a Flex Live-Positions
    response can contain:
      1. A single row — the simple case, pass through.
      2. One SUMMARY row plus N LOT rows — when the Flex Query has lot
         detail enabled. The SUMMARY's `position` already equals Σ(LOT
         positions) and `costBasisPrice` is already the weighted avg; we
         **keep the SUMMARY** and drop the lots (taking IBKR's own
         aggregation is more trustworthy than re-deriving it).
      3. Only LOT rows (no summary) — older / minimal queries. We aggregate
         them ourselves: positions summed; cost = Σ(costBasisMoney) / Σ(qty)
         with a position-weighted-mean fallback if `costBasisMoney` is
         missing; other numeric fields summed; categorical fields taken
         from the first row; side re-derived from the signed total.

    Detection ladder:
      • `levelOfDetail` attribute (`"SUMMARY"` / `"LOT"`) when IBKR gives it.
      • Heuristic: a row whose `position` equals the sum of all OTHER rows'
        positions (within tolerance) is the implicit summary — this is how
        IBKR's "lot detail" mode lays out a multi-fill equity position when
        `levelOfDetail` isn't included in the query output.
      • Otherwise, all rows are treated as lots and aggregated.

    History: an earlier fix unconditionally aggregated every group, which
    silently **double-counted** when both a SUMMARY and the lots were
    present (TSLA: 1 summary of 40 + 10 lots summing to 40 → "80 shares").
    The IBKR Activity Statement is the source of truth; the SUMMARY row
    matches it exactly.
    """
    if not rows:
        return rows
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str, str]] = []
    for row in rows:
        key = (
            row.get("accountId") or "",
            row.get("symbol") or "",
            row.get("reportDate") or "",
        )
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    out: list[dict[str, Any]] = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            out.append(group[0])
            continue

        # (1) IBKR-provided levelOfDetail.
        summaries = [
            r for r in group
            if (r.get("levelOfDetail") or "").upper() == "SUMMARY"
        ]
        if summaries:
            out.append(summaries[0])
            continue

        # (2) Heuristic: a row whose position is the sum of all other rows'
        # positions is the implicit summary.
        positions = [_as_float(r.get("position")) for r in group]
        if all(p is not None for p in positions):
            total = sum(positions)  # type: ignore[arg-type]
            tol = max(1e-6, abs(total) * 1e-9)
            for i, p in enumerate(positions):
                if p is not None and abs(2 * p - total) < tol:
                    out.append(group[i])
                    break
            else:
                # No summary row found — fall through to aggregation.
                out.append(_aggregate_flex_lots(group))
            continue

        # (3) Some position fields missing — best-effort aggregate.
        out.append(_aggregate_flex_lots(group))
    return out


def _aggregate_flex_lots(lots: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum a list of LOT rows into one row matching the SUMMARY shape.

    Used only when no SUMMARY row is present in the response. Field rules
    are documented in `_collapse_flex_lots`. The original per-lot rows are
    preserved under `_lots` for downstream audit (lands in `Holding.raw`).
    """
    head = dict(lots[0])

    def _sum(field: str) -> float | None:
        vals = [_as_float(lot.get(field)) for lot in lots]
        present = [v for v in vals if v is not None]
        return sum(present) if present else None

    total_qty = _sum("position") or 0.0
    total_cb_money = _sum("costBasisMoney")
    total_value = _sum("positionValue")

    if total_cb_money is not None and total_qty:
        avg_cost: float | None = total_cb_money / total_qty
    else:
        num = 0.0
        den = 0.0
        for lot in lots:
            q = _as_float(lot.get("position"))
            p = _as_float(lot.get("costBasisPrice"))
            if q is not None and p is not None:
                num += q * p
                den += q
        avg_cost = num / den if den else None

    head["position"] = total_qty
    head["positionValue"] = total_value
    head["costBasisMoney"] = total_cb_money
    head["costBasisPrice"] = avg_cost
    head["fifoPnlUnrealized"] = _sum("fifoPnlUnrealized")
    head["percentOfNAV"] = _sum("percentOfNAV")
    if total_qty > 0:
        head["side"] = "Long"
    elif total_qty < 0:
        head["side"] = "Short"
    head["_lots"] = lots
    return head


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


def import_trades(
    session: Session,
    *,
    payload: list[dict[str, Any]],
) -> TradeImportResult:
    """Map raw Flex `<Trade>` attr dicts → `Trade` rows and `session.merge` them.

    An empty `payload` is a legitimate no-trade day (returns `imported=0`),
    not an error. `merge` keys on `(account_id, ib_exec_id)` so re-pulling the
    same business day is idempotent.
    """
    mapped: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for i, raw in enumerate(payload):
        try:
            row = _flex_to_trade(raw)
            for col in ("account_id", "ib_exec_id", "symbol"):
                if not row.get(col):
                    raise ValueError(f"missing {col}")
            mapped.append(row)
        except (ValueError, KeyError, TypeError) as exc:
            skipped.append({"index": str(i), "error": str(exc)})

    for row in mapped:
        session.merge(Trade(source="flex", **row))

    accounts = {r["account_id"] for r in mapped}
    return TradeImportResult(
        source="flex",
        account_id=next(iter(accounts)) if len(accounts) == 1 else None,
        imported=len(mapped),
        symbols=sorted({r["symbol"] for r in mapped}),
        skipped=skipped,
    )


def _flex_to_trade(row: dict[str, Any]) -> dict[str, Any]:
    """Map raw IBKR Flex `<Trade>` attribute dict → Trade kwargs."""
    return {
        "account_id": row.get("accountId") or "",
        "ib_exec_id": row.get("ibExecID") or "",
        "trade_id": row.get("tradeID") or None,
        "symbol": row.get("symbol") or "",
        "conid": _as_int(row.get("conid")),
        "asset_category": row.get("assetCategory") or "STK",
        "sub_category": row.get("subCategory") or None,
        "description": row.get("description") or None,
        "trade_date": _parse_flex_date(row.get("tradeDate")),
        "executed_at": row.get("dateTime") or None,
        "side": row.get("buySell") or None,
        "quantity": _as_float(row.get("quantity")) or 0.0,
        "price": _as_float(row.get("tradePrice")),
        "proceeds": _as_float(row.get("proceeds")),
        "net_cash": _as_float(row.get("netCash")),
        "commission": _as_float(row.get("ibCommission")),
        "realized_pnl": _as_float(row.get("fifoPnlRealized")),
        "open_close": row.get("openCloseIndicator") or None,
        "order_reference": row.get("orderReference") or None,
        "exchange": row.get("exchange") or None,
        "currency": row.get("currency") or None,
        "fx_rate_to_base": _as_float(row.get("fxRateToBase")),
        "notes": row.get("notes") or row.get("code") or None,
        "raw": dict(row),
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
