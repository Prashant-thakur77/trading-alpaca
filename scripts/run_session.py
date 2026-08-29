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
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpaca_cli import AlpacaCLI
from analytics import (
    CONTRACT_MULTIPLIER, atm_implied_vol, greeks, implied_vol, position_greeks,
    realized_volatility, time_to_expiry_years,
)
from committee.decide import ABSTAIN, NOT_RUN, CommitteeDecision, cached_client
from committee.decide import decide as committee_decide
from committee.premortem import (
    PREMORTEM_MODEL, deterministic_triggers, premortem as run_premortem,
)
from candidate_builder import (
    build_bear_call_spread, build_bull_put_spread, build_long_straddle,
)
from executor_options import OptionsExecutor
from exit_monitor import OpenTrade, OpenTradeStore, monitor_positions
from journal import Journal
from options_orders import build_mleg_payload
from risk_guard import PortfolioState, RiskGuard, load_risk_config

logger = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parent.parent
JOURNAL_PATH = REPO_ROOT / "logs" / "journal.jsonl"
# The prompt cache is the cost saver, the audit record AND the replay corpus
# the /judge page reads back, so it lives on disk beside the journal rather
# than in a temp dir that a reboot would erase.
PROMPT_CACHE_DIR = REPO_ROOT / "logs" / "prompt_cache"
# What the desk currently has open, so the NEXT process can manage it. Each
# `make session` run is a fresh process; the broker's position rows carry the
# symbols but not the structure, the entry credit, the pre-mortem's triggers
# or the snapshot_hash that attributes the outcome back to the analysts.
OPEN_TRADES_PATH = REPO_ROOT / "logs" / "open_trades.json"

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

    INERT IN PRODUCTION as of Plan 1: this derivation is correct and
    covered by tests, but no writer in this codebase yet journals a "close"
    (or "exit") entry carrying realized_pnl — exit monitoring lands in
    Plan 2. Until then this always reads 0 in a live session, so risk.yaml's
    "3 consecutive losers halves size" limit never fires. The 2% daily-loss
    halt is the live backstop in the meantime: it is derived from
    equity - last_equity (see daily_pnl()), so it captures open P&L too and
    does not depend on any close entry existing.
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


def _equity_delta(row: dict, qty: float | None, spot_for) -> float | None:
    """Position delta of a share row: +1 per share long, -1 per share short.

    A share's delta is 1 by definition — it IS the underlying — so this needs
    no implied vol, no expiry and no pricing model, and it is signed by the
    broker's own signed quantity. A spot lookup is still required: a symbol
    this desk cannot even price is one it does not understand, and an
    unrecognised instrument must not be silently scored as flat.

    Returns None when the row has no usable quantity or no spot, which the
    caller turns into an abstention.
    """
    symbol = str(row.get("symbol", "")).strip().upper()
    if not qty:
        logger.warning("Equity position %r has no usable qty — book is "
                       "unmeasurable", symbol)
        return None
    spot = spot_for(symbol)
    if not spot or spot <= 0:
        logger.warning("No spot for equity position %r — book is unmeasurable",
                       symbol)
        return None
    logger.info("Equity position %s: %+.0f shares = %+.1f delta (assigned "
                "stock is valued, not treated as unmeasurable)", symbol, qty, qty)
    return qty


