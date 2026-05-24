"""Refresh pipelines for FMP-sourced warehouse tables.

`refresh_profile` overwrites `profiles` rows per symbol (FMP is the truth).
`refresh_quotes` is mostly append-only on `daily_prices`: it asks for OHLCV
since the latest known trade_date (or `today − lookback_days` if the symbol is
new) and inserts dates not yet in the table. The exception is a trailing
`heal_window_days` of recent bars, which are re-fetched and UPSERTed every run
so a preliminary close/volume/adj_close captured near the US close gets
corrected once FMP finalizes it. Older EOD bars stay immutable (insert-only).

Per-symbol failures are isolated: one bad ticker yields a `SymbolOutcome`
with `ok=False` and the loop continues. The caller decides what to do with
the `RefreshResult` (emit envelope, retry, etc.) and commits the session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from quant_researcher.data.fmp import FMPClient, FMPError
from quant_researcher.data.freshness import stale_symbols
from quant_researcher.models.estimates import AnalystEstimate
from quant_researcher.models.financials import BalanceSheet, CashFlow, IncomeStatement
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile
from quant_researcher.models.ratios import FinancialRatios


@dataclass(frozen=True)
class SymbolOutcome:
    symbol: str
    ok: bool
    upserted: int = 0
    skipped: int = 0
    error: str | None = None


@dataclass(frozen=True)
class RefreshResult:
    scope: str
    outcomes: list[SymbolOutcome] = field(default_factory=list)

    @property
    def total_upserted(self) -> int:
        return sum(o.upserted for o in self.outcomes)

    @property
    def total_skipped(self) -> int:
        return sum(o.skipped for o in self.outcomes)

    @property
    def succeeded(self) -> list[str]:
        return [o.symbol for o in self.outcomes if o.ok]

    @property
    def failed(self) -> list[dict[str, str]]:
        return [{"symbol": o.symbol, "error": o.error or ""} for o in self.outcomes if not o.ok]


# ----- profile --------------------------------------------------------------


def refresh_profile(
    session: Session,
    client: FMPClient,
    symbols: list[str],
    *,
    only_stale: bool = True,
) -> RefreshResult:
    """Refresh `profiles` for each symbol. Latest FMP payload overwrites the row.

    When `only_stale=True` (the default since MA-4), the symbol list is first
    narrowed by `stale_symbols(...)` so fresh rows skip the FMP call entirely.
    Pass `only_stale=False` (CLI: `--force`) to fetch every requested symbol.
    """
    if only_stale:
        symbols = stale_symbols(session, "profile", symbols)
    outcomes: list[SymbolOutcome] = []
    for sym in symbols:
        try:
            payload = client.get_profile(sym)
        except FMPError as exc:
            outcomes.append(SymbolOutcome(sym, ok=False, error=str(exc)))
            continue
        if not payload:
            outcomes.append(SymbolOutcome(sym, ok=False, error="empty profile response"))
            continue
        session.merge(_profile_from_fmp(sym, payload))
        outcomes.append(SymbolOutcome(sym, ok=True, upserted=1))
    return RefreshResult(scope="profile", outcomes=outcomes)


def _profile_from_fmp(symbol: str, payload: dict[str, Any]) -> Profile:
    return Profile(
        symbol=symbol,
        company_name=payload.get("companyName"),
        sector=payload.get("sector"),
        industry=payload.get("industry"),
        exchange=payload.get("exchangeShortName") or payload.get("exchange"),
        currency=payload.get("currency"),
        country=payload.get("country"),
        beta=_as_float(payload.get("beta")),
        ipo_date=_as_date(payload.get("ipoDate")),
        is_etf=payload.get("isEtf"),
        is_fund=payload.get("isFund"),
        is_adr=payload.get("isAdr"),
        is_actively_trading=payload.get("isActivelyTrading"),
        raw=payload,
        known_at=datetime.now(UTC),
    )


# ----- quotes (daily OHLCV) ------------------------------------------------


def refresh_quotes(
    session: Session,
    client: FMPClient,
    symbols: list[str],
    *,
    lookback_days: int = 730,
    heal_window_days: int = 7,
    only_stale: bool = True,
) -> RefreshResult:
    """Mostly-append refresh of `daily_prices` per symbol, with recent-bar heal.

    Fetches raw OHLCV from `/historical-price-eod/full` AND the split/dividend-
    adjusted stream from `/historical-price-eod/dividend-adjusted`, joining each
    `adjClose` onto its bar by date so `adj_close` is populated (`/full` omits
    it). Both calls share the `try`: a TRANSIENT adjusted-endpoint error
    (429/5xx) fails the symbol — it self-heals next run, since `since` restarts
    at the last STORED date — rather than silently storing unadjusted prices. A
    402 instead soft-fails inside `get_adjusted_prices` → `[]`, so raw bars still
    ingest with `adj_close=None` (panel fallback) rather than a plan gate halting
    ALL quote ingestion.

    Ingest is append-only EXCEPT for a trailing `heal_window_days` of recent
    bars. The incremental fetch starts at `latest − heal_window_days` (not
    `latest + 1`), so the last few stored bars are re-pulled and UPSERTed: a bar
    stored with a PRELIMINARY close/volume/adj_close near the US close (e.g. the
    23:00-UTC CI run, which can capture an intraday value FMP later revises)
    gets corrected once finalized. Bars OLDER than the window are insert-only —
    a filed EOD bar is immutable, so the duplicate date is skipped. The initial
    bulk load (no prior data) is pure insert. See `_upsert_prices`.

    Two staleness caveats the heal window does NOT fully fix:
    (1) a split/dividend re-scales FMP's WHOLE adjusted series — bars OLDER than
    the window keep their prior `adj_close` and go stale; re-run
    `scripts/backfill_adj_close.py` (idempotent) after a corporate action.
    (2) under the default `only_stale=True`, a symbol whose latest bar is within
    the `quote` freshness threshold (3 calendar days) is filtered out before any
    fetch, so a preliminary bar heals only on the next run that re-fetches it
    (within ~3 days, or immediately under `--force`).

    `only_stale=True` (default since MA-4) narrows `symbols` via
    `stale_symbols("quote", ...)` before any FMP call.
    """
    if only_stale:
        symbols = stale_symbols(session, "quote", symbols)
    outcomes: list[SymbolOutcome] = []
    today = date.today()
    for sym in symbols:
        # latest-lookup + window math don't raise FMPError → keep them out of
        # the try (§2: try wraps only the calls that can fail per-symbol).
        latest = session.scalar(
            select(DailyPrice.trade_date)
            .where(DailyPrice.symbol == sym)
            .order_by(DailyPrice.trade_date.desc())
            .limit(1)
        )
        heal_floor: date | None = None
        if latest is not None:
            heal_floor = latest - timedelta(days=heal_window_days)
            since = heal_floor
        else:
            since = today - timedelta(days=lookback_days)
        try:
            rows = client.get_historical_prices(sym, since=since)
            adj_rows = client.get_adjusted_prices(sym, since=since)
        except FMPError as exc:
            outcomes.append(SymbolOutcome(sym, ok=False, error=str(exc)))
            continue
        outcomes.append(
            _upsert_prices(session, sym, rows, _adj_close_by_date(adj_rows), heal_floor)
        )
    return RefreshResult(scope="quote", outcomes=outcomes)


def _adj_close_by_date(rows: list[dict[str, Any]]) -> dict[date, float]:
    """Index `/dividend-adjusted` rows' `adjClose` by trade_date for the join.

    Requested over the same date range as `/full`, so `trade_date` alone is a
    safe key. Rows missing a date or adjClose are dropped.
    """
    out: dict[date, float] = {}
    for r in rows:
        d = _as_date(r.get("date"))
        adj = _as_float(r.get("adjClose"))
        if d is not None and adj is not None:
            out[d] = adj
    return out


def _upsert_prices(
    session: Session,
    symbol: str,
    rows: list[dict[str, Any]],
    adj_by_date: dict[date, float],
    heal_floor: date | None,
) -> SymbolOutcome:
    """Insert new bars; heal (UPSERT) existing bars on/after `heal_floor`.

    A `trade_date` not yet stored is inserted. An EXISTING bar is overwritten
    only when it falls in the heal window (`trade_date >= heal_floor`) — this
    corrects a preliminary close/volume/adj_close once FMP finalizes it. Existing
    bars older than `heal_floor` (and every existing bar when `heal_floor is
    None`, i.e. the initial bulk load) are insert-only: a filed EOD bar is
    immutable, so the duplicate date is skipped. `adj_close` is carried through
    both paths (from `/full`, else the dividend-adjusted join); on heal a None
    incoming `adj_close` falls back to the stored value so a lagging adjusted
    stream never regresses a good `adj_close` to None. `known_at` is left at the
    original ingestion timestamp.
    """
    parsed: list[dict[str, Any]] = []
    for r in rows:
        mapped = _price_from_fmp(symbol, r)
        if mapped["trade_date"] is None:
            continue
        # `/full` has no adjClose; fill from the dividend-adjusted stream. A
        # non-null value from `/full` (defensive) still wins.
        if mapped["adj_close"] is None:
            mapped["adj_close"] = adj_by_date.get(mapped["trade_date"])
        parsed.append(mapped)
    if not parsed:
        return SymbolOutcome(symbol, ok=True, upserted=0)

    incoming_dates = {p["trade_date"] for p in parsed}
    existing_adj: dict[date, float | None] = {
        d: a
        for d, a in session.execute(
            select(DailyPrice.trade_date, DailyPrice.adj_close).where(
                DailyPrice.symbol == symbol,
                DailyPrice.trade_date.in_(incoming_dates),
            )
        )
    }
    new_rows: list[dict[str, Any]] = []
    heal_rows: list[dict[str, Any]] = []
    skipped = 0
    for p in parsed:
        if p["trade_date"] not in existing_adj:
            new_rows.append(p)
        elif heal_floor is not None and p["trade_date"] >= heal_floor:
            if p["adj_close"] is None:
                p["adj_close"] = existing_adj[p["trade_date"]]
            heal_rows.append(p)
        else:
            skipped += 1
    if new_rows:
        session.execute(insert(DailyPrice), new_rows)
    if heal_rows:
        # Bulk UPDATE by PK (symbol, trade_date): SET OHLCV/adj_close/volume,
        # leave `known_at` untouched.
        session.execute(update(DailyPrice), heal_rows)
    return SymbolOutcome(
        symbol, ok=True, upserted=len(new_rows) + len(heal_rows), skipped=skipped
    )


def _price_from_fmp(symbol: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "trade_date": _as_date(row.get("date")),
        "open": _as_float(row.get("open")),
        "high": _as_float(row.get("high")),
        "low": _as_float(row.get("low")),
        "close": _as_float(row.get("close")),
        "adj_close": _as_float(row.get("adjClose")) or _as_float(row.get("adj_close")),
        "volume": _as_int(row.get("volume")),
    }


# ----- financials (income / balance / cash flow) ---------------------------


_FINANCIAL_PERIODS = ("annual", "quarter")


def refresh_financials(
    session: Session,
    client: FMPClient,
    symbols: list[str],
    *,
    periods: tuple[str, ...] = _FINANCIAL_PERIODS,
    only_stale: bool = True,
) -> RefreshResult:
    """Append-only refresh of income / balance / cash-flow tables.

    For each symbol × period, fetches all three statement endpoints. Rows are
    keyed on `(symbol, period, fiscal_date)` and **`known_at` is set from FMP
    `acceptedDate`** (D6 strict). Per-period errors are isolated; a 5xx on
    quarterly doesn't block annual for the same symbol.

    `only_stale=True` (default since MA-4) narrows `symbols` via the
    `financials` staleness rule (`MAX(fiscal_date) > 100d ago`).
    """
    if only_stale:
        symbols = stale_symbols(session, "financials", symbols)
    outcomes: list[SymbolOutcome] = []
    for sym in symbols:
        upserted = 0
        skipped = 0
        errs: list[str] = []
        for period in periods:
            try:
                income_rows = client.get_income_statement(sym, period=period)
                balance_rows = client.get_balance_sheet(sym, period=period)
                cash_rows = client.get_cash_flow(sym, period=period)
            except FMPError as exc:
                errs.append(f"{period}: {exc}")
                continue
            u1, s1 = _ingest_statement(session, sym, IncomeStatement, income_rows, _income_from_fmp)
            u2, s2 = _ingest_statement(session, sym, BalanceSheet, balance_rows, _balance_from_fmp)
            u3, s3 = _ingest_statement(session, sym, CashFlow, cash_rows, _cashflow_from_fmp)
            upserted += u1 + u2 + u3
            skipped += s1 + s2 + s3
        outcomes.append(
            SymbolOutcome(
                sym,
                ok=not errs,
                upserted=upserted,
                skipped=skipped,
                error="; ".join(errs) if errs else None,
            )
        )
    return RefreshResult(scope="financials", outcomes=outcomes)


def _ingest_statement(
    session: Session,
    symbol: str,
    model: type,
    rows: list[dict[str, Any]],
    mapper: Any,
) -> tuple[int, int]:
    """Common path: map → drop incomplete → diff existing PKs → insert new."""
    parsed: list[dict[str, Any]] = []
    for r in rows:
        row = mapper(symbol, r)
        if (
            row["fiscal_date"] is None
            or not row.get("period")
            or row.get("known_at") is None
        ):
            continue
        parsed.append(row)
    if not parsed:
        return (0, 0)

    incoming_keys = {(p["symbol"], p["period"], p["fiscal_date"]) for p in parsed}
    existing = {
        (row.symbol, row.period, row.fiscal_date)
        for row in session.execute(
            select(model.symbol, model.period, model.fiscal_date)  # type: ignore[attr-defined]
            .where(model.symbol == symbol)  # type: ignore[attr-defined]
            .where(model.fiscal_date.in_({k[2] for k in incoming_keys}))  # type: ignore[attr-defined]
        )
    }
    new_rows = [p for p in parsed if (p["symbol"], p["period"], p["fiscal_date"]) not in existing]
    if new_rows:
        session.execute(insert(model), new_rows)
    return (len(new_rows), len(parsed) - len(new_rows))


def _common_financial_fields(symbol: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "period": row.get("period") or None,
        "fiscal_date": _as_date(row.get("date")),
        "calendar_year": _as_int(row.get("calendarYear")),
        "reported_currency": row.get("reportedCurrency"),
        "raw": row,
        "known_at": _as_datetime(row.get("acceptedDate")),
    }


def _income_from_fmp(symbol: str, row: dict[str, Any]) -> dict[str, Any]:
    base = _common_financial_fields(symbol, row)
    base.update(
        {
            "revenue": _as_float(row.get("revenue")),
            "cost_of_revenue": _as_float(row.get("costOfRevenue")),
            "gross_profit": _as_float(row.get("grossProfit")),
            "operating_income": _as_float(row.get("operatingIncome")),
            "net_income": _as_float(row.get("netIncome")),
            "eps": _as_float(row.get("eps")),
            "eps_diluted": _as_float(row.get("epsdiluted") or row.get("epsDiluted")),
        }
    )
    return base


def _balance_from_fmp(symbol: str, row: dict[str, Any]) -> dict[str, Any]:
    base = _common_financial_fields(symbol, row)
    base.update(
        {
            "total_assets": _as_float(row.get("totalAssets")),
            "total_liabilities": _as_float(row.get("totalLiabilities")),
            "total_equity": _as_float(
                row.get("totalEquity") or row.get("totalStockholdersEquity")
            ),
            "cash_and_equivalents": _as_float(row.get("cashAndCashEquivalents")),
            "short_term_debt": _as_float(row.get("shortTermDebt")),
            "long_term_debt": _as_float(row.get("longTermDebt")),
        }
    )
    return base


def _cashflow_from_fmp(symbol: str, row: dict[str, Any]) -> dict[str, Any]:
    base = _common_financial_fields(symbol, row)
    base.update(
        {
            "operating_cash_flow": _as_float(
                row.get("operatingCashFlow")
                or row.get("netCashProvidedByOperatingActivities")
            ),
            "investing_cash_flow": _as_float(
                row.get("investingCashFlow")
                or row.get("netCashUsedForInvestingActivites")
                or row.get("netCashUsedForInvestingActivities")
            ),
            "financing_cash_flow": _as_float(
                row.get("financingCashFlow")
                or row.get("netCashUsedProvidedByFinancingActivities")
            ),
            "capital_expenditure": _as_float(row.get("capitalExpenditure")),
            "free_cash_flow": _as_float(row.get("freeCashFlow")),
            "dividends_paid": _as_float(row.get("dividendsPaid")),
        }
    )
    return base


# ----- ratios ---------------------------------------------------------------


def refresh_ratios(
    session: Session,
    client: FMPClient,
    symbols: list[str],
    *,
    periods: tuple[str, ...] = _FINANCIAL_PERIODS,
    only_stale: bool = True,
) -> RefreshResult:
    """Refresh `financial_ratios` per (symbol, period). `known_at` = now(UTC).

    Uses `session.merge` (not `_ingest_statement`'s insert-only path) because
    ratios are **derived** from price + financials — FMP recomputes them
    whenever the market price moves, so an existing `(symbol, period,
    fiscal_date)` row should refresh with the new values rather than be
    skipped as a duplicate. Statements (income/balance/cashflow) stay
    insert-only because filed reports are immutable.

    `only_stale=True` (default since MA-4) narrows `symbols` via the `ratios`
    threshold (`MAX(known_at) > 100d ago`).
    """
    if only_stale:
        symbols = stale_symbols(session, "ratios", symbols)
    outcomes: list[SymbolOutcome] = []
    for sym in symbols:
        upserted = 0
        errs: list[str] = []
        for period in periods:
            try:
                rows = client.get_ratios(sym, period=period)
            except FMPError as exc:
                errs.append(f"{period}: {exc}")
                continue
            # ROE/ROA/fcf_yield live in /key-metrics, not /ratios — fetch and
            # merge by fiscal_date. A key-metrics failure marks the symbol
            # ok=False (these are first-class for MB screening) but the
            # /ratios rows above still get ingested (per-period isolation).
            try:
                km_by_date = _key_metrics_by_date(
                    client.get_key_metrics(sym, period=period)
                )
            except FMPError as exc:
                errs.append(f"{period} key-metrics: {exc}")
                km_by_date = {}
            for raw in rows:
                mapped = _ratio_from_fmp(sym, raw)
                if mapped["fiscal_date"] is None or not mapped["period"]:
                    continue
                _merge_key_metrics(mapped, km_by_date.get(mapped["fiscal_date"]))
                session.merge(FinancialRatios(**mapped))
                upserted += 1
        outcomes.append(
            SymbolOutcome(
                sym,
                ok=not errs,
                upserted=upserted,
                skipped=0,
                error="; ".join(errs) if errs else None,
            )
        )
    return RefreshResult(scope="ratios", outcomes=outcomes)


def _ratio_from_fmp(symbol: str, row: dict[str, Any]) -> dict[str, Any]:
    """Map FMP `/ratios` payload to FinancialRatios columns.

    Field names verified against real FMP /ratios response 2026-05-21. ROE/ROA
    and fcf_yield are usually absent from /ratios (they live in /key-metrics),
    so they parse to None here — `_merge_key_metrics` backfills them in
    `refresh_ratios`. Reading `returnOnEquity` etc. anyway is defensive: if a
    plan/endpoint ever does return them in /ratios, that value wins.
    """
    return {
        "symbol": symbol,
        "period": row.get("period") or None,
        "fiscal_date": _as_date(row.get("date")),
        # FMP uses priceToEarningsRatio (not priceEarningsRatio).
        "pe_ratio": _as_float(
            row.get("priceToEarningsRatio")
            or row.get("priceEarningsRatio")
            or row.get("peRatio")
        ),
        "peg_ratio": _as_float(
            row.get("priceToEarningsGrowthRatio")
            or row.get("forwardPriceToEarningsGrowthRatio")
            or row.get("pegRatio")
        ),
        "price_to_book": _as_float(row.get("priceToBookRatio") or row.get("pbRatio")),
        "price_to_sales": _as_float(row.get("priceToSalesRatio")),
        # FMP returns `enterpriseValueMultiple` as the EV/EBITDA-style metric.
        "ev_to_ebitda": _as_float(
            row.get("enterpriseValueMultiple")
            or row.get("enterpriseValueOverEBITDA")
            or row.get("evToEbitda")
        ),
        "ev_to_sales": _as_float(row.get("evToSales")),
        "current_ratio": _as_float(row.get("currentRatio")),
        "debt_to_equity": _as_float(
            row.get("debtToEquityRatio")
            or row.get("debtEquityRatio")
            or row.get("debtToEquity")
        ),
        # ROE/ROA/ROIC usually absent from /ratios — backfilled from /key-metrics.
        "return_on_equity": _as_float(row.get("returnOnEquity")),
        "return_on_assets": _as_float(row.get("returnOnAssets")),
        "return_on_invested_capital": _as_float(row.get("returnOnInvestedCapital")),
        "gross_margin": _as_float(row.get("grossProfitMargin")),
        "operating_margin": _as_float(row.get("operatingProfitMargin")),
        "net_margin": _as_float(
            row.get("netProfitMargin") or row.get("bottomLineProfitMargin")
        ),
        # fcf_yield / earnings_yield absent from /ratios — from /key-metrics.
        "fcf_yield": _as_float(row.get("freeCashFlowYield")),
        "earnings_yield": _as_float(row.get("earningsYield")),
        "payout_ratio": _as_float(
            row.get("dividendPayoutRatio") or row.get("payoutRatio")
        ),
        "raw": row,
        "known_at": datetime.now(UTC),
    }


# FinancialRatios column -> FMP /key-metrics field. These are the metrics
# /ratios leaves None; /key-metrics is the authoritative source.
_KEY_METRIC_FIELDS = {
    "return_on_equity": "returnOnEquity",
    "return_on_assets": "returnOnAssets",
    "return_on_invested_capital": "returnOnInvestedCapital",
    "fcf_yield": "freeCashFlowYield",
    "earnings_yield": "earningsYield",
}


def _key_metrics_by_date(rows: list[dict[str, Any]]) -> dict[date, dict[str, Any]]:
    """Index /key-metrics rows by fiscal date so they join onto /ratios rows.

    Caller fetches /key-metrics for the same period as /ratios, so fiscal_date
    alone is a safe join key (no annual/quarter collision).
    """
    out: dict[date, dict[str, Any]] = {}
    for row in rows:
        fiscal = _as_date(row.get("date"))
        if fiscal is not None:
            out[fiscal] = row
    return out


def _merge_key_metrics(mapped: dict[str, Any], km_row: dict[str, Any] | None) -> None:
    """Backfill `_KEY_METRIC_FIELDS` (ROE/ROA/ROIC/fcf_yield/earnings_yield)
    from /key-metrics, in place.

    Only fills columns /ratios left None — a non-null /ratios value wins
    (defensive: don't clobber a real value if FMP starts returning these).
    """
    if km_row is None:
        return
    for col, fmp_key in _KEY_METRIC_FIELDS.items():
        if mapped.get(col) is None:
            mapped[col] = _as_float(km_row.get(fmp_key))


# ----- analyst estimates ---------------------------------------------------


def refresh_estimates(
    session: Session,
    client: FMPClient,
    symbols: list[str],
    *,
    periods: tuple[str, ...] = _FINANCIAL_PERIODS,
    only_stale: bool = True,
) -> RefreshResult:
    """Refresh `analyst_estimates` via `session.merge` (estimates revise).

    `only_stale=True` (default since MA-4) narrows `symbols` via the
    `estimates` threshold (`MAX(known_at) > 7d ago`).
    """
    if only_stale:
        symbols = stale_symbols(session, "estimates", symbols)
    outcomes: list[SymbolOutcome] = []
    for sym in symbols:
        upserted = 0
        errs: list[str] = []
        for period in periods:
            try:
                rows = client.get_analyst_estimates(sym, period=period)
            except FMPError as exc:
                errs.append(f"{period}: {exc}")
                continue
            # FMP /analyst-estimates rows don't always carry a `period` field —
            # stamp it from the request param so the PK is well-defined.
            request_period = "FY" if period == "annual" else "Q"
            for r in rows:
                mapped = _estimate_from_fmp(sym, r, request_period)
                if mapped["fiscal_date"] is None:
                    continue
                session.merge(AnalystEstimate(**mapped))
                upserted += 1
        outcomes.append(
            SymbolOutcome(
                sym,
                ok=not errs,
                upserted=upserted,
                skipped=0,
                error="; ".join(errs) if errs else None,
            )
        )
    return RefreshResult(scope="estimates", outcomes=outcomes)


def _estimate_from_fmp(
    symbol: str, row: dict[str, Any], request_period: str
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "fiscal_date": _as_date(row.get("date")),
        "period": row.get("period") or request_period,
        "revenue_avg": _as_float(row.get("estimatedRevenueAvg")),
        "revenue_low": _as_float(row.get("estimatedRevenueLow")),
        "revenue_high": _as_float(row.get("estimatedRevenueHigh")),
        "eps_avg": _as_float(row.get("estimatedEpsAvg")),
        "eps_low": _as_float(row.get("estimatedEpsLow")),
        "eps_high": _as_float(row.get("estimatedEpsHigh")),
        "ebitda_avg": _as_float(row.get("estimatedEbitdaAvg")),
        "net_income_avg": _as_float(row.get("estimatedNetIncomeAvg")),
        "num_analysts_revenue": _as_int(row.get("numberAnalystEstimatedRevenue")),
        "num_analysts_eps": _as_int(row.get("numberAnalystsEstimatedEps")),
        "raw": row,
        "known_at": datetime.now(UTC),
    }


# ----- coercion helpers ----------------------------------------------------


def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_date(v: Any) -> date | None:
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _as_datetime(v: Any) -> datetime | None:
    """Parse FMP `acceptedDate` (commonly "YYYY-MM-DD HH:MM:SS", ET, no tz suffix).

    Returns a tz-aware UTC datetime so it can land in `DateTime(timezone=True)`
    columns. v1 pragmatic shortcut: we attach UTC rather than converting from
    ET — the date component (what point-in-time queries filter on) is what
    matters; a 4-5h offset only matters at midnight ET, rare for SEC filings.
    """
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    s = str(v).strip()
    if not s:
        return None
    for fmt, length in (
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%dT%H:%M:%S", 19),
        ("%Y-%m-%d", 10),
    ):
        if len(s) < length:
            continue
        try:
            return datetime.strptime(s[:length], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    # Fallback: ISO parser handles `2024-11-01T17:23:54+00:00` and similar.
    try:
        parsed = datetime.fromisoformat(s)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None
