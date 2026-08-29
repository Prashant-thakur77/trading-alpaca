# Calibration seeding run — SPY

- Window: **2026-06-01 .. 2026-08-07** (every 1 trading day(s))
- Attempted: **48** decision dates — **43** replayed, **5** skipped
- Committee abstained on **31** of 43 replayed windows (**72%** abstention rate)
- Windows resolved to a realized P&L: **12**
- LLM: **76** calls made, **68** served from the prompt cache, **$3.7417** spent

## Why the window starts where it does

The model's **knowledge cutoff is May 2026**, so no window may begin before **2026-06-01**. Replaying an LLM committee over dates inside its own training data is the knowledge-contamination criticism this project levels at TradingAgents (arXiv 2412.20138) and FinMem (arXiv 2311.13743); making it of ourselves would be worse. `seed_replay.validate_window` raises rather than clamps, and `seed_replay.decision_dates` drops any pre-cutoff day it is handed, so the only possible response to running out of post-cutoff calendar is FEWER windows, never earlier ones.

## Windows replayed

| as-of | expiry | spot | cands | choice | structure | resolution method | exit | realized P&L |
|---|---|---|---|---|---|---|---|---|
| 2026-06-01 | 2026-06-26 | 758.54 | 12 | c9 | bull_put_spread | leg_bars_forced_dte_exit | 2026-06-23 | $-246.00 |
| 2026-06-09 | 2026-07-10 | 737.05 | 12 | c9 | bull_put_spread | leg_bars_profit_target | 2026-06-15 | $76.00 |
| 2026-06-10 | 2026-07-10 | 725.43 | 11 | ABSTAIN | — | — | — | — |
| 2026-06-11 | 2026-07-10 | 737.76 | 12 | c9 | bull_put_spread | leg_bars_profit_target | 2026-06-15 | $76.00 |
| 2026-06-12 | 2026-07-10 | 741.75 | 12 | c10 | bull_put_spread | leg_bars_profit_target | 2026-06-15 | $86.00 |
| 2026-06-15 | 2026-07-10 | 754.83 | 12 | ABSTAIN | — | — | — | — |
| 2026-06-16 | 2026-07-17 | 750.33 | 12 | ABSTAIN | — | — | — | — |
| 2026-06-17 | 2026-07-17 | 740.96 | 12 | ABSTAIN | — | — | — | — |
| 2026-06-18 | 2026-07-17 | 746.74 | 12 | ABSTAIN | — | — | — | — |
| 2026-06-22 | 2026-07-17 | 744.39 | 12 | ABSTAIN | — | — | — | — |
| 2026-06-23 | 2026-07-24 | 733.58 | 12 | ABSTAIN | — | — | — | — |
| 2026-06-24 | 2026-07-24 | 733.24 | 12 | ABSTAIN | — | — | — | — |
| 2026-06-25 | 2026-07-24 | 734.30 | 11 | ABSTAIN | — | — | — | — |
| 2026-06-26 | 2026-07-24 | 728.99 | 10 | ABSTAIN | — | — | — | — |
| 2026-06-29 | 2026-07-24 | 741.00 | 12 | ABSTAIN | — | — | — | — |
| 2026-06-30 | 2026-07-31 | 746.77 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-01 | 2026-07-31 | 745.76 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-02 | 2026-07-31 | 744.78 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-06 | 2026-07-31 | 751.28 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-07 | 2026-08-07 | 747.71 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-08 | 2026-08-07 | 745.40 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-09 | 2026-08-07 | 751.71 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-10 | 2026-08-07 | 754.95 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-13 | 2026-08-07 | 749.17 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-14 | 2026-08-14 | 751.83 | 10 | c8 | bull_put_spread | leg_bars_profit_target | 2026-08-03 | $66.00 |
| 2026-07-15 | 2026-08-14 | 754.81 | 11 | c10 | bull_put_spread | leg_bars_profit_target | 2026-08-04 | $103.00 |
| 2026-07-16 | 2026-08-14 | 750.72 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-17 | 2026-08-14 | 743.29 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-20 | 2026-08-14 | 742.09 | 11 | c2 | bear_call_spread | leg_bars_profit_target | 2026-07-29 | $164.00 |
| 2026-07-21 | 2026-08-21 | 748.28 | 12 | c8 | bull_put_spread | leg_bars_profit_target | 2026-08-03 | $51.00 |
| 2026-07-22 | 2026-08-21 | 747.41 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-23 | 2026-08-21 | 738.18 | 12 | c10 | bull_put_spread | leg_bars_profit_target | 2026-08-03 | $79.00 |
| 2026-07-24 | 2026-08-21 | 738.93 | 12 | c9 | bull_put_spread | leg_bars_profit_target | 2026-08-03 | $79.00 |
| 2026-07-27 | 2026-08-21 | 739.09 | 12 | c8 | bull_put_spread | leg_bars_profit_target | 2026-08-03 | $62.00 |
| 2026-07-28 | 2026-08-28 | 740.86 | 10 | ABSTAIN | — | — | — | — |
| 2026-07-29 | 2026-08-28 | 729.46 | 10 | c3 | bear_call_spread | leg_bars_forced_dte_exit | 2026-08-25 | $-238.00 |
| 2026-07-30 | 2026-08-28 | 741.69 | 12 | ABSTAIN | — | — | — | — |
| 2026-07-31 | 2026-08-28 | 747.03 | 12 | ABSTAIN | — | — | — | — |
| 2026-08-03 | 2026-08-28 | 757.67 | 12 | ABSTAIN | — | — | — | — |
| 2026-08-04 | 2026-08-28 | 771.33 | 12 | ABSTAIN | — | — | — | — |
| 2026-08-05 | 2026-08-28 | 769.79 | 12 | ABSTAIN | — | — | — | — |
| 2026-08-06 | 2026-08-28 | 768.56 | 12 | ABSTAIN | — | — | — | — |
| 2026-08-07 | 2026-08-28 | 773.26 | 11 | ABSTAIN | — | — | — | — |

