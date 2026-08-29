# SDD ledger — plan: docs/superpowers/plans/2026-08-30-compliance-and-options-execution.md

Spec: docs/superpowers/specs/2026-08-29-agentic-options-desk-design.md (read)
Base: 4b846132d5abf2dc62a0e9deb97147c1e91c73c8 (main)

## Pre-flight conflict scan

### Cross-task rows (tasks sharing a file or interface)
| Pair | Produces -> Consumes | Finding |
|---|---|---|
| T1 -> T3 | `AlpacaCLI.post_order(payload)->dict`, `get_order(id)->dict` -> T3 FakeCLI implements both | consistent |
| T2 -> T3 | `build_mleg_payload(intent, contracts, limit_price=None)` -> T3 calls `(intent, contracts)` | consistent |
| T1 -> T5 | `available()`, `get_account()`, `list_positions()` -> T5 calls all three | consistent |
| T2 -> T3 | `closing_payload` produced, not consumed until Plan 2 exit monitoring | consistent (forward use) |
| T3 -> T5 | `OptionsExecutor(cli,guard,journal)`, `.submit(intent,state)` -> T5 calls exactly that | consistent |
| T4 -> none | `.mcp.json` config only, no code interface | independent |
| T5 -> T6 | `make session` target -> T6 runs it | consistent |

### Per-task self-consistency rows
| Task | Tests vs code it specifies | Finding |
|---|---|---|
| T1 | args assertions match `[binary, account, get, --quiet]`; env assertion matches impl | agrees |
| T2 | credit -> negative limit (net_credit +1.00 -> -1.00); debit -> positive (-9.10 -> +9.10); leg-count and unique checks ordered correctly | agrees |
| T3 | poll_seconds=0 returns "new" -> pending; downsize 3->2 asserts qty "2" | agrees |
| T4 | config only, no tests specified | agrees (nothing to contradict) |
| T5 | illiquid chain -> [] via gate; 8-strike chain yields 3 bull puts + 3 bear calls + 1 straddle | agrees |

### Findings and rulings
F1: T5 `test_candidates_have_unique_ids_for_llm_selection` is vacuous — it asserts
    uniqueness of ids it constructs itself from `range()`, never touching
    build_candidates output. Review rubric treats a test that asserts nothing as
    a defect.
    Ruling: replace with a test asserting the returned intents are mutually
    distinct (structure + leg symbols). Cost if wrong: negligible; a stronger
    test than the plan specified.

F2: T5 straddle ATM uses `min({...set of floats...})` — tie-breaking depends on
    set iteration order when two strikes are equidistant from spot (445/455 at
    spot 450 in the test chain).
    Ruling: sort the strike candidates before `min` so selection is
    deterministic. Cost if wrong: none; determinism is required later for
    golden-file replay in Plan 3.

F3: T1 `run()` returns `dict | list` but `get_account()` annotates `-> dict`.
    Ruling: acceptable — `run` is annotated `dict | list`, callers narrow.
    Minor, no action. Cost if wrong: none.

R1 (branch): Implementation proceeds on `main` rather than a worktree. The user
    has directed all work to main this session (12 commits pushed), this is a
    solo hackathon repo with a hard 2026-09-04 deadline, and branch ceremony
    risks unmerged work at submission. Cost if wrong: history is linear on main;
    revert is per-commit.

R2 (model ids, forward-looking): canonical Claude model IDs carry no date suffix
    (`claude-haiku-4-5`, not `claude-haiku-4-5-20251001`). Applies to Plan 2's
    committee, not this plan. Cost if wrong: a 404 on first LLM call.

## Task log

### Live-data validation of Phase 2 (controller, read-only, 2026-08-29)
SPY spot 769.35. Chain 3686 contracts. Liquidity gate: 1078 PASS / 3686.
Failures: oi 1595, oi+spread 612, spread 401 — all correctly rejected.
Liquid put strikes 1 point apart near the money (745..756); liquid DTEs
10,11,12,13,20,27,32,34,41 — all inside the 7-45 window. A 5-wide spread
risks <= $500/contract, under the $1000 cap.
Conclusion: candidate generation will have material choice on Monday.

