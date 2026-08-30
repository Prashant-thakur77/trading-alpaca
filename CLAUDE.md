# CLAUDE.md — Trading Alpaca

AI options-trading agent for the Alpaca AI Trading Agents Hackathon (lablab.ai).
Deadline: Fri Sep 4 2026, 15:00 UTC. Submit by 12:00 UTC. Track: Options Alpha Agents.
Repo: github.com/Prashant-thakur77/trading-alpaca. Converting a crypto/Kraken agent
into an Alpaca US-equities OPTIONS agent.

Read @PLAN.md for the phased task list. Work top-down, check off tasks as done.

## What we're building
"The options desk that grades itself": LLM analyst committee debates → dual-model
veto → deterministic RiskGuard → atomic multi-leg options orders on Alpaca paper →
hash-chained journal → walk-forward validation + Brier calibration that demotes
badly calibrated agents → public dashboard with a credential-free /judge page.

## Hard rules (never violate)
1. LLMs NEVER invent strikes, quantities, or order parameters. Deterministic code
   builds fully-specified TradeIntent candidates; LLMs only pick one or ABSTAIN.
2. Every order passes RiskGuard first → ALLOW / DENY / ALLOW_WITH_DOWNSIZE per
   risk.yaml. Fail-closed: any error, missing data, or guard exception = no trade.
3. Defined-risk structures only (credit/debit spreads, iron condor, long straddle).
   No naked short options. Paper account only, ever.
4. ABSTAIN is a first-class output. Never force a trade.
5. Every decision (proposal, veto, verdict, fill, exit) appends one JSONL entry to
   the journal with prev_hash = SHA-256 of previous entry. Never edit past entries.
6. Kill switch: if KILL_SWITCH file exists or env KILL=1, halt all trading.
7. All ERC-8004/EIP-712/on-chain and Kraken/crypto code gets removed, not adapted
   (exception: standalone hashing utilities may be reused for the journal).

## Inherited layout (audit in Phase 0/1, then follow this disposition)
CORRECTED 2026-08-29: validate.py is a REPORT GENERATOR, not a walk-forward
engine. Its "OOS 82.2% / PF 3.79 / 366 trades" figures were hardcoded crypto-era
literals and have been deleted. The real engine is walkforward.py (Phase 2).
Never reintroduce a performance number that isn't computed from real bars.

SUPERSEDED 2026-08-30: the KEEP+EXTEND list below was a Phase-0 prediction that
the options desk would be built by extending the inherited crypto modules. It
was not. The committee was written fresh on llm.py, and risk_guard.py replaced
risk_manager.py's layered checks. An import-closure walk over every shipping
entry point found the whole inherited cluster reachable from none of them, so
it was removed rather than kept. Final disposition:

REMOVED (crypto agent, unreachable from the product):
  agent.py, agent_state.py, executor.py, kraken_cli.py, kraken_data.py,
  validation_writer.py, tests/test_integration.py
REMOVED 2026-08-30 (inherited dual-model machinery and crypto TA, superseded
  by llm.py + committee/ and by risk_guard.py; suite 938 → 900):
  agent_signals.py, ai_backends.py, ai_prompts.py, chart_analyzer.py,
  config.py, indicators.py, opus_analyst.py, risk_manager.py, strategies.py,
  tests/test_core.py
REMOVED in Phase 1 (on-chain): erc8004.py, erc8004_abi.py, hackathon_abi.py,
  calc_reputation.py, tests/test_merkle_reputation.py.
KEPT: validate.py (rewritten for options: structures, committee outcomes,
  per-gate verdicts, candidate funnel), merkle.py (stdlib-only), Makefile,
  tests/.

The options desk is: alpaca_cli, alpaca_data, analytics, calibration,
candidate_builder, committee/, executor_options, exit_monitor, journal, llm,
merkle, options_orders, risk_guard, seed_replay, walkforward, validate.
Nothing else is reachable from an entry point. If a module is not on that
list, check the closure before assuming it runs.

## Risk limits (risk.yaml source of truth)
max_loss_per_position: $1,000 (1% of $100k) · max_positions: 3 ·
max_new_per_underlying_per_day: 1 · |net delta| ≤ 30 · |net vega| ≤ 200 ·
DTE 7–45 · option spread ≤ 10% of mid · min OI 100 · daily loss 2% = halt ·
3 consecutive losers = halve size.

## Stack & conventions
Python 3.10+. Execution: alpaca-py + official alpaca-mcp-server (agents' tool
layer) + Alpaca CLI in scheduler scripts. Greeks/IV: py_vollib. Secrets in .env
only (update .env.example for Alpaca paper keys), never committed.
Keep the Makefile chain green: make install && make test && make validate &&
make verify. Small single-purpose modules, type hints, tests for every new
module (target 25+ passing).

## Judging rubric (optimize for these)
Application of Technology · Presentation · Business Value · Originality.
README gets one section per axis + architecture diagram + judge-page link.
