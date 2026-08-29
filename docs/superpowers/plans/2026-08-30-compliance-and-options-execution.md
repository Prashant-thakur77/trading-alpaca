# Compliance & Options Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Satisfy the hackathon's "Alpaca Trading API + MCP server or CLI" requirement and place a real, filled, atomic multi-leg options order on a paper account.

**Architecture:** A thin subprocess adapter wraps the official Alpaca CLI (`alpaca_cli.py`). A pure function converts a `TradeIntent` into Alpaca's multi-leg (`mleg`) order payload (`options_orders.py`). An executor submits through the adapter, polls for fill, and monitors exits (`executor_options.py`). `alpaca-py` is retained for analysis only. A committed `.mcp.json` covers the MCP half of the requirement for read-only data.

**Tech Stack:** Python 3.10, pytest, alpaca-py 0.44.0, the official Alpaca CLI binary, PyYAML.

**Spec:** `docs/superpowers/specs/2026-08-29-agentic-options-desk-design.md`

## Global Constraints

- Paper account only, ever. Never construct a client with `paper=False`.
- Every order passes `RiskGuard.evaluate()` before submission; any error is DENY.
- Defined-risk structures only: `bull_put_spread`, `bear_call_spread`, `iron_condor`, `long_straddle`. No naked shorts.
- Alpaca multi-leg limits: **2–4 legs**, unique symbols, `time_in_force="day"` only, **no top-level `symbol`/`side`**, `order_class="mleg"`.
- `limit_price` sign convention: **positive = net debit paid, negative = net credit received.** Closing a debit spread therefore uses a negative limit price.
- Options **level 3** required (level 2 cannot trade spreads).
- Kill switch: `KILL_SWITCH` file or `KILL=1` halts everything.
- Every decision appends one `journal.py` entry. Never edit past entries.
- No network calls in the test suite. Subprocess and API clients are injected.
- Run `python3 -m pytest tests/ -q` before every commit; it must stay green (currently 216 passing).

---

### Task 1: Alpaca CLI subprocess adapter

Wraps the official `alpaca` binary. This is the compliance path for the order route.

**Files:**
- Create: `alpaca_cli.py`
- Test: `tests/test_alpaca_cli.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `AlpacaCLI(binary="alpaca", runner=subprocess.run)` with methods
  `run(args: list[str], payload: dict | None = None) -> dict`,
  `get_account() -> dict`, `list_positions() -> list[dict]`,
  `get_order(order_id: str) -> dict`, `post_order(payload: dict) -> dict`,
  and `available() -> bool`. Raises `AlpacaCLIError` on non-zero exit.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_alpaca_cli.py
import json, os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from alpaca_cli import AlpacaCLI, AlpacaCLIError


class FakeCompleted:
    def __init__(self, returncode=0, stdout="{}", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _runner(result, calls):
    def run(args, **kwargs):
        calls.append({"args": args, "input": kwargs.get("input"), "env": kwargs.get("env")})
        return result
    return run


def test_get_account_parses_json():
    calls = []
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout='{"equity":"100000"}'), calls))
    assert cli.get_account() == {"equity": "100000"}


def test_account_command_requests_json_and_quiet():
    calls = []
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout="{}"), calls))
    cli.get_account()
    assert calls[0]["args"][:3] == ["alpaca", "account", "get"]
    assert "--quiet" in calls[0]["args"]


def test_forces_paper_mode_in_env():
    """Never let an inherited env var flip this to live trading."""
    calls = []
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout="{}"), calls))
    cli.get_account()
    assert calls[0]["env"]["ALPACA_LIVE_TRADE"] == "false"


def test_post_order_sends_payload_as_stdin_json():
    calls = []
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout='{"id":"abc"}'), calls))
    payload = {"order_class": "mleg", "qty": "1"}
    assert cli.post_order(payload) == {"id": "abc"}
    assert calls[0]["args"][1:4] == ["api", "POST", "/v2/orders"]
    assert json.loads(calls[0]["input"]) == payload


def test_nonzero_exit_raises_with_stderr():
    cli = AlpacaCLI(runner=_runner(FakeCompleted(returncode=1, stdout="", stderr="boom"), []))
    with pytest.raises(AlpacaCLIError, match="boom"):
        cli.get_account()


def test_unparseable_output_raises():
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout="not json"), []))
    with pytest.raises(AlpacaCLIError, match="parse"):
        cli.get_account()


def test_list_positions_returns_list():
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout='[{"symbol":"SPY"}]'), []))
    assert cli.list_positions() == [{"symbol": "SPY"}]


def test_empty_output_is_an_empty_list_for_positions():
    cli = AlpacaCLI(runner=_runner(FakeCompleted(stdout=""), []))
    assert cli.list_positions() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_alpaca_cli.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'alpaca_cli'`

