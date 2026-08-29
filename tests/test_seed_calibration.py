"""Tests for scripts/seed_calibration.py — the seeding run's plumbing.

No network: every Alpaca client here is a stub built from literals. The real
script obviously talks to the API; what is tested here is everything that
decides WHAT to ask for, and what happens when the answer is missing —
because "missing data skips the window" is the property that keeps a
fabricated price out of a journal the judge is invited to verify.
"""
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import seed_calibration as seed_cal  # noqa: E402
import seed_replay  # noqa: E402


class _Bar(SimpleNamespace):
    pass


def _bar(day: date, close: float, volume: float = 1000.0):
    return _Bar(timestamp=SimpleNamespace(date=lambda d=day: d),
                close=close, volume=volume)


class FakeStockClient:
    def __init__(self, bars):
        self.bars = bars
        self.requests = []

    def get_stock_bars(self, request):
        self.requests.append(request)
        return SimpleNamespace(data={"SPY": self.bars})


class FakeOptionClient:
    def __init__(self, data):
        self.data = data
        self.requests = []

    def get_option_bars(self, request):
        self.requests.append(request)
        return SimpleNamespace(data=self.data)


class TestHistoricalBars:
    def test_stock_closes_keys_by_date(self):
        market = seed_cal.HistoricalBars(
            FakeStockClient([_bar(date(2026, 6, 1), 758.54),
                             _bar(date(2026, 6, 2), 759.57)]), FakeOptionClient({}))
        assert market.stock_closes("SPY", date(2026, 6, 1), date(2026, 6, 2)) == {
            date(2026, 6, 1): 758.54, date(2026, 6, 2): 759.57}

    def test_stock_request_never_reaches_past_the_data_ceiling(self):
        client = FakeStockClient([])
        seed_cal.HistoricalBars(client, FakeOptionClient({})).stock_closes(
            "SPY", date(2026, 6, 1), date(2099, 1, 1))
        # The free feed rejects "recent SIP data"; the request must be clamped.
        assert client.requests[0].end.date() <= seed_cal._data_ceiling()

    def test_option_bars_returns_close_and_volume_per_date(self):
        market = seed_cal.HistoricalBars(FakeStockClient([]), FakeOptionClient(
            {"spy260821c00750000": [_bar(date(2026, 7, 24), 15.32, 1591.0)]}))
        got = market.option_bars(["SPY260821C00750000"], date(2026, 7, 24),
                                 date(2026, 8, 21))
        assert got == {"SPY260821C00750000": {date(2026, 7, 24): (15.32, 1591.0)}}

    def test_no_symbols_makes_no_request(self):
        client = FakeOptionClient({})
        assert seed_cal.HistoricalBars(FakeStockClient([]), client).option_bars(
            [], date(2026, 7, 24), date(2026, 8, 21)) == {}
        assert client.requests == []

    def test_a_window_entirely_beyond_the_ceiling_makes_no_request(self):
        client = FakeOptionClient({})
        future = date.today() + timedelta(days=30)
        assert seed_cal.HistoricalBars(FakeStockClient([]), client).option_bars(
            ["SPY991231C00750000"], future, future + timedelta(days=10)) == {}
        assert client.requests == []


class TestChainConstruction:
    AS_OF, EXPIRY = date(2026, 7, 24), date(2026, 8, 21)

    def test_only_legs_with_a_real_bar_become_quotes(self):
        bars = {
            "SPY260821C00750000": {self.AS_OF: (15.32, 1591.0)},
            "SPY260821P00750000": {self.AS_OF: (16.19, 1404.0)},
            # every other ladder strike has no bar at all
        }
        chain = seed_cal.chain_for("SPY", self.AS_OF, self.EXPIRY, 750.0, bars)
        assert sorted(q.symbol for q in chain) == ["SPY260821C00750000",
                                                   "SPY260821P00750000"]

    def test_quotes_carry_the_decision_date_so_dte_is_historical(self):
        bars = {"SPY260821C00750000": {self.AS_OF: (15.32, 1591.0)}}
        (quote,) = seed_cal.chain_for("SPY", self.AS_OF, self.EXPIRY, 750.0, bars)
        assert quote.as_of == self.AS_OF
        assert quote.dte == 28

    def test_a_bar_on_a_different_day_is_not_used_for_this_decision(self):
        bars = {"SPY260821C00750000": {date(2026, 7, 25): (15.32, 1591.0)}}
        assert seed_cal.chain_for("SPY", self.AS_OF, self.EXPIRY, 750.0, bars) == []

    def test_leg_close_series_drops_the_volume(self):
        assert seed_cal.leg_close_series(
            {"A": {self.AS_OF: (1.5, 900.0)}}) == {"A": {self.AS_OF: 1.5}}


