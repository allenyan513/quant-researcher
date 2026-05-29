"""qr-service — the long-running service layer (Issue #53).

Sits on top of the qr core (which it calls in-process as a library) and adds the
event-driven loop: ingestion, the Agent brain, persistence of signals, monitoring,
and notification. Phase 0 ships only the skeleton: a FastAPI app (`api.py`) and
the in-process tool wrappers (`tools.py`). The CLI / JSON-envelope contracts are
untouched; nothing here is a `qr` subcommand (a daemon would break the
one-envelope-per-command contract — run via uvicorn / docker instead).
"""

from __future__ import annotations
