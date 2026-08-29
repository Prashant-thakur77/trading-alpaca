"""Tests for candidate_builder.py — deterministic TradeIntent construction.

Hard rule 1: this module fully specifies strikes, quantities and prices.
LLMs only pick one of these candidates or ABSTAIN — they never build one.
Hard rule 3: defined-risk structures only; no naked short options.
"""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from candidate_builder import (
    OptionQuote,
    TradeIntent,
    build_bull_put_spread,
    build_bear_call_spread,
    build_bull_call_spread,
    build_bear_put_spread,
    build_iron_condor,
    build_long_straddle,
    passes_liquidity,
)

EXPIRY = date.today() + timedelta(days=30)


def _q(strike: float, right: str, bid: float, ask: float, oi: int = 500) -> OptionQuote:
    return OptionQuote(
        symbol=f"SPY{EXPIRY:%y%m%d}{right.upper()}{int(strike*1000):08d}",
        underlying="SPY",
        strike=strike,
        expiry=EXPIRY,
        right=right.lower(),
        bid=bid,
        ask=ask,
        open_interest=oi,
    )


class TestOptionQuote:
    def test_mid_is_bid_ask_midpoint(self):
        assert _q(450, "p", 2.00, 2.20).mid == pytest.approx(2.10)

    def test_spread_pct_of_mid(self):
        # spread 0.20 on mid 2.10 => 9.52%
        assert _q(450, "p", 2.00, 2.20).spread_pct == pytest.approx(0.20 / 2.10)

    def test_zero_mid_is_maximally_wide(self):
        """A 0/0 quote must never look tight; it is untradeable."""
        assert _q(450, "p", 0.0, 0.0).spread_pct == float("inf")

    def test_dte_counts_days_to_expiry(self):
        assert _q(450, "p", 1.0, 1.1).dte == 30


class TestLiquidityFilter:
    def test_accepts_tight_liquid_quote(self):
        assert passes_liquidity(_q(450, "p", 2.00, 2.10, oi=500)) is True

    def test_rejects_wide_spread(self):
        """spread > 10% of mid — risk.yaml limit."""
        assert passes_liquidity(_q(450, "p", 2.00, 2.60, oi=500)) is False

    def test_rejects_low_open_interest(self):
        assert passes_liquidity(_q(450, "p", 2.00, 2.10, oi=99)) is False

    def test_rejects_dte_below_floor(self):
        near = OptionQuote("X", "SPY", 450, date.today() + timedelta(days=3),
                           "p", 2.00, 2.10, 500)
        assert passes_liquidity(near) is False

    def test_rejects_dte_above_ceiling(self):
        far = OptionQuote("X", "SPY", 450, date.today() + timedelta(days=60),
                          "p", 2.00, 2.10, 500)
        assert passes_liquidity(far) is False


class TestBullPutSpread:
    """Sell the higher put, buy the lower put. Credit, defined risk."""

    def _spread(self, contracts: int = 1) -> TradeIntent:
        short_leg = _q(445, "p", 3.00, 3.10)   # sold
        long_leg = _q(440, "p", 2.00, 2.10)    # bought
        return build_bull_put_spread(short_leg, long_leg, contracts=contracts)

    def test_builds_two_legs(self):
        assert len(self._spread().legs) == 2

    def test_sells_the_higher_strike(self):
        intent = self._spread()
        sold = [l for l in intent.legs if l.side == "sell"][0]
        bought = [l for l in intent.legs if l.side == "buy"][0]
        assert sold.quote.strike > bought.quote.strike

    def test_is_a_net_credit(self):
        # sell mid 3.05, buy mid 2.05 => credit 1.00/share
        assert self._spread().net_credit == pytest.approx(1.00)

    def test_max_loss_is_width_minus_credit(self):
        """Width 5.00 - credit 1.00 = 4.00/share = $400 per contract."""
        assert self._spread().max_loss == pytest.approx(400.0)

    def test_max_profit_is_the_credit(self):
        assert self._spread().max_profit == pytest.approx(100.0)

    def test_scales_with_contracts(self):
        assert self._spread(contracts=3).max_loss == pytest.approx(1200.0)

    def test_breakeven_is_short_strike_minus_credit(self):
        assert self._spread().breakevens == pytest.approx([444.0])

    def test_is_defined_risk(self):
        """Hard rule 3 — max loss is finite and every short leg is covered."""
        assert self._spread().is_defined_risk is True

    def test_rejects_inverted_strikes(self):
        """Short strike below long strike is not a bull put spread."""
        with pytest.raises(ValueError):
            build_bull_put_spread(_q(440, "p", 2.0, 2.1), _q(445, "p", 3.0, 3.1))

    def test_rejects_calls(self):
        with pytest.raises(ValueError):
            build_bull_put_spread(_q(445, "c", 3.0, 3.1), _q(440, "c", 2.0, 2.1))

    def test_rejects_illiquid_leg(self):
        """Fail closed: an illiquid leg yields no candidate at all."""
        assert build_bull_put_spread(_q(445, "p", 3.0, 3.1, oi=10),
                                     _q(440, "p", 2.0, 2.1)) is None


