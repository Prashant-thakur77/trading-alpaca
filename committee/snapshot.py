"""Renders market state + candidates into ONE deterministic, hashable string.

This is the only thing any analyst ever "sees" — no tool calls, no live data
fetch mid-reasoning (design spec 2, non-goals: "data is fetched
deterministically before any LLM runs"). Determinism matters twice over: it
keeps `prompt_hash`-based caching correct (same inputs -> same cache key ->
same cost), and it is what makes golden-file replay tests possible (same
snapshot -> same decision_hash).

To stay deterministic under those two pressures, this module:
  - never includes a timestamp or wall-clock value,
  - formats every float at a fixed precision (no repr()-driven drift),
  - sorts candidates by a canonical key before assigning ids, so byte-identical
    output does not depend on the order candidates happened to arrive in from
    a live chain fetch (a real chain yields ~600 candidates in no promised
    order).

It also has to CONTAIN the analysts' decision variables. The vol analyst is
asked to judge implied-vs-realized vol and liquidity; against an earlier
version of this snapshot — which carried neither — it abstained with "Implied
volatility data is not provided" on every live call. So each leg renders its
implied vol, open interest, bid, ask and quote width, and the header carries
the ATM implied vol and the IV-minus-realized spread, which is the actual
decision variable for premium selling. Where no IV solves, the field says
`unavailable` rather than disappearing: an analyst must be able to tell "no
data" from "low vol".
"""
import logging
from dataclasses import dataclass, field

import analytics
from candidate_builder import TradeIntent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Snapshot:
    """The rendered text AND the id -> TradeIntent mapping that produced it.

    Returning only the text would force every orchestrator to re-derive this
    module's private sort and cap in order to turn the trader's validated
    `"c3"` back into an order. An orchestrator that indexed its own unsorted
    candidate list instead (a live chain yields ~600 candidates in no promised
    order) would send a *different trade than the id names*, while the id
    validated cleanly and every guard reported PASS — CLAUDE.md hard rule 1
    defeated silently. So exactly one piece of code owns id assignment: this
    one, and it hands the mapping back with the text.
    """
    text: str
    candidates: dict[str, TradeIntent] = field(default_factory=dict)

    @property
    def candidate_ids(self) -> list[str]:
        """Ids in rendered order — what the trader is offered to choose from."""
        return list(self.candidates)


def _money(x: float) -> str:
    return f"{x:.2f}"


def _pct(x: float) -> str:
    return f"{x * 100:.2f}"


def _candidate_sort_key(intent: TradeIntent):
    """Canonical, order-independent sort key for capping the candidate list.

    `contracts` is part of the key because max_loss/max_profit render from it:
    two candidates identical on everything else but differing in size are two
    distinguishable rows, and without this term their order would fall back to
    input order — exactly the non-determinism this module exists to remove.
    """
    strikes = tuple(round(leg.quote.strike, 4) for leg in intent.legs)
    return (intent.structure, intent.dte, strikes, round(intent.net_credit, 4),
            intent.contracts)


def _stratified_cap(ordered: list[TradeIntent], max_candidates: int) -> list[TradeIntent]:
    """Select up to `max_candidates` from `ordered`, giving every structure
    type present a fair, round-robin share instead of a global top-N.

    A global top-N by `_candidate_sort_key` sorts by structure name first, so
    whichever structure sorts first (or is simply most numerous) fills the
    entire cap and every other structure — however sound a candidate it
    holds — never reaches the committee. On a real chain that surfaced 12
    bear_call_spread candidates and zero bull_put_spread or long_straddle,
    making the correct structure for the regime structurally invisible.

    `ordered` is already the canonical, order-independent sort (see
    `render_snapshot`), so grouping by structure and walking each group in
    that order gives a `(structures, per-structure index)` selection that
    depends only on the canonical key — never on the caller's input order.
    Round-robin (one candidate per present structure per round, skipping any
    structure already exhausted) ensures a structure with fewer candidates
    than its even share is included in full rather than starving out the
    others: it simply drops out of later rounds and its slots roll over to
    whichever structures still have candidates left.

    The selection is returned in the original canonical order (structure,
    dte, strikes, net_credit, contracts) — i.e. the existing sort remains
    both the within-structure ordering and, for the surfaced subset, the
    final rendering order. When nothing needs to be dropped (total fits
    within `max_candidates`) this is byte-identical to the previous
    behaviour of taking `ordered[:max_candidates]` in full.
    """
    groups: dict[str, list[int]] = {}
    for idx, intent in enumerate(ordered):
        groups.setdefault(intent.structure, []).append(idx)
    structures = sorted(groups)  # deterministic, independent of input order
    pointers = {s: 0 for s in structures}

    selected_idx: list[int] = []
    while len(selected_idx) < max_candidates:
        progressed = False
        for s in structures:
            if len(selected_idx) >= max_candidates:
                break
            p = pointers[s]
            idxs = groups[s]
            if p < len(idxs):
                selected_idx.append(idxs[p])
                pointers[s] = p + 1
                progressed = True
        if not progressed:
            break  # every structure exhausted before reaching the cap

    selected_idx.sort()
    return [ordered[i] for i in selected_idx]