- [ ] **Step 3: Write minimal implementation**

```python
# alpaca_cli.py
"""
Subprocess adapter over the official Alpaca CLI.

This is the hackathon compliance path: the rules require the Trading API plus
the MCP server or the CLI, and orders are routed through the CLI binary here.
alpaca-py is retained for analysis, where the CLI is awkward.

Paper mode is forced in the environment on every call, so an inherited
ALPACA_LIVE_TRADE cannot flip this to live trading.
"""
import json
import logging
import os
import subprocess
from shutil import which

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


class AlpacaCLIError(RuntimeError):
    """The CLI exited non-zero, timed out, or emitted unparseable output."""


class AlpacaCLI:
    def __init__(self, binary: str = "alpaca", runner=subprocess.run,
                 timeout: int = DEFAULT_TIMEOUT):
        self.binary, self._runner, self.timeout = binary, runner, timeout

    def available(self) -> bool:
        return which(self.binary) is not None

    def _env(self) -> dict:
        env = dict(os.environ)
        env["ALPACA_LIVE_TRADE"] = "false"   # paper only, ever
        env["ALPACA_OUTPUT"] = "json"
        return env

    def run(self, args: list[str], payload: dict | None = None) -> dict | list:
        cmd = [self.binary, *args]
        try:
            result = self._runner(
                cmd, capture_output=True, text=True, timeout=self.timeout,
                env=self._env(),
                input=json.dumps(payload) if payload is not None else None,
            )
        except subprocess.TimeoutExpired as e:
            raise AlpacaCLIError(f"CLI timed out after {self.timeout}s: {' '.join(cmd)}") from e

        if result.returncode != 0:
            raise AlpacaCLIError(
                f"CLI failed ({result.returncode}): {' '.join(cmd)}\n{result.stderr.strip()}"
            )

        out = (result.stdout or "").strip()
        if not out:
            return {}
        try:
            return json.loads(out)
        except json.JSONDecodeError as e:
            raise AlpacaCLIError(f"Could not parse CLI output as JSON: {out[:200]}") from e

    def get_account(self) -> dict:
        return self.run(["account", "get", "--quiet"])

    def list_positions(self) -> list:
        out = self.run(["position", "list", "--quiet"])
        return out if isinstance(out, list) else []

    def get_order(self, order_id: str) -> dict:
        return self.run(["order", "get", "--order-id", order_id, "--quiet"])

    def post_order(self, payload: dict) -> dict:
        return self.run(["api", "POST", "/v2/orders"], payload=payload)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_alpaca_cli.py -q`
Expected: PASS, 8 tests

- [ ] **Step 5: Run the full suite and commit**

```bash
python3 -m pytest tests/ -q
git add alpaca_cli.py tests/test_alpaca_cli.py
git commit -m "feat: Alpaca CLI subprocess adapter (compliance order path)"
```

---

### Task 2: Multi-leg order payload builder

Pure function: `TradeIntent` → Alpaca `mleg` payload. No I/O, so it is exhaustively testable — and it is where the broker's sign and shape rules get encoded.

**Files:**
- Create: `options_orders.py`
- Test: `tests/test_options_orders.py`

**Interfaces:**
- Consumes: `candidate_builder.TradeIntent`, `Leg`, `OptionQuote` (already built).
- Produces: `build_mleg_payload(intent: TradeIntent, contracts: int, limit_price: float | None = None) -> dict`
  and `closing_payload(intent: TradeIntent, contracts: int, limit_price: float) -> dict`.
  Raises `ValueError` for leg-count or structure violations.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_options_orders.py
import os, sys
from datetime import date, timedelta
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from candidate_builder import OptionQuote, build_bull_put_spread, build_long_straddle
from options_orders import build_mleg_payload, closing_payload

EXPIRY = date.today() + timedelta(days=30)


def _q(strike, right, bid, ask, oi=500):
    return OptionQuote(f"SPY{EXPIRY:%y%m%d}{right.upper()}{int(strike*1000):08d}",
                       "SPY", strike, EXPIRY, right, bid, ask, oi)


