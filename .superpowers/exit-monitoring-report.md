# Exit monitoring + the pre-mortem — work package report

Date: 2026-08-29
Base: `24d6d27` (556 tests) → head `3a66b26`
Suite: **556 → 665 of my own tests; 692 total green** (the judge-replay
workstream landed 27 more in parallel, in `94b4fe3`).

> **Read the incident note at the bottom first.** An accidental shell
> expansion in a commit message ran `make session` and put a real (paper)
> order at the broker. It never filled, it was cancelled, and the account is
> back to $100,000 / 0 positions / 0 working orders — but it happened and it
> is journalled.

---

## 1. What was built

### `committee/premortem.py` (new, 33 tests)

```python
premortem(intent, spot, realized_vol, client=...) -> tuple[ExitTrigger, ...]
```

Asks the model *what would have to be true for this trade to lose money*, then
compiles the answer into machine-checkable triggers. The compilation is the
feature: a paragraph nobody reads again is worth nothing, a `dte_below(3)` the
monitor evaluates every cycle is worth a lot.

The model fills in values for a **fixed set of four kinds** and can no more
invent a kind than the trader can invent a strike (hard rule 1). Every value
comes back through `_validate` and is **discarded with a logged reason** —
never repaired, clamped, or coerced — when it is:

| Rejected | Why |
|---|---|
| an unrecognised `kind` | not in `TRIGGER_KINDS` |
| a non-numeric / non-finite `threshold` | `_finite()` returns `None`; bools excluded |
| `underlying_beyond` on the winning side of spot | a bull put spread does not lose to a rally |
| `underlying_beyond` on a long straddle | its failure mode is stillness, not a level |
| `underlying_beyond` inside an iron condor's short strikes | that is the profit zone |
| `underlying_beyond` outside ±50% of spot | "SPY goes to a penny" is not an exit rule |
| `iv_spike` ≤ current realized vol | it would fire on entry |
| `dte_below` > the trade's own DTE | it would fire before the trade was open |
| `credit_decay` outside (0,1), or on a debit structure | no credit was received |

Two things are **not the model's to decide**:

1. `dte_below(3)` is appended *after* validation, so no model output can
   suppress it (assignment avoidance, design spec A.5). A model trigger with
   the same `(kind, threshold)` collapses onto the hard-rule rationale.
2. An LLM failure — timeout, unparseable JSON, non-dict `parsed`,
   `failure_modes` that is not a list, or a client that raises — returns
   `deterministic_triggers(intent, reason=PREMORTEM_UNAVAILABLE)`: the
   50%-of-credit profit target plus the 3-DTE exit, with
   `"pre-mortem unavailable"` in **every** rationale so a judge can tell a
   fallback from a real pre-mortem at a glance. `premortem()` never raises.

A model can therefore only make an exit **earlier**, never later or absent.

### `exit_monitor.py` (new, 39 tests)

```python
monitor_positions(cli, data, guard, journal, triggers_by_order) -> list[ExitEvent]
```

`triggers_by_order` maps a broker order id → `OpenTrade`, which carries the
`TradeIntent` (there is no way to close a multi-leg position without knowing
its legs), the pre-mortem triggers, the guard-**approved** contract count (a
downsized position must close at the size opened, not proposed), the entry
spot and the `snapshot_hash`.

Per position: read the legs' own broker rows, derive the per-share
`close_net_credit` (a long leg is sold, a short leg bought back), and

```
realized_pnl = (intent.net_credit + close_net_credit) * 100 * contracts
```

one formula for both directions — a 1.00 credit bought back at 0.40 is +$60; a
6.00 debit sold at 8.00 is +$200.

Exits, in firing priority:

1. **max loss reached** (not expressible as one of the four kinds, so enforced
   unconditionally rather than as a trigger that could be dropped);
2. `deterministic_triggers(intent)` — **re-derived every pass**, not trusted
   from the stored record, so a trade whose triggers were lost or written by
   older code is still protected;
3. the pre-mortem's own triggers.

`underlying_beyond` takes its direction from **which side of the entry spot
the threshold sits on**, so one rule serves bullish, bearish and two-sided
structures and the trigger never has to carry a direction field the model
could get wrong. `iv_spike` uses the position's *own* implied vol, solved from
its legs' marks — no second data fetch that could fail independently, and an
IV that will not solve is **not** an IV that spiked.

Closing submits **one new inverted mleg order** via
`options_orders.closing_payload` — `close_position` closes a single leg at a
time and would leave a naked short between calls (design spec A.1, verified
live). Limit price is `-close_net_credit`, matching Alpaca's signed
convention.

