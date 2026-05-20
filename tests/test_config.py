"""Settings DSN normalization — paste-any-provider ergonomics."""

from __future__ import annotations

from quant_researcher.config import Settings


def test_normalizes_postgres_scheme() -> None:
    s = Settings(qr_database_url="postgres://u:p@h.example.com:5432/db")
    assert s.qr_database_url == "postgresql+psycopg://u:p@h.example.com:5432/db"


def test_normalizes_postgresql_scheme() -> None:
    s = Settings(qr_database_url="postgresql://u:p@h.example.com:5432/db?sslmode=require")
    assert (
        s.qr_database_url
        == "postgresql+psycopg://u:p@h.example.com:5432/db?sslmode=require"
    )


def test_preserves_explicit_psycopg_driver() -> None:
    s = Settings(qr_database_url="postgresql+psycopg://u:p@h/db")
    assert s.qr_database_url == "postgresql+psycopg://u:p@h/db"


def test_preserves_other_explicit_driver() -> None:
    # If user explicitly picks another driver, respect it (they'll get a clear
    # ModuleNotFoundError from SQLAlchemy if it's not installed).
    s = Settings(qr_database_url="postgresql+psycopg2://u:p@h/db")
    assert s.qr_database_url == "postgresql+psycopg2://u:p@h/db"
