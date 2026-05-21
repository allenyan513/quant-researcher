"""CSV holdings importer — for users who don't want to wire Flex.

Expected schema (headers, case-insensitive):
* **Required**: `account_id`, `symbol`, `quantity`, `as_of_date` (YYYY-MM-DD).
* **Optional**: `asset_category` (default STK), `sub_category`, `avg_cost`,
  `mark_price`, `market_value`, `currency`, `description`, `side`.

Numbers parse as floats; empty cells → `None`. Extra columns are ignored.
"""

from __future__ import annotations

import csv as stdlib_csv
from datetime import date
from pathlib import Path
from typing import Any

_REQUIRED = {"account_id", "symbol", "quantity", "as_of_date"}
_NUMERIC_OPTIONAL = ("avg_cost", "mark_price", "market_value")


class CSVError(ValueError):
    """Raised on schema or per-row parse errors."""


def parse_holdings_csv(path: Path) -> list[dict[str, Any]]:
    """Read `path` and return one dict per row, typed."""
    if not path.exists():
        raise CSVError(f"no such file: {path}")
    with path.open(newline="") as f:
        reader = stdlib_csv.DictReader(f)
        if not reader.fieldnames:
            raise CSVError(f"empty CSV (no header): {path}")
        headers = {(fn or "").strip().lower() for fn in reader.fieldnames}
        missing = _REQUIRED - headers
        if missing:
            raise CSVError(
                f"missing required columns: {sorted(missing)} "
                f"(found: {sorted(h for h in headers if h)})"
            )
        rows: list[dict[str, Any]] = []
        for line_no, raw in enumerate(reader, start=2):
            r = {
                (k or "").strip().lower(): (v.strip() if isinstance(v, str) else v)
                for k, v in raw.items()
                if k
            }
            rows.append(_coerce_row(r, line_no))
    return rows


def _coerce_row(r: dict[str, Any], line_no: int) -> dict[str, Any]:
    if not r.get("account_id") or not r.get("symbol"):
        raise CSVError(f"row {line_no}: account_id and symbol are required")
    try:
        r["quantity"] = float(r["quantity"])
    except (KeyError, ValueError, TypeError) as exc:
        raise CSVError(f"row {line_no}: bad quantity ({exc})") from exc
    try:
        r["as_of_date"] = date.fromisoformat(str(r["as_of_date"])[:10])
    except (KeyError, ValueError) as exc:
        raise CSVError(
            f"row {line_no}: bad as_of_date (need YYYY-MM-DD), got {r.get('as_of_date')!r}"
        ) from exc
    for col in _NUMERIC_OPTIONAL:
        v = r.get(col)
        if v is None or v == "":
            r[col] = None
        else:
            try:
                r[col] = float(v)
            except (ValueError, TypeError):
                r[col] = None
    r.setdefault("asset_category", "STK")
    return r
