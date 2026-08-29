"""Pure logic for seeding the Brier calibration loop from real history.

`make calibration` is the project's one genuine differentiator — the desk
grades its own analysts — but a calibration loop with nothing resolved
reports zero predictions and a weight of exactly 1.0 for everybody. That is
the honest answer (calibration.py's docstring is emphatic about not papering
over it), and it is also invisible. Seeding fixes the visibility without
touching the honesty: it replays the REAL committee over REAL past market
data and scores it against what actually happened next.

Two constraints shape everything in this module.

**1. Contamination.** The site publicly argues — citing TradingAgents
(arXiv 2412.20138) and FinMem (arXiv 2311.13743) — that an LLM backtested
over dates inside its own training data may be recalling prices rather than
reasoning about them. The model's knowledge cutoff is **May 2026**, so every
replay window here must START on or after `KNOWLEDGE_CUTOFF` (2026-06-01).
`validate_window` refuses anything earlier and `decision_dates` drops
pre-cutoff days even if a caller hands them over. Producing FEWER windows is
always the correct response to running out of post-cutoff calendar; reaching
back in time would make the project's own central credibility claim false.

**2. Fail closed on data.** Historical option bars are OHLCV, not quotes,
and expired contracts are not enumerable through the trading API at all
(`TradingClient.get_option_contracts` returns nothing for a past expiry).
So OCC symbols are constructed directly and their daily bars fetched. Where
a bar does not exist, nothing is invented: `quote_from_bar` returns None,
`resolve_outcome` returns None, and the caller skips the whole window with a
logged reason. Two substitutions are made and both are named in the artifact
rather than hidden:

  * `bid == ask == close`. A daily bar records a traded price, not a spread.
    Widening it by a guessed amount would be fabricating the one number the
    liquidity gate reads, so the traded price is used as both sides and the
    spread gate is simply not exercised in replay.
  * `open_interest := bar volume`. Historical OI is not retrievable. Volume
    on the day is real, observed, and the closest available liquidity proxy;
    `candidate_builder.MIN_OPEN_INTEREST` then filters on it exactly as it
    would on OI.

Neither substitution can manufacture a favourable OUTCOME — outcomes come
from subsequent price action — but both are recorded so a judge can discount
them.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from candidate_builder import CONTRACT_MULTIPLIER, OptionQuote
from committee.premortem import DEFAULT_PROFIT_TARGET, FORCED_DTE_BELOW
from exit_monitor import close_net_credit, realized_pnl

logger = logging.getLogger(__name__)

#: The first date a replay window may start on. The model's knowledge cutoff
#: is May 2026; anything from 2026-06-01 onward post-dates the training data,
#: so a decision made on it cannot be recall dressed up as reasoning. Never
#: move this earlier to obtain more windows — produce fewer windows instead.
KNOWLEDGE_CUTOFF = date(2026, 6, 1)

#: Stamped into every journal payload this replay writes, so nothing in the
#: seeded journal can be mistaken for a real broker interaction.
SEED_SOURCE = "seed_replay"

#: Tenor band for the replayed expiry. Inside candidate_builder's 7-45 DTE
#: window with room on both sides, so a candidate is never rejected merely
#: for sitting on the boundary.
MIN_DTE = 21
MAX_DTE = 35
TARGET_DTE = 28

#: How the realized P&L of a window was established. Recorded per window in
#: the artifact, because they are not equally strong evidence.
METHOD_PROFIT_TARGET = "leg_bars_profit_target"
METHOD_FORCED_DTE = "leg_bars_forced_dte_exit"
METHOD_INTRINSIC = "intrinsic_at_expiry"


class ContaminationError(ValueError):
    """A requested window would run the committee over pre-cutoff dates."""


# ── the contamination guard ──────────────────────────────────

def validate_window(start: date, end: date) -> None:
    """Raise unless `[start, end]` lies entirely on or after the cutoff.

    This is the one hard rule of the seeding exercise, so it raises rather
    than clamping: silently moving a caller's start date forward would let a
    typo in a Makefile target quietly change what the artifact claims.
    """
    if end < start:
        raise ContaminationError(
            f"window end {end.isoformat()} precedes start {start.isoformat()}")
    if start < KNOWLEDGE_CUTOFF:
        raise ContaminationError(
            f"window starts {start.isoformat()}, before the "
            f"{KNOWLEDGE_CUTOFF.isoformat()} knowledge-cutoff boundary (the "
            f"model's training data runs to May 2026). Replaying the "
            f"committee over dates inside its own training data is exactly "
            f"the contamination criticism this project makes of "
            f"TradingAgents and FinMem. Use a later start, or accept fewer "
            f"windows.")


def decision_dates(trading_days, start: date, end: date, spacing: int,
                   max_windows: int) -> list[date]:
    """Thin a list of real trading days into the dates to replay.

    Pre-cutoff days are dropped here as well as refused in `validate_window`:
    a bar series fetched with a little lead-in (needed for a realized-vol
    window) legitimately contains May dates, and none of them may become a
    decision date.

    `spacing` exists so consecutive windows do not overlap into near-identical
    market states — twenty windows one day apart would be twenty views of the
    same fortnight, which would inflate the resolved count without adding
    independent evidence.
    """
    eligible = sorted(d for d in trading_days
                      if start <= d <= end and d >= KNOWLEDGE_CUTOFF)
    return eligible[::max(1, int(spacing))][:max(0, int(max_windows))]


# ── constructing the chain that the API will not enumerate ───

def occ_symbol(root: str, expiry: date, right: str, strike: float) -> str:
    """`{ROOT}{YYMMDD}{C|P}{strike*1000 zero-padded to 8}`.

    Verified against Alpaca on 2026-08-29: `SPY260821C00750000` returns real
    daily bars, while `get_option_contracts` returns nothing for that same
    expired expiry. Constructing the symbol is the only route to a historical
    chain.
    """
    return (f"{root.upper()}{expiry:%y%m%d}{right.upper()}"
            f"{int(round(strike * 1000)):08d}")


def strike_ladder(spot: float, width: float = 5.0, steps: int = 6) -> list[float]:
    """Strikes on a `width`-spaced grid bracketing `spot`.

    The grid is deliberately `width`-spaced rather than the underlying's real
    1-point increments: `scripts/run_session.build_candidates` pairs a short
    leg with the leg exactly `width` away, so a ladder off that grid would
    produce a chain from which no vertical spread can be assembled at all.
    """
    base = round(spot / width) * width
    ladder = [round(base + i * width, 4) for i in range(-steps, steps + 1)]
    return [s for s in ladder if s > 0]


def pick_expiry(as_of: date, available, min_dte: int = MIN_DTE,
                max_dte: int = MAX_DTE, target_dte: int = TARGET_DTE,
                max_expiry: date | None = None) -> date | None:
    """The expiry to trade on `as_of`, or None if the band is empty.

    `max_expiry` is the last date for which real price action exists. An
    expiry beyond it cannot be resolved from what actually happened next —
    only guessed at — so the window is refused here, BEFORE any LLM call is
    spent on it, rather than being replayed and then quietly settled early
    against a stale underlying price.

    Fridays are preferred because weekly and monthly expiries carry the deep
    liquidity, and a Friday expiry is what a live session would actually
    trade; within that preference the expiry nearest `target_dte` wins, with
    the date itself as the final tie-break so the choice is deterministic.
    Returning None (rather than stretching the band) is what makes a window
    get skipped instead of silently traded at a tenor the desk would refuse.
    """
    band = [e for e in available if min_dte <= (e - as_of).days <= max_dte
            and (max_expiry is None or e <= max_expiry)]
    if not band:
        return None
    return min(band, key=lambda e: (0 if e.weekday() == 4 else 1,
                                    abs((e - as_of).days - target_dte), e))


def quote_from_bar(root: str, expiry: date, right: str, strike: float,
                   close: float, volume, as_of: date) -> OptionQuote | None:
    """One `OptionQuote` from one real daily option bar, or None.

    See the module docstring for the two documented substitutions
    (`bid == ask == close`, `open_interest := volume`) and why neither is
    allowed to invent a price. A non-positive close is not a cheap option, it
    is an unusable bar, and it yields no quote at all.
    """
    try:
        close = float(close)
        volume = int(float(volume))
    except (TypeError, ValueError):
        return None
    if close <= 0:
        return None
    return OptionQuote(
        symbol=occ_symbol(root, expiry, right, strike),
        underlying=root.upper(),
        strike=float(strike),
        expiry=expiry,
        right=right.lower(),
        bid=close,
        ask=close,
        open_interest=max(0, volume),
        as_of=as_of,
    )


# ── resolving the outcome from real subsequent price action ──

def intrinsic(right: str, strike: float, spot: float) -> float:
    """Per-share settlement value of one option at `spot`. Never negative."""
    if right.lower() == "c":
        return max(0.0, spot - strike)
    return max(0.0, strike - spot)


@dataclass(frozen=True)
class WindowOutcome:
    """What actually happened to one replayed trade, and how we know."""
    realized_pnl: float
    method: str
    exit_date: date
    detail: str


def _marks_on(intent, leg_closes, day: date) -> dict | None:
    """Every leg's real close on `day`, or None if any leg has no bar.

    All-or-nothing on purpose: pricing three legs from bars and the fourth
    from a guess would put a fabricated number inside a figure the artifact
    presents as measured.
    """
    marks = {}
    for leg in intent.legs:
        symbol = str(leg.quote.symbol).upper()
        value = (leg_closes.get(symbol) or {}).get(day)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        marks[symbol] = value
    return marks


def _expiry_spot(underlying_closes, expiry: date) -> tuple[date, float] | None:
    """The underlying's close at expiry, or on the last session before it."""
    usable = sorted(d for d in underlying_closes if d <= expiry)
    if not usable:
        return None
    day = usable[-1]
    return day, float(underlying_closes[day])