def _credit_spread(contracts=1):
    return build_bull_put_spread(_q(445, "p", 3.00, 3.10), _q(440, "p", 2.00, 2.10),
                                 contracts=contracts)


def test_payload_uses_mleg_order_class():
    assert build_mleg_payload(_credit_spread(), 1)["order_class"] == "mleg"


def test_payload_has_no_top_level_symbol_or_side():
    """Alpaca rejects multi-leg orders that carry a top-level symbol or side."""
    payload = build_mleg_payload(_credit_spread(), 1)
    assert "symbol" not in payload
    assert "side" not in payload


def test_time_in_force_is_day():
    """Alpaca supports only day TIF for multi-leg options orders."""
    assert build_mleg_payload(_credit_spread(), 1)["time_in_force"] == "day"


def test_each_leg_carries_symbol_side_and_ratio():
    legs = build_mleg_payload(_credit_spread(), 1)["legs"]
    assert len(legs) == 2
    for leg in legs:
        assert leg["symbol"]
        assert leg["side"] in ("buy", "sell")
        assert leg["ratio_qty"] == "1"
        assert leg["position_intent"] in ("buy_to_open", "sell_to_open")


def test_credit_spread_uses_a_negative_limit_price():
    """Alpaca's convention: negative limit price = net credit received."""
    payload = build_mleg_payload(_credit_spread(), 1)
    assert float(payload["limit_price"]) < 0


def test_debit_structure_uses_a_positive_limit_price():
    straddle = build_long_straddle(_q(450, "c", 5.00, 5.10), _q(450, "p", 4.00, 4.10))
    assert float(build_mleg_payload(straddle, 1)["limit_price"]) > 0


def test_qty_is_the_strategy_multiplier():
    assert build_mleg_payload(_credit_spread(contracts=3), 3)["qty"] == "3"


def test_explicit_limit_price_overrides_the_computed_one():
    payload = build_mleg_payload(_credit_spread(), 1, limit_price=-0.95)
    assert float(payload["limit_price"]) == pytest.approx(-0.95)


def test_rejects_zero_contracts():
    with pytest.raises(ValueError, match="contracts"):
        build_mleg_payload(_credit_spread(), 0)


def test_rejects_more_than_four_legs():
    from dataclasses import replace
    intent = _credit_spread()
    too_many = replace(intent, legs=intent.legs * 3)   # 6 legs
    with pytest.raises(ValueError, match="4 legs"):
        build_mleg_payload(too_many, 1)


def test_rejects_duplicate_leg_symbols():
    from dataclasses import replace
    intent = _credit_spread()
    dupe = replace(intent, legs=(intent.legs[0], intent.legs[0]))
    with pytest.raises(ValueError, match="unique"):
        build_mleg_payload(dupe, 1)


def test_closing_payload_inverts_every_side():
    opening = build_mleg_payload(_credit_spread(), 1)
    closing = closing_payload(_credit_spread(), 1, limit_price=0.50)
    for o, c in zip(opening["legs"], closing["legs"]):
        assert o["side"] != c["side"]


def test_closing_payload_uses_close_position_intents():
    closing = closing_payload(_credit_spread(), 1, limit_price=0.50)
    for leg in closing["legs"]:
        assert leg["position_intent"] in ("buy_to_close", "sell_to_close")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_options_orders.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'options_orders'`

- [ ] **Step 3: Write minimal implementation**

```python
# options_orders.py
"""
Convert a TradeIntent into an Alpaca multi-leg ("mleg") order payload.

Pure functions, no I/O — this is where the broker's shape and sign rules are
encoded, and encoding them wrong is silently expensive:

  * 2-4 legs, symbols unique, time_in_force "day" only
  * no top-level symbol/side on a multi-leg order (Alpaca rejects it)
  * limit_price is signed: POSITIVE = net debit paid, NEGATIVE = net credit
    received. Closing a debit spread therefore takes a negative limit price.
"""
from candidate_builder import TradeIntent

MIN_LEGS, MAX_LEGS = 2, 4

_OPEN_INTENT = {"buy": "buy_to_open", "sell": "sell_to_open"}
_CLOSE_INTENT = {"buy": "buy_to_close", "sell": "sell_to_close"}
_OPPOSITE = {"buy": "sell", "sell": "buy"}


