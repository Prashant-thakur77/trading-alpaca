# Debit vertical spreads — closing the long-premium gap

**Date:** 2026-08-30
**Defect:** 72% abstention (31/43 seeded post-cutoff windows), 23 of them sharing
one cause: the candidate menu was structurally 100% short premium.
**Suite:** 849 → 911 passing (+62), zero existing tests weakened or deleted.

**Bottom line, up front.** The structural gap is closed and provable: the live
surfaced menu went from **0/12 to 6/12 long-premium slots**, and the committee
transcripts show the analysts now naming and arguing for those candidates by id.
But on a 10-window re-run of the June–July regime the **abstention rate did not
fall — 8/10 before, 8/10 after on the same dates** — and no debit vertical was
traded. The *cause* of the refusal changed completely (from "there is no
long-premium structure" to "the two reviewers cannot agree on a direction"),
which is a real and useful result, but it is not a lower abstention rate. Full
numbers and the mechanism in §7. Nothing was tuned to improve it.

---

## 1. The defect, re-confirmed from the shipped journal

Recomputed directly from `logs/seed_journal.jsonl` (the 43-window seeded run),
not taken on faith:

```
windows: 43   abstain: 31   72.1%
```

The abstain reasons cluster hard on one theme. Verbatim fragments:

- `trader abstained: Every available candidate is a short-premium credit ...`
- `trader abstained: All 10 candidates are credit spreads (selling premiu...`
- `trader abstained: Every candidate is a premium-selling structure, but ...`
- `trader abstained: Every available candidate is a short-volatility stru...`
- `trader abstained: IV is 2.82pp below realized vol, arguing for buying ...`
- `trader abstained: Implied vol sits 0.94pp below realized, a regime tha...`

The desk held a *buy premium* view and had no structure with which to express
it. The only long-premium structure it could build was `long_straddle`, whose
max loss at SPY's price ($2,270–$2,639) exceeds `risk.yaml`'s
`max_loss_per_position: 1000.0`, so `committee/snapshot._drop_certain_denials`
removed it from every single window before the committee ever saw it.

Contiguous ABSTAIN run in the June–July IV-below-realized stretch:
`2026-06-15` through `2026-07-13` — **19 consecutive windows**, no trade.

---

## 2. TDD evidence

Red first, in every case.

**Step 1 — builders.** Added `TestBullCallSpread` (14 tests), `TestBearPutSpread`
(12), `TestDebitVerticalPayoffIdentity` (3) to `tests/test_candidate_builder.py`,
plus the two new structures to the existing `test_no_naked_short_legs` loop.

```
ImportError: cannot import name 'build_bull_call_spread' from 'candidate_builder'
```

Implemented `_vertical_debit_spread` + `build_bull_call_spread` +
`build_bear_put_spread` → `71 passed`.

**Step 2 — wiring tests, written before each wiring change:**

| Layer | Test file | Red symptom |
|---|---|---|
| `risk.yaml` allowlist | `test_risk_guard.py` (2 tests) | `allowed_structures` did not contain the new names → guard `DENY` by name |
| `run_session` enumeration | `test_run_session.py` (5 tests) | `build_candidates` produced no `bull_call_spread`/`bear_put_spread` |
| `snapshot` credit floor + cap | `test_snapshot.py` (6 tests) | — (see §4) |
| `veto.thesis_check` | `test_veto.py` (2 tests) | fell through to `unknown structure ... — failing closed`, vetoing 100% of debit trades |
| `premortem` structure map | `test_premortem.py` (5 tests) | every `underlying_beyond` trigger discarded as "does not lose money by the underlying reaching a level" |

**Step 3 — legibility fixes, driven by the live replay (§6).** 7 more tests, red
first, in `test_snapshot.py` and `test_veto.py`.

Final: `python3 -m pytest tests/ -q` → **911 passed**.

I also hardened one existing test rather than editing it around the change:
`test_all_candidates_use_allowed_structures` used to compare against a
hardcoded set of four names; it now loads `risk.yaml` through
`risk_guard.load_risk_config()`, so builder-vs-guard drift is caught rather
than encoded twice.

---

## 3. The payoff assertions

Fixture: 5-wide, long leg mid `5.05`, short leg mid `3.05` → **debit `2.00`**.

**Bull call spread** — buy 450c, sell 455c:

| Quantity | Asserted | Test |
|---|---|---|
| `net_credit` | `-2.00` (NEGATIVE for a debit, matching `build_long_straddle`) | `test_is_a_net_debit` |
| `is_credit` | `False` | `test_is_not_a_credit_trade` |
| `max_loss` | `$200.00` = debit × 100 × contracts | `test_max_loss_is_the_debit_paid` |
| `max_profit` | `$300.00` = (width − debit) × 100 | `test_max_profit_is_width_minus_debit` |
| `breakevens` | `(452.0,)` = long strike + debit | `test_breakeven_is_long_strike_plus_debit` |
| 3 contracts | `max_loss $600`, `max_profit $900` | `test_scales_with_contracts` |
| `is_defined_risk` | `True` | `test_is_defined_risk` |

**Bear put spread** — buy 450p, sell 445p: identical, except
`breakevens == (448.0,)` = long strike − debit
(`test_breakeven_is_long_strike_minus_debit`).

**Cross-cutting identity** (`TestDebitVerticalPayoffIdentity`) — the assertion
that catches a sign error in *either* term independently:

```
max_loss + max_profit == width * 100        # 200 + 300 == 500
long_strike < bull_breakeven < short_strike # 450 < 452 < 455
short_strike < bear_breakeven < long_strike # 445 < 448 < 450
```

**Structural failures raise `ValueError`** (wrong rights, inverted strikes, equal
strikes, mismatched expiries); **liquidity failure returns `None`** — matching the
credit builders' conventions exactly. Both covered per structure.

---

## 4. `_drop_thin_credit` — the exact bug class, tested explicitly

`_drop_thin_credit` keys off `CREDIT_STRUCTURES` (a **structure-name set**), not
`intent.is_credit`. A debit vertical is therefore already excluded from the
credit-to-risk floor and **no code change was needed** — but this is precisely
the class of rule that produced the original defect (a filter written for one
structure family silently erasing another), so it is now pinned by three tests
rather than left to inspection:

- `test_debit_verticals_are_not_dropped_by_the_credit_to_risk_floor` — a
  `bull_call_spread` and a `bear_put_spread`, both with `net_credit < 0` and
  `is_credit is False`, survive `render_snapshot`.
- `test_debit_verticals_survive_alongside_a_thin_credit_spread` — the floor
  fires on a $0.02-credit `bear_call_spread` and leaves the debit vertical
  untouched **in the same call**.
- `test_debit_verticals_clear_the_max_loss_cap_a_straddle_cannot` — the
  measured defect in miniature: with `max_loss_cap=200`, the straddle is
  dropped and the debit vertical is kept.

Had the filter been written as `if intent.is_credit`, it would have rejected
100% of the long-premium menu unconditionally. That reasoning is now recorded
in the function's docstring.

**Stratified cap** (`_stratified_cap`) verified with three more tests:
`test_credit_structures_do_not_crowd_out_the_long_premium_structures` (20+20
credit vs 3+3 debit, cap 12 → both debit structures still surface),
`test_mixed_menu_of_all_five_structures_fits_the_cap` (all five present, cap 10,
exactly 10 returned), and
`test_selection_stays_deterministic_and_order_independent_with_debit_verticals`.

**Determinism** is preserved and re-pinned: `render_snapshot` output is
byte-identical for identical input, and shuffling the candidate list
(`random.Random(7)`, `random.Random(11)`) changes neither `.text` nor
`.candidates`. Existing determinism tests untouched and still green.

---

## 5. Live dry-run structure mix — before and after

One live SPY chain fetch (2026-08-30, spot **$769.35**, realized vol 9.52%,
ATM IV 11.98%, **IV − RV = +2.46pp**). "Before" = the same chain with the two
new structures removed, i.e. exactly what the shipped code produced.

### BEFORE

```
built     632  {bear_call_spread: 188, bull_put_spread: 440, long_straddle: 4}
surfaced   12  {bear_call_spread: 6,   bull_put_spread: 6}
long-premium slots surfaced: 0/12
```

All 4 straddles dropped by `_drop_certain_denials`. **The menu was 100% short
premium — there was no long-premium candidate to choose, at any price.**

### AFTER

```
built    1311  {bear_call_spread: 188, bear_put_spread: 467,
                bull_call_spread: 212, bull_put_spread: 440, long_straddle: 4}
surfaced   12  {bear_call_spread: 3, bear_put_spread: 3,
                bull_call_spread: 3, bull_put_spread: 3}
long-premium slots surfaced: 6/12
```

The surfaced long-premium candidates and their economics:

```
c4 bear_put_spread   credit=$  -0.61  max_loss=$  61.00  max_profit=$439.00
c5 bear_put_spread   credit=$  -0.07  max_loss=$   7.00  max_profit=$493.00
c6 bear_put_spread   credit=$  -0.22  max_loss=$  22.50  max_profit=$477.50
c7 bull_call_spread  credit=$  -1.36  max_loss=$ 136.00  max_profit=$364.00
c8 bull_call_spread  credit=$  -0.39  max_loss=$  39.00  max_profit=$461.00
c9 bull_call_spread  credit=$  -0.04  max_loss=$   4.00  max_profit=$496.00
```

Every one clears the $1,000 cap with room to spare — $4 to $136 against the
straddle's $2,270+. **The structural gap is closed: a long-premium option is now
visible.**

`python3 scripts/run_session.py --dry-run` ran clean end to end (exit 0) against
that chain: 1311 candidates, committee ran, thesis veto PASS, blind veto PASS,
guard `ALLOW`, pre-mortem plan built, payload printed, no order sent. Today's
regime is IV **above** realized (+2.46pp), so the committee correctly chose a
`bear_call_spread` (short premium) — which is the right answer for today and not
evidence against the fix; the fix is about the regime where IV sits *below*
realized.

---

## 6. Two further defects the change surfaced — found by measurement, not guessed

Re-running the June–July windows immediately after wiring in the builders
exposed two more instances of the *same* bug class one layer up: machinery
written for credit structures misreading a correctly formed debit trade.

**(a) The blind reviewer vetoed a valid debit vertical for being a debit.**
Window `2026-06-15`: the committee chose `c4`, a well-formed `bear_put_spread`.
The blind reviewer vetoed it, verbatim:

> "This bear put spread is structured as a debit trade (paying 0.62 to enter),
> not a credit spread."

The prompt showed it `NET_CREDIT: -0.62` and never stated what the sign meant.
`committee/premortem.py` already labels the identical field
`"(per share, negative = debit paid)"`; `committee/veto.py` did not. Fixed with a
`CONVENTIONS.` paragraph in `_BLIND_REVIEW_PROMPT`, plus a `MAX_PROFIT` field —
for a credit trade `NET_CREDIT` *is* the reward, but for a debit trade it is the
cost, so the reviewer was previously shown a price and a loss with no upside at
all to weigh them against.

**(b) The vol analyst could not tell a debit spread from a credit spread.**
Window `2026-06-18`, IV **2.42pp below** realized — the exact regime this whole
change targets. `bull_call_spread` and `bear_put_spread` candidates *were*
surfaced, and the analyst abstained anyway, verbatim:

> "Market favours long-vol (IV −2.42pp vs realized), but all candidates are
> credit/mixed spreads with net short or zero premium bias. No straddles
> available."

It was looking straight at long-premium candidates and could not tell. The only
thing distinguishing them in the rendered snapshot was a minus sign, with no
statement of what that sign means. This is the *identical* failure
`_iv_minus_realized` already documents at length for the IV spread — readers
supply a missing convention themselves, and supply it backwards about half the
time.

Fixed by adding a constant `NET_CREDIT_CONVENTION` line to the snapshot header.
Like the IV line, it states **only the definition** — never which side to take;
`test_the_convention_line_never_prescribes_which_to_pick` asserts the line
contains none of `should / prefer / favours / favors / recommend / abstain`,
because prescribing the answer would make the committee's agreement meaningless.

Present-but-unreadable is not visible. Without these two fixes the builders
alone would have moved the abstention rate very little, and the reason would
have been invisible in the aggregate number.

---

## 7. Abstention measurement

### Setup

10 windows, `scripts/seed_calibration.py --symbol SPY --start 2026-06-15
--end 2026-07-24 --max-windows 10 --spacing 3`, written to a scratch journal so
the judged `logs/seed_journal.jsonl` is untouched. Every prompt was a cache MISS
(the snapshot text changed), so these are 10 fresh committee runs, not replays of
recorded answers. The dates land inside the June–July IV-below-realized stretch
that produced the original defect. Nothing was tuned between the before and after
runs; the only difference is this change.

### Result: the abstention rate did NOT fall

| | Abstained | Rate |
|---|---|---|
| **BEFORE** (baseline journal, same 10 dates) | 8/10 | **80%** |
| **AFTER** (this change) | 8/10 | **80%** |

Matched date by date:

| date | BEFORE | AFTER |
|---|---|---|
| 2026-06-15 | ABSTAIN | ABSTAIN |
| 2026-06-18 | ABSTAIN | ABSTAIN |
| 2026-06-24 | ABSTAIN | ABSTAIN |
| 2026-06-29 | ABSTAIN | ABSTAIN |
| 2026-07-02 | ABSTAIN | ABSTAIN |
| 2026-07-08 | ABSTAIN | ABSTAIN |
| 2026-07-13 | ABSTAIN | ABSTAIN |
| 2026-07-16 | ABSTAIN | **c11 bull_put_spread** |
| 2026-07-21 | c8 bull_put_spread | **ABSTAIN** |
| 2026-07-24 | c9 bull_put_spread | c12 bull_put_spread |

Two windows flipped, one in each direction. Net change: **zero**.

**And not one of the 10 windows chose a debit vertical.** Both trades taken were
`bull_put_spread` — short premium — on the two dates where IV sat *above*
realized (07-16: rv 11.7% / iv 13.8%; 07-24: rv 11.6% / iv 15.2%). That is
correct behaviour for those two dates, but it means the long-premium structures
were surfaced, advocated, and still never traded.

I am reporting this as measured. Nothing was adjusted to improve it.

### What DID change: the cause of the abstention

The rate is flat; the mechanism is not. This is the substantive result.

**Before** — 23 of 31 abstentions cited the *absence of a structure*:

> "Every available candidate is a short-premium credit structure…"
> "All 10 candidates are credit spreads (selling premium)…"
> "Every candidate is a premium-selling structure, but…"

**After** — **0 of 8** abstentions cite structure availability. Seven of eight
cite failure of the two-model directional agreement rule; one is an LLM timeout.
The analysts now see, name and argue for the long-premium candidates by id:

> **2026-06-15** — "vol_analyst favors a **bullish long-premium structure
> (cheap calls, c7-c9)** while bear_adversary flags material downside/gap risk
> … the two views don't converge on a single direction, so per veto policy that
> disagreement forces ABSTAIN."

> **2026-06-29** — "vol_analyst leans bullish (p=0.65) while bear_adversary
> leans bearish/neutral (p=0.42) and specifically undercuts the breakeven odds
> on **the bull call spreads it favors**."

> **2026-06-24** — "vol_analyst favors **long-premium exposure** on the cheap-IV
> signal while bear_adversary directly rebuts the long-call thesis…"

The desk is no longer refusing because it has nothing to offer. It is refusing
because its two reviewers cannot agree on a direction.

### The new binding constraint, and why it is structural too

The clearest statement of it is the committee's own, on **2026-06-18** (IV 2.42pp
below realized — the archetypal regime for this change):

