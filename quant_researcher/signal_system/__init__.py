"""System B — trading-signal analysis (Issue #53).

The price/event-driven counterpart to System A valuation (`quant_researcher/
valuation/`): given an Event plus facts, decide direction + target + stop +
horizon. System A's fair value is just one input here; System B must be able to
run without it.

Phase 0 is a package placeholder — the `SignalGenerator` interface and the
`compute_signal` logic land in Phase 1. Cross-subsystem calls go through
interfaces/contracts only (see `docs/architecture-subsystems.md`).
"""

from __future__ import annotations
