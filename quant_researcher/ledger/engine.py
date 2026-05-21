"""Decision ledger orchestration.

Three entry points:

* `record_decision(...)` — write a Decision row + auto-bundle the warehouse
  state (so the snapshot is reproducible). Returns the new decision id.
* `track_decisions(...)` — for every Decision, compute forward returns at
  1w/1m/3m/6m horizons that have elapsed since `opened_at`. Looks up the
  symbol's price + SPY + sector ETF in `daily_prices`. Uses
  `session.merge` so re-runs idempotently overwrite tracking rows.
* `scorecard(...)` — group decisions + their latest tracking by confidence /
  sector / tag and emit average alpha + count.

Returns are computed as `(price_at_T - price_at_open) / price_at_open`, with
sign flipped for short ("sell") decisions so a falling stock counts as
positive return.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant_researcher.contract import code_version
from quant_researcher.ledger.sectors import MARKET_ETF, etf_for_sector
from quant_researcher.models.decisions import Decision, DecisionTracking
from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile
from quant_researcher.research.bundler import bundle as build_and_persist_bundle

HORIZON_DAYS: dict[str, int] = {"1w": 7, "1m": 30, "3m": 91, "6m": 182}
_VALID_SIDES = ("buy", "sell")
_VALID_GROUP_BY = ("confidence", "sector", "tag")


@dataclass(frozen=True)
class RecordResult:
    decision_id: str
    bundle_id: str | None
    price_at_open: float | None
    sector_at_open: str | None


def record_decision(
    session: Session,
    *,
    symbol: str,
    side: str,
    thesis: str | None = None,
    confidence: int | None = None,
    tags: list[str] | None = None,
    opened_at: date | None = None,
    auto_bundle: bool = True,
) -> RecordResult:
    """Append a Decision row. Auto-snapshots the warehouse via `research.bundler.bundle`."""
    if side not in _VALID_SIDES:
        raise ValueError(f"side must be one of {_VALID_SIDES}, got {side!r}")
    opened = opened_at or date.today()

    # Snapshot the warehouse state at decision time → research_bundles row.
    bundle_id: str | None = None
    if auto_bundle:
        bundle_id, _payload = build_and_persist_bundle(
            session, symbol, save=True
        )

    # Use the close on (or just before) `opened` — not "latest known" — so
    # forward-return math anchors on the actual day the decision was made.
    # Falls back to latest_close only when no bar <= opened exists.
    price = _price_at_or_before(session, symbol, opened) or _latest_close(
        session, symbol
    )
    sector = session.scalar(
        select(Profile.sector).where(Profile.symbol == symbol)
    )

    decision_id = str(uuid.uuid4())
    session.add(
        Decision(
            decision_id=decision_id,
            symbol=symbol,
            side=side,
            opened_at=opened,
            price_at_open=price,
            thesis=thesis,
            confidence=confidence,
            tags=tags or None,
            sector_at_open=sector,
            bundle_id=bundle_id,
            code_version=code_version(),
        )
    )
    return RecordResult(
        decision_id=decision_id,
        bundle_id=bundle_id,
        price_at_open=price,
        sector_at_open=sector,
    )


@dataclass(frozen=True)
class TrackResult:
    decisions_touched: int
    rows_written: int
    rows_skipped_horizon_not_elapsed: int


def track_decisions(
    session: Session,
    *,
    as_of: date | None = None,
    decision_ids: list[str] | None = None,
) -> TrackResult:
    """Compute forward returns for each Decision × eligible horizon."""
    today = as_of or date.today()
    stmt = select(Decision)
    if decision_ids:
        stmt = stmt.where(Decision.decision_id.in_(decision_ids))
    decisions = list(session.scalars(stmt))
    rows_written = 0
    rows_skipped = 0
    for d in decisions:
        for horizon, n_days in HORIZON_DAYS.items():
            target_date = d.opened_at + timedelta(days=n_days)
            if target_date > today:
                rows_skipped += 1
                continue
            tracking = _compute_tracking(session, d, horizon, target_date)
            session.merge(tracking)
            rows_written += 1
    return TrackResult(
        decisions_touched=len(decisions),
        rows_written=rows_written,
        rows_skipped_horizon_not_elapsed=rows_skipped,
    )


def _compute_tracking(
    session: Session, d: Decision, horizon: str, target_date: date
) -> DecisionTracking:
    # Use a recency window: a bar more than ~10 days stale doesn't reflect
    # the price "at" the target date (delisting, missing snapshots, etc.) —
    # treat it as None so the row shows missing instead of fake data.
    sym_price = _price_near_date(session, d.symbol, target_date)
    spy_price_open = _price_near_date(session, MARKET_ETF, d.opened_at)
    spy_price_target = _price_near_date(session, MARKET_ETF, target_date)
    sector_etf = etf_for_sector(d.sector_at_open)
    sector_open = (
        _price_near_date(session, sector_etf, d.opened_at)
        if sector_etf != MARKET_ETF
        else None
    )
    sector_target = (
        _price_near_date(session, sector_etf, target_date)
        if sector_etf != MARKET_ETF
        else None
    )

    sign = -1.0 if d.side == "sell" else 1.0
    return_pct = _safe_return(d.price_at_open, sym_price, sign)
    spy_return_pct = _safe_return(spy_price_open, spy_price_target, 1.0)
    sector_return_pct = (
        _safe_return(sector_open, sector_target, 1.0)
        if sector_etf != MARKET_ETF
        else None
    )

    # Alpha = symbol - sector benchmark when we have a sector ETF;
    # else symbol - SPY.
    bench = sector_return_pct if sector_return_pct is not None else spy_return_pct
    alpha = (
        return_pct - bench if return_pct is not None and bench is not None else None
    )

    return DecisionTracking(
        decision_id=d.decision_id,
        horizon=horizon,
        tracked_at=target_date,
        price=sym_price,
        return_pct=return_pct,
        spy_return_pct=spy_return_pct,
        sector_etf=sector_etf if sector_etf != MARKET_ETF else None,
        sector_return_pct=sector_return_pct,
        alpha_pct=alpha,
        extras={
            "side": d.side,
            "sym_price_open": d.price_at_open,
            "spy_open": spy_price_open,
            "spy_target": spy_price_target,
            "sector_open": sector_open,
            "sector_target": sector_target,
            "computed_at": datetime.now(UTC).isoformat(),
        },
        updated_at=datetime.now(UTC),
    )


@dataclass(frozen=True)
class ScorecardRow:
    group: str
    decision_count: int
    tracked_count: int
    avg_return_pct: float | None
    avg_alpha_pct: float | None
    median_alpha_pct: float | None


def scorecard(
    session: Session,
    *,
    group_by: str = "confidence",
    horizon: str = "1m",
) -> list[dict[str, Any]]:
    """Aggregate decisions by chosen dimension at one horizon → list of dicts.

    `group_by` ∈ {confidence, sector, tag}. `horizon` ∈ HORIZON_DAYS keys.
    """
    if group_by not in _VALID_GROUP_BY:
        raise ValueError(f"group_by must be in {_VALID_GROUP_BY}, got {group_by!r}")
    if horizon not in HORIZON_DAYS:
        raise ValueError(
            f"horizon must be in {tuple(HORIZON_DAYS)}, got {horizon!r}"
        )

    # Join Decision + its tracking row at this horizon.
    rows = session.execute(
        select(Decision, DecisionTracking)
        .join(
            DecisionTracking,
            (Decision.decision_id == DecisionTracking.decision_id)
            & (DecisionTracking.horizon == horizon),
            isouter=True,
        )
    ).all()

    groups: dict[str, list[tuple[Decision, DecisionTracking | None]]] = defaultdict(list)
    for d, t in rows:
        keys = _keys_for_group(d, group_by)
        for k in keys:
            groups[k].append((d, t))

    result: list[ScorecardRow] = []
    for key, items in groups.items():
        returns = [t.return_pct for _d, t in items if t and t.return_pct is not None]
        alphas = [t.alpha_pct for _d, t in items if t and t.alpha_pct is not None]
        result.append(
            ScorecardRow(
                group=key,
                decision_count=len(items),
                tracked_count=sum(1 for _d, t in items if t is not None),
                avg_return_pct=sum(returns) / len(returns) if returns else None,
                avg_alpha_pct=sum(alphas) / len(alphas) if alphas else None,
                median_alpha_pct=_median(alphas) if alphas else None,
            )
        )
    result.sort(
        key=lambda r: (r.avg_alpha_pct if r.avg_alpha_pct is not None else -9e9),
        reverse=True,
    )
    return [
        {
            "group": r.group,
            "decision_count": r.decision_count,
            "tracked_count": r.tracked_count,
            "avg_return_pct": r.avg_return_pct,
            "avg_alpha_pct": r.avg_alpha_pct,
            "median_alpha_pct": r.median_alpha_pct,
        }
        for r in result
    ]


def _keys_for_group(d: Decision, group_by: str) -> list[str]:
    if group_by == "confidence":
        return [str(d.confidence) if d.confidence is not None else "unrated"]
    if group_by == "sector":
        return [d.sector_at_open or "unknown"]
    if group_by == "tag":
        return list(d.tags) if d.tags else ["untagged"]
    return ["?"]


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    mid = len(s) // 2
    if len(s) % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def _latest_close(session: Session, symbol: str) -> float | None:
    return session.scalar(
        select(DailyPrice.close)
        .where(DailyPrice.symbol == symbol)
        .order_by(DailyPrice.trade_date.desc())
        .limit(1)
    )


def _price_at_or_before(
    session: Session, symbol: str, on: date
) -> float | None:
    """Most recent close <= `on` for `symbol`. None if no bar exists."""
    return session.scalar(
        select(DailyPrice.close)
        .where(DailyPrice.symbol == symbol, DailyPrice.trade_date <= on)
        .order_by(DailyPrice.trade_date.desc())
        .limit(1)
    )


_PRICE_STALENESS_DAYS = 3  # weekend / single-holiday buffer; bigger gaps = data issue


def _price_near_date(
    session: Session,
    symbol: str,
    target: date,
    *,
    max_staleness_days: int = _PRICE_STALENESS_DAYS,
) -> float | None:
    """Most recent close <= `target` AND within `max_staleness_days` of it.

    Used by tracker so a bar from months ago doesn't pretend to be "the
    price at target" — that produces fake zero returns. Default 10-day
    window covers weekends + market holidays comfortably.
    """
    row = session.execute(
        select(DailyPrice.trade_date, DailyPrice.close)
        .where(DailyPrice.symbol == symbol, DailyPrice.trade_date <= target)
        .order_by(DailyPrice.trade_date.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    bar_date, close = row
    if (target - bar_date).days > max_staleness_days:
        return None
    return close


def _safe_return(start: float | None, end: float | None, sign: float) -> float | None:
    if start is None or end is None or start == 0:
        return None
    return sign * (end / start - 1)
