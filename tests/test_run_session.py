import os, sys
from datetime import date, timedelta
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from candidate_builder import OptionQuote
from run_session import build_candidates

EXPIRY = date.today() + timedelta(days=30)


def _chain():
    quotes = []
    for strike in (430.0, 435.0, 440.0, 445.0, 455.0, 460.0, 465.0, 470.0):
        for right in ("p", "c"):
            quotes.append(OptionQuote(
                f"SPY{EXPIRY:%y%m%d}{right.upper()}{int(strike*1000):08d}",
                "SPY", strike, EXPIRY, right, 2.00, 2.10, 800))
    return quotes


def test_builds_candidates_from_a_chain():
    assert len(build_candidates(_chain(), "SPY", spot=450.0)) > 0


def test_all_candidates_are_defined_risk():
    for intent in build_candidates(_chain(), "SPY", spot=450.0):
        assert intent.is_defined_risk
        assert intent.max_loss < float("inf")


def test_all_candidates_use_allowed_structures():
    """Read the allowlist from risk.yaml itself, not a copy of it: a builder
    that produced a structure RiskGuard has never heard of would be denied at
    the guard, and a hardcoded set here could not tell the difference."""
    import risk_guard
    allowed = set(risk_guard.load_risk_config(
        os.path.join(os.path.dirname(__file__), "..", "risk.yaml")).allowed_structures)
    for intent in build_candidates(_chain(), "SPY", spot=450.0):
        assert intent.structure in allowed


def test_chain_yields_long_premium_debit_verticals():
    """The structural gap this fixes: before debit verticals existed, the only
    long-premium structure was a long_straddle, whose max_loss exceeded
    risk.yaml's $1,000 cap on every live SPY chain — so the menu was 100%
    short premium and the desk could not express a buy-premium view at all."""
    structures = {i.structure for i in build_candidates(_chain(), "SPY", spot=450.0)}
    assert "bull_call_spread" in structures
    assert "bear_put_spread" in structures


def test_chain_yields_a_non_directional_long_premium_structure():
    """The gap the debit verticals did NOT close: "IV below realized -> buy
    premium" is a view about VOLATILITY, and both debit verticals are
    DIRECTIONAL, so the two-model directional-agreement rule can never be
    satisfied by it. Measured over 10 seeded June-July windows, 7 of 8
    abstentions cited exactly that. The long_iron_butterfly is long premium
    AND direction-neutral AND priced under the cap."""
    structures = {i.structure for i in build_candidates(_chain(), "SPY", spot=450.0)}
    assert "long_iron_butterfly" in structures


def test_long_iron_butterfly_is_a_defined_risk_debit_with_symmetric_wings():
    for intent in build_candidates(_chain(), "SPY", spot=450.0):
        if intent.structure != "long_iron_butterfly":
            continue
        assert intent.net_credit <= 0
        assert intent.is_credit is False
        assert intent.is_defined_risk
        assert intent.max_loss < float("inf")
        bought = sorted(l.quote.strike for l in intent.legs if l.side == "buy")
        sold = sorted(l.quote.strike for l in intent.legs if l.side == "sell")
        assert bought[0] == bought[1]                      # one body strike
        body = bought[0]
        assert sold[0] < body < sold[1]                    # wings straddle it
        assert body - sold[0] == sold[1] - body            # symmetric


def test_debit_verticals_are_priced_as_debits_and_defined_risk():
    for intent in build_candidates(_chain(), "SPY", spot=450.0):
        if intent.structure in ("bull_call_spread", "bear_put_spread"):
            assert intent.net_credit <= 0
            assert intent.is_credit is False
            assert intent.is_defined_risk
            assert intent.max_loss < float("inf")


def test_bull_call_spread_buys_below_and_sells_above():
    for intent in build_candidates(_chain(), "SPY", spot=450.0):
        if intent.structure == "bull_call_spread":
            bought = [l for l in intent.legs if l.side == "buy"][0]
            sold = [l for l in intent.legs if l.side == "sell"][0]
            assert bought.quote.strike < sold.quote.strike
            assert all(l.quote.right == "c" for l in intent.legs)


def test_bear_put_spread_buys_above_and_sells_below():
    for intent in build_candidates(_chain(), "SPY", spot=450.0):
        if intent.structure == "bear_put_spread":
            bought = [l for l in intent.legs if l.side == "buy"][0]
            sold = [l for l in intent.legs if l.side == "sell"][0]
            assert bought.quote.strike > sold.quote.strike
            assert all(l.quote.right == "p" for l in intent.legs)


def test_candidates_are_mutually_distinct():
    """Each candidate must be a genuinely different trade, since the LLM will
    pick among them by id."""
    intents = build_candidates(_chain(), "SPY", spot=450.0)
    seen = {(i.structure, tuple(l.quote.symbol for l in i.legs)) for i in intents}
    assert len(seen) == len(intents)


def test_empty_chain_yields_no_candidates():
    assert build_candidates([], "SPY", spot=450.0) == []


def test_illiquid_chain_yields_no_candidates():
    """Fail closed: a chain that fails liquidity produces nothing to trade."""
    bad = [OptionQuote(f"X{s}{r}", "SPY", s, EXPIRY, r, 2.00, 2.10, 5)
           for s in (440.0, 445.0) for r in ("p", "c")]
    assert build_candidates(bad, "SPY", spot=450.0) == []


def test_multi_expiry_chain_never_pairs_legs_across_expiries():
    """A real chain spans many expiries with overlapping strikes (e.g. SPY has
    ~9 expiries in the 7-45 DTE window, most strikes repeated on each). Legs
    must never be paired across expiries — that would raise inside the
    candidate_builder ('...legs must share an expiry') and crash the whole
    session instead of abstaining."""
    near = date.today() + timedelta(days=10)
    far = date.today() + timedelta(days=40)
    quotes = []
    for expiry in (near, far):
        for strike in (440.0, 445.0, 455.0, 460.0):
            for right in ("p", "c"):
                quotes.append(OptionQuote(
                    f"SPY{expiry:%y%m%d}{right.upper()}{int(strike*1000):08d}",
                    "SPY", strike, expiry, right, 2.00, 2.10, 800))

    intents = build_candidates(quotes, "SPY", spot=450.0)
    assert len(intents) > 0
    for intent in intents:
        expiries = {leg.quote.expiry for leg in intent.legs}
        assert len(expiries) == 1
