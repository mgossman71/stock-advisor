"""Alerts: list, acknowledge, create; surfaced as the nav badge on the dashboard."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.config import get_settings
from app.db.base import session_scope
from app.db.models import Alert, Ticker
from app.web.common import DISCLAIMER, templates

router = APIRouter()


@router.get("/alerts")
def alerts_page(request: Request):
    with session_scope() as session:
        alerts = session.query(Alert).order_by(
            Alert.triggered_at.desc().nullslast(), Alert.created_at.desc()
        ).limit(200).all()
        for a in alerts:
            if a.ticker is not None:
                _ = a.ticker.symbol
        symbols = session.scalars(
            select(Ticker.symbol).order_by(Ticker.symbol)
        ).all()
    return templates.TemplateResponse(
        request=request, name="alerts.html",
        context={"disclaimer": DISCLAIMER, "alerts": alerts, "symbols": symbols},
    )


@router.post("/alerts")
def create_alert(symbol: str = Form(...), alert_type: str = Form(...),
                 threshold: str | None = Form(None)):
    settings = get_settings()
    value: Decimal | None = None
    if threshold:
        try:
            value = Decimal(threshold)
        except InvalidOperation:
            return RedirectResponse("/alerts", status_code=303)
    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=symbol.strip().upper()).first()
        if ticker is not None and alert_type in ("price_above", "price_below",
                                                 "earnings_within", "drawdown_pct"):
            message = f"{alert_type} on {ticker.symbol}"
            session.add(Alert(ticker_id=ticker.id, type=alert_type, threshold=value,
                              message=message))
        session.commit()
    return RedirectResponse("/alerts", status_code=303)


@router.post("/alerts/{alert_id}/ack")
def ack_alert(alert_id: int):
    with session_scope() as session:
        alert = session.get(Alert, alert_id)
        if alert is not None:
            alert.acknowledged = True
        session.commit()
    return RedirectResponse("/alerts", status_code=303)
