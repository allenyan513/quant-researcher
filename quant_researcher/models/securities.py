"""Master ticker registry.

Slim in MA-1: just enough to anchor foreign references and record when a
symbol first entered our warehouse. MA-2's profile refresh will widen this
(sector / industry / exchange / currency / raw profile JSON, with
`profile_known_at`).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, String, func
from sqlalchemy.orm import Mapped, mapped_column

from quant_researcher.db import Base


class Security(Base):
    __tablename__ = "securities"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
