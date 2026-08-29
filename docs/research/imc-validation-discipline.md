# What the 2nd-place IMC teams actually did, and three corrections

Studied 2026-08-30 from ~/ta-reference/imc/. Read-only.

## Three corrections to the brief

**1. "Linear utility" is a team name, not a technique.** ericcccsliu's team was
called Linear Utility. There is no utility function anywhere in the repo; a grep
across all Python and Markdown finds it only in the README title. Their actual
rule is a z-score threshold followed by a jump to a fixed target position.

**2. TimoDiehm deliberately REJECTED z-scores.** Their README states it plainly:
"using a z-score normalizes the spread by recent volatility, but unless
volatility is known to vary meaningfully over time (which we did not observe
here), this introduces unnecessary complexity and risk of overfitting." They
used fixed absolute thresholds throughout. The 2nd-place team's considered
position is the opposite of "z-score everything".

**3. Neither team used conviction-based sizing.** Both size binary: on signal,
jump to the position limit. Timo uses `max_allowed_buy_volume` on every entry;
Eric jumps to a constant target (58 of 60, or the full 600). Size scaled with
signal strength appears nowhere. Their sizing cap was the exchange position
limit, which in the simulator *was* their risk framework.

## What is genuinely worth porting

1. **Structural justification before statistical fit.** Require a written
   mechanism for why an edge should exist. If the only evidence is backtest
   P&L, abstain.
2. **Warm-up gate.** Emit nothing until history length >= the indicator's own
   lookback. Timo returns `{}` while `timestamp/100 < window`; Eric returns
   `None` while `len(hist) < 30`.
3. **Hysteresis: entry and exit thresholds must differ.** Timo opens at 0.5 and
   closes at 0.0. Never reuse one number for both.
4. **A signal-liveness switch beats conviction sizing.** Track a rolling mean
   absolute deviation per instrument and only activate it when that clears a
   cost-based floor; when it drops below, flatten rather than hold. This maps
   directly onto "only sell premium where the recent IV-RV gap exceeds spread
   plus fees".
5. **Widen the threshold when the edge is small relative to tick cost.** Timo
   adds +0.5 to the entry threshold when vega <= 1.
6. **Select parameters on a plateau, not a peak.** They grid-searched then chose
   the centre of a flat region: "we chose combinations that showed consistent,
   flat regions" rather than maximum backtested profit. If a +/-20% perturbation
   changes the result materially, the parameter is not real.
7. **Cut size when protecting capital.** Both halved exposure in the round where
   downside mattered most.

## What to reject outright

- **Their threshold numbers.** `zscore_threshold: 9 / 7 / 5.1` and
  `default_spread_mean: 379.50439988484239` are fitted per round on three days
  of simulator data. A |z| >= 7 entry is roughly 1-in-10^12 under normality; it
  only worked because the simulated spread was a bounded synthetic process.
- **Their error handling.** Timo wraps product traders in `except: pass`
  fourteen times. An exception silently skips orders and never halts. That is
  fail-open, the exact inverse of our hard rule.
- **Their risk model.** Neither repo has a loss-streak, daily-loss or drawdown
  guard. `risk.yaml` already exceeds both. Do not weaken ours toward theirs.
- Eric's README itself confesses lookahead bias in a fixed mean and calls a
  mean-reversion finding "very very likely overfit". Round 5 was fitted to
  leaked prior-year competition data.
