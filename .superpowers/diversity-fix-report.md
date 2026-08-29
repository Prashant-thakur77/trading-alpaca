# Diversity + veto-display fixes — report

## Finding 1 — stratified candidate cap (committee/snapshot.py)

### TDD evidence

Added tests to `tests/test_snapshot.py`:
- `test_every_present_structure_type_appears_in_the_surfaced_set`
- `test_single_structure_present_still_fills_the_cap`
- `test_shuffling_the_input_does_not_change_the_stratified_surfaced_set`
- `test_a_scarce_structure_does_not_starve_the_others_of_slots`

Before the fix, run against the old `capped = ordered[:max_candidates]`:

```
tests/test_snapshot.py::test_every_present_structure_type_appears_in_the_surfaced_set FAILED
  AssertionError: {'bear_call_spread', 'bull_put_spread'} == {..., 'long_straddle'}
  Extra items in the right set: 'long_straddle'
tests/test_snapshot.py::test_a_scarce_structure_does_not_starve_the_others_of_slots FAILED
  KeyError: 'long_straddle'
```

Both failed for the intended reason: the global top-N sorts by structure name
first, so with 5 bull_put + 5 bear_call + 2 straddle candidates, straddle
(alphabetically last) never made the cap; with a single straddle candidate
mixed among 20 spreads, it was dropped entirely. The other two new tests
(single-structure fill, shuffle-invariance) passed even before the fix,
since they don't exercise the bug — expected, they guard against
regressions in the new code, not the old bug.

After implementing `_stratified_cap` (round-robin across structures present,
grouped from the already-canonically-sorted list, skip exhausted groups,
re-sort the selected subset back into canonical order): all 4 new tests
plus all 21 pre-existing `test_snapshot.py` tests pass (25/25).

### Design

`_stratified_cap(ordered, max_candidates)`:
1. Groups `ordered` (already sorted by the canonical key) by `structure`,
   preserving each group's internal canonical order.
2. Iterates structures in `sorted(groups)` order (alphabetical on the
   structure name — deterministic, independent of input order).
3. Round-robins one candidate per present structure per round, skipping a
   structure once its group is exhausted, until `max_candidates` is reached
   or every group is exhausted.
4. Returns the selected candidates re-sorted into the original canonical
   order (index-based, so equal-comparing duplicates aren't collapsed).

This satisfies determinism (selection depends only on the canonical sort
key, never on input list order — proved by the shuffle test), preserves the
existing sort as the within-structure ordering, and is a no-op — byte
identical to the previous behaviour — whenever nothing needs to be dropped
(total candidates <= max_candidates), which is why zero pre-existing tests
needed to change.

### Before/after structure counts (real SPY chain, 2026-08-29, live via
`AlpacaData.from_env()` + `build_candidates` + `render_snapshot`, default
`max_candidates=12`)

```
spot: $769.35
built total:    632  {bull_put_spread: 440, bear_call_spread: 188, long_straddle: 4}
surfaced BEFORE: 12  {bear_call_spread: 12}
surfaced AFTER:  12  {bear_call_spread: 4, bull_put_spread: 4, long_straddle: 4}
```

All three structures are now represented; the scarce structure
(long_straddle, only 4 built) is included in full rather than starved out.

### Live committee decision after the fix

Ran `python3 scripts/run_session.py --dry-run` against the real, current
chain (spot $769.35, 10 DTE window). Result:

```
vol_analyst      p=0.63 — Implied volatility is 0.72pp above realized, favoring
                 premium-selling; bear call spreads (especially c1) offer
                 superior liquidity and risk-reward relative to long
                 straddles in this modest-vol-edge regime.
bear_adversary   p=0.55 — ... liquidity/breach concerns re: c1 ...
aggregate probability: 0.59
trader: c4 — same premium-selling structure with the widest cushion and
             deepest OI, better surviving a normal-vol move or forced unwind.
veto thesis: PASS — net delta -15.73 consistent with bear call spread's
             bearish thesis
veto blind:  PASS — realized vol 9.52%, tight DTE, betting against a 0.73%
             upside move is reasonable.
Selected bear_call_spread: credit $1.35, max loss $365.00
guard: ALLOW — 1 contract(s) approved
```

The committee did NOT abstain this time, and it did NOT pick the straddle —
but this run's actual market regime is the opposite of the one described in
the finding: IV is currently 0.72pp ABOVE realized (favouring premium
SELLING), not below it. Critically, `vol_analyst` explicitly weighed the
long straddle by name ("... superior liquidity and risk-reward relative to
long straddles ...") and reasoned it away on liquidity grounds — which is
exactly the fix's purpose: the straddle is now a real, visible option that
gets evaluated and can lose the argument, instead of being structurally
absent from the menu. I could not reproduce the original IV-below-realized
regime on demand (spot market data, not something this fix controls), so I
cannot report a live "committee sees straddle AND regime favours buying AND
picks it" run — that would require the market to actually be in that
state at run time.

## Finding 2 — veto "not run" display (scripts/run_session.py)

### TDD evidence

Added to `tests/test_run_session_main.py`:
- `TestNotRunVetoRendering::test_not_run_veto_does_not_read_as_a_fired_veto_or_a_pass`
- `TestNotRunVetoRendering::test_a_veto_that_actually_ran_still_renders_pass_or_veto`

Before the fix:

```
FAILED test_not_run_veto_does_not_read_as_a_fired_veto_or_a_pass
AssertionError: assert 'not run' in
  '    veto thesis: VETO — not reached — the cycle abstained before the veto layer'
```

Failed for the intended reason — the line led with the literal word `VETO`.

### Fix

Added `_veto_verdict(ok, reason)` in `scripts/run_session.py`: when
`reason == NOT_RUN` (imported from `committee.decide`, the single source of
truth for that string), it renders `not run (cycle abstained before the veto
layer)` instead of `VETO — ...`; otherwise it renders `PASS — ...` /
`VETO — ...` exactly as before. `CommitteeDecision.thesis_ok` /
`blind_ok` / `*_reason` are untouched — display-only change.

After the fix, both new tests pass, and the pre-existing dry-run
transcript tests (`TestCommitteeDryRun`) still pass unchanged, including
`test_a_failed_veto_is_shown_as_failed` which still requires `"VETO"` to
appear for an *actually run and failed* veto.

## Full suite

`python3 -m pytest tests/ -q` → 497 passed (was 491 before this work; +4 for
Finding 1, +2 for Finding 2). No existing test was weakened or deleted.

## Commits

1. `90d9e0a` — fix: veto not-run display no longer reads as a fired veto
2. `43d4b56` — fix: stratify the snapshot candidate cap by structure

## What I could not do

- Could not force the live market into the IV-below-realized regime the
  finding described, so the "committee picks the straddle live" scenario is
  demonstrated structurally (the straddle is visible and gets argued about)
  but not as a live executed pick — that depends on market state I don't
  control.