def resolve_outcome(intent, contracts: int, leg_closes: dict,
                    underlying_closes: dict, entry_date: date,
                    profit_target: float = DEFAULT_PROFIT_TARGET,
                    forced_dte: float = FORCED_DTE_BELOW) -> WindowOutcome | None:
    """Realized P&L for one replayed trade, from real bars only.

    The exit rules replayed are exactly the deterministic two the live desk
    always carries (`committee/premortem.py`): close at `profit_target` of
    the credit received, and force a close once fewer than `forced_dte` days
    remain, so a short leg can never be assigned into stock. Whichever fires
    first on the real tape ends the trade.

    Three resolution methods, in decreasing order of evidential strength, and
    the one used is recorded on the result:

      * `METHOD_PROFIT_TARGET` — every leg had a real bar on the day the
        target was reached; P&L is marked from those bars.
      * `METHOD_FORCED_DTE` — the target never came, and every leg had a real
        bar on the forced-exit day; P&L is marked from those bars.
      * `METHOD_INTRINSIC` — the legs' bars are not all available (thin
        contracts stop printing), so the position is settled at expiry
        against the underlying's real close. This holds three days longer
        than the live rule would, which is a difference worth knowing about,
        which is why it is labelled rather than smoothed over.

    Returns None — never a zero, never a guess — when neither the legs nor
    the underlying can price the exit. The caller then skips the window.
    """
    contracts = int(contracts)
    expiry = intent.legs[0].quote.expiry
    forced_exit = expiry - timedelta(days=int(forced_dte))
    credit_total = intent.net_credit * CONTRACT_MULTIPLIER * contracts

    days = sorted(d for d in underlying_closes if entry_date < d <= forced_exit)
    last_marked: tuple[date, float] | None = None
    for day in days:
        marks = _marks_on(intent, leg_closes, day)
        if marks is None:
            continue
        pnl = realized_pnl(intent, contracts, close_net_credit(intent, marks))
        last_marked = (day, pnl)
        # A profit target stated as a fraction of credit received is
        # meaningless for a debit structure, which received none.
        if credit_total > 0 and pnl >= profit_target * credit_total:
            return WindowOutcome(
                realized_pnl=pnl, method=METHOD_PROFIT_TARGET, exit_date=day,
                detail=(f"marked from real leg bars on {day.isoformat()}; "
                        f"captured {pnl / credit_total:.0%} of the "
                        f"${credit_total:.2f} credit received"))

    if last_marked is not None and days and last_marked[0] == days[-1]:
        day, pnl = last_marked
        return WindowOutcome(
            realized_pnl=pnl, method=METHOD_FORCED_DTE, exit_date=day,
            detail=(f"profit target never reached; marked from real leg bars "
                    f"on the {int(forced_dte)}-DTE forced-exit day "
                    f"{day.isoformat()}"))

    settled = _expiry_spot(underlying_closes, expiry)
    if settled is None:
        return None
    day, spot = settled
    closing_credit = 0.0
    for leg in intent.legs:
        value = intrinsic(leg.quote.right, leg.quote.strike, spot)
        closing_credit += value if leg.side == "buy" else -value
    pnl = realized_pnl(intent, contracts, closing_credit)
    return WindowOutcome(
        realized_pnl=pnl, method=METHOD_INTRINSIC, exit_date=day,
        detail=(f"leg bars incomplete over the holding period; settled at "
                f"intrinsic value against the underlying's real "
                f"{day.isoformat()} close of {spot:.2f}. NOTE: this holds "
                f"past the {int(forced_dte)}-DTE forced exit the live desk "
                f"would have taken"))


