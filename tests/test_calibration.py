"""Tests for calibration.py — Brier scoring of resolved analyst predictions,
and the voting weights derived from them.

This is "the desk that grades itself" made literal: an analyst that is
confidently wrong loses influence, but never all of it, and never on a small
sample. Three properties are load-bearing here and each gets its own class:

  * brier_score is the textbook mean-squared-error definition, verified
    against hand-computed numbers including the always-0.5 baseline of 0.25.
  * resolved_predictions correlates an analyst_view entry with its eventual
    outcome via the snapshot_hash both entries carry — and returns []
    (never a fabricated score) when nothing correlates, which is exactly
    today's state on the real journal: nothing yet journals a closing entry
    with realized P&L (exit monitoring is PLAN.md Phase 3).
  * analyst_weights never demotes on a small sample (min_predictions), never
    silences even a maximally-wrong analyst (the weight floor), and always
    recomputes from the journal rather than storing mutable state.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from calibration import (
    DEFAULT_MIN_PREDICTIONS,
    WEIGHT_FLOOR,
    analyst_weights,
    brier_score,
    resolved_predictions,
)
from journal import Journal


@pytest.fixture
def jrnl(tmp_path):
    return Journal(tmp_path / "journal.jsonl")


def _view(journal, role, probability, snapshot_hash, abstained=False,
          abstain_reason=""):
    journal.append("analyst_view", {
        "role": role, "probability": probability, "abstained": abstained,
        "abstain_reason": abstain_reason, "reasoning": "", "model": "m",
        "prompt_hash": "h", "snapshot_hash": snapshot_hash,
    })


def _resolve(journal, snapshot_hash, realized_pnl, entry_type="exit"):
    journal.append(entry_type, {
        "snapshot_hash": snapshot_hash, "realized_pnl": realized_pnl,
    })


def _seed_resolved_cycle(journal, role, probability, outcome_profitable,
                          snapshot_hash):
    """One cycle: one analyst_view plus the closing entry that resolves it."""
    _view(journal, role, probability, snapshot_hash)
    _resolve(journal, snapshot_hash, 100.0 if outcome_profitable else -100.0)


# ── brier_score ──────────────────────────────────────────────

class TestBrierScore:
    def test_hand_computed_value(self):
        # (0.9-1)^2 = 0.01, (0.1-0)^2 = 0.01 -> mean 0.01
        assert brier_score([(0.9, True), (0.1, False)]) == pytest.approx(0.01)

    def test_another_hand_computed_value(self):
        # (0.7-1)^2 = 0.09, (0.2-1)^2 = 0.64, (0.3-0)^2 = 0.09
        # mean = (0.09 + 0.64 + 0.09) / 3
        preds = [(0.7, True), (0.2, True), (0.3, False)]
        assert brier_score(preds) == pytest.approx((0.09 + 0.64 + 0.09) / 3)

    def test_always_half_baseline_is_quarter(self):
        preds = [(0.5, True), (0.5, False), (0.5, True), (0.5, False)]
        assert brier_score(preds) == pytest.approx(0.25)

    def test_perfect_predictions_score_zero(self):
        preds = [(1.0, True), (0.0, False), (1.0, True)]
        assert brier_score(preds) == pytest.approx(0.0)

    def test_maximally_wrong_predictions_score_one(self):
        preds = [(1.0, False), (0.0, True)]
        assert brier_score(preds) == pytest.approx(1.0)

    def test_empty_predictions_returns_none_not_a_fabricated_score(self):
        assert brier_score([]) is None


# ── resolved_predictions ─────────────────────────────────────

class TestResolvedPredictions:
    def test_empty_journal_returns_no_predictions(self, jrnl):
        assert resolved_predictions(jrnl, "vol_analyst") == []

    def test_a_correlated_win_resolves_to_true(self, jrnl):
        _seed_resolved_cycle(jrnl, "vol_analyst", 0.7, True, "hash1" * 8)
        preds = resolved_predictions(jrnl, "vol_analyst")
        assert preds == [(0.7, True)]

    def test_a_correlated_loss_resolves_to_false(self, jrnl):
        _seed_resolved_cycle(jrnl, "vol_analyst", 0.7, False, "hash1" * 8)
        preds = resolved_predictions(jrnl, "vol_analyst")
        assert preds == [(0.7, False)]

    def test_an_analyst_view_with_no_resolving_entry_is_excluded(self, jrnl):
        _view(jrnl, "vol_analyst", 0.7, "unresolved" * 4)
        assert resolved_predictions(jrnl, "vol_analyst") == []

    def test_an_abstained_view_is_never_scored_even_if_resolved(self, jrnl):
        sh = "hash2" * 8
        _view(jrnl, "vol_analyst", None, sh, abstained=True,
              abstain_reason="timeout")
        _resolve(jrnl, sh, 100.0)
        assert resolved_predictions(jrnl, "vol_analyst") == []

    def test_only_the_requested_role_is_returned(self, jrnl):
        sh = "hash3" * 8
        _view(jrnl, "vol_analyst", 0.6, sh)
        _view(jrnl, "bear_adversary", 0.3, sh)
        _resolve(jrnl, sh, 50.0)
        assert resolved_predictions(jrnl, "vol_analyst") == [(0.6, True)]
        assert resolved_predictions(jrnl, "bear_adversary") == [(0.3, True)]

    def test_multiple_resolved_cycles_all_come_back(self, jrnl):
        _seed_resolved_cycle(jrnl, "vol_analyst", 0.8, True, "a" * 40)
        _seed_resolved_cycle(jrnl, "vol_analyst", 0.3, False, "b" * 40)
        _seed_resolved_cycle(jrnl, "vol_analyst", 0.9, False, "c" * 40)
        preds = resolved_predictions(jrnl, "vol_analyst")
        assert preds == [(0.8, True), (0.3, False), (0.9, False)]

    def test_resolving_entry_accepts_pnl_key_as_well_as_realized_pnl(self, jrnl):
        sh = "d" * 40
        _view(jrnl, "vol_analyst", 0.6, sh)
        jrnl.append("close", {"snapshot_hash": sh, "pnl": -5.0})
        assert resolved_predictions(jrnl, "vol_analyst") == [(0.6, False)]

    def test_a_closing_entry_with_no_pnl_key_does_not_resolve_anything(self, jrnl):
        sh = "e" * 40
        _view(jrnl, "vol_analyst", 0.6, sh)
        jrnl.append("fill", {"snapshot_hash": sh, "order_id": "abc"})
        assert resolved_predictions(jrnl, "vol_analyst") == []

    def test_unrelated_entry_types_are_ignored(self, jrnl):
        sh = "f" * 40
        _view(jrnl, "vol_analyst", 0.6, sh)
        jrnl.append("veto", {"snapshot_hash": sh, "realized_pnl": 999.0})
        assert resolved_predictions(jrnl, "vol_analyst") == []

    def test_the_real_journal_has_zero_resolved_predictions_today(self):
        """Documents the current honest state: no writer in this codebase
        yet journals a closing entry carrying realized P&L (exit monitoring
        is PLAN.md Phase 3), so this must find nothing on the live journal —
        never a plausible-looking default."""
        path = os.path.join(os.path.dirname(__file__), "..", "logs", "journal.jsonl")
        if not os.path.exists(path):
            pytest.skip("no live journal on this machine")
        real_journal = Journal(path)
        assert resolved_predictions(real_journal, "vol_analyst") == []
        assert resolved_predictions(real_journal, "bear_adversary") == []


# ── analyst_weights ───────────────────────────────────────────

class TestAnalystWeights:
    def test_empty_journal_gives_every_role_weight_one(self, jrnl):
        weights = analyst_weights(jrnl, ["vol_analyst", "bear_adversary"])
        assert weights == {"vol_analyst": 1.0, "bear_adversary": 1.0}

    def test_fewer_than_min_predictions_gets_weight_exactly_one(self, jrnl):
        # 5 confidently-wrong predictions, but min_predictions defaults to 10:
        # unproven is not the same as bad.
        for i in range(5):
            _seed_resolved_cycle(jrnl, "vol_analyst", 0.95, False, f"s{i}" * 8)
        weights = analyst_weights(jrnl, ["vol_analyst"])
        assert weights["vol_analyst"] == 1.0

    def test_a_calibrated_analyst_beats_a_confidently_wrong_one_in_score_and_weight(self, jrnl):
        for i in range(DEFAULT_MIN_PREDICTIONS):
            outcome = i % 2 == 0
            _seed_resolved_cycle(jrnl, "good", 0.9 if outcome else 0.1, outcome, f"g{i}" * 8)
            _seed_resolved_cycle(jrnl, "bad", 0.9 if not outcome else 0.1, outcome, f"b{i}" * 8)

        good_preds = resolved_predictions(jrnl, "good")
        bad_preds = resolved_predictions(jrnl, "bad")
        assert brier_score(good_preds) < brier_score(bad_preds)

        weights = analyst_weights(jrnl, ["good", "bad"])
        assert weights["good"] > weights["bad"]

    def test_weight_floor_holds_for_an_analyst_wrong_on_everything(self, jrnl):
        # Confidently wrong every single time: probability near 1 when the
        # outcome is always False.
        for i in range(DEFAULT_MIN_PREDICTIONS * 2):
            _seed_resolved_cycle(jrnl, "vol_analyst", 0.99, False, f"w{i}" * 8)
        weights = analyst_weights(jrnl, ["vol_analyst"])
        assert weights["vol_analyst"] >= WEIGHT_FLOOR
        assert weights["vol_analyst"] < 1.0

    def test_weight_is_never_exactly_zero(self, jrnl):
        for i in range(DEFAULT_MIN_PREDICTIONS * 2):
            _seed_resolved_cycle(jrnl, "vol_analyst", 1.0, False, f"z{i}" * 8)
        weights = analyst_weights(jrnl, ["vol_analyst"])
        assert weights["vol_analyst"] > 0.0

    def test_a_well_calibrated_analyst_can_score_above_one(self, jrnl):
        # Perfect calibration: every prediction exactly matches its outcome.
        for i in range(DEFAULT_MIN_PREDICTIONS):
            outcome = i % 2 == 0
            _seed_resolved_cycle(jrnl, "vol_analyst", 1.0 if outcome else 0.0,
                                  outcome, f"p{i}" * 8)
        weights = analyst_weights(jrnl, ["vol_analyst"])
        assert weights["vol_analyst"] > 1.0

    def test_weights_are_recomputed_not_cached_across_calls(self, jrnl):
        """The journal is the only source of truth — no mutable state."""
        weights_before = analyst_weights(jrnl, ["vol_analyst"])
        assert weights_before["vol_analyst"] == 1.0
        for i in range(DEFAULT_MIN_PREDICTIONS * 2):
            _seed_resolved_cycle(jrnl, "vol_analyst", 0.99, False, f"r{i}" * 8)
        weights_after = analyst_weights(jrnl, ["vol_analyst"])
        assert weights_after["vol_analyst"] != weights_before["vol_analyst"]

    def test_custom_min_predictions_is_honored(self, jrnl):
        for i in range(3):
            _seed_resolved_cycle(jrnl, "vol_analyst", 0.99, False, f"m{i}" * 8)
        # With min_predictions=2, 3 resolved predictions is enough to score.
        weights = analyst_weights(jrnl, ["vol_analyst"], min_predictions=2)
        assert weights["vol_analyst"] < 1.0
