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
    allowed = {"bull_put_spread", "bear_call_spread", "iron_condor", "long_straddle"}
    for intent in build_candidates(_chain(), "SPY", spot=450.0):
        assert intent.structure in allowed


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
