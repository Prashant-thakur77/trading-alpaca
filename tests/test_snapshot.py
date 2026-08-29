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
from committee.snapshot import Snapshot, render_snapshot

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
    assert a.text == b.text


def test_render_is_byte_identical_across_separately_built_equal_candidates():
    a = render_snapshot("SPY", 500.0, 0.18, [_bull_put(495, 490)])
    b = render_snapshot("SPY", 500.0, 0.18, [_bull_put(495, 490)])
    assert a.text == b.text


def test_snapshot_contains_underlying_spot_and_vol():
    s = render_snapshot("SPY", 500.1234, 0.1832, [_bull_put(495, 490)])
    assert "SPY" in s.text
    assert "500.12" in s.text
    assert "18.32" in s.text


def test_snapshot_assigns_ids_c1_upwards():
    candidates = [_bull_put(495, 490), _bear_call(510, 515)]
    s = render_snapshot("SPY", 500.0, 0.18, candidates)
    assert "c1" in s.text
    assert "c2" in s.text
    assert "c3" not in s.text


def test_snapshot_includes_structure_legs_credit_loss_profit_breakevens_dte():
    s = render_snapshot("SPY", 500.0, 0.18, [_bull_put(495, 490)])
    text = s.text
    assert "bull_put_spread" in text
    assert "495" in text and "490" in text
    assert "credit" in text.lower()
    assert "max_loss" in text.lower() or "max loss" in text.lower()
    assert "max_profit" in text.lower() or "max profit" in text.lower()
    assert "breakeven" in text.lower()
    assert "dte" in text.lower()


def test_max_candidates_caps_the_list_deterministically():
    candidates = [_bull_put(500 - i, 495 - i) for i in range(0, 20, 1) if 500 - i > 495 - i]
    s = render_snapshot("SPY", 500.0, 0.18, candidates, max_candidates=3)
    ids_present = [f"c{i}" for i in range(1, 21) if f"c{i}" in s.text]
    assert len(ids_present) == 3
    assert ids_present == ["c1", "c2", "c3"]


def test_no_timestamp_leaks_into_snapshot():
    s = render_snapshot("SPY", 500.0, 0.18, [_bull_put(495, 490)])
    # A rendered snapshot must not embed wall-clock time (would break hashing
    # across two calls made a second apart).
    import re
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", s.text)


# ---- the vol analyst's decision variables must actually be in the text ----
#
# The vol analyst is asked to judge implied-vs-realized vol and liquidity.
# Against a snapshot that carried neither, it abstained with "Implied
# volatility data is not provided" every time — a committee permanently
# biased toward ABSTAIN. Everything below already exists on OptionQuote
# (bid/ask/open_interest) or is computable from it (analytics.implied_vol).

def test_each_leg_renders_implied_vol_open_interest_and_quote_width():
    snap = render_snapshot("SPY", 500.0, 0.18, [_bull_put(495, 490)])
    text = snap.text
    assert "iv=" in text
    assert "oi=500" in text
    assert "bid=" in text and "ask=" in text
    assert "width=" in text


def test_header_reports_implied_vol_and_the_iv_minus_realized_spread():
    snap = render_snapshot("SPY", 500.0, 0.18, [_bull_put(495, 490)])
    text = snap.text
    assert "IMPLIED_VOL_ATM:" in text
    assert "IV_MINUS_REALIZED:" in text
    # the spread is a real number here, not a placeholder
    line = [l for l in text.splitlines() if l.startswith("IV_MINUS_REALIZED:")][0]
    assert "unavailable" not in line
    assert "pp" in line


def test_unsolvable_iv_is_rendered_as_explicitly_unavailable_not_omitted():
    # A quote priced far below intrinsic: no IV solves. The analyst must be
    # able to tell "no data" from "low vol", so the field is present and says
    # so rather than vanishing.
    from candidate_builder import Leg, TradeIntent
    bad = OptionQuote(symbol="SPYBAD", underlying="SPY", strike=100.0,
                      expiry=EXPIRY, right="c", bid=0.01, ask=0.02,
                      open_interest=500)
    intent = TradeIntent(
        underlying="SPY", structure="bull_put_spread",
        legs=(Leg(bad, "sell", 1), Leg(bad, "buy", 1)),
        contracts=1, net_credit=0.0, max_loss=100.0, max_profit=10.0,
        breakevens=(100.0,), dte=30,
    )
    snap = render_snapshot("SPY", 500.0, 0.18, [intent])
    assert "iv=unavailable" in snap.text
    assert "IMPLIED_VOL_ATM: unavailable" in snap.text
    assert "IV_MINUS_REALIZED: unavailable" in snap.text


