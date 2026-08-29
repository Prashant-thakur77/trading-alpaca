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
CONTRACT_MULTIPLIER = 100

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


def atm_implied_vol(chain, spot: float, dte_target: int = 30) -> float | None:
    """Market ATM implied vol: mean of call and put IV at the strike and
    expiry nearest (spot, dte_target).

    This must be a property of the MARKET, never of which candidates a
    caller happened to construct or choose to surface. Deriving "ATM IV"
    from surfaced candidates instead (as committee/snapshot.py once did) let
    candidate SELECTION swing the number: on one unchanged SPY chain,
    surfacing only bear call spreads sampled only OTM calls -- which sit
    lower on the vol skew -- and biased the estimate down enough to flip the
    committee's regime call (buy premium vs sell premium) even though the
    market never moved. Reading the chain's own contracts nearest spot,
    independent of any candidate list, removes that dependency.

    Averaging the call and the put at the same strike is more robust than
    either alone: put-call parity says they should imply similar vol at a
    given strike/expiry, so the mean cancels idiosyncratic noise (a stale
    quote, a wide market) in either single leg.

    Returns None -- never a guess -- when the chain is empty or neither leg
    at the nearest strike/expiry can be priced (`analytics.implied_vol`
    already fails closed on a quote below intrinsic or a zero bid).
    """
    if not chain or spot <= 0:
        return None

    by_expiry: dict = {}
    for q in chain:
        by_expiry.setdefault(q.expiry, []).append(q)
    if not by_expiry:
        return None

    # Ties broken by the expiry date itself so the choice is deterministic.
    target_expiry = min(
        by_expiry, key=lambda e: (abs(by_expiry[e][0].dte - dte_target), e)
    )
    quotes = by_expiry[target_expiry]
    strikes = {q.strike for q in quotes}
    if not strikes:
        return None
    target_strike = min(strikes, key=lambda s: (abs(s - spot), s))

    call = next((q for q in quotes if q.strike == target_strike and q.right == "c"), None)
    put = next((q for q in quotes if q.strike == target_strike and q.right == "p"), None)

    t = time_to_expiry_years(quotes[0].dte)
    ivs = []
    if call is not None:
        iv = implied_vol(call.mid, spot, call.strike, t, "c")
        if iv is not None:
            ivs.append(iv)
    if put is not None:
        iv = implied_vol(put.mid, spot, put.strike, t, "p")
        if iv is not None:
            ivs.append(iv)

    if not ivs:
        return None
    return sum(ivs) / len(ivs)


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


def position_greeks(intent, spot: float) -> tuple[float, float] | None:
    """Net (delta, vega) for a whole position, in position terms.

    Per-share Greeks scaled by side (short legs negate), the 100-share contract
    multiplier, and the contract count — which is what RiskGuard's
    |net delta| and |net vega| limits are expressed in.

    Returns **None** when the position cannot be measured: no intent, no spot,
    or any leg whose IV will not solve (a quote below intrinsic, a zero bid).
    None is a sentinel the caller must treat as an abstain — it is never a
    number and must never be coerced to one.

    Skipping an unpriceable leg and returning the rest would be worse than
    useless: a long straddle with one deep-ITM leg dropped reports delta -12
    for a position that is really about +90, so a book that breaches the
    |delta| <= 30 limit measures as comfortably inside it. Partial Greeks are
    a wrong answer wearing the costume of a right one.
    """
    if intent is None or spot <= 0:
        return None

    t = time_to_expiry_years(intent.dte)
    net_delta = net_vega = 0.0
    for leg in intent.legs:
        q = leg.quote
        iv = implied_vol(q.mid, spot, q.strike, t, q.right)
        if iv is None:
            logger.warning(
                "Cannot price leg %s (mid=%.2f S=%.2f K=%.2f %s) — position "
                "Greeks are unmeasurable, abstaining", q.symbol, q.mid, spot,
                q.strike, q.right,
            )
            return None
        g = greeks(q.right, spot, q.strike, t, iv)
        sign = -1.0 if leg.side == "sell" else 1.0
        scale = sign * CONTRACT_MULTIPLIER * leg.contracts
        net_delta += g.delta * scale
        net_vega += g.vega * scale
    return net_delta, net_vega


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
