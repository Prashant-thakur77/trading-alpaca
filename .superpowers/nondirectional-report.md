# A non-directional long-premium structure — and the debit-side quality floor

**Date:** 2026-08-30
**Suite:** 911 → 957 passing (+46). Zero existing tests weakened or deleted.
**Commit:** `635f2aa` candidates: debit-side quality floor + long iron butterfly

**Bottom line, up front.** Both fixes landed and are provable at the layer they
act on. The debit floor removes 385 of 991 surviving live candidates — 371
lottery tickets and 14 spreads priced at more than the full width, including
`bear_put_spread`s carrying a **negative** debit. The `long_iron_butterfly`
builds, surfaces (2 of 12 live slots; 1–2 of 12 in every replay window), passes
the neutral veto band, and clears the $1,000 cap at $618–$858 where the straddle
it is built from costs $2,270+.

On the same 10 June–July windows the abstention rate is **8/10 — identical to
the 8/10 baseline**. But the desk did something it had never done: on
**2026-07-02** the committee **chose the long iron butterfly** (`c12`), and the
independent blind reviewer vetoed it on its economics — not on a
misunderstanding of the structure. And on **2026-07-13**, previously an
abstention, it traded a **debit vertical** for the first time (−$52). The new
structure itself was **never traded**. Nothing was tuned. Full numbers in §5.

---

## 1. Fix 1 — the debit mirror of `_drop_thin_credit`

### What the defect was

`_drop_thin_credit` drops a CREDIT structure whose reward is a negligible
fraction of its risk (`MIN_CREDIT_TO_RISK = 0.10`). Debit structures had no
equivalent, so the previous change's own report recorded, unfixed:

> `c9 bull_call_spread  credit=$-0.04  max_loss=$4.00  max_profit=$496.00`

`_structure_fill_order` ranks on `_reward_to_risk = max_profit / max_loss`,
which for a $4 debit is 124:1 — so these ranked **top** of their band and were
actively selected, spending a surfaced slot on a trade the market is pricing at
essentially zero probability of paying off.

### The rule, and where its number comes from

I did **not** copy `0.10` across as a new "10% of width" constant. The credit
rule reads:

```
reward  >=  MIN_CREDIT_TO_RISK * risk        (credit collected vs capital at risk)
```

A debit vertical is the mirror image of the credit vertical struck at the same
two strikes: the debit paid is the risk, and the width less the debit is the
reward. So the mirror of the rule is the **same inequality with the two terms
exchanged**:

```
risk  >=  MIN_CREDIT_TO_RISK * reward
```

Because risk + reward always sum to the full width for these structures, that
is exactly `debit >= width / 11` ≈ **9.1% of width** — the conventional ~10%
desk floor for a vertical, *arrived at* rather than picked.

**The upper bound uses the same constant, not a second one.** Applying the ratio
the other way (`reward >= 0.10 * risk`) caps the debit at 10/11 ≈ **90.9% of
width**. I deliberately did not invent the 60–70% figure the brief floated: a
debit vertical at 91% of width risks $4.55 to make $0.45, which is the identical
10:1-against reward/risk the blind reviewer once called "indefensible" on the
credit side, reached from the opposite end. One constant, two directions. It
also removes the degenerate `debit >= width` case, which cannot profit at any
settlement price. **It is not vacuous** — it bound 14 live candidates (§1.3).

### Where the number lives, and why

It stays a module constant in `committee/snapshot.py` — in fact the *same*
constant — rather than moving to `risk.yaml`. `risk.yaml` is the source of truth
for limits **RiskGuard enforces**, and the guard never reads this one. It is a
question about which candidates deserve one of the committee's twelve slots, not
about what may be sent to a broker; a number in `risk.yaml` that no guard
evaluates would read as a limit and be neither. `_default_max_loss_cap()` pulls
from `risk.yaml` for the opposite reason: that number *is* a guard limit, and
duplicating it here would let the two drift.

