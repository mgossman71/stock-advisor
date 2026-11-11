"""Dashboard: account summary, equity curve, alerts badge, today's calls, regime."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Request

from app.config import get_settings
from app.db.base import session_scope
from app.db.models import Alert, Recommendation
from app.research import indicators
from app.research.outcomes import Scoreboard, scoreboard as build_scoreboard
from app.research.report import closes_with_dates
from app.trading.accounts import get_or_open_account
from app.trading.snapshots import equity_curve, portfolio_values
from app.web.common import DISCLAIMER, _json_safe, templates

router = APIRouter()


@router.get("/", include_in_schema=False)
def dashboard(request: Request):
    settings = get_settings()
    with session_scope() as session:
        account = get_or_open_account(session, settings)
        values = portfolio_values(session, account)
        curve = equity_curve(session)
        open_recs = session.query(Recommendation).filter_by(status="open") \
            .order_by(Recommendation.confidence.desc()).limit(10).all()
        untriggered_alerts = session.query(Alert).filter(
            Alert.acknowledged.is_(False)).order_by(Alert.triggered_at.desc()).limit(12).all()
        for rec in open_recs:  # materialize lazy relationships inside the session
            _ = (rec.ticker.symbol, rec.stop_price, rec.target_price)
        for alert in untriggered_alerts:
            if alert.ticker is not None:
                _ = alert.ticker.symbol
        triggered_count = session.query(Alert).filter(
            Alert.triggered_at.isnot(None), Alert.acknowledged.is_(False)).count()
        regime = _regime_panel(session, settings)
        board = build_scoreboard(session).overall
        as_of = _latest_data_as_of(session)
    return templates.TemplateResponse(
        request=request, name="dashboard.html",
        context={
            "disclaimer": DISCLAIMER, "account": account, "values": values,
            "curve": _json_safe(curve), "open_recs": open_recs, "alerts": untriggered_alerts,
            "alert_badge": triggered_count, "regime": regime, "scoreboard": board,
            "data_as_of": as_of, "settings": settings,
        },
    )


def _latest_data_as_of(session) -> dt.datetime | None:
    from app.db.models import PriceHistory

    day = session.query(PriceHistory.date).order_by(PriceHistory.date.desc()).first()
    return dt.datetime.combine(day[0], dt.time(), tzinfo=dt.timezone.utc) if day else None


def _regime_panel(session, settings) -> dict:
    """Index vs 50/200-day SMA, VIX level, sector 1-month leaders/laggards."""
    out: dict = {"index_symbol": settings.benchmark_symbol, "index_vs_sma50": None,
                 "index_vs_sma200": None, "index_return_1m": None, "vix": None,
                 "sector_leaders": [], "sector_laggards": []}
    from app.db.models import Ticker

    bench = session.query(Ticker).filter_by(symbol=settings.benchmark_symbol).first()
    if bench:
        closes = [c for _, c in closes_with_dates(session, bench.id)]
        sma50, sma200 = indicators.sma(closes, 50), indicators.sma(closes, 200)
        if closes and sma50 is not None:
            out["index_last"] = closes[-1]
            out["index_as_of"] = closes_with_dates(session, bench.id)[-1][0]
            out["index_vs_sma50"] = "above" if closes[-1] >= sma50 else "below"
        if closes and sma200 is not None:
            out["index_vs_sma200"] = "above" if closes[-1] >= sma200 else "below"
        out["index_return_1m"] = indicators.period_return(closes, 21)

    vix = session.query(Ticker).filter_by(symbol=settings.vix_symbol).first()
    if vix:
        vcloses = [c for _, c in closes_with_dates(session, vix.id)]
        if vcloses:
            out["vix"] = vcloses[-1]

    # Sector 1-month performance from stored closes of universe tickers.
    per_sector: dict[str, list] = {}
    tickers = session.query(Ticker).filter(Ticker.in_universe.is_(True),
                                           Ticker.sector.isnot(None)).all()
    for ticker in tickers[:250]:
        closes = [c for _, c in closes_with_dates(session, ticker.id)]
        ret = indicators.period_return(closes, 21)
        if ret is not None:
            per_sector.setdefault(ticker.sector, []).append(ret)
    sector_avg = sorted(
        ((sector, sum(vals) / len(vals)) for sector, vals in per_sector.items() if vals),
        key=lambda kv: kv[1], reverse=True,
    )
    out["sector_leaders"] = sector_avg[:3]
    out["sector_laggards"] = list(reversed(sector_avg[-3:]))
    return out
