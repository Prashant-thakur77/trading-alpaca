"""Tests for risk_guard.py — the deterministic gate every order must pass.

Hard rule 2: every order gets ALLOW / DENY / ALLOW_WITH_DOWNSIZE per risk.yaml,
and any error, missing data or exception is DENY. Fail closed, always.
Hard rule 6: KILL_SWITCH file or KILL=1 halts everything.
"""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from candidate_builder import OptionQuote, build_bull_put_spread, build_long_straddle
from risk_guard import (
    RiskGuard,
    Verdict,
    PortfolioState,
    load_risk_config,
)

EXPIRY = date.today() + timedelta(days=30)


def _q(strike, right, bid, ask, oi=500):
    return OptionQuote(f"SPY-{strike}{right}", "SPY", strike, EXPIRY, right, bid, ask, oi)


def _intent(contracts=1):
    """Standard $5-wide bull put spread: $1.00 credit => $400 max loss/contract."""
    return build_bull_put_spread(_q(445, "p", 3.00, 3.10), _q(440, "p", 2.00, 2.10),
                                 contracts=contracts)


def _flat() -> PortfolioState:
    return PortfolioState(open_positions=0, net_delta=0.0, net_vega=0.0,
                          daily_realized_pnl=0.0, consecutive_losses=0,
                          new_today_by_underlying={})


@pytest.fixture
def guard(tmp_path, monkeypatch):
    monkeypatch.delenv("KILL", raising=False)
    monkeypatch.chdir(tmp_path)
    return RiskGuard(load_risk_config(os.path.join(os.path.dirname(__file__), "..", "risk.yaml")))


class TestConfigLoading:
    def test_loads_limits_from_yaml(self):
        cfg = load_risk_config(os.path.join(os.path.dirname(__file__), "..", "risk.yaml"))
        assert cfg.max_loss_per_position == 1000.0
        assert cfg.max_positions == 3
        assert cfg.max_abs_net_delta == 30.0
        assert cfg.max_abs_net_vega == 200.0

    def test_missing_file_raises_rather_than_defaulting(self):
        """A guard with invented defaults is worse than no guard."""
        with pytest.raises(Exception):
            load_risk_config("/nonexistent/risk.yaml")


class TestAllow:
    def test_allows_a_compliant_order(self, guard):
        assert guard.evaluate(_intent(), _flat()).decision == Verdict.ALLOW

    def test_allow_states_a_reason(self, guard):
        assert guard.evaluate(_intent(), _flat()).reason


class TestPositionLimits:
    def test_denies_when_max_positions_reached(self, guard):
        state = PortfolioState(3, 0.0, 0.0, 0.0, 0, {})
        v = guard.evaluate(_intent(), state)
        assert v.decision == Verdict.DENY
        assert "max_positions" in v.reason

    def test_allows_at_one_below_the_cap(self, guard):
        state = PortfolioState(2, 0.0, 0.0, 0.0, 0, {})
        assert guard.evaluate(_intent(), state).decision == Verdict.ALLOW

    def test_denies_second_trade_in_same_underlying_same_day(self, guard):
        state = PortfolioState(0, 0.0, 0.0, 0.0, 0, {"SPY": 1})
        v = guard.evaluate(_intent(), state)
        assert v.decision == Verdict.DENY
        assert "underlying" in v.reason.lower()

    def test_allows_a_different_underlying_same_day(self, guard):
        state = PortfolioState(0, 0.0, 0.0, 0.0, 0, {"QQQ": 1})
        assert guard.evaluate(_intent(), state).decision == Verdict.ALLOW