def book_greeks(positions: list[dict], spot_for) -> tuple[float, float] | None:
    """Net (delta, vega) of the EXISTING book, in position terms.

    Returns None if any row cannot be valued — a missing spot, a missing
    quantity or a mark that no IV solves. None means "unmeasurable", and the
    caller must abstain: reporting an unknown book as flat Greeks is what
    makes the |net delta| and |net vega| limits decorative.

    **A share position is not unmeasurable.** An OCC symbol that will not
    parse is an EQUITY row — overwhelmingly, this desk's own short leg
    assigned into stock at expiry (design spec A.5). Its Greeks are the most
    certain in the whole book: 100 shares are exactly 100 delta and zero
    vega, no model and no implied vol required. Returning None for it froze
    the desk permanently — every later cycle printed "the existing book
    cannot be valued" and abstained, with no way left to manage or exit the
    assigned position (parked ruling, Plan 1 final review). It failed closed,
    so no money was at risk, but a desk that can never trade again is worse
    than one that values a share position correctly.

    Everything that genuinely cannot be valued still returns None.
    """
    net_delta = net_vega = 0.0
    today = date.today()
    for row in positions:
        occ = parse_occ(str(row.get("symbol", "")))
        qty = _float(row.get("qty"))
        if occ is None:
            equity_delta = _equity_delta(row, qty, spot_for)
            if equity_delta is None:
                return None
            net_delta += equity_delta     # shares carry no vega at all
            continue
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


# ── candidate selection: the committee, or the deterministic fallback ──

def best_by_credit_ratio(candidates: list):
    """The deterministic selector: most credit per dollar risked.

    This is what ran before the committee was wired in, and it is what
    `--no-llm` still runs. It is not a placeholder any more — it is the
    fallback that keeps the desk able to trade when the LLM is unavailable.
    """
    return max(candidates, key=lambda c: c.net_credit / max(c.max_loss, 1e-9))


def _make_default_committee(atm_iv):
    """Build the production committee: `committee.decide.decide` with the
    on-disk prompt cache attached, and `atm_iv` bound in by closure.

    `run_committee` (below) invokes every committee — injected test doubles
    included — as `committee(underlying, spot, realized_vol, candidates,
    journal)`. Adding `atm_iv` as a 6th positional/keyword argument there
    would break every test double written against that 5-arg shape. Binding
    it via closure instead keeps the shared call surface untouched while
    still carrying the chain-derived ATM IV (computed once in `main`, before
    the committee runs) into `committee_decide`.
    """
    def _default_committee(underlying, spot, realized_vol, candidates, journal):
        from llm.cache import PromptCache
        return committee_decide(underlying, spot, realized_vol, candidates, journal,
                                cache=PromptCache(PROMPT_CACHE_DIR), atm_iv=atm_iv)
    return _default_committee


def _failed_committee(reason: str) -> CommitteeDecision:
    """A committee that could not run is an abstention, never a fall-through.

    `decide()` already folds every internal failure into an abstention, so
    reaching here means the committee itself was unavailable (an injected
    one, an import failure, an OOM). Fail closed identically.
    """
    return CommitteeDecision(
        chosen=None, choice_id=ABSTAIN, views=(), aggregate_probability=None,
        trader_reasoning="", thesis_ok=False, thesis_reason=reason,
        blind_ok=False, blind_reason=reason, snapshot_hash="",
        abstain_reason=reason,
    )


def run_committee(committee, underlying, spot, realized_vol, candidates,
                  journal) -> CommitteeDecision:
    """Call the committee, converting any escape into an abstention."""
    try:
        decision = committee(underlying, spot, realized_vol, candidates, journal)
    except Exception as e:  # noqa: BLE001 - a broken committee must not trade
        logger.error("Committee failed: %s", e, exc_info=True)
        return _failed_committee(f"the committee could not run "
                                 f"({type(e).__name__}: {e})")
    if decision is None:
        return _failed_committee("the committee returned nothing")
    return decision


def _veto_verdict(ok: bool, reason: str) -> str:
    """Render one veto's verdict line — without ever leading with the word
    VETO for a check that never ran.

    When the cycle abstains before the veto layer, `decide()` leaves both
    `ok` flags False with `reason=NOT_RUN` (committee/decide.py). Printing
    that as "VETO — not reached ..." reads, to a judge skimming the
    transcript, as a veto having actually fired on a trade that was never
    proposed. "not run" leads instead — never "PASS", since a check that
    did not run must never render as passed.
    """
    if reason == NOT_RUN:
        detail = NOT_RUN.split("— ", 1)[1] if "— " in NOT_RUN else NOT_RUN
        return f"not run ({detail})"
    return f"{'PASS' if ok else 'VETO'} — {reason}"


