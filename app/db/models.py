"""ORM model registry.

Importing this module registers every table on :class:`Base.metadata`, which is
what Alembic autogenerate and the test fixtures rely on.
"""

from app.db.models_advice import (
    AppSetting,
    NotificationChannel,
    Recommendation,
    RecommendationOutcome,
    ResearchReport,
    ScoringWeight,
)
from app.db.models_market import (
    AnalystAction,
    DataFetchLog,
    EarningsEvent,
    Fundamentals,
    NewsItem,
    PriceHistory,
    Ticker,
)
from app.db.models_trading import (
    Account,
    Alert,
    Order,
    PortfolioSnapshot,
    Position,
)

__all__ = [
    "Account",
    "Alert",
    "AnalystAction",
    "AppSetting",
    "DataFetchLog",
    "EarningsEvent",
    "Fundamentals",
    "NewsItem",
    "NotificationChannel",
    "Order",
    "PortfolioSnapshot",
    "Position",
    "PriceHistory",
    "Recommendation",
    "RecommendationOutcome",
    "ResearchReport",
    "ScoringWeight",
    "Ticker",
]
