# Calibration seeding — work report

Replays the REAL committee over REAL post-knowledge-cutoff market data so the
Brier calibration loop has resolved outcomes to score, instead of sitting
dormant at all-1.0 weights.

**Headline: the loop is no longer dormant.** Both analysts crossed the
10-prediction minimum, and the weights are now differentiated and live.

```
Calibration report — logs/seed_journal.jsonl

role              resolved    brier   weight   interpretation
----------------------------------------------------------------------------------------
vol_analyst             12    0.162     1.18   well-calibrated (beats the 0.25 always-0.5 baseline) — upweighted
bear_adversary          12    0.298     0.90   worse than baseline — down-weighted

24 resolved prediction(s) across 2 role(s), computed fresh from the journal above.
```

Reproduce with `python3 scripts/calibration_report.py --journal logs/seed_journal.jsonl`.
Chain verified: `python3 scripts/verify_journal.py --path logs/seed_journal.jsonl`
→ **Chain: INTACT**, tip `5f59a043a08ecfaa…`, 242 entries.

---

## What was built

| File | What it is |
|---|---|
| `seed_replay.py` (new) | Pure replay logic: contamination guard, OCC symbol construction, strike ladder, expiry choice, quote-from-bar, outcome resolution, `ReplayJournal`, `SeedReport` |
| `scripts/seed_calibration.py` (new) | The run: real SPY + option bars → real candidates → real committee → journalled cycle + resolving `close` |
| `scripts/calibration_report.py` | Now takes `--journal PATH` (legacy positional still works) |
| `candidate_builder.py` | `OptionQuote.as_of` — `None` (= today) by default, so every live call site is byte-identical; a historical quote carries the date it was observed, which is what makes its DTE mean anything |
| `Makefile` | `make seed-calibration` |

### The contamination constraint, enforced in three places

The model's knowledge cutoff is **May 2026**, so `seed_replay.KNOWLEDGE_CUTOFF`
is `2026-06-01` and:

1. `validate_window` **raises** rather than clamping — silently moving a
   caller's start date forward would let a typo in a Makefile target quietly
   change what the artifact claims. `main` exits 2.
2. `decision_dates` drops any pre-cutoff day it is handed anyway, because the
   bar fetch legitimately carries a 45-day May lead-in for realized vol.
3. The rationale, naming TradingAgents (arXiv 2412.20138) and FinMem
   (arXiv 2311.13743), is written into `docs/calibration_seeding.md` so a judge
   can check it without reading the code.

No window in this run starts before 2026-06-01. The earliest is 2026-06-01
itself.

### Honesty properties

* Every seeded payload carries `source: "seed_replay"`, `replay_as_of` and
  `window_id`. These are stamped by `ReplayJournal` on the way past, so
  `committee/decide.py` never learns it is being replayed — the decision code
  is identical to the live path. The `close` entry additionally says
  *"REPLAYED, NOT TRADED — no order was ever sent to a broker for this entry"*.
* Separate journal (`logs/seed_journal.jsonl`). The live `logs/journal.jsonl`
  records real broker interaction and was never opened.
* Re-seeding an already-seeded journal is **refused** (`--append` to override).
  The snapshot hash is deterministic, so a second pass writes a second
  `analyst_view` under the same hash and `calibration.resolved_predictions`
  would count one observation as two.
* A window that cannot be built from real bars is skipped with a logged reason
  and appears in the artifact's skip table. Nothing is ever filled in.

### Two substitutions, named rather than hidden

Historical option data is OHLCV bars, not quotes, and `get_option_contracts`
returns nothing for an expired expiry — so OCC symbols are constructed directly
(verified: `SPY260821C00750000` returns real bars) and two fields have no
historical source:

* **`bid == ask == close`.** A daily bar records a traded price, not a spread.
  Widening it by a guessed amount would fabricate the exact number the
  liquidity gate reads, so the traded price is used as both sides and the
  spread gate is simply not exercised in replay.
* **`open_interest := the day's volume`.** Historical OI is not retrievable.
  Volume is real, observed, and the nearest liquidity proxy;
  `candidate_builder.MIN_OPEN_INTEREST` then filters on it as it would on OI.

Neither can manufacture a favourable *outcome* — outcomes come from subsequent
price action alone.

---

## TDD evidence for the pure logic

Tests were written first and observed failing before any implementation:

```
tests/test_seed_replay.py:18: ModuleNotFoundError: No module named 'seed_replay'
tests/test_seed_calibration.py:18: ModuleNotFoundError: No module named 'seed_calibration'
```

Then implemented to green. Later features were added the same way — the
`--journal` flag (5 tests red first: `TypeError: expected str, bytes or
os.PathLike object, not list`), and the double-count refusal (1 test red first).