### Feed finding
OptionChainRequest.feed defaults to None; the server resolves it to the
account entitlement (indicative). `opra` is NOT signed on this account and
would 403 if requested explicitly. Do not set feed=opra anywhere.

### Plan 2 dependency checks (controller, live, read-only)
NEWS      OK  NewsClient.get_news -> real Benzinga headlines for SPY
CALENDAR  OK  69 sessions ahead, correctly flags 2026-11-27 early close 13:00
SCREENER  OK  get_most_actives returns a live universe
GREEKS    Alpaca supplies greeks+IV free on the indicative feed for ~61% of
          contracts; None exactly where the quote is unpriceable. Plan: prefer
          Alpaca's, fall back to vollib, log divergence.
FILLS     TradingStream.subscribe_trade_updates replaces order polling; per-leg
          TradeUpdate events for an mleg order.
CORRECTION to earlier MCP research: `do_not_exercise_options_position` does NOT
          exist in alpaca-py (support ticket only) — do not build on it.
CORRECTION: close_position closes ONE LEG at a time — there is no atomic mleg
          close. Unwinding a spread must submit a new mleg order with inverted
          sides, which is exactly what options_orders.closing_payload does.

### Controller cross-check of Task 2 output (independent of task review)
Built a real bull put spread payload and validated the identical shape through
alpaca-py's own LimitOrderRequest/OptionLegRequest validators:
  PASS  limit_price -1.00 (negative = credit), order_class mleg, tif day,
        legs sell_to_open 445P / buy_to_open 440P, ratio_qty 1
  Negative controls: 1-leg and 5-leg MLEG both correctly rejected by Alpaca.
Conclusion: sign convention and leg shape match broker expectations.

Task 1,2,4: implementer DONE (commits 3e8ae18, f62f3cb, eee6531), 216 -> 237
tests passing. Task review dispatched.
Task 1,2,4: review clean — Spec OK, quality Approved, 0 Critical/Important.
  Reviewer independently ran `uvx alpaca-mcp-server` and read toolsets.py:
  order-placement tools are gated behind the "trading" toolset, which
  .mcp.json does not enable -> "no trading exposed over MCP" is structurally
  true, not an assumption. COMPLIANCE.md append integrity confirmed.
Task 1,2,4: minor (deferred): alpaca_cli.list_positions coerces a non-list
  JSON body to [] — brief-specified code, but a genuine anomaly could read as
  "no positions". Hardening candidate for the final review.
Task 1,2,4: minor (deferred): pre-existing websockets.legacy DeprecationWarning
  surfaces only under -W error; disclosed, out of scope for this batch.
Task 1,2,4: complete (commits 4b84613..eee6531, review clean)

### Controller end-to-end simulation on live data (2026-08-29)
SPY 769.35, chain 3686 -> 628 candidates (440 bull put, 188 bear call).
Best by credit-per-dollar-risked: bear call 770/775 exp 2026-10-02 (34 DTE),
credit $2.83/share, max_loss $217, max_profit $283, breakeven 772.83,
payload limit_price -2.83 (credit, correct sign).

Ruling: PLAN DEFECT — Task 5 must compute and pass position Greeks.
  Finding: RiskGuard.evaluate(intent, state) defaults position_delta and
  position_vega to 0.0. Nothing in Plan 1 computes them, so the risk.yaml
  limits |net delta| <= 30 and |net vega| <= 200 NEVER FIRE. All 628
  candidates returned ALLOW in simulation.
  Verified the guard itself is correct: passing real Greeks for the 770/775
  spread (delta -8.01, vega +0.33 per spread) makes it correctly DENY at 4
  contracts (delta -32.0). Vega is near zero for a vertical because the legs'
  vegas cancel (0.9302 vs 0.9335) — that limit will bind on straddles, not
  verticals, which is expected and correct.
  Decision: amend Task 5 to compute per-position Greeks from the chosen
  intent's legs (sign by side, x100 x contracts) and pass them to
  guard.evaluate(). Carry this into the Task 5 dispatch.
  Cost if wrong: two headline risk limits stay decorative; a delta-heavy book
  could be built without the guard objecting. Not fixing this would make
  CLAUDE.md's stated limits untrue.

