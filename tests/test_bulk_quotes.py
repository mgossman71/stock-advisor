"""Bulk quotes: Yahoo batch parsing, chunking, and the settled-close guard."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

from app.config import get_settings
from app.db.models import PriceHistory
from app.ingestion import ingest
from app.ingestion.ingest import ensure_ticker, ingest_quotes
from app.providers.base import ProviderResult, Quote
from app.providers.yfinance_provider import YFinanceProvider
from tests.conftest import FakeProvider

# 2026-11-12 15:47:00 UTC = 10:47 New York
STAMP = 1794501620


class _Response:
    status_code = 200

    def __init__(self, rows):
        self._rows = rows

    def json(self):
        return {"quoteResponse": {"result": self._rows}}


def test_yahoo_batch_parsing(monkeypatch):
    rows = [
        {"symbol": "NVDA", "regularMarketPrice": 164.88, "regularMarketTime": STAMP,
         "regularMarketPreviousClose": 173.73, "currency": "USD", "shortName": "NVIDIA"},
        {"symbol": "DEAD", "regularMarketTime": STAMP},           # no price: skipped
        {"symbol": "^GSPC", "regularMarketPrice": 6800.1, "regularMarketTime": STAMP},
    ]
    seen = {}

    class FakeYfData:
        def get(self, url, params):
            seen.update(url=url, params=params)
            return _Response(rows)

    monkeypatch.setattr("yfinance.data.YfData", FakeYfData)
    provider = YFinanceProvider(get_settings())
    provider.throttler.wait = lambda host: None
    result = provider.get_quotes(["NVDA", "DEAD", "^GSPC"])
    assert seen["url"].endswith("/v7/finance/quote")
    assert seen["params"]["symbols"] == "NVDA,DEAD,^GSPC"
    assert result.ok and [q.symbol for q in result.data] == ["NVDA", "^GSPC"]
    nvda = result.data[0]
    assert nvda.price == D("164.88") and nvda.previous_close == D("173.73")
    assert nvda.as_of.date() == dt.date(2026, 11, 12)          # New York trading day
    assert nvda.as_of.utcoffset() == dt.timedelta(hours=-5)


class _Bulk(FakeProvider):
    def __init__(self, prices):
        super().__init__()
        self.prices, self.chunks = prices, []

    def get_quotes(self, symbols):
        self.chunks.append(list(symbols))
        rows = tuple(Quote(symbol=s, price=D(self.prices[s]), source="fake",
                           as_of=dt.datetime(2026, 11, 10, 15, 47, tzinfo=dt.timezone.utc))
                     for s in symbols if s in self.prices)
        return ProviderResult(self.name, "quotes", ok=bool(rows), data=rows,
                              row_count=len(rows), error=None if rows else "none")


def test_chunks_unknown_symbols_and_settled_closes(session, monkeypatch):
    monkeypatch.setattr(ingest, "QUOTE_CHUNK", 2)
    for symbol in ("AAA", "BBB", "CCC"):
        ensure_ticker(session, symbol)
    settled = ensure_ticker(session, "CCC")
    session.add(PriceHistory(ticker_id=settled.id, date=dt.date(2026, 11, 10),  # past session: final
                             close=D("9"), open=D("8"), source="fake", provisional=False))
    session.flush()
    provider = _Bulk({"AAA": "1", "BBB": "2", "CCC": "3"})
    priced, notes = ingest_quotes(session, provider, ["AAA", "BBB", "CCC", "NOPE"])
    assert provider.chunks == [["AAA", "BBB"], ["CCC"]]        # NOPE isn't stored: not asked
    assert priced == {"AAA", "BBB", "CCC"} and notes == []
    ccc = session.query(PriceHistory).filter_by(ticker_id=settled.id).one()
    assert ccc.close == D("9") and ccc.open == D("8")          # settled close untouched
    aaa = session.query(PriceHistory).filter_by(ticker_id=ensure_ticker(session, "AAA").id).one()
    assert aaa.close == D("1") and aaa.provisional


def test_a_failed_chunk_is_noted_not_fatal(session):
    ensure_ticker(session, "AAA")
    session.flush()
    priced, notes = ingest_quotes(session, _Bulk({}), ["AAA"])
    assert priced == set() and notes and "bulk quotes 1-1" in notes[0]
