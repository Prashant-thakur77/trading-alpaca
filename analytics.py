"""
Options analytics — realized volatility, implied volatility, Greeks.

Greeks use the `vollib` Black-Scholes implementation (installed as py_vollib;
we import the non-deprecated `vollib` path). All Greeks are **per share** —
multiply by contracts * 100 to get position Greeks. RiskGuard's net-delta and
net-vega limits depend on that convention.

The smile fit (`fit_smile` / `richness`) is a MEASUREMENT, not an edge. See
docs/research/smile-feasibility.md: on a live SPY chain the apparent signal-to-
noise of smile residuals falls monotonically from 5.51x to 1.63x as the
moneyness band narrows and the polynomial degree rises, which is the signature
of a misspecified fit rather than a mispriced market — roughly 70% of the
apparent signal is our own model error. What survives is ~0.11 vol points,
about $8 per contract against a multi-dollar bid-ask. **The richness is smaller
than the cost of crossing the spread**, so it is only ever a tie-breaker among
strikes that are already viable on liquidity, DTE and guard grounds. It is not
alpha, not edge, and never a filter.

Corroborating external evidence (docs/research/imc-validation-discipline.md):
the two top-10 IMC Prosperity teams who attempted a vol surface both abandoned
it. One hardcoded a fitted quadratic that broke on submission day; the other's
README says a vol surface "proved too complex to implement reliably". Both
converged on a rolling mean of live mid-IV instead. We ship the fit as a
measurement precisely because two teams who shipped it as a signal regretted it.

Every function fails closed: bad inputs yield None or zeroed Greeks rather than
raising mid-scan (CLAUDE.md hard rule 2).
"""
import logging
import math
import warnings

import numpy as np
import pandas as pd
from dataclasses import dataclass
from datetime import date

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

# IV solves outside this range are rejected before any smile fit. The live SPY
# chain contains deep-ITM calls whose quotes solve to ~240% IV; a single such
# point dominates a least-squares polynomial and bends the whole curve. Measured
# on real data (docs/research/smile-feasibility.md), not a hypothetical guard.
MIN_FITTABLE_IV = 0.01
MAX_FITTABLE_IV = 1.5


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


def group_by_expiry(quotes) -> dict:
    """Bucket option quotes by expiry date, preserving input order within each
    bucket. Shared by `atm_implied_vol` and the smile fit so there is exactly
    one grouping rule in this module, not two that can drift apart."""
    by_expiry: dict = {}
    for q in quotes or ():
        by_expiry.setdefault(q.expiry, []).append(q)
    return by_expiry


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

    by_expiry = group_by_expiry(chain)
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


# ── volatility smile ────────────────────────────────────────────────────


