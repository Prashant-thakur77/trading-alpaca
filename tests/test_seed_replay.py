"""Pure-logic tests for the calibration seeding replay (`seed_replay.py`).

No network anywhere: every price in here is a literal, and the module under
test never fetches. The seeding SCRIPT talks to Alpaca; this module is the
part that has to be right regardless of what came back.

The contamination rule (windows must start on or after the model's May 2026
knowledge cutoff) is tested first and hardest, because a replay that reaches
back into the training data would make the project's own published
credibility claim false.
"""
from datetime import date, timedelta

import pytest

import seed_replay
from candidate_builder import OptionQuote, build_bull_put_spread, build_long_straddle
from journal import Journal


# ── the contamination guard ──────────────────────────────────

class TestKnowledgeCutoff:
    def test_cutoff_is_the_documented_june_first(self):
        # May 2026 cutoff -> the first admissible window start is 2026-06-01.
        assert seed_replay.KNOWLEDGE_CUTOFF == date(2026, 6, 1)

    def test_window_starting_on_the_cutoff_is_allowed(self):
        seed_replay.validate_window(date(2026, 6, 1), date(2026, 8, 15))

    def test_window_starting_after_the_cutoff_is_allowed(self):
        seed_replay.validate_window(date(2026, 7, 1), date(2026, 8, 15))

    def test_window_starting_one_day_before_the_cutoff_is_refused(self):
        with pytest.raises(seed_replay.ContaminationError) as e:
            seed_replay.validate_window(date(2026, 5, 31), date(2026, 8, 15))
        assert "2026-06-01" in str(e.value)

    def test_reversed_window_is_refused(self):
        with pytest.raises(seed_replay.ContaminationError):
            seed_replay.validate_window(date(2026, 8, 15), date(2026, 6, 1))

    def test_decision_dates_before_the_cutoff_are_dropped_not_used(self):
        days = [date(2026, 5, 28), date(2026, 5, 29), date(2026, 6, 1), date(2026, 6, 2)]
        picked = seed_replay.decision_dates(days, date(2026, 6, 1), date(2026, 6, 2),
                                            spacing=1, max_windows=10)
        assert picked == [date(2026, 6, 1), date(2026, 6, 2)]


class TestDecisionDates:
    def test_spacing_thins_the_calendar_deterministically(self):
        days = [date(2026, 6, 1) + timedelta(days=i) for i in range(10)]
        picked = seed_replay.decision_dates(days, date(2026, 6, 1), date(2026, 6, 10),
                                            spacing=3, max_windows=10)
        assert picked == [date(2026, 6, 1), date(2026, 6, 4), date(2026, 6, 7),
                          date(2026, 6, 10)]

    def test_max_windows_caps_the_list(self):
        days = [date(2026, 6, 1) + timedelta(days=i) for i in range(30)]
        picked = seed_replay.decision_dates(days, date(2026, 6, 1), date(2026, 6, 30),
                                            spacing=1, max_windows=4)
        assert len(picked) == 4

    def test_end_bound_is_inclusive_and_respected(self):
        days = [date(2026, 6, 1) + timedelta(days=i) for i in range(10)]
        picked = seed_replay.decision_dates(days, date(2026, 6, 1), date(2026, 6, 3),
                                            spacing=1, max_windows=10)
        assert picked[-1] == date(2026, 6, 3)


# ── OCC symbols ──────────────────────────────────────────────

class TestOccSymbol:
    def test_matches_the_verified_live_symbol(self):
        # SPY260821C00750000 returned real bars from Alpaca on 2026-08-29.
        assert seed_replay.occ_symbol("SPY", date(2026, 8, 21), "c", 750.0) \
            == "SPY260821C00750000"

    def test_put_and_fractional_strike(self):
        assert seed_replay.occ_symbol("SPY", date(2026, 7, 2), "p", 742.5) \
            == "SPY260702P00742500"

    def test_root_is_upper_cased(self):
        assert seed_replay.occ_symbol("spy", date(2026, 7, 2), "P", 100.0) \
            == "SPY260702P00100000"


