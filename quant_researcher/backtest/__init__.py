"""Backtest orchestration layer (MH) — qr-specific glue over the ported engine.

`runner.run_backtest` is the single entry point; `strategies/` holds the
built-in registry; `loader` handles `--strategy-file`.
"""