@dataclass(frozen=True)
class SmileFit:
    """A polynomial fit of implied vol against standardised moneyness, for ONE
    (expiry, right) group.

    `coeffs` is highest-power-first, matching numpy's convention, over the
    abscissa

        m = ln(K / S) / sqrt(T)

    That abscissa is taken from the 7th-place IMC Prosperity team and is the
    right choice because dividing by sqrt(T) normalises for tenor: an option 2%
    out of the money is a very different animal at 7 DTE than at 45, and only
    the T-normalised form makes the two comparable. (The 9th-place team's raw
    S/K abscissa does not, which is why we do not use it.)

    `t_years` is a field, not a derived value, because the fit is only meaningful
    against the T it was built with — recomputing it later from today's calendar
    would silently re-scale every residual.

    This object measures the SHAPE of the quoted surface. It says nothing about
    whether that shape is wrong; see the module docstring.
    """
    expiry: date | None
    right: str
    coeffs: tuple[float, ...]
    degree: int
    n_points: int
    rmse: float
    band: float
    t_years: float

    @property
    def is_measured(self) -> bool:
        """False for the absent-fit sentinel — nothing was fitted."""
        return self.n_points > 0

    def curve_at(self, m: float) -> float | None:
        """Fitted IV at standardised moneyness `m`, or None outside the band.

        Extrapolating a cubic past the strikes it was fitted on produces
        confident nonsense, so out-of-band is None, never a number.
        """
        if not self.is_measured or m is None or not math.isfinite(m):
            return None
        if abs(m) > self.band:
            return None
        value = 0.0
        for c in self.coeffs:          # Horner, highest power first
            value = value * m + c
        return float(value)

    def residual_for(self, strike: float, spot: float, iv: float | None) -> float | None:
        """Signed vol points by which `iv` sits above (+) or below (-) the curve.

        None — never 0.0 — when the point cannot be placed on the curve: no IV,
        a bad spot/strike, or a moneyness outside the fitted band. A caller that
        cannot tell "on the curve" from "unmeasurable" would treat garbage as
        agreement.
        """
        if not self.is_measured:
            return None
        if iv is None or not math.isfinite(iv):
            return None
        if spot is None or strike is None or spot <= 0 or strike <= 0:
            return None
        if self.t_years <= 0:
            return None
        m = math.log(strike / spot) / math.sqrt(self.t_years)
        fitted = self.curve_at(m)
        if fitted is None:
            return None
        return float(iv - fitted)


# The absent fit, mirroring ZERO_GREEKS: a typed placeholder for "no smile was
# measured". `fit_smile` returns None rather than this — None is the abstain
# sentinel — but callers holding a SmileFit-typed field can default to NO_SMILE
# and every method on it fails closed.
NO_SMILE = SmileFit(
    expiry=None, right="", coeffs=(), degree=0, n_points=0,
    rmse=0.0, band=0.0, t_years=0.0,
)


def fit_smile(
    quotes,
    spot: float,
    *,
    band: float = 0.2,
    degree: int = 3,
    min_points: int = 8,
) -> SmileFit | None:
    """Fit IV vs standardised moneyness for ONE (expiry, right) group.

    `quotes` must all share an expiry and a right; a mixed list returns None.
    Fitting whichever subset happened to be largest would answer a question the
    caller did not ask. Use `fit_smiles` for a whole chain.

    Fails closed with None — never a degenerate fit — when:
      * fewer than max(min_points, degree + 2) strikes survive. degree + 2 is
        the floor because degree + 1 points interpolate exactly, giving a
        zero-RMSE "perfect" fit that measures nothing;
      * the input spans several expiries or rights, or the spot is missing;
      * the least-squares solve is rank-deficient or non-finite.

    Points are dropped when their IV does not solve, when it falls outside
    (MIN_FITTABLE_IV, MAX_FITTABLE_IV), or when |m| > band. The band matters:
    smile-feasibility.md shows a wide fit is misspecified, and its inflated
    residuals are our error, not the market's.

    Liquidity gating is deliberately NOT done here — `candidate_builder.
    passes_liquidity` owns that policy, and duplicating it would give two
    thresholds to keep in sync.
    """
    if not quotes or spot is None or spot <= 0 or not math.isfinite(spot):
        return None
    if degree < 1 or band <= 0 or min_points < 1:
        return None

    try:
        groups = _group_by_expiry_and_right(quotes)
    except Exception as e:                       # malformed quote objects
        logger.debug("Smile grouping failed: %s", e)
        return None
    if len(groups) != 1:
        logger.debug("fit_smile got %d (expiry, right) groups; expected 1", len(groups))
        return None

    (expiry, right), group = next(iter(groups.items()))
    # Sorted by strike so the fitted arrays — and therefore the coefficients —
    # do not depend on the order the chain happened to arrive in.
    group = sorted(group, key=lambda q: (q.strike, q.symbol))
    t = time_to_expiry_years(group[0].dte)
    if t <= 0:
        return None
    root_t = math.sqrt(t)

    ms: list[float] = []
    ivs: list[float] = []
    for q in group:
        try:
            if q.strike <= 0:
                continue
            m = math.log(q.strike / spot) / root_t
            if abs(m) > band:
                continue
            iv = implied_vol(q.mid, spot, q.strike, t, q.right)
        except Exception as e:
            logger.debug("Smile point skipped (%s): %s", getattr(q, "symbol", "?"), e)
            continue
        if iv is None or not (MIN_FITTABLE_IV < iv < MAX_FITTABLE_IV):
            continue
        ms.append(m)
        ivs.append(iv)

    if len(ms) < max(min_points, degree + 2):
        return None

    with warnings.catch_warnings():
        # A RankWarning means the design matrix is degenerate; treat it as a
        # failure to fit rather than shipping meaningless coefficients.
        warnings.simplefilter("error")
        try:
            coeffs = np.polyfit(np.asarray(ms), np.asarray(ivs), degree)
        except Exception as e:
            logger.debug("Smile polyfit failed (%s %s): %s", expiry, right, e)
            return None

    if not np.all(np.isfinite(coeffs)):
        return None

    residuals = np.asarray(ivs) - np.polyval(coeffs, np.asarray(ms))
    rmse = float(math.sqrt(float(np.mean(residuals ** 2))))
    if not math.isfinite(rmse):
        return None

    return SmileFit(
        expiry=expiry,
        right=right,
        coeffs=tuple(float(c) for c in coeffs),
        degree=degree,
        n_points=len(ms),
        rmse=rmse,
        band=band,
        t_years=t,
    )


