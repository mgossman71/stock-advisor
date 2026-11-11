"""Ingestion of news, analyst actions, earnings, and the full refresh pass."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import func, select

from app.db.models import AnalystAction, EarningsEvent, NewsItem, PriceHistory
from app.ingestion.ingest import (
    ingest_analyst_actions, ingest_earnings, ingest_news, refresh_symbol,
)
from app.providers.base import (
    AnalystAction as AnalystPayload, EarningsEvent as EarningsPayload,
    FundamentalsSnapshot, NewsHeadline, Quote,
)
from tests.conftest import FakeProvider, make_bar

NOW = dt.datetime(2026, 1, 9, 21, 0, tzinfo=dt.timezone.utc)
RECENT = dt.date.today() - dt.timedelta(days=5)


def test_news_dedupes_on_url(session):
    headlines = [
        NewsHeadline(symbol="AAPL", title="A", url="https://x/1"),
        NewsHeadline(symbol="AAPL", title="B", url="https://x/2"),
    ]
    ingest_news(session, FakeProvider(news=headlines), "AAPL")
    session.commit()
    again = ingest_news(session, FakeProvider(news=headlines), "AAPL")
    session.flush()
    assert again.skipped == 2
    assert session.scalar(select(func.count()).select_from(NewsItem)) == 2


def test_unmeasured_news_sentiment_is_stored_as_null(session):
    """No provider hands us a sentiment score, so the column must stay NULL."""
    ingest_news(session, FakeProvider(news=[
        NewsHeadline(symbol="AAPL", title="A", url="https://x/1", sentiment_score=None)
    ]), "AAPL")
    session.flush()
    assert session.scalar(select(NewsItem)).sentiment_score is None


def test_news_headlines_without_a_url_are_dropped(session):
    """A headline we cannot cite is useless for evidence, so it is discarded."""
    provider = FakeProvider(news=[NewsHeadline(symbol="AAPL", title="No link", url="")])
    stats = ingest_news(session, provider, "AAPL")
    session.flush()
    assert stats.written == 0


def test_analyst_actions_outside_the_lookback_are_not_stored(session):
    actions = [
        AnalystPayload(symbol="AAPL", action_date=RECENT, action="upgrade", firm="Firm A"),
        AnalystPayload(symbol="AAPL",
                       action_date=dt.date.today() - dt.timedelta(days=400),
                       action="upgrade", firm="Firm B"),
    ]
    stats = ingest_analyst_actions(session, FakeProvider(actions=actions), "AAPL")
    session.flush()
    assert stats.written == 1 and stats.skipped == 1


def test_duplicate_analyst_actions_are_deduped(session):
    action = AnalystPayload(symbol="AAPL", action_date=RECENT, action="target_raise",
                            firm="Firm A", new_target=Decimal("380"))
    ingest_analyst_actions(session, FakeProvider(actions=[action]), "AAPL")
    session.commit()
    again = ingest_analyst_actions(session, FakeProvider(actions=[action]), "AAPL")
    session.flush()
    assert session.scalar(select(func.count()).select_from(AnalystAction)) == 1
    assert again.skipped == 1


def test_earnings_keep_estimates_and_actuals_separately(session):
    events = [
        EarningsPayload(symbol="AAPL", event_date=dt.date(2026, 1, 28),
                        eps_estimate=Decimal("2.91"), eps_actual=None, status="scheduled"),
        EarningsPayload(symbol="AAPL", event_date=dt.date(2025, 11, 2),
                        eps_estimate=Decimal("1.98"), eps_actual=Decimal("2.10"),
                        surprise_pct=Decimal("5.8"), status="reported"),
    ]
    stats = ingest_earnings(session, FakeProvider(earnings=events), "AAPL")
    session.flush()
    assert stats.written == 2
    rows = {row.event_date: row for row in session.scalars(select(EarningsEvent))}
    upcoming = rows[dt.date(2026, 1, 28)]
    assert upcoming.eps_actual is None and upcoming.status == "scheduled"
    assert rows[dt.date(2025, 11, 2)].eps_actual == Decimal("2.10")


def test_earnings_rerun_updates_rather_than_duplicates(session):
    event = EarningsPayload(symbol="AAPL", event_date=dt.date(2026, 1, 28),
                            eps_estimate=Decimal("2.91"), status="scheduled")
    ingest_earnings(session, FakeProvider(earnings=[event]), "AAPL")
    session.commit()
    reported = EarningsPayload(symbol="AAPL", event_date=dt.date(2026, 1, 28),
                               eps_estimate=Decimal("2.91"), eps_actual=Decimal("3.01"),
                               surprise_pct=Decimal("3.4"), status="reported")
    ingest_earnings(session, FakeProvider(earnings=[reported]), "AAPL")
    session.flush()
    assert session.scalar(select(func.count()).select_from(EarningsEvent)) == 1
    row = session.scalar(select(EarningsEvent))
    assert row.eps_actual == Decimal("3.01") and row.status == "reported"


def test_refresh_symbol_covers_every_capability(session):
    provider = FakeProvider(
        bars=[make_bar(date=dt.date(2026, 1, 9), close="102.25")],
        quote=Quote(symbol="AAPL", price=Decimal("102.25"), as_of=NOW),
        fundamentals=FundamentalsSnapshot(symbol="AAPL", as_of_date=dt.date(2026, 1, 9)),
        news=[NewsHeadline(symbol="AAPL", title="A", url="https://x/1")],
        actions=[AnalystPayload(symbol="AAPL", action_date=RECENT, action="upgrade",
                                firm="Firm A")],
        earnings=[EarningsPayload(symbol="AAPL", event_date=dt.date(2026, 1, 28),
                                  eps_estimate=Decimal("2.91"))],
    )
    outcomes = refresh_symbol(
        session, provider, "AAPL", start=dt.date(2026, 1, 2), end=dt.date(2026, 1, 9)
    )
    session.commit()
    assert set(outcomes) == {
        "history", "fundamentals", "quote", "news", "analyst_actions", "earnings"
    }
    assert outcomes["history"].written == 1
    assert session.scalar(select(func.count()).select_from(PriceHistory)) == 1
    assert session.scalar(select(func.count()).select_from(NewsItem)) == 1
    assert session.scalar(select(func.count()).select_from(AnalystAction)) == 1
    assert session.scalar(select(func.count()).select_from(EarningsEvent)) == 1