Task 3: review — Spec OK, quality Approved. 1 Important + 1 Minor, both in the
  brief's own code, not implementer deviations. Reviewer independently traced
  DENY->broker (never reaches), downsize->payload (approved_contracts reaches
  the wire, not intent.contracts), fill fidelity (only "filled" sets
  filled=True), and _poll termination in four scenarios. No money-path defects.

Task 3: Ruling: journal.append is not exception-safe on the success path —
  a raise after post_order succeeded escapes submit(), leaving the caller with
  no ExecutionResult and an order live at the broker, inviting a retry and a
  DUPLICATE LIVE ORDER. Plan-mandated code, but the spec's fail-closed
  principle is the binding authority and outranks transcribe-verbatim.
  Decision: fix — route every journal write through a _record() helper that
  logs loudly and never raises. Cost if wrong: a slightly larger diff than the
  plan specified; the alternative risks duplicate live orders on Monday.

Task 3: Ruling: bundling the Minor polling-coverage finding into the same fix
  round rather than deferring it. test_pending_order_is_reported_after_polling
  makes exactly ONE get_order call (poll_seconds=0), so nothing exercises the
  sleep-and-recheck loop we depend on at market open. Same file, no extra
  round. Cost if wrong: negligible.

Task 3: fix round 1/5 dispatched (2 findings)
Task 3: fix round 1/5 (2 addressed, 0 open; commits 7d1d17c..4c28219)
  Re-review confirmed all six journal.append sites inside submit() route
  through _record(), which logs at error level and never re-raises; both
  injected-failure tests assert the returned status AND that nothing reached
  the broker on denial. Multi-iteration polling now genuinely exercised
  (3 get_order calls, injected clock, asserted call count). No new breakage.
Task 3: complete (commits 0bb84d4..4c28219, review clean)
Task 3: minor (deferred): under `-W error::DeprecationWarning` the
  websockets.legacy warning raised inside alpaca_data.get_stock_bars is caught
  by its broad `except Exception` and reported as "Bar fetch failed",
  returning an empty frame. Not a defect in normal runs (pyproject filters the
  warning) and empty-on-failure is the designed fail-closed behaviour, but it
  shows the broad except can mask an unexpected error class. Flag to the final
  whole-branch review.
Task 5: dispatched with controller amendment (position Greeks, vacuous-test
  replacement, deterministic ATM selection). BASE 4c28219.

### Controller verification of Task 5 (live, read-only)
`python3 scripts/run_session.py --dry-run` against the real chain:
  SPY spot $769.35 — 632 candidates
  Selected bear_call_spread: credit $2.83, max loss $217.00
  DRY RUN — no order sent.   exit 0
Confirmed via TradingClient: 0 open orders, 0 positions. Nothing was sent.

Controller finding (to raise if the review does not): the dry-run path returns
BEFORE position_greeks is computed, so --dry-run does not display the Greeks or
the guard verdict the live run would produce. Functionally safe — nothing
submits — but it makes the dry run a weaker Monday preflight than it should be,
since the whole point is to see what the live run would do.

Task 5: implementer DONE (ccdb0fa, c36f85d, 4f7f011), 248 -> 261 tests.
  Implementer independently found and fixed a PLAN defect: build_candidates
  paired legs by strike alone, colliding quotes across the chain's multiple
  expiries (build_bull_put_spread then raises "legs must share an expiry").
  My own simulation grouped per-expiry, which is why I never hit it. Fixed
  with per-expiry grouping plus a regression test.
