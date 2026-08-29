# Trading Alpaca

## Compliance

Orders route through the official **Alpaca CLI**, wrapped by `alpaca_cli.py`
(`alpaca api POST /v2/orders`) — this satisfies the hackathon rule that
entries use the Trading API plus the MCP server or the CLI.

Market data reads (account, options chains, stock bars) are available through
the official **MCP server**, declared in `.mcp.json` and restricted to the
`account,options-data,stock-data` toolsets. **No trading toolset is exposed
over MCP**, so the LLM cannot place an order through MCP even in principle —
only the deterministic `alpaca_cli.py` path can submit orders.

`alpaca-py` is used for analysis and backtesting, not for order placement.

See `docs/COMPLIANCE.md` for verified evidence (CLI account check, MCP server
`--help` output, account provenance).

## Kill switch

Hard rule 6: to halt all trading immediately, either create a file named
`KILL_SWITCH` in the repo root, or set the environment variable `KILL=1`.

It is checked at startup and again before every order, so a session already
running stops before its next submission. The `KILL_SWITCH` file resolves
relative to `risk.yaml`'s own directory, not the process's current working
directory — so it works the same way whether the session is launched from a
shell in the repo, or from cron/systemd with an unrelated working directory.