class TestBearCallSpread:
    """Sell the lower call, buy the higher call. Credit, defined risk."""

    def _spread(self) -> TradeIntent:
        return build_bear_call_spread(_q(455, "c", 3.00, 3.10), _q(460, "c", 2.00, 2.10))

    def test_sells_the_lower_strike(self):
        intent = self._spread()
        sold = [l for l in intent.legs if l.side == "sell"][0]
        bought = [l for l in intent.legs if l.side == "buy"][0]
        assert sold.quote.strike < bought.quote.strike

    def test_max_loss_is_width_minus_credit(self):
        assert self._spread().max_loss == pytest.approx(400.0)

    def test_breakeven_is_short_strike_plus_credit(self):
        assert self._spread().breakevens == pytest.approx([456.0])

    def test_rejects_inverted_strikes(self):
        with pytest.raises(ValueError):
            build_bear_call_spread(_q(460, "c", 2.0, 2.1), _q(455, "c", 3.0, 3.1))


class TestIronCondor:
    """Bull put spread + bear call spread. Credit, defined risk both sides."""

    def _condor(self) -> TradeIntent:
        return build_iron_condor(
            short_put=_q(445, "p", 3.00, 3.10), long_put=_q(440, "p", 2.00, 2.10),
            short_call=_q(455, "c", 3.00, 3.10), long_call=_q(460, "c", 2.00, 2.10),
        )

    def test_builds_four_legs(self):
        assert len(self._condor().legs) == 4

    def test_collects_both_credits(self):
        assert self._condor().net_credit == pytest.approx(2.00)

    def test_max_loss_uses_widest_side_only(self):
        """Only one side can lose. Width 5.00 - total credit 2.00 = $300."""
        assert self._condor().max_loss == pytest.approx(300.0)

    def test_has_two_breakevens(self):
        assert len(self._condor().breakevens) == 2

    def test_rejects_overlapping_wings(self):
        """Short call must sit above the short put."""
        with pytest.raises(ValueError):
            build_iron_condor(
                short_put=_q(455, "p", 3.0, 3.1), long_put=_q(450, "p", 2.0, 2.1),
                short_call=_q(445, "c", 3.0, 3.1), long_call=_q(460, "c", 2.0, 2.1),
            )


class TestLongStraddle:
    """Buy call + buy put at the same strike. Debit, loss capped at premium."""

    def _straddle(self) -> TradeIntent:
        return build_long_straddle(_q(450, "c", 5.00, 5.10), _q(450, "p", 4.00, 4.10))

    def test_both_legs_are_bought(self):
        assert all(l.side == "buy" for l in self._straddle().legs)

    def test_is_a_net_debit(self):
        """Debit is a negative credit: -(5.05 + 4.05)."""
        assert self._straddle().net_credit == pytest.approx(-9.10)

    def test_max_loss_is_the_premium_paid(self):
        assert self._straddle().max_loss == pytest.approx(910.0)

    def test_max_profit_is_unbounded(self):
        assert self._straddle().max_profit == float("inf")

    def test_has_two_breakevens(self):
        assert self._straddle().breakevens == pytest.approx([440.90, 459.10])

    def test_rejects_mismatched_strikes(self):
        with pytest.raises(ValueError):
            build_long_straddle(_q(450, "c", 5.0, 5.1), _q(445, "p", 4.0, 4.1))


