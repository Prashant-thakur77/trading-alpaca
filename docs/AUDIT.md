# Phase 1 Audit — strip on-chain + dead code

Date: 2026-08-29. Scope: remove all ERC-8004/EIP-712/on-chain code from the
inherited Kraken crypto agent, per CLAUDE.md hard rule 7. No Kraken/crypto code
was touched beyond chain call sites — that replacement is Phase 2.

## Baseline finding

Before any Phase 1 change, the test suite already failed at collection:
`erc8004.py` imported `erc8004_card` / `erc8004_chain`, and `agent_state.py`
imported `hackathon_chain` — three modules that were never carried over from the
base repo. The chain integration was dead on arrival; deleting it (rather than
repairing it) is what made the suite runnable again.

## Deleted

Files (git rm):

| File | What it was |
|---|---|
| `erc8004.py` | ERC-8004 facade (agent card + Sepolia registration CLI) |
| `erc8004_abi.py` | ABI fragments for Identity/Reputation contracts |
| `hackathon_abi.py` | Hackathon vault/RiskRouter ABIs + EIP-712 domain/types |
| `calc_reputation.py` | On-chain reputation score calculator |
| `tests/test_merkle_reputation.py` | Tests for calc_reputation + merkle integration (merkle-only tests will be rebuilt with the Phase 2 journal) |

Code paths stripped (no no-op stubs left behind):

- `agent_state.py` — `erc8004` / `hackathon_chain` imports; `sync_agent_card`,
  `post_reputation`, `post_onchain_intent`, `post_onchain_checkpoint`; the
  agent-card sync call inside `save_state`. Now pure save/load state management.
- `agent.py` — chain imports, the `_post_onchain_intent` /
  `_post_onchain_checkpoint` / `_post_reputation` wrappers, and all seven call
  sites in `scan_and_trade` / `monitor_positions` (open/close intents, scan and
  monitor checkpoints, reputation posts).
- `config.py` — entire ERC-8004/Sepolia section: `HACKATHON_*` contract
  addresses, `ERC8004_*` contracts, `SEPOLIA_RPC`, `SEPOLIA_CHAIN_ID`, plus
  `AGENT_NAME`/`AGENT_DESCRIPTION` (only consumed by the deleted card module).
- `tests/test_core.py` — erc8004 imports, `TestERC8004` (3 agent-card tests),
  `TestERC8004ModuleSplit` (3 facade/ABI tests). All strategy/risk/exit tests kept.
