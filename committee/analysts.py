"""Two committee analyst roles, each a thin prompt wrapped around `llm.client`.

Written for defined-risk US-equity options, not the inherited crypto prompt
(`ai_prompts.py`'s single-shot classifier, describing a cryptocurrency
analyst with EMA/MACD TA and an unearned "derived from 1006 trade analyses"
claim — deleted, not adapted, per design spec section 8).

  - `vol_analyst` judges whether implied vol looks rich or cheap versus
    realized, and whether premium-selling structures are favoured: IV rank,
    the realized-vs-implied spread, term structure, and liquidity (open
    interest, quote width).
  - `bear_adversary` is deliberately adversarial: its job is to find the
    failure mode of the setup the committee is leaning towards (assignment
    risk, event calendar, gap risk), not to agree with it.

Both analysts run on `claude-haiku-4-5` (cheap, several calls per cycle).
Neither analyst is shown the other's reasoning here — `committee/debate.py`
(a later piece) is where cross-talk happens.

CLAUDE.md hard rule 2 (ABSTAIN is first-class) governs every failure path:
a timeout, a non-zero exit, unparseable JSON, an explicit model abstention,
a missing/non-numeric probability, or a probability outside [0, 1] all
produce `AnalystView(abstained=True, ...)` with a reason — never a fabricated
number. This is the "fail soft on the LLM" half of spec 4.4.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial

from llm.client import call_claude

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnalystView:
    """One analyst's opinion on the current cycle's candidate set."""
    role: str
    # P(the favoured trade is profitable), 0..1 — or **None** when this view
    # abstained. It is deliberately NOT 0.0: a consumer reading `.probability`
    # alone would read a 0.0 as maximum bearishness, i.e. an abstention would
    # masquerade as the strongest possible opinion. `None` cannot be misread,
    # and any arithmetic on it fails loudly. INVARIANT: abstained is True
    # if and only if probability is None.
    probability: float | None
    abstained: bool
    abstain_reason: str
    reasoning: str
    model: str
    prompt_hash: str


_VOL_ANALYST_PROMPT = """You are the volatility analyst on a defined-risk US-equity \
options desk. You are given a market snapshot and a fixed list of \
already-built candidate spreads/structures (you cannot invent one).

Judge whether implied volatility looks rich or cheap versus realized \
volatility, and whether the snapshot favours a premium-selling structure \
(credit spread, iron condor) or a long-volatility structure (straddle). \
Weigh: the realized-vs-implied spread, IV rank if inferable, term structure, \
and liquidity (open interest, quote width) of the candidates shown. Also \
consider assignment risk and any event-calendar risk implied by the DTE.

Respond with ONLY a JSON object, no prose outside it:
{{"probability": <0.0-1.0, P the favoured structure is profitable>, \
"reasoning": "<one or two sentences>"}}
If you cannot form a view from the data given, respond instead with:
{{"abstain": true, "reason": "<why>"}}

MARKET SNAPSHOT:
{snapshot}
"""

_BEAR_ADVERSARY_PROMPT = """You are the bear adversary on a defined-risk \
US-equity options desk. Your job is to argue AGAINST the trade the snapshot \
seems to favour — find its failure mode, not agree with it. You are given a \
market snapshot and a fixed list of already-built candidate spreads/structures \
(you cannot invent one).

Consider: assignment/early-exercise risk on short legs, gap risk into any \
event implied by the DTE, whether liquidity (open interest, quote width) is \
thin enough to make an exit expensive, and whether realized volatility \
suggests the move needed to breach a breakeven is plausible within the DTE \
window.

Respond with ONLY a JSON object, no prose outside it:
{{"probability": <0.0-1.0, P the favoured structure is STILL profitable \
despite your case against it>, "reasoning": "<your strongest bearish case, \
one or two sentences>"}}
If you cannot form a view from the data given, respond instead with:
{{"abstain": true, "reason": "<why>"}}

