"""The pre-mortem: LLM reasoning compiled into enforceable exit rules.

Before an order is sent, the desk asks one question — *what would have to be
true for this trade to lose money?* — and then does the thing that makes the
question worth asking: it **compiles the answer into deterministic,
machine-checkable exit triggers** that `exit_monitor.monitor_positions`
evaluates on every subsequent cycle.

That compilation step is the entire point. A paragraph of plausible reasoning
attached to a trade is worth nothing, because nobody reads it again; a
`dte_below(3)` the monitor actually checks is worth a great deal. This is the
same bargain the rest of the desk strikes with the models everywhere else:
the LLM contributes judgement, deterministic code holds the pen.

**The model may not invent trigger kinds, any more than the trader may invent
a strike (CLAUDE.md hard rule 1).** It fills in values for the fixed set in
`TRIGGER_KINDS`, and every one comes back through `_validate`:

  * an unrecognised kind is discarded;
  * a non-numeric or non-finite threshold is discarded;
  * a value that is nonsensical *for this structure* is discarded — an
    `underlying_beyond` on the winning side of spot (a bull put spread does
    not lose to a rally), an `iv_spike` below current realized vol (it would
    fire the moment the trade opened), a `dte_below` past the trade's own
    expiry, a `credit_decay` on a structure that received no credit.

Every discard is logged with its reason. Nothing is silently coerced into
something acceptable: a trigger this module cannot verify is a trigger it
does not emit.

Two rules are not the model's to decide:

  1. **`dte_below` at 3 is always present.** Short ITM legs are assigned into
     stock at expiry on paper (design spec A.5), which is exactly the failure
     that freezes a desk. Assignment avoidance is a hard rule, so the forced
     trigger is added after validation and cannot be argued away, removed, or
     duplicated.
  2. **LLM failure never means "no triggers".** A timeout, an unparseable
     answer or a raising client falls back to `deterministic_triggers` — the
     50%-of-credit profit target and the 3-DTE exit — every one of them
     carrying `PREMORTEM_UNAVAILABLE` in its rationale so a judge reading the
     journal can tell a fallback from a real pre-mortem at a glance. (Max
     loss is enforced unconditionally by the monitor and is not expressible
     as one of these four kinds, so it is not represented here.)

`premortem()` never raises.
"""
import logging
import math
from dataclasses import dataclass

from llm.client import call_claude

logger = logging.getLogger(__name__)

KIND_UNDERLYING_BEYOND = "underlying_beyond"
KIND_IV_SPIKE = "iv_spike"
KIND_DTE_BELOW = "dte_below"
KIND_CREDIT_DECAY = "credit_decay"

#: The complete vocabulary. The model chooses from this and nothing else.
TRIGGER_KINDS = (
    KIND_UNDERLYING_BEYOND, KIND_IV_SPIKE, KIND_DTE_BELOW, KIND_CREDIT_DECAY,
)

#: Assignment avoidance. Not an opinion (design spec A.5, PLAN.md Phase 3).
FORCED_DTE_BELOW = 3.0

#: Profit target: close once half the credit received has been captured.
DEFAULT_PROFIT_TARGET = 0.5

#: Marker written into every fallback rationale.
PREMORTEM_UNAVAILABLE = "pre-mortem unavailable"

#: A pre-mortem is a handful of failure modes. A model returning fifty is
#: not reasoning, and fifty triggers on one position is a way to churn a book
#: rather than protect it.
MAX_MODEL_TRIGGERS = 6

#: An `underlying_beyond` level must sit inside this band around spot. A
#: level outside it is either a typo or a scenario ("SPY goes to a penny")
#: that no exit rule can usefully express.
SANITY_BAND = 0.5           # +/- 50% of spot

#: An `iv_spike` threshold above this is not a volatility forecast.
MAX_IV_THRESHOLD = 5.0      # 500% annualized

PREMORTEM_MODEL = "claude-haiku-4-5"

#: Structures whose loss comes from the underlying moving DOWN / UP. A
#: long straddle appears in neither: it loses to the underlying standing
#: still, so no single "beyond" level describes its failure.
_DOWNSIDE_STRUCTURES = {"bull_put_spread"}
_UPSIDE_STRUCTURES = {"bear_call_spread"}
_TWO_SIDED_STRUCTURES = {"iron_condor"}