`OpenTradeStore` persists the book to `logs/open_trades.json` as inspectable
JSON, because each `make session` run is a fresh process and the broker's rows
carry the symbols but none of the structure, entry credit, triggers or
snapshot hash. A missing file is an empty book; a corrupt file or an
unrestorable record is logged at **error** level and skipped (loud, because a
skipped record means an unmonitored position).

### The keystone `close` entry

```json
{"type": "close", "payload": {
  "realized_pnl": 60.0, "underlying": "SPY", "structure": "bull_put_spread",
  "snapshot_hash": "...", "exit_reason": "...", "trigger": {...},
  "order_id": "...", "close_order_id": "...", "contracts": 1,
  "open_net_credit": 1.0, "close_net_credit": -0.4}}
```

The realized number **prefers the broker's fill** over the mark estimate:
magnitude from `filled_avg_price`, sign from whichever of ±price sits closer
to the mark-derived estimate — the true number without betting the P&L on an
unverified multi-leg sign convention.

An **unfilled** closing order journals `exit_pending`, never `close`: a
working order has realized nothing, and writing a P&L for it would feed the
calibration loop a number that never happened.

### `book_greeks` — the parked ruling, fixed

A row whose symbol is not an OCC option symbol is an **equity** row —
overwhelmingly this desk's own short leg assigned into stock. Its delta is
`±qty` and its vega is 0: no implied vol, no expiry, no model, the most
certain Greek in the book. Returning `None` for it froze the desk into
permanent abstention ("the existing book cannot be valued", exit 0, forever).
Still returns `None` for a row with no usable quantity, and for an instrument
whose spot will not resolve.

### Calibration weights in the live decision

`decide()` called `aggregate(views)` unweighted. It now computes
`calibration.analyst_weights` from the journal every cycle, passes them
through, and records them in that cycle's `trader_choice` payload so a judge
sees which weights produced which decision. It **fails soft** — and only here:
calibration is an input to a vote, not a precondition for one, so an
unreadable journal degrades to equal weights rather than abstaining a desk
that reasoned correctly. (The guard and the veto keep failing closed: those
decide *whether* to trade, this decides only how loudly each analyst speaks.)

### Production wiring (`scripts/run_session.py`)

- **manages the open book FIRST**, before looking for a new trade, so an exit
  is never skipped because the cycle later abstains on a working order or an
  empty candidate list;
- **runs the pre-mortem before the order is sent** and journals a `premortem`
  entry before `submit()` — triggers written after the fill would leave a live
  position with no exit plan if the process died in between;
- records a filled position in the store with its intent, triggers, approved
  contracts and snapshot hash;
- routes the pre-mortem through the **same prompt cache** the committee uses
  (`committee.decide.cached_client`), so a replayed cycle re-derives identical
  triggers; runs it only for a candidate the guard would let through, so a
  refusal costs no LLM call;
- `--no-llm` means no LLM in the pre-mortem either — the deterministic exits
  still apply;
- a dry run neither manages the book nor writes to it (a rehearsal must never
  close a real position) but **does** print the exit plan, which makes it a
  genuine preflight.

---

## 2. TDD evidence

Every module: tests written and run **red** first, then the implementation.

| Step | Red | Green |
|---|---|---|
| `committee/premortem.py` | `ModuleNotFoundError: No module named 'committee.premortem'` | 33 passed |
| `exit_monitor.py` | `ImportError` on `exit_monitor` | 32 passed |
| `OpenTradeStore` | 6 failed | 39 passed |
| `book_greeks` equity | 4 failed (`assert None is not None`, `'cannot be valued' in out`) | 6 passed |
| calibration weights in `decide()` | 3 failed (`KeyError: 'analyst_weights'`) | 46 passed |
| session wiring | 12 failed | 78 passed |

**Mutation check on `exit_monitor` (5 injected defects, scratch copy):**

| Mutation | Result |
|---|---|
| max-loss exit disabled | 3 tests fail |
| `close` journal entry not written | 1 fails |
| broker fill price ignored | 1 fails |
| `underlying_beyond` direction ignored | 1 fails |
| unknown IV counts as a spike | **0 fails — survived** |

The survivor was a real coverage gap on a fail-closed path, so I added
`test_an_unsolvable_iv_does_not_count_as_a_spike` (a mark below intrinsic
solves for no IV; "unknown" must not read as "spiked"). Re-run: **1 fails.**
All five now caught.

**No test was deleted or weakened.** Three existing assertions encoded claims
this work made false, and each was corrected to something *still true* rather
than removed:

- `test_an_unpriceable_book_abstains_rather_than_assuming_zero` used
  `"NOT-AN-OCC-SYMBOL"` as a stand-in for unvaluable. That is now a valid
  equity row, so the fixture withholds the **spot** instead — which is what
  actually makes a row unmeasurable — and the test additionally asserts the
  reason given. Strictly stronger.