class TestMaxLossAndDownsizing:
    def test_downsizes_when_max_loss_exceeded(self, guard):
        """3 contracts = $1200 loss > $1000 cap => downsize to 2 ($800)."""
        v = guard.evaluate(_intent(contracts=3), _flat())
        assert v.decision == Verdict.ALLOW_WITH_DOWNSIZE
        assert v.approved_contracts == 2

    def test_downsized_order_respects_the_cap(self, guard):
        intent = _intent(contracts=10)
        v = guard.evaluate(intent, _flat())
        per_contract = intent.max_loss / intent.contracts
        assert v.approved_contracts * per_contract <= 1000.0

    def test_denies_when_even_one_contract_is_too_large(self, guard):
        """A single contract risking > $1000 cannot be downsized into compliance."""
        wide = build_bull_put_spread(_q(500, "p", 3.00, 3.10), _q(480, "p", 2.00, 2.10))
        v = guard.evaluate(wide, _flat())
        assert v.decision == Verdict.DENY
        assert "max_loss" in v.reason

    def test_allows_exactly_at_the_cap(self, guard):
        """$1000 is allowed; the limit is inclusive."""
        at_cap = build_bull_put_spread(_q(445, "p", 3.00, 3.10),
                                       _q(435, "p", 2.00, 2.10))  # $900/contract
        assert guard.evaluate(at_cap, _flat()).decision == Verdict.ALLOW


class TestGreekLimits:
    def test_denies_when_net_delta_would_breach(self, guard):
        state = PortfolioState(0, 29.5, 0.0, 0.0, 0, {})
        v = guard.evaluate(_intent(), state, position_delta=5.0)
        assert v.decision == Verdict.DENY
        assert "delta" in v.reason.lower()

    def test_denies_when_net_vega_would_breach(self, guard):
        state = PortfolioState(0, 0.0, 195.0, 0.0, 0, {})
        v = guard.evaluate(_intent(), state, position_vega=20.0)
        assert v.decision == Verdict.DENY
        assert "vega" in v.reason.lower()

    def test_uses_absolute_value_for_negative_delta(self, guard):
        """A short-delta book breaches at -30 just as it does at +30."""
        state = PortfolioState(0, -29.5, 0.0, 0.0, 0, {})
        v = guard.evaluate(_intent(), state, position_delta=-5.0)
        assert v.decision == Verdict.DENY

    def test_allows_delta_reducing_trade_near_the_limit(self, guard):
        """Trading back toward flat must stay legal even at the limit."""
        state = PortfolioState(0, 29.0, 0.0, 0.0, 0, {})
        assert guard.evaluate(_intent(), state, position_delta=-5.0).decision == Verdict.ALLOW


class TestDailyLossAndStreaks:
    def test_halts_after_daily_loss_limit(self, guard):
        """2% of $100k = $2,000 down on the day => no new trades."""
        state = PortfolioState(0, 0.0, 0.0, -2100.0, 0, {})
        v = guard.evaluate(_intent(), state)
        assert v.decision == Verdict.DENY
        assert "daily" in v.reason.lower()

    def test_trades_normally_below_the_daily_limit(self, guard):
        state = PortfolioState(0, 0.0, 0.0, -500.0, 0, {})
        assert guard.evaluate(_intent(), state).decision == Verdict.ALLOW

    def test_halves_size_after_three_consecutive_losses(self, guard):
        state = PortfolioState(0, 0.0, 0.0, 0.0, 3, {})
        v = guard.evaluate(_intent(contracts=2), state)
        assert v.decision == Verdict.ALLOW_WITH_DOWNSIZE
        assert v.approved_contracts == 1

    def test_denies_when_halving_rounds_to_zero(self, guard):
        """Half of one contract is nothing tradeable — abstain, don't round up."""
        state = PortfolioState(0, 0.0, 0.0, 0.0, 3, {})
        v = guard.evaluate(_intent(contracts=1), state)
        assert v.decision == Verdict.DENY


class TestStructureAllowlist:
    def test_allows_a_listed_structure(self, guard):
        straddle = build_long_straddle(_q(450, "c", 2.00, 2.10), _q(450, "p", 2.00, 2.10))
        assert guard.evaluate(straddle, _flat()).decision == Verdict.ALLOW

    def test_denies_an_unlisted_structure(self, guard):
        """Anything not on the risk.yaml allowlist is refused by name."""
        from dataclasses import replace
        rogue = replace(_intent(), structure="naked_put")
        v = guard.evaluate(rogue, _flat())
        assert v.decision == Verdict.DENY
        assert "structure" in v.reason.lower()

    def test_denies_an_undefined_risk_intent(self, guard):
        from dataclasses import replace
        unbounded = replace(_intent(), max_loss=float("inf"))
        assert guard.evaluate(unbounded, _flat()).decision == Verdict.DENY


