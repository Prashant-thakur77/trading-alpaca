#!/usr/bin/env python3
# scripts/run_session.py
"""
Run one trading session cycle: preflight, build candidates, guard, execute.

This is the deterministic spine. The LLM committee (next plan) slots in at the
selection step; until then the session picks the highest-credit candidate,
which makes the execution path independently demoable.
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpaca_cli import AlpacaCLI
from analytics import position_greeks
from candidate_builder import (
    build_bear_call_spread, build_bull_put_spread, build_long_straddle,
)
from executor_options import OptionsExecutor
from journal import Journal
from risk_guard import PortfolioState, RiskGuard, load_risk_config

logger = logging.getLogger(__name__)
JOURNAL_PATH = Path(__file__).resolve().parent.parent / "logs" / "journal.jsonl"


def build_candidates(chain, underlying: str, spot: float, width: float = 5.0) -> list:
    """Enumerate every defined-risk candidate the chain supports.

    Deterministic and exhaustive: the LLM later picks among these by id and
    can never introduce a strike that this function did not produce.

    A real chain spans several expiries in the DTE window (e.g. SPY has ~9 in
    7-45 DTE) with the same strike repeated on each. Matching legs by strike
    alone would silently pair legs from different expiries — candidate_builder
    then raises "legs must share an expiry", crashing the whole session
    instead of abstaining. So candidates are built per expiry, independently.
    """
    if not chain:
        return []

    candidates = []
    expiries = sorted({q.expiry for q in chain})
    for expiry in expiries:
        candidates.extend(_build_candidates_for_expiry(
            [q for q in chain if q.expiry == expiry], spot, width))
    return candidates


def _build_candidates_for_expiry(chain, spot: float, width: float) -> list:
    """Candidate sweep within a single expiry's quotes."""
    puts = sorted([q for q in chain if q.right == "p"], key=lambda q: q.strike)
    calls = sorted([q for q in chain if q.right == "c"], key=lambda q: q.strike)
    by_strike_p = {q.strike: q for q in puts}
    by_strike_c = {q.strike: q for q in calls}
    candidates = []

    # Bull put spreads: short strike below spot, long strike `width` lower.
    for short in [q for q in puts if q.strike < spot]:
        long_leg = by_strike_p.get(short.strike - width)
        if long_leg:
            intent = build_bull_put_spread(short, long_leg)
            if intent:
                candidates.append(intent)

    # Bear call spreads: short strike above spot, long strike `width` higher.
    for short in [q for q in calls if q.strike > spot]:
        long_leg = by_strike_c.get(short.strike + width)
        if long_leg:
            intent = build_bear_call_spread(short, long_leg)
            if intent:
                candidates.append(intent)

    # Long straddle at the strike nearest spot, if both legs exist.
    if puts:
        # Sort before min() so a tie (e.g. 445 vs 455 at spot 450) resolves
        # deterministically instead of by set iteration order — required for
        # golden-file replay in a later phase.
        atm = min(sorted({q.strike for q in puts}), key=lambda s: abs(s - spot))
        call, put = by_strike_c.get(atm), by_strike_p.get(atm)
        if call and put:
            intent = build_long_straddle(call, put)
            if intent:
                candidates.append(intent)

    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one options session cycle")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--dry-run", action="store_true", help="Never send an order")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    guard = RiskGuard(load_risk_config())
    cli = AlpacaCLI()
    if not cli.available():
        print("  Alpaca CLI not found. Install: brew install alpacahq/tap/cli")
        return 1

    account = cli.get_account()
    positions = cli.list_positions()
    ready, err = guard.startup_checks(
        is_paper=True,
        options_level=int(account.get("options_trading_level", 0) or 0),
        open_positions=len(positions),
        equity=float(account.get("equity", 0) or 0),
    )
    if not ready:
        print(f"  Preflight failed: {err}")
        return 1

    from alpaca_data import AlpacaData
    data = AlpacaData.from_env()
    bars = data.get_stock_bars(args.symbol, days=30)
    if bars.empty:
        print(f"  No bars for {args.symbol} — cannot proceed (fail loud on data).")
        return 1
    spot = float(bars["close"].iloc[-1])

    chain = data.get_option_chain(args.symbol)
    candidates = build_candidates(chain, args.symbol, spot)
    print(f"  {args.symbol} spot ${spot:,.2f} — {len(candidates)} candidate(s)")
    if not candidates:
        print("  ABSTAIN: no candidate passed the liquidity gate.")
        return 0

    # Deterministic placeholder for the committee: best credit per dollar risked.
    chosen = max(candidates, key=lambda c: c.net_credit / max(c.max_loss, 1e-9))
    print(f"  Selected {chosen.structure}: credit ${chosen.net_credit:.2f}, "
          f"max loss ${chosen.max_loss:,.2f}")

    if args.dry_run:
        print("  DRY RUN — no order sent.")
        return 0

    pos_delta, pos_vega = position_greeks(chosen, spot)
    print(f"  position greeks: delta {pos_delta:+.1f}, vega {pos_vega:+.1f}")

    executor = OptionsExecutor(cli, guard, Journal(JOURNAL_PATH))
    state = PortfolioState(len(positions), 0.0, 0.0, 0.0, 0, {})
    result = executor.submit(chosen, state,
                             position_delta=pos_delta, position_vega=pos_vega)
    print(f"  Result: {result.status} ({result.reason})")
    return 0 if result.status in ("filled", "pending", "denied") else 1


if __name__ == "__main__":
    sys.exit(main())
