"""Price-series indicators - pure functions, Decimal in, Decimal out.

Every function returns ``None`` when there is not enough data.  A short series
never silently produces a "neutral" placeholder: the caller must render
"unavailable" and the scorer must exclude the component from the composite.
"""

from __future__ import annotations

import math
from decimal import Decimal

ZERO = Decimal("0")


def sma(closes: list[Decimal], window: int) -> Decimal | None:
    """Simple moving average of the last ``window`` closes."""
    if len(closes) < window or window <= 0:
        return None
    return sum(closes[-window:]) / Decimal(window)


def period_return(closes: list[Decimal], days: int) -> Decimal | None:
    """Percent-point return over the last ``days`` bars (e.g. 63 ≈ 3 months)."""
    if len(closes) < days + 1 or days <= 0 or closes[-days - 1] == 0:
        return None
    first, last = closes[-days - 1], closes[-1]
    return ((last / first - Decimal("1")) * Decimal("100")).quantize(Decimal("0.0001"))


def rsi(closes: list[Decimal], window: int = 14) -> Decimal | None:
    """Wilder RSI over daily changes (needs ``window + 1`` closes)."""
    if len(closes) < window + 1:
        return None
    gains = losses = Decimal("0")
    # Wilder smoothing: simple average for the first window, then EMA-style.
    for i in range(1, window + 1):
        delta = closes[i] - closes[i - 1]
        gains, losses = gains + max(delta, ZERO), losses + max(-delta, ZERO)
    avg_gain, avg_loss = gains / window, losses / window
    for i in range(window + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (window - 1) + max(delta, ZERO)) / window
        avg_loss = (avg_loss * (window - 1) + max(-delta, ZERO)) / window
    if avg_loss == 0:
        return Decimal("100") if avg_gain > 0 else Decimal("50")
    rs = avg_gain / avg_loss
    value = Decimal("100") - Decimal("100") / (Decimal("1") + rs)
    return value.quantize(Decimal("0.0001"))


def annualized_volatility_pct(closes: list[Decimal], window: int = 126) -> Decimal | None:
    """Std-dev of daily log returns, annualised, in percent points.

    Internal float math is confined here: volatility is a *statistic*, not
    money, and log() has no Decimal implementation.  Output is Decimal.
    """
    sample = closes[-(window + 1):]
    if len(sample) < window + 1:
        return None
    returns = [
        math.log(float(b) / float(a))
        for a, b in zip(sample[:-1], sample[1:])
        if a > 0 and b > 0
    ]
    if len(returns) < window:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return Decimal(str(math.sqrt(var) * math.sqrt(252) * 100)).quantize(Decimal("0.0001"))


def max_drawdown_pct(closes: list[Decimal]) -> Decimal | None:
    """Worst peak-to-trough decline in the series, as a negative percent point."""
    if len(closes) < 2:
        return None
    peak, worst = closes[0], ZERO
    for price in closes:
        peak = max(peak, price)
        if peak > 0:
            worst = min(worst, (price / peak - Decimal("1")) * Decimal("100"))
    return worst.quantize(Decimal("0.0001"))


def distance_from_high_pct(closes: list[Decimal], window: int = 252) -> Decimal | None:
    """How far the last close sits below the highest close of ``window`` bars."""
    sample = closes[-window:]
    if not sample or sample[-1] == 0:
        return None
    high = max(sample)
    return ((sample[-1] / high - Decimal("1")) * Decimal("100")).quantize(Decimal("0.0001"))


def pct_below(series: list[Decimal], value: Decimal) -> Decimal | None:
    """Percentile of ``value`` within ``series`` (0 = cheapest, 100 = dearest)."""
    if not series or value is None:
        return None
    below = sum(1 for x in series if x <= value)
    return Decimal(below * 100) / Decimal(len(series))
