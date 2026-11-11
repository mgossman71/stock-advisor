"""Application configuration.

Every tunable lives here and is sourced from the environment / `.env`
(python-dotenv via pydantic-settings).  No secret ever appears in source.
Money-valued settings are :class:`~decimal.Decimal` because nothing that
touches P&L is allowed to be a float.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR: Path = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration, overridable through environment variables."""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- app ---------------------------------------------------------------
    app_name: str = "Stock Advisor"
    debug: bool = False

    # --- database ----------------------------------------------------------
    database_url: str = "sqlite:///./data/stock_advisor.db"

    # --- market data -------------------------------------------------------
    # "auto" builds the provider chain: yfinance -> nasdaq -> fred.  Set to a
    # single provider name to pin it during debugging.
    market_data_provider: str = "auto"
    min_provider_interval_seconds: float = Field(default=1.2, ge=0.0)
    max_retries: int = Field(default=4, ge=0, le=10)
    backoff_base_seconds: float = Field(default=1.0, gt=0.0)
    backoff_max_seconds: float = Field(default=30.0, gt=0.0)
    provider_timeout_seconds: float = Field(default=20.0, gt=0.0)

    finnhub_api_key: str | None = None
    alphavantage_api_key: str | None = None
    polygon_api_key: str | None = None

    # --- universe / benchmark ---------------------------------------------
    benchmark_symbol: str = "^GSPC"
    vix_symbol: str = "^VIX"
    universe_size: int = Field(default=100, ge=1)

    # --- paper trading -----------------------------------------------------
    initial_cash: Decimal = Decimal("100000")
    commission_per_share: Decimal = Decimal("0.00")
    min_commission: Decimal = Decimal("0.00")
    slippage_bps: Decimal = Decimal("5")
    short_borrow_apy: Decimal = Decimal("0.03")
    #: Reg-T style initial margin on shorts: equity must be at least this % of
    #: total short market value after a new short fills.
    short_margin_pct: Decimal = Decimal("50")
    risk_per_trade_pct: Decimal = Decimal("1.0")

    # --- scheduling / alerts ----------------------------------------------
    scheduler_enabled: bool = True
    #: "minute hour dom month dow", evaluated in UTC (21:15 UTC = 16:15 EST /
    #: 17:15 EDT, i.e. after the US close all year).
    nightly_cron: str = "15 21 * * *"
    alert_default_drawdown_pct: Decimal = Decimal("10")
    alert_default_earnings_within_days: int = 14

    # --- demo seed ---------------------------------------------------------
    seed_symbols: str = (
        "AAPL,MSFT,NVDA,AMZN,GOOGL,META,NFLX,TSLA,JPM,XOM,"
        "UNH,PG,KO,HD,CAT,DE,AMD,DIS,NKE"
    )
    seed_history_years: int = Field(default=2, ge=1, le=10)

    @field_validator("database_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        """Fail loudly on unsupported backends instead of at first query."""
        if not (value.startswith("sqlite") or value.startswith("postgresql")):
            raise ValueError("DATABASE_URL must be a sqlite:// or postgresql:// URL")
        return value

    # ------------------------------------------------------------------ misc
    @property
    def seed_symbol_list(self) -> list[str]:
        """Seed tickers as a clean, de-duplicated, ordered list."""
        seen: list[str] = []
        for raw in self.seed_symbols.split(","):
            symbol = raw.strip().upper()
            if symbol and symbol not in seen:
                seen.append(symbol)
        return seen

    @property
    def db_path(self) -> Path | None:
        """Absolute path of the SQLite file, or ``None`` for other backends."""
        if not self.database_url.startswith("sqlite"):
            return None
        raw = self.database_url.split("sqlite:///", 1)[-1]
        if not raw or raw == ":memory:":
            return None
        path = Path(raw)
        return path if path.is_absolute() else (BASE_DIR / path).resolve()

    def sqlalchemy_connect_args(self) -> dict[str, object]:
        """Connect args for :func:`sqlalchemy.create_engine`."""
        if self.database_url.startswith("sqlite"):
            return {"check_same_thread": False}
        return {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings object (cached)."""
    return Settings()
