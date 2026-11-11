"""Seed the demo database with REAL market data (idempotent).

Acceptance criterion #1: ``make dev`` / ``./run.sh`` must produce a DB with at
least 15 tickers carrying >= 1 year of real price history.  This script:

1. opens the paper account,
2. seeds the scoring weights,
3. fetches >= 2 years of real history (+ fundamentals + today's quote) for
   every ``SEED_SYMBOLS`` ticker plus the benchmark and VIX (as indices),
4. marks positions and writes today's portfolio snapshot,
5. researches a handful so the UI has reports to show on first load.

It is safe to re-run: ingestion upserts and skips fresh ranges.  If the
network is down the script still leaves a consistent DB (partial data is
reported, never faked).
"""

from __future__ import annotations

import datetime as dt
import sys

from app.config import get_settings
from app.db.base import dispose_all, session_scope
from app.ingestion.ingest import refresh_symbol
from app.ingestion.universe import load_universe, persist_universe
from app.providers.registry import build_provider
from app.research.engine import RuleBasedAdvisor
from app.research.scoring import ensure_default_weights
from app.trading.accounts import get_or_open_account
from app.trading.snapshots import mark_positions, write_snapshot

RESEARCH_DEPTH = ("AAPL", "MSFT", "NVDA", "JPM", "XOM")  # full pipeline for these


def main() -> int:
    settings = get_settings()
    provider = build_provider(settings)
    end = dt.date.today()
    start = end - dt.timedelta(days=365 * max(2, settings.seed_history_years))
    symbols = settings.seed_symbol_list
    extra = [settings.benchmark_symbol, settings.vix_symbol]
    print(f"seeding {len(symbols)} tickers + {extra} ({start}..{end}) "
          f"provider chain: {' -> '.join(s for s in provider_names_or_all(provider))}")

    with session_scope() as session:
        account = get_or_open_account(session, settings)
        ensure_default_weights(session)
        universe = load_universe(settings, limit=max(20, settings.universe_size))
        count = persist_universe(session, universe.members)
        print(f"universe: source={universe.source} persisted={count}")
        session.commit()

    failures: list[str] = []
    for index, symbol in enumerate(symbols + extra, start=1):
        asset = "index" if symbol in extra else "stock"
        with session_scope() as session:
            try:
                outcomes = refresh_symbol(session, provider, symbol, start=start, end=end,
                                          asset_class=asset, with_alternatives=False)
                session.commit()
                hist = outcomes["history"]
                print(f"  [{index:2d}/{len(symbols) + 1}] {symbol:7s} "
                      f"bars+{hist.written:<5d} skipped={hist.skipped:<5d} {'; '.join(hist.notes)}")
                if hist.written == 0 and hist.skipped == 0:
                    failures.append(symbol)
            except Exception as exc:
                failures.append(symbol)
                print(f"  [{index:2d}] {symbol:7s} FAILED: {exc}")

    with session_scope() as session:
        mark_positions(session, account)
        write_snapshot(session, account, end, benchmark_symbol=settings.benchmark_symbol)
        ensure_default_weights(session)
        session.commit()

    advisor = RuleBasedAdvisor()
    for symbol in RESEARCH_DEPTH:
        try:
            with session_scope() as session:
                report = advisor.research(session, symbol)
                if report is not None:
                    advisor.recommendations_for(session, report)
                session.commit()
            if report is not None:
                print(f"researched {symbol}: {report.overall_stance} {report.score}/100")
        except Exception as exc:
            print(f"research {symbol} failed: {exc}")

    with session_scope() as session:
        seed_real_trades(session, end)
        session.commit()

    print(f"\naccount cash={account.cash_balance} opened id={account.id}")
    if failures:
        print(f"WARNING: {len(failures)} symbol(s) not fetched: {', '.join(failures)}")
        return 0  # partial seed is still a usable demo; caller decides
    return 0


def seed_real_trades(session, end: dt.date) -> None:
    """A few real-money trades so the Investments page has content on first load.

    Prices are the *stored* closes for these tickers (never invented); dates
    are picked ~420+ days back for one lot so the long-term/short-term split
    is visible.  Idempotent: skipped when the ledger already has trades.
    """
    from decimal import Decimal

    from sqlalchemy import func, select

    from app.db.models import PriceHistory, RealTrade, Ticker
    from app.tracking import record_trade

    if session.scalar(select(func.count(RealTrade.id))):
        print("real trades: ledger already populated, skipped")
        return
    seeded: list[str] = []
    for ticker_id in session.scalars(
        select(PriceHistory.ticker_id)
        .where(PriceHistory.date <= end - dt.timedelta(days=420))
        .distinct()
    ).all():
        symbol = session.get(Ticker, ticker_id).symbol
        old = session.scalars(
            select(PriceHistory)
            .where(PriceHistory.ticker_id == ticker_id,
                   PriceHistory.date <= end - dt.timedelta(days=420))
            .order_by(PriceHistory.date.desc()).limit(1)
        ).first()
        recent = session.scalars(
            select(PriceHistory)
            .where(PriceHistory.ticker_id == ticker_id,
                   PriceHistory.date <= end - dt.timedelta(days=35))
            .order_by(PriceHistory.date.desc()).limit(1)
        ).first()
        if old is None or recent is None:
            continue
        record_trade(session, symbol, "buy", Decimal("10"), old.close,
                     trade_date=old.date, note="seeded demo lot")
        record_trade(session, symbol, "buy", Decimal("5"), recent.close,
                     trade_date=recent.date, note="seeded demo lot")
        record_trade(session, symbol, "sell", Decimal("3"), recent.close,
                     trade_date=recent.date, note="seeded demo trim")
        seeded.append(symbol)
        if len(seeded) == 2:
            break
    print(f"seeded real trades for: {', '.join(seeded) or '(nothing suitable)'}")


def provider_names_or_all(provider) -> list[str]:
    from app.providers.registry import provider_names

    return provider_names(provider)


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        dispose_all()