class TestBullCallSpread:
    """Buy the lower call, sell the higher call. DEBIT, defined risk.

    The long-premium structure the desk was missing. A 5-wide SPY vertical
    costs the debit paid ($150-$350 live), not the $2,270+ a straddle costs,
    so it clears risk.yaml's $1,000 max_loss_per_position and can actually
    be surfaced when implied vol sits BELOW realized.
    """

    def _spread(self, contracts: int = 1) -> TradeIntent:
        long_leg = _q(450, "c", 5.00, 5.10)    # bought, mid 5.05
        short_leg = _q(455, "c", 3.00, 3.10)   # sold,   mid 3.05
        return build_bull_call_spread(long_leg, short_leg, contracts=contracts)

    def test_builds_two_legs(self):
        assert len(self._spread().legs) == 2

    def test_buys_the_lower_strike(self):
        intent = self._spread()
        bought = [l for l in intent.legs if l.side == "buy"][0]
        sold = [l for l in intent.legs if l.side == "sell"][0]
        assert bought.quote.strike < sold.quote.strike

    def test_is_a_net_debit(self):
        """Debit is a NEGATIVE credit, matching build_long_straddle."""
        assert self._spread().net_credit == pytest.approx(-2.00)

    def test_is_not_a_credit_trade(self):
        assert self._spread().is_credit is False

    def test_max_loss_is_the_debit_paid(self):
        """Debit 2.00/share = $200 per contract — under the $1,000 cap."""
        assert self._spread().max_loss == pytest.approx(200.0)

    def test_max_profit_is_width_minus_debit(self):
        """Width 5.00 - debit 2.00 = 3.00/share = $300 per contract."""
        assert self._spread().max_profit == pytest.approx(300.0)

    def test_breakeven_is_long_strike_plus_debit(self):
        assert self._spread().breakevens == pytest.approx([452.0])

    def test_scales_with_contracts(self):
        intent = self._spread(contracts=3)
        assert intent.max_loss == pytest.approx(600.0)
        assert intent.max_profit == pytest.approx(900.0)

    def test_is_defined_risk(self):
        assert self._spread().is_defined_risk is True

    def test_clears_the_thousand_dollar_cap_a_straddle_cannot(self):
        """The whole point of this structure: a long-premium view the guard
        will actually let through. The ATM straddle on the same strikes costs
        multiples of the vertical's debit."""
        straddle = build_long_straddle(_q(450, "c", 5.00, 5.10), _q(450, "p", 4.00, 4.10))
        assert straddle.max_loss > self._spread().max_loss

    def test_rejects_inverted_strikes(self):
        """Buying the higher strike is not a bull call spread."""
        with pytest.raises(ValueError):
            build_bull_call_spread(_q(455, "c", 3.0, 3.1), _q(450, "c", 5.0, 5.1))

    def test_rejects_equal_strikes(self):
        with pytest.raises(ValueError):
            build_bull_call_spread(_q(450, "c", 5.0, 5.1), _q(450, "c", 5.0, 5.1))

    def test_rejects_puts(self):
        with pytest.raises(ValueError):
            build_bull_call_spread(_q(450, "p", 5.0, 5.1), _q(455, "p", 3.0, 3.1))

    def test_rejects_mismatched_expiries(self):
        other = OptionQuote(
            symbol="SPYOTHER", underlying="SPY", strike=455.0,
            expiry=EXPIRY + timedelta(days=7), right="c",
            bid=3.0, ask=3.1, open_interest=500,
        )
        with pytest.raises(ValueError):
            build_bull_call_spread(_q(450, "c", 5.0, 5.1), other)

    def test_rejects_illiquid_leg(self):
        """Fail closed: an illiquid leg yields no candidate at all."""
        assert build_bull_call_spread(_q(450, "c", 5.0, 5.1, oi=10),
                                      _q(455, "c", 3.0, 3.1)) is None


class TestBearPutSpread:
    """Buy the higher put, sell the lower put. DEBIT, defined risk."""

    def _spread(self, contracts: int = 1) -> TradeIntent:
        long_leg = _q(450, "p", 5.00, 5.10)    # bought, mid 5.05
        short_leg = _q(445, "p", 3.00, 3.10)   # sold,   mid 3.05
        return build_bear_put_spread(long_leg, short_leg, contracts=contracts)

    def test_builds_two_legs(self):
        assert len(self._spread().legs) == 2

    def test_buys_the_higher_strike(self):
        intent = self._spread()
        bought = [l for l in intent.legs if l.side == "buy"][0]
        sold = [l for l in intent.legs if l.side == "sell"][0]
        assert bought.quote.strike > sold.quote.strike

    def test_is_a_net_debit(self):
        assert self._spread().net_credit == pytest.approx(-2.00)

    def test_is_not_a_credit_trade(self):
        assert self._spread().is_credit is False

    def test_max_loss_is_the_debit_paid(self):
        assert self._spread().max_loss == pytest.approx(200.0)

    def test_max_profit_is_width_minus_debit(self):
        assert self._spread().max_profit == pytest.approx(300.0)

    def test_breakeven_is_long_strike_minus_debit(self):
        assert self._spread().breakevens == pytest.approx([448.0])

    def test_scales_with_contracts(self):
        intent = self._spread(contracts=3)
        assert intent.max_loss == pytest.approx(600.0)
        assert intent.max_profit == pytest.approx(900.0)

    def test_is_defined_risk(self):
        assert self._spread().is_defined_risk is True

    def test_rejects_inverted_strikes(self):
        """Buying the lower strike is not a bear put spread."""
        with pytest.raises(ValueError):
            build_bear_put_spread(_q(445, "p", 3.0, 3.1), _q(450, "p", 5.0, 5.1))

    def test_rejects_equal_strikes(self):
        with pytest.raises(ValueError):
            build_bear_put_spread(_q(450, "p", 5.0, 5.1), _q(450, "p", 5.0, 5.1))

    def test_rejects_calls(self):
        with pytest.raises(ValueError):
            build_bear_put_spread(_q(450, "c", 5.0, 5.1), _q(445, "c", 3.0, 3.1))

    def test_rejects_mismatched_expiries(self):
        other = OptionQuote(
            symbol="SPYOTHER", underlying="SPY", strike=445.0,
            expiry=EXPIRY + timedelta(days=7), right="p",
            bid=3.0, ask=3.1, open_interest=500,
        )
        with pytest.raises(ValueError):
            build_bear_put_spread(_q(450, "p", 5.0, 5.1), other)

    def test_rejects_illiquid_leg(self):
        assert build_bear_put_spread(_q(450, "p", 5.0, 5.1, oi=10),
                                     _q(445, "p", 3.0, 3.1)) is None


