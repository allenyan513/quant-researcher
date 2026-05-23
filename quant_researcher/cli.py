"""CLI entry point (typer). Per I1: single `qr` binary, stable JSON envelope
on stdout, exit 0 on ok / 1 on error. Subcommands grow per milestone — M0 ships
the `qr db` group only.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import typer
from sqlalchemy import text

from quant_researcher.config import settings
from quant_researcher.contract import Envelope

app = typer.Typer(
    name="qr",
    help="quant-researcher — auxiliary investing-research substrate for Claude Code.",
    no_args_is_help=True,
    add_completion=False,
)

db_app = typer.Typer(
    name="db",
    help="Database utilities (status, init, ping).",
    no_args_is_help=True,
)
app.add_typer(db_app)

universe_app = typer.Typer(
    name="universe",
    help="Watchlist universe (D3) — set from file, list current.",
    no_args_is_help=True,
)
app.add_typer(universe_app)

data_app = typer.Typer(
    name="data",
    help="Warehouse refresh from FMP (profiles, daily prices).",
    no_args_is_help=True,
)
app.add_typer(data_app)

screen_app = typer.Typer(
    name="screen",
    help="Stock screening (MB) — fundamental expressions + technical predicates.",
    no_args_is_help=True,
)
app.add_typer(screen_app)

holdings_app = typer.Typer(
    name="holdings",
    help="Holdings (ME) — sync from IBKR Flex / import CSV / list / history.",
    no_args_is_help=True,
)
app.add_typer(holdings_app)

research_app = typer.Typer(
    name="research",
    help="Research data packages (MD) — bundle aggregator + news refresh.",
    no_args_is_help=True,
)
app.add_typer(research_app)

ledger_app = typer.Typer(
    name="ledger",
    help="Decision ledger (MF) — record / track / scorecard.",
    no_args_is_help=True,
)
app.add_typer(ledger_app)

backtest_app = typer.Typer(
    name="backtest",
    help="Strategy backtests (MH) — run / list / show over warehouse prices.",
    no_args_is_help=True,
)
app.add_typer(backtest_app)

# ---------------------------------------------------------------------------
# qr holdings ...
# ---------------------------------------------------------------------------


@holdings_app.command("sync")
def holdings_sync(
    account_override: str | None = typer.Option(
        None,
        "--account",
        help="Override account_id (defaults to value in Flex statement).",
    ),
    max_attempts: int = typer.Option(
        6,
        "--max-attempts",
        help=(
            "Max poll attempts (applied to both SendRequest transient codes "
            "1001/1004 and GetStatement 1007/1019). Default 6."
        ),
    ),
    poll_delay: float = typer.Option(
        8.0,
        "--poll-delay",
        help=(
            "Seconds between attempts. IBKR sometimes needs minutes between "
            "Flex calls for the same query — bump this to e.g. 30 with "
            "--max-attempts 12 for a ~6-minute patience budget."
        ),
    ),
) -> None:
    """Pull live positions from IBKR Flex (FLEX_TOKEN_KEY + FLEX_QUERY_ID_LIVE)."""
    from quant_researcher.db import session_factory
    from quant_researcher.holdings.ibkr_flex import FlexClient, FlexError
    from quant_researcher.holdings.importer import import_holdings

    cfg = settings()
    if not cfg.flex_token_key:
        _emit(
            Envelope.failure(
                "missing_flex_token", "FLEX_TOKEN_KEY is not set in .env."
            )
        )
    if not cfg.flex_query_id_live:
        _emit(
            Envelope.failure(
                "missing_flex_query_id",
                "FLEX_QUERY_ID_LIVE is not set in .env.",
            )
        )

    try:
        with FlexClient(
            token=cfg.flex_token_key,
            max_poll_attempts=max_attempts,
            poll_delay=poll_delay,
        ) as flex:
            meta, raw_positions = flex.fetch_positions(cfg.flex_query_id_live)
        if account_override:
            for row in raw_positions:
                row["accountId"] = account_override
        with session_factory()() as sess, sess.begin():
            result = import_holdings(sess, source="flex", payload=raw_positions)
    except FlexError as exc:
        _emit(Envelope.failure("flex_fetch_failed", str(exc)))
    except Exception as exc:
        _emit(Envelope.failure("holdings_sync_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={
                    "source": "flex",
                    "account_id": result.account_id,
                    "as_of_date": (
                        result.as_of_date.isoformat() if result.as_of_date else None
                    ),
                    "imported": result.imported,
                    "symbols": result.symbols,
                    "skipped": result.skipped,
                    "statement": {
                        "query_name": meta.query_name,
                        "from_date": meta.from_date,
                        "to_date": meta.to_date,
                        "when_generated": meta.when_generated,
                    },
                },
                data_freshness={"flex": "live"},
            )
        )


@holdings_app.command("import-csv")
def holdings_import_csv(
    file: Path = typer.Option(
        ..., "--file", "-f", help="CSV file with required columns: "
        "account_id, symbol, quantity, as_of_date."
    ),
    account_override: str | None = typer.Option(
        None,
        "--account",
        help="Use this account_id when CSV rows don't have one.",
    ),
    as_of_override: str | None = typer.Option(
        None,
        "--as-of",
        help="Override as_of_date (YYYY-MM-DD) when CSV rows don't have one.",
    ),
) -> None:
    """Import holdings from a CSV file (file → DB upsert)."""
    from datetime import date

    from quant_researcher.db import session_factory
    from quant_researcher.holdings.csv import CSVError, parse_holdings_csv
    from quant_researcher.holdings.importer import import_holdings

    if not file.exists():
        _emit(Envelope.failure("csv_file_missing", f"no such file: {file}"))

    as_of_date: date | None = None
    if as_of_override:
        try:
            as_of_date = date.fromisoformat(as_of_override)
        except ValueError:
            _emit(
                Envelope.failure(
                    "invalid_as_of",
                    f"--as-of must be YYYY-MM-DD, got {as_of_override!r}",
                )
            )

    try:
        rows = parse_holdings_csv(file)
    except CSVError as exc:
        _emit(Envelope.failure("csv_parse_failed", str(exc)))

    try:
        with session_factory()() as sess, sess.begin():
            result = import_holdings(
                sess,
                source="csv",
                payload=rows,
                account_id_override=account_override,
                as_of_date_override=as_of_date,
            )
    except ValueError as exc:
        _emit(Envelope.failure("holdings_import_failed", str(exc)))
    except Exception as exc:
        _emit(Envelope.failure("holdings_import_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={
                    "source": "csv",
                    "file": str(file),
                    "account_id": result.account_id,
                    "as_of_date": (
                        result.as_of_date.isoformat() if result.as_of_date else None
                    ),
                    "imported": result.imported,
                    "symbols": result.symbols,
                    "skipped": result.skipped,
                },
                data_freshness={"csv": "live"},
            )
        )


@holdings_app.command("list")
def holdings_list(
    account: str | None = typer.Option(
        None, "--account", "-a", help="Filter to a specific account_id."
    ),
    as_of: str = typer.Option(
        "latest",
        "--as-of",
        help=(
            "`latest` (default) or YYYY-MM-DD. `latest` picks the most-recent"
            " date per (account, symbol)."
        ),
    ),
) -> None:
    """List current holdings (defaults to latest snapshot per account/symbol)."""
    from datetime import date as _date

    from sqlalchemy import func, select

    from quant_researcher.db import session_factory
    from quant_researcher.models.holdings import Holding

    target_date: _date | None = None
    if as_of != "latest":
        try:
            target_date = _date.fromisoformat(as_of)
        except ValueError:
            _emit(
                Envelope.failure(
                    "invalid_as_of",
                    f"--as-of must be 'latest' or YYYY-MM-DD, got {as_of!r}",
                )
            )

    try:
        with session_factory()() as sess:
            if target_date is not None:
                stmt = select(Holding).where(Holding.as_of_date == target_date)
            else:
                # Latest per (account, symbol) via correlated subquery.
                sub = (
                    select(
                        Holding.account_id,
                        Holding.symbol,
                        func.max(Holding.as_of_date).label("max_date"),
                    )
                    .group_by(Holding.account_id, Holding.symbol)
                    .subquery()
                )
                stmt = select(Holding).join(
                    sub,
                    (Holding.account_id == sub.c.account_id)
                    & (Holding.symbol == sub.c.symbol)
                    & (Holding.as_of_date == sub.c.max_date),
                )
            if account:
                stmt = stmt.where(Holding.account_id == account)
            rows = list(sess.scalars(stmt.order_by(Holding.account_id, Holding.symbol)))
            items = [_holding_to_dict(h) for h in rows]
    except Exception as exc:
        _emit(Envelope.failure("holdings_list_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={
                    "count": len(items),
                    "filter": {"account": account, "as_of": as_of},
                    "holdings": items,
                    "total_market_value": _sum_floats(
                        items, "market_value"
                    ),
                },
                data_freshness={"db": "live"},
            )
        )


@holdings_app.command("history")
def holdings_history(
    symbol: str = typer.Option(..., "--symbol", "-s", help="Symbol to trace."),
    account: str | None = typer.Option(
        None, "--account", "-a", help="Optional account filter."
    ),
    limit: int = typer.Option(
        30, "--limit", "-l", help="Most-recent N rows (default 30)."
    ),
) -> None:
    """Show snapshot history for one symbol (sorted newest first)."""
    from sqlalchemy import select

    from quant_researcher.db import session_factory
    from quant_researcher.models.holdings import Holding

    try:
        with session_factory()() as sess:
            stmt = select(Holding).where(Holding.symbol == symbol)
            if account:
                stmt = stmt.where(Holding.account_id == account)
            stmt = stmt.order_by(Holding.as_of_date.desc()).limit(limit)
            rows = list(sess.scalars(stmt))
            items = [_holding_to_dict(h) for h in rows]
    except Exception as exc:
        _emit(Envelope.failure("holdings_history_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={
                    "symbol": symbol,
                    "account": account,
                    "count": len(items),
                    "history": items,
                },
                data_freshness={"db": "live"},
            )
        )


def _holding_to_dict(h: Any) -> dict[str, Any]:
    return {
        "account_id": h.account_id,
        "symbol": h.symbol,
        "as_of_date": h.as_of_date.isoformat() if h.as_of_date else None,
        "asset_category": h.asset_category,
        "sub_category": h.sub_category,
        "quantity": h.quantity,
        "mark_price": h.mark_price,
        "market_value": h.market_value,
        "avg_cost": h.avg_cost,
        "cost_basis_total": h.cost_basis_total,
        "unrealized_pnl": h.unrealized_pnl,
        "percent_of_nav": h.percent_of_nav,
        "side": h.side,
        "currency": h.currency,
        "source": h.source,
    }


def _sum_floats(items: list[dict], key: str) -> float | None:
    vals = [i.get(key) for i in items if i.get(key) is not None]
    if not vals:
        return None
    return sum(vals)


# ---------------------------------------------------------------------------
# qr research ...
# ---------------------------------------------------------------------------


@research_app.command("bundle")
def research_bundle(
    symbol: str = typer.Argument(..., help="Ticker to research (e.g. AAPL)."),
    save: bool = typer.Option(
        True, "--save/--no-save", help="Persist the bundle to research_bundles."
    ),
    news_limit: int = typer.Option(
        10, "--news-limit", help="How many recent news headlines to include."
    ),
) -> None:
    """Aggregate everything the warehouse knows about `symbol` into a JSON bundle.

    Pulls profile + financials (income/balance/cashflow) + ratios + forward
    estimates + valuation snapshots + holdings + recent news → single dict.
    Missing data falls to None/[] gracefully. Returns `bundle_id` if saved.
    """
    from quant_researcher.db import session_factory
    from quant_researcher.research.bundler import bundle

    try:
        with session_factory()() as sess, sess.begin():
            bundle_id, payload = bundle(sess, symbol.upper(), news_limit=news_limit, save=save)
    except Exception as exc:
        _emit(Envelope.failure("research_bundle_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={
                    "bundle_id": bundle_id,
                    "saved": save,
                    "payload": payload,
                },
                data_freshness={"warehouse": "live"},
            )
        )


@research_app.command("news")
def research_news_cmd(
    symbols: str = typer.Option(
        ...,
        "--symbols",
        help="Comma-separated tickers to fetch news for, e.g. 'AAPL,MSFT,NVDA'.",
    ),
    limit: int = typer.Option(
        50, "--limit", "-l", help="FMP /news/stock-latest limit."
    ),
) -> None:
    """Fetch + dedupe recent news from FMP into `news_items`."""
    from quant_researcher.data.fmp import FMPClient
    from quant_researcher.db import session_factory
    from quant_researcher.research.refresh import refresh_news

    cfg = settings()
    if not cfg.fmp_api_key:
        _emit(
            Envelope.failure(
                "missing_fmp_api_key", "FMP_API_KEY is not set in .env."
            )
        )
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not syms:
        _emit(Envelope.failure("no_symbols", "--symbols cannot be empty."))

    try:
        with (
            session_factory()() as sess,
            sess.begin(),
            FMPClient(api_key=cfg.fmp_api_key) as client,
        ):
            result = refresh_news(sess, client, syms, limit=limit)
    except Exception as exc:
        _emit(Envelope.failure("news_refresh_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={
                    "symbols_requested": syms,
                    "fetched": result.fetched,
                    "inserted": result.inserted,
                    "skipped_duplicate": result.skipped_duplicate,
                    "failed": result.failed,
                },
                data_freshness={"fmp": "live"},
            )
        )


@research_app.command("list")
def research_list(
    symbol: str | None = typer.Option(
        None, "--symbol", "-s", help="Filter to a specific ticker."
    ),
    limit: int = typer.Option(
        20, "--limit", "-l", help="Newest-first row cap (default 20)."
    ),
) -> None:
    """List past bundles (newest first)."""
    from sqlalchemy import select

    from quant_researcher.db import session_factory
    from quant_researcher.models.research import ResearchBundle

    try:
        with session_factory()() as sess:
            stmt = select(ResearchBundle).order_by(ResearchBundle.created_at.desc()).limit(limit)
            if symbol:
                stmt = stmt.where(ResearchBundle.symbol == symbol.upper())
            rows = list(sess.scalars(stmt))
            items = [
                {
                    "bundle_id": r.bundle_id,
                    "symbol": r.symbol,
                    "as_of": r.as_of.isoformat() if r.as_of else None,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "code_version": r.code_version,
                }
                for r in rows
            ]
    except Exception as exc:
        _emit(Envelope.failure("research_list_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={"count": len(items), "bundles": items},
                data_freshness={"db": "live"},
            )
        )


@research_app.command("show")
def research_show(
    bundle_id: str = typer.Argument(..., help="Bundle UUID from `qr research list`."),
) -> None:
    """Show a saved bundle by id."""
    from quant_researcher.db import session_factory
    from quant_researcher.models.research import ResearchBundle

    try:
        with session_factory()() as sess:
            row = sess.get(ResearchBundle, bundle_id)
    except Exception as exc:
        _emit(Envelope.failure("research_show_failed", str(exc)))
    else:
        if row is None:
            _emit(
                Envelope.failure(
                    "bundle_not_found", f"no bundle with id={bundle_id!r}"
                )
            )
        _emit(
            Envelope.success(
                data={
                    "bundle_id": row.bundle_id,
                    "symbol": row.symbol,
                    "as_of": row.as_of.isoformat() if row.as_of else None,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "code_version": row.code_version,
                    "payload": row.payload,
                },
                data_freshness={"db": "live"},
            )
        )


# ---------------------------------------------------------------------------
# qr ledger ...
# ---------------------------------------------------------------------------


@ledger_app.command("add")
def ledger_add(
    symbol: str = typer.Argument(..., help="Ticker the decision is about."),
    side: str = typer.Option(..., "--side", help="`buy` or `sell`."),
    thesis: str | None = typer.Option(
        None, "--thesis", help="Free-text thesis / reason for the decision."
    ),
    confidence: int | None = typer.Option(
        None, "--confidence", "-c", help="1–5 (or any int) confidence score."
    ),
    tags: str | None = typer.Option(
        None,
        "--tags",
        help="Comma-separated tags, e.g. 'AI,growth,catalyst-q4'.",
    ),
    opened: str | None = typer.Option(
        None,
        "--opened",
        help="Override decision date (YYYY-MM-DD). Defaults to today.",
    ),
    no_bundle: bool = typer.Option(
        False,
        "--no-bundle",
        help="Skip auto-snapshotting research_bundle (faster, less reproducible).",
    ),
) -> None:
    """Record a buy/sell decision with thesis + confidence + auto-snapshot."""
    from datetime import date as _date

    from quant_researcher.db import session_factory
    from quant_researcher.ledger.engine import record_decision

    parsed_opened: _date | None = None
    if opened:
        try:
            parsed_opened = _date.fromisoformat(opened)
        except ValueError:
            _emit(
                Envelope.failure(
                    "invalid_opened",
                    f"--opened must be YYYY-MM-DD, got {opened!r}",
                )
            )

    tag_list = (
        [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    )

    try:
        with session_factory()() as sess, sess.begin():
            result = record_decision(
                sess,
                symbol=symbol.upper(),
                side=side,
                thesis=thesis,
                confidence=confidence,
                tags=tag_list,
                opened_at=parsed_opened,
                auto_bundle=not no_bundle,
            )
    except ValueError as exc:
        _emit(Envelope.failure("invalid_decision", str(exc)))
    except Exception as exc:
        _emit(Envelope.failure("ledger_add_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={
                    "decision_id": result.decision_id,
                    "bundle_id": result.bundle_id,
                    "symbol": symbol.upper(),
                    "side": side,
                    "price_at_open": result.price_at_open,
                    "sector_at_open": result.sector_at_open,
                    "confidence": confidence,
                    "tags": tag_list,
                },
                data_freshness={"warehouse": "live"},
            )
        )


@ledger_app.command("track")
def ledger_track(
    as_of: str | None = typer.Option(
        None,
        "--as-of",
        help="Override 'today' (YYYY-MM-DD) when computing horizons.",
    ),
    decision_id: str | None = typer.Option(
        None, "--decision-id", help="Limit tracking to one decision."
    ),
) -> None:
    """Refresh 1w/1m/3m/6m forward returns for every Decision row."""
    from datetime import date as _date

    from quant_researcher.db import session_factory
    from quant_researcher.ledger.engine import track_decisions

    parsed_as_of: _date | None = None
    if as_of:
        try:
            parsed_as_of = _date.fromisoformat(as_of)
        except ValueError:
            _emit(
                Envelope.failure(
                    "invalid_as_of",
                    f"--as-of must be YYYY-MM-DD, got {as_of!r}",
                )
            )

    try:
        with session_factory()() as sess, sess.begin():
            result = track_decisions(
                sess,
                as_of=parsed_as_of,
                decision_ids=[decision_id] if decision_id else None,
            )
    except Exception as exc:
        _emit(Envelope.failure("ledger_track_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={
                    "decisions_touched": result.decisions_touched,
                    "rows_written": result.rows_written,
                    "rows_skipped_horizon_not_elapsed": result.rows_skipped_horizon_not_elapsed,
                },
                data_freshness={"db": "live"},
            )
        )


@ledger_app.command("list")
def ledger_list(
    symbol: str | None = typer.Option(
        None, "--symbol", "-s", help="Filter to a ticker."
    ),
    side: str | None = typer.Option(
        None, "--side", help="`buy` or `sell` filter."
    ),
    limit: int = typer.Option(50, "--limit", "-l", help="Newest-first row cap."),
) -> None:
    """List recorded decisions (newest first)."""
    from sqlalchemy import select

    from quant_researcher.db import session_factory
    from quant_researcher.models.decisions import Decision

    try:
        with session_factory()() as sess:
            stmt = select(Decision).order_by(Decision.created_at.desc()).limit(limit)
            if symbol:
                stmt = stmt.where(Decision.symbol == symbol.upper())
            if side:
                stmt = stmt.where(Decision.side == side)
            rows = list(sess.scalars(stmt))
            items = [
                {
                    "decision_id": d.decision_id,
                    "symbol": d.symbol,
                    "side": d.side,
                    "opened_at": d.opened_at.isoformat() if d.opened_at else None,
                    "price_at_open": d.price_at_open,
                    "confidence": d.confidence,
                    "tags": d.tags,
                    "sector_at_open": d.sector_at_open,
                    "thesis": d.thesis,
                    "bundle_id": d.bundle_id,
                }
                for d in rows
            ]
    except Exception as exc:
        _emit(Envelope.failure("ledger_list_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={"count": len(items), "decisions": items},
                data_freshness={"db": "live"},
            )
        )


@ledger_app.command("scorecard")
def ledger_scorecard(
    group_by: str = typer.Option(
        "confidence",
        "--group-by",
        "-g",
        help="One of: confidence, sector, tag.",
    ),
    horizon: str = typer.Option(
        "1m", "--horizon", help="One of: 1w, 1m, 3m, 6m."
    ),
) -> None:
    """Aggregate decisions by dimension + show avg alpha / return."""
    from quant_researcher.db import session_factory
    from quant_researcher.ledger.engine import scorecard

    try:
        with session_factory()() as sess:
            rows = scorecard(sess, group_by=group_by, horizon=horizon)
    except ValueError as exc:
        _emit(Envelope.failure("invalid_scorecard_param", str(exc)))
    except Exception as exc:
        _emit(Envelope.failure("ledger_scorecard_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={
                    "group_by": group_by,
                    "horizon": horizon,
                    "rows": rows,
                },
                data_freshness={"db": "live"},
            )
        )


@ledger_app.command("show")
def ledger_show(
    decision_id: str = typer.Argument(..., help="Decision UUID."),
) -> None:
    """Show one decision + its tracking rows."""
    from sqlalchemy import select

    from quant_researcher.db import session_factory
    from quant_researcher.models.decisions import Decision, DecisionTracking

    try:
        with session_factory()() as sess:
            d = sess.get(Decision, decision_id)
            if d is None:
                _emit(
                    Envelope.failure(
                        "decision_not_found",
                        f"no decision with id={decision_id!r}",
                    )
                )
            tracking = list(
                sess.scalars(
                    select(DecisionTracking)
                    .where(DecisionTracking.decision_id == decision_id)
                    .order_by(DecisionTracking.horizon)
                )
            )
    except Exception as exc:
        _emit(Envelope.failure("ledger_show_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={
                    "decision_id": d.decision_id,
                    "symbol": d.symbol,
                    "side": d.side,
                    "opened_at": d.opened_at.isoformat(),
                    "price_at_open": d.price_at_open,
                    "confidence": d.confidence,
                    "tags": d.tags,
                    "sector_at_open": d.sector_at_open,
                    "thesis": d.thesis,
                    "bundle_id": d.bundle_id,
                    "tracking": [
                        {
                            "horizon": t.horizon,
                            "tracked_at": t.tracked_at.isoformat() if t.tracked_at else None,
                            "price": t.price,
                            "return_pct": t.return_pct,
                            "spy_return_pct": t.spy_return_pct,
                            "sector_etf": t.sector_etf,
                            "sector_return_pct": t.sector_return_pct,
                            "alpha_pct": t.alpha_pct,
                        }
                        for t in tracking
                    ],
                },
                data_freshness={"db": "live"},
            )
        )


# `qr value` is a top-level command (not a subgroup) — implementation-plan.md
# §5 specifies `qr value AAPL [--model X]`.


# ---------------------------------------------------------------------------
# qr backtest ...
# ---------------------------------------------------------------------------


def _coerce_param(v: str) -> Any:
    """Coerce a --params string value to int / float / bool / str (in order)."""
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    return v


def _parse_kv_params(raw: str | None) -> dict[str, Any]:
    """Parse 'fast_period=10,slow_period=30' into a kwargs dict."""
    out: dict[str, Any] = {}
    if not raw:
        return out
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(f"bad --params entry {pair!r} (expected key=value)")
        key, val = pair.split("=", 1)
        out[key.strip()] = _coerce_param(val.strip())
    return out


@backtest_app.command("run")
def backtest_run(
    symbols: str = typer.Option(
        ..., "--symbols", help="Comma-separated tickers to trade (e.g. 'AAPL')."
    ),
    start: str = typer.Option(..., "--start", help="Backtest start (YYYY-MM-DD)."),
    end: str = typer.Option(..., "--end", help="Backtest end (YYYY-MM-DD)."),
    strategy: str | None = typer.Option(
        None, "--strategy", help="Built-in strategy name (e.g. sma_crossover)."
    ),
    strategy_file: str | None = typer.Option(
        None,
        "--strategy-file",
        help="Path to a .py defining a BaseStrategy subclass (overrides --strategy).",
    ),
    strategy_class: str | None = typer.Option(
        None,
        "--strategy-class",
        help="Class to pick when --strategy-file defines several subclasses.",
    ),
    params: str | None = typer.Option(
        None,
        "--params",
        help="Strategy kwargs, e.g. 'fast_period=10,slow_period=30,size=100'.",
    ),
    cash: float = typer.Option(100_000.0, "--cash", help="Initial cash."),
    fee: str = typer.Option(
        "per-share", "--fee", help="Fee model: zero, per-share, percentage."
    ),
    slippage: float = typer.Option(
        0.0005, "--slippage", help="Slippage rate (fraction, e.g. 0.0005)."
    ),
    benchmark: str | None = typer.Option(
        None,
        "--benchmark",
        help="Benchmark symbol read from warehouse for alpha/beta (e.g. SPY).",
    ),
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Use raw close instead of split/dividend-adjusted (default adjusted).",
    ),
    risk_free: float = typer.Option(
        0.0, "--risk-free", help="Annual risk-free rate for Sharpe / Sortino."
    ),
) -> None:
    """Run a strategy backtest over warehouse prices; persist + summarize."""
    from datetime import date as _date

    from quant_researcher.backtest.runner import run_backtest
    from quant_researcher.db import session_factory

    # --- validation (must stay OUTSIDE the try; _emit raises typer.Exit) ---
    if not strategy and not strategy_file:
        _emit(
            Envelope.failure(
                "missing_strategy", "provide --strategy or --strategy-file"
            )
        )
    target_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not target_list:
        _emit(Envelope.failure("missing_symbols", "--symbols is empty"))
    for label, value in (("--start", start), ("--end", end)):
        try:
            _date.fromisoformat(value)
        except ValueError:
            _emit(
                Envelope.failure(
                    "invalid_date", f"{label} must be YYYY-MM-DD, got {value!r}"
                )
            )
    try:
        param_dict = _parse_kv_params(params)
    except ValueError as exc:
        _emit(Envelope.failure("invalid_params", str(exc)))

    bench = benchmark.strip().upper() if benchmark else None

    try:
        with session_factory()() as sess, sess.begin():
            summary = run_backtest(
                sess,
                strategy=strategy or "",
                symbols=target_list,
                start=start,
                end=end,
                initial_cash=cash,
                params=param_dict,
                fee=fee,
                slippage_rate=slippage,
                benchmark_symbol=bench,
                adjusted=not raw,
                strategy_file=strategy_file,
                strategy_class=strategy_class,
                risk_free_rate=risk_free,
            )
    except KeyError as exc:
        _emit(
            Envelope.failure(
                "invalid_backtest_spec", exc.args[0] if exc.args else str(exc)
            )
        )
    except (ValueError, FileNotFoundError) as exc:
        _emit(Envelope.failure("invalid_backtest_spec", str(exc)))
    except Exception as exc:
        _emit(Envelope.failure("backtest_run_failed", str(exc)))
    else:
        _emit(Envelope.success(data=summary, data_freshness={"warehouse": "live"}))


@backtest_app.command("list")
def backtest_list(
    strategy: str | None = typer.Option(
        None, "--strategy", "-s", help="Filter by strategy name."
    ),
    limit: int = typer.Option(50, "--limit", "-l", help="Newest-first row cap."),
) -> None:
    """List backtest runs (newest first) with headline metrics."""
    from sqlalchemy import select

    from quant_researcher.db import session_factory
    from quant_researcher.models.backtest import BacktestRun

    try:
        with session_factory()() as sess:
            stmt = (
                select(BacktestRun)
                .order_by(BacktestRun.created_at.desc())
                .limit(limit)
            )
            if strategy:
                stmt = stmt.where(BacktestRun.strategy == strategy)
            rows = list(sess.scalars(stmt))
            items = [
                {
                    "run_id": r.run_id,
                    "strategy": r.strategy,
                    "symbols": r.symbols,
                    "start": r.start.isoformat() if r.start else None,
                    "end": r.end.isoformat() if r.end else None,
                    "benchmark_symbol": r.benchmark_symbol,
                    "total_return": (r.metrics or {}).get("total_return"),
                    "sharpe_ratio": (r.metrics or {}).get("sharpe_ratio"),
                    "max_drawdown": (r.metrics or {}).get("max_drawdown"),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
    except Exception as exc:
        _emit(Envelope.failure("backtest_list_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={"count": len(items), "runs": items},
                data_freshness={"db": "live"},
            )
        )


@backtest_app.command("show")
def backtest_show(
    run_id: str = typer.Argument(..., help="Backtest run UUID."),
) -> None:
    """Show one backtest run in full (metrics + equity curve + trades)."""
    from quant_researcher.db import session_factory
    from quant_researcher.models.backtest import BacktestRun

    try:
        with session_factory()() as sess:
            r = sess.get(BacktestRun, run_id)
            data = None
            if r is not None:
                data = {
                    "run_id": r.run_id,
                    "strategy": r.strategy,
                    "symbols": r.symbols,
                    "start": r.start.isoformat() if r.start else None,
                    "end": r.end.isoformat() if r.end else None,
                    "initial_cash": r.initial_cash,
                    "benchmark_symbol": r.benchmark_symbol,
                    "params": r.params,
                    "config": r.config,
                    "metrics": r.metrics,
                    "equity_curve": r.equity_curve,
                    "trade_log": r.trade_log,
                    "code_version": r.code_version,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
    except Exception as exc:
        _emit(Envelope.failure("backtest_show_failed", str(exc)))
    else:
        # None-check + emit stay OUTSIDE the try (CLAUDE.md §2: _emit raises
        # typer.Exit, which `except Exception` would otherwise double-emit).
        if data is None:
            _emit(Envelope.failure("not_found", f"no backtest run {run_id!r}"))
        _emit(Envelope.success(data=data, data_freshness={"db": "live"}))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit(env: Envelope) -> None:
    """Emit a JSON envelope to stdout and exit (0 on ok, 1 on error)."""
    typer.echo(env.to_json())
    raise typer.Exit(code=0 if env.ok else 1)


def _mask_dsn(dsn: str) -> str:
    """Hide password in a DSN for display.

    `postgresql+psycopg://user:pass@host:5432/db` → `postgresql+psycopg://user:***@host:5432/db`
    """
    if "://" not in dsn or "@" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:***@{host}"
    return dsn


# ---------------------------------------------------------------------------
# qr db ...
# ---------------------------------------------------------------------------


@db_app.command("ping")
def db_ping() -> None:
    """`SELECT 1` round-trip + latency. Useful as a managed-Postgres keepalive
    (Neon scale-to-zero / Supabase free-tier pause)."""
    # Import lazily so `--help` doesn't require QR_DATABASE_URL to be set.
    from quant_researcher.db import engine

    started = time.perf_counter()
    try:
        with engine().connect() as conn:
            result = conn.execute(text("SELECT 1")).scalar()
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
    except Exception as exc:  # broad on purpose — surface as structured error
        _emit(Envelope.failure("db_ping_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={"select_1": result, "latency_ms": latency_ms},
                data_freshness={"db": "live"},
            )
        )


@db_app.command("status")
def db_status() -> None:
    """DB connectivity, server version, and the set of project tables present."""
    from sqlalchemy import inspect

    from quant_researcher.db import Base, engine

    try:
        eng = engine()
        with eng.connect() as conn:
            server_version = conn.execute(text("SHOW server_version")).scalar()
        inspector = inspect(eng)
        existing = set(inspector.get_table_names())
        expected = set(Base.metadata.tables.keys())
    except Exception as exc:
        _emit(Envelope.failure("db_status_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={
                    "dsn": _mask_dsn(settings().qr_database_url),
                    "server_version": server_version,
                    "expected_tables": sorted(expected),
                    "present_tables": sorted(expected & existing),
                    "missing_tables": sorted(expected - existing),
                },
                data_freshness={"db": "live"},
            )
        )


@db_app.command("init")
def db_init() -> None:
    """Create any tables defined in SQLAlchemy models that don't yet exist in Supabase.

    Idempotent: uses `Base.metadata.create_all(checkfirst=True)`. Does NOT alter
    existing tables — for column/type changes, edit them via the Supabase
    dashboard or hand-written SQL (D11: no Alembic).
    """
    from sqlalchemy import inspect

    from quant_researcher.db import Base, engine

    try:
        eng = engine()
        before = set(inspect(eng).get_table_names())
        Base.metadata.create_all(eng, checkfirst=True)
        after = set(inspect(eng).get_table_names())
        created = sorted(after - before)
    except Exception as exc:
        _emit(Envelope.failure("db_init_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={
                    "created_tables": created,
                    "total_project_tables": sorted(Base.metadata.tables.keys()),
                },
                data_freshness={"db": "live"},
            )
        )


# ---------------------------------------------------------------------------
# qr universe ...
# ---------------------------------------------------------------------------


@universe_app.command("set")
def universe_set(
    file: Path = typer.Option(..., "--file", "-f", help="Watchlist file (one ticker per line)."),
    source: str | None = typer.Option(
        None, "--source", "-s", help="Label stored on each row (defaults to the file stem)."
    ),
) -> None:
    """Replace the universe with the symbols from `--file` (transactional).

    Blank lines and `#` comments are ignored; tickers are upper-cased and
    de-duplicated. Also upserts a `securities` master row per new ticker.
    """
    from quant_researcher.db import session_factory
    from quant_researcher.universe import parse_watchlist_file, replace_universe

    # Pre-flight validation: each `_emit` raises `typer.Exit` (an Exception
    # subclass), so we keep these checks OUT of the try block — otherwise the
    # `except Exception` below would catch the Exit and emit a second envelope.
    if not file.exists():
        _emit(Envelope.failure("universe_file_missing", f"no such file: {file}"))
    symbols = parse_watchlist_file(file)
    if not symbols:
        _emit(Envelope.failure("universe_file_empty", f"no symbols parsed from {file}"))
    label = source or file.stem
    try:
        with session_factory()() as sess, sess.begin():
            result = replace_universe(sess, symbols, source=label)
    except Exception as exc:
        _emit(Envelope.failure("universe_set_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={
                    "source": label,
                    "file": str(file),
                    "total": result.total,
                    "added": result.added,
                    "removed": result.removed,
                    "kept_count": len(result.kept),
                    "new_securities": result.new_securities,
                },
                data_freshness={"universe": "live"},
            )
        )


@universe_app.command("list")
def universe_list(
    limit: int | None = typer.Option(None, "--limit", "-n", help="Cap rows returned."),
) -> None:
    """Print the current universe (symbol + source + added_at)."""
    from quant_researcher.db import session_factory
    from quant_researcher.universe import list_universe

    try:
        with session_factory()() as sess:
            rows = list_universe(sess, limit=limit)
            members = [
                {"symbol": m.symbol, "source": m.source, "added_at": m.added_at.isoformat()}
                for m in rows
            ]
    except Exception as exc:
        _emit(Envelope.failure("universe_list_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={"count": len(members), "members": members},
                data_freshness={"universe": "live"},
            )
        )


# ---------------------------------------------------------------------------
# qr data ...
# ---------------------------------------------------------------------------


_VALID_SCOPES = ("profile", "quote", "financials", "ratios", "estimates", "all")


@data_app.command("refresh")
def data_refresh(
    scope: str = typer.Option(
        "all", "--scope", "-s", help=f"One of: {', '.join(_VALID_SCOPES)}."
    ),
    symbols: str | None = typer.Option(
        None,
        "--symbols",
        help="Comma-separated subset of the universe (default: all universe rows).",
    ),
    lookback_days: int = typer.Option(
        730,
        "--lookback-days",
        help="Initial OHLCV window for tickers with no prior data (default 2y).",
    ),
    periods: str = typer.Option(
        "annual,quarter",
        "--periods",
        help=(
            "Comma-separated periods for financials / ratios / estimates "
            "(default: annual,quarter). Drop 'quarter' if your FMP plan doesn't "
            "include the quarterly variant of those endpoints."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Refresh every requested symbol regardless of freshness. Default "
            "(MA-4) is only-stale — fresh rows skip the FMP call. Per-scope "
            "thresholds: profile=30d, quote=3d, financials=100d, ratios=100d, "
            "estimates=7d."
        ),
    ),
) -> None:
    """Refresh FMP-sourced warehouse tables for the active universe.

    `--scope profile`: replace `profiles` rows from FMP `/profile`.
    `--scope quote`: append new OHLCV bars to `daily_prices`.
    `--scope financials`: ingest income / balance sheet / cash flow (annual +
    quarterly, `known_at = acceptedDate` per D6).
    `--scope ratios`: `/ratios` rows per period (`known_at = now`).
    `--scope estimates`: forward analyst consensus (`session.merge` revises).
    `--scope all`: runs every scope in the order above.

    **MA-4 breaking change**: default is now only-stale. Each scope's
    response includes `skipped_fresh: [...]` listing symbols that already
    had fresh enough data. Pass `--force` for the pre-MA-4 "refresh
    everything" behavior.
    """
    from sqlalchemy import select

    from quant_researcher.data.fmp import FMPClient
    from quant_researcher.data.freshness import stale_symbols
    from quant_researcher.data.refresh import (
        refresh_estimates,
        refresh_financials,
        refresh_profile,
        refresh_quotes,
        refresh_ratios,
    )
    from quant_researcher.db import session_factory
    from quant_researcher.models.universe import UniverseMember

    # Pre-flight (kept outside try — `_emit` raises typer.Exit).
    if scope not in _VALID_SCOPES:
        _emit(
            Envelope.failure(
                "invalid_scope", f"--scope must be one of {_VALID_SCOPES}, got {scope!r}"
            )
        )
    cfg = settings()
    if not cfg.fmp_api_key:
        _emit(
            Envelope.failure(
                "missing_fmp_api_key", "FMP_API_KEY is not set (configure it in .env)."
            )
        )
    parsed_periods = tuple(p.strip() for p in periods.split(",") if p.strip())
    if not parsed_periods or any(p not in ("annual", "quarter") for p in parsed_periods):
        _emit(
            Envelope.failure(
                "invalid_periods",
                f"--periods must be a comma list of 'annual'|'quarter', got {periods!r}",
            )
        )

    # Phase 1 (read-only): resolve target symbols. Connection is pooled so the
    # extra open is cheap, and keeping the emit outside any try block avoids
    # the `_emit`-inside-try double-envelope trap.
    with session_factory()() as sess:
        if symbols:
            targets = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        else:
            targets = sorted(sess.scalars(select(UniverseMember.symbol)))
    if not targets:
        _emit(
            Envelope.failure(
                "empty_universe",
                "no symbols to refresh — run `qr universe set --file …` first.",
            )
        )

    # Phase 2 (write): refresh inside a single transaction.
    def _resolve(sess, scope_name: str) -> tuple[list[str], list[str]]:
        """Return (effective_targets, skipped_fresh). `force` bypasses the filter."""
        if force:
            return targets, []
        stale = stale_symbols(sess, scope_name, targets)
        return stale, sorted(set(targets) - set(stale))

    try:
        scopes_out: dict[str, dict] = {}
        with (
            session_factory()() as sess,
            sess.begin(),
            FMPClient(api_key=cfg.fmp_api_key) as client,
        ):
            if scope in ("profile", "all"):
                effective, skipped = _resolve(sess, "profile")
                r = refresh_profile(sess, client, effective, only_stale=False)
                scopes_out["profile"] = {
                    "succeeded_count": len(r.succeeded),
                    "failed": r.failed,
                    "total_upserted": r.total_upserted,
                    "skipped_fresh": skipped,
                }
            if scope in ("quote", "all"):
                effective, skipped = _resolve(sess, "quote")
                r = refresh_quotes(
                    sess, client, effective, lookback_days=lookback_days, only_stale=False
                )
                scopes_out["quote"] = {
                    "succeeded_count": len(r.succeeded),
                    "failed": r.failed,
                    "total_upserted": r.total_upserted,
                    "total_skipped": r.total_skipped,
                    "skipped_fresh": skipped,
                }
            if scope in ("financials", "all"):
                effective, skipped = _resolve(sess, "financials")
                r = refresh_financials(
                    sess, client, effective, periods=parsed_periods, only_stale=False
                )
                scopes_out["financials"] = {
                    "succeeded_count": len(r.succeeded),
                    "failed": r.failed,
                    "total_upserted": r.total_upserted,
                    "total_skipped": r.total_skipped,
                    "skipped_fresh": skipped,
                }
            if scope in ("ratios", "all"):
                effective, skipped = _resolve(sess, "ratios")
                r = refresh_ratios(
                    sess, client, effective, periods=parsed_periods, only_stale=False
                )
                scopes_out["ratios"] = {
                    "succeeded_count": len(r.succeeded),
                    "failed": r.failed,
                    "total_upserted": r.total_upserted,
                    "total_skipped": r.total_skipped,
                    "skipped_fresh": skipped,
                }
            if scope in ("estimates", "all"):
                effective, skipped = _resolve(sess, "estimates")
                r = refresh_estimates(
                    sess, client, effective, periods=parsed_periods, only_stale=False
                )
                scopes_out["estimates"] = {
                    "succeeded_count": len(r.succeeded),
                    "failed": r.failed,
                    "total_upserted": r.total_upserted,
                    "total_skipped": r.total_skipped,
                    "skipped_fresh": skipped,
                }
    except Exception as exc:
        _emit(Envelope.failure("data_refresh_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={
                    "scope": scope,
                    "force": force,
                    "periods": list(parsed_periods),
                    "universe_size": len(targets),
                    "symbols_processed": targets,
                    "scopes": scopes_out,
                },
                data_freshness={"fmp": "live"},
            )
        )


@data_app.command("freshness")
def data_freshness(
    scope: str = typer.Option(
        "all",
        "--scope",
        "-s",
        help=(
            "Restrict the report to one scope. One of: profile, quote, "
            "financials, ratios, estimates, all (default)."
        ),
    ),
    symbols: str | None = typer.Option(
        None,
        "--symbols",
        help="Comma-separated subset of the universe (default: all universe rows).",
    ),
) -> None:
    """Per-scope freshness report.

    Returns counts + a `stale_symbols` action list per scope. Claude can pipe
    `data.scopes.<scope>.stale_symbols` straight into
    `qr data refresh --scope <scope> --symbols ...` to refresh only what's
    out of date. Hardcoded thresholds (MA-4): profile=30d, quote=3d,
    financials=100d, ratios=100d, estimates=7d.
    """
    from sqlalchemy import select

    from quant_researcher.data.freshness import (
        SCOPE_THRESHOLDS,
        check_freshness,
    )
    from quant_researcher.db import session_factory
    from quant_researcher.models.universe import UniverseMember

    valid = (*SCOPE_THRESHOLDS.keys(), "all")
    if scope not in valid:
        _emit(
            Envelope.failure(
                "invalid_scope", f"--scope must be one of {valid}, got {scope!r}"
            )
        )

    with session_factory()() as sess:
        if symbols:
            targets = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        else:
            targets = sorted(sess.scalars(select(UniverseMember.symbol)))
    if not targets:
        _emit(
            Envelope.failure(
                "empty_universe",
                "no symbols to inspect — run `qr universe set --file …` first.",
            )
        )

    scopes_to_check = tuple(SCOPE_THRESHOLDS.keys()) if scope == "all" else (scope,)
    try:
        with session_factory()() as sess:
            report = check_freshness(sess, targets, scopes=scopes_to_check)
    except Exception as exc:
        _emit(Envelope.failure("data_freshness_failed", str(exc)))
    else:
        scopes_out = {
            name: {
                "total": sf.total,
                "fresh": len(sf.fresh),
                "stale": len(sf.stale),
                "missing": len(sf.missing),
                "threshold_days": sf.threshold_days,
                "stale_symbols": sf.needs_refresh,
            }
            for name, sf in report.scopes.items()
        }
        _emit(
            Envelope.success(
                data={
                    "scope": scope,
                    "universe_size": len(targets),
                    "scopes": scopes_out,
                },
                data_freshness={"db": "live"},
            )
        )


# ---------------------------------------------------------------------------
# qr screen ...
# ---------------------------------------------------------------------------


@screen_app.command("run")
def screen_run(
    expr: str | None = typer.Option(
        None,
        "--expr",
        "-e",
        help=(
            "Fundamental Python-like expression, e.g. \"pe < 30 and peg < 1.5\". "
            "Allowed fields: see `qr screen fields` (TODO) or the docstring of "
            "quant_researcher.screen.expression.FIELDS."
        ),
    ),
    technical: str | None = typer.Option(
        None,
        "--technical",
        "-t",
        help=(
            "Comma-separated technical predicates, e.g. "
            "'above_sma[200],macd_golden_cross[5]'."
        ),
    ),
    symbols: str | None = typer.Option(
        None,
        "--symbols",
        help="Comma-separated subset of the universe (default: full universe).",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        "-n",
        help="If given, save the screen definition under this name (upsert).",
    ),
    description: str | None = typer.Option(
        None, "--description", help="Optional description, only stored if --name given."
    ),
) -> None:
    """Run a screen (fundamental and/or technical) over the universe.

    At least one of `--expr` / `--technical` must be supplied. Results are
    persisted to `screen_runs` (anonymous if `--name` omitted); the envelope
    returns the `run_id` so you can later diff against another run.
    """
    from quant_researcher.db import session_factory
    from quant_researcher.screen.engine import run_screen
    from quant_researcher.screen.expression import ExpressionError
    from quant_researcher.screen.technical import TechnicalError

    if not expr and not technical:
        _emit(
            Envelope.failure(
                "missing_predicate",
                "at least one of --expr / --technical is required.",
            )
        )

    target_list = (
        [s.strip().upper() for s in symbols.split(",") if s.strip()] if symbols else None
    )

    try:
        with session_factory()() as sess, sess.begin():
            result = run_screen(
                sess,
                expr=expr,
                technical=technical,
                symbols=target_list,
                save_name=name,
                description=description,
            )
    except (ExpressionError, TechnicalError) as exc:
        _emit(Envelope.failure("invalid_screen_spec", str(exc)))
    except ValueError as exc:
        _emit(Envelope.failure("screen_run_failed", str(exc)))
    except Exception as exc:
        _emit(Envelope.failure("screen_run_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={
                    "run_id": result.run_id,
                    "screen_name": result.screen_name,
                    "expr": result.expr,
                    "technical": result.technical,
                    "universe_size": result.universe_size,
                    "matched": len(result.result_symbols),
                    "symbols": result.result_symbols,
                    "expr_hash": result.expr_hash,
                },
                data_freshness={"warehouse": "live"},
            )
        )


@screen_app.command("list")
def screen_list() -> None:
    """List saved screen definitions."""
    from sqlalchemy import select

    from quant_researcher.db import session_factory
    from quant_researcher.models.screens import Screen

    try:
        with session_factory()() as sess:
            rows = list(sess.scalars(select(Screen).order_by(Screen.name)))
            items = [
                {
                    "name": s.name,
                    "expr": s.expr,
                    "technical": s.technical,
                    "description": s.description,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                }
                for s in rows
            ]
    except Exception as exc:
        _emit(Envelope.failure("screen_list_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={"count": len(items), "screens": items},
                data_freshness={"db": "live"},
            )
        )


@screen_app.command("runs")
def screen_runs(
    name: str | None = typer.Option(
        None, "--name", "-n", help="Filter to runs of a specific saved screen."
    ),
    limit: int = typer.Option(
        20, "--limit", "-l", help="Cap rows returned, newest first (default 20)."
    ),
) -> None:
    """List recent screen runs (newest first)."""
    from sqlalchemy import select

    from quant_researcher.db import session_factory
    from quant_researcher.models.screens import ScreenRun

    try:
        with session_factory()() as sess:
            q = select(ScreenRun).order_by(ScreenRun.ran_at.desc()).limit(limit)
            if name:
                q = q.where(ScreenRun.screen_name == name)
            rows = list(sess.scalars(q))
            items = [
                {
                    "run_id": r.run_id,
                    "screen_name": r.screen_name,
                    "expr": r.expr,
                    "technical": r.technical,
                    "ran_at": r.ran_at.isoformat() if r.ran_at else None,
                    "universe_size": r.universe_size,
                    "matched": len(r.result_symbols or []),
                    "expr_hash": r.expr_hash,
                }
                for r in rows
            ]
    except Exception as exc:
        _emit(Envelope.failure("screen_runs_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={"count": len(items), "runs": items, "limit": limit},
                data_freshness={"db": "live"},
            )
        )


@screen_app.command("diff")
def screen_diff(
    from_run: str = typer.Option(..., "--from", help="Older run_id."),
    to_run: str = typer.Option(..., "--to", help="Newer run_id."),
) -> None:
    """Compare two screen runs — added / removed / kept symbols."""
    from quant_researcher.db import session_factory
    from quant_researcher.screen.engine import diff_runs

    try:
        with session_factory()() as sess:
            diff = diff_runs(sess, from_run, to_run)
    except ValueError as exc:
        _emit(Envelope.failure("screen_diff_failed", str(exc)))
    except Exception as exc:
        _emit(Envelope.failure("screen_diff_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={
                    "from": from_run,
                    "to": to_run,
                    "added": diff["added"],
                    "removed": diff["removed"],
                    "kept": diff["kept"],
                    "added_count": len(diff["added"]),
                    "removed_count": len(diff["removed"]),
                    "kept_count": len(diff["kept"]),
                },
                data_freshness={"db": "live"},
            )
        )


@screen_app.command("fields")
def screen_fields() -> None:
    """List the field names usable in `--expr` and `--technical`."""
    from quant_researcher.screen.expression import FIELDS
    from quant_researcher.screen.technical import available_predicates

    _emit(
        Envelope.success(
            data={
                "expression_fields": sorted(FIELDS.keys()),
                "technical_predicates": available_predicates(),
            },
            data_freshness={"code": "live"},
        )
    )


# ---------------------------------------------------------------------------
# qr value ...
# ---------------------------------------------------------------------------


@app.command("value")
def value_company_cmd(
    symbol: str = typer.Argument(..., help="Ticker to value (e.g. AAPL)."),
    model: str = typer.Option(
        "all",
        "--model",
        "-m",
        help="Which valuation model(s) to run: dcf | peg | multiples | all.",
    ),
    assumptions: str | None = typer.Option(
        None,
        "--assumptions",
        "-a",
        help=(
            "JSON dict of override assumptions, e.g. "
            "'{\"growth_rate\": 0.10, \"terminal_growth\": 0.025, \"wacc\": 0.09}'. "
            "Keys: growth_rate, terminal_growth, wacc, n_years, rf, erp, "
            "base_fcf, net_debt, shares."
        ),
    ),
) -> None:
    """Run valuation models against the warehouse and persist a snapshot.

    Defaults to `--model all` which runs DCF + PEG + relative multiples
    and reports the simple mean of available per-share fair values as
    `fair_value_per_share_mean`. Pass `--model dcf` (etc.) to limit. The
    snapshot row in `valuation_snapshots` records assumptions + result +
    sensitivity grid so a decision is replayable.
    """
    import json as _json

    from quant_researcher.db import session_factory
    from quant_researcher.valuation.engine import VALID_MODELS, value_company

    if model not in VALID_MODELS:
        _emit(
            Envelope.failure(
                "invalid_model",
                f"--model must be one of {VALID_MODELS}, got {model!r}",
            )
        )

    parsed_assumptions: dict | None = None
    if assumptions:
        try:
            parsed_assumptions = _json.loads(assumptions)
            if not isinstance(parsed_assumptions, dict):
                raise ValueError("--assumptions must be a JSON object")
        except (ValueError, TypeError) as exc:
            _emit(
                Envelope.failure(
                    "invalid_assumptions",
                    f"failed to parse --assumptions JSON: {exc}",
                )
            )

    try:
        with session_factory()() as sess, sess.begin():
            result = value_company(
                sess,
                symbol.upper(),
                model=model,
                assumptions=parsed_assumptions,
            )
    except ValueError as exc:
        _emit(Envelope.failure("valuation_failed", str(exc)))
    except Exception as exc:
        _emit(Envelope.failure("valuation_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data=result,
                data_freshness={"warehouse": "live"},
            )
        )


if __name__ == "__main__":
    app()
