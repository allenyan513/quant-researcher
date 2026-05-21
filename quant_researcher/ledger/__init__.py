"""Decision ledger (MF) — record / track / scorecard.

`engine.record_decision` snapshots the warehouse state (via
`research.bundler.bundle`) and writes a `Decision` row with thesis +
confidence + tags + sector-at-open. `engine.track_decisions` walks every
open decision and computes 1w/1m/3m/6m forward returns vs SPY + the
sector ETF (mapping in `sectors.py`). `engine.scorecard` aggregates the
tracked alpha by chosen dimension.
"""
