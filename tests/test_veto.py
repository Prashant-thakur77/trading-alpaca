"""Tests for committee/veto.py — the two decorrelated reviewers.

Design spec's amended dual-model veto (§3): since only one model family
(claude) is available, "two different model families must agree" is
restated as two decorrelated reviewers: (1) a deterministic thesis-
consistency check in pure code, and (2) a blind Claude call that never sees
the committee's own reasoning. Both must pass.

thesis_check is PURE CODE — no LLM, no injected client. If Greeks are
unmeasurable it fails closed (returns False), because an unmeasurable
position can't be verified consistent with anything.
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from candidate_builder import (
    OptionQuote, Leg, TradeIntent, build_bull_put_spread, build_bear_call_spread,
    build_long_straddle,
)
from llm.client import LLMResponse
from committee.veto import thesis_check, blind_review

EXPIRY = date.today() + timedelta(days=30)
SPOT = 500.0


def _q(strike, right, bid, ask, oi=500):
    return OptionQuote(
        symbol=f"SPY{EXPIRY:%y%m%d}{right.upper()}{int(strike*1000):08d}",
        underlying="SPY", strike=strike, expiry=EXPIRY, right=right,
        bid=bid, ask=ask, open_interest=oi,
    )


def test_bull_put_spread_is_net_long_delta():
    intent = build_bull_put_spread(
        _q(495, "p", 2.40, 2.60), _q(490, "p", 1.45, 1.55)
    )
    ok, reason = thesis_check(intent, SPOT)
    assert ok is True
    assert "delta" in reason.lower()


def test_bear_call_spread_is_net_short_delta():
    intent = build_bear_call_spread(
        _q(505, "c", 2.40, 2.60), _q(510, "c", 1.45, 1.55)
    )
    ok, reason = thesis_check(intent, SPOT)
    assert ok is True


def test_long_straddle_is_near_delta_neutral():
    intent = build_long_straddle(
        _q(500, "c", 5.90, 6.10), _q(500, "p", 5.85, 6.05)
    )
    ok, reason = thesis_check(intent, SPOT)
    assert ok is True


def test_neutral_thesis_verdict_is_invariant_to_contract_count():
    # position_greeks scales by contracts, so a fixed 15-delta band vetoed the
    # SAME structure with the SAME thesis purely for being bigger (measured:
    # 1 contract +11.30 passes, 2 -> +22.59 vetoed, 3 -> +33.89 vetoed) —
    # before RiskGuard, whose job it is to downsize, ever saw it.
    verdicts = []
    for contracts in (1, 2, 3):
        intent = build_long_straddle(
            _q(500, "c", 5.90, 6.10), _q(500, "p", 5.85, 6.05), contracts=contracts
        )
        assert intent is not None and intent.contracts == contracts
        ok, reason = thesis_check(intent, SPOT)
        verdicts.append(ok)
    assert len(set(verdicts)) == 1, f"size changed the verdict: {verdicts}"
    assert verdicts[0] is True


def test_neutral_band_still_rejects_a_directional_position_at_any_size():
    # Size-invariance must not become "anything passes": a structure whose
    # per-contract delta is far outside the band still fails at every size.
    deep_itm_call = _q(450, "c", 52.00, 52.20)
    for contracts in (1, 2, 3):
        # a long deep-ITM call is ~+90 delta per contract — nothing like
        # neutral at any size
        directional = TradeIntent(
            underlying="SPY", structure="iron_condor",
            legs=(Leg(deep_itm_call, "buy", contracts),),
            contracts=contracts, net_credit=-52.10,
            max_loss=5210.0 * contracts, max_profit=float("inf"),
            breakevens=(502.10,), dte=30,
        )
        ok, reason = thesis_check(directional, SPOT)
        assert ok is False, (contracts, reason)


def test_directionally_inconsistent_structure_fails():
    # A bear call spread (bearish) plumbed in as if it were a bullish thesis
    # by mislabeling the structure would be a real bug; simulate a corrupted
    # intent claiming to be a bull put spread but built like a bear call.
    short_call = _q(505, "c", 2.40, 2.60)
    long_call = _q(510, "c", 1.45, 1.55)
    bad = TradeIntent(
        underlying="SPY", structure="bull_put_spread",
        legs=(Leg(short_call, "sell", 1), Leg(long_call, "buy", 1)),
        contracts=1, net_credit=1.0, max_loss=400.0, max_profit=100.0,
        breakevens=(506.0,), dte=30,
    )
    ok, reason = thesis_check(bad, SPOT)
    assert ok is False


def test_unmeasurable_greeks_fails_closed():
    # Deep-ITM, absurdly mispriced quote: mid price sits far below intrinsic
    # value, so implied_vol cannot solve and position_greeks returns None.
    bad_quote = OptionQuote(
        symbol="SPYBAD", underlying="SPY", strike=100.0, expiry=EXPIRY,
        right="c", bid=0.01, ask=0.02, open_interest=500,
    )
    ok_quote = _q(90, "c", 0.01, 0.02)
    intent = TradeIntent(
        underlying="SPY", structure="bull_put_spread",
        legs=(Leg(bad_quote, "sell", 1), Leg(ok_quote, "buy", 1)),
        contracts=1, net_credit=1.0, max_loss=400.0, max_profit=100.0,
        breakevens=(94.0,), dte=30,
    )
    ok, reason = thesis_check(intent, SPOT)
    assert ok is False
    assert "unmeasurable" in reason.lower() or "closed" in reason.lower()


def test_unknown_structure_fails_closed():
    intent = build_bull_put_spread(
        _q(495, "p", 2.40, 2.60), _q(490, "p", 1.45, 1.55)
    )
    from dataclasses import replace
    weird = replace(intent, structure="mystery_structure")
    ok, reason = thesis_check(weird, SPOT)
    assert ok is False


# ---- blind_review: an independent Claude call, decorrelated on purpose ----

def _ok_response(parsed):
    return LLMResponse(ok=True, text=str(parsed), parsed=parsed,
                        model="claude-haiku-4-5", prompt_hash="h", error="",
                        cost_usd=0.019)


def _fail_response(error="claude CLI timeout after 120s"):
    return LLMResponse(ok=False, text="", parsed=None, model="claude-haiku-4-5",
                        prompt_hash="h", error=error, cost_usd=0.0)


def _intent():
    return build_bull_put_spread(
        _q(495, "p", 2.40, 2.60), _q(490, "p", 1.45, 1.55)
    )


def test_blind_review_agree_passes():
    def fake_client(prompt):
        return _ok_response({"agree": True, "reasoning": "reasonable bullish structure"})

    ok, reason = blind_review(_intent(), SPOT, 0.18, client=fake_client)
    assert ok is True


def test_blind_review_disagree_fails():
    def fake_client(prompt):
        return _ok_response({"agree": False, "reasoning": "direction looks wrong"})

    ok, reason = blind_review(_intent(), SPOT, 0.18, client=fake_client)
    assert ok is False


def test_blind_review_llm_failure_vetoes_fail_closed():
    def fake_client(prompt):
        return _fail_response()

    ok, reason = blind_review(_intent(), SPOT, 0.18, client=fake_client)
    assert ok is False
    assert "timeout" in reason.lower() or "failure" in reason.lower()


def test_blind_review_malformed_response_vetoes_fail_closed():
    def fake_client(prompt):
        return _ok_response({"reasoning": "forgot to say agree or not"})

    ok, reason = blind_review(_intent(), SPOT, 0.18, client=fake_client)
    assert ok is False


def test_blind_review_non_dict_parsed_payloads_veto_rather_than_raise():
    # Defence in depth for the `llm.client` contract: a non-dict `parsed`
    # must fail closed (veto), never TypeError mid-cycle.
    for payload in ("ABSTAIN", [{"agree": True}], 0.62, True, ["yes"]):
        def fake_client(prompt, payload=payload):
            return _ok_response(payload)

        ok, reason = blind_review(_intent(), SPOT, 0.18, client=fake_client)
        assert ok is False, payload
        assert reason


def test_blind_review_prompt_contains_exactly_the_intent_derived_fields():
    # blind_review is decorrelated by CONSTRUCTION: its signature admits no
    # channel through which committee reasoning could arrive (intent, spot,
    # realized_vol, client — nothing else). Asserting that some analyst
    # sentence is absent is therefore tautological; the meaningful assertion
    # is positive — the prompt is built from the intent and price context and
    # nothing else, so this test fails the moment a reasoning/debate/views
    # parameter is threaded in.
    import inspect
    from committee.veto import blind_review as _br
    params = set(inspect.signature(_br).parameters)
    assert params == {"intent", "spot", "realized_vol", "client"}

    captured = {}

    def fake_client(prompt):
        captured["prompt"] = prompt
        return _ok_response({"agree": True, "reasoning": "ok"})

    intent = _intent()
    blind_review(intent, SPOT, 0.18, client=fake_client)
    prompt = captured["prompt"]

    assert f"UNDERLYING: {intent.underlying}" in prompt
    assert f"SPOT: {SPOT:.2f}" in prompt
    assert "REALIZED_VOL: 18.00%" in prompt
    assert f"STRUCTURE: {intent.structure}" in prompt
    assert f"DTE: {intent.dte}" in prompt
    assert f"NET_CREDIT: {intent.net_credit:.2f}" in prompt
    assert f"MAX_LOSS: {intent.max_loss:.2f}" in prompt
    for leg in intent.legs:
        assert f"{leg.side} {leg.quote.strike:.2f}{leg.quote.right}" in prompt
    for b in intent.breakevens:
        assert f"{b:.2f}" in prompt
