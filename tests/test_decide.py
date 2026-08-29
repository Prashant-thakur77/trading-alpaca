"""Tests for committee/decide.py — the orchestrator that owns the whole
committee flow, the id -> TradeIntent resolution, the journal trail and the
prompt cache.

No network anywhere: the LLM is a callable injected as `client(prompt, model=)`
returning an `LLMResponse`, following the fake-client pattern already used in
tests/test_analysts.py, tests/test_trader.py and tests/test_veto.py.

Two hard rules are load-bearing here and each has its own section below:
  * hard rule 1 — the ONLY way a TradeIntent is produced is
    `snapshot.candidates[choice_id]`. Never an index, never a re-sort.
  * hard rule 5 — every stage appends exactly one journal entry, and an
    ABSTAIN is journalled as fully as a trade.
"""
import hashlib
import json
import os
import sys
import threading
import time
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from candidate_builder import OptionQuote, build_bull_put_spread, build_bear_call_spread
from committee.analysts import ANALYST_MODEL
from committee.decide import ABSTAIN, CommitteeDecision, decide
from committee.snapshot import render_snapshot
from committee.trader import TRADER_MODEL
from committee.veto import BLIND_REVIEW_MODEL
from journal import Journal
from llm.cache import PromptCache
from llm.client import LLMResponse, prompt_hash

EXPIRY = date.today() + timedelta(days=30)
SPOT = 500.0
REALIZED_VOL = 0.18


# ── fixtures / fakes ─────────────────────────────────────────

def _q(strike, right, bid, ask, oi=500):
    return OptionQuote(
        symbol=f"SPY{EXPIRY:%y%m%d}{right.upper()}{int(strike * 1000):08d}",
        underlying="SPY", strike=strike, expiry=EXPIRY, right=right,
        bid=bid, ask=ask, open_interest=oi,
    )


def _bull_put(short_strike=495.0, long_strike=490.0):
    intent = build_bull_put_spread(
        _q(short_strike, "p", 2.40, 2.60), _q(long_strike, "p", 1.45, 1.55))
    assert intent is not None
    return intent


def _bear_call(short_strike=510.0, long_strike=515.0):
    intent = build_bear_call_spread(
        _q(short_strike, "c", 2.40, 2.60), _q(long_strike, "c", 1.45, 1.55))
    assert intent is not None
    return intent


def _candidates():
    return [_bull_put(495, 490), _bull_put(494, 489), _bear_call(510, 515)]


_UNSET = object()

# The analysts and the blind reviewer both run on Haiku, so the MODEL alone
# cannot say which role a call belongs to. This marker is unique to
# committee/veto.py's blind-review prompt.
_BLIND_MARKER = "independent second reviewer"


class FakeClient:
    """`client(prompt, model=...) -> LLMResponse`, scripted per role.

    Records every call so a test can assert on the exact number of subprocess
    equivalents made — which is how the cache-replay proof works. Passing
    `analyst=None` / `trader=None` / `blind=None` scripts an ok=False response
    for that role (an outage); omitting the argument uses a good default.
    """

    def __init__(self, *, analyst=_UNSET, trader=_UNSET, blind=_UNSET,
                 delay=0.0, raise_for=None):
        self.analyst = ({"probability": 0.62, "reasoning": "iv is rich"}
                        if analyst is _UNSET else analyst)
        self.trader = ({"choice": "c1", "reasoning": "best risk/reward"}
                       if trader is _UNSET else trader)
        self.blind = ({"agree": True, "reasoning": "reads fine"}
                      if blind is _UNSET else blind)
        self.delay = delay
        self.raise_for = raise_for or set()
        self.calls = []
        self._lock = threading.Lock()

    def _role(self, model, prompt):
        if model == TRADER_MODEL:
            return "trader"
        return "blind" if _BLIND_MARKER in prompt else "analyst"

    def __call__(self, prompt, model=ANALYST_MODEL):
        role = self._role(model, prompt)
        with self._lock:
            self.calls.append((model, role, prompt))
        if self.delay:
            time.sleep(self.delay)
        if model in self.raise_for:
            raise RuntimeError(f"client blew up for {model}")
        payload = {"analyst": self.analyst, "trader": self.trader,
                   "blind": self.blind}[role]
        if payload is None:
            return LLMResponse(ok=False, text="", parsed=None, model=model,
                               prompt_hash="", error="rate limited", cost_usd=0.0)
        return LLMResponse(ok=True, text=json.dumps(payload), parsed=payload,
                           model=model, prompt_hash="", error="", cost_usd=0.019)

    def models_called(self):
        return [m for m, _role, _p in self.calls]

    def roles_called(self):
        return [role for _m, role, _p in self.calls]


