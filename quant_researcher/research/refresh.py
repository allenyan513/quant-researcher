"""News ingestion — fills `news_items` from FMP `/news/stock-latest`.

Per-symbol isolation: a 4xx on one symbol doesn't block the rest. FMP's
news endpoint is keyed by symbol in the response, so we trust that and
just dedupe by `(symbol, published_at, url)` (the table's PK).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from quant_researcher.data.fmp import FMPClient, FMPError
from quant_researcher.models.research import NewsItem


@dataclass(frozen=True)
class NewsRefreshResult:
    fetched: int
    inserted: int
    skipped_duplicate: int
    failed: list[dict[str, str]] = field(default_factory=list)


def refresh_news(
    session: Session,
    client: FMPClient,
    symbols: list[str],
    *,
    limit: int = 50,
) -> NewsRefreshResult:
    """Fetch recent news for `symbols` (one FMP call) and append to `news_items`."""
    if not symbols:
        return NewsRefreshResult(fetched=0, inserted=0, skipped_duplicate=0)
    try:
        rows = client.get_news(symbols, limit=limit)
    except FMPError as exc:
        return NewsRefreshResult(
            fetched=0,
            inserted=0,
            skipped_duplicate=0,
            failed=[{"symbols": ",".join(symbols), "error": str(exc)}],
        )

    parsed: list[dict[str, Any]] = []
    for raw in rows:
        mapped = _news_from_fmp(raw)
        if (
            not mapped["symbol"]
            or mapped["published_at"] is None
            or not mapped["url"]
        ):
            continue
        parsed.append(mapped)

    if not parsed:
        return NewsRefreshResult(fetched=len(rows), inserted=0, skipped_duplicate=0)

    # Normalize tz before tuple compare — SQLite strips tz on read, Postgres
    # keeps it, so without this the dedup tuple never matches an existing row.
    def _key(sym: str, pa: datetime | None, url: str) -> tuple[str, datetime | None, str]:
        if pa is None:
            return (sym, None, url)
        naive = pa.astimezone(UTC).replace(tzinfo=None) if pa.tzinfo else pa
        return (sym, naive, url)

    incoming_keys = {_key(p["symbol"], p["published_at"], p["url"]) for p in parsed}
    existing = {
        _key(sym, pa, url)
        for sym, pa, url in session.execute(
            select(NewsItem.symbol, NewsItem.published_at, NewsItem.url).where(
                NewsItem.url.in_({k[2] for k in incoming_keys})
            )
        )
    }
    new_rows = [
        p
        for p in parsed
        if _key(p["symbol"], p["published_at"], p["url"]) not in existing
    ]
    if new_rows:
        session.execute(insert(NewsItem), new_rows)
    return NewsRefreshResult(
        fetched=len(rows),
        inserted=len(new_rows),
        skipped_duplicate=len(parsed) - len(new_rows),
    )


def _news_from_fmp(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": row.get("symbol") or "",
        "published_at": _as_datetime(row.get("publishedDate") or row.get("date")),
        "url": row.get("url") or "",
        "headline": row.get("title") or row.get("headline") or None,
        "source": row.get("site") or row.get("source") or row.get("publisher") or None,
        "summary": row.get("text") or row.get("summary") or None,
        "image_url": row.get("image") or row.get("imageUrl") or None,
        "raw": row,
    }


def _as_datetime(v: Any) -> datetime | None:
    if not v:
        return None
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
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None
