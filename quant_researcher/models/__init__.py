"""SQLAlchemy model package.

Importing this package as a side-effect registers every declarative model
onto `quant_researcher.db.Base.metadata`, so `Base.metadata.create_all` and
`qr db status` see the full schema. Per D11 (no Alembic), additive table /
column changes are picked up by re-running `qr db init`.

MA-1: `universe`, `securities`. MA-2: `profiles`, `daily_prices`. MA-3:
`income_statement`, `balance_sheet`, `cash_flow`, `financial_ratios`,
`analyst_estimates`. MB: `screens`, `screen_runs`. MC: `valuation_snapshots`.
ME: `holdings`. MD: `news_items`, `research_bundles`. MF: `decisions`,
`decision_tracking`. MH: `backtest_runs`. MG: `signals`, `signal_runs`.
"""

from __future__ import annotations

from quant_researcher.models.backtest import BacktestRun
from quant_researcher.models.decisions import Decision, DecisionTracking
from quant_researcher.models.estimates import AnalystEstimate
from quant_researcher.models.financials import BalanceSheet, CashFlow, IncomeStatement
from quant_researcher.models.holdings import Holding
from quant_researcher.models.morningcall import MorningCallSnapshot
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile
from quant_researcher.models.ratios import FinancialRatios
from quant_researcher.models.research import NewsItem, ResearchBundle
from quant_researcher.models.screens import Screen, ScreenRun
from quant_researcher.models.securities import Security
from quant_researcher.models.signals import Signal, SignalRun
from quant_researcher.models.universe import UniverseMember
from quant_researcher.models.valuation import ValuationSnapshot

__all__ = [
    "AnalystEstimate",
    "BacktestRun",
    "BalanceSheet",
    "CashFlow",
    "DailyPrice",
    "Decision",
    "DecisionTracking",
    "FinancialRatios",
    "Holding",
    "IncomeStatement",
    "MorningCallSnapshot",
    "NewsItem",
    "Profile",
    "ResearchBundle",
    "Screen",
    "ScreenRun",
    "Security",
    "Signal",
    "SignalRun",
    "UniverseMember",
    "ValuationSnapshot",
]