@dataclass(frozen=True)
class ExitTrigger:
    """One machine-checkable exit condition.

    `rationale` is the pre-mortem sentence that produced it, carried through
    verbatim so the journal (and the judge page) can show *why* a position
    was closed, in the words of the reasoning that anticipated it.
    """
    kind: str
    threshold: float
    rationale: str


_PROMPT = """You are running a PRE-MORTEM on a defined-risk US-equity options \
trade that is about to be sent. Assume it is some weeks later and this trade \
has LOST MONEY. Your job is to say what would have had to be true for this \
trade to lose money, and then to express each of those failure modes as a \
numeric exit threshold this desk can check automatically.

THE TRADE
UNDERLYING: {underlying}
STRUCTURE: {structure}
SPOT: {spot:.2f}
REALIZED_VOL: {realized_vol:.4f} ({realized_vol_pct:.2f}% annualized)
LEGS: {legs}
NET_CREDIT (per share, negative = debit paid): {net_credit:.2f}
MAX_LOSS (position dollars): {max_loss:.2f}
BREAKEVENS: {breakevens}
DTE: {dte}

YOU MAY ONLY USE THESE FOUR TRIGGER KINDS. You cannot invent a kind, write \
code, or name an indicator that is not listed here:

  "{k_underlying}" — threshold is a PRICE of the underlying. The trade is \
exited once spot reaches that level on the losing side. Only give a level on \
the side that HURTS this structure.
  "{k_iv}"        — threshold is an implied-volatility LEVEL as a decimal \
(0.35 = 35% annualized). The trade is exited if the position's own implied \
vol rises to it. It must be above the realized vol given above.
  "{k_dte}"       — threshold is a NUMBER OF DAYS to expiry. The trade is \
exited once fewer days remain. Must be no greater than the DTE above.
  "{k_credit}"    — threshold is a FRACTION between 0 and 1 of the credit \
received. The trade is exited once that fraction of the credit has been \
captured. Only meaningful when a credit was received.

Respond with ONLY a JSON object, no prose outside it:
{{"failure_modes": [{{"kind": "<one of the four above>", "threshold": <number>, \
"rationale": "<the one sentence of the pre-mortem that produced this \
threshold>"}}]}}

Give at most {max_triggers} entries, each a genuinely different failure mode. \
If you cannot identify a checkable failure mode, return \
{{"failure_modes": []}}.
"""


def _default_client():
    from functools import partial
    return partial(call_claude, model=PREMORTEM_MODEL)


def _finite(value) -> float | None:
    """A threshold is a number or it is nothing. Bools are not numbers."""
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _short_strikes(intent) -> list[float]:
    return [leg.quote.strike for leg in intent.legs if leg.side == "sell"]


def _validate(kind, threshold, rationale, intent, spot, realized_vol
              ) -> ExitTrigger | None:
    """Turn one model-proposed failure mode into a trigger, or reject it.

    Returns None (with a logged reason) for anything this module cannot
    verify. Nothing is repaired, clamped or coerced into acceptability: a
    threshold we had to correct is a threshold the model did not actually
    mean, and enforcing a number nobody chose is worse than enforcing none.
    """
    kind = str(kind or "")
    if kind not in TRIGGER_KINDS:
        logger.warning("Pre-mortem trigger discarded: unrecognised kind %r "
                       "(allowed: %s)", kind, ", ".join(TRIGGER_KINDS))
        return None

    value = _finite(threshold)
    if value is None:
        logger.warning("Pre-mortem trigger discarded: %s threshold %r is not a "
                       "finite number", kind, threshold)
        return None

    reason = _reject_reason(kind, value, intent, spot, realized_vol)
    if reason:
        logger.warning("Pre-mortem trigger discarded: %s(%s) — %s",
                       kind, value, reason)
        return None

    return ExitTrigger(kind=kind, threshold=value, rationale=str(rationale or ""))