> "Both analysts agree IV is underpriced relative to realized vol and
> short-premium structures (c1-c3, c10-c12) are the wrong side, but neither
> commits to a directional bet — **vol_analyst cites both bullish (c7-c9) and
> bearish (c4-c6) debit spreads as equally favored by the vol regime**, and
> bear_adversary only critiques a specific short-call trade without endorsing a
> direction. With no two-model agreement on direction, ABSTAIN is the correct
> call."

"IV is below realized, so buy premium" is a view about **volatility**, not about
**direction**. Both debit verticals are directional structures. So the committee
now reaches unanimous agreement on the vol regime and is then blocked by a veto
rule that demands directional agreement it was never going to reach. The only
non-directional long-premium structure the desk can build is `long_straddle` —
still priced out by `max_loss_per_position: 1000.0`, exactly as before.

The gap this change closed was real and is closed: the menu is no longer 100%
short premium, and the reasoning transcripts prove the analysts can now act on
it. But it was **not the only thing standing between this regime and a trade**,
and the honest 10-window number says so.

Recommended follow-up, stated rather than done (it is a new structure, not a
wiring fix, and belongs in its own measured change): a **defined-risk,
non-directional long-premium structure that fits under the $1,000 cap** — a long
call vertical plus a long put vertical at the same expiry costs the sum of two
debits (~$200-$400 on this chain), is long vol, is direction-neutral, and would
let the committee express the exact view it keeps reaching and then discarding.