def fit_smiles(
    quotes,
    spot: float,
    *,
    band: float = 0.2,
    degree: int = 3,
    min_points: int = 8,
) -> dict:
    """Fit every (expiry, right) group in a chain.

    Returns {(expiry, right): SmileFit} containing only the groups that
    produced a real fit — a group with too few liquid strikes is simply absent,
    so a caller can never mistake a missing smile for a flat one. Groups are
    visited in sorted (expiry, right) order, so the result is deterministic.
    """
    if not quotes or spot is None or spot <= 0:
        return {}
    try:
        groups = _group_by_expiry_and_right(quotes)
    except Exception as e:
        logger.debug("Smile grouping failed: %s", e)
        return {}

    fits = {}
    for key in sorted(groups, key=lambda k: (k[0], k[1])):
        fit = fit_smile(groups[key], spot, band=band, degree=degree,
                        min_points=min_points)
        if fit is not None:
            fits[key] = fit
    return fits


def richness(quote, fit: SmileFit | None, spot: float) -> float:
    """Signed vol points by which one strike sits above its own smile.

    Positive = the strike's mid IV is ABOVE the fitted curve, i.e. rich; the
    market is charging more vol for it than its neighbours imply. Negative =
    cheap relative to its neighbours.

    **This is not edge.** The measured magnitude on a live chain is ~0.11 vol
    points, roughly $8 per contract, against a bid-ask of several dollars — you
    cannot capture it by trading, because crossing the spread costs more than
    the whole signal. Its only sanctioned use is as a TIE-BREAKER between
    strikes that have already passed liquidity, DTE and RiskGuard. Never use it
    to admit a strike, to reject one, or to size a position.

    Returns exactly **0.0** — meaning "no measurable signal", never "fairly
    priced" — when the residual is smaller than the strike's own IV bid-ask
    half-width, (IV(ask) - IV(bid)) / 2. That is the brief's half-spread rule
    and, generalised, the 7th-place IMC team's own gate: do not act on a vol
    band worth less than the tick. 0.0 is also what every failure returns (no
    fit, mismatched contract, an IV that will not solve, a zero bid), so a
    caller ranking on richness degrades to "no preference", never to a
    fabricated one.

    Note the residual is measured against a curve the strike itself helped fit,
    so least squares pulls the curve toward it and the reported deviation is
    (1 - leverage) x the true one — i.e. conservative, which is the direction we
    want a number this small to err in.
    """
    if quote is None or fit is None or not isinstance(fit, SmileFit):
        return 0.0
    if not fit.is_measured:
        return 0.0
    if spot is None or spot <= 0 or not math.isfinite(spot):
        return 0.0
    if quote.expiry != fit.expiry or quote.right != fit.right:
        # A call scored against the put smile is not a measurement.
        return 0.0

    t = fit.t_years
    try:
        iv_mid = implied_vol(quote.mid, spot, quote.strike, t, quote.right)
        residual = fit.residual_for(quote.strike, spot, iv_mid)
        if residual is None:
            return 0.0
        iv_bid = implied_vol(quote.bid, spot, quote.strike, t, quote.right)
        iv_ask = implied_vol(quote.ask, spot, quote.strike, t, quote.right)
    except Exception as e:
        logger.debug("Richness unmeasurable (%s): %s", getattr(quote, "symbol", "?"), e)
        return 0.0

    if iv_bid is None or iv_ask is None:
        # Without both sides we cannot price the noise, so we cannot claim signal.
        return 0.0
    noise_floor = (iv_ask - iv_bid) / 2.0
    if not math.isfinite(noise_floor) or noise_floor < 0:
        return 0.0
    if abs(residual) < noise_floor:
        return 0.0
    return float(residual)