def _reject_reason(kind, value, intent, spot, realized_vol) -> str:
    """Why this value is nonsense for this structure — "" if it is sound."""
    if kind == KIND_UNDERLYING_BEYOND:
        return _reject_underlying(value, intent, spot)

    if kind == KIND_IV_SPIKE:
        if value <= 0 or value > MAX_IV_THRESHOLD:
            return (f"an implied-vol level must be within (0, "
                    f"{MAX_IV_THRESHOLD}]")
        if value <= (realized_vol or 0.0):
            return (f"it is at or below current realized vol "
                    f"{realized_vol:.4f}, so it would fire on entry")
        return ""

    if kind == KIND_DTE_BELOW:
        if value <= 0:
            return "a days-to-expiry threshold must be positive"
        if value > intent.dte:
            return (f"the trade only has {intent.dte} DTE, so this would fire "
                    f"before it was open")
        return ""

    if kind == KIND_CREDIT_DECAY:
        if not (0.0 < value < 1.0):
            return "a credit-decay fraction must be strictly inside (0, 1)"
        if intent.net_credit <= 0:
            return (f"{intent.structure} received no credit "
                    f"(net_credit {intent.net_credit:.2f})")
        return ""

    return f"no validation rule for {kind}"    # unreachable; fail closed


def _reject_underlying(value, intent, spot) -> str:
    if spot is None or spot <= 0:
        return "no usable spot to place the level against"
    if not (spot * (1 - SANITY_BAND) <= value <= spot * (1 + SANITY_BAND)):
        return (f"{value} is outside +/-{SANITY_BAND:.0%} of spot {spot:.2f} — "
                f"not a level any exit rule can act on")

    structure = intent.structure
    if structure in _DOWNSIDE_STRUCTURES:
        if value >= spot:
            return (f"{structure} loses to the DOWNSIDE; {value} is at or above "
                    f"spot {spot:.2f}, the winning side")
        return ""
    if structure in _UPSIDE_STRUCTURES:
        if value <= spot:
            return (f"{structure} loses to the UPSIDE; {value} is at or below "
                    f"spot {spot:.2f}, the winning side")
        return ""
    if structure in _TWO_SIDED_STRUCTURES:
        # Both sides hurt an iron condor, but a level inside the short strikes
        # is inside the profit zone, where the position is winning.
        shorts = _short_strikes(intent)
        if shorts and min(shorts) < value < max(shorts):
            return (f"{value} sits between the short strikes "
                    f"{min(shorts)}/{max(shorts)} — inside the profit zone")
        if value == spot:
            return "a level exactly at spot has no side"
        return ""

    return (f"{structure} does not lose money by the underlying reaching a "
            f"level (a long straddle loses by it standing still)")


def deterministic_triggers(intent, reason: str = "") -> tuple[ExitTrigger, ...]:
    """The exits this desk applies whatever any model says.

    Used both as the LLM-failure fallback and as the floor under every
    successful pre-mortem. `reason` is prefixed to each rationale so a
    fallback is distinguishable from a real pre-mortem in the journal.

    Max loss is deliberately absent: it is not expressible as one of the four
    kinds, and `exit_monitor` enforces it unconditionally rather than as a
    trigger that could be dropped.
    """
    prefix = f"{reason}; " if reason else ""
    triggers = []
    if intent is not None and intent.net_credit > 0:
        triggers.append(ExitTrigger(
            KIND_CREDIT_DECAY, DEFAULT_PROFIT_TARGET,
            f"{prefix}deterministic profit target: close once "
            f"{DEFAULT_PROFIT_TARGET:.0%} of the credit received is captured",
        ))
    triggers.append(ExitTrigger(
        KIND_DTE_BELOW, FORCED_DTE_BELOW,
        f"{prefix}hard rule: close at {FORCED_DTE_BELOW:.0f} DTE to avoid "
        f"assignment of a short leg into stock",
    ))
    return tuple(triggers)