### 1.3 Measured live, on one SPY chain (2026-08-30, spot $769.35)

```
built              1483  {bear_call 188, bear_put 467, bull_call 212,
                          bull_put 440, long_iron_butterfly 172, straddle 4}
after max-loss cap 1362  (all 4 straddles + 117 butterflies dropped)
after thin-credit   991
after thin-debit    606   <- 385 dropped by the new rule
```

```
dropped by _drop_thin_debit: 385
  {bear_put_spread: 311, bull_call_spread: 60, long_iron_butterfly: 14}
  too-cheap (debit < ~9.1% of width): 371
  too-rich  (debit > ~90.9% of width): 14
```

Cheapest dropped — and this is the find worth naming:

```
  bear_put_spread  dte=19 width=5.00 debit=$-0.03 max_loss=$  -3.00 max_profit=$503.00
  bear_put_spread  dte=31 width=5.00 debit=$-0.03 max_loss=$  -2.50 max_profit=$502.50
```

A *debit* vertical priced to a **net credit**, i.e. a **negative max_loss**.
That is the exact debit-side twin of the negative-credit credit spreads
`CREDIT_STRUCTURES` was made a name-set to catch, and it was surfaceable until
now. Richest dropped:

```
  long_iron_butterfly dte=26 width=4.00 debit=$4.00 max_loss=$400.00 max_profit=$  0.00
  long_iron_butterfly dte=26 width=5.00 debit=$4.88 max_loss=$487.50 max_profit=$ 12.50
```

Live surfaced menu after the floor — no lottery ticket survives; the cheapest
debit vertical is now $0.46 on 5-wide (9.2% of width), where the previous pass
surfaced $0.04 and $0.07:

```
c4 bear_put_spread  credit=$-0.46 max_loss=$ 46.00   c7 bull_call_spread  credit=$-1.54 max_loss=$154.00
c5 bear_put_spread  credit=$-1.11 max_loss=$111.00   c8 bull_call_spread  credit=$-0.58 max_loss=$ 58.00
c6 bear_put_spread  credit=$-0.47 max_loss=$ 47.00
```

### TDD evidence (red first, every case)

8 tests in `tests/test_snapshot.py`; the 5 marked below failed before the
change, the 3 marked (guard) pin behaviour the rule must NOT break:

| Test | Red symptom |
|---|---|
| `test_a_lottery_ticket_debit_vertical_is_dropped` | the $0.04/5-wide `bull_call_spread` was surfaced |
| `test_a_lottery_ticket_bear_put_spread_is_dropped_too` | same, $0.07 `bear_put_spread` |
| `test_a_debit_vertical_priced_at_nearly_the_full_width_is_dropped` | $4.70/5-wide (risk $470 / reward $30) surfaced |
| `test_the_debit_floor_uses_the_same_ratio_as_the_credit_floor` | imports `MIN_CREDIT_TO_RISK` and computes the w/11 boundary; $0.43 must drop, $0.47 must survive |
| `test_a_long_iron_butterfly_that_cannot_profit_is_dropped` | debit == width, max_profit $0, surfaced |
| `test_a_debit_vertical_paying_a_real_fraction_of_width_survives` | (guard against over-filtering) |
| `test_long_straddle_is_not_dropped_by_the_debit_floor` | (guard: unbounded max_profit has no width) |
| `test_credit_structures_are_untouched_by_the_debit_floor` | (guard: family isolation) |

`DEBIT_WIDTH_STRUCTURES` is a **structure-name set**, matching `CREDIT_STRUCTURES`
and for the same recorded reason. `long_straddle` is deliberately absent: its
`max_profit` is `inf`, so it has no width to measure a debit against, and
applying the ratio to it would erase the structure — the precise bug class that
produced the original 72% abstention rate.

---

## 2. Fix 2 — `long_iron_butterfly`