class TestStrikeLadder:
    def test_ladder_is_on_the_width_grid_and_brackets_spot(self):
        ladder = seed_replay.strike_ladder(757.3, width=5.0, steps=2)
        assert ladder == [745.0, 750.0, 755.0, 760.0, 765.0]

    def test_every_neighbour_is_exactly_one_width_apart(self):
        ladder = seed_replay.strike_ladder(769.35, width=5.0, steps=6)
        assert all(round(b - a, 6) == 5.0 for a, b in zip(ladder, ladder[1:]))

    def test_non_positive_strikes_are_never_produced(self):
        assert all(s > 0 for s in seed_replay.strike_ladder(8.0, width=5.0, steps=6))


class TestPickExpiry:
    AVAILABLE = [date(2026, 7, 1), date(2026, 7, 17), date(2026, 7, 24),
                 date(2026, 7, 31), date(2026, 8, 21), date(2026, 9, 18)]

    def test_prefers_a_friday_inside_the_dte_band(self):
        picked = seed_replay.pick_expiry(date(2026, 6, 30), self.AVAILABLE)
        assert picked == date(2026, 7, 31)      # 31 DTE, nearest a Friday
        assert picked.weekday() == 4

    def test_returns_none_when_no_expiry_falls_in_the_band(self):
        assert seed_replay.pick_expiry(date(2026, 6, 30), [date(2026, 7, 2)]) is None

    def test_an_expiry_that_has_not_happened_yet_is_never_picked(self):
        # A window whose expiry is still in the future cannot be resolved
        # from real subsequent price action, so it must not be traded at all.
        assert seed_replay.pick_expiry(date(2026, 6, 30), self.AVAILABLE,
                                       max_expiry=date(2026, 7, 20)) is None

    def test_max_expiry_still_allows_an_expiry_on_the_boundary(self):
        picked = seed_replay.pick_expiry(date(2026, 6, 30), self.AVAILABLE,
                                         max_expiry=date(2026, 7, 24))
        assert picked == date(2026, 7, 24)

    def test_never_picks_an_expiry_outside_min_max_dte(self):
        picked = seed_replay.pick_expiry(date(2026, 6, 30), self.AVAILABLE,
                                         min_dte=21, max_dte=35)
        assert 21 <= (picked - date(2026, 6, 30)).days <= 35


# ── quotes built from real bars, never invented ──────────────

class TestQuoteFromBar:
    def test_close_becomes_both_sides_of_the_market(self):
        q = seed_replay.quote_from_bar("SPY", date(2026, 8, 21), "c", 750.0,
                                       close=15.32, volume=1591,
                                       as_of=date(2026, 7, 24))
        assert (q.bid, q.ask) == (15.32, 15.32)
        assert q.mid == 15.32
        assert q.spread_pct == 0.0

    def test_volume_is_the_documented_open_interest_proxy(self):
        q = seed_replay.quote_from_bar("SPY", date(2026, 8, 21), "c", 750.0,
                                       close=15.32, volume=1591.0,
                                       as_of=date(2026, 7, 24))
        assert q.open_interest == 1591

    def test_dte_is_measured_from_the_decision_date_not_today(self):
        q = seed_replay.quote_from_bar("SPY", date(2026, 8, 21), "c", 750.0,
                                       close=15.32, volume=1591,
                                       as_of=date(2026, 7, 24))
        assert q.dte == 28

    def test_a_zero_or_negative_close_yields_no_quote(self):
        assert seed_replay.quote_from_bar("SPY", date(2026, 8, 21), "c", 750.0,
                                          close=0.0, volume=500,
                                          as_of=date(2026, 7, 24)) is None
        assert seed_replay.quote_from_bar("SPY", date(2026, 8, 21), "c", 750.0,
                                          close=-1.0, volume=500,
                                          as_of=date(2026, 7, 24)) is None

    def test_symbol_round_trips_the_occ_construction(self):
        q = seed_replay.quote_from_bar("SPY", date(2026, 8, 21), "p", 742.5,
                                       close=3.0, volume=500,
                                       as_of=date(2026, 7, 24))
        assert q.symbol == "SPY260821P00742500"


