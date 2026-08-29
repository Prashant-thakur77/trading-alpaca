"""Exit monitoring — managing the open book, and journalling a CLOSED trade.

This module closes the loop the rest of the desk was waiting on. Until it
existed, nothing in this codebase journalled a closed trade carrying a
realized P&L, and four separate things were dormant or broken as a result:

  1. `calibration.py` was built and tested but had nothing to score, so
     `make calibration` honestly reported "0 resolved predictions" forever;
  2. `run_session.consecutive_losses` was permanently 0, so risk.yaml's
     "3 consecutive losers halves size" never fired;
  3. an assigned equity position froze the desk into permanent abstention
     (fixed alongside this, in `run_session.book_greeks`);
  4. `committee/premortem.py`'s triggers were rules nobody evaluated.

The keystone is one journal entry: `close`, carrying `realized_pnl` and the
originating `snapshot_hash`. That hash is what lets
`calibration.resolved_predictions` join an OUTCOME back to the analyst
predictions that produced it — the join that makes "the desk that grades
itself" literal rather than a slogan.

**There is no atomic multi-leg close.** Verified live against the paper
account (design spec A.1): `close_position` closes one leg at a time, which
would leave a naked short leg in the market between calls. Unwinding a spread
therefore submits ONE NEW multi-leg order with every side inverted —
`options_orders.closing_payload` — and that is the only way this module ever
closes a position.

Three exits apply to every position whatever the pre-mortem said, because
they are risk controls rather than opinions:

  * **max loss reached** — the defined risk was the whole premise;
  * **50% of the credit received** — the profit target;
  * **DTE <= 3** — forced, to avoid a short ITM leg being assigned into stock
    at expiry (design spec A.5).

Everything fails closed. An unreadable mark, a missing leg, a broker
rejection, a data outage or a raising journal produces an `ExitEvent`
carrying the error and leaves the position open and still monitored — it
never opens a position, and it never reports a P&L that did not happen.
"""
import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from analytics import (
    CONTRACT_MULTIPLIER, implied_vol, time_to_expiry_years,
)
from candidate_builder import Leg, OptionQuote, TradeIntent
from committee.premortem import (
    KIND_CREDIT_DECAY, KIND_DTE_BELOW, KIND_IV_SPIKE, KIND_UNDERLYING_BEYOND,
    ExitTrigger, deterministic_triggers,
)
from options_orders import closing_payload

logger = logging.getLogger(__name__)

TERMINAL_FILLED = {"filled"}
TERMINAL_DEAD = {"canceled", "expired", "rejected", "done_for_day"}

#: Reason text for the one exit that is not expressible as an `ExitTrigger`.
MAX_LOSS_REASON = "max loss reached — the defined risk of the structure"


class _Unmeasurable(RuntimeError):
    """This position cannot be valued right now. Never a reason to trade."""


@dataclass(frozen=True)
class OpenTrade:
    """What the desk must remember about a position in order to manage it.

    `triggers` are the pre-mortem's compiled exits; the deterministic ones
    are re-derived here on every pass, so a trade recorded without them is
    still protected. `entry_spot` is what gives an `underlying_beyond` level
    its direction: a threshold below the spot at entry is a downside breach,
    one above it is an upside breach. That single rule serves bullish,
    bearish and two-sided structures without the trigger having to carry a
    direction field the model could get wrong.

    `contracts` is the guard-APPROVED count actually sent, which is not
    necessarily `intent.contracts` — a downsized position must be closed at
    the size that was opened, not the size that was proposed.
    """
    order_id: str
    intent: object                    # candidate_builder.TradeIntent
    triggers: tuple[ExitTrigger, ...] = ()
    contracts: int = 1
    entry_spot: float = 0.0
    snapshot_hash: str = ""
    opened_at: str = ""


@dataclass(frozen=True)
class ExitEvent:
    """What monitoring did about one open trade this pass.

    `still_open` is what the caller acts on: False means stop monitoring this
    trade (it closed, or it is no longer in the book). Any error leaves it
    True — an unmanageable position must stay visible, not be forgotten.
    """
    order_id: str
    underlying: str
    structure: str
    closed: bool
    still_open: bool
    reason: str
    trigger: ExitTrigger | None = None
    realized_pnl: float | None = None
    close_order_id: str = ""
    error: str = ""


# ── reading the book ────────────────────────────────────────