- `Makefile` — `card`, `register`, `show`, `reputation` targets, and the dead
  `service-install` target (referenced a `hackathon-agent.service` file that
  doesn't exist in this repo).
- `requirements.txt` / `pyproject.toml` — `web3==6.20.1`.
- `.env.example` — Sepolia private key/wallet/RPC vars, replaced with Alpaca
  paper-trading vars (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`,
  `ALPACA_PAPER_TRADE`) per the official alpaca-mcp-server README, plus the
  kill-switch env documented in CLAUDE.md.

## Audited and kept

- `merkle.py` — **KEEP.** Pure stdlib (`hashlib`, `json`, `pathlib`); zero chain
  deps. SHA-256 sorted-pair Merkle tree over validation artifacts. Reused as-is
  by `validate.py` and `make verify`; hashing utilities will back the
  hash-chained journal in Phase 2.
- `validation_writer.py` — **KEEP.** Pure stdlib (`fcntl`, `json`); zero chain
  deps. fcntl-locked atomic appends to `validation/*.json`, used by
  `agent_signals.py`. The append-one-record-per-decision pattern is the seed of
  Phase 2's `journal.py`.

## Where the keep-and-extend machinery lives

- **Walk-forward engine** — `validate.py` (windows/IS-OOS reporting; Phase 2
  points it at SPY/QQQ daily bars). `make validate` / `make validate-json`.
- **Risk layers** — `risk_manager.py` (position caps, daily-loss stop, drawdown
  emergency, consecutive-loss scaling/pause, per-pair cooldowns) with limits in
  `config.py:RiskConfig`. Phase 2 extends this into RiskGuard with `risk.yaml`
  verdicts.
- **Dual-model machinery** — `ai_backends.py` (model backends), `ai_prompts.py`,
  `agent_signals.py` (AI review + validation artifact writing),
  `opus_analyst.py` (analyst + position review), `chart_analyzer.py`. Becomes
  the analyst committee + dual-model veto in Phase 3.
- **Kraken execution (Phase 2 replacement target, untouched)** —
  `kraken_cli.py`, `kraken_data.py` (data), `executor.py` (order execution +
  SL/TP state machine — the state machine logic survives, the Kraken transport
  goes). `strategies.py` crypto TA is replaced by the options candidate builder.

## Test status after Phase 1

`python3 -m pytest tests/ -v` → **74 passed, 0 failed** (was: collection error).
`make test`, `make validate`, `make verify` all run green.
`agent.py`, `agent_state.py`, `config.py`, `validate.py` all import cleanly.

---

# Phase 2 audit — Alpaca + options engine + guard + journal

Date: 2026-08-29. Two inherited premises turned out to be wrong; both are
recorded here because they change what later phases can assume.

## Correction 1: validate.py was not a walk-forward engine

CLAUDE.md listed `validate.py` under KEEP+EXTEND as "walk-forward engine". It
is not one. It is a report generator, and its performance section consisted of
hardcoded string literals inherited from the crypto agent:

    "oosWinRate": "82.2%"       "expectancy": "+0.61R per trade (OOS)"
    "profitFactor": 3.79        "tradeCount": 366
    "maxOosDrawdown": "-8.7%"   "wfoWindows": 8

Not one was computed from data. `make validate` printed them under a "JUDGING
CRITERIA ALIGNMENT" heading, i.e. presented fabricated results to judges.

Removed rather than adapted. A real engine now exists:
- `walkforward.py` — rolling anchored IS/OOS windows. OOS strictly follows IS
  with no overlap, so no bar is ever both trained on and scored. Win rate,
  expectancy (R), profit factor and max drawdown are computed from the trades
  actually produced; a strategy that raises contributes zero trades instead of
  a fabricated result.
- `scripts/run_walkforward.py` / `make walkforward` — real SPY/QQQ/AAPL/MSFT
  daily bars in, `validation/walkforward.json` out.
- `validate.py` reads that file and reports absence honestly when it is missing.

## Correction 2: option chains carry no open interest

risk.yaml gates on open interest >= 100, but Alpaca's `get_option_chain`
snapshots expose only quote, IV and Greeks. Open interest lives on the trading
API's `get_option_contracts` records. `alpaca_data.get_option_chain` therefore
merges both sources and drops any contract whose OI cannot be established;
unknown OI becomes 0, which fails the liquidity gate. Fail closed, not blind.

## Deviation: RiskGuard is a new module, not an edit to risk_manager.py

The plan said "extend risk_manager.py". Instead `risk_guard.py` is new and
separate, because the guard is stateless — it judges one candidate against one
portfolio snapshot — and that is precisely what makes it exhaustively testable
(34 tests, mutation-checked). `risk_manager.py` retains the running session
state (P&L, streaks, cooldowns) and supplies the snapshot.

## Not done, and why

`kraken_data.py` / `kraken_cli.py` are still present. `agent.py` and
`executor.py` depend on them and there is no Alpaca execution layer until
Phase 3, so deleting them now would break the build and 21 passing integration
tests for no benefit. Retirement moved to Phase 3, where executor.py is ported.

## Phase 2 module map

| Module | Purpose | Tests |
|---|---|---|
| `journal.py` | hash-chained append-only decision log (rule 5) | 12 |
| `analytics.py` | realized vol, IV, per-share Greeks | 17 |
| `candidate_builder.py` | deterministic TradeIntent construction (rules 1, 3) | 39 |
| `risk_guard.py` + `risk.yaml` | ALLOW/DENY/DOWNSIZE gate (rules 2, 6) | 34 |
| `alpaca_data.py` | bars + chains, 15-min cache, OI merge | 16 |
| `walkforward.py` | honest OOS validation | 19 |

Suite total after Phase 2: **211 passing**, no warnings.
