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
        # --no-llm pins these tests to the deterministic spine they were
        # written against. Every test using this fixture is about preflight,
        # the guard or portfolio-state derivation — none is about candidate
        # selection — so the selector they run under is incidental, while
        # letting the real committee run would put `claude -p` subprocess
        # calls in the suite. The committee path has its own fixture
        # (`llm_bench`) and its own tests further down.
        return run_session.main(["--no-llm", *(argv or [])], cli=cli or FakeCLI(),
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
        """A position we cannot value must never be scored as flat Greeks.

        The row is a non-OCC symbol whose SPOT will not resolve. A non-OCC
        symbol alone is no longer unmeasurable — that is an assigned equity
        position, and it is now valued at 1 delta per share rather than
        freezing the desk (see TestAssignedEquityDoesNotFreezeTheDesk). What
        remains unmeasurable, and must still abstain, is an instrument this
        desk cannot even price.
        """
        class NoSpotForStrangers(FakeData):
            def get_stock_bars(self, symbol, days=30):
                if symbol != "SPY":
                    return pd.DataFrame()
                return super().get_stock_bars(symbol, days)

        cli = FakeCLI(positions=[{"symbol": "NOT-AN-OCC-SYMBOL", "qty": "1",
                                  "market_value": "100", "current_price": "1.00"}])
        bench.journal.append("fill", {"underlying": "QQQ", "structure": "iron_condor"})
        assert bench(cli=cli, data=NoSpotForStrangers()) == 0
        assert cli.posted == []
        out = capsys.readouterr().out.lower()
        assert "abstain" in out
        assert "cannot be valued" in out


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


# ── an assigned equity position must not freeze the desk ─────
#
# Parked ruling from the Plan 1 final review: book_greeks returned None for
# any row whose symbol is not an OCC option symbol, so once a short put was
# assigned and the account held 100 shares of SPY, EVERY later cycle printed
# "the existing book cannot be valued" and exited 0 — with no way left to
# manage or exit the position. It failed closed, so no money was at risk, but
# a desk that can never trade again is a demo-killer.
#
# An equity row is not unpriceable at all: 100 shares are exactly 100 delta
# and zero vega, which is the most certain Greek in the whole book.

class TestAssignedEquityDoesNotFreezeTheDesk:
    def test_a_long_share_position_is_plus_one_delta_per_share(self):
        rows = [{"symbol": "SPY", "qty": "100", "market_value": "45000",
                 "current_price": "450"}]
        delta, vega = run_session.book_greeks(rows, lambda root: 450.0)
        assert delta == pytest.approx(100.0)
        assert vega == pytest.approx(0.0)

    def test_a_short_share_position_is_minus_one_delta_per_share(self):
        rows = [{"symbol": "SPY", "qty": "-100", "market_value": "-45000",
                 "current_price": "450"}]
        delta, vega = run_session.book_greeks(rows, lambda root: 450.0)
        assert delta == pytest.approx(-100.0)
        assert vega == pytest.approx(0.0)

    def test_a_mixed_book_of_an_option_and_assigned_shares_is_valued(self):
        """The exact shape a partially assigned short put spread leaves
        behind: one surviving long put plus 100 shares."""
        rows = [
            {"symbol": _occ(445, "p"), "qty": "1", "market_value": "500",
             "current_price": "5.00"},
            {"symbol": "SPY", "qty": "100", "market_value": "45000",
             "current_price": "450"},
        ]
        book = run_session.book_greeks(rows, lambda root: 450.0)
        assert book is not None, "an assigned equity row must not blind the book"
        delta, vega = book
        option_delta, option_vega = run_session.book_greeks(
            rows[:1], lambda root: 450.0)
        assert delta == pytest.approx(option_delta + 100.0)
        assert vega == pytest.approx(option_vega)

    def test_a_genuinely_unvaluable_row_still_fails_closed(self):
        """Only an equity row gains a valuation. A row with no usable
        quantity is still unmeasurable, and unmeasurable still means abstain."""
        rows = [{"symbol": "SPY", "qty": "0", "market_value": "0"}]
        assert run_session.book_greeks(rows, lambda root: 450.0) is None

    def test_an_equity_row_without_a_spot_still_fails_closed(self):
        rows = [{"symbol": "SPY", "qty": "100", "market_value": "45000"}]
        assert run_session.book_greeks(rows, lambda root: None) is None

    def test_a_session_with_a_mixed_book_reaches_the_guard(self, bench, capsys):
        """End to end: the desk must keep running with shares in the account,
        not print "cannot be valued" and abstain forever."""
        bench.journal.append("fill", {"underlying": "SPY",
                                      "structure": "bull_put_spread",
                                      "contracts": 1})
        cli = FakeCLI(
            account={"options_trading_level": 3, "equity": "99000",
                     "last_equity": "100000"},
            positions=[
                {"symbol": _occ(440, "p"), "qty": "1", "market_value": "200",
                 "current_price": "2.00"},
                {"symbol": "SPY", "qty": "100", "market_value": "45000",
                 "current_price": "450"},
            ])
        assert bench(cli=cli, argv=["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "cannot be valued" not in out
        assert "guard:" in out, "the cycle must reach the guard, not freeze"
        assert "book:" in out


# ── the LLM committee is actually in the loop ────────────────
#
# committee/ was built, tested and verified against real Claude calls — and
# scripts/run_session.py never called it. The agentic layer was a library
# nobody invoked, and the session still picked by a deterministic
# credit/max-loss ratio. These tests hold that wiring in place.
#
# Note on the `bench` fixture above: it now prepends --no-llm, so every test
# before this point exercises the deterministic spine exactly as it always
# did. Those tests are about preflight, the guard and portfolio-state
# derivation — none of them is about candidate selection — so pinning them to
# the deterministic selector preserves what they were written to prove while
# keeping the network out of the suite. The committee path is covered here,
# with an injected committee.

from committee.decide import ABSTAIN, CommitteeDecision


def _decision(chosen=None, *, choice_id=ABSTAIN, abstain_reason="",
              aggregate=0.62, views=(), trader_reasoning="best risk/reward",
              thesis=(True, "net delta consistent with the thesis"),
              blind=(True, "reads fine")):
    return CommitteeDecision(
        chosen=chosen, choice_id=choice_id, views=tuple(views),
        aggregate_probability=aggregate, trader_reasoning=trader_reasoning,
        thesis_ok=thesis[0], thesis_reason=thesis[1],
        blind_ok=blind[0], blind_reason=blind[1],
        snapshot_hash="f" * 64, abstain_reason=abstain_reason,
    )


def _views(vol_p=0.62, bear_abstains=False):
    from committee.analysts import AnalystView
    return (
        AnalystView("vol_analyst", vol_p, False, "", "implied is below realized",
                    "claude-haiku-4-5", "a" * 64),
        AnalystView("bear_adversary", None, True, "no event calendar given", "",
                    "claude-haiku-4-5", "b" * 64) if bear_abstains else
        AnalystView("bear_adversary", 0.44, False, "", "gap risk into the print",
                    "claude-haiku-4-5", "b" * 64),
    )


def _spreads(candidates):
    """Credit spreads only — the fake chain's straddle has an unpriceable leg,
    so its Greeks are unmeasurable and the session abstains before the guard.
    That abstention is its own (already covered) behaviour, not what these
    selection tests are about."""
    return [c for c in candidates if c.structure != "long_straddle"]


class FakeCommittee:
    """Injected in place of committee.decide.decide. Records its inputs."""

    def __init__(self, decision=None, error=None, pick=None):
        self.decision, self.error, self.pick = decision, error, pick
        self.calls = []

    def __call__(self, underlying, spot, realized_vol, candidates, journal):
        self.calls.append({"underlying": underlying, "spot": spot,
                           "realized_vol": realized_vol,
                           "candidates": candidates, "journal": journal})
        if self.error:
            raise self.error
        if self.pick is not None:
            chosen = self.pick(candidates)
            return _decision(chosen, choice_id="c1", views=_views())
        return self.decision if self.decision is not None else _decision(
            abstain_reason="the committee found no edge", views=_views())


@pytest.fixture
def llm_bench(tmp_path, monkeypatch):
    """Like `bench`, but the committee is IN the loop (no --no-llm)."""
    monkeypatch.delenv("KILL", raising=False)
    shutil.copy(RISK_YAML, tmp_path / "risk.yaml")
    guard = RiskGuard(load_risk_config(tmp_path / "risk.yaml"))
    journal = Journal(tmp_path / "journal.jsonl")

    def run(committee, cli=None, data=None, argv=None):
        return run_session.main(argv or [], cli=cli or FakeCLI(),
                                data=data or FakeData(), journal=journal,
                                guard=guard, committee=committee)

    run.guard, run.journal, run.tmp_path = guard, journal, tmp_path
    return run


class TestCommitteeIsWired:
    def test_the_committee_is_called_by_default(self, llm_bench):
        committee = FakeCommittee(pick=lambda c: c[0])
        llm_bench(committee)
        assert len(committee.calls) == 1

    def test_the_committee_receives_the_built_candidates_and_live_state(self, llm_bench):
        committee = FakeCommittee(pick=lambda c: c[0])
        llm_bench(committee)
        call = committee.calls[0]
        assert call["underlying"] == "SPY"
        assert call["spot"] == 450.0
        assert call["candidates"], "the committee must see the deterministic candidates"
        assert isinstance(call["realized_vol"], float)

    def test_the_committees_choice_is_the_order_that_is_sent(self, llm_bench):
        """Not the highest-credit candidate — the one the committee named."""
        picked = {}

        def pick(candidates):
            # A credit spread other than the ratio-best one, so this test
            # fails if the session quietly re-selects for itself.
            best = run_session.best_by_credit_ratio(candidates)
            chosen = next(c for c in _spreads(candidates) if c is not best)
            picked["intent"] = chosen
            return chosen

        cli = FakeCLI()
        assert llm_bench(FakeCommittee(pick=pick), cli=cli) == 0
        assert len(cli.posted) == 1
        sent = {leg["symbol"] for leg in cli.posted[0]["legs"]}
        assert sent == {leg.quote.symbol for leg in picked["intent"].legs}

    def test_the_deterministic_and_committee_paths_can_disagree(self, llm_bench, bench):
        """If they always agreed, the wiring would be untestable — and
        pointless. The committee must be able to pick a different trade."""
        seen = {}

        def pick(candidates):
            seen["best"] = run_session.best_by_credit_ratio(candidates)
            # A DIFFERENT spread from the deterministic pick, by identity.
            seen["other"] = next(c for c in _spreads(candidates)
                                 if c is not seen["best"])
            return seen["other"]

        cli = FakeCLI()
        llm_bench(FakeCommittee(pick=pick), cli=cli)
        assert seen["other"] is not seen["best"]
        assert len(cli.posted) == 1

    def test_the_guard_still_runs_on_whatever_the_committee_chose(self, llm_bench, capsys):
        """The committee proposes; RiskGuard disposes. A committee choice must
        never reach the broker without a verdict."""
        cli = FakeCLI()
        llm_bench(FakeCommittee(pick=lambda c: c[0]), cli=cli)
        assert "guard:" in capsys.readouterr().out

    def test_a_committee_choice_the_guard_denies_is_never_sent(self, llm_bench, capsys):
        cli = FakeCLI(account={"options_trading_level": 3,
                               "equity": "97000", "last_equity": "100000"})
        llm_bench.journal.append("fill", {"underlying": "QQQ", "structure": "iron_condor"})
        assert llm_bench(FakeCommittee(pick=lambda c: c[0]), cli=cli) == 0
        assert cli.posted == []
        assert "guard refuses" in capsys.readouterr().out.lower()


class TestCommitteeAbstention:
    def test_an_abstention_is_exit_zero_with_the_reason_printed(self, llm_bench, capsys):
        """An ABSTAIN is a normal outcome, not a failure."""
        cli = FakeCLI()
        committee = FakeCommittee(_decision(
            abstain_reason="c1 (bull_put_spread) vetoed — blind review: gap risk"))
        assert llm_bench(committee, cli=cli) == 0
        assert cli.posted == []
        out = capsys.readouterr().out
        assert "abstain" in out.lower()
        assert "gap risk" in out

    def test_a_committee_that_raises_abstains_rather_than_crashing(self, llm_bench, capsys):
        cli = FakeCLI()
        committee = FakeCommittee(error=RuntimeError("claude is rate limited"))
        assert llm_bench(committee, cli=cli) == 0
        assert cli.posted == [], "a broken committee must never fall through to trading"
        assert "abstain" in capsys.readouterr().out.lower()

    def test_an_abstention_is_journalled(self, llm_bench):
        before = len(llm_bench.journal.entries())
        llm_bench(FakeCommittee(_decision(abstain_reason="no edge")))
        assert len(llm_bench.journal.entries()) > before


class TestNoLLMFallback:
    def test_no_llm_never_calls_the_committee(self, llm_bench):
        """Monday's escape hatch: if Claude rate-limits mid-session the desk
        must still be able to trade rather than being dead."""
        committee = FakeCommittee(pick=lambda c: c[0])
        cli = FakeCLI()
        assert llm_bench(committee, cli=cli, argv=["--no-llm"]) == 0
        assert committee.calls == []
        assert len(cli.posted) == 1

    def test_no_llm_reproduces_the_deterministic_selection(self, llm_bench, capsys):
        assert llm_bench(FakeCommittee(), argv=["--no-llm", "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "Selected" in out

    def test_the_active_mode_is_printed_in_both_modes(self, llm_bench, capsys):
        llm_bench(FakeCommittee(pick=lambda c: c[0]), argv=["--dry-run"])
        llm_out = capsys.readouterr().out
        assert "COMMITTEE" in llm_out.upper()

        llm_bench(FakeCommittee(), argv=["--no-llm", "--dry-run"])
        det_out = capsys.readouterr().out
        assert "DETERMINISTIC" in det_out.upper()
        assert "--no-llm" in det_out


class TestCommitteeDryRun:
    def test_the_dry_run_shows_the_whole_reasoning_chain(self, llm_bench, capsys):
        """A judge watching the screen must see every link: each analyst's
        probability or abstention, the aggregate, the trader's choice and
        reasoning, both veto results, the guard verdict and the payload."""
        committee = FakeCommittee(pick=lambda c: c[0])
        committee.decision = None
        assert llm_bench(committee, argv=["--dry-run"]) == 0
        out = capsys.readouterr().out

        assert "vol_analyst" in out and "0.62" in out
        assert "bear_adversary" in out and "0.44" in out
        assert "implied is below realized" in out
        assert "gap risk into the print" in out
        assert "aggregate" in out.lower()
        assert "trader" in out.lower() and "best risk/reward" in out
        assert "thesis" in out.lower() and "blind" in out.lower()
        assert "guard:" in out
        assert "client_order_id" in out and "mleg" in out
        assert "DRY RUN" in out

    def test_an_abstaining_analyst_is_shown_as_abstained_not_as_zero(self, llm_bench, capsys):
        committee = FakeCommittee(_decision(
            abstain_reason="no edge", views=_views(bear_abstains=True)))
        llm_bench(committee, argv=["--dry-run"])
        out = capsys.readouterr().out
        assert "bear_adversary" in out
        assert "ABSTAIN" in out
        assert "no event calendar given" in out
        assert "0.00" not in out.split("bear_adversary")[1].split("\n")[0]

    def test_a_failed_veto_is_shown_as_failed(self, llm_bench, capsys):
        committee = FakeCommittee(_decision(
            abstain_reason="vetoed", views=_views(),
            blind=(False, "the breakeven is one gap away")))
        llm_bench(committee, argv=["--dry-run"])
        out = capsys.readouterr().out
        assert "the breakeven is one gap away" in out
        assert "VETO" in out.upper() or "FAIL" in out.upper()

    def test_the_dry_run_keeps_the_committee_out_of_the_judged_journal(self, llm_bench):
        """A dry run is a rehearsal — decide() gets no journal to write to."""
        committee = FakeCommittee(pick=lambda c: c[0])
        before = len(llm_bench.journal.entries())
        llm_bench(committee, argv=["--dry-run"])
        assert committee.calls[0]["journal"] is None
        assert len(llm_bench.journal.entries()) == before


# ── a not-run veto must not read as a fired veto ─────────────
#
# When the cycle abstains before the veto layer, both thesis_ok and
# blind_ok stay False with reason=NOT_RUN (committee/decide.py). Printing
# that as "VETO — not reached ..." puts the word VETO in front of a check
# that never ran: a judge skimming the transcript reads it as two vetoes
# having fired on a trade that was never proposed. The display must make
# the not-run state unmistakable without claiming PASS (a check that did
# not run must never render as passed) and without leading with VETO.

class TestNotRunVetoRendering:
    def test_not_run_veto_does_not_read_as_a_fired_veto_or_a_pass(self, capsys):
        from committee.decide import NOT_RUN
        decision = _decision(thesis=(False, NOT_RUN), blind=(False, NOT_RUN))
        run_session.print_committee(decision)
        out = capsys.readouterr().out
        thesis_line = [l for l in out.splitlines() if "veto thesis" in l][0]
        blind_line = [l for l in out.splitlines() if "veto blind" in l][0]
        for line in (thesis_line, blind_line):
            stripped = line.strip()
            assert not stripped.startswith("VETO"), line
            assert "PASS" not in line, line
            assert "not run" in line.lower()

    def test_a_veto_that_actually_ran_still_renders_pass_or_veto(self, capsys):
        run_session.print_committee(_decision(thesis=(True, "fine"),
                                               blind=(False, "gap risk")))
        out = capsys.readouterr().out
        assert "veto thesis: PASS" in out
        assert "veto blind:  VETO" in out
