"""The committee orchestrator: one call that owns a whole decision cycle.

Before this module existed, `committee/` was a library nobody invoked —
`scripts/run_session.py` picked a candidate by a deterministic credit/max-loss
ratio and the agentic layer sat on the shelf. `decide()` is the single entry
point that wires the pieces together, and it owns three things that must not
be owned anywhere else:

1. **The id -> TradeIntent resolution (CLAUDE.md hard rule 1).** The ONLY way
   a `TradeIntent` leaves this module is `snapshot.candidates[choice_id]`.
   Never an index into a caller's list, never a re-sort, never a "best match".
   A live chain hands us ~600 candidates in no promised order; an orchestrator
   that resolved "c1" positionally would send a *different trade than the id
   names* while the id validated cleanly and every guard reported PASS. Hard
   rule 1 would be defeated silently, which is the worst way to defeat it.

2. **The journal trail (CLAUDE.md hard rule 5).** One entry per stage —
   snapshot, one analyst_view per analyst, trader_choice, veto,
   committee_decision — appended in that order. An ABSTAIN is journalled as
   fully as a trade: refusals are the product, and they are what the judge
   page replays. Every append goes through `_record`, which logs and swallows
   exactly as `executor_options._record` does: a full disk must not turn a
   sound decision into an exception.

3. **The prompt cache.** Every LLM call is keyed by `llm.client.prompt_hash`
   and served from disk when present. That one artifact is simultaneously the
   cost saver, the audit record (`LLMResponse` carries no prompt, so the
   wrapper supplies it) and the deterministic-replay corpus — so a cache hit
   must reproduce the identical decision, including a cached *failure*. A
   failure served from cache replays as a failure, which is what makes a
   refusal cycle replayable at all; the operational escape hatch for a live
   rate-limit is `run_session --no-llm`, not a cache that quietly forgets.

Fail closed throughout. Any exception, any LLM outage, any all-abstain, any
unresolvable id, and either veto failing produces `chosen=None` with a
populated `abstain_reason`. `decide()` never raises.
"""
import hashlib
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from candidate_builder import TradeIntent
from committee.analysts import ANALYST_MODEL, AnalystView, aggregate, run_analysts
from committee.snapshot import ATM_IV_NOT_SUPPLIED, render_snapshot
from committee.trader import TRADER_MODEL, choose
from committee.veto import BLIND_REVIEW_MODEL, blind_review, thesis_check
from llm.client import LLMResponse, call_claude, prompt_hash

logger = logging.getLogger(__name__)

ABSTAIN = "ABSTAIN"

# What the veto fields say when the cycle abstained before the veto layer was
# reached. Both `ok` flags stay False in that case: "not run" is not a pass.
NOT_RUN = "not reached — the cycle abstained before the veto layer"


@dataclass(frozen=True)
class CommitteeDecision:
    """The whole outcome of one committee cycle, trade or refusal.

    `choice_id` names what the DESK decided, so it is `"ABSTAIN"` whenever
    `chosen` is None — including when a specific candidate was picked and then
    vetoed. Which candidate was vetoed is recorded in `abstain_reason` and in
    the journal's `veto` entry; conflating "the trader liked c3" with "the desk
    chose c3" is how a vetoed trade gets executed by a careless reader.
    """
    chosen: TradeIntent | None
    choice_id: str
    views: tuple[AnalystView, ...]
    aggregate_probability: float | None
    trader_reasoning: str
    thesis_ok: bool
    thesis_reason: str
    blind_ok: bool
    blind_reason: str
    snapshot_hash: str
    abstain_reason: str


# ── journal + cache plumbing ─────────────────────────────────