def print_committee(decision: CommitteeDecision) -> None:
    """Print the whole reasoning chain: a judge watching the screen should be
    able to follow it from each analyst's view to both veto verdicts."""
    print("  committee:")
    for view in decision.views:
        if view.abstained:
            print(f"    {view.role:<16} ABSTAINED — {view.abstain_reason}")
        else:
            print(f"    {view.role:<16} p={view.probability:.2f} — {view.reasoning}")
    aggregate = decision.aggregate_probability
    print(f"    aggregate probability: "
          f"{'none — every analyst abstained' if aggregate is None else f'{aggregate:.2f}'}")
    print(f"    trader: {decision.choice_id} — {decision.trader_reasoning or '(no reasoning given)'}")
    print(f"    veto thesis: {_veto_verdict(decision.thesis_ok, decision.thesis_reason)}")
    print(f"    veto blind:  {_veto_verdict(decision.blind_ok, decision.blind_reason)}")


# ── the pre-mortem and the open book ─────────────────────────

def _make_default_premortem():
    """The production pre-mortem: an LLM call routed through the SAME prompt
    cache the committee uses, so a replayed cycle re-derives the identical
    triggers rather than diverging from what was recorded."""
    def call(intent, spot, realized_vol):
        from llm.cache import PromptCache
        from llm.client import call_claude
        client = cached_client(call_claude, PromptCache(PROMPT_CACHE_DIR),
                               PREMORTEM_MODEL)
        return run_premortem(intent, spot, realized_vol, client=client)
    return call


def _select_premortem(no_llm: bool):
    """`--no-llm` means no LLM anywhere in the loop, the pre-mortem included.

    The deterministic exits are the whole point of the fallback: the desk
    still closes at 50% of the credit and still closes at 3 DTE, it just has
    no model-authored failure modes on top.
    """
    if no_llm:
        return lambda intent, spot, realized_vol: deterministic_triggers(
            intent, reason="--no-llm: no LLM in the loop")
    return _make_default_premortem()


def build_exit_plan(premortem, chosen, spot, realized_vol):
    """Compile the pre-mortem into exit triggers. Never raises.

    An injected pre-mortem that blows up must not stop a guarded trade, and
    must not leave the position without its deterministic exits — so any
    escape falls back to `deterministic_triggers`, exactly as
    `committee.premortem` does internally for an LLM failure.
    """
    try:
        triggers = premortem(chosen, spot, realized_vol)
    except Exception as e:  # noqa: BLE001 - fail soft on the LLM, never open-ended
        logger.error("Pre-mortem failed (%s: %s) — deterministic exits only",
                     type(e).__name__, e, exc_info=True)
        triggers = deterministic_triggers(chosen, reason="pre-mortem unavailable")
    return tuple(triggers or deterministic_triggers(chosen))


def print_exit_plan(triggers) -> None:
    print("  pre-mortem exit plan:")
    for trigger in triggers:
        print(f"    {trigger.kind:<18} {trigger.threshold:<10g} {trigger.rationale}")


def _trigger_payloads(triggers) -> list[dict]:
    return [{"kind": t.kind, "threshold": t.threshold, "rationale": t.rationale}
            for t in triggers]


def remember_open_trade(store, result, chosen, triggers, spot,
                        snapshot_hash: str) -> None:
    """Write the new position into the open book so the NEXT cycle can exit it.

    Records the guard-APPROVED contract count from the execution result, not
    `chosen.contracts`: a downsized position must be closed at the size that
    was actually opened. Never raises — the position exists at the broker
    whatever this file ends up saying, and an exception here after a fill
    would look to the caller like the trade did not happen.
    """
    try:
        store.save([*store.load(), OpenTrade(
            order_id=result.order_id, intent=chosen, triggers=tuple(triggers),
            contracts=result.contracts, entry_spot=spot,
            snapshot_hash=snapshot_hash,
            opened_at=datetime.now(timezone.utc).isoformat(),
        )])
    except Exception as e:  # noqa: BLE001
        logger.error("Could not record the open trade (%s) — the next cycle "
                     "may not manage it: %s", result.order_id, e, exc_info=True)


