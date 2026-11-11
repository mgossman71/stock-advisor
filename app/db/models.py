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
    ScanScore,
    ScoringWeight,
)
from app.db.models_market import (
    AnalystAction,
    DataFetchLog,
    Dividend,
    EarningsEvent,
    Fundamentals,
    NewsItem,
    PriceHistory,
    StockSplit,
    Ticker,
)
from app.db.models_trading import (
    Account,
    Alert,
    CashAdjustment,
    DividendPayment,
    Order,
    PortfolioSnapshot,
    Position,
    RealTrade,
)

__all__ = [
    "Account",
    "Alert",
    "AnalystAction",
    "AppSetting",
    "CashAdjustment",
    "DataFetchLog",
    "Dividend",
    "DividendPayment",
    "EarningsEvent",
    "Fundamentals",
    "NewsItem",
    "NotificationChannel",
    "Order",
    "PortfolioSnapshot",
    "Position",
    "PriceHistory",
    "StockSplit",
    "RealTrade",
    "Recommendation",
    "RecommendationOutcome",
    "ResearchReport",
    "ScanScore",
    "ScoringWeight",
    "Ticker",
]