# ── journal plumbing ─────────────────────────────────────────

class ReplayJournal:
    """A `Journal` that stamps every payload as replayed, not live.

    `committee.decide` and the closing writer build their own payloads and
    must not learn about replay — so the marker is applied here, on the way
    past, rather than threaded through the decision code. Every entry carries
    `source="seed_replay"` plus the decision date it is replaying, so no
    entry in a seeded journal can be read as a real broker interaction.

    The live `logs/journal.jsonl` is a judged artifact recording real orders;
    seeding writes to a separate file (`logs/seed_journal.jsonl` by default)
    and the two chains are never mixed.
    """

    def __init__(self, journal, as_of: date, window_id: int):
        self.journal = journal
        self.as_of = as_of
        self.window_id = int(window_id)

    @property
    def path(self):
        return self.journal.path

    def append(self, entry_type: str, payload: dict) -> dict:
        stamped = dict(payload or {})
        stamped["source"] = SEED_SOURCE
        stamped["replay_as_of"] = self.as_of.isoformat()
        stamped["window_id"] = self.window_id
        return self.journal.append(entry_type, stamped)

    def entries(self) -> list[dict]:
        return self.journal.entries()


def seeded_entry_count(entries) -> int:
    """How many entries in `entries` were written by a previous replay.

    A second seeding run over the same window produces the SAME
    `snapshot_hash` (the snapshot is deterministic by construction — see
    `committee/snapshot.py`), so it writes a second `analyst_view` and a
    second `close` that `calibration.resolved_predictions` happily joins into
    a second, identical prediction. The Brier sample would double with no new
    observation behind it — a bigger-looking number resting on nothing, which
    is the exact failure this whole exercise exists to avoid. So the caller
    refuses to append to an already-seeded journal unless told to.
    """
    return sum(1 for e in entries or []
               if (e.get("payload") or {}).get("source") == SEED_SOURCE)


