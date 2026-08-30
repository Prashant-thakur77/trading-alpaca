# PLAN.md — build plan (check off as completed)

## Phase 1 — Sat Aug 29: strip on-chain + dead code, tests green
- [x] Delete erc8004.py, erc8004_abi.py, hackathon_abi.py, calc_reputation.py,
      tests/test_merkle_reputation.py
- [x] Remove every erc8004/chain import & code path from agent.py, agent_state.py,
      config.py, tests/test_core.py — delete the code paths, no shipped no-op stubs
- [x] Audit merkle.py + validation_writer.py: keep only if free of chain deps
      (repurpose later for journal); otherwise delete with their tests
      → both stdlib-only, KEPT (see docs/AUDIT.md)
- [x] In tests/, drop only cases testing removed chain code; keep all
      strategy/risk/exit tests
- [x] Update .env.example: remove Kraken/chain vars, add Alpaca paper-key vars
      per alpaca-mcp-server README
- [x] `python3 -m pytest tests/ -v` fully green (74 passed), `make test` passes
- [x] Write docs/AUDIT.md: deleted / kept / where walk-forward, risk layers,
      dual-model code, Kraken execution live
- [x] Commit "phase 1: strip on-chain + dead code, tests green"

## Phase 2 — Sun Aug 30: Alpaca + options engine + guard + journal (markets closed)
REORDERED 2026-08-29 into dependency order, zero-credential modules first, so
work is never blocked on Alpaca paper keys arriving. Rationale in docs/AUDIT.md.
- [x] journal.py: hash-chained JSONL + scripts/verify_journal.py (no deps)
- [x] analytics.py: realized vol from bars, IV + Greeks via vollib
      (import `vollib`, not deprecated `py_vollib` alias; py_vollib is the
      pip install name)
- [x] candidate_builder.py: TradeIntent dataclass; builds bull put credit spread,
      bear call credit spread, iron condor, long straddle — fully specified
- [x] risk.yaml: single source of truth for every limit in CLAUDE.md
- [x] RiskGuard (new risk_guard.py, not an edit to risk_manager.py): risk.yaml +
      ALLOW/DENY/ALLOW_WITH_DOWNSIZE verdicts, downsizing math, startup checks,
      kill switch. Kept separate because the guard is *stateless* — one
      candidate vs one snapshot — which is what makes it exhaustively testable.
      risk_manager.py keeps the running session P&L/streak state and feeds it in.
- [x] alpaca_data.py: stock bars + option-chain fetch via alpaca-py, 15-min cache
      → NOTE: option-chain snapshots carry no open interest; it comes from the
      trading API's contract records, so the adapter merges both sources.
      → kraken_data.py NOT yet retired: agent.py + executor.py still depend on
      it and there is no Alpaca execution layer until Phase 3. Deleting it now
      would break the build for no gain. Moved to Phase 3.
- [x] Real walk-forward engine (walkforward.py + scripts/run_walkforward.py)
      over SPY/QQQ/AAPL/MSFT daily bars
      → CORRECTION: validate.py was never a walk-forward engine. It was a
      reporter printing hardcoded 82.2% OOS / 3.79 PF / 366-trade claims
      inherited from the crypto agent. Those were removed, not adapted.
- [x] Unit tests: builder 39, guard 34, journal 12, analytics 17, data 16,
      walkforward 19 → suite 211 passing
- [x] Commit per module

### Phase 2 leftovers carried into Phase 3
- [x] Retire kraken_data.py / kraken_cli.py when executor.py is ported to Alpaca
      → 2026-08-30: removed agent.py, agent_state.py, executor.py, kraken_cli.py,
        kraken_data.py + tests/test_integration.py. An import-closure check
        proved all five unreachable from every shipping entry point
        (run_session, replay, seed_calibration, build_site, validate, merkle).
        `make status/scan/dry-run/run/reset` had been routing judges straight
        into a `FileNotFoundError: 'kraken'` traceback.
- [ ] Run `make walkforward` against live Alpaca data once paper keys exist
      (engine + tests are done; only the real-data run is outstanding)

Decision: alpaca-py direct for the Phase 2 data/execution layer (unit-testable,
no server process). alpaca-mcp-server is the Phase 3 agent tool layer.

## Phase 3 — Mon Aug 31: committee + first live session
- [x] committee/: vol_analyst, bear_adversary, trader vote → one TradeIntent
      or ABSTAIN, reasoning captured (committee/analysts.py, trader.py,
      decide.py)
      → DEVIATION: no news_analyst and no 2-round debate. Sentiment was
        dropped deliberately: with a May 2026 knowledge cutoff and live Aug
        2026 markets, an LLM "news" view is recalled training data, not news
        (contamination, arXiv 2412.20138). The adversary supplies the
        opposing case the debate was meant to produce.
      → NOT built on ai_backends.py / agent_signals.py. That machinery was
        superseded by a fresh llm.py; the old modules are now orphaned and
        their disposition is an open question (see Phase 8).
