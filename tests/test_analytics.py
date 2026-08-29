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
