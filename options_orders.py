"""
Convert a TradeIntent into an Alpaca multi-leg ("mleg") order payload.

Pure functions, no I/O — this is where the broker's shape and sign rules are
encoded, and encoding them wrong is silently expensive:

  * 2-4 legs, symbols unique, time_in_force "day" only
  * no top-level symbol/side on a multi-leg order (Alpaca rejects it)
  * limit_price is signed: POSITIVE = net debit paid, NEGATIVE = net credit
    received. Closing a debit spread therefore takes a negative limit price.
"""
import hashlib
from datetime import date, datetime, timezone

from candidate_builder import TradeIntent

MIN_LEGS, MAX_LEGS = 2, 4

# Alpaca accepts up to 128 chars; 32 hex characters is 128 bits of collision
# resistance and stays comfortably inside every logging surface.
CLIENT_ORDER_ID_CHARS = 32

_OPEN_INTENT = {"buy": "buy_to_open", "sell": "sell_to_open"}
_CLOSE_INTENT = {"buy": "buy_to_close", "sell": "sell_to_close"}
_OPPOSITE = {"buy": "sell", "sell": "buy"}


def _validate(intent: TradeIntent, contracts: int) -> None:
    if contracts <= 0:
        raise ValueError(f"contracts must be positive, got {contracts}")
    if not (MIN_LEGS <= len(intent.legs) <= MAX_LEGS):
        raise ValueError(
            f"Alpaca accepts 2 to 4 legs, got {len(intent.legs)}"
        )
    symbols = [leg.quote.symbol for leg in intent.legs]
    if len(set(symbols)) != len(symbols):
        raise ValueError(f"multi-leg order requires unique symbols, got {symbols}")


def _base(contracts: int, limit_price: float) -> dict:
    return {
        "order_class": "mleg",
        "type": "limit",
        "time_in_force": "day",          # the only TIF Alpaca allows for mleg
        "qty": str(contracts),
        "limit_price": f"{limit_price:.2f}",
    }


def client_order_id(intent: TradeIntent, on: date | None = None) -> str:
    """Deterministic broker-side identity for "this trade, today".

    Alpaca rejects a second order carrying a client_order_id it has already
    seen. That makes submission idempotent: a re-run of the session, an
    operator retry, or a crash after the POST reached Alpaca all collapse to
    one order plus a visible duplicate rejection, instead of two identical
    live spreads that can both fill.

    Deliberately keyed on the trade's *identity* only — underlying, structure,
    leg symbols, date. Contract count is excluded, because a downsized retry of
    a spread already working is the same trade, not a new one; and the date is
    included so yesterday's identical spread cannot block today's.

    The date is UTC, not local — never `date.today()`. The live session runs
    19:00-01:30 IST, which straddles LOCAL midnight while the per-day cap in
    scripts/run_session.py is keyed on the UTC date; a local-date id would
    silently change mid-session while the UTC day has not, in exactly the
    window where broker-side duplicate-order protection matters most.
    """
    key = "|".join((
        intent.underlying,
        intent.structure,
        "|".join(sorted(leg.quote.symbol for leg in intent.legs)),
        (on or datetime.now(timezone.utc).date()).isoformat(),
    ))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:CLIENT_ORDER_ID_CHARS]


# Zero by DEFAULT so that replay, the judge scenarios and every decision-level
# test keep pricing at the theoretical mid: what the desk DECIDES must not
# move because of how we choose to get filled. The live submission path opens
# the concession explicitly (see scripts/run_session.py), which is the only
# place a real order is priced.
DEFAULT_CONCESSION = 0.0
LIVE_CONCESSION = 0.20


def build_mleg_payload(
    intent: TradeIntent, contracts: int, limit_price: float | None = None,
    concession: float = DEFAULT_CONCESSION,
) -> dict:
    """Opening order. Credit structures get a negative limit price.

    `concession` is the fraction of the theoretical edge we give up to become
    marketable, ignored when `limit_price` is passed explicitly.

    Why it is not zero: `intent.net_credit` is computed from quote MIDS, and a
    mid-priced limit on a multi-leg spread only fills if somebody crosses to
    us. On 2026-08-31 a live order asking the full $1.96 mid credit rested 55
    minutes against a market paying $1.63, and — because the desk refuses to
    stack a second order on one underlying — it blocked every later cycle of
    the session. Conceding a slice of the credit is the difference between an
    order that prices correctly and an order that trades.

    The concession only ever moves the limit TOWARD the market: it shrinks a
    credit we demand, or raises a debit we will pay. It can never turn a
    credit structure into one we pay to open, however large it is set.
    """
    _validate(intent, contracts)
    if concession < 0:
        raise ValueError(f"concession must be >= 0, got {concession}")
    # intent.net_credit is per share, positive for a credit. Alpaca wants the
    # signed net price: negative to receive a credit, positive to pay a debit.
    if limit_price is None:
        edge = intent.net_credit
        if edge >= 0:
            # A credit: ask for less of it, but never below zero, which would
            # mean paying to open a structure whose whole point is the credit.
            price = -max(0.0, edge * (1.0 - concession))
        else:
            # A debit (net_credit negative): be willing to pay a little more.
            price = abs(edge) * (1.0 + concession)
    else:
        price = limit_price
    payload = _base(contracts, price)
    payload["client_order_id"] = client_order_id(intent)
    payload["legs"] = [
        {
            "symbol": leg.quote.symbol,
            "side": leg.side,
            "ratio_qty": "1",
            "position_intent": _OPEN_INTENT[leg.side],
        }
        for leg in intent.legs
    ]
    return payload


def closing_payload(intent: TradeIntent, contracts: int, limit_price: float) -> dict:
    """Closing order: every side inverted, close intents, caller sets the price."""
    _validate(intent, contracts)
    payload = _base(contracts, limit_price)
    payload["legs"] = [
        {
            "symbol": leg.quote.symbol,
            "side": _OPPOSITE[leg.side],
            "ratio_qty": "1",
            "position_intent": _CLOSE_INTENT[_OPPOSITE[leg.side]],
        }
        for leg in intent.legs
    ]
    return payload