def _validate(intent: TradeIntent, contracts: int) -> None:
    if contracts <= 0:
        raise ValueError(f"contracts must be positive, got {contracts}")
    if not (MIN_LEGS <= len(intent.legs) <= MAX_LEGS):
        raise ValueError(
            f"Alpaca accepts 2 to 4 legs, got {len(intent.legs)}"
        )
    symbols = [leg.quote.symbol for leg in intent.legs]
    if len(set(symbols)) != len(symbols):
        raise ValueError(f"multi-leg order requires unique symbols, got {symbols}")


def _base(contracts: int, limit_price: float) -> dict:
    return {
        "order_class": "mleg",
        "type": "limit",
        "time_in_force": "day",          # the only TIF Alpaca allows for mleg
        "qty": str(contracts),
        "limit_price": f"{limit_price:.2f}",
    }


def build_mleg_payload(
    intent: TradeIntent, contracts: int, limit_price: float | None = None
) -> dict:
    """Opening order. Credit structures get a negative limit price."""
    _validate(intent, contracts)
    # intent.net_credit is per share, positive for a credit. Alpaca wants the
    # signed net price: negative to receive a credit, positive to pay a debit.
    price = -intent.net_credit if limit_price is None else limit_price
    payload = _base(contracts, price)
    payload["legs"] = [
        {
            "symbol": leg.quote.symbol,
            "side": leg.side,
            "ratio_qty": "1",
            "position_intent": _OPEN_INTENT[leg.side],
        }
        for leg in intent.legs
    ]
    return payload


def closing_payload(intent: TradeIntent, contracts: int, limit_price: float) -> dict:
    """Closing order: every side inverted, close intents, caller sets the price."""
    _validate(intent, contracts)
    payload = _base(contracts, limit_price)
    payload["legs"] = [
        {
            "symbol": leg.quote.symbol,
            "side": _OPPOSITE[leg.side],
            "ratio_qty": "1",
            "position_intent": _CLOSE_INTENT[_OPPOSITE[leg.side]],
        }
        for leg in intent.legs
    ]
    return payload
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_options_orders.py -q`
Expected: PASS, 13 tests

- [ ] **Step 5: Run the full suite and commit**

```bash
python3 -m pytest tests/ -q
git add options_orders.py tests/test_options_orders.py
git commit -m "feat: Alpaca multi-leg order payload builder with signed limit prices"
```

---

### Task 3: Options executor with guard integration and fill polling

**Files:**
- Create: `executor_options.py`
- Test: `tests/test_executor_options.py`

**Interfaces:**
- Consumes: `AlpacaCLI` (Task 1), `build_mleg_payload`/`closing_payload` (Task 2), `risk_guard.RiskGuard`/`PortfolioState`/`Verdict`, `journal.Journal`.
- Produces: `OptionsExecutor(cli, guard, journal, clock=time.monotonic)` with
  `submit(intent, state, poll_seconds=30) -> ExecutionResult` and
  `ExecutionResult(status, order_id, filled, reason, contracts)` where
  `status` is one of `"filled" | "pending" | "rejected" | "denied"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_executor_options.py
import os, sys
from datetime import date, timedelta
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from candidate_builder import OptionQuote, build_bull_put_spread
from risk_guard import RiskGuard, PortfolioState, load_risk_config
from journal import Journal
from executor_options import OptionsExecutor

EXPIRY = date.today() + timedelta(days=30)
RISK_YAML = os.path.join(os.path.dirname(__file__), "..", "risk.yaml")


def _q(strike, right, bid, ask, oi=500):
    return OptionQuote(f"SPY-{strike}{right}", "SPY", strike, EXPIRY, right, bid, ask, oi)


def _intent(contracts=1):
    return build_bull_put_spread(_q(445, "p", 3.00, 3.10), _q(440, "p", 2.00, 2.10),
                                 contracts=contracts)


def _flat():
    return PortfolioState(0, 0.0, 0.0, 0.0, 0, {})


class FakeCLI:
    def __init__(self, order=None, statuses=None, raises=False):
        self.posted, self.raises = [], raises
        self._order = order or {"id": "o-1", "status": "accepted"}
        self._statuses = list(statuses or ["filled"])

    def post_order(self, payload):
        if self.raises:
            raise RuntimeError("broker rejected: insufficient options level")
        self.posted.append(payload)
        return self._order

    def get_order(self, order_id):
        status = self._statuses.pop(0) if self._statuses else "filled"
        return {"id": order_id, "status": status, "filled_avg_price": "1.00"}


