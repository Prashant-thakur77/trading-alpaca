HOW TO VERIFY IT (no credentials needed)
- Judge desk: https://trading-alpaca-judge.vercel.app/judge replays four real decisions, two of them refusals, through every stage: snapshot, committee, veto, guard, execution.
- Clone the repo, then: make test (933 tests, offline), make judge (replays the four scenarios), make verify-journal (proves the hash chain is intact).
- Every LLM call is content-addressed (sha256 of model + prompt) and cached with its raw response, so any past decision replays exactly at $0.00.

WHAT THE JOURNAL MADE POSSIBLE
Four bugs were found during live sessions while the test suite was green: a host plugin silently telling analysts to look for skills instead of answering; a mid-priced limit that rested unfilled for 55 minutes and blocked every later cycle; a trailing comma that discarded a valid, reasoned trade; and a new spread whose leg reused a contract already held. Each was root-caused from the journal and prompt cache, fixed with a regression test, and shipped mid-session.

COMPLIANCE
New dedicated $100k paper account, created 2026-08-29, flat until the official window opened. Orders route through the official Alpaca CLI. Market data reads through the official Alpaca MCP server with read-only toolsets, so no LLM can place an order through it. alpaca-py is used for analysis. Every commit postdates the hackathon opening; the inherited crypto codebase it started from is fully disposed of in docs/AUDIT.md.

STATED LIMITS
One closed live trade proves nothing statistically. Paper fills are simulated against live quotes. The desk used about 25% of its permitted risk budget (candidates are built at one contract; the guard only downsizes). All three live trades were bear call spreads, one view expressed three times, and SPY rallied. We report the flat result rather than dress it up.

ASSETS
Demo video (4:22), a 15-slide presentation (PDF and PPTX), and a walk-forward backtest whose losing symbol (AAPL, PF 0.83) is reported alongside the winners.
