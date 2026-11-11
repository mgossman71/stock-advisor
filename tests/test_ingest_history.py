"""Ingestion of price history: idempotent upserts, freshness cache, logging.

Re-running the nightly job or hammering "refresh now" must never duplicate rows
or invent prices - that is what these tests lock down.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import func, select

from app.db.models import DataFetchLog, PriceHistory, Ticker
from app.ingestion.ingest import covered_range, ensure_ticker, ingest_history
from tests.conftest import FakeProvider, make_bar

START = dt.date(2026, 1, 2)
MID = START + dt.timedelta(days=1)
END = dt.date(2026, 1, 9)
BARS = [
    make_bar(date=START, close="100.00", open="99", high="101", low="98", volume=1000),
    make_bar(date=MID, close="101.50"),
    make_bar(date=END, close="102.25"),
]


def _provider(**kwargs) -> FakeProvider:
    return FakeProvider(bars=BARS, **kwargs)


def test_ensure_ticker_is_idempotent_and_never_blanks_existing_values(session):
    first = ensure_ticker(session, "aapl", name="Apple Inc.", sector="Technology")
    again = ensure_ticker(session, "AAPL")
    assert first.id == again.id
    assert again.symbol == "AAPL"
    assert again.name == "Apple Inc.", "a later fetch without a name must not wipe it"
    assert session.scalar(select(func.count()).select_from(Ticker)) == 1


def test_ingest_history_writes_bars_and_logs_one_fetch(session):
    stats = ingest_history(session, _provider(), "AAPL", START, END)
    session.flush()
    assert stats.written == 3
    assert {row.date for row in session.scalars(select(PriceHistory))} == {START, MID, END}
    logs = session.scalars(select(DataFetchLog)).all()
    assert len(logs) == 1 and logs[0].status == "ok" and logs[0].row_count == 3


def test_ingest_history_skips_a_range_that_is_already_fresh(session):
    ingest_history(session, _provider(), "AAPL", START, END)
    session.commit()
    second = ingest_history(session, _provider(), "AAPL", START, END)
    session.flush()
    assert second.skipped == 3 and second.written == 0
    assert session.scalar(select(func.count()).select_from(PriceHistory)) == 3, "no duplicates"
    assert session.scalars(select(DataFetchLog.status)).all() == ["ok", "skipped_fresh"]


def test_a_range_that_does_not_reach_end_is_refetched(session):
    """Freshness is relative to `end`: stale coverage triggers a new fetch."""
    ingest_history(session, _provider(), "AAPL", START, END)
    session.commit()
    second = ingest_history(
        session, _provider(), "AAPL", START, END + dt.timedelta(days=10), freshness_days=1
    )
    session.flush()
    assert second.written == 3
    assert session.scalars(select(DataFetchLog.status)).all() == ["ok", "ok"]


def test_reingesting_a_range_updates_bars_in_place(session):
    """A corrected bar replaces the old one instead of piling up a second row."""
    ingest_history(session, _provider(), "AAPL", START, END)
    session.commit()
    revised = [
        make_bar(date=START, close="105.00"),
        make_bar(date=MID, close="101.50"),
        make_bar(date=END, close="102.25"),
    ]
    ingest_history(
        session, FakeProvider(bars=revised, name="nasdaq"), "AAPL",
        START, END + dt.timedelta(days=3), freshness_days=1,
    )
    session.flush()
    assert session.scalar(select(func.count()).select_from(PriceHistory)) == 3
    row = session.scalar(select(PriceHistory).where(PriceHistory.date == START))
    assert row.close == Decimal("105.00"), "a corrected bar must replace the old one"
    assert row.source == "nasdaq"


def test_covered_range_reports_stored_bounds(session):
    ingest_history(session, _provider(), "AAPL", START, END)
    session.flush()
    assert covered_range(session, 1, START, END) == (START, END, 3)


def test_covered_range_is_empty_for_an_unknown_ticker(session):
    assert covered_range(session, 999, START, END) == (None, None, 0)


def test_history_failure_logs_an_error_and_stores_nothing(session):
    provider = FakeProvider(bars=BARS, fail={"history"})
    stats = ingest_history(session, provider, "AAPL", START, END)
    session.flush()
    assert stats.written == 0 and "history unavailable" in stats.notes[0]
    log = session.scalar(select(DataFetchLog))
    assert log.status == "error" and "history down" in (log.error or "")
    assert session.scalar(select(func.count()).select_from(PriceHistory)) == 0


def test_adjusted_close_prefers_adj_and_falls_back_to_close(session):
    """Readers use ``adjusted_close``; storage keeps NULL honest (no copying)."""
    bars = [
        make_bar(date=START, close="100.00", adj_close=Decimal("95.00")),
        make_bar(date=MID, close="101.50"),
    ]
    ingest_history(session, FakeProvider(bars=bars), "AAPL", START, MID)
    session.flush()
    with_adj = session.scalar(select(PriceHistory).where(PriceHistory.date == START))
    without_adj = session.scalar(select(PriceHistory).where(PriceHistory.date == MID))
    assert with_adj.adjusted_close == Decimal("95.00")
    assert without_adj.adj_close is None, "never copy close into adj_close on the way in"
    assert without_adj.adjusted_close == Decimal("101.50"), "display falls back to close"


def _quote(day: dt.date, price: str):
    from app.providers.base import Quote

    return Quote(symbol="AAPL", price=Decimal(price),
                 as_of=dt.datetime(day.year, day.month, day.day, 20, tzinfo=dt.timezone.utc))


def test_a_quote_bar_does_not_make_history_look_fresh(session):
    """Regression: one quote per day used to stop real bars ever being fetched."""
    from app.ingestion.ingest import ingest_quote

    ingest_history(session, _provider(), "AAPL", START, END)
    nxt = END + dt.timedelta(days=1)
    ingest_quote(session, FakeProvider(quote=_quote(nxt, "103")), "AAPL")
    session.commit()
    provisional = session.scalar(select(PriceHistory).where(PriceHistory.date == nxt))
    assert provisional.provisional and provisional.high is None

    full = [*BARS, make_bar(date=nxt, close="103.40", open=Decimal("102"), high=Decimal("104"), low=Decimal("101"),
                            volume=500)]
    stats = ingest_history(session, FakeProvider(bars=full), "AAPL", START, nxt)
    session.flush()
    assert stats.written == 4, "history must be fetched despite the quote row"
    row = session.scalar(select(PriceHistory).where(PriceHistory.date == nxt))
    assert (row.close, row.high, row.provisional) == (Decimal("103.40"), Decimal("104"), False)


def test_a_quote_never_overwrites_a_settled_bar(session):
    """Regression: the post-close quote wiped OHLC from the day's real bar."""
    from app.ingestion.ingest import ingest_quote

    ingest_history(session, _provider(), "AAPL", START, END)
    stats, _ = ingest_quote(session, FakeProvider(quote=_quote(START, "99.10")), "AAPL")
    session.flush()
    row = session.scalar(select(PriceHistory).where(PriceHistory.date == START))
    assert row.close == Decimal("100.00") and row.high == Decimal("101")
    assert not row.provisional and stats.written == 0