@pytest.fixture
def executor(tmp_path, monkeypatch):
    monkeypatch.delenv("KILL", raising=False)
    monkeypatch.chdir(tmp_path)
    guard = RiskGuard(load_risk_config(RISK_YAML))
    return lambda cli: OptionsExecutor(cli, guard, Journal(tmp_path / "j.jsonl"))


def test_submits_a_compliant_order(executor):
    cli = FakeCLI()
    result = executor(cli).submit(_intent(), _flat())
    assert result.status == "filled"
    assert len(cli.posted) == 1


def test_denied_order_is_never_sent(executor):
    """Guard DENY must stop the order reaching the broker at all."""
    cli = FakeCLI()
    state = PortfolioState(3, 0.0, 0.0, 0.0, 0, {})   # at max_positions
    result = executor(cli).submit(_intent(), state)
    assert result.status == "denied"
    assert cli.posted == []


def test_downsize_submits_the_approved_quantity(executor):
    """3 contracts risks $1200 > $1000 cap, so 2 must be sent, not 3."""
    cli = FakeCLI()
    result = executor(cli).submit(_intent(contracts=3), _flat())
    assert result.contracts == 2
    assert cli.posted[0]["qty"] == "2"


def test_broker_rejection_is_reported_not_raised(executor):
    cli = FakeCLI(raises=True)
    result = executor(cli).submit(_intent(), _flat())
    assert result.status == "rejected"
    assert "options level" in result.reason


def test_pending_order_is_reported_after_polling(executor):
    cli = FakeCLI(statuses=["new", "new", "new"])
    result = executor(cli).submit(_intent(), _flat(), poll_seconds=0)
    assert result.status == "pending"


def test_kill_switch_blocks_submission(executor, tmp_path):
    (tmp_path / "KILL_SWITCH").touch()
    cli = FakeCLI()
    result = executor(cli).submit(_intent(), _flat())
    assert result.status == "denied"
    assert cli.posted == []


def test_every_outcome_is_journalled(executor, tmp_path):
    cli = FakeCLI()
    ex = executor(cli)
    ex.submit(_intent(), _flat())
    types = [e["type"] for e in ex.journal.entries()]
    assert "verdict" in types
    assert "fill" in types


def test_denied_orders_are_journalled_too(executor, tmp_path):
    """A refusal is a decision and must be as auditable as a trade."""
    cli = FakeCLI()
    ex = executor(cli)
    ex.submit(_intent(), PortfolioState(3, 0.0, 0.0, 0.0, 0, {}))
    assert any(e["type"] == "verdict" for e in ex.journal.entries())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_executor_options.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'executor_options'`

- [ ] **Step 3: Write minimal implementation**

```python
# executor_options.py
"""
Submit defined-risk multi-leg options orders through the Alpaca CLI.

Nothing reaches the broker without a RiskGuard verdict first, and every
outcome — including a refusal — is journalled. Broker rejections are returned
as results, never raised, so one bad order cannot kill a scan cycle.
"""
import logging
import time
from dataclasses import dataclass

from options_orders import build_mleg_payload
from risk_guard import Verdict

logger = logging.getLogger(__name__)

TERMINAL_FILLED = {"filled"}
TERMINAL_DEAD = {"canceled", "expired", "rejected", "done_for_day"}


@dataclass(frozen=True)
class ExecutionResult:
    status: str          # "filled" | "pending" | "rejected" | "denied"
    order_id: str = ""
    filled: bool = False
    reason: str = ""
    contracts: int = 0


