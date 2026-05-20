"""Configuration: env + settings loading.

Per D8 / I7: secrets live in `.env` (git-ignored). The repo only ships `.env.example`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Project configuration sourced from env (and `.env`, if present).

    Field names map case-insensitively to env vars
    (e.g. `qr_database_url` ← `QR_DATABASE_URL`).
    """

    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Required: Postgres DSN. Per D10/D11 this is Supabase or any other managed
    # Postgres (Neon, etc.). The scheme is auto-normalized below — paste whatever
    # the provider gives you (`postgres://...` or `postgresql://...`).
    qr_database_url: str

    # Required at MA (data warehouse ingestion)
    fmp_api_key: str | None = None

    # Optional: FRED 10Y Treasury → WACC risk-free rate
    fred_api_key: str | None = None

    # Optional: IBKR Flex Query (needed at ME for live holdings)
    flex_token_key: str | None = None
    flex_query_id_live: str | None = None
    flex_query_id_historical: str | None = None

    @field_validator("qr_database_url", mode="before")
    @classmethod
    def _normalize_dsn_scheme(cls, v: object) -> object:
        """Normalize provider-supplied DSN schemes to psycopg v3.

        SQLAlchemy's default driver for `postgresql://` is psycopg2 (v2, not
        installed); Neon/Supabase hand out `postgres://` or `postgresql://`.
        Rewrite both to `postgresql+psycopg://` so users can paste the DSN
        verbatim. Explicit `postgresql+<driver>://` is respected as-is.
        """
        if not isinstance(v, str):
            return v
        if v.startswith("postgres://"):
            return "postgresql+psycopg://" + v[len("postgres://") :]
        if v.startswith("postgresql://"):
            return "postgresql+psycopg://" + v[len("postgresql://") :]
        return v


@lru_cache(maxsize=1)
def settings() -> Settings:
    """Load and cache project settings. Raises if QR_DATABASE_URL is unset."""
    return Settings()  # type: ignore[call-arg]
