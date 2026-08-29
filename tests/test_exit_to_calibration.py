"""End-to-end: a closed trade resolves an analyst prediction and scores it.

This is the proof that the whole work package actually connects. Every piece
of it existed in isolation before and the loop was still dead, because
nothing joined an OUTCOME back to the PREDICTIONS that produced it:

    committee.decide  --writes-->  analyst_view {snapshot_hash, probability}
                                              |
                                     snapshot_hash
                                              |
    exit_monitor      --writes-->  close      {snapshot_hash, realized_pnl}
                                              |
    calibration       --joins--->  (probability, was it profitable?)
                                              |
                                   Brier score -> voting weight

If that join does not work, `make calibration` reports "0 resolved
predictions" forever, `consecutive_losses` stays 0 so risk.yaml's 3-loser
halving never fires, and the pre-mortem's triggers are rules nobody scores.
So the assertions here are deliberately about the SEAM rather than about any
one module: real journal on disk, real `decide()`, real `monitor_positions()`,
real `calibration`, real `scripts/calibration_report.render` — only the LLM
and the broker are fakes.
"""
import json
import os
import shutil
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import calibration
import run_session
from calibration import (
    DEFAULT_MIN_PREDICTIONS, analyst_weights, brier_score, resolved_predictions,
)
from calibration_report import ROLES, render
from candidate_builder import OptionQuote, build_bear_call_spread, build_bull_put_spread
from committee.decide import decide
from exit_monitor import OpenTrade, monitor_positions
from journal import Journal, verify_chain
from llm.client import LLMResponse
from risk_guard import RiskGuard, load_risk_config

RISK_YAML = os.path.join(os.path.dirname(__file__), "..", "risk.yaml")
EXPIRY = date.today() + timedelta(days=30)
SPOT = 500.0
REALIZED_VOL = 0.18


# ── fakes: only the LLM and the broker ──────────────────────

def _q(strike, right, bid, ask, oi=500):
    return OptionQuote(
        symbol=f"SPY{EXPIRY:%y%m%d}{right.upper()}{int(strike * 1000):08d}",
        underlying="SPY", strike=strike, expiry=EXPIRY, right=right,
        bid=bid, ask=ask, open_interest=oi,
    )


def _candidates():
    return [
        build_bull_put_spread(_q(495, "p", 2.40, 2.60), _q(490, "p", 1.45, 1.55)),
        build_bull_put_spread(_q(494, "p", 2.30, 2.50), _q(489, "p", 1.40, 1.50)),
        build_bear_call_spread(_q(510, "c", 2.40, 2.60), _q(515, "c", 1.45, 1.55)),
    ]


class FakeLLM:
    """`client(prompt, model=...)`. The vol analyst is confident, the bear
    adversary is not — so the two roles are gradeable independently."""

    def __init__(self, vol_p=0.85, bear_p=0.40):
        self.vol_p, self.bear_p = vol_p, bear_p

    def __call__(self, prompt, model=None):
        if "volatility analyst" in prompt:
            payload = {"probability": self.vol_p, "reasoning": "iv is rich"}
        elif "bear adversary" in prompt:
            payload = {"probability": self.bear_p, "reasoning": "breakevens are close"}
        elif "independent second reviewer" in prompt:
            payload = {"agree": True, "reasoning": "reads fine"}
        else:
            payload = {"choice": "c1", "reasoning": "widest cushion"}
        return LLMResponse(ok=True, text=json.dumps(payload), parsed=payload,
                           model=str(model), prompt_hash="", error="", cost_usd=0.0)


class FakeCLI:
    """A broker whose book holds exactly the legs of the given intent."""

    def __init__(self, intent, short_mark, long_mark):
        self.rows = []
        for leg in intent.legs:
            qty = leg.contracts if leg.side == "buy" else -leg.contracts
            mark = long_mark if leg.side == "buy" else short_mark
            self.rows.append({
                "symbol": leg.quote.symbol, "qty": str(qty),
                "market_value": f"{mark * abs(qty) * 100 * (1 if qty > 0 else -1):.2f}",
                "current_price": f"{mark:.2f}",
            })
        self.posted = []

    def list_positions(self):
        return self.rows

    def post_order(self, payload):
        self.posted.append(payload)
        return {"id": "close-1", "status": "accepted"}

    def get_order(self, order_id):
        return {"id": order_id, "status": "filled"}