class TestOptionQuoteAsOf:
    """`as_of` is what makes a historical quote's DTE meaningful at all."""

    def test_default_as_of_is_today_so_live_quotes_are_unchanged(self):
        q = OptionQuote("X", "SPY", 750.0, date.today() + timedelta(days=30),
                        "c", 1.0, 1.1, 500)
        assert q.dte == 30

    def test_as_of_overrides_today(self):
        q = OptionQuote("X", "SPY", 750.0, date(2026, 8, 21), "c", 1.0, 1.1, 500,
                        as_of=date(2026, 7, 24))
        assert q.dte == 28


# ── outcome resolution from real subsequent price action ─────

def _quote(strike, right, close, as_of=date(2026, 7, 24), expiry=date(2026, 8, 21)):
    return seed_replay.quote_from_bar("SPY", expiry, right, strike, close, 1000, as_of)


def _bull_put(short_close=3.00, long_close=1.00):
    return build_bull_put_spread(_quote(745.0, "p", short_close),
                                 _quote(740.0, "p", long_close), contracts=1)


class TestResolveOutcome:
    ENTRY = date(2026, 7, 24)
    EXPIRY = date(2026, 8, 21)
    FORCED = date(2026, 8, 18)          # expiry - 3 days

    def _underlying(self, through=EXPIRY, price=760.0):
        d, out = self.ENTRY, {}
        while d <= through:
            out[d] = price
            d += timedelta(days=1)
        return out

    def test_profit_target_fires_on_the_first_day_it_is_reached(self):
        intent = _bull_put()                       # $2.00 credit -> $200 total
        legs = {
            "SPY260821P00745000": {date(2026, 7, 28): 1.20, date(2026, 7, 27): 2.90},
            "SPY260821P00740000": {date(2026, 7, 28): 0.20, date(2026, 7, 27): 0.95},
        }
        out = seed_replay.resolve_outcome(intent, 1, legs, self._underlying(),
                                          self.ENTRY)
        assert out.method == seed_replay.METHOD_PROFIT_TARGET
        assert out.exit_date == date(2026, 7, 28)
        assert out.realized_pnl == pytest.approx(100.0)

    def test_a_day_short_of_the_target_does_not_fire(self):
        intent = _bull_put()
        legs = {                                    # closing cost 1.95 -> +$5
            "SPY260821P00745000": {date(2026, 7, 27): 2.90},
            "SPY260821P00740000": {date(2026, 7, 27): 0.95},
        }
        out = seed_replay.resolve_outcome(intent, 1, legs, self._underlying(),
                                          self.ENTRY)
        assert out.method != seed_replay.METHOD_PROFIT_TARGET

    def test_forced_dte_exit_uses_the_last_available_leg_bars(self):
        # Marks chosen so the position is UP but short of the 50%-of-credit
        # target ($90 of $200): the forced exit is what ends it, not profit.
        intent = _bull_put()
        legs = {
            "SPY260821P00745000": {self.FORCED: 1.30},
            "SPY260821P00740000": {self.FORCED: 0.20},
        }
        out = seed_replay.resolve_outcome(intent, 1, legs, self._underlying(),
                                          self.ENTRY)
        assert out.method == seed_replay.METHOD_FORCED_DTE
        assert out.exit_date == self.FORCED
        assert out.realized_pnl == pytest.approx(90.0)

    def test_no_leg_bars_falls_back_to_intrinsic_at_expiry(self):
        intent = _bull_put()
        out = seed_replay.resolve_outcome(intent, 1, {}, self._underlying(price=760.0),
                                          self.ENTRY)
        assert out.method == seed_replay.METHOD_INTRINSIC
        assert out.exit_date == self.EXPIRY
        # Both puts expire worthless above 745 -> keep the whole $200 credit.
        assert out.realized_pnl == pytest.approx(200.0)

    def test_intrinsic_fallback_can_produce_a_real_loser(self):
        intent = _bull_put()
        out = seed_replay.resolve_outcome(intent, 1, {}, self._underlying(price=730.0),
                                          self.ENTRY)
        assert out.method == seed_replay.METHOD_INTRINSIC
        # Both legs ITM: short 745 put worth 15, long 740 put worth 10,
        # closing credit -5.00 -> (2.00 - 5.00) * 100 = -$300 = full max loss.
        assert out.realized_pnl == pytest.approx(-300.0)

    def test_unresolvable_window_returns_none_never_a_guess(self):
        intent = _bull_put()
        assert seed_replay.resolve_outcome(intent, 1, {}, {}, self.ENTRY) is None

    def test_debit_structure_never_fires_the_credit_profit_target(self):
        straddle = build_long_straddle(_quote(760.0, "c", 12.0),
                                       _quote(760.0, "p", 11.0), contracts=1)
        legs = {
            "SPY260821C00760000": {self.FORCED: 40.0},
            "SPY260821P00760000": {self.FORCED: 0.05},
        }
        out = seed_replay.resolve_outcome(straddle, 1, legs, self._underlying(),
                                          self.ENTRY)
        assert out.method == seed_replay.METHOD_FORCED_DTE
        assert out.realized_pnl == pytest.approx((-23.0 + 40.05) * 100)

    def test_expiry_price_falls_back_to_the_last_close_before_expiry(self):
        intent = _bull_put()
        underlying = self._underlying(through=date(2026, 8, 20), price=760.0)
        out = seed_replay.resolve_outcome(intent, 1, {}, underlying, self.ENTRY)
        assert out.method == seed_replay.METHOD_INTRINSIC
        assert out.exit_date == date(2026, 8, 20)

    def test_a_partially_priced_day_is_skipped_not_half_used(self):
        intent = _bull_put()
        legs = {                     # only one of the two legs has a bar
            "SPY260821P00745000": {date(2026, 7, 28): 0.10},
        }
        out = seed_replay.resolve_outcome(intent, 1, legs, self._underlying(),
                                          self.ENTRY)
        assert out.method == seed_replay.METHOD_INTRINSIC