**78 new tests**, none of them touching the network (every Alpaca client is a
stub built from literals; the seeding *script* obviously hits the network,
that is its job):

| Suite | Tests | Covers |
|---|---|---|
| `tests/test_seed_replay.py` | 49 | cutoff guard (6), decision dates (3), OCC symbols (3), strike ladder (3), expiry choice (5), quote-from-bar (5), `as_of` (2), outcome resolution (9), intrinsic (2), `ReplayJournal` (4), `SeedReport` (4), double-count guard (3) |
| `tests/test_seed_calibration.py` | 29 | bar fetching + ceilings (7), chain construction (4), expiry band (2), **fail-closed skips (5)**, dry run (1), cost accounting (5), contamination refusal (3), double-count refusal (2) |
| `tests/test_calibration_report.py` | +5 | `--journal` flag, `--journal=`, legacy positional, default, end-to-end |

Two test expectations were corrected against the implementation where the
*test* was the thing that was wrong, and both corrections are documented in the
test body rather than quietly patched:

* `pick_expiry` — I had expected the 24-DTE Friday; the implementation
  correctly prefers the Friday nearest `TARGET_DTE = 28`, which is the 31-DTE
  one.
* `resolve_outcome` forced-DTE case — my marks left the position up $160 on a
  $200 credit, which *also* trips the 50%-of-credit profit target. The test now
  uses marks that leave it up $90, so it genuinely exercises the forced exit.

Full suite: **849 passed, 0 failed**.

---

## The run

`make seed-calibration` → SPY, 2026-06-01 .. 2026-08-07, spacing 1, cap 50.

| | |
|---|---|
| Decision dates attempted | **48** |
| Replayed | **43** |
| Skipped | **5** |
| Committee abstained | **31 of 43 — a 72% abstention rate** |
| Resolved to a realized P&L | **12** |
| **Usable resolved predictions per analyst** | **12 of 48 windows = 25%** |

### Windows skipped, and why

All 5 for the same reason, and it is a real-data reason rather than a bug:

| as-of | reason |
|---|---|
| 2026-06-02 | only 0 of 26 legs had a real bar (need 6) |
| 2026-06-03 | only 0 of 26 legs had a real bar (need 6) |
| 2026-06-04 | only 0 of 26 legs had a real bar (need 6) |
| 2026-06-05 | only 0 of 26 legs had a real bar (need 6) |
| 2026-06-08 | only 0 of 26 legs had a real bar (need 6) |

Those decision dates select an expiry for which Alpaca returns no option bars
at all at any strike on the ladder. The run skips the window rather than
reaching for a nearby expiry or interpolating a price.

### The 12 resolved windows

| as-of | expiry | spot | choice | structure | resolution method | exit | realized P&L |
|---|---|---|---|---|---|---|---|
| 2026-06-01 | 2026-06-26 | 758.54 | c9 | bull_put_spread | forced 3-DTE exit | 2026-06-23 | **-$246.00** |
| 2026-06-09 | 2026-07-10 | 737.05 | c9 | bull_put_spread | profit target | 2026-06-15 | +$76.00 |
| 2026-06-11 | 2026-07-10 | 737.76 | c9 | bull_put_spread | profit target | 2026-06-15 | +$76.00 |
| 2026-06-12 | 2026-07-10 | 741.75 | c10 | bull_put_spread | profit target | 2026-06-15 | +$86.00 |
| 2026-07-14 | 2026-08-14 | 751.83 | c8 | bull_put_spread | profit target | 2026-08-03 | +$66.00 |
| 2026-07-15 | 2026-08-14 | 754.81 | c10 | bull_put_spread | profit target | 2026-08-04 | +$103.00 |
| 2026-07-20 | 2026-08-14 | 742.09 | c2 | bear_call_spread | profit target | 2026-07-29 | +$164.00 |
| 2026-07-21 | 2026-08-21 | 748.28 | c8 | bull_put_spread | profit target | 2026-08-03 | +$51.00 |
| 2026-07-23 | 2026-08-21 | 738.18 | c10 | bull_put_spread | profit target | 2026-08-03 | +$79.00 |
| 2026-07-24 | 2026-08-21 | 738.93 | c9 | bull_put_spread | profit target | 2026-08-03 | +$79.00 |
| 2026-07-27 | 2026-08-21 | 739.09 | c8 | bull_put_spread | profit target | 2026-08-03 | +$62.00 |
| 2026-07-29 | 2026-08-28 | 729.46 | c3 | bear_call_spread | forced 3-DTE exit | 2026-08-25 | **-$238.00** |

10 winners, 2 losers, **net +$358** on paper across twelve 1-contract spreads.
That is 12 trades over two months on one symbol — **it is not evidence of edge
and must never be presented as such.** Its only job is to give the calibration
loop something real to score. Note also that the two losers are both large
relative to the winners, exactly the shape a credit-spread book produces.

