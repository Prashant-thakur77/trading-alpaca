# Sharpening plan, from research into what earns credibility

Written 2026-08-29 after researching famous trading systems and the LLM-agent
trading literature. Ranked by impact per hour of effort.

## What the research actually established

**Renaissance / Medallion.** The famous 66% gross figure traces to Zuckerman's
2019 book, built from interviews. It is journalism, not audited primary source,
and no SEC filing or court document discloses Medallion's returns. Post-2005
figures are hearsay repeated by reporters. What keeps the legend durable is not
the number, it is that Zuckerman named his sources and stated plainly where the
audit trail stops.

**Jane Street, Two Sigma, Citadel Securities.** All three publish real technical
work: OxCaml and Hardcaml are open source, Two Sigma publishes regime-modelling
methodology, Citadel discloses market share. None of them ever publishes live
strategy P&L or Sharpe. Their transparency is scoped to engineering and
methodology, never to alpha, because engineering claims are verifiable and do
not leak edge.

**Quantopian.** Shut its free platform in 2020. The documented post-mortem is
that community backtests did not survive contact with live markets. This is
exactly the failure mode of the hardcoded "82.2% OOS win rate" this repo
deleted in Phase 1.

**The LLM-agent literature.** TradingAgents (arXiv 2412.20138) and FinMem
(arXiv 2311.13743) both report backtested outperformance, both are backtest
only with no live validation, both use short windows and small ticker
universes, and reviewers flag Sharpe ratios above the plausible empirical
range. The sharpest criticism is **knowledge contamination**: an LLM backtested
over dates inside its own training data may be recalling prices rather than
reasoning about them.

**Direct competitors.** The lablab listing shows "Dawn Of The Trading Agents"
running bull/bear/risk debate agents, and "LS101" using a vector-DB memory
layer. No judge commentary is published for past cohorts, so no scoring
inference is available. Debate alone is therefore **not** a differentiator.

## Ranked actions

### 1. State the knowledge-contamination position. High impact, ~30 minutes.
This is the single sharpest criticism in the LLM-trading literature and almost
nobody addresses it. Our position is unusually strong and currently unstated:
the model's knowledge cutoff is May 2026, and every live committee decision is
made on August 2026 market data. The prices, the chain and the volatility
regime post-date the training data, so a decision cannot be recall dressed up
as reasoning. Say so on the site, in the README, and in the video.
Also state the converse honestly: the walk-forward harness spans 720 days that
partly predate the cutoff, which is fine because that harness runs **no LLM at
all**, only a deterministic proxy. Naming both halves is what makes it credible.

### 2. Add a "what we cannot prove" section. High impact, ~30 minutes.
The Medallion lesson: credibility comes from naming where verification stops.
Ours stops in specific, nameable places, and saying so pre-empts the judge
thinking it for us:
  * paper trading only, so no fill-quality or slippage evidence
  * a handful of trades, so no statistical claim of edge
  * the calibration loop is wired and proven end to end in tests, but has few
    resolved live outcomes, so most analyst weights are still 1.0
  * the fail-closed scenario is constructed, since no live cycle has hit an outage

### 3. Reframe against the debate-agent competitors. High impact, ~1 hour.
A rival is already running bull/bear/risk debate. If our pitch is "multi-agent
debate" we are one of several. Our actual differentiator is the **constraint**:
the model may pick one candidate by id or abstain, and there is no code path by
which a strike, quantity or price can originate from model output. Lead every
surface with that, and demote the debate to a supporting detail.

### 4. Put live and backtest numbers side by side. Medium impact, ~2 hours.
The Quantopian failure was backtests that did not survive live markets. Showing
both, with the live sample honestly tiny, demonstrates we know the difference.
Belongs in the video and on the evidence section.

### 5. Cite the literature we are improving on. Medium impact, ~30 minutes.
Naming TradingAgents and FinMem, and stating precisely how this differs
(bounded action space, live post-cutoff data, verifiable journal), signals to a
technical judge that we know the field. Most hackathon entries cite nothing.

### 6. Make the engineering transparency prominent. Medium impact, ~1 hour.
The Jane Street pattern: publish the machinery, never a performance claim. We
already open the risk engine, the walk-forward code and the journal verifier.
Surface them as the headline evidence rather than a footnote.

## What risks being misread as weakness

* **"It abstained"** can read as "it does not work". Frame every refusal with
  the reason and the cost avoided, so it reads as judgement, not failure.
* **"Most weights are 1.0"** can read as "the calibration loop does nothing".
  Frame as: the mechanism is proven in tests and correctly dormant until
  outcomes resolve, because demoting on a tiny sample is the error we refuse
  to make.
* **"One symbol loses money"** can read as a bad backtest. Frame as: a harness
  that cannot produce a loser is broken, and an earlier version of ours was.
* **"Paper only"** can read as untested. Frame as a hard rule, not a limitation.
