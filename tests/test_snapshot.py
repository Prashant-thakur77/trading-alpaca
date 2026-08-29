"""Tests for committee/snapshot.py — the one hashable string analysts see.

Determinism is the whole point: identical inputs must render identical bytes
so prompt_hash-based caching and golden-file replay work. No timestamps, no
dict-ordering leakage, no float-formatting drift.
"""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from candidate_builder import (
    OptionQuote, build_bull_put_spread, build_bear_call_spread, build_long_straddle,
    build_bull_call_spread, build_bear_put_spread, build_long_iron_butterfly,
)
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


def _bull_call(long_strike, short_strike):
    """A long-premium DEBIT vertical: buy the lower call, sell the higher."""
    intent = build_bull_call_spread(
        _q(long_strike, "c", 2.40, 2.60), _q(short_strike, "c", 1.45, 1.55)
    )
    assert intent is not None
    return intent


def _bear_put(long_strike, short_strike):
    """A long-premium DEBIT vertical: buy the higher put, sell the lower."""
    intent = build_bear_put_spread(
        _q(long_strike, "p", 2.40, 2.60), _q(short_strike, "p", 1.45, 1.55)
    )
    assert intent is not None
    return intent


def _straddle(strike):
    intent = build_long_straddle(_q(strike, "c", 2.40, 2.60), _q(strike, "p", 2.40, 2.60))
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
        # net_credit=1.0 against max_loss=100.0 clears the credit-to-risk
        # floor (see committee/snapshot.MIN_CREDIT_TO_RISK) comfortably --
        # this fixture is about unsolvable IV rendering, not credit
        # economics, so it must not incidentally trip an unrelated filter.
        contracts=1, net_credit=1.0, max_loss=100.0, max_profit=100.0,
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
    #
    # NOTE: sized at contracts=2 (max_loss $800), not the original 3 (would be
    # $1200): the ranking fix (see the "guard-certain-denial" tests below)
    # now drops any candidate whose own max_loss exceeds risk.yaml's
    # max_loss_per_position ($1000) before ranking even runs. At contracts=3
    # `three` would be silently filtered out of both renders, leaving only
    # `one` in each — the assertions below would still pass, but on a single
    # surviving candidate, no longer exercising a real tie at all.
    from dataclasses import replace
    one = _bull_put(495, 490)
    three = replace(one, contracts=2, max_loss=one.max_loss * 2,
                    max_profit=one.max_profit * 2)
    a = render_snapshot("SPY", 500.0, 0.18, [one, three])
    b = render_snapshot("SPY", 500.0, 0.18, [three, one])
    assert a.text == b.text
    assert a.candidates["c1"].contracts == b.candidates["c1"].contracts


def test_empty_candidates_still_renders_without_error():
    s = render_snapshot("SPY", 500.0, 0.18, [])
    assert "SPY" in s.text
    assert isinstance(s.text, str)
    assert s.candidates == {}


# ---- the IV-vs-realized sign convention must be unmisreadable ----
#
# Live run 2026-08-29: vol_analyst and bear_adversary drew OPPOSITE
# conclusions from the SAME number. The snapshot said
# "IV_MINUS_REALIZED: -0.69pp"; vol_analyst called that "a structural edge
# for short premium" while bear_adversary called it "the market underpricing
# volatility". The bear is right — implied BELOW realized means options are
# cheap versus how much the underlying actually moves, so a premium SELLER
# collects less than the movement warrants. The header now renders the signed
# number AND its definitional meaning, so the sign cannot be read backwards.
# It deliberately does NOT tell an analyst what to conclude overall.

def test_iv_above_realized_is_labelled_rich_and_favouring_selling():
    snap = render_snapshot("SPY", 500.0, 0.01, [_bull_put(495, 490)])
    line = [l for l in snap.text.splitlines()
            if l.startswith("IV_MINUS_REALIZED:")][0]
    assert line.startswith("IV_MINUS_REALIZED: +")
    assert "ABOVE" in line
    assert "rich" in line.lower()
    assert "SELLING" in line


def test_iv_below_realized_is_labelled_cheap_and_favouring_buying():
    snap = render_snapshot("SPY", 500.0, 0.90, [_bull_put(495, 490)])
    line = [l for l in snap.text.splitlines()
            if l.startswith("IV_MINUS_REALIZED:")][0]
    assert line.startswith("IV_MINUS_REALIZED: -")
    assert "BELOW" in line
    assert "cheap" in line.lower()
    # the correction: cheap options favour BUYING premium, not selling it
    assert "BUYING" in line
    assert "not selling" in line.lower()


def test_the_convention_line_never_prescribes_a_verdict():
    """The header states a definition, not a recommendation. It must not tell
    the committee which candidate to pick or that it should trade at all."""
    snap = render_snapshot("SPY", 500.0, 0.90, [_bull_put(495, 490)])
    line = [l for l in snap.text.splitlines()
            if l.startswith("IV_MINUS_REALIZED:")][0].lower()
    for prescription in ("you should", "recommend", "pick ", "abstain"):
        assert prescription not in line


def test_the_sign_convention_line_stays_deterministic():
    a = render_snapshot("SPY", 500.0, 0.90, [_bull_put(495, 490)])
    b = render_snapshot("SPY", 500.0, 0.90, [_bull_put(495, 490)])
    assert a.text == b.text


