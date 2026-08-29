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

import time

from llm.client import LLMResponse
from committee import analysts
from committee.analysts import (
    AnalystView, vol_analyst, bear_adversary, aggregate, run_analysts,
)

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


def test_abstained_view_carries_none_probability_not_zero():
    # 0.0 is maximum bearishness to anything reading `.probability` alone;
    # an abstention is the absence of a view, so it must be None.
    clients = [
        lambda p: _fail_response(),
        lambda p: _ok_response({"abstain": True, "reason": "no IV data"}),
        lambda p: _ok_response({"probability": "high"}),
        lambda p: _ok_response({"probability": 1.4}),
        lambda p: _ok_response("ABSTAIN"),
    ]
    for client in clients:
        view = vol_analyst(SNAPSHOT, client=client)
        assert view.abstained is True
        assert view.probability is None


def test_active_view_probability_is_never_none():
    view = vol_analyst(SNAPSHOT, client=lambda p: _ok_response({"probability": 0.0}))
    assert view.abstained is False
    assert view.probability == 0.0     # a genuine 0.0 view survives as a number


def test_non_dict_parsed_payloads_abstain_rather_than_raise():
    # Defence in depth for the `llm.client` contract: a non-dict `parsed`
    # must abstain, never AttributeError/TypeError mid-cycle.
    for payload in ("ABSTAIN", [{"probability": 0.6}], 0.62, True, ["a", "b"]):
        def fake_client(prompt, payload=payload):
            return _ok_response(payload)

        for analyst in (vol_analyst, bear_adversary):
            view = analyst(SNAPSHOT, client=fake_client)
            assert view.abstained is True, (analyst.__name__, payload)
            assert view.abstain_reason


# ---- run_analysts: independent roles, so they run concurrently ----

def test_run_analysts_returns_both_views_in_a_stable_order():
    def fake_client(prompt):
        role = "vol" if "volatility analyst" in prompt else "bear"
        return _ok_response({"probability": 0.6 if role == "vol" else 0.3,
                             "reasoning": role})

    views = run_analysts(SNAPSHOT, client=fake_client)
    assert [v.role for v in views] == ["vol_analyst", "bear_adversary"]
    assert views[0].probability == 0.6
    assert views[1].probability == 0.3


def test_run_analysts_overlaps_the_two_calls():
    # These are subprocess calls to the claude CLI: run sequentially they cost
    # the sum of their latencies for no reason — neither analyst sees the
    # other's output.
    def slow_client(prompt):
        time.sleep(0.4)
        return _ok_response({"probability": 0.5, "reasoning": "ok"})

    start = time.monotonic()
    views = run_analysts(SNAPSHOT, client=slow_client)
    elapsed = time.monotonic() - start
    assert len(views) == 2
    assert elapsed < 0.7, f"analysts appear to have run sequentially ({elapsed:.2f}s)"


def test_run_analysts_turns_a_raising_client_into_an_abstention():
    # A worker thread that raises must not take the cycle down with it.
    def exploding_client(prompt):
        raise RuntimeError("thread boom")

    views = run_analysts(SNAPSHOT, client=exploding_client)
    assert len(views) == 2
    assert all(v.abstained for v in views)
    assert all("boom" in v.abstain_reason for v in views)


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


# ---- the IV sign convention is stated in every analyst prompt ----
#
# Two analysts read "IV_MINUS_REALIZED: -0.69pp" in opposite directions on a
# real 2026-08-29 run. The snapshot header now carries the definition; each
# prompt restates it so a model that skims the header still cannot invert it.

class TestIVSignConvention:
    PROMPTS = (analysts._VOL_ANALYST_PROMPT, analysts._BEAR_ADVERSARY_PROMPT)

    def test_every_analyst_prompt_defines_the_iv_minus_realized_sign(self):
        for prompt in self.PROMPTS:
            assert "IV_MINUS_REALIZED" in prompt
            assert "ABOVE" in prompt and "BELOW" in prompt
            assert "rich" in prompt.lower() and "cheap" in prompt.lower()

    def test_the_convention_block_is_embedded_verbatim_in_both_prompts(self):
        for prompt in self.PROMPTS:
            assert analysts._IV_SIGN_CONVENTION in prompt

    def test_the_convention_binds_rich_to_selling_and_cheap_to_buying(self):
        # "rich ... favours SELLING", "cheap ... favours BUYING" — the exact
        # pairing the 2026-08-29 live run inverted.
        body = analysts._IV_SIGN_CONVENTION.lower()
        rich_at, cheap_at = body.index("rich"), body.index("cheap")
        assert "sell" in body[rich_at:cheap_at]
        assert "buy" in body[cheap_at:]
        assert "buy" not in body[rich_at:cheap_at]

    def test_no_analyst_prompt_prescribes_a_conclusion(self):
        """State the definition; never state the verdict."""
        for prompt in self.PROMPTS:
            body = prompt.lower()
            assert "you should sell" not in body
            assert "you should buy" not in body
            assert "always abstain" not in body