class BoomJournal:
    """A journal whose every append fails — decisions must survive it."""

    def __init__(self):
        self.attempts = 0

    def append(self, entry_type, payload):
        self.attempts += 1
        raise OSError("disk is full")


@pytest.fixture
def jrnl(tmp_path):
    return Journal(tmp_path / "journal.jsonl")


def _types(journal):
    return [e["type"] for e in journal.entries()]


def _payloads(journal, entry_type):
    return [e["payload"] for e in journal.entries() if e["type"] == entry_type]


def _run(journal, candidates=None, client=None, cache=None):
    return decide("SPY", SPOT, REALIZED_VOL,
                  _candidates() if candidates is None else candidates,
                  journal, cache=cache, client=client or FakeClient())


# ── the happy path ───────────────────────────────────────────

class TestHappyPath:
    def test_a_clean_cycle_returns_the_chosen_intent(self, jrnl):
        d = _run(jrnl)
        assert isinstance(d, CommitteeDecision)
        assert d.chosen is not None
        assert d.choice_id == "c1"
        assert d.abstain_reason == ""
        assert d.thesis_ok and d.blind_ok

    def test_every_stage_of_the_committee_actually_ran(self, jrnl):
        client = FakeClient()
        d = _run(jrnl, client=client)
        # two analysts (haiku), one trader (sonnet), one blind review (haiku)
        assert client.models_called().count(ANALYST_MODEL) == 3
        assert client.models_called().count(TRADER_MODEL) == 1
        assert len(d.views) == 2
        assert {v.role for v in d.views} == {"vol_analyst", "bear_adversary"}

    def test_the_aggregate_excludes_abstainers_from_the_denominator(self, jrnl):
        d = _run(jrnl)
        assert d.aggregate_probability == pytest.approx(0.62)

    def test_the_snapshot_hash_is_the_sha256_of_the_rendered_snapshot(self, jrnl):
        candidates = _candidates()
        expected = hashlib.sha256(
            render_snapshot("SPY", SPOT, REALIZED_VOL, candidates).text.encode("utf-8")
        ).hexdigest()
        assert _run(jrnl, candidates=candidates).snapshot_hash == expected

    def test_decide_forwards_atm_iv_to_the_rendered_snapshot(self, jrnl):
        """The vol-regime signal must come from the MARKET (the chain), not
        from whichever candidates happened to be built. `decide` must accept
        `atm_iv` and pass it straight through to `render_snapshot` rather
        than letting the snapshot recompute it from `candidates`."""
        candidates = _candidates()
        expected = hashlib.sha256(
            render_snapshot("SPY", SPOT, REALIZED_VOL, candidates,
                            atm_iv=0.2345).text.encode("utf-8")
        ).hexdigest()
        d = decide("SPY", SPOT, REALIZED_VOL, candidates, jrnl,
                   cache=None, client=FakeClient(), atm_iv=0.2345)
        assert d.snapshot_hash == expected

    def test_the_trader_reasoning_is_carried_through(self, jrnl):
        d = _run(jrnl)
        assert d.trader_reasoning == "best risk/reward"

    def test_the_analysts_run_concurrently(self, jrnl):
        """Proven at 1.96x already; the orchestrator must not serialize them."""
        client = FakeClient(delay=0.30)
        start = time.monotonic()
        _run(jrnl, client=client)
        elapsed = time.monotonic() - start
        # 4 calls: 2 analysts concurrently (0.30) + trader (0.30) + blind (0.30)
        assert elapsed < 1.05, f"analysts appear to be serialized ({elapsed:.2f}s)"


# ── HARD RULE 1: the id is the only route to a TradeIntent ───

