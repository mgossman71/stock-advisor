"""The current price is visible, labelled, and separated from report snapshots.

Reports are snapshots of the price they were written at; a price refresh
updates stored prices, not reports.  These pin that the stock page shows the
*current* stored price prominently and says when the report is out of date -
the NVDA case: report written at 168.89, stock since closed at 173.73.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

from app.db.base import session_scope
from app.db.models import PriceHistory, ResearchReport
from app.ingestion.ingest import ensure_ticker
from app.web.display import PriceNow, price_now
from tests.test_web_pages import client  # noqa: F401 - shared fixture

NOV10, NOV11, NOV12 = dt.date(2026, 11, 10), dt.date(2026, 11, 11), dt.date(2026, 11, 12)


def _seed(*, live: bool = False) -> None:
    with session_scope() as s:
        t = ensure_ticker(s, "NVDA", name="NVIDIA Corporation")
        s.add(PriceHistory(ticker_id=t.id, date=NOV10, close=D("168.89"), source="fake"))
        s.add(PriceHistory(ticker_id=t.id, date=NOV11, close=D("173.73"), source="fake"))
        if live:
            s.add(PriceHistory(ticker_id=t.id, date=NOV12, close=D("163.63"), source="fake",
                               provisional=True,
                               fetched_at=dt.datetime(2026, 11, 12, 15, 33,
                                                      tzinfo=dt.timezone.utc)))
        s.add(ResearchReport(
            ticker_id=t.id, model_or_rule_version="rules-v1", summary="snapshot",
            overall_stance="neutral", score=D("49.390000"),
            payload_json={"evidence": [{"label": "last close", "value": "168.89",
                                        "source": "price_history", "as_of": "2026-11-10"}],
                          "bull_points": [], "bear_points": [], "risks": [], "sub_scores": [],
                          "weights_used": {}, "missing_fields": []}))
        s.commit()


def test_price_now_reads_the_newest_bar_and_previous_close(session):
    t = ensure_ticker(session, "AAA")
    assert price_now(session, t.id) is None
    session.add(PriceHistory(ticker_id=t.id, date=NOV10, close=D("100"), source="fake"))
    session.add(PriceHistory(ticker_id=t.id, date=NOV11, close=D("110"), source="fake"))
    session.flush()
    now = price_now(session, t.id)
    assert (now.price, now.previous_close, now.change, now.live) == (D("110"), D("100"),
                                                                     D("10"), False)
    assert now.change_pct == D("10") and now.label == "close Nov 11"


def test_live_label_is_new_york_time():
    now = PriceNow(price=D("1"), date=NOV12, live=True, previous_close=None,
                   fetched_at=dt.datetime(2026, 11, 12, 15, 33, tzinfo=dt.timezone.utc))
    assert now.label == "live quote 10:33 ET, Nov 12"
    naive = PriceNow(price=D("1"), date=NOV12, live=True, previous_close=None,
                     fetched_at=dt.datetime(2026, 11, 12, 15, 33))  # SQLite: naive = UTC
    assert naive.label == "live quote 10:33 ET, Nov 12"


def test_stock_page_shows_current_price_and_flags_the_stale_report(client):
    _seed()
    html = client.get("/research/NVDA").text
    head = html[html.index("price-now"):html.index("Price history")]
    assert "173.73" in head and "+4.84" in head and "(+2.87%)" in head
    assert "close Nov 11" in head and "prev. close 168.89" in head
    assert "This report was written when NVDA was" in head and "<b>168.89</b>" in head
    assert "+2.9%" in head and "Run new research" in head
    assert "49.4/100" in html  # score no longer shown as 49.390000


def test_live_quote_is_labelled_and_change_is_vs_yesterday(client):
    _seed(live=True)
    head = client.get("/research/NVDA").text
    assert "163.63" in head and "-10.10" in head and "(-5.81%)" in head
    assert "live quote 10:33 ET, Nov 12" in head


def test_no_stale_note_when_the_price_has_not_moved(client):
    _seed()
    with session_scope() as s:
        t = ensure_ticker(s, "NVDA")
        s.query(PriceHistory).filter_by(ticker_id=t.id, date=NOV11).delete()
        s.commit()
    assert "This report was written when" not in client.get("/research/NVDA").text


def test_research_lists_show_last_price(client):
    _seed()
    listing = client.get("/research").text
    latest = listing[listing.index("Latest reports"):]
    assert "Last price" in latest and "173.73" in latest and "+2.9%" in latest
    search = client.get("/research?q=NVDA").text
    assert "173.73" in search[search.index("Stocks matching"):]


# ------------------------------------------------- staleness + live refresh

from app.providers.base import Quote  # noqa: E402
from app.web.display import NEW_YORK, needs_live_quote  # noqa: E402
from tests.conftest import FakeProvider  # noqa: E402


def _at(day: dt.date, hh: int, mm: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=NEW_YORK)


def _pn(day, *, live, fetched):
    return PriceNow(price=D("1"), date=day, live=live, previous_close=None, fetched_at=fetched)


def test_needs_live_quote_while_the_market_is_open():
    thu = NOV12  # a Thursday
    assert needs_live_quote(None, _at(thu, 11))
    assert needs_live_quote(_pn(NOV11, live=False, fetched=_at(NOV11, 17)), _at(thu, 11))
    assert not needs_live_quote(_pn(thu, live=True, fetched=_at(thu, 10, 50)), _at(thu, 11))
    assert needs_live_quote(_pn(thu, live=True, fetched=_at(thu, 10, 40)), _at(thu, 11))


def test_needs_live_quote_when_closed():
    thu, fri, sat = NOV12, dt.date(2026, 11, 13), dt.date(2026, 11, 14)
    settled_fri = _pn(fri, live=False, fetched=_at(fri, 18))
    assert not needs_live_quote(settled_fri, _at(sat, 12))         # weekend, have Friday
    assert needs_live_quote(_pn(thu, live=False, fetched=None), _at(sat, 12))  # missing Fri
    intraday_fri = _pn(fri, live=True, fetched=_at(fri, 14))
    assert needs_live_quote(intraday_fri, _at(sat, 12))            # want Friday's close
    assert not needs_live_quote(_pn(fri, live=True, fetched=_at(fri, 16, 5)), _at(sat, 12))
    # Pre-market Friday: Thursday's close is the latest finished session.
    assert not needs_live_quote(_pn(thu, live=False, fetched=_at(thu, 17)), _at(fri, 8))
    # After Thursday's close, a 15:00 quote is not the close yet.
    assert needs_live_quote(_pn(thu, live=True, fetched=_at(thu, 15)), _at(thu, 17))


def _quote(price: str) -> Quote:
    return Quote(symbol="NVDA", price=D(price), source="fake",
                 as_of=dt.datetime(2026, 11, 12, 15, 47, tzinfo=dt.timezone.utc))


def test_stale_page_asks_for_a_live_price(client, monkeypatch):
    _seed()  # newest stored price: Nov 11 close - stale on any later day
    monkeypatch.setattr("app.web.routes_research.needs_live_quote", lambda now, at=None: True)
    html = client.get("/research/NVDA").text
    assert 'hx-get="/research/NVDA/price" hx-trigger="load"' in html and "updating…" in html


def test_price_endpoint_fetches_stores_and_never_loops(client, monkeypatch):
    _seed()
    monkeypatch.setattr("app.web.routes_research.quote_provider",
                        lambda settings: FakeProvider(quote=_quote("164.42")))
    block = client.get("/research/NVDA/price?force=1").text
    assert "164.42" in block and "-9.31" in block  # vs the Nov 11 close 173.73
    assert 'hx-trigger="load"' not in block and "↻ refresh" in block
    # Stored: the full page and the research list now show it too.
    assert "164.42" in client.get("/research/NVDA").text
    assert "164.42" in client.get("/research").text


def test_quote_failure_keeps_the_stored_price(client, monkeypatch):
    _seed()
    monkeypatch.setattr("app.web.routes_research.quote_provider",
                        lambda settings: FakeProvider(fail={"quote"}))
    block = client.get("/research/NVDA/price?force=1").text
    assert "173.73" in block and "Couldn't fetch a live quote" in block
    assert client.get("/research/NOPE/price").status_code == 404
