"""Tests for committee/snapshot.py — the one hashable string analysts see.

Determinism is the whole point: identical inputs must render identical bytes
so prompt_hash-based caching and golden-file replay work. No timestamps, no
dict-ordering leakage, no float-formatting drift.
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from candidate_builder import OptionQuote, build_bull_put_spread, build_bear_call_spread
from committee.snapshot import render_snapshot

EXPIRY = date.today() + timedelta(days=30)


def _q(strike, right, bid, ask, oi=500):
    return OptionQuote(
        symbol=f"SPY{EXPIRY:%y%m%d}{right.upper()}{int(strike*1000):08d}",
        underlying="SPY", strike=strike, expiry=EXPIRY, right=right,
        bid=bid, ask=ask, open_interest=oi,
    )


def _bull_put(short_strike, long_strike):
    intent = build_bull_put_spread(
        _q(short_strike, "p", 2.40, 2.60), _q(long_strike, "p", 1.45, 1.55)
    )
    assert intent is not None
    return intent


def _bear_call(short_strike, long_strike):
    intent = build_bear_call_spread(
        _q(short_strike, "c", 2.40, 2.60), _q(long_strike, "c", 1.45, 1.55)
    )
    assert intent is not None
    return intent


def test_render_is_deterministic_for_identical_inputs():
    candidates = [_bull_put(495, 490), _bear_call(510, 515)]
    a = render_snapshot("SPY", 500.0, 0.18, candidates)
    b = render_snapshot("SPY", 500.0, 0.18, candidates)
    assert a == b


def test_render_is_byte_identical_across_separately_built_equal_candidates():
    a = render_snapshot("SPY", 500.0, 0.18, [_bull_put(495, 490)])
    b = render_snapshot("SPY", 500.0, 0.18, [_bull_put(495, 490)])
    assert a == b


def test_snapshot_contains_underlying_spot_and_vol():
    s = render_snapshot("SPY", 500.1234, 0.1832, [_bull_put(495, 490)])
    assert "SPY" in s
    assert "500.12" in s
    assert "18.32" in s


def test_snapshot_assigns_ids_c1_upwards():
    candidates = [_bull_put(495, 490), _bear_call(510, 515)]
    s = render_snapshot("SPY", 500.0, 0.18, candidates)
    assert "c1" in s
    assert "c2" in s
    assert "c3" not in s


def test_snapshot_includes_structure_legs_credit_loss_profit_breakevens_dte():
    s = render_snapshot("SPY", 500.0, 0.18, [_bull_put(495, 490)])
    assert "bull_put_spread" in s
    assert "495" in s and "490" in s
    assert "credit" in s.lower()
    assert "max_loss" in s.lower() or "max loss" in s.lower()
    assert "max_profit" in s.lower() or "max profit" in s.lower()
    assert "breakeven" in s.lower()
    assert "dte" in s.lower()


def test_max_candidates_caps_the_list_deterministically():
    candidates = [_bull_put(500 - i, 495 - i) for i in range(0, 20, 1) if 500 - i > 495 - i]
    s = render_snapshot("SPY", 500.0, 0.18, candidates, max_candidates=3)
    ids_present = [f"c{i}" for i in range(1, 21) if f"c{i}" in s]
    assert len(ids_present) == 3
    assert ids_present == ["c1", "c2", "c3"]


def test_no_timestamp_leaks_into_snapshot():
    s = render_snapshot("SPY", 500.0, 0.18, [_bull_put(495, 490)])
    # A rendered snapshot must not embed wall-clock time (would break hashing
    # across two calls made a second apart).
    import re
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", s)


def test_empty_candidates_still_renders_without_error():
    s = render_snapshot("SPY", 500.0, 0.18, [])
    assert "SPY" in s
    assert isinstance(s, str)