# ---- the cap must be stratified by structure, not a global top-N ----
#
# On a real SPY chain (2026-08-29) `build_candidates` produced 440
# bull_put_spread, 188 bear_call_spread and 4 long_straddle candidates. A
# global top-N by `_candidate_sort_key` — which sorts by structure name
# first — surfaced 12 bear_call_spread and NOTHING else: the committee chose
# from a menu with one dish, and abstained with "every candidate is a bear
# call spread, the wrong direction" in a regime (IV below realized) where the
# correct structure — long straddle — was structurally invisible. The cap
# must give every present structure type a fair, round-robin share so the
# committee can actually see its options.

def _many_bull_puts(n, start=500):
    return [_bull_put(start - i, start - 5 - i) for i in range(n)]


def _many_bear_calls(n, start=500):
    return [_bear_call(start + i, start + 5 + i) for i in range(n)]


def _many_straddles(n, start=500):
    return [_straddle(start + 5 * i) for i in range(n)]


def test_every_present_structure_type_appears_in_the_surfaced_set():
    candidates = _many_bull_puts(5) + _many_bear_calls(5) + _many_straddles(2)
    s = render_snapshot("SPY", 500.0, 0.18, candidates, max_candidates=6)
    structures = {intent.structure for intent in s.candidates.values()}
    assert structures == {"bull_put_spread", "bear_call_spread", "long_straddle"}


def test_single_structure_present_still_fills_the_cap():
    candidates = _many_bull_puts(20)
    s = render_snapshot("SPY", 500.0, 0.18, candidates, max_candidates=5)
    assert len(s.candidates) == 5
    assert all(i.structure == "bull_put_spread" for i in s.candidates.values())


def test_shuffling_the_input_does_not_change_the_stratified_surfaced_set():
    candidates = _many_bull_puts(10) + _many_bear_calls(10) + _many_straddles(3)
    baseline = render_snapshot("SPY", 500.0, 0.18, candidates, max_candidates=9)
    import random
    shuffled_candidates = list(candidates)
    random.Random(42).shuffle(shuffled_candidates)
    shuffled = render_snapshot("SPY", 500.0, 0.18, shuffled_candidates, max_candidates=9)
    assert shuffled.text == baseline.text
    assert shuffled.candidates == baseline.candidates


def test_a_scarce_structure_does_not_starve_the_others_of_slots():
    # Only 1 straddle candidate exists; the remaining 8 slots (of 9) must
    # still be split fairly between the other two structures rather than
    # all going to whichever sorts first.
    candidates = _many_bull_puts(10) + _many_bear_calls(10) + _many_straddles(1)
    s = render_snapshot("SPY", 500.0, 0.18, candidates, max_candidates=9)
    counts = {}
    for intent in s.candidates.values():
        counts[intent.structure] = counts.get(intent.structure, 0) + 1
    assert counts["long_straddle"] == 1
    assert counts["bull_put_spread"] == 4
    assert counts["bear_call_spread"] == 4


# ---- ATM IV must be a property of the MARKET, not of candidate selection --
#
# render_snapshot used to derive IMPLIED_VOL_ATM from the CAPPED candidate
# list it was about to render. That made the number a function of which
# candidates happened to be surfaced rather than of the market: on one
# unchanged SPY chain (same spot, same bars, same session), surfacing only
# bear call spreads (all OTM calls, which sit low on the vol skew) produced
# "IV -0.69pp below realized"; including straddles too flipped it to "+0.72pp
# above realized" -- the committee's entire regime call reversed although the
# market never moved. The caller now computes atm_iv once from the full
# chain (analytics.atm_implied_vol) and supplies it explicitly.

def test_supplied_atm_iv_is_rendered_verbatim_not_recomputed_from_candidates():
    snap = render_snapshot("SPY", 500.0, 0.18, [_bull_put(495, 490)], atm_iv=0.25)
    assert "IMPLIED_VOL_ATM: 25.00%" in snap.text


def test_same_atm_iv_line_for_different_candidate_sets_from_the_same_chain():
    """THE key regression test. Two different surfaced candidate sets built
    from the same chain, given the same supplied atm_iv, must render a
    byte-identical IMPLIED_VOL_ATM (and IV_MINUS_REALIZED) line. Before the
    fix this line was a function of `candidates` and would differ."""
    bear_calls_only = [_bear_call(510, 515), _bear_call(520, 525)]
    with_a_straddle = [_bear_call(510, 515), _straddle(500)]

    a = render_snapshot("SPY", 500.0, 0.18, bear_calls_only, atm_iv=0.1952)
    b = render_snapshot("SPY", 500.0, 0.18, with_a_straddle, atm_iv=0.1952)

    line_a = [l for l in a.text.splitlines() if l.startswith("IMPLIED_VOL_ATM:")][0]
    line_b = [l for l in b.text.splitlines() if l.startswith("IMPLIED_VOL_ATM:")][0]
    assert line_a == line_b == "IMPLIED_VOL_ATM: 19.52%"

    spread_a = [l for l in a.text.splitlines() if l.startswith("IV_MINUS_REALIZED:")][0]
    spread_b = [l for l in b.text.splitlines() if l.startswith("IV_MINUS_REALIZED:")][0]
    assert spread_a == spread_b


