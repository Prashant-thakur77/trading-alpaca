# Calibration seeding run — SPY

- Window: **2026-06-01 .. 2026-07-31** (every 2 trading day(s))
- Attempted: **20** decision dates — **18** replayed, **2** skipped
- Committee abstained on **11** of 18 replayed windows (**61%** abstention rate)
- Windows resolved to a realized P&L: **7**
- LLM: **60** calls made, **2** served from the prompt cache, **$3.0663** spent

## Why the window starts where it does

The model's **knowledge cutoff is May 2026**, so no window may begin before **2026-06-01**. Replaying an LLM committee over dates inside its own training data is the knowledge-contamination criticism this project levels at TradingAgents (arXiv 2412.20138) and FinMem (arXiv 2311.13743); making it of ourselves would be worse. `seed_replay.validate_window` raises rather than clamps, and `seed_replay.decision_dates` drops any pre-cutoff day it is handed, so the only possible response to running out of post-cutoff calendar is FEWER windows, never earlier ones.

## Windows replayed

| as-of | expiry | spot | cands | choice | structure | resolution method | exit | realized P&L |
|---|---|---|---|---|---|---|---|---|
| 2026-06-01 | 2026-06-26 | 758.54 | 12 | c9 | bull_put_spread | leg_bars_forced_dte_exit | 2026-06-23 | $-246.00 |
| 2026-06-09 | 2026-07-10 | 737.05 | 12 | c9 | bull_put_spread | leg_bars_profit_target | 2026-06-15 | $76.00 |
| 2026-06-11 | 2026-07-10 | 737.76 | 12 | c9 | bull_put_spread | leg_bars_profit_target | 2026-06-15 | $76.00 |
| 2026-06-15 | 2026-07-10 | 754.83 | 12 | ABSTAIN | — | — | — | — |
| 2026-06-17 | 2026-07-17 | 740.96 | 12 | ABSTAIN | — | — | — | — |
| 2026-06-22 | 2026-07-17 | 744.39 | 12 | ABSTAIN | — | — | — | — |
| 2026-06-24 | 2026-07-24 | 733.24 | 12 | ABSTAIN | — | — | — | — |
| 2026-06-26 | 2026-07-24 | 728.99 | 10 | ABSTAIN | — | — | — | — |
| 2026-06-30 | 2026-07-31 | 746.77 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-02 | 2026-07-31 | 744.78 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-07 | 2026-08-07 | 747.71 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-09 | 2026-08-07 | 751.71 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-13 | 2026-08-07 | 749.17 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-15 | 2026-08-14 | 754.81 | 11 | c10 | bull_put_spread | leg_bars_profit_target | 2026-08-04 | $103.00 |
| 2026-07-17 | 2026-08-14 | 743.29 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-21 | 2026-08-21 | 748.28 | 12 | c8 | bull_put_spread | leg_bars_profit_target | 2026-08-03 | $51.00 |
| 2026-07-23 | 2026-08-21 | 738.18 | 12 | c10 | bull_put_spread | leg_bars_profit_target | 2026-08-03 | $79.00 |
| 2026-07-27 | 2026-08-21 | 739.09 | 12 | c8 | bull_put_spread | leg_bars_profit_target | 2026-08-03 | $62.00 |

## Windows skipped

| as-of | reason |
|---|---|
| 2026-06-03 | only 0 of 26 legs had a real bar on 2026-06-03 (need 6) |
| 2026-06-05 | only 0 of 26 legs had a real bar on 2026-06-05 (need 6) |

## Abstentions

- **2026-06-15** — trader abstained: vol_analyst abstained so there is no second model family to confirm a direction, and the sole view (bear_adversary) argues realized vol (16.16%) exceeds implied (13.05%) by 3.11pp, meaning every available candidate here is a short-premium structure priced against the desk's own edge.
- **2026-06-17** — trader abstained: Both analysts assign sub-50% probability and realized vol exceeds implied by 0.95pp, meaning every available candidate sells premium into a mechanically disadvantageous regime with thin breakeven cushions; no candidate offers enough edge to override that headwind.
- **2026-06-22** — trader abstained: Both analysts agree implied vol is below realized vol, favoring buying premium, but every available candidate is a short-vol premium-selling spread structurally opposed to that signal, so no candidate has a favorable edge this cycle.
- **2026-06-24** — trader abstained: All 10 candidates are credit spreads (selling premium), but IV is 89bps below realized vol, which both vol_analyst and bear_adversary flag as unfavorable for premium-selling; bear_adversary's 0.40 probability and vol_analyst's long-vol thesis point away from every available structure, so no candidate has committee conviction behind it.
- **2026-06-26** — trader abstained: Implied vol sits 0.94pp below realized, a regime that structurally disadvantages every candidate here since all nine are short-premium credit spreads; the analysts disagree (vol_analyst 0.48, bear_adversary 0.25) rather than confirming a shared directional edge, so per the veto rule we sit out this cycle.
- **2026-06-30** — trader abstained: Both committee members flag that realized vol exceeds implied vol by 4.4pp, favoring long-premium structures, yet every available candidate is a short-premium credit spread; the bear_adversary further notes the most favored strikes (c1, c11) sit only 3-4 points OTM against an ~18.6% realized-vol regime, so no candidate is well-aligned with the analysts' shared view.
- **2026-07-02** — trader abstained: Both analysts agree IV is cheap relative to realized vol (13.66% vs 18.40%), which favors buying premium, but every available candidate is a short-premium credit spread with breakevens uncomfortably close to spot given that vol regime; no candidate matches the favored structure and the committee doesn't converge on a short-premium trade, so ABSTAIN.
- **2026-07-07** — trader abstained: IV sits 2.73pp below realized vol, meaning premium is cheap relative to actual movement, yet every available candidate is a credit spread that sells that cheap premium; with vol_analyst abstaining for lack of a long-vol structure and bear_adversary flagging the regime as backwards for selling, there's no directional or vol-regime consensus supporting any short-premium candidate this cycle.
- **2026-07-09** — trader abstained: Both analysts agree implied vol (13.31%) sits below realized vol (14.94%), a regime that favors buying premium, yet every available candidate is a credit spread selling that same underpriced vol; with the closest breakeven (c1) only 0.79% from spot, the structural mismatch applies to all 11 candidates so no credit spread here has a favorable edge.
- **2026-07-13** — trader abstained: vol_analyst's conviction is essentially a coin flip (0.48) and bear_adversary flags that realized vol exceeding implied structurally disadvantages every candidate here, since all are premium-selling credit spreads; with no directional edge and an unfavorable vol regime for this whole candidate set, standing aside is more defensible than forcing a trade.
- **2026-07-17** — c10 (bull_put_spread) vetoed — blind review: Short strike at 735 is only 1.11% cushion below spot with 28 DTE; realized vol of 11.96% implies ~4% expected move, placing the short put within one std dev of likely price range. Insufficient margin for error.