def _group_by_expiry_and_right(quotes) -> dict:
    """{(expiry, right): [quotes]} — the per-expiry buckets of
    `group_by_expiry`, split again by right, since a call smile and a put smile
    are different curves."""
    groups: dict = {}
    for expiry, bucket in group_by_expiry(quotes).items():
        for q in bucket:
            groups.setdefault((expiry, q.right), []).append(q)
    return groups


# ── realized-vol regime ─────────────────────────────────────────────────


def realized_vol_rank(bars, window: int = 252, *, vol_window: int = 20) -> float | None:
    """Percentile in [0, 1] of today's REALIZED vol within its own trailing
    history: 1.0 = the most volatile the stock has been in `window` bars.

    **This is a realized-vol rank, not IV-rank.** True IV-rank compares today's
    *implied* vol to a history of implied vol, and we do not have that history:
    the live journal holds 2 snapshots whose `atm_iv` is None, and the 23 seed
    snapshots carry none either. Building that history is a prerequisite for
    IV-rank; until then, do not relabel this number as one. Realized vol answers
    "how much has the underlying actually been moving lately, relative to its
    own recent past", which is a regime question, not a rich/cheap one.

    Each reading is `realized_volatility` over `vol_window` bars, so the history
    is the same estimator the rest of the desk uses. Ties are mid-ranked, which
    is what makes a perfectly flat series return 0.5: every reading is identical,
    so today is exactly as typical as every other day. Callers needing to tell
    "typical" apart from "no variation at all" must read `realized_volatility`
    itself, which returns 0.0 there.

    Returns **None** — not 0.5, not any other plausible-looking default — when
    there are fewer than `window` closes. A fabricated neutral reading is the
    exact failure mode this codebase refuses everywhere else: it looks like a
    measurement and silently isn't one.
    """
    if bars is None or "close" not in bars:
        return None
    if window < vol_window + 2 or vol_window < 2:
        return None

    closes = pd.to_numeric(bars["close"], errors="coerce").dropna()
    if len(closes) < window:
        return None

    history = pd.DataFrame({"close": closes.iloc[-window:].reset_index(drop=True)})
    vols = [
        realized_volatility(history.iloc[:end], window=vol_window)
        for end in range(vol_window, window + 1)
    ]
    if len(vols) < 2:
        return None

    current = vols[-1]
    if not math.isfinite(current):
        return None
    below = sum(1 for v in vols if v < current)
    tied = sum(1 for v in vols if v == current)
    rank = (below + 0.5 * tied) / len(vols)
    return float(min(1.0, max(0.0, rank)))