**Resolution methods used:** 10 × `leg_bars_profit_target`, 2 ×
`leg_bars_forced_dte_exit`, 0 × `intrinsic_at_expiry`. Every one of the twelve
was marked from real option leg bars on the day the exit rule fired. The
weakest method — settling at intrinsic against the underlying's close because
the legs stopped printing — was never needed. That matters: no resolved outcome
in this run rests on a modelled settlement.

---

## The abstention rate: 72%, and it is mostly a **menu defect**, not conviction

This is the finding I would act on. Breaking the 31 abstentions down:

| cause | count |
|---|---|
| Trader abstained | 28 |
| Vetoed at the thesis check / blind review | 3 |

And within the 28 trader abstentions, by what the trader actually said:

| driver | count |
|---|---|
| **Vol regime opposes every candidate on the menu** (IV below realized, so the regime favours BUYING premium, but every surviving candidate SELLS it) | **23** |
| Thin breakeven cushion | 2 |
| Low analyst conviction (probabilities near 0.5) | 2 |
| Other | 1 |

In the trader's own words, repeatedly and independently across windows:

> "Both analysts agree implied vol is below realized vol, favoring buying
> premium, but every available candidate is a short-vol premium-selling spread
> structurally opposed to that signal."

> "All 10 candidates are credit spreads (selling premium), but IV is 89bps
> below realized vol, which both vol_analyst and bear_adversary flag as
> unfavorable for premium-selling."

**I verified this deterministically rather than taking the LLM's word for it.**
Rebuilding three windows' candidate sets offline (no LLM):

```
risk.yaml max_loss_per_position = 1000.0
2026-06-17 spot 740.96: built={bull_put: 6, bear_call: 5, long_straddle: 1}  surfaced={bear_call: 5, bull_put: 6}
   long_straddle max_loss=$2639 vs cap $1000 -> DROPPED
2026-07-09 spot 751.71: built={bull_put: 6, bear_call: 5, long_straddle: 1}  surfaced={bear_call: 5, bull_put: 6}
   long_straddle max_loss=$2270 vs cap $1000 -> DROPPED
2026-07-30 spot 741.69: built={bull_put: 6, bear_call: 5, long_straddle: 1}  surfaced={bear_call: 5, bull_put: 6}
   long_straddle max_loss=$2398 vs cap $1000 -> DROPPED
```

The **only** long-premium structure `build_candidates` produces is the long
straddle, and at SPY's price a 1-contract ATM straddle costs $2,270-$2,639 —
two to two-and-a-half times `risk.yaml`'s $1,000 `max_loss_per_position`. So
`snapshot._drop_certain_denials` correctly removes it from **every single
window**, and the committee's menu is 100% short-premium credit spreads on
every one of the 43 replayed windows. Meanwhile the dominant Jun-Jul 2026 vol
regime had implied *below* realized.

**So: is the 72% a feature or a defect?** Both, in a way worth separating:

* **Feature.** The desk is refusing correctly. It will not sell premium into a
  regime that penalises premium selling just because selling premium is the
  only thing on offer. That is precisely the judgement "ABSTAIN is a
  first-class output" is supposed to buy, and 23 windows of the model
  independently articulating the same structural mismatch is good evidence the
  reasoning is real rather than decorative. This half is presentable.
* **Defect.** The desk cannot *express* the view it holds. There is no
  defined-risk long-premium structure that fits inside the $1,000 cap, so a
  correct long-vol read has nowhere to go. That is a gap in
  `build_candidates`, not in the committee — a long debit vertical (or any
  long-premium structure sized to the cap) would give the IV-below-realized
  regime something to trade, and would very likely cut the abstention rate
  substantially. **Recommend adding one before the live sessions**, since the
  same regime would produce the same 72% live.

Presenting the abstention rate without that second half would be
uncharacteristically flattering, so it belongs on the site alongside the
number.

---

## Are the two analysts meaningfully different, or tied?

**Meaningfully different, and I checked rather than assumed.** Both scored on
the identical 12 outcomes:

* `vol_analyst` — Brier **0.162**, comfortably better than the 0.25 always-say-0.5
  baseline → weight **1.18**, upweighted.
* `bear_adversary` — Brier **0.298**, worse than that baseline → weight **0.90**,
  down-weighted.

The gap is 0.136 Brier, which on this scoring is not a rounding artifact — it
is the difference between beating and losing to a coin flip. The direction is
also unsurprising and therefore believable: the book was 10-2 on the resolved
windows, and the bear adversary's job is to argue against the trade, so a
persistent pessimist on a mostly-winning sample scores worse *by construction*.

