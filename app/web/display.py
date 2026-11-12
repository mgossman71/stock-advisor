"""Display helpers: chart range preferences and the "current price" readout.

Analysis always uses all stored history; this only decides what the user
*sees*.  The global default lives in ``app_settings`` (Settings -> Display).
A stock page can override it with ``?range=``, which is never saved: every
fresh visit starts from the global default.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AppSetting, PriceHistory, ResearchReport

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


# ------------------------------------------------------------ current price

NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class PriceNow:
    """The newest stored price and how it compares with the previous close."""

    price: Decimal
    date: dt.date
    #: True = an intraday quote (replaced by the real bar after the close).
    live: bool
    fetched_at: dt.datetime | None
    previous_close: Decimal | None

    @property
    def change(self) -> Decimal | None:
        return None if self.previous_close is None else self.price - self.previous_close

    @property
    def change_pct(self) -> Decimal | None:
        if not self.previous_close:
            return None
        return (self.price / self.previous_close - 1) * 100

    @property
    def label(self) -> str:
        """"live quote 10:33 ET, Nov 12" / "close Nov 11"."""
        day = self.date.strftime("%b %-d")
        if self.live and self.fetched_at is not None:
            fetched = self.fetched_at if self.fetched_at.tzinfo else \
                self.fetched_at.replace(tzinfo=dt.timezone.utc)
            return f"live quote {fetched.astimezone(NEW_YORK):%H:%M} ET, {day}"
        return f"{'live quote' if self.live else 'close'} {day}"


def price_now(session: Session, ticker_id: int) -> PriceNow | None:
    """Newest stored price (intraday quote or settled close) + previous close."""
    rows = session.execute(
        select(PriceHistory.date, PriceHistory.close, PriceHistory.provisional,
               PriceHistory.fetched_at)
        .where(PriceHistory.ticker_id == ticker_id, PriceHistory.close.isnot(None))
        .order_by(PriceHistory.date.desc()).limit(2)).all()
    if not rows:
        return None
    last = rows[0]
    return PriceNow(price=last.close, date=last.date, live=bool(last.provisional),
                    fetched_at=last.fetched_at,
                    previous_close=rows[1].close if len(rows) > 1 else None)


def report_price(report: ResearchReport) -> tuple[Decimal, str | None] | None:
    """The price a report was written at: its cited "last close" evidence
    (value, date).  Reports are snapshots - this is what their numbers and
    the AI's text are based on."""
    for row in (report.payload_json or {}).get("evidence", []):
        if row.get("label") == "last close" and row.get("value") not in (None, "None"):
            try:
                return Decimal(str(row["value"])), row.get("as_of")
            except ArithmeticError:
                return None
    return None

