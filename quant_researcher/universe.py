"""Universe (watchlist) loader.

`qr universe set` replaces the whole `universe` table with the symbols from a
file, and upserts a `securities` master row for any new ticker. Pure file
parsing lives in `parse_watchlist_file` (used by tests + the CLI); DB ops
live in `replace_universe` / `list_universe`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from quant_researcher.models.securities import Security
from quant_researcher.models.universe import UniverseMember


@dataclass(frozen=True)
class ReplaceResult:
    """Diff summary for `qr universe set`."""

    total: int
    added: list[str]
    removed: list[str]
    kept: list[str]
    new_securities: list[str]


def parse_watchlist_file(path: Path) -> list[str]:
    """Read a watchlist file → de-duped, upper-cased, order-preserving list.

    Format: one ticker per line. Blank lines and `#`-prefixed comments are
    ignored. Whitespace is trimmed. Duplicates are dropped (first occurrence
    wins). Tickers are upper-cased so `aapl` and `AAPL` collapse to one.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        sym = line.upper()
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def list_universe(session: Session, limit: int | None = None) -> list[UniverseMember]:
    """Return all universe rows ordered by symbol."""
    stmt = select(UniverseMember).order_by(UniverseMember.symbol)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def replace_universe(
    session: Session, symbols: list[str], source: str | None = None
) -> ReplaceResult:
    """Replace the `universe` table contents with `symbols`, transactionally.

    Also upserts a `securities` master row for any ticker not yet known. Old
    securities rows are NOT deleted when a symbol leaves the watchlist —
    historical price/financial data referenced by other tables would dangle.
    Caller commits.
    """
    current_symbols = set(session.scalars(select(UniverseMember.symbol)))
    target_symbols = list(dict.fromkeys(symbols))  # preserve order, drop dups
    target_set = set(target_symbols)

    added = sorted(target_set - current_symbols)
    removed = sorted(current_symbols - target_set)
    kept = sorted(current_symbols & target_set)

    existing_securities = set(session.scalars(select(Security.symbol)))
    new_securities = sorted(target_set - existing_securities)

    session.execute(delete(UniverseMember))
    if target_symbols:
        session.execute(
            insert(UniverseMember),
            [{"symbol": s, "source": source} for s in target_symbols],
        )
    if new_securities:
        session.execute(
            insert(Security),
            [{"symbol": s, "is_active": True} for s in new_securities],
        )

    return ReplaceResult(
        total=len(target_symbols),
        added=added,
        removed=removed,
        kept=kept,
        new_securities=new_securities,
    )