- `test_empty_journal_states_no_resolved_predictions_plainly` asserted the
  report says `"DORMANT"`. That would now be a false claim in the opposite
  direction; it asserts `"no trade has closed yet"` instead, and the honesty
  property (`"0.00" not in report`) is untouched.
- `test_enough_resolved_predictions_drops_the_dormant_message` — same, plus a
  **new** test that the report no longer understates what is wired.

The two fixtures (`bench`, `llm_bench`) gained `store=` and `premortem=`
injection so no test can write the repo's real `logs/open_trades.json` or put
a `claude -p` subprocess in the suite. Every existing assertion is byte
identical.

The 12 original journal tests and the abstention-contract tests are untouched
and green.

---

## 3. End-to-end proof: a journalled close resolves an analyst prediction

`tests/test_exit_to_calibration.py` (12 tests) exercises the **seam**: a real
`Journal` on disk, real `committee.decide`, real `monitor_positions`, real
`calibration`, real `scripts/calibration_report.render`. Only the LLM and the
broker are fakes.

Ten committee cycles, each opened and then closed by the monitor:

```
=== BEFORE any close ===
role              resolved    brier   weight   interpretation
vol_analyst              0      n/a     1.00   unproven (0/10 resolved) …
bear_adversary           0      n/a     1.00   unproven (0/10 resolved) …
No resolved predictions yet; …

=== AFTER 10 cycles, each closed by exit_monitor ===
role              resolved    brier   weight   interpretation
vol_analyst             10    0.302     0.90   worse than baseline — down-weighted
bear_adversary          10    0.280     0.94   worse than baseline — down-weighted
20 resolved prediction(s) across 2 role(s) …

journal entry types: ['analyst_view', 'close', 'committee_decision',
                      'exit_signal', 'snapshot', 'trader_choice', 'veto']
close entries: 10
consecutive_losses now: 1
chain verify: (True, '')
```

**So: yes — a non-zero resolved count and a real Brier score.** The join works
because both sides carry the same key, which is asserted directly
(`test_the_close_entry_carries_the_cycles_own_snapshot_hash`). A single
winning cycle scores `(0.85 - 1)² = 0.0225` exactly; the adversary, who said
0.40 on the same winner, scores worse — the roles are graded independently.

The same proof runs **through `main()`** in
`test_a_closed_position_makes_the_calibration_loop_resolvable`: a real session
opens a position, a second session closes it, and
`resolved_predictions(journal, "vol_analyst") == [(0.8, True)]`.

`consecutive_losses` — the second dormant thing — is covered by its own class:
0 before any close, 3 after three losing closes, reset by a winner, and it
reaches `risk.yaml`'s `consecutive_losses_to_halve` from real journal data.

---

## 4. Mixed-book test result

`TestAssignedEquityDoesNotFreezeTheDesk` (6 tests), all green:

- 100 shares long → delta **+100.0**, vega **0.0**
- 100 shares short → delta **−100.0**, vega **0.0**
- **mixed book** (one long put + 100 shares, the exact shape a partially
  assigned short put spread leaves behind) → book is valued, and
  `delta == option_delta + 100.0`, `vega == option_vega`
- a row with no usable quantity → still `None` (fails closed)
- an equity row whose spot will not resolve → still `None`
- **`test_a_session_with_a_mixed_book_reaches_the_guard`**: through `main()`,
  `"cannot be valued" not in out`, `"guard:" in out`, exit 0.

**An assigned equity position no longer freezes the desk.**

---

## 5. Live aggregation is unchanged with all-1.0 weights

`TestCalibrationWeightsAreWired::test_todays_aggregate_is_unchanged_because_everything_is_unproven`
asserts `decision.aggregate_probability == aggregate(list(decision.views))`
— the plain unweighted mean — because nothing has 10 resolved predictions yet.
`test_the_weights_used_are_journalled` pins the journalled value at exactly
`{"vol_analyst": 1.0, "bear_adversary": 1.0}`.

A cycle with **no** weights at all (dry run, unreadable journal) takes the
byte-identical unweighted code path (`aggregate(views, weights or None)`)
rather than a weighted one that merely happens to agree.

The loop is nonetheless genuinely closed, and two tests prove it bites once
outcomes resolve: ten winners called at 0.9 push `vol_analyst` **above** 1.0;
ten losers called at 0.95 push it **below** 1.0 but never past
`WEIGHT_FLOOR`, and leave the adversary — who was less wrong — weighted
higher.

`make calibration` on the real journal today, correctly:

```
vol_analyst      0   n/a   1.00   unproven (0/10 resolved) — weight defaults to 1.0, not demoted
bear_adversary   0   n/a   1.00   unproven (0/10 resolved) — weight defaults to 1.0, not demoted
No resolved predictions yet; weights default to 1.0 … but no trade has closed
yet, so nothing above is influencing committee votes.
```

