#!/usr/bin/env python3
# scripts/run_session.py
"""
Run one trading session cycle: preflight, build candidates, guard, execute.

This is the deterministic spine. The LLM committee (next plan) slots in at the
selection step; until then the session picks the highest-credit candidate,
which makes the execution path independently demoable.

It is also the ONLY production caller of OptionsExecutor.submit(), which makes
three things its responsibility and nobody else's:

  1. **Not sending the same order twice.** An unfilled `day` order stays live
     all session and is invisible to `position list`. So the session reads
     working orders too, and every payload carries a deterministic
     client_order_id that Alpaca will reject on a repeat.
  2. **Telling the guard the truth.** RiskGuard is stateless: every limit in
     risk.yaml is only as real as the PortfolioState handed to it. Daily P&L,
     the losing streak, today's trades per underlying and the existing book's
     Greeks are all derived here — from the account, the journal and the
     position rows — never hardcoded.
  3. **Failing closed.** Any broker error, unreadable journal, unmeasurable
     Greek or failed data fetch ends the cycle with no order.
"""
import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpaca_cli import AlpacaCLI
from analytics import (
    CONTRACT_MULTIPLIER, greeks, implied_vol, position_greeks, time_to_expiry_years,
)
from candidate_builder import (
    build_bear_call_spread, build_bull_put_spread, build_long_straddle,
)
from executor_options import OptionsExecutor
from journal import Journal
from options_orders import build_mleg_payload
from risk_guard import PortfolioState, RiskGuard, load_risk_config

logger = logging.getLogger(__name__)
JOURNAL_PATH = Path(__file__).resolve().parent.parent / "logs" / "journal.jsonl"

# Exit codes. 0 is "the cycle completed" — including a deliberate abstention,
# which is a first-class outcome (hard rule 4). Anything else means the cycle
# could not complete and a human should look.
EXIT_OK, EXIT_FAILED, EXIT_HALTED = 0, 1, 2

# Journal entry types that mean an order actually existed at the broker.
TRADED_TYPES = {"fill", "partial_fill", "pending"}
# Entry types that open exposure in an underlying (for the per-day cap).
OPENING_TYPES = {"fill", "partial_fill"}
# Entry types that may carry a realized P&L (for the losing streak).
CLOSING_TYPES = {"close", "exit", "fill", "partial_fill"}
PNL_KEYS = ("realized_pnl", "pnl")

# OCC option symbol: root, YYMMDD, C/P, strike x 1000 in 8 digits.
OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


# ── small pure helpers ───────────────────────────────────────

@dataclass(frozen=True)
class OccSymbol:
    root: str
    expiry: date
    right: str          # "c" or "p"
    strike: float


def parse_occ(symbol: str) -> OccSymbol | None:
    """Decompose an OCC option symbol, or None if it is not one.

    Position rows arrive as one row per contract with nothing but the symbol
    to say what the contract is, so this is how the existing book is read.
    """
    m = OCC_RE.match(str(symbol or "").strip().upper())
    if not m:
        return None
    root, yy, mm, dd, right, strike = m.groups()
    try:
        expiry = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None
    return OccSymbol(root, expiry, right.lower(), int(strike) / 1000.0)


