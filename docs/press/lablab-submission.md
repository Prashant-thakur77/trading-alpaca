# lablab submission text

## Title
Trading Alpaca: an options desk that grades itself

## Short description
An AI options desk where deterministic code builds every defined-risk trade and the LLM may only pick one or refuse. Two vetoes, a fail-closed risk guard, a hash-chained journal, and Brier-scored analysts that lose their vote when miscalibrated.

## Long description
Most trading agents ask a language model what to buy and act on the answer. This desk inverts that: deterministic Python enumerates every legal, defined-risk options structure the live SPY chain supports (about 1,500 fully priced candidates per cycle), and the analyst committee may only pick one by id or ABSTAIN. There is no code path by which a model can invent a strike, change a quantity, or move a limit price.

Every pick then passes two independent vetoes with decorrelated failure modes: a pure-code thesis check (do the position Greeks match the structure's own directional claim?) and a blind LLM review starved of the committee's reasoning. RiskGuard then enforces risk.yaml as the single source of truth ($1,000 max loss per position, 3 positions, one new trade per underlying per day, delta/vega caps, DTE 7-45, liquidity gates, a 2% daily-loss halt) and fails closed on any error. A pre-mortem compiles each trade's failure modes into deterministic exit triggers, and the exit monitor manages the book before it adds to it.

Each analyst emits a probability, not a verdict. Every decision is appended to a hash-chained journal; closes resolve those predictions and Brier scores recompute analyst weights every cycle, so a confidently wrong analyst loses its vote. A credential-free judge page replays four real decisions, two of them refusals.

Live, on a new $100k paper account inside the official window: 3 fills (each at better than its limit), 1 automated close on its own breakeven trigger, 0 corrupted positions, closing equity $99,827.64 (-0.17%). Four bugs were found by running while the 933-test suite was green, each root-caused from the journal and fixed mid-session. We claim architecture and refusal discipline, not edge; the README says what cannot be proved.

Orders route through the official Alpaca CLI; market data through the official Alpaca MCP server (read-only toolsets); alpaca-py for analysis.

## Technologies used
Alpaca Trading API, Alpaca CLI, Alpaca MCP Server, alpaca-py, Python, Claude (Anthropic), py_vollib, pytest, Playwright, Chatterbox TTS, Vercel, Docker, GitHub Actions

## Track
Options Alpha Agents

## Links
- Repo: https://github.com/Prashant-thakur77/trading-alpaca
- Demo URL: https://trading-alpaca-judge.vercel.app
- Judge desk: https://trading-alpaca-judge.vercel.app/judge
- Video: docs/press/demo.mp4 (4:22)
- Deck: docs/press/trading-alpaca-deck.pdf