class OptionsExecutor:
    def __init__(self, cli, guard, journal, clock=time.monotonic, sleep=time.sleep):
        self.cli, self.guard, self.journal = cli, guard, journal
        self._clock, self._sleep = clock, sleep

    def submit(self, intent, state, poll_seconds: int = 30) -> ExecutionResult:
        verdict = self.guard.evaluate(intent, state)
        self.journal.append("verdict", {
            "underlying": getattr(intent, "underlying", "?"),
            "structure": getattr(intent, "structure", "?"),
            "decision": verdict.decision.value,
            "reason": verdict.reason,
            "approved_contracts": verdict.approved_contracts,
        })

        if verdict.decision == Verdict.DENY:
            logger.info("Order denied by guard: %s", verdict.reason)
            return ExecutionResult("denied", reason=verdict.reason)

        contracts = verdict.approved_contracts
        payload = build_mleg_payload(intent, contracts)
        self.journal.append("proposal", {"payload": payload})

        try:
            order = self.cli.post_order(payload)
        except Exception as e:
            logger.error("Broker rejected the order: %s", e)
            self.journal.append("rejected", {"error": str(e)})
            return ExecutionResult("rejected", reason=str(e), contracts=contracts)

        order_id = str(order.get("id", ""))
        status = self._poll(order_id, poll_seconds)

        if status in TERMINAL_FILLED:
            self.journal.append("fill", {"order_id": order_id, "contracts": contracts})
            return ExecutionResult("filled", order_id, True, "filled", contracts)

        if status in TERMINAL_DEAD:
            self.journal.append("rejected", {"order_id": order_id, "status": status})
            return ExecutionResult("rejected", order_id, reason=status, contracts=contracts)

        self.journal.append("pending", {"order_id": order_id, "status": status})
        return ExecutionResult("pending", order_id, reason=status, contracts=contracts)

    def _poll(self, order_id: str, poll_seconds: int) -> str:
        """Poll until terminal or the window elapses. Returns the last status."""
        deadline = self._clock() + poll_seconds
        status = "new"
        while True:
            try:
                status = str(self.cli.get_order(order_id).get("status", "new"))
            except Exception as e:
                logger.warning("Could not read order %s: %s", order_id, e)
                return status
            if status in TERMINAL_FILLED or status in TERMINAL_DEAD:
                return status
            if self._clock() >= deadline:
                return status
            self._sleep(1)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_executor_options.py -q`
Expected: PASS, 8 tests

- [ ] **Step 5: Run the full suite and commit**

```bash
python3 -m pytest tests/ -q
git add executor_options.py tests/test_executor_options.py
git commit -m "feat: options executor — guard-gated multi-leg submission with fill polling"
```

---

### Task 4: MCP configuration for the read-only data path

Covers the MCP half of "Trading API + MCP server or CLI". Read-only by design: trading tools are not exposed.

**Files:**
- Create: `.mcp.json`
- Modify: `README.md` (add a "Compliance" section)

**Interfaces:**
- Consumes: nothing.
- Produces: a committed MCP server declaration usable by any MCP client.

- [ ] **Step 1: Create the MCP configuration**

```json
{
  "mcpServers": {
    "alpaca": {
      "command": "uvx",
      "args": ["alpaca-mcp-server"],
      "env": {
        "ALPACA_API_KEY": "${ALPACA_API_KEY}",
        "ALPACA_SECRET_KEY": "${ALPACA_SECRET_KEY}",
        "ALPACA_PAPER_TRADE": "true",
        "ALPACA_TOOLSETS": "account,options-data,stock-data"
      }
    }
  }
}
```

- [ ] **Step 2: Verify the server starts and lists options tools**

Run: `ALPACA_PAPER_TRADE=true uvx alpaca-mcp-server --help`
Expected: help text listing `--transport`. If `uvx` is missing, install `uv` first.
Record the output in `docs/COMPLIANCE.md` as evidence.

- [ ] **Step 3: Document the compliance story in README.md**

Add a section stating: orders route through the official Alpaca CLI
(`alpaca_cli.py`, `alpaca api POST /v2/orders`); market data reads are available
through the official MCP server via `.mcp.json` restricted to
`account,options-data,stock-data`; `alpaca-py` is used for analysis and
backtesting. Note explicitly that **no trading toolset is exposed over MCP**, so
the LLM cannot place an order even in principle.

- [ ] **Step 4: Commit**

```bash
git add .mcp.json README.md docs/COMPLIANCE.md
git commit -m "feat: MCP server config (read-only) + documented compliance path"
```

---

### Task 5: Live session entrypoint

Ties preflight, kill switch and execution into one runnable command. This is the task that produces the actual filled order.

**Files:**
- Create: `scripts/run_session.py`
- Modify: `Makefile`
- Test: `tests/test_run_session.py`

**Interfaces:**
- Consumes: everything above, plus `alpaca_data.AlpacaData`, `candidate_builder`, `risk_guard`.
- Produces: `build_candidates(chain, underlying, spot, width=5.0) -> list[TradeIntent]` — the deterministic candidate sweep used by the session and later by the committee.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_session.py
import os, sys
from datetime import date, timedelta
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from candidate_builder import OptionQuote
from run_session import build_candidates

EXPIRY = date.today() + timedelta(days=30)


def _chain():
    quotes = []
    for strike in (430.0, 435.0, 440.0, 445.0, 455.0, 460.0, 465.0, 470.0):
        for right in ("p", "c"):
            quotes.append(OptionQuote(
                f"SPY{EXPIRY:%y%m%d}{right.upper()}{int(strike*1000):08d}",
                "SPY", strike, EXPIRY, right, 2.00, 2.10, 800))
    return quotes


def test_builds_candidates_from_a_chain():
    assert len(build_candidates(_chain(), "SPY", spot=450.0)) > 0


def test_all_candidates_are_defined_risk():
    for intent in build_candidates(_chain(), "SPY", spot=450.0):
        assert intent.is_defined_risk
        assert intent.max_loss < float("inf")


def test_all_candidates_use_allowed_structures():
    allowed = {"bull_put_spread", "bear_call_spread", "iron_condor", "long_straddle"}
    for intent in build_candidates(_chain(), "SPY", spot=450.0):
        assert intent.structure in allowed


def test_candidates_have_unique_ids_for_llm_selection():
    intents = build_candidates(_chain(), "SPY", spot=450.0)
    ids = [f"c{i+1}" for i in range(len(intents))]
    assert len(set(ids)) == len(ids)


def test_empty_chain_yields_no_candidates():
    assert build_candidates([], "SPY", spot=450.0) == []


def test_illiquid_chain_yields_no_candidates():
    """Fail closed: a chain that fails liquidity produces nothing to trade."""
    bad = [OptionQuote(f"X{s}{r}", "SPY", s, EXPIRY, r, 2.00, 2.10, 5)
           for s in (440.0, 445.0) for r in ("p", "c")]
    assert build_candidates(bad, "SPY", spot=450.0) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_run_session.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'run_session'`