---

## 8. Honest findings NOT engineered away

**(i) Near-zero-debit lottery tickets are now surfaceable.** On the live chain,
`c9` is a `bull_call_spread` with a **$0.04 debit** (max loss $4, max profit
$496) and `c5` a `bear_put_spread` at **$0.07**. These are the debit-side mirror
of the $0.02-credit spreads that `MIN_CREDIT_TO_RISK` was introduced to remove:
the market is pricing them at essentially zero probability of paying off.
`_structure_fill_order` ranks on `_reward_to_risk = max_profit / max_loss`, which
for a $4 debit is 124:1 — so these rank *top* of their band and are actively
selected.

I did **not** add a debit-side quality floor. It is outside what was asked, and
adding an untasked filter would have altered the very abstention measurement I
was told not to tune. Recommended follow-up: a symmetric floor — e.g. require
`debit >= k * width` (a conventional desk floor is ~10–20% of width, which would
have removed `c5`, `c8` and `c9` and kept `c4`, `c6`, `c7`). It should be
introduced and measured on its own, not folded into this change.

**(ii) `--no-llm` mode remains 100% short premium.** The deterministic fallback
`best_by_credit_ratio` maximises `net_credit / max_loss`, which is negative for
every debit structure by construction, so it can never select one (verified:
given a `bull_call_spread` at −0.01 and a `bull_put_spread` at +0.0025, it picks
the bull put). The fallback is documented as a credit-ratio selector and is out
of scope here, but the desk's LLM-unavailable path still cannot express a
long-premium view.

