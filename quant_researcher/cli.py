"""CLI entry point (typer). Per I1: single `qr` binary, stable JSON envelope
on stdout, exit 0 on ok / 1 on error. Subcommands grow per milestone — M0 ships
the `qr db` group only.
"""

from __future__ import annotations

import time

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


if __name__ == "__main__":
    app()