class FakeData:
    def get_stock_bars(self, symbol, days=30):
        import pandas as pd
        return pd.DataFrame({"close": [SPOT] * 5})


@pytest.fixture
def guard(tmp_path, monkeypatch):
    monkeypatch.delenv("KILL", raising=False)
    shutil.copy(RISK_YAML, tmp_path / "risk.yaml")
    return RiskGuard(load_risk_config(tmp_path / "risk.yaml"))


@pytest.fixture
def jrnl(tmp_path):
    return Journal(tmp_path / "journal.jsonl")


def _cycle(jrnl, guard, spot=SPOT, won=True, llm=None):
    """One full cycle: committee decides, position opens, position closes.

    `won` picks the closing marks — a cheap buy-back banks most of the credit,
    an expensive one is the full defined loss.
    """
    decision = decide("SPY", spot, REALIZED_VOL, _candidates(), jrnl,
                      client=llm or FakeLLM())
    assert decision.chosen is not None, "the fake committee must pick something"

    short_mark, long_mark = (0.30, 0.10) if won else (5.20, 0.20)
    cli = FakeCLI(decision.chosen, short_mark, long_mark)
    trade = OpenTrade(order_id=f"o-{spot}", intent=decision.chosen,
                      triggers=(), contracts=1, entry_spot=spot,
                      snapshot_hash=decision.snapshot_hash)
    events = monitor_positions(cli, FakeData(), guard, jrnl,
                               {trade.order_id: trade},
                               poll_seconds=0, sleep=lambda s: None)
    assert events[0].closed is True, "the cycle must actually close"
    return decision, events[0]


# ── the join, end to end ────────────────────────────────────

class TestOneCycleResolves:
    def test_a_journalled_close_resolves_the_analyst_predictions(self, jrnl, guard):
        decision, event = _cycle(jrnl, guard, won=True)

        for role in ROLES:
            preds = resolved_predictions(jrnl, role)
            assert len(preds) == 1, f"{role} must now have a graded prediction"
            probability, outcome = preds[0]
            assert outcome is True, "a profitable close resolves to True"
            assert 0.0 <= probability <= 1.0

    def test_the_close_entry_carries_the_cycles_own_snapshot_hash(self, jrnl, guard):
        decision, _ = _cycle(jrnl, guard)
        closes = [e for e in jrnl.entries() if e["type"] == "close"]
        views = [e for e in jrnl.entries() if e["type"] == "analyst_view"]
        assert closes[0]["payload"]["snapshot_hash"] == decision.snapshot_hash
        assert all(v["payload"]["snapshot_hash"] == decision.snapshot_hash
                   for v in views), "the join key must be the SAME on both sides"

    def test_a_loss_resolves_to_false(self, jrnl, guard):
        _cycle(jrnl, guard, won=False)
        assert resolved_predictions(jrnl, "vol_analyst") == [(0.85, False)]

    def test_a_real_brier_score_is_produced(self, jrnl, guard):
        _cycle(jrnl, guard, won=True)
        score = brier_score(resolved_predictions(jrnl, "vol_analyst"))
        assert score is not None, "a resolved prediction must be scoreable"
        # vol_analyst said 0.85 and the trade won: (0.85 - 1)^2
        assert score == pytest.approx(0.0225)

    def test_the_adversary_is_scored_separately_and_worse_on_a_winner(
            self, jrnl, guard):
        _cycle(jrnl, guard, won=True)
        vol = brier_score(resolved_predictions(jrnl, "vol_analyst"))
        bear = brier_score(resolved_predictions(jrnl, "bear_adversary"))
        assert bear > vol, "the analyst who called it right must score better"

    def test_the_hash_chain_survives_a_full_cycle_plus_close(self, jrnl, guard):
        _cycle(jrnl, guard)
        ok, err = verify_chain(jrnl.path)
        assert ok, err


