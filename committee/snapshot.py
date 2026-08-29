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
import risk_guard
from candidate_builder import CONTRACT_MULTIPLIER, TradeIntent

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


def _default_max_loss_cap() -> float:
    """The guard's own max_loss_per_position, loaded from risk.yaml.

    Never hardcode this number here: `risk_guard.load_risk_config()` is the
    one source of truth the guard itself evaluates every candidate against,
    and duplicating the figure would let the two drift apart silently.
    """
    return risk_guard.load_risk_config().max_loss_per_position


def _drop_certain_denials(
    candidates: list[TradeIntent], max_loss_cap: float
) -> list[TradeIntent]:
    """Drop any candidate whose OWN max_loss already exceeds the guard's cap.

    Measured on a real SPY chain (2026-08-29): all 4 surfaced long_straddle
    candidates had max_loss $1,211-$2,154 against risk.yaml's $1,000
    max_loss_per_position — a guaranteed RiskGuard DENY at the contract count
    they were built with. Surfacing them anyway wasted 4 of the 12 slots the
    committee saw on trades that could never be executed, and asking an LLM
    to reason about an uncrossable trade is pure noise.

    This is a per-candidate, structure-blind filter: if it eliminates every
    candidate of one structure type (as it did for long_straddle here), that
    is the correct outcome for that chain/moment, not a bug to work around —
    the stratified cap below simply gives the remaining structures the freed
    slots (see its docstring) rather than leaving them empty or backfilling
    with more of a structure already represented.
    """
    return [c for c in candidates if c.max_loss <= max_loss_cap]


# Minimum net credit as a fraction of the width (max loss per contract), for
# CREDIT structures (bull_put_spread, bear_call_spread, iron_condor) only.
#
# Measured live on SPY (2026-08-29, spot 769.35): the cushion-spread ranking
# fix above (Part B, see `_structure_fill_order`) correctly stopped
# clustering the surfaced set at the tightest breakevens -- but its wide-
# cushion end turned out to be populated by far-OTM spreads with negligible
# credit: c4 and c6 were bear_call_spreads at cushion 7.89%/8.54% collecting
# $0.02 credit against $498 max loss each -- 249:1 risk against reward. The
# independent blind reviewer flagged both unprompted, in its own words:
# "$0.02 credit against $498 max loss is indefensible risk/reward (249:1
# against trader)" and "Strikes 7.9% OTM should collect far more premium to
# justify the capital at risk." One unusable menu (tightest-breakeven-only)
# had been traded for another (worthless-credit-included). 10% of width is a
# conventional desk floor for a vertical -- do not raise it to chase a higher
# hit rate against the blind veto, and do not lower it to let more candidates
# through: the veto's job is to catch what this floor misses, not the other
# way around.
MIN_CREDIT_TO_RISK = 0.10

# The three structures this floor applies to. Deliberately a structure-name
# test, NOT `intent.is_credit` (`net_credit > 0`): a live SPY chain produced
# bull_put_spread/bear_call_spread candidates with net_credit of $0.00 and
# even $-0.03 (illiquid far-OTM legs pricing at mid to a net debit). Those
# are credit-structure candidates in exactly as much trouble as the $0.02
# case -- more, in the $-0.03 case -- but `is_credit` reads negative/zero
# credit as "not a credit trade" and would have let them through this filter
# untouched, re-opening the same hole this fix exists to close. Testing the
# structure name catches all three regardless of which way net_credit rounds.
CREDIT_STRUCTURES = {"bull_put_spread", "bear_call_spread", "iron_condor"}


