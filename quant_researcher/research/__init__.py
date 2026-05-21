"""Research data packages (MD).

`bundler.build_bundle` aggregates everything the warehouse knows about one
symbol — profile, latest financials/ratios/estimates, recent valuation
snapshots, current holdings, recent news, latest earnings transcript — into
a single JSON dict that Claude consumes for deep-dive narratives. Output is
persisted to `research_bundles` so a decision can later be replayed.

`refresh.refresh_news` is the only FMP-touching part — fills `news_items`.
"""