Buy the ATM straddle, sell both wings. Also known as a reverse iron butterfly;
equivalently a long call vertical plus a long put vertical sharing the body
strike. Named `long_iron_butterfly` because it is the LONG-premium member of the
family — the conventional "iron butterfly" is the credit version.

### Payoff arithmetic, verified then asserted

Fixture: body 450, wings ±5 (width 5); body 450c mid 5.05 + 450p mid 4.05 =
9.10 paid, wings 455c mid 4.05 + 445p mid 3.05 = 7.10 received → **net debit
2.00**.

| Quantity | Asserted | Test |
|---|---|---|
| `net_credit` | `-2.00` (NEGATIVE, matching `build_long_straddle`) | `test_is_a_net_debit` |
| `is_credit` | `False` | `test_is_not_a_credit_trade` |
| `max_loss` | `$200.00` = debit × 100 × contracts | `test_max_loss_is_the_debit_paid` |
| `max_profit` | `$300.00` = (width − debit) × 100 | `test_max_profit_is_width_minus_debit` |
| identity | `max_loss + max_profit == width × 100` | `test_payoff_terms_sum_to_the_full_width` |
| `breakevens` | `(448.0, 452.0)` = **body** ± debit | `test_has_two_breakevens_at_body_plus_minus_debit` |
| ordering | `445 < 448 < 450 < 452 < 455` | `test_breakevens_straddle_the_body_inside_the_wings` |
| 3 contracts | `max_loss $600`, `max_profit $900` | `test_scales_with_contracts` |
| `is_defined_risk` | `True` | `test_is_defined_risk` |
| no naked short | short 455c covered by long 450c; short 445p by long 450p | `test_every_short_leg_is_covered_by_a_long_of_the_same_right` + the shared `test_no_naked_short_legs` loop |
| delta | `|net delta| < 15` at spot 450 | `test_is_delta_neutral_by_construction` |
| vega | `net vega > 0` (long volatility) | `test_is_long_volatility` |

**One correction to the brief, stated rather than quietly absorbed.** The brief
asked for "two breakevens, short strike ± net debit". For the structure it
describes (long the ATM straddle, short the wings) the breakevens are the
**body** strike ± net debit — the body is the LONG strike here, not a short one.
The brief's other clause is exactly right and is asserted as written: max profit
*is* first achieved **at** a short strike (either wing), and is flat beyond it.
I verified this from the expiry payoff before writing the code: at S = body,
every leg expires worthless and the entire debit is lost — that is the **max
loss**, and it is why the structure is long volatility.

Structural failures raise `ValueError` (wrong rights, split body, asymmetric
wings, a wing on the wrong side, mismatched expiries); a liquidity failure
returns `None` — matching every other builder. Asymmetric wings are refused
rather than priced because with unequal widths max profit differs by side, so a
single `width` would silently misstate one of them.

### Wiring — and the one non-obvious decision

| Layer | Change | Red symptom before it |
|---|---|---|
| `risk.yaml` `structures.allowed` | `+ long_iron_butterfly` | `test_allowlist_covers_every_structure_the_builder_can_produce` failed → guard `DENY` by name |
| `scripts/run_session.py::_build_candidates_for_expiry` | new enumeration loop | `test_chain_yields_a_non_directional_long_premium_structure` failed |
| `committee/snapshot.py` | in `DEBIT_WIDTH_STRUCTURES`; participates in `_stratified_cap`; named in `NET_CREDIT_CONVENTION` | filter/cap tests |
| `committee/veto.py::thesis_check` | added to `_NEUTRAL_STRUCTURES` | fell through to `unknown structure ... — failing closed`, vetoing 100% of them |
| `committee/premortem.py` | deliberately in **no** side-map; fall-through message generalised | already correct; now pinned by 5 tests |
| `scripts/replay.py::rebuild_intent` | reconstruction branch | a future fixture carrying one would raise `ScenarioError` |

