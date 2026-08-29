# Credit-to-risk floor fix — report

## The defect

The cushion-spread ranking fix (Part B, `_structure_fill_order`) correctly
stopped clustering the surfaced menu at the tightest breakevens, but its
wide-cushion end turned out to be populated by far-OTM spreads with
negligible credit. Measured live on SPY, spot 769.35:

    c4  bear_call_spread  cushion 7.89%  credit $0.02  max_loss $498
    c6  bear_call_spread  cushion 8.54%  credit $0.02  max_loss $498

249:1 risk against reward. The blind reviewer caught both unprompted
("$0.02 credit against $498 max loss is indefensible risk/reward (249:1
against trader)"). The system was swapping one unusable menu (tightest
breakevens only) for another (worthless credit included).

## TDD evidence

Added 5 tests to `tests/test_snapshot.py` before touching `committee/snapshot.py`:

1. `test_credit_spread_with_negligible_credit_to_risk_is_not_surfaced` —
   `$0.02` credit / `$498` max_loss (the exact live case). Failed first
   (`thin` was surfaced) for the right reason — no filter existed yet.
2. `test_credit_spread_comfortably_above_the_floor_is_surfaced` — `$2.00`
   credit / `$300` max_loss (66.7% of risk). Passed trivially before the fix
   (nothing filtered anything yet) — encodes "don't over-filter."
3. `test_long_straddle_is_not_excluded_by_the_credit_floor` — a debit
   structure must survive this filter. Passed trivially before the fix, for
   the same reason as #2.
4. `test_credit_floor_emptying_a_structure_lets_the_rest_fill_the_cap` — all
   `bear_call_spread` candidates thin, `bull_put_spread` healthy; cap must
   still fill entirely from the surviving structure. **Failed first**
   (`bear_call_spread` was still present in the surfaced set) — the right
   failure.
5. `test_credit_spread_with_zero_or_negative_credit_is_not_surfaced` — added
   after a live run surfaced a real edge case my first implementation missed
   (see "One bug found and fixed" below).

Full run before the fix (`-k negligible`):

    AssertionError: assert TradeIntent(...max_loss=498.00...) not in dict_values([...])

i.e. the thin-credit candidate was present in the surfaced set — confirmed
failing for the intended reason, not a typo or setup error.

## The fix

`committee/snapshot.py`:

- `MIN_CREDIT_TO_RISK = 0.10` — a documented constant carrying the measured
  justification (the `$0.02`-against-`$498` case and the reviewer's exact
  words), so a later reader does not "clean it up."
- `CREDIT_STRUCTURES = {"bull_put_spread", "bear_call_spread", "iron_condor"}`
- `_drop_thin_credit(candidates, min_ratio)`: drops any `CREDIT_STRUCTURES`
  candidate where `net_credit * 100 < min_ratio * (max_loss / contracts)`.
  Long straddles (not in `CREDIT_STRUCTURES`) are untouched — they are
  already governed by `_drop_certain_denials`'s max-loss filter.
- Wired into `render_snapshot` right after `_drop_certain_denials`:
  `eligible = _drop_thin_credit(eligible, MIN_CREDIT_TO_RISK)`.

## One bug found and fixed during verification

My first implementation keyed the filter off `intent.is_credit`
(`net_credit > 0`), skipping the floor for anything that wasn't
strictly-positive credit — meaning to exempt debit structures. Running the
live dry-run diagnostic against the real SPY chain surfaced the hole: real
`bull_put_spread`/`bear_call_spread` candidates from illiquid far-OTM legs
priced to **exactly $0.00** or even **-$0.03** net credit. Those are credit
structures in exactly as much trouble as the $0.02 case (more, in the
negative case) but `is_credit` reads them as "not a credit trade" and let
them straight through, reopening the hole this fix exists to close.

Fixed by testing `intent.structure in CREDIT_STRUCTURES` instead of
`is_credit`'s sign. Added test 5 above to pin it. This is why the "before"
live menu below (captured with the `is_credit`-based version) still shows
`$0.00`/`$-0.03` credit rows — the "after" menu (structure-based version)
does not.

## Before / after: live surfaced menu (SPY, spot $769.35, 632 raw candidates)

**Before the fix** (max-loss filter only, ranking fix from the prior task):

    c4  bear_call_spread  cushion 7.89%  credit $0.02  max_loss $498  ratio 0.004
    c6  bear_call_spread  cushion 8.54%  credit $0.02  max_loss $498  ratio 0.004
    (plus 10 other candidates at healthier ratios)

**After the fix, with the `is_credit`-based bug** (intermediate, wrong):

    c4  bear_call_spread  cushion 7.10%  credit $0.00   max_loss $500.00  (slipped through — bug)
    c9  bull_put_spread   cushion 32.41% credit $0.00   max_loss $500.00  (slipped through — bug)
    c10 bull_put_spread   cushion 27.86% credit $-0.03  max_loss $503.00  (slipped through — bug, worse than original defect)