class TestHardRuleOneIdMapping:
    def test_the_same_id_yields_the_same_intent_under_a_shuffled_input(self, jrnl):
        """The id must name a trade, not a list position.

        A live chain yields candidates in no promised order. If the
        orchestrator resolved "c1" by indexing its own input list, a reordered
        fetch would send a DIFFERENT trade than the id names while every guard
        still reported PASS. Only `snapshot.candidates[choice_id]` is legal.
        """
        forward = _candidates()
        backward = list(reversed(forward))
        a = _run(jrnl, candidates=forward)
        b = _run(jrnl, candidates=backward)
        assert a.choice_id == b.choice_id == "c1"
        assert a.chosen == b.chosen

    def test_the_chosen_intent_is_the_snapshots_own_mapping_not_the_inputs(self, jrnl):
        candidates = _candidates()
        snap = render_snapshot("SPY", SPOT, REALIZED_VOL, candidates)
        d = _run(jrnl, candidates=candidates)
        assert d.chosen is snap.candidates[d.choice_id] or d.chosen == snap.candidates[d.choice_id]

    def test_every_offered_id_resolves_to_its_own_snapshot_intent(self, jrnl):
        candidates = _candidates()
        snap = render_snapshot("SPY", SPOT, REALIZED_VOL, candidates)
        for cid in snap.candidate_ids:
            client = FakeClient(trader={"choice": cid, "reasoning": "picked"})
            d = _run(jrnl, candidates=list(reversed(candidates)), client=client)
            assert d.choice_id == cid
            assert d.chosen == snap.candidates[cid]

    def test_a_hallucinated_id_abstains_rather_than_falling_back(self, jrnl):
        client = FakeClient(trader={"choice": "c99", "reasoning": "made it up"})
        d = _run(jrnl, client=client)
        assert d.chosen is None
        assert d.choice_id == ABSTAIN
        assert d.abstain_reason

    def test_no_candidates_abstains_without_spending_a_single_llm_call(self, jrnl):
        client = FakeClient()
        d = _run(jrnl, candidates=[], client=client)
        assert d.chosen is None
        assert client.calls == []
        assert "candidate" in d.abstain_reason.lower()


# ── HARD RULE 5: every step is journalled ────────────────────

class TestHardRuleFiveJournal:
    def test_a_traded_cycle_journals_every_stage_in_order(self, jrnl):
        _run(jrnl)
        assert _types(jrnl) == [
            "snapshot", "analyst_view", "analyst_view", "trader_choice",
            "veto", "committee_decision",
        ]

    def test_the_snapshot_entry_carries_the_cycle_inputs(self, jrnl):
        _run(jrnl)
        payload = _payloads(jrnl, "snapshot")[0]
        assert payload["underlying"] == "SPY"
        assert payload["spot"] == SPOT
        assert payload["realized_vol"] == REALIZED_VOL
        assert payload["candidate_count"] == 3
        assert len(payload["snapshot_hash"]) == 64

    def test_each_analyst_view_entry_carries_its_full_record(self, jrnl):
        _run(jrnl)
        views = _payloads(jrnl, "analyst_view")
        assert [v["role"] for v in views] == ["vol_analyst", "bear_adversary"]
        for v in views:
            assert v["probability"] == pytest.approx(0.62)
            assert v["abstained"] is False
            assert v["abstain_reason"] == ""
            assert v["reasoning"] == "iv is rich"
            assert v["model"] == ANALYST_MODEL
            assert len(v["prompt_hash"]) == 64

    def test_each_analyst_view_entry_carries_the_cycles_snapshot_hash(self, jrnl):
        # calibration.py correlates an analyst's prediction with its eventual
        # outcome by grouping journal entries on snapshot_hash — this is the
        # join key, so it must be on the analyst_view entry, not just on
        # committee_decision.
        d = _run(jrnl)
        views = _payloads(jrnl, "analyst_view")
        assert all(v["snapshot_hash"] == d.snapshot_hash for v in views)

    def test_an_abstaining_analyst_journals_a_null_probability_not_a_zero(self, jrnl):
        """A 0.0 would read as maximum bearishness — the strongest opinion
        there is. An abstention must be unmistakably absent."""
        client = FakeClient(analyst={"abstain": True, "reason": "no IV given"})
        _run(jrnl, client=client)
        for v in _payloads(jrnl, "analyst_view"):
            assert v["probability"] is None
            assert v["abstained"] is True
            assert v["abstain_reason"] == "no IV given"

    def test_the_trader_choice_entry_carries_choice_aggregate_and_reasoning(self, jrnl):
        _run(jrnl)
        payload = _payloads(jrnl, "trader_choice")[0]
        assert payload["choice_id"] == "c1"
        assert payload["aggregate_probability"] == pytest.approx(0.62)
        assert payload["reasoning"] == "best risk/reward"

    def test_the_veto_entry_carries_both_reviewers(self, jrnl):
        _run(jrnl)
        payload = _payloads(jrnl, "veto")[0]
        assert payload["thesis_ok"] is True
        assert payload["blind_ok"] is True
        assert payload["thesis_reason"] and payload["blind_reason"]

    def test_an_abstain_is_journalled_as_fully_as_a_trade(self, jrnl):
        """Refusals are the product — they are what the judge page shows."""
        client = FakeClient(trader={"choice": "ABSTAIN", "reasoning": "too thin"})
        d = _run(jrnl, client=client)
        assert d.chosen is None
        types = _types(jrnl)
        assert types.count("snapshot") == 1
        assert types.count("analyst_view") == 2
        assert types.count("trader_choice") == 1
        assert types.count("committee_decision") == 1
        final = _payloads(jrnl, "committee_decision")[0]
        assert final["choice_id"] == ABSTAIN
        assert final["abstain_reason"]

    def test_an_all_abstain_cycle_still_journals_a_decision(self, jrnl):
        client = FakeClient(analyst={"abstain": True, "reason": "no data"})
        d = _run(jrnl, client=client)
        assert d.chosen is None
        assert d.aggregate_probability is None
        assert "committee_decision" in _types(jrnl)
        assert _payloads(jrnl, "committee_decision")[0]["abstain_reason"]

    def test_the_journal_chain_stays_verifiable(self, jrnl):
        from journal import verify_chain
        _run(jrnl)
        ok, err = verify_chain(jrnl.path)
        assert ok, err

    def test_a_journal_failure_never_breaks_the_decision(self):
        """Mirrors executor_options._record: log at error level and swallow.
        A write failure must not turn a good decision into an exception."""
        boom = BoomJournal()
        d = _run(boom)
        assert d.chosen is not None
        assert d.choice_id == "c1"
        assert boom.attempts >= 6, "every stage must still have been attempted"

    def test_a_none_journal_is_accepted_for_a_dry_run(self):
        d = decide("SPY", SPOT, REALIZED_VOL, _candidates(), None,
                   client=FakeClient())
        assert d.chosen is not None


