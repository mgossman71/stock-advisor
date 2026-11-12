"""Display preferences: how much price history a chart shows.

Analysis always uses all stored history; this only decides what the user
*sees*.  The global default lives in ``app_settings`` (Settings -> Display).
A stock page can override it with ``?range=``, which is never saved: every
fresh visit starts from the global default.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AppSetting

#: key -> (button label, calendar days back from the newest bar; None = all).
RANGES: dict[str, tuple[str, int | None]] = {
    "1m": ("1M", 31), "3m": ("3M", 92), "6m": ("6M", 183), "1y": ("1Y", 365),
    "2y": ("2Y", 730), "3y": ("3Y", 1095), "5y": ("5Y", 1826), "max": ("Max", None),
}
DEFAULT_RANGE = "1y"
SETTING_KEY = "chart_default_range"


def valid_range(key: str | None) -> str | None:
    key = (key or "").strip().lower()
    return key if key in RANGES else None


def load_default_range(session: Session) -> str:
    stored = session.scalar(select(AppSetting.value).where(AppSetting.key == SETTING_KEY))
    return valid_range(stored) or DEFAULT_RANGE


def save_default_range(session: Session, key: str) -> bool:
    """Store a new default (caller commits); False for an unknown key."""
    key = valid_range(key)
    if key is None:
        return False
    row = session.scalar(select(AppSetting).where(AppSetting.key == SETTING_KEY))
    if row is None:
        row = AppSetting(key=SETTING_KEY, value=key)
        session.add(row)
    row.value = key
    row.updated_at = dt.datetime.now(dt.timezone.utc)
    return True


def slice_range(bars: list[tuple[dt.date, Decimal]], key: str) -> list[tuple[dt.date, Decimal]]:
    """Bars within the range, counted back from the newest bar (not today),
    so a stock with stale data still shows a chart."""
    days = RANGES[key][1]
    if days is None or not bars:
        return bars
    start = bars[-1][0] - dt.timedelta(days=days)
    return [bar for bar in bars if bar[0] >= start]
