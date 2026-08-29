"""Tests for committee/analysts.py — vol_analyst, bear_adversary, aggregate.

Abstention is the safety property under test here as much as the happy path:
an LLM failure or unparseable/out-of-range output must never be coerced into
a confident probability (CLAUDE.md hard rule 2 + spec 4.4 "fail soft on the
LLM"). `aggregate` must exclude abstaining views from both numerator and
denominator — a genuine 0.5 is a view, an abstention is not.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llm.client import LLMResponse
from committee.analysts import AnalystView, vol_analyst, bear_adversary, aggregate

SNAPSHOT = "UNDERLYING: SPY\nSPOT: 500.00\nREALIZED_VOL: 18.00%\nCANDIDATES (1 of 1):\nc1 | bull_put_spread | DTE=30\n"


def _ok_response(parsed, model="claude-haiku-4-5"):
    return LLMResponse(ok=True, text=str(parsed), parsed=parsed, model=model,
                        prompt_hash="hash", error="", cost_usd=0.019)


def _fail_response(error="claude CLI timeout after 120s", model="claude-haiku-4-5"):
    return LLMResponse(ok=False, text="", parsed=None, model=model,
                        prompt_hash="hash", error=error, cost_usd=0.0)


def test_vol_analyst_happy_path_returns_view():
    def fake_client(prompt):
        assert "SPY" in prompt
        return _ok_response({"probability": 0.62, "reasoning": "IV rich vs realized"})

    view = vol_analyst(SNAPSHOT, client=fake_client)
    assert isinstance(view, AnalystView)
    assert view.role == "vol_analyst"
    assert view.abstained is False
    assert view.probability == 0.62
    assert view.reasoning == "IV rich vs realized"
    assert view.model == "claude-haiku-4-5"
    assert view.prompt_hash == "hash"


def test_bear_adversary_happy_path_returns_view():
    def fake_client(prompt):
        return _ok_response({"probability": 0.2, "reasoning": "gap risk into earnings"})

    view = bear_adversary(SNAPSHOT, client=fake_client)
    assert view.role == "bear_adversary"
    assert view.abstained is False
    assert view.probability == 0.2


def test_llm_failure_abstains_never_fabricates_probability():
    def fake_client(prompt):
        return _fail_response(error="claude CLI timeout after 120s")

    view = vol_analyst(SNAPSHOT, client=fake_client)
    assert view.abstained is True
    assert "timeout" in view.abstain_reason.lower()


def test_model_explicit_abstain_is_honored():
    def fake_client(prompt):
        return _ok_response({"abstain": True, "reason": "no term structure data"})

    view = vol_analyst(SNAPSHOT, client=fake_client)
    assert view.abstained is True
    assert view.abstain_reason == "no term structure data"


def test_out_of_range_probability_abstains():
    def fake_client(prompt):
        return _ok_response({"probability": 1.4, "reasoning": "overconfident"})

    view = vol_analyst(SNAPSHOT, client=fake_client)
    assert view.abstained is True


def test_missing_probability_key_abstains():
    def fake_client(prompt):
        return _ok_response({"reasoning": "no number given"})

    view = vol_analyst(SNAPSHOT, client=fake_client)
    assert view.abstained is True


def test_non_numeric_probability_abstains():
    def fake_client(prompt):
        return _ok_response({"probability": "high", "reasoning": "vague"})

    view = vol_analyst(SNAPSHOT, client=fake_client)
    assert view.abstained is True


def test_aggregate_excludes_abstaining_analysts_from_mean():
    views = [
        AnalystView("vol_analyst", 0.8, False, "", "", "m", "h1"),
        AnalystView("bear_adversary", 0.0, True, "timeout", "", "m", "h2"),
    ]
    assert aggregate(views) == 0.8


def test_aggregate_returns_none_when_all_abstain():
    views = [
        AnalystView("vol_analyst", 0.0, True, "timeout", "", "m", "h1"),
        AnalystView("bear_adversary", 0.0, True, "malformed", "", "m", "h2"),
    ]
    assert aggregate(views) is None


def test_aggregate_a_genuine_half_is_not_treated_as_abstention():
    views = [AnalystView("vol_analyst", 0.5, False, "", "genuine coin-flip view", "m", "h1")]
    result = aggregate(views)
    assert result == 0.5
    assert result is not None


def test_aggregate_mean_of_two_active_views():
    views = [
        AnalystView("vol_analyst", 0.6, False, "", "", "m", "h1"),
        AnalystView("bear_adversary", 0.4, False, "", "", "m", "h2"),
    ]
    assert aggregate(views) == 0.5


def test_aggregate_empty_list_returns_none():
    assert aggregate([]) is None
