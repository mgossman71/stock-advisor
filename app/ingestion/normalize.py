"""Pure normalisation of provider payloads into ``Decimal``/``date`` values.

Providers hand back wildly inconsistent encodings - ``"$348.9607"``,
``"5,064,610,748,820"``, ``"1.2B"``, ``"355.38/243.42"``, ``"--"``, ``NaN``,
fractions vs percent points.  This module is the single place that converts
them; it is dependency-free and side-effect-free so it can be unit-tested
(``tests/test_normalize.py``).

Hard rule: anything unparseable becomes ``None`` - never ``0``, never a guess.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

#: Strings providers use for "no value".
_MISSING_TOKENS = {
    "", "-", "--", "---", "n/a", "n/a.", "na", "nan", "none", "null",
    "not available", "undefined", "-  -",
}

_SUFFIX_MULTIPLIERS = {
    "k": Decimal(1_000),
    "m": Decimal(1_000_000),
    "b": Decimal(1_000_000_000),
    "bn": Decimal(1_000_000_000),
    "t": Decimal(1_000_000_000_000),
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def is_missing(value: Any) -> bool:
    """True when ``value`` means "the provider gave us nothing"."""
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value) or math.isinf(value)
    if isinstance(value, str):
        return value.strip().lower() in _MISSING_TOKENS
    return False


def _parse_numeric_string(raw: str) -> Decimal | None:
    """Parse a human-formatted number, tolerating $ % commas and B/M suffixes."""
    text = raw.strip()
    if is_missing(text):
        return None
    text = text.replace("$", "").replace(",", "").replace(" ", "")
    if text.endswith("%"):
        text = text[:-1]
    text = text.strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    match = re.fullmatch(r"([-+]?[0-9]*\.?[0-9]+)([a-zA-Z]{1,2})", text)
    try:
        if match:
            number, suffix = match.group(1), match.group(2).lower()
            multiplier = _SUFFIX_MULTIPLIERS.get(suffix)
            if multiplier is None:
                return None
            result = Decimal(number) * multiplier
        else:
            result = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return -result if negative else result


def to_decimal(value: Any, *, quantize: str | None = None) -> Decimal | None:
    """Coerce a scalar to ``Decimal`` or ``None``.

    Accepts ``Decimal``, ``int``, ``float`` and human strings ("$1,234.5",
    "12.5%", "3.4B").  Floats pass through ``str()`` first and are then rounded
    to 6 dp, so binary representation noise (``350.69000244140625`` from a
    Yahoo bar) never reaches the database.  Values that arrive as strings
    (Nasdaq's "$348.9607") are kept exactly as written.
    """
    if is_missing(value):
        return None
    from_float = isinstance(value, float) and not isinstance(value, bool)
    if isinstance(value, Decimal):
        result: Decimal | None = value
    elif isinstance(value, bool):  # bool is an int subclass - treat as unusable
        return None
    else:
        result = _parse_numeric_string(str(value))
    if result is None:
        return None
    if quantize is not None:
        return result.quantize(Decimal(quantize))
    return result.quantize(Decimal("0.000001")) if from_float else result


def to_int(value: Any) -> int | None:
    """Coerce to an ``int`` share count, or ``None`` when unavailable."""
    dec = to_decimal(value)
    return None if dec is None else int(dec.to_integral_value())


def percent_to_points(value: Any, *, source_is_fraction: bool) -> Decimal | None:
    """Normalise a percentage to percent POINTS (``12.5`` == 12.5 %).

    ``source_is_fraction=True`` scales a ratio (0.117 -> 11.7).  When the source
    already reports points (``dividendYield`` 0.31 meaning 0.31 %) it passes
    through untouched.  Getting this wrong silently distorts every score, which
    is why the flag is explicit rather than inferred.
    """
    dec = to_decimal(value)
    if dec is None:
        return None
    return (dec * 100) if source_is_fraction else dec


def percent_to_ratio(value: Any) -> Decimal | None:
    """A percentage rendered as text/points (``"105.58%"``) -> ratio (``1.0558``).

    Nasdaq prints debt/equity this way, while the schema stores it as a ratio.
    """
    dec = to_decimal(value)
    return None if dec is None else dec / 100


def ratio(value: Any) -> Decimal | None:
    """Coerce a unitless multiple (P/E, P/B, beta, D/E) to ``Decimal``."""
    return to_decimal(value)


def parse_date(value: Any) -> dt.date | None:
    """Parse ``date``/``datetime``/ISO/"9/1/2026"/"Nov 9, 2026" into a ``date``."""
    if is_missing(value):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:  # ISO first: pandas Timestamps and FRED both use it
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%b %d, %Y", "%d %b %Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(text[: len(text)], fmt).date()
        except ValueError:
            continue
    return None


def parse_datetime(value: Any) -> dt.datetime | None:
    """Parse into an aware ``datetime`` (UTC assumed when the source is naive)."""
    if is_missing(value):
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, dt.date):
        parsed = dt.datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = None
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = dt.datetime.fromisoformat(text)
            except ValueError:
                return None
    return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed


def clean_text(value: Any, max_length: int = 400) -> str | None:
    """Strip HTML, unescape entities, collapse whitespace, truncate."""
    if is_missing(value):
        return None
    text = _TAG_RE.sub(" ", str(value))
    for entity, char in (("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"),
                         ("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">")):
        text = text.replace(entity, char)
    text = _WS_RE.sub(" ", text).strip()
    if not text:
        return None
    return text[:max_length]


def split_pair(
    value: Any, *, separator: str = "/"
) -> tuple[Decimal | None, Decimal | None]:
    """Split ``"$355.38/$243.42"`` (Nasdaq 52-week high/low) into two Decimals."""
    if is_missing(value):
        return None, None
    parts = str(value).split(separator)
    if len(parts) != 2:
        return None, None
    return to_decimal(parts[0]), to_decimal(parts[1])


def bar_from_nasdaq_z(symbol: str, zone: dict[str, Any], source: str) -> Any | None:
    """Build a :class:`~app.providers.base.Bar` from a Nasdaq chart ``z`` dict.

    A bar without a usable close is dropped rather than kept half-priced.
    """
    from app.providers.base import Bar  # local import keeps this module cheap

    close = to_decimal(zone.get("close")) or to_decimal(zone.get("value"))
    date = parse_date(zone.get("dateTime"))
    if close is None or date is None:
        return None
    return Bar(
        symbol=symbol.upper(),
        date=date,
        close=close,
        open=to_decimal(zone.get("open")),
        high=to_decimal(zone.get("high")),
        low=to_decimal(zone.get("low")),
        # Nasdaq's chart series is split-adjusted; it publishes no separate
        # dividend-adjusted close, so adj_close == close here (documented, not
        # silently faked: the row's `source` column says "nasdaq").
        adj_close=close,
        volume=to_int(zone.get("volume")),
        source=source,
    )