UNAVAILABLE = "unavailable"


def _leg_iv(leg, spot: float, dte: int) -> float | None:
    """Implied vol for one leg, or None when no IV solves its quote.

    `analytics.implied_vol` already fails closed (a quote below intrinsic, a
    zero bid) — None here means "no data", which is rendered as such rather
    than omitted. An analyst that cannot distinguish "no data" from "low vol"
    will read one as the other.
    """
    q = leg.quote
    return analytics.implied_vol(
        q.mid, spot, q.strike, analytics.time_to_expiry_years(dte), q.right)


def _render_leg(leg, iv: float | None) -> str:
    q = leg.quote
    iv_text = f"{_pct(iv)}%" if iv is not None else UNAVAILABLE
    width = q.spread_pct
    width_text = f"{_pct(width)}%" if width != float("inf") else UNAVAILABLE
    return (
        f"{leg.side} {_money(q.strike)}{q.right} "
        f"(iv={iv_text} oi={q.open_interest} bid={_money(q.bid)} "
        f"ask={_money(q.ask)} width={width_text})"
    )


def _render_candidate(cid: str, intent: TradeIntent, spot: float) -> str:
    leg_ivs = [_leg_iv(leg, spot, intent.dte) for leg in intent.legs]
    legs = "; ".join(_render_leg(leg, iv) for leg, iv in zip(intent.legs, leg_ivs))
    breakevens = ", ".join(_money(b) for b in intent.breakevens)
    max_profit = "inf" if intent.max_profit == float("inf") else _money(intent.max_profit)
    solved = [iv for iv in leg_ivs if iv is not None]
    mean_iv = f"{_pct(sum(solved) / len(solved))}%" if solved else UNAVAILABLE
    return (
        f"{cid} | {intent.structure} | DTE={intent.dte} | contracts={intent.contracts}\n"
        f"  legs: {legs}\n"
        f"  mean_leg_iv={mean_iv}\n"
        f"  net_credit={_money(intent.net_credit)} "
        f"max_loss={_money(intent.max_loss)} max_profit={max_profit}\n"
        f"  breakevens={breakevens}"
    )


def _candidate_derived_atm_iv(candidates: list[TradeIntent], spot: float) -> float | None:
    """FALLBACK ONLY — IV of the leg closest to the money among the
    candidates this call happened to be given.

    This is the selection-dependent estimate `render_snapshot` used
    unconditionally until it was found to bias the vol-regime call: on one
    unchanged SPY chain, surfacing only bear call spreads (all OTM calls,
    low on the vol skew) put this at -0.69pp vs realized, while including a
    straddle too flipped it to +0.72pp — same market, opposite regime call.
    `analytics.atm_implied_vol`, computed once from the full chain, is now
    the real source; this only runs when a caller omits `atm_iv` entirely
    (see `render_snapshot`), to keep old callers from crashing.

    Ties are broken on (strike, right) so the choice is deterministic. None
    when no leg's IV solves.
    """
    legs = [(abs(leg.quote.strike - spot), leg.quote.strike, leg.quote.right,
             leg, intent.dte)
            for intent in candidates for leg in intent.legs]
    for _distance, _strike, _right, leg, dte in sorted(legs, key=lambda x: x[:3]):
        iv = _leg_iv(leg, spot, dte)
        if iv is not None:
            return iv
    return None