class TestExpiryCandidates:
    def test_only_weekdays_inside_the_replay_dte_band(self):
        got = seed_cal.expiry_candidates(date(2026, 6, 30))
        assert all(e.weekday() < 5 for e in got)
        assert all(seed_replay.MIN_DTE <= (e - date(2026, 6, 30)).days
                   <= seed_replay.MAX_DTE for e in got)

    def test_the_band_is_not_empty(self):
        assert seed_cal.expiry_candidates(date(2026, 6, 30))


class _Market:
    """A HistoricalBars stand-in with whatever option bars the test wants."""

    def __init__(self, option_bars=None, raises=None):
        self._bars = option_bars or {}
        self._raises = raises

    def option_bars(self, symbols, start, end):
        if self._raises:
            raise self._raises
        return self._bars


def _report():
    return seed_replay.SeedReport(symbol="SPY", start=date(2026, 6, 1),
                                  end=date(2026, 7, 31))


def _run(as_of, market, closes, report, dry_run=True):
    seed_cal.run_window(1, as_of, "SPY", market, closes, tmp_journal_path(),
                        cache=None, client=_never_called, report=report,
                        dry_run=dry_run)


def _never_called(*a, **kw):  # pragma: no cover - failing it is the assertion
    raise AssertionError("no LLM call may be made on a skipped or dry window")


def tmp_journal_path():
    return "/dev/null"


class TestRunWindowFailsClosed:
    """Every unusable window is SKIPPED with a reason, never filled in."""

    def test_no_underlying_bar_on_the_decision_date(self):
        report = _report()
        _run(date(2026, 6, 3), _Market(), {}, report)
        assert report.used == 0
        assert "no underlying bar" in report.skipped[0][1]

    def test_no_already_expired_expiry_in_the_band(self):
        report = _report()
        # A decision date so recent that every 21-35 DTE expiry is still in
        # the future: nothing can resolve it, so nothing is replayed.
        as_of = date.today()
        _run(as_of, _Market(), {as_of: 750.0}, report)
        assert report.used == 0
        assert "already expired" in report.skipped[0][1]

    def test_a_data_outage_skips_rather_than_raising(self):
        report = _report()
        as_of = date(2026, 6, 30)
        _run(as_of, _Market(raises=RuntimeError("upstream 500")),
             {as_of: 746.77}, report)
        assert report.used == 0
        assert "option bar fetch failed" in report.skipped[0][1]

    def test_too_few_priced_legs_skips_the_whole_window(self):
        report = _report()
        as_of = date(2026, 6, 30)
        expiry = seed_replay.pick_expiry(as_of, seed_cal.expiry_candidates(as_of),
                                         max_expiry=seed_cal._data_ceiling())
        bars = {seed_replay.occ_symbol("SPY", expiry, "c", 745.0):
                {as_of: (5.0, 900.0)}}
        _run(as_of, _Market(bars), {as_of: 746.77}, report)
        assert report.used == 0
        assert "had a real bar" in report.skipped[0][1]

    def test_a_full_chain_that_builds_no_candidate_is_skipped(self):
        report = _report()
        as_of = date(2026, 6, 30)
        expiry = seed_replay.pick_expiry(as_of, seed_cal.expiry_candidates(as_of),
                                         max_expiry=seed_cal._data_ceiling())
        # Enough legs to clear MIN_PRICED_LEGS, but all volume below
        # candidate_builder.MIN_OPEN_INTEREST, so every candidate is gated out.
        bars = {}
        for strike in seed_replay.strike_ladder(746.77, seed_cal.WIDTH,
                                                seed_cal.LADDER_STEPS):
            for right in ("c", "p"):
                bars[seed_replay.occ_symbol("SPY", expiry, right, strike)] = \
                    {as_of: (5.0, 3.0)}
        _run(as_of, _Market(bars), {as_of: 746.77}, report)
        assert report.used == 0
        assert "no candidate" in report.skipped[0][1]


class TestDryRun:
    def test_dry_run_records_the_window_without_spending_a_call(self):
        report = _report()
        as_of = date(2026, 6, 30)
        expiry = seed_replay.pick_expiry(as_of, seed_cal.expiry_candidates(as_of),
                                         max_expiry=seed_cal._data_ceiling())
        bars = {}
        for strike in seed_replay.strike_ladder(746.77, seed_cal.WIDTH,
                                                seed_cal.LADDER_STEPS):
            for right in ("c", "p"):
                bars[seed_replay.occ_symbol("SPY", expiry, right, strike)] = \
                    {as_of: (5.0, 900.0)}
        closes = {as_of - timedelta(days=i): 746.77 + i for i in range(40)}
        _run(as_of, _Market(bars), closes, report, dry_run=True)
        assert report.used == 1
        assert report.windows[0].choice_id == "(dry-run)"


