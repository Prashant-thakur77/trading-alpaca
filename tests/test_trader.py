"""Tests for committee/trader.py — picks one candidate id or ABSTAIN.

The critical safety property (spec 4.3): a candidate id is validated against
the set generated THIS cycle. A hallucinated or malformed id is treated as
ABSTAIN, not papered over as if it were valid. LLM failure -> ABSTAIN too.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llm.client import LLMResponse
from committee.analysts import AnalystView
from committee.trader import choose

SNAPSHOT = "UNDERLYING: SPY\nSPOT: 500.00\nCANDIDATES (2 of 2):\nc1 ...\nc2 ...\n"
VIEWS = [
    AnalystView("vol_analyst", 0.65, False, "", "IV rich", "claude-haiku-4-5", "h1"),
    AnalystView("bear_adversary", 0.4, False, "", "assignment risk", "claude-haiku-4-5", "h2"),
]


def _ok_response(parsed, model="claude-sonnet-5"):
    return LLMResponse(ok=True, text=str(parsed), parsed=parsed, model=model,
                        prompt_hash="hash", error="", cost_usd=0.033)


def _fail_response(error="claude CLI timeout after 120s"):
    return LLMResponse(ok=False, text="", parsed=None, model="claude-sonnet-5",
                        prompt_hash="hash", error=error, cost_usd=0.0)


def test_valid_choice_is_returned():
    def fake_client(prompt):
        return _ok_response({"choice": "c2", "reasoning": "best risk/reward"})

    choice, reasoning = choose(SNAPSHOT, VIEWS, ["c1", "c2"], client=fake_client)
    assert choice == "c2"
    assert reasoning == "best risk/reward"


def test_model_abstain_choice_is_honored():
    def fake_client(prompt):
        return _ok_response({"choice": "ABSTAIN", "reasoning": "no edge"})

    choice, reasoning = choose(SNAPSHOT, VIEWS, ["c1", "c2"], client=fake_client)
    assert choice == "ABSTAIN"
    assert reasoning == "no edge"


def test_hallucinated_id_is_treated_as_abstain_not_an_error():
    def fake_client(prompt):
        return _ok_response({"choice": "c999", "reasoning": "sounds good"})

    choice, reasoning = choose(SNAPSHOT, VIEWS, ["c1", "c2"], client=fake_client)
    assert choice == "ABSTAIN"
    assert "c999" in reasoning


def test_malformed_choice_type_is_treated_as_abstain():
    def fake_client(prompt):
        return _ok_response({"choice": 123, "reasoning": "nonsense"})

    choice, reasoning = choose(SNAPSHOT, VIEWS, ["c1", "c2"], client=fake_client)
    assert choice == "ABSTAIN"


def test_missing_choice_key_is_treated_as_abstain():
    def fake_client(prompt):
        return _ok_response({"reasoning": "forgot the choice field"})

    choice, reasoning = choose(SNAPSHOT, VIEWS, ["c1", "c2"], client=fake_client)
    assert choice == "ABSTAIN"


def test_llm_failure_is_abstain():
    def fake_client(prompt):
        return _fail_response()

    choice, reasoning = choose(SNAPSHOT, VIEWS, ["c1", "c2"], client=fake_client)
    assert choice == "ABSTAIN"
    assert "timeout" in reasoning.lower()


def test_empty_candidate_ids_forces_abstain_even_on_llm_success():
    def fake_client(prompt):
        return _ok_response({"choice": "c1", "reasoning": "why not"})

    choice, reasoning = choose(SNAPSHOT, VIEWS, [], client=fake_client)
    assert choice == "ABSTAIN"


def test_non_dict_parsed_payloads_abstain_rather_than_raise():
    # Defence in depth for the `llm.client` contract: even if a non-dict ever
    # reached `parsed` (a hand-built response, a future extraction tier, a
    # replayed cache record), the trader must abstain, never AttributeError
    # mid-cycle. "ABSTAIN" as a bare JSON string is the natural reply to this
    # module's own prompt, which offers "the literal string ABSTAIN".
    for payload in ("ABSTAIN", [{"choice": "c1"}], 0.62, True, ["c1", "c2"]):
        def fake_client(prompt, payload=payload):
            return _ok_response(payload)

        choice, reasoning = choose(SNAPSHOT, VIEWS, ["c1", "c2"], client=fake_client)
        assert choice == "ABSTAIN", payload
        assert reasoning


def test_prompt_includes_candidate_ids_and_snapshot():
    captured = {}

    def fake_client(prompt):
        captured["prompt"] = prompt
        return _ok_response({"choice": "c1", "reasoning": "ok"})

    choose(SNAPSHOT, VIEWS, ["c1", "c2"], client=fake_client)
    assert "c1" in captured["prompt"]
    assert "c2" in captured["prompt"]
    assert "SPY" in captured["prompt"]
