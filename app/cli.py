"""Command-line entry point for data work (no web server required).

    python -m app.cli fetch AAPL --period 1y
    python -m app.cli fetch-universe --size 60 --with-history
    python -m app.cli stats
    python -m app.cli providers
    python -m app.cli adjust-cash 500 --withdraw --note "rebalance"
    python -m app.cli record-buy AAPL 10 210.50 --rec 7
    python -m app.cli holdings
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from decimal import Decimal

from sqlalchemy import func, select

from app.config import get_settings
from app.db.base import dispose_all, session_scope
from app.db.models import Fundamentals, PriceHistory, Ticker
from app.ingestion.ingest import refresh_symbol
from app.ingestion.stats import coverage_by_ticker, recent_fetch_log, table_counts
from app.ingestion.universe import load_universe, persist_universe
from app.providers.registry import build_provider, provider_names
from app.research.llm_advisor import advisor_for
from app.trading.accounts import (
    CashAdjustmentError,
    adjust_cash,
    get_or_open_account,
)
from app.trading.freshness import ensure_fresh_price
from app.trading.orders import cancel_order, execute_order, latest_price
from app.trading.snapshots import mark_positions, portfolio_values, write_snapshot
from app.tracking import RealTradeError, lot_valuations, record_trade, tax_summary

PERIOD_DAYS = {"3mo": 92, "6mo": 186, "1y": 365, "2y": 730, "5y": 1826}


def _period_to_dates(period: str) -> tuple[dt.date, dt.date]:
    """Translate a period label into an inclusive (start, end) date range."""
    end = dt.date.today()
    if period not in PERIOD_DAYS:
        raise SystemExit(f"unknown period {period!r}; choose from {sorted(PERIOD_DAYS)}")
    return end - dt.timedelta(days=PERIOD_DAYS[period]), end


def cmd_fetch(args: argparse.Namespace) -> int:
    """Fetch history + fundamentals (+ news/analysts/earnings) for one symbol."""
    settings = get_settings()
    provider = build_provider(settings)
    start, end = _period_to_dates(args.period)
    print(f"provider chain: {' -> '.join(provider_names(provider))}")
    print(f"fetching {args.symbol.upper()} {start.isoformat()}..{end.isoformat()}")
    with session_scope() as session:
        outcomes = refresh_symbol(session, provider, args.symbol, start=start, end=end)
        session.commit()
        for capability, stats in outcomes.items():
            print(
                f"  {capability:16s} fetched={stats.fetched:<5d} written={stats.written:<5d} "
                f"skipped={stats.skipped:<5d} {'; '.join(stats.notes)}"
            )
        symbol = args.symbol.upper()
        ticker = session.scalar(select(Ticker).where(Ticker.symbol == symbol))
        if ticker is None:
            print("ticker row was not created")
            return 1
        bars = session.scalar(
            select(func.count(PriceHistory.id)).where(PriceHistory.ticker_id == ticker.id)
        )
        bounds = session.execute(
            select(func.min(PriceHistory.date), func.max(PriceHistory.date)).where(
                PriceHistory.ticker_id == ticker.id
            )
        ).one()
        sources = list(session.scalars(
            select(PriceHistory.source).where(PriceHistory.ticker_id == ticker.id).distinct()
        ))
        print(f"\nticker {ticker.symbol}: {bars} bars, {bounds[0]} .. {bounds[1]}, sources={sources}")
        fundamentals = session.scalar(
            select(Fundamentals).where(Fundamentals.ticker_id == ticker.id)
            .order_by(Fundamentals.as_of_date.desc())
        )
        if fundamentals:
            print(
                f"fundamentals as of {fundamentals.as_of_date} (source={fundamentals.source}): "
                f"pe_trailing={fundamentals.pe_trailing} pe_forward={fundamentals.pe_forward} "
                f"ev_ebitda={fundamentals.ev_ebitda} roe={fundamentals.return_on_equity} "
                f"d/e={fundamentals.debt_to_equity} target={fundamentals.target_price_consensus}"
            )
    return 0


def cmd_fetch_universe(args: argparse.Namespace) -> int:
    """Load the screening universe and (optionally) prime its price history."""
    settings = get_settings()
    result = load_universe(settings, limit=args.size)
    print(f"universe source={result.source} members={len(result.members)}")
    for note in result.notes:
        print(f"  note: {note}")
    if not args.with_history:
        with session_scope() as session:
            print(f"persisted {persist_universe(session, result.members)} tickers")
            session.commit()
        return 0

    provider = build_provider(settings)
    start, end = _period_to_dates(args.period)
    with session_scope() as session:
        print(f"persisted {persist_universe(session, result.members)} tickers")
        session.commit()
        for index, member in enumerate(result.members, start=1):
            outcomes = refresh_symbol(
                session, provider, member.symbol, start=start, end=end, with_alternatives=False
            )
            session.commit()
            history = outcomes["history"]
            print(
                f"  [{index}/{len(result.members)}] {member.symbol:7s} "
                f"bars+{history.written:<4d} skip={history.skipped:<4d} {'; '.join(history.notes)}"
            )
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    """Row counts per table, price coverage, and recent fetch-log entries."""
    with session_scope() as session:
        print("table row counts")
        for name, count in table_counts(session).items():
            print(f"  {name:18s} {count}")

        print("\nprice coverage")
        for row in coverage_by_ticker(session):
            print(f"  {row['symbol']:7s} {row['bars']:5d} bars  {row['first_date']} .. {row['last_date']}")

        print("\nlast 8 fetch-log entries")
        for entry in recent_fetch_log(session, limit=8):
            print(
                f"  {entry['fetched_at']:%Y-%m-%d %H:%M:%S} {str(entry['provider']):9s} "
                f"{str(entry['endpoint']):30s} {str(entry['status']):13s} rows={entry['rows']} "
                f"{entry['error'] or ''}"
            )
    return 0


def cmd_providers(_: argparse.Namespace) -> int:
    """Show the configured provider chain and probe history for two symbols."""
    settings = get_settings()
    provider = build_provider(settings)
    print(f"MARKET_DATA_PROVIDER={settings.market_data_provider}")
    print(f"providers: {' -> '.join(provider_names(provider))}")
    print(f"min interval between calls per host: {settings.min_provider_interval_seconds}s")
    print(f"max retries: {settings.max_retries}")
    end = dt.date.today()
    start = end - dt.timedelta(days=92)
    for symbol in ("AAPL", settings.benchmark_symbol):
        result = provider.get_history(symbol, start, end)
        print(f"  probe {symbol:7s} {result.summary()}")
        if result.ok:
            last = result.data[-1]
            print(f"    last bar {last.date} close={last.close} source={last.source}")
    return 0


def cmd_trade(args: argparse.Namespace) -> int:
    """Execute one paper order (buy/sell/short/cover) and print the outcome."""
    settings = get_settings()
    with session_scope() as session:
        account = get_or_open_account(session, settings)
        if args.fetch:
            provider = build_provider(settings)
            refresh_symbol(session, provider, args.symbol,
                           with_alternatives=False)
            session.commit()
        limit = Decimal(args.limit) if args.limit is not None else None
        ticker = session.scalar(select(Ticker).where(Ticker.symbol == args.symbol.upper()))
        fresh = (ensure_fresh_price(session, ticker, build_provider(settings))
                 if ticker is not None else None)
        if fresh is not None and fresh.refreshed:
            print(f"refreshed from live quote: {fresh.describe()}")
        result = execute_order(session, account, args.symbol, args.side, args.qty,
                               settings=settings, limit_price=limit,
                               price_not_before=(fresh.required_since
                                                 if fresh is not None and fresh.stale_reason
                                                 else None))
        if not result.accepted:
            print(f"order {result.order.status}: {result.reason}")
            if result.order.status == "pending":
                session.commit()  # keep the resting limit; the nightly job retries it
                print(f"order #{result.order.id} rests until filled or "
                      f"`cancel-order {result.order.id}`")
                return 0
            return 1
        order = result.order
        priced = latest_price(session, order.ticker_id)
        print(
            f"{args.side} {args.qty} {args.symbol.upper()} @ {order.fill_price} "
            f"(ref {order.reference_price}, commission {order.commission}) "
            f"as of {priced[1] if priced else '?'}"
        )
        print(f"cash after: {account.cash_balance}")
        _print_portfolio(session, account, settings)
        session.commit()
    return 0


def cmd_cancel_order(args: argparse.Namespace) -> int:
    """Cancel one pending paper limit order."""
    with session_scope() as session:
        try:
            order = cancel_order(session, args.order_id)
        except ValueError as exc:
            print(f"cannot cancel: {exc}")
            return 1
        session.commit()
        print(f"order #{order.id} cancelled")
    return 0


def cmd_portfolio(_: argparse.Namespace) -> int:
    """Show account, open positions and the latest snapshot."""
    settings = get_settings()
    with session_scope() as session:
        account = get_or_open_account(session, settings)
        mark_positions(session, account)
        _print_portfolio(session, account, settings)
        write_snapshot(session, account, dt.date.today(),
                       benchmark_symbol=settings.benchmark_symbol)
        session.commit()
    return 0


def cmd_adjust_cash(args: argparse.Namespace) -> int:
    """Deposit or withdraw paper cash (auditable adjustment, not a trade)."""
    settings = get_settings()
    delta = Decimal(str(args.amount))
    if args.withdraw:
        delta = -abs(delta)
    with session_scope() as session:
        account = get_or_open_account(session, settings)
        try:
            adjust_cash(session, account, delta, note=args.note)
        except CashAdjustmentError as exc:
            print(f"rejected: {exc}")
            return 1
        verb = "deposited" if delta > 0 else "withdrew"
        print(f"{verb} {abs(delta):,.2f} -> cash {account.cash_balance:,.2f}")
        write_snapshot(session, account, dt.date.today(),
                       benchmark_symbol=settings.benchmark_symbol)
        session.commit()
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """Record a real buy/sell you executed at your broker (not a paper fill)."""
    with session_scope() as session:
        try:
            trade = record_trade(
                session, args.symbol, args.side,
                Decimal(args.qty), Decimal(args.price),
                trade_date=dt.date.fromisoformat(args.date) if args.date else None,
                commission=Decimal(args.commission),
                recommendation_id=args.rec, note=args.note,
            )
        except (RealTradeError, ValueError) as exc:  # fromisoformat raises ValueError too
            print(f"rejected: {exc}")
            return 1
        session.commit()
        link = f" (call #{args.rec})" if args.rec else ""
        print(f"recorded {trade.side} {trade.qty} {trade.ticker.symbol} "
              f"@ {trade.price} on {trade.trade_date}{link}")
    return 0


def cmd_holdings(_: argparse.Namespace) -> int:
    """Open real-money lots with gain/loss and estimated tax if sold today."""
    with session_scope() as session:
        today = dt.date.today()
        print(f"open real lots as of {today}:")
        for v in lot_valuations(session, today):
            mark = f"last={v.price} ({v.price_date})" if v.price is not None else "unpriced"
            gain = f"{v.unrealized:+}" if v.unrealized is not None else "?"
            tax = f" est_tax_if_sold={v.est_tax_if_sold}" if v.price is not None else ""
            print(
                f"  {v.symbol:7s} {v.klass:5s} {v.held_days:>4d}d  "
                f"qty={str(v.qty):>8s} cost={v.cost_basis} {mark} "
                f"uPnL={gain}{tax}"
            )
        s = tax_summary(session, today)
        print(
            f"cost_basis={s['cost_basis']} unrealized={s['unrealized']} "
            f"est_tax_if_sold_today={s['est_tax_if_sold_today']} "
            f"est_tax_realized={s['est_tax_realized']} "
            f"(rates ST={s['rates']['short']} LT={s['rates']['long']}; estimate only)"
        )
    return 0


def _print_portfolio(session, account, settings) -> None:
    """Print open positions + equity (shared by trade/portfolio commands)."""
    values = portfolio_values(session, account)
    print(f"positions ({values['cash']} cash):")
    for position in account.positions:
        if position.qty == 0:
            continue
        side = "SHORT" if position.is_short else "LONG "
        print(
            f"  {side} {position.ticker.symbol:7s} qty={str(position.qty):>8s} "
            f"avg={position.avg_cost} last={position.last_price} "
            f"uPnL={position.unrealized_pnl} rPnL={position.realized_pnl}"
        )
    print(
        f"equity={values['total_value']} positions_value={values['positions_value']} "
        f"short_exposure={values['short_exposure']}"
    )


def cmd_research(args: argparse.Namespace) -> int:
    """Fetch (optional) -> score -> persist a full research report for one symbol."""
    settings = get_settings()
    with session_scope() as session:
        if args.refresh:
            provider = build_provider(settings)
            refresh_symbol(session, provider, args.symbol)
            session.commit()
        advisor = advisor_for(session)
        report = advisor.research(session, args.symbol)
        if report is None:
            print(f"no ticker {args.symbol.upper()} in DB - fetch first")
            return 1
        recs = advisor.recommendations_for(session, report)
        session.commit()
        print(f"{report.ticker.symbol} stance={report.overall_stance} "
              f"score={report.score} as_of={report.data_as_of} version={report.model_or_rule_version}")
        if report.payload_json.get("llm_error"):
            print(f"AI analysis failed, rules used: {report.payload_json['llm_error']}")
        print(f"summary: {report.summary}")
        for label, items in (("bull", report.payload_json["bull_points"]),
                             ("bear", report.payload_json["bear_points"]),
                             ("risk", report.payload_json["risks"])):
            for item in items[:6]:
                print(f"  {label}: {item}")
        if report.payload_json["missing_fields"]:
            print(f"  unavailable: {', '.join(report.payload_json['missing_fields'])}")
        print(f"  evidence: {len(report.payload_json['evidence'])} cited data points")
        for rec in recs:
            print(f"  -> {rec.direction.upper()} {rec.ticker.symbol} conf={rec.confidence} "
                  f"entry={rec.entry_price_at_call} stop={rec.stop_price} target={rec.target_price}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Assemble the argparse tree."""
    parser = argparse.ArgumentParser(prog="app.cli", description="Stock advisor data tools")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="fetch one symbol end to end")
    fetch.add_argument("symbol")
    fetch.add_argument("--period", default="1y", choices=sorted(PERIOD_DAYS))
    fetch.set_defaults(func=cmd_fetch)

    universe = sub.add_parser("fetch-universe", help="load the S&P 500 universe")
    universe.add_argument("--size", type=int, default=25)
    universe.add_argument("--period", default="2y", choices=sorted(PERIOD_DAYS))
    universe.add_argument("--with-history", action="store_true", help="also prime price history")
    universe.set_defaults(func=cmd_fetch_universe)

    sub.add_parser("stats", help="row counts + coverage + fetch log").set_defaults(func=cmd_stats)
    sub.add_parser("providers", help="show/probe the provider chain").set_defaults(func=cmd_providers)

    trade = sub.add_parser("trade", help="paper order: buy/sell/short/cover")
    trade.add_argument("side", choices=["buy", "sell", "short", "cover"])
    trade.add_argument("symbol")
    trade.add_argument("qty", type=int)
    trade.add_argument("--fetch", action="store_true",
                       help="refresh market data for the symbol before filling")
    trade.add_argument("--limit", default=None,
                       help="limit price; an unreachable limit rests and is retried nightly")
    trade.set_defaults(func=cmd_trade)

    cancel = sub.add_parser("cancel-order", help="cancel a pending paper limit order")
    cancel.add_argument("order_id", type=int)
    cancel.set_defaults(func=cmd_cancel_order)

    sub.add_parser("portfolio", help="account + open positions + snapshot").set_defaults(
        func=cmd_portfolio
    )

    for name, side in (("record-buy", "buy"), ("record-sell", "sell")):
        rec_cmd = sub.add_parser(name, help="record a real-world trade you executed")
        rec_cmd.add_argument("symbol")
        rec_cmd.add_argument("qty")
        rec_cmd.add_argument("price", help="actual fill price you paid/received")
        rec_cmd.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
        rec_cmd.add_argument("--commission", default="0")
        rec_cmd.add_argument("--rec", type=int, default=None,
                             help="link to the recommendation id that drove this")
        rec_cmd.add_argument("--note", default=None)
        rec_cmd.set_defaults(func=cmd_record, side=side)

    sub.add_parser("holdings",
                   help="real lots + gain/loss + estimated tax if sold today").set_defaults(
        func=cmd_holdings
    )

    adjust = sub.add_parser("adjust-cash",
                            help="deposit cash (or --withdraw) without a trade")
    adjust.add_argument("amount", type=Decimal, help="positive dollar amount")
    adjust.add_argument("--withdraw", action="store_true",
                        help="take cash out instead of depositing")
    adjust.add_argument("--note", default=None)
    adjust.set_defaults(func=cmd_adjust_cash)

    research = sub.add_parser("research", help="score + report one symbol")
    research.add_argument("symbol")
    research.add_argument("--refresh", action="store_true", help="fetch fresh data first")
    research.set_defaults(func=cmd_research)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    finally:
        dispose_all()


if __name__ == "__main__":
    sys.exit(main())