def test_atm_iv_none_supplied_renders_unavailable_even_though_candidates_solve():
    """Explicitly supplying atm_iv=None means the MARKET computation could
    not establish one. It must render `unavailable`, never silently fall
    back to the (selection-dependent) candidate-derived estimate -- doing
    that would resurrect exactly the bug being fixed."""
    snap = render_snapshot("SPY", 500.0, 0.18, [_bull_put(495, 490)], atm_iv=None)
    assert "IMPLIED_VOL_ATM: unavailable" in snap.text
    assert "IV_MINUS_REALIZED: unavailable" in snap.text


def test_omitting_atm_iv_falls_back_to_the_candidate_derived_estimate(caplog):
    """Backward compatibility: an OMITTED atm_iv (as opposed to an explicit
    None) keeps the pre-fix behaviour so no existing caller crashes -- but it
    must log a warning that the fallback is selection-dependent."""
    import logging
    from analytics import implied_vol, time_to_expiry_years

    with caplog.at_level(logging.WARNING, logger="committee.snapshot"):
        snap = render_snapshot("SPY", 500.0, 0.18, [_bull_put(495, 490)])

    expected = implied_vol(2.50, 500.0, 495.0, time_to_expiry_years(30), "p")
    assert f"IMPLIED_VOL_ATM: {expected * 100:.2f}%" in snap.text
    assert any("selection-dependent" in r.message for r in caplog.records)


def test_render_is_deterministic_with_atm_iv_supplied():
    candidates = [_bull_put(495, 490), _bear_call(510, 515)]
    a = render_snapshot("SPY", 500.0, 0.18, candidates, atm_iv=0.21)
    b = render_snapshot("SPY", 500.0, 0.18, candidates, atm_iv=0.21)
    assert a.text == b.text


def test_shuffling_candidates_does_not_change_the_atm_iv_line_when_supplied():
    candidates = [_bull_put(495, 490), _bear_call(510, 515), _straddle(500)]
    baseline = render_snapshot("SPY", 500.0, 0.18, candidates, atm_iv=0.30)
    shuffled = render_snapshot("SPY", 500.0, 0.18, list(reversed(candidates)), atm_iv=0.30)
    assert baseline.text == shuffled.text


# ---- the ranking fix: never surface a certain denial, and span the range --
#
# Measured on a real SPY chain (2026-08-29, spot 769.35): the stratified cap
# above gave every structure a fair share of SLOTS, but within each structure
# it filled those slots by walking the canonical (structure, dte, strikes,
# credit, contracts) order from the front -- which, on a real chain, is one
# extreme of the distribution. The 4 surfaced bear_call_spreads were the FOUR
# TIGHTEST of 188 available (cushion 0.39%-0.91%, when cushions up to 8.54%
# existed); the 4 straddles all had max_loss ($1,211-$2,154) already over
# risk.yaml's $1,000 max_loss_per_position, i.e. a guaranteed DENY that could
# never be executed. The committee was shown a menu where every dish was
# either untradeable or a bad trade, and abstained -- correctly, given what it
# was shown. These tests pin the fix: (A) drop certain denials before ranking
# even starts, and (B) rank+select so the surfaced set of each structure
# spans the available range instead of clustering at one extreme.

from dataclasses import replace as _replace


def _bear_call_range(n, spot=500.0, width=5.0):
    """`n` bear call spreads at increasing distance from `spot`, with credit
    that DECAYS as distance grows -- mirroring the real chain: the tightest
    (closest-to-money) strike carries the most credit, and credit falls off
    as the short strike moves further out of the money. This makes "highest
    credit" and "tightest cushion" the same four candidates, exactly the
    correlation that made the old top-N-by-credit selection pathological.
    """
    out = []
    for i in range(n):
        short_strike = spot + 1 + 2 * i
        long_strike = short_strike + width
        credit = max(0.5, 3.0 - 0.15 * i)
        short = OptionQuote(
            symbol=f"SPYC{i}S", underlying="SPY", strike=short_strike, expiry=EXPIRY,
            right="c", bid=credit * 0.97, ask=credit * 1.03, open_interest=500,
        )
        long = OptionQuote(
            symbol=f"SPYC{i}L", underlying="SPY", strike=long_strike, expiry=EXPIRY,
            right="c", bid=0.097, ask=0.103, open_interest=500,
        )
        intent = build_bear_call_spread(short, long)
        assert intent is not None
        out.append(intent)
    return out


def _top_n_by_credit(candidates, n):
    """The OLD behaviour this fix replaces: highest net_credit first."""
    return sorted(candidates, key=lambda c: c.net_credit, reverse=True)[:n]


def _cushion_of(intent, spot):
    return min(abs(b - spot) for b in intent.breakevens) / spot


def test_a_candidate_whose_max_loss_exceeds_the_cap_is_never_surfaced():
    from dataclasses import replace
    one = _bull_put(495, 490)                       # max_loss = $400
    denied = replace(one, contracts=3, max_loss=1500.0, max_profit=300.0)
    s = render_snapshot("SPY", 500.0, 0.18, [one, denied], max_loss_cap=1000.0)
    assert denied not in s.candidates.values()
    assert one in s.candidates.values()
    assert "1500.00" not in s.text