def manage_open_book(cli, data, guard, journal, store, working_order_ids) -> None:
    """Evaluate the exits on everything already open, before opening anything.

    Runs FIRST in a live cycle: managing the book outranks adding to it, and
    an exit must not be skipped just because this cycle later abstains on a
    working order or a failed liquidity gate.

    A trade the monitor reports as no longer open is dropped from the store —
    unless the broker still shows a working order for it, which is the case
    where an order was submitted but has not filled yet and its legs are
    legitimately not in the position list.

    Never raises: a failure here must not stop the cycle, and it must never
    open a position.
    """
    try:
        trades = store.load()
    except Exception as e:  # noqa: BLE001 - the store is state, not a gate
        logger.error("Could not read the open book: %s", e, exc_info=True)
        return
    if not trades:
        return

    print(f"  managing {len(trades)} open trade(s) before looking for a new one")
    try:
        events = monitor_positions(cli, data, guard, journal,
                                   {t.order_id: t for t in trades})
    except Exception as e:  # noqa: BLE001 - monitoring must never crash a cycle
        logger.error("Exit monitoring failed: %s", e, exc_info=True)
        return

    by_id = {e.order_id: e for e in events}
    for event in events:
        detail = event.error or event.reason
        marker = "CLOSED" if event.closed else "open  "
        pnl = "" if event.realized_pnl is None else f" (${event.realized_pnl:+,.2f})"
        print(f"    {marker} {event.underlying} {event.structure}{pnl} — {detail}")

    keep = [t for t in trades
            if by_id.get(t.order_id) is None
            or by_id[t.order_id].still_open
            or t.order_id in working_order_ids]
    if len(keep) != len(trades):
        store.save(keep)


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


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (mirrors scripts/check_account.py) so `make session`
    works from a plain shell or a cron entry, where nothing has been exported.

    `setdefault`, so a variable already in the environment always wins.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main(argv=None, *, cli=None, data=None, journal=None, guard=None,
         committee=None, premortem=None, store=None) -> int:
    _load_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Run one options session cycle")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--dry-run", action="store_true", help="Never send an order")
    parser.add_argument(
        "--no-llm", action="store_true",
        help="Skip the LLM committee and select deterministically (most credit "
             "per dollar risked). The escape hatch for a live session: if Claude "
             "is rate-limited or down, the desk still trades inside the same "
             "RiskGuard rather than being dead.")
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

    if data is None:
        try:
            from alpaca_data import AlpacaData
            data = AlpacaData.from_env()
        except Exception as e:
            print(f"  DATA FETCH FAILED — market data unavailable ({e}).")
            return EXIT_FAILED

    # ── manage what is already open, BEFORE looking for anything new ──
    # An exit must not be skipped because this cycle later abstains on a
    # working order or an empty candidate list. A dry run is a rehearsal and
    # never sends a closing order.
    store = store if store is not None else OpenTradeStore(OPEN_TRADES_PATH)
    if args.dry_run:
        print("  DRY RUN — the open book is not managed and nothing is closed.")
    else:
        manage_open_book(cli, data, guard, journal, store,
                         {str(o.get("id", "")) for o in working})

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

    # ATM IV must be a property of the market (the whole chain), never of
    # which candidates happened to be built or survive the committee's
    # surfacing cap — see committee/snapshot.py's render_snapshot docstring
    # for the regime-flip this caused when it was candidate-derived.
    market_atm_iv = atm_implied_vol(chain, spot)
    print(f"  ATM IV (chain-derived, ~30 DTE): "
          f"{f'{market_atm_iv * 100:.2f}%' if market_atm_iv is not None else 'unavailable'}")

    # ── selection: the committee proposes, RiskGuard disposes ──
    realized_vol = realized_volatility(bars)
    # The join key that lets a later `close` entry resolve this cycle's
    # analyst predictions (calibration.resolved_predictions). Empty on the
    # --no-llm path, where there were no predictions to resolve.
    snapshot_hash = ""
    if args.no_llm:
        print("  mode: DETERMINISTIC (--no-llm) — no LLM in the loop; "
              "selecting the most credit per dollar risked")
        chosen = best_by_credit_ratio(candidates)
    else:
        print("  mode: LLM COMMITTEE — vol_analyst + bear_adversary -> trader "
              "-> thesis veto + blind veto")
        decision = run_committee(
            committee or _make_default_committee(market_atm_iv), symbol, spot,
            realized_vol, candidates,
            # A dry run is a rehearsal and stays out of the judged chain, the
            # same rule `_abstain(dry_run=True)` already follows. In a live
            # run the committee journals every stage itself (hard rule 5).
            None if args.dry_run else journal,
        )
        print_committee(decision)
        if decision.chosen is None:
            # A refusal is a normal outcome, not a failure: exit 0.
            return _abstain(journal, f"committee abstained — {decision.abstain_reason}",
                            {"underlying": symbol,
                             "snapshot_hash": decision.snapshot_hash},
                            args.dry_run)
        chosen = decision.chosen
        snapshot_hash = decision.snapshot_hash

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
        # INERT until Plan 2: no writer yet journals a "close"/"exit" entry
        # carrying realized_pnl, so this reads 0 every cycle and the 3-loser
        # halving never fires in production. See consecutive_losses()
        # docstring. The 2% daily-loss halt above (day_pnl) is the live
        # backstop — it uses equity - last_equity, so it catches open P&L
        # too and does not depend on any close entry existing.
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

    # The pre-mortem runs BEFORE the order is sent, never after: triggers
    # written after the fill would leave a live position with no exit plan if
    # the process died in between. It runs only for a candidate the guard
    # would actually let through, so a refusal costs no LLM call.
    triggers = ()
    if verdict.is_tradeable and verdict.approved_contracts >= 1:
        triggers = build_exit_plan(
            premortem or _select_premortem(args.no_llm), chosen, spot,
            realized_vol)
        print_exit_plan(triggers)

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
        # Stop here — do not fall through to executor.submit(), which
        # re-evaluates the guard from scratch (including the kill switch).
        # If the kill switch were lifted between this evaluate() and that
        # one, falling through would submit an order right after printing a
        # refusal. Journal the verdict directly instead, mirroring what
        # executor.submit() would have recorded, so the refusal is still
        # auditable (hard rule 5) without a second evaluation.
        print("  Guard refuses this candidate.")
        try:
            journal.append("verdict", {
                "underlying": chosen.underlying,
                "structure": chosen.structure,
                "decision": getattr(verdict.decision, "value", str(verdict.decision)),
                "reason": verdict.reason,
                "approved_contracts": verdict.approved_contracts,
            })
        except Exception as e:
            logger.error("Could not journal the guard verdict: %s", e, exc_info=True)
        return EXIT_OK

    # Journalled before submission, for the same reason it is computed before
    # submission: a position must never exist at the broker without its exit
    # plan already written down (hard rule 5).
    try:
        journal.append("premortem", {
            "underlying": chosen.underlying, "structure": chosen.structure,
            "snapshot_hash": snapshot_hash,
            "triggers": _trigger_payloads(triggers),
        })
    except Exception as e:
        logger.error("Could not journal the pre-mortem: %s", e, exc_info=True)

    executor = OptionsExecutor(cli, guard, journal)
    result = executor.submit(chosen, state,
                             position_delta=pos_delta, position_vega=pos_vega)
    print(f"  Result: {result.status} ({result.reason})")

    if result.status in ("filled", "partially_filled") and result.contracts > 0:
        remember_open_trade(store, result, chosen, triggers, spot, snapshot_hash)

    if result.status == "partially_filled":
        print("  NOTE: the remainder is still working — the next cycle will see it.")
    return EXIT_OK if result.status in (
        "filled", "partially_filled", "pending", "denied") else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
