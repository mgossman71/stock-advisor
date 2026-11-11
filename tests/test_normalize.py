"""Data-normalisation tests: the conversions that keep every score honest.

Percent-vs-fraction and money-vs-string bugs are the classic way a screener
silently lies, so they are pinned here explicitly.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.ingestion.normalize import (
    bar_from_nasdaq_z, clean_text, is_missing, parse_date, parse_datetime,
    percent_to_points, ratio, split_pair, to_decimal, to_int,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("$348.9607", Decimal("348.9607")),
        ("1,234.50", Decimal("1234.50")),
        ("--", None),
        ("N/A", None),
        ("N/A.", None),
        ("", None),
        ("  ", None),
        ("nan", None),
        ("-- ", None),
        ("(15.50)", Decimal("-15.50")),
        ("12.5%", Decimal("12.5")),
        ("3.4B", Decimal("3400000000")),
        ("1.2M", Decimal("1200000")),
        ("500K", Decimal("500000")),
        ("2.1T", Decimal("2100000000000")),
        ("not-a-number", None),
        ("$-", None),
    ],
)
def test_to_decimal_handles_provider_strings(raw, expected):
    assert to_decimal(raw) == expected


def test_to_decimal_keeps_string_precision_exactly():
    """Provider strings are not rounded - Nasdaq's "$348.9607" stays as written."""
    assert to_decimal("$348.9607") == Decimal("348.9607")
    assert to_decimal("5,064,610,748,820") == Decimal("5064610748820")


def test_to_decimal_strips_float_binary_noise():
    """A Yahoo float bar must not persist 350.69000244140625-style noise."""
    assert to_decimal(350.69000244140625) == Decimal("350.690002")
    assert to_decimal(38.809578999999995) == Decimal("38.809579")


def test_to_decimal_rejects_bools_and_accepts_native_types():
    assert to_decimal(True) is None
    assert to_decimal(Decimal("1.23")) == Decimal("1.23")
    assert to_decimal(7) == Decimal("7")


def test_to_decimal_quantize_when_asked():
    assert to_decimal("1.23456789", quantize="0.01") == Decimal("1.23")


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, True), ("", True), ("--", True), ("--", True), ("n/a", True),
        ("N/A.", True), ("NaN", True), (float("nan"), True), (float("inf"), True),
        ("0", False), (0, False), (Decimal("0"), False), ("348.96", False),
    ],
)
def test_is_missing(raw, expected):
    assert is_missing(raw) is expected


def test_percent_to_points_distinguishes_fractions_from_points():
    """The single most dangerous conversion in the app - pinned both ways."""
    assert percent_to_points(0.117, source_is_fraction=True) == Decimal("11.700")
    assert percent_to_points(0.31, source_is_fraction=False) == Decimal("0.31")
    assert percent_to_points("11.7%", source_is_fraction=False) == Decimal("11.7")
    assert percent_to_points("--", source_is_fraction=True) is None


def test_percent_to_points_does_not_double_scale_already_percent_values():
    """Re-scaling an already-percent number would inflate every value score."""
    dividend_yield = percent_to_points(0.31, source_is_fraction=False)
    assert dividend_yield < 1, "0.31 % must stay 0.31, not become 31"


def test_ratio_passes_multiples_through():
    assert ratio("38.81") == Decimal("38.81")
    assert ratio("--") is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        (dt.date(2026, 1, 5), dt.date(2026, 1, 5)),
        (dt.datetime(2026, 1, 5, 16, 0), dt.date(2026, 1, 5)),
        ("2026-01-05", dt.date(2026, 1, 5)),
        ("2026-01-05T09:30:00", dt.date(2026, 1, 5)),
        ("9/1/2026", dt.date(2026, 9, 1)),
        ("Nov 9, 2026", dt.date(2026, 11, 9)),
        ("not a date", None),
        (None, None),
        ("--", None),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_parse_datetime_assumes_utc_for_naive_input():
    parsed = parse_datetime("2026-01-05T16:00:00")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == dt.timedelta(0)
    assert parse_datetime("2026-01-05T16:00:00+00:00").hour == 16
    assert parse_datetime("garbage") is None


def test_clean_text_strips_html_and_unescapes():
    assert clean_text("<p>Apple &amp; <b>quotes</b></p>") == "Apple & quotes"
    assert clean_text("  spaced \n out  ") == "spaced out"
    assert clean_text("") is None
    assert clean_text("--") is None


def test_clean_text_truncates_to_column_width():
    assert len(clean_text("x" * 900, max_length=400)) == 400


def test_split_pair_parses_nasdaq_range_strings():
    assert split_pair("$355.38/$243.42") == (Decimal("355.38"), Decimal("243.42"))
    assert split_pair("--") == (None, None)
    assert split_pair("355.38") == (None, None)


def test_bar_from_nasdaq_z_maps_strings_and_keeps_missing_volume_null():
    bar = bar_from_nasdaq_z(
        "aapl",
        {"dateTime": "9/1/2026", "open": "$345.00", "high": "$352.33",
         "low": "$344.78", "close": "$350.69", "volume": "48,753,100"},
        "nasdaq",
    )
    assert bar is not None
    assert bar.symbol == "AAPL"
    assert bar.date == dt.date(2026, 9, 1)
    assert bar.close == Decimal("350.69")
    assert bar.volume == 48753100
    # Nasdaq publishes no separate dividend-adjusted close; we record the same
    # number and let `source` say where it came from rather than inventing one.
    assert bar.adj_close == Decimal("350.69")


def test_bar_from_nasdaq_z_drops_rows_without_a_close():
    """A bar we cannot price is dropped, not stored with a guessed close."""
    assert bar_from_nasdaq_z("AAPL", {"dateTime": "9/1/2026", "close": "--"}, "nasdaq") is None
    assert bar_from_nasdaq_z("AAPL", {"close": "350.69", "dateTime": "nope"}, "nasdaq") is None


def test_to_int_rounds_provider_volume_strings():
    assert to_int("29,756,060.192259") == 29756060
    assert to_int(None) is None
