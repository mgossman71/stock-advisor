"""Universe resolution: live Wikipedia list, checked-in CSV, then config seed.

The order matters: an offline machine must still screen a real universe instead
of silently falling back to 19 seed tickers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.ingestion.universe import (
    UniverseMember, load_universe, normalize_symbol, persist_universe, read_universe_csv,
    universe_symbols, write_universe_csv,
)
from app.db.models import Ticker


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("brk.b", "BRK-B"),        # Yahoo/Nasdaq convention for share classes
        ("BF.B", "BF-B"),
        ("  googl ", "GOOGL"),
        ("Stc.A", "STC-A"),
        ("TE", "TE"),
    ],
)
def test_normalize_symbol_applies_the_yahoo_convention(raw, expected):
    assert normalize_symbol(raw) == expected


def test_csv_round_trip_preserves_sectors(tmp_path: Path):
    members = [
        UniverseMember(symbol="AAPL", name="Apple Inc.", sector="Technology",
                       industry="Consumer Electronics"),
        UniverseMember(symbol="BRK-B", name="Berkshire Hathaway Inc.", sector="Financials",
                       industry="Multi-Securities"),
    ]
    path = tmp_path / "sp500.csv"
    write_universe_csv(members, path)
    assert read_universe_csv(path) == members


def test_read_universe_csv_returns_empty_when_the_file_is_absent(tmp_path: Path):
    assert read_universe_csv(tmp_path / "nope.csv") == []


def test_csv_fallback_is_used_when_the_live_fetch_fails(monkeypatch, tmp_path: Path):
    from app.ingestion import universe as module

    def explode(timeout: float = 20.0) -> list[UniverseMember]:
        raise RuntimeError("wikipedia unreachable")

    monkeypatch.setattr(module, "fetch_sp500_live", explode)
    csv_path = tmp_path / "sp500.csv"
    write_universe_csv([UniverseMember(symbol="AAPL", name="Apple Inc.")], csv_path)

    result = load_universe(limit=10, csv_path=csv_path)
    assert result.source == "csv"
    assert [m.symbol for m in result.members] == ["AAPL"]
    assert any("Wikipedia fetch failed" in note for note in result.notes)


def test_config_seed_is_the_last_resort(monkeypatch, tmp_path: Path):
    from app.ingestion import universe as module

    def explode(timeout: float = 20.0) -> list[UniverseMember]:
        raise OSError("wikipedia unreachable")

    monkeypatch.setattr(module, "fetch_sp500_live", explode)
    result = load_universe(limit=100, csv_path=tmp_path / "absent.csv")
    assert result.source == "config"
    assert "AAPL" in [m.symbol for m in result.members]
    assert any("SEED_SYMBOLS fallback" in note for note in result.notes)


def test_a_live_fetch_rewrites_the_csv(monkeypatch, tmp_path: Path):
    from app.ingestion import universe as module

    csv_path = tmp_path / "sp500.csv"
    members = [UniverseMember(symbol="MMM", name="3M", sector="Industrials")]
    monkeypatch.setattr(module, "fetch_sp500_live", lambda timeout=20.0: members)

    result = load_universe(limit=10, csv_path=csv_path)
    assert result.source == "wikipedia"
    assert read_universe_csv(csv_path) == members


def test_load_universe_honours_the_size_limit(monkeypatch, tmp_path: Path):
    from app.ingestion import universe as module

    many = [UniverseMember(symbol=f"S{index}") for index in range(60)]
    monkeypatch.setattr(module, "fetch_sp500_live", lambda timeout=20.0: many)
    assert len(load_universe(limit=25, csv_path=tmp_path / "sp500.csv").members) == 25


def test_persist_universe_flags_members_and_is_idempotent(session):
    members = [UniverseMember(symbol="AAPL", name="Apple Inc.", sector="Technology")]
    assert persist_universe(session, members) == 1
    persist_universe(session, members)
    ticker = session.scalar(select(Ticker).where(Ticker.symbol == "AAPL"))
    assert ticker.in_universe is True
    assert ticker.sector == "Technology"
    assert session.scalar(select(Ticker).where(Ticker.symbol == "AAPL")).id == ticker.id


def test_universe_symbols_includes_watchlist_and_seed_names(session):
    persist_universe(session, [UniverseMember(symbol="NVDA")])
    from app.ingestion.ingest import ensure_ticker

    ensure_ticker(session, "TSLA", in_watchlist=True)
    symbols = universe_symbols(session)
    assert {"NVDA", "TSLA", "AAPL"} <= set(symbols), "seed symbols are always screened"