class TestCountingClient:
    def test_counts_calls_and_sums_cost(self, capsys):
        responses = [SimpleNamespace(cost_usd=0.01), SimpleNamespace(cost_usd=0.02)]
        client = seed_cal.CountingClient(lambda prompt, model=None: responses.pop(0))
        client("p1", model="m")
        client("p2", model="m")
        assert client.calls == 2
        assert client.cost_usd == pytest.approx(0.03)

    def test_a_missing_cost_field_is_telemetry_not_an_error(self):
        client = seed_cal.CountingClient(lambda prompt, model=None: SimpleNamespace())
        client("p", model="m")
        assert client.calls == 1
        assert client.cost_usd == 0.0


class TestMainRefusesContamination:
    def test_a_pre_cutoff_start_is_refused_before_any_network_call(self, capsys):
        rc = seed_cal.main(["--start", "2026-05-15", "--end", "2026-06-30"])
        assert rc == 2
        assert "2026-06-01" in capsys.readouterr().err

    def test_the_default_start_is_the_cutoff_itself(self):
        assert seed_cal.parse_args([]).start == \
            seed_replay.KNOWLEDGE_CUTOFF.isoformat()

    def test_defaults_write_to_the_seed_journal_not_the_live_one(self):
        assert seed_cal.parse_args([]).journal.endswith("seed_journal.jsonl")


class TestCountingCache:
    def test_counts_only_hits_not_misses(self):
        inner = {"a": {"ok": True}}
        cache = seed_cal.CountingCache(SimpleNamespace(
            get=inner.get, put=lambda k, v: inner.__setitem__(k, v)))
        assert cache.get("a") == {"ok": True}
        assert cache.get("missing") is None
        assert cache.hits == 1

    def test_put_delegates_and_is_never_counted_as_a_hit(self):
        store = {}
        cache = seed_cal.CountingCache(SimpleNamespace(
            get=store.get, put=lambda k, v: store.__setitem__(k, v)))
        cache.put("k", {"ok": True})
        assert store == {"k": {"ok": True}}
        assert cache.hits == 0


class TestCountingClientConcurrency:
    def test_no_increment_is_lost_when_analysts_run_in_parallel(self):
        """`run_analysts` runs the two analysts concurrently on purpose; an
        unsynchronised counter silently under-reports the cost."""
        import threading
        client = seed_cal.CountingClient(
            lambda prompt, model=None: SimpleNamespace(cost_usd=0.001))
        threads = [threading.Thread(target=client, args=("p",), kwargs={"model": "m"})
                   for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert client.calls == 50
        assert client.cost_usd == pytest.approx(0.05)


class TestRefusesToDoubleCount:
    def test_an_already_seeded_journal_is_refused_by_default(self, tmp_path, capsys):
        from journal import Journal
        path = tmp_path / "seed.jsonl"
        Journal(path).append("analyst_view", {"source": seed_replay.SEED_SOURCE,
                                              "role": "vol_analyst"})
        rc = seed_cal.main(["--journal", str(path), "--start", "2026-06-01",
                            "--end", "2026-06-30"])
        assert rc == 3
        assert "count one observation as two" in capsys.readouterr().err

    def test_a_journal_of_only_real_entries_is_not_refused(self, tmp_path):
        """A live journal has no seed entries, so nothing here should block a
        run that was legitimately pointed at a fresh file."""
        from journal import Journal
        path = tmp_path / "live.jsonl"
        Journal(path).append("fill", {"order_id": "abc"})
        assert seed_replay.seeded_entry_count(Journal(path).entries()) == 0


class TestRequestEnd:
    """The free feed rejects any request whose end reaches the last quarter
    hour — which an end-of-day timestamp on "yesterday" does at 00:05 UTC."""

    def test_a_past_day_keeps_its_end_of_day_timestamp(self):
        got = seed_cal._request_end(date(2026, 6, 1))
        assert got.date() == date(2026, 6, 1)
        assert got.hour == 23

    def test_a_recent_end_is_pulled_back_out_of_the_embargo(self):
        from datetime import datetime as dt, timezone as tz
        got = seed_cal._request_end(date.today())
        assert got <= dt.now(tz.utc) - timedelta(minutes=seed_cal.DATA_LAG_MINUTES - 1)
