"""
Options analytics — realized volatility, implied volatility, Greeks.

Greeks use the `vollib` Black-Scholes implementation (installed as py_vollib;
we import the non-deprecated `vollib` path). All Greeks are **per share** —
multiply by contracts * 100 to get position Greeks. RiskGuard's net-delta and
net-vega limits depend on that convention.

Every function fails closed: bad inputs yield None or zeroed Greeks rather than
raising mid-scan (CLAUDE.md hard rule 2).
"""
import logging
import math

import numpy as np
import pandas as pd
from dataclasses import dataclass

from vollib.black_scholes import black_scholes
from vollib.black_scholes.greeks.analytical import delta as _delta
from vollib.black_scholes.greeks.analytical import gamma as _gamma
from vollib.black_scholes.greeks.analytical import theta as _theta
from vollib.black_scholes.greeks.analytical import vega as _vega
from vollib.black_scholes.implied_volatility import implied_volatility

logger = logging.getLogger(__name__)

TRADING_DAYS = 252
RISK_FREE_RATE = 0.045  # ~US 3M T-bill; refreshed per session, not per trade.

# Black-Scholes divides by t; an expiring option would blow up. One hour floor.
MIN_T_YEARS = 1.0 / (365 * 24)


@dataclass(frozen=True)
class OptionGreeks:
    """Per-share Greeks for a single option."""
    delta: float
    gamma: float
    theta: float
    vega: float


ZERO_GREEKS = OptionGreeks(0.0, 0.0, 0.0, 0.0)


def time_to_expiry_years(days: float) -> float:
    """Convert DTE to years, floored so Black-Scholes never divides by zero."""
    return max(days / 365.0, MIN_T_YEARS)


def realized_volatility(bars: pd.DataFrame, window: int = 20) -> float:
    """Annualized realized volatility from close-to-close log returns.

    Returns 0.0 when there is too little data to measure — callers treat
    zero vol as "no signal", never as "calm market".
    """
    if bars is None or "close" not in bars or len(bars) < 2:
        return 0.0

    closes = pd.to_numeric(bars["close"], errors="coerce").dropna()
    if len(closes) < 2:
        return 0.0

    # window bars of prices yield window-1 returns.
    closes = closes.iloc[-window:]
    log_returns = np.log(closes / closes.shift(1)).dropna()
    if len(log_returns) < 2:
        return 0.0

    return float(log_returns.std(ddof=1) * math.sqrt(TRADING_DAYS))


def implied_vol(
    price: float,
    spot: float,
    strike: float,
    t_years: float,
    flag: str,
    rate: float = RISK_FREE_RATE,
) -> float | None:
    """Back out implied volatility from an option's market price.

    Returns None when no real IV solves the price (e.g. a quote below
    intrinsic value) — the caller must abstain rather than guess.
    """
    try:
        iv = implied_volatility(price, spot, strike, t_years, rate, flag.lower())
    except Exception as e:  # vollib raises on unsolvable / below-intrinsic input
        logger.debug("IV unsolvable (S=%s K=%s t=%.4f %s): %s", spot, strike, t_years, flag, e)
        return None

    if iv is None or not math.isfinite(iv) or iv <= 0:
        return None
    return float(iv)


def greeks(
    flag: str,
    spot: float,
    strike: float,
    t_years: float,
    vol: float,
    rate: float = RISK_FREE_RATE,
) -> OptionGreeks:
    """Per-share Black-Scholes Greeks. Returns zeroed Greeks on bad input.

    theta is per calendar day, matching vollib's convention.
    """
    if vol is None or vol <= 0 or t_years <= 0 or spot <= 0 or strike <= 0:
        return ZERO_GREEKS

    f = flag.lower()
    try:
        g = OptionGreeks(
            delta=float(_delta(f, spot, strike, t_years, rate, vol)),
            gamma=float(_gamma(f, spot, strike, t_years, rate, vol)),
            theta=float(_theta(f, spot, strike, t_years, rate, vol)),
            vega=float(_vega(f, spot, strike, t_years, rate, vol)),
        )
    except Exception as e:
        logger.warning("Greeks failed (S=%s K=%s t=%.4f vol=%s %s): %s",
                       spot, strike, t_years, vol, flag, e)
        return ZERO_GREEKS

    if not all(math.isfinite(v) for v in (g.delta, g.gamma, g.theta, g.vega)):
        return ZERO_GREEKS
    return g


def theoretical_price(
    flag: str,
    spot: float,
    strike: float,
    t_years: float,
    vol: float,
    rate: float = RISK_FREE_RATE,
) -> float | None:
    """Black-Scholes fair value, or None if it cannot be computed."""
    if vol is None or vol <= 0 or t_years <= 0:
        return None
    try:
        return float(black_scholes(flag.lower(), spot, strike, t_years, rate, vol))
    except Exception:
        return None