MARKET SNAPSHOT:
{snapshot}
"""

ANALYST_MODEL = "claude-haiku-4-5"


def _default_client():
    return partial(call_claude, model=ANALYST_MODEL)


def _view_from_response(role: str, response) -> AnalystView:
    if not response.ok:
        return AnalystView(
            role=role, probability=None, abstained=True,
            abstain_reason=f"LLM failure: {response.error}", reasoning="",
            model=response.model, prompt_hash=response.prompt_hash,
        )

    # `llm.client` guarantees a dict or None, but this is the safety-critical
    # boundary: a non-dict here (a replayed cache record, a hand-built
    # response) must abstain, not raise AttributeError mid-cycle.
    if not isinstance(response.parsed, dict):
        return AnalystView(
            role=role, probability=None, abstained=True,
            abstain_reason=f"model output was not a JSON object: "
                           f"{type(response.parsed).__name__}",
            reasoning="", model=response.model, prompt_hash=response.prompt_hash,
        )
    parsed = response.parsed

    if parsed.get("abstain"):
        return AnalystView(
            role=role, probability=None, abstained=True,
            abstain_reason=str(parsed.get("reason", "model abstained")),
            reasoning="", model=response.model, prompt_hash=response.prompt_hash,
        )

    raw_p = parsed.get("probability")
    try:
        probability = float(raw_p)
    except (TypeError, ValueError):
        return AnalystView(
            role=role, probability=None, abstained=True,
            abstain_reason=f"malformed probability in model output: {raw_p!r}",
            reasoning="", model=response.model, prompt_hash=response.prompt_hash,
        )

    if not (0.0 <= probability <= 1.0):
        return AnalystView(
            role=role, probability=None, abstained=True,
            abstain_reason=f"probability out of range [0,1]: {probability}",
            reasoning="", model=response.model, prompt_hash=response.prompt_hash,
        )

    return AnalystView(
        role=role, probability=probability, abstained=False, abstain_reason="",
        reasoning=str(parsed.get("reasoning", "")), model=response.model,
        prompt_hash=response.prompt_hash,
    )


def vol_analyst(snapshot: str, client=None) -> AnalystView:
    """IV-rich-vs-cheap judgement. `client` is a callable(prompt) -> LLMResponse,
    defaulting to `call_claude` bound to `claude-haiku-4-5`."""
    client = client or _default_client()
    response = client(_VOL_ANALYST_PROMPT.format(snapshot=snapshot))
    return _view_from_response("vol_analyst", response)


def bear_adversary(snapshot: str, client=None) -> AnalystView:
    """Argues against the trade; finds the failure mode. Same client contract
    as `vol_analyst`."""
    client = client or _default_client()
    response = client(_BEAR_ADVERSARY_PROMPT.format(snapshot=snapshot))
    return _view_from_response("bear_adversary", response)


ANALYSTS = (vol_analyst, bear_adversary)


def run_analysts(snapshot: str, client=None, analysts=ANALYSTS) -> list[AnalystView]:
    """Run every analyst on the same snapshot, concurrently, in a stable order.

    The roles are independent by design — neither sees the other's reasoning
    (that is `committee/debate.py`'s job) — but each is a `claude -p`
    subprocess call of 6-20s, so running them in sequence cost the sum of
    their latencies for nothing. Threads, not processes: the work is entirely
    waiting on a subprocess.

    Results come back in `analysts` order regardless of which finished first,
    so the committee's input stays deterministic. A worker that raises becomes
    an abstention, never an exception escaping into the cycle.
    """
    with ThreadPoolExecutor(max_workers=max(len(analysts), 1)) as pool:
        futures = [pool.submit(fn, snapshot, client) for fn in analysts]
        views = []
        for fn, future in zip(analysts, futures):
            try:
                views.append(future.result())
            except Exception as e:  # noqa: BLE001 - fail soft on the LLM
                role = getattr(fn, "__name__", "analyst")
                logger.error("Analyst %s raised: %s", role, e, exc_info=True)
                views.append(AnalystView(
                    role=role, probability=None, abstained=True,
                    abstain_reason=f"analyst raised {type(e).__name__}: {e}",
                    reasoning="", model=ANALYST_MODEL, prompt_hash="",
                ))
    return views


def aggregate(views: list[AnalystView]) -> float | None:
    """Weighted mean probability, excluding abstaining analysts from BOTH the
    numerator and the denominator.

    Currently equal-weighted (weight=1 per active analyst); per-analyst
    calibration weights land in the later `calibration.py` module and would
    plug in here without changing this contract. If every analyst abstained,
    returns None — the caller must abstain too. A genuine 0.5 is a view; an
    abstention is not, so it must never silently pull the mean toward 0.5 by
    being included as a phantom neutral vote.
    """
    active = [v for v in views if not v.abstained]
    if not active:
        return None
    return sum(v.probability for v in active) / len(active)