Task 5: review — Spec OK (amendments A/B/C all genuinely implemented), quality
  Approved, 0 Critical/Important. Reviewer verified Greeks are truly forwarded
  to guard.evaluate (not accepted-and-dropped), grepped the repo to confirm
  run_session is the ONLY production submit() call site and always computes
  them first, traced every --dry-run return as preceding executor construction,
  and independently measured straddle vega ~101.8 vs vertical <50.
Task 5: minor (deferred): scripts/run_session.py cli.get_account()/
  list_positions() are unguarded — a transient CLI/auth failure crashes with a
  raw traceback BEFORE startup_checks and the kill switch are reached, instead
  of failing closed with a clean message. Brief-inherited.
Task 5: minor (deferred): tests/test_analytics.py
  test_unpriceable_leg_yields_zero_not_an_exception is misnamed — spot=0.0
  short-circuits the top-level guard and never reaches the per-leg
  implied_vol/continue branch it claims to test. That branch has no direct
  test. This came from MY amendment, not the implementer.
Task 5: minor (deferred, controller-found): --dry-run returns before
  position_greeks, so it does not display the Greeks or the guard verdict the
  live run would produce — weaker Monday preflight than intended.
Task 5: complete (commits 4c28219..4f7f011, review clean)

All 5 code tasks complete. Dispatching final whole-branch review.

## FINAL WHOLE-BRANCH REVIEW (opus) — verdict: DO NOT run `make session` Monday as-is
Per-module code judged good (options_orders, executor_options, risk_guard
"careful, correctly signed, genuinely tested"). Defects concentrate at ONE
seam: scripts/run_session.py — the only production submit() caller and the
only file with no tests of its money path.

CRITICAL
C1 duplicate live order on rerun: nothing queries OPEN ORDERS; an unfilled
   order returns "pending", main() exits 0, order stays live all day, next run
   sees unchanged equity + 0 positions, preflight passes, same best candidate
   -> second identical spread. No client_order_id, so non-idempotent at broker.
C2 four of six PortfolioState fields hardcoded -> daily-loss halt, 3-loser
   halving, per-underlying cap, and existing-book Greeks ALL inert. This is the
   SAME defect class I caught for Greeks, one field over in the same
   constructor. I fixed one field and did not look at the other four.
C3 make session is single-use: equity= passed unconditionally, so after the
   first fill preflight fails forever ("equity is not the expected fresh
   $100,000"). Live session gets exactly one trade then a hard stop.

IMPORTANT I1 executor gates on != DENY (fail-open by shape; is_tradeable unused)
  I2 CLI timeout after POST reached Alpaca reported as "rejected" -> retry risk
  I3 chain API failure reported as liquidity ABSTAIN, exit 0 (spec requires
     fail-loud on data) — misreports an outage as a market judgement on camera
  I4 kill switch is CWD-relative: inert outside repo root, so cron/systemd has
     NO kill switch. Hard rule 6.
  I5 open_positions counts option LEGS not positions -> max_positions 3 really
     permits ONE spread
  I6 journal reads tail BEFORE flock -> concurrent journalers fork the chain
     and make verify-journal (a judged artifact) fails permanently
  I7 test coverage inverted: main() has zero tests; pure functions have 13

MINOR M1 unpriceable leg silently yields REMAINING legs' Greeks (demonstrated
     -22.2 vs true ~+12, wrong sign) — reachable once condors/ITM enabled
  M2 max(credit/max_loss) selects the most anomalous quote by construction
  M3 partially_filled in neither terminal set
  M4 DOWNSIZE branch dead in production (contracts always 1)
  M5 get_option_contracts does not paginate
  M6 UTC/local date skew at the DTE boundary during the IST session

Ruling: ONE consolidated fix wave dispatched (opus) covering all Critical, all
Important, deferred minors 1/3/4/5, and M1/M3. Deliberately NOT fixing M2, M4,
M5, M6, deferred-minor-2 — each is fail-closed and none blocks Monday.
Cost if wrong: a larger diff to re-review; the alternative is shipping a
session that can double-submit and whose stated limits are decorative.

### Controller verification after fix wave (live, read-only)
Suite: 261 -> 333 passing (+72 tests from the fix wave).