class TestIntrinsic:
    def test_call_intrinsic(self):
        assert seed_replay.intrinsic("c", 750.0, 760.0) == 10.0
        assert seed_replay.intrinsic("c", 750.0, 740.0) == 0.0

    def test_put_intrinsic(self):
        assert seed_replay.intrinsic("p", 750.0, 740.0) == 10.0
        assert seed_replay.intrinsic("p", 750.0, 760.0) == 0.0


# ── the replay journal marker ────────────────────────────────

class TestReplayJournal:
    def test_every_entry_is_stamped_as_replayed(self, tmp_path):
        j = seed_replay.ReplayJournal(Journal(tmp_path / "seed.jsonl"),
                                      as_of=date(2026, 7, 24), window_id=3)
        j.append("snapshot", {"underlying": "SPY"})
        entry = j.entries()[0]
        assert entry["payload"]["source"] == seed_replay.SEED_SOURCE
        assert entry["payload"]["replay_as_of"] == "2026-07-24"
        assert entry["payload"]["window_id"] == 3
        assert entry["payload"]["underlying"] == "SPY"

    def test_it_never_overwrites_a_real_payload_field(self, tmp_path):
        j = seed_replay.ReplayJournal(Journal(tmp_path / "seed.jsonl"),
                                      as_of=date(2026, 7, 24), window_id=1)
        j.append("close", {"realized_pnl": -42.0, "snapshot_hash": "abc"})
        payload = j.entries()[0]["payload"]
        assert payload["realized_pnl"] == -42.0
        assert payload["snapshot_hash"] == "abc"

    def test_entries_delegate_so_calibration_can_read_the_chain(self, tmp_path):
        base = Journal(tmp_path / "seed.jsonl")
        j = seed_replay.ReplayJournal(base, as_of=date(2026, 7, 24), window_id=1)
        j.append("analyst_view", {"role": "vol_analyst", "probability": 0.7,
                                  "snapshot_hash": "h1"})
        j.append("close", {"realized_pnl": 50.0, "snapshot_hash": "h1"})
        import calibration
        assert calibration.resolved_predictions(j, "vol_analyst") == [(0.7, True)]

    def test_the_as_of_stamp_advances_with_the_window(self, tmp_path):
        base = Journal(tmp_path / "seed.jsonl")
        seed_replay.ReplayJournal(base, as_of=date(2026, 7, 1), window_id=1) \
            .append("snapshot", {})
        seed_replay.ReplayJournal(base, as_of=date(2026, 7, 8), window_id=2) \
            .append("snapshot", {})
        stamps = [e["payload"]["replay_as_of"] for e in base.entries()]
        assert stamps == ["2026-07-01", "2026-07-08"]