def _drop_thin_credit(candidates: list[TradeIntent], min_ratio: float) -> list[TradeIntent]:
    """Drop CREDIT_STRUCTURES candidates whose net credit is a negligible (or
    negative/zero) fraction of the capital at risk (see `MIN_CREDIT_TO_RISK`).

    DEBIT structures (long_straddle, bull_call_spread, bear_put_spread --
    none of them in `CREDIT_STRUCTURES`) are never touched here: a
    credit-to-risk floor is meaningless for a trade that pays a debit rather
    than collects one, and every debit structure has a NEGATIVE net_credit by
    construction, so applying the ratio to them would reject 100% of the
    long-premium menu unconditionally. That is precisely the failure mode
    that produced the 72% abstention rate one filter over -- a rule written
    for one structure family silently erasing another -- so the exclusion is
    pinned by its own tests. Debit structures are filtered only on their own
    max_loss, by `_drop_certain_denials`.

    Structure-blind within the credit set and per-candidate, same shape as
    `_drop_certain_denials`: if it empties one structure entirely,
    `_stratified_cap` gives the freed slots to whichever structures still
    have eligible candidates rather than leaving them empty (see its
    docstring).
    """
    return [
        c for c in candidates
        if c.structure not in CREDIT_STRUCTURES
        or c.net_credit * CONTRACT_MULTIPLIER >= min_ratio * (c.max_loss / c.contracts)
    ]


def _breakeven_cushion(intent: TradeIntent, spot: float) -> float:
    """Distance from spot to the NEAREST breakeven, as a fraction of spot.

    This is "how far the underlying has to move before this trade starts
    losing", which is the safety axis the old ranking collapsed: taking the
    canonical (structure, dte, strikes, ...) order from the front walks
    strikes closest-to-the-money first, i.e. the tightest cushion first.
    """
    if not intent.breakevens or spot <= 0:
        return 0.0
    return min(abs(b - spot) for b in intent.breakevens) / spot


def _reward_to_risk(intent: TradeIntent) -> float:
    """Reward per dollar of risk taken: max_profit / max_loss.

    `max_profit` is `float("inf")` for every long_straddle by construction
    (unbounded upside) — every candidate of that structure carries the same
    value, so it contributes no ranking signal within that group and
    `_normalize` below correctly flattens it to a constant instead of
    dividing infinities.
    """
    if intent.max_loss <= 0:
        return float("inf")
    return intent.max_profit / intent.max_loss


def _normalize(values: dict[int, float]) -> dict[int, float]:
    """Min-max scale a {index: value} map into [0, 1].

    When every value is equal (including the all-`inf` long_straddle case,
    since `inf == inf`) there is no ranking signal to extract, and scaling
    would divide zero by zero — this flattens that case to a constant 0.5
    for everyone rather than letting whichever index happens to sort first
    win the tie-break by accident.
    """
    lo, hi = min(values.values()), max(values.values())
    if lo == hi:
        return {i: 0.5 for i in values}
    return {i: (v - lo) / (hi - lo) for i, v in values.items()}


def _split_evenly(items: list[int], n: int) -> list[list[int]]:
    """Split `items` into `n` contiguous, near-equal chunks, in order.

    Chunk sizes differ by at most one. Fewer than `n` items still yields `n`
    chunks (some possibly empty) so callers can always index all `n` bands.
    """
    k, m = divmod(len(items), n)
    chunks, start = [], 0
    for i in range(n):
        size = k + (1 if i < m else 0)
        chunks.append(items[start:start + size])
        start += size
    return chunks