## Windows skipped

| as-of | reason |
|---|---|
| 2026-06-02 | only 0 of 26 legs had a real bar on 2026-06-02 (need 6) |
| 2026-06-03 | only 0 of 26 legs had a real bar on 2026-06-03 (need 6) |
| 2026-06-04 | only 0 of 26 legs had a real bar on 2026-06-04 (need 6) |
| 2026-06-05 | only 0 of 26 legs had a real bar on 2026-06-05 (need 6) |
| 2026-06-08 | only 0 of 26 legs had a real bar on 2026-06-08 (need 6) |

## Abstentions

- **2026-06-10** — c8 (bull_put_spread) vetoed — blind review: Bull put spread is directionally reasonable, but the sold 720p is dangerously tight (only 0.75% OTM) with inadequate compensation: 2.08 credit on a 5-wide spread creates a 140:1 risk-reward with breakeven just 1.04% below spot—minimal margin for error in a 30-DTE window.
- **2026-06-15** — trader abstained: Both analysts agree IV (13.05%) is below realized vol (16.16%), which favors buying volatility, but every candidate this cycle is a short-premium credit spread with no long-vol structure offered; forcing a sale of underpriced premium into a higher-realized-vol regime is exactly the negative edge the bear_adversary flagged, so the disciplined call is to sit out rather than pick the least-bad short spread.
- **2026-06-16** — trader abstained: Both analysts agree implied vol (13.75%) sits 2.36pp below realized vol (16.11%), meaning premium is underpriced for actual movement, yet every candidate this cycle is a short-premium credit spread with no long-premium alternative offered; forcing a sale into this regime works against the edge rather than with it.
- **2026-06-17** — trader abstained: Both analysts assign sub-50% probability and realized vol exceeds implied by 0.95pp, meaning every available candidate sells premium into a mechanically disadvantageous regime with thin breakeven cushions; no candidate offers enough edge to override that headwind.
- **2026-06-18** — trader abstained: vol_analyst and bear_adversary disagree on direction (bear_adversary expects SPY to breach the c1 breakeven, invalidating the very trade vol_analyst favors), and IV sitting 2.42pp below realized vol argues for buying premium, not selling any of the credit spreads on offer — no candidate matches the actual edge, so no trade.
- **2026-06-22** — trader abstained: Both analysts agree implied vol is below realized vol, favoring buying premium, but every available candidate is a short-vol premium-selling spread structurally opposed to that signal, so no candidate has a favorable edge this cycle.
- **2026-06-23** — trader abstained: Every available candidate is a short-premium credit spread, but implied vol sits below realized vol (16.61% vs 17.26%), meaning options are underpriced relative to actual movement — a setup that structurally favors buying volatility, not selling it. Both committee members flag this same misalignment (probabilities 0.28 and 0.42, both sub-50%), so no candidate on the list is well-supported this cycle.
- **2026-06-24** — trader abstained: All 10 candidates are credit spreads (selling premium), but IV is 89bps below realized vol, which both vol_analyst and bear_adversary flag as unfavorable for premium-selling; bear_adversary's 0.40 probability and vol_analyst's long-vol thesis point away from every available structure, so no candidate has committee conviction behind it.
- **2026-06-25** — trader abstained: IV is 0.96pp below realized vol, meaning options are underpriced relative to actual movement — a structural headwind for every candidate here since all nine are premium-selling credit spreads with no long-premium alternative offered. Both analysts flag this mismatch (vol_analyst at 0.42, bear_adversary at 0.32 citing gamma bleed outrunning theta over 29 DTE), so with no aligned bullish case for selling vol in a regime that favors buying it, the disciplined call is to sit out this cycle.
- **2026-06-26** — trader abstained: Implied vol sits 0.94pp below realized, a regime that structurally disadvantages every candidate here since all nine are short-premium credit spreads; the analysts disagree (vol_analyst 0.48, bear_adversary 0.25) rather than confirming a shared directional edge, so per the veto rule we sit out this cycle.
- **2026-06-29** — trader abstained: Both committee views agree implied vol sits below realized vol (14.76% vs 18.35%), which disfavors premium-selling structures, and every candidate here is a credit spread; the bear_adversary also flags that even the best-breakeven bull put (c11) sits only 2.6 points below spot against plausible ±4.8% realized-vol moves, so no listed candidate matches a sound thesis this cycle.
- **2026-06-30** — trader abstained: Both committee members flag that realized vol exceeds implied vol by 4.4pp, favoring long-premium structures, yet every available candidate is a short-premium credit spread; the bear_adversary further notes the most favored strikes (c1, c11) sit only 3-4 points OTM against an ~18.6% realized-vol regime, so no candidate is well-aligned with the analysts' shared view.
- **2026-07-01** — trader abstained: Every available candidate is a short-volatility structure (bear call or bull put credit spread), but both analysts agree implied vol is running 4.59pp below realized — the data favors buying premium, not selling it. With no long-vol structure on the candidate list and the bear_adversary explicitly dissenting from all short-premium trades, the committee lacks the required two-model directional agreement, so no trade.
- **2026-07-02** — trader abstained: Both analysts agree IV is cheap relative to realized vol (13.66% vs 18.40%), which favors buying premium, but every available candidate is a short-premium credit spread with breakevens uncomfortably close to spot given that vol regime; no candidate matches the favored structure and the committee doesn't converge on a short-premium trade, so ABSTAIN.
- **2026-07-06** — trader abstained: IV is 2.82pp below realized vol, arguing for buying premium, but every candidate is a credit spread (short premium); combined with vol_analyst's abstention and bear_adversary's low-confidence (0.22) view against the whole short-premium set, there's no coherent edge to act on this cycle.
- **2026-07-07** — trader abstained: IV sits 2.73pp below realized vol, meaning premium is cheap relative to actual movement, yet every available candidate is a credit spread that sells that cheap premium; with vol_analyst abstaining for lack of a long-vol structure and bear_adversary flagging the regime as backwards for selling, there's no directional or vol-regime consensus supporting any short-premium candidate this cycle.
- **2026-07-08** — trader abstained: Every candidate is a premium-selling structure, but IV sits 192bps below realized vol, which argues for buying premium instead — a mismatch neither analyst resolves, and bear_adversary flags thin breakeven cushion and OI risk even on the best-supported bear call. No candidate has committee support for the current vol regime, so no trade.
- **2026-07-09** — trader abstained: Both analysts agree implied vol (13.31%) sits below realized vol (14.94%), a regime that favors buying premium, yet every available candidate is a credit spread selling that same underpriced vol; with the closest breakeven (c1) only 0.79% from spot, the structural mismatch applies to all 11 candidates so no credit spread here has a favorable edge.
- **2026-07-10** — trader abstained: vol_analyst abstained and bear_adversary rates the lead candidate at only 30% probability, both flagging that IV sits below realized vol — a setup that structurally favors buying premium, not the short-vol credit spreads that are the only candidates this cycle; with no long-vol alternative and no two-model agreement on direction, the disciplined call is to abstain rather than force a mismatched trade.
- **2026-07-13** — trader abstained: vol_analyst's conviction is essentially a coin flip (0.48) and bear_adversary flags that realized vol exceeding implied structurally disadvantages every candidate here, since all are premium-selling credit spreads; with no directional edge and an unfavorable vol regime for this whole candidate set, standing aside is more defensible than forcing a trade.
- **2026-07-16** — trader abstained: The veto rule requires two independent model families to agree on direction before a trade is authorized; bear_adversary failed to render a view (CLI timeout) rather than concurring, so there is no confirmed directional consensus and the desk defaults to no-trade rather than acting on a single analyst's view.
- **2026-07-17** — c10 (bull_put_spread) vetoed — blind review: Short strike at 735 is only 1.11% cushion below spot with 28 DTE; realized vol of 11.96% implies ~4% expected move, placing the short put within one std dev of likely price range. Insufficient margin for error.
- **2026-07-22** — trader abstained: vol_analyst favors premium-selling (0.70) but bear_adversary rates the trade below coin-flip (0.47), flagging FOMC gap risk within the DTE window that the IV richness doesn't offset — this is a genuine direction/conviction disagreement between model families, so per the desk's veto rule the correct call is to abstain rather than force a trade.
- **2026-07-28** — trader abstained: bear_adversary's 0.50 probability is a coin-flip with no directional edge, and it specifically argues the thin breakeven cushion structurally negates the premium-selling thesis vol_analyst relies on — the two model families don't agree on direction, so per the veto rule this cycle abstains rather than forcing a trade.
- **2026-07-30** — trader abstained: The two analysts disagree on whether there's tradeable edge: vol_analyst's case rests on a razor-thin +1.51pp IV-RV premium that bear_adversary correctly notes gives almost no cushion, and the elevated 18% put skew cuts against the calm-vol premium-selling thesis rather than supporting it. With no consensus across the committee and breakevens for the tightest spreads only ~0.8% away in a 29-day window where 12.76% realized vol can easily travel, there's insufficient edge to override that disagreement.
- **2026-07-31** — c2 (bear_call_spread) vetoed — blind review: Realized vol of 12.62% implies ~3% moves likely in 28 days; a 1.37% cushion to breakeven is too tight relative to this volatility environment.
- **2026-08-03** — trader abstained: Both analysts flag that IV trading below realized vol disfavors premium-selling, yet all 11 candidates are credit spreads with no long-premium structure to align with the favored regime; absent committee agreement on a specific strike that adequately compensates for this headwind, sitting out is safer than forcing a trade.
- **2026-08-04** — trader abstained: Realized vol (14.80%) exceeds implied (13.91%) by 0.89pp, and every available candidate is a short-premium structure — there's no long-vol candidate to express the favored view, and both analysts flag that this vol regime erodes the edge on the credit spreads offered, especially the tighter c1 breakeven (777.51) only 24 days out.
- **2026-08-05** — trader abstained: Realized vol (14.64%) exceeds implied vol (13.01%) by 1.62pp, favoring long-volatility structures, but all 11 candidates are short-premium credit spreads; both vol_analyst and bear_adversary flag this regime mismatch, and forcing a credit spread here works against the actual edge.
- **2026-08-06** — trader abstained: Both committee members flag that implied vol sits below realized vol, which favors buying premium, not selling it — yet every available candidate (c1-c11) is a short-premium credit spread, so none matches the favored regime; with vol_analyst abstaining outright and bear_adversary warning realized moves could breach the tightest breakevens, there's no defensible pick among the offered structures.
- **2026-08-07** — trader abstained: IV is running 2.37pp below realized vol, which favors buying premium, but every available candidate is a short-vol credit spread; both the vol_analyst and bear_adversary flag that selling cheap vol here means breakevens are well inside the realized-vol move, so no candidate has a favorable risk/reward this cycle.
