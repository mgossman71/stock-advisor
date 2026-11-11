"""Every page router, in nav order."""

from app.web.routes_alerts import router as alerts_router
from app.web.routes_auth import router as auth_router
from app.web.routes_dashboard import router as dashboard_router
from app.web.routes_investments import router as investments_router
from app.web.routes_jobs import router as jobs_router
from app.web.routes_portfolio import router as portfolio_router
from app.web.routes_recommendations import router as recommendations_router
from app.web.routes_research import router as research_router
from app.web.routes_screen import router as screen_router
from app.web.routes_settings import router as settings_router

routers = [
    dashboard_router,
    research_router,
    screen_router,
    portfolio_router,
    investments_router,
    recommendations_router,
    alerts_router,
    settings_router,
    jobs_router,
    auth_router,
]
