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
- [ ] alpaca_data.py: stock bars + option-chain fetch (MCP tools + alpaca-py),
      15-min cache; retire kraken_data.py
- [ ] analytics: realized vol from bars, IV + Greeks via py_vollib
- [ ] candidate_builder.py: TradeIntent dataclass; builds bull put credit spread,
      bear call credit spread, iron condor, long straddle — fully specified
- [ ] Extend risk_manager.py: risk.yaml + ALLOW/DENY/ALLOW_WITH_DOWNSIZE verdicts,
      downsizing math, startup checks (fresh acct, options level, no positions),
      kill switch
- [ ] journal.py: hash-chained JSONL + scripts/verify_journal.py
- [ ] Point validate.py walk-forward at SPY/QQQ + 2 liquid names (daily bars)
- [ ] Unit tests: builder 5+, guard 8+, journal 3+
- [ ] Commit per module

## Phase 3 — Mon Aug 31: committee + first live session
- [ ] committee/: vol_analyst, news_analyst, bull-vs-bear debate (2 rounds),
      trader vote → one TradeIntent or ABSTAIN, reasoning captured
      (build on ai_backends.py / agent_signals.py dual-model machinery)
- [ ] veto: two different model families must agree on direction, else ABSTAIN;
      rule-based fallback on API failure
- [ ] executor.py → atomic multi-leg order via MCP; monitor exits: profit target
      50% of credit, max-loss, DTE ≤ 3, deadline flatten
- [ ] scheduler script using Alpaca CLI, one cycle / 30 min in market hours
- [ ] LIVE SESSION 7:00 PM–1:30 AM IST — real fills, screen-record everything

## Phase 4 — Tue Sep 1: self-grading + dashboard core
- [ ] Per-analyst Brier calibration over walk-forward windows; demotion weights
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