A transient DNS failure hit during the first dry-run attempt and the I3 fix
demonstrated itself in production:
  "DATA FETCH FAILED — option chain for SPY (...NameResolutionError...).
   This is an outage, not a market judgement. No trade."
The old code would have printed "ABSTAIN: no candidate passed the liquidity
gate" and exited 0 — the exact misreport the reviewer warned about, and it
occurred naturally rather than being contrived.

Retry with the network up:
  SPY spot $769.35 — 632 candidates
  Selected bear_call_spread: credit $2.83, max loss $217.00
  position greeks: delta -8.0, vega +0.3        (matches my manual -8.01)
  book: 0 positions, delta +0.0, vega +0.0, day P&L $+0.00,
        0 consecutive losses, opened today {}   <- C2 fixed, real values
  guard: ALLOW — risk $217.00, 1/3 positions (1 contract approved)
  payload includes client_order_id ad0641c4...  <- C1 fixed, idempotent
  DRY RUN — no order sent.   exit 0
The dry run is now a genuine preflight: it shows the Greeks, the book, the
guard verdict and the exact wire payload before anything is sent.

## SCOPED RE-REVIEW OF FIX WAVE (opus) — verdict: SAFE TO RUN LIVE MONDAY
All 13 findings ADDRESSED, each verified by MUTATION: the reviewer reverted
every fix in a scratch copy and confirmed the named tests fail. Six mutations
knocked out 33 distinct tests across 6 files.

Test integrity: NO test deleted (2 removed def lines were renames; per-file
counts additive 261 -> 333). Both rewritten assertions ENCODED THE DEFECT
(M1 asserted zeros for unpriceable Greeks; I3 asserted [] on error) and each
was replaced with strictly stronger coverage plus a preserved counterpart.
The two fixture changes are mechanics — every assertion byte-identical. One
test was found passing for the wrong reason and genuinely strengthened.
No new test asserts on a mock or passes regardless of what it names.

RESIDUALS — rulings:
Ruling: FIX client_order_id local-date -> UTC. Load-bearing, not cosmetic: the
  live session runs 19:00-01:30 IST, straddling LOCAL midnight, so mid-session
  the idempotency key would change while the UTC day did not — exactly the
  window where duplicate protection matters. Cost if wrong: a trivial diff.
Ruling: FIX print-refusal-then-submit. Outcome is correct today (the executor
  re-evaluates), but removing KILL_SWITCH between the two evaluate() calls
  would submit right after printing a refusal. Cheap to close.
Ruling: FIX the honesty gaps — comment that consecutive_losses is INERT until
  exit monitoring journals closes with realized_pnl (Plan 2), and document the
  kill switch in README (it is the operator's only manual halt and appears
  nowhere).
Ruling: PARK — an assigned equity position permanently freezes the desk into
  abstention (book_greeks returns None for a non-OCC row, so every later cycle
  prints "existing book cannot be valued" and exits 0, forever). Real, and a
  demo-killer, but it FAILS CLOSED — no money at risk, honest message — and a
  proper fix needs the Plan 2 exit monitor. Carry into Plan 2 as a first-class
  requirement. Cost if wrong: a frozen desk mid-demo, recoverable by closing
  the equity position manually.
Ruling: PARK — count_positions groups by (root, expiry), so two distinct
  spreads on the same underlying and expiry count as one. Under-counts in the
  permissive direction, but max_new_per_underlying_per_day: 1 prevents it
  within a day and total exposure stays bounded by max_positions x
  max_loss_per_position. Cost if wrong: at most one extra concurrent spread.

---

## Sunday-eve committee session — findings (2026-08-29)

Five defects, every one found by RUNNING the system, not reading it. The suite
was green (335 -> 497) throughout all of them.

1. **Committee was never wired into the session.** `committee/` was built,
   tested and verified live, but `scripts/run_session.py` never called it —
   selection was still the deterministic credit/max-loss ratio. Monday would
   have produced a fill from the non-agentic path with the committee sitting
   unused. Nothing journalled it either, so the judge-page replay corpus was
   empty. Fixed: `committee/decide.py` orchestrator + full journal trail +
   wired prompt cache + a `--no-llm` fallback for rate limits.

