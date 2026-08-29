#!/usr/bin/env python3
"""
Verify the Alpaca paper account is correctly set up for this agent.

Run this after creating the account and filling in .env. It checks every
prerequisite on the critical path and tells you exactly what to fix:

    python3 scripts/check_account.py        (or: make check-account)

Exit 0 = ready to trade. Exit 1 = something needs fixing (it says what).
Touches nothing and places no orders.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so this works without python-dotenv installed."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _ok(msg: str) -> None:
    print(f"  \033[32mPASS\033[0m  {msg}")


def _fail(msg: str, fix: str) -> None:
    print(f"  \033[31mFAIL\033[0m  {msg}")
    print(f"        → {fix}")


def _warn(msg: str) -> None:
    print(f"  \033[33mWARN\033[0m  {msg}")


def main() -> int:
    _load_dotenv(REPO / ".env")
    print("\n  Alpaca paper account preflight\n" + "  " + "─" * 52)

    failures = 0

    # ── 1. credentials present ───────────────────────────────
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        _fail("ALPACA_API_KEY / ALPACA_SECRET_KEY not set",
              "cp .env.example .env and fill in your PAPER account keys")
        print()
        return 1
    _ok(f"Credentials present (key ...{key[-4:]})")

    # ── 2. reach the account ─────────────────────────────────
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(key, secret, paper=True)
        acct = client.get_account()
    except Exception as e:
        _fail(f"Could not fetch the account: {e}",
              "Check the keys are PAPER keys (paper and live keys differ)")
        print()
        return 1
    _ok(f"Reached account {acct.account_number}")

    # ── 3. gather the facts the guard needs ──────────────────
    equity = float(acct.equity or 0)
    level = getattr(acct, "options_trading_level", None)
    level = int(level) if level is not None else -1
    approved = getattr(acct, "options_approved_level", None)
    try:
        open_positions = len(client.get_all_positions())
    except Exception:
        open_positions = 0

    print(f"        equity=${equity:,.2f}  options_level={level}"
          f"  approved_level={approved}  positions={open_positions}")

    if getattr(acct, "trading_blocked", False):
        _fail("Account has trading_blocked=True", "Resolve this in the Alpaca dashboard")
        failures += 1
    if getattr(acct, "account_blocked", False):
        _fail("Account has account_blocked=True", "Resolve this in the Alpaca dashboard")
        failures += 1

    # ── 4. run the real guard, not a reimplementation ────────
    from risk_guard import RiskGuard, load_risk_config
    guard = RiskGuard(load_risk_config())
    ready, err = guard.startup_checks(
        is_paper=True, options_level=level,
        open_positions=open_positions, equity=equity,
    )
    if ready:
        _ok("RiskGuard startup checks passed")
    else:
        fix = {
            "level": "Enable options LEVEL 3 for this paper account — level 2 "
                     "cannot trade spreads at all",
            "equity": "Create a NEW dedicated paper account; the rules require a "
                      "fresh $100,000 balance",
            "position": "Close existing positions, or use a fresh paper account",
            "paper": "These must be PAPER keys, never live",
        }
        hint = next((v for k, v in fix.items() if k in err.lower()),
                    "See risk.yaml for the limit that failed")
        _fail(err, hint)
        failures += 1

    # ── 5. compliance path: official CLI present ─────────────
    from shutil import which
    if which("alpaca"):
        _ok("Alpaca CLI found on PATH (required for the compliance path)")
    else:
        _warn("Alpaca CLI not on PATH — needed for the order path. "
              "Install: brew install alpacahq/tap/cli")

    # ── 6. market status, for context only ───────────────────
    try:
        clock = client.get_clock()
        state = "OPEN" if clock.is_open else "CLOSED"
        _ok(f"Market is {state} (next open {clock.next_open:%Y-%m-%d %H:%M %Z})")
    except Exception:
        _warn("Could not read market clock")

    print("  " + "─" * 52)
    if failures:
        print(f"  {failures} blocking issue(s). Fix the FAIL lines above.\n")
        return 1
    print("  Ready to trade.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