def test_eliminating_a_whole_structure_lets_the_rest_fill_the_cap():
    # All 4 straddles exceed the cap; the 10+10 verticals do not. The cap
    # (8) must still be fully filled by the surviving two structures, split
    # fairly between them -- not left at 4 (verticals-only fair share) plus 4
    # empty slots, and not backfilled with more of a structure already
    # present beyond its fair round-robin share.
    straddles = [_replace(_straddle(500 + 5 * i), contracts=3, max_loss=3600.0)
                 for i in range(4)]
    candidates = _many_bull_puts(10) + _many_bear_calls(10) + straddles
    s = render_snapshot("SPY", 500.0, 0.18, candidates, max_candidates=8,
                        max_loss_cap=1000.0)
    structures = {i.structure for i in s.candidates.values()}
    assert structures == {"bull_put_spread", "bear_call_spread"}
    assert len(s.candidates) == 8
    counts = {}
    for i in s.candidates.values():
        counts[i.structure] = counts.get(i.structure, 0) + 1
    assert counts == {"bull_put_spread": 4, "bear_call_spread": 4}


def test_default_max_loss_cap_comes_from_risk_yaml_not_a_hardcoded_number():
    import risk_guard
    real_cap = risk_guard.load_risk_config().max_loss_per_position
    one = _bull_put(495, 490)
    denied = _replace(one, contracts=3, max_loss=real_cap + 1.0, max_profit=1.0)
    s = render_snapshot("SPY", 500.0, 0.18, [one, denied])  # no max_loss_cap passed
    assert denied not in s.candidates.values()
    assert one in s.candidates.values()


def test_surfaced_cushions_span_a_wider_range_than_top_n_by_credit_would():
    # 20 bear call spreads, tightest to widest. The OLD ranking (top-N by
    # canonical order, which on a real chain runs tightest-first) and a
    # literal top-N-by-credit are the same pathological menu here, since
    # credit decays monotonically with distance from spot. The fix must
    # surface a set whose cushions actually spread out.
    candidates = _bear_call_range(20)
    s = render_snapshot("SPY", 500.0, 0.18, candidates, max_candidates=4,
                        max_loss_cap=1000.0)
    assert len(s.candidates) == 4

    surfaced_cushions = [_cushion_of(i, 500.0) for i in s.candidates.values()]
    old_menu = _top_n_by_credit(candidates, 4)
    old_cushions = [_cushion_of(i, 500.0) for i in old_menu]

    surfaced_spread = max(surfaced_cushions) - min(surfaced_cushions)
    old_spread = max(old_cushions) - min(old_cushions)
    all_cushions = [_cushion_of(i, 500.0) for i in candidates]
    full_range = max(all_cushions) - min(all_cushions)

    # The old menu is the four tightest by construction (credit decays
    # monotonically with distance) -- its cushions barely spread at all
    # relative to what is actually available.
    assert old_spread < full_range * 0.2
    # The new menu must span materially more of the available range.
    assert surfaced_spread > old_spread * 3
    assert surfaced_spread > full_range * 0.5


def test_ranking_fix_selection_stays_deterministic_and_order_independent():
    candidates = _bear_call_range(20)
    baseline = render_snapshot("SPY", 500.0, 0.18, candidates, max_candidates=4,
                               max_loss_cap=1000.0)
    import random
    shuffled_input = list(candidates)
    random.Random(7).shuffle(shuffled_input)
    shuffled = render_snapshot("SPY", 500.0, 0.18, shuffled_input, max_candidates=4,
                               max_loss_cap=1000.0)
    assert shuffled.text == baseline.text
    assert shuffled.candidates == baseline.candidates

    again = render_snapshot("SPY", 500.0, 0.18, candidates, max_candidates=4,
                            max_loss_cap=1000.0)
    assert again.text == baseline.text


# ---- Part A2: a minimum reward-to-risk floor for CREDIT structures ----
#
# Measured live on SPY (2026-08-29, spot 769.35): the cushion-spread fix
# above surfaced c4/c6 bear_call_spread candidates at cushion 7.89%/8.54%
# collecting $0.02 credit against $498 max loss each -- 249:1 risk against
# reward. The independent blind reviewer flagged both unprompted: "$0.02
# credit against $498 max loss is indefensible risk/reward (249:1 against
# trader)" and "Strikes 7.9% OTM should collect far more premium to justify
# the capital at risk." One unusable menu (tightest-breakeven-only) was
# traded for another (worthless-credit-included). These tests pin a credit
# floor for credit structures, alongside the existing max-loss filter, so
# committee slots are never spent on objectively indefensible trades.

def _credit_spread_with_ratio(spot, credit, width, contracts=1):
    """A bear_call_spread with an exact `credit` and `width`, both legs
    comfortably inside the liquidity gate (spread_pct <= 10%, oi=500).
    """
    short_strike = spot + 7.0
    long_strike = short_strike + width
    short_mid = 2.0 + credit
    long_mid = 2.0
    short = OptionQuote(
        symbol="SPYTHINS", underlying="SPY", strike=short_strike, expiry=EXPIRY,
        right="c", bid=short_mid - 0.05, ask=short_mid + 0.05, open_interest=500,
    )
    long = OptionQuote(
        symbol="SPYTHINL", underlying="SPY", strike=long_strike, expiry=EXPIRY,
        right="c", bid=long_mid - 0.05, ask=long_mid + 0.05, open_interest=500,
    )
    intent = build_bear_call_spread(short, long, contracts=contracts)
    assert intent is not None
    assert abs(intent.net_credit - credit) < 1e-9
    return intent