def _render_prompt(intent, spot: float, realized_vol: float) -> str:
    legs = "; ".join(
        f"{leg.side} {leg.contracts}x {leg.quote.strike:.2f}{leg.quote.right}"
        for leg in intent.legs
    )
    return _PROMPT.format(
        underlying=intent.underlying,
        structure=intent.structure,
        spot=spot,
        realized_vol=realized_vol,
        realized_vol_pct=realized_vol * 100,
        legs=legs,
        net_credit=intent.net_credit,
        max_loss=intent.max_loss,
        breakevens=", ".join(f"{b:.2f}" for b in intent.breakevens),
        dte=intent.dte,
        k_underlying=KIND_UNDERLYING_BEYOND,
        k_iv=KIND_IV_SPIKE,
        k_dte=KIND_DTE_BELOW,
        k_credit=KIND_CREDIT_DECAY,
        max_triggers=MAX_MODEL_TRIGGERS,
    )


def premortem(intent, spot: float, realized_vol: float, client=None
              ) -> tuple[ExitTrigger, ...]:
    """Ask what would sink this trade; return enforceable triggers.

    `client` is `callable(prompt) -> LLMResponse`, defaulting to
    `call_claude` bound to `PREMORTEM_MODEL` — injected everywhere in tests
    and routed through the committee's prompt cache in production, so a
    replayed cycle re-derives the identical triggers.

    Never raises. The returned tuple always contains the forced 3-DTE exit,
    and on any LLM failure it is exactly `deterministic_triggers(...,
    PREMORTEM_UNAVAILABLE)`.
    """
    try:
        return _premortem_inner(intent, spot, realized_vol, client)
    except Exception as e:  # noqa: BLE001 - a broken pre-mortem still protects
        logger.error("Pre-mortem raised %s: %s — falling back to the "
                     "deterministic exits", type(e).__name__, e, exc_info=True)
        return deterministic_triggers(intent, reason=PREMORTEM_UNAVAILABLE)


def _premortem_inner(intent, spot, realized_vol, client) -> tuple[ExitTrigger, ...]:
    client = client or _default_client()
    response = client(_render_prompt(intent, spot, realized_vol))

    if not getattr(response, "ok", False):
        logger.warning("Pre-mortem LLM failure (%s) — deterministic exits only",
                       getattr(response, "error", "unknown"))
        return deterministic_triggers(intent, reason=PREMORTEM_UNAVAILABLE)

    parsed = getattr(response, "parsed", None)
    if not isinstance(parsed, dict):
        logger.warning("Pre-mortem output was not a JSON object (%s) — "
                       "deterministic exits only", type(parsed).__name__)
        return deterministic_triggers(intent, reason=PREMORTEM_UNAVAILABLE)

    modes = parsed.get("failure_modes")
    if not isinstance(modes, list):
        logger.warning("Pre-mortem 'failure_modes' was %s, expected a list — "
                       "deterministic exits only", type(modes).__name__)
        return deterministic_triggers(intent, reason=PREMORTEM_UNAVAILABLE)

    kept: list[ExitTrigger] = []
    for mode in modes[:MAX_MODEL_TRIGGERS]:
        if not isinstance(mode, dict):
            logger.warning("Pre-mortem entry discarded: %s is not an object",
                           type(mode).__name__)
            continue
        trigger = _validate(mode.get("kind"), mode.get("threshold"),
                            mode.get("rationale"), intent, spot, realized_vol)
        if trigger is not None:
            kept.append(trigger)

    return _finalise(kept, intent)


def _finalise(kept: list[ExitTrigger], intent) -> tuple[ExitTrigger, ...]:
    """Add the non-negotiable exits, de-duplicate, and freeze the order.

    The deterministic triggers are added AFTER validation so no model output
    can suppress them, and any model trigger of the same (kind, threshold) is
    dropped in their favour — the hard-rule rationale is the one worth
    keeping. A model may still make an exit *earlier* (a `credit_decay` of
    0.3, a `dte_below` of 10 alongside the forced 3); it can never make one
    later, because the deterministic trigger it would have to remove is still
    there and the monitor fires on the first one that trips.
    """
    forced = list(deterministic_triggers(intent))
    seen = {(t.kind, t.threshold) for t in forced}
    out = list(forced)
    for trigger in kept:
        if (trigger.kind, trigger.threshold) in seen:
            continue
        seen.add((trigger.kind, trigger.threshold))
        out.append(trigger)
    return tuple(out)
