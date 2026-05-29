"""FastAPI app for the real-time signal service (Issue #53, Phase 0 skeleton).

Endpoints are sync `def` — FastAPI runs them in a threadpool, so they reuse the
project's synchronous `session_factory` + core functions with **zero** changes to
the qr core. Responses reuse the CLI's `Envelope` so HTTP and stdout share one
machine-readable contract. This is only the skeleton: no Agent, poller, or
monitor yet (Phase 1+).

Run locally:  uvicorn quant_researcher.service.api:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from quant_researcher.contract import Envelope

app = FastAPI(
    title="qr-service",
    summary="Real-time news → valuation → trading-signal service (Issue #53).",
)


def _json(env: Envelope, *, status_code: int = 200) -> JSONResponse:
    """Serialize an Envelope as the HTTP body, mirroring the CLI's stdout contract."""
    return JSONResponse(content=env.model_dump(mode="json"), status_code=status_code)


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Liveness + DB connectivity (SELECT 1). 200 when reachable, 503 when not."""
    from sqlalchemy import text

    from quant_researcher.db import engine

    try:
        with engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # surface as a structured failure envelope
        return _json(Envelope.failure("db_unreachable", str(exc)), status_code=503)
    return _json(
        Envelope.success(data={"status": "ok", "db": "ok"}, data_freshness={"db": "live"})
    )


@app.get("/value/{symbol}")
def value(symbol: str, model: str = "all") -> JSONResponse:
    """Value a company — end-to-end proof: HTTP → tool → sync session → core fn."""
    from quant_researcher.service.tools import value_company_tool
    from quant_researcher.valuation.engine import VALID_MODELS

    if model not in VALID_MODELS:
        return _json(
            Envelope.failure(
                "invalid_model", f"model must be one of {VALID_MODELS}, got {model!r}"
            ),
            status_code=400,
        )
    try:
        result = value_company_tool(symbol, model=model)
    except Exception as exc:  # surface business/DB errors as a failure envelope
        return _json(Envelope.failure("valuation_failed", str(exc)), status_code=500)
    return _json(Envelope.success(data=result, data_freshness={"warehouse": "live"}))
