# Compliance evidence — Alpaca Trading API + CLI

Hackathon rule: entries must use Alpaca's Trading API **plus** the MCP server
or the CLI.

## Official CLI — installed and verified 2026-08-29

Binary: `alpaca` v0.0.14 (`alpacahq/cli`, official Linux amd64 release),
installed to `~/.local/bin/alpaca`.

Authenticates non-interactively from `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`
environment variables — no browser OAuth needed, so it works under cron.
Verified against paper account `PA3JR0GVVEN0`:

    $ alpaca account get --quiet
    { "account_number": "PA3JR0GVVEN0", "equity": "100000",
      "options_approved_level": 3, "buying_power": "400000", ... }

## Order route

Orders are submitted through the CLI's raw-API passthrough, which accepts the
JSON body on stdin:

    echo '{...mleg payload...}' | alpaca api POST /v2/orders

This is the path `alpaca_cli.py` implements. `alpaca-py` is retained for
analysis and backtesting only.

## MCP server

`.mcp.json` declares the official `alpaca-mcp-server` restricted to
`account,options-data,stock-data` toolsets — read-only. No trading toolset is
exposed, so an LLM cannot place an order through MCP even in principle.

## Account provenance

New dedicated paper account, created 2026-08-29, opening equity exactly
$100,000.00, options level 3 (required for spreads; level 2 cannot trade them),
zero pre-existing positions. Asserted on every run by `make check-account`.

## MCP server — verified 2026-08-29

`uvx` and `uv` are installed (`~/.local/bin/uvx`, `~/.local/bin/uv`).
Ran the server's help directly against PyPI (no local checkout involved):

    $ ALPACA_PAPER_TRADE=true uvx alpaca-mcp-server --help
    Usage: alpaca-mcp-server [OPTIONS]

      Alpaca MCP Server — Trading API integration for Model Context Protocol.

    Options:
      --version                       Show the version and exit.
      --transport [stdio|streamable-http|sse]
                                      Transport protocol (default: stdio)
      --host TEXT                     Host to bind (HTTP transport only)
      --port INTEGER                  Port to bind (HTTP transport only; defaults
                                      to $PORT or 8000)
      --env-file PATH                 Load environment variables from this file
                                      before starting
      --help                          Show this message and exit.

    $ uvx alpaca-mcp-server --version
    alpaca-mcp-server, version 2.3.0

Toolset membership was confirmed by reading the installed package's
`toolsets.py` (via `uv`'s cache, resolved from the same PyPI release): the
`account` toolset covers only `getAccount`/`getAccountConfig`/portfolio
history/activities; `stock-data` and `options-data` cover only market-data
reads. Order placement and position management live exclusively in the
separate `trading` toolset, which `.mcp.json`'s `ALPACA_TOOLSETS` value
(`account,options-data,stock-data`) does not include — so an MCP client
configured from this repo's `.mcp.json` has no tool capable of submitting,
modifying, or cancelling an order.