def test_credit_spread_with_negligible_credit_to_risk_is_not_surfaced():
    # $0.02 credit, width $5 -> max_loss $498, the exact live-measured case:
    # 2 / 498 = 0.4% of capital at risk, nowhere near a defensible floor.
    thin = _credit_spread_with_ratio(500.0, credit=0.02, width=5.0)
    assert abs(thin.max_loss - 498.0) < 1e-6
    healthy = _bull_put(495, 490)
    s = render_snapshot("SPY", 500.0, 0.18, [healthy, thin], max_loss_cap=1000.0)
    assert thin not in s.candidates.values()
    assert healthy in s.candidates.values()
    assert "0.02" not in s.text


def test_credit_spread_comfortably_above_the_floor_is_surfaced():
    # $2.00 credit, width $5 -> max_loss $300: credit is 2/300 = 66.7% of
    # risk, well above the 10%-of-width floor.
    healthy_wide = _credit_spread_with_ratio(500.0, credit=2.0, width=5.0)
    assert abs(healthy_wide.max_loss - 300.0) < 1e-6
    s = render_snapshot("SPY", 500.0, 0.18, [healthy_wide], max_loss_cap=1000.0)
    assert healthy_wide in s.candidates.values()


def test_long_straddle_is_not_excluded_by_the_credit_floor():
    # A debit structure has no "credit" to floor against risk -- it must
    # survive this filter regardless of how small its net debit's magnitude
    # looks relative to max_loss, and be excluded only by the max-loss rule
    # (already covered by test_a_candidate_whose_max_loss_exceeds_the_cap_is_never_surfaced).
    straddle = _straddle(500)
    assert not straddle.is_credit
    s = render_snapshot("SPY", 500.0, 0.18, [straddle], max_loss_cap=10000.0)
    assert straddle in s.candidates.values()


def test_credit_spread_with_zero_or_negative_credit_is_not_surfaced():
    # A real SPY chain produced bear_call_spread/bull_put_spread candidates
    # with net_credit of $0.00 and even $-0.03 (illiquid far-OTM legs pricing
    # to a net debit). `is_credit` (net_credit > 0) reads these as "not a
    # credit trade" and would skip the floor entirely -- letting through
    # something worse than the $0.02 case this fix targets. The filter must
    # key off the structure name, not the credit's sign.
    zero_credit = _credit_spread_with_ratio(500.0, credit=0.0, width=5.0)
    negative_credit = _credit_spread_with_ratio(500.0, credit=-0.03, width=5.0)
    assert not zero_credit.is_credit
    assert not negative_credit.is_credit
    healthy = _bull_put(495, 490)
    s = render_snapshot("SPY", 500.0, 0.18, [healthy, zero_credit, negative_credit],
                        max_loss_cap=1000.0)
    assert zero_credit not in s.candidates.values()
    assert negative_credit not in s.candidates.values()
    assert healthy in s.candidates.values()


def test_credit_floor_emptying_a_structure_lets_the_rest_fill_the_cap():
    # All bear_call_spreads are thin-credit (below the floor); the bull put
    # spreads are healthy. The cap must still be filled entirely from the
    # surviving structure, mirroring _drop_certain_denials' documented
    # behaviour for the max-loss filter.
    thin_calls = [
        _credit_spread_with_ratio(500.0 + i, credit=0.02, width=5.0) for i in range(4)
    ]
    healthy_puts = _many_bull_puts(6)
    s = render_snapshot("SPY", 500.0, 0.18, thin_calls + healthy_puts,
                        max_candidates=4, max_loss_cap=1000.0)
    structures = {i.structure for i in s.candidates.values()}
    assert structures == {"bull_put_spread"}
    assert len(s.candidates) == 4


# ---- the DEBIT verticals must survive the credit floor and be visible ----
#
# This is the exact class of bug that produced the 72% abstention rate: a
# filter written for one structure family silently eliminating another. The
# long-premium structures exist so the desk can express a buy-premium view
# when implied vol sits BELOW realized; a filter or a cap that removes them
# reproduces the original defect in a new place.

def _many_bull_calls(n, start=500):
    return [_bull_call(start + i, start + 5 + i) for i in range(n)]


def _many_bear_puts(n, start=500):
    return [_bear_put(start - i, start - 5 - i) for i in range(n)]


def test_debit_verticals_are_not_dropped_by_the_credit_to_risk_floor():
    """`_drop_thin_credit` is a rule about CREDIT collected. A debit vertical
    has a NEGATIVE net_credit by construction, so a credit-to-risk ratio test
    would reject it unconditionally — removing the only affordable
    long-premium structure the desk has."""
    bull = _bull_call(500, 505)
    bear = _bear_put(500, 495)
    assert bull.net_credit < 0 and bear.net_credit < 0
    assert not bull.is_credit and not bear.is_credit
    s = render_snapshot("SPY", 500.0, 0.18, [bull, bear], max_loss_cap=1000.0)
    assert bull in s.candidates.values()
    assert bear in s.candidates.values()