**After the fix, corrected (structure-based)** — full 12-candidate surfaced menu:

    id   structure          cushion   credit   max_loss   credit/risk
    c1   bear_call_spread    0.44%    $2.76    $224.00    1.2321
    c2   bear_call_spread    1.27%    $2.12    $288.50    0.7331
    c3   bear_call_spread    1.75%    $1.78    $322.00    0.5528
    c4   bear_call_spread    0.45%    $2.83    $217.00    1.3041
    c5   bear_call_spread    4.05%    $0.52    $448.00    0.1161
    c6   bear_call_spread    4.18%    $0.49    $451.00    0.1086
    c7   bull_put_spread     0.30%    $1.97    $302.55    0.6526
    c8   bull_put_spread     0.30%    $1.93    $307.50    0.6260
    c9   bull_put_spread     4.40%    $0.47    $453.00    0.1038
    c10  bull_put_spread     4.40%    $0.53    $446.50    0.1198
    c11  bull_put_spread     1.90%    $1.29    $371.50    0.3459
    c12  bull_put_spread     1.77%    $1.30    $370.00    0.3514

Every ratio is now ≥ 0.10 (the floor); no candidate below ~5% cushion is
dropped, and cushion still spans 0.30%–4.40% (both structures represented,
6+6 split — the stratified cap still works). No `$0.02`-style or zero/negative
credit rows remain anywhere in the surfaced set. `long_straddle` candidates
are still absent from this particular chain snapshot, but that is the
pre-existing max-loss filter (all had max_loss over the $1,000 cap on this
chain), unrelated to this change.

## Live committee run (`python3 scripts/run_session.py --dry-run`)

Ran multiple times against the live paper sandbox (flaky network, several
retries needed as warned) — final run:

    SPY spot $769.35 — 632 candidate(s)
    ATM IV (chain-derived, ~30 DTE): 11.78%
    mode: LLM COMMITTEE — vol_analyst + bear_adversary -> trader -> thesis veto + blind veto

    vol_analyst    p=0.63 — IV 2.27pp above realized favours the offered credit
                   spreads; best candidates (c1, c4, c7) show strong liquidity
                   and acceptable risk/reward, edge is moderate not extreme.
    bear_adversary p=0.40 — the vol edge assumes realized vol stays low; most
                   spreads have breakevens only 0.4-0.6% away, a routine 1-2%
                   SPY move in 27 days breaches them.
    aggregate probability: 0.52
    trader: c3 — bear call spread, cushion 1.75% (materially wider than the
            0.3-0.5% top picks) with by far the deepest liquidity, directly
            answering bear_adversary's tightness objection.

    veto thesis: PASS — net delta -8.46 consistent with bear call spread's
                 bearish thesis
    veto blind:  PASS — SPY 11.65 points below the 781 short strike, 32 DTE,
                 low realized vol (9.52%); 1.76% rally needed to breach —
                 conservative, well-supported by the vol regime.

    Selected bear_call_spread: credit $1.78, max loss $322.00
    position greeks: delta -8.5, vega -9.3
    book: 0 position(s), delta +0.0, vega +0.0, day P&L $+0.00, 0 consecutive losses
    guard: ALLOW — Within all limits: risk $322.00, 1/3 positions (1 contract(s) approved)
    DRY RUN — no order sent.

**Honest outcome: the committee did NOT abstain this run.** Both vetoes
passed, and RiskGuard returned ALLOW for c3 (bear_call_spread, credit
$1.78, max_loss $322.00, credit/risk 0.55 — well clear of the new floor).
This was not tuned to force a result — c3 was already in the surfaced menu
before this session started, at a healthy ratio; the fix only removed the
indefensible-ratio candidates, it did not add or favor any particular one.

## Verification

- `python3 -m pytest tests/ -q` → 519 passed (up from the 514 baseline; 5 new
  tests added, none removed or weakened).
- One existing test (`test_unsolvable_iv_is_rendered_as_explicitly_unavailable_not_omitted`)
  needed its synthetic fixture's `net_credit`/`max_profit` adjusted
  (0.0 → 1.0 / 10.0 → 100.0) because its hand-built `TradeIntent` used a
  placeholder zero credit unrelated to what the test actually checks (IV
  rendering) — it was incidentally tripping the new floor. The test's
  assertions were not touched or weakened, only the incidental fixture data.

## Not changed

Per instructions: the blind reviewer, thesis check, trader, and all analyst
prompts are untouched. Only `committee/snapshot.py` (the deterministic
candidate filter) and `tests/test_snapshot.py` were modified.

## Files touched

- `/home/prashant/trading-alpaca/committee/snapshot.py` — added
  `MIN_CREDIT_TO_RISK`, `CREDIT_STRUCTURES`, `_drop_thin_credit`; wired into
  `render_snapshot`; updated its docstring.
- `/home/prashant/trading-alpaca/tests/test_snapshot.py` — added 5 tests;
  adjusted one existing fixture's placeholder credit value (see above).

## What I could not do

Nothing left undone from the assigned scope. The sandbox network was flaky
as warned; it took several retries (some `timeout 120s` calls hung on the
Alpaca API and had to be re-run in the background with a longer timeout) to
get a clean live run, but a real committee decision with both vetoes and a
guard verdict was obtained.
