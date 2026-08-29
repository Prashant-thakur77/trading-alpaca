"""Tests for walkforward.py — real out-of-sample validation.

Every number this module reports must be computed from the bars it was given.
Nothing is hardcoded; a run with no trades reports no trades, not a default.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from walkforward import (
    Trade,
    generate_windows,
    summarize,
    run_walk_forward,
)


def _bars(n: int, start_price: float = 400.0, drift: float = 0.5) -> pd.DataFrame:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    closes = [start_price + drift * i for i in range(n)]
    return pd.DataFrame({
        "timestamp": [start + timedelta(days=i) for i in range(n)],
        "open": closes,
        "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes],
        "close": closes,
        "volume": [1_000_000.0] * n,
    })


class TestGenerateWindows:
    def test_splits_into_is_and_oos_segments(self):
        windows = generate_windows(total_bars=360, is_bars=90, oos_bars=30)
        assert len(windows) > 0
        w = windows[0]
        assert w.is_end - w.is_start == 90
        assert w.oos_end - w.oos_start == 30

    def test_oos_immediately_follows_is(self):
        """No gap and no overlap — OOS must be the unseen bars right after IS."""
        for w in generate_windows(total_bars=360, is_bars=90, oos_bars=30):
            assert w.oos_start == w.is_end

    def test_windows_roll_forward_by_oos_length(self):
        windows = generate_windows(total_bars=360, is_bars=90, oos_bars=30)
        assert windows[1].is_start - windows[0].is_start == 30

    def test_no_window_exceeds_available_bars(self):
        for w in generate_windows(total_bars=200, is_bars=90, oos_bars=30):
            assert w.oos_end <= 200

    def test_insufficient_history_yields_no_windows(self):
        """Fewer bars than one IS+OOS span must produce nothing, not a partial window."""
        assert generate_windows(total_bars=100, is_bars=90, oos_bars=30) == []

    def test_oos_segments_do_not_overlap(self):
        windows = generate_windows(total_bars=360, is_bars=90, oos_bars=30)
        spans = [(w.oos_start, w.oos_end) for w in windows]
        for (_, prev_end), (next_start, _) in zip(spans, spans[1:]):
            assert next_start >= prev_end


class TestSummarize:
    def test_no_trades_reports_zero_not_a_default(self):
        s = summarize([])
        assert s.trades == 0
        assert s.win_rate == 0.0
        assert s.expectancy_r == 0.0

    def test_win_rate_is_wins_over_trades(self):
        trades = [Trade("SPY", 1.0), Trade("SPY", 1.0), Trade("SPY", -1.0), Trade("SPY", -1.0)]
        assert summarize(trades).win_rate == pytest.approx(50.0)

    def test_expectancy_is_mean_r(self):
        trades = [Trade("SPY", 2.0), Trade("SPY", -1.0), Trade("SPY", -1.0), Trade("SPY", 2.0)]
        assert summarize(trades).expectancy_r == pytest.approx(0.5)

    def test_profit_factor_is_gross_win_over_gross_loss(self):
        trades = [Trade("SPY", 3.0), Trade("SPY", -1.0)]
        assert summarize(trades).profit_factor == pytest.approx(3.0)

    def test_all_winners_give_infinite_profit_factor(self):
        """No losses means no denominator — report inf rather than a fake number."""
        assert summarize([Trade("SPY", 1.0)]).profit_factor == float("inf")

    def test_max_drawdown_from_equity_curve(self):
        """+2 then -3 then +1: peak 2, trough -1 => drawdown of 3R."""
        trades = [Trade("SPY", 2.0), Trade("SPY", -3.0), Trade("SPY", 1.0)]
        assert summarize(trades).max_drawdown_r == pytest.approx(3.0)

    def test_drawdown_is_zero_when_only_rising(self):
        assert summarize([Trade("SPY", 1.0), Trade("SPY", 1.0)]).max_drawdown_r == 0.0


class TestRunWalkForward:
    def test_only_out_of_sample_bars_reach_the_strategy_test_phase(self):
        """The core guarantee: a strategy is never tested on bars it trained on."""
        seen_train, seen_test = [], []

        def fit(train_df):
            seen_train.append((train_df.index[0], train_df.index[-1]))
            return {}

        def test(test_df, params):
            seen_test.append((test_df.index[0], test_df.index[-1]))
            return []

        run_walk_forward(_bars(200), fit, test, is_bars=90, oos_bars=30)
        for (tr_lo, tr_hi), (te_lo, te_hi) in zip(seen_train, seen_test):
            assert te_lo > tr_hi

    def test_aggregates_trades_across_windows(self):
        def fit(train_df):
            return {}

        def test(test_df, params):
            return [Trade("SPY", 1.0)]

        result = run_walk_forward(_bars(200), fit, test, is_bars=90, oos_bars=30)
        assert result.oos.trades == len(result.windows)

    def test_reports_per_window_results(self):
        def fit(train_df):
            return {}

        def test(test_df, params):
            return [Trade("SPY", 1.0), Trade("SPY", -1.0)]

        result = run_walk_forward(_bars(200), fit, test, is_bars=90, oos_bars=30)
        assert len(result.window_results) == len(result.windows)
        assert all(w.trades == 2 for w in result.window_results)

    def test_insufficient_data_produces_empty_honest_result(self):
        result = run_walk_forward(_bars(50), lambda d: {}, lambda d, p: [Trade("X", 1.0)],
                                  is_bars=90, oos_bars=30)
        assert result.windows == []
        assert result.oos.trades == 0

    def test_strategy_exception_does_not_fabricate_results(self):
        """A broken strategy yields no trades for that window, never invented ones."""
        def fit(train_df):
            return {}

        def test(test_df, params):
            raise RuntimeError("strategy blew up")

        result = run_walk_forward(_bars(200), fit, test, is_bars=90, oos_bars=30)
        assert result.oos.trades == 0

    def test_result_is_json_serializable(self):
        """Results get written to disk for the report and the judge page."""
        import json
        result = run_walk_forward(_bars(200), lambda d: {}, lambda d, p: [Trade("SPY", 1.0)],
                                  is_bars=90, oos_bars=30)
        assert json.dumps(result.to_dict())