- [x] veto: two different model families must agree, else ABSTAIN; rule-based
      fallback on API failure (committee/veto.py: thesis_check + blind_review)
- [x] executor.py → atomic multi-leg order via the Alpaca CLI; monitor exits:
      profit target 50% of credit, max-loss, DTE ≤ 3 (exit_monitor.py, wired
      into run_session; unwinds with a new inverted mleg order because there
      is no atomic multi-leg close)
- [x] committee/premortem.py: LLM failure modes compiled into deterministic,
      validated ExitTriggers; forced 3-DTE exit; deterministic fallback
- [x] journal a `close` entry with realized_pnl + snapshot_hash — closes the
      calibration loop and makes consecutive_losses live
- [ ] scheduler script using Alpaca CLI, one cycle / 30 min in market hours
- [ ] LIVE SESSION 7:00 PM–1:30 AM IST — real fills, screen-record everything

## Phase 4 — Tue Sep 1: self-grading + dashboard core
- [x] Per-analyst Brier calibration; demotion weights recomputed from the
      journal every cycle and passed into committee/decide.py's aggregate()
- [x] Deterministic replay mode for any past day (offline)
      → scripts/seed_calibration.py + seed_replay.py replay the real committee
        over any historical window; scripts/replay.py replays the committed
        judge scenarios credential-free. 43 windows replayed to date.
- [x] dashboard: decision feed + funnel counters (seen / guard-denied /
      vetoed / abstained / executed) on site/index.html and site/judge/
      → PARTIAL: no live positions/Greeks/P&L panel. There are no live
        positions to show until the first session fills.

## Phase 5 — Wed Sep 2: judge experience + hardening
- [x] /judge page: credential-free, 4 one-click replay scenarios (allow, bear,
      veto-disagreement, guard-denial) — site/judge/, `make judge` verifies
      all 4 at as_of, +3d and +30d
- [x] Deploy dashboard publicly (trading-alpaca-judge.vercel.app); Docker
      present; CI green (.github/workflows/test.yml); 938 tests vs a 25 target
- [x] README: 4 rubric sections + architecture diagram + quickstart + judge link
- [ ] Live session #2

## Phase 6 — Thu Sep 3: demo assets
- [ ] Live session #3 for footage; record + edit 2–4 min video (honest
      live-vs-backtest numbers side by side); presentation PDF with real text

## Phase 7 — Fri Sep 4: ship
- [ ] Flatten all positions; journal verify; freeze
- [ ] Submit on lablab by 12:00 UTC (5:30 PM IST): repo, video, PDF, demo URL
- [ ] Build-in-public post tagging Alpaca + lablab

## Open question — 9 orphaned inherited modules
An import-closure walk over every shipping entry point on 2026-08-30 found
nine top-level modules reachable from none of them, importing only each other
and tests/test_core.py:

    agent_signals  ai_backends  ai_prompts  chart_analyzer  config
    indicators     opus_analyst  risk_manager  strategies

CLAUDE.md lists all nine under KEEP+EXTEND, on the Phase-0 expectation that
the committee would be built on the inherited dual-model machinery. It was
not: committee/ runs on a fresh llm.py, and risk_guard.py replaced
risk_manager.py's layered checks. So the KEEP note describes a plan that
reality overtook.

Leaving them costs a crypto TA engine sitting in an options agent's repo, and
38 of the tests exercise it rather than the product. Removing them contradicts
an explicit CLAUDE.md instruction and drops the suite 938 → 900.

DECIDED 2026-08-30: remove them. The user approved after the trade was put to
them explicitly. The deletions are blocked by a tooling permission rule, not by
any remaining question, so this is execution pending, not an open decision.

    git rm agent_signals.py ai_backends.py ai_prompts.py chart_analyzer.py \
           config.py indicators.py opus_analyst.py risk_manager.py \
           strategies.py tests/test_core.py

Afterwards: rerun `make test` (expect 900), rebuild the deck and one-pager so
their test count is not stale, and confirm the make chain is still green.

## Carried
- [ ] Run `make walkforward` against live Alpaca data (engine + tests done)
- [ ] Walk-forward with and without richness scoring; report the delta in the
      README even if flat or negative (blocked on the richness tie-breaker,
      which is deliberately still default-off)
- [ ] scheduler script, one cycle / 30 min in market hours
