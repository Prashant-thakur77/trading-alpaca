"""Tests for committee/premortem.py — LLM reasoning compiled into rules.

The pre-mortem asks the model "what would have to be true for this trade to
lose money?" and then *compiles the answer into machine-checkable exit
triggers*. That compilation step is the whole feature: a paragraph of
plausible reasoning nobody enforces is worth nothing, while a
`dte_below(3)` the exit monitor actually evaluates every cycle is worth a
great deal.

So these tests are overwhelmingly about the boundary, not the happy path:

  * the model may only fill in values for a FIXED set of trigger kinds — it
    can no more invent a trigger kind than the trader can invent a strike
    (CLAUDE.md hard rule 1);
  * a nonsensical value (an `underlying_beyond` on the *winning* side of
    spot, an `iv_spike` below current realized vol, a `dte_below` past
    expiry) is discarded with a logged reason, never accepted;
  * the 3-DTE assignment-avoidance exit is ALWAYS present, whatever the
    model says or fails to say — it is a hard rule, not an opinion;
  * an LLM failure never means "no triggers": it means the deterministic
    defaults plus a recorded note that the pre-mortem was unavailable.
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from candidate_builder import (
    OptionQuote, build_bear_call_spread, build_bull_put_spread,
    build_long_straddle, build_bull_call_spread, build_bear_put_spread,
)
from committee.premortem import (
    FORCED_DTE_BELOW, KIND_CREDIT_DECAY, KIND_DTE_BELOW, KIND_IV_SPIKE,
    KIND_UNDERLYING_BEYOND, MAX_MODEL_TRIGGERS, PREMORTEM_UNAVAILABLE,
    TRIGGER_KINDS, ExitTrigger, deterministic_triggers, premortem,
)
from llm.client import LLMResponse

EXPIRY = date.today() + timedelta(days=30)
SPOT = 500.0
REALIZED_VOL = 0.18


def _q(strike, right, bid, ask, oi=500):
    return OptionQuote(
        symbol=f"SPY{EXPIRY:%y%m%d}{right.upper()}{int(strike * 1000):08d}",
        underlying="SPY", strike=strike, expiry=EXPIRY, right=right,
        bid=bid, ask=ask, open_interest=oi,
    )


def _bull_put():
    return build_bull_put_spread(_q(495, "p", 2.40, 2.60), _q(490, "p", 1.45, 1.55))


def _bear_call():
    return build_bear_call_spread(_q(505, "c", 2.40, 2.60), _q(510, "c", 1.45, 1.55))


def _straddle():
    return build_long_straddle(_q(500, "c", 5.90, 6.10), _q(500, "p", 5.85, 6.05))


def _bull_call():
    return build_bull_call_spread(_q(500, "c", 5.90, 6.10), _q(505, "c", 3.40, 3.60))


def _bear_put():
    return build_bear_put_spread(_q(500, "p", 5.85, 6.05), _q(495, "p", 3.40, 3.60))


def _client(payload, ok=True, error=""):
    """A one-shot fake LLM client returning `payload` as the parsed object."""
    def call(prompt):
        call.prompts.append(prompt)
        return LLMResponse(ok=ok, text="", parsed=payload, model="fake",
                           prompt_hash="h", error=error, cost_usd=0.0)
    call.prompts = []
    return call


def _kinds(triggers):
    return [t.kind for t in triggers]


def _of_kind(triggers, kind):
    return [t for t in triggers if t.kind == kind]


# ── the hard rule: 3 DTE is never negotiable ────────────────

class TestForcedDteExit:
    def test_the_3_dte_exit_is_present_even_when_the_model_says_nothing(self):
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL,
                             client=_client({"failure_modes": []}))
        dte = _of_kind(triggers, KIND_DTE_BELOW)
        assert any(t.threshold == FORCED_DTE_BELOW for t in dte), (
            "assignment avoidance at 3 DTE is a hard rule, not an opinion")

    def test_the_3_dte_exit_survives_a_model_that_argues_against_it(self):
        """A model asking to hold to expiry must not be able to remove it."""
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_DTE_BELOW, "threshold": 0,
                 "rationale": "hold all the way to expiry for full decay"}]}))
        assert FORCED_DTE_BELOW in [t.threshold for t in _of_kind(triggers, KIND_DTE_BELOW)]

    def test_the_3_dte_exit_is_present_when_the_llm_fails(self):
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL,
                             client=_client(None, ok=False, error="timeout"))
        assert FORCED_DTE_BELOW in [t.threshold for t in _of_kind(triggers, KIND_DTE_BELOW)]

    def test_the_3_dte_exit_is_present_when_the_client_raises(self):
        def boom(prompt):
            raise RuntimeError("subprocess exploded")
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL, client=boom)
        assert FORCED_DTE_BELOW in [t.threshold for t in _of_kind(triggers, KIND_DTE_BELOW)]

    def test_a_duplicate_model_dte_at_3_is_not_emitted_twice(self):
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [{"kind": KIND_DTE_BELOW, "threshold": 3,
                               "rationale": "assignment risk"}]}))
        assert len(_of_kind(triggers, KIND_DTE_BELOW)) == 1


# ── LLM failure falls back, it never returns nothing ────────

class TestFailureIsNeverSilence:
    @pytest.mark.parametrize("client", [
        _client(None, ok=False, error="claude CLI timeout after 120s"),
        _client("ABSTAIN"),                    # parsed is not a dict
        _client({"failure_modes": "not a list"}),
        _client({}),                           # no failure_modes at all
    ])
    def test_an_unusable_response_yields_the_deterministic_defaults(self, client):
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL, client=client)
        assert triggers == deterministic_triggers(_bull_put(),
                                                  reason=PREMORTEM_UNAVAILABLE)

    def test_the_defaults_record_that_the_premortem_was_unavailable(self):
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL,
                             client=_client(None, ok=False, error="timeout"))
        assert all(PREMORTEM_UNAVAILABLE in t.rationale for t in triggers), (
            "a judge must be able to tell a fallback from a real pre-mortem")

    def test_the_defaults_carry_the_50pct_profit_target_on_a_credit_structure(self):
        triggers = deterministic_triggers(_bull_put())
        decay = _of_kind(triggers, KIND_CREDIT_DECAY)
        assert [t.threshold for t in decay] == [0.5]

    def test_a_debit_structure_gets_no_credit_decay_default(self):
        """50% of the credit received is meaningless when no credit was
        received — a straddle is a debit."""
        triggers = deterministic_triggers(_straddle())
        assert _of_kind(triggers, KIND_CREDIT_DECAY) == []
        assert FORCED_DTE_BELOW in [t.threshold for t in _of_kind(triggers, KIND_DTE_BELOW)]


# ── the model may not invent kinds or code ──────────────────

class TestKindsAreFixed:
    def test_an_unrecognised_kind_is_discarded(self, caplog):
        with caplog.at_level("WARNING"):
            triggers = premortem(_bull_put(), SPOT, REALIZED_VOL, client=_client({
                "failure_modes": [
                    {"kind": "close_if_vix_over", "threshold": 30,
                     "rationale": "VIX regime change"}]}))
        assert "close_if_vix_over" not in _kinds(triggers)
        assert "close_if_vix_over" in caplog.text, "a discard must be logged, not silent"

    def test_every_returned_kind_is_in_the_fixed_set(self):
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_UNDERLYING_BEYOND, "threshold": 493.0, "rationale": "a"},
                {"kind": "exec:rm -rf /", "threshold": 1, "rationale": "b"},
                {"kind": KIND_IV_SPIKE, "threshold": 0.40, "rationale": "c"}]}))
        assert set(_kinds(triggers)) <= set(TRIGGER_KINDS)

    def test_a_non_numeric_threshold_is_discarded(self):
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_UNDERLYING_BEYOND, "threshold": "below the 200dma",
                 "rationale": "trend break"}]}))
        assert _of_kind(triggers, KIND_UNDERLYING_BEYOND) == []

    def test_a_non_finite_threshold_is_discarded(self):
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_IV_SPIKE, "threshold": float("inf"),
                 "rationale": "vol explodes"}]}))
        assert _of_kind(triggers, KIND_IV_SPIKE) == []

    def test_a_malformed_entry_does_not_discard_the_good_ones(self):
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                "not an object",
                {"kind": KIND_UNDERLYING_BEYOND, "threshold": 493.0,
                 "rationale": "breaks the short strike"}]}))
        assert [t.threshold for t in _of_kind(triggers, KIND_UNDERLYING_BEYOND)] == [493.0]

    def test_the_number_of_model_triggers_is_capped(self):
        many = [{"kind": KIND_UNDERLYING_BEYOND, "threshold": 490.0 + i * 0.1,
                 "rationale": f"level {i}"} for i in range(50)]
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL,
                             client=_client({"failure_modes": many}))
        assert len(_of_kind(triggers, KIND_UNDERLYING_BEYOND)) <= MAX_MODEL_TRIGGERS


# ── nonsensical values are discarded, not accepted ──────────

class TestValuesAreSanityChecked:
    def test_a_bull_put_underlying_level_above_spot_is_discarded(self):
        """A bull put spread loses to the DOWNSIDE. A level above spot is on
        the winning side and would either never fire or fire on a profit."""
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_UNDERLYING_BEYOND, "threshold": 520.0,
                 "rationale": "a rally hurts the short put"}]}))
        assert _of_kind(triggers, KIND_UNDERLYING_BEYOND) == []

    def test_a_bull_put_underlying_level_below_spot_is_kept(self):
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_UNDERLYING_BEYOND, "threshold": 492.0,
                 "rationale": "spot through the short 495 put"}]}))
        kept = _of_kind(triggers, KIND_UNDERLYING_BEYOND)
        assert [t.threshold for t in kept] == [492.0]
        assert kept[0].rationale == "spot through the short 495 put", (
            "the sentence that produced the trigger must survive into it")

    def test_a_bear_call_underlying_level_below_spot_is_discarded(self):
        triggers = premortem(_bear_call(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_UNDERLYING_BEYOND, "threshold": 480.0,
                 "rationale": "a selloff"}]}))
        assert _of_kind(triggers, KIND_UNDERLYING_BEYOND) == []

    def test_a_bear_call_underlying_level_above_spot_is_kept(self):
        triggers = premortem(_bear_call(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_UNDERLYING_BEYOND, "threshold": 507.0,
                 "rationale": "spot through the short 505 call"}]}))
        assert [t.threshold for t in _of_kind(triggers, KIND_UNDERLYING_BEYOND)] == [507.0]

    def test_a_bull_call_underlying_level_below_spot_is_kept(self):
        """A bull CALL spread is long premium but loses to the DOWNSIDE, just
        like a bull put spread. Leaving it out of the structure map would make
        every underlying level nonsense for it and discard them all."""
        triggers = premortem(_bull_call(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_UNDERLYING_BEYOND, "threshold": 492.0,
                 "rationale": "spot falls away from the long 500 call"}]}))
        assert [t.threshold for t in _of_kind(triggers, KIND_UNDERLYING_BEYOND)] == [492.0]

    def test_a_bull_call_underlying_level_above_spot_is_discarded(self):
        triggers = premortem(_bull_call(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_UNDERLYING_BEYOND, "threshold": 520.0,
                 "rationale": "a rally"}]}))
        assert _of_kind(triggers, KIND_UNDERLYING_BEYOND) == []

    def test_a_bear_put_underlying_level_above_spot_is_kept(self):
        triggers = premortem(_bear_put(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_UNDERLYING_BEYOND, "threshold": 508.0,
                 "rationale": "spot rallies away from the long 500 put"}]}))
        assert [t.threshold for t in _of_kind(triggers, KIND_UNDERLYING_BEYOND)] == [508.0]

    def test_a_bear_put_underlying_level_below_spot_is_discarded(self):
        triggers = premortem(_bear_put(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_UNDERLYING_BEYOND, "threshold": 480.0,
                 "rationale": "a selloff"}]}))
        assert _of_kind(triggers, KIND_UNDERLYING_BEYOND) == []

    def test_a_debit_vertical_gets_no_credit_decay_trigger(self):
        """A debit structure received no credit, so a credit-decay fraction
        has nothing to decay. Already enforced by the net_credit <= 0 rule;
        pinned here because the debit verticals are new to that path."""
        triggers = premortem(_bull_call(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_CREDIT_DECAY, "threshold": 0.5,
                 "rationale": "half the credit gone"}]}))
        assert _of_kind(triggers, KIND_CREDIT_DECAY) == []

    def test_a_straddle_gets_no_underlying_level_at_all(self):
        """A long straddle's failure mode is the underlying NOT moving. A
        'beyond' level is the profit case, not the loss case."""
        triggers = premortem(_straddle(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_UNDERLYING_BEYOND, "threshold": 470.0,
                 "rationale": "a big move down"}]}))
        assert _of_kind(triggers, KIND_UNDERLYING_BEYOND) == []

    def test_an_absurd_underlying_level_is_discarded(self):
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_UNDERLYING_BEYOND, "threshold": 0.01,
                 "rationale": "SPY goes to a penny"}]}))
        assert _of_kind(triggers, KIND_UNDERLYING_BEYOND) == []

    def test_an_iv_spike_below_current_realized_vol_is_discarded(self):
        """It would fire immediately on entry — an exit that closes the trade
        it was written for is not a risk control."""
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_IV_SPIKE, "threshold": 0.05,
                 "rationale": "vol rises"}]}))
        assert _of_kind(triggers, KIND_IV_SPIKE) == []

    def test_an_iv_spike_above_realized_vol_is_kept(self):
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_IV_SPIKE, "threshold": 0.35,
                 "rationale": "an IV expansion re-prices the short leg"}]}))
        assert [t.threshold for t in _of_kind(triggers, KIND_IV_SPIKE)] == [0.35]

    def test_a_dte_below_past_the_trades_own_expiry_is_discarded(self):
        intent = _bull_put()
        triggers = premortem(intent, SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_DTE_BELOW, "threshold": intent.dte + 5,
                 "rationale": "exit before it is even open"}]}))
        assert [t.threshold for t in _of_kind(triggers, KIND_DTE_BELOW)] == [FORCED_DTE_BELOW]

    def test_a_credit_decay_outside_zero_to_one_is_discarded(self):
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_CREDIT_DECAY, "threshold": 1.8,
                 "rationale": "take 180% of the credit"}]}))
        assert [t.threshold for t in _of_kind(triggers, KIND_CREDIT_DECAY)] == [0.5]

    def test_credit_decay_is_rejected_for_a_debit_structure(self):
        triggers = premortem(_straddle(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [
                {"kind": KIND_CREDIT_DECAY, "threshold": 0.5,
                 "rationale": "take half the credit"}]}))
        assert _of_kind(triggers, KIND_CREDIT_DECAY) == []


# ── the prompt itself ───────────────────────────────────────

class TestPrompt:
    def test_the_prompt_states_the_trade_and_the_allowed_kinds(self):
        client = _client({"failure_modes": []})
        premortem(_bull_put(), SPOT, REALIZED_VOL, client=client)
        prompt = client.prompts[0]
        assert "bull_put_spread" in prompt
        assert "500.00" in prompt
        for kind in TRIGGER_KINDS:
            assert kind in prompt, f"the model must be told {kind} is available"

    def test_the_prompt_asks_the_premortem_question(self):
        client = _client({"failure_modes": []})
        premortem(_bull_put(), SPOT, REALIZED_VOL, client=client)
        assert "lose money" in client.prompts[0].lower()


class TestExitTriggerShape:
    def test_the_trigger_is_frozen(self):
        trigger = ExitTrigger(KIND_DTE_BELOW, 3.0, "assignment")
        with pytest.raises(Exception):
            trigger.threshold = 5.0

    def test_thresholds_are_floats_even_when_the_model_sends_an_int(self):
        triggers = premortem(_bull_put(), SPOT, REALIZED_VOL, client=_client({
            "failure_modes": [{"kind": KIND_DTE_BELOW, "threshold": 7,
                               "rationale": "gamma risk"}]}))
        assert all(isinstance(t.threshold, float) for t in triggers)