**The wing offset is swept, not fixed.** Every other structure pairs legs at a
single `width`. For a butterfly the wing offset dominates the economics and the
right offset is a property of the chain, not a constant: a wing one strike out
barely reduces the debit (the structure then costs ~the full width and cannot
pay for itself), while a wing a full expected-move out leaves a debit above the
cap. Measured on the live chain, a 5-wide SPY butterfly costs **86.5% of width**
— unusable — while a 13-wide costs 66.0%. Enumerating every symmetric offset the
ladder supports lets the two deterministic filters keep whichever offsets
actually clear both the max-loss cap and `_drop_thin_debit`, instead of this loop
guessing one. Offsets are taken in sorted order, so enumeration stays
order-independent.

### `thesis_check` and the size-scaled neutral band — confirmed, not assumed

`NEUTRAL_DELTA_THRESHOLD` is still `15.0` per contract and the band is still
`NEUTRAL_DELTA_THRESHOLD * max(intent.contracts, 1)`. Three tests pin it for the
butterfly: it is judged against the neutral band (`"neutral band" in reason`),
it is not an unknown structure, and the verdict is `[True, True, True]` across
1/2/3 contracts. The measured live thesis reasons show the band being applied,
not bypassed.

### `premortem` — it loses by standing still

The butterfly is deliberately in **none** of `_DOWNSIDE_STRUCTURES`,
`_UPSIDE_STRUCTURES` or `_TWO_SIDED_STRUCTURES`. Like a straddle, it loses by
the underlying *standing still*, so no single "underlying beyond X" level
describes its failure — a level on either side names the side it is **winning**
on. Four tests pin this: an `underlying_beyond` above spot is discarded, one
below spot is discarded, no `credit_decay` trigger is generated (it pays a
debit), and the forced 3-DTE exit still applies. The fall-through rejection
message was generalised to name both structures.

---

## 3. Live dry-run structure mix

`python3 scripts/run_session.py --dry-run` — clean, exit 0, no order sent.
SPY spot **$769.35**, realized vol 9.52%, ATM IV 11.98%, **IV − RV = +2.46pp**.

```
built     1483  {bear_call_spread: 188, bear_put_spread: 467, bull_call_spread: 212,
                 bull_put_spread: 440, long_iron_butterfly: 172, long_straddle: 4}
surfaced    12  {bear_call_spread: 3, bear_put_spread: 3, bull_call_spread: 2,
                 bull_put_spread: 2, long_iron_butterfly: 2}
long-premium slots surfaced: 7/12   (was 6/12 last pass, 0/12 before that)
```

```
c11 long_iron_butterfly  credit=$-6.18  max_loss=$618.00  max_profit=$182.00
c12 long_iron_butterfly  credit=$-8.58  max_loss=$858.05  max_profit=$441.95
```

Both clear `max_loss_per_position: 1000.0`. The straddle they are built from is
$2,270+ and all 4 were dropped by `_drop_certain_denials`, exactly as before.
**The desk now has a non-directional long-premium structure it can actually
afford.**

Today's committee correctly chose a `bear_call_spread` (`c2`, credit $2.83, max
loss $217): IV is **above** realized today, which argues for *selling* premium.
Guard `ALLOW`, thesis veto PASS (net delta −7.98), blind veto PASS, pre-mortem
plan built, payload printed, nothing sent. The vol analyst's own words —
`"avoid debit structures (c4–c8, c11–c12) which pay up to enter in rich vol"` —
show it reading the butterflies by id and rejecting them **for the right
reason**, which is the outcome you want on a +2.46pp day.

---

## 4. Regression surface

- `python3 -m pytest tests/ -q` → **957 passed** (from 911). Nothing weakened.
- `python3 scripts/replay.py --all --verify` → **exit 0** (`allow`, `deny`,
  `downsize`, `fail_closed`).
- Same with `date.today()` frozen at **+3d (2026-09-02)** and **+30d
  (2026-09-29)** → **exit 0** both.
