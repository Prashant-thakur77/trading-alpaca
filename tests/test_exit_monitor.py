"""Tests for exit_monitor.py — managing an open book, and the keystone
`close` journal entry that carries realized P&L.

Nothing in this codebase journalled a CLOSED trade with a realized P&L before
this module, and four separate things were dormant or broken because of it:
the Brier calibration loop had nothing to score, `consecutive_losses` was
permanently 0 so risk.yaml's 3-loser size-halving never fired, an assigned
equity position froze the desk, and the pre-mortem's triggers were rules
nobody evaluated.

The properties held here:

  * the deterministic exits apply to EVERY position whatever the pre-mortem
    said — 50% of the credit, max loss, and DTE <= 3;
  * a fired trigger closes with a NEW inverted multi-leg order, because
    `close_position` closes one leg at a time and there is no atomic
    multi-leg close (design spec A.1, verified live);
  * the `close` entry carries realized_pnl, underlying, structure, the
    originating snapshot_hash and the trigger that fired — everything
    `calibration.resolved_predictions` needs to join an outcome back to the
    analyst predictions that produced it;
  * every failure fails closed: an unreadable mark, a missing leg, a broker
    rejection or a raising journal never opens a position and never silently
    swallows a protective exit.
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import pytest

from candidate_builder import (
    Leg, OptionQuote, TradeIntent, build_bull_put_spread,
)
from committee.premortem import (
    KIND_CREDIT_DECAY, KIND_DTE_BELOW, KIND_IV_SPIKE, KIND_UNDERLYING_BEYOND,
    ExitTrigger,
)
from exit_monitor import ExitEvent, OpenTrade, monitor_positions
from journal import Journal
from risk_guard import RiskGuard, load_risk_config

RISK_YAML = os.path.join(os.path.dirname(__file__), "..", "risk.yaml")
EXPIRY = date.today() + timedelta(days=30)
SPOT = 500.0


# ── fixtures and fakes ──────────────────────────────────────

def _occ(strike, right, expiry=EXPIRY, root="SPY"):
    return f"{root}{expiry:%y%m%d}{right.upper()}{int(strike * 1000):08d}"


def _q(strike, right, bid, ask, expiry=EXPIRY, oi=500):
    return OptionQuote(symbol=_occ(strike, right, expiry), underlying="SPY",
                       strike=strike, expiry=expiry, right=right,
                       bid=bid, ask=ask, open_interest=oi)


def _bull_put(expiry=EXPIRY):
    """Short 495p / long 490p: credit 1.00, width 5, max loss $400."""
    if expiry == EXPIRY:
        return build_bull_put_spread(_q(495, "p", 2.40, 2.60),
                                     _q(490, "p", 1.45, 1.55))
    # Near expiry fails the builder's 7-45 DTE liquidity gate by design, so a
    # trade that has AGED into the exit window is constructed directly.
    short, long_ = _q(495, "p", 2.40, 2.60, expiry), _q(490, "p", 1.45, 1.55, expiry)
    return TradeIntent(
        underlying="SPY", structure="bull_put_spread",
        legs=(Leg(short, "sell", 1), Leg(long_, "buy", 1)), contracts=1,
        net_credit=1.00, max_loss=400.0, max_profit=100.0,
        breakevens=(494.0,), dte=short.dte,
    )


def _row(strike, right, qty, mark, expiry=EXPIRY):
    return {"symbol": _occ(strike, right, expiry), "qty": str(qty),
            "market_value": f"{mark * abs(qty) * 100 * (1 if qty > 0 else -1):.2f}",
            "current_price": f"{mark:.2f}"}


def _book(short_mark, long_mark, expiry=EXPIRY):
    """The two position rows of an open 495/490 bull put spread."""
    return [_row(495, "p", -1, short_mark, expiry),
            _row(490, "p", 1, long_mark, expiry)]


class FakeCLI:
    def __init__(self, positions=None, statuses=None, post_error=None):
        self._positions = positions if positions is not None else []
        self._statuses = list(statuses or ["filled"])
        self._post_error = post_error
        self.posted = []
        self.order_calls = []

    def list_positions(self):
        return self._positions

    def post_order(self, payload):
        if self._post_error:
            raise self._post_error
        self.posted.append(payload)
        return {"id": "close-1", "status": "accepted"}

    def get_order(self, order_id):
        self.order_calls.append(order_id)
        status = self._statuses.pop(0) if self._statuses else "filled"
        return {"id": order_id, "status": status}


class FakeData:
    def __init__(self, spot=SPOT, error=None):
        self._spot, self._error = spot, error
        self.bar_calls = []

    def get_stock_bars(self, symbol, days=30):
        self.bar_calls.append(symbol)
        if self._error:
            raise self._error
        if self._spot is None:
            return pd.DataFrame()
        return pd.DataFrame({"close": [self._spot] * 5})


@pytest.fixture
def guard(tmp_path, monkeypatch):
    """A guard whose kill switch resolves inside tmp_path, so a test can trip
    it without ever touching the real repo."""
    import shutil
    monkeypatch.delenv("KILL", raising=False)
    shutil.copy(RISK_YAML, tmp_path / "risk.yaml")
    return RiskGuard(load_risk_config(tmp_path / "risk.yaml"))


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.jsonl")


def _trade(intent=None, triggers=(), contracts=1, entry_spot=SPOT,
           snapshot_hash="snap-1", order_id="o-1"):
    return OpenTrade(order_id=order_id, intent=intent or _bull_put(),
                     triggers=tuple(triggers), contracts=contracts,
                     entry_spot=entry_spot, snapshot_hash=snapshot_hash)


def _run(cli, journal, guard, trades, data=None, **kw):
    data = data if data is not None else FakeData()
    return monitor_positions(cli, data, guard, journal,
                             {t.order_id: t for t in trades},
                             poll_seconds=0, sleep=lambda s: None, **kw)


def _closes(journal):
    return [e for e in journal.entries() if e["type"] == "close"]


# ── nothing fires: the position is left alone ───────────────

class TestQuietPosition:
    def test_a_healthy_position_is_not_closed(self, journal, guard):
        cli = FakeCLI(positions=_book(2.50, 1.50))
        events = _run(cli, journal, guard, [_trade()])
        assert cli.posted == [], "no trigger fired — nothing may be sent"
        assert [e.closed for e in events] == [False]
        assert [e.still_open for e in events] == [True]
        assert _closes(journal) == []

    def test_an_empty_book_of_trades_does_nothing(self, journal, guard):
        cli = FakeCLI(positions=_book(2.50, 1.50))
        assert _run(cli, journal, guard, []) == []
        assert cli.posted == []


# ── the deterministic exits, independent of the pre-mortem ──

class TestDeterministicExits:
    def test_50pct_of_the_credit_closes_the_position(self, journal, guard):
        """Credit 1.00; buying it back for 0.40 banks 60% of it."""
        cli = FakeCLI(positions=_book(0.60, 0.20))
        events = _run(cli, journal, guard, [_trade(triggers=())])
        assert events[0].closed is True
        assert events[0].trigger.kind == KIND_CREDIT_DECAY
        assert len(cli.posted) == 1

    def test_max_loss_closes_the_position(self, journal, guard):
        """Short leg at 5.20 against a long at 0.20 is the full $400 loss."""
        cli = FakeCLI(positions=_book(5.20, 0.20))
        events = _run(cli, journal, guard, [_trade(triggers=())])
        assert events[0].closed is True
        assert "max loss" in events[0].reason.lower()
        assert events[0].realized_pnl == pytest.approx(-400.0)

    def test_dte_3_forces_a_close_even_with_no_triggers(self, journal, guard):
        """Assignment avoidance is not optional (design spec A.5)."""
        near = date.today() + timedelta(days=2)
        intent = _bull_put(expiry=near)
        cli = FakeCLI(positions=_book(2.50, 1.50, expiry=near))
        events = _run(cli, journal, guard, [_trade(intent=intent, triggers=())])
        assert events[0].closed is True
        assert events[0].trigger.kind == KIND_DTE_BELOW

    def test_max_loss_outranks_the_profit_target(self, journal, guard):
        cli = FakeCLI(positions=_book(5.20, 0.20))
        events = _run(cli, journal, guard, [_trade()])
        assert "max loss" in events[0].reason.lower()


# ── the pre-mortem's own triggers ───────────────────────────

class TestPremortemTriggers:
    def test_an_underlying_level_below_entry_spot_fires_on_a_selloff(
            self, journal, guard):
        trigger = ExitTrigger(KIND_UNDERLYING_BEYOND, 492.0, "through the short strike")
        cli = FakeCLI(positions=_book(2.50, 1.50))
        events = _run(cli, journal, guard, [_trade(triggers=[trigger])],
                      data=FakeData(spot=490.0))
        assert events[0].closed is True
        assert events[0].trigger == trigger
        assert "through the short strike" in events[0].reason, (
            "the pre-mortem sentence must reach the exit record")

    def test_the_same_level_does_not_fire_above_it(self, journal, guard):
        trigger = ExitTrigger(KIND_UNDERLYING_BEYOND, 492.0, "r")
        cli = FakeCLI(positions=_book(2.50, 1.50))
        events = _run(cli, journal, guard, [_trade(triggers=[trigger])],
                      data=FakeData(spot=495.0))
        assert events[0].closed is False
        assert cli.posted == []

    def test_an_upside_level_fires_on_a_rally(self, journal, guard):
        """Direction comes from the level's side of the ENTRY spot, so one
        rule serves bullish, bearish and two-sided structures alike."""
        trigger = ExitTrigger(KIND_UNDERLYING_BEYOND, 510.0, "rally")
        cli = FakeCLI(positions=_book(2.50, 1.50))
        events = _run(cli, journal, guard, [_trade(triggers=[trigger])],
                      data=FakeData(spot=512.0))
        assert events[0].closed is True

    def test_an_iv_spike_fires_when_the_positions_own_iv_exceeds_it(
            self, journal, guard):
        trigger = ExitTrigger(KIND_IV_SPIKE, 0.01, "vol explodes")
        cli = FakeCLI(positions=_book(2.50, 1.50))
        events = _run(cli, journal, guard, [_trade(triggers=[trigger])])
        assert events[0].closed is True
        assert events[0].trigger.kind == KIND_IV_SPIKE

    def test_an_unsolvable_iv_does_not_count_as_a_spike(self, journal, guard):
        """A mark below intrinsic solves for no IV. "Unknown" must not read as
        "spiked" — that would close a position on a bad quote."""
        trigger = ExitTrigger(KIND_IV_SPIKE, 0.01, "vol explodes")
        cli = FakeCLI(positions=_book(2.50, 1.50))
        # Spot 450 puts both 495p and 490p deep in the money, so a 2.50 mark
        # is far below intrinsic and analytics.implied_vol returns None.
        events = _run(cli, journal, guard, [_trade(triggers=[trigger])],
                      data=FakeData(spot=450.0))
        assert events[0].closed is False
        assert cli.posted == []

    def test_an_unreachable_iv_spike_does_not_fire(self, journal, guard):
        trigger = ExitTrigger(KIND_IV_SPIKE, 4.0, "vol explodes")
        cli = FakeCLI(positions=_book(2.50, 1.50))
        events = _run(cli, journal, guard, [_trade(triggers=[trigger])])
        assert events[0].closed is False

    def test_a_tighter_model_dte_fires_before_the_forced_one(self, journal, guard):
        near = date.today() + timedelta(days=6)
        intent = _bull_put(expiry=near)
        trigger = ExitTrigger(KIND_DTE_BELOW, 10.0, "gamma risk into the last week")
        cli = FakeCLI(positions=_book(2.50, 1.50, expiry=near))
        events = _run(cli, journal, guard,
                      [_trade(intent=intent, triggers=[trigger])])
        assert events[0].closed is True
        assert events[0].trigger == trigger


# ── the closing order itself ────────────────────────────────

class TestClosingOrder:
    def test_the_closing_order_inverts_every_side(self, journal, guard):
        cli = FakeCLI(positions=_book(0.60, 0.20))
        _run(cli, journal, guard, [_trade()])
        payload = cli.posted[0]
        by_symbol = {leg["symbol"]: leg for leg in payload["legs"]}
        assert by_symbol[_occ(495, "p")]["side"] == "buy", "the short leg is bought back"
        assert by_symbol[_occ(495, "p")]["position_intent"] == "buy_to_close"
        assert by_symbol[_occ(490, "p")]["side"] == "sell"
        assert by_symbol[_occ(490, "p")]["position_intent"] == "sell_to_close"

    def test_closing_a_credit_spread_pays_a_debit(self, journal, guard):
        """Alpaca's sign rule: positive limit_price = net debit paid. Buying
        back a credit spread costs money, so the price must be positive."""
        cli = FakeCLI(positions=_book(0.60, 0.20))
        _run(cli, journal, guard, [_trade()])
        assert float(cli.posted[0]["limit_price"]) == pytest.approx(0.40)

    def test_the_closing_order_is_one_atomic_multi_leg_order(self, journal, guard):
        """There is no atomic multi-leg close in the SDK (design spec A.1):
        unwinding submits ONE new inverted mleg order, not a close per leg."""
        cli = FakeCLI(positions=_book(0.60, 0.20))
        _run(cli, journal, guard, [_trade()])
        assert len(cli.posted) == 1
        assert cli.posted[0]["order_class"] == "mleg"
        assert len(cli.posted[0]["legs"]) == 2

    def test_the_approved_contract_count_is_what_is_closed(self, journal, guard):
        rows = [_row(495, "p", -2, 0.60), _row(490, "p", 2, 0.20)]
        cli = FakeCLI(positions=rows)
        _run(cli, journal, guard, [_trade(contracts=2)])
        assert cli.posted[0]["qty"] == "2"


# ── the keystone: the close entry and its realized P&L ──────

class TestCloseJournalEntry:
    def test_a_close_entry_carries_everything_calibration_needs(
            self, journal, guard):
        cli = FakeCLI(positions=_book(0.60, 0.20))
        _run(cli, journal, guard, [_trade(snapshot_hash="deadbeef")])
        entries = _closes(journal)
        assert len(entries) == 1
        payload = entries[0]["payload"]
        assert payload["snapshot_hash"] == "deadbeef"
        assert payload["underlying"] == "SPY"
        assert payload["structure"] == "bull_put_spread"
        assert isinstance(payload["realized_pnl"], float)
        assert payload["realized_pnl"] == pytest.approx(60.0)
        assert payload["exit_reason"]
        assert payload["trigger"]["kind"] == KIND_CREDIT_DECAY

    def test_a_winning_close_reports_a_positive_pnl(self, journal, guard):
        cli = FakeCLI(positions=_book(0.60, 0.20))
        events = _run(cli, journal, guard, [_trade()])
        assert events[0].realized_pnl == pytest.approx(60.0)

    def test_a_losing_close_reports_a_negative_pnl(self, journal, guard):
        cli = FakeCLI(positions=_book(5.20, 0.20))
        events = _run(cli, journal, guard, [_trade()])
        assert events[0].realized_pnl == pytest.approx(-400.0)

    def test_the_pnl_scales_with_the_contract_count(self, journal, guard):
        rows = [_row(495, "p", -2, 0.60), _row(490, "p", 2, 0.20)]
        cli = FakeCLI(positions=rows)
        events = _run(cli, journal, guard, [_trade(contracts=2)])
        assert events[0].realized_pnl == pytest.approx(120.0)

    def test_the_actual_fill_price_beats_the_mark_estimate(self, journal, guard):
        """A close that filled at 0.30 rather than the 0.40 mark banked 70c
        of the 1.00 credit, and the journal must say 70, not 60."""
        class FilledCLI(FakeCLI):
            def get_order(self, order_id):
                return {"id": order_id, "status": "filled",
                        "filled_avg_price": "0.30"}
        cli = FilledCLI(positions=_book(0.60, 0.20))
        events = _run(cli, journal, guard, [_trade()])
        assert events[0].realized_pnl == pytest.approx(70.0)

    def test_no_close_entry_is_written_when_nothing_closed(self, journal, guard):
        cli = FakeCLI(positions=_book(2.50, 1.50))
        _run(cli, journal, guard, [_trade()])
        assert _closes(journal) == []

    def test_an_unfilled_closing_order_is_not_journalled_as_realized(
            self, journal, guard):
        """A working order has realized nothing. Writing a P&L for it would
        feed the calibration loop a number that never happened."""
        cli = FakeCLI(positions=_book(0.60, 0.20), statuses=["new"])
        events = _run(cli, journal, guard, [_trade()])
        assert _closes(journal) == []
        assert events[0].closed is False
        assert events[0].still_open is True
        assert any(e["type"] == "exit_pending" for e in journal.entries())


# ── failing closed ──────────────────────────────────────────

class TestFailsClosed:
    def test_the_kill_switch_stops_all_monitoring(self, journal, guard, tmp_path):
        (tmp_path / "KILL_SWITCH").touch()
        cli = FakeCLI(positions=_book(0.60, 0.20))
        events = _run(cli, journal, guard, [_trade()])
        assert cli.posted == [], "hard rule 6 halts ALL trading, closes included"
        assert events == []

    def test_a_position_no_longer_in_the_book_stops_being_monitored(
            self, journal, guard):
        cli = FakeCLI(positions=[])
        events = _run(cli, journal, guard, [_trade()])
        assert events[0].still_open is False
        assert cli.posted == []

    def test_a_partially_present_position_is_not_closed(self, journal, guard):
        """One leg assigned away: an inverted mleg over legs that are not all
        there would be rejected, so this reports an error rather than send."""
        cli = FakeCLI(positions=[_row(495, "p", -1, 0.60)])
        events = _run(cli, journal, guard, [_trade()])
        assert cli.posted == []
        assert events[0].error
        assert events[0].still_open is True

    def test_a_broker_rejection_does_not_journal_a_close(self, journal, guard):
        cli = FakeCLI(positions=_book(0.60, 0.20),
                      post_error=RuntimeError("insufficient buying power"))
        events = _run(cli, journal, guard, [_trade()])
        assert _closes(journal) == []
        assert "insufficient buying power" in events[0].error
        assert events[0].still_open is True

    def test_an_unreadable_spot_does_not_close_anything(self, journal, guard):
        cli = FakeCLI(positions=_book(0.60, 0.20))
        events = _run(cli, journal, guard, [_trade()],
                      data=FakeData(error=RuntimeError("data outage")))
        assert cli.posted == []
        assert events[0].error
        assert events[0].still_open is True

    def test_an_unreadable_position_list_yields_no_events(self, journal, guard):
        class BrokenCLI(FakeCLI):
            def list_positions(self):
                raise RuntimeError("CLI auth failed")
        events = _run(BrokenCLI(), journal, guard, [_trade()])
        assert events == []

    def test_a_journal_failure_never_breaks_the_close(self, journal, guard):
        """The journal is an audit obligation, not a precondition: an order
        already at the broker must not look to the caller like it never
        happened (executor_options._record's contract)."""
        class BrokenJournal:
            def append(self, *a, **kw):
                raise OSError("disk full")

            def entries(self):
                return []
        cli = FakeCLI(positions=_book(0.60, 0.20))
        events = _run(cli, journal=BrokenJournal(), guard=guard, trades=[_trade()])
        assert events[0].closed is True
        assert len(cli.posted) == 1

    def test_one_bad_trade_does_not_stop_the_others(self, journal, guard):
        good = _trade(order_id="good")
        bad = _trade(order_id="bad", intent=None)
        bad = OpenTrade(order_id="bad", intent="not an intent", triggers=(),
                        contracts=1, entry_spot=SPOT, snapshot_hash="s")
        cli = FakeCLI(positions=_book(0.60, 0.20))
        events = _run(cli, journal, guard, [bad, good])
        by_id = {e.order_id: e for e in events}
        assert by_id["bad"].error
        assert by_id["good"].closed is True


class TestExitEventShape:
    def test_the_event_is_frozen(self):
        event = ExitEvent(order_id="o", underlying="SPY", structure="s",
                          closed=False, still_open=True, reason="r")
        with pytest.raises(Exception):
            event.closed = True


# ── persisting the open book between cycles ─────────────────
#
# Each `make session` run is a fresh process, so a position opened by one
# cycle is invisible to the next unless the desk writes down what it opened.
# The broker's position rows are not enough on their own: they carry no
# structure, no entry credit, no snapshot_hash and no pre-mortem triggers —
# everything needed to close the position correctly and to attribute the
# outcome back to the analysts who chose it.

class TestOpenTradeStore:
    def test_a_trade_survives_a_round_trip(self, tmp_path):
        from exit_monitor import OpenTradeStore

        store = OpenTradeStore(tmp_path / "open_trades.json")
        trade = _trade(triggers=[ExitTrigger(KIND_UNDERLYING_BEYOND, 492.0, "why")],
                       contracts=2, entry_spot=501.5, snapshot_hash="abc123")
        store.save([trade])

        back = OpenTradeStore(tmp_path / "open_trades.json").load()
        assert len(back) == 1
        restored = back[0]
        assert restored.order_id == trade.order_id
        assert restored.contracts == 2
        assert restored.entry_spot == pytest.approx(501.5)
        assert restored.snapshot_hash == "abc123"
        assert restored.triggers == trade.triggers

    def test_the_restored_intent_can_still_build_a_closing_order(self, tmp_path):
        """The whole point of persisting it: a later process must be able to
        unwind the position it did not open."""
        from options_orders import closing_payload
        from exit_monitor import OpenTradeStore

        store = OpenTradeStore(tmp_path / "open_trades.json")
        store.save([_trade()])
        restored = store.load()[0]

        payload = closing_payload(restored.intent, 1, 0.40)
        assert [leg["symbol"] for leg in payload["legs"]] == [
            _occ(495, "p"), _occ(490, "p")]
        assert payload["legs"][0]["side"] == "buy"
        assert restored.intent.net_credit == pytest.approx(1.00)
        assert restored.intent.max_loss == pytest.approx(400.0)

    def test_a_missing_file_loads_as_an_empty_book(self, tmp_path):
        from exit_monitor import OpenTradeStore
        assert OpenTradeStore(tmp_path / "nope.json").load() == []

    def test_a_corrupt_file_loads_as_empty_and_does_not_raise(self, tmp_path, caplog):
        from exit_monitor import OpenTradeStore
        path = tmp_path / "open_trades.json"
        path.write_text("{not json at all")
        with caplog.at_level("ERROR"):
            assert OpenTradeStore(path).load() == []
        assert "open trade" in caplog.text.lower()

    def test_one_unreadable_record_does_not_lose_the_others(self, tmp_path):
        import json
        from exit_monitor import OpenTradeStore

        path = tmp_path / "open_trades.json"
        store = OpenTradeStore(path)
        store.save([_trade(order_id="good")])
        records = json.loads(path.read_text())
        records.insert(0, {"order_id": "broken"})
        path.write_text(json.dumps(records))

        loaded = OpenTradeStore(path).load()
        assert [t.order_id for t in loaded] == ["good"]

    def test_saving_an_empty_book_clears_the_file(self, tmp_path):
        from exit_monitor import OpenTradeStore
        store = OpenTradeStore(tmp_path / "open_trades.json")
        store.save([_trade()])
        store.save([])
        assert store.load() == []
