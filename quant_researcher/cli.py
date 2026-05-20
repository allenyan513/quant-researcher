"""CLI entry point (typer). Per I1: single `qr` binary, stable JSON envelope
on stdout, exit 0 on ok / 1 on error. Subcommands grow per milestone — M0 ships
the `qr db` group only.
"""

from __future__ import annotations

import time
from pathlib import Path

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
) -> None:
    """Refresh FMP-sourced warehouse tables for the active universe.

    `--scope profile`: replace `profiles` rows from FMP `/profile`.
    `--scope quote`: append new OHLCV bars to `daily_prices`.
    `--scope financials`: ingest income / balance sheet / cash flow (annual +
    quarterly, `known_at = acceptedDate` per D6).
    `--scope ratios`: `/ratios` rows per period (`known_at = now`).
    `--scope estimates`: forward analyst consensus (`session.merge` revises).
    `--scope all`: runs every scope in the order above.
    """
    from sqlalchemy import select

    from quant_researcher.data.fmp import FMPClient
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
    try:
        scopes_out: dict[str, dict] = {}
        with (
            session_factory()() as sess,
            sess.begin(),
            FMPClient(api_key=cfg.fmp_api_key) as client,
        ):
            if scope in ("profile", "all"):
                r = refresh_profile(sess, client, targets)
                scopes_out["profile"] = {
                    "succeeded_count": len(r.succeeded),
                    "failed": r.failed,
                    "total_upserted": r.total_upserted,
                }
            if scope in ("quote", "all"):
                r = refresh_quotes(sess, client, targets, lookback_days=lookback_days)
                scopes_out["quote"] = {
                    "succeeded_count": len(r.succeeded),
                    "failed": r.failed,
                    "total_upserted": r.total_upserted,
                    "total_skipped": r.total_skipped,
                }
            if scope in ("financials", "all"):
                r = refresh_financials(sess, client, targets, periods=parsed_periods)
                scopes_out["financials"] = {
                    "succeeded_count": len(r.succeeded),
                    "failed": r.failed,
                    "total_upserted": r.total_upserted,
                    "total_skipped": r.total_skipped,
                }
            if scope in ("ratios", "all"):
                r = refresh_ratios(sess, client, targets, periods=parsed_periods)
                scopes_out["ratios"] = {
                    "succeeded_count": len(r.succeeded),
                    "failed": r.failed,
                    "total_upserted": r.total_upserted,
                    "total_skipped": r.total_skipped,
                }
            if scope in ("estimates", "all"):
                r = refresh_estimates(sess, client, targets, periods=parsed_periods)
                scopes_out["estimates"] = {
                    "succeeded_count": len(r.succeeded),
                    "failed": r.failed,
                    "total_upserted": r.total_upserted,
                    "total_skipped": r.total_skipped,
                }
    except Exception as exc:
        _emit(Envelope.failure("data_refresh_failed", str(exc)))
    else:
        _emit(
            Envelope.success(
                data={
                    "scope": scope,
                    "periods": list(parsed_periods),
                    "universe_size": len(targets),
                    "symbols_processed": targets,
                    "scopes": scopes_out,
                },
                data_freshness={"fmp": "live"},
            )
        )


if __name__ == "__main__":
    app()