class TestSkipReasons:
    """A window that cannot be built from real data is skipped, never filled."""

    def test_skip_is_recorded_with_its_reason(self):
        report = seed_replay.SeedReport(symbol="SPY", start=date(2026, 6, 1),
                                        end=date(2026, 8, 15))
        report.skip(date(2026, 6, 3), "no expiry in the 21-35 DTE band")
        assert report.attempted == 1
        assert report.used == 0
        assert report.skipped == [(date(2026, 6, 3), "no expiry in the 21-35 DTE band")]

    def test_abstention_rate_counts_abstained_over_attempted_committee_runs(self):
        report = seed_replay.SeedReport(symbol="SPY", start=date(2026, 6, 1),
                                        end=date(2026, 8, 15))
        report.record(date(2026, 6, 3), abstained=True, reason="every analyst abstained")
        report.record(date(2026, 6, 10), abstained=False, reason="")
        report.record(date(2026, 6, 17), abstained=False, reason="")
        assert report.attempted == 3
        assert report.used == 3
        assert report.abstained == 1
        assert report.abstention_rate == pytest.approx(1 / 3)

    def test_no_committee_runs_means_no_abstention_rate_not_a_zero(self):
        report = seed_replay.SeedReport(symbol="SPY", start=date(2026, 6, 1),
                                        end=date(2026, 8, 15))
        assert report.abstention_rate is None

    def test_markdown_states_the_cutoff_rationale_verbatim(self):
        report = seed_replay.SeedReport(symbol="SPY", start=date(2026, 6, 1),
                                        end=date(2026, 8, 15))
        text = report.render()
        assert "2026-06-01" in text
        assert "knowledge cutoff" in text.lower()
        assert "May 2026" in text


class TestSeededEntryCount:
    """Re-seeding an already-seeded journal double-counts every prediction.

    `calibration.resolved_predictions` appends one pair per `analyst_view`
    entry, and correlates it by `snapshot_hash` — which is deterministic, so
    a second run over the same window writes a SECOND analyst_view with the
    SAME hash and the same outcome. The Brier sample would silently double
    without a single new observation behind it.
    """

    def test_counts_only_entries_this_replay_wrote(self):
        entries = [
            {"type": "snapshot", "payload": {"source": seed_replay.SEED_SOURCE}},
            {"type": "analyst_view", "payload": {"source": seed_replay.SEED_SOURCE}},
            {"type": "fill", "payload": {}},                     # a real trade
            {"type": "close", "payload": {"realized_pnl": 1.0}},  # a real close
        ]
        assert seed_replay.seeded_entry_count(entries) == 2

    def test_an_empty_journal_is_zero(self):
        assert seed_replay.seeded_entry_count([]) == 0

    def test_a_journal_of_only_real_entries_is_zero(self):
        assert seed_replay.seeded_entry_count(
            [{"type": "fill", "payload": {"order_id": "x"}}]) == 0