def test_implied_vol_rendering_stays_deterministic():
    a = render_snapshot("SPY", 500.0, 0.18, [_bull_put(495, 490), _bear_call(510, 515)])
    b = render_snapshot("SPY", 500.0, 0.18, [_bear_call(510, 515), _bull_put(495, 490)])
    assert a.text == b.text
    # and rounded, not repr-drifted
    import re
    for value in re.findall(r"iv=([0-9.]+)%", a.text):
        assert len(value.split(".")[1]) == 2


# ---- the id -> TradeIntent mapping is part of the return value ----
#
# Hard rule 1 says an LLM may only pick an id that deterministic code built.
# If render_snapshot returned only text, every orchestrator would have to
# re-derive the same sort and cap to turn a validated "c3" back into a
# TradeIntent — and an orchestrator that indexed its own unsorted candidate
# list instead would send a DIFFERENT trade than the one the id names, with
# every guard still reporting PASS. Exactly one piece of code must own the id
# assignment, so it is returned alongside the text.

def test_render_returns_text_and_id_to_intent_mapping():
    candidates = [_bull_put(495, 490), _bear_call(510, 515)]
    snap = render_snapshot("SPY", 500.0, 0.18, candidates)
    assert isinstance(snap, Snapshot)
    assert isinstance(snap.text, str)
    assert set(snap.candidates) == {"c1", "c2"}
    assert set(snap.candidates.values()) == set(candidates)
    assert snap.candidate_ids == ["c1", "c2"]


def test_id_rendered_in_text_names_the_intent_the_mapping_returns():
    candidates = [_bull_put(495, 490), _bear_call(510, 515)]
    snap = render_snapshot("SPY", 500.0, 0.18, candidates)
    for cid, intent in snap.candidates.items():
        block = [b for b in snap.text.split("\n") if b.startswith(f"{cid} |")]
        assert len(block) == 1, cid
        assert intent.structure in block[0]
        assert f"DTE={intent.dte}" in block[0]
        # the legs rendered under this id are this intent's legs
        legs_line = snap.text.split(f"{cid} |")[1].split("\n")[1]
        for leg in intent.legs:
            assert f"{leg.quote.strike:.2f}{leg.quote.right}" in legs_line


def test_shuffling_the_input_does_not_change_which_intent_an_id_refers_to():
    candidates = [_bull_put(495, 490), _bear_call(510, 515), _bull_put(480, 475)]
    baseline = render_snapshot("SPY", 500.0, 0.18, candidates)
    for order in ([2, 0, 1], [1, 2, 0], [2, 1, 0]):
        shuffled = render_snapshot("SPY", 500.0, 0.18, [candidates[i] for i in order])
        assert shuffled.text == baseline.text
        assert shuffled.candidates == baseline.candidates


def test_mapping_only_contains_the_capped_candidates():
    candidates = [_bull_put(500 - i, 495 - i) for i in range(0, 20)]
    snap = render_snapshot("SPY", 500.0, 0.18, candidates, max_candidates=3)
    assert list(snap.candidates) == ["c1", "c2", "c3"]
    for cid, intent in snap.candidates.items():
        assert intent in candidates


def test_ties_on_every_other_field_are_broken_by_contract_count():
    # max_loss/max_profit render from `contracts`, so two candidates identical
    # apart from size are NOT interchangeable; leaving contracts out of the
    # sort key let their order fall back to input order, falsifying the
    # module's determinism claim.
    from dataclasses import replace
    one = _bull_put(495, 490)
    three = replace(one, contracts=3, max_loss=one.max_loss * 3,
                    max_profit=one.max_profit * 3)
    a = render_snapshot("SPY", 500.0, 0.18, [one, three])
    b = render_snapshot("SPY", 500.0, 0.18, [three, one])
    assert a.text == b.text
    assert a.candidates["c1"].contracts == b.candidates["c1"].contracts


def test_empty_candidates_still_renders_without_error():
    s = render_snapshot("SPY", 500.0, 0.18, [])
    assert "SPY" in s.text
    assert isinstance(s.text, str)
    assert s.candidates == {}