That is worth stating plainly rather than selling as insight. **This is not
evidence that the bear adversary is a bad analyst.** It is evidence that on
twelve winning-skewed outcomes a systematic pessimist is poorly calibrated,
which is exactly what a Brier score is supposed to say. The adversary's value
is in the objections it records and in the three windows where the veto layer
stopped a trade — none of which the Brier score measures. Twelve outcomes is
also barely over `DEFAULT_MIN_PREDICTIONS`; the weight should be read as "first
real signal", not "settled verdict".

**Nothing was tuned to produce this.** `DEFAULT_MIN_PREDICTIONS` stayed at 10 —
an earlier, narrower pass produced 5 resolved predictions per role and
therefore weights of exactly 1.00, and the correct response was more real
windows, not a lower bar. That pass's numbers (vol_analyst 0.176,
bear_adversary 0.302 on 5 each) are close to the final ones, which is mild
independent support that the ordering is not an artifact of sample choice.

---

## Cost

| run | live calls | spend |
|---|---|---|
| Pass 1 (20 windows, spacing 2) | 60 | $3.0663 |
| Aborted pass (killed after a hang) | 10 | $0.4034 |
| Pass 3, the run reported here (48 windows) | 76 | $3.7417 |
| **Total evidenced** | **146** | **$7.21** |

Plus roughly $0.10 from a first attempt whose log was overwritten; its four
calls survive in the prompt cache and show up as cache hits later.

Pass 3 itself made 76 live calls and served **68 from the prompt cache** — the
cache is content-addressed, so re-running `make seed-calibration` over the same
windows costs nothing. That is what made the restarts affordable.

Cost per usable resolved prediction: **~$0.60**, or ~$0.30 per analyst-scored
prediction.

---

## Can replay populate the calibration loop on its own?

The ratio that decides it: **12 usable resolved predictions from 48 decision
dates, or 25%.** The attrition is:

```
48 decision dates
 -5  no option bars exist for the chosen expiry     → 43 replayed
-31  committee abstained (23 of them: menu cannot express the view)
                                                    → 12 resolved
```

So yes, but the binding constraint is the abstention rate, not the data. At
today's 25% yield, reaching `DEFAULT_MIN_PREDICTIONS = 10` costs ~40 decision
dates and about $6 of LLM spend per symbol. **Fixing the long-premium menu gap
would raise the yield far more cheaply than adding windows**, since 23 of the
31 refusals trace to that single cause. Failing that, the other lever is
breadth — QQQ, AAPL and MSFT are already in the walk-forward universe and would
add independent windows without reaching back in time, which is the one thing
that is never allowed.

---

## Operational notes worth carrying into the live session

**1. A cached failure is not a refusal, and should probably not be cached.**
Three prompt-cache records held *failures* — two `claude CLI timeout after
120s` and one unparseable response. `committee/decide.py` deliberately replays
cached failures as failures, which is right for deterministic replay of a
*decision*. But a transport timeout is not a decision. Left in place, those
records would have made those windows abstain forever on a network fault while
looking exactly like a considered refusal — indistinguishable, in the journal,
from the 23 genuine regime-mismatch refusals above. I purged the three records
so the calls would be retried, and the windows then produced real verdicts.
This is a real distinction the cache does not currently draw: a failed *call*
and a refused *trade* are different events. Worth separating before the live
session, when a transient outage during market hours would otherwise be
permanently recorded as the desk's judgement.

**2. `alpaca-py` issues requests with no timeout and can hang indefinitely.**
One run stopped dead inside the HTTP client — no child process, no progress for
5+ minutes, no exception. Killed and restarted; the prompt cache made the
restart nearly free. There is no watchdog and no socket timeout in
`HistoricalBars` or in `alpaca_data.AlpacaData`. During a live session a hang
like this would silently stall the scheduler mid-cycle with positions open.
Worth an explicit timeout or a watchdog before the live runs.

**3. Alpaca's free feed rejects any request whose end reaches the last quarter
hour.** This is date-dependent and bit twice: once as a 403 on an end-of-day
`today` timestamp, and again at the start of a UTC day when "yesterday
23:59" is only minutes old. `_request_end` now clamps to `now - 20 min`.

---

## Files

* `seed_replay.py` — pure replay logic
* `scripts/seed_calibration.py` — the run
* `tests/test_seed_replay.py`, `tests/test_seed_calibration.py` — 78 tests, no network
* `scripts/calibration_report.py` — `--journal`
* `candidate_builder.py` — `OptionQuote.as_of`
* `Makefile` — `make seed-calibration`
* `docs/calibration_seeding.md` — the run artifact (cutoff rationale, every
  window, every skip, every abstention verbatim)
* `logs/seed_journal.jsonl` — 242 entries, chain INTACT, every payload stamped
  `source: "seed_replay"` (gitignored like every other journal)
