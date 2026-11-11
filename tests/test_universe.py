"""Universe resolution: live Wikipedia list, checked-in CSV, then config seed.

The order matters: an offline machine must still screen a real universe instead
of silently falling back to 19 seed tickers.
"""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import select

from app.ingestion.universe import (
    UniverseMember, load_universe, normalize_symbol, persist_universe, read_universe_csv,
    universe_symbols, write_universe_csv,
)
from app.db.models import Ticker


@pytest.fixture(autouse=True)
def offline_us_listed(monkeypatch, tmp_path: Path):
    """No network: the Nasdaq screener is down unless a test says otherwise,
    and its CSV cache points into the temp dir (never the repo's copy)."""
    from app.ingestion import universe as module

    def down(timeout: float = 30.0):
        raise OSError("nasdaq unreachable")

    monkeypatch.setattr(module, "fetch_us_listed", down)
    monkeypatch.setattr(module, "US_LISTED_CSV_PATH", tmp_path / "us_listed.csv")


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


# ------------------------------------------------------------ US-listed scan

def _row(symbol, name, cap="5e9", price="50", volume="1000000"):
    from app.ingestion.universe import parse_listed_row

    return parse_listed_row({"symbol": symbol, "name": name, "marketCap": cap,
                             "lastsale": f"${price}", "volume": volume,
                             "sector": "Industrials", "industry": "Machinery"})


FLOORS = {"min_market_cap": D("300000000"), "min_price": D("5"),
          "min_dollar_volume": D("1000000")}


def test_filter_keeps_common_stock_above_every_floor():
    from app.ingestion.universe import filter_listed

    rows = [
        _row("BIG", "Big Corp Common Stock", cap="900000000000.00"),
        _row("GEM", "Hidden Gem Inc. Class A Common Stock", cap="450000000"),
        _row("TINY", "Tiny Co Common Stock", cap="100000000"),          # cap floor
        _row("PENNY", "Penny Co Common Stock", price="2.50"),           # price floor
        _row("THIN", "Thin Co Common Stock", volume="1000"),            # $ volume floor
        _row("NOCAP", "Unknown Cap Common Stock", cap=""),              # unknown != big
        _row("ABC^A", "ABC Corp 6% Series A Preferred Stock"),
        _row("XYZW", "XYZ Holdings Warrants"),
        _row("NOTE", "Big Corp 5.875% Notes due 2060"),
        _row("CEF", "Income Opportunities Fund Common Shares of Beneficial Interest"),
        _row("SPAC", "Ares Acquisition Corporation III Class A Ordinary Shares"),
        _row("MLP", "Pipeline Partners L.P. Common Units"),
        _row("ADR", "Abivax SA American Depositary Shares"),
        _row("BRK/B", "Berkshire Hathaway Inc. Class B Common Stock", cap="1e12"),
    ]
    members = filter_listed(rows, **FLOORS)
    symbols = [m.symbol for m in members]
    assert symbols == ["BRK-B", "BIG", "ADR", "GEM"], "largest first, junk and floors out"
    gem = members[3]
    assert gem.name == "Hidden Gem Inc." and gem.market_cap == D("450000000")


def test_live_us_listed_list_wins_and_is_cached(monkeypatch, tmp_path: Path):
    from app.ingestion import universe as module

    monkeypatch.setattr(module, "fetch_us_listed", lambda timeout=30.0: [
        _row("GEM", "Hidden Gem Inc. Common Stock", cap="450000000"),
        _row("TINY", "Tiny Co Common Stock", cap="100000000"),
    ])
    monkeypatch.setattr(module, "fetch_sp500_live",
                        lambda timeout=20.0: pytest.fail("S&P is only a fallback"))
    listed_csv = tmp_path / "listed.csv"
    result = load_universe(listed_csv_path=listed_csv, **FLOORS)
    assert result.source == "nasdaq"
    assert [m.symbol for m in result.members] == ["GEM"]
    assert [m.symbol for m in read_universe_csv(listed_csv)] == ["GEM"]
    assert read_universe_csv(listed_csv)[0].market_cap == D("450000000")


def test_cached_us_listed_csv_beats_the_sp500(monkeypatch, tmp_path: Path):
    from app.ingestion import universe as module

    listed_csv = tmp_path / "listed.csv"
    write_universe_csv([UniverseMember("GEM", market_cap=D("450000000")),
                        UniverseMember("TINY", market_cap=D("100000000"))], listed_csv)
    monkeypatch.setattr(module, "fetch_sp500_live",
                        lambda timeout=20.0: pytest.fail("cached list comes first"))
    result = load_universe(listed_csv_path=listed_csv, **FLOORS)
    assert result.source == "us_listed_csv"
    assert [m.symbol for m in result.members] == ["GEM"], "cached rows still meet the cap floor"


def test_a_size_cap_keeps_the_largest_never_an_alphabetical_slice():
    from app.ingestion.universe import cap_universe

    members = [UniverseMember("AAA", market_cap=D("1e9")), UniverseMember("ZZZ", market_cap=D("9e9")),
               UniverseMember("MMM"), UniverseMember("BBB", market_cap=D("5e9"))]
    assert [m.symbol for m in cap_universe(members, 2)] == ["ZZZ", "BBB"]
    assert cap_universe(members, None) == members


def test_replacing_the_universe_unflags_dropouts_without_deleting(session):
    persist_universe(session, [UniverseMember("OLD"), UniverseMember("KEEP")])
    persist_universe(session, [UniverseMember("KEEP", market_cap=D("5e9")),
                               UniverseMember("NEW")], replace=True)
    flags = {t.symbol: t.in_universe for t in session.query(Ticker)}
    assert flags == {"OLD": False, "KEEP": True, "NEW": True}
    keep = session.scalar(select(Ticker).where(Ticker.symbol == "KEEP"))
    assert keep.market_cap == D("5e9")


def test_a_degraded_fallback_never_shrinks_an_existing_universe(session, monkeypatch):
    from app.ingestion.universe import UniverseResult
    from app.jobs import JobReport, refresh_stock_list
    from app.config import Settings

    persist_universe(session, [UniverseMember(f"S{i}") for i in range(30)])
    monkeypatch.setattr("app.jobs.load_universe", lambda s, **floors: UniverseResult(
        source="config", members=[UniverseMember("AAPL")], notes=["seed"]))
    report = JobReport()
    refresh_stock_list(session, Settings(), report, has_universe=True)
    assert session.query(Ticker).filter(Ticker.in_universe.is_(True)).count() == 30
    assert "stock list kept: config fallback not applied" in report.notes


def test_one_share_class_per_company_the_most_traded():
    from app.ingestion.universe import filter_listed

    rows = [
        _row("GOOG", "Alphabet Inc. Class C Capital Stock", cap="2e12", volume="20000000"),
        _row("GOOGL", "Alphabet Inc. Class A Common Stock", cap="2e12", volume="30000000"),
        _row("BRK/A", "Berkshire Hathaway Inc.", cap="1e12", price="700000", volume="200"),
        _row("BRK/B", "Berkshire Hathaway Inc.", cap="1e12", price="480", volume="4000000"),
    ]
    assert sorted(m.symbol for m in filter_listed(rows, **FLOORS)) == ["BRK-B", "GOOGL"]
