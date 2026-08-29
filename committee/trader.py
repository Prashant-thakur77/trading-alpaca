"""Trader: picks one candidate id or ABSTAIN. Never builds an order itself.

Runs on `claude-sonnet-5` — the stronger model makes the final pick, after
the (cheaper Haiku) analysts have formed their views. This module still
never invents a strike or quantity (CLAUDE.md hard rule 1): its only two
legal outputs are one of `candidate_ids` (built by `candidate_builder`,
deterministic code) or the literal string "ABSTAIN".

The safety-critical step here is validation, not selection: a hallucinated
or malformed id from the LLM is treated as ABSTAIN, never as an error to
paper over by e.g. picking the first candidate instead (spec 4.3, "Candidate
ids are validated against the set generated this cycle").
"""
from functools import partial

from llm.client import call_claude
from committee.analysts import AnalystView

TRADER_MODEL = "claude-sonnet-5"

_TRADER_PROMPT = """You are the trader on a defined-risk US-equity options \
desk, making the final call for this cycle. You may ONLY pick one of the \
candidate ids listed below, or ABSTAIN. You may never propose a strike, \
quantity, or structure that is not already in the list.

COMMITTEE VIEWS:
{views}

CANDIDATE IDS AVAILABLE THIS CYCLE: {candidate_ids}

MARKET SNAPSHOT:
{snapshot}

Respond with ONLY a JSON object, no prose outside it:
{{"choice": "<one of the candidate ids above, or the literal string ABSTAIN>", \
"reasoning": "<one or two sentences>"}}
"""


def _render_views(views: list[AnalystView]) -> str:
    lines = []
    for v in views:
        if v.abstained:
            lines.append(f"- {v.role}: ABSTAINED ({v.abstain_reason})")
        else:
            lines.append(f"- {v.role}: probability={v.probability:.2f} — {v.reasoning}")
    return "\n".join(lines) if lines else "(no committee views)"


def _default_client():
    return partial(call_claude, model=TRADER_MODEL)


def choose(
    snapshot: str,
    views: list[AnalystView],
    candidate_ids: list[str],
    client=None,
) -> tuple[str, str]:
    """Return (choice, reasoning). choice is a member of candidate_ids or
    "ABSTAIN". `client` is callable(prompt) -> LLMResponse, defaulting to
    `call_claude` bound to `claude-sonnet-5`."""
    if not candidate_ids:
        return "ABSTAIN", "no candidates were generated this cycle"

    client = client or _default_client()
    prompt = _TRADER_PROMPT.format(
        views=_render_views(views),
        candidate_ids=", ".join(candidate_ids),
        snapshot=snapshot,
    )
    response = client(prompt)

    if not response.ok:
        return "ABSTAIN", f"LLM failure: {response.error}"

    parsed = response.parsed or {}
    choice = parsed.get("choice")
    reasoning = str(parsed.get("reasoning", ""))

    if choice == "ABSTAIN":
        return "ABSTAIN", reasoning or "model chose to abstain"

    if not isinstance(choice, str) or choice not in candidate_ids:
        return "ABSTAIN", f"invalid candidate id from model: {choice!r}"

    return choice, reasoning
