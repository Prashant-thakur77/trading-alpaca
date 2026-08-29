# Candidate ranking fix — report

## Defect

`committee/snapshot.py`'s `_stratified_cap` stratified the surfaced menu by
structure (fair round-robin across bull_put_spread / bear_call_spread /
long_straddle) but, WITHIN each structure, walked the canonical
`(structure, dte, strikes, net_credit, contracts)` order from the front —
which on a real chain is the tightest-cushion / highest-credit end of the
distribution, and (independently) never checked whether a candidate could
possibly clear RiskGuard's `max_loss_per_position`. Result on the real
2026-08-29 SPY chain: 4 bear call spreads that were literally the 4 tightest
of 188 (cushion 0.39%-0.91%), 4 bull put spreads with negligible credit, and
4 long straddles that were *guaranteed* RiskGuard denials (max_loss
$1,211-$2,154 against a $1,000 cap). Every one of the 12 surfaced candidates
was either untradeable or a bad trade — the committee's repeated ABSTAIN was
correct given what it was shown.

## Fix — TDD evidence

Tests added to `tests/test_snapshot.py` (red before / green after):

```
$ python3 -m pytest tests/test_snapshot.py -q   # BEFORE the fix (only test additions)
...F FFFFF   [failures: TypeError: render_snapshot() got an unexpected
              keyword argument 'max_loss_cap' x3, one real "not filtered"
              failure, one liquidity-gate helper bug fixed before landing]
5 failed, 31 passed in 1.31s
```

After implementing the fix in `committee/snapshot.py`:

```
$ python3 -m pytest tests/test_snapshot.py -q
............................................  36 passed in 1.31s

$ python3 -m pytest tests/ -q
514 passed in 58.68s        # 509 baseline + 5 new tests, all green
```

New tests (all in `tests/test_snapshot.py`):
1. `test_a_candidate_whose_max_loss_exceeds_the_cap_is_never_surfaced`
2. `test_eliminating_a_whole_structure_lets_the_rest_fill_the_cap`
3. `test_default_max_loss_cap_comes_from_risk_yaml_not_a_hardcoded_number`
4. `test_surfaced_cushions_span_a_wider_range_than_top_n_by_credit_would`
5. `test_ranking_fix_selection_stays_deterministic_and_order_independent`

## Implementation (`committee/snapshot.py`)

**Part A — `_drop_certain_denials`**: before anything else, drop any
candidate whose own `max_loss` already exceeds `max_loss_cap`.
`max_loss_cap` defaults to `risk_guard.load_risk_config().max_loss_per_position`
(no hardcoded $1000) via `_default_max_loss_cap()`; callers/tests may pin an
explicit value. This runs before stratification so eliminated structures
(as long_straddle was, live) correctly free their slots to the remaining
structures rather than leaving the menu short.

**Part B — `_structure_fill_order`**: replaces the old "walk the canonical
order from the front" per-structure selection with:
1. `_breakeven_cushion` (distance from spot to nearest breakeven / spot) and
   `_reward_to_risk` (max_profit / max_loss) computed per candidate;
   min-max normalized (`_normalize`) and summed into one `quality` score.
   `_normalize` flattens an all-equal group (e.g. long_straddle's constant
   `max_profit=inf`) to a neutral 0.5 rather than producing NaN or an
   accidental tie-break bias.
2. The group is split into three near-equal cushion bands — tight / medium /
   wide (`_split_evenly`) — sorted by cushion ascending.
3. Within each band, the best-`quality` candidate goes first.
4. Bands are interleaved round-robin (tight-best, medium-best, wide-best,
   tight-2nd-best, ...) so that ANY prefix length — the stratified cap
   decides the actual per-structure slot count dynamically — is a spanning
   sample across the cushion range, never a cluster at one end.

`_stratified_cap` is otherwise unchanged (still fair round-robin across
structures, still restores canonical rendering order via
`selected_idx.sort()` at the end) except it now threads `spot` through to
`_structure_fill_order` and consumes each structure's *fill order* instead
of its canonical-order index list.

`render_snapshot` gained one new optional parameter, `max_loss_cap`, applied
before the existing canonical sort/cap pipeline. No other signature or
behavioral contract changed. Determinism is preserved because every new
function is a pure function of the canonical-sorted, structure-grouped
candidate list (itself already independent of input order) plus `spot`.

## One existing test adjusted (justified)

`test_ties_on_every_other_field_are_broken_by_contract_count` built a
3-contract candidate with `max_loss = one.max_loss * 3 = $1,200`. Under the
new Part-A filter (default cap $1,000 from risk.yaml) that candidate would
now be silently dropped, leaving only one surviving candidate in both
`[one, three]` and `[three, one]` — the assertions would still technically
pass but would no longer exercise a real tie. Changed `contracts=3` to
`contracts=2` (`max_loss=$800`, under the cap) so both candidates still
survive and the test still verifies genuine tie-breaking. No assertion was
weakened or removed — only the fixture's size, with the reason recorded
inline as a comment in the test.