def test_debit_verticals_survive_alongside_a_thin_credit_spread():
    """The credit floor must fire on the credit spread and leave the debit
    verticals untouched in the same call."""
    thin = _credit_spread_with_ratio(500.0, credit=0.02, width=5.0)
    bull = _bull_call(500, 505)
    s = render_snapshot("SPY", 500.0, 0.18, [thin, bull], max_loss_cap=1000.0)
    assert thin not in s.candidates.values()
    assert bull in s.candidates.values()


def test_debit_verticals_clear_the_max_loss_cap_a_straddle_cannot():
    """The measured defect: at SPY's live price the ATM straddle's max_loss
    ($2,270-$2,639) exceeds risk.yaml's $1,000 cap, so `_drop_certain_denials`
    removed it from every window. A 5-wide debit vertical costs the debit
    paid, which clears the cap comfortably."""
    straddle = _straddle(500)
    bull = _bull_call(500, 505)
    assert bull.max_loss < 1000.0 < straddle.max_loss * 10  # sanity on units
    s = render_snapshot("SPY", 500.0, 0.18, [straddle, bull], max_loss_cap=200.0)
    assert straddle not in s.candidates.values()
    assert bull in s.candidates.values()


def test_credit_structures_do_not_crowd_out_the_long_premium_structures():
    """Round-robin across structures: a menu dominated in COUNT by credit
    spreads must still surface the debit verticals. This is the whole point —
    a long-premium option has to be VISIBLE when the regime calls for it."""
    candidates = (_many_bull_puts(20) + _many_bear_calls(20)
                  + _many_bull_calls(3) + _many_bear_puts(3))
    s = render_snapshot("SPY", 500.0, 0.18, candidates, max_candidates=12,
                        max_loss_cap=1000.0)
    structures = {i.structure for i in s.candidates.values()}
    assert "bull_call_spread" in structures
    assert "bear_put_spread" in structures


def test_mixed_menu_of_all_five_structures_fits_the_cap():
    candidates = (_many_bull_puts(6) + _many_bear_calls(6) + _many_straddles(2)
                  + _many_bull_calls(6) + _many_bear_puts(6))
    s = render_snapshot("SPY", 500.0, 0.18, candidates, max_candidates=10,
                        max_loss_cap=100000.0)
    assert len(s.candidates) == 10
    structures = {i.structure for i in s.candidates.values()}
    assert structures == {"bull_put_spread", "bear_call_spread", "long_straddle",
                          "bull_call_spread", "bear_put_spread"}


def test_debit_verticals_render_their_negative_credit_and_finite_profit():
    s = render_snapshot("SPY", 500.0, 0.18, [_bull_call(500, 505)], max_loss_cap=1000.0)
    assert "bull_call_spread" in s.text
    assert "net_credit=-" in s.text
    assert "max_profit=inf" not in s.text


def test_selection_stays_deterministic_and_order_independent_with_debit_verticals():
    candidates = (_many_bull_puts(8) + _many_bear_calls(8)
                  + _many_bull_calls(5) + _many_bear_puts(5))
    baseline = render_snapshot("SPY", 500.0, 0.18, candidates, max_candidates=10,
                               max_loss_cap=1000.0)
    import random
    shuffled_candidates = list(candidates)
    random.Random(7).shuffle(shuffled_candidates)
    shuffled = render_snapshot("SPY", 500.0, 0.18, shuffled_candidates,
                               max_candidates=10, max_loss_cap=1000.0)
    assert shuffled.text == baseline.text
    assert shuffled.candidates == baseline.candidates


# ---- the net_credit SIGN must be legible, not just present ----
#
# Measured on the re-run of the June-July seeded windows, immediately after
# the debit verticals were wired in. Window 2026-06-18 (IV 2.42pp BELOW
# realized -- the regime that argues for buying premium) surfaced
# bull_call_spread and bear_put_spread candidates, and the vol analyst still
# abstained, in its own words: "Market favours long-vol (IV -2.42pp vs
# realized), but all candidates are credit/mixed spreads with net short or
# zero premium bias. No straddles available." It was looking at long-premium
# candidates and could not tell. The only thing distinguishing them in the
# rendered text was a minus sign on `net_credit`, with no statement of what
# that sign means -- the identical failure `_iv_minus_realized` already
# documents for the IV spread, where readers supplied the convention
# backwards half the time. Present-but-unreadable is not visible.

def test_snapshot_states_the_net_credit_sign_convention():
    s = render_snapshot("SPY", 500.0, 0.18, [_bull_call(500, 505)], max_loss_cap=1000.0)
    assert "NET_CREDIT_CONVENTION:" in s.text
    lowered = s.text.lower()
    assert "debit" in lowered
    assert "long-premium" in lowered or "long premium" in lowered


def test_the_convention_line_is_present_even_for_an_all_credit_menu():
    """A reader must not have to infer the convention from the sign it
    happens to see; the definition is unconditional."""
    s = render_snapshot("SPY", 500.0, 0.18, [_bull_put(495, 490)], max_loss_cap=1000.0)
    assert "NET_CREDIT_CONVENTION:" in s.text


