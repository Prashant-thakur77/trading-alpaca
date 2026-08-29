"""Tests for analytics.py — realized vol, implied vol, Greeks.

Greeks feed RiskGuard's net-delta/net-vega limits, so sign conventions and
per-contract scaling are load-bearing, not cosmetic.
"""
import math
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from analytics import (
    realized_volatility,
    implied_vol,
    greeks,
    OptionGreeks,
    time_to_expiry_years,
)


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