def _float(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def mark_price(row: dict) -> float | None:
    """Per-share option mark for a broker position row.

    Derived from `market_value / (|qty| * 100)` where possible: market_value
    is documented as the total dollar amount, so this is independent of
    whether `current_price` is quoted per share or per contract. Shared with
    `scripts/run_session.book_greeks` so both read a row the same way.
    """
    market_value, qty = _float(row.get("market_value")), _float(row.get("qty"))
    if market_value is not None and qty:
        mark = abs(market_value) / (abs(qty) * CONTRACT_MULTIPLIER)
        if mark > 0:
            return mark
    current = _float(row.get("current_price"))
    return current if current and current > 0 else None


def _leg_marks(intent, rows_by_symbol: dict) -> dict:
    """Per-share mark for every leg, or raise if any leg cannot be read."""
    marks = {}
    for leg in intent.legs:
        symbol = str(leg.quote.symbol).upper()
        row = rows_by_symbol.get(symbol)
        if row is None:
            raise _Unmeasurable(
                f"leg {symbol} is not in the book — the position is partially "
                f"closed, expired or assigned; an inverted multi-leg order "
                f"over legs that are not all open would be rejected")
        mark = mark_price(row)
        if mark is None:
            raise _Unmeasurable(f"leg {symbol} has no readable mark")
        marks[symbol] = mark
    return marks


def close_net_credit(intent, marks: dict) -> float:
    """Per-share net credit received by CLOSING, at the given marks.

    Closing inverts every side: a leg we are long is sold (we receive its
    mark), a leg we are short is bought back (we pay its mark). Negative
    therefore means unwinding costs money, which is the normal case for a
    credit spread.
    """
    total = 0.0
    for leg in intent.legs:
        mark = marks[str(leg.quote.symbol).upper()]
        total += mark if leg.side == "buy" else -mark
    return total


def realized_pnl(intent, contracts: int, closing_credit: float) -> float:
    """Total dollars made or lost, opening credit plus closing credit.

    `intent.net_credit` is per share and positive for a credit received;
    `closing_credit` is the same convention for the unwind. One formula
    covers both directions: a bull put spread opened for 1.00 and bought back
    for 0.40 is (1.00 - 0.40) * 100 = +$60, and a straddle opened for a 6.00
    debit and sold for 8.00 is (-6.00 + 8.00) * 100 = +$200.
    """
    return (intent.net_credit + closing_credit) * CONTRACT_MULTIPLIER * contracts


def _position_iv(intent, marks: dict, spot: float, dte: int) -> float | None:
    """The position's own implied vol: the highest IV that solves any leg.

    Deliberately not a chain-wide ATM number. An `iv_spike` matters because
    it re-prices THIS position, and the legs' own marks are both the most
    relevant measure and the only one available without a second data fetch
    that could fail independently of everything else in this pass.
    """
    t = time_to_expiry_years(dte)
    ivs = []
    for leg in intent.legs:
        iv = implied_vol(marks[str(leg.quote.symbol).upper()], spot,
                         leg.quote.strike, t, leg.quote.right)
        if iv is not None:
            ivs.append(iv)
    return max(ivs) if ivs else None


def _dte_of(intent, today: date) -> int:
    return min((leg.quote.expiry - today).days for leg in intent.legs)


# ── evaluating the exits ────────────────────────────────────

def _all_triggers(trade) -> tuple[ExitTrigger, ...]:
    """The deterministic exits first, then the pre-mortem's own.

    Re-derived on every pass rather than trusted from the stored record, so a
    trade whose triggers were lost, truncated or written by an older version
    of this code is still protected by the non-negotiable ones. Order is the
    firing priority, and duplicates collapse onto the deterministic
    rationale.
    """
    out, seen = [], set()
    for trigger in (*deterministic_triggers(trade.intent), *trade.triggers):
        key = (trigger.kind, trigger.threshold)
        if key in seen:
            continue
        seen.add(key)
        out.append(trigger)
    return tuple(out)


def _fires(trigger: ExitTrigger, trade, spot, dte, iv, pnl, credit_total) -> bool:
    """Whether this trigger has tripped. Unknowable never means "yes"."""
    if trigger.kind == KIND_DTE_BELOW:
        return dte <= trigger.threshold

    if trigger.kind == KIND_UNDERLYING_BEYOND:
        entry = trade.entry_spot
        if not entry or spot is None:
            return False
        if trigger.threshold < entry:
            return spot <= trigger.threshold
        if trigger.threshold > entry:
            return spot >= trigger.threshold
        return False

    if trigger.kind == KIND_IV_SPIKE:
        # An IV that will not solve is not an IV that spiked. The exits that
        # matter most (max loss, DTE) do not depend on it.
        return iv is not None and iv >= trigger.threshold

    if trigger.kind == KIND_CREDIT_DECAY:
        if credit_total <= 0:
            return False
        return pnl >= trigger.threshold * credit_total

    logger.warning("Unknown trigger kind %r on %s — not firing",
                   trigger.kind, trade.order_id)
    return False


def _max_loss_dollars(intent, contracts: int) -> float:
    """`intent.max_loss` is total dollars for `intent.contracts`; rescale it
    to the size actually opened."""
    per_contract = intent.max_loss / max(getattr(intent, "contracts", 1) or 1, 1)
    return per_contract * contracts


# ── the pass ────────────────────────────────────────────────

def _record(journal, entry_type: str, payload: dict) -> None:
    """Append one journal entry. A journal failure must NEVER break an exit.

    Same contract and rationale as `executor_options._record` and
    `committee.decide._record`: once a closing order exists at the broker, an
    exception here would make a completed action look to the caller like it
    never happened, and the position would be closed a second time.
    """
    if journal is None:
        return
    try:
        journal.append(entry_type, payload)
    except Exception as e:  # noqa: BLE001 - auditing must not break managing
        logger.error("Journal write failed for %s: %s", entry_type, e, exc_info=True)


def _spot_lookup(data):
    cache: dict[str, float | None] = {}

    def get(underlying: str) -> float:
        root = str(underlying).upper()
        if root not in cache:
            bars = data.get_stock_bars(root, days=5)
            cache[root] = (None if bars is None or bars.empty
                           else float(bars["close"].iloc[-1]))
        spot = cache[root]
        if spot is None or spot <= 0:
            raise _Unmeasurable(f"no usable spot for {root}")
        return spot

    return get


def monitor_positions(cli, data, guard, journal, triggers_by_order,
                      poll_seconds: int = 30, clock=time.monotonic,
                      sleep=time.sleep, today: date | None = None
                      ) -> list[ExitEvent]:
    """Evaluate every open trade's exits and close the ones that have tripped.

    `triggers_by_order` maps a broker order id to the `OpenTrade` record for
    that position — its `TradeIntent` (needed to build the inverted closing
    order), its pre-mortem triggers, the approved contract count and the spot
    at entry. The parameter keeps the name it has in the design because that
    is what it is *for*; it carries the intent alongside because there is no
    way to close a multi-leg position without knowing its legs.

    Returns one `ExitEvent` per trade. Never raises: a failure anywhere is an
    event carrying `error`, with the position left open and still monitored.
    """
    killed, why = guard.kill_switch_active()
    if killed:
        logger.warning("Exit monitoring halted — %s", why)
        _record(journal, "monitor_halted", {"reason": why,
                                            "open_trades": len(triggers_by_order)})
        return []

    try:
        positions = cli.list_positions()
    except Exception as e:  # noqa: BLE001 - an unreadable book manages nothing
        logger.error("Could not read positions — no exits evaluated: %s", e)
        _record(journal, "monitor_error", {"error": f"position list failed: {e}"})
        return []

    rows_by_symbol = {str(row.get("symbol", "")).upper(): row
                      for row in (positions or [])}
    spot_for = _spot_lookup(data)
    when = today or date.today()

    events = []
    for order_id, trade in (triggers_by_order or {}).items():
        try:
            events.append(_evaluate(cli, journal, spot_for, rows_by_symbol,
                                    order_id, trade, when, poll_seconds,
                                    clock, sleep))
        except _Unmeasurable as e:
            events.append(_error_event(order_id, trade, str(e)))
        except Exception as e:  # noqa: BLE001 - one bad trade must not blind the rest
            logger.error("Exit evaluation failed for %s: %s", order_id, e,
                         exc_info=True)
            events.append(_error_event(order_id, trade,
                                       f"{type(e).__name__}: {e}"))
    return events


def _describe(trade) -> tuple[str, str]:
    intent = getattr(trade, "intent", None)
    return (str(getattr(intent, "underlying", "?")),
            str(getattr(intent, "structure", "?")))


def _error_event(order_id, trade, error: str) -> ExitEvent:
    underlying, structure = _describe(trade)
    logger.warning("Position %s could not be managed this pass: %s",
                   order_id, error)
    return ExitEvent(order_id=order_id, underlying=underlying,
                     structure=structure, closed=False, still_open=True,
                     reason="not evaluated", error=error)


def _evaluate(cli, journal, spot_for, rows_by_symbol, order_id, trade, when,
              poll_seconds, clock, sleep) -> ExitEvent:
    intent = trade.intent
    underlying, structure = _describe(trade)
    if not getattr(intent, "legs", None):
        raise _Unmeasurable("the stored trade has no legs to close")

    # Gone entirely: expired worthless, closed by hand, or assigned away. Not
    # an error — just nothing left to manage.
    symbols = [str(leg.quote.symbol).upper() for leg in intent.legs]
    if not any(s in rows_by_symbol for s in symbols):
        logger.info("Position %s (%s %s) is no longer in the book",
                    order_id, underlying, structure)
        return ExitEvent(order_id=order_id, underlying=underlying,
                         structure=structure, closed=False, still_open=False,
                         reason="position is no longer in the book")

    marks = _leg_marks(intent, rows_by_symbol)
    spot = spot_for(underlying)
    dte = _dte_of(intent, when)
    contracts = max(int(trade.contracts or 1), 1)

    closing_credit = close_net_credit(intent, marks)
    pnl = realized_pnl(intent, contracts, closing_credit)
    credit_total = intent.net_credit * CONTRACT_MULTIPLIER * contracts
    iv = _position_iv(intent, marks, spot, dte)

    # Max loss first: it is the most urgent condition and the one the whole
    # defined-risk premise rests on.
    if pnl <= -_max_loss_dollars(intent, contracts):
        return _close(cli, journal, order_id, trade, contracts, closing_credit,
                      pnl, None, MAX_LOSS_REASON, poll_seconds, clock, sleep)

    for trigger in _all_triggers(trade):
        if _fires(trigger, trade, spot, dte, iv, pnl, credit_total):
            reason = f"{trigger.kind} at {trigger.threshold:g} fired"
            if trigger.rationale:
                reason = f"{reason}: {trigger.rationale}"
            return _close(cli, journal, order_id, trade, contracts,
                          closing_credit, pnl, trigger, reason, poll_seconds,
                          clock, sleep)

    return ExitEvent(
        order_id=order_id, underlying=underlying, structure=structure,
        closed=False, still_open=True,
        reason=(f"no exit trigger fired (spot {spot:.2f}, {dte} DTE, "
                f"unrealized ${pnl:+,.2f})"),
        realized_pnl=None,
    )


def _close(cli, journal, order_id, trade, contracts, closing_credit, pnl,
           trigger, reason, poll_seconds, clock, sleep) -> ExitEvent:
    """Submit the inverted multi-leg order and journal what happened."""
    intent = trade.intent
    underlying, structure = _describe(trade)

    # Alpaca's signed limit price: positive = net debit paid, negative = net
    # credit received. We receive `closing_credit` per share on the unwind,
    # so the wire price is its negation — the same inversion
    # `options_orders.build_mleg_payload` applies when opening.
    payload = closing_payload(intent, contracts, -closing_credit)
    _record(journal, "exit_signal", {
        "order_id": order_id, "underlying": underlying, "structure": structure,
        "reason": reason, "trigger": _trigger_payload(trigger),
        "estimated_pnl": pnl, "payload": payload,
    })

    try:
        order = cli.post_order(payload)
    except Exception as e:  # noqa: BLE001 - a rejection is a result, not a crash
        logger.error("Closing order rejected for %s: %s", order_id, e)
        _record(journal, "exit_rejected", {
            "order_id": order_id, "underlying": underlying,
            "structure": structure, "reason": reason, "error": str(e),
        })
        return ExitEvent(order_id=order_id, underlying=underlying,
                         structure=structure, closed=False, still_open=True,
                         reason=reason, trigger=trigger, error=str(e))

    close_order_id = str((order or {}).get("id", ""))
    last = _poll(cli, close_order_id, poll_seconds, clock, sleep)
    status = str(last.get("status", "new"))

    if status not in TERMINAL_FILLED:
        # Nothing has been realized. Writing a P&L for a working order would
        # feed the calibration loop a number that never happened.
        logger.warning("Closing order %s for %s is %s, not filled",
                       close_order_id, order_id, status)
        _record(journal, "exit_pending", {
            "order_id": order_id, "close_order_id": close_order_id,
            "underlying": underlying, "structure": structure,
            "status": status, "reason": reason,
        })
        return ExitEvent(order_id=order_id, underlying=underlying,
                         structure=structure, closed=False, still_open=True,
                         reason=f"{reason}; closing order is {status}",
                         trigger=trigger, close_order_id=close_order_id,
                         error="" if status not in TERMINAL_DEAD
                               else f"closing order {status}")

    filled_credit = _filled_credit(last, closing_credit)
    final_pnl = realized_pnl(intent, contracts, filled_credit)

    # THE KEYSTONE ENTRY. `snapshot_hash` is what lets
    # calibration.resolved_predictions join this outcome back to the
    # analyst_view entries of the cycle that produced the trade.
    _record(journal, "close", {
        "realized_pnl": float(final_pnl),
        "underlying": underlying,
        "structure": structure,
        "snapshot_hash": trade.snapshot_hash,
        "exit_reason": reason,
        "trigger": _trigger_payload(trigger),
        "order_id": order_id,
        "close_order_id": close_order_id,
        "contracts": contracts,
        "open_net_credit": intent.net_credit,
        "close_net_credit": filled_credit,
    })
    logger.info("Closed %s (%s %s): realized $%+,.2f — %s",
                order_id, underlying, structure, final_pnl, reason)
    return ExitEvent(order_id=order_id, underlying=underlying,
                     structure=structure, closed=True, still_open=False,
                     reason=reason, trigger=trigger,
                     realized_pnl=float(final_pnl),
                     close_order_id=close_order_id)


def _trigger_payload(trigger: ExitTrigger | None) -> dict | None:
    if trigger is None:
        return None
    return {"kind": trigger.kind, "threshold": trigger.threshold,
            "rationale": trigger.rationale}


def _filled_credit(order: dict, estimate: float) -> float:
    """The closing credit actually realized, preferring the broker's fill.

    `filled_avg_price` is the truth about what happened, but Alpaca's sign
    convention for a multi-leg average fill is not something to assume: so
    the magnitude is taken from the broker and the SIGN from whichever of
    +price / -price sits closer to the mark-derived estimate. That keeps the
    realized number honest without betting the P&L on an unverified
    convention, and it degrades to the estimate when no fill price is given.
    """
    price = _float(order.get("filled_avg_price"))
    if price is None or price == 0:
        return estimate
    return min((-abs(price), abs(price)), key=lambda c: abs(c - estimate))


def _poll(cli, order_id: str, poll_seconds: int, clock, sleep) -> dict:
    """Poll the closing order until terminal or the window elapses."""
    if not order_id:
        return {"status": "unknown"}
    deadline = clock() + poll_seconds
    last: dict = {"status": "new"}
    while True:
        try:
            last = cli.get_order(order_id) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not read closing order %s: %s", order_id, e)
            return last
        status = str(last.get("status", "new"))
        if status in TERMINAL_FILLED or status in TERMINAL_DEAD:
            return last
        if clock() >= deadline:
            return last
        sleep(1)


# ── persisting the open book between processes ──────────────
#
# Each `make session` run is a fresh process, so a position opened by one
# cycle is invisible to the next unless the desk writes down what it opened.
# The broker's own position rows are not enough: they carry the symbols and
# the marks, but not the structure, not the credit received at entry, not the
# pre-mortem's triggers, and not the snapshot_hash that attributes the
# eventual outcome back to the analysts who chose the trade. Without those
# last two the exit is uninformed and the calibration join is impossible.
#
# JSON rather than pickle, deliberately: this file is read by a later process
# and is worth being able to inspect by eye during a live session.

def _quote_record(q: OptionQuote) -> dict:
    return {"symbol": q.symbol, "underlying": q.underlying, "strike": q.strike,
            "expiry": q.expiry.isoformat(), "right": q.right, "bid": q.bid,
            "ask": q.ask, "open_interest": q.open_interest}


def _quote_from_record(r: dict) -> OptionQuote:
    return OptionQuote(
        symbol=str(r["symbol"]), underlying=str(r["underlying"]),
        strike=float(r["strike"]), expiry=date.fromisoformat(str(r["expiry"])),
        right=str(r["right"]), bid=float(r["bid"]), ask=float(r["ask"]),
        open_interest=int(r["open_interest"]),
    )


def trade_record(trade: OpenTrade) -> dict:
    """One `OpenTrade` as plain JSON-serialisable data."""
    intent = trade.intent
    return {
        "order_id": trade.order_id,
        "contracts": int(trade.contracts),
        "entry_spot": float(trade.entry_spot),
        "snapshot_hash": trade.snapshot_hash,
        "opened_at": trade.opened_at,
        "triggers": [{"kind": t.kind, "threshold": t.threshold,
                      "rationale": t.rationale} for t in trade.triggers],
        "intent": {
            "underlying": intent.underlying,
            "structure": intent.structure,
            "contracts": intent.contracts,
            "net_credit": intent.net_credit,
            "max_loss": intent.max_loss,
            "max_profit": intent.max_profit,
            "breakevens": list(intent.breakevens),
            "dte": intent.dte,
            "rationale": intent.rationale,
            "legs": [{"quote": _quote_record(leg.quote), "side": leg.side,
                      "contracts": leg.contracts} for leg in intent.legs],
        },
    }


def trade_from_record(record: dict) -> OpenTrade:
    """Rebuild an `OpenTrade`. Raises on anything it cannot fully restore.

    Raising is right here and swallowing is not: a half-restored intent would
    build a *wrong* closing order — the wrong strikes, the wrong sides, or a
    max_loss that no longer matches the position. `OpenTradeStore.load` turns
    the raise into "drop this one record, keep the rest, log loudly".
    """
    i = record["intent"]
    intent = TradeIntent(
        underlying=str(i["underlying"]), structure=str(i["structure"]),
        legs=tuple(Leg(quote=_quote_from_record(l["quote"]), side=str(l["side"]),
                       contracts=int(l["contracts"])) for l in i["legs"]),
        contracts=int(i["contracts"]), net_credit=float(i["net_credit"]),
        max_loss=float(i["max_loss"]), max_profit=float(i["max_profit"]),
        breakevens=tuple(float(b) for b in i["breakevens"]),
        dte=int(i["dte"]), rationale=str(i.get("rationale", "")),
    )
    return OpenTrade(
        order_id=str(record["order_id"]), intent=intent,
        triggers=tuple(ExitTrigger(kind=str(t["kind"]),
                                   threshold=float(t["threshold"]),
                                   rationale=str(t.get("rationale", "")))
                       for t in record.get("triggers", [])),
        contracts=int(record.get("contracts", 1)),
        entry_spot=float(record.get("entry_spot", 0.0)),
        snapshot_hash=str(record.get("snapshot_hash", "")),
        opened_at=str(record.get("opened_at", "")),
    )


class OpenTradeStore:
    """The desk's memory of what it currently has open.

    Never raises on read. A missing file is an empty book; a corrupt file or
    an unrestorable record is logged at error level and skipped, because a
    session that crashes on its own state file cannot manage anything at all
    — while a session that silently drops a record leaves a position
    unmonitored, so the log must be loud enough to notice.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load(self) -> list[OpenTrade]:
        if not self.path.exists():
            return []
        try:
            records = json.loads(self.path.read_text() or "[]")
        except (OSError, ValueError) as e:
            logger.error("Open trade store %s is unreadable (%s) — treating the "
                         "book as empty; open positions will NOT be monitored "
                         "this cycle", self.path, e)
            return []
        if not isinstance(records, list):
            logger.error("Open trade store %s is a %s, expected a list — "
                         "treating the book as empty", self.path,
                         type(records).__name__)
            return []

        trades = []
        for record in records:
            try:
                trades.append(trade_from_record(record))
            except Exception as e:  # noqa: BLE001 - one bad record, not all of them
                logger.error("Unrestorable open trade record %r (%s) — skipped; "
                             "that position will not be monitored",
                             (record or {}).get("order_id", "?"), e)
        return trades

    def save(self, trades) -> None:
        """Overwrite the store. A write failure is logged, never raised: the
        position exists at the broker whatever this file says."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(
                [trade_record(t) for t in trades], indent=2, default=str))
        except Exception as e:  # noqa: BLE001
            logger.error("Could not write the open trade store %s: %s — the "
                         "next cycle may not know about an open position",
                         self.path, e, exc_info=True)
