"""Tests for scripts/calibration_report.py — the `make calibration` output.

The honesty requirement is the behavior under test: when nothing has
resolved, the report must say so in plain language rather than printing
zeros a reader could mistake for an actual Brier score of 0, and it must not
imply live decisions are being influenced when they are not.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest

from calibration import DEFAULT_MIN_PREDICTIONS
from journal import Journal

from calibration_report import ROLES, render


@pytest.fixture
def jrnl(tmp_path):
    return Journal(tmp_path / "journal.jsonl")


def _seed_resolved_cycle(journal, role, probability, outcome_profitable, snapshot_hash):
    journal.append("analyst_view", {
        "role": role, "probability": probability, "abstained": False,
        "abstain_reason": "", "reasoning": "", "model": "m", "prompt_hash": "h",
        "snapshot_hash": snapshot_hash,
    })
    journal.append("exit", {
        "snapshot_hash": snapshot_hash,
        "realized_pnl": 100.0 if outcome_profitable else -100.0,
    })


def test_empty_journal_states_no_resolved_predictions_plainly(jrnl):
    """The report must say, in plain language, that nothing has resolved.

    It used to have to say the loop was DORMANT — literally true then, because
    no writer journalled a closing trade with a realized P&L. `exit_monitor`
    is that writer and `committee/decide.py` now consumes the weights, so the
    word would be a false claim in the opposite direction. What has to stay
    true is the honesty: an empty journal must not read as a score.
    """
    report = render(jrnl)
    assert "No resolved predictions yet" in report
    assert "no trade has closed yet" in report
    assert "0.00" not in report  # no bare zero that reads as a Brier score


def test_empty_journal_lists_every_role_with_weight_one(jrnl):
    report = render(jrnl)
    for role in ROLES:
        assert role in report
    assert "1.00" in report


def test_report_never_claims_live_decisions_are_influenced_when_dormant(jrnl):
    report = render(jrnl)
    assert "influencing committee votes" in report.lower()


def test_below_min_predictions_says_unproven_not_bad(jrnl):
    for i in range(3):
        _seed_resolved_cycle(jrnl, ROLES[0], 0.9, False, f"u{i}" * 8)
    report = render(jrnl)
    assert "unproven" in report
    assert "3/" in report


def test_enough_resolved_predictions_drops_the_dormant_message(jrnl):
    for i in range(DEFAULT_MIN_PREDICTIONS):
        outcome = i % 2 == 0
        _seed_resolved_cycle(jrnl, ROLES[0], 0.9 if outcome else 0.1, outcome, f"r{i}" * 8)
    report = render(jrnl)
    assert "No resolved predictions yet" not in report
    assert "no trade has closed yet" not in report
    assert str(DEFAULT_MIN_PREDICTIONS) in report


def test_the_report_says_the_weights_are_wired_once_predictions_resolve(jrnl):
    """The old report told the reader these weights were NOT wired into
    committee/decide.py. They are now, and the report must not go on saying
    otherwise — an honest report can be wrong in the modest direction too."""
    for i in range(DEFAULT_MIN_PREDICTIONS):
        outcome = i % 2 == 0
        _seed_resolved_cycle(jrnl, ROLES[0], 0.9 if outcome else 0.1, outcome, f"w{i}" * 8)
    report = render(jrnl)
    assert "ARE wired into" in report
    assert "not yet done" not in report


def test_report_shows_a_brier_score_once_predictions_resolve(jrnl):
    for i in range(DEFAULT_MIN_PREDICTIONS):
        outcome = i % 2 == 0
        _seed_resolved_cycle(jrnl, ROLES[0], 1.0 if outcome else 0.0, outcome, f"s{i}" * 8)
    report = render(jrnl)
    assert "0.000" in report  # perfectly calibrated in this synthetic case