2. **Walk-forward harness inflated its own win rate.** Held 11 bars, scaled the
   breach threshold to 21 days (sqrt-of-time), giving a threshold ~1.38x too
   generous and a manufactured 29/30 win rate. Corrected: AAPL now loses money
   (-0.12R, 6R drawdown). Same species as the fabricated 82.2% deleted in
   Phase 1, except computed wrongly rather than invented.

3. **Vol analyst read its central input backwards.** IV below realized means
   options are cheap relative to actual movement — an argument AGAINST selling
   premium. The analyst concluded the opposite (p=0.66) while the adversary got
   it right. Cause: the snapshot stated the signed number without its
   interpretation. Fixed; the analyst now reads p=0.32 and both agree.

4. **The committee could only see ONE structure type.** Of 632 candidates (440
   bull put, 188 bear call, 4 straddle) the cap surfaced 12 bear calls and
   nothing else. Its abstention was correct reasoning on a rigged menu — and it
   meant Monday would likely abstain and produce NO FILL. Fixed with stratified
   selection: now 4/4/4.

5. **ATM implied vol was computed from the surfaced candidates, not the
   market** (`snapshot.py:256`). The vol-regime signal — the analysts' primary
   input — was therefore a property of our own selection. Measured: on one
   unchanged chain (same spot, same bars) the reading went from 0.69pp BELOW
   realized to 0.72pp ABOVE, and the recommendation flipped from ABSTAIN to a
   selected bear call spread. Cause is skew: surfacing only bear call spreads
   sampled only OTM calls, which sit lower on the skew.

   NOTE: the subagent that fixed finding 4 attributed this flip to "live market
   state". That was wrong — spot, bars and session were identical. Accepting
   that explanation would have closed a real defect as a non-issue. Diagnosed
   by the controller and fixed separately.

Pattern worth keeping: unit tests with injected fakes passed through all five.
Only live execution against the real chain and real models exposed them.

## MILESTONE — full pipeline produces a guard-approved trade (2026-08-29 late)

519 tests. Live SPY, spot 769.35, 632 candidates -> 12 surfaced.

    vol_analyst    p=0.63  IV 2.27pp above realized; options rich for selling
    bear_adversary p=0.40  "breakevens only 0.4-0.6% away; a routine 1-2% move
                            in 27 days breaches them"
    trader: c3            "materially wider cushion (1.75% vs 0.3-0.5% on the
                            other top picks) and by far the deepest liquidity
                            (OI 1699/21255), directly addressing
                            bear_adversary's core objection"
    thesis veto PASS      net delta -8.46 consistent with the bearish thesis
    blind veto  PASS      independent agreement
    guard ALLOW           risk $322.00, 1/3 positions, 1 contract
    payload               limit_price -1.78, client_order_id a2bb861b...

The adversary's objection CHANGED the selection — the trader declined the
highest-credit candidate for the one that answers the criticism. That is the
demonstrable difference between a committee and a rubber stamp.

Every abstention before this point was correct given what the committee was
shown. Each fix was to the MENU, never to the judgement:
  - a single-structure menu (12 bear calls, 0 of anything else)
  - straddles exceeding the max-loss cap (guaranteed denials)
  - the four tightest breakevens of 188 available
  - far-OTM wings at $0.02 credit against $498 risk (249:1)

Deliberately NOT tuned: the blind veto. When it blocked a trade, I tested it
across six candidates first — it passes some and vetoes others with sound
reasons, so it discriminates rather than blanket-refusing. Softening a safety
component until it approves more trades is the failure this project exists to
avoid.

Notable: the blind reviewer caught a defect in the deterministic code meant to
constrain it — it flagged "$0.02 credit against $498 max loss is indefensible
risk/reward (249:1 against trader)" unprompted, which is what prompted the
credit floor. The decorrelated veto is not theatre.