def test_the_convention_line_never_prescribes_which_to_pick():
    """It states the definition only. Telling the committee which side to
    take would make its agreement meaningless — the same rule the
    IV-vs-realized line follows."""
    line = [l for l in render_snapshot(
        "SPY", 500.0, 0.18, [_bull_call(500, 505)], max_loss_cap=1000.0
    ).text.splitlines() if l.startswith("NET_CREDIT_CONVENTION:")][0]
    lowered = line.lower()
    for word in ("should", "prefer", "favours", "favors", "recommend", "abstain"):
        assert word not in lowered


def test_the_convention_line_does_not_break_determinism():
    candidates = _many_bull_calls(4) + _many_bull_puts(4)
    a = render_snapshot("SPY", 500.0, 0.18, candidates, max_loss_cap=1000.0)
    import random
    shuffled = list(candidates)
    random.Random(11).shuffle(shuffled)
    b = render_snapshot("SPY", 500.0, 0.18, shuffled, max_loss_cap=1000.0)
    assert a.text == b.text


# ---- the DEBIT-side quality floor: the mirror of MIN_CREDIT_TO_RISK ----
#
# `_drop_thin_credit` removes a credit structure whose REWARD is a negligible
# fraction of its RISK. A debit structure has the same defect with the two
# terms exchanged: a $0.04 debit on a 5-wide spread is $4 of risk against
# $496 of reward — a 124:1 payoff the market is pricing at essentially zero
# probability — and `_structure_fill_order` ranks on max_profit/max_loss, so
# these rank TOP of their band and are actively selected. Measured live on
# SPY 2026-08-30: c9 bull_call_spread debit $0.04, c5 bear_put_spread $0.07.


def _bull_call_with_debit(debit, width=5.0, long_strike=500.0):
    """A bull_call_spread paying exactly `debit` per share on `width`."""
    short_mid = 2.00
    long_mid = short_mid + debit
    intent = build_bull_call_spread(
        _q(long_strike, "c", long_mid - 0.05, long_mid + 0.05),
        _q(long_strike + width, "c", short_mid - 0.05, short_mid + 0.05),
    )
    assert intent is not None
    return intent


def _bear_put_with_debit(debit, width=5.0, long_strike=500.0):
    short_mid = 2.00
    long_mid = short_mid + debit
    intent = build_bear_put_spread(
        _q(long_strike, "p", long_mid - 0.05, long_mid + 0.05),
        _q(long_strike - width, "p", short_mid - 0.05, short_mid + 0.05),
    )
    assert intent is not None
    return intent


def _long_iron_fly(body=500.0, width=5.0, debit=2.0):
    """A long_iron_butterfly paying exactly `debit` per share on `width`.

    The whole debit is carried on the call side so the arithmetic under test
    is the sum, not the split.
    """
    intent = build_long_iron_butterfly(
        long_call=_q(body, "c", 2.00 + debit - 0.05, 2.00 + debit + 0.05),
        long_put=_q(body, "p", 1.95, 2.05),
        short_call=_q(body + width, "c", 1.95, 2.05),
        short_put=_q(body - width, "p", 1.95, 2.05),
    )
    assert intent is not None
    return intent


def _many_long_iron_flies(n, body=500.0):
    return [_long_iron_fly(body=body + i) for i in range(n)]


def test_a_lottery_ticket_debit_vertical_is_dropped():
    """The measured live defect: c9, a bull_call_spread with a $0.04 debit
    against $496 of upside. It is the exact mirror of the $0.02-credit spread
    `MIN_CREDIT_TO_RISK` was introduced to remove."""
    lottery = _bull_call_with_debit(0.04)
    assert lottery.max_loss == pytest.approx(4.0)
    assert lottery.max_profit == pytest.approx(496.0)
    s = render_snapshot("SPY", 500.0, 0.18, [lottery, _bull_put(495, 490)],
                        max_loss_cap=1000.0)
    assert lottery not in s.candidates.values()


def test_a_lottery_ticket_bear_put_spread_is_dropped_too():
    lottery = _bear_put_with_debit(0.07)
    s = render_snapshot("SPY", 500.0, 0.18, [lottery, _bull_put(495, 490)],
                        max_loss_cap=1000.0)
    assert lottery not in s.candidates.values()


def test_a_debit_vertical_paying_a_real_fraction_of_width_survives():
    real = _bull_call_with_debit(1.00)   # 20% of width; 4:1 reward-to-risk
    s = render_snapshot("SPY", 500.0, 0.18, [real], max_loss_cap=1000.0)
    assert real in s.candidates.values()


def test_a_debit_vertical_priced_at_nearly_the_full_width_is_dropped():
    """The other direction: paying $4.70 for a $5-wide spread risks $470 to
    make $30 — the same 10:1-against reward/risk the blind reviewer called
    indefensible on the credit side, arrived at from the opposite end."""
    lopsided = _bull_call_with_debit(4.70)
    assert lopsided.max_profit == pytest.approx(30.0)
    s = render_snapshot("SPY", 500.0, 0.18, [lopsided, _bull_put(495, 490)],
                        max_loss_cap=1000.0)
    assert lopsided not in s.candidates.values()