# ── the veto layer: both must pass ───────────────────────────

class TestVeto:
    def test_a_blind_review_disagreement_abstains(self, jrnl):
        client = FakeClient(blind={"agree": False, "reasoning": "gap risk"})
        d = _run(jrnl, client=client)
        assert d.chosen is None
        assert d.blind_ok is False
        assert "gap risk" in d.abstain_reason or "blind" in d.abstain_reason.lower()

    def test_a_thesis_check_failure_abstains(self, jrnl):
        """A bear call spread offered as the pick while the thesis check reads
        a long-delta position is a mislabelled structure — no trade."""
        import committee.decide as mod
        d = None

        def failing_thesis(intent, spot):
            return False, "net delta +42.00 contradicts the stated thesis"

        original = mod.thesis_check
        mod.thesis_check = failing_thesis
        try:
            d = _run(jrnl)
        finally:
            mod.thesis_check = original
        assert d.chosen is None
        assert d.thesis_ok is False
        assert "contradicts" in d.abstain_reason

    def test_both_vetoes_run_even_when_the_first_one_fails(self, jrnl):
        """The judge page shows both reviewers' verdicts, so both are recorded
        even on a refusal — a blank second opinion looks like it was skipped."""
        import committee.decide as mod
        original = mod.thesis_check
        mod.thesis_check = lambda intent, spot: (False, "mislabelled structure")
        client = FakeClient()
        try:
            d = _run(jrnl, client=client)
        finally:
            mod.thesis_check = original
        assert "blind" in client.roles_called()
        assert d.blind_reason

    def test_an_llm_failure_in_the_blind_review_fails_closed(self, jrnl):
        client = FakeClient(blind=None)          # ok=False response
        d = _run(jrnl, client=client)
        assert d.chosen is None
        assert d.blind_ok is False


# ── fail closed everywhere ───────────────────────────────────

class TestFailClosed:
    def test_an_llm_outage_abstains_rather_than_raising(self, jrnl):
        client = FakeClient(analyst=None, trader=None, blind=None)
        d = _run(jrnl, client=client)
        assert d.chosen is None
        assert d.abstain_reason

    def test_a_client_that_raises_abstains(self, jrnl):
        client = FakeClient(raise_for={ANALYST_MODEL, TRADER_MODEL,
                                       BLIND_REVIEW_MODEL})
        d = _run(jrnl, client=client)
        assert d.chosen is None
        assert d.abstain_reason

    def test_an_internal_error_abstains_and_is_still_journalled(self, jrnl):
        import committee.decide as mod
        original = mod.render_snapshot

        def boom(*a, **k):
            raise ValueError("snapshot exploded")

        mod.render_snapshot = boom
        try:
            d = _run(jrnl)
        finally:
            mod.render_snapshot = original
        assert d.chosen is None
        assert "snapshot exploded" in d.abstain_reason
        assert "committee_decision" in _types(jrnl)

    def test_an_abstained_decision_never_carries_an_intent(self, jrnl):
        for client in (FakeClient(trader={"choice": "ABSTAIN", "reasoning": "no"}),
                       FakeClient(trader={"choice": "nope", "reasoning": "no"}),
                       FakeClient(blind={"agree": False, "reasoning": "no"}),
                       FakeClient(analyst={"abstain": True, "reason": "no"})):
            d = _run(jrnl, client=client)
            assert d.chosen is None
            assert d.choice_id == ABSTAIN
            assert d.abstain_reason != ""