class TestDebitVerticalPayoffIdentity:
    """Max loss + max profit must equal the full width, for both debit
    verticals — the identity that catches a sign error in either term."""

    def test_bull_call_terms_sum_to_the_width(self):
        i = build_bull_call_spread(_q(450, "c", 5.00, 5.10), _q(455, "c", 3.00, 3.10))
        assert i.max_loss + i.max_profit == pytest.approx(5.0 * 100)

    def test_bear_put_terms_sum_to_the_width(self):
        i = build_bear_put_spread(_q(450, "p", 5.00, 5.10), _q(445, "p", 3.00, 3.10))
        assert i.max_loss + i.max_profit == pytest.approx(5.0 * 100)

    def test_breakeven_sits_between_the_strikes(self):
        bull = build_bull_call_spread(_q(450, "c", 5.00, 5.10), _q(455, "c", 3.00, 3.10))
        bear = build_bear_put_spread(_q(450, "p", 5.00, 5.10), _q(445, "p", 3.00, 3.10))
        assert 450.0 < bull.breakevens[0] < 455.0
        assert 445.0 < bear.breakevens[0] < 450.0


class TestTradeIntentContract:
    """Every candidate must be fully specified — nothing left for an LLM to fill."""

    def test_every_leg_has_a_concrete_order(self):
        intent = build_bull_put_spread(_q(445, "p", 3.0, 3.1), _q(440, "p", 2.0, 2.1),
                                       contracts=2)
        for leg in intent.legs:
            assert leg.side in ("buy", "sell")
            assert leg.quote.symbol
            assert leg.quote.strike > 0
            assert leg.contracts == 2

    def test_no_naked_short_legs(self):
        """Hard rule 3: every short leg is covered by a long leg of the same right."""
        for intent in (
            build_bull_put_spread(_q(445, "p", 3.0, 3.1), _q(440, "p", 2.0, 2.1)),
            build_bear_call_spread(_q(455, "c", 3.0, 3.1), _q(460, "c", 2.0, 2.1)),
            build_iron_condor(
                short_put=_q(445, "p", 3.0, 3.1), long_put=_q(440, "p", 2.0, 2.1),
                short_call=_q(455, "c", 3.0, 3.1), long_call=_q(460, "c", 2.0, 2.1)),
            build_bull_call_spread(_q(450, "c", 5.0, 5.1), _q(455, "c", 3.0, 3.1)),
            build_bear_put_spread(_q(450, "p", 5.0, 5.1), _q(445, "p", 3.0, 3.1)),
        ):
            shorts = [l for l in intent.legs if l.side == "sell"]
            longs = [l for l in intent.legs if l.side == "buy"]
            for s in shorts:
                assert any(l.quote.right == s.quote.right for l in longs)

    def test_max_loss_is_always_finite(self):
        intent = build_long_straddle(_q(450, "c", 5.0, 5.1), _q(450, "p", 4.0, 4.1))
        assert intent.max_loss < float("inf")

    def test_intent_is_frozen(self):
        intent = build_bull_put_spread(_q(445, "p", 3.0, 3.1), _q(440, "p", 2.0, 2.1))
        with pytest.raises(Exception):
            intent.max_loss = 0.0  # type: ignore