- `render_snapshot` determinism and shuffle-invariance re-pinned WITH
  butterflies in the mix (`random.Random(7)`, `random.Random(11)`; both `.text`
  and `.candidates` byte-identical). Existing determinism tests untouched.
- `make validate` → exit 0. `make verify` → exit 0.
  `scripts/verify_journal.py` → chain INTACT.
- **Not touched:** `executor_options.py`, `exit_monitor.py`, `options_orders.py`.
  `build_mleg_payload` computes `price = -intent.net_credit` and already handles
  4-leg orders (iron condor), so it prices the butterfly as a debit unchanged.
- `logs/seed_journal.jsonl` and `logs/journal.jsonl` untouched — the measurement
  below wrote to a scratch journal.

---

## 5. The 10-window measurement

### Setup

`scripts/seed_calibration.py --symbol SPY --start 2026-06-15 --end 2026-07-24
--max-windows 10 --spacing 3`, written to a **scratch** journal and artifact.
The identical dates the previous report used. **33 LLM calls, 0 served from the
prompt cache** — ten genuinely fresh committee runs, not replays of recorded
answers. $1.99 spent. Nothing was tuned between the runs; the only difference is
this change.

### Result: the abstention rate did NOT fall

| | Abstained | Rate |
|---|---|---|
| **BASELINE** (previous report, same 10 dates) | 8/10 | **80%** |
| **AFTER** (this change) | 8/10 | **80%** |

Matched date by date:

| date | BASELINE | AFTER |
|---|---|---|
| 2026-06-15 | ABSTAIN | ABSTAIN |
| 2026-06-18 | ABSTAIN | ABSTAIN |
| 2026-06-24 | ABSTAIN | ABSTAIN |
| 2026-06-29 | ABSTAIN | ABSTAIN (butterfly `c12` argued down on merits) |
| 2026-07-02 | ABSTAIN | ABSTAIN (**butterfly `c12` CHOSEN, then blind-vetoed**) |
| 2026-07-08 | ABSTAIN | ABSTAIN |
| 2026-07-13 | ABSTAIN | **c4 `bear_put_spread`** → −$52.00 |
| 2026-07-16 | c11 bull_put_spread | c10 `bull_put_spread` → +$71.00 |
| 2026-07-21 | ABSTAIN | ABSTAIN |
| 2026-07-24 | c12 bull_put_spread | ABSTAIN |

Two windows flipped, one each way. **Net change: zero.** I am reporting this as
measured; nothing was adjusted to improve it.

### Was the new structure ever traded? No — but it was chosen

**The butterfly was surfaced in every single window** (1–2 of 12 slots;
reconstructed offline from the same replay chains):

```
2026-06-29  surfaced 12 {bear_call 3, bear_put 3, bull_call 3, bull_put 2, long_iron_butterfly 1}
2026-07-02  surfaced 12 {bear_call 3, bear_put 3, bull_call 3, bull_put 2, long_iron_butterfly 1}
2026-07-13  surfaced 12 {bear_call 3, bear_put 3, bull_call 2, bull_put 2, long_iron_butterfly 2}
```

On **2026-07-02** (IV −4.74pp below realized — the archetypal regime) the trader
**selected `c12`, the long iron butterfly**. That is the first time in any
measured pass that the committee chose a non-directional long-premium structure.
It died at the blind veto, verbatim:

> "At 18.40% realized vol, the expected 1-sigma move over 29 days (~46 points or
> 6%) far exceeds the butterfly's 2.4% breakeven range; this implies <33% win
> probability and poor expected value unless implied vol is significantly
> elevated above realized vol."

That is a **structure-aware economic argument**, not a misreading — the reviewer
priced the breakeven range against the expected move and found it too narrow. It
is the veto doing its job. (Contrast the last pass, where the blind reviewer
vetoed a valid debit vertical *for being a debit*.)

