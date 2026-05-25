"""Freshness reporting + staleness filtering.

Single source of truth for "is this data fresh enough?" used by both:
* `qr data freshness` (the report, exposed via `check_freshness`), and
* `qr data refresh` default-only-stale path (via `stale_symbols`).

Thresholds are hardcoded here (MA-4 decision: low-config / personal-first per
features.md). To tune them, edit `SCOPE_THRESHOLDS` and re-deploy.

Staleness rules per scope:
* **profile / ratios / estimates** — `now() − MAX(known_at) > threshold`
* **quote** — `today − MAX(trade_date) > 3 calendar days` (pragmatic Fri→Mon
  safe; avoids a business-calendar dependency)
* **financials** — `now() − MAX(fiscal_date) > 100d` from `income_statement`
  (canonical "has a new quarter dropped?" signal; we DON'T use `known_at`
  because re-ingesting an old quarter shouldn't reset the freshness clock)

A symbol with zero rows in the scope's table is `missing` (never fetched);
a symbol with rows past threshold is `stale`. `needs_refresh = stale ∪
missing` is what refresh consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from quant_researcher.models.estimates import AnalystEstimate
from quant_researcher.models.financials import IncomeStatement
from quant_researcher.models.insider import InsiderTransaction
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile
from quant_researcher.models.ratios import FinancialRatios
from quant_researcher.models.transcripts import Transcript

# --- Hardcoded thresholds (MA-4) -------------------------------------------

PROFILE_STALE_AFTER = timedelta(days=30)
QUOTE_STALE_AFTER = timedelta(days=3)
FINANCIALS_STALE_AFTER = timedelta(days=100)
RATIOS_STALE_AFTER = timedelta(days=100)
ESTIMATES_STALE_AFTER = timedelta(days=7)
TRANSCRIPT_STALE_AFTER = timedelta(days=100)  # quarterly cadence, like financials
INSIDER_STALE_AFTER = timedelta(days=30)  # Form 4s land sporadically; re-check monthly

SCOPE_THRESHOLDS: dict[str, timedelta] = {
    "profile": PROFILE_STALE_AFTER,
    "quote": QUOTE_STALE_AFTER,
    "financials": FINANCIALS_STALE_AFTER,
    "ratios": RATIOS_STALE_AFTER,
    "estimates": ESTIMATES_STALE_AFTER,
    "transcript": TRANSCRIPT_STALE_AFTER,
    "insider": INSIDER_STALE_AFTER,
}


# --- Report shape ----------------------------------------------------------


@dataclass(frozen=True)
class ScopeFreshness:
    """Per-scope freshness state for a fixed set of symbols.

    `fresh`/`stale`/`missing` are disjoint and partition the input symbol set.
    `needs_refresh` is the sorted union of `stale` and `missing` — Claude
    pipes this list into `qr data refresh --symbols ...`.
    """

    scope: str
    threshold_days: int
    total: int
    fresh: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def needs_refresh(self) -> list[str]:
        return sorted(set(self.stale) | set(self.missing))


@dataclass(frozen=True)
class FreshnessReport:
    as_of: datetime
    scopes: dict[str, ScopeFreshness]


# --- Public API ------------------------------------------------------------


def check_freshness(
    session: Session,
    symbols: list[str],
    *,
    scopes: tuple[str, ...] = tuple(SCOPE_THRESHOLDS.keys()),
    now: datetime | None = None,
) -> FreshnessReport:
    """Compute freshness for each (scope, symbol) combination."""
    _now = now or datetime.now(UTC)
    report_scopes: dict[str, ScopeFreshness] = {}
    for scope in scopes:
        if scope not in SCOPE_THRESHOLDS:
            raise ValueError(f"unknown scope: {scope!r}")
        report_scopes[scope] = _compute_scope(session, scope, symbols, _now)
    return FreshnessReport(as_of=_now, scopes=report_scopes)


def stale_symbols(
    session: Session,
    scope: str,
    symbols: list[str],
    *,
    now: datetime | None = None,
) -> list[str]:
    """Return `(stale ∪ missing)` for one scope. Used by `refresh_X(only_stale=True)`."""
    if scope not in SCOPE_THRESHOLDS:
        raise ValueError(f"unknown scope: {scope!r}")
    return _compute_scope(session, scope, symbols, now or datetime.now(UTC)).needs_refresh


# --- Internals -------------------------------------------------------------


def _compute_scope(
    session: Session, scope: str, symbols: list[str], now: datetime
) -> ScopeFreshness:
    threshold = SCOPE_THRESHOLDS[scope]
    if not symbols:
        return ScopeFreshness(scope=scope, threshold_days=threshold.days, total=0)

    if scope == "quote":
        latest = _latest_per_symbol(
            session, DailyPrice.symbol, DailyPrice.trade_date, symbols
        )
        cutoff_date = (now.date() if isinstance(now, datetime) else now) - threshold
        fresh, stale, missing = _partition_dates(symbols, latest, cutoff_date)
    elif scope == "financials":
        latest = _latest_per_symbol(
            session, IncomeStatement.symbol, IncomeStatement.fiscal_date, symbols
        )
        cutoff_date = now.date() - threshold
        fresh, stale, missing = _partition_dates(symbols, latest, cutoff_date)
    elif scope == "transcript":
        # Judge on the call's own date (like financials' fiscal_date), not
        # known_at — re-ingesting an old call must not reset the freshness clock.
        latest = _latest_per_symbol(
            session, Transcript.symbol, Transcript.call_date, symbols
        )
        cutoff_date = now.date() - threshold
        fresh, stale, missing = _partition_dates(symbols, latest, cutoff_date)
    elif scope == "insider":
        # Judge on the latest Form 4 filing date (the SEC filing event).
        latest = _latest_per_symbol(
            session, InsiderTransaction.symbol, InsiderTransaction.filing_date, symbols
        )
        cutoff_date = now.date() - threshold
        fresh, stale, missing = _partition_dates(symbols, latest, cutoff_date)
    else:
        sym_col, ts_col = _known_at_columns(scope)
        latest = _latest_per_symbol(session, sym_col, ts_col, symbols)
        cutoff_dt = now - threshold
        fresh, stale, missing = _partition_datetimes(symbols, latest, cutoff_dt)

    return ScopeFreshness(
        scope=scope,
        threshold_days=threshold.days,
        total=len(symbols),
        fresh=sorted(fresh),
        stale=sorted(stale),
        missing=sorted(missing),
    )


def _known_at_columns(scope: str):
    """Return `(symbol_col, known_at_col)` for the model backing `scope`."""
    if scope == "profile":
        return Profile.symbol, Profile.known_at
    if scope == "ratios":
        return FinancialRatios.symbol, FinancialRatios.known_at
    if scope == "estimates":
        return AnalystEstimate.symbol, AnalystEstimate.known_at
    raise ValueError(f"no known_at columns for scope {scope!r}")


def _latest_per_symbol(
    session: Session, sym_col, ts_col, symbols: list[str]
) -> dict[str, object]:
    """GROUP-BY-symbol max(ts_col) restricted to `symbols`. None values dropped."""
    rows = session.execute(
        select(sym_col, func.max(ts_col)).where(sym_col.in_(symbols)).group_by(sym_col)
    ).all()
    return {sym: latest for sym, latest in rows if latest is not None}


def _partition_dates(
    symbols: list[str], latest: dict[str, object], cutoff: date
) -> tuple[list[str], list[str], list[str]]:
    fresh: list[str] = []
    stale: list[str] = []
    missing: list[str] = []
    for sym in symbols:
        val = latest.get(sym)
        if val is None:
            missing.append(sym)
        else:
            val_d = val if isinstance(val, date) and not isinstance(val, datetime) else val
            # Normalize datetime → date if a DateTime column slipped in
            if isinstance(val_d, datetime):
                val_d = val_d.date()
            if val_d >= cutoff:
                fresh.append(sym)
            else:
                stale.append(sym)
    return fresh, stale, missing


def _partition_datetimes(
    symbols: list[str], latest: dict[str, object], cutoff: datetime
) -> tuple[list[str], list[str], list[str]]:
    fresh: list[str] = []
    stale: list[str] = []
    missing: list[str] = []
    cutoff_naive = cutoff.replace(tzinfo=None) if cutoff.tzinfo else cutoff
    for sym in symbols:
        val = latest.get(sym)
        if val is None:
            missing.append(sym)
            continue
        val_dt = val if isinstance(val, datetime) else None
        if val_dt is None:
            # Date column where we expected datetime — be lenient
            missing.append(sym)
            continue
        # SQLite strips tz; Postgres preserves it. Normalize to naive for compare.
        val_naive = val_dt.astimezone(UTC).replace(tzinfo=None) if val_dt.tzinfo else val_dt
        if val_naive >= cutoff_naive:
            fresh.append(sym)
        else:
            stale.append(sym)
    return fresh, stale, missing
