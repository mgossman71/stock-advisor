"""Portfolio: positions, order ticket (HTMX), trade history, realised P&L."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.config import get_settings
from app.db.base import session_scope
from app.db.models import CashAdjustment, Order, Position, Ticker
from app.trading.accounts import CashAdjustmentError, adjust_cash, get_or_open_account
from app.trading.orders import execute_order, latest_price
from app.trading.sizing import suggested_size
from app.trading.snapshots import mark_positions, portfolio_values, write_snapshot
from app.web.common import DISCLAIMER, templates

router = APIRouter()


def _positions_context(session, account):
    positions = session.scalars(
        select(Position).where(Position.account_id == account.id, Position.qty != 0)
        .order_by(Position.ticker_id)
    ).all()
    for p in positions:  # ensure ticker is loaded inside the session
        _ = p.ticker.symbol
    return positions


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
        prefill_data = None
        if prefill:
            from app.db.models import Recommendation

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
        closes = {sym: (str(p[0]) if (p := latest_price(session, tid)) else "")
                  for sym, tid in session.execute(
                      select(Ticker.symbol, Ticker.id).order_by(Ticker.symbol))}
        adjustments = session.scalars(
            select(CashAdjustment).where(CashAdjustment.account_id == account.id)
            .order_by(CashAdjustment.created_at.desc()).limit(25)
        ).all()
    return templates.TemplateResponse(
        request=request, name="portfolio.html",
        context={"disclaimer": DISCLAIMER, "account": account, "positions": positions,
                 "values": values, "orders": orders, "prefill": prefill_data,
                 "realized": realized, "symbols": symbols, "closes": closes,
                 "settings": settings, "adjustments": adjustments,
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
        try:
            result = execute_order(
                session, account, symbol, side, Decimal(qty),
                settings=settings, note=note or None, recommendation_id=recommendation_id,
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