And on **2026-07-13** the desk traded a **debit vertical** for the first time —
a window that abstained in the baseline. It lost $52 on a forced 3-DTE exit. A
loss, but a long-premium view the desk could not previously express at all.

### The remaining 8 abstentions, in the committee's own words

Verbatim, unedited. Note **0 of 8** cite structure availability:

> **2026-06-15** — "vol_analyst's IV<RV thesis is directionally agnostic
> (recommends both bull call and bear put structures equally), and
> bear_adversary specifically undercuts the bearish leg (c4) on skew and thin
> short-strike OI, so there is no genuine cross-model directional agreement to
> act on — per the desk's veto rule, that means no trade this cycle."

> **2026-06-18** — "vol_analyst's long-premium thesis is directly undermined by
> bear_adversary's mean-reversion and thin-OI critique of the same structural
> approach (RV>IV converging plus gap risk on short legs), so the committee
> lacks real agreement on direction rather than just strike selection, and the
> combined conviction (0.70 vs 0.43) doesn't clear a bar for taking on defined
> but real debit-spread risk this cycle."

> **2026-06-24** — "Both analysts agree cheap IV favors debit over credit
> structures, but neither commits to a directional bias (vol_analyst cites both
> bull-call and bear-put debit trades as viable, and bear_adversary is only
> critiquing a credit-spread strike rather than proposing a bearish view) —
> without cross-model agreement on direction the veto rule requires ABSTAIN
> rather than picking a strike."

> **2026-06-29** — "The committee only debates volatility level (IV cheap vs.
> RV), not direction, and the adversary raises a credible vega-decay risk that
> could offset any directional gain; with no directional edge, taking one of
> these directional debit/credit spreads is a coin flip and **c12's risk/reward
> (842 max loss vs 158 max profit) is unattractive**, so abstaining is the
> disciplined call."

> **2026-07-02** — c12 (long_iron_butterfly) vetoed — blind review: [quoted in
> full above].

> **2026-07-08** — "vol_analyst favors long-premium debit spreads on the
> IV-below-RV gap while bear_adversary argues that same gap signals imminent
> downward vol mean-reversion that would crush any long-vega position; this is a
> direct disagreement between model families on trade direction, which per the
> veto rule means no trade this cycle."

> **2026-07-21** — "bear_adversary's probability of 0.45 signals genuine
> disagreement with vol_analyst's 0.72, and the specific concern (breakeven only
> ~0.2 sigma from spot with FOMC/CPI event risk into 31 DTE) applies comparably
> to both leading credit-spread candidates (c1, c11); with no convergence
> between the two views, ABSTAIN is the correct call rather than forcing a
> tight-margin short-premium trade into event risk."

> **2026-07-24** — "vol_analyst (0.70) and bear_adversary (0.45) disagree on the
> only candidate under serious discussion, c1 — the adversary's breakeven/gap-
> risk critique is substantive, not perfunctory, so per the desk's veto rule
> requiring cross-family agreement, this cycle abstains rather than forcing a
> trade."

### What this says

The previous report's diagnosis was that the directional-agreement rule blocked
a volatility view because no non-directional long-premium structure existed. That
diagnosis is now **partially falsified by its own remedy, and that is worth more
than a lower number**:

- **2026-07-02 confirms it.** Given the structure, the committee reached it,
  chose it, and only the independent reviewer's pricing argument stopped it.
- **2026-06-29 refutes the simple version.** The butterfly was there, on the
  menu, and the trader named it and **rejected it on its economics**
  ("842 max loss vs 158 max profit is unattractive") — not on direction. The
  binding constraint that window was the structure's *price on that ladder*, not
  its existence.
- **2026-06-18, 07-08 show a third constraint** the last report did not isolate:
  the two analysts now disagree about the **volatility view itself**
  (`bear_adversary`: the IV-below-RV gap signals imminent vol mean-reversion
  that would "crush any long-vega position"). That is a genuine two-model
  disagreement on the *vol* axis, and the veto rule is behaving correctly in
  refusing it. No structure fixes that.