**(iii) The seeded replay is a coarser chain than live.** `seed_replay.strike_ladder`
builds a 13-strike, 5-wide grid around spot (~25 legs, ~25 candidates) versus
1311 on a live 1-point chain. The debit verticals do assemble on it, but the
replay's structure mix is not the live mix, and its abstention rate should be
read as directional, not as a live forecast.

**(iv) One replay window lost an analyst to infrastructure**, not to the change:
`bear_adversary` on 2026-06-18 returned `LLM failure: claude CLI timeout after
120s`. Unrelated to this work; noted so it is not misread as a veto.

---

## 9. Regression surface checked

- `python3 -m pytest tests/ -q` → **911 passed** (from 849).
- `python3 scripts/replay.py --all --verify` → **exit 0** (`allow`, `deny`,
  `downsize`, `fail_closed` all OK).
- Same, with `date.today()` frozen at **+3d (2026-09-02)** and **+30d
  (2026-09-29)** → **exit 0** both. Judge fixtures record no `snapshot_hash`, so
  the new header line does not disturb them; verified empirically, not assumed.
- `make validate` → exit 0. `make verify` → exit 0.
- `scripts/replay.py::rebuild_intent` extended to reconstruct the two new
  structures, so a future fixture carrying one does not raise `ScenarioError`.