def test_ensure_ticker_keeps_an_existing_asset_class(session):
    ensure_ticker(session, "^GSPC", asset_class="index")
    assert ensure_ticker(session, "^GSPC").asset_class == "index"
    assert ensure_ticker(session, "NEW").asset_class == "stock"


def test_a_split_rescales_history_positions_and_calls_once(session):
    """4:1 split: the provider re-serves old bars divided by 4."""
    from app.config import Settings
    from app.db.models import Position, Recommendation, StockSplit
    from app.trading.accounts import get_or_open_account
    from app.trading.orders import execute_order

    days = [dt.date(2026, 3, d) for d in (2, 3, 4, 5, 6)]
    pre = [make_bar(date=d, close="400", high=Decimal("404"), low=Decimal("396"), volume=1000) for d in days[:3]]
    ingest_history(session, FakeProvider(bars=pre), "AAPL", days[0], days[2])
    settings = Settings(initial_cash=Decimal("100000"), slippage_bps=Decimal("0"),
                        commission_per_share=Decimal("0"), min_commission=Decimal("0"))
    account = get_or_open_account(session, settings)
    assert execute_order(session, account, "AAPL", "buy", 10, settings=settings).accepted
    ticker = ensure_ticker(session, "AAPL")
    rec = Recommendation(ticker_id=ticker.id, direction="long", confidence=Decimal("70"),
                         rationale="t", entry_price_at_call=Decimal("400"),
                         stop_price=Decimal("340"), target_price=Decimal("500"),
                         generated_at=dt.datetime(2026, 3, 4, tzinfo=dt.timezone.utc))
    session.add(rec)
    session.commit()

    adjusted = [make_bar(date=d, close="100", high=Decimal("101"), low=Decimal("99"), volume=4000)
                for d in days[:3]]
    post = [make_bar(date=d, close="102", high=Decimal("103"), low=Decimal("101"), volume=4000) for d in days[3:]]
    stats = ingest_history(session, FakeProvider(bars=adjusted + post), "AAPL",
                           days[0], days[-1])
    session.flush()
    assert any(n.startswith("split 4:1") for n in stats.notes)
    (split,) = session.scalars(select(StockSplit)).all()
    assert (split.split_date, split.split_from, split.split_to) == (days[3], 1, 4)

    position = session.scalar(select(Position))
    assert (position.qty, position.avg_cost) == (Decimal("40"), Decimal("100"))
    assert (rec.entry_price_at_call, rec.stop_price, rec.target_price) == (
        Decimal("100"), Decimal("85"), Decimal("125"))
    closes = [r.close for r in session.scalars(select(PriceHistory).order_by(PriceHistory.date))]
    assert closes == [Decimal("100")] * 3 + [Decimal("102")] * 2

    # Re-running is a no-op: the stored history now agrees with the provider.
    again = ingest_history(session, FakeProvider(bars=adjusted + post), "AAPL",
                           days[0], days[-1] + dt.timedelta(days=1))
    assert not any(n.startswith("split") for n in again.notes)
    assert session.scalar(select(func.count()).select_from(StockSplit)) == 1
    assert session.scalar(select(Position)).qty == Decimal("40")


def test_a_single_revised_bar_is_not_mistaken_for_a_split(session):
    ingest_history(session, _provider(), "AAPL", START, END)
    revised = [make_bar(date=START, close="50.00"), BARS[1], BARS[2]]
    stats = ingest_history(session, FakeProvider(bars=revised), "AAPL", START,
                           END + dt.timedelta(days=3))
    assert not any(n.startswith("split") for n in stats.notes)
    assert session.scalar(select(PriceHistory).where(PriceHistory.date == MID)).close \
        == Decimal("101.50")
