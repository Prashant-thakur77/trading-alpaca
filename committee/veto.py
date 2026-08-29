"""The dual veto: two decorrelated reviewers, both must pass.

CLAUDE.md's hard rule says "two different model families must agree" before
a trade fires. Only the `claude` CLI is available in this environment — no
API key, no Ollama — so two Claude calls on the same context would correlate
heavily and produce a veto that *looks* rigorous without being one. The
design spec (§3, "amended rule") restates the requirement as two decorrelated
reviewers instead:

  1. `thesis_check` — PURE CODE, no LLM at all. Verifies the chosen
     structure's own position Greeks are consistent with its own directional
     thesis (a bull put spread must be net long delta, a bear call spread
     net short delta, a neutral structure near delta-neutral). If Greeks are
     unmeasurable, this fails closed — an unpriceable leg cannot be verified
     consistent with anything, so silence is not a pass.
  2. `blind_review` — an independent Claude call that sees ONLY the candidate
     and price/vol context, never the committee's own reasoning or debate
     transcript. That blindness is the point: a call that saw the analysts'
     reasoning would tend to agree with it, defeating the purpose of a
     second opinion.

Both must pass for a trade to proceed to RiskGuard.
"""
from functools import partial

import analytics
from candidate_builder import TradeIntent
from llm.client import call_claude

# Position-delta units PER CONTRACT (per-share delta * 100, RiskGuard's own
# convention divided by the contract count). Half of risk.yaml's
# |net delta| <= 30 limit — comfortably inside "clearly directional" while
# still admitting the small residual delta a real neutral structure carries
# away from a round strike. A heuristic, deliberately: it exists to catch a
# mislabelled structure, not to size a position — that is RiskGuard's job,
# which is why the band scales with `intent.contracts` (see thesis_check).
NEUTRAL_DELTA_THRESHOLD = 15.0

_NEUTRAL_STRUCTURES = {"long_straddle", "iron_condor"}

BLIND_REVIEW_MODEL = "claude-haiku-4-5"

_BLIND_REVIEW_PROMPT = """You are an independent second reviewer on a \
defined-risk US-equity options desk. You are shown ONLY the candidate trade \
and current price/volatility context below — you have not seen any other \
analyst's reasoning or debate. Judge, from this alone, whether the trade's \
direction (or lack of one, for a neutral structure) is a reasonable reading \
of the given price action.

UNDERLYING: {underlying}
SPOT: {spot:.2f}
REALIZED_VOL: {realized_vol_pct:.2f}%
STRUCTURE: {structure}
LEGS: {legs}
NET_CREDIT: {net_credit:.2f}
MAX_LOSS: {max_loss:.2f}
BREAKEVENS: {breakevens}
DTE: {dte}

Respond with ONLY a JSON object, no prose outside it:
{{"agree": <true/false>, "reasoning": "<one or two sentences>"}}
"""


def thesis_check(intent: TradeIntent, spot: float) -> tuple[bool, str]:
    """Pure-code check: does the position's own delta match its structure's
    directional thesis? Fails closed if Greeks are unmeasurable or the
    structure is unrecognized."""
    position_greeks = analytics.position_greeks(intent, spot)
    if position_greeks is None:
        return False, "position Greeks unmeasurable (unpriceable leg) — failing closed"

    net_delta, _net_vega = position_greeks
    structure = intent.structure

    if structure == "bull_put_spread":
        ok = net_delta > 0
        return ok, (
            f"net delta {net_delta:.2f} consistent with bull put spread's bullish thesis"
            if ok else
            f"bull put spread requires net long delta; measured {net_delta:.2f}"
        )

    if structure == "bear_call_spread":
        ok = net_delta < 0
        return ok, (
            f"net delta {net_delta:.2f} consistent with bear call spread's bearish thesis"
            if ok else
            f"bear call spread requires net short delta; measured {net_delta:.2f}"
        )

    if structure in _NEUTRAL_STRUCTURES:
        # The band scales with size because `position_greeks` does. This test
        # asks "is this structure directional?", which is a property of the
        # structure, not of how many of it we buy — an unscaled band vetoed
        # the same trade with the same thesis purely for being bigger
        # (measured on one straddle: 1 contract +11.30 passes, 2 -> +22.59
        # vetoed, 3 -> +33.89 vetoed), pre-empting RiskGuard, which is the
        # layer that owns sizing and can downsize rather than refuse.
        band = NEUTRAL_DELTA_THRESHOLD * max(intent.contracts, 1)
        ok = abs(net_delta) <= band
        return ok, (
            f"net delta {net_delta:.2f} within neutral band (+/-{band:.2f} "
            f"= {NEUTRAL_DELTA_THRESHOLD} x {intent.contracts} contract(s))"
            if ok else
            f"{structure} expected near delta-neutral; measured {net_delta:.2f} "
            f"against band +/-{band:.2f}"
        )

    return False, f"unknown structure {structure!r} — failing closed"


def _default_client():
    return partial(call_claude, model=BLIND_REVIEW_MODEL)


def blind_review(
    intent: TradeIntent, spot: float, realized_vol: float, client=None
) -> tuple[bool, str]:
    """Independent Claude call, starved of the committee's own reasoning.
    LLM failure or an unparseable/missing `agree` field vetoes (fails
    closed) — silence from this reviewer must never be read as a pass."""
    legs = "; ".join(
        f"{leg.side} {leg.quote.strike:.2f}{leg.quote.right}" for leg in intent.legs
    )
    prompt = _BLIND_REVIEW_PROMPT.format(
        underlying=intent.underlying,
        spot=spot,
        realized_vol_pct=realized_vol * 100,
        structure=intent.structure,
        legs=legs,
        net_credit=intent.net_credit,
        max_loss=intent.max_loss,
        breakevens=", ".join(f"{b:.2f}" for b in intent.breakevens),
        dte=intent.dte,
    )

    client = client or _default_client()
    response = client(prompt)

    if not response.ok:
        return False, f"blind review LLM failure — failing closed: {response.error}"

    # A non-dict `parsed` is not a pass: `"agree" not in "ABSTAIN"` is a
    # substring test that would quietly succeed, and a float would raise.
    if not isinstance(response.parsed, dict):
        return False, (
            f"blind review output was not a JSON object "
            f"({type(response.parsed).__name__}) — failing closed"
        )

    parsed = response.parsed
    if "agree" not in parsed or not isinstance(parsed["agree"], bool):
        return False, "blind review response malformed (missing/invalid 'agree') — failing closed"

    return bool(parsed["agree"]), str(parsed.get("reasoning", ""))
