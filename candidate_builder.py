"""
Deterministic options candidate builder.

Builds fully-specified TradeIntent candidates — every strike, side, contract
count and price is computed here, in code. LLMs downstream may only *choose*
among these candidates or ABSTAIN; they never invent order parameters
(CLAUDE.md hard rule 1).

Only defined-risk structures are constructible (hard rule 3):
  - bull put credit spread   (bullish / neutral)
  - bear call credit spread  (bearish / neutral)
  - iron condor              (neutral, range-bound)
  - long straddle            (long volatility)
There is no code path that produces a naked short option.

Builders return None when a leg fails the liquidity gate — a missing candidate,
never a marginal one (hard rule 2, fail closed). They raise ValueError on
structurally invalid input, which is a programming error, not a market condition.

Pricing convention: per-share dollars for credits/debits, total position dollars
for max_loss / max_profit. One contract = 100 shares.
"""
import logging
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)

CONTRACT_MULTIPLIER = 100

# Liquidity gates — mirrored in risk.yaml, which is the source of truth for
# the guard. These are the builder's pre-filter so bad candidates never form.
MAX_SPREAD_PCT_OF_MID = 0.10
MIN_OPEN_INTEREST = 100
MIN_DTE = 7
MAX_DTE = 45


@dataclass(frozen=True)
class OptionQuote:
    """A single option contract's market state."""
    symbol: str          # OCC symbol
    underlying: str
    strike: float
    expiry: date
    right: str           # "c" or "p"
    bid: float
    ask: float
    open_interest: int
    # The date this quote was observed. `None` means "now", which is the only
    # correct answer for a live chain fetch and stays the default so no live
    # call site changes. A HISTORICAL quote (scripts/seed_calibration.py
    # replays past decision dates) must carry the date it was observed on,
    # because every DTE-dependent gate below — the 7-45 DTE window, the IV
    # solve, the pre-mortem's 3-DTE exit — is derived from `dte`, and
    # measuring a June expiry against today's calendar would make every
    # replayed candidate fail the gate for a reason that has nothing to do
    # with the market on the day it is replaying.
    as_of: date | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_pct(self) -> float:
        """Bid-ask spread as a fraction of mid. A zero mid is untradeable."""
        mid = self.mid
        if mid <= 0:
            return float("inf")
        return (self.ask - self.bid) / mid

    @property
    def dte(self) -> int:
        return (self.expiry - (self.as_of or date.today())).days


@dataclass(frozen=True)
class Leg:
    """One leg of an order: what to do with which contract, and how many."""
    quote: OptionQuote
    side: str            # "buy" or "sell"
    contracts: int


@dataclass(frozen=True)
class TradeIntent:
    """A fully-specified, defined-risk options order."""
    underlying: str
    structure: str
    legs: tuple[Leg, ...]
    contracts: int
    net_credit: float                 # per share; negative = net debit paid
    max_loss: float                   # total position dollars, always finite
    max_profit: float                 # total position dollars; inf for straddle
    breakevens: tuple[float, ...]
    dte: int
    rationale: str = ""

    @property
    def is_credit(self) -> bool:
        return self.net_credit > 0

    @property
    def is_defined_risk(self) -> bool:
        """Finite max loss and every short leg covered by a long of the same right."""
        if self.max_loss == float("inf"):
            return False
        longs = [l for l in self.legs if l.side == "buy"]
        return all(
            any(l.quote.right == s.quote.right for l in longs)
            for s in self.legs if s.side == "sell"
        )


def passes_liquidity(quote: OptionQuote) -> bool:
    """Liquidity + tenor gate. Every leg of every candidate must pass."""
    if quote.open_interest < MIN_OPEN_INTEREST:
        return False
    if quote.spread_pct > MAX_SPREAD_PCT_OF_MID:
        return False
    if not (MIN_DTE <= quote.dte <= MAX_DTE):
        return False
    return True


def _gate(*quotes: OptionQuote) -> bool:
    """All legs must pass liquidity, else no candidate is produced."""
    for q in quotes:
        if not passes_liquidity(q):
            logger.debug(
                "Candidate rejected: %s fails liquidity (oi=%d spread=%.1f%% dte=%d)",
                q.symbol, q.open_interest, q.spread_pct * 100, q.dte,
            )
            return False
    return True


def _vertical_credit_spread(
    short: OptionQuote,
    long: OptionQuote,
    structure: str,
    contracts: int,
) -> TradeIntent | None:
    """Shared math for the two credit verticals."""
    if not _gate(short, long):
        return None

    credit = short.mid - long.mid
    width = abs(short.strike - long.strike)
    max_loss = (width - credit) * CONTRACT_MULTIPLIER * contracts
    max_profit = credit * CONTRACT_MULTIPLIER * contracts

    if structure == "bull_put_spread":
        breakevens = (short.strike - credit,)
    else:  # bear_call_spread
        breakevens = (short.strike + credit,)

    return TradeIntent(
        underlying=short.underlying,
        structure=structure,
        legs=(Leg(short, "sell", contracts), Leg(long, "buy", contracts)),
        contracts=contracts,
        net_credit=credit,
        max_loss=max_loss,
        max_profit=max_profit,
        breakevens=breakevens,
        dte=short.dte,
    )


