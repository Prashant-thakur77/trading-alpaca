"""
Walk-forward out-of-sample validation.

Rolling anchored windows: fit on `is_bars` of history, then evaluate on the
`oos_bars` that immediately follow — bars the strategy has never seen. Windows
roll forward by the OOS length, so every OOS segment is disjoint and each bar
is scored at most once.

Every statistic here is computed from the trades actually produced. There are no
default or placeholder figures: a run with no trades reports zero trades. If a
strategy raises, that window contributes nothing rather than fabricating a
result. This module exists because reported performance must be reproducible
from the bars and the code alone.

Returns are expressed in R (multiples of the risk taken on the trade), which
makes windows with different position sizes comparable.
"""
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_IS_BARS = 90
DEFAULT_OOS_BARS = 30


@dataclass(frozen=True)
class Trade:
    """One closed trade, scored in R."""
    symbol: str
    r_multiple: float
    note: str = ""


@dataclass(frozen=True)
class Window:
    """Index bounds of one walk-forward split. Half-open: [start, end)."""
    is_start: int
    is_end: int
    oos_start: int
    oos_end: int


@dataclass(frozen=True)
class Stats:
    """Performance of a set of trades. All fields computed, none assumed."""
    trades: int
    wins: int
    losses: int
    win_rate: float          # percent
    expectancy_r: float      # mean R per trade
    profit_factor: float     # gross win / gross loss; inf when there are no losses
    max_drawdown_r: float    # largest peak-to-trough decline of the R equity curve
    total_r: float


@dataclass
class WalkForwardResult:
    windows: list[Window]
    window_results: list[Stats]
    oos: Stats
    symbol: str = ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "windows": len(self.windows),
            "window_results": [asdict(s) for s in self.window_results],
            "oos": asdict(self.oos),
        }


EMPTY_STATS = Stats(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0)


def generate_windows(
    total_bars: int, is_bars: int = DEFAULT_IS_BARS, oos_bars: int = DEFAULT_OOS_BARS
) -> list[Window]:
    """Rolling IS/OOS splits. Returns [] when history is too short for even one."""
    if total_bars < is_bars + oos_bars or is_bars <= 0 or oos_bars <= 0:
        return []

    windows = []
    start = 0
    while start + is_bars + oos_bars <= total_bars:
        windows.append(Window(
            is_start=start,
            is_end=start + is_bars,
            oos_start=start + is_bars,
            oos_end=start + is_bars + oos_bars,
        ))
        start += oos_bars
    return windows


def summarize(trades: list[Trade]) -> Stats:
    """Compute performance statistics from closed trades."""
    if not trades:
        return EMPTY_STATS

    rs = [t.r_multiple for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    # No losses means the ratio has no denominator. Report inf, don't invent one.
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    # Max drawdown of the cumulative R curve.
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return Stats(
        trades=len(rs),
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / len(rs) * 100.0,
        expectancy_r=sum(rs) / len(rs),
        profit_factor=profit_factor,
        max_drawdown_r=max_dd,
        total_r=sum(rs),
    )


def run_walk_forward(
    bars: pd.DataFrame,
    fit: Callable[[pd.DataFrame], dict],
    test: Callable[[pd.DataFrame, dict], list[Trade]],
    is_bars: int = DEFAULT_IS_BARS,
    oos_bars: int = DEFAULT_OOS_BARS,
    symbol: str = "",
) -> WalkForwardResult:
    """Fit on in-sample bars, score on the out-of-sample bars that follow.

    `fit` receives only in-sample bars and returns parameters. `test` receives
    only the following out-of-sample bars plus those parameters, and returns the
    trades it would have taken. The engine never shows `test` an in-sample bar.
    """
    if bars is None or len(bars) == 0:
        return WalkForwardResult([], [], EMPTY_STATS, symbol)

    windows = generate_windows(len(bars), is_bars, oos_bars)
    if not windows:
        logger.warning(
            "Not enough history for a walk-forward window: %d bars, need %d",
            len(bars), is_bars + oos_bars,
        )
        return WalkForwardResult([], [], EMPTY_STATS, symbol)

    all_trades: list[Trade] = []
    window_results: list[Stats] = []

    for w in windows:
        train_df = bars.iloc[w.is_start:w.is_end]
        test_df = bars.iloc[w.oos_start:w.oos_end]
        try:
            params = fit(train_df)
            trades = test(test_df, params) or []
        except Exception as e:
            # A broken strategy contributes nothing. It must never be scored
            # as if it had produced results.
            logger.warning("Walk-forward window %s failed, scoring zero trades: %s", w, e)
            trades = []

        all_trades.extend(trades)
        window_results.append(summarize(trades))

    return WalkForwardResult(
        windows=windows,
        window_results=window_results,
        oos=summarize(all_trades),
        symbol=symbol,
    )


def save_results(results: dict[str, WalkForwardResult], path: Path | str) -> Path:
    """Persist results for the validation report and the judge page."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "is_bars": DEFAULT_IS_BARS,
        "oos_bars": DEFAULT_OOS_BARS,
        "symbols": {sym: r.to_dict() for sym, r in results.items()},
    }
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    return path
