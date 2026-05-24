"""One-off backfill: populate `daily_prices.adj_close` for existing rows.

Why this exists
---------------
`refresh_quotes` only sets `adj_close` on NEW bars (ingest is append-only with
dedup on `trade_date`), so rows ingested before the adj_close fix carry
`adj_close = NULL`. Downstream consumers (signal panel / backtest feed / ledger)
then silently fall back to the UNADJUSTED `close`, corrupting momentum/forward
returns across any split or dividend. This script fetches FMP's split/dividend-
adjusted stream and writes `adj_close` onto the matching existing rows by
`(symbol, trade_date)`.

Safety
------
* Non-destructive: only the `adj_close` column is written. Raw OHLCV is never
  touched and no rows are deleted.
* Idempotent: re-running writes the same authoritative values.
* Per-symbol isolation: one bad ticker is reported and skipped; the rest
  continue. Commit is per symbol, so partial progress survives an interruption.
* The symbol list comes from `daily_prices` itself (not the universe), so
  symbols no longer in the universe (e.g. RKLB) are still covered.

Usage
-----
    uv run python scripts/backfill_adj_close.py             # all symbols
    uv run python scripts/backfill_adj_close.py --dry-run   # preview, no writes
    uv run python scripts/backfill_adj_close.py --symbols AAPL,MSFT
"""

from __future__ import annotations

import argparse
from datetime import date

from sqlalchemy import func, select, update

from quant_researcher.config import settings
from quant_researcher.data.fmp import FMPClient, FMPError
from quant_researcher.data.refresh import _adj_close_by_date
from quant_researcher.db import session_factory
from quant_researcher.models.prices import DailyPrice


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill daily_prices.adj_close from FMP.")
    p.add_argument(
        "--symbols", help="Comma-separated subset (default: all symbols in daily_prices)."
    )
    p.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
    p.add_argument(
        "--since",
        help="ISO date floor for the FMP fetch (default: earliest stored trade_date).",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    cfg = settings()
    Session = session_factory()

    # ORM bulk UPDATE by primary key: a bare `update(Model)` executed with a
    # list of dicts (each carrying the PK columns + the column to set) issues
    # one executemany `UPDATE ... WHERE symbol=? AND trade_date=?`. An explicit
    # `.where()` here is rejected for executemany ("bulk synchronize ... not
    # supported"), so the WHERE is left implicit on the PK.
    update_stmt = update(DailyPrice)

    with Session() as session, FMPClient(api_key=cfg.fmp_api_key or "") as client:
        if args.symbols:
            symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        else:
            symbols = sorted(session.scalars(select(func.distinct(DailyPrice.symbol))))

        since = (
            date.fromisoformat(args.since)
            if args.since
            else session.scalar(select(func.min(DailyPrice.trade_date)))
        )

        print(f"symbols={len(symbols)}  since={since}  dry_run={args.dry_run}")
        total_updated = 0
        failures: list[tuple[str, str]] = []

        for i, sym in enumerate(symbols, 1):
            try:
                adj_rows = client.get_adjusted_prices(sym, since=since)
            except FMPError as exc:
                failures.append((sym, str(exc)))
                print(f"[{i}/{len(symbols)}] {sym}: FAILED {exc}")
                continue

            adj_by_date = _adj_close_by_date(adj_rows)
            existing = set(
                session.scalars(select(DailyPrice.trade_date).where(DailyPrice.symbol == sym))
            )
            params = [
                {"symbol": sym, "trade_date": d, "adj_close": adj}
                for d, adj in adj_by_date.items()
                if d in existing
            ]
            if params and not args.dry_run:
                session.execute(update_stmt, params)
                session.commit()
            total_updated += len(params)
            verb = "would update" if args.dry_run else "updated"
            print(f"[{i}/{len(symbols)}] {sym}: {verb} {len(params)} rows")

        verb = "would update" if args.dry_run else "updated"
        ok_count = len(symbols) - len(failures)
        print(f"\nDONE: {verb} {total_updated} rows across {ok_count}/{len(symbols)} symbols")
        if failures:
            print(f"FAILURES ({len(failures)}):")
            for sym, err in failures:
                print(f"  {sym}: {err}")


if __name__ == "__main__":
    main()