---

## 6. Live verification (paper account, real SPY chain)

`python3 scripts/run_session.py --dry-run --no-llm`:

```
guard: ALLOW — Within all limits: risk $217.00, 1/3 positions (1 contract approved)
pre-mortem exit plan:
  credit_decay  0.5   --no-llm: no LLM in the loop; deterministic profit target …
  dte_below     3     --no-llm: no LLM in the loop; hard rule: close at 3 DTE …
DRY RUN — no order sent.
```

A **real** `claude` pre-mortem call on the same live candidate
(bear_call_spread 770/775, credit 2.83, max loss 217, 34 DTE, realized vol
9.52%) returned five model triggers, **all kept, all correctly on the
upside**:

```
credit_decay       0.5      deterministic profit target
dte_below          3        hard rule, assignment avoidance
underlying_beyond  770      Short strike breached; the sold 770c moves into intrinsic territory
underlying_beyond  772.83   Breakeven crossed; P&L turns negative above this price
underlying_beyond  777      Max loss level (short strike + width); hard stop
iv_spike           0.18     IV expansion to 18% (1.9x realized) …
dte_below          7        One week to expiry; gamma risk peaks
```

That is LLM reasoning that has become seven enforced rules.

`make verify-journal` → **Chain: INTACT**.

---

## 7. Known gaps (not fixed, deliberately)

- **A `pending` order is not written to the open book.** Only `filled` /
  `partially_filled` are. While an order is working the session abstains on
  it, so nothing is double-sent; but if it fills between cycles, the position
  exists with no `OpenTrade` record and the monitor will not manage it.
  Closing this properly needs order→position reconciliation. Mitigation
  already in place: `manage_open_book` will not drop a trade whose order id is
  still among the broker's working orders.
- **`--no-llm` cycles carry an empty `snapshot_hash`**, so their closes cannot
  resolve any analyst prediction. Correct — there were no predictions.
- The `close` entry's realized P&L is the broker's fill price where available,
  the mark estimate otherwise. Both are recorded (`open_net_credit`,
  `close_net_credit`) so the arithmetic is auditable.

---

## 8. INCIDENT — an unintended paper order was submitted and cancelled

**What happened.** While committing the wiring change, my `git commit -m`
message contained backticks (`` `make session` ``). The shell expanded them as
command substitution and **ran `make session`**, which executed a full live
cycle and submitted a real multi-leg order to the paper account at
**14:43:11 UTC**.

**State now — verified against the broker:**

- order `b73f3824-eaa2-421e-9a71-0086a4759a3b` — **canceled** at 14:44:38 UTC
- `filled_qty: 0` — it **never filled** (Saturday, market closed)
- equity **$100,000**, `last_equity` $100,000 — unchanged
- **0 positions, 0 working orders**

**What it was.** Not a rogue trade: a fully guarded, committee-approved
bear_call_spread 781/786, 1 contract, credit 1.78, max loss $322, both vetoes
PASS, guard ALLOW. Every safety rail worked. What failed was my shell
discipline, not the desk.

**Journal.** Entries `seq 6–15` (snapshot → analyst_view ×2 → trader_choice →
veto → committee_decision → premortem → verdict → proposal → pending) are a
truthful record and were **not** edited — the chain is append-only (hard rule
5). I appended `seq 16`, type `operator_cancel`, stating plainly that the
order was submitted by an accidental shell expansion and cancelled, with
`realized_pnl: null`. `operator_cancel` is not in `CLOSING_TYPES`, so it
cannot pollute `consecutive_losses` or the calibration join. `verify_chain`
returns `(True, '')`.

**Two things for you to decide:**

1. Whether `operator_cancel` should become a real, tested entry type (there is
   currently no cancel writer in the codebase), or whether you would rather
   the incident be recorded only in the ledger.
2. Whether the pre-existing `abstain` at `seq 5` plus this cycle changes your
   Monday preflight expectations — `has_ever_traded(entries)` is now **True**,
   so the fresh-account assertions no longer run. That is by design (Plan 1
   C3) but it is now genuinely triggered rather than hypothetical.

I have not re-run `make session` since, and I did not run any command that
sends an order after the cancellation.

---

## 9. Commits

```
908d9e2  add committee/premortem.py: LLM failure modes compiled into enforceable exit triggers
445e8c1  add exit_monitor.py: manage the open book and journal a CLOSED trade with realized P&L
0c9bd13  fix: an assigned equity position no longer freezes the desk into permanent abstention
70f9cc0  wire calibration weights into the live committee decision
d4f3cc9  test: prove end to end that a journalled close resolves an analyst prediction
baabd29  wire the pre-mortem and the exit monitor into the live session
3a66b26  docs: the calibration loop and the pre-mortem are no longer "planned"
```
