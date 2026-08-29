"""Tests for scripts/run_session.py `main()` — the money path.

This is the only production caller of OptionsExecutor.submit(), and every
Critical finding of the final whole-branch review lived in these ~60 lines
with no test over them. Nothing here touches the network: the Alpaca CLI and
the data adapter are injected fakes, following the FakeCLI pattern in
tests/test_executor_options.py.
"""
import json
import os
import shutil
import sys
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from candidate_builder import OptionQuote
from journal import Journal
from risk_guard import RiskGuard, load_risk_config

import run_session

EXPIRY = date.today() + timedelta(days=30)
RISK_YAML = os.path.join(os.path.dirname(__file__), "..", "risk.yaml")


# ── fakes ────────────────────────────────────────────────────

def _occ(strike: float, right: str, expiry: date = EXPIRY, root: str = "SPY") -> str:
    return f"{root}{expiry:%y%m%d}{right.upper()}{int(strike * 1000):08d}"


def _chain(strikes=(430.0, 435.0, 440.0, 445.0, 455.0, 460.0, 465.0, 470.0)):
    return [OptionQuote(_occ(k, r), "SPY", k, EXPIRY, r, 2.00, 2.10, 800)
            for k in strikes for r in ("p", "c")]


def _bars(last_close=450.0, days=30):
    return pd.DataFrame([
        {"timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(days=i),
         "open": 450.0, "high": 452.0, "low": 448.0,
         "close": last_close, "volume": 1_000_000}
        for i in range(days)
    ])


class FakeData:
    """Injected market data. Raises where the real adapter raises."""

    def __init__(self, chain=None, bars=None, chain_error=None, bars_error=None):
        self._chain = _chain() if chain is None else chain
        self._bars = _bars() if bars is None else bars
        self._chain_error, self._bars_error = chain_error, bars_error
        self.bar_calls = []

    def get_stock_bars(self, symbol, days=30):
        self.bar_calls.append(symbol)
        if self._bars_error:
            raise self._bars_error
        return self._bars

    def get_option_chain(self, symbol):
        if self._chain_error:
            raise self._chain_error
        return self._chain


class FakeCLI:
    """The Alpaca CLI adapter's surface, with every response injectable."""

    def __init__(self, account=None, positions=None, orders=None,
                 statuses=None, errors=None):
        self.posted = []
        self._account = account if account is not None else {
            "options_trading_level": 3, "equity": "100000", "last_equity": "100000",
        }
        self._positions = positions if positions is not None else []
        self._orders = orders if orders is not None else []
        self._statuses = list(statuses or ["filled"])
        self._errors = errors or {}

    def _maybe_raise(self, name):
        if name in self._errors:
            raise self._errors[name]

    def available(self):
        return True

    def get_account(self):
        self._maybe_raise("get_account")
        return self._account

    def list_positions(self):
        self._maybe_raise("list_positions")
        return self._positions

    def list_orders(self, status="open", limit=500):
        self._maybe_raise("list_orders")
        return self._orders

    def post_order(self, payload):
        self.posted.append(payload)
        return {"id": "o-1", "status": "accepted"}

    def get_order(self, order_id):
        status = self._statuses.pop(0) if self._statuses else "filled"
        return {"id": order_id, "status": status, "filled_qty": "1"}


@pytest.fixture
def bench(tmp_path, monkeypatch):
    """A session bench: guard over a tmp risk.yaml, journal in tmp_path.

    The kill switch resolves against the config directory, so a test trips it
    by dropping KILL_SWITCH into tmp_path — never into the real repo.
    """
    monkeypatch.delenv("KILL", raising=False)
    shutil.copy(RISK_YAML, tmp_path / "risk.yaml")
    guard = RiskGuard(load_risk_config(tmp_path / "risk.yaml"))
    journal = Journal(tmp_path / "journal.jsonl")

    def run(cli=None, data=None, argv=None):
        return run_session.main(argv or [], cli=cli or FakeCLI(),
                                data=data or FakeData(), journal=journal,
                                guard=guard)

    run.guard, run.journal, run.tmp_path = guard, journal, tmp_path
    return run


# ── preflight and halts ──────────────────────────────────────