def _structure_fill_order(
    idxs: list[int], ordered: list[TradeIntent], spot: float
) -> list[int]:
    """The pick order for one structure's candidates: which one fills that
    structure's 1st slot, which fills the 2nd if it gets two, and so on.

    This is Part B of the ranking fix. The old code walked `idxs` in the
    canonical (structure, dte, strikes, net_credit, contracts) order — on a
    real SPY chain that put the 4 tightest-cushion, highest-credit
    bear_call_spreads of 188 in the first 4 slots and nothing else was ever
    seen, even though cushions up to 8.54% existed. Highest credit is
    closest to spot, which is the tightest, riskiest spread — the previous
    ranking always favoured that extreme.

    The fix ranks candidates by a quality score that balances reward against
    safety — credit relative to the risk taken (`_reward_to_risk`) combined
    with the breakeven cushion (`_breakeven_cushion`) — and then selects
    ACROSS that ordering instead of from its top:

      1. Sort the group by cushion, tight -> wide, and split it into three
         near-equal bands: tight / medium / wide. This is the axis that was
         invisible before; splitting on it directly (rather than hoping a
         combined score happens to spread it) is what actually guarantees a
         tight, a medium and a wide candidate are all visible whenever the
         structure has enough candidates to offer them.
      2. Within each band, best quality_score first — so which ONE candidate
         represents "medium cushion" is still the best trade at that risk
         level, not an arbitrary one.
      3. Interleave the three bands round-robin: 1st slot = tight's best,
         2nd = medium's best, 3rd = wide's best, 4th = tight's 2nd best, ...

    Interleaving matters because the stratified cap below decides how many
    slots each structure actually gets dynamically (depending on how many
    other structures are present and how large they are) — this module does
    not know that count when it ranks. Interleaving means ANY prefix length
    of the result is a spanning sample, so whatever number of slots this
    structure ends up with, the surfaced set still spans the range rather
    than exhausting the tight band before ever reaching medium or wide.

    Deliberately not encoded here: which cushion is "correct". A 0.39%
    cushion and a 3.9% cushion are both surfaced: which one is worth trading
    is the committee's judgement and the guard's verdict, not this ranking's.
    """
    if not idxs:
        return []

    quality = _normalize({i: _reward_to_risk(ordered[i]) for i in idxs})
    cushion = _normalize({i: _breakeven_cushion(ordered[i], spot) for i in idxs})
    score = {i: quality[i] + cushion[i] for i in idxs}

    by_cushion = sorted(
        idxs, key=lambda i: (_breakeven_cushion(ordered[i], spot), _candidate_sort_key(ordered[i]))
    )
    bands = [
        sorted(band, key=lambda i: (-score[i], _candidate_sort_key(ordered[i])))
        for band in _split_evenly(by_cushion, 3)
    ]

    fill_order: list[int] = []
    for round_i in range(max((len(b) for b in bands), default=0)):
        for band in bands:
            if round_i < len(band):
                fill_order.append(band[round_i])
    return fill_order


def _stratified_cap(
    ordered: list[TradeIntent], max_candidates: int, spot: float
) -> list[TradeIntent]:
    """Select up to `max_candidates` from `ordered`, giving every structure
    type present a fair, round-robin share instead of a global top-N.

    A global top-N by `_candidate_sort_key` sorts by structure name first, so
    whichever structure sorts first (or is simply most numerous) fills the
    entire cap and every other structure — however sound a candidate it
    holds — never reaches the committee. On a real chain that surfaced 12
    bear_call_spread candidates and zero bull_put_spread or long_straddle,
    making the correct structure for the regime structurally invisible.

    Which structures get how many slots is still decided by simple
    round-robin over `structures` (deterministic, independent of input
    order): a structure with fewer candidates than its even share is
    included in full and its unused slots roll over to whichever structures
    still have candidates left. WHICH candidates of a structure fill its
    slots is decided by `_structure_fill_order` (Part B of the ranking fix,
    see its docstring) instead of the plain canonical order.

    The selection is returned in the original canonical order (structure,
    dte, strikes, net_credit, contracts) regardless of pick order — i.e. the
    canonical sort remains the final RENDERING order; `_structure_fill_order`
    only changes which candidates are chosen, never how the chosen ones are
    displayed.
    """
    groups: dict[str, list[int]] = {}
    for idx, intent in enumerate(ordered):
        groups.setdefault(intent.structure, []).append(idx)
    structures = sorted(groups)  # deterministic, independent of input order
    fill_orders = {s: _structure_fill_order(idxs, ordered, spot) for s, idxs in groups.items()}
    pointers = {s: 0 for s in structures}

    selected_idx: list[int] = []
    while len(selected_idx) < max_candidates:
        progressed = False
        for s in structures:
            if len(selected_idx) >= max_candidates:
                break
            p = pointers[s]
            order = fill_orders[s]
            if p < len(order):
                selected_idx.append(order[p])
                pointers[s] = p + 1
                progressed = True
        if not progressed:
            break  # every structure exhausted before reaching the cap

    selected_idx.sort()
    return [ordered[i] for i in selected_idx]


