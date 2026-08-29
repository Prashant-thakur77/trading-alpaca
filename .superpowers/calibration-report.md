# Brier calibration loop — build report

Date: 2026-08-29

## What was built

1. **`calibration.py`** (new module)
   - `brier_score(predictions: list[tuple[float, bool]]) -> float | None` —
     standard Brier score (mean of `(p - outcome)**2`); `None` on an empty
     list, never a fabricated 0.
   - `resolved_predictions(journal, role) -> list[tuple[float, bool]]` —
     walks `journal.entries()`, correlates each role's `analyst_view`
     entries with their eventual outcome via the shared `snapshot_hash`,
     resolved by the first closing entry (`exit`/`close`/`fill`/
     `partial_fill`, matching `scripts/run_session.py`'s `CLOSING_TYPES`
     vocabulary) that carries a `realized_pnl`/`pnl` field. Abstaining views
     and unresolved cycles are excluded. Documented at the function that it
     returns `[]` on the live journal today, since no writer yet journals a
     closing entry with realized P&L (exit monitoring is PLAN.md Phase 3).
   - `analyst_weights(journal, roles, min_predictions=10) -> dict[str, float]`
     — recomputed fresh from the journal every call (no mutable state).
     Fewer than `min_predictions` resolved predictions → weight `1.0`
     (unproven, not demoted). Otherwise weight is a monotonically decreasing
     function of Brier score, floored at `WEIGHT_FLOOR = 0.2` so a demoted
     analyst is never silenced.

2. **`committee/analysts.py`** — `aggregate()` gained an optional `weights`
   parameter (role → weight). `weights=None` is byte-identical to the prior
   behaviour (regression test). With weights, abstaining views are still
   excluded from both numerator and denominator before any weight lookup
   happens — the abstention contract is untouched.

3. **`committee/decide.py`** — small, necessary addition: `analyst_view`
   journal entries now carry `snapshot_hash`, the join key
   `resolved_predictions` needs to correlate a prediction with its cycle
   (`committee_decision` entries already carried it; `analyst_view` did
   not). Covered by a new regression test in `tests/test_decide.py`.

4. **`scripts/calibration_report.py`** + **`make calibration`** — prints a
   per-analyst table (role, resolved count, Brier score, weight,
   interpretation) and an explicit honesty statement: when nothing has
   resolved, it says so in plain language rather than printing zeros that
   could pass for an actual score, and states outright that the weights are
   not yet wired into `decide()`'s live aggregation.

5. **README.md** — moved the "Calibration loop" bullet from *Planned, not
   yet built* to *Built*, phrased with the same honesty: mechanism built and
   tested, weights plug into `aggregate()`, but dormant on the live journal
   and not yet wired into the live decision path.

## TDD evidence

Every behavior was test-first: RED confirmed before implementation for each
of the three production changes.

- `tests/test_decide.py::test_each_analyst_view_entry_carries_the_cycles_snapshot_hash`
  — written first, run and confirmed to fail with
  `KeyError: 'snapshot_hash'`, then `committee/decide.py` was changed to add
  the field, then confirmed green (40/40 in `test_decide.py`).
- `tests/test_analysts.py` — 5 new `aggregate(..., weights=...)` tests
  written first, run and confirmed to fail with
  `TypeError: aggregate() got an unexpected keyword argument 'weights'`,
  then `aggregate()` was extended, then confirmed green (27/27).
- `tests/test_calibration.py` — 25 tests written first against a
  not-yet-existing `calibration` module, confirmed to fail on collection
  with `ModuleNotFoundError: No module named 'calibration'`, then
  `calibration.py` was implemented, then confirmed green on the first run
  (25/25) with no fix-up cycle needed.
- `tests/test_calibration_report.py` — 6 tests for the report's honesty
  language. These were written after the report script (not strict
  red-green), so a mutation check was run to prove they are not vacuous:
  the string `"DORMANT"` was mutated to `"READY"` in
  `scripts/calibration_report.py`, the corresponding test
  (`test_empty_journal_states_no_resolved_predictions_plainly`) failed as
  expected, and the mutation was reverted, restoring green.

Full suite: 556 passed (up from the 519 baseline; 37 new tests added:
25 + 6 in the two new calibration test files, plus 5 weighted-`aggregate`
tests in `test_analysts.py` and 1 snapshot-hash regression test in
`test_decide.py`, minus 0 removed — no existing test was weakened or
deleted, including the abstention-contract tests in `test_analysts.py`).

```
$ python3 -m pytest tests/ -q
============================= 556 passed in 57.12s ==============================
```

## `make calibration` output against the real journal

```
$ make calibration
python3 scripts/calibration_report.py
Calibration report — /home/prashant/trading-alpaca/logs/journal.jsonl

role              resolved    brier   weight   interpretation
----------------------------------------------------------------------------------------
vol_analyst              0      n/a     1.00   unproven (0/10 resolved) — weight defaults to 1.0, not demoted
bear_adversary            0      n/a     1.00   unproven (0/10 resolved) — weight defaults to 1.0, not demoted

No resolved predictions yet; weights default to 1.0 (unproven, not demoted). The calibration loop is BUILT but DORMANT: no writer in this codebase yet journals a closing trade entry (exit/close) with a realized P&L — exit monitoring has not been built (PLAN.md Phase 3). Nothing above is influencing committee votes yet.
```

This is the genuinely honest result: the live journal at
`logs/journal.jsonl` has one recorded cycle with `analyst_view` entries but
no closing entry, so `resolved_predictions` correctly returns nothing to
score. (The pre-existing `analyst_view` entries in that journal also predate
this change and lack `snapshot_hash` entirely — new cycles going forward
will carry it, but old ones can never retroactively resolve, matching hard
rule 5's "past entries are never rewritten.")

## What is, and is not, live

**Is live / built / tested:**
- Brier scoring arithmetic.
- Journal correlation machinery (`resolved_predictions`), tested against
  synthetic journals covering: correlated wins/losses, unresolved cycles,
  abstained views, role filtering, multiple cycles, both `realized_pnl`/`pnl`
  keys, closing entries with no P&L field, and unrelated entry types.
- Weight derivation with the unproven-floor and the wrong-floor, including a
  perfectly-calibrated analyst scoring above the equal-weight baseline.
- `aggregate()`'s optional weighting, with the abstention contract proven to
  still hold under weights.
- The report script and its honesty language, mutation-checked.

**Is NOT live:**
- No trade outcome has ever been journalled in this codebase — exit
  monitoring (closing a position and recording its realized P&L) is not
  built yet (PLAN.md Phase 3), so `resolved_predictions` and therefore
  `analyst_weights` return empty/`1.0` on every real cycle so far.
- `committee/decide.py` does not call `analyst_weights()` or pass `weights=`
  into `aggregate()` — the committee still aggregates equal-weighted in
  production. Wiring that in is a follow-up, not part of this task, and the
  README/report both say so explicitly rather than implying it is already
  happening.