# ── the `make calibration` surface itself ───────────────────

class TestCalibrationReportIsNoLongerDormant:
    def test_the_report_shows_a_non_zero_resolved_count(self, jrnl, guard):
        """`make calibration` said "0 resolved predictions" for every analyst
        forever. This is the same code path it runs."""
        assert "No resolved predictions yet" in render(jrnl), "precondition"

        _cycle(jrnl, guard, won=True)

        report = render(jrnl)
        assert "No resolved predictions yet" not in report
        # One cycle resolves one prediction for each of the two analysts.
        assert "2 resolved prediction(s) across 2 role(s)" in report
        for role in ROLES:
            row = [ln for ln in report.splitlines() if ln.startswith(role)][0]
            assert row.split()[1] == "1", f"{role} row must show 1 resolved"

    def test_the_report_shows_a_real_brier_score_after_enough_cycles(
            self, jrnl, guard):
        for i in range(DEFAULT_MIN_PREDICTIONS):
            _cycle(jrnl, guard, spot=SPOT + i, won=i % 2 == 0)

        report = render(jrnl)
        assert f"{DEFAULT_MIN_PREDICTIONS} resolved" not in report.split("\n")[0]
        vol_line = [ln for ln in report.splitlines()
                    if ln.startswith("vol_analyst")][0]
        assert f"{DEFAULT_MIN_PREDICTIONS}" in vol_line
        assert "n/a" not in vol_line, "a real score, not a placeholder"
        # 0.85 said every cycle, right half the time: mean of .15^2 and .85^2
        score = brier_score(resolved_predictions(jrnl, "vol_analyst"))
        assert score == pytest.approx((0.15 ** 2 + 0.85 ** 2) / 2)

    def test_weights_move_off_one_once_a_role_is_proven(self, jrnl, guard):
        """The demotion half of "the desk that grades itself": an analyst that
        was confident and wrong every time loses voting weight."""
        for i in range(DEFAULT_MIN_PREDICTIONS):
            _cycle(jrnl, guard, spot=SPOT + i, won=False)

        weights = analyst_weights(jrnl, ROLES)
        assert weights["vol_analyst"] < 1.0, "0.85 on ten losers must be demoted"
        assert weights["vol_analyst"] >= calibration.WEIGHT_FLOOR, "never silenced"
        assert weights["bear_adversary"] > weights["vol_analyst"], (
            "the adversary said 0.40 on the same ten losers and was less wrong")


# ── the second thing the missing close entry broke ──────────

class TestConsecutiveLossesIsNoLongerInert:
    def test_a_losing_close_is_counted_by_the_risk_streak(self, jrnl, guard):
        """risk.yaml's "3 consecutive losers halves size" could never fire
        because consecutive_losses only counts closing entries carrying a
        realized P&L, and nothing wrote one."""
        assert run_session.consecutive_losses(jrnl.entries()) == 0

        for i in range(3):
            _cycle(jrnl, guard, spot=SPOT + i, won=False)

        assert run_session.consecutive_losses(jrnl.entries()) == 3

    def test_a_winning_close_resets_the_streak(self, jrnl, guard):
        _cycle(jrnl, guard, spot=SPOT, won=False)
        _cycle(jrnl, guard, spot=SPOT + 1, won=False)
        _cycle(jrnl, guard, spot=SPOT + 2, won=True)
        assert run_session.consecutive_losses(jrnl.entries()) == 0

    def test_the_streak_reaches_the_guards_halving_limit(self, jrnl, guard):
        for i in range(guard.config.consecutive_losses_to_halve):
            _cycle(jrnl, guard, spot=SPOT + i, won=False)
        assert (run_session.consecutive_losses(jrnl.entries())
                >= guard.config.consecutive_losses_to_halve), (
            "the limit in risk.yaml is now reachable from real journal data")