def _iv_minus_realized(atm_iv: float | None, realized_vol: float) -> str:
    """The IV-vs-realized spread, rendered with its own sign convention spelled out.

    On a real 2026-08-29 cycle the two analysts read the SAME line —
    "IV_MINUS_REALIZED: -0.69pp" — in opposite directions: the vol analyst
    called it "a structural edge for short premium", the bear adversary called
    it "the market underpricing volatility". The bear was right. Implied BELOW
    realized means options are cheap relative to how much the underlying
    actually moves, so a premium *seller* collects less than the movement
    warrants — which argues against short premium, not for it.

    A bare signed number cannot fix that: the reader has to supply the
    convention, and half the time supplies it backwards. So the definitional
    meaning is rendered alongside the number. It states only the definition —
    never which candidate to pick, never whether to trade at all. That
    judgement is the committee's, and telling it the answer here would make
    the analysts' agreement meaningless.
    """
    if atm_iv is None:
        return UNAVAILABLE

    points = (atm_iv - realized_vol) * 100
    rendered = f"{points:+.2f}pp"
    # Compare the RENDERED value, not the raw float: a spread of -0.001pp
    # prints as "-0.00pp" and must not be described as BELOW realized.
    if rendered.startswith("+0.00") or rendered.startswith("-0.00"):
        return (f"{rendered} (implied is LEVEL with realized — options are priced "
                f"in line with actual movement, so there is no volatility edge "
                f"in either direction)")
    if points > 0:
        return (f"{rendered} (implied is ABOVE realized — options are rich "
                f"relative to actual movement, which favours SELLING premium, "
                f"not buying it)")
    return (f"{rendered} (implied is BELOW realized — options are cheap "
            f"relative to actual movement, which favours BUYING premium, "
            f"not selling it)")


# Sentinel distinguishing "caller did not pass atm_iv at all" (legacy call
# site — fall back, with a warning) from "caller passed atm_iv=None" (the
# market computation ran and could not establish one — render `unavailable`,
# and NEVER fall back to the selection-dependent estimate, which would
# resurrect the exact bug this sentinel exists to prevent).
ATM_IV_NOT_SUPPLIED = object()


def render_snapshot(
    underlying: str,
    spot: float,
    realized_vol: float,
    candidates: list[TradeIntent],
    max_candidates: int = 12,
    atm_iv: float | None = ATM_IV_NOT_SUPPLIED,
) -> Snapshot:
    """Render one deterministic `Snapshot` for the given cycle inputs.

    Candidates are sorted by a canonical key (structure, dte, strikes, net
    credit, contracts) before ids c1..cN are assigned. The list is then
    capped at `max_candidates` via a stratified, round-robin selection across
    the structure types actually present (see `_stratified_cap`) — not a
    global top-N — so every available structure type is represented in what
    the committee sees, and the selection is reproducible regardless of the
    order the caller's candidate list happened to be built in.

    `atm_iv` is the ATM implied vol the header reports and compares against
    `realized_vol` — the analysts' primary vol-regime signal. It MUST be a
    property of the market, not of `candidates`: pass
    `analytics.atm_implied_vol(chain, spot)`, computed once from the full
    option chain, not from whichever candidates happened to be built or
    survive the `max_candidates` cap. Pass `None` when the market computation
    itself could not establish one — that renders `unavailable`. Omitting
    the argument entirely falls back to the old candidate-derived estimate
    (logging a warning that it is selection-dependent), purely so a caller
    that has not been updated yet does not crash.

    Returns both the text the analysts see and the id -> TradeIntent mapping
    that names what each id actually is (see `Snapshot`).
    """
    ordered = sorted(candidates, key=_candidate_sort_key)
    total = len(ordered)
    capped = _stratified_cap(ordered, max_candidates)
    by_id = {f"c{i}": intent for i, intent in enumerate(capped, start=1)}

    if atm_iv is ATM_IV_NOT_SUPPLIED:
        logger.warning(
            "render_snapshot called without atm_iv — falling back to the "
            "candidate-derived estimate, which is selection-dependent (it "
            "varies with which candidates were surfaced, not the market). "
            "Callers should pass analytics.atm_implied_vol(chain, spot)."
        )
        atm_iv = _candidate_derived_atm_iv(capped, spot)
    lines = [
        f"UNDERLYING: {underlying}",
        f"SPOT: {_money(spot)}",
        f"REALIZED_VOL: {_pct(realized_vol)}%",
        f"IMPLIED_VOL_ATM: {f'{_pct(atm_iv)}%' if atm_iv is not None else UNAVAILABLE}",
        f"IV_MINUS_REALIZED: {_iv_minus_realized(atm_iv, realized_vol)}",
        f"CANDIDATES ({len(capped)} of {total}):",
    ]
    for cid, intent in by_id.items():
        lines.append(_render_candidate(cid, intent, spot))

    return Snapshot(text="\n".join(lines) + "\n", candidates=by_id)