The menu gap is closed. What remains is a committee that disagrees — which is
what an adversarial committee is for.

---

## 6. Honest findings NOT engineered away

**(i) The replay ladder understates the butterfly badly.** `seed_replay.strike_ladder`
is a 13-strike, 5-wide grid, so the widest symmetric wing available is ±30
against a ~±46-point expected move. The surfaced replay butterflies therefore
cost **84–87% of width** ($822–$873 max loss for $127–$178 of profit), which is
exactly what the trader called "unattractive" on 06-29 and what the blind
reviewer priced out on 07-02. On the live **1-point** chain the same filters
surface butterflies at **66–77% of width** ($618/$182 and $858/$442). The
replay's verdict on this structure is **pessimistic** and should not be read as
a live forecast. I did not widen the ladder: that would have changed the very
measurement I was told not to tune. It is the single highest-value follow-up.

**(ii) The butterfly is expensive at ANY offset when IV is fair.** Even live, the
cheapest surfaced butterfly is 66% of width. The structure is only compelling
when the market is underpricing movement — which is precisely the regime it was
built for, but it means it will rarely be the *best* candidate on a normal day,
and the 2026-08-30 dry run confirms that (the committee took a credit spread and
said so explicitly).

**(iii) `--no-llm` mode remains 100% short premium.** `best_by_credit_ratio`
maximises `net_credit / max_loss`, which is negative for every debit structure
by construction, so it can never select a butterfly either. Documented as a
credit-ratio selector, out of scope here, still a real gap in the
LLM-unavailable path.

**(iv) The debit floor is a menu filter, not a guard.** A lottery-ticket debit
vertical would still pass RiskGuard on its merits (its max loss is tiny). This
change stops it *reaching* the committee; it does not make it unsendable. That
is the correct division — `risk.yaml` governs what may be sent, this governs
what is worth a slot — but it is worth stating plainly.

**(v) Butterflies now consume 172 of 1483 built candidates and 2 of 12 slots.**
That is 2 slots taken from the four vertical families. `_stratified_cap` gives
every present structure a round-robin share, so adding a sixth structure
necessarily thins the others (bull_call and bull_put dropped 3→2 live). Whether
12 slots is still the right cap with six structures is a question this change
raises and does not answer.

---

## 7. Hard rules

1. **LLMs never invent parameters** — `build_long_iron_butterfly` is pure
   deterministic code; every strike, side, count and price is computed before
   any model runs. The wing-offset sweep is enumeration, not selection.
2. **RiskGuard first** — `long_iron_butterfly` added to `risk.yaml`
   `structures.allowed`; the existing allowlist-superset test now covers it, so
   builder and guard cannot drift.
3. **Defined risk only, no naked shorts** — the short call wing sits above the
   long body call and the short put wing below the long body put, so each short
   is covered by a long of the same right. `is_defined_risk` is `True`, and both
   the structure's own test and the shared `test_no_naked_short_legs` loop
   assert it.
4. **Fail closed** — `None` on a liquidity gate failure, `ValueError` on a
   structural violation, unchanged from every other builder.
5. **ABSTAIN is first-class** — 8 of 10 windows abstained and that is reported
   as the result, not worked around.

## Files changed

```
candidate_builder.py    +103   build_long_iron_butterfly
committee/snapshot.py    +81   DEBIT_WIDTH_STRUCTURES, _drop_thin_debit, docs
committee/veto.py         +2    neutral band + blind-prompt conventions
committee/premortem.py    +6    generalised the "loses by standing still" reason
risk.yaml                 +7    long_iron_butterfly on the allowlist
scripts/run_session.py   +35    swept-offset butterfly enumeration
scripts/replay.py         +8    fixture reconstruction
tests/  (6 files)       +462    46 new tests
                        ----
                        704 insertions, 12 deletions (all 12 rewordings)
```
