"""
Deterministic options candidate builder.

Builds fully-specified TradeIntent candidates — every strike, side, contract
count and price is computed here, in code. LLMs downstream may only *choose*
among these candidates or ABSTAIN; they never invent order parameters
(CLAUDE.md hard rule 1).

Only defined-risk structures are constructible (hard rule 3):
  - bull put credit spread   (bullish / neutral, SHORT premium)
  - bear call credit spread  (bearish / neutral, SHORT premium)
  - iron condor              (neutral, range-bound, SHORT premium)
  - long straddle            (long volatility, LONG premium)
  - bull call debit spread   (bullish, LONG premium)
  - bear put debit spread    (bearish, LONG premium)
  - long iron butterfly      (long volatility, NEUTRAL, LONG premium)
There is no code path that produces a naked short option.

The two debit verticals exist because the long-premium half of the menu was
otherwise unreachable. Measured over a 43-window replay of post-cutoff data,
the desk abstained in 31 windows and 23 of those shared one cause: the only
long-premium structure was the long straddle, which at SPY's price costs
$2,270-$2,639 in max loss against risk.yaml's $1,000 max_loss_per_position,
so `committee/snapshot._drop_certain_denials` removed it from EVERY window.
The surfaced menu was therefore 100% short premium during a stretch where
implied vol sat below realized — the regime that argues for BUYING premium.
The desk refused correctly and had nothing else to offer. A 5-wide debit
vertical costs only the debit paid (typically $150-$350), so the same view
now has an expressible, cap-clearing structure.

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


def _vertical_debit_spread(
    long: OptionQuote,
    short: OptionQuote,
    structure: str,
    contracts: int,
) -> TradeIntent | None:
    """Shared math for the two debit verticals.

    Sign convention matches `build_long_straddle`: a debit paid is rendered as
    a NEGATIVE `net_credit`, so every downstream consumer that reads the sign
    (options_orders' limit price, exit_monitor's P&L) works unchanged.

    Risk is the premium paid; reward is the width less that premium. The two
    always sum to the full width, which is the identity worth remembering:
    a debit vertical is the mirror image of the credit vertical struck at the
    same two strikes.
    """
    if not _gate(long, short):
        return None

    debit = long.mid - short.mid
    width = abs(long.strike - short.strike)
    max_loss = debit * CONTRACT_MULTIPLIER * contracts
    max_profit = (width - debit) * CONTRACT_MULTIPLIER * contracts

    if structure == "bull_call_spread":
        breakevens = (long.strike + debit,)
    else:  # bear_put_spread
        breakevens = (long.strike - debit,)

    return TradeIntent(
        underlying=long.underlying,
        structure=structure,
        legs=(Leg(long, "buy", contracts), Leg(short, "sell", contracts)),
        contracts=contracts,
        net_credit=-debit,
        max_loss=max_loss,
        max_profit=max_profit,
        breakevens=breakevens,
        dte=long.dte,
    )


def build_bull_call_spread(
    long_call: OptionQuote, short_call: OptionQuote, contracts: int = 1
) -> TradeIntent | None:
    """Buy the lower-strike call, sell the higher-strike call. Bullish, LONG
    premium. Max loss is the debit paid; max profit is width minus debit."""
    if long_call.right != "c" or short_call.right != "c":
        raise ValueError("bull call spread requires two calls")
    if long_call.strike >= short_call.strike:
        raise ValueError(
            f"bull call spread buys the lower strike "
            f"(got long={long_call.strike}, short={short_call.strike})"
        )
    if long_call.expiry != short_call.expiry:
        raise ValueError("vertical spread legs must share an expiry")
    return _vertical_debit_spread(long_call, short_call, "bull_call_spread", contracts)


def build_bear_put_spread(
    long_put: OptionQuote, short_put: OptionQuote, contracts: int = 1
) -> TradeIntent | None:
    """Buy the higher-strike put, sell the lower-strike put. Bearish, LONG
    premium. Max loss is the debit paid; max profit is width minus debit."""
    if long_put.right != "p" or short_put.right != "p":
        raise ValueError("bear put spread requires two puts")
    if long_put.strike <= short_put.strike:
        raise ValueError(
            f"bear put spread buys the higher strike "
            f"(got long={long_put.strike}, short={short_put.strike})"
        )
    if long_put.expiry != short_put.expiry:
        raise ValueError("vertical spread legs must share an expiry")
    return _vertical_debit_spread(long_put, short_put, "bear_put_spread", contracts)


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


def build_long_iron_butterfly(
    long_call: OptionQuote,
    long_put: OptionQuote,
    short_call: OptionQuote,
    short_put: OptionQuote,
    contracts: int = 1,
) -> TradeIntent | None:
    """Buy the ATM straddle, sell both wings. LONG premium, direction-NEUTRAL.

    Also called a reverse iron butterfly. It is the long-premium mirror of
    the credit iron butterfly, and equivalently a long call vertical
    (body -> call wing) plus a long put vertical (body -> put wing) sharing
    the body strike.

    WHY IT EXISTS. "Implied vol sits below realized, so buy premium" is a
    view about VOLATILITY, not about direction. Measured over the seeded
    June-July replay, the committee reached exactly that view repeatedly and
    then abstained anyway, in its own words: "vol_analyst cites both bullish
    (c7-c9) and bearish (c4-c6) debit spreads as equally favored by the vol
    regime ... With no two-model agreement on direction, ABSTAIN is the
    correct call." Both debit verticals are directional, so the two-reviewer
    directional-agreement rule could never be satisfied by a view that names
    no direction. The only non-directional long-premium structure the builder
    had was `long_straddle`, whose max_loss ($2,270-$2,639 on a live SPY
    chain) exceeds risk.yaml's $1,000 max_loss_per_position and is therefore
    dropped from every window before the committee sees it.

    Selling the two wings against that straddle is what makes the same view
    affordable: the premium received cuts the debit paid (and the max loss
    with it) to a few hundred dollars, at the cost of capping the upside at
    the wing width. Direction-neutral, defined risk, under the cap.

    PAYOFF (wings symmetric at +/- `width` from the body):
      max loss    = the net debit paid, realised when the underlying pins the
                    body and every leg expires worthless
      max profit  = (width - net debit), first reached AT either wing strike
                    and flat beyond it
      breakevens  = body +/- net debit
      the two always sum to the full width, exactly as they do for a vertical

    NO NAKED LEG: the short call wing sits above the long body call and the
    short put wing below the long body put, so each short is covered by a
    long of the same right (hard rule 3).

    Asymmetric wings are refused rather than priced: with unequal widths the
    max profit differs by side, so a single `width` would silently misstate
    one of them.
    """
    if long_call.right != "c" or short_call.right != "c":
        raise ValueError("long iron butterfly call legs must both be calls")
    if long_put.right != "p" or short_put.right != "p":
        raise ValueError("long iron butterfly put legs must both be puts")
    if long_call.strike != long_put.strike:
        raise ValueError(
            f"long iron butterfly body must be one strike "
            f"(got call={long_call.strike}, put={long_put.strike})"
        )
    body = long_call.strike
    if short_call.strike <= body:
        raise ValueError(
            f"call wing {short_call.strike} must sit above the body {body}")
    if short_put.strike >= body:
        raise ValueError(
            f"put wing {short_put.strike} must sit below the body {body}")
    call_width = short_call.strike - body
    put_width = body - short_put.strike
    if call_width != put_width:
        raise ValueError(
            f"long iron butterfly wings must be symmetric "
            f"(call wing {call_width}, put wing {put_width})"
        )
    if len({long_call.expiry, long_put.expiry,
            short_call.expiry, short_put.expiry}) != 1:
        raise ValueError("long iron butterfly legs must share an expiry")

    if not _gate(long_call, long_put, short_call, short_put):
        return None

    debit = (long_call.mid - short_call.mid) + (long_put.mid - short_put.mid)
    width = call_width

    # A debit at or above the width can never profit. That is a market
    # condition, not a programming error, so it is built honestly and dropped
    # downstream by committee.snapshot._drop_thin_debit rather than hidden
    # here — the same treatment credit structures get when they price to a
    # negligible or negative credit.
    return TradeIntent(
        underlying=long_call.underlying,
        structure="long_iron_butterfly",
        legs=(
            Leg(long_call, "buy", contracts), Leg(long_put, "buy", contracts),
            Leg(short_call, "sell", contracts), Leg(short_put, "sell", contracts),
        ),
        contracts=contracts,
        net_credit=-debit,
        max_loss=debit * CONTRACT_MULTIPLIER * contracts,
        max_profit=(width - debit) * CONTRACT_MULTIPLIER * contracts,
        breakevens=(body - debit, body + debit),
        dte=long_call.dte,
    )