- [ ] **Step 3: Write minimal implementation**

```python
#!/usr/bin/env python3
# scripts/run_session.py
"""
Run one trading session cycle: preflight, build candidates, guard, execute.

This is the deterministic spine. The LLM committee (next plan) slots in at the
selection step; until then the session picks the highest-credit candidate,
which makes the execution path independently demoable.
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpaca_cli import AlpacaCLI
from candidate_builder import (
    build_bear_call_spread, build_bull_put_spread, build_long_straddle,
)
from executor_options import OptionsExecutor
from journal import Journal
from risk_guard import PortfolioState, RiskGuard, load_risk_config

logger = logging.getLogger(__name__)
JOURNAL_PATH = Path(__file__).resolve().parent.parent / "logs" / "journal.jsonl"


def build_candidates(chain, underlying: str, spot: float, width: float = 5.0) -> list:
    """Enumerate every defined-risk candidate the chain supports.

    Deterministic and exhaustive: the LLM later picks among these by id and
    can never introduce a strike that this function did not produce.
    """
    if not chain:
        return []

    puts = sorted([q for q in chain if q.right == "p"], key=lambda q: q.strike)
    calls = sorted([q for q in chain if q.right == "c"], key=lambda q: q.strike)
    by_strike_p = {q.strike: q for q in puts}
    by_strike_c = {q.strike: q for q in calls}
    candidates = []

    # Bull put spreads: short strike below spot, long strike `width` lower.
    for short in [q for q in puts if q.strike < spot]:
        long_leg = by_strike_p.get(short.strike - width)
        if long_leg:
            intent = build_bull_put_spread(short, long_leg)
            if intent:
                candidates.append(intent)

    # Bear call spreads: short strike above spot, long strike `width` higher.
    for short in [q for q in calls if q.strike > spot]:
        long_leg = by_strike_c.get(short.strike + width)
        if long_leg:
            intent = build_bear_call_spread(short, long_leg)
            if intent:
                candidates.append(intent)

    # Long straddle at the strike nearest spot, if both legs exist.
    if puts:
        atm = min({q.strike for q in puts}, key=lambda s: abs(s - spot))
        call, put = by_strike_c.get(atm), by_strike_p.get(atm)
        if call and put:
            intent = build_long_straddle(call, put)
            if intent:
                candidates.append(intent)

    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one options session cycle")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--dry-run", action="store_true", help="Never send an order")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    guard = RiskGuard(load_risk_config())
    cli = AlpacaCLI()
    if not cli.available():
        print("  Alpaca CLI not found. Install: brew install alpacahq/tap/cli")
        return 1

    account = cli.get_account()
    positions = cli.list_positions()
    ready, err = guard.startup_checks(
        is_paper=True,
        options_level=int(account.get("options_trading_level", 0) or 0),
        open_positions=len(positions),
        equity=float(account.get("equity", 0) or 0),
    )
    if not ready:
        print(f"  Preflight failed: {err}")
        return 1

    from alpaca_data import AlpacaData
    data = AlpacaData.from_env()
    bars = data.get_stock_bars(args.symbol, days=30)
    if bars.empty:
        print(f"  No bars for {args.symbol} — cannot proceed (fail loud on data).")
        return 1
    spot = float(bars["close"].iloc[-1])

    chain = data.get_option_chain(args.symbol)
    candidates = build_candidates(chain, args.symbol, spot)
    print(f"  {args.symbol} spot ${spot:,.2f} — {len(candidates)} candidate(s)")
    if not candidates:
        print("  ABSTAIN: no candidate passed the liquidity gate.")
        return 0

    # Deterministic placeholder for the committee: best credit per dollar risked.
    chosen = max(candidates, key=lambda c: c.net_credit / max(c.max_loss, 1e-9))
    print(f"  Selected {chosen.structure}: credit ${chosen.net_credit:.2f}, "
          f"max loss ${chosen.max_loss:,.2f}")

    if args.dry_run:
        print("  DRY RUN — no order sent.")
        return 0

    executor = OptionsExecutor(cli, guard, Journal(JOURNAL_PATH))
    state = PortfolioState(len(positions), 0.0, 0.0, 0.0, 0, {})
    result = executor.submit(chosen, state)
    print(f"  Result: {result.status} ({result.reason})")
    return 0 if result.status in ("filled", "pending", "denied") else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_run_session.py -q`