def _float(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def daily_pnl(account: dict) -> float | None:
    """The day's equity move: equity - last_equity.

    Returns None when it cannot be computed — the 2% halt cannot be enforced
    without it, and an unmeasurable day must not read as a flat one.

    This is the change in *total* equity, so it includes open P&L as well as
    realized. That is stricter than the name `daily_realized_pnl` in the guard
    and errs toward halting earlier, which is the safe direction.
    """
    equity, last = _float(account.get("equity")), _float(account.get("last_equity"))
    if equity is None or last is None:
        return None
    return equity - last


def consecutive_losses(entries: list[dict]) -> int:
    """Length of the trailing run of losing closed trades in the journal.

    Zero when nothing has closed yet — an honest zero, not an assumption.
    """
    run = 0
    for entry in reversed(entries):
        if entry.get("type") not in CLOSING_TYPES:
            continue
        payload = entry.get("payload") or {}
        pnl = next((_float(payload[k]) for k in PNL_KEYS if k in payload), None)
        if pnl is None:
            continue          # an opening fill carries no realized P&L
        if pnl < 0:
            run += 1
        else:
            return run
    return run


def _entry_date(entry: dict) -> date | None:
    try:
        return datetime.fromisoformat(str(entry.get("timestamp"))).date()
    except (TypeError, ValueError):
        return None


def underlyings_of_order(order: dict) -> set[str]:
    """Every underlying an order touches.

    A multi-leg order carries no top-level symbol (Alpaca rejects one), so the
    underlying has to come from the nested legs' OCC symbols. An empty set
    means the order could not be attributed — callers treat that as "could be
    ours" rather than "not ours".
    """
    symbols = [order.get("symbol")]
    for leg in order.get("legs") or []:
        symbols.append((leg or {}).get("symbol"))

    roots = set()
    for symbol in symbols:
        if not symbol:
            continue
        occ = parse_occ(symbol)
        roots.add(occ.root if occ else str(symbol).upper())
    return roots


def working_orders_for(orders: list[dict], underlying: str) -> list[dict]:
    """Working orders that may already express this underlying.

    An order we cannot attribute counts as a match: guessing "not mine" is how
    a second identical spread gets sent.
    """
    want = underlying.upper()
    return [o for o in orders
            if not underlyings_of_order(o) or want in underlyings_of_order(o)]


def opened_today_by_underlying(entries: list[dict], orders: list[dict],
                               today: date) -> dict[str, int]:
    """Positions opened per underlying today: journalled fills plus live orders.

    A partially filled order can be counted twice — once as a journalled
    partial_fill and once as a still-working order. That over-counts, which
    denies rather than permits, so it is left as the safe direction.
    """
    counts: dict[str, int] = {}
    for entry in entries:
        if entry.get("type") not in OPENING_TYPES:
            continue
        if _entry_date(entry) != today:
            continue
        underlying = (entry.get("payload") or {}).get("underlying")
        if underlying:
            counts[str(underlying).upper()] = counts.get(str(underlying).upper(), 0) + 1

    for order in orders:
        for root in underlyings_of_order(order):
            counts[root] = counts.get(root, 0) + 1
    return counts


def has_ever_traded(entries: list[dict]) -> bool:
    """Whether an order has ever reached the broker from this journal."""
    return any(e.get("type") in TRADED_TYPES for e in entries)


def count_positions(positions: list[dict]) -> int:
    """Number of open STRATEGIES, which is what risk.yaml's max_positions means.

    Alpaca returns one row per option contract, so a two-leg spread is two
    rows. Counting rows made `max_positions: 3` mean one spread. Rows are
    grouped by underlying and expiry — the granularity at which this desk
    opens and closes a defined-risk structure. A row whose symbol cannot be
    parsed is counted on its own, which can only over-count.
    """
    groups, loose = set(), 0
    for row in positions:
        occ = parse_occ(str(row.get("symbol", "")))
        if occ is None:
            loose += 1
        else:
            groups.add((occ.root, occ.expiry))
    return len(groups) + loose


def _mark_price(row: dict) -> float | None:
    """Per-share option mark for a position row.

    Derived from market_value / (|qty| x 100) where possible: market_value is
    documented as the total dollar amount, so this is independent of whether
    current_price is quoted per share or per contract.
    """
    market_value, qty = _float(row.get("market_value")), _float(row.get("qty"))
    if market_value is not None and qty:
        mark = abs(market_value) / (abs(qty) * CONTRACT_MULTIPLIER)
        if mark > 0:
            return mark
    current = _float(row.get("current_price"))
    return current if current and current > 0 else None


def book_greeks(positions: list[dict], spot_for) -> tuple[float, float] | None:
    """Net (delta, vega) of the EXISTING book, in position terms.

    Returns None if any row cannot be valued — an unparseable symbol, a
    missing spot or a mark that no IV solves. None means "unmeasurable", and
    the caller must abstain: reporting an unknown book as flat Greeks is what
    makes the |net delta| and |net vega| limits decorative.
    """
    net_delta = net_vega = 0.0
    today = date.today()
    for row in positions:
        occ = parse_occ(str(row.get("symbol", "")))
        if occ is None:
            logger.warning("Cannot parse position symbol %r — book is unmeasurable",
                           row.get("symbol"))
            return None
        qty = _float(row.get("qty"))
        if not qty:
            logger.warning("Position %s has no usable qty", occ.root)
            return None
        mark, spot = _mark_price(row), spot_for(occ.root)
        if mark is None or not spot or spot <= 0:
            logger.warning("Cannot price position %s (mark=%s spot=%s)",
                           row.get("symbol"), mark, spot)
            return None

        t = time_to_expiry_years((occ.expiry - today).days)
        iv = implied_vol(mark, spot, occ.strike, t, occ.right)
        if iv is None:
            logger.warning("No IV solves position %s at mark %.2f — unmeasurable",
                           row.get("symbol"), mark)
            return None
        g = greeks(occ.right, spot, occ.strike, t, iv)
        # qty is signed: negative for a short leg, so the sign carries through.
        net_delta += g.delta * CONTRACT_MULTIPLIER * qty
        net_vega += g.vega * CONTRACT_MULTIPLIER * qty
    return net_delta, net_vega


# ── candidate generation ─────────────────────────────────────

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


# ── session ──────────────────────────────────────────────────

def _spot_lookup(data, seed: dict[str, float]):
    """Cached spot price per underlying, seeded with what we already fetched."""
    cache = dict(seed)

    def get(root: str) -> float | None:
        if root not in cache:
            try:
                bars = data.get_stock_bars(root, days=5)
                cache[root] = float(bars["close"].iloc[-1]) if not bars.empty else None
            except Exception as e:
                logger.warning("Spot lookup failed for %s: %s", root, e)
                cache[root] = None
        return cache[root]

    return get


def _abstain(journal, reason: str, detail: dict | None = None,
             dry_run: bool = False) -> int:
    """Print and journal a deliberate abstention. Always exit code 0.

    A refusal is a decision and must be as auditable as a trade (hard rule 5),
    but a dry run is a rehearsal and stays out of the judged journal.
    """
    print(f"  ABSTAIN: {reason}")
    if not dry_run and journal is not None:
        try:
            journal.append("abstain", {"reason": reason, **(detail or {})})
        except Exception as e:
            logger.error("Could not journal the abstention: %s", e, exc_info=True)
    return EXIT_OK


def main(argv=None, *, cli=None, data=None, journal=None, guard=None) -> int:
    parser = argparse.ArgumentParser(description="Run one options session cycle")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--dry-run", action="store_true", help="Never send an order")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    symbol = args.symbol.upper()

    guard = guard or RiskGuard(load_risk_config())

    # Hard rule 6 first, before any broker or data call.
    killed, why = guard.kill_switch_active()
    if killed:
        print(f"  HALTED — {why}")
        return EXIT_HALTED

    cli = cli or AlpacaCLI()
    if not cli.available():
        print("  Alpaca CLI not found. Install: brew install alpacahq/tap/cli")
        return EXIT_FAILED

    # One consistent broker snapshot. Any failure here is fail-closed: we
    # cannot know what is open, so we cannot safely open anything.
    try:
        account = cli.get_account()
        positions = cli.list_positions()
        working = cli.list_orders()
    except Exception as e:
        print(f"  Broker query failed ({e}) — no trade this cycle.")
        return EXIT_FAILED

    journal = journal if journal is not None else Journal(JOURNAL_PATH)
    try:
        entries = journal.entries()
    except Exception as e:
        print(f"  Journal unreadable ({e}) — refusing to trade unaudited.")
        return EXIT_FAILED

    open_positions = count_positions(positions)
    # The fresh-account assertions belong to the first run on a new dedicated
    # paper account. Once an order has ever reached the broker, equity has
    # legitimately moved and a position may be open; insisting otherwise makes
    # the desk single-use and locks it out of its own exit cycle.
    first_ever_run = not has_ever_traded(entries) and not working
    ready, err = guard.startup_checks(
        is_paper=True,
        options_level=int(account.get("options_trading_level", 0) or 0),
        open_positions=open_positions,
        equity=_float(account.get("equity")) if first_ever_run else None,
        require_flat=first_ever_run,
    )
    if not ready:
        print(f"  Preflight failed: {err}")
        return EXIT_FAILED

    # C1: an unfilled `day` order stays working all session and does not show
    # up in `position list`. Submitting again would put two identical spreads
    # in the market, and both can fill.
    already_working = working_orders_for(working, symbol)
    if already_working:
        ids = ", ".join(str(o.get("id", "?")) for o in already_working)
        return _abstain(journal,
                        f"{len(already_working)} working order(s) already open for "
                        f"{symbol} ({ids}) — not stacking a second one",
                        {"underlying": symbol, "order_ids": ids}, args.dry_run)

    if data is None:
        try:
            from alpaca_data import AlpacaData
            data = AlpacaData.from_env()
        except Exception as e:
            print(f"  DATA FETCH FAILED — market data unavailable ({e}).")
            return EXIT_FAILED

    try:
        bars = data.get_stock_bars(symbol, days=30)
    except Exception as e:
        print(f"  DATA FETCH FAILED — bars for {symbol} ({e}).")
        return EXIT_FAILED
    if bars is None or bars.empty:
        print(f"  No bars for {symbol} — cannot proceed (fail loud on data).")
        return EXIT_FAILED
    spot = float(bars["close"].iloc[-1])

    # Spec 4.4: fail LOUD on data. An outage must never be reported as a
    # judgement about the market.
    try:
        chain = data.get_option_chain(symbol)
    except Exception as e:
        print(f"  DATA FETCH FAILED — option chain for {symbol} ({e}).")
        print("  This is an outage, not a market judgement. No trade.")
        return EXIT_FAILED

    candidates = build_candidates(chain, symbol, spot)
    print(f"  {symbol} spot ${spot:,.2f} — {len(candidates)} candidate(s)")
    if not candidates:
        return _abstain(journal, "no candidate passed the liquidity gate",
                        {"underlying": symbol}, args.dry_run)

    # Deterministic placeholder for the committee: best credit per dollar risked.
    chosen = max(candidates, key=lambda c: c.net_credit / max(c.max_loss, 1e-9))
    print(f"  Selected {chosen.structure}: credit ${chosen.net_credit:.2f}, "
          f"max loss ${chosen.max_loss:,.2f}")

    # ── the state the guard will judge against ───────────────
    measured = position_greeks(chosen, spot)
    if measured is None:
        return _abstain(journal,
                        "the candidate's Greeks are unmeasurable (a leg has no "
                        "solvable IV) — cannot check the delta/vega limits",
                        {"underlying": symbol, "structure": chosen.structure},
                        args.dry_run)
    pos_delta, pos_vega = measured
    print(f"  position greeks: delta {pos_delta:+.1f}, vega {pos_vega:+.1f}")

    book = book_greeks(positions, _spot_lookup(data, {symbol: spot}))
    if book is None:
        return _abstain(journal,
                        f"the existing book ({len(positions)} row(s)) cannot be "
                        f"valued — refusing to score it as flat Greeks",
                        {"open_rows": len(positions)}, args.dry_run)
    net_delta, net_vega = book

    day_pnl = daily_pnl(account)
    if day_pnl is None:
        return _abstain(journal,
                        "the day's P&L cannot be read from the account — the "
                        "daily-loss halt cannot be enforced",
                        {"underlying": symbol}, args.dry_run)

    state = PortfolioState(
        open_positions=open_positions,
        net_delta=net_delta,
        net_vega=net_vega,
        daily_realized_pnl=day_pnl,
        consecutive_losses=consecutive_losses(entries),
        new_today_by_underlying=opened_today_by_underlying(
            entries, working, datetime.now(timezone.utc).date()),
    )
    print(f"  book: {state.open_positions} position(s), delta {state.net_delta:+.1f}, "
          f"vega {state.net_vega:+.1f}, day P&L ${state.daily_realized_pnl:+,.2f}, "
          f"{state.consecutive_losses} consecutive loss(es), "
          f"opened today {state.new_today_by_underlying or '{}'}")

    verdict = guard.evaluate(chosen, state, pos_delta, pos_vega)
    print(f"  guard: {getattr(verdict.decision, 'value', verdict.decision)} — "
          f"{verdict.reason} ({verdict.approved_contracts} contract(s) approved)")

    if args.dry_run:
        # The dry run exists to show exactly what the live run would do, so it
        # runs the whole preflight and prints the wire payload it would send.
        if verdict.is_tradeable and verdict.approved_contracts >= 1:
            payload = build_mleg_payload(chosen, verdict.approved_contracts)
            print("  payload that would be sent:")
            print(json.dumps(payload, indent=2))
        else:
            print("  no payload — the guard would refuse this candidate.")
        print("  DRY RUN — no order sent.")
        return EXIT_OK

    if not verdict.is_tradeable:
        # Say it here too, so the operator sees the refusal without reading
        # the journal. The executor re-evaluates and journals it.
        print("  Guard refuses this candidate.")

    executor = OptionsExecutor(cli, guard, journal)
    result = executor.submit(chosen, state,
                             position_delta=pos_delta, position_vega=pos_vega)
    print(f"  Result: {result.status} ({result.reason})")
    if result.status == "partially_filled":
        print("  NOTE: the remainder is still working — the next cycle will see it.")
    return EXIT_OK if result.status in (
        "filled", "partially_filled", "pending", "denied") else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
