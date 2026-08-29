"""Tests for analytics.py — realized vol, implied vol, Greeks.

Greeks feed RiskGuard's net-delta/net-vega limits, so sign conventions and
per-contract scaling are load-bearing, not cosmetic.
"""
import math
import os
import sys
from datetime import date, timedelta

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from analytics import (
    realized_volatility,
    implied_vol,
    greeks,
    OptionGreeks,
    time_to_expiry_years,
    atm_implied_vol,
)
from candidate_builder import OptionQuote


def _bars(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


class TestRealizedVolatility:
    def test_flat_prices_have_zero_vol(self):
        assert realized_volatility(_bars([100.0] * 40)) == pytest.approx(0.0)

    def test_is_annualized(self):
        """A constant daily log return of r has zero stdev → zero vol;
        alternating moves produce a positive annualized number."""
        closes = [100.0 * (1.01 if i % 2 else 1.0) for i in range(60)]
        vol = realized_volatility(_bars(closes))
        assert vol > 0

    def test_known_value_matches_manual_calculation(self):
        """Annualized = stdev(log returns) * sqrt(252)."""
        closes = [100, 102, 101, 104, 103, 106, 105, 108, 107, 110]
        df = _bars([float(c) for c in closes])
        rets = pd.Series(closes).astype(float).apply(math.log).diff().dropna()
        expected = rets.std(ddof=1) * math.sqrt(252)
        assert realized_volatility(df, window=len(closes)) == pytest.approx(expected, rel=1e-9)

    def test_insufficient_data_returns_zero(self):
        assert realized_volatility(_bars([100.0])) == 0.0

    def test_respects_window(self):
        """Only the last `window` bars matter — an old shock is excluded."""
        calm = [100.0 + 0.01 * i for i in range(40)]
        shocked = [50.0, 150.0] + calm
        assert realized_volatility(_bars(shocked), window=20) == pytest.approx(
            realized_volatility(_bars(calm), window=20)
        )


class TestTimeToExpiry:
    def test_thirty_days_is_thirty_over_365(self):
        assert time_to_expiry_years(30) == pytest.approx(30 / 365)

    def test_expired_is_never_zero(self):
        """t=0 would divide by zero inside Black-Scholes; floor it."""
        assert time_to_expiry_years(0) > 0


class TestImpliedVol:
    def test_recovers_the_vol_used_to_price(self):
        """Price a put at 20% vol, then back out 20%."""
        from vollib.black_scholes import black_scholes
        t = time_to_expiry_years(30)
        price = black_scholes("p", 450.0, 440.0, t, 0.045, 0.20)
        assert implied_vol(price, 450.0, 440.0, t, "p") == pytest.approx(0.20, abs=1e-4)

    def test_price_below_intrinsic_returns_none(self):
        """An ITM call (S=450, K=400) has intrinsic 50; a $1 quote is impossible.
        No real IV solves it — fail closed rather than guess."""
        assert implied_vol(1.0, 450.0, 400.0, time_to_expiry_years(30), "c") is None

    def test_deep_otm_penny_option_still_solves(self):
        """A cheap far-OTM quote is above intrinsic (0) and must still solve —
        guard against over-eagerly returning None on legitimate cheap wings."""
        iv = implied_vol(0.001, 450.0, 550.0, time_to_expiry_years(30), "c")
        assert iv is not None and iv > 0


class TestGreeks:
    def test_long_call_delta_is_positive(self):
        g = greeks("c", 450.0, 450.0, time_to_expiry_years(30), 0.20)
        assert 0 < g.delta < 1

    def test_long_put_delta_is_negative(self):
        g = greeks("p", 450.0, 450.0, time_to_expiry_years(30), 0.20)
        assert -1 < g.delta < 0

    def test_atm_vega_is_positive(self):
        g = greeks("c", 450.0, 450.0, time_to_expiry_years(30), 0.20)
        assert g.vega > 0

    def test_long_option_theta_is_negative(self):
        """Long options decay."""
        g = greeks("c", 450.0, 450.0, time_to_expiry_years(30), 0.20)
        assert g.theta < 0

    def test_deep_itm_call_delta_approaches_one(self):
        g = greeks("c", 600.0, 400.0, time_to_expiry_years(30), 0.20)
        assert g.delta > 0.95

    def test_greeks_are_per_share_not_per_contract(self):
        """Per-share convention: ATM call delta ≈ 0.5, not ≈ 50.
        Position scaling by the 100x multiplier is the caller's job."""
        g = greeks("c", 450.0, 450.0, time_to_expiry_years(30), 0.20)
        assert g.delta == pytest.approx(0.5, abs=0.1)

    def test_invalid_vol_returns_zeroed_greeks(self):
        """Fail closed: a bad IV yields zeros, never an exception mid-scan."""
        g = greeks("c", 450.0, 450.0, time_to_expiry_years(30), 0.0)
        assert isinstance(g, OptionGreeks)
        assert g.delta == 0.0 and g.vega == 0.0


class TestPositionGreeks:
    """Position-level Greeks: per-share Greeks scaled by side, 100x, contracts."""

    def _spread(self, contracts=1):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from datetime import date, timedelta
        from candidate_builder import OptionQuote, build_bear_call_spread
        exp = date.today() + timedelta(days=30)
        def q(k, r, b, a):
            return OptionQuote(f"SPY{k}{r}", "SPY", k, exp, r, b, a, 500)
        return build_bear_call_spread(q(455, "c", 3.00, 3.10),
                                      q(460, "c", 2.00, 2.10), contracts=contracts)

    def test_short_call_spread_is_net_short_delta(self):
        """Selling the nearer call dominates, so the spread is short delta."""
        from analytics import position_greeks
        delta, _ = position_greeks(self._spread(), spot=450.0)
        assert delta < 0

    def test_scales_with_contract_count(self):
        from analytics import position_greeks
        d1, v1 = position_greeks(self._spread(1), spot=450.0)
        d3, v3 = position_greeks(self._spread(3), spot=450.0)
        assert d3 == pytest.approx(d1 * 3, rel=1e-6)
        assert v3 == pytest.approx(v1 * 3, rel=1e-6)

    def test_vertical_spread_has_near_zero_vega(self):
        """The two legs' vegas nearly cancel — this is why the vega limit
        binds on straddles, not verticals."""
        from analytics import position_greeks
        _, vega = position_greeks(self._spread(), spot=450.0)
        assert abs(vega) < 50

    def test_uses_contract_multiplier(self):
        """Greeks are per share; a position is 100 shares per contract."""
        from analytics import position_greeks
        delta, _ = position_greeks(self._spread(), spot=450.0)
        assert abs(delta) > 1.0   # would be < 1 if the 100x were missing

    def test_nonpositive_spot_returns_the_none_sentinel(self):
        """Renamed from test_unpriceable_leg_yields_zero_not_an_exception, which
        never reached the per-leg branch it claimed to test: spot<=0 short-
        circuits at the top. What it does test is missing spot, and (0.0, 0.0)
        was the wrong answer for that — zero Greeks sail through every limit.
        No spot means no measurement, which must force an abstain."""
        from analytics import position_greeks
        assert position_greeks(self._spread(), spot=0.0) is None

    def test_none_intent_returns_the_none_sentinel(self):
        from analytics import position_greeks
        assert position_greeks(None, spot=450.0) is None

    def test_one_unpriceable_leg_returns_none_not_the_other_legs_greeks(self):
        """The real per-leg case, with a valid spot.

        A long straddle struck at 400 with spot 450: the call is deep ITM and
        its quote sits below intrinsic, so no IV solves it, while the put
        prices fine. Dropping the call and returning the put's Greeks reports
        delta -12.1 for a position whose true delta is roughly +90 — wrong
        sign and wrong magnitude, and it passes the |delta| <= 30 limit that
        the real position would breach. A partial answer is worse than none."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from datetime import date, timedelta
        from analytics import position_greeks, implied_vol, time_to_expiry_years
        from candidate_builder import OptionQuote, build_long_straddle

        exp = date.today() + timedelta(days=30)
        call = OptionQuote("SPY400C", "SPY", 400.0, exp, "c", 3.00, 3.10, 500)
        put = OptionQuote("SPY400P", "SPY", 400.0, exp, "p", 3.00, 3.10, 500)
        straddle = build_long_straddle(call, put)
        assert straddle is not None

        t = time_to_expiry_years(30)
        assert implied_vol(call.mid, 450.0, 400.0, t, "c") is None   # unpriceable
        assert implied_vol(put.mid, 450.0, 400.0, t, "p") is not None  # priceable

        assert position_greeks(straddle, spot=450.0) is None


# ---- atm_implied_vol: ATM IV as a property of the MARKET ---------------
#
# committee/snapshot.py used to derive "ATM implied vol" from whichever
# candidates a caller happened to surface. On a real SPY chain, surfacing
# only bear call spreads sampled only OTM calls — which sit lower on the
# vol skew — and biased the estimate down enough to flip the committee's
# entire regime call (cheap-vol / buy-premium vs rich-vol / sell-premium)
# with the market never moving. This helper instead reads the chain's own
# contracts nearest spot, independent of any candidate construction.

def _quote(strike, right, bid, ask, dte=30, oi=500):
    expiry = date.today() + timedelta(days=dte)
    return OptionQuote(
        symbol=f"SPY{expiry:%y%m%d}{right.upper()}{int(strike*1000):08d}",
        underlying="SPY", strike=strike, expiry=expiry, right=right,
        bid=bid, ask=ask, open_interest=oi,
    )


class TestAtmImpliedVol:
    def test_picks_the_strike_nearest_spot_not_the_chain_extremes(self):
        from vollib.black_scholes import black_scholes
        spot, t = 450.0, time_to_expiry_years(30)
        true_vol = 0.20
        near_call_px = black_scholes("c", spot, 450.0, t, 0.045, true_vol)
        near_put_px = black_scholes("p", spot, 450.0, t, 0.045, true_vol)

        chain = [
            # Deep extremes: if these leaked in, the answer would be wildly
            # different (or fail to solve at all near intrinsic).
            _quote(300.0, "p", 0.01, 0.02),
            _quote(700.0, "c", 0.01, 0.02),
            # The true ATM pair.
            _quote(450.0, "c", near_call_px, near_call_px),
            _quote(450.0, "p", near_put_px, near_put_px),
        ]
        result = atm_implied_vol(chain, spot)
        assert result == pytest.approx(true_vol, abs=1e-3)

    def test_averages_call_and_put_at_the_same_strike(self):
        chain = [_quote(450.0, "c", 12.0, 12.2), _quote(450.0, "p", 11.0, 11.2)]
        call_iv = implied_vol(12.1, 450.0, 450.0, time_to_expiry_years(30), "c")
        put_iv = implied_vol(11.1, 450.0, 450.0, time_to_expiry_years(30), "p")
        assert atm_implied_vol(chain, 450.0) == pytest.approx(
            (call_iv + put_iv) / 2, abs=1e-9
        )

    def test_prefers_the_expiry_nearest_dte_target(self):
        spot = 450.0
        far_dte, near_dte = 45, 30
        # A far-DTE ATM pair priced at a different (wrong-if-picked) vol.
        chain = [
            _quote(450.0, "c", 8.0, 8.2, dte=far_dte),
            _quote(450.0, "p", 8.0, 8.2, dte=far_dte),
            _quote(450.0, "c", 12.0, 12.2, dte=near_dte),
            _quote(450.0, "p", 11.0, 11.2, dte=near_dte),
        ]
        expected = atm_implied_vol(
            [q for q in chain if q.dte == near_dte], spot
        )
        assert atm_implied_vol(chain, spot, dte_target=30) == pytest.approx(expected)

    def test_empty_chain_returns_none(self):
        assert atm_implied_vol([], 450.0) is None

    def test_chain_where_no_iv_solves_returns_none(self):
        # Zero-priced quotes: mid is 0, so no positive vol solves for either
        # leg — the fail-closed "no data" case, not a crash or a guess.
        chain = [_quote(450.0, "c", 0.0, 0.0), _quote(450.0, "p", 0.0, 0.0)]
        assert atm_implied_vol(chain, 450.0) is None


# ── volatility smile fit + richness ─────────────────────────────────────
#
# These test a MEASUREMENT, not an edge. docs/research/smile-feasibility.md
# measured the residual signal on a live SPY chain at ~0.11 vol points, worth
# roughly $8/contract against a multi-dollar spread — smaller than the cost of
# crossing it. So the tests below check that the number is honest (recovers a
# planted deviation, excludes garbage, is deterministic, and reports exactly
# zero when the deviation is inside the quote's own noise), never that it is
# profitable.

SMILE_RATE = 0.045


def _base_vol(m: float) -> float:
    """A realistic skewed smile: level 20%, downward skew, mild curvature."""
    return 0.20 - 0.06 * m + 0.15 * m * m


def _smile_chain(
    spot: float = 450.0,
    dte: int = 30,
    right: str = "c",
    strikes: list[float] | None = None,
    vol_fn=_base_vol,
    bump: tuple[float, float] | None = None,   # (strike, extra vol points)
    spread_frac: float = 0.0,                  # bid/ask = px * (1 -/+ frac)
    oi: int = 500,
) -> list[OptionQuote]:
    """Build a chain from a KNOWN vol function, so every expected answer is
    derivable rather than copied from a golden run."""
    from vollib.black_scholes import black_scholes

    t = time_to_expiry_years(dte)
    root_t = math.sqrt(t)
    expiry = date.today() + timedelta(days=dte)
    if strikes is None:
        strikes = [425.0 + 2.5 * i for i in range(21)]   # 425.0 … 475.0

    quotes = []
    for k in strikes:
        iv = vol_fn(math.log(k / spot) / root_t)
        if bump is not None and abs(k - bump[0]) < 1e-9:
            iv += bump[1]
        px = black_scholes(right, spot, k, t, SMILE_RATE, iv)
        quotes.append(OptionQuote(
            symbol=f"SPY{expiry:%y%m%d}{right.upper()}{int(k * 1000):08d}",
            underlying="SPY", strike=k, expiry=expiry, right=right,
            bid=px * (1.0 - spread_frac), ask=px * (1.0 + spread_frac),
            open_interest=oi,
        ))
    return quotes


class TestFitSmile:
    def test_recovers_a_planted_deviation(self):
        """Plant +5 vol points at one strike; richness must recover it.

        The bumped strike is IN the fit, so least squares pulls the curve
        toward it and the measured residual is (1 - leverage) * bump. With 21
        points and degree 3 the leverage at an interior point is ~0.15-0.25, so
        the recoverable fraction is roughly 0.75-0.9. The stated tolerance is
        therefore 'same sign, between 60% and 105% of the planted size' — an
        honest statement of what a least-squares residual can return, not a
        loosened threshold to make a weak test pass."""
        from analytics import fit_smile, richness

        spot, bump_k, bump_v = 450.0, 460.0, 0.05
        quotes = _smile_chain(spot=spot, bump=(bump_k, bump_v))
        fit = fit_smile(quotes, spot)
        assert fit is not None

        bumped = next(q for q in quotes if q.strike == bump_k)
        r = richness(bumped, fit, spot)
        assert 0.60 * bump_v <= r <= 1.05 * bump_v

        # An untouched strike sits on the curve: no richness worth the name.
        clean = next(q for q in quotes if q.strike == 445.0)
        assert abs(richness(clean, fit, spot)) < 0.01

    def test_fewer_than_min_points_returns_none(self):
        """Never a degenerate fit — below min_points there is no measurement."""
        from analytics import fit_smile
        quotes = _smile_chain(strikes=[445.0, 447.5, 450.0, 452.5])
        assert fit_smile(quotes, 450.0, min_points=8) is None

    def test_absurd_iv_outlier_is_excluded_and_does_not_move_the_fit(self):
        """The live chain carries deep-ITM calls solving to 240% IV. One such
        point would dominate a polynomial. Measured, not hypothetical."""
        from analytics import fit_smile

        spot = 450.0
        clean = _smile_chain(spot=spot)
        baseline = fit_smile(clean, spot)
        assert baseline is not None

        outlier_vol = 2.40
        polluted = clean + _smile_chain(
            spot=spot, strikes=[451.0], vol_fn=lambda m: outlier_vol
        )
        # The outlier really does solve to ~240% — otherwise this tests nothing.
        bad = polluted[-1]
        assert implied_vol(bad.mid, spot, bad.strike,
                           time_to_expiry_years(30), "c") == pytest.approx(
                               outlier_vol, abs=0.02)

        polluted_fit = fit_smile(polluted, spot)
        assert polluted_fit is not None
        assert polluted_fit.n_points == baseline.n_points
        assert polluted_fit.coeffs == baseline.coeffs

    def test_deviation_below_the_half_spread_scores_exactly_zero(self):
        """The gate: a residual smaller than (IV(ask) - IV(bid)) / 2 is noise,
        and noise must score 0.0 exactly, not 'a small number'."""
        from analytics import fit_smile, richness

        spot, k = 450.0, 460.0
        # 5% wide quotes → ~1 vol point of IV half-width near the money.
        quotes = _smile_chain(spot=spot, bump=(k, 0.004), spread_frac=0.05)
        fit = fit_smile(quotes, spot)
        assert fit is not None
        bumped = next(q for q in quotes if q.strike == k)
        assert richness(bumped, fit, spot) == 0.0

        # Same quote width, a deviation an order of magnitude larger: scores.
        loud = _smile_chain(spot=spot, bump=(k, 0.05), spread_frac=0.05)
        loud_fit = fit_smile(loud, spot)
        assert loud_fit is not None
        assert richness(next(q for q in loud if q.strike == k),
                        loud_fit, spot) > 0.0

    def test_is_deterministic_and_order_invariant(self):
        """Identical inputs give identical coefficients; input order is not a
        hidden parameter."""
        import random
        from analytics import fit_smile

        spot = 450.0
        quotes = _smile_chain(spot=spot, bump=(460.0, 0.03))
        a = fit_smile(quotes, spot)
        b = fit_smile(list(quotes), spot)
        shuffled = list(quotes)
        random.Random(7).shuffle(shuffled)
        c = fit_smile(shuffled, spot)

        assert a is not None and b is not None and c is not None
        assert a.coeffs == b.coeffs == c.coeffs
        assert a.rmse == c.rmse and a.n_points == c.n_points

    def test_restricts_to_the_moneyness_band(self):
        """Strikes outside |m| <= band are not fitted — the whole point of the
        band is that a wide fit is misspecified (smile-feasibility.md)."""
        from analytics import fit_smile

        spot = 450.0
        wide = _smile_chain(spot=spot, strikes=[380.0 + 5.0 * i for i in range(30)])
        narrow = fit_smile(wide, spot, band=0.2)
        assert narrow is not None
        assert narrow.n_points < len(wide)
        assert narrow.band == 0.2
        # Widening the band admits strictly more points.
        assert fit_smile(wide, spot, band=1.0).n_points > narrow.n_points

    def test_mixed_expiries_or_rights_returns_none(self):
        """A smile is a per-(expiry, right) object. Silently fitting a subset
        of what the caller handed over would be a wrong answer in the costume
        of a right one — fit_smiles() is the entry point for a whole chain."""
        from analytics import fit_smile, fit_smiles

        spot = 450.0
        calls = _smile_chain(spot=spot, right="c")
        puts = _smile_chain(spot=spot, right="p")
        far = _smile_chain(spot=spot, right="c", dte=45)

        assert fit_smile(calls + puts, spot) is None
        assert fit_smile(calls + far, spot) is None

        fits = fit_smiles(calls + puts + far, spot)
        assert len(fits) == 3
        assert set(fits) == {(calls[0].expiry, "c"), (puts[0].expiry, "p"),
                             (far[0].expiry, "c")}

    def test_residual_outside_the_band_is_none_not_an_extrapolation(self):
        from analytics import fit_smile
        spot = 450.0
        fit = fit_smile(_smile_chain(spot=spot), spot)
        assert fit is not None
        assert fit.residual_for(1000.0, spot, 0.20) is None
        assert fit.residual_for(450.0, spot, None) is None

    def test_nothing_raises_on_junk_input(self):
        """Module contract: every function fails closed."""
        from analytics import fit_smile, fit_smiles, richness
        assert fit_smile([], 450.0) is None
        assert fit_smile(None, 450.0) is None
        assert fit_smile(_smile_chain(), 0.0) is None
        assert fit_smiles([], 450.0) == {}
        assert richness(_smile_chain()[0], None, 450.0) == 0.0
        # The absent-fit sentinel scores no signal, it never raises.
        from analytics import NO_SMILE
        assert NO_SMILE.is_measured is False
        assert NO_SMILE.residual_for(450.0, 450.0, 0.2) is None
        assert richness(_smile_chain()[0], NO_SMILE, 450.0) == 0.0

    def test_richness_of_a_mismatched_contract_is_zero(self):
        """A call's IV measured against the put smile is not a measurement."""
        from analytics import fit_smile, richness
        spot = 450.0
        put_fit = fit_smile(_smile_chain(spot=spot, right="p"), spot)
        assert put_fit is not None
        a_call = _smile_chain(spot=spot, right="c")[10]
        assert richness(a_call, put_fit, spot) == 0.0

    def test_zero_bid_quote_scores_zero_because_its_noise_is_unmeasurable(self):
        from analytics import fit_smile, richness
        spot = 450.0
        quotes = _smile_chain(spot=spot)
        fit = fit_smile(quotes, spot)
        q = quotes[10]
        dead = OptionQuote(q.symbol, q.underlying, q.strike, q.expiry,
                           q.right, 0.0, q.ask, q.open_interest)
        assert richness(dead, fit, spot) == 0.0


# ── realized-vol regime rank ────────────────────────────────────────────

def _alternating_closes(sigmas: list[float], start: float = 100.0) -> pd.DataFrame:
    """Deterministic price path whose per-day |log return| is sigmas[i].

    Alternating sign gives a stationary path with stdev of returns ≈ sigma, so
    the realized-vol reading over any window is known by construction — no RNG,
    no golden numbers."""
    closes, px = [start], start
    for i, s in enumerate(sigmas):
        px *= math.exp(s if i % 2 == 0 else -s)
        closes.append(px)
    return pd.DataFrame({"close": closes})


class TestRealizedVolRank:
    def test_current_vol_at_the_top_of_its_history_ranks_near_one(self):
        from analytics import realized_vol_rank
        bars = _alternating_closes([0.004] * 260 + [0.03] * 25)
        rank = realized_vol_rank(bars)
        assert rank is not None and rank > 0.95

    def test_current_vol_at_the_bottom_of_its_history_ranks_near_zero(self):
        from analytics import realized_vol_rank
        bars = _alternating_closes([0.03] * 260 + [0.004] * 25)
        rank = realized_vol_rank(bars)
        assert rank is not None and rank < 0.05

    def test_mid_history_vol_ranks_near_a_half(self):
        """Half the history calmer than today, half of it wilder.

        Exactly 252 closes so nothing is truncated, and today's vol sits
        between the two regimes. The answer is ~0.43 rather than exactly 0.50
        because the rolling windows that straddle the regime change produce
        blended readings, all of which land above the calm level — an artefact
        of the estimator, stated rather than tuned away."""
        from analytics import realized_vol_rank
        bars = _alternating_closes([0.004] * 113 + [0.030] * 113 + [0.012] * 25)
        rank = realized_vol_rank(bars)
        assert rank is not None and 0.35 < rank < 0.65

    def test_insufficient_history_returns_none_not_a_neutral_default(self):
        """0.5 would be a fabricated reading — the one failure this codebase
        refuses everywhere else."""
        from analytics import realized_vol_rank
        assert realized_vol_rank(_alternating_closes([0.01] * 100)) is None
        assert realized_vol_rank(None) is None
        assert realized_vol_rank(pd.DataFrame()) is None
        assert realized_vol_rank(pd.DataFrame({"open": [1.0] * 400})) is None

    def test_flat_series_does_not_raise_and_returns_one_half(self):
        """Every reading in the history is 0.0, so today's reading is exactly
        as typical as every other — the mid-rank of a fully tied sample is 0.5.
        Callers that need to tell 'typical' from 'no variation at all' apart
        must read realized_volatility itself, which returns 0.0."""
        from analytics import realized_vol_rank, realized_volatility
        bars = pd.DataFrame({"close": [100.0] * 400})
        assert realized_volatility(bars) == 0.0
        assert realized_vol_rank(bars) == 0.5

    def test_rank_is_always_a_probability(self):
        from analytics import realized_vol_rank
        for sigmas in ([0.004] * 300,
                       [0.004] * 150 + [0.03] * 150,
                       [0.03] * 150 + [0.004] * 150,
                       [0.001 * (1 + i % 17) for i in range(400)]):
            rank = realized_vol_rank(_alternating_closes(sigmas))
            assert rank is not None and 0.0 <= rank <= 1.0

    def test_window_controls_how_much_history_is_ranked_against(self):
        """A shock older than the window cannot influence today's rank."""
        from analytics import realized_vol_rank
        recent = [0.004] * 200 + [0.012] * 25
        assert realized_vol_rank(_alternating_closes([0.05] * 300 + recent),
                                 window=200) == pytest.approx(
            realized_vol_rank(_alternating_closes(recent), window=200))