Expected: PASS, 6 tests

- [ ] **Step 5: Add Makefile targets**

```makefile
session-dry:
	python3 scripts/run_session.py --dry-run

session:
	python3 scripts/run_session.py
```

Also add both to `.PHONY` and to the `help` target.

- [ ] **Step 6: Run the full suite and commit**

```bash
python3 -m pytest tests/ -q
git add scripts/run_session.py tests/test_run_session.py Makefile
git commit -m "feat: session entrypoint — preflight, candidate sweep, guarded execution"
```

---

### Task 6: First live paper fill (requires credentials)

**Blocked until:** `make check-account` passes. This task is manual verification, not new code.

- [ ] **Step 1: Verify the account**

Run: `make check-account`
Expected: "Ready to trade." If options level < 3, stop — spreads cannot be placed.

- [ ] **Step 2: Dry run during market hours**

Run: `make session-dry`
Expected: a spot price, a non-zero candidate count, and a selected structure with a finite max loss.

- [ ] **Step 3: Place one real order, smallest size**

Run: `make session`
Expected: `Result: filled` or `pending`. If `rejected`, read the broker text in
the journal — the most likely causes are options level, buying power for the
spread, or a stale limit price.

- [ ] **Step 4: Verify the journal chain**

Run: `make verify-journal`
Expected: `Chain: INTACT` with `verdict`, `proposal` and `fill` entries.

- [ ] **Step 5: Capture evidence**

Save the CLI output and the Alpaca dashboard order screenshot to `docs/evidence/`.
This is the artifact no competitor has demonstrated; it belongs in the README
and the video.

- [ ] **Step 6: Commit the evidence**

```bash
git add docs/evidence/
git commit -m "docs: evidence of first filled multi-leg paper order"
```

---

## Self-Review Notes

**Spec coverage:** This plan implements spec §4.5 (compliance path), §3 (broker
constraints), and the execution half of §4.1. Deferred to Plan 2: the committee
(§4.1 `llm/`, `committee/`), calibration (§4.3), and pre-mortem. Deferred to
Plan 3: dashboard and judge surface (§4.6).

**Interface consistency:** `build_mleg_payload(intent, contracts, limit_price=None)`
is defined in Task 2 and consumed with that exact signature in Task 3.
`AlpacaCLI.post_order`/`get_order` are defined in Task 1 and faked with matching
signatures in Task 3. `build_candidates(chain, underlying, spot, width)` is
defined in Task 5 and is the documented entrypoint the committee will consume in
Plan 2.

**Known gap:** exit monitoring (profit target at 50% of credit, max loss, DTE ≤ 3
forced exit) is intentionally not in this plan — it needs an open position to be
meaningful and is the first task of Plan 2.
