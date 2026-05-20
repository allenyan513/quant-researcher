"""JSON envelope contract — the stable machine-readable interface between `qr`
commands and the Claude orchestrator.

See features.md §5 ("机读契约") and implementation-plan.md §3.
"""

from __future__ import annotations

import subprocess
from datetime import date
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, Field

from quant_researcher import __version__

SCHEMA_VERSION = "1"


class ErrorDetail(BaseModel):
    """Structured error returned in failure envelopes."""

    code: str
    message: str
    details: dict[str, Any] | None = None


class Envelope(BaseModel):
    """Stable JSON envelope emitted to stdout by every `qr` command.

    Shape (success):
        {
          "ok": true,
          "schema_version": "1",
          "as_of": "2026-05-19",
          "data_freshness": {"prices": "2026-05-19", "financials": "2026-Q1"},
          "snapshot_id": "sha256:…" | null,
          "code_version": "git:abc1234" | "pkg:0.1.0",
          "data": {...} | null,
          "error": null
        }

    Shape (failure): `ok=false`, `data=null`, `error={...}`.
    """

    ok: bool
    schema_version: str = SCHEMA_VERSION
    as_of: str
    data_freshness: dict[str, str] = Field(default_factory=dict)
    snapshot_id: str | None = None
    code_version: str = Field(default_factory=lambda: code_version())
    data: dict[str, Any] | None = None
    error: ErrorDetail | None = None

    @classmethod
    def success(
        cls,
        data: dict[str, Any] | None = None,
        *,
        as_of: str | None = None,
        data_freshness: dict[str, str] | None = None,
        snapshot_id: str | None = None,
    ) -> Envelope:
        return cls(
            ok=True,
            as_of=as_of or date.today().isoformat(),
            data_freshness=data_freshness or {},
            snapshot_id=snapshot_id,
            data=data or {},
        )

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> Envelope:
        return cls(
            ok=False,
            as_of=date.today().isoformat(),
            error=ErrorDetail(code=code, message=message, details=details),
        )

    def to_json(self) -> str:
        return self.model_dump_json(exclude_none=False)


@lru_cache(maxsize=1)
def code_version() -> str:
    """Return a stable identifier for the running code.

    Prefers `git:<short-sha>[+dirty]` when in a git checkout; falls back to
    `pkg:<__version__>` otherwise.
    """
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return f"git:{sha}{'+dirty' if dirty else ''}"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return f"pkg:{__version__}"