class TestPreflight:
    def test_preflight_failure_exits_nonzero_and_submits_nothing(self, bench):
        """Options level 1 cannot trade spreads at all."""
        cli = FakeCLI(account={"options_trading_level": 1, "equity": "100000",
                               "last_equity": "100000"})
        assert bench(cli=cli) == 1
        assert cli.posted == []

    def test_kill_switch_halts_before_anything_is_sent(self, bench):
        (bench.tmp_path / "KILL_SWITCH").touch()
        cli, data = FakeCLI(), FakeData()
        assert bench(cli=cli, data=data) != 0
        assert cli.posted == []
        assert data.bar_calls == [], "halted sessions must not even fetch data"

    def test_kill_env_var_halts(self, bench, monkeypatch):
        monkeypatch.setenv("KILL", "1")
        cli = FakeCLI()
        assert bench(cli=cli) != 0
        assert cli.posted == []

    def test_a_broker_query_failure_fails_closed(self, bench):
        """A transient CLI/auth failure must abort cleanly, not traceback past
        the guard."""
        cli = FakeCLI(errors={"list_orders": RuntimeError("CLI auth failed")})
        assert bench(cli=cli) == 1
        assert cli.posted == []

    def test_stale_equity_does_not_block_a_session_that_has_traded(self, bench, capsys):
        """C3: after the first fill, equity has moved and a position is open.
        The desk must still be able to run."""
        bench.journal.append("fill", {"underlying": "QQQ", "structure": "iron_condor",
                                      "contracts": 1})
        cli = FakeCLI(account={"options_trading_level": 3, "equity": "98750.25",
                               "last_equity": "100000"},
                      positions=[{"symbol": _occ(400, "p", root="QQQ"), "qty": "-1",
                                  "market_value": "-300", "current_price": "3.00"},
                                 {"symbol": _occ(395, "p", root="QQQ"), "qty": "1",
                                  "market_value": "200", "current_price": "2.00"}])
        assert bench(cli=cli, argv=["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "Preflight failed" not in out
        assert "guard:" in out, "the cycle must reach the guard, not abort early"

    def test_first_ever_run_still_demands_a_fresh_account(self, bench):
        """The submission requires a new dedicated $100k paper account. With an
        empty journal and no working orders, that assertion still fires."""
        cli = FakeCLI(account={"options_trading_level": 3, "equity": "87000",
                               "last_equity": "87000"})
        assert bench(cli=cli) == 1
        assert cli.posted == []


# ── C1: duplicate order prevention ───────────────────────────

class TestWorkingOrders:
    def test_a_working_order_in_the_underlying_abstains(self, bench):
        """C1: an unfilled day order is invisible to list_positions. Without
        this check the next cycle sees a flat account and submits the same
        spread again, and both can fill."""
        cli = FakeCLI(orders=[{
            "id": "o-9", "status": "new", "order_class": "mleg",
            "legs": [{"symbol": _occ(445, "p")}, {"symbol": _occ(440, "p")}],
        }])
        assert bench(cli=cli) == 0
        assert cli.posted == []

    def test_a_working_order_in_another_underlying_does_not_block(self, bench):
        cli = FakeCLI(orders=[{
            "id": "o-9", "status": "new", "order_class": "mleg",
            "legs": [{"symbol": _occ(300, "p", root="QQQ")}],
        }])
        assert bench(cli=cli) == 0
        assert len(cli.posted) == 1

    def test_an_unattributable_working_order_blocks_conservatively(self, bench):
        """If we cannot tell what an order is in, we must assume it is ours."""
        cli = FakeCLI(orders=[{"id": "o-9", "status": "new"}])
        assert bench(cli=cli) == 0
        assert cli.posted == []

    def test_the_submitted_payload_is_idempotent_at_the_broker(self, bench):
        cli = FakeCLI()
        assert bench(cli=cli) == 0
        assert cli.posted[0]["client_order_id"]


# ── I3: data failures are loud ───────────────────────────────

class TestDataFailures:
    def test_chain_fetch_failure_exits_nonzero(self, bench, capsys):
        """Spec 4.4. An outage must not print ABSTAIN and exit 0."""
        from alpaca_data import MarketDataError
        cli = FakeCLI()
        data = FakeData(chain_error=MarketDataError("chain fetch failed for SPY"))
        assert bench(cli=cli, data=data) != 0
        assert cli.posted == []
        out = capsys.readouterr().out.lower()
        assert "data" in out and "abstain" not in out

    def test_empty_bars_exit_nonzero(self, bench):
        cli = FakeCLI()
        assert bench(cli=cli, data=FakeData(bars=pd.DataFrame())) == 1
        assert cli.posted == []

    def test_an_illiquid_chain_abstains_with_exit_zero(self, bench, capsys):
        """The other side of the distinction: a real, healthy, untradeable
        market is an abstention, not a failure."""
        illiquid = [OptionQuote(_occ(k, r), "SPY", k, EXPIRY, r, 2.00, 2.10, 5)
                    for k in (440.0, 445.0) for r in ("p", "c")]
        cli = FakeCLI()
        assert bench(cli=cli, data=FakeData(chain=illiquid)) == 0
        assert cli.posted == []
        assert "abstain" in capsys.readouterr().out.lower()


# ── deferred minor 5: the dry run is a real preflight ────────

class TestDryRun:
    def test_dry_run_submits_nothing(self, bench):
        cli = FakeCLI()
        assert bench(cli=cli, argv=["--dry-run"]) == 0
        assert cli.posted == []

    def test_dry_run_shows_greeks_verdict_and_the_exact_payload(self, bench, capsys):
        """The point of a dry run is to see what the live run would do. It used
        to return before the Greeks were even computed."""
        assert bench(argv=["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "delta" in out.lower() and "vega" in out.lower()
        assert "ALLOW" in out or "DENY" in out
        assert "client_order_id" in out, "the exact wire payload must be shown"
        assert "mleg" in out

    def test_dry_run_writes_nothing_to_the_journal(self, bench):
        before = len(bench.journal.entries())
        bench(argv=["--dry-run"])
        assert len(bench.journal.entries()) == before


# ── C2: the portfolio state is real ──────────────────────────

class TestPortfolioStateIsDerived:
    def test_daily_loss_halt_actually_fires(self, bench, capsys):
        """risk.yaml halts at 2% of $100k. equity - last_equity is the day's
        move; hardcoding 0.0 made this limit decorative."""
        cli = FakeCLI(account={"options_trading_level": 3,
                               "equity": "97000", "last_equity": "100000"})
        bench.journal.append("fill", {"underlying": "QQQ", "structure": "iron_condor"})
        assert bench(cli=cli) == 0
        assert cli.posted == []
        assert "daily loss" in capsys.readouterr().out.lower()

    def test_consecutive_losses_from_the_journal_reach_the_guard(self, bench, capsys):
        """Three trailing losing closes halve size; one contract halves to
        nothing, so the guard abstains."""
        for i in range(3):
            bench.journal.append("close", {"underlying": "SPY", "realized_pnl": -120.0})
        cli = FakeCLI()
        assert bench(cli=cli) == 0
        assert cli.posted == []
        assert "consecutive losses" in capsys.readouterr().out.lower()

    def test_a_win_resets_the_losing_streak(self, bench):
        bench.journal.append("close", {"underlying": "SPY", "realized_pnl": -120.0})
        bench.journal.append("close", {"underlying": "SPY", "realized_pnl": -120.0})
        bench.journal.append("close", {"underlying": "SPY", "realized_pnl": +50.0})
        bench.journal.append("close", {"underlying": "SPY", "realized_pnl": -120.0})
        cli = FakeCLI()
        assert bench(cli=cli) == 0
        assert len(cli.posted) == 1

    def test_one_new_trade_per_underlying_per_day_actually_fires(self, bench, capsys):
        bench.journal.append("fill", {"underlying": "SPY", "structure": "bull_put_spread",
                                      "contracts": 1})
        cli = FakeCLI()
        assert bench(cli=cli) == 0
        assert cli.posted == []
        assert "underlying" in capsys.readouterr().out.lower()

    def test_yesterdays_fill_does_not_count_against_today(self, bench):
        entry = bench.journal.append("fill", {"underlying": "SPY",
                                              "structure": "bull_put_spread"})
        # Rewrite the journal with a stale timestamp (a fixture, not tampering
        # with a live chain — the file is rebuilt from scratch).
        stale = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        lines = bench.journal.path.read_text().splitlines()
        obj = json.loads(lines[0])
        obj["timestamp"] = stale
        bench.journal.path.write_text(json.dumps(obj) + "\n")
        cli = FakeCLI()
        assert bench(cli=cli) == 0
        assert len(cli.posted) == 1

    def test_the_existing_books_greeks_are_charged_against_the_limit(self, bench, capsys):
        """C2: only the new position's Greeks were checked, so a book already
        past the |net delta| <= 30 limit could not block the next trade.

        Ten long 445 puts against spot 450 is roughly -400 delta — far outside
        the limit, and invisible while net_delta was hardcoded to 0.0.
        """
        heavy = [{"symbol": _occ(445, "p"), "qty": "10",
                  "market_value": "5000", "current_price": "5.00"}]
        cli = FakeCLI(positions=heavy)
        bench.journal.append("fill", {"underlying": "QQQ", "structure": "iron_condor"})
        assert bench(cli=cli) == 0
        assert cli.posted == []
        out = capsys.readouterr().out
        assert "net delta would reach" in out.lower(), out
        # and the book really was measured, not assumed flat
        book_line = [ln for ln in out.splitlines() if ln.strip().startswith("book:")][0]
        assert "delta +0.0" not in book_line

    def test_an_unpriceable_book_abstains_rather_than_assuming_zero(self, bench, capsys):
        """A position we cannot value must never be scored as flat Greeks."""
        cli = FakeCLI(positions=[{"symbol": "NOT-AN-OCC-SYMBOL", "qty": "1",
                                  "market_value": "100", "current_price": "1.00"}])
        bench.journal.append("fill", {"underlying": "QQQ", "structure": "iron_condor"})
        assert bench(cli=cli) == 0
        assert cli.posted == []
        assert "abstain" in capsys.readouterr().out.lower()


# ── guard refusal must short-circuit, not fall through ───────

class TestGuardRefusalShortCircuits:
    def test_a_guard_refusal_never_reaches_executor_submit(self, bench, monkeypatch, capsys):
        """TOCTOU guard: previously, printing "Guard refuses this candidate"
        was followed unconditionally by executor.submit(), which
        re-evaluates the guard from scratch. If the kill switch were lifted
        between the two evaluate() calls, that fall-through would submit an
        order right after printing a refusal. Prove submit() is never
        called at all once the pre-check has already refused."""
        import run_session as rs

        def _boom(self, *a, **k):
            raise AssertionError("executor.submit() must not be reached "
                                  "after a pre-check guard refusal")
        monkeypatch.setattr(rs.OptionsExecutor, "submit", _boom)

        cli = FakeCLI(account={"options_trading_level": 3,
                               "equity": "97000", "last_equity": "100000"})
        bench.journal.append("fill", {"underlying": "QQQ", "structure": "iron_condor"})
        assert bench(cli=cli) == 0
        assert cli.posted == []
        assert "guard refuses" in capsys.readouterr().out.lower()


# ── I5: positions vs legs ────────────────────────────────────

class TestPositionCounting:
    def test_a_two_leg_spread_counts_as_one_position(self):
        rows = [{"symbol": _occ(445, "p")}, {"symbol": _occ(440, "p")}]
        assert run_session.count_positions(rows) == 1

    def test_two_spreads_in_different_expiries_count_as_two(self):
        other = date.today() + timedelta(days=20)
        rows = [{"symbol": _occ(445, "p")}, {"symbol": _occ(440, "p")},
                {"symbol": _occ(445, "p", expiry=other)},
                {"symbol": _occ(440, "p", expiry=other)}]
        assert run_session.count_positions(rows) == 2

    def test_spreads_in_different_underlyings_count_separately(self):
        rows = [{"symbol": _occ(445, "p")}, {"symbol": _occ(440, "p")},
                {"symbol": _occ(300, "p", root="QQQ")}]
        assert run_session.count_positions(rows) == 2

    def test_an_unparseable_row_counts_as_its_own_position(self):
        """Conservative: an unknown row is a position we cannot group away."""
        assert run_session.count_positions([{"symbol": "AAPL"}]) == 1

    def test_three_spreads_fill_the_position_cap(self, bench, capsys):
        """risk.yaml says max_positions: 3. Counting legs made that mean ONE
        spread; counting strategies makes it mean three."""
        rows = []
        for i, exp in enumerate((date.today() + timedelta(days=d) for d in (10, 20, 30))):
            rows += [{"symbol": _occ(445, "p", expiry=exp), "qty": "-1",
                      "market_value": "-100", "current_price": "1.00"},
                     {"symbol": _occ(440, "p", expiry=exp), "qty": "1",
                      "market_value": "50", "current_price": "0.50"}]
        assert run_session.count_positions(rows) == 3
        cli = FakeCLI(positions=rows)
        bench.journal.append("fill", {"underlying": "QQQ", "structure": "iron_condor"})
        assert bench(cli=cli) == 0
        assert cli.posted == []
        assert "max_positions" in capsys.readouterr().out


# ── pure helpers ─────────────────────────────────────────────

class TestHelpers:
    def test_parse_occ_reads_root_expiry_right_and_strike(self):
        occ = run_session.parse_occ("SPY260918P00445000")
        assert occ.root == "SPY"
        assert occ.expiry == date(2026, 9, 18)
        assert occ.right == "p"
        assert occ.strike == 445.0

    def test_parse_occ_rejects_an_equity_symbol(self):
        assert run_session.parse_occ("SPY") is None
        assert run_session.parse_occ("") is None

    def test_daily_pnl_is_equity_minus_last_equity(self):
        assert run_session.daily_pnl({"equity": "99000", "last_equity": "100000"}) == -1000.0

    def test_daily_pnl_is_none_when_it_cannot_be_computed(self):
        """Unmeasurable P&L must not read as a flat day."""
        assert run_session.daily_pnl({"equity": "99000"}) is None
        assert run_session.daily_pnl({}) is None

    def test_consecutive_losses_counts_only_the_trailing_run(self):
        entries = [
            {"type": "close", "payload": {"realized_pnl": -10}},
            {"type": "close", "payload": {"realized_pnl": 20}},
            {"type": "close", "payload": {"realized_pnl": -10}},
            {"type": "close", "payload": {"realized_pnl": -10}},
        ]
        assert run_session.consecutive_losses(entries) == 2

    def test_consecutive_losses_is_zero_with_no_closed_trades(self):
        """An honest zero: the journal has nothing to say yet."""
        assert run_session.consecutive_losses([{"type": "proposal", "payload": {}}]) == 0
        assert run_session.consecutive_losses([]) == 0

    def test_opening_fills_without_pnl_do_not_break_the_streak(self):
        entries = [
            {"type": "close", "payload": {"realized_pnl": -10}},
            {"type": "fill", "payload": {"underlying": "SPY"}},
            {"type": "close", "payload": {"realized_pnl": -10}},
        ]
        assert run_session.consecutive_losses(entries) == 2

    def test_underlyings_of_order_reads_nested_legs(self):
        order = {"legs": [{"symbol": _occ(445, "p")}, {"symbol": _occ(440, "p")}]}
        assert run_session.underlyings_of_order(order) == {"SPY"}

    def test_underlyings_of_order_reads_a_top_level_symbol(self):
        assert run_session.underlyings_of_order({"symbol": "AAPL"}) == {"AAPL"}

    def test_book_greeks_returns_none_for_an_unpriceable_position(self):
        rows = [{"symbol": _occ(400, "c"), "qty": "1",
                 "market_value": "305", "current_price": "3.05"}]
        # A 400-strike call marked at 3.05 with spot 450 is below intrinsic.
        assert run_session.book_greeks(rows, lambda root: 450.0) is None

    def test_book_greeks_signs_a_short_position_negative(self):
        long_row = [{"symbol": _occ(445, "p"), "qty": "1",
                     "market_value": "500", "current_price": "5.00"}]
        short_row = [{"symbol": _occ(445, "p"), "qty": "-1",
                      "market_value": "-500", "current_price": "5.00"}]
        long_d, _ = run_session.book_greeks(long_row, lambda root: 450.0)
        short_d, _ = run_session.book_greeks(short_row, lambda root: 450.0)
        assert long_d < 0 < short_d          # a long put is short delta
        assert long_d == pytest.approx(-short_d)

    def test_book_greeks_of_an_empty_book_is_flat(self):
        assert run_session.book_greeks([], lambda root: 450.0) == (0.0, 0.0)

    def test_book_greeks_returns_none_without_a_spot(self):
        rows = [{"symbol": _occ(445, "p"), "qty": "1", "market_value": "500"}]
        assert run_session.book_greeks(rows, lambda root: None) is None