def test_the_debit_floor_uses_the_same_ratio_as_the_credit_floor():
    """One constant, applied with risk and reward exchanged — not a second
    number invented for symmetry's sake. `MIN_CREDIT_TO_RISK` says reward
    must be at least 10% of risk; the debit rule says risk must be at least
    10% of reward, which on a width-`w` spread puts the floor at w/11."""
    from committee.snapshot import MIN_CREDIT_TO_RISK
    floor = 5.0 * MIN_CREDIT_TO_RISK / (1 + MIN_CREDIT_TO_RISK)   # 0.4545...
    below = _bull_call_with_debit(round(floor - 0.02, 2))
    above = _bull_call_with_debit(round(floor + 0.02, 2))
    s = render_snapshot("SPY", 500.0, 0.18, [below, above], max_loss_cap=1000.0)
    assert below not in s.candidates.values()
    assert above in s.candidates.values()


def test_long_straddle_is_not_dropped_by_the_debit_floor():
    """A straddle's max_profit is unbounded, so it has no width to measure a
    debit against. Applying a width ratio to it would erase the structure
    entirely — the same class of bug as applying a credit ratio to a debit."""
    straddle = _straddle(500)
    s = render_snapshot("SPY", 500.0, 0.18, [straddle], max_loss_cap=10000.0)
    assert straddle in s.candidates.values()


def test_credit_structures_are_untouched_by_the_debit_floor():
    """A healthy credit spread has a small max_loss relative to nothing on
    the debit side; the debit rule must never see it."""
    healthy = _credit_spread_with_ratio(500.0, credit=2.0, width=5.0)
    s = render_snapshot("SPY", 500.0, 0.18, [healthy], max_loss_cap=1000.0)
    assert healthy in s.candidates.values()


# ---- the non-directional long-premium structure ----

def test_long_iron_butterfly_is_surfaced():
    fly = _long_iron_fly(debit=2.0)
    s = render_snapshot("SPY", 500.0, 0.18, [fly], max_loss_cap=1000.0)
    assert fly in s.candidates.values()
    assert "long_iron_butterfly" in s.text


def test_long_iron_butterfly_clears_the_cap_a_straddle_cannot():
    """The whole point: the same long-vol, direction-neutral view, priced
    under risk.yaml's max_loss_per_position."""
    straddle = _straddle(500)
    fly = _long_iron_fly(debit=2.0)
    assert fly.max_loss < straddle.max_loss
    s = render_snapshot("SPY", 500.0, 0.18, [straddle, fly], max_loss_cap=300.0)
    assert straddle not in s.candidates.values()
    assert fly in s.candidates.values()


def test_long_iron_butterfly_is_not_dropped_by_the_credit_floor():
    fly = _long_iron_fly(debit=2.0)
    assert fly.net_credit < 0 and not fly.is_credit
    s = render_snapshot("SPY", 500.0, 0.18, [fly], max_loss_cap=1000.0)
    assert fly in s.candidates.values()


def test_a_long_iron_butterfly_that_cannot_profit_is_dropped():
    """Debit at or above the width: max_profit <= 0, so the trade cannot make
    money at any settlement price. The builder prices it honestly; this
    filter is what removes it."""
    dead = _long_iron_fly(debit=5.0)
    assert dead.max_profit <= 0
    s = render_snapshot("SPY", 500.0, 0.18, [dead, _bull_put(495, 490)],
                        max_loss_cap=1000.0)
    assert dead not in s.candidates.values()


def test_credit_structures_do_not_crowd_out_the_long_iron_butterfly():
    candidates = (_many_bull_puts(20) + _many_bear_calls(20)
                  + _many_long_iron_flies(3))
    s = render_snapshot("SPY", 500.0, 0.18, candidates, max_candidates=12,
                        max_loss_cap=1000.0)
    assert "long_iron_butterfly" in {i.structure for i in s.candidates.values()}


def test_mixed_menu_of_all_six_structures_fits_the_cap():
    candidates = (_many_bull_puts(6) + _many_bear_calls(6) + _many_straddles(2)
                  + _many_bull_calls(6) + _many_bear_puts(6)
                  + _many_long_iron_flies(6))
    s = render_snapshot("SPY", 500.0, 0.18, candidates, max_candidates=12,
                        max_loss_cap=100000.0)
    assert len(s.candidates) == 12
    assert {i.structure for i in s.candidates.values()} == {
        "bull_put_spread", "bear_call_spread", "long_straddle",
        "bull_call_spread", "bear_put_spread", "long_iron_butterfly"}


def test_selection_stays_deterministic_and_order_independent_with_butterflies():
    candidates = (_many_bull_puts(8) + _many_bear_calls(8)
                  + _many_bull_calls(5) + _many_long_iron_flies(5))
    baseline = render_snapshot("SPY", 500.0, 0.18, candidates, max_candidates=10,
                               max_loss_cap=1000.0)
    import random
    for seed in (7, 11):
        shuffled = list(candidates)
        random.Random(seed).shuffle(shuffled)
        other = render_snapshot("SPY", 500.0, 0.18, shuffled, max_candidates=10,
                                max_loss_cap=1000.0)
        assert other.text == baseline.text
        assert list(other.candidates.values()) == list(baseline.candidates.values())
