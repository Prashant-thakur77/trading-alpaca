"""Brier calibration loop — the desk grades itself.

Each committee analyst emits a *probability*, not a verdict
(`committee.analysts.AnalystView`). Once a cycle's outcome resolves, its
prediction can be scored against reality with the Brier score, and analysts
that are confidently wrong lose voting weight while analysts that are simply
unproven do not. This is what makes "grades itself" literal rather than a
slogan (design spec Sect. 4.3, "Calibration weights").

Everything here reads the journal fresh on every call — weights are never
stored as mutable state, because the journal is the only source of truth
(hard rule 5) and a cached weight could silently go stale.

**Honesty note, read before wiring this in anywhere:** `resolved_predictions`
correlates an `analyst_view` entry with its eventual outcome via the
`snapshot_hash` both entries carry. Today nothing in this codebase journals a
closing entry (`exit`/`close`/`fill`/`partial_fill`) with a realized P&L —
there is no exit monitoring yet (PLAN.md Phase 3) — so this machinery
legitimately finds **zero** resolved predictions against the live journal.
That is the correct, honest answer, not a bug: it must never be papered over
with a plausible-looking default. `analyst_weights` therefore returns 1.0
for every role until real outcomes exist to grade.
"""
from dataclasses import dataclass

# A role with fewer resolved predictions than this is "unproven", not "bad":
# demoting on a small sample is exactly the statistical error the rest of
# this project has been careful to avoid (see risk_manager.py, validate.py).
DEFAULT_MIN_PREDICTIONS = 10

# An analyst never loses its vote entirely — a demoted analyst that later
# improves needs a path back, and silencing it would remove the evidence
# that it improved.
WEIGHT_FLOOR = 0.2

# brier_score() of an analyst who always says 0.5: the reference point a
# weight of exactly 1.0 (equal-weighted, today's status quo) is pinned to.
BASELINE_BRIER = 0.25

# How strongly a Brier score away from the baseline moves the weight. Chosen
# so that a perfectly calibrated analyst (score 0.0) gets weight 1.5 and a
# maximally wrong one (score 1.0) hits the floor — see
# tests/test_calibration.py::TestAnalystWeights for the worked cases.
_WEIGHT_SENSITIVITY = 2.0

# Journal entry types that may carry the realized outcome of a cycle. Mirrors
# scripts/run_session.py's CLOSING_TYPES — the same vocabulary the rest of
# the codebase already uses for "this entry might resolve a trade".
_CLOSING_TYPES = {"close", "exit", "fill", "partial_fill"}
_PNL_KEYS = ("realized_pnl", "pnl")


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def brier_score(predictions: list[tuple[float, bool]]) -> float | None:
    """Mean squared error of probability vs. binary outcome.

    Standard Brier score: mean((p - outcome)**2). 0.0 is perfect, 1.0 is
    maximally wrong, 0.25 is what always saying 0.5 scores regardless of
    outcomes — the reference an analyst has to beat to be worth more than an
    equal-weighted coin flip.

    Returns `None` for an empty prediction list — never a default number
    that could be misread as an actual score of 0.
    """
    if not predictions:
        return None
    return sum((p - float(outcome)) ** 2 for p, outcome in predictions) / len(predictions)


def resolved_predictions(journal, role: str) -> list[tuple[float, bool]]:
    """Every (probability, outcome) pair this role has actually been graded on.

    Walks `journal.entries()` once, in order. An `analyst_view` entry for
    `role` is correlated with a cycle outcome by `snapshot_hash` — the same
    hash the analyst_view and committee_decision entries for one cycle both
    carry (`committee/decide.py`). The outcome comes from the first closing
    entry (`exit`/`close`/`fill`/`partial_fill`, mirroring
    `scripts/run_session.py`'s `CLOSING_TYPES`) that carries the same
    `snapshot_hash` and a `realized_pnl` or `pnl` field: profitable
    (pnl > 0) resolves to `True`, otherwise `False`.

    Excluded, deliberately:
      - abstaining views (no probability was actually offered — CLAUDE.md
        hard rule 2, "ABSTAIN is first-class");
      - views whose cycle never resolves (no matching closing entry, or one
        with no P&L field — e.g. an opening `fill`).

    **Returns `[]` until real trades close.** No writer in this codebase yet
    journals a closing entry with a realized P&L — exit monitoring has not
    been built (PLAN.md Phase 3) — so on the live journal this function
    correctly and honestly finds nothing to grade. That is not a placeholder
    to silence; it is the accurate state of the system.
    """
    entries = journal.entries()

    outcomes: dict[str, bool] = {}
    for entry in entries:
        if entry.get("type") not in _CLOSING_TYPES:
            continue
        payload = entry.get("payload") or {}
        snapshot_hash = payload.get("snapshot_hash")
        if not snapshot_hash:
            continue
        pnl = next((_as_float(payload[k]) for k in _PNL_KEYS if k in payload), None)
        if pnl is None:
            continue
        # First resolution wins; a cycle resolves once.
        outcomes.setdefault(snapshot_hash, pnl > 0)

    predictions: list[tuple[float, bool]] = []
    for entry in entries:
        if entry.get("type") != "analyst_view":
            continue
        payload = entry.get("payload") or {}
        if payload.get("role") != role or payload.get("abstained"):
            continue
        snapshot_hash = payload.get("snapshot_hash")
        if not snapshot_hash or snapshot_hash not in outcomes:
            continue
        probability = _as_float(payload.get("probability"))
        if probability is None:
            continue
        predictions.append((probability, outcomes[snapshot_hash]))

    return predictions


def _weight_from_score(score: float) -> float:
    """Lower Brier -> higher weight; floors at WEIGHT_FLOOR, never zero."""
    raw = 1.0 + (BASELINE_BRIER - score) * _WEIGHT_SENSITIVITY
    return max(WEIGHT_FLOOR, raw)


def analyst_weights(journal, roles, min_predictions: int = DEFAULT_MIN_PREDICTIONS
                     ) -> dict[str, float]:
    """Voting weight per role, recomputed fresh from the journal every call.

    Never stored as mutable state — the journal is the source of truth, so a
    cached weight could go stale the moment a new outcome resolves.

    - Fewer than `min_predictions` resolved predictions -> weight `1.0`.
      Unproven is not the same as bad; demoting on a tiny sample is exactly
      the statistical error the rest of this project avoids elsewhere
      (walk-forward validation, RiskGuard's fail-closed defaults).
    - Otherwise, weight is a monotonically decreasing function of Brier
      score, floored at `WEIGHT_FLOOR` — a demoted analyst still speaks but
      barely votes, and never loses its vote entirely, because it needs a
      path back if it improves.

    On the live journal today this returns 1.0 for every role: see
    `resolved_predictions`'s docstring for why that is the honest answer,
    not a bug.
    """
    weights: dict[str, float] = {}
    for role in roles:
        predictions = resolved_predictions(journal, role)
        if len(predictions) < min_predictions:
            weights[role] = 1.0
            continue
        score = brier_score(predictions)
        weights[role] = _weight_from_score(score)
    return weights