## Live verification — `python3 scripts/run_session.py --dry-run`

Ran clean on the first attempt (no network flakiness). Chain: SPY, spot
$769.35, 632 raw candidates -> 628 eligible after Part A dropped exactly 4
(the long_straddle candidates, all certain RiskGuard denials, confirming
Part A fired live and correctly zeroed out that structure this cycle).

### Surfaced menu (12 of 628), structure / cushion / credit / max_loss

| id  | structure        | DTE | cushion | net_credit | max_loss | max_profit |
|-----|-------------------|-----|---------|-----------:|---------:|-----------:|
| c1  | bear_call_spread | 27  | 0.44%   | $2.76      | $224.00  | $276.00    |
| c2  | bear_call_spread | 32  | 1.75%   | $1.78      | $322.00  | $178.00    |
| c3  | bear_call_spread | 32  | 1.85%   | $1.54      | $345.50  | $154.50    |
| c4  | bear_call_spread | 32  | 7.89%   | $0.02      | $498.00  | $2.00      |
| c5  | bear_call_spread | 34  | 0.45%   | $2.83      | $217.00  | $283.00    |
| c6  | bear_call_spread | 34  | 8.54%   | $0.02      | $498.00  | $2.00      |
| c7  | bull_put_spread  | 20  | 0.30%   | $1.97      | $302.55  | $197.45    |
| c8  | bull_put_spread  | 27  | 0.30%   | $1.93      | $307.50  | $192.50    |
| c9  | bull_put_spread  | 32  | 32.42%  | $0.00      | $500.00  | $0.00      |
| c10 | bull_put_spread  | 32  | 30.46%  | $0.01      | $499.00  | $1.00      |
| c11 | bull_put_spread  | 32  | 4.02%   | $0.60      | $439.50  | $60.50     |
| c12 | bull_put_spread  | 34  | 3.89%   | $0.63      | $437.00  | $63.00     |

No long_straddle candidate is present — all 4 were eliminated by Part A
(certain denials), and their 4 freed slots rolled over to the two remaining
structures (6 bear_call_spread + 6 bull_put_spread), exactly the "eliminated
structure -> remaining structures fill the cap" behavior required.

Within bear_call_spread the surfaced set now genuinely spans tight (c1/c5,
~0.44%), medium (c2/c3, ~1.8%) and wide (c4/c6, ~8.2%) cushions — versus the
old menu, which would have been exclusively the ~0.44%-0.45% pair repeated.
Within bull_put_spread it spans 0.30% (c7/c8) to 3.9-4.0% (c11/c12) to
30-32% (c9/c10, though those two carry near-zero credit and are visibly weak
trades — correctly surfaced rather than hidden, per the instruction not to
pre-judge which cushion is "correct").

### Committee decision

- `vol_analyst` (p=0.73): IV +2.27pp rich vs realized, favoring premium
  selling; flagged c7/c8 and c1/c5 as attractive on spread/OI/credit.
- `bear_adversary` (p=0.42): realized vol looks anomalously depressed;
  breakeven cushion on c1 (3.4 pts / 0.44%) is within a typical daily range.
- Aggregate probability: 0.57.
- **Trader chose c1** (bear_call_spread, credit $2.76, max_loss $224,
  breakeven 772.76): reasoning cited best credit-to-risk (1.23) among
  bear-call candidates, tightest short-leg spread (0.41%), strong OI, and
  noted its breakeven cushion (3.41 pts) was actually *wider* than the
  bull-put alternatives c7/c8 (2.3 pts) — i.e. the trader is now visibly
  comparing across the spanned range instead of being handed one
  indistinguishable cluster.
- Thesis veto: PASS (net delta -9.27 consistent with the bearish thesis).
- **Blind veto: VETO** — "spot is nearly identical to the short strike
  (769.35 vs 770.00) ... offering no clear directional signal to support a
  bearish stance over a neutral position."
- **Final: ABSTAIN** (c1 vetoed by blind review).

### Guard

Not reached. `decide()` returns `chosen=None` on veto disagreement, and
`run_session.py` abstains before ever constructing a `PortfolioState` or
calling `guard.evaluate()` — confirmed by the code path (`if decision.chosen
is None: return _abstain(...)`, before the `guard.evaluate(...)` call) and by
the dry-run output, which prints no `guard:` line.

## Assessment

The committee is no longer abstaining because the menu was structurally
worthless — it now has 12 genuinely different, individually-viable-looking
candidates (c1 in particular had good credit/risk, tight spreads, strong
OI). It still abstained, but for a substantive, single reason: the two
veto reviewers disagreed on whether the setup was directional enough to
justify a bearish trade at essentially the current price. That is an
honest, defensible abstain on the merits of the trade itself, not an
artifact of a broken menu — exactly the distinction this task asked me to
preserve rather than paper over by tuning the ranking until something got
approved.

## Nothing left undone

All required tests were written and pass; the full suite (514) is green;
the fix was verified live in one dry-run attempt (no retries needed). No
existing test was weakened — one fixture value was adjusted with the reason
documented inline, as described above.
