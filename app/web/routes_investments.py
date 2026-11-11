"""My Investments: real purchases made off the advice, gain/loss by holding
period, and estimated tax liability.  Separate from the paper portfolio on
purpose - these rows are facts about real money, not simulated fills."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import RealTrade, Recommendation, Ticker
from app.tracking import RealTradeError, lot_valuations, record_trade, tax_summary
from app.tracking.taxes import TaxRateError, get_tax_rates, set_tax_rates
from app.trading.orders import latest_price
from app.web.common import DISCLAIMER, templates

router = APIRouter()


def _back(**params: str) -> RedirectResponse:
    """303 to the page with a flash/err message, safely query-encoded."""
    return RedirectResponse(f"/investments?{urlencode(params)}", status_code=303)


@router.get("/investments")
def investments_page(request: Request, rec: int | None = None,
                     qty: int | None = None):
    """`?rec=<id>&qty=N` pre-fills the ticket from a recommendation."""
    with session_scope() as session:
        today = dt.date.today()
        lots = lot_valuations(session, today)
        summary = tax_summary(session, today)
        trades = session.scalars(
            select(RealTrade).order_by(RealTrade.trade_date.desc(), RealTrade.id.desc())
            .limit(100)
        ).all()
        for t in trades:
            _ = t.ticker.symbol
        rates = get_tax_rates(session)
        symbols = session.scalars(select(Ticker.symbol).order_by(Ticker.symbol)).all()
        # Latest stored close per ticker for the live "trade cost" readout.
        closes = {sym: (str(p[0]) if (p := latest_price(session, tid)) else "")
                  for sym, tid in session.execute(
                      select(Ticker.symbol, Ticker.id).order_by(Ticker.symbol))}
        prefill = None
        if rec:
            call = session.get(Recommendation, rec)
            if call is not None:
                priced = latest_price(session, call.ticker_id)
                prefill = {
                    "symbol": call.ticker.symbol,
                    "side": "buy",  # v1 real ledger is long-only; shorts stay paper
                    "qty": qty or 10,
                    # Prefill with the newest close, not the (possibly stale)
                    # call price - the user edits it to their actual fill anyway.
                    "price": str(priced[0]) if priced else str(call.entry_price_at_call),
                    "recommendation_id": call.id,
                    "note": f"executed call #{call.id}",
                }
    return templates.TemplateResponse(
        request=request, name="investments.html",
        context={"disclaimer": DISCLAIMER, "lots": lots, "summary": summary,
                 "trades": trades, "rates": rates, "symbols": symbols, "closes": closes,
                 "prefill": prefill, "today": today,
                 "flash": request.query_params.get("flash"),
                 "err": request.query_params.get("err")},
    )


@router.post("/investments/trade")
def record_real_trade(
    request: Request,
    symbol: str = Form(...),
    side: str = Form(..., pattern="^(buy|sell)$"),
    qty: str = Form(...),
    price: str = Form(...),
    trade_date: str = Form(""),
    commission: str = Form("0"),
    note: str | None = Form(None),
    recommendation_id: int | None = Form(None),
):
    """Record a real-world fill.  Errors come back as a flash on the page."""
    try:
        when = dt.date.fromisoformat(trade_date) if trade_date else None
        with session_scope() as session:
            trade = record_trade(
                session, symbol, side, Decimal(qty), Decimal(price),
                trade_date=when, commission=Decimal(commission or "0"),
                recommendation_id=recommendation_id, note=note or None,
            )
            session.commit()
            flash = (f"recorded {trade.side} {trade.qty} {trade.ticker.symbol} "
                     f"@ {trade.price} on {trade.trade_date}")
    except (RealTradeError, InvalidOperation, ValueError) as exc:
        return _back(err=str(exc))
    return _back(flash=flash)


@router.post("/investments/rates")
def save_rates(short_term_pct: str = Form(...), long_term_pct: str = Form(...)):
    """Percent inputs (e.g. 32 / 15) stored as fractions in AppSetting."""
    try:
        with session_scope() as session:
            set_tax_rates(session,
                          Decimal(short_term_pct) / 100,
                          Decimal(long_term_pct) / 100)
            session.commit()
    except (TaxRateError, InvalidOperation) as exc:
        return _back(err=str(exc))
    return _back(flash="tax rates updated")
