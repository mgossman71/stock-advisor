"""Shared web plumbing: templates, Jinja filters, small helpers."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR

templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))

DISCLAIMER = "Educational simulation - not financial advice. No real orders are placed."


def _money(value: Decimal | int | str | None, places: str = "0.01") -> str:
    """Render money/ratios with grouping; None renders as an em-dash, never 0."""
    if value is None:
        return "—"
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    quantised = d.quantize(Decimal(places))
    return f"{quantised:,.2f}" if places == "0.01" else f"{quantised:,.4f}".rstrip("0")


def _price(value: Decimal | None) -> str:
    if value is None:
        return "unavailable"
    try:
        return f"{Decimal(str(value)):,.2f}"
    except InvalidOperation:  # undefined/placeholder values render honestly
        return "unavailable"


def _as_of(value: dt.datetime | dt.date | None) -> str:
    if value is None or not isinstance(value, (dt.datetime, dt.date)):
        return "no timestamp"  # also covers Jinja Undefined
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M UTC")
    return value.isoformat()


def _json_safe(obj):
    """Recursively convert Decimal/datetime for JSON payloads in <script> tags."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def register_filters(env) -> None:
    env.filters["money"] = _money
    env.filters["price"] = _price
    env.filters["as_of"] = _as_of


register_filters(templates.env)
