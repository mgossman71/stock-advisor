"""The ordered "ideas today" list: open calls ranked by confidence.

Ranking rule (documented, on purpose, simple): one entry per ticker - the
*newest* open call wins, because it reflects the latest data - then order by
confidence DESC, newest-first as tiebreaker.  Each idea is tagged with which
ledger already holds the ticker ("real" / "paper" / None) so the list
doubles as "what have I not acted on yet", and carries the risk-based
suggested size from :mod:`app.trading.sizing`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Account, Position, RealTrade, Recommendation
from app.trading.sizing import suggested_size
from app.trading.snapshots import portfolio_values


@dataclass
class RankedIdea:
    rank: int
    rec: Recommendation
    symbol: str
    #: "real" | "paper" | None - which ledger already holds this ticker
    held: str | None
    #: Risk-based quantity for the paper account (None when unpriced/short-call)
    suggested_qty: int | None


def _held_map(session: Session) -> dict[int, str]:
    """ticker_id -> 'real' (wins) or 'paper' for currently-held tickers."""
    held: dict[int, str] = {}
    for tid in session.scalars(
        select(Position.ticker_id).where(Position.qty != 0).distinct()
    ):
        held[tid] = "paper"
    net = session.execute(
        select(
            RealTrade.ticker_id,
            func.sum(case((RealTrade.side == "buy", RealTrade.qty),
                          else_=-RealTrade.qty)),
        ).group_by(RealTrade.ticker_id)
    ).all()
    for tid, qty in net:
        if qty is not None and qty != 0:
            held[tid] = "real"  # a real position outranks the paper tag
    return held


def ranked_ideas(
    session: Session,
    *,
    account: Account | None = None,
    settings: Settings | None = None,
    direction: str | None = None,
    limit: int | None = None,
) -> list[RankedIdea]:
    """Open calls, deduped to newest-per-ticker, ranked by confidence."""
    rows = session.scalars(
        select(Recommendation)
        .where(Recommendation.status == "open")
        .order_by(Recommendation.generated_at.desc(), Recommendation.id.desc())
    ).all()
    if direction:
        rows = [r for r in rows if r.direction == direction]

    newest_per_ticker: list[Recommendation] = []
    seen: set[int] = set()
    for rec in rows:  # rows are newest-first, so first sighting wins per ticker
        if rec.ticker_id in seen:
            continue
        seen.add(rec.ticker_id)
        newest_per_ticker.append(rec)
    newest_per_ticker.sort(key=lambda r: (r.confidence, r.generated_at), reverse=True)
    if limit is not None:
        newest_per_ticker = newest_per_ticker[:limit]

    held = _held_map(session)
    total_value: Decimal | None = None
    if account is not None:
        total_value = portfolio_values(session, account)["total_value"]

    ideas: list[RankedIdea] = []
    for rank, rec in enumerate(newest_per_ticker, start=1):
        qty: int | None = None
        if account is not None and total_value and rec.stop_price:
            qty = suggested_size(total_value, rec.entry_price_at_call,
                                 rec.stop_price, settings=settings)
        ideas.append(RankedIdea(
            rank=rank, rec=rec, symbol=rec.ticker.symbol,
            held=held.get(rec.ticker_id), suggested_qty=qty,
        ))
    return ideas
