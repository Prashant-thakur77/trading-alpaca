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
"""
from candidate_builder import TradeIntent


def _money(x: float) -> str:
    return f"{x:.2f}"


def _pct(x: float) -> str:
    return f"{x * 100:.2f}"


def _candidate_sort_key(intent: TradeIntent):
    """Canonical, order-independent sort key for capping the candidate list."""
    strikes = tuple(round(leg.quote.strike, 4) for leg in intent.legs)
    return (intent.structure, intent.dte, strikes, round(intent.net_credit, 4))


def _render_candidate(cid: str, intent: TradeIntent) -> str:
    legs = "; ".join(
        f"{leg.side} {_money(leg.quote.strike)}{leg.quote.right}"
        for leg in intent.legs
    )
    breakevens = ", ".join(_money(b) for b in intent.breakevens)
    max_profit = "inf" if intent.max_profit == float("inf") else _money(intent.max_profit)
    return (
        f"{cid} | {intent.structure} | DTE={intent.dte}\n"
        f"  legs: {legs}\n"
        f"  net_credit={_money(intent.net_credit)} "
        f"max_loss={_money(intent.max_loss)} max_profit={max_profit}\n"
        f"  breakevens={breakevens}"
    )


def render_snapshot(
    underlying: str,
    spot: float,
    realized_vol: float,
    candidates: list[TradeIntent],
    max_candidates: int = 12,
) -> str:
    """Render one deterministic snapshot string for the given cycle inputs.

    Candidates are sorted by a canonical key (structure, dte, strikes, net
    credit) before ids c1..cN are assigned and the list is capped at
    `max_candidates` — so the selection is reproducible regardless of the
    order the caller's candidate list happened to be built in.
    """
    ordered = sorted(candidates, key=_candidate_sort_key)
    total = len(ordered)
    capped = ordered[:max_candidates]

    lines = [
        f"UNDERLYING: {underlying}",
        f"SPOT: {_money(spot)}",
        f"REALIZED_VOL: {_pct(realized_vol)}%",
        f"CANDIDATES ({len(capped)} of {total}):",
    ]
    for i, intent in enumerate(capped, start=1):
        lines.append(_render_candidate(f"c{i}", intent))

    return "\n".join(lines) + "\n"
