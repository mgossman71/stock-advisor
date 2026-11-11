"""Position sizing: risk-per-trade given account value and a stop level.

The helper answers "how many shares can I buy so that hitting the stop loses
at most RISK_PER_TRADE_PCT of the account?"  Returns ``None`` when inputs make
the question unanswerable (stop on the wrong side, missing price) rather than
inventing a size.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR

from app.config import Settings, get_settings

ZERO = Decimal("0")


def suggested_size(
    account_value: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    *,
    risk_pct: Decimal | None = None,
    settings: Settings | None = None,
) -> int | None:
    """Whole shares to buy so ``|entry-stop| * shares <= risk_pct% of account``.

    * ``account_value`` should be total equity (cash + marks), not just cash.
    * Shorts use a stop *above* entry; the per-share risk is still
      ``|entry - stop|``.
    * Returns ``None`` when ``entry == stop`` (infinite risk per share) or any
      input is missing.
    """
    settings = settings or get_settings()
    if account_value is None or entry_price is None or stop_price is None:
        return None
    if entry_price <= 0 or account_value <= 0:
        return None
    per_share_risk = abs(entry_price - stop_price)
    if per_share_risk == ZERO:
        return None
    pct = Decimal(risk_pct if risk_pct is not None else settings.risk_per_trade_pct)
    budget = account_value * pct / Decimal("100")
    return int((budget / per_share_risk).to_integral_value(rounding=ROUND_FLOOR)) or None
