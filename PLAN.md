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
- [ ] committee/: vol_analyst, news_analyst, bull-vs-bear debate (2 rounds),
      trader vote → one TradeIntent or ABSTAIN, reasoning captured
      (build on ai_backends.py / agent_signals.py dual-model machinery)
- [ ] veto: two different model families must agree on direction, else ABSTAIN;
      rule-based fallback on API failure
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
- [ ] Deterministic replay mode for any past day (offline)
- [ ] dashboard: Live Desk (positions, Greeks, P&L, decision feed) + Counters
      (seen / guard-denied / vetoed / abstained / executed)

## Phase 5 — Wed Sep 2: judge experience + hardening
- [ ] /judge page: credential-free, 4 one-click replay scenarios (bull, bear,
      veto-disagreement, guard-denial)
- [ ] Deploy dashboard publicly; Docker updated; CI green; 25+ tests
- [ ] README: 4 rubric sections + architecture diagram + quickstart + judge link
- [ ] Live session #2

## Phase 6 — Thu Sep 3: demo assets
- [ ] Live session #3 for footage; record + edit 2–4 min video (honest
      live-vs-backtest numbers side by side); presentation PDF with real text

## Phase 7 — Fri Sep 4: ship
- [ ] Flatten all positions; journal verify; freeze
- [ ] Submit on lablab by 12:00 UTC (5:30 PM IST): repo, video, PDF, demo URL
- [ ] Build-in-public post tagging Alpaca + lablab
