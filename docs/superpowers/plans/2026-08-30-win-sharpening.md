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

---

## URGENT: our headline collides with the strongest competitor

`aegis-q` leads with:

> **"The AI can choose. It cannot improvise."**

Ours currently reads:

> **"The model can refuse. It cannot invent."**

Same structure, same claim, same rhythm. A judge reading both back to back
will notice, and whoever they read second looks derivative. aegis-q is also
the best-packaged entry in the set: cover image, one-page PDF, six-slide deck
in PDF and PPTX, a video script, a live dashboard URL, and every backtest
number tied to `reports/metrics.json` computed from real Alpaca IEX bars, plus
an honest disclaimer that the legacy backtest is not evidence for the options
strategy actually submitted.

Worse, the *constraint itself* is not differentiating. aegis-q bounds its AI to
pick an ID or ABSTAIN with a deterministic fallback. babil restricts its model
to a five-field proposal schema that cannot set price, strike or quantity.
Three of us independently built the same guardrail.

### What is actually ours alone

Checked against every competitor repo on disk, no one else has:

1. **A calibration loop that demotes its own analysts.** Brier-scored against
   resolved outcomes, weights recomputed from the journal each cycle. Nobody
   else grades their agents at all.
2. **An adversary whose objections are recorded whether or not they win**, and
   which is documented changing a live decision from c1 to c4.
3. **A hash-chained journal a stranger can verify with no credentials**, plus
   four replayable scenarios that recompute their verdicts in the browser.
4. **Refusal presented as the product**, with two of four demo scenarios being
   refusals.

### Recommended headline change

Lead with the self-grading, which is unique, and demote the constraint to
supporting evidence. Candidate lines, in order of preference:

* **"The desk that grades itself."** Already the project's own subtitle,
  distinctive, and true only of us.
* **"Every analyst has a track record. The bad ones lose their vote."**
* **"It argues with itself before it trades."**

## Competitor evidence standards worth matching

* **aegis-q** ties every figure to a named file that reproduces it, and
  explicitly disclaims what the evidence does not cover. Match this exactly.
* **vertex-sentinel** puts a claimed award badge above the fold with no in-repo
  evidence for it. If a judge cannot verify a headline claim, the whole README
  becomes suspect. Never do this.
* **ai-tradingagent-kraken** cites `results/latest/fhe_vs_current_backtest.json`
  as its evidence file. That file does not exist in the repo; the numbers live
  in a differently-named file after a rename. Every path we cite must resolve.
* **babil** claims 476 tests; 476 collect but one fails without credentials
  set. Our claim must hold on a clean clone with no environment.

### Immediate check on our own claims

Verify that every number and file path on the site and in the README resolves
on a fresh clone with no `.env` present, since that is exactly how a judge will
encounter it.