class TestKillSwitch:
    def test_kill_switch_file_halts_trading(self, guard, tmp_path):
        (tmp_path / "KILL_SWITCH").touch()
        v = guard.evaluate(_intent(), _flat())
        assert v.decision == Verdict.DENY
        assert "kill" in v.reason.lower()

    def test_kill_env_var_halts_trading(self, guard, monkeypatch):
        monkeypatch.setenv("KILL", "1")
        v = guard.evaluate(_intent(), _flat())
        assert v.decision == Verdict.DENY
        assert "kill" in v.reason.lower()

    def test_kill_zero_does_not_halt(self, guard, monkeypatch):
        monkeypatch.setenv("KILL", "0")
        assert guard.evaluate(_intent(), _flat()).decision == Verdict.ALLOW


class TestFailClosed:
    def test_none_intent_is_denied(self, guard):
        assert guard.evaluate(None, _flat()).decision == Verdict.DENY

    def test_missing_portfolio_state_is_denied(self, guard):
        assert guard.evaluate(_intent(), None).decision == Verdict.DENY

    def test_internal_exception_becomes_deny(self, guard):
        """A guard that throws must not let the order through."""
        class Exploding:
            def __getattr__(self, name):
                raise RuntimeError("boom")
        v = guard.evaluate(Exploding(), _flat())
        assert v.decision == Verdict.DENY
        assert "error" in v.reason.lower()

    def test_zero_contracts_is_denied(self, guard):
        from dataclasses import replace
        assert guard.evaluate(replace(_intent(), contracts=0), _flat()).decision == Verdict.DENY


class TestStartupChecks:
    def test_passes_on_a_fresh_paper_account(self, guard):
        ok, err = guard.startup_checks(is_paper=True, options_level=3, open_positions=0)
        assert ok is True, err

    def test_refuses_a_live_account(self, guard):
        """Paper only, ever (hard rule 3)."""
        ok, err = guard.startup_checks(is_paper=False, options_level=3, open_positions=0)
        assert ok is False
        assert "paper" in err.lower()

    def test_refuses_insufficient_options_level(self, guard):
        ok, err = guard.startup_checks(is_paper=True, options_level=1, open_positions=0)
        assert ok is False
        assert "level" in err.lower()

    def test_refuses_level_2_which_cannot_trade_spreads(self, guard):
        """Level 2 permits only long calls/puts. A level-2 account would pass
        startup and then have every multi-leg order rejected by the broker."""
        ok, err = guard.startup_checks(is_paper=True, options_level=2, open_positions=0)
        assert ok is False
        assert "level" in err.lower()

    def test_refuses_preexisting_positions(self, guard):
        ok, err = guard.startup_checks(is_paper=True, options_level=3, open_positions=2)
        assert ok is False
        assert "position" in err.lower()

    def test_accepts_the_expected_starting_equity(self, guard):
        ok, err = guard.startup_checks(is_paper=True, options_level=3,
                                       open_positions=0, equity=100_000.0)
        assert ok is True, err

    def test_refuses_an_account_that_is_not_a_fresh_100k(self, guard):
        """Hackathon rules require a new dedicated paper account at $100k.
        A used account means the reported P&L is not attributable to this agent."""
        ok, err = guard.startup_checks(is_paper=True, options_level=3,
                                       open_positions=0, equity=87_432.10)
        assert ok is False
        assert "equity" in err.lower()

    def test_equity_check_tolerates_sub_cent_drift(self, guard):
        ok, err = guard.startup_checks(is_paper=True, options_level=3,
                                       open_positions=0, equity=100_000.004)
        assert ok is True, err

    def test_equity_check_is_skipped_when_not_supplied(self, guard):
        """Equity is optional so mid-session checks don't fail once P&L moves."""
        ok, err = guard.startup_checks(is_paper=True, options_level=3, open_positions=0)
        assert ok is True, err
