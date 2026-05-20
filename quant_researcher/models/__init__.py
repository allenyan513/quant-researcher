"""SQLAlchemy model package.

Importing this package as a side-effect registers every declarative model
onto `quant_researcher.db.Base.metadata`, so `Base.metadata.create_all` and
`qr db status` see the full schema. Per D11 (no Alembic), additive table /
column changes are picked up by re-running `qr db init`.

MA-1: `universe`, `securities`. MA-2: `profiles`, `daily_prices`. MA-3 will
add financials, ratios, estimates as their FMP payloads are wired in.
"""

from __future__ import annotations

from quant_researcher.models.prices import DailyPrice
from quant_researcher.models.profile import Profile
from quant_researcher.models.securities import Security
from quant_researcher.models.universe import UniverseMember

__all__ = ["DailyPrice", "Profile", "Security", "UniverseMember"]