UNAVAILABLE = "unavailable"

# The `net_credit` sign convention, stated for the reader rather than left to
# be inferred from whichever sign happens to appear.
#
# Measured on the seeded June-July replay windows immediately after the debit
# verticals were added: window 2026-06-18 surfaced bull_call_spread and
# bear_put_spread candidates into an IV-2.42pp-BELOW-realized regime, and the
# vol analyst abstained anyway with "all candidates are credit/mixed spreads
# with net short or zero premium bias. No straddles available." It was reading
# long-premium candidates and could not tell, because the only thing marking
# them was a minus sign whose meaning the snapshot never stated. This is the
# same failure `_iv_minus_realized` documents at length for the IV spread:
# readers supply a missing convention themselves, and supply it backwards
# about half the time.
#
# Like that line, this one states ONLY the definition. It never says which
# side to take — that judgement belongs to the committee, and prescribing it
# here would make the analysts' agreement meaningless.
NET_CREDIT_CONVENTION = (
    "NET_CREDIT_CONVENTION: net_credit is per share. POSITIVE means premium "
    "is RECEIVED (a short-premium/credit trade: bull_put_spread, "
    "bear_call_spread, iron_condor). NEGATIVE means premium is PAID (a "
    "long-premium/debit trade: bull_call_spread, bear_put_spread, "
    "long_straddle) — a negative net_credit is a correctly formed debit "
    "trade, not a malformed credit spread. For a credit trade max_profit is "
    "the premium received; for a debit trade max_loss is the premium paid."
)


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
    max_loss_cap: float | None = None,
) -> Snapshot:
    """Render one deterministic `Snapshot` for the given cycle inputs.

    Before anything else, any candidate whose own max_loss already exceeds
    `max_loss_cap` is dropped (`_drop_certain_denials`) — RiskGuard would
    DENY it on that basis alone, at the contract count it was built with, so
    surfacing it only wastes one of the committee's limited slots on a trade
    that can never be executed. `max_loss_cap` defaults to
    `risk_guard.load_risk_config().max_loss_per_position` — the SAME number
    the guard itself enforces — rather than a hardcoded figure that could
    drift from it; pass an explicit value to pin it (tests do this to stay
    independent of risk.yaml's current contents).

    Next, any CREDIT candidate (bull_put_spread, bear_call_spread,
    iron_condor) whose net credit is a negligible fraction of its max loss is
    dropped (`_drop_thin_credit`, floor `MIN_CREDIT_TO_RISK`) — see that
    constant's docstring for the measured live case (a 249:1 risk/reward
    spread the blind reviewer had to catch by hand) that motivates it. DEBIT
    candidates (long_straddle, bull_call_spread, bear_put_spread) are
    untouched by this filter.

    The surviving candidates are sorted by a canonical key (structure, dte,
    strikes, net credit, contracts) before ids c1..cN are assigned. The list
    is then capped at `max_candidates` via a stratified selection across the
    structure types actually present (see `_stratified_cap`) — not a global
    top-N — so every available structure type is represented, and within
    each structure the surfaced set spans tight/medium/wide breakeven
    cushions rather than clustering at one extreme (see
    `_structure_fill_order`). Both steps are pure functions of the canonical
    sort, which is itself independent of input order, so the whole selection
    is reproducible regardless of the order the caller's candidate list
    happened to be built in.

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
    if max_loss_cap is None:
        max_loss_cap = _default_max_loss_cap()
    eligible = _drop_certain_denials(candidates, max_loss_cap)
    eligible = _drop_thin_credit(eligible, MIN_CREDIT_TO_RISK)
    ordered = sorted(eligible, key=_candidate_sort_key)
    total = len(ordered)
    capped = _stratified_cap(ordered, max_candidates, spot)
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
        NET_CREDIT_CONVENTION,
        f"CANDIDATES ({len(capped)} of {total}):",
    ]
    for cid, intent in by_id.items():
        lines.append(_render_candidate(cid, intent, spot))

    return Snapshot(text="\n".join(lines) + "\n", candidates=by_id)
