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
from dataclasses import dataclass, field

import analytics
from candidate_builder import TradeIntent


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


def _atm_implied_vol(candidates: list[TradeIntent], spot: float) -> float | None:
    """IV of the leg closest to the money across the rendered candidates.

    That single number — not a skew-weighted average over every strike — is
    what "implied volatility" means when set against realized vol, so it is
    what the header compares. Ties are broken on (strike, right) so the choice
    is deterministic. None when no leg's IV solves.
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


def render_snapshot(
    underlying: str,
    spot: float,
    realized_vol: float,
    candidates: list[TradeIntent],
    max_candidates: int = 12,
) -> Snapshot:
    """Render one deterministic `Snapshot` for the given cycle inputs.

    Candidates are sorted by a canonical key (structure, dte, strikes, net
    credit, contracts) before ids c1..cN are assigned and the list is capped
    at `max_candidates` — so the selection is reproducible regardless of the
    order the caller's candidate list happened to be built in.

    Returns both the text the analysts see and the id -> TradeIntent mapping
    that names what each id actually is (see `Snapshot`).
    """
    ordered = sorted(candidates, key=_candidate_sort_key)
    total = len(ordered)
    capped = ordered[:max_candidates]
    by_id = {f"c{i}": intent for i, intent in enumerate(capped, start=1)}

    atm_iv = _atm_implied_vol(capped, spot)
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