def build_bull_put_spread(
    short_put: OptionQuote, long_put: OptionQuote, contracts: int = 1
) -> TradeIntent | None:
    """Sell the higher-strike put, buy the lower-strike put. Bullish/neutral."""
    if short_put.right != "p" or long_put.right != "p":
        raise ValueError("bull put spread requires two puts")
    if short_put.strike <= long_put.strike:
        raise ValueError(
            f"bull put spread sells the higher strike "
            f"(got short={short_put.strike}, long={long_put.strike})"
        )
    if short_put.expiry != long_put.expiry:
        raise ValueError("vertical spread legs must share an expiry")
    return _vertical_credit_spread(short_put, long_put, "bull_put_spread", contracts)


def build_bear_call_spread(
    short_call: OptionQuote, long_call: OptionQuote, contracts: int = 1
) -> TradeIntent | None:
    """Sell the lower-strike call, buy the higher-strike call. Bearish/neutral."""
    if short_call.right != "c" or long_call.right != "c":
        raise ValueError("bear call spread requires two calls")
    if short_call.strike >= long_call.strike:
        raise ValueError(
            f"bear call spread sells the lower strike "
            f"(got short={short_call.strike}, long={long_call.strike})"
        )
    if short_call.expiry != long_call.expiry:
        raise ValueError("vertical spread legs must share an expiry")
    return _vertical_credit_spread(short_call, long_call, "bear_call_spread", contracts)


def build_iron_condor(
    short_put: OptionQuote,
    long_put: OptionQuote,
    short_call: OptionQuote,
    long_call: OptionQuote,
    contracts: int = 1,
) -> TradeIntent | None:
    """Bull put spread + bear call spread. Neutral, defined risk on both sides."""
    if short_put.right != "p" or long_put.right != "p":
        raise ValueError("iron condor put wing requires two puts")
    if short_call.right != "c" or long_call.right != "c":
        raise ValueError("iron condor call wing requires two calls")
    if short_put.strike <= long_put.strike:
        raise ValueError("put wing sells the higher strike")
    if short_call.strike >= long_call.strike:
        raise ValueError("call wing sells the lower strike")
    if short_call.strike <= short_put.strike:
        raise ValueError(
            f"iron condor wings overlap: short call {short_call.strike} "
            f"must sit above short put {short_put.strike}"
        )
    if len({short_put.expiry, long_put.expiry, short_call.expiry, long_call.expiry}) != 1:
        raise ValueError("iron condor legs must share an expiry")

    if not _gate(short_put, long_put, short_call, long_call):
        return None

    credit = (short_put.mid - long_put.mid) + (short_call.mid - long_call.mid)
    # Only one wing can finish in the money, so risk is the wider wing's width.
    width = max(short_put.strike - long_put.strike, long_call.strike - short_call.strike)
    max_loss = (width - credit) * CONTRACT_MULTIPLIER * contracts

    return TradeIntent(
        underlying=short_put.underlying,
        structure="iron_condor",
        legs=(
            Leg(short_put, "sell", contracts), Leg(long_put, "buy", contracts),
            Leg(short_call, "sell", contracts), Leg(long_call, "buy", contracts),
        ),
        contracts=contracts,
        net_credit=credit,
        max_profit=credit * CONTRACT_MULTIPLIER * contracts,
        max_loss=max_loss,
        breakevens=(short_put.strike - credit, short_call.strike + credit),
        dte=short_put.dte,
    )


def build_long_straddle(
    call: OptionQuote, put: OptionQuote, contracts: int = 1
) -> TradeIntent | None:
    """Buy call + buy put at the same strike. Long volatility.

    Risk is defined by construction: the most that can be lost is the premium.
    """
    if call.right != "c" or put.right != "p":
        raise ValueError("long straddle requires one call and one put")
    if call.strike != put.strike:
        raise ValueError(
            f"straddle legs must share a strike (got {call.strike} / {put.strike})"
        )
    if call.expiry != put.expiry:
        raise ValueError("straddle legs must share an expiry")

    if not _gate(call, put):
        return None

    debit = call.mid + put.mid
    return TradeIntent(
        underlying=call.underlying,
        structure="long_straddle",
        legs=(Leg(call, "buy", contracts), Leg(put, "buy", contracts)),
        contracts=contracts,
        net_credit=-debit,
        max_loss=debit * CONTRACT_MULTIPLIER * contracts,
        max_profit=float("inf"),
        breakevens=(call.strike - debit, call.strike + debit),
        dte=call.dte,
    )
