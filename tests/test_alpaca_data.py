"""Tests for alpaca_data.py — bars + option chain with a 15-minute cache.

Uses fake clients rather than live Alpaca calls: the merge logic and the
fail-closed behaviour are what matter here, and they must be testable with
no credentials and markets closed.
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from alpaca_data import AlpacaData, CACHE_TTL_SECONDS

EXPIRY = date.today() + timedelta(days=30)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


def _snapshot(symbol, bid, ask, iv=0.20):
    return SimpleNamespace(
        symbol=symbol,
        latest_quote=SimpleNamespace(bid_price=bid, ask_price=ask),
        implied_volatility=iv,
        greeks=SimpleNamespace(delta=-0.30, gamma=0.01, theta=-0.05, vega=0.10),
    )


def _contract(symbol, strike, right, oi):
    return SimpleNamespace(
        symbol=symbol, strike_price=strike, expiration_date=EXPIRY,
        type=SimpleNamespace(value=right), open_interest=oi,
        underlying_symbol="SPY", tradable=True,
    )


class FakeStockClient:
    def __init__(self, bars=None, raises=False):
        self.calls = 0
        self.raises = raises
        self._bars = bars if bars is not None else [
            SimpleNamespace(timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(days=i),
                            open=450.0, high=452.0, low=448.0, close=450.0 + i, volume=1_000_000)
            for i in range(30)
        ]

    def get_stock_bars(self, request):
        self.calls += 1
        if self.raises:
            raise RuntimeError("alpaca down")
        return SimpleNamespace(data={request.symbol_or_symbols: self._bars})


class FakeOptionClient:
    def __init__(self, snapshots=None, raises=False):
        self.calls = 0
        self.raises = raises
        self._snapshots = snapshots if snapshots is not None else {
            "SPY_445P": _snapshot("SPY_445P", 3.00, 3.10),
            "SPY_440P": _snapshot("SPY_440P", 2.00, 2.10),
        }

    def get_option_chain(self, request):
        self.calls += 1
        if self.raises:
            raise RuntimeError("alpaca down")
        return self._snapshots


class FakeTradingClient:
    def __init__(self, contracts=None, raises=False):
        self.raises = raises
        self._contracts = contracts if contracts is not None else [
            _contract("SPY_445P", 445.0, "put", 800),
            _contract("SPY_440P", 440.0, "put", 600),
        ]

    def get_option_contracts(self, request):
        if self.raises:
            raise RuntimeError("alpaca down")
        return SimpleNamespace(option_contracts=self._contracts)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def data(clock):
    return AlpacaData(
        stock_client=FakeStockClient(),
        option_client=FakeOptionClient(),
        trading_client=FakeTradingClient(),
        clock=clock,
    )


class TestStockBars:
    def test_returns_ohlcv_dataframe(self, data):
        df = data.get_stock_bars("SPY", days=30)
        assert isinstance(df, pd.DataFrame)
        for col in ("open", "high", "low", "close", "volume"):
            assert col in df.columns

    def test_bars_are_usable_by_realized_volatility(self, data):
        from analytics import realized_volatility
        assert realized_volatility(data.get_stock_bars("SPY", days=30)) > 0

    def test_api_error_returns_empty_frame(self, clock):
        """Fail closed: no data beats bad data. An empty frame yields no signal."""
        d = AlpacaData(FakeStockClient(raises=True), FakeOptionClient(),
                       FakeTradingClient(), clock=clock)
        assert d.get_stock_bars("SPY", days=30).empty


class TestCaching:
    def test_second_call_within_ttl_uses_cache(self, data):
        data.get_stock_bars("SPY", days=30)
        data.get_stock_bars("SPY", days=30)
        assert data.stock_client.calls == 1

    def test_call_after_ttl_refetches(self, data, clock):
        data.get_stock_bars("SPY", days=30)
        clock.advance(CACHE_TTL_SECONDS + 1)
        data.get_stock_bars("SPY", days=30)
        assert data.stock_client.calls == 2

    def test_different_symbols_cache_separately(self, data):
        data.get_stock_bars("SPY", days=30)
        data.get_stock_bars("QQQ", days=30)
        assert data.stock_client.calls == 2

    def test_chain_is_cached_too(self, data):
        data.get_option_chain("SPY")
        data.get_option_chain("SPY")
        assert data.option_client.calls == 1

    def test_empty_results_are_not_cached(self, clock):
        """A failed fetch must not poison the cache for 15 minutes."""
        stock = FakeStockClient(raises=True)
        d = AlpacaData(stock, FakeOptionClient(), FakeTradingClient(), clock=clock)
        d.get_stock_bars("SPY", days=30)
        d.get_stock_bars("SPY", days=30)
        assert stock.calls == 2


class TestOptionChain:
    def test_returns_option_quotes(self, data):
        from candidate_builder import OptionQuote
        quotes = data.get_option_chain("SPY")
        assert quotes and all(isinstance(q, OptionQuote) for q in quotes)

    def test_merges_open_interest_from_contracts(self, data):
        """OI is absent from chain snapshots; it must come from get_option_contracts."""
        by_symbol = {q.symbol: q for q in data.get_option_chain("SPY")}
        assert by_symbol["SPY_445P"].open_interest == 800
        assert by_symbol["SPY_440P"].open_interest == 600

    def test_quote_carries_bid_ask_strike_and_right(self, data):
        q = {q.symbol: q for q in data.get_option_chain("SPY")}["SPY_445P"]
        assert (q.bid, q.ask, q.strike, q.right) == (3.00, 3.10, 445.0, "p")

    def test_contract_without_open_interest_defaults_to_zero(self, clock):
        """Unknown OI must fail the liquidity gate, not silently pass it."""
        d = AlpacaData(
            FakeStockClient(),
            FakeOptionClient({"SPY_445P": _snapshot("SPY_445P", 3.00, 3.10)}),
            FakeTradingClient([_contract("SPY_445P", 445.0, "put", None)]),
            clock=clock,
        )
        from candidate_builder import passes_liquidity
        q = d.get_option_chain("SPY")[0]
        assert q.open_interest == 0
        assert passes_liquidity(q) is False

    def test_snapshot_without_matching_contract_is_dropped(self, clock):
        """No contract row means no verifiable OI — drop rather than guess."""
        d = AlpacaData(
            FakeStockClient(),
            FakeOptionClient({"GHOST": _snapshot("GHOST", 1.0, 1.1)}),
            FakeTradingClient([]),
            clock=clock,
        )
        assert d.get_option_chain("SPY") == []

    def test_quote_missing_bid_ask_is_dropped(self, clock):
        d = AlpacaData(
            FakeStockClient(),
            FakeOptionClient({"SPY_445P": SimpleNamespace(
                symbol="SPY_445P", latest_quote=None,
                implied_volatility=0.2, greeks=None)}),
            FakeTradingClient([_contract("SPY_445P", 445.0, "put", 800)]),
            clock=clock,
        )
        assert d.get_option_chain("SPY") == []

    def test_api_error_returns_empty_list(self, clock):
        d = AlpacaData(FakeStockClient(), FakeOptionClient(raises=True),
                       FakeTradingClient(), clock=clock)
        assert d.get_option_chain("SPY") == []

    def test_chain_quotes_feed_the_candidate_builder(self, data):
        """End-to-end: a fetched chain builds a real defined-risk spread."""
        from candidate_builder import build_bull_put_spread
        by_symbol = {q.symbol: q for q in data.get_option_chain("SPY")}
        intent = build_bull_put_spread(by_symbol["SPY_445P"], by_symbol["SPY_440P"])
        assert intent is not None
        assert intent.max_loss == pytest.approx(400.0)
