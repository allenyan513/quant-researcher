"""In-process tool wrappers over the qr core (Issue #53, the S3 tool layer).

The service demotes qr to a library: these thin wrappers open their own
short-lived session and call the existing pure functions, returning plain dicts.
They are what the Phase 1 Agent Core will register as callable tools. No FMP
client and no LLM here — just warehouse reads/writes through the core functions,
which stay untouched. Imports are lazy (per the project's lazy-import convention)
so importing this module stays cheap.
"""

from __future__ import annotations

from typing import Any


def value_company_tool(
    symbol: str,
    *,
    model: str = "all",
    assumptions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Value a company from the warehouse — wraps `valuation.engine.value_company`."""
    from quant_researcher.db import session_factory
    from quant_researcher.valuation.engine import value_company

    with session_factory()() as session, session.begin():
        return value_company(session, symbol.upper(), model=model, assumptions=assumptions)


def research_bundle_tool(
    symbol: str,
    *,
    news_limit: int = 10,
    save: bool = False,
) -> dict[str, Any]:
    """One-shot research aggregate — wraps `research.bundler.bundle`.

    `save=False` by default (a read for the agent); when `save=True` the returned
    dict carries the persisted `bundle_id`.
    """
    from quant_researcher.db import session_factory
    from quant_researcher.research.bundler import bundle

    with session_factory()() as session, session.begin():
        bundle_id, payload = bundle(session, symbol.upper(), news_limit=news_limit, save=save)
    return {"bundle_id": bundle_id, **payload}