# ── the run artifact ─────────────────────────────────────────

@dataclass
class WindowRecord:
    """One replayed decision date, whatever came of it."""
    as_of: date
    abstained: bool
    reason: str = ""
    expiry: date | None = None
    spot: float | None = None
    candidates: int = 0
    choice_id: str = ""
    structure: str = ""
    method: str = ""
    realized_pnl: float | None = None
    exit_date: date | None = None
    detail: str = ""
    snapshot_hash: str = ""


@dataclass
class SeedReport:
    """Attempted vs used vs skipped, with a reason for every skip.

    Exists so the artifact cannot be flattering by omission: a run that
    produced four usable windows out of twenty attempted has to say so, and
    say why the other sixteen failed.
    """
    symbol: str
    start: date
    end: date
    spacing: int = 1
    windows: list[WindowRecord] = field(default_factory=list)
    skipped: list[tuple] = field(default_factory=list)
    llm_calls: int = 0
    llm_cache_hits: int = 0
    llm_cost_usd: float = 0.0

    def skip(self, as_of: date, reason: str) -> None:
        logger.info("window %s skipped: %s", as_of, reason)
        self.skipped.append((as_of, reason))

    def record(self, as_of: date, abstained: bool, reason: str = "", **kw) -> WindowRecord:
        record = WindowRecord(as_of=as_of, abstained=bool(abstained),
                              reason=reason, **kw)
        self.windows.append(record)
        return record

    @property
    def used(self) -> int:
        return len(self.windows)

    @property
    def attempted(self) -> int:
        return len(self.windows) + len(self.skipped)

    @property
    def abstained(self) -> int:
        return sum(1 for w in self.windows if w.abstained)

    @property
    def resolved(self) -> int:
        return sum(1 for w in self.windows if w.realized_pnl is not None)

    @property
    def abstention_rate(self) -> float | None:
        """None, not 0.0, when the committee never ran. A rate over zero runs
        is undefined and printing 0% would read as "it never abstained"."""
        if not self.windows:
            return None
        return self.abstained / len(self.windows)

    def render(self) -> str:
        """The markdown artifact: cutoff rationale, windows, skips, methods."""
        rate = self.abstention_rate
        lines = [
            f"# Calibration seeding run — {self.symbol}",
            "",
            f"- Window: **{self.start.isoformat()} .. {self.end.isoformat()}**"
            f" (every {self.spacing} trading day(s))",
            f"- Attempted: **{self.attempted}** decision dates — "
            f"**{self.used}** replayed, **{len(self.skipped)}** skipped",
            f"- Committee abstained on **{self.abstained}** of {self.used} "
            f"replayed windows"
            + (f" (**{rate:.0%}** abstention rate)" if rate is not None else ""),
            f"- Windows resolved to a realized P&L: **{self.resolved}**",
            f"- LLM: **{self.llm_calls}** calls made, "
            f"**{self.llm_cache_hits}** served from the prompt cache, "
            f"**${self.llm_cost_usd:.4f}** spent",
            "",
            "## Why the window starts where it does",
            "",
            "The model's **knowledge cutoff is May 2026**, so no window may "
            f"begin before **{KNOWLEDGE_CUTOFF.isoformat()}**. Replaying an "
            "LLM committee over dates inside its own training data is the "
            "knowledge-contamination criticism this project levels at "
            "TradingAgents (arXiv 2412.20138) and FinMem (arXiv 2311.13743); "
            "making it of ourselves would be worse. `seed_replay."
            "validate_window` raises rather than clamps, and "
            "`seed_replay.decision_dates` drops any pre-cutoff day it is "
            "handed, so the only possible response to running out of "
            "post-cutoff calendar is FEWER windows, never earlier ones.",
            "",
            "## Windows replayed",
            "",
            "| as-of | expiry | spot | cands | choice | structure | "
            "resolution method | exit | realized P&L |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for w in self.windows:
            lines.append("| " + " | ".join([
                w.as_of.isoformat(),
                w.expiry.isoformat() if w.expiry else "—",
                f"{w.spot:.2f}" if w.spot is not None else "—",
                str(w.candidates),
                w.choice_id or "ABSTAIN",
                w.structure or "—",
                w.method or "—",
                w.exit_date.isoformat() if w.exit_date else "—",
                "—" if w.realized_pnl is None else f"${w.realized_pnl:,.2f}",
            ]) + " |")
        lines += ["", "## Windows skipped", ""]
        if not self.skipped:
            lines.append("None.")
        else:
            lines.append("| as-of | reason |")
            lines.append("|---|---|")
            for as_of, reason in self.skipped:
                lines.append(f"| {as_of.isoformat()} | {reason} |")
        lines += [
            "",
            "## Abstentions",
            "",
        ]
        abstentions = [w for w in self.windows if w.abstained]
        if not abstentions:
            lines.append("None — the committee chose a trade on every "
                         "replayed window.")
        else:
            for w in abstentions:
                lines.append(f"- **{w.as_of.isoformat()}** — {w.reason}")
        return "\n".join(lines) + "\n"