def _record(journal, entry_type: str, payload: dict) -> None:
    """Append one journal entry. A journal failure must NEVER break a decision.

    Same contract and same rationale as `executor_options._record`: the
    journal is an audit obligation, not a precondition. Losing an entry is bad
    and is logged at error level; raising here would abort a cycle that has
    already reasoned correctly, or (worse, once orders exist) make a completed
    action look like it never happened. `journal=None` is the dry-run case: a
    rehearsal stays out of the judged chain.
    """
    if journal is None:
        return
    try:
        journal.append(entry_type, payload)
    except Exception as e:  # noqa: BLE001 - auditing must not break deciding
        logger.error("Journal write failed for %s: %s", entry_type, e, exc_info=True)


def _record_from_response(prompt: str, response: LLMResponse) -> dict:
    """The cache record for one call: prompt, model, raw response, parsed, error.

    `LLMResponse` deliberately carries no `prompt` field (it is an *answer*),
    so the prompt is supplied here. Without it the cache would be a cost saver
    only — unable to serve as either the audit record or the replay corpus,
    which is two thirds of why it exists.

    `timestamp` is written but never read back into a replayed `LLMResponse`,
    so it documents when a record was made without making replay
    time-dependent.
    """
    return {
        "prompt": prompt,
        "model": response.model,
        "ok": bool(response.ok),
        "raw_response": response.text,
        "parsed": response.parsed,
        "error": response.error,
        "cost_usd": response.cost_usd,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _response_from_record(record: dict, key: str) -> LLMResponse | None:
    """Rebuild an `LLMResponse` from a cached record, or None to treat as a miss.

    Anything unexpected — a non-dict `parsed`, a missing field, a record from
    an older schema with no `ok` flag — returns None so the caller simply
    re-runs the call and overwrites the bad record. A half-understood record
    must never be reconstructed into a *confident* answer.
    """
    try:
        if "ok" not in record or "raw_response" not in record:
            return None
        parsed = record.get("parsed")
        if parsed is not None and not isinstance(parsed, dict):
            return None
        return LLMResponse(
            ok=bool(record["ok"]),
            text=str(record["raw_response"]),
            parsed=parsed,
            model=str(record.get("model", "")),
            prompt_hash=key,
            error=str(record.get("error", "")),
            cost_usd=float(record.get("cost_usd", 0.0) or 0.0),
        )
    except (TypeError, ValueError, AttributeError) as e:
        logger.warning("Unusable cache record %s (%s) — treating as a miss", key, e)
        return None


def _cached_client(client, cache, model: str):
    """Bind `client` to one model and route it through the prompt cache.

    Returns the single-argument `callable(prompt) -> LLMResponse` that every
    committee role expects. The returned response always carries the cache key
    as its `prompt_hash`, so `AnalystView.prompt_hash` links to the on-disk
    artifact even if an injected client neglects to set it.
    """
    def call(prompt: str) -> LLMResponse:
        key = prompt_hash(model, prompt)

        if cache is not None:
            try:
                record = cache.get(key)
            except Exception as e:  # noqa: BLE001 - a bad cache is a miss
                logger.warning("Prompt cache read failed for %s: %s", key, e)
                record = None
            if record is not None:
                hit = _response_from_record(record, key)
                if hit is not None:
                    logger.info("Prompt cache hit (%s) — no LLM call made", model)
                    return hit

        response = replace(client(prompt, model=model), prompt_hash=key)

        if cache is not None:
            try:
                cache.put(key, _record_from_response(prompt, response))
            except Exception as e:  # noqa: BLE001 - caching must not break the call
                logger.error("Prompt cache write failed for %s: %s", key, e,
                             exc_info=True)
        return response

    return call


# ── the cycle ────────────────────────────────────────────────

def decide(underlying: str, spot: float, realized_vol: float,
           candidates: list[TradeIntent], journal, cache=None,
           client=call_claude, atm_iv=ATM_IV_NOT_SUPPLIED) -> CommitteeDecision:
    """Run one committee cycle. Never raises; abstains instead.

    `client` is `callable(prompt, model=...) -> LLMResponse`, defaulting to
    `llm.client.call_claude`. One factory rather than one bound client per
    role, because the roles deliberately run on different models (Haiku for
    the analysts and the blind reviewer, Sonnet for the trader) and a single
    pre-bound client would silently collapse that split.

    `atm_iv` is the market's ATM implied vol — the analysts' primary
    vol-regime signal — and is passed straight through to `render_snapshot`.
    It must come from `analytics.atm_implied_vol(chain, spot)`, computed once
    from the full option chain, never re-derived from `candidates` (see
    `committee.snapshot.render_snapshot` for why that biased the regime
    call). Omitting it falls back to that old, selection-dependent estimate.
    """
    try:
        return _decide_inner(underlying, spot, realized_vol, candidates,
                             journal, cache, client, atm_iv)
    except Exception as e:  # noqa: BLE001 - fail closed, always
        reason = f"committee raised {type(e).__name__}: {e}"
        logger.error("Committee cycle failed: %s", reason, exc_info=True)
        _record(journal, "committee_decision",
                {"choice_id": ABSTAIN, "underlying": underlying,
                 "abstain_reason": reason})
        return CommitteeDecision(
            chosen=None, choice_id=ABSTAIN, views=(), aggregate_probability=None,
            trader_reasoning="", thesis_ok=False, thesis_reason=NOT_RUN,
            blind_ok=False, blind_reason=NOT_RUN, snapshot_hash="",
            abstain_reason=reason,
        )


def _decide_inner(underlying, spot, realized_vol, candidates, journal, cache,
                  client, atm_iv=ATM_IV_NOT_SUPPLIED) -> CommitteeDecision:
    candidates = list(candidates)
    snapshot = render_snapshot(underlying, spot, realized_vol, candidates,
                               atm_iv=atm_iv)
    snapshot_hash = hashlib.sha256(snapshot.text.encode("utf-8")).hexdigest()

    _record(journal, "snapshot", {
        "underlying": underlying,
        "spot": spot,
        "realized_vol": realized_vol,
        "candidate_count": len(snapshot.candidates),
        "total_candidates": len(candidates),
        "snapshot_hash": snapshot_hash,
    })

    def abstain(reason: str, *, views=(), agg=None, trader_reasoning="",
                thesis=(False, NOT_RUN), blind=(False, NOT_RUN)) -> CommitteeDecision:
        """Build, journal and return a refusal. Every early exit goes here so
        no path can return an abstention that was never written down."""
        _record(journal, "committee_decision", {
            "choice_id": ABSTAIN,
            "underlying": underlying,
            "structure": None,
            "aggregate_probability": agg,
            "thesis_ok": thesis[0],
            "blind_ok": blind[0],
            "snapshot_hash": snapshot_hash,
            "abstain_reason": reason,
        })
        return CommitteeDecision(
            chosen=None, choice_id=ABSTAIN, views=tuple(views),
            aggregate_probability=agg, trader_reasoning=trader_reasoning,
            thesis_ok=thesis[0], thesis_reason=thesis[1],
            blind_ok=blind[0], blind_reason=blind[1],
            snapshot_hash=snapshot_hash, abstain_reason=reason,
        )

    # Nothing to choose between: refuse before spending a single paid call.
    if not snapshot.candidates:
        return abstain("no candidate survived the deterministic build — "
                       "nothing for the committee to choose between")

    # The two analysts are independent and each is a 6-20s subprocess call, so
    # `run_analysts` runs them concurrently (measured 1.96x). Results come back
    # in analyst order regardless of completion order.
    views = tuple(run_analysts(
        snapshot.text, client=_cached_client(client, cache, ANALYST_MODEL)))
    for view in views:
        _record(journal, "analyst_view", {
            "role": view.role,
            "probability": view.probability,
            "abstained": view.abstained,
            "abstain_reason": view.abstain_reason,
            "reasoning": view.reasoning,
            "model": view.model,
            "prompt_hash": view.prompt_hash,
            # The join key calibration.py uses to correlate this prediction
            # with the cycle's eventual outcome (a later closing entry
            # carrying the same snapshot_hash). See calibration.py.
            "snapshot_hash": snapshot_hash,
        })

    agg = aggregate(list(views))
    if agg is None:
        reasons = "; ".join(f"{v.role}: {v.abstain_reason}" for v in views) or "no analysts ran"
        return abstain(f"every analyst abstained ({reasons})", views=views)

    choice_id, trader_reasoning = choose(
        snapshot.text, list(views), snapshot.candidate_ids,
        client=_cached_client(client, cache, TRADER_MODEL))
    _record(journal, "trader_choice", {
        "choice_id": choice_id,
        "aggregate_probability": agg,
        "reasoning": trader_reasoning,
    })

    if choice_id == ABSTAIN:
        return abstain(f"trader abstained: {trader_reasoning}",
                       views=views, agg=agg, trader_reasoning=trader_reasoning)

    # ── HARD RULE 1 ──────────────────────────────────────────
    # The one and only route from an id to a trade. `.get` on the snapshot's
    # own mapping: no index, no re-sort, no fallback to "the closest one".
    chosen = snapshot.candidates.get(choice_id)
    if chosen is None:
        return abstain(
            f"trader returned {choice_id!r}, which is not an id in this cycle's "
            f"snapshot ({', '.join(snapshot.candidate_ids)}) — treating a "
            f"hallucinated id as an abstention",
            views=views, agg=agg, trader_reasoning=trader_reasoning)

    # Both reviewers always run, even when the first has already failed: the
    # judge page shows both verdicts, and a blank second opinion is
    # indistinguishable from one that was skipped.
    try:
        thesis_ok, thesis_reason = thesis_check(chosen, spot)
    except Exception as e:  # noqa: BLE001 - an unverifiable thesis is a veto
        thesis_ok, thesis_reason = False, f"thesis check raised {type(e).__name__}: {e}"
    try:
        blind_ok, blind_reason = blind_review(
            chosen, spot, realized_vol,
            client=_cached_client(client, cache, BLIND_REVIEW_MODEL))
    except Exception as e:  # noqa: BLE001 - silence from the reviewer is a veto
        blind_ok, blind_reason = False, f"blind review raised {type(e).__name__}: {e}"

    _record(journal, "veto", {
        "thesis_ok": thesis_ok, "thesis_reason": thesis_reason,
        "blind_ok": blind_ok, "blind_reason": blind_reason,
        "choice_id": choice_id, "structure": chosen.structure,
    })

    if not (thesis_ok and blind_ok):
        failures = []
        if not thesis_ok:
            failures.append(f"thesis check: {thesis_reason}")
        if not blind_ok:
            failures.append(f"blind review: {blind_reason}")
        return abstain(
            f"{choice_id} ({chosen.structure}) vetoed — " + "; ".join(failures),
            views=views, agg=agg, trader_reasoning=trader_reasoning,
            thesis=(thesis_ok, thesis_reason), blind=(blind_ok, blind_reason))

    _record(journal, "committee_decision", {
        "choice_id": choice_id,
        "underlying": chosen.underlying,
        "structure": chosen.structure,
        "contracts": chosen.contracts,
        "aggregate_probability": agg,
        "thesis_ok": thesis_ok,
        "blind_ok": blind_ok,
        "snapshot_hash": snapshot_hash,
        "abstain_reason": "",
    })
    return CommitteeDecision(
        chosen=chosen, choice_id=choice_id, views=views,
        aggregate_probability=agg, trader_reasoning=trader_reasoning,
        thesis_ok=thesis_ok, thesis_reason=thesis_reason,
        blind_ok=blind_ok, blind_reason=blind_reason,
        snapshot_hash=snapshot_hash, abstain_reason="",
    )