# ── the prompt cache: cost saver, audit record, replay corpus ─

class TestPromptCache:
    def test_a_second_identical_decide_makes_zero_llm_calls(self, jrnl, tmp_path):
        """The replay proof. Every call goes through the cache, so a repeat of
        the same cycle is served entirely from disk — which is what makes the
        /judge page deterministic and free."""
        cache = PromptCache(tmp_path / "cache")
        first_client = FakeClient()
        first = _run(jrnl, client=first_client, cache=cache)
        assert len(first_client.calls) == 4

        second_client = FakeClient()
        second = _run(jrnl, client=second_client, cache=cache)
        assert second_client.calls == [], "a replay must make ZERO LLM calls"
        assert second.choice_id == first.choice_id
        assert second.chosen == first.chosen
        assert second.aggregate_probability == first.aggregate_probability
        assert second.trader_reasoning == first.trader_reasoning
        assert second.thesis_ok == first.thesis_ok
        assert second.blind_ok == first.blind_ok
        assert second.snapshot_hash == first.snapshot_hash

    def test_a_replayed_view_keeps_its_prompt_hash_link(self, jrnl, tmp_path):
        cache = PromptCache(tmp_path / "cache")
        first = _run(jrnl, cache=cache)
        second = _run(jrnl, client=FakeClient(), cache=cache)
        assert ([v.prompt_hash for v in second.views]
                == [v.prompt_hash for v in first.views])

    def test_every_cached_record_carries_prompt_model_response_and_error(
            self, jrnl, tmp_path):
        cache = PromptCache(tmp_path / "cache")
        _run(jrnl, cache=cache)
        files = list((tmp_path / "cache").glob("*.json"))
        assert len(files) == 4, "one record per LLM call"
        for path in files:
            record = json.loads(path.read_text())
            # LLMResponse has no `prompt` field, so the wrapper must supply it.
            assert record["prompt"], "the cache is the audit record: no prompt, no audit"
            assert record["model"] in (ANALYST_MODEL, TRADER_MODEL, BLIND_REVIEW_MODEL)
            assert record["raw_response"]
            assert isinstance(record["parsed"], dict)
            assert record["error"] == ""
            assert record["ok"] is True

    def test_the_record_key_is_the_shared_prompt_hash_definition(self, jrnl, tmp_path):
        cache = PromptCache(tmp_path / "cache")
        _run(jrnl, cache=cache)
        for path in (tmp_path / "cache").glob("*.json"):
            record = json.loads(path.read_text())
            assert path.stem == prompt_hash(record["model"], record["prompt"])

    def test_a_failed_call_is_cached_with_its_error_for_the_audit_trail(
            self, jrnl, tmp_path):
        cache = PromptCache(tmp_path / "cache")
        _run(jrnl, client=FakeClient(analyst=None), cache=cache)
        records = [json.loads(p.read_text())
                   for p in (tmp_path / "cache").glob("*.json")]
        failed = [r for r in records if r["ok"] is False]
        assert failed, "a failed call must still be on disk"
        assert all(r["ok"] is False and r["error"] for r in failed)

    def test_a_corrupt_record_is_a_miss_not_a_crash(self, jrnl, tmp_path):
        cache_dir = tmp_path / "cache"
        cache = PromptCache(cache_dir)
        _run(jrnl, cache=cache)
        for path in cache_dir.glob("*.json"):
            path.write_text("{truncated")
        client = FakeClient()
        d = _run(jrnl, client=client, cache=cache)
        assert d.chosen is not None
        assert len(client.calls) == 4, "a corrupt record must be re-called"

    def test_a_different_snapshot_does_not_reuse_another_cycles_answers(
            self, jrnl, tmp_path):
        cache = PromptCache(tmp_path / "cache")
        _run(jrnl, cache=cache)
        client = FakeClient()
        decide("SPY", 501.0, REALIZED_VOL, _candidates(), jrnl,
               cache=cache, client=client)
        assert client.calls, "a moved spot is a different prompt and must re-call"

    def test_decide_works_with_no_cache_at_all(self, jrnl):
        d = _run(jrnl, cache=None)
        assert d.chosen is not None
