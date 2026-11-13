"""Portfolio: positions, order ticket (HTMX), trade history, realised P&L."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.config import get_settings
from app.db.base import session_scope
from app.db.models import (CashAdjustment, DividendPayment, Order, Position,
                           Recommendation, Ticker)
from app.providers.registry import build_provider
from app.trading.accounts import CashAdjustmentError, adjust_cash, get_or_open_account
from app.trading.freshness import ensure_fresh_price, last_session_date
from app.trading.orders import cancel_order, execute_order, latest_price
from app.trading.sizing import suggested_size
from app.trading.snapshots import mark_positions, portfolio_values, write_snapshot
from app.web.common import DISCLAIMER, templates

router = APIRouter()


def order_provider(settings):
    """Provider used to refresh a stale quote before a fill (tests override it)."""
    return build_provider(settings)


def _positions_context(session, account):
    positions = session.scalars(
        select(Position).where(Position.account_id == account.id, Position.qty != 0)
        .order_by(Position.ticker_id)
    ).all()
    for p in positions:  # ensure ticker is loaded inside the session
        _ = p.ticker.symbol
    return positions


def _position_calls(session, positions) -> dict[int, Recommendation]:
    """ticker_id -> newest open call, for the stop/target columns on positions."""
    ids = {p.ticker_id for p in positions}
    if not ids:
        return {}
    calls: dict[int, Recommendation] = {}
    for rec in session.scalars(
        select(Recommendation)
        .where(Recommendation.status == "open", Recommendation.ticker_id.in_(ids))
        .order_by(Recommendation.generated_at.desc(), Recommendation.id.desc())
    ):
        calls.setdefault(rec.ticker_id, rec)  # newest-first: first sighting wins
    return calls


@router.get("/portfolio")
def portfolio_page(request: Request, prefill: int | None = None):
    """`?prefill=<rec_id>` pre-fills the ticket from an open recommendation."""
    settings = get_settings()
    with session_scope() as session:
        account = get_or_open_account(session, settings)
        mark_positions(session, account)
        positions = _positions_context(session, account)
        values = portfolio_values(session, account)
        orders = session.scalars(
            select(Order).where(Order.account_id == account.id)
            .order_by(Order.submitted_at.desc()).limit(50)
        ).all()
        for o in orders:
            _ = o.ticker.symbol
        calls = _position_calls(session, positions)
        prefill_data = None
        if prefill:
            rec = session.get(Recommendation, prefill)
            if rec is not None and rec.status == "open":
                side = "buy" if rec.direction == "long" else "short"
                qty = suggested_size(values["total_value"], rec.entry_price_at_call,
                                     rec.stop_price or rec.entry_price_at_call * Decimal("0.9"),
                                     settings=settings) or 1
                prefill_data = {"symbol": rec.ticker.symbol, "side": side, "qty": qty,
                                "recommendation_id": rec.id,
                                "note": f"execute call #{rec.id}"}
        realized = sorted(
            ((p.ticker.symbol, p.realized_pnl) for p in session.scalars(
                select(Position).where(Position.account_id == account.id,
                                       Position.realized_pnl != 0))),
            key=lambda kv: abs(kv[1]), reverse=True,
        )
        symbols = session.scalars(select(Ticker.symbol).order_by(Ticker.symbol)).all()
        # Latest stored close per ticker: powers the live "est. trade cost"
        # readout on the order ticket (data-close on each <option>).
        closes, closes_asof = {}, {}
        for sym, tid in session.execute(select(Ticker.symbol, Ticker.id)
                                        .order_by(Ticker.symbol)):
            priced = latest_price(session, tid)
            closes[sym] = str(priced[0]) if priced else ""
            closes_asof[sym] = priced[1].isoformat() if priced else ""
        dividends = session.scalars(
            select(DividendPayment).where(DividendPayment.account_id == account.id)
            .order_by(DividendPayment.booked_at.desc()).limit(25)
        ).all()
        for d in dividends:  # touch lazy relationships inside the session
            _ = d.dividend.ticker.symbol
        adjustments = session.scalars(
            select(CashAdjustment).where(CashAdjustment.account_id == account.id)
            .order_by(CashAdjustment.created_at.desc()).limit(25)
        ).all()
    return templates.TemplateResponse(
        request=request, name="portfolio.html",
        context={"disclaimer": DISCLAIMER, "account": account, "positions": positions,
                 "calls": calls, "values": values, "orders": orders, "prefill": prefill_data,
                 "realized": realized, "symbols": symbols, "closes": closes,
                 "settings": settings, "adjustments": adjustments,
                 "closes_asof": closes_asof, "dividends": dividends,
                 "session_date": last_session_date().isoformat(),
                 "data_today": dt.date.today()},
    )


@router.post("/portfolio/order", response_class=HTMLResponse)
def place_order(
    request: Request,
    symbol: str = Form(...),
    side: str = Form(..., pattern="^(buy|sell|short|cover)$"),
    qty: int = Form(..., gt=0),
    note: str | None = Form(None),
    recommendation_id: int | None = Form(None),
):
    """HTMX order ticket submit: returns the refreshed account + positions fragment."""
    settings = get_settings()
    with session_scope() as session:
        account = get_or_open_account(session, settings)
        ticker = session.scalar(select(Ticker).where(Ticker.symbol == symbol.upper()))
        fresh = (ensure_fresh_price(session, ticker, order_provider(settings))
                 if ticker is not None else None)
        try:
            result = execute_order(
                session, account, symbol, side, Decimal(qty),
                settings=settings, note=note or None, recommendation_id=recommendation_id,
                price_not_before=(fresh.required_since
                                  if fresh is not None and fresh.stale_reason else None),
            )
        except ValueError as exc:  # bad qty/side at the arithmetic layer
            return templates.TemplateResponse(
                request=request, name="partials/order_result.html", status_code=422,
                context={"disclaimer": DISCLAIMER, "accepted": False,
                         "reason": str(exc), "account": account, "positions": [],
                         "values": {}, "settings": settings},
            )
        mark_positions(session, account)
        positions = _positions_context(session, account)
        values = portfolio_values(session, account)
        write_snapshot(session, account, dt.date.today(),
                       benchmark_symbol=settings.benchmark_symbol)
        session.commit()
        message = (
            f"PAPER account: filled {side} {qty} {symbol.upper()} @ {result.order.fill_price}"
            f" ({fresh.describe() if fresh else 'stored price'})"
            if result.accepted else f"Order {result.order.status}: {result.reason}"
        )
    return templates.TemplateResponse(
        request=request, name="partials/order_result.html",
        context={"disclaimer": DISCLAIMER, "accepted": result.accepted,
                 "message": message, "reason": result.reason, "account": account,
                 "positions": positions, "values": values, "settings": settings},
    )


@router.post("/portfolio/adjust-cash", response_class=HTMLResponse)
def adjust_cash_route(
    request: Request,
    direction: str = Form(..., pattern="^(deposit|withdraw)$"),
    amount: str = Form(...),
    note: str | None = Form(None),
):
    """HTMX deposit/withdraw submit: same result fragment as the order ticket."""
    settings = get_settings()
    with session_scope() as session:
        account = get_or_open_account(session, settings)

        def _reject(reason: str) -> HTMLResponse:
            return templates.TemplateResponse(
                request=request, name="partials/order_result.html", status_code=422,
                context={"disclaimer": DISCLAIMER, "accepted": False, "message": None,
                         "reason": reason, "account": account, "positions": [],
                         "values": {}, "settings": settings},
            )

        try:
            amt = Decimal(amount)
        except InvalidOperation:
            return _reject("amount must be a number")
        delta = amt if direction == "deposit" else -amt
        try:
            adjust_cash(session, account, delta, note or None)
        except CashAdjustmentError as exc:
            return _reject(str(exc))
        mark_positions(session, account)
        positions = _positions_context(session, account)
        values = portfolio_values(session, account)
        write_snapshot(session, account, dt.date.today(),
                       benchmark_symbol=settings.benchmark_symbol)
        session.commit()
        verb = "Deposit" if delta > 0 else "Withdrawal"
        message = f"{verb} of {abs(delta):,.2f} booked - cash now {values['cash']:,.2f}"
    return templates.TemplateResponse(
        request=request, name="partials/order_result.html",
        context={"disclaimer": DISCLAIMER, "accepted": True, "message": message,
                 "reason": None, "account": account, "positions": positions,
                 "values": values, "settings": settings},
    )


@router.post("/portfolio/orders/{order_id}/cancel")
def cancel_order_route(order_id: int):
    """Withdraw a pending limit order (plain form post -> 303 back)."""
    with session_scope() as session:
        try:
            cancel_order(session, order_id)
        except ValueError:
            pass  # already filled/cancelled: the refreshed page shows its real status
        session.commit()
    return RedirectResponse("/portfolio", status_code=303)
