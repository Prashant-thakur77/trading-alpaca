# REFERENCES.md — study repos (local clones in ~/ta-reference/)
When implementing a module, read the matching reference FIRST, extract the
pattern, then implement it fitted to our architecture in CLAUDE.md.

## Multi-agent LLM trading architecture (committee, debate, roles)
- ~/ta-reference/TradingAgents — https://github.com/TauricResearch/TradingAgents
  bull-vs-bear researcher debate, analyst roles, risk team, trader node,
  decision logs + reflection. Our committee/ mirrors this, leaner.
- ~/ta-reference/ai-hedge-fund — https://github.com/virattt/ai-hedge-fund
  pluggable analyst "alpha model" agents, risk manager → portfolio manager
  flow, backtester loop, FastAPI+React app structure for our dashboard.
- FinMem paper (layered memory + character design for a trading agent):
  arxiv.org/abs/2311.13743 — find its official GitHub via the paper; pattern
  for continual-learning memory if we add it.
- FinCon paper (manager–analyst multi-agent + verbal reinforcement, NeurIPS):
  useful for how a manager agent synthesizes analyst outputs.

## Execution layer: Alpaca + MCP wiring
- ~/ta-reference/alpaca-mcp-server — https://github.com/alpacahq/alpaca-mcp-server
  the tool surface we call: option chains, quotes, multi-leg orders, account.
- ~/ta-reference/alpaca-py — https://github.com/alpacahq/alpaca-py
  direct API for risk_guard/executor/monitor paths.
- ~/ta-reference/python-sdk — https://github.com/modelcontextprotocol/python-sdk
  MCP client: how our Python agent loop connects to alpaca-mcp-server (stdio).
- ~/ta-reference/langgraph — https://github.com/langchain-ai/langgraph
  graph orchestration if we rebuild the loop as a proper agent graph.

## Risk guards, intents, audit (the trust layer)
- ~/ta-reference/vertex-sentinel — https://github.com/TheVertexAgents/vertex-sentinel
  TradeIntent object; intent → validate → execute separation; audit.json.
- ~/ta-reference/Vartovii-Sentinel-8004 — https://github.com/Vetassikc/Vartovii-Sentinel-8004
  ALLOW/DENY/ALLOW_WITH_DOWNSIZE verdicts; credential-free /judge page with
  canned scenarios (copy this page's shape for our judge mode).
- ~/ta-reference/swiftward-ai-trading-agents — https://github.com/disciplinedware/swiftward-ai-trading-agents
  declarative YAML risk-rules engine + violation counters; debate sub-agents;
  embedded React dashboard. Take patterns only — do NOT copy its scope.
- ~/ta-reference/ai-tradingagent-kraken — https://github.com/jjliu2008/ai-tradingagent-kraken
  research layer vs execution layer split; backtest → promote-to-registry.

## Same-event competitors (benchmark: match their floor, beat their ceiling)
- ~/ta-reference/aegis-q — https://github.com/VicensPaneque/aegis-q
  deterministic pre-built spread candidates, bounded AI choice, atomic
  multi-leg orders, account verification, Docker+CI+20 tests.
- ~/ta-reference/babil-alpaca-hackathon-2026 — https://github.com/TAKA2SEA/babil-alpaca-hackathon-2026
  fail-closed execution, kill switch, human-approval gate.
- ~/ta-reference/SPY-Sentinel-AI — https://github.com/ajennings1974/SPY-Sentinel-AI
  refusal-to-trade gates, walk-forward + transaction-cost sensitivity checks.

## Options math + validation + infra patterns (read online as needed)
- https://github.com/vollib/py_vollib — Black-Scholes, IV, Greeks (we use it).
- https://github.com/kernc/backtesting.py — clean backtest API design.
- https://github.com/Open-Finance-Lab/FinRL_Contest_2025 — LLM-signal + RL
  trading starter kits (incl. FinRL-DeepSeek task).
- https://github.com/AI4Finance-Foundation/FinRL · /FinGPT · /FinRobot —
  RL trading envs, financial LLMs, agent platform.
- https://github.com/microsoft/RD-Agent · https://github.com/microsoft/qlib —
  automated factor/strategy research loops.
- https://github.com/nautechsystems/nautilus_trader — event-driven engine
  design, order lifecycle, risk hooks.
- https://github.com/freqtrade/freqtrade · https://github.com/hummingbot/hummingbot
  — production bot architecture: dry-run mode, config-driven strategies,
  protections/circuit breakers.
- https://github.com/OpenBB-finance/OpenBB — data-layer organization.
- https://github.com/anthropics/anthropic-cookbook — tool-use and agent
  prompting patterns for our committee prompts.
- IMC Prosperity winner writeups (strategy + validation discipline):
  https://github.com/TimoDiehm/imc-prosperity-3 ·
  https://github.com/chrispyroberts/imc-prosperity-3 ·
  https://github.com/CarterT27/imc-prosperity-3 ·
  https://github.com/jmerle/imc-prosperity-3-backtester ·
  https://github.com/jmerle/imc-prosperity-3-visualizer
- Multi-agent framework alternatives (only if LangGraph fights us):
  https://github.com/crewAIInc/crewAI · https://github.com/geekan/MetaGPT
