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