- **Not touched:** `executor_options.py`, `exit_monitor.py`, `options_orders.py`.
  All three are sign-generic already — `build_mleg_payload` computes
  `price = -intent.net_credit`, which yields a positive (debit) limit price for
  the new structures exactly as it already did for `long_straddle`.

## 10. Hard rules

1. **LLMs never invent parameters** — both builders are pure deterministic code;
   every strike, side, count and price is computed before any model runs.
2. **RiskGuard first** — `bull_call_spread` / `bear_put_spread` added to
   `risk.yaml` `structures.allowed`, and a new test asserts the allowlist is a
   superset of everything the builder can emit, so builder and guard cannot
   drift.
3. **Defined risk only** — both are long-a-leg/short-a-leg of the same right;
   `is_defined_risk` is `True` and `test_no_naked_short_legs` now covers them.
4. **Fail closed** — `None` on a liquidity gate failure, `ValueError` on a
   structural violation, unchanged from the credit builders.

## Files changed

```
candidate_builder.py    +99   _vertical_debit_spread, two builders
committee/premortem.py   +9   debit verticals added to the downside/upside maps
committee/snapshot.py   +45   NET_CREDIT_CONVENTION line; docstrings
committee/veto.py       +28   thesis_check direction sets; prompt conventions + MAX_PROFIT
risk.yaml               +11   two structures on the allowlist
scripts/replay.py        +8   fixture reconstruction for the new structures
scripts/run_session.py  +31   two enumeration loops
tests/  (5 files)      +527   62 new tests
```
