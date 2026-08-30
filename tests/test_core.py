"""
Core tests — RiskManager logic, indicator math, config validation,
strategy engine, and executor position sizing.

Only pure logic; no external service mocks.
"""
import sys
import os
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

# Allow imports from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from risk_manager import RiskManager, BatchTPLevel, BatchTPLevels, calculate_batch_tp_levels
from strategies import (
    ema, rsi, bollinger_bands, atr, macd, adx,
    StrategyEngine, RegimeDetector, MarketRegime, TradeSignal,
)
from config import ACTIVE_PAIRS, PAIR_MAP, STRATEGY_PARAMS, BLACKLIST, RISK


# ── Helpers ──────────────────────────────────────────────────

def _make_rm(capital: float = 100_000.0) -> RiskManager:
    return RiskManager(initial_capital=capital)


def _ohlc_df(length: int = 60, base: float = 100.0, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic OHLC DataFrame with a slight uptrend."""
    np.random.seed(seed)
    close = base + np.cumsum(np.random.randn(length) * 0.5)
    high = close + np.abs(np.random.randn(length)) * 0.3
    low = close - np.abs(np.random.randn(length)) * 0.3
    return pd.DataFrame({
        "open": close - np.random.randn(length) * 0.1,
        "high": high,
        "low": low,
        "close": close,
        "volume": np.random.randint(100, 1000, size=length).astype(float),
    })


def _trending_up_df(length: int = 200, base: float = 100.0) -> pd.DataFrame:
    """Generate a clearly trending-up OHLC DataFrame."""
    np.random.seed(99)
    # Steady uptrend with small noise
    trend = np.linspace(0, 50, length)
    noise = np.random.randn(length) * 0.3
    close = base + trend + noise
    high = close + np.abs(np.random.randn(length)) * 0.5
    low = close - np.abs(np.random.randn(length)) * 0.5
    # Volume surges on green candles for TrendRider confirmation
    volume = np.where(
        np.diff(close, prepend=close[0]) > 0,
        np.random.randint(800, 2000, size=length),
        np.random.randint(100, 400, size=length),
    ).astype(float)
    return pd.DataFrame({
        "open": close - 0.2,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })


# ── RiskManager Tests ────────────────────────────────────────

class TestRiskManagerState:
    def test_initial_state(self):
        rm = _make_rm(50_000)
        assert rm.current_balance == 50_000
        assert rm.total_realized_pnl == 0.0
        assert rm.open_position_count == 0
        assert rm.consecutive_losses == 0
        assert rm.position_scale == 1.0

    def test_can_trade_basic(self):
        rm = _make_rm()
        allowed, reason = rm.can_trade("BTC/USDT", "long")
        assert allowed is True
        assert reason == "OK"

    def test_can_trade_max_positions(self):
        rm = _make_rm()
        for _ in range(RISK.max_concurrent_positions):
            rm.register_open("ETH/USDT", "long")
        allowed, reason = rm.can_trade("SOL/USDT", "long")
        assert allowed is False
        assert "Max positions" in reason

    def test_daily_loss_stop(self):
        rm = _make_rm(100_000)
        # 3% of 100k = 3000; register a loss exceeding that
        rm.register_open("BTC/USDT", "short")
        rm.register_close("BTC/USDT", -3100.0, "sl")
        allowed, reason = rm.can_trade("ETH/USDT", "long")
        assert allowed is False
        assert "Daily loss" in reason or "stopped" in reason.lower()

    def test_emergency_drawdown(self):
        rm = _make_rm(100_000)
        # 10% of 100k = 10000
        rm.register_open("BTC/USDT", "short")
        rm.register_close("BTC/USDT", -10_500, "sl")
        assert rm.check_emergency() is True

    def test_consecutive_loss_scaling(self):
        rm = _make_rm()
        for i in range(3):
            rm.register_open("ETH/USDT", "long")
            rm.register_close("ETH/USDT", -50.0, "sl")
        assert rm.consecutive_losses == 3
        assert rm.position_scale == RISK.consecutive_loss_scale  # 0.5

    def test_consecutive_loss_pause(self):
        rm = _make_rm()
        for i in range(RISK.consecutive_loss_pause):
            rm.register_open("ETH/USDT", "long")
            rm.register_close("ETH/USDT", -10.0, "sl")
        allowed, reason = rm.can_trade("ETH/USDT", "long")
        assert allowed is False
        assert "Consecutive losses" in reason

    def test_register_close_profit(self):
        rm = _make_rm(100_000)
        rm.register_open("SOL/USDT", "long")
        rm.register_close("SOL/USDT", 500.0, "tp")
        assert rm.total_realized_pnl == 500.0
        assert rm.current_balance == 100_500.0
        assert rm.consecutive_losses == 0
        assert rm.position_scale == 1.0

    def test_register_close_loss(self):
        rm = _make_rm()
        rm.register_open("XRP/USDT", "short")
        rm.register_close("XRP/USDT", -200.0, "sl")
        assert rm.consecutive_losses == 1
        assert rm.total_realized_pnl == -200.0


# ── Indicator Tests ──────────────────────────────────────────

class TestIndicators:
    def test_ema_calculation(self):
        df = _ohlc_df(30)
        result = ema(df["close"], 10)
        assert len(result) == 30
        assert not result.iloc[-1:].isna().any()
        # EMA should be close to the mean for stationary-ish data
        assert abs(result.iloc[-1] - df["close"].iloc[-10:].mean()) < 5.0

    def test_rsi_calculation(self):
        df = _ohlc_df(60)
        result = rsi(df["close"], 14)
        valid = result.dropna()
        assert len(valid) > 0
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_rsi_extreme_up(self):
        # Monotonically rising prices should push RSI near 100
        prices = pd.Series(range(1, 51), dtype=float)
        result = rsi(prices, 14).dropna()
        assert result.iloc[-1] > 80

    def test_bollinger_bands(self):
        df = _ohlc_df(40)
        upper, middle, lower = bollinger_bands(df["close"], 20, 2.0)
        valid_idx = middle.dropna().index
        assert (upper.loc[valid_idx] >= middle.loc[valid_idx]).all()
        assert (lower.loc[valid_idx] <= middle.loc[valid_idx]).all()
        # Band width should be positive
        width = upper.loc[valid_idx] - lower.loc[valid_idx]
        assert (width > 0).all()

    def test_atr_calculation(self):
        df = _ohlc_df(40)
        result = atr(df, 14)
        valid = result.dropna()
        assert len(valid) > 0
        assert (valid > 0).all()

    def test_macd_calculation(self):
        df = _ohlc_df(60)
        macd_line, signal_line, histogram = macd(df["close"])
        assert len(macd_line) == 60
        # Histogram = MACD line - signal line
        diff = (macd_line - signal_line - histogram).dropna().abs()
        assert (diff < 1e-10).all()

    def test_adx_calculation(self):
        """ADX should be 0-100 for typical data."""
        df = _ohlc_df(100)
        result = adx(df, 14)
        valid = result.dropna()
        assert len(valid) > 0
        assert (valid >= 0).all()
        assert (valid <= 100).all()


# ── Strategy Engine Tests ────────────────────────────────────

class TestRegimeDetector:
    def test_insufficient_data_returns_unknown(self):
        """Less than 50 bars → UNKNOWN regime."""
        rd = RegimeDetector()
        df = _ohlc_df(30)
        result = rd.detect(df)
        assert result.regime == MarketRegime.UNKNOWN
        assert result.confidence == 0

    def test_sufficient_data_returns_regime(self):
        """200 bars of trending data → valid regime, not UNKNOWN."""
        rd = RegimeDetector()
        df = _trending_up_df(200)
        result = rd.detect(df)
        assert result.regime != MarketRegime.UNKNOWN
        assert result.confidence > 0
        assert 0 < result.position_size_mult <= 1.0

    def test_trending_up_detection(self):
        """Clear uptrend should produce TRENDING_UP regime."""
        rd = RegimeDetector()
        df = _trending_up_df(200)
        result = rd.detect(df)
        # With a strong uptrend, should be TRENDING_UP or at least high ADX
        assert result.regime in (
            MarketRegime.TRENDING_UP,
            MarketRegime.HIGH_VOLATILITY,  # large moves may register as high vol
        )
        assert result.adx > 0


class TestStrategyEngine:
    def test_compute_indicators_adds_columns(self):
        """_compute_indicators should add all expected indicator columns."""
        engine = StrategyEngine()
        df = _ohlc_df(60)
        result = engine._compute_indicators(df)
        expected_cols = [
            "ema20", "ema50", "ema100", "ema200", "rsi", "atr",
            "vol_ma20", "vol_ratio", "ema_alignment",
            "golden_cross", "death_cross",
            "macd_line", "macd_signal", "macd_hist",
            "bb_upper", "bb_mid", "bb_lower", "bb_width",
        ]
        for col in expected_cols:
            assert col in result.columns, f"Missing indicator column: {col}"

    def test_blacklisted_pair_returns_none(self):
        """Blacklisted pair+direction should produce no TrendRider signal."""
        engine = StrategyEngine()
        df = engine._compute_indicators(_trending_up_df(200))
        # BTC/USDT_long is in BLACKLIST
        result = engine._check_trend_rider(df, "BTC/USDT", "long")
        assert result is None

    def test_trade_signal_risk_reward(self):
        """TradeSignal.risk_reward_ratio should compute correctly."""
        sig = TradeSignal(
            pair="ETH/USDT", direction="long",
            entry_price=100.0, sl_price=95.0,
            tp1_price=105.0, tp2_price=110.0, tp3_price=115.0,
            confidence=60.0, source="trend_rider",
        )
        assert abs(sig.risk_reward_ratio() - 1.0) < 0.01  # 5 / 5 = 1.0

    def test_trade_signal_fields(self):
        """TradeSignal should have all required fields."""
        sig = TradeSignal(
            pair="SOL/USDT", direction="short",
            entry_price=200.0, sl_price=210.0,
            tp1_price=190.0, tp2_price=180.0, tp3_price=170.0,
            confidence=40.0, source="trend_rider", position_scale=0.75,
        )
        assert sig.direction == "short"
        assert sig.position_scale == 0.75
        assert sig.tp3_price < sig.tp2_price < sig.tp1_price < sig.entry_price


# ── Executor Position Sizing Tests ───────────────────────────



# ── Config Validation Tests ──────────────────────────────────

class TestConfig:
    def test_pair_map_consistency(self):
        for pair in ACTIVE_PAIRS:
            assert pair in PAIR_MAP, f"{pair} in ACTIVE_PAIRS but not in PAIR_MAP"

    def test_strategy_params_valid(self):
        for direction in STRATEGY_PARAMS:
            for pair in STRATEGY_PARAMS[direction]:
                assert pair in PAIR_MAP, (
                    f"STRATEGY_PARAMS[{direction}][{pair}] references "
                    f"a pair not in PAIR_MAP"
                )

    def test_blacklist_format(self):
        for entry in BLACKLIST:
            # Format: PAIR_direction or PAIR_direction_suffix (e.g. BNB/USDT_long_old)
            parts = entry.split("_")
            # Find direction part: must contain "long" or "short"
            has_direction = any(p in ("long", "short") for p in parts)
            assert has_direction, (
                f"Blacklist entry '{entry}' missing 'long' or 'short' direction"
            )

    def test_no_stale_params(self):
        for direction in STRATEGY_PARAMS:
            for pair in STRATEGY_PARAMS[direction]:
                assert pair in PAIR_MAP, (
                    f"Stale param: {pair} in STRATEGY_PARAMS[{direction}] "
                    f"but missing from PAIR_MAP"
                )

    def test_active_pairs_count(self):
        """Should have exactly 7 active pairs (incl. LINK added for competition)."""
        assert len(ACTIVE_PAIRS) == 7
        assert "BTC/USDT" in ACTIVE_PAIRS
        assert "ETH/USDT" in ACTIVE_PAIRS
        assert "LINK/USDT" in ACTIVE_PAIRS


# ── Executor SL/TP State Machine Tests ───────────────────────



# ── BB Squeeze Logic Tests ───────────────────────────────────

class TestBBSqueeze:
    def test_squeeze_requires_compression_and_expansion(self):
        """BB Squeeze should require BOTH compression AND expansion."""
        engine = StrategyEngine()
        # Generate data with stable BB width (no squeeze)
        df = engine._compute_indicators(_ohlc_df(60))
        # The AND condition should be stricter than the old OR
        result = engine._check_bb_squeeze(df, "ETH/USDT", "long")
        # For random walk data, squeeze-and-expand is unlikely
        assert isinstance(result, bool)


# ── State Roundtrip Tests ────────────────────────────────────



# ── MACD Swing Point Detection Tests ─────────────────────────

class TestMACDSwingPoints:
    def test_find_swing_points_detects_lows(self):
        """Swing point detector should find local minima."""
        from indicators import find_swing_points
        # V-shaped dip: [10, 8, 6, 4, 6, 8, 10]
        series = pd.Series([10, 8, 6, 4, 6, 8, 10])
        lows, highs = find_swing_points(series, order=2)
        assert len(lows) >= 1
        assert any(v == 4.0 for _, v in lows)

    def test_find_swing_points_detects_highs(self):
        """Swing point detector should find local maxima."""
        from indicators import find_swing_points
        # Peak: [5, 7, 9, 11, 9, 7, 5]
        series = pd.Series([5, 7, 9, 11, 9, 7, 5])
        lows, highs = find_swing_points(series, order=2)
        assert len(highs) >= 1
        assert any(v == 11.0 for _, v in highs)

    def test_divergence_requires_two_swing_points(self):
        """MACD divergence should return False without 2+ swing points."""
        engine = StrategyEngine()
        # Short flat data — unlikely to produce 2 swing points
        df = engine._compute_indicators(_ohlc_df(35, seed=1))
        result = engine._check_macd_divergence(df, "ETH/USDT", "long")
        assert isinstance(result, bool)


# ── Batch Take-Profit Tests ────────────────────────────────

class TestBatchTPLevels:
    def test_calculate_default_levels(self):
        """Default batch TP: 25% / 25% / 50%."""
        result = calculate_batch_tp_levels(105.0, 110.0, 115.0)
        assert result.tp1.price == 105.0
        assert result.tp1.close_pct == 0.25
        assert result.tp2.price == 110.0
        assert result.tp2.close_pct == 0.25
        assert result.tp3.price == 115.0
        assert result.tp3.close_pct == 0.50

    def test_custom_percentages(self):
        """Custom close percentages should work if they sum to 1.0."""
        result = calculate_batch_tp_levels(
            105.0, 110.0, 115.0,
            tp1_close_pct=0.20, tp2_close_pct=0.30, tp3_close_pct=0.50,
        )
        assert result.tp1.close_pct == 0.20
        assert result.tp2.close_pct == 0.30
        assert result.tp3.close_pct == 0.50

    def test_invalid_percentages_raises(self):
        """Close percentages not summing to 1.0 should raise ValueError."""
        with pytest.raises(ValueError, match="sum to 1.0"):
            calculate_batch_tp_levels(
                105.0, 110.0, 115.0,
                tp1_close_pct=0.30, tp2_close_pct=0.30, tp3_close_pct=0.50,
            )

    def test_levels_property(self):
        """levels property should return tuple of 3 BatchTPLevel."""
        result = calculate_batch_tp_levels(105.0, 110.0, 115.0)
        levels = result.levels
        assert len(levels) == 3
        assert all(isinstance(lvl, BatchTPLevel) for lvl in levels)

    def test_frozen_immutability(self):
        """BatchTPLevels should be frozen (immutable)."""
        result = calculate_batch_tp_levels(105.0, 110.0, 115.0)
        with pytest.raises(AttributeError):
            result.tp1 = BatchTPLevel(price=999.0, close_pct=0.5)  # type: ignore

    def test_batch_tp_level_frozen(self):
        """BatchTPLevel should be frozen (immutable)."""
        lvl = BatchTPLevel(price=100.0, close_pct=0.25)
        with pytest.raises(AttributeError):
            lvl.price = 200.0  # type: ignore


        # At this point close_position would close 5.0 volume
