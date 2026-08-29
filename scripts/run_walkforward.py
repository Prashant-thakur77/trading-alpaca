#!/usr/bin/env python3
"""
Run walk-forward out-of-sample validation on the traded universe.

Pulls daily bars from Alpaca, runs rolling IS/OOS windows, and writes the
computed results to validation/walkforward.json for `make validate` and the
judge page to read.

    python3 scripts/run_walkforward.py
    python3 scripts/run_walkforward.py --symbols SPY QQQ --days 720

Requires ALPACA_API_KEY / ALPACA_SECRET_KEY (see .env.example). Every figure it
writes is computed from the bars it fetched — nothing is defaulted or assumed.
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import realized_volatility
from walkforward import (
    DEFAULT_IS_BARS,
    DEFAULT_OOS_BARS,
    Trade,
    run_walk_forward,
    save_results,
)

# SPY/QQQ plus two liquid single names, per the plan's universe.
DEFAULT_SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT"]
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "validation" / "walkforward.json"

logger = logging.getLogger(__name__)


def fit_vol_regime(train_df) -> dict:
    """Fit phase: learn the in-sample realized-vol baseline.

    Deliberately simple and fully deterministic — the point of this harness is
    an honest OOS measurement, not a tuned strategy. Phase 3 swaps in the
    committee's structure selection behind this same interface.
    """
    return {"baseline_vol": realized_volatility(train_df, window=len(train_df))}


# One trade per 21-bar cycle: 10 bars to measure entry vol, then hold to the end.
CYCLE_BARS = 21
ENTRY_BAR = 9                       # entry is taken on bar index 9 of the cycle
HOLD_BARS = CYCLE_BARS - 1 - ENTRY_BAR   # bars actually at risk: 9 -> 20 = 11


def test_premium_selling(test_df, params) -> list[Trade]:
    """Score phase: a defined-risk premium-selling proxy on unseen bars.

    Sells premium when realized vol sits below its in-sample baseline (calm),
    and stands aside otherwise. Wins are capped at the credit (+1R) and losses
    at the spread width (-2R), mirroring a real credit spread's payoff.

    The breach threshold must be scaled to the bars actually held (HOLD_BARS),
    not to the length of the cycle. An earlier version used sqrt(21) while
    holding only 11 bars, comparing an 11-day realized move against a 21-day
    sigma — a threshold ~1.38x too generous, which manufactured a ~97% win rate
    by construction rather than by edge. Vol scales with the square root of
    time, so the horizon in the threshold and the horizon actually at risk have
    to be the same number.
    """
    baseline = params.get("baseline_vol", 0.0)
    if baseline <= 0 or len(test_df) < CYCLE_BARS:
        return []

    trades: list[Trade] = []
    for start in range(0, len(test_df) - CYCLE_BARS, CYCLE_BARS):
        window = test_df.iloc[start:start + CYCLE_BARS]
        entry_vol = realized_volatility(window.iloc[:ENTRY_BAR + 1], window=ENTRY_BAR + 1)
        if entry_vol <= 0 or entry_vol >= baseline:
            continue  # Not calm relative to what we learned in-sample.

        entry = float(window["close"].iloc[ENTRY_BAR])
        exit_price = float(window["close"].iloc[-1])
        move_pct = abs(exit_price - entry) / entry

        # Short strike ~1 baseline sigma away over the bars actually held.
        breach = baseline / (252 ** 0.5) * (HOLD_BARS ** 0.5)
        trades.append(Trade(
            symbol=str(test_df.get("symbol", ["?"]).iloc[0]) if "symbol" in test_df else "",
            r_multiple=-2.0 if move_pct > breach else 1.0,
            note=f"move={move_pct:.4f} breach={breach:.4f}",
        ))
    return trades


def main() -> int:
    parser = argparse.ArgumentParser(description="Walk-forward OOS validation")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--days", type=int, default=720, help="Calendar days of history")
    parser.add_argument("--is-bars", type=int, default=DEFAULT_IS_BARS)
    parser.add_argument("--oos-bars", type=int, default=DEFAULT_OOS_BARS)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    try:
        from alpaca_data import AlpacaData
        data = AlpacaData.from_env()
    except Exception as e:
        print(f"\n  Cannot reach Alpaca: {e}")
        print("  Set ALPACA_API_KEY / ALPACA_SECRET_KEY in .env (see .env.example).\n")
        return 1

    results = {}
    for symbol in args.symbols:
        bars = data.get_stock_bars(symbol, days=args.days)
        if bars.empty:
            logger.warning("%s: no bars returned, skipping", symbol)
            continue
        result = run_walk_forward(
            bars, fit_vol_regime, test_premium_selling,
            is_bars=args.is_bars, oos_bars=args.oos_bars, symbol=symbol,
        )
        results[symbol] = result
        o = result.oos
        logger.info(
            "%-5s %d bars, %d windows, %d OOS trades, win %.1f%%, expectancy %+.2fR",
            symbol, len(bars), len(result.windows), o.trades, o.win_rate, o.expectancy_r,
        )

    if not results:
        print("\n  No results computed — no symbol returned usable bars.\n")
        return 1

    path = save_results(results, OUTPUT_PATH)
    print(f"\n  Wrote {path}\n  Now run: make validate\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
