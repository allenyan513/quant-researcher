"""Valuation (MC): WACC + DCF-FCFF + PEG + relative multiples + snapshots.

Per implementation-plan.md §7 line 89. v1 deliberately simplifies WACC
(CAPM with default RF=4.5%, no debt-mix adjustment) and skips EPV / DDM —
both can be added without changing the model schema.
"""
